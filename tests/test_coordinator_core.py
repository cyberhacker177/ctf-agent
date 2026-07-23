from types import SimpleNamespace

import pytest

from backend.agents.coordinator_core import do_spawn_swarm
from backend.platform import InstanceStatus


@pytest.mark.asyncio
async def test_spawn_skips_challenge_unavailable_to_ai(tmp_path):
    class Client:
        async def fetch_solved_names(self):
            return set()

    policy = tmp_path / "challenge-policy.yml"
    policy.write_text("unavailable_for_ai:\n  - Discord Challenge\n")

    deps = SimpleNamespace(
        platform_client=Client(),
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
        platform_client=Client(),
        swarms={},
        swarm_tasks={},
        max_concurrent_challenges=1,
        settings=SimpleNamespace(challenge_policy_file=str(tmp_path / "missing.yml")),
    )

    result = await do_spawn_swarm(deps, "Solved Challenge")

    assert result == "Skipped Solved Challenge: it was already solved by your team."


@pytest.mark.asyncio
async def test_spawn_does_not_launch_agents_without_required_instance_target(tmp_path):
    class Client:
        platform_name = "htb"

        async def fetch_solved_names(self):
            return set()

        async def fetch_all_challenges(self):
            return [{"name": "Remote", "_instance_supported": True}]

        async def start_instance(self, _challenge_name):
            return InstanceStatus(status="timeout", message="no endpoint yet")

    deps = SimpleNamespace(
        platform_client=Client(),
        swarms={},
        swarm_tasks={},
        max_concurrent_challenges=1,
        settings=SimpleNamespace(challenge_policy_file=str(tmp_path / "missing.yml")),
        challenge_dirs={"Remote": str(tmp_path)},
        challenge_metas={},
    )

    result = await do_spawn_swarm(deps, "Remote")

    assert result == "Skipped Remote: required instance is timeout and no connection target is available."
    assert deps.swarms == {}
