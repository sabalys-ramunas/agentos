"""The daily digest, brief B5 (agentos.engage.digest): one day per surface from engage.db, the
engage_digest tool, `agentos engage digest`, and GET /api/engage/digest."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Principal
from agentos.engage import digest as dg
from agentos.engage.caps import Kind
from agentos.engage.digest import DayRecord, day_record, render_text
from agentos.engage.models import DayPlan, Session
from agentos.engage.service import EngageService

OWNER = {"kind": "human", "id": "owner", "owner": "owner"}
AGENT = Principal(kind="agent", id="agent-1")
IG = "biz-a/instagram"
RD = "biz-b/reddit"
D = date(2026, 11, 4)                                   # a Wednesday
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)           # 05:00 in Vilnius, 22:00 the day before in New York
VILNIUS = ZoneInfo("Europe/Vilnius")
DRAFT_TEXT = "Lovely crema on this one, what beans did you use? " * 3

IG_YAML = """
program: biz-a-ig
surface: biz-a/instagram
timezone: Europe/Vilnius
phases:
  - kind: steady
    daily: {like: 15, comment: 3, follow: 2, story_view: 10}
comments: {mode: approve, topics: [coffee], max_chars: 140}
"""

RD_YAML = """
program: biz-b-rd
surface: biz-b/reddit
timezone: America/New_York
phases:
  - kind: steady
    daily: {comment: 1, post: 1, join: 1}
