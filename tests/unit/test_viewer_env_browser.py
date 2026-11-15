"""Take over on an environment's own browser (QA 2026-11-15, P2-4), and that account lanes keep every
rule of docs/THREAT-MODEL.md, "Viewer links".

An environment's browser (env/<id>-browser) holds no account, no login and no vault entry, so the
holder of its control link may take over, type, copy and hand back with no panel sign-in, recorded as
"link holder". The same link on an account lane still cannot hand back or type, whatever the
environments service says about other lanes. A freeze still ends the environment browser's link.
"""

from __future__ import annotations

import httpx
import pytest
from test_lanes import make
from test_viewer_app import (  # noqa: F401  (fixtures)
    FakeTraces,
    Node,
    _poke_on_view_changes,
    boot_of,
    handshake,
    serve,
    tcp_upstream,
)
from test_vnc_relay import HANDSHAKE_OUT, FakeVnc, fbur, key, pointer
from websockets.asyncio.client import connect

from agentos.core.models import ControlState, LaneSpec
from agentos.node import viewer_app
from agentos.node.viewer_app import LINK_HOLDER_BY, VIEWER_BY, build_viewer_app

ENV_LANE = "env/eabcdefghjk-browser"
ACCOUNT = "personal/reddit"


class Environments:
    """The environments service as the viewer asks it."""

    def __init__(self, lanes=(ENV_LANE,)):
        self.lanes = set(lanes)
        self.proofs: dict[tuple[str, str, str, str], bytes] = {}

    def is_browser_lane(self, lane_id):
        return lane_id in self.lanes

    def signed_proof(self, workspace, name, proof, token):
        return self.proofs.get((workspace, name, proof, token))


async def _node(tmp_path, monkeypatch, lane: str, envs: Environments):
    monkeypatch.setattr(viewer_app, "EVENT_POLL_SECONDS", 0.05)
    vnc_cm = FakeVnc()
    vnc = await vnc_cm.__aenter__()
    mgr, *_ = make(tmp_path, vnc_base=vnc.port, control_idle_seconds=600)
    await mgr.create(LaneSpec(id=lane))
    await mgr.open(lane)
    _poke_on_view_changes(mgr, vnc)
    app = build_viewer_app(mgr, mgr.config, traces=FakeTraces(), environments=lambda: envs)
    serve_cm = serve(app)
    host = await serve_cm.__aenter__()

    async def close():
        await serve_cm.__aexit__(None, None, None)
        await mgr.shutdown()
        await vnc_cm.__aexit__(None, None, None)

    return Node(mgr, vnc, app, host, None), close


@pytest.fixture
async def env_node(tmp_path, monkeypatch):
    envs = Environments()
    node, close = await _node(tmp_path, monkeypatch, ENV_LANE, envs)
    node.envs = envs
    yield node
    await close()


@pytest.fixture
async def account_node(tmp_path, monkeypatch):
    node, close = await _node(tmp_path, monkeypatch, ACCOUNT, Environments())  # it knows env lanes, not this one
    yield node
    await close()


def client(node) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"origin": f"http://{node.host}"})  # no panel session


def control(lane: str) -> str:
    return f"/view/{lane}/control"


# ---- an environment's browser: the link alone ----------------------------------------------


async def test_the_link_holder_takes_types_and_hands_back_an_environment_browser(env_node):
    node = env_node
    token = node.token("control", lane=ENV_LANE)  # what environment()'s links.browser is
    async with client(node) as c:
        r = await c.post(node.url(control(ENV_LANE), token), json={"action": "take"})
        assert r.status_code == 200, r.text
        assert r.json()["lane"]["control"]["by"] == LINK_HOLDER_BY and "owner_link" not in r.json()
    vnc = node.vnc
    async with connect(node.ws(token, lane=ENV_LANE), subprotocols=["binary"], origin=f"http://{node.host}") as ws:
        await handshake(ws)
        await ws.send(pointer(1, 300, 300) + key(1, 0x62) + fbur())
        await vnc.until(lambda: bytes(vnc.received).endswith(fbur()))
        assert bytes(vnc.received) != HANDSHAKE_OUT + fbur()  # the input went to the browser
    assert node.mgr.get(ENV_LANE).control.last_human_input is not None
    async with client(node) as c:
        r = await c.post(node.url(control(ENV_LANE), token), json={"action": "hand_back", "note": "looked"})
        assert r.status_code == 200, r.text
        assert r.json()["lane"]["control"]["state"] == "agent"


async def test_the_link_holder_hands_back_a_held_environment_browser(env_node):
    node = env_node
    await node.mgr.hold(ENV_LANE, reason="needs_human:captcha")
    token = node.token("control", lane=ENV_LANE)
    async with client(node) as c:
        r = await c.post(node.url(control(ENV_LANE), token), json={"action": "take"})
        assert r.status_code == 200, r.text
        r = await c.post(node.url(control(ENV_LANE), token), json={"action": "hand_back"})
        assert r.status_code == 200, r.text
    assert node.mgr.get(ENV_LANE).control.state == ControlState.AGENT


