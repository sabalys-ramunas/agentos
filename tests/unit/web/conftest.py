"""A real node for the web contract: the daemon's own services, fake lanes, signed in as the owner.

`client` is the same composition as `agentos dev up --seed`, so a contract route is tested against
real policy, vault, runner, workspaces and brains, and against what a real node really answers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.api import create_login_code
from agentos.api.app import ApiSettings, build_api
from agentos.core.config import NodeConfig
from agentos.dev.node import build_runtime, prepare_home, seed

TOKEN = "t" * 43


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Any]:
    """The runtime behind `client`, for tests that set something up first."""
    prepare_home(tmp_path)
    config = NodeConfig(name="dev", home=tmp_path, mcp_host="127.0.0.1", mcp_port=7999, mcp_token=TOKEN)
    runtime, lanes = build_runtime(config)
    asyncio.run(seed(runtime, lanes))
    yield runtime
    asyncio.run(lanes.shutdown())


@pytest.fixture
def client(node: Any, tmp_path: Path) -> Iterator[TestClient]:
    settings = ApiSettings(home=tmp_path, owner_token=TOKEN, lane_pump_seconds=0, schedule_pump_seconds=0,
                           brain_pump_seconds=0, sse_max_seconds=0.2)
    app = build_api(node, settings)
    with TestClient(app, base_url="http://localhost") as test_client:
        link = create_login_code(tmp_path, base_url="http://localhost")
        code = link.url.split("code=", 1)[1]
        signed_in = test_client.post("/api/session", json={"code": code})
        assert signed_in.status_code == 200, signed_in.text
        test_client.headers["x-agentos-csrf"] = signed_in.json()["csrf"]
        yield test_client
