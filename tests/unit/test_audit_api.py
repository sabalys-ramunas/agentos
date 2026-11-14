"""GET /api/audit/replay (scope-checked like Activity), run screenshots, and read-only share links,
against a node with the real policy service."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime

import pytest
from test_api_members import browser, join, owner, role
from test_node_panel import TOKEN, Node, bearer
from test_policy_support import Clock

from agentos.api.auth import RateLimiter
from agentos.api.zone import node_zone
from agentos.audit.errors import ShareInvalid
from agentos.audit.service import AuditService, today
from agentos.core.errors import InvalidRequest
from agentos.policy.traces import TraceEvent

LANE = "biz-a/instagram"
OWNER = bearer(TOKEN)


class FakeRunner:
    def __init__(self, runs: dict):
        self.runs = runs

    def get(self, run_id):
        return self.runs[run_id]

    def describe(self, run):
        return run


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)
    shot = n.config.home / "runner" / "shots" / "run_1" / "step-final-a1.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"\x89PNG proof")
    n.runtime.register("runner", FakeRunner({"run_1": {
        "id": "run_1", "flow": "instagram-post", "status": "succeeded", "lane": LANE, "summary": "posted",
        "proof": {"url": "https://www.instagram.com/p/abc/", "readbacks": [{"passed": True}],
                  "screenshot": "runner/shots/run_1/step-final-a1.png"}}}))
    yield n
    n.close()


def day() -> str:
    return datetime.now(UTC).date().isoformat()


def node_day() -> str:
    return today(node_zone(None), time.time())  # POST /api/audit/shares takes no tz: its day is the node's


def call(node: Node, grant, *, lane: str = LANE, tool: str = "flow_run", run_id: str | None = None,
         summary: str = "Posted to instagram.com as biz-a") -> str:
    """A tool call the way policy.wrap traces it: start, an event inside it, end."""
    t = node.policy.traces
    start = t.write(TraceEvent(kind="tool_start", tool=tool, lane=lane, effect="commit", status="started",
                               principal=grant.subject.id, principal_kind="agent", grant_id=grant.id,
                               grant_path=grant.path, args={"caption": "hello", "on": lane}, run_id=run_id))
    t.write(TraceEvent(kind="page", tool=tool, lane=None, status="ok", summary="clicked Share", parent_id=start.id,
                       grant_id=grant.id, grant_path=grant.path, args={"page_url": "https://www.instagram.com/"}))
    t.write(TraceEvent(kind="tool_end", tool=tool, lane=lane, effect="commit", status="ok", summary=summary,
                       principal=grant.subject.id, principal_kind="agent", grant_id=grant.id, grant_path=grant.path,
                       parent_id=start.id, run_id=run_id, duration_ms=812))
    return start.id


def agent(node: Node, name: str, workspace: str = "biz-a", note: str | None = None):
    g, tok = node.policy.issue(subject={"id": name}, workspaces=[workspace], ceiling="commit", note=note)
    return g, tok


def replay(client, headers, lane: str = LANE):
    return client.get(f"/api/audit/replay?lane={lane}&day={day()}&tz=UTC", headers=headers)


def test_replay_shape_and_chain_status(node):
    g, _ = agent(node, "claude-code", note="weekly posts")
    call(node, g, run_id="run_1")
    with node.client() as client:
        r = replay(client, OWNER)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"lane", "day", "timezone", "from", "to", "chain", "steps", "truncated"}
        (step,) = body["steps"]
        assert step["kind"] == "call" and step["tool"] == "flow_run" and step["status"] == "ok"
        assert step["args"] == {"caption": "hello", "on": LANE} and step["duration_ms"] == 812
        assert step["grant"] == {"id": g.id, "label": "claude-code, up to commit on biz-a: weekly posts"}
        assert step["principal"] == {"kind": "agent", "id": "claude-code"}
        assert [e["summary"] for e in step["events"]] == ["clicked Share"]
        assert step["run"]["proof"]["url"] == "https://www.instagram.com/p/abc/"
        assert step["screenshot"] == "/api/audit/runs/run_1/screenshot"
        assert step["chain"]["status"] == "unprovable" and body["chain"]["status"] == "unprovable"
        png = client.get(step["screenshot"], headers=OWNER)
        assert png.status_code == 200 and png.content == b"\x89PNG proof"

        node.policy.traces.checkpoint()
        body = replay(client, OWNER).json()
        assert body["steps"][0]["chain"]["status"] == "ok" and body["chain"]["status"] == "ok"
        assert body["chain"]["proven_through"]["seq"] >= body["steps"][0]["seq"]

        raw = sqlite3.connect(node.config.home / "traces.db", isolation_level=None)
        raw.execute("DROP TRIGGER traces_append_only")
        raw.execute("UPDATE traces SET summary = 'Read a post' WHERE kind = 'tool_end' AND tool = 'flow_run'")
        raw.close()
        body = replay(client, OWNER).json()
        assert body["steps"][0]["summary"] == "Read a post" and body["steps"][0]["chain"]["status"] == "tampered"
        assert body["chain"]["status"] == "tampered"
        assert any("was edited after it was written" in f["text"] for f in body["chain"]["findings"])


def test_the_day_index_lists_only_lanes_the_caller_may_see(node):
    a, tok_a = agent(node, "worker-a")
    call(node, a)
    b, _ = agent(node, "worker-b", workspace="biz-b")
    call(node, b, lane="biz-b/facebook")
    with node.client() as client:
        assert [x["lane"] for x in client.get("/api/audit/replay?tz=UTC", headers=OWNER).json()["lanes"]] == \
            ["biz-a/instagram", "biz-b/facebook"]
        mine = client.get("/api/audit/replay?tz=UTC", headers=bearer(tok_a)).json()
        assert mine["lanes"] == [{"lane": LANE, "rows": 2}]


def test_a_grant_sees_only_its_own_family_and_lanes(node):
    a, tok_a = agent(node, "worker-a")
    b, tok_b = agent(node, "worker-b")
    _c, tok_c = agent(node, "worker-c", workspace="biz-b")
    call(node, a, summary="from a")
    call(node, b, summary="from b", run_id="run_1")
    with node.client() as client:
        assert [s["summary"] for s in replay(client, OWNER).json()["steps"]] == ["from a", "from b"]
        assert [s["summary"] for s in replay(client, bearer(tok_a)).json()["steps"]] == ["from a"]
        assert [s["summary"] for s in replay(client, bearer(tok_b)).json()["steps"]] == ["from b"]
        refused = replay(client, bearer(tok_c))
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "denied"
        assert client.get("/api/audit/runs/run_1/screenshot", headers=bearer(tok_c)).status_code == 403
        assert client.get("/api/audit/verify", headers=bearer(tok_a)).status_code == 403  # owner only


def test_a_member_sees_every_principal_in_their_workspaces(node):
    a, _ = agent(node, "worker-a")
    call(node, a)
    b, _ = agent(node, "worker-b", workspace="biz-b")
    call(node, b, lane="biz-b/facebook")
    boss = owner(node)
    va = join(node, boss, "va", [role("viewer")])
    r = va.get(f"/api/audit/replay?lane={LANE}&day={day()}&tz=UTC")
    assert r.status_code == 200 and len(r.json()["steps"]) == 1
    assert va.get(f"/api/audit/replay?lane=biz-b/facebook&day={day()}&tz=UTC").status_code == 403


def test_share_links(node):
    g, tok = agent(node, "claude-code")
    call(node, g, run_id="run_1")
    with node.client() as client:
        assert client.post("/api/audit/shares", json={"lane": LANE, "day": node_day()},
                           headers=bearer(tok)).status_code == 403  # owner only
        person = node.sign_in(client)  # the owner in person (a panel session): a share link is never made by a token
        too_long = client.post("/api/audit/shares", json={"lane": LANE, "day": node_day(), "ttl": "8d"},
                               headers=person)
        assert too_long.status_code == 400
        made = client.post("/api/audit/shares", json={"lane": LANE, "day": node_day(), "screenshots": True},
                           headers=person).json()
        assert made["token"].startswith("ash_") and made["url"].endswith(f"/shared#token={made['token']}")
        assert made["api_url"].endswith(f"/api/audit/shared/{made['token']}")
        hours = (datetime.fromisoformat(made["expires_at"]) - datetime.now(UTC)).total_seconds() / 3600
        assert 23.9 < hours <= 24  # 24 h by default
        anon = browser(node)
        seen = anon.get(f"/api/audit/shared/{made['token']}")
        assert seen.status_code == 200, seen.text
        body = seen.json()
        assert body["share"]["id"] == made["id"] and body["lane"] == LANE
        step, made_it = body["steps"]  # the call, and the owner making this very link (a panel action on the lane)
        assert made_it["tool"] == "panel.audit.share" and made_it["grant"]["label"] == "the owner"
        assert step["grant"] == {"id": None, "label": "claude-code, up to commit on biz-a"}  # no ids leave the node
        assert step["screenshot"] == f"/api/audit/shared/{made['token']}/runs/run_1/screenshot"
        assert anon.get(step["screenshot"]).content == b"\x89PNG proof"
        # each view is logged, in the action log and on the share
        views = node.policy.traces.query(kind="share_view")[0]
        assert len(views) == 1 and views[0].args["share"] == made["id"]
        assert client.get("/api/audit/shares", headers=OWNER).json()[0]["views"] == 1
        # a forged or edited token is refused
        assert anon.get(f"/api/audit/shared/{made['token'][:-3]}abc").status_code == 404
        assert client.delete(f"/api/audit/shares/{made['id']}", headers=OWNER).json()["state"] == "revoked"
        gone = anon.get(f"/api/audit/shared/{made['token']}")
        assert gone.status_code == 404 and gone.json()["error"]["code"] == "share_invalid"
        assert "revoked" in gone.json()["error"]["message"]


def test_a_share_link_is_made_by_a_person_never_by_a_token(node):
    """A share link opens with no sign-in, so the node makes one only for the owner in person. The node
    token and an admin grant get presence_required and nothing is written; the signed-in owner gets one."""
    g, _ = agent(node, "claude-code")
    call(node, g, run_id="run_1")
    admin = node.grant(admin=True)
    body = {"lane": LANE, "day": node_day()}
    with node.client() as client:
        for caller in (OWNER, bearer(admin)):
            r = client.post("/api/audit/shares", json=body, headers=caller)
            assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
        assert client.get("/api/audit/shares", headers=OWNER).json() == []
        person = node.sign_in(client)
        made = client.post("/api/audit/shares", json=body, headers=person)
        assert made.status_code == 200 and made.json()["token"].startswith("ash_"), made.text
        assert len(client.get("/api/audit/shares", headers=OWNER).json()) == 1


def test_a_trace_share_shows_only_that_trace_and_expires(node, tmp_path):
    g, _ = agent(node, "claude-code")
    first = call(node, g, summary="the one")
    call(node, g, summary="another")
    clock = Clock(time.time())
    svc = AuditService(node.policy.traces, policy=node.policy, home=node.config.home, clock=clock)
    made = svc.share_create(trace_id=first, ttl="1h")
    body = svc.share_view(made["token"])
    assert body["step"]["summary"] == "the one" and "steps" not in body
    assert body["step"]["screenshot"] is None  # screenshots are off unless the owner asks
    clock.advance(3601)
    with pytest.raises(ShareInvalid, match="expired"):
        svc.share_open(made["token"])
    with pytest.raises(InvalidRequest):
        svc.share_create(trace_id=first, lane=LANE, day=day())


def test_the_replay_as_an_html_file_is_for_the_owner_in_person(node):
    """GET /api/audit/replay.html: one self-contained page anyone who gets it can read, so, like a share
    link, only the owner in person downloads it."""
    g, tok = agent(node, "claude-code")
    call(node, g, run_id="run_1")
    url = f"/api/audit/replay.html?lane={LANE}&day={day()}&tz=UTC"
    with node.client() as client:
        assert client.get(url, headers=bearer(tok)).status_code == 403
        assert client.get(url, headers=OWNER).json()["error"]["code"] == "presence_required"
        person = node.sign_in(client)
        r = client.get(url, headers=person)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/html") and "attachment" in r.headers["content-disposition"]
        assert "default-src 'none'" in r.headers["content-security-policy"]
        assert "<script" not in r.text and g.id not in r.text and "data:image" not in r.text
        assert "Posted to instagram.com as biz-a" in r.text
        # the arguments' values stay out unless asked for (the file cannot be taken back)
        assert "hello" not in r.text and "names only" in r.text
        r = client.get(url + "&include_args=1", headers=person)
        assert r.status_code == 200 and "hello" in r.text and "names only" not in r.text


def test_a_screenshot_kept_after_a_commit_is_served_to_who_may_see_the_call(node):
    import hashlib

    from agentos.audit.captures import CAPTURE_KIND, capture_file

    a, tok_a = agent(node, "worker-a")
    _b, tok_b = agent(node, "worker-b")
    start = call(node, a)
    shot = b"\x89PNG\r\n\x1a\nkept"
    sha = "sha256:" + hashlib.sha256(shot).hexdigest()
    path = capture_file(node.config.home, sha, "png")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(shot)
    row = node.policy.traces.write(TraceEvent(
        kind=CAPTURE_KIND, tool="browser_act", lane=LANE, status="ok", parent_id=start, grant_id=a.id,
        grant_path=a.path, args={"sha256": sha, "url": "https://x.example/", "title": "t", "bytes": len(shot),
                                 "format": "png"}))
    with node.client() as client:
        step = next(s for s in replay(client, OWNER).json()["steps"] if s["id"] == start)
        assert step["capture"]["sha256"] == sha and step["screenshot"] == f"/api/audit/captures/{row.id}"
        assert client.get(step["screenshot"], headers=OWNER).content == shot
        assert client.get(step["screenshot"], headers=bearer(tok_a)).content == shot
        assert client.get(step["screenshot"], headers=bearer(tok_b)).status_code == 404  # another family's call
        assert client.get(f"/api/audit/captures/{start}", headers=OWNER).status_code == 404  # not a capture row
