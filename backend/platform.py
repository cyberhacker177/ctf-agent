"""Platform-neutral CTF client contract and shared result types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SubmitResult:
    status: str  # "correct" | "already_solved" | "incorrect" | "unknown"
    message: str
    display: str


@dataclass(frozen=True)
class InstanceStatus:
    status: str
    connection_info: str = ""
    message: str = ""


@runtime_checkable
class PlatformClient(Protocol):
    """Operations required by the coordinator and solver swarm."""

    platform_name: str

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]: ...

    async def fetch_all_challenges(self) -> list[dict[str, Any]]: ...

    async def fetch_solved_names(self) -> set[str]: ...

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult: ...

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str: ...

    async def start_instance(self, challenge_name: str) -> InstanceStatus: ...

    async def get_instance_status(self, challenge_name: str) -> InstanceStatus: ...

    async def stop_instance(self, challenge_name: str) -> InstanceStatus: ...

    async def close(self) -> None: ...
