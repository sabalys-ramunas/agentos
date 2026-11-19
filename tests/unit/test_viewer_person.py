"""What a viewer link an agent can hold may do (docs/THREAT-MODEL.md, "Viewer links").

An agent gets its link from browser_view and has a shell, so it can open the link itself. Whatever only a
person may do (hand back, clear a hold or a quarantine, take a held lane, add a site, log in) needs a
person on top of the link: an owner link (made in person) or the owner's panel session in the same
browser. Every link also has a hard end that renewing cannot move.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from test_lanes import make
from test_viewer_app import (  # noqa: F401  (fixtures)
    LANE,
    Clock,
    FakeTraces,
    Node,
    _poke_on_view_changes,
    boot_of,
    handshake,
    serve,
    tcp_upstream,
    tokens_at,
)
from test_vnc_relay import HANDSHAKE_OUT, FakeVnc, fbur, key, pointer
from websockets.asyncio.client import connect

from agentos.api.auth import PRESENCE_HEADER, SESSION_COOKIE, PanelStore
from agentos.core.models import ControlState, LaneSpec
from agentos.node import viewer_app, viewer_tokens
from agentos.node.viewer_app import build_viewer_app
from agentos.node.viewer_routes import control_api_routes
from agentos.node.viewer_tokens import ViewTokens

HOLD = "needs_human:captcha"
CONTROL = "/view/personal/reddit/control"


@pytest.fixture
async def node(tmp_path, monkeypatch):
    monkeypatch.setattr(viewer_app, "EVENT_POLL_SECONDS", 0.05)
    async with FakeVnc() as vnc:
        mgr, *_ = make(tmp_path, vnc_base=vnc.port, control_idle_seconds=600)
        await mgr.create(LaneSpec(id=LANE))
        await mgr.open(LANE)
        _poke_on_view_changes(mgr, vnc)
        app = build_viewer_app(mgr, mgr.config, traces=FakeTraces(), login=lambda: FakeLogin())
        async with serve(app) as host:
            yield Node(mgr, vnc, app, host, None)
        await mgr.shutdown()


class FakeLogin:
    def get(self, lane_id):
        return {"lane": lane_id, "state": "none"}

    async def start(self, lane_id, *, by, url=None):
        raise AssertionError("an agent's link must never start a login")


def session(mgr, via: str = "code", member: str | None = None) -> str:
    sid, _ = PanelStore.for_home(mgr.config.home).create_session("owner", 3600, via=via, member_id=member)
    return sid


def client(node, sid: str | None = None) -> httpx.AsyncClient:
    cookies = {SESSION_COOKIE: sid} if sid else None
    return httpx.AsyncClient(headers={"origin": f"http://{node.host}"}, cookies=cookies)


async def held(node) -> None:
    await node.mgr.hold(LANE, reason=HOLD)
    assert node.mgr.get(LANE).control.state == ControlState.PAUSED


# ---- the agent's link alone ----------------------------------------------------------------


async def test_an_agents_link_cannot_hand_back_a_held_lane(node):
    await held(node)
    token = node.token("control")  # what browser_view gives the agent
    async with client(node) as c:
        for action in ("hand_back", "take"):
            r = await c.post(node.url(CONTROL, token), json={"action": action})
            assert r.status_code == 403, (action, r.text)
            assert r.json()["error"]["code"] == "presence_required"
            assert "sign in" in r.json()["error"]["message"].lower()
        r = await c.post(node.url(CONTROL, token), json={"action": "stop_agent"})  # stopping is always fine
        assert r.status_code == 200
    control = node.mgr.get(LANE).control
    assert control.state == ControlState.PAUSED and control.reason == HOLD


async def test_an_agents_link_cannot_hand_back_even_without_a_hold(node):
    token = node.token("control")
    async with client(node) as c:
        r = await c.post(node.url(CONTROL, token), json={"action": "take"})  # taking an unheld lane only stops agents
        assert r.status_code == 200 and r.json()["lane"]["control"]["state"] == "human"
        r = await c.post(node.url(CONTROL, token), json={"action": "hand_back"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
    assert node.mgr.get(LANE).control.state == ControlState.HUMAN


async def test_an_agents_link_cannot_clear_a_quarantine_take_after_a_stop_or_log_in(node):
    token = node.token("control")
    async with client(node) as c:
        await c.post(node.url(CONTROL, token), json={"action": "stop_agent"})
        r = await c.post(node.url(CONTROL, token), json={"action": "take"})  # a stop is a person's to end
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
        for method in ("GET", "POST"):
            r = await c.request(method, node.url("/view/personal/reddit/login", token), json={"action": "start"})
            assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", (method, r.text)
        r = await c.post(node.url("/view/personal/reddit/sites", token), json={"add": "example.com"})
        assert r.status_code == 403
    assert "example.com" not in node.mgr.get(LANE).spec.sites


async def test_an_agents_link_never_types_even_into_a_lane_it_took(node):
    token = node.token("control")
    async with client(node) as c:
        assert (await c.post(node.url(CONTROL, token), json={"action": "take"})).status_code == 200
    assert node.mgr.get(LANE).control.state == ControlState.HUMAN and not node.mgr.get(LANE).control.reason
    vnc = node.vnc
    async with connect(node.ws(token), subprotocols=["binary"], origin=f"http://{node.host}") as ws:
        await handshake(ws)
        await ws.send(pointer(1, 300, 300) + key(1, 0x62) + fbur())
        await vnc.until(lambda: bytes(vnc.received).endswith(fbur()))
        assert bytes(vnc.received) == HANDSHAKE_OUT + fbur()
    assert node.mgr.get(LANE).control.last_human_input is None


async def test_a_frozen_agents_link_shows_no_page(node, monkeypatch):
    viewer = node.app.state.viewer
    monkeypatch.setattr(viewer, "_frozen", lambda lane_id: True)
    async with client(node) as c:
        r = await c.get(node.url("/view/personal/reddit", node.token("control")))
        assert r.status_code == 403 and "agentos-boot" not in r.text
        owner = node.tokens.issue(LANE, "control", owner=True).token
        assert (await c.get(node.url("/view/personal/reddit", owner))).status_code == 200


async def test_an_agents_link_sends_no_input_while_the_lane_is_held(node):
    token = node.token("control")
    async with client(node) as c:
        r = await c.post(node.url(CONTROL, token), json={"action": "take"})
        assert r.status_code == 200
    await node.mgr.hold(LANE, reason=HOLD)  # a captcha while the "human" (the agent's link) has control
    assert node.mgr.get(LANE).control.state == ControlState.HUMAN
    vnc = node.vnc
    async with connect(node.ws(token), subprotocols=["binary"], origin=f"http://{node.host}") as ws:
        await handshake(ws)
        await ws.send(pointer(1, 300, 300) + key(1, 0x62) + fbur())
        await vnc.until(lambda: bytes(vnc.received).endswith(fbur()))
        assert bytes(vnc.received) == HANDSHAKE_OUT + fbur()
    assert node.mgr.get(LANE).control.last_human_input is None


# ---- the owner in the same browser ---------------------------------------------------------


async def test_the_owner_signed_in_takes_and_hands_back_with_the_agents_link(node):
    await held(node)
    token = node.token("control")
    async with client(node, session(node.mgr)) as c:
        r = await c.post(node.url(CONTROL, token), json={"action": "take"})
        assert r.status_code == 200, r.text
        owner = r.json()["owner_link"]  # the page swaps to a person's own link
        assert node.tokens.verify(owner["token"], LANE).owner
        r = await c.post(node.url(CONTROL, token), json={"action": "hand_back"})
        assert r.status_code == 200 and r.json()["lane"]["control"]["state"] == "agent"
    assert node.mgr.get(LANE).control.reason is None


async def test_the_page_gives_a_signed_in_owner_their_own_link(node):
    token = node.token("control")
    async with client(node) as c:
        boot = boot_of((await c.get(node.url("/view/personal/reddit", token))).text)
        assert boot["owner"] is False and boot["token"] == token and boot["person"] is False
    async with client(node, session(node.mgr)) as c:
        boot = boot_of((await c.get(node.url("/view/personal/reddit", token))).text)
        assert boot["owner"] is True and boot["token"] != token
        assert node.tokens.verify(boot["token"], LANE).owner
    async with client(node, session(node.mgr)) as c:  # a view link stays a view link
        boot = boot_of((await c.get(node.url("/view/personal/reddit", node.token("view")))).text)
        assert boot["owner"] is False and boot["mode"] == "view"


async def test_a_device_or_member_session_is_not_a_person(node):
    await held(node)
    token = node.token("control")
    for sid in (session(node.mgr, via="device"), session(node.mgr, member="m_1")):
        async with client(node, sid) as c:
            r = await c.post(node.url(CONTROL, token), json={"action": "hand_back"})
            assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
            boot = boot_of((await c.get(node.url("/view/personal/reddit", token))).text)
            assert boot["owner"] is False
    assert node.mgr.get(LANE).control.reason == HOLD


async def test_a_cookie_from_another_site_is_refused(node):
    await held(node)
    async with httpx.AsyncClient(cookies={SESSION_COOKIE: session(node.mgr)}) as bare:  # no Origin: a script
        r = await bare.post(node.url(CONTROL, node.token("control")), json={"action": "hand_back"})
        assert r.status_code == 403
    assert node.mgr.get(LANE).control.reason == HOLD


async def test_an_owner_link_hands_back_on_its_own(node):
    await held(node)
    token = node.tokens.issue(LANE, "control", owner=True).token
    async with client(node) as c:
        r = await c.post(node.url(CONTROL, token), json={"action": "hand_back"})
        assert r.status_code == 200 and r.json()["lane"]["control"]["state"] == "agent"


# ---- the owner's API ---------------------------------------------------------------------------


async def test_the_control_api_needs_a_person_to_hand_back(tmp_path):
    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id=LANE))
    await mgr.hold(LANE, reason=HOLD)
    tokens = ViewTokens.for_config(mgr.config)
    app = Starlette(routes=[Mount("/api", routes=control_api_routes(mgr, tokens))])
    async with serve(app) as host, httpx.AsyncClient() as c:
        api = f"http://{host}/api/lanes/personal/reddit/control"
        for action in ("hand_back", "take"):  # the node token alone (an agent may hold it)
            r = await c.post(api, json={"action": action})
            assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
        r = await c.post(api, json={"action": "stop_agent"})
        assert r.status_code == 200
        code = "c0de-" + "x" * 20
        PanelStore.for_home(mgr.config.home).add_code(code, 60)  # the node's CLI: a person
        r = await c.post(api, json={"action": "hand_back"}, headers={PRESENCE_HEADER: code})
        assert r.status_code == 200 and r.json()["control"]["state"] == "agent"


# ---- a hard end ---------------------------------------------------------------------------------


def test_every_link_has_a_hard_end_and_an_agents_is_short(tmp_path):
    clock = Clock()
    tokens = tokens_at(tmp_path, clock)
    agent = tokens.issue(LANE, "control", ttl=10**9)
    assert agent.hard_until == clock.now + viewer_tokens.AGENT_LINK_MAX
    assert agent.expires_at <= agent.hard_until
    owner = tokens.issue(LANE, "control", ttl=10**9, owner=True)
    assert owner.hard_until == clock.now + viewer_tokens.OWNER_LINK_MAX
    assert viewer_tokens.AGENT_LINK_MAX <= 3600 < viewer_tokens.OWNER_LINK_MAX <= 12 * 3600
    assert tokens.verify(agent.token, LANE).hard_until == agent.hard_until


async def test_renewing_never_moves_the_hard_end(node):
    clock = Clock(__import__("time").time())
    node.tokens.clock = clock
    try:
        first = node.tokens.issue(LANE, "control")
        token = first.token
        async with client(node) as c:
            while True:
                clock.now += 600
                r = await c.post(node.url("/view/personal/reddit/renew", token))
                if r.status_code != 200:
                    break
                fresh = node.tokens.verify(r.json()["token"], LANE)
                assert fresh.hard_until == first.hard_until and fresh.expires_at <= first.hard_until
                token = r.json()["token"]
                assert clock.now < first.hard_until + 1
            assert r.status_code == 403 and "new one" in r.json()["error"]["message"]
        assert clock.now >= first.hard_until - 600
    finally:
        node.tokens.clock = __import__("time").time


# ---- copying out of the lane ---------------------------------------------------------------------

CLIPBOARD = "/view/personal/reddit/clipboard"
SECRET = "Kopija ąčęėįšųūž € 42"


@pytest.fixture
def lane_clip(monkeypatch):
    """The reader on the node, faked: what it was asked for, and what the lane 'copied'."""
    reads: list[tuple[str, str | None]] = []

    async def read(display, xauthority):
        reads.append((display, xauthority))
        return SECRET

    monkeypatch.setattr(viewer_app.lane_clipboard, "read_clipboard", read)
    return reads


async def taken(node, token: str, sid: str | None = None) -> None:
    async with client(node, sid) as c:
        assert (await c.post(node.url(CONTROL, token), json={"action": "take"})).status_code == 200
    assert node.mgr.get(LANE).control.state == ControlState.HUMAN


async def test_a_view_link_never_reads_the_lanes_clipboard(node, lane_clip):
    await taken(node, node.tokens.issue(LANE, "control", owner=True).token)
    async with client(node, session(node.mgr)) as c:
        r = await c.post(node.url(CLIPBOARD, node.token("view")))
    assert r.status_code == 403 and r.json()["error"]["code"] == "denied" and lane_clip == []
    assert "only watch" in r.json()["error"]["message"]


async def test_no_read_while_an_agent_has_control_even_for_the_owner(node, lane_clip):
    owner = node.tokens.issue(LANE, "control", owner=True).token
    async with client(node) as c:
        r = await c.post(node.url(CLIPBOARD, owner))
    assert r.status_code == 403 and "Take control first" in r.json()["error"]["message"] and lane_clip == []


async def test_an_agents_link_never_reads_it_even_on_a_lane_it_took(node, lane_clip):
    """A clipboard can hold what was never on the screen: a link an agent may hold is not a person."""
    token = node.token("control")
    await taken(node, token)
    for sid in (None, session(node.mgr, via="device"), session(node.mgr, member="m_1")):
        async with client(node, sid) as c:
            r = await c.post(node.url(CLIPBOARD, token))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
    async with httpx.AsyncClient(cookies={SESSION_COOKIE: session(node.mgr)}) as bare:  # no Origin: a script
        assert (await bare.post(node.url(CLIPBOARD, token))).status_code == 403
    assert lane_clip == []


async def test_a_person_in_control_gets_the_text_and_the_log_gets_only_its_length(node, lane_clip, caplog):
    caplog.set_level("DEBUG")
    owner = node.tokens.issue(LANE, "control", owner=True).token
    await taken(node, owner)
    async with client(node) as c:
        r = await c.post(node.url(CLIPBOARD, owner))
    assert r.status_code == 200, r.text
    assert r.json() == {"lane": LANE, "text": SECRET, "chars": len(SECRET)}
    assert r.headers["cache-control"] == "no-store"
    viewer = node.mgr.view(LANE)
    assert lane_clip == [(viewer.display, viewer.xauthority)]
    (row,) = node.app.state.viewer.traces.written
    assert row.kind == "lane_clipboard" and row.lane == LANE and row.principal == viewer_app.VIEWER_BY
    assert row.principal_kind == "human" and row.args == {"chars": len(SECRET)}
    assert SECRET not in row.model_dump_json() and "ąčę" not in row.model_dump_json()
    assert SECRET not in caplog.text and "ąčę" not in caplog.text


async def test_the_owner_signed_in_reads_with_the_agents_link(node, lane_clip):
    token, sid = node.token("control"), session(node.mgr)
    await taken(node, token, sid)
    async with client(node, sid) as c:
        r = await c.post(node.url(CLIPBOARD, token))
    assert r.status_code == 200 and r.json()["text"] == SECRET


async def test_one_link_reads_only_so_often(node, lane_clip, monkeypatch):
    monkeypatch.setattr(viewer_app, "CLIPBOARD_READS", 2)
    owner = node.tokens.issue(LANE, "control", owner=True).token
    await taken(node, owner)
    async with client(node) as c:
        codes = [(await c.post(node.url(CLIPBOARD, owner))).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        other = node.tokens.issue(LANE, "control", owner=True).token  # the limit is the link's own
        assert (await c.post(node.url(CLIPBOARD, other))).status_code == 200
    assert len(lane_clip) == 3


async def test_xclip_missing_tells_the_owner_how_to_install_it(node, monkeypatch):
    from agentos.node.lane_clipboard import INSTALL_XCLIP, XclipMissing

    async def read(display, xauthority):
        raise XclipMissing()

    monkeypatch.setattr(viewer_app.lane_clipboard, "read_clipboard", read)
    owner = node.tokens.issue(LANE, "control", owner=True).token
    await taken(node, owner)
    async with client(node) as c:
        r = await c.post(node.url(CLIPBOARD, owner))
    err = r.json()["error"]
    assert r.status_code == 503 and err["code"] == "xclip_missing"
    assert INSTALL_XCLIP in err["message"] and INSTALL_XCLIP in err["fix"]
    assert node.app.state.viewer.traces.written == []  # nothing was read


async def test_an_environments_browser_offers_no_login(node):
    """An agent environment's browser (env/<id>-browser) holds no account: its live view has no Log in."""
    await node.mgr.create(LaneSpec(id="env/e1-browser"))
    await node.mgr.open("env/e1-browser")
    async with client(node) as c:
        account = boot_of((await c.get(node.url("/view/personal/reddit", node.token("control")))).text)
        env = boot_of((await c.get(node.url("/view/env/e1-browser",
                                            node.token("control", lane="env/e1-browser")))).text)
    assert "login" in account["paths"]
    assert "login" not in env["paths"] and env["lane"]["id"] == "env/e1-browser"