"""


def z(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def at(text: str) -> str:
    return z(text).isoformat(timespec="seconds")


def make_service(home) -> EngageService:
    svc = EngageService(home=home, clock=lambda: NOW.timestamp())
    for surface, text in ((IG, IG_YAML), (RD, RD_YAML)):
        out = svc.draft(surface, text, by=OWNER)
        svc.approve(out["program"], by=OWNER)
    return svc


def seed(svc: EngageService) -> None:
    """Two days of Instagram in Vilnius (UTC+2): the day D and a verified like the day before."""
    s = svc.store
    local = lambda h, m=0: datetime(2026, 11, 4, h, m, tzinfo=VILNIUS)
    s.put_plan(DayPlan(surface=IG, day=D, timezone="Europe/Vilnius", phase="steady", phase_day=3,
                       sessions=[Session(start=local(8), minutes=20, actions={Kind.LIKE: 3, Kind.COMMENT: 1}),
                                 Session(start=local(18), minutes=15, actions={Kind.LIKE: 2})],
                       caps={Kind.LIKE: 4, Kind.COMMENT: 1, Kind.FOLLOW: 2, Kind.STORY_VIEW: 10},
                       hourly={Kind.LIKE: 4, Kind.COMMENT: 1}))
    s.set_cap(IG, Kind.LIKE, 3, by="owner")              # the owner lowered it after the day was drawn
    # session 0: done; two likes verified, a timeout, and a comment waiting for the owner
    r0 = s.put_session(IG, D, 0, planned_start=local(8), minutes=20)
    o = s.add_ops(r0["id"], IG, D, ["like", "comment", "like", "like"])
    s.set_op(o[0]["id"], status="verified", at=at("2026-11-04 06:02"), run_id="run-1")
    s.set_op(o[1]["id"], status="awaiting_approval", at=at("2026-11-04 06:05"), run_id="run-c", target="post-1",
             content_origin="draft", content_id="dr_00000000000000aa")
    s.set_op(o[2]["id"], status="verified", at=at("2026-11-04 06:09"), run_id="run-2")
    s.set_op(o[3]["id"], status="failed", at=at("2026-11-04 06:15"), run_id="run-3", reason="timeout")
    s.set_session(r0["id"], status="done", started_at=at("2026-11-04 06:00"), ended_at=at("2026-11-04 06:20"))
    # session 1: a rate limit ends it, the rest moves to a new window that the hold refuses
    r1 = s.put_session(IG, D, 1, planned_start=local(18), minutes=15)
    o = s.add_ops(r1["id"], IG, D, ["like", "like"])
    s.set_op(o[0]["id"], status="failed", at=at("2026-11-04 16:05"), run_id="run-4", reason="signal: rate_limited")
    s.set_op(o[1]["id"], status="skipped", at=at("2026-11-04 16:05"), reason="rescheduled")
    s.set_session(r1["id"], status="ended_early", end_reason="signal: rate_limited",
                  started_at=at("2026-11-04 16:00"), ended_at=at("2026-11-04 16:05"))
    s.add_history(IG, "op", {"kind": "like", "status": "failed", "reason": "signal: rate_limited",
                             "session": [D.isoformat(), 1]}, at=z("2026-11-04 16:05"))
    s.add_history(IG, "op", {"kind": "like", "status": "verified", "reason": None,
                             "session": [D.isoformat(), 0]}, at=z("2026-11-04 06:02"))
    s.add_hold(IG, reason="rate_limited", signal="rate_limited", since=z("2026-11-04 16:05"),
               until=z("2026-11-04 22:05"))
    r2 = s.put_session(IG, D, 2, planned_start=z("2026-11-04 20:30"), minutes=10, origin="reschedule")
    o = s.add_ops(r2["id"], IG, D, ["like"], moved_from=[o[1]["id"]])
    s.set_op(o[0]["id"], status="skipped", at=at("2026-11-04 20:30"), reason="held")
    s.set_session(r2["id"], status="refused", end_reason="held: rate_limited", ended_at=at("2026-11-04 20:30"))
    # the day before (local): a verified like that must not count
    rp = s.put_session(IG, D - timedelta(days=1), 0, planned_start=z("2026-11-03 21:30"), minutes=10)
    o = s.add_ops(rp["id"], IG, D - timedelta(days=1), ["like"])
    s.set_op(o[0]["id"], status="verified", at=at("2026-11-03 21:31"))
    # holds: one older still in force, one cleared before the day began
    s.add_hold(IG, reason="connector_broken", kind="comment", since=z("2026-11-01 10:00"), detail="flow")
    old = s.add_hold(IG, reason="needs_human:captcha", signal="captcha", since=z("2026-11-02 10:00"))
    s.clear_holds(IG, by="owner", at=z("2026-11-03 21:00"), hold_id=old)
    # the draft provider gave nothing: a pause of provider text only, in force until the next session
    s.add_hold(IG, reason="draft_provider_unavailable", kind="draft_provider", since=z("2026-11-04 17:00"),
               until=z("2026-11-05 06:00"), detail="timeout")
    with s._tx() as conn:
        for did, status, created in (("dr_00000000000000aa", "used", "2026-11-04 06:04"),
                                     ("dr_00000000000000ab", "alternative", "2026-11-04 06:04"),
                                     ("dr_00000000000000ac", "expired", "2026-11-03 21:59")):
            conn.execute("INSERT INTO drafts (id, surface, kind, batch, text, target, status, approval_id, run_id, "
                         "provider, created_at) VALUES (?, ?, 'comment', 'db_1', ?, 'post-1', ?, ?, ?, 'http', ?)",
                         (did, IG, DRAFT_TEXT, status, "ap_1" if status == "used" else None,
                          "run-c" if status == "used" else None, at(created)))
        # a draft whose run may have acted, failed on the day (written two days before)
        conn.execute("INSERT INTO drafts (id, surface, kind, batch, text, status, provider, created_at, settled_at) "
                     "VALUES ('dr_00000000000000ad', ?, 'comment', 'db_0', 'x', 'failed', 'http', ?, ?)",
                     (IG, at("2026-11-02 10:00"), at("2026-11-04 16:06")))
        for iid, status, submitted, settled in (("it_used", "used", "2026-11-02 10:00", "2026-11-03 22:30"),
                                                ("it_exp", "expired", "2026-11-02 10:00", "2026-11-03 21:30"),
                                                ("it_q", "queued", "2026-11-04 10:00", None),
                                                ("it_fail", "failed", "2026-11-01 10:00", "2026-11-04 16:06"),
                                                ("it_wait", "awaiting", "2026-11-01 10:00", None)):
            conn.execute("INSERT INTO items (id, surface, kind, text, expires_at, status, submitted_by, submitted_at, "
                         "settled_at) VALUES (?, ?, 'post', 'x', ?, ?, 'owner', ?, ?)",
                         (iid, IG, at("2026-11-20 00:00"), status, at(submitted), at(settled) if settled else None))
        for when, surface, text in (("2026-11-04T16:05:00.123456+00:00", IG, "biz-a/instagram rests until 22:05"),
                                    ("2026-11-03T20:00:00.000001+00:00", IG, "the day before"),
                                    ("2026-11-04T12:00:00+00:00", None, "a node notice")):
            conn.execute("INSERT INTO notices (at, surface, kind, text) VALUES (?, ?, 'hold', ?)", (when, surface, text))


@pytest.fixture
def svc(tmp_path):
    s = make_service(tmp_path)
    seed(s)
    yield s
    s.store.close()


# ---------------------------------------------------------------- the record


def test_a_seeded_day_gives_the_exact_record(svc):
    rec = day_record(svc.store, IG, D, now=NOW).public()
    assert rec["schema"] == 1 and rec["surface"] == IG and rec["site"] == "instagram"
    assert rec["day"] == "2026-11-04" and rec["timezone"] == "Europe/Vilnius"
    assert rec["program"] == {"id": "biz-a-ig", "version": 1, "approved": True}
    assert (rec["plan_saved"], rec["quiet"], rec["phase"], rec["sessions_on"]) == (True, False, "steady", None)
    s = rec["sessions"]
    assert {k: v for k, v in s.items() if k != "windows"} == {
        "planned": 2, "rescheduled": 1, "run": 2, "scheduled": 0, "running": 0, "done": 1, "ended_early": 1, "aborted": 0,
        "refused": 1, "missed": 0}
    assert s["windows"] == [
        {"idx": 0, "origin": "plan", "planned_start": "2026-11-04T06:00:00+00:00", "minutes": 20.0,
         "started_at": "2026-11-04T06:00:00+00:00", "ended_at": "2026-11-04T06:20:00+00:00", "status": "done",
         "end_reason": None, "operations": 4},
        {"idx": 1, "origin": "plan", "planned_start": "2026-11-04T16:00:00+00:00", "minutes": 15.0,
         "started_at": "2026-11-04T16:00:00+00:00", "ended_at": "2026-11-04T16:05:00+00:00",
         "status": "ended_early", "end_reason": "signal: rate_limited", "operations": 2},
        {"idx": 2, "origin": "reschedule", "planned_start": "2026-11-04T20:30:00+00:00", "minutes": 10.0,
         "started_at": None, "ended_at": "2026-11-04T20:30:00+00:00", "status": "refused",
         "end_reason": "held: rate_limited", "operations": 1}]
    zero = {"planned": 0, "moved_in": 0, "attempted": 0, "verified": 0, "unverified": 0, "failed": 0,
            "awaiting_approval": 0, "no_outcome": 0, "held": 0, "skipped": 0, "skip_reasons": {}, "pending": 0,
            "failed_reasons": {}, "held_reasons": {}}
    assert rec["operations"] == [
        {**zero, "kind": "comment", "planned": 1, "attempted": 1, "awaiting_approval": 1},
        {**zero, "kind": "like", "planned": 5, "moved_in": 1, "attempted": 4, "verified": 2, "failed": 2,
         "skipped": 2, "skip_reasons": {"held": 1, "rescheduled": 1},
         "failed_reasons": {"signal: rate_limited": 1, "timeout": 1}}]
    assert rec["totals"] == {"planned": 6, "attempted": 5, "verified": 2, "failed": 2, "awaiting_approval": 1,
                             "skipped": 2}
    none = {"account": None, "counted": None}   # no action ledger here: engage's own counts alone
    assert rec["caps"] == [
        {"kind": "post", "used": 0, "cap": 0, "ceiling": 3, "hourly": 0, **none},
        {"kind": "comment", "used": 1, "cap": 1, "ceiling": 10, "hourly": 1, **none},
        # the owner's 3, under the plan's 4, and the plan's hourly 4 never above the day's 3
        {"kind": "like", "used": 4, "cap": 3, "ceiling": 80, "hourly": 3, **none},
        {"kind": "follow", "used": 0, "cap": 2, "ceiling": 20, "hourly": 2, **none},   # no hourly: the day's
        {"kind": "story_view", "used": 0, "cap": 10, "ceiling": 80, "hourly": 10, **none}]
    waiting = {"id": "dr_00000000000000aa", "kind": "comment", "approval_id": "ap_1", "target": "post-1",
               "created_at": "2026-11-04T06:04:00+00:00", "run_id": "run-c", "preview": None, "day": None}
    assert rec["drafts"] == {"awaiting_approval": 1, "created": 2, "waiting": [waiting], "failed": 1, "expired": 0,
                             "provider_paused": True}
    assert rec["awaiting_now"] == {
        "count": 1, "days": {"2026-11-04": 1}, "drafts": [{**waiting, "day": "2026-11-04"}]}
    assert rec["items"] == {"submitted": 1, "used": 1, "failed": 1, "expired": 0, "withdrawn": 0, "queued_now": 1,
                            "awaiting_now": 1}
    assert rec["holds"] == [
        {"id": 2, "scope": "kind", "kind": "comment", "signal": None, "reason": "connector_broken",
         "detail": "flow", "since": "2026-11-01T10:00:00+00:00", "until": None, "cleared_at": None, "created_that_day": False,
         "active_now": True},
        {"id": 1, "scope": "surface", "kind": None, "signal": "rate_limited", "reason": "rate_limited",
         "detail": None, "since": "2026-11-04T16:05:00+00:00", "until": "2026-11-04T22:05:00+00:00", "cleared_at": None,
         "created_that_day": True, "active_now": False},
        {"id": 4, "scope": "drafts", "kind": "draft_provider", "signal": None, "reason": "draft_provider_unavailable",
         "detail": "timeout", "since": "2026-11-04T17:00:00+00:00", "until": "2026-11-05T06:00:00+00:00", "cleared_at": None,
         "created_that_day": True, "active_now": True}]
    assert rec["signals"] == [{"signal": "rate_limited", "at": "2026-11-04T16:05:00+00:00", "kind": "like",
                               "session": 1}]
    assert rec["notices"] == [{"at": "2026-11-04T16:05:00.123Z", "kind": "hold",
                               "text": "biz-a/instagram rests until 22:05"}]
    assert DayRecord.model_validate(rec).public() == rec            # the JSON reads back as itself
    assert DRAFT_TEXT.strip()[:20] not in json.dumps(rec)            # no draft text by default


def test_the_owner_preview_is_short_and_only_on_request(svc):
    rec = day_record(svc.store, IG, D, now=NOW, include_text=True)
    preview = rec.drafts.waiting[0].preview
    assert preview is not None and len(preview) <= dg.PREVIEW_CHARS and preview.endswith("...")
    assert preview.startswith("Lovely crema")


def test_the_day_is_the_surfaces_own_and_defaults_to_yesterday(svc):
    # at 03:00 UTC it is already the 5th in Vilnius but still the 4th in New York
    assert day_record(svc.store, IG, now=NOW).day == D
    assert day_record(svc.store, RD, now=NOW).day == D - timedelta(days=1)
    rd = day_record(svc.store, RD, now=NOW)
    assert rd.timezone == "America/New_York" and rd.site == "reddit"
    # the day before in Vilnius holds only its own like
    before = day_record(svc.store, IG, D - timedelta(days=1), now=NOW)
    assert [(k.kind, k.verified) for k in before.operations] == [("like", 1)]
    assert before.items.expired == 1 and before.items.used == 0      # 21:30 UTC is still the 3rd locally
    assert before.drafts.created == 1 and before.notices[0].text == "the day before"
    assert [h.reason for h in before.holds] == ["connector_broken", "needs_human:captcha"]


def test_a_surface_with_no_data_and_an_unknown_one(svc):
    rec = day_record(svc.store, RD, D, now=NOW)
    assert not rec.plan_saved and rec.sessions.planned == 0 and rec.operations == []
    assert [(c.kind, c.used, c.cap) for c in rec.caps] == [("post", 0, None), ("comment", 0, None),
                                                            ("join", 0, None)]
    assert rec.holds == [] and rec.signals == [] and rec.notices == [] and rec.drafts.waiting == []
    assert "No plan was saved for this day." in render_text(rec)
    with pytest.raises(NotFound):
        day_record(svc.store, "biz-a/nothing", D)


def test_a_planned_day_with_sessions_off_shows_what_was_planned(tmp_path):
    svc = make_service(tmp_path)
    plan = svc.day_plan(IG, D + timedelta(days=1))                   # what the nightly saves
    assert plan is not None
    rec = dg.record_for(svc, IG, D + timedelta(days=1))
    assert rec.sessions_on is False and rec.plan_saved
    assert rec.sessions.planned == len(plan.sessions) == rec.sessions.scheduled and rec.sessions.run == 0
    assert rec.totals.planned == sum(plan.totals().values()) and rec.totals.attempted == 0
    assert all(w.status == "scheduled" for w in rec.sessions.windows)
    assert {c.kind: c.cap for c in rec.caps if c.cap} == {str(k): v for k, v in plan.caps.items() if v}


def test_parse_day_takes_only_yyyy_mm_dd():
    assert dg.parse_day(None) is None and dg.parse_day(" ") is None and dg.parse_day("2026-11-04") == D
    for bad in ("yesterday", "2026-13-01", "04/11/2026", "20261104", "2026-W45-3", "2026-W45", "2026-11-4",
                "2026-11-04T00:00", "２０２６-11-04"):
        with pytest.raises(InvalidRequest):
            dg.parse_day(bad)


def test_a_day_is_within_400_days_back_and_one_ahead(svc):
    today = NOW.date()
    for ok in (today - timedelta(days=dg.DAYS_BACK), today + timedelta(days=1), D):
        assert dg.check_day(ok, NOW) == ok
    for bad in (date(9999, 12, 31), date(1, 1, 1), today - timedelta(days=dg.DAYS_BACK + 1), today + timedelta(days=2)):
        with pytest.raises(InvalidRequest):
            dg.check_day(bad, NOW)
        with pytest.raises(InvalidRequest):
            dg.digest(svc, IG, bad)
        with pytest.raises(InvalidRequest):
            dg.record_for(svc, IG, bad)
        with pytest.raises(InvalidRequest):
            dg.week(svc, IG, bad)


def test_home_counts_every_approval_waiting_now_and_the_day_keeps_its_own(svc):
    s = svc.store
    old = date(2026, 11, 1)
    r = s.put_session(IG, old, 0, planned_start=datetime(2026, 11, 1, 6, tzinfo=UTC), minutes=10)
    o = s.add_ops(r["id"], IG, old, ["comment", "like"])
    s.set_op(o[0]["id"], status="awaiting_approval", at=at("2026-11-01 06:05"), run_id="run-x",
             content_origin="draft", content_id="dr_00000000000000ac")
    s.set_op(o[1]["id"], status="awaiting_approval", at=at("2026-11-01 06:06"), run_id="run-y")   # the owner's words
    yesterday = dg.record_for(svc, IG)
    assert yesterday.day == D and yesterday.totals.awaiting_approval == 1 and len(yesterday.drafts.waiting) == 1
    assert yesterday.awaiting_now.count == 3 and yesterday.awaiting_now.days == {"2026-11-01": 2, "2026-11-04": 1}
    assert [(d.id, d.day) for d in yesterday.awaiting_now.drafts] == [("dr_00000000000000ac", "2026-11-01"),
                                                                       ("dr_00000000000000aa", "2026-11-04")]
    nov1 = dg.record_for(svc, IG, old)
    assert nov1.totals.awaiting_approval == 2 and nov1.awaiting_now.count == 3
    assert dg.record_for(svc, RD).awaiting_now.count == 0


def test_the_text_says_the_same_numbers(svc):
    rec = day_record(svc.store, IG, D, now=NOW)
    text = render_text(rec)
    assert text == render_text(rec.public())
    for line in ("biz-a/instagram on 2026-11-04 (Europe/Vilnius)",
                 "Sessions: 2 run of 2 planned, 1 rescheduled (1 done, 1 ended early, 1 refused)",
                 "Operations: 2 verified of 5 attempted, 6 planned, 1 awaiting approval, 2 failed, 2 skipped",
                 ("like: planned 5, moved in 1, attempted 4, verified 2, failed 2, skipped 2 (held 1, moved 1), "
                 "failed because site showed rate limited 1, timed out 1"),
                 "comment: planned 1, attempted 1, verified 0, awaiting approval 1",
                 "like: 4 of 3, ceiling 80", "comment: 1 of 1, ceiling 10",
                 "Drafts: 1 awaiting approval, 2 written that day, 1 failed",
                 "The draft provider is paused now: text comes only from items until the next session.",
                 "draft_provider_unavailable (draft provider only: nothing else is held)",
                 "dr_00000000000000aa comment on post-1, approval ap_1",
                 ("Items that day: 1 used, 1 failed, 0 expired, 1 submitted, 0 withdrawn. Waiting now: 1, "
                 "1 awaiting approval"),
                 "Awaiting approval now (any day): 1",
                 "rate_limited, since 2026-11-04T16:05:00+00:00, ended 2026-11-04T22:05:00+00:00",
                 "connector_broken (comment only: the rest goes on), since 2026-11-01T10:00:00+00:00, in force now",
                 "Signals: rate_limited at 2026-11-04T16:05:00+00:00 (like)",
                 "2026-11-04 16:05 UTC biz-a/instagram rests until 22:05"):
        assert line in text, line
    assert "Lovely crema" not in text and "—" not in text


def test_the_text_spells_out_session_counts_above_one(svc):
    rec = day_record(svc.store, IG, D, now=NOW).public()
    rec["sessions"].update(done=2, ended_early=2, aborted=1, refused=2, missed=3)
    text = render_text(rec)
    assert "(2 done, 2 ended early, 1 aborted, 2 refused, 3 missed)" in text
    assert "earlys" not in text and "dones" not in text and "refuseds" not in text and "misseds" not in text
    assert "#0 plan 2026-11-04T06:00:00Z: done, 4 operations" in text


def test_the_week_reads_once_and_matches_each_days_record(svc):
    weekly = dg.week(svc, IG, now=NOW)                           # anchored on the surface's yesterday
    tz = svc.timezone(svc.store.active(IG).definition)
    assert weekly == [dg.summary(day_record(svc.store, IG, D - timedelta(days=n), tz=tz, now=NOW))
                      for n in range(6, -1, -1)]
    assert weekly[-1]["day"] == D.isoformat() and dg.week(svc, IG, D, now=NOW) == weekly
    stmts: list[str] = []
    svc.store._conn.set_trace_callback(stmts.append)
    try:
        dg.week(svc, IG, now=NOW)
    finally:
        svc.store._conn.set_trace_callback(None)
    assert len(stmts) <= 8, stmts                                # not one round per day


def test_the_day_reads_items_and_notices_by_time(svc):
    """Items and notices are read for the day (with a day of slack), not the whole history."""
    stmts: list[str] = []
    svc.store._conn.set_trace_callback(stmts.append)
    try:
        day_record(svc.store, IG, D, now=NOW)
    finally:
        svc.store._conn.set_trace_callback(None)
    items = [q for q in stmts if "FROM items" in q and "submitted_at" in q]
    notices = [q for q in stmts if "FROM notices" in q]
    assert items and all("submitted_at >= '2026-11-02T22:00:00+00:00'" in q for q in items)
    assert notices and all("at < '2026-11-05T22:00:00+00:00'" in q for q in notices)
    # an old item settled long ago stays out of the day, a queued one still counts as waiting now
    with svc.store._tx() as conn:
        conn.execute("INSERT INTO items (id, surface, kind, text, expires_at, status, submitted_by, submitted_at, "
                     "settled_at) VALUES ('it_old', ?, 'post', 'x', ?, 'used', 'owner', ?, ?)",
                     (IG, at("2026-11-20 00:00"), at("2025-01-01 00:00"), at("2025-01-02 00:00")))
    assert day_record(svc.store, IG, D, now=NOW).items.model_dump() == {
        "submitted": 1, "used": 1, "failed": 1, "expired": 0, "withdrawn": 0, "queued_now": 1, "awaiting_now": 1}


# ---------------------------------------------------------------- the executor's counting


def test_the_digest_and_the_executor_share_one_cap_rule(tmp_path):
    from agentos.engage.caps import day_limits

    # the plan's, under the owner's, under the hard ceiling; the hour's under the day's
    caps, hourly = day_limits("instagram", {Kind.LIKE: 500, Kind.COMMENT: 5, "reply": 3},
                              {Kind.LIKE: 400, Kind.COMMENT: 9, "reply": 2}, {Kind.COMMENT: 2, Kind.FOLLOW: 1})
    assert caps == {"like": 80, "comment": 2, "reply": 3}       # reply has no caps table row
    assert hourly == {"like": 80, "comment": 2, "reply": 2}
    assert day_limits("instagram", None, None, {Kind.LIKE: 1}) == ({}, {})
    assert dg.day_limits is day_limits
    import agentos.engage.executor as ex
    assert ex.day_limits is day_limits


def test_cap_use_matches_the_executors_counting(tmp_path):
    """Real windows through the executor: `used` is what it counts against the day's cap, and
    `cap` the cap it checked. A reschedule pushes past the plan, so the cap really stops one."""
    from test_engage_executor import IG as RIG_IG
    from test_engage_executor import MONDAY, Rig

    from agentos.engage.ports import OpResult, OpStatus

    rig = Rig(tmp_path)
    rig.ops.script[1] = OpResult(OpStatus.FAILED, error="timeout")
    rig.ops.script[2] = OpResult(OpStatus.AWAITING_APPROVAL)
    rig.run_days(MONDAY, 2)
    tuesday = MONDAY + timedelta(days=1)
    # one more window on Tuesday with more likes than the day's cap leaves (a reschedule's shape)
    plan = rig.svc.store.plans(RIG_IG, tuesday, tuesday)[0]
    extra = plan.caps[Kind.LIKE] + 2
    row = rig.svc.store.put_session(RIG_IG, tuesday, len(plan.sessions) + 5,
                                    planned_start=datetime(2026, 11, 3, 19, 0, tzinfo=UTC), minutes=30,
                                    origin="reschedule")
    rig.svc.store.add_ops(row["id"], RIG_IG, tuesday, ["like"] * extra)
    rig.run(tuesday, len(plan.sessions) + 5)
    for day in (MONDAY, tuesday):
        rec = day_record(rig.svc.store, RIG_IG, day, tz=rig.svc.timezone(rig.svc.store.active(RIG_IG).definition),
                         now=datetime.fromtimestamp(rig.t, UTC))
        plan = rig.svc.store.plans(RIG_IG, day, day)[0]
        caps, hourly = rig.ex._limits(rig.svc.store.active(RIG_IG).definition, plan)
        for c in rec.caps:
            assert c.used == rig.svc.store.count_ops(RIG_IG, c.kind, day=day)
            assert c.cap == plan.caps.get(Kind(c.kind), 0)            # what the executor checked
            assert (c.cap, c.hourly) == (caps.get(c.kind, 0), hourly.get(c.kind, caps.get(c.kind, 0)))
        calls = [c for c in rig.ops.calls if f":{day.isoformat()}:" in c.idempotency_key]
        assert rec.totals.attempted == len(calls)
        assert rec.totals.verified + rec.totals.failed + rec.totals.awaiting_approval == len(calls)
    like = next(c for c in rec.caps if c.kind == "like")
    ops = {k.kind: k for k in rec.operations}
    assert like.used == like.cap and ops["like"].skip_reasons["cap_daily"] >= 2   # stopped exactly at the cap
    monday = day_record(rig.svc.store, RIG_IG, MONDAY, now=NOW)
    assert monday.totals.failed == 1 and monday.totals.awaiting_approval == 1


def test_the_caps_show_the_accounts_ledger_by_actor(svc):
    """The digest (tool and API alike, both through record_for) shows the account's actions on every
    path, split by actor, and the count the cap holds: the larger of engage's and agent + flow."""
    from agentos.engage.digest import digest, record_for, render_text
    from agentos.policy.ledger import ActionLedger

    ledger = ActionLedger(None, clock=lambda: NOW.timestamp())
    tz = ZoneInfo("Europe/Vilnius")
    noon = datetime.combine(D, datetime.min.time(), tz) + timedelta(hours=12)
    for actor, n in (("agent", 2), ("flow", 3), ("person", 4)):
        for _ in range(n):
            ledger.debit(IG, "like", actor=actor, at=noon)
    ledger.debit(IG, "comment", actor="agent", at=noon)
    ledger.debit(IG, "like", actor="agent", at=noon + timedelta(days=1))       # another day
    ledger.debit(RD, "like", actor="agent", at=noon)                           # another account

    class Runtime:
        def get(self, name):
            return ledger if name == "ledger" else None

    svc.runtime = Runtime()
    rec = record_for(svc, IG, D, now=NOW)
    caps = {c.kind: c for c in rec.caps}
    assert caps["like"].account.model_dump() == {"agent": 2, "flow": 3, "person": 4}
    assert caps["like"].used == 4 and caps["like"].counted == 5          # agent + flow beats engage's own 4
    assert caps["comment"].account.model_dump() == {"agent": 1, "flow": 0, "person": 0}
    assert caps["comment"].counted == 1
    assert caps["story_view"].account is None and caps["story_view"].counted is None   # no action on the ledger
    out = digest(svc, IG, D, now=NOW)["surfaces"][0]
    assert next(c for c in out["caps"] if c["kind"] == "like")["account"] == {"agent": 2, "flow": 3, "person": 4}
    text = render_text(rec)
    assert "like: 5 of 3" in text and "2 by agents, 3 by flows and engagement, 4 by you" in text
    assert "\u2014" not in text


