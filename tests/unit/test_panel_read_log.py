"""2026-11-15 test, P2-11: the owner's open page polls an environment and a job every few seconds. Their
reads are logged once per page and thing, not on every poll; real actions and denials always are."""

from __future__ import annotations

import asyncio

from agentos.api import access as access_mod
from agentos.api.access import PolicyAccess
from agentos.core.models import Effect
from agentos.core.presence import Presence


class _Ctx:
    def __init__(self, grant_id: str):
        self.tool = ""
        self.admin = True
        self.grant = type("G", (), {"id": grant_id})()
        self.rows: list[tuple[str, str, str | None]] = []

    def precheck(self, effect, lane_id=None):
        return None

    def trace(self, kind, *, status, lane=None, **fields):
        self.rows.append((self.tool, status, lane))


def _read(access: PolicyAccess, action: str, lane: str) -> None:
    asyncio.run(access.authorize(Effect.READ, action=action, lane_id=lane))


def test_a_persons_repeated_read_is_logged_once(monkeypatch):
    monkeypatch.setattr(access_mod, "_READS_SEEN", type(access_mod._READS_SEEN)())
    ctx = _Ctx("g-poll-1")
    for _ in range(5):
        _read(PolicyAccess(ctx, presence=Presence.PERSON), "environment.read", "env/abc")
    _read(PolicyAccess(ctx, presence=Presence.PERSON), "environment.read", "env/other")
    asyncio.run(PolicyAccess(ctx, presence=Presence.PERSON).authorize(Effect.COMMIT, action="lane.post", lane_id="env/abc"))
    assert ctx.rows == [("panel.environment.read", "ok", "env/abc"), ("panel.environment.read", "ok", "env/other"),
                        ("panel.lane.post", "ok", "env/abc")]


def test_a_read_is_logged_again_after_a_while(monkeypatch):
    monkeypatch.setattr(access_mod, "_READS_SEEN", type(access_mod._READS_SEEN)())
    clock = [1000.0]
    monkeypatch.setattr(access_mod.time, "monotonic", lambda: clock[0])
    ctx = _Ctx("g-poll-2")
    _read(PolicyAccess(ctx, presence=Presence.PERSON), "job.read", "env/abc")
    clock[0] += access_mod.ROUTINE_READ_S + 1
    _read(PolicyAccess(ctx, presence=Presence.PERSON), "job.read", "env/abc")
    assert len(ctx.rows) == 2


def test_a_deferred_read_that_fails_is_still_logged(monkeypatch):
    monkeypatch.setattr(access_mod, "_READS_SEEN", type(access_mod._READS_SEEN)())
    ctx = _Ctx("g-poll-3")
    _read(PolicyAccess(ctx, presence=Presence.PERSON), "job.read", "env/abc")
    again = PolicyAccess(ctx, presence=Presence.PERSON)
    again.defer_done = True
    _read(again, "job.read", "env/abc")
    again.settle(RuntimeError("the job is gone"))
    assert [row[1] for row in ctx.rows] == ["ok", "error"]


def test_an_agents_reads_are_all_logged(monkeypatch):
    monkeypatch.setattr(access_mod, "_READS_SEEN", type(access_mod._READS_SEEN)())
    ctx = _Ctx("g-poll-4")
    ctx.authorize = lambda *a, **k: asyncio.sleep(0)  # the policy's own routing, which traces itself
    for _ in range(3):
        _read(PolicyAccess(ctx, presence=Presence.AGENT), "job.read", "env/abc")
    assert len(ctx.rows) == 3


def test_a_lane_open_is_logged_every_time(monkeypatch):
    """Opening a lane is authorized as a read, but it is a real action: never folded."""
    monkeypatch.setattr(access_mod, "_READS_SEEN", type(access_mod._READS_SEEN)())
    ctx = _Ctx("g-poll-5")
    for _ in range(2):
        _read(PolicyAccess(ctx, presence=Presence.PERSON), "lane.open", "biz-a/instagram")
    assert len(ctx.rows) == 2
