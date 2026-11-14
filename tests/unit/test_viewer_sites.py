"""A lane's site list from the daemon and the live view: POST /api/lanes/{ws}/{name}/sites
reaches a warm lane's guard at once, owner links and their one-click allow, and the guard's
block notices on the viewer's event stream. Served by a real uvicorn, as in test_viewer_app."""

from __future__ import annotations

import asyncio

import httpx
from starlette.applications import Starlette
from starlette.routing import Mount
from test_egress import FakeContext, FakeFrame, FakeRequest
from test_viewer_app import LANE, EventReader, node, serve, tokens_at  # noqa: F401 - node is a fixture

from agentos.api.auth import PRESENCE_HEADER, PanelStore, new_login_code
from agentos.core.models import LaneSpec
from agentos.node import egress
from agentos.node.egress import IDENTITY_SITES, LaneGuard
from agentos.node.viewer_actions import change_sites
from agentos.node.viewer_routes import control_api_routes
from agentos.node.viewer_tokens import ViewTokens

EM_DASH = "—"


async def warm_guard(mgr, sites=()) -> tuple[LaneGuard, FakeContext]:
    """The egress guard of the warm lane, bound by the manager as the engine's would be, with the
    lane's stored list."""
    guard = LaneGuard(LANE)
    ctx = FakeContext()
    guard.attach(ctx)
    mgr._lanes[LANE].session.guard = guard
    mgr._bind_guard(LANE, guard)
    guard.settings = None  # the store is never reread here: only the manager's own call updates the guard
    await mgr.set_sites(LANE, list(sites))
    return guard, ctx


def person(mgr) -> dict:
    """The node's CLI as a person: a one-time code it writes into panel.db, beside the node token. Spent
    on the one request, so every call that must be a person's takes a new one."""
    code = new_login_code()
    PanelStore.for_home(mgr.config.home).add_code(code, ttl_seconds=60)
    return {PRESENCE_HEADER: code}


# ---- owner links ---------------------------------------------------------------------------

def test_owner_links_are_signed_and_only_for_control(tmp_path):
    tokens = tokens_at(tmp_path)
    owner = tokens.verify(tokens.issue(LANE, "control", owner=True).token, LANE)
    assert owner.owner and owner.can_control
    assert not tokens.verify(tokens.issue(LANE, "control").token, LANE).owner
    assert not tokens.verify(tokens.issue(LANE, "view", owner=True).token, LANE).owner  # a view link only watches


# ---- the owner's API -----------------------------------------------------------------------

async def test_the_sites_route_updates_a_warm_lane_at_once(node):  # noqa: F811
    guard, _ = await warm_guard(node.mgr, ["reddit.com"])
    app = Starlette(routes=[Mount("/api", routes=control_api_routes(node.mgr, ViewTokens.for_config(node.mgr.config)))])
    async with serve(app) as host, httpx.AsyncClient() as client:
        api = f"http://{host}/api/lanes/personal/reddit/sites"
        r = await client.post(api, json={"op": "add", "sites": ["evil.example"]})  # the node token is no person
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
        assert guard.sites == ("reddit.com",)
        r = await client.post(api, json={"op": "add", "sites": ["*.reddit.com", "frame:www.google.com"]},
                              headers=person(node.mgr))
        assert r.status_code == 200, r.text
        assert r.json() == {"lane": LANE, "sites": ["reddit.com", "*.reddit.com", "frame:www.google.com"],
                            "strict": False, "changed": True, "running": True}
        assert guard.sites == ("reddit.com", "*.reddit.com", "frame:www.google.com")  # no restart, no reread
        r = await client.post(api, json={"op": "add", "sites": ["reddit.com"]}, headers=person(node.mgr))
        assert r.json()["changed"] is False
        r = await client.post(api, json={"op": "remove", "sites": ["nope.example"]}, headers=person(node.mgr))
        assert r.status_code == 400 and "not on the list: nope.example" in r.text
        r = await client.post(api, json={"op": "remove", "sites": ["Reddit.com"]}, headers=person(node.mgr))
        assert r.json()["sites"] == ["*.reddit.com", "frame:www.google.com"]
        r = await client.post(api, json={"op": "add", "sites": [], "strict": True}, headers=person(node.mgr))
        assert r.json()["strict"] is True and guard.strict is True
        r = await client.post(api, json={"op": "set", "sites": []}, headers=person(node.mgr))
        assert r.json()["sites"] == [] and guard.sites == ()
        for body in ({"op": "fly"}, {"sites": "reddit.com"}, {"sites": ["*"]}, {"strict": "yes"}, ["x"]):
            r = await client.post(api, json=body, headers=person(node.mgr))
            assert r.status_code == 400 and EM_DASH not in r.text, body
        r = await client.post(api, content=b"[")
        assert r.status_code == 400
        r = await client.post(f"http://{host}/api/lanes/personal/nope/sites", json={"op": "add", "sites": []},
                              headers=person(node.mgr))
        assert r.status_code == 404
        # the node token (no principal behind the bare bearer check) gets a link that is not an owner's
        link = f"http://{host}/api/lanes/personal/reddit/view-link"
        assert (await client.post(link, json={"mode": "control"})).json()["owner"] is False
        assert (await client.post(link, json={"mode": "control"}, headers=person(node.mgr))).json()["owner"] is True