# ---------------------------------------------------------------- the tool


class Ctx:
    principal = AGENT

    def __init__(self, runtime, *, reach: str = "biz-a/"):
        self.runtime = runtime
        self.reach = reach
        self.seen: list[tuple[str, str | None]] = []

    async def authorize(self, effect, **kw):
        self.seen.append((effect.value, kw.get("lane_id")))

    def can_access_lane(self, lane_id):
        return lane_id.startswith(self.reach)


def test_the_tool_reads_only_what_the_grant_reaches(svc):
    from agentos.core.runtime import Runtime
    from agentos.engage import tools

    runtime = Runtime(engage=svc)
    ctx = Ctx(runtime)
    out = asyncio.run(tools.engage_digest(ctx))
    assert [r["surface"] for r in out["surfaces"]] == [IG] and out["schema"] == 1
    assert out["surfaces"][0]["day"] == "2026-11-04" and ctx.seen == [("read", None)]
    assert out["surfaces"][0]["drafts"]["waiting"][0]["preview"] is None
    assert "Lovely crema" not in json.dumps(out)
    both = asyncio.run(tools.engage_digest(Ctx(runtime, reach=""), day="2026-11-04"))
    assert [r["surface"] for r in both["surfaces"]] == [IG, RD]
    text = asyncio.run(tools.engage_digest(ctx, surface=IG, day="2026-11-04", format="text"))
    assert text["format"] == "text" and "Sessions: 2 run of 2 planned" in text["text"]
    with pytest.raises(InvalidRequest):
        asyncio.run(tools.engage_digest(ctx, format="html"))
    with pytest.raises(InvalidRequest):
        asyncio.run(tools.engage_digest(ctx, day="last tuesday"))
    with pytest.raises(NotFound):
        asyncio.run(tools.engage_digest(ctx, surface="biz-a/nothing"))
    # an empty surface is no surface: the list, filtered by the grant
    blank = Ctx(runtime)
    assert [r["surface"] for r in asyncio.run(tools.engage_digest(blank, surface="", day="2026-11-04"))["surfaces"]] \
        == [IG] and blank.seen == [("read", None)]
    spec = next(s for s in tools.tool_specs(runtime) if s.name == "engage_digest")
    assert spec.effect.value == "read" and spec.lane_arg == "surface"


