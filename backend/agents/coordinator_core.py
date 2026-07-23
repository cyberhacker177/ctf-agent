"""Shared coordinator tool logic — called by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import yaml

from backend.deps import CoordinatorDeps
from backend.platform import InstanceStatus
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND

logger = logging.getLogger(__name__)


def unavailable_for_ai(settings: object) -> set[str]:
    """Return manually excluded challenge names from the optional policy file."""
    policy_path = Path(getattr(settings, "challenge_policy_file", "challenge-policy.yml"))
    if not policy_path.exists():
        return set()
    try:
        data = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
        unavailable = data.get("unavailable_for_ai", [])
        return {str(name) for name in unavailable}
    except (OSError, yaml.YAMLError, TypeError) as exc:
        logger.warning("Could not read challenge policy %s: %s", policy_path, exc)
        return set()


async def do_fetch_challenges(deps: CoordinatorDeps) -> str:
    challenges = await deps.platform_client.fetch_all_challenges()
    solved = await deps.platform_client.fetch_solved_names()
    unavailable = unavailable_for_ai(deps.settings)
    result = [
        {
            "name": ch.get("name", "?"),
            "category": ch.get("category", "?"),
            "value": ch.get("value", 0),
            "solves": ch.get("solves", 0),
            "status": (
                "SOLVED" if ch.get("name") in solved
                else "unavailable_for_ai" if ch.get("name") in unavailable
                else "unsolved"
            ),
            "description": (ch.get("description") or "")[:200],
        }
        for ch in challenges
    ]
    return json.dumps(result, indent=2)


async def do_get_solve_status(deps: CoordinatorDeps) -> str:
    solved = await deps.platform_client.fetch_solved_names()
    swarm_status = {name: swarm.get_status() for name, swarm in deps.swarms.items()}
    return json.dumps({"solved": sorted(solved), "active_swarms": swarm_status}, indent=2)


async def do_spawn_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    # Retire ALL finished swarms before checking capacity
    finished = [
        name for name, swarm in deps.swarms.items()
        if swarm.cancel_event.is_set()
        or (name in deps.swarm_tasks and deps.swarm_tasks[name].done())
    ]
    for name in finished:
        del deps.swarms[name]
        deps.swarm_tasks.pop(name, None)

    # Never allocate an AI swarm after the team has already solved it.
    try:
        if challenge_name in await deps.platform_client.fetch_solved_names():
            if swarm := deps.swarms.get(challenge_name):
                swarm.kill()
            logger.info("[Coordinator] Challenge skipped (already solved): %s", challenge_name)
            return f"Skipped {challenge_name}: it was already solved by your team."
    except Exception as exc:
        logger.debug("Could not verify solve status before spawning %s: %s", challenge_name, exc)

    unavailable = unavailable_for_ai(deps.settings)
    if challenge_name in unavailable:
        logger.info("[Coordinator] Challenge skipped (unavailable for AI): %s", challenge_name)
        return f"Skipped {challenge_name}: it is configured as unavailable for AI."

    active_count = len(deps.swarms)
    if active_count >= deps.max_concurrent_challenges:
        return f"At capacity ({active_count}/{deps.max_concurrent_challenges} challenges running). Wait for one to finish."

    if challenge_name in deps.swarms:
        return f"Swarm still running for {challenge_name}"

    # Auto-pull challenge if needed
    if challenge_name not in deps.challenge_dirs:
        challenges = await deps.platform_client.fetch_all_challenges()
        ch_data = next((c for c in challenges if c.get("name") == challenge_name), None)
        if not ch_data:
            return f"Challenge '{challenge_name}' not found on the configured platform"
        output_dir = str(Path(deps.challenges_root))
        ch_dir = await deps.platform_client.pull_challenge(ch_data, output_dir)
        deps.challenge_dirs[challenge_name] = ch_dir
        deps.challenge_metas[challenge_name] = ChallengeMeta.from_yaml(Path(ch_dir) / "metadata.yml")

    # HTB container challenges need a remote instance before solvers can connect.
    # Other platforms can explicitly report that lifecycle operations are unsupported.
    ch_data = next(
        (ch for ch in await deps.platform_client.fetch_all_challenges() if ch.get("name") == challenge_name), None
    )
    if ch_data and ch_data.get("_instance_supported"):
        try:
            platform_name = getattr(deps.platform_client, "platform_name", "platform").upper()
            logger.info("[%s] Starting instance for %s", platform_name, challenge_name)
            instance: InstanceStatus = await deps.platform_client.start_instance(challenge_name)
            if instance.connection_info:
                meta = deps.challenge_metas[challenge_name]
                meta.connection_info = instance.connection_info
                metadata_path = Path(deps.challenge_dirs[challenge_name]) / "metadata.yml"
                metadata = yaml.safe_load(metadata_path.read_text()) or {}
                metadata["connection_info"] = instance.connection_info
                metadata_path.write_text(
                    yaml.dump(metadata, allow_unicode=True, default_flow_style=False, sort_keys=False),
                    encoding="utf-8",
                )
                logger.info(
                    "[%s] Instance ready for %s: %s",
                    platform_name,
                    challenge_name,
                    instance.connection_info,
                )
            else:
                logger.warning(
                    "[%s] Instance unavailable for %s (%s): %s",
                    platform_name,
                    challenge_name,
                    instance.status,
                    instance.message or "no connection target was provided",
                )
                return (
                    f"Skipped {challenge_name}: required instance is {instance.status} "
                    f"and no connection target is available."
                )
        except NotImplementedError:
            logger.info("[Platform] Instance lifecycle unsupported for %s", challenge_name)
        except Exception as exc:
            logger.warning("[Platform] Instance startup failed for %s: %s", challenge_name, exc)
            return f"Skipped {challenge_name}: required instance could not be started ({exc})."

    from backend.agents.swarm import ChallengeSwarm

    swarm = ChallengeSwarm(
        challenge_dir=deps.challenge_dirs[challenge_name],
        meta=deps.challenge_metas[challenge_name],
        platform_client=deps.platform_client,
        cost_tracker=deps.cost_tracker,
        settings=deps.settings,
        model_specs=deps.model_specs,
        no_submit=deps.no_submit,
        coordinator_inbox=deps.coordinator_inbox,
    )
    deps.swarms[challenge_name] = swarm

    async def _run_and_cleanup() -> None:
        result = await swarm.run()
        # Flag already submitted/confirmed by solver's submit_fn — just record the result
        if result and result.status == FLAG_FOUND:
            deps.results[challenge_name] = {
                "flag": result.flag,
                "submit": "DRY RUN" if deps.no_submit else "confirmed by solver",
            }

    task = asyncio.create_task(_run_and_cleanup(), name=f"swarm-{challenge_name}")
    deps.swarm_tasks[challenge_name] = task
    return f"Swarm spawned for {challenge_name} with {len(deps.model_specs)} models"


async def do_check_swarm_status(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    return json.dumps(swarm.get_status(), indent=2)


async def do_submit_flag(deps: CoordinatorDeps, challenge_name: str, flag: str) -> str:
    if deps.no_submit:
        return f'DRY RUN — would submit "{flag.strip()}" for {challenge_name}'
    try:
        result = await deps.platform_client.submit_flag(challenge_name, flag)
        return result.display
    except Exception as e:
        return f"submit_flag error: {e}"


async def do_kill_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    swarm.kill()
    return f"Swarm for {challenge_name} cancelled"


async def do_bump_agent(deps: CoordinatorDeps, challenge_name: str, model_spec: str, insights: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec} in {challenge_name}"
    solver.bump(insights)
    return f"Bumped {model_spec} on {challenge_name}"


async def do_read_solver_trace(deps: CoordinatorDeps, challenge_name: str, model_spec: str, last_n: int = 20) -> str:
    """Read the last N trace events from a solver's JSONL log."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec}"
    trace_path = getattr(solver, "tracer", None)
    if not trace_path:
        return "No tracer on solver"
    path = trace_path.path if hasattr(trace_path, "path") else str(trace_path)
    try:
        lines = Path(path).read_text().strip().split("\n")
        recent = lines[-last_n:]
        summary = []
        for line in recent:
            try:
                d = json.loads(line)
                t = d.get("type", "?")
                if t == "tool_call":
                    args_str = str(d.get("args", ""))[:100]
                    summary.append(f"step {d.get('step','?')} CALL {d.get('tool','?')}: {args_str}")
                elif t == "tool_result":
                    result_str = str(d.get("result", ""))[:100]
                    summary.append(f"step {d.get('step','?')} RESULT {d.get('tool','?')}: {result_str}")
                elif t in ("finish", "error", "bump", "turn_failed"):
                    summary.append(f"** {t}: {json.dumps({k:v for k,v in d.items() if k != 'ts'})}")
                elif t == "usage":
                    summary.append(f"usage: in={d.get('input_tokens',0)} out={d.get('output_tokens',0)} cost=${d.get('cost_usd',0):.4f}")
                else:
                    summary.append(f"{t}: {str(d)[:80]}")
            except Exception:
                summary.append(line[:100])
        return "\n".join(summary)
    except FileNotFoundError:
        return f"Trace file not found: {path}"
    except Exception as e:
        return f"Error reading trace: {e}"


async def do_broadcast(deps: CoordinatorDeps, challenge_name: str, message: str) -> str:
    """Broadcast a message to all solvers working on a challenge."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    await swarm.message_bus.broadcast(message)
    return f"Broadcast to all solvers on {challenge_name}"
