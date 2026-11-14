"""Bearer auth on the streamable HTTP app, and one real MCP session over HTTP."""

import asyncio

import httpx2
import pytest
from fakes import FakeInteraction, FakeLaneManager, mcp_port_for_tests
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from agentos.core.config import NodeConfig
from agentos.core.models import LaneSpec
from agentos.mcp.http import BearerAuth
from agentos.mcp.server import build_http_app, build_server
from agentos.node.daemon import build_node_app, node_runtime, serve
from agentos.node.tools import normalize_url

TOKEN = "s3cr3t-token-value"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def config(tmp_path):
    return NodeConfig(home=tmp_path, mcp_token=TOKEN, mcp_port=mcp_port_for_tests())


@pytest.fixture(params=["daemon", "compat"])
def http(request, config):
    if request.param == "daemon":
        app = build_node_app(config, node_runtime(config, FakeLaneManager(config), FakeInteraction()))
    else:
        app = build_http_app(build_server(FakeLaneManager(config), FakeInteraction(), config), config)
    # Host matches what the SSH tunnel sends, so the trusted-Host checks pass.
    with TestClient(app, base_url="http://localhost:7400") as client:
        yield client


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Bearer ", "Bearer wrong", f"Basic {TOKEN}", f"Bearer {TOKEN}x", TOKEN],
)
def test_rejects_missing_or_wrong_token(http, authorization):
    headers = dict(MCP_HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization
    response = http.post("/mcp", json=INIT, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert TOKEN not in response.text


def test_rejects_every_path_without_token(http):
    assert http.get("/mcp").status_code == 401
    assert http.get("/").status_code == 401
    assert http.get("/.well-known/oauth-protected-resource").status_code == 401


@pytest.mark.parametrize("scheme", ["Bearer", "bearer"])
def test_accepts_right_token(http, scheme):
    response = http.post("/mcp", json=INIT, headers=MCP_HEADERS | {"Authorization": f"{scheme} {TOKEN}"})
    assert response.status_code == 200
    assert "agentos" in response.text


def test_empty_token_refuses_to_build():
    with pytest.raises(ValueError, match="mcp_token is empty"):
        BearerAuth(app=None, token="")


async def test_real_session_over_http(config):
    """agentosd as Claude Code sees it: uvicorn, the bearer header, a full MCP session."""
    manager = FakeLaneManager(config)
    await manager.create(LaneSpec(id="personal/reddit"))
    stop = asyncio.Event()
    port: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(serve(config, node_runtime(config, manager, FakeInteraction()), stop=stop,
                                     on_started=port.set_result))
    try:
        url = f"http://localhost:{await asyncio.wait_for(port, 10)}/mcp"

        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http_client:
            transport = streamable_http_client(url, http_client=http_client)
            async with Client(transport, mode="legacy") as client:
                tools = await client.list_tools()
                assert "browser_snapshot" in {t.name for t in tools.tools}
                result = await client.call_tool("browser_snapshot", {"on": "personal/reddit"})
                assert not result.is_error and "[ref=e1]" in result.content[0].text

        async with httpx2.AsyncClient() as anonymous:
            response = await anonymous.post(url, json=INIT, headers=MCP_HEADERS)
            assert response.status_code == 401
    finally:
        stop.set()
        await asyncio.wait_for(task, 15)
    assert manager.started and manager.shut_down


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("reddit.com/r/x", "https://reddit.com/r/x"),
        ("localhost:8080/a", "https://localhost:8080/a"),
        ("http://127.0.0.1:9000/", "http://127.0.0.1:9000/"),
        ("  https://example.com  ", "https://example.com"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected
