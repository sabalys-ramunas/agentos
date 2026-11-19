"""The meters, a lane's queue and its taint in the panel API (agentos.api.routes_meters): who sees
what (the owner, a member, a grant), the owner setting a lane's limits, and clearing taint in person."""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient
from test_api_members import ORIGIN, accept, invite, owner, role
from test_node_panel import TOKEN, Node, bearer

from agentos.api.auth import SESSION_COOKIE
from agentos.core.models import Effect

EM_DASH = "—"
CONFIRM = {"confirm": "clear"}


@pytest.fixture
def panel(tmp_path):
    n = Node(tmp_path)
    n.api_app.state.api.limiter.per_key = n.api_app.state.api.limiter.global_max = 10_000
    yield n
    n.close()


def test_the_owner_sets_a_lanes_limits_and_everyone_who_sees_the_lane_reads_its_meter(panel):
    agent = panel.grant(ceiling="commit")
    boss = owner(panel)
    with TestClient(panel.app, base_url=ORIGIN) as client:
        r = client.put("/api/lanes/biz-a/instagram/meter", json={"limits": {"commits_per_day": 3}},
                       headers=bearer(agent))
        assert r.status_code == 403  # an agent never sets its own limits
        r = client.put("/api/lanes/biz-a/instagram/meter", json={"limits": {"commits_per_day": 3}},
                       headers=bearer(TOKEN))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"  # the node token is no person
        r = boss.req("PUT", "/api/lanes/biz-a/instagram/meter", {"limits": {"commits_per_day": 3, "posts": 1}})
        assert r.status_code == 400
        r = boss.req("PUT", "/api/lanes/biz-a/instagram/meter", {"limits": {"commits_per_day": 3}})
        assert r.status_code == 200, r.text
        assert r.json()["limits"][0] | {"name": "commits_per_day", "value": 3, "left": 3, "state": "ok"} \
            == r.json()["limits"][0] and r.json()["limits_from"] == "owner"
        r = client.get("/api/lanes/biz-a/instagram/meter", headers=bearer(agent))
        assert r.status_code == 200 and r.json()["id"] == "biz-a/instagram"
        r = client.get("/api/lanes/biz-b/facebook/meter", headers=bearer(agent))
        assert r.status_code == 403  # outside its grant
        r = boss.req("PUT", "/api/lanes/biz-a/instagram/meter", {"limits": None})
        assert r.status_code == 200 and r.json()["limits"] == [] and r.json()["limits_from"] is None


def test_meters_show_the_owner_everything_and_a_grant_itself_and_its_lanes(panel):
    panel.policy.set_lane_limits("biz-a/instagram", {"commits_per_day": 1}, by="owner")
    g, agent = panel.policy.issue(subject={"id": "poster"}, workspaces=["biz-a"], ceiling="commit",
                                  budget={"usd_per_day": 2.0})
    other, _ = panel.policy.issue(subject={"id": "other"}, workspaces=["biz-b"], ceiling="read")
    panel.policy.reserve(g, effect=Effect.COMMIT, lane_id="biz-a/instagram")  # a commit that ran
    with TestClient(panel.app, base_url=ORIGIN) as client:
        mine = client.get("/api/meters", headers=bearer(agent)).json()
        assert [m["id"] for m in mine["grants"]] == [g.id]
        assert mine["grants"][0]["limits"][0]["name"] == "usd_per_day"
        assert [m["id"] for m in mine["lanes"]] == ["biz-a/instagram"]
        lane = mine["lanes"][0]
        assert lane["state"] == "cut_off" and lane["usage"]["commits_today"] == 1
        assert lane["explain"].startswith("Browser biz-a/instagram reached its limit of 1 commit a day")
        assert mine["timezone"] and mine["resets_at"] and mine["warn_at"] == [0.8]
        everything = client.get("/api/meters", headers=bearer(TOKEN)).json()
        assert {g.id, other.id} <= {m["id"] for m in everything["grants"]}
        assert {m["id"] for m in everything["lanes"]} == {"personal/reddit", "biz-a/instagram", "biz-b/facebook"}
        assert EM_DASH not in str(everything)


def test_a_member_sees_their_lanes_and_their_own_agents_only(panel):
    boss = owner(panel)
    r = invite(boss, "ada", [role("operator", "biz-a")])
    assert r.status_code == 200, r.text
    ada = accept(panel, r.json()["code"])
    panel.policy.issue(subject={"id": "owners-agent"}, workspaces=["biz-a"], ceiling="read")
    out = ada.get("/api/meters")
    assert out.status_code == 200, out.text
    assert out.json()["grants"] == []
    assert [m["id"] for m in out.json()["lanes"]] == ["biz-a/instagram"]
    assert ada.req("PUT", "/api/lanes/biz-a/instagram/meter", {"limits": {"commits_per_day": 9}}).status_code == 403
    assert ada.get("/api/lanes/biz-b/facebook/queue").status_code == 403


