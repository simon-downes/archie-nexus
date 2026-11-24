"""Tests for the agent-facing orchestrator gateway."""

import asyncio
import logging
from unittest.mock import patch

import httpx
import pytest
from archie_orchestrator.agent_gateway import AgentGateway, gateway_port, upstream_url
from archie_orchestrator.docker import DockerError, docker_bridge_gateway
from httpx import ASGITransport


async def _client(gateway: AgentGateway) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=gateway), base_url="http://gateway")


@pytest.mark.asyncio
async def test_health_is_forwarded_and_disallowed_routes_are_not():
    requests: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"}, headers={"X-Upstream": "yes"})

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    gateway = AgentGateway("http://main", client=upstream_client)
    async with await _client(gateway) as client:
        health = await client.get("/health")
        rejected = await client.post("/health")
        hidden = await client.get("/api/sessions")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert rejected.status_code == 404
    assert hidden.status_code == 404
    assert [request.url.path for request in requests] == ["/health"]
    await upstream_client.aclose()


@pytest.mark.asyncio
async def test_agent_prefix_forwards_all_methods_query_body_and_headers():
    requests: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            content=request.method.encode(),
            headers={"X-Upstream": "yes", "Connection": "close"},
        )

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    gateway = AgentGateway("http://main", client=upstream_client)
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]
    async with await _client(gateway) as client:
        responses = [
            await client.request(
                method,
                "/api/agent/test?cursor=one",
                content=b"sentinel-body",
                headers={
                    "Authorization": "Bearer sentinel-token",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Request": "kept",
                    "Connection": "close",
                },
            )
            for method in methods
        ]

    assert [response.status_code for response in responses] == [201] * len(methods)
    assert [request.method for request in requests] == methods
    assert all(request.url.path == "/api/agent/test" for request in requests)
    assert all(request.url.params["cursor"] == "one" for request in requests)
    assert all(request.content == b"sentinel-body" for request in requests)
    assert all(request.headers["authorization"] == "Bearer sentinel-token" for request in requests)
    assert all(request.headers["content-type"] == "application/json" for request in requests)
    assert all(request.headers["x-request"] == "kept" for request in requests)
    assert all("connection" not in request.headers for request in requests)
    assert all(response.headers["x-upstream"] == "yes" for response in responses)
    assert all("connection" not in response.headers for response in responses)
    await upstream_client.aclose()


@pytest.mark.asyncio
async def test_rejected_websocket_does_not_forward():
    messages: list[dict] = []
    gateway = AgentGateway(
        "http://main",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("forwarded"))),
    )

    async def receive() -> dict:
        return {"type": "websocket.connect"}

    async def send(message: dict) -> None:
        messages.append(message)

    await gateway({"type": "websocket", "path": "/api/sessions"}, receive, send)
    assert messages == [{"type": "websocket.close", "code": 1008}]
    await gateway._client.aclose()


@pytest.mark.asyncio
async def test_upstream_error_returns_502_without_logging_request_data(caplog):
    async def upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    gateway = AgentGateway("http://main", client=upstream_client)
    with caplog.at_level(logging.WARNING, logger="archie_orchestrator.agent_gateway"):
        async with await _client(gateway) as client:
            response = await client.post(
                "/api/agent/sync",
                content=b"secret-body",
                headers={"Authorization": "Bearer secret-token"},
            )

    assert response.status_code == 502
    assert "secret-body" not in caplog.text
    assert "secret-token" not in caplog.text
    await upstream_client.aclose()


@pytest.mark.asyncio
async def test_agent_sync_passes_through_gateway_to_existing_handler(tmp_path, monkeypatch):
    from archie_orchestrator.app import app
    from archie_shared.agent_sync import token_hash

    from tests.session_fakes import record, serve

    monkeypatch.setenv("ARCHIE_HOME_DIR", str(tmp_path))
    (tmp_path / "config.yaml").write_text("global:\n  model: bedrock-claude-sonnet-4-6\n")
    token = "gateway-agent-token"
    session_id = "gateway-01ktoken0001"
    upstream_client = httpx.AsyncClient(transport=ASGITransport(app=app))
    gateway = AgentGateway("http://main", client=upstream_client)

    with serve(app) as tracker:
        item = record(session_id)
        tracker.store.insert(item, agent_token_hash=token_hash(token))
        tracker._records[session_id] = item
        async with await _client(gateway) as client:
            valid = await client.post(
                "/api/agent/sync",
                content=b"{}",
                headers={"Authorization": f"Bearer {token}"},
            )
            invalid = await client.post(
                "/api/agent/sync",
                content=b"{}",
                headers={"Authorization": "Bearer wrong-token"},
            )

    assert valid.status_code == 200
    assert invalid.status_code == 401
    await upstream_client.aclose()


def test_gateway_port_and_upstream_urls():
    assert gateway_port(7600) == 7601
    assert upstream_url("127.0.0.1", 7600) == "http://127.0.0.1:7600"
    assert upstream_url("::1", 7600) == "http://[::1]:7600"
    with pytest.raises(ValueError, match="65535"):
        gateway_port(65535)


def test_docker_bridge_gateway_reads_and_validates_output(monkeypatch):
    result = type("Result", (), {"returncode": 0, "stdout": "172.17.0.1\n", "stderr": ""})()
    monkeypatch.setattr("archie_orchestrator.docker.subprocess.run", lambda *args, **kwargs: result)
    assert docker_bridge_gateway() == "172.17.0.1"

    bad = type("Result", (), {"returncode": 0, "stdout": "not-an-ip\n", "stderr": ""})()
    monkeypatch.setattr("archie_orchestrator.docker.subprocess.run", lambda *args, **kwargs: bad)
    with pytest.raises(DockerError, match="invalid bridge gateway"):
        docker_bridge_gateway()


def test_gateway_bind_host_is_runtime_specific(monkeypatch):
    from archie_cli import cli

    monkeypatch.setattr(cli.sys, "platform", "darwin")
    assert cli._agent_gateway_host() == "127.0.0.1"
    monkeypatch.setattr(cli.sys, "platform", "linux")
    with patch("archie_orchestrator.docker.docker_bridge_gateway", return_value="172.17.0.1"):
        assert cli._agent_gateway_host() == "172.17.0.1"
    assert cli._upstream_host("127.0.0.1") == "127.0.0.1"
    assert cli._upstream_host("0.0.0.0") == "127.0.0.1"
    assert cli._upstream_host("::") == "::1"


@pytest.mark.asyncio
async def test_server_failure_stops_the_peer_server():
    from archie_cli import cli

    servers = []

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False
            self.config = config
            servers.append(self)

        def install_signal_handlers(self):
            pass

        async def serve(self):
            if self is servers[0]:
                raise RuntimeError("main failed")
            while not self.should_exit:
                await asyncio.sleep(0)

    with patch.object(cli.uvicorn, "Server", FakeServer):
        with pytest.raises(RuntimeError, match="main failed"):
            await cli._serve_servers(
                main_host="127.0.0.1",
                main_port=7600,
                gateway_host="127.0.0.1",
                gateway_port=7601,
                upstream_host="127.0.0.1",
            )
    assert len(servers) == 2
    assert all(server.should_exit for server in servers)