async def test_change_sites_on_a_cold_lane_writes_the_store(tmp_path):
    from test_lanes import make

    mgr, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id=LANE))
    result = await change_sites(mgr, LANE, {"op": "add", "sites": list(egress.SUGGESTED_SITES["reddit"])}, by="owner")
    assert result["running"] is False and result["sites"][-len(IDENTITY_SITES):] == list(IDENTITY_SITES)
    assert mgr.store.require(LANE).spec.sites == result["sites"]


# ---- the live view ---------------------------------------------------------------------------

async def test_an_owner_link_allows_a_refused_host_in_one_click(node):  # noqa: F811
    guard, _ = await warm_guard(node.mgr, ["reddit.com"])
    owner = node.tokens.issue(LANE, "control", owner=True).token
    async with httpx.AsyncClient() as bare:  # an owner link alone, from a script: no Origin, refused
        r = await bare.post(node.url("/view/personal/reddit/sites", owner), json={"add": "frame:www.google.com"})
        assert r.status_code == 403 and "cross-site" in r.json()["error"]["message"]
    async with httpx.AsyncClient(headers={"origin": f"http://{node.host}"}) as client:
        page = await client.get(node.url("/view/personal/reddit", owner))
        assert '"owner":true' in page.text and "/view/personal/reddit/sites" in page.text
        url = "/view/personal/reddit/sites"
        for token in (node.token("control"), node.token("view")):  # lane_view's links never change the list
            r = await client.post(node.url(url, token), json={"add": "frame:www.google.com"})
            assert r.status_code == 403 and r.json()["error"]["code"] in ("denied", "presence_required")
            assert EM_DASH not in r.text
        r = await client.post(node.url(url, owner), json={"add": "frame:www.google.com"},
                              headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        for body in ({}, {"add": ""}, {"add": ["x"]}, {"add": "*"}):
            assert (await client.post(node.url(url, owner), json=body)).status_code == 400, body
        r = await client.post(node.url(url, owner), json={"add": "frame:www.google.com"})
        assert r.status_code == 200 and r.json()["sites"] == ["reddit.com", "frame:www.google.com"]
        assert guard.sites == ("reddit.com", "frame:www.google.com")
        renewed = await client.post(node.url("/view/personal/reddit/renew", owner))
        assert node.tokens.verify(renewed.json()["token"], LANE).owner  # an open page stays an owner page


async def test_an_off_list_document_while_a_human_has_control_reaches_the_live_view(node):  # noqa: F811
    _, ctx = await warm_guard(node.mgr, ["reddit.com", "*.reddit.com"])
    await node.mgr.take_control(LANE, by="owner")
    top = FakeFrame("https://www.reddit.com/login/")
    frame = FakeFrame("about:blank", parent=top)
    url = node.url("/view/personal/reddit/events", node.token("view"))
    async with httpx.AsyncClient(timeout=5) as client, client.stream("GET", url) as r:
        reader = EventReader(r)
        await reader.until(lambda ev: ev[-1][0] == "hello")
        ctx.emit("request", FakeRequest("https://www.google.com/recaptcha/api2/anchor?k=KEY&co=SECRET",
                                        nav=True, frame=frame))
        events = await reader.until(lambda ev: ev[-1][0] == "egress")
        notice = events[-1][1]
        assert notice["lane"] == LANE and notice["host"] == "www.google.com" and notice["kind"] == "subframe"
        assert notice["rule"] == "noted" and notice["path"] == "/recaptcha/api2/anchor"
        assert notice["command"] == "agentos lane sites personal/reddit add frame:www.google.com"
        assert "SECRET" not in repr(notice) and "KEY" not in repr(notice)
        ctx.emit("request", FakeRequest("https://www.google.com/recaptcha/api2/anchor?k=2", nav=True,
                                        frame=frame))  # the same block again: folded, no second notice
        await asyncio.sleep(0.2)
        assert [e for e in reader.events if e[0] == "egress"] == [events[-1]]
    await asyncio.sleep(0.05)
    assert not node.app.state.viewer.hub._notice_watchers  # the stream stopped watching when it closed
