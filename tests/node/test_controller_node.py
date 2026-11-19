"""The controller and a real node on one box: a Camoufox lane, Xvfb and x11vnc, driven through
the controller's one MCP URL, and its live view relayed as a channel stream.

Run on the node:  AGENTOS_TEST_SLOT=28 scripts/node-test.sh channel tests/node/test_controller_node.py -q
Slot 28: display :280, VNC 5280, viewer 6280, MCP 7680 for the node and 7681 for the controller.
No site is visited: the lane stays on its blank start page.
"""

from __future__ import annotations

import asyncio
import os
import struct
import sys

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from websockets.asyncio.client import connect

from agentos.controller.app import serve_controller
from agentos.controller.config import ControllerConfig
from agentos.core.config import NodeConfig
from agentos.core.models import LaneSpec
from agentos.node import daemon
from agentos.node.channel.enroll import enroll

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb, x11vnc and Camoufox"),
]

DISPLAY_BASE = int(os.environ.get("AGENTOS_TEST_DISPLAY_BASE", "280"))
VNC_BASE = int(os.environ.get("AGENTOS_TEST_VNC_BASE", "5280"))
VIEWER_BASE = int(os.environ.get("AGENTOS_TEST_VIEWER_BASE", "6280"))
NODE_PORT = int(os.environ.get("AGENTOS_TEST_MCP_PORT_BASE", "7680"))
CONTROLLER_PORT = NODE_PORT + 1
LANE = "chan/blank"
AGENT = "aga_node_test_agent_token_value"


async def test_a_real_lane_through_the_controller(tmp_path):
    stop_controller = asyncio.Event()
    started: asyncio.Future = asyncio.get_running_loop().create_future()
    ctl_cfg = ControllerConfig(home=tmp_path / "controller", port=CONTROLLER_PORT, agent_token=AGENT,
                               owner_token="owner-node-test")
    ctl_task = asyncio.create_task(serve_controller(ctl_cfg, stop=stop_controller,
                                                    on_started=lambda port, c: started.set_result(c)))
    controller = await asyncio.wait_for(started, 20)
    config = NodeConfig(home=tmp_path / "node", name="chan-node", mcp_token="node-test-owner-token",
                        mcp_port=NODE_PORT, display_base=DISPLAY_BASE, vnc_base=VNC_BASE, viewer_base=VIEWER_BASE,
                        max_slots=2, max_warm_lanes=1, idle_seconds=3600, ssh_host="mynode")
    (tmp_path / "node").mkdir()
    _, token = controller.create_join_token(controller.org["id"], name="chan-node")
    config = await enroll(config, f"ws://localhost:{CONTROLLER_PORT}", token)
    runtime = daemon.build_runtime(config, ["agentos.policy.service", "agentos.node.channel.service"])
    await runtime.lanes.create(LaneSpec(id=LANE))
    stop_node = asyncio.Event()
    node_task = asyncio.create_task(daemon.serve(config, runtime, stop=stop_node))
    try:
        await asyncio.wait_for(runtime.channel.welcomed.wait(), 30)
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {AGENT}"}, timeout=120) as http,
            Client(streamable_http_client(f"http://localhost:{CONTROLLER_PORT}/mcp", http_client=http),
                   mode="legacy") as client,
        ):
            for _ in range(100):
                if "browser_open" in {t.name for t in (await client.list_tools()).tools}:
                    break
                await asyncio.sleep(0.1)
            opened = await client.call_tool("browser_open", {"on": LANE})
            assert not opened.is_error, opened.content[0].text
            shot = await client.call_tool("browser_screenshot", {"on": LANE})
            assert not shot.is_error, shot.content
            image = next(c for c in shot.content if c.type == "image")
            import base64

            assert base64.b64decode(image.data).startswith(b"\x89PNG")

        url, _ = controller.login_link()
        code = url.split("#code=")[1]
        async with httpx2.AsyncClient(base_url=f"http://localhost:{CONTROLLER_PORT}") as web:
            session = await web.post("/api/session", json={"code": code})
            assert session.status_code == 200
            cookie = session.cookies.get("agentos_controller_session")
        node_id = config.node_id
        ws_url = f"ws://localhost:{CONTROLLER_PORT}/v1/view/{node_id}/{LANE}?mode=view"
        async with connect(ws_url, additional_headers={"Cookie": f"agentos_controller_session={cookie}"},
                           subprotocols=["binary"]) as view:
            greeting = await asyncio.wait_for(view.recv(), 20)
            assert greeting.startswith(b"RFB 003.00"), greeting[:20]
            await view.send(b"RFB 003.008\n")
            security = await asyncio.wait_for(view.recv(), 10)
            assert security[0] >= 1 and 1 in security[1:1 + security[0]]  # x11vnc offers None on localhost
            await view.send(bytes([1]))
            result = await asyncio.wait_for(view.recv(), 10)
            buffered = result[4:]
            assert struct.unpack(">I", result[:4])[0] == 0  # security handshake ok
            await view.send(bytes([1]))  # ClientInit (shared)
            server_init = buffered or await asyncio.wait_for(view.recv(), 10)
            width, height = struct.unpack(">HH", server_init[:4])
            assert width >= 800 and height >= 600
        closed = await runtime.lanes.close(LANE)
        assert closed.state.value == "cold"
    finally:
        stop_node.set()
        await asyncio.wait_for(node_task, 60)
        stop_controller.set()
        await asyncio.wait_for(ctl_task, 20)