def test_the_queue_says_who_holds_the_lane_and_who_waits(panel):
    agent = panel.grant()
    with TestClient(panel.app, base_url=ORIGIN) as client:
        r = client.get("/api/lanes/biz-a/instagram/queue", headers=bearer(agent))
        assert r.status_code == 200, r.text
        assert r.json() | {"lane": "biz-a/instagram", "claim": None, "queue": []} == r.json()
        assert client.get("/api/lanes/biz-b/facebook/queue", headers=bearer(agent)).status_code == 403


def test_lane_taint_names_grants_for_the_owner_and_only_the_owner_in_person_clears_it(panel):
    g, agent = panel.policy.issue(subject={"id": "reader"}, workspaces=["biz-a"], ceiling="commit")
    ctx = panel.policy.context(g, tool="web_fetch")
    ctx.taint("untrusted", source="https://evil.example/page", kind="web_page", host="evil.example")
    boss = owner(panel)
    with TestClient(panel.app, base_url=ORIGIN) as client:
        seen = client.get("/api/lanes/biz-a/instagram/taint", headers=bearer(agent)).json()
        assert seen["tainted"] and "grant_id" not in seen["families"][0]
        r = client.post(f"/api/grants/{g.id}/taint/clear", json={}, headers=bearer(TOKEN))
        assert r.status_code == 403 and "A token cannot clear it" in r.json()["error"]["message"]
        r = client.post(f"/api/grants/{g.id}/taint/clear", json={}, headers=bearer(agent))
        assert r.status_code == 403
    full = boss.get("/api/lanes/biz-a/instagram/taint").json()
    assert full["tainted"] and full["families"][0]["grant_id"] == g.id
    assert full["families"][0]["follows"][0]["host"] == "evil.example"
    r = boss.post(f"/api/grants/{g.id}/taint/clear")
    assert r.status_code == 400 and r.json()["error"]["message"] == 'Type "clear" to confirm.'
    r = boss.post(f"/api/grants/{g.id}/taint/clear", CONFIRM)
    assert r.status_code == 200 and r.json()["origins_cleared"] >= 1, r.text
    assert boss.get("/api/lanes/biz-a/instagram/taint").json()["tainted"] is False
    assert boss.post("/api/grants/nope123/taint/clear", CONFIRM).status_code == 404
    traces = panel.policy.traces.query(kind="taint_cleared", limit=10)[0]
    assert [t.grant_id for t in traces] == [g.id]


def test_with_members_clearing_taint_needs_a_fresh_passkey_check(panel):
    g, _ = panel.policy.issue(subject={"id": "reader"}, workspaces=["biz-a"], ceiling="commit")
    panel.policy.context(g, tool="web_fetch").taint("untrusted", source="https://x.example/", kind="web")
    boss = owner(panel)
    r = invite(boss, "ada", [role("admin", "biz-a")])
    ada = accept(panel, r.json()["code"])
    assert ada.post(f"/api/grants/{g.id}/taint/clear", CONFIRM).status_code == 403  # an admin never clears it
    owner_passkey(panel, boss)
    r = boss.post(f"/api/grants/{g.id}/taint/clear", CONFIRM)
    assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required"
    panel.store.verified(panel.store.get_session(boss.client.cookies[SESSION_COOKIE]))
    assert boss.post(f"/api/grants/{g.id}/taint/clear", CONFIRM).status_code == 200


def sessions(panel, sql: str) -> None:
    with panel.store._lock:
        panel.store._db.execute(sql)
        panel.store._db.commit()


def owner_passkey(panel, boss) -> None:
    opts = boss.post("/api/me/passkeys/options").json()["publicKey"]
    r = boss.post("/api/me/passkeys", {"credential": boss.auth.create(opts), "device_label": "laptop"})
    assert r.status_code == 200, r.text
    sessions(panel, "UPDATE sessions SET last_verified_at = last_verified_at - 3600")  # that check is old now


def test_without_members_clearing_taint_still_needs_a_fresh_in_person_step(panel):
    g, _ = panel.policy.issue(subject={"id": "reader"}, workspaces=["biz-a"], ceiling="commit")
    panel.policy.context(g, tool="web_fetch").taint("untrusted", source="https://x.example/", kind="web")
    url = f"/api/grants/{g.id}/taint/clear"
    boss = owner(panel)  # no members, no passkey: a fresh sign-in with a one-time code and the typed word
    sessions(panel, "UPDATE sessions SET created_at = created_at - 3600")
    r = boss.post(url, CONFIRM)
    assert r.status_code == 403 and "agentos panel login" in r.json()["error"]["message"]
    sessions(panel, f"UPDATE sessions SET created_at = {time.time()}")  # signed in again just now
    assert boss.post(url, {"confirm": "yes"}).status_code == 400
    assert boss.post(url, CONFIRM).status_code == 200
    owner_passkey(panel, boss)  # with a passkey, a fresh code sign-in is not enough: the passkey check is
    r = boss.post(url, CONFIRM)
    assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required"


