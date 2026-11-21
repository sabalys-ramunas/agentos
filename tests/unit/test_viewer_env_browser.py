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
from testkit.timing import until
from websockets.asyncio.client import connect

from openajar.core.models import ControlState, LaneSpec
from openajar.node import viewer_app
from openajar.node.viewer_app import LINK_HOLDER_BY, VIEWER_BY, build_viewer_app

ENV_LANE = "env/eabcdefghjk-browser"
ACCOUNT = "personal/reddit"


class Environments:
    """The environments service as the viewer asks it."""

    def __init__(self, lanes=(ENV_LANE,)):
        self.lanes = set(lanes)
        self.proofs: dict[tuple[str, str, str, str], bytes] = {}

    def is_browser_lane(self, lane_id):
        return lane_id in self.lanes

    def lane_environment(self, lane_id):
        return getattr(self, "env", None) if lane_id in self.lanes else None

    def signed_proof(self, workspace, name, proof, token):
        return self.proofs.get((workspace, name, proof, token))

    def watch_lane(self, lane_id, kind="live_view"):
        """A person watching counts as use (openajar.environments.watching): open now, and until it stops."""
        self.watching = [*getattr(self, "watching", []), (lane_id, kind)]

        def stop():
            self.stopped = [*getattr(self, "stopped", []), lane_id]

        return stop


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


@pytest.mark.parametrize(("laptop", "desktop", "drives"), [(False, True, False), (False, False, True),
                                                           (True, True, True)])
async def test_on_a_server_node_the_link_alone_does_not_drive_a_desktop(env_node, laptop, desktop, drives):
    """With the desktop on, an environment's screen has a root terminal in its dock: on a server node the
    control link alone gets the usual rules (a panel sign-in or an owner link); on a laptop it is loopback."""
    from types import SimpleNamespace

    node = env_node
    node.envs.on_laptop = laptop
    node.envs.vm_browsers = lambda: SimpleNamespace(desktops={ENV_LANE: desktop})
    async with client(node) as c:
        r = await c.post(node.url(control(ENV_LANE), node.token("control", lane=ENV_LANE)), json={"action": "take"})
        assert r.status_code == 200, r.text  # taking only stops agents, as on any lane
        r = await c.post(node.url(control(ENV_LANE), node.token("control", lane=ENV_LANE)),
                         json={"action": "hand_back"})
    assert (r.status_code == 200) is drives, r.text
    async with httpx.AsyncClient() as c:
        boot = boot_of((await c.get(node.url(f"/view/{ENV_LANE}", node.token("control", lane=ENV_LANE)))).text)
    assert boot["holder"] is drives


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
        assert page.status_code == 403 and "openajar-boot" not in page.text


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


# ---- who has control, by name ----------------------------------------------------------------


class _Call:
    def __init__(self, client):
        self.client = client


class _Env:
    def __init__(self, agent="an agent", client=None, calls=(), title=None):
        self.agent, self.client, self.calls = agent, client, [_Call(c) for c in calls]
        if title is not None:
            self.title = title


def test_the_agent_is_named_from_its_client_and_never_guessed():
    from openajar.node.viewer_state import agent_name

    assert agent_name("claude-code 2.1.3") == "Claude Code"
    assert agent_name("codex-mcp-client 0.40.0") == "Codex"
    assert agent_name("my_tool") == "My tool"
    assert agent_name("Claude Code") == "Claude Code"
    assert agent_name(None) is None and agent_name("") is None and agent_name("  ") is None
    assert len(agent_name("x" * 200)) <= 32


async def test_the_page_knows_the_agents_name_when_the_environment_does(env_node):
    """The live view says "Claude Code has control": the environment's agent, else its newest call's MCP
    client, else the one that opened it; nothing at all when none said a name."""
    async def boot():
        async with httpx.AsyncClient() as client:
            r = await client.get(env_node.url(f"/view/{ENV_LANE}", env_node.token("view", lane=ENV_LANE)))
        return boot_of(r.text)

    assert (await boot())["agent"] is None  # no environment record: no name
    env_node.envs.env = _Env(client="codex 0.40.0")
    assert (await boot())["agent"] == "Codex"
    env_node.envs.env = _Env(client="codex 0.40.0", calls=["claude-code 2.1.3"])
    assert (await boot())["agent"] == "Claude Code"
    env_node.envs.env = _Env(agent="Claude Code", client="t3-code 1.0")
    assert (await boot())["agent"] == "Claude Code"


async def test_the_page_is_headed_with_the_environments_title(env_node):
    """A person reads the environment's own title ("Launch checklist"), not its lane's id."""
    async def page():
        async with httpx.AsyncClient() as client:
            return await client.get(env_node.url(f"/view/{ENV_LANE}", env_node.token("view", lane=ENV_LANE)))

    r = await page()
    assert boot_of(r.text)["title"] is None and f"<title>{ENV_LANE} · openajar</title>" in r.text
    env_node.envs.env = _Env(title="Launch checklist")
    r = await page()
    assert boot_of(r.text)["title"] == "Launch checklist" and "<title>Launch checklist · openajar</title>" in r.text


def test_the_page_has_a_keyboard_for_touch_and_a_screen_that_outlives_the_browser():
    from pathlib import Path

    root = Path(viewer_app.__file__).parent / "viewer"
    page, js = (root / "page.html").read_text(), (root / "assets" / "viewer.js").read_text()
    assert 'id="btn-keyboard"' in page and 'id="kbd-input"' in page and 'autocorrect="off"' in page
    assert "(pointer: coarse)" in js and "rfb.sendKey(XK.BackSpace, 'Backspace')" in js
    assert "Boolean(state.lane.live)" in js  # a cold lane with a live screen connects
    assert "This is the agent\\'s computer: what you type here, the agent could read." in js  # under Take over
    assert " lane" not in page.split("<body", 1)[1].replace('class="lane', "").replace('id="lane', "")


async def test_an_account_lane_names_no_agent(account_node):
    async with httpx.AsyncClient() as client:
        r = await client.get(account_node.url(f"/view/{ACCOUNT}", account_node.token("view", lane=ACCOUNT)))
    assert boot_of(r.text)["agent"] is None


async def test_an_open_live_view_counts_as_watching_until_it_closes(env_node):
    node = env_node
    token = node.token("view", lane=ENV_LANE)  # watching only: a view link counts too
    async with connect(node.ws(token, lane=ENV_LANE), subprotocols=["binary"], origin=f"http://{node.host}") as ws:
        await handshake(ws)
        assert node.envs.watching == [(ENV_LANE, "live_view")] and not getattr(node.envs, "stopped", [])
    await until(lambda: getattr(node.envs, "stopped", None))
    assert node.envs.stopped == [ENV_LANE]

