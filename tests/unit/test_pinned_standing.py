"""Pins which standing approval covers a call (openajar.policy.standing.consume_standing): fixed
standing rows, then each call against a fresh copy of them, and a script of calls that uses them
up (max_uses, per_day across a day boundary). Each case pins the id used, or None."""

from __future__ import annotations

import itertools
import json

import pytest
from pinned import check, outcome

from openajar.policy.approval_binding import content_digest
from openajar.policy.approvals import ApprovalStore
from openajar.policy.store import DB
from openajar.policy.trace_models import EventBus

NOW = 1_800_000_000.0
LANE = "biz-a/instagram"
FLOW = "flow:biz-a.instagram.story@v1"
CONTENT = {"flow": "biz-a.instagram.story", "version": "v1", "lane": LANE, "params": {"caption": "hi"}}
TEMPLATE = {"flow": "biz-a.instagram.story", "version": "v1", "lane": LANE, "params": {}}
CONNECTOR = {"operation": "instagram.post", "contract": "c1", "surface": LANE, "acts_as": None, "params": {}}


def standing(sid: str, **kw) -> dict:
    row = {"id": sid, "effects": ["commit"], "workspaces": [], "lanes": [LANE], "grant_id": None, "max_uses": None,
           "uses": 0, "per_day": None, "day": None, "day_uses": 0, "starts_at": NOW - 10, "expires_at": NOW + 3600,
           "created_by": "owner", "created_at": NOW - 10, "revoked_at": None, "note": None, "targets": [],
           "contents": [], "shape": 0, "issuer": None, "withdrawn_at": None, "confirmed_at": None}
    row.update(kw)
    return row


ROWS = [
    standing("sa_plain_read", effects=["read"]),
    standing("sa_draft_any", effects=["draft"], lanes=[], workspaces=["biz-*"]),
    standing("sa_draft_content", effects=["draft"], contents=[content_digest(CONTENT)], expires_at=NOW + 7200),
    standing("sa_commit_untargeted", expires_at=NOW + 100),
    standing("sa_commit_tool", targets=["tool:lane_post"], expires_at=NOW + 200),
    standing("sa_commit_flow_content", targets=[FLOW], contents=[content_digest(CONTENT)], expires_at=NOW + 300),
    standing("sa_commit_flow_template", targets=[FLOW], expires_at=NOW + 400),
    standing("sa_commit_flow_shape", targets=[FLOW], shape=1, expires_at=NOW + 500),
    standing("sa_commit_grant", targets=["tool:lane_post"], grant_id="gr_parent", expires_at=NOW + 50),
    standing("sa_commit_other_lane", targets=["tool:lane_post"], lanes=["biz-b/*"], expires_at=NOW + 10),
    standing("sa_commit_issued", targets=["tool:lane_post"], issuer="ctl_a", confirmed_at=NOW - 60,
             expires_at=NOW + 20),
    standing("sa_commit_issued_stale", targets=["tool:lane_post"], issuer="ctl_a", confirmed_at=NOW - 90_000,
             expires_at=NOW + 15),
    standing("sa_commit_issued_untrusted", targets=["tool:lane_post"], issuer="ctl_b", expires_at=NOW + 5),
    standing("sa_commit_revoked", targets=["tool:lane_post"], revoked_at=NOW - 1, expires_at=NOW + 1),
    standing("sa_commit_withdrawn", targets=["tool:lane_post"], withdrawn_at=NOW - 1, expires_at=NOW + 2),
    standing("sa_commit_future", targets=["tool:lane_post"], starts_at=NOW + 10, expires_at=NOW + 3),
    standing("sa_commit_expired", targets=["tool:lane_post"], expires_at=NOW - 1),
    standing("sa_commit_used_up", targets=["tool:lane_post"], max_uses=2, uses=2, expires_at=NOW + 4),
    standing("sa_commit_day_full", targets=["tool:lane_post"], per_day=1, day="2027-01-15", day_uses=1,
             expires_at=NOW + 6),
    standing("sa_commit_connector", targets=["connector:instagram.post@c1"], expires_at=NOW + 600),
    standing("sa_pay_content", effects=["pay"], contents=[content_digest({"amount": 5})], expires_at=NOW + 700),
    standing("sa_pay_any", effects=["pay"], expires_at=NOW + 800),
]


