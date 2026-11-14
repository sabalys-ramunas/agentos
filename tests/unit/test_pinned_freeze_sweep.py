"""Pins the freeze's sweep (agentos.policy.freeze.revoke_reaching): which grants and standing
approvals a lane, workspace or node freeze revokes, which it keeps, and what it reports when the
store fails. The policy is a stand-in that records every revoke; the grant rows are real blocks."""

from __future__ import annotations

import json
from types import SimpleNamespace

from pinned import check, outcome
from pinned_grants_blocks import block

from agentos.core.models import Effect
from agentos.policy.errors import Conflict
from agentos.policy.freeze import Reach, revoke_reaching
from agentos.policy.grants import Subject

NOW = 1_800_000_000


def row(b, path, *, subject=None, expires_at=None, raw_subject=None, raw_block=None, issued_at=0) -> dict:
    sub = subject or (b.sub.model_dump() if b.sub else {"kind": "agent", "id": "child"})
    return {"id": b.id, "rid": b.rid, "path": "/" + "/".join(x.id for x in path) + "/", "depth": len(path) - 1,
            "issued_at": issued_at, "subject": raw_subject if raw_subject is not None else json.dumps(sub),
            "expires_at": expires_at, "block": raw_block if raw_block is not None else json.dumps(b.to_data())}


A = block(sub=Subject(id="agent-a"), workspaces=["biz-a"], ceiling=Effect.COMMIT, note="the poster")
A_CHILD = block(A, lanes=["biz-a/instagram"])
WIDE = block(sub=Subject(id="agent-wide"), workspaces=["biz-a", "biz-b"], ceiling=Effect.COMMIT)
HUMAN = block(sub=Subject(kind="human", id="m_va"), workspaces=["biz-a"], ceiling=Effect.READ)
HUMAN_CHILD = block(HUMAN, lanes=["biz-a/x"])
OLD = block(sub=Subject(id="agent-old"), workspaces=["biz-a"], ceiling=Effect.COMMIT)
SITE = block(sub=Subject(id="site-key:reddit"), workspaces=["biz-a"], ceiling=Effect.COMMIT)
DEAD = block(sub=Subject(id="agent-dead"), workspaces=["biz-a"], ceiling=Effect.COMMIT)
DEAD_CHILD = block(DEAD, lanes=["biz-a/x"])
BROKEN = block(sub=Subject(id="agent-broken"), workspaces=["biz-a"], ceiling=Effect.COMMIT)
CONFLICT = block(sub=Subject(id="agent-conflict"), workspaces=["biz-a"], ceiling=Effect.COMMIT)
FAILS = block(sub=Subject(id="agent-fails"), workspaces=["biz-a"], ceiling=Effect.COMMIT)

ROWS = [
    row(A, [A], issued_at=1), row(A_CHILD, [A, A_CHILD], issued_at=2), row(WIDE, [WIDE], issued_at=3),
    row(HUMAN, [HUMAN], issued_at=4), row(HUMAN_CHILD, [HUMAN, HUMAN_CHILD], issued_at=5),
    row(OLD, [OLD], expires_at=NOW - 1, issued_at=6), row(SITE, [SITE], expires_at=NOW - 1, issued_at=7),
    row(DEAD, [DEAD], issued_at=8), row(DEAD_CHILD, [DEAD, DEAD_CHILD], issued_at=9),
    row(BROKEN, [BROKEN], raw_subject="{not json", raw_block="{not json", issued_at=10),
    row(CONFLICT, [CONFLICT], issued_at=11), row(FAILS, [FAILS], issued_at=12),
]
STANDING = [
    SimpleNamespace(id="sa_by_grant", grant_id=A.id, workspaces=[], lanes=[]),
    SimpleNamespace(id="sa_by_workspace", grant_id=None, workspaces=["biz-a"], lanes=[]),
    SimpleNamespace(id="sa_by_lane", grant_id=None, workspaces=[], lanes=["biz-a/instagram"]),
    SimpleNamespace(id="sa_elsewhere", grant_id=None, workspaces=["biz-z"], lanes=["biz-z/*"]),
    SimpleNamespace(id="sa_conflict", grant_id=None, workspaces=["biz-a"], lanes=[]),
    SimpleNamespace(id="sa_fails", grant_id=None, workspaces=["biz-a"], lanes=[]),
]


class Policy:
    def __init__(self, *, rows_fail=False, standing_fail=False, bus_fail=False, approvals=True):
        self.calls: list = []
        self.rows_fail, self.standing_fail, self.bus_fail = rows_fail, standing_fail, bus_fail
        self.db = SimpleNamespace(all=self._all)
        self.bus = SimpleNamespace(publish=self._publish)
        self.runtime = None
        if approvals:
            self.approvals = SimpleNamespace(list_standing=self._list_standing, revoke_standing=self._revoke_standing)

    def clock(self):
        return NOW

    def _all(self, sql):
        if self.rows_fail:
            raise RuntimeError("disk")
        return ROWS

    def _revoked_map(self):
        return {DEAD.id: NOW - 100}

    def revoke(self, gid, *, by, reason):
        self.calls.append(["revoke", gid, by, reason])
        if gid == CONFLICT.id:
            raise Conflict("already revoked")
        if gid == FAILS.id:
            raise OSError("disk")
        return {"cascades_to": [A_CHILD.id, "gr_offline_child"] if gid == A.id else []}

    def _list_standing(self, *, active_only):
        if self.standing_fail:
            raise ValueError("bad row")
        return STANDING

    def _revoke_standing(self, sid, *, by):
        self.calls.append(["revoke_standing", sid, by])
        if sid == "sa_conflict":
            raise Conflict("gone")
        if sid == "sa_fails":
            raise OSError("disk")

    def _publish(self, kind, payload):
        if self.bus_fail:
            raise RuntimeError("bus")
        self.calls.append(["publish", kind, payload])


REACHES = {
    "node": Reach(everything=True),
    "workspace biz-a": Reach(workspaces=["biz-a"]),
    "lane biz-a/instagram": Reach(lanes=["biz-a/instagram"]),
    "workspace biz-z": Reach(workspaces=["biz-z"]),
}


def test_the_sweep_is_pinned():
    cases = {}
    for name, reach in REACHES.items():
        for variant, kw in {"": {}, ", rows fail": {"rows_fail": True}, ", standing fails": {"standing_fail": True},
                            ", bus fails": {"bus_fail": True}, ", no approvals": {"approvals": False}}.items():
            def run(reach=reach, kw=kw):
                p = Policy(**kw)
                out = revoke_reaching(p, reach, by="owner", reason="frozen: test")
                return {"out": out, "calls": p.calls}
            cases[name + variant] = outcome(run)
    text = json.dumps(cases)
    for b, label in ((A, "A"), (A_CHILD, "A_CHILD"), (WIDE, "WIDE"), (HUMAN, "HUMAN"), (HUMAN_CHILD, "HUMAN_CHILD"),
                     (OLD, "OLD"), (SITE, "SITE"), (DEAD, "DEAD"), (DEAD_CHILD, "DEAD_CHILD"), (BROKEN, "BROKEN"),
                     (CONFLICT, "CONFLICT"), (FAILS, "FAILS")):
        text = text.replace(b.id, f"<{label}>")
    check("revoke_reaching", json.loads(text))
