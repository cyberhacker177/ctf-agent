"""Hack The Box CTF event client.

The official HTB MCP endpoint is preferred in ``auto`` mode.  Because event
owners may disable MCP, the client can fall back to the experimental web API.
The web API paths are configurable because HTB does not publish that API as a
stable integration contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from base64 import b64encode
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from markdownify import markdownify as html2md

from backend.platform import InstanceStatus, SubmitResult

logger = logging.getLogger(__name__)

DEFAULT_MCP_URL = "https://mcp.hackthebox.ai/v1/ctf/mcp/"
DEFAULT_API_URL = "https://ctf.hackthebox.com/api"
DEFAULT_LOGIN_URL = "https://account.hackthebox.com/api/v1/auth/login"
USER_AGENT = "ctf-agent/0.1"


def _json_value(value: Any) -> Any:
    """Decode MCP text content while preserving non-JSON responses."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _unwrap(data: Any) -> Any:
    """Remove common MCP and HTTP response envelopes."""
    if isinstance(data, dict):
        if data.get("isError"):
            text = " ".join(
                str(item.get("text", ""))
                for item in data.get("content", [])
                if isinstance(item, dict)
            )
            raise RuntimeError(text or "HTB MCP tool returned an error")
        if data.get("structuredContent") is not None:
            return _unwrap(data["structuredContent"])
        if isinstance(data.get("content"), list):
            values = [
                _json_value(item.get("text"))
                for item in data["content"]
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if len(values) == 1:
                return _unwrap(values[0])
            if values:
                return [_unwrap(value) for value in values]
        for key in ("data", "result"):
            if key in data and len(data) <= 4:
                return _unwrap(data[key])
    return data


def _first(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return default


def _nested_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(_first(value, "name", "title", "value", default=""))
    return str(value or "")


def _connection_info(raw: dict[str, Any]) -> str:
    direct = _first(raw, "connection_info", "connectionInfo", "connection", "service")
    if isinstance(direct, str):
        return direct
    container = _first(raw, "container", "instance", "spawn", default=raw)
    if not isinstance(container, dict):
        container = raw
    direct = _first(container, "connection_info", "connectionInfo", "url")
    if direct:
        return str(direct)
    host = _first(container, "ip", "host", "hostname")
    port = _first(container, "port", "port_number")
    if port is None:
        ports = _first(container, "docker_ports", "ports", default=[])
        if isinstance(ports, list) and ports:
            port = ports[0]
    if host and port:
        protocol = str(
            _first(container, "protocol", "scheme", "docker_instance_type", default="nc")
        ).lower()
        return f"{protocol}://{host}:{port}" if protocol in {"http", "https"} else f"nc {host} {port}"
    return str(host or "")


def normalize_htb_challenge(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize HTB challenge payloads to the fields used by metadata.yml."""
    challenge_id = _first(raw, "id", "challenge_id", "challengeId")
    category = _nested_text(
        _first(raw, "category", "challenge_category", "challengeCategory", default="")
    )
    tags = _first(raw, "tags", "skills", default=[]) or []
    if isinstance(tags, str):
        tags = [tags]
    files = _first(raw, "files", "downloads", "download_links", default=[]) or []
    if isinstance(files, (str, dict)):
        files = [files]
    solved = bool(
        _first(raw, "solved", "is_solved", "isSolved", "completed", default=False)
    )
    return {
        "id": challenge_id,
        "_htb_id": challenge_id,
        "name": str(_first(raw, "name", "title", default=f"challenge-{challenge_id}")),
        "category": category,
        "description": str(_first(raw, "description", "content", "details", default="")),
        "value": int(_first(raw, "value", "points", "score", default=0) or 0),
        "connection_info": _connection_info(raw),
        "tags": tags,
        "solves": int(_first(raw, "solves", "solve_count", "solveCount", default=0) or 0),
        "hints": _first(raw, "hints", default=[]) or [],
        "files": files,
        "solved": solved,
        "_instance_supported": bool(
            raw.get("hasDocker")
            or raw.get("hasMachine")
            or _first(raw, "container", "docker", "machine", "instance", "spawn", default=None)
        ),
        "_raw": raw,
    }


def _find_challenge_list(data: Any) -> list[dict[str, Any]]:
    """Locate a challenge collection in version-varying HTB payloads."""
    data = _unwrap(data)
    if isinstance(data, list) and all(isinstance(item, dict) for item in data):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("challenges", "challenge_list", "challengeList", "content"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            found = _find_challenge_list(value)
            if found:
                return found
    for value in data.values():
        if isinstance(value, dict):
            found = _find_challenge_list(value)
            if found:
                return found
    return []


@dataclass
class HTBMCPTransport:
    token: str
    url: str = DEFAULT_MCP_URL
    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    _session_id: str = ""
    _initialized: bool = False
    _tools: list[dict[str, Any]] | None = field(default=None, repr=False)
    _request_id: int = 0

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
        return self._client

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        client = await self._ensure_client()
        self._request_id += 1
        headers = {"Mcp-Session-Id": self._session_id} if self._session_id else {}
        response = await client.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params or {},
            },
            headers=headers,
        )
        response.raise_for_status()
        if response.headers.get("mcp-session-id"):
            self._session_id = response.headers["mcp-session-id"]
        payload = self._decode_response(response)
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"]
            raise RuntimeError(error.get("message", str(error)))
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    @staticmethod
    def _decode_response(response: httpx.Response) -> Any:
        if "text/event-stream" not in response.headers.get("content-type", ""):
            return response.json()
        events = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
        if not events:
            raise RuntimeError("HTB MCP returned an empty event stream")
        return events[-1]

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ctf-agent", "version": "0.1"},
            },
        )
        client = await self._ensure_client()
        headers = {"Mcp-Session-Id": self._session_id} if self._session_id else {}
        response = await client.post(
            self.url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )
        response.raise_for_status()
        self._initialized = True

    async def tools(self) -> list[dict[str, Any]]:
        await self.initialize()
        if self._tools is None:
            result = await self._rpc("tools/list")
            self._tools = result.get("tools", []) if isinstance(result, dict) else []
        return self._tools

    async def call(
        self,
        aliases: tuple[str, ...],
        values: dict[str, Any],
        required_terms: tuple[str, ...],
    ) -> Any:
        tools = await self.tools()
        normalized_aliases = {re.sub(r"[^a-z0-9]", "", value.lower()) for value in aliases}

        def score(tool: dict[str, Any]) -> tuple[int, int]:
            name = str(tool.get("name", "")).lower()
            compact = re.sub(r"[^a-z0-9]", "", name)
            exact = int(compact in normalized_aliases)
            haystack = f"{name} {tool.get('description', '')}".lower()
            terms = sum(term in haystack for term in required_terms)
            return exact, terms

        candidates = sorted(tools, key=score, reverse=True)
        if not candidates or score(candidates[0]) == (0, 0):
            raise NotImplementedError(f"HTB MCP does not expose {'/'.join(required_terms)}")
        tool = candidates[0]
        properties = (tool.get("inputSchema") or {}).get("properties", {})
        arguments: dict[str, Any] = {}
        variants = {
            "event_id": ("event_id", "eventId", "ctf_id", "ctfId", "id"),
            "challenge_id": ("challenge_id", "challengeId", "id"),
            "flag": ("flag", "submission"),
        }
        for logical_name, value in values.items():
            names = variants.get(logical_name, (logical_name,))
            target = next((name for name in names if name in properties), names[0])
            arguments[target] = value
        result = await self._rpc(
            "tools/call",
            {"name": tool["name"], "arguments": arguments},
        )
        return _unwrap(result)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()


@dataclass
class HTBClient:
    event_id: int
    token: str = ""
    cookie: str = ""
    username: str = ""
    password: str = ""
    captcha_token: str = ""
    mode: str = "auto"
    api_url: str = DEFAULT_API_URL
    mcp_url: str = DEFAULT_MCP_URL
    event_path: str = "/ctfs/{event_id}"
    submit_path: str = "/flags/own"
    download_path: str = "/challenges/{challenge_id}/download"
    start_path: str = "/challenges/containers/start"
    status_path: str = "/ctfs/{event_id}/connection-status/{challenge_id}"
    stop_path: str = "/challenges/containers/stop"
    login_path: str = "/auth/login"
    login_url: str = DEFAULT_LOGIN_URL
    instance_ready_timeout_s: float = 15.0
    instance_poll_interval_s: float = 1.0

    platform_name: str = field(default="htb", init=False)
    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    _mcp: HTBMCPTransport | None = field(default=None, repr=False)
    _active_mode: str = ""
    _challenges: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _authenticated: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"auto", "mcp", "http"}:
            raise ValueError("HTB mode must be one of: auto, mcp, http")
        if not self.event_id:
            raise ValueError("An HTB event ID is required")
        if self.mode == "mcp" and not self.token:
            raise ValueError("HTB MCP mode requires --htb-token")
        if self.mode == "http" and not (self.token or self.cookie or (self.username and self.password)):
            raise ValueError("HTB HTTP mode requires token, cookie, or username/password")

    async def _ensure_http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            if self.cookie:
                headers["Cookie"] = self.cookie
            self._client = httpx.AsyncClient(
                base_url=self.api_url.rstrip("/"),
                headers=headers,
                follow_redirects=True,
                timeout=30.0,
            )
        if not self._authenticated and not self.token and not self.cookie and self.username and self.password:
            response = await self._client.post(
                self.login_url if self.login_url.startswith("http") else self.login_path,
                json={"email": self.username, "password": self.password, **({"captcha_token": self.captcha_token} if self.captcha_token else {})},
            )
            response.raise_for_status()
            cookies = response.headers.get_list("set-cookie")
            if cookies:
                self._client.headers["Cookie"] = "; ".join(item.split(";", 1)[0] for item in cookies)
            payload = response.json() if response.content else {}
            token = payload.get("token") or payload.get("access_token") if isinstance(payload, dict) else None
            if token:
                self._client.headers["Authorization"] = f"Bearer {token}"
            self._authenticated = True
        return self._client

    def _ensure_mcp(self) -> HTBMCPTransport:
        if self._mcp is None:
            self._mcp = HTBMCPTransport(token=self.token, url=self.mcp_url)
        return self._mcp

    async def _with_fallback(self, mcp_call, http_call):
        if self.mode == "http" or self._active_mode == "http":
            return await http_call()
        if not self.token:
            logger.info("[HTB] MCP unavailable without a bearer token; using experimental HTTP API")
            self._active_mode = "http"
            return await http_call()
        try:
            result = await mcp_call()
            self._active_mode = "mcp"
            return result
        except Exception as exc:
            if self.mode == "mcp" or not (self.token or self.cookie):
                raise
            logger.warning("[HTB] MCP unavailable; falling back to experimental HTTP API: %s", exc)
            self._active_mode = "http"
            return await http_call()

    async def _http_get(self, path: str) -> Any:
        client = await self._ensure_http_client()
        response = await client.get(path)
        response.raise_for_status()
        return _unwrap(response.json())

    async def _http_post(self, path: str, body: dict[str, Any]) -> Any:
        client = await self._ensure_http_client()
        response = await client.post(path, json=body)
        response.raise_for_status()
        return _unwrap(response.json())

    async def _mcp_event(self) -> Any:
        return await self._ensure_mcp().call(
            ("get_ctf_details", "retrieve_ctf_details", "get_event_details", "get_ctf"),
            {"event_id": self.event_id},
            ("ctf", "detail"),
        )

    async def _http_event(self) -> Any:
        return await self._http_get(self.event_path.format(event_id=self.event_id))

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        data = await self._with_fallback(self._mcp_event, self._http_event)
        challenges = [normalize_htb_challenge(item) for item in _find_challenge_list(data)]
        self._challenges = {challenge["name"]: challenge for challenge in challenges}
        return challenges

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        return [
            {
                "id": challenge["id"],
                "name": challenge["name"],
                "category": challenge["category"],
                "value": challenge["value"],
            }
            for challenge in await self.fetch_all_challenges()
        ]

    async def _challenge(self, name: str) -> dict[str, Any]:
        if name not in self._challenges:
            await self.fetch_all_challenges()
        if name not in self._challenges:
            raise RuntimeError(f'Challenge "{name}" not found in HTB event {self.event_id}')
        return self._challenges[name]

    async def fetch_solved_names(self) -> set[str]:
        challenges = await self.fetch_all_challenges()
        return {challenge["name"] for challenge in challenges if challenge.get("solved")}

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        challenge = await self._challenge(challenge_name)
        challenge_id = challenge["_htb_id"]

        async def mcp_call():
            return await self._ensure_mcp().call(
                ("submit_flag", "submit_challenge_flag"),
                {"event_id": self.event_id, "challenge_id": challenge_id, "flag": flag},
                ("submit", "flag"),
            )

        async def http_call():
            return await self._http_post(
                self.submit_path.format(
                    event_id=self.event_id, challenge_id=challenge_id
                ),
                {
                    "ctf_id": self.event_id,
                    "challenge_id": challenge_id,
                    "flag": b64encode(flag.encode()).decode(),
                    "flag_encoding": "base64",
                },
            )

        data = await self._with_fallback(mcp_call, http_call)
        data = data if isinstance(data, dict) else {"message": str(data)}
        message = str(_first(data, "message", "detail", default=""))
        raw_status = str(_first(data, "status", "result", default="")).lower()
        correct = _first(data, "correct", "success")
        if raw_status in {"correct", "accepted", "success"} or correct is True:
            status = "correct"
        elif raw_status in {"already_solved", "already solved"}:
            status = "already_solved"
        elif raw_status in {"incorrect", "wrong", "failed"} or correct is False:
            status = "incorrect"
        else:
            status = "unknown"
        labels = {
            "correct": "CORRECT",
            "already_solved": "ALREADY SOLVED",
            "incorrect": "INCORRECT",
            "unknown": "UNKNOWN",
        }
        verb = "accepted" if status in {"correct", "already_solved"} else "rejected"
        display = f'{labels[status]} — "{flag}" {verb}. {message}'.strip()
        return SubmitResult(status, message, display)

    async def _download_url(self, challenge: dict[str, Any]) -> str:
        challenge_id = challenge["_htb_id"]

        async def mcp_call():
            return await self._ensure_mcp().call(
                ("get_download_link", "download_challenge", "challenge_download"),
                {"event_id": self.event_id, "challenge_id": challenge_id},
                ("download",),
            )

        async def http_call():
            data = await self._http_get(
                self.download_path.format(
                    event_id=self.event_id, challenge_id=challenge_id
                )
            )
            return data

        data = await self._with_fallback(mcp_call, http_call)
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            return str(_first(data, "url", "download_url", "downloadUrl", "link", default=""))
        return ""

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        name = challenge["name"]
        slug = re.sub(r'[<>:"/\\|?*.\x00-\x1f]', "", name.lower().strip())
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-") or "challenge"
        challenge_dir = Path(output_dir) / slug
        challenge_dir.mkdir(parents=True, exist_ok=True)

        downloads = list(challenge.get("files") or [])
        if not downloads:
            try:
                url = await self._download_url(challenge)
                if url:
                    downloads.append(url)
            except (NotImplementedError, httpx.HTTPStatusError):
                logger.info("No downloadable files for HTB challenge %s", name)

        client = await self._ensure_http_client()
        for index, item in enumerate(downloads, 1):
            url = item
            if isinstance(item, dict):
                url = _first(item, "url", "download_url", "downloadUrl", "link", default="")
            if not url:
                continue
            response = await client.get(str(url))
            response.raise_for_status()
            filename = urlparse(str(url)).path.rstrip("/").rsplit("/", 1)[-1]
            disposition = response.headers.get("content-disposition", "")
            match = re.search(r"filename\\*?=(?:UTF-8''|[\"']?)([^\"';]+)", disposition)
            if match:
                filename = match.group(1)
            filename = Path(filename or f"download-{index}").name
            distfiles = challenge_dir / "distfiles"
            distfiles.mkdir(exist_ok=True)
            (distfiles / filename).write_bytes(response.content)

        try:
            description = html2md(
                challenge.get("description") or "",
                heading_style="atx",
                escape_asterisks=False,
            ).strip()
        except Exception:
            description = challenge.get("description") or ""
        tags = [
            item.get("value") or item.get("name") if isinstance(item, dict) else str(item)
            for item in challenge.get("tags") or []
        ]
        metadata = {
            "name": name,
            "category": challenge.get("category", ""),
            "description": description,
            "value": challenge.get("value", 0),
            "connection_info": challenge.get("connection_info", ""),
            "tags": [tag for tag in tags if tag],
            "solves": challenge.get("solves", 0),
        }
        hints = challenge.get("hints") or []
        if hints:
            metadata["hints"] = hints
        (challenge_dir / "metadata.yml").write_text(
            yaml.dump(metadata, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        return str(challenge_dir)

    async def _instance_action(self, action: str, challenge_name: str) -> InstanceStatus:
        challenge = await self._challenge(challenge_name)
        challenge_id = challenge["_htb_id"]
        aliases = {
            "start": ("start_container", "start_challenge_container"),
            "status": ("container_status", "get_container_status"),
            "stop": ("stop_container", "stop_challenge_container"),
        }
        paths = {
            "start": self.start_path,
            "status": self.status_path,
            "stop": self.stop_path,
        }

        async def mcp_call():
            return await self._ensure_mcp().call(
                aliases[action],
                {"event_id": self.event_id, "challenge_id": challenge_id},
                ((action, "container") if action != "status" else ("container", "status")),
            )

        async def http_call():
            path = paths[action].format(
                event_id=self.event_id, challenge_id=challenge_id
            )
            if action == "status":
                # HTB exposes live instance data alongside the CTF challenge payload.
                await self.fetch_all_challenges()
                refreshed = await self._challenge(challenge_name)
                return refreshed.get("_raw", refreshed)
            return await self._http_post(
                path, {"id": challenge_id}
            )

        data = await self._with_fallback(mcp_call, http_call)
        data = data if isinstance(data, dict) else {"message": str(data)}
        status = str(_first(data, "status", "state", default=action))
        connection = _connection_info(data) or _connection_info(
            data.get("container", {}) if isinstance(data.get("container"), dict) else {}
        )
        message = str(_first(data, "message", "detail", default=""))
        if connection:
            challenge["connection_info"] = connection
        return InstanceStatus(status=status, connection_info=connection, message=message)

    async def start_instance(self, challenge_name: str) -> InstanceStatus:
        challenge = await self._challenge(challenge_name)
        raw = challenge.get("_raw", {})
        if raw.get("docker_online") or raw.get("machine_online"):
            logger.info("[HTB] Instance already running for %s", challenge_name)
            return InstanceStatus(
                status="running",
                connection_info=_connection_info(raw),
                message="HTB instance is already running",
            )
        logger.info("[HTB] Starting instance for %s", challenge_name)
        started = await self._instance_action("start", challenge_name)
        if started.connection_info:
            logger.info("[HTB] Instance ready for %s: %s", challenge_name, started.connection_info)
            return started

        # HTB acknowledges a container start before assigning host/port. Refresh
        # the CTF payload until its asynchronous launcher publishes the endpoint.
        elapsed = 0.0
        while elapsed < self.instance_ready_timeout_s:
            await asyncio.sleep(self.instance_poll_interval_s)
            elapsed += self.instance_poll_interval_s
            await self.fetch_all_challenges()
            refreshed = await self._challenge(challenge_name)
            raw = refreshed.get("_raw", {})
            connection = _connection_info(raw)
            if connection:
                status = InstanceStatus(
                    status="running" if raw.get("docker_online") else started.status,
                    connection_info=connection,
                    message="HTB instance is ready",
                )
                logger.info("[HTB] Instance ready for %s: %s", challenge_name, connection)
                return status
        logger.warning("[HTB] Instance startup timed out for %s", challenge_name)
        return InstanceStatus(
            status="timeout",
            message=(
                "HTB accepted the start request but did not publish a host/port "
                f"within {self.instance_ready_timeout_s:g} seconds"
            ),
        )

    async def get_instance_status(self, challenge_name: str) -> InstanceStatus:
        return await self._instance_action("status", challenge_name)

    async def stop_instance(self, challenge_name: str) -> InstanceStatus:
        return await self._instance_action("stop", challenge_name)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
        if self._mcp:
            await self._mcp.close()