async def test_the_environment_page_says_the_holder_may_act(env_node):
    node = env_node
    async with httpx.AsyncClient() as c:
        boot = boot_of((await c.get(node.url(f"/view/{ENV_LANE}", node.token("control", lane=ENV_LANE)))).text)
        assert boot["person"] is True and boot["holder"] is True and boot["me"] == LINK_HOLDER_BY
        assert "login" not in boot["paths"]
        watch = boot_of((await c.get(node.url(f"/view/{ENV_LANE}", node.token("view", lane=ENV_LANE)))).text)
        assert watch["person"] is False and watch["holder"] is False  # a view link only watches, here too


async def test_a_view_link_still_cannot_act_on_an_environment_browser(env_node):
    node = env_node
    async with client(node) as c:
        r = await c.post(node.url(control(ENV_LANE), node.token("view", lane=ENV_LANE)), json={"action": "take"})
    assert r.status_code == 403 and node.mgr.get(ENV_LANE).control.state == ControlState.AGENT


async def test_a_freeze_still_ends_the_environment_browsers_link(env_node, monkeypatch):
    node = env_node
    monkeypatch.setattr(node.app.state.viewer, "_frozen", lambda lane_id: True)
    async with client(node) as c:
        r = await c.post(node.url(control(ENV_LANE), node.token("control", lane=ENV_LANE)), json={"action": "take"})
        assert r.status_code == 403
        page = await c.get(node.url(f"/view/{ENV_LANE}", node.token("control", lane=ENV_LANE)))
        assert page.status_code == 403 and "agentos-boot" not in page.text


async def test_a_lane_the_service_no_longer_knows_gets_the_usual_rules(env_node):
    node = env_node
    node.envs.lanes.clear()  # the environment closed: its lane is no environment's browser any more
    token = node.token("control", lane=ENV_LANE)
    async with client(node) as c:
        assert (await c.post(node.url(control(ENV_LANE), token), json={"action": "take"})).status_code == 200
        r = await c.post(node.url(control(ENV_LANE), token), json={"action": "hand_back"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"


# ---- an account lane: today's rules, exactly -----------------------------------------------


async def test_an_account_lanes_link_still_cannot_hand_back_or_type(account_node):
    node = account_node
    token = node.token("control", lane=ACCOUNT)
    async with client(node) as c:
        r = await c.post(node.url(control(ACCOUNT), token), json={"action": "take"})
        assert r.status_code == 200 and r.json()["lane"]["control"]["by"] == VIEWER_BY
        r = await c.post(node.url(control(ACCOUNT), token), json={"action": "hand_back"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
        clip = await c.post(node.url(f"/view/{ACCOUNT}/clipboard", token), json={})
        assert clip.status_code == 403 and clip.json()["error"]["code"] == "presence_required"
    vnc = node.vnc
    async with connect(node.ws(token, lane=ACCOUNT), subprotocols=["binary"], origin=f"http://{node.host}") as ws:
        await handshake(ws)
        await ws.send(pointer(1, 300, 300) + key(1, 0x62) + fbur())
        await vnc.until(lambda: bytes(vnc.received).endswith(fbur()))
        assert bytes(vnc.received) == HANDSHAKE_OUT + fbur()  # nothing but the frame request passed
    assert node.mgr.get(ACCOUNT).control.last_human_input is None
    async with httpx.AsyncClient() as c:
        boot = boot_of((await c.get(node.url(f"/view/{ACCOUNT}", token))).text)
    assert boot["person"] is False and boot["holder"] is False and boot["me"] == VIEWER_BY


async def test_an_account_lane_held_for_a_captcha_is_not_taken_by_the_link(account_node):
    node = account_node
    await node.mgr.hold(ACCOUNT, reason="needs_human:captcha")
    async with client(node) as c:
        r = await c.post(node.url(control(ACCOUNT), node.token("control", lane=ACCOUNT)), json={"action": "take"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"


async def test_a_broken_environments_service_means_the_usual_rules(account_node):
    node = account_node

    class Broken:
        def is_browser_lane(self, lane_id):
            raise RuntimeError("boom")

    node.app.state.viewer.environments = lambda: Broken()
    token = node.token("control", lane=ACCOUNT)
    async with client(node) as c:
        await c.post(node.url(control(ACCOUNT), token), json={"action": "take"})
        r = await c.post(node.url(control(ACCOUNT), token), json={"action": "hand_back"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"


# ---- proof images --------------------------------------------------------------------------


async def test_a_proof_link_opens_its_image_and_nothing_else(env_node):
    node = env_node
    node.envs.proofs[("env", "eabcdefghjk-browser", "pabcdefghjk", "good")] = b"\x89PNG proof"
    async with httpx.AsyncClient() as c:
        r = await c.get(node.url(f"/view/{ENV_LANE}/proof/pabcdefghjk.png", "good"))
        assert r.status_code == 200 and r.content == b"\x89PNG proof" and r.headers["content-type"] == "image/png"
        assert r.headers["cache-control"] == "private, no-store"
        bad = await c.get(node.url(f"/view/{ENV_LANE}/proof/pabcdefghjk.png", "forged"))
        assert bad.status_code == 403 and "not valid or has expired" in bad.json()["error"]["message"]
