from types import SimpleNamespace

import pytest

from backend.agents.coordinator_core import do_spawn_swarm


@pytest.mark.asyncio
async def test_spawn_skips_challenge_unavailable_to_ai(tmp_path):
    class Client:
        async def fetch_solved_names(self):
            return set()

    policy = tmp_path / "challenge-policy.yml"
    policy.write_text("unavailable_for_ai:\n  - Discord Challenge\n")

    deps = SimpleNamespace(
        ctfd=Client(),
        swarms={},
        swarm_tasks={},
        max_concurrent_challenges=1,
        settings=SimpleNamespace(challenge_policy_file=str(policy)),
    )

    result = await do_spawn_swarm(deps, "Discord Challenge")

    assert result == "Skipped Discord Challenge: it is configured as unavailable for AI."


@pytest.mark.asyncio
async def test_spawn_skips_challenge_solved_by_team(tmp_path):
    class Client:
        async def fetch_solved_names(self):
            return {"Solved Challenge"}

    deps = SimpleNamespace(
        ctfd=Client(),
        swarms={},
        swarm_tasks={},
        max_concurrent_challenges=1,
        settings=SimpleNamespace(challenge_policy_file=str(tmp_path / "missing.yml")),
    )

    result = await do_spawn_swarm(deps, "Solved Challenge")

    assert result == "Skipped Solved Challenge: it was already solved by your team."
