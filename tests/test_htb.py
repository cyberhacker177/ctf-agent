import json

import httpx
import pytest

from backend.htb import HTBClient, normalize_htb_challenge


def test_normalize_htb_payload():
    result = normalize_htb_challenge({
        "id": 7, "title": "Web", "category": {"name": "Web"},
        "points": 25, "container": {"host": "10.0.0.2", "port": 8080},
        "tags": [{"name": "xss"}], "isSolved": True,
    })
    assert result["name"] == "Web"
    assert result["category"] == "Web"
    assert result["connection_info"] == "http://10.0.0.2:8080" or result["connection_info"] == "nc 10.0.0.2 8080"
    assert result["solved"] is True


def test_normalize_live_htb_docker_fields():
    result = normalize_htb_challenge(
        {
            "id": 40784,
            "name": "Dynamic Paths",
            "hasDocker": 1,
            "docker_online": 1,
            "docker_instance_type": "TCP",
            "hostname": "154.57.164.75",
            "docker_ports": [31449],
        }
    )
    assert result["_instance_supported"] is True
    assert result["connection_info"] == "nc 154.57.164.75 31449"


@pytest.mark.asyncio
async def test_http_fallback_discovery_and_submit():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ctfs/1434":
            return httpx.Response(200, json={"data": {"challenges": [{"id": 7, "name": "Web", "category": "web", "solved": False}]}})
        if request.url.path == "/flags/own":
            return httpx.Response(200, json={"status": "correct", "message": "ok"})
        return httpx.Response(404)

    client = HTBClient(event_id=1434, token="token", mode="http", api_url="https://mock")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mock")
    assert (await client.fetch_challenge_stubs())[0]["name"] == "Web"
    result = await client.submit_flag("Web", "HTB{ok}")
    assert result.status == "correct"
    await client.close()


@pytest.mark.asyncio
async def test_http_credentials_login_sets_cookie():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            assert json.loads(request.content) == {"email": "u", "password": "p"}
            return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "session=abc; Path=/"})
        return httpx.Response(200, json={"challenges": []})

    client = HTBClient(event_id=1434, username="u", password="p", mode="http", api_url="https://mock", login_url="https://mock/auth/login")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mock")
    await client.fetch_all_challenges()
    assert client._client.headers["Cookie"] == "session=abc"
    await client.close()


@pytest.mark.asyncio
async def test_http_start_instance_uses_htb_id_payload():
    event_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal event_calls
        if request.url.path == "/ctfs/1434":
            event_calls += 1
            if event_calls == 1:
                return httpx.Response(200, json={"challenges": [{"id": 40784, "name": "Instance", "hasDocker": 1, "docker_online": 0}]})
            return httpx.Response(200, json={"challenges": [{"id": 40784, "name": "Instance", "hasDocker": 1, "docker_online": 1, "hostname": "10.10.10.10", "docker_ports": [1337]}]})
        if request.url.path == "/challenges/containers/start":
            assert json.loads(request.content) == {"id": 40784}
            return httpx.Response(200, json={"status": "starting"})
        return httpx.Response(404)

    client = HTBClient(event_id=1434, token="token", mode="http", api_url="https://mock")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mock")
    status = await client.start_instance("Instance")
    assert status.status == "running"
    assert status.connection_info == "nc 10.10.10.10 1337"
    await client.close()