class BareCtx(Ctx):
    can_access_lane = None                                         # a context that cannot say what it covers


def test_the_tool_fails_closed_without_a_lane_check(svc):
    from agentos.core.runtime import Runtime
    from agentos.engage import tools

    runtime = Runtime(engage=svc)
    assert asyncio.run(tools.engage_digest(BareCtx(runtime), day="2026-11-04"))["surfaces"] == []
    assert asyncio.run(tools.engage_digest(BareCtx(runtime), surface="  ", day="2026-11-04"))["surfaces"] == []


# ---------------------------------------------------------------- the CLI


def test_the_cli_prints_text_and_json(svc, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentos.engage.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    out = CliRunner().invoke(app, ["digest", IG, "--day", "2026-11-04"])
    assert out.exit_code == 0, out.output
    assert "Sessions: 2 run of 2 planned" in out.output and "Lovely crema" not in out.output
    out = CliRunner().invoke(app, ["digest", "--day", "2026-11-04", "--json"])
    assert out.exit_code == 0, out.output
    data = json.loads(out.output)
    assert [r["surface"] for r in data["surfaces"]] == [IG, RD] and data["sessions_on"] is False
    out = CliRunner().invoke(app, ["digest", IG, "--day", "nope"])
    assert out.exit_code == 1 and "YYYY-MM-DD" in out.output


# ---------------------------------------------------------------- the panel API


@pytest.fixture
def panel(tmp_path):
    from test_node_panel import Node

    (tmp_path / "node").mkdir()
    n = Node(tmp_path / "node")
    n.api_app.state.api.limiter.per_key = n.api_app.state.api.limiter.global_max = 10_000
    svc = make_service(tmp_path / "engage")
    seed(svc)
    n.runtime.register("engage", svc)
    yield n
    svc.store.close()
    n.close()


def test_the_digest_routes_need_credentials_and_stay_inside_a_grant(panel):
    from starlette.testclient import TestClient
    from test_node_panel import TOKEN, bearer

    agent = panel.grant()                                          # biz-a only, read
    with TestClient(panel.app, base_url="http://localhost:7400") as client:
        assert client.get("/api/engage/digest").status_code == 401
        assert client.get(f"/api/engage/digest/{IG}").status_code == 401
        everything = client.get("/api/engage/digest", headers=bearer(TOKEN)).json()
        assert everything["available"] and [r["surface"] for r in everything["surfaces"]] == [IG, RD]
        assert [r["day"] for r in everything["surfaces"]] == ["2026-11-04", "2026-11-03"]
        mine = client.get("/api/engage/digest?day=2026-11-04", headers=bearer(agent)).json()
        assert [r["surface"] for r in mine["surfaces"]] == [IG]
        assert mine["surfaces"][0]["drafts"]["waiting"][0]["preview"] is None
        assert "Lovely crema" not in json.dumps(mine)
        one = client.get(f"/api/engage/digest/{IG}?day=2026-11-04", headers=bearer(TOKEN))
        assert one.status_code == 200, one.text
        body = one.json()
        assert body["record"]["totals"]["verified"] == 2
        assert [w["day"] for w in body["week"]] == [(D - timedelta(days=n)).isoformat() for n in range(6, -1, -1)]
        assert body["week"][-1] == {"day": "2026-11-04", "sessions_planned": 2, "sessions_rescheduled": 1,
                                    "sessions_run": 2, "attempted": 5, "verified": 2, "awaiting_approval": 1,
                                    "holds": 2, "signals": 1}
        assert body["week"][-2]["verified"] == 1
        # the week is the surface's last seven days whatever day is open
        older = client.get(f"/api/engage/digest/{IG}?day=2026-10-20", headers=bearer(TOKEN)).json()
        assert older["record"]["day"] == "2026-10-20" and older["week"] == body["week"]
        default = client.get(f"/api/engage/digest/{IG}", headers=bearer(agent)).json()
        assert default["record"]["day"] == "2026-11-04" and default["record"]["drafts"]["waiting"][0]["preview"] is None
        r = client.get(f"/api/engage/digest/{RD}", headers=bearer(agent))
        assert r.status_code == 403 and r.json()["error"]["code"] == "denied"      # outside the grant
        r = client.get("/api/engage/digest/biz-a/nothing", headers=bearer(TOKEN))
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
        for bad in ("04-11-2026", "20261104", "2026-W45-3", "9999-12-31", "0001-01-01", "2025-09-30"):
            r = client.get(f"/api/engage/digest/{IG}?day={bad}", headers=bearer(TOKEN))
            assert r.status_code == 400 and r.json()["error"]["code"] == "invalid", (bad, r.text)
            assert client.get(f"/api/engage/digest?day={bad}", headers=bearer(TOKEN)).status_code == 400, bad
        assert client.get("/api/engage/digest?day=x", headers=bearer(TOKEN)).status_code == 400
        assert "—" not in json.dumps(everything)


def test_only_the_owner_in_person_sees_draft_words(panel):
    """The node token is the owner's, but agents hold it: a preview needs the owner's panel session."""
    from starlette.testclient import TestClient
    from test_node_panel import TOKEN, bearer

    admin = panel.grant(admin=True)
    url = f"/api/engage/digest/{IG}?day=2026-11-04"
    with TestClient(panel.app, base_url="http://localhost:7400") as client:
        for who in (TOKEN, admin):
            one = client.get(url, headers=bearer(who)).json()
            assert one["record"]["drafts"]["waiting"][0]["preview"] is None, who
            assert one["record"]["awaiting_now"]["drafts"][0]["preview"] is None
            every = client.get("/api/engage/digest?day=2026-11-04", headers=bearer(who)).json()
            assert "Lovely crema" not in json.dumps(one) + json.dumps(every)
        panel.sign_in(client)                                       # the owner's cookie session
        one = client.get(url).json()
        assert one["record"]["drafts"]["waiting"][0]["preview"].startswith("Lovely crema")
        assert one["record"]["awaiting_now"]["drafts"][0]["preview"].startswith("Lovely crema")
        every = client.get("/api/engage/digest?day=2026-11-04").json()
        assert every["surfaces"][0]["drafts"]["waiting"][0]["preview"].startswith("Lovely crema")


def test_a_notice_hides_the_accounts_a_caller_cannot_see(panel):
    from starlette.testclient import TestClient
    from test_node_panel import TOKEN, bearer

    svc = panel.runtime.get("engage")
    text = f"{IG} and {RD} were active at the same hours (0.91 over 14 days). {IG} gets new habits."
    with svc.store._tx() as conn:
        conn.execute("INSERT INTO notices (at, surface, kind, text) VALUES (?, ?, 'in_sync', ?)",
                     ("2026-11-04T09:00:00.5+00:00", IG, text))
    agent = panel.grant()                                          # biz-a only
    with TestClient(panel.app, base_url="http://localhost:7400") as client:
        for url in (f"/api/engage/digest/{IG}?day=2026-11-04", "/api/engage/digest?day=2026-11-04"):
            body = client.get(url, headers=bearer(agent)).json()
            rec = body.get("record") or body["surfaces"][0]
            sync = [n for n in rec["notices"] if n["kind"] == "in_sync"]
            assert sync == [{"at": "2026-11-04T09:00:00.500Z", "kind": "in_sync",
                             "text": f"{IG} and another account were active at the same hours (0.91 over 14 days). "
                                     f"{IG} gets new habits."}], url
            assert RD not in json.dumps(body)
        owner = client.get(f"/api/engage/digest/{IG}?day=2026-11-04", headers=bearer(TOKEN)).json()
        assert any(n["text"] == text for n in owner["record"]["notices"])
    assert svc.store._rows("SELECT text FROM notices WHERE kind = 'in_sync'")[0][0] == text   # stored as it was
    # the tool, for a grant that reaches biz-a only
    from agentos.core.runtime import Runtime
    from agentos.engage import tools

    out = asyncio.run(tools.engage_digest(Ctx(Runtime(engage=svc)), surface=IG, day="2026-11-04", format="text"))
    assert "and another account were active" in out["text"] and RD not in out["text"]


def test_a_node_without_the_engage_service_says_so(tmp_path):
    from starlette.testclient import TestClient
    from test_node_panel import TOKEN, Node, bearer

    n = Node(tmp_path)
    try:
        with TestClient(n.app, base_url="http://localhost:7400") as client:
            assert client.get("/api/engage/digest", headers=bearer(TOKEN)).json() == {
                "available": False, "schema": 1, "sessions_on": None, "surfaces": []}
            assert client.get(f"/api/engage/digest/{IG}", headers=bearer(TOKEN)).json() == {
                "available": False, "surface": IG}
    finally:
        n.close()


def test_migration_5_indexes_notices_and_settled_items_on_a_version_4_database(tmp_path):
    import sqlite3

    from agentos.engage.migrations import DB, MIGRATIONS
    from agentos.engage.store import EngageStore
    from agentos.ops import migrate

    v4 = migrate.MigrationSet(package="engage", db=DB, create=True, migrations=MIGRATIONS.migrations[:4])
    migrate.migrate(tmp_path, [v4], backup=False)
    conn = sqlite3.connect(tmp_path / DB)
    conn.execute("INSERT INTO notices (at, surface, kind, text) VALUES ('2026-11-04T10:00:00+00:00', "
                 "'biz-a/instagram', 'hold', 'kept')")
    conn.commit()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "notices_surface" not in names and "items_settled" not in names
    conn.close()
    store = EngageStore(tmp_path)
    try:
        idx = {r["name"]: r["tbl_name"] for r in store._dicts("SELECT name, tbl_name FROM sqlite_master "
                                                              "WHERE type = 'index'")}
        assert idx["notices_surface"] == "notices" and idx["items_settled"] == "items"
        assert [r["name"] for r in store._dicts("PRAGMA index_info(notices_surface)")] == ["surface", "at"]
        assert [r["name"] for r in store._dicts("PRAGMA index_info(items_settled)")] == ["surface", "settled_at"]
        assert store._rows("SELECT text FROM notices")[0][0] == "kept"
    finally:
        store.close()
    [st] = [s for s in migrate.status(tmp_path) if s.package == "engage"]
    assert st.current == st.latest == 5


def test_every_skip_reason_the_executor_writes_has_words():
    """grep the executor for every reason it skips an operation with: each has words in the text."""
    import inspect
    import re

    from agentos.engage import executor, operations, profile

    src = inspect.getsource(executor)
    written = set(re.findall(r'skip\("([a-z_]+)"\)', src)) | set(re.findall(r'op_reason="([a-z_]+)"', src)) \
        | set(re.findall(r'status="skipped", reason="([a-z_]+)"', src)) | {profile.CURRENT}   # skip(profile.CURRENT)
    assert {"interrupted", "lane_lost", "no_valid_rule", "draft_unavailable", "week_over", "held", "bad_target",
            "no_target", "no_content", "profile_current"} <= written
    assert written <= set(dg.SKIP_WORDS), written - set(dg.SKIP_WORDS)
    # and the reasons a run fails or is held with that the engine itself writes
    assert {"foreign_run", "signal, then week_over", "connector_broken", profile.ALREADY, "denied"} <= set(dg.RUN_WORDS)
    assert dg.run_words("signal: rate_limited") == "site showed rate limited"
    # every problem a no_valid_rule hold's detail can name (Registry.problem, Registry.reason)
    problems = {operations.NO_SPEC, operations.NO_FLOW, operations.NO_RULE, operations.RULE_UNVERIFIED,
                operations.OP_UNVERIFIED, operations.VERIFIED_STALE}
    assert problems == set(dg.PROBLEM_WORDS)
    assert dg.detail_words("post (op_unverified: verified_stale), join (no_flow)") == \
        "post (no canary verified it: changed since its canary), join (connector not installed)"
    # every kind has a caps row name the per-kind table shows, profile_edit included
    assert "profile_edit" in dg.KIND_ORDER
    for words in [*dg.SKIP_WORDS.values(), *dg.RUN_WORDS.values(), *dg.PROBLEM_WORDS.values()]:
        assert "\u2014" not in words


def test_a_profile_edit_hold_holds_one_kind_and_names_its_problem(svc):
    """A kind-only profile_edit hold is not a surface hold: its scope is `kind`, and the text says
    the rest goes on and why the edit may not run."""
    since = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
    svc.store.add_hold(IG, reason="no_valid_rule", since=since, kind="profile_edit",
                       detail="profile_headline (op_unverified: verified_stale)")
    rec = dg.record_for(svc, IG, D, now=NOW)
    [h] = [h for h in rec.holds if h.kind == "profile_edit"]
    assert h.scope == "kind" and h.detail == "profile_headline (op_unverified: verified_stale)"
    text = render_text(rec)
    assert "no_valid_rule (profile_edit only: the rest goes on): profile_headline (no canary verified it: " \
           "changed since its canary)" in text


def test_the_day_says_when_its_time_zone_fell_back_to_utc(svc, monkeypatch):
    assert dg.record_for(svc, IG, D, now=NOW).timezone_fallback is None
    monkeypatch.setattr(svc, "_timezone", lambda program: (ZoneInfo("UTC"), ("neither the program nor the lane "
                                                                            "names a time zone")))
    monkeypatch.setattr(svc, "timezone", lambda program: ZoneInfo("UTC"))
    rec = dg.record_for(svc, IG, D, now=NOW)
    assert rec.timezone == "UTC" and rec.timezone_fallback.startswith("neither the program")
    assert "Time zone: UTC, because neither the program nor the lane names a time zone." in render_text(rec)
