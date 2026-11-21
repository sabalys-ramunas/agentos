"""A schedule never outlives or exceeds the grant that made it (docs/THREAT-MODEL.md).

Every fire runs under the schedule's grant, so a revoked or expired grant could never act. But the
schedule stayed on: a page watch went on reading the account's page every few minutes, and every
slot was tried again. Now the first tick after the grant ends turns the schedule off, says why, and
does nothing else.

The owner's own grant never ends, so a schedule under it is the owner's word. The node token and the
device credential carry that grant but an agent may hold them, so making such a schedule, turning one
on, and a workspace import that turns its schedules on all need a person. An admin grant still makes
schedules, bound to itself.
"""

from __future__ import annotations

import pytest
from test_node_panel import TOKEN, Node, bearer
from test_presence import Device, Token, refused
from test_runner_safety import LOOK, agent, with_policy

from openajar.core.models import Effect
from openajar.runner.fakes import BASE, OWNER
from openajar.runner.tools import flow_save

WATCH = {"url": "https://x.test/", "locator": {"css": "h1"}, "every_s": 60}


# ---------------------------------------------------------------- the scheduler


async def test_a_schedule_whose_grant_was_revoked_turns_off_and_fires_nothing(tmp_path):
    h, svc, _ = with_policy(tmp_path)
    fv = h.save(LOOK)
    g, ctx = agent(svc, ceiling=Effect.DRAFT)
    s, _ = h.scheduler.add(flow=fv.id, cron="* * * * *", timezone="UTC", principal=ctx.principal)
    svc.revoke(g.id)
    h.clock.advance(61)
    await h.scheduler.tick()
    after = h.store.get_schedule(s.id)
    assert after.enabled is False
    assert after.state["stopped"]["reason"] == "revoked"
    assert h.store.list_runs(schedule_id=s.id) == []


async def test_a_schedule_whose_grant_expired_turns_off(tmp_path):
    h, svc, clock = with_policy(tmp_path)
    fv = h.save(LOOK)
    _, ctx = agent(svc, ceiling=Effect.DRAFT, ttl_s=3600)
    s, _ = h.scheduler.add(flow=fv.id, cron="* * * * *", timezone="UTC", principal=ctx.principal)
    clock.advance(7200)
    h.clock.advance(61)
    await h.scheduler.tick()
    after = h.store.get_schedule(s.id)
    assert after.enabled is False and after.state["stopped"]["reason"] == "expired"
    assert h.store.list_runs(schedule_id=s.id) == []


async def test_a_page_watch_stops_reading_the_page_once_its_grant_is_revoked(tmp_path):
    h, svc, _ = with_policy(tmp_path)
    fv = h.save(LOOK)
    g, ctx = agent(svc, ceiling=Effect.DRAFT)
    s, _ = h.scheduler.add(flow=fv.id, watch=WATCH, principal=ctx.principal)
    svc.revoke(g.id)
    await h.scheduler.tick()
    after = h.store.get_schedule(s.id)
    assert after.enabled is False
    assert "last_checked_at" not in after.state and "next_check_at" not in after.state  # no page was read


async def test_the_owners_schedule_keeps_running(tmp_path):
    h, _, _ = with_policy(tmp_path)
    fv = h.save(LOOK)
    s, _ = h.scheduler.add(flow=fv.id, cron="* * * * *", timezone="UTC")
    h.clock.advance(61)
    await h.scheduler.tick()
    assert h.store.get_schedule(s.id).enabled is True
    assert len(h.store.list_runs(schedule_id=s.id)) == 1


# ---------------------------------------------------------------- the API


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.runtime.get("flows").save({"id": "biz-a.instagram.look", "lane": "biz-a/instagram", "effect": "read",
                                 "steps": [{"type": "navigate", "url": "https://x.test/"}]}, by="owner")
    yield n
    n.close()


BODY = {"flow": "biz-a.instagram.look", "cron": "0 9 * * *", "params": {}}


@pytest.mark.parametrize("who", ["node_token", "device"])
def test_the_owners_token_and_device_never_make_a_schedule_that_runs_as_the_owner(node, who):
    caller = Token(node, TOKEN) if who == "node_token" else Device(node)
    refused(caller.req("POST", "/api/schedules", BODY))
    assert node.runtime.get("runner").scheduler.list() == []