def test_a_device_credential_never_clears_taint(panel):
    g, _ = panel.policy.issue(subject={"id": "reader"}, workspaces=["biz-a"], ceiling="commit")
    boss = owner(panel)
    sessions(panel, "UPDATE sessions SET via = 'device'")
    r = boss.post(f"/api/grants/{g.id}/taint/clear", CONFIRM)
    assert r.status_code == 403 and "device credential" in r.json()["error"]["message"]


def test_lane_taint_shows_a_grant_only_its_own_family(panel):
    mine, agent = panel.policy.issue(subject={"id": "mine"}, workspaces=["biz-a"], ceiling="commit")
    other, _ = panel.policy.issue(subject={"id": "other"}, workspaces=["biz-a"], ceiling="commit")
    panel.policy.context(other, tool="web_fetch").taint("untrusted", source="https://x.example/", kind="web_page",
                                                        host="other.example")
    with TestClient(panel.app, base_url=ORIGIN) as client:
        seen = client.get("/api/lanes/biz-a/instagram/taint", headers=bearer(agent)).json()
        assert seen["tainted"] is False and seen["families"] == []
        panel.policy.context(mine, tool="web_fetch").taint("untrusted", source="https://y.example/",
                                                           kind="web_page", host="mine.example")
        seen = client.get("/api/lanes/biz-a/instagram/taint", headers=bearer(agent)).json()
        assert [f["follows"][0]["host"] for f in seen["families"]] == ["mine.example"]
        everything = client.get("/api/lanes/biz-a/instagram/taint", headers=bearer(TOKEN)).json()
        assert {f["grant_id"] for f in everything["families"]} == {mine.id, other.id}


def test_the_queue_says_which_claim_is_the_callers(panel):
    seen: list = []
    panel.lanes.queue_info = lambda lane, viewer=None: seen.append(viewer) or {"claim": None, "queue": []}
    agent = panel.grant()
    with TestClient(panel.app, base_url=ORIGIN) as client:
        assert client.get("/api/lanes/biz-a/instagram/queue", headers=bearer(agent)).status_code == 200
    assert seen[0] is not None and seen[0].grant_id


def test_the_owner_sets_a_lanes_posting_pace_and_its_timezone_source_shows(panel):
    """AUD-13: the pace through the API: the preset, a custom pace, the zone and its source."""
    agent = panel.grant(ceiling="commit")
    boss = owner(panel)
    with TestClient(panel.app, base_url=ORIGIN) as client:
        r = client.put("/api/lanes/biz-a/instagram/pace", json={"preset": True}, headers=bearer(agent))
        assert r.status_code == 403  # an agent never sets its own pace
        r = client.put("/api/lanes/biz-a/instagram/pace", json={"preset": True}, headers=bearer(TOKEN))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"  # the node token is no person
        r = boss.req("PUT", "/api/lanes/biz-a/instagram/pace", {"pace": {"active_hours": ["8-22"]}})
        assert r.status_code == 400 and EM_DASH not in r.text
        r = boss.req("PUT", "/api/lanes/biz-a/instagram/pace", {"preset": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pace"] == {"commits_per_day": 3, "min_gap_minutes": 45, "active_hours": ["08:00-22:00"]}
        assert body["paced"] and body["zone"]["source"] in ("pin", "node") and "next_allowed_at" in body
        r = client.put("/api/pace/workspaces/biz-a", json={"timezone": "Asia/Tokyo"}, headers=bearer(TOKEN))
        assert r.status_code == 200 and r.json() == {"workspace": "biz-a", "timezone": "Asia/Tokyo"}
        r = client.put("/api/pace/workspaces/biz-a", json={"timezone": "Mars/Base"}, headers=bearer(TOKEN))
        assert r.status_code == 400
        r = boss.req("PUT", "/api/lanes/biz-a/instagram/pace",
                     {"pace": {"min_gap_minutes": 10, "timezone": "Europe/London"}})
        assert r.json()["zone"] == r.json()["zone"] | {"timezone": "Europe/London", "source": "lane"}
        r = client.get("/api/lanes/biz-a/instagram/pace", headers=bearer(agent))
        assert r.status_code == 200 and r.json()["pace"] == {"min_gap_minutes": 10, "timezone": "Europe/London"}
        r = client.get("/api/lanes/biz-b/facebook/pace", headers=bearer(agent))
        assert r.status_code == 403
        r = boss.req("PUT", "/api/lanes/biz-a/instagram/pace", {"pace": None})
        assert r.status_code == 200 and r.json()["pace"] is None and not r.json()["paced"]