@pytest.fixture
def store(tmp_path):
    made = itertools.count()

    def make() -> ApprovalStore:
        path = tmp_path / f"policy-{next(made)}.db"
        s = ApprovalStore(DB(path, ""), bus=EventBus(), link_key=b"k" * 32, clock=lambda: NOW,
                          standing_effects=["read", "draft", "commit", "pay"])
        s.issued_ok = lambda issuer, effect: issuer == "ctl_a"
        for r in ROWS:
            values = {k: json.dumps(v) if isinstance(v, list) else v for k, v in r.items()}
            s.db.execute(f"INSERT INTO standing ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                         tuple(values.values()))
        return s
    return make


CALLS = {
    "effect not standing": ("irreversible", LANE, [], {}),
    "read": ("read", LANE, [], {}),
    "draft, any content, workspace pattern": ("draft", "biz-a/x", [], {"content": {"a": 1}}),
    "draft, after untrusted content": ("draft", "biz-a/x", [], {"content": {"a": 1}, "bound_only": True}),
    "draft, bound content after untrusted": ("draft", LANE, [], {"content": CONTENT, "bound_only": True}),
    "commit, no tool": ("commit", LANE, [], {}),
    "commit, tool target": ("commit", LANE, [], {"tool": "lane_post", "content": {"text": "x"}}),
    "commit, tool target, grant in chain": ("commit", LANE, ["gr_parent", "gr_child"], {"tool": "lane_post",
                                                                                     "content": {"text": "x"}}),
    "commit, flow content approved": ("commit", LANE, [], {"tool": "flow_run", "content": CONTENT}),
    "commit, flow other content": ("commit", LANE, [], {"tool": "flow_run",
                                                        "content": {**CONTENT, "params": {"caption": "other"}}}),
    "commit, flow template": ("commit", LANE, [], {"tool": "flow_run", "content": TEMPLATE}),
    "commit, flow other content after untrusted": ("commit", LANE, [], {
        "tool": "flow_run", "content": {**CONTENT, "params": {"caption": "other"}}, "bound_only": True}),
    "commit, flow template after untrusted": ("commit", LANE, [], {"tool": "flow_run", "content": TEMPLATE,
                                                                   "bound_only": True}),
    "commit, other lane": ("commit", "biz-b/x", [], {"tool": "lane_post", "content": {"text": "x"}}),
    "commit, connector": ("commit", LANE, [], {"tool": "connector_run", "content": CONNECTOR}),
    "commit, connector after untrusted": ("commit", LANE, [], {"tool": "connector_run", "content": CONNECTOR,
                                                               "bound_only": True}),
    "pay, content approved": ("pay", LANE, [], {"content": {"amount": 5}}),
    "pay, other content": ("pay", LANE, [], {"content": {"amount": 6}}),
    "pay, after untrusted": ("pay", LANE, [], {"content": {"amount": 5}, "bound_only": True}),
    "commit, unknown workspace": ("commit", "zzz/x", [], {"tool": "lane_post"}),
}


def test_each_call_is_pinned(store):
    check("consume_standing", {name: outcome(lambda a=a: store().consume_standing(a[0], a[1], a[2], **a[3]))
                               for name, a in CALLS.items()})


def test_using_them_up_is_pinned(store):
    s = store()
    s.db.execute("UPDATE standing SET max_uses = 3, expires_at = ? WHERE id = 'sa_draft_any'", (NOW + 864000,))
    s.db.execute("UPDATE standing SET per_day = 2, expires_at = ? WHERE id = 'sa_commit_flow_template'",
                 (NOW + 864000,))
    days = {"day": NOW, "next day": NOW + 86400}
    script = {}
    for label, now in days.items():
        s.clock = lambda now=now: now
        for i in range(4):
            script[f"{label} draft {i}"] = outcome(lambda: s.consume_standing(
                "draft", "biz-a/x", [], tool="lane_post", content={"text": "x"}))
            script[f"{label} template {i}"] = outcome(lambda: s.consume_standing(
                "commit", LANE, [], tool="flow_run", content=TEMPLATE))
    rows = s.db.all("SELECT id, uses, day, day_uses FROM standing ORDER BY id")
    script["rows"] = [dict(r) for r in rows]
    check("consume_standing_script", script)