def test_an_admin_grants_schedule_is_bound_to_that_grant(node):
    grant, token = node.policy.issue(subject={"id": "boss-agent"}, workspaces=["biz-a"], ceiling="read",
                                     admin=True)
    r = Token(node, token).req("POST", "/api/schedules", BODY)
    assert r.status_code == 200, r.text
    assert r.json()["grant_id"] == grant.id


def test_a_person_makes_one_and_only_a_person_turns_an_owner_schedule_on(node):
    c = node.client()
    h = node.sign_in(c)
    r = c.post("/api/schedules", json=BODY, headers=h)
    assert r.status_code == 200 and r.json()["grant_id"] == "owner", r.text
    sid = r.json()["id"]
    token = Token(node, TOKEN)
    assert token.req("PATCH", f"/api/schedules/{sid}", {"enabled": False}).status_code == 200  # stopping is fine
    refused(token.req("PATCH", f"/api/schedules/{sid}", {"enabled": True}))
    refused(token.req("POST", f"/api/schedules/{sid}/enable", {}))
    assert node.runtime.get("runner").scheduler.get(sid).enabled is False
    assert token.req("POST", f"/api/schedules/{sid}/disable", {}).status_code == 200
    assert c.patch(f"/api/schedules/{sid}", json={"enabled": True}, headers=h).status_code == 200


def test_an_import_that_turns_its_schedules_on_needs_a_person(node):
    r = node.client().post("/api/workspaces/import?enable_schedules=1", content=b"x",
                           headers={**bearer(TOKEN), "X-Agentos-Passphrase": "pw"})
    refused(r)


# ---------------------------------------------------------------- what a schedule runs

AGENT_HEAD = {"id": LOOK["id"], "lane": LOOK["lane"], "effect": "draft", "steps": [
    {"type": "navigate", "url": f"{BASE}/"},
    {"type": "click", "locator": {"role": "button", "name": "New post"}},
    {"type": "click", "locator": {"role": "button", "name": "Share"}}]}


async def test_an_agent_cannot_change_what_the_owners_schedule_runs(tmp_path):
    """flow_save with a draft ceiling may replace a flow's head on its lane. The owner's schedule ran the
    head as the owner, so the agent's Share click went out under the owner's grant, after its own
    grant was revoked too. A schedule now follows a flow only to versions the owner saved."""
    h, svc, _ = with_policy(tmp_path)
    made_with = h.save(LOOK)
    s, _ = h.scheduler.add(flow=LOOK["id"], cron="* * * * *", timezone="UTC")  # the owner's grant
    _, ctx = agent(svc, ceiling=Effect.DRAFT)
    saved = await flow_save(ctx, definition=AGENT_HEAD)
    assert saved["effect"] == "commit" and h.flows.get(LOOK["id"]).version == saved["version"]
    h.clock.advance(61)
    await h.scheduler.tick()
    await h.runner.run_until_idle()
    (run,) = h.store.list_runs(schedule_id=s.id)
    assert run.flow_version == made_with.version and run.grant_id == "owner"
    assert h.site.state["posts"] == []
    assert h.store.get_schedule(s.id).state["held_back"]["version"] == saved["version"]
    # the owner saves a version in person: the schedule follows it
    mine = h.save({**LOOK, "title": "Look, again"}, by=OWNER)
    h.clock.advance(61)
    await h.scheduler.tick()
    runs = h.store.list_runs(schedule_id=s.id)
    assert len(runs) == 2 and {r.flow_version for r in runs} == {made_with.version, mine.version}
    assert "held_back" not in h.store.get_schedule(s.id).state


def _marked(fid: str) -> dict:
    return {"id": fid, "lane": "biz-a/instagram", "effect": "draft",
            "steps": [{"type": "navigate", "url": "https://x.test/"},
                      {"type": "click", "locator": {"role": "button", "name": "Share"}, "publishes": False}]}


def test_the_device_credential_never_saves_a_flow_as_the_owner_in_person(node):
    """`publishes: false` lowers a flow's effect only in a version the owner saved in person, and only
    such a version is one the owner's schedules follow. The device credential is no person."""
    r = Device(node).req("POST", "/api/flows", {"definition": _marked("biz-a.instagram.device")})
    assert r.status_code == 201, r.text
    fv = node.runtime.get("flows").get("biz-a.instagram.device")
    assert not fv.owner_saved and fv.effect == Effect.COMMIT
    c = node.client()
    h = node.sign_in(c)
    assert c.post("/api/flows", json={"definition": _marked("biz-a.instagram.mine")}, headers=h).status_code == 201
    assert node.runtime.get("flows").get("biz-a.instagram.mine").owner_saved
