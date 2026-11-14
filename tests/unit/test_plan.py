"""`agentos plan`: the join over real stores, the draft's rules, and that nothing is ever written.

The node is seeded through the stores' own write paths, so a schema change shows up here. The text
output is compared with tests/fixtures/plan; after a change that is meant to alter it, write the
files again with AGENTOS_RECORD_PLAN=1 and review the diff.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.cli import app
from agentos.core.config import NodeConfig
from agentos.core.models import ActsAs, ControlState, LaneControl, LaneSpec, RunStatus, Signal, Surface
from agentos.engage.caps import Kind
from agentos.engage.models import DayPlan, Session
from agentos.engage.service import EngageService, plan_name
from agentos.node.store import LaneStore, QuarantineRecord, SignalRecord
from agentos.plan.cli import render_draft, render_status
from agentos.plan.draft import ContentDraft, draft_day
from agentos.plan.state import collect, connect_ro
from agentos.policy.approvals import SCHEMA as APPROVALS_SCHEMA
from agentos.policy.traces import SCHEMA as TRACES_SCHEMA
from agentos.runner.models import Run
from agentos.runner.store import RunnerStore

NOW = datetime(2026, 11, 6, 9, 30, tzinfo=UTC)
TODAY = NOW.date()
FIXTURES = Path(__file__).parent.parent / "fixtures" / "plan"
Z = "UTC"
OWNER = {"kind": "human", "id": "owner", "owner": "owner"}


def program_yaml(pid: str, surface: str, *, warm: int | None = None) -> str:
    warm_up = f"  - {{kind: warm_up, days: {warm}}}\n" if warm else ""
    site = "site: facebook\n" if surface.endswith("facebook-page") else ""
    return (f"program: {pid}\nsurface: {surface}\n{site}timezone: {Z}\nphases:\n{warm_up}"
            "  - kind: steady\n    daily: {like: 15}\n    targets:\n      - {source: own_feed}\n")


def day_plan(surface: str, pid: str, *, actions: dict | None = None, phase="steady", phase_day=12, quiet=False):
    sessions = [] if quiet else [Session(start=NOW, minutes=9.0, actions=actions or {Kind.LIKE: 12, Kind.COMMENT: 2})]
    return DayPlan(surface=surface, day=TODAY, timezone=Z, program=pid, phase=phase, phase_day=phase_day, quiet=quiet,
                   sessions=sessions)


def add_program(svc, pid, surface, *, starts_on=date(2026, 10, 1), approve=True, warm=None):
    svc.draft(surface, program_yaml(pid, surface, warm=warm), by=OWNER)
    if approve:
        svc.approve(pid, by=OWNER)
        svc.store._conn.execute("UPDATE programs SET starts_on = ? WHERE id = ?", (starts_on.isoformat(), pid))


def seed(home: Path) -> NodeConfig:
    cfg = NodeConfig(home=home)
    lanes = LaneStore(cfg.db_path)
    for lane in ("biz-a/instagram", "biz-a/facebook", "biz-a/linkedin", "biz-b/instagram", "biz-b/reddit", "personal/medium",
                 "personal/reddit", "personal/x"):
        lanes.add(LaneSpec(id=lane))
    ok = SignalRecord(signal=Signal.OK, at=NOW - timedelta(hours=3))
    lanes.set_signal("biz-a/instagram", ok)
    lanes.set_signal("biz-b/instagram", ok)
    lanes.set_signal("personal/medium", SignalRecord(signal=Signal.OK, at=NOW - timedelta(days=5)))
    lanes.set_signal("biz-a/facebook", SignalRecord(signal=Signal.CHECKPOINT, at=NOW - timedelta(minutes=40)))
    lanes.set_control("biz-a/facebook", LaneControl(state=ControlState.PAUSED, by="agentos",
                                                    reason="needs_human:checkpoint"))
    lanes.set_signal("biz-a/linkedin", SignalRecord(signal=Signal.RATE_LIMITED, at=NOW - timedelta(hours=1)))
    lanes.set_control("biz-a/linkedin", LaneControl(state=ControlState.PAUSED, by="agentos",
                                                    reason="backoff:rate_limited", hold_until=NOW + timedelta(hours=5)))
    lanes.set_quarantine("biz-b/reddit", QuarantineRecord(reason="exit IP changed", at=NOW, kind="exit_ip_changed"))
    lanes.set_control("personal/x", LaneControl(state=ControlState.HUMAN, by="robin"))
    lanes.close()  # its connection stays open otherwise, and a later checkpoint rewrites the file

    svc = EngageService(home=home)
    add_program(svc, "biz-a-ig", "biz-a/instagram")
    svc.store.put_plan(day_plan("biz-a/instagram", "biz-a-ig"))
    svc.store._conn.execute("UPDATE counters SET done = 4 WHERE surface = 'biz-a/instagram' AND kind = 'like'")
    add_program(svc, "biz-a-fb", "biz-a/facebook")
    svc.store.put_plan(day_plan("biz-a/facebook", "biz-a-fb"))
    add_program(svc, "biz-b-ig", "biz-b/instagram", warm=10, starts_on=date(2026, 11, 4))
    svc.store.put_plan(day_plan("biz-b/instagram", "biz-b-ig", phase="warm_up", phase_day=3, actions={Kind.STORY_VIEW: 4}))
    add_program(svc, "personal-medium", "personal/medium", approve=False)
    add_program(svc, "biz-a-page", "biz-a/facebook-page")  # acts through a lane of another name, saved plan missing
    svc.store.close()

    runner = RunnerStore(home / "runner.db")
    runner.put_surface(Surface(id="biz-a/facebook-page", site="facebook", lane="personal/x",
                               acts_as=ActsAs(kind="fb_page", id="1234", name="Biz A")))
    runner.put_health("facebook_post", "biz-a/facebook-page", {"state": "ok"})
    runner.put_health("medium_post", "personal/medium", {"state": "broken", "since": NOW.isoformat()})
    runner.insert_run(Run(id="r1", lane="biz-a/instagram", status=RunStatus.QUEUED))
    runner.insert_run(Run(id="r2", lane="biz-a/instagram", status=RunStatus.SUCCEEDED_VERIFIED))
    runner.close()

    policy = sqlite3.connect(home / "policy.db")
    policy.executescript(APPROVALS_SCHEMA)
    rows = [("a1", "pending", "biz-a/instagram", "biz-a", 3600), ("a2", "pending", "biz-a/instagram", "biz-a", 3600),
            ("a3", "pending", None, "biz-b", 3600), ("a4", "approved", "biz-a/instagram", "biz-a", 3600),
            ("a5", "pending", "biz-a/instagram", "biz-a", -60)]
    policy.executemany("INSERT INTO approvals (id, kind, state, lane, workspace, summary, requester, created_at, "
                       "expires_at) VALUES (?, 'action', ?, ?, ?, 's', 'r', 0, ?)",
                       [(i, st, lane, ws, NOW.timestamp() + left) for i, st, lane, ws, left in rows])
    policy.commit()
    policy.close()

    traces = sqlite3.connect(home / "traces.db")
    traces.executescript(TRACES_SCHEMA)
    events = [("t1", 60, "tool", "ok"), ("t2", 120, "tool", "error"), ("t3", 120, "tool_start", "started"),
              ("t4", 90_000, "tool", "ok")]
    traces.executemany("INSERT INTO traces (id, at, kind, lane, status) VALUES (?, ?, ?, 'biz-a/instagram', ?)",
                       [(i, NOW.timestamp() - ago, kind, st) for i, ago, kind, st in events])
    traces.commit()
    traces.close()
    return cfg


@pytest.fixture
def cfg(tmp_path):
    return seed(tmp_path)


def row(state, lane):
    return next(r for r in state.lanes if r.lane == lane)


def tree(home: Path) -> dict[str, str]:
    """Every file's content, leaving out SQLite's own -wal and -shm side files."""
    return {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(home.rglob("*"))
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))}


def rows_in(home: Path) -> dict[str, int]:
    out = {}
    for db, tables in (("engage/engage.db", ("personas", "plans", "notices")), ("agentos.db", ("lanes",)),
                       ("runner.db", ("runs",)), ("policy.db", ("approvals",)), ("traces.db", ("traces",))):
        conn = sqlite3.connect(home / db)
        for table in tables:
            out[f"{db}:{table}"] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.close()
    return out


# ---- the golden output ------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "render"), [
    ("status.txt", lambda s: render_status(s)),
    ("draft.txt", lambda s: render_draft(draft_day(s))),
])
def test_the_output_matches_its_fixture(cfg, name, render):
    text = render(collect(cfg, now=NOW)) + "\n"
    path = FIXTURES / name
    if os.environ.get("AGENTOS_RECORD_PLAN"):
        path.write_text(text)
    assert text == path.read_text()
    assert "—" not in text


# ---- the join ---------------------------------------------------------------------------------


def test_readiness_comes_from_who_may_act(cfg):
    state = collect(cfg, now=NOW)
    assert {r.lane: r.readiness for r in state.lanes} == {
        "biz-a/facebook": "needs_human", "biz-a/instagram": "ready", "biz-a/linkedin": "backoff",
        "biz-b/instagram": "ready", "biz-b/reddit": "quarantined", "personal/medium": "connector_broken",
        "personal/reddit": "ready", "personal/x": "human"}
    assert row(state, "biz-a/facebook").detail == "checkpoint"


def test_programs_join_through_the_surfaces_binding(cfg):
    state = collect(cfg, now=NOW)
    assert [p.surface for p in row(state, "personal/x").programs] == ["biz-a/facebook-page"]
    assert not any(r.lane == "biz-a/facebook-page" for r in state.lanes)
    assert row(state, "personal/x").workspaces == ["biz-a", "personal"]


def test_due_is_what_is_planned_and_not_done(cfg):
    [program] = row(collect(cfg, now=NOW), "biz-a/instagram").programs
    assert program.due == {"like": 8, "comment": 2} and program.done == {"like": 4}
    assert program.phase == "steady day 12"


def test_pending_counts_live_work_only(cfg):
    state = collect(cfg, now=NOW)
    pending = row(state, "biz-a/instagram").pending
    assert (pending.approvals, pending.runs, pending.broken) == (2, 1, [])  # a4 decided, a5 expired, r2 done
    assert state.unattached_approvals == {"biz-b": 1}
    assert row(state, "personal/medium").pending.broken == ["medium_post"]


def test_activity_is_the_last_day_of_finished_calls(cfg):
    r = row(collect(cfg, now=NOW), "biz-a/instagram")
    assert (r.calls_24h, r.errors_24h) == (2, 1)


def test_a_signal_goes_stale_after_three_days(cfg):
    state = collect(cfg, now=NOW)
    assert not row(state, "biz-a/instagram").signal_stale
    assert row(state, "personal/medium").signal_stale and row(state, "personal/reddit").signal_stale


def test_a_workspace_filter_follows_the_surfaces_brand(cfg):
    state = collect(cfg, now=NOW, workspace="biz-a")
    assert [r.lane for r in state.lanes] == ["biz-a/facebook", "biz-a/instagram", "biz-a/linkedin", "personal/x"]
    assert state.unattached_approvals == {}


def test_a_backoff_past_its_time_is_ready_again(cfg):
    later = collect(cfg, now=NOW + timedelta(hours=6))
    assert row(later, "biz-a/linkedin").readiness == "ready"


@pytest.mark.parametrize(("marker", "frozen"), [
    ("node.frozen", 8), ("mesh.frozen", 8), ("workspace.biz-b.frozen", 2), ("lane.biz-a.instagram.frozen", 1),
    ("not a marker.frozen", 8),  # the store's rule: anything unclear freezes the node
])
def test_a_freeze_outranks_everything(cfg, marker, frozen):
    (cfg.home / "freeze").mkdir(exist_ok=True)
    (cfg.home / "freeze" / marker).write_text("")
    state = collect(cfg, now=NOW)
    assert sum(r.readiness == "frozen" for r in state.lanes) == frozen


def test_a_program_with_no_lane_is_shown_and_blocked(cfg):
    svc = EngageService(home=cfg.home)
    add_program(svc, "biz-c-ig", "biz-c/instagram")
    svc.store.close()
    state = collect(cfg, now=NOW)
    assert row(state, "biz-c/instagram").readiness == "no_lane"
    assert any(i.lane == "biz-c/instagram" and i.section == "blocked" for i in draft_day(state).items)


def test_one_unreadable_store_is_named_and_the_rest_still_works(cfg):
    (cfg.home / "runner.db").write_bytes(b"this is not a database at all, not even close")
    for side in ("runner.db-wal", "runner.db-shm"):
        (cfg.home / side).unlink(missing_ok=True)
    state = collect(cfg, now=NOW)
    assert state.unavailable == ["runner: DatabaseError"]
    assert row(state, "biz-a/instagram").programs
    assert "could not read runner" in render_status(state) and "could not read runner" in render_draft(draft_day(state))


def test_an_older_schema_is_unavailable_not_a_crash(cfg):
    conn = sqlite3.connect(cfg.home / "policy.db")
    conn.execute("DROP TABLE approvals")
    conn.commit()
    conn.close()
    assert collect(cfg, now=NOW).unavailable == ["approvals: OperationalError"]


# ---- the draft's rules ------------------------------------------------------------------------


def items(cfg, section, lane=None):
    out = [i for i in draft_day(collect(cfg, now=NOW)).items if i.section == section]
    return [i.text for i in out if lane is None or i.lane == lane]


def test_a_lane_that_cannot_act_gets_no_work(cfg):
    draft = draft_day(collect(cfg, now=NOW))
    due_lanes = {i.lane for i in draft.items if i.section == "due"}
    assert due_lanes == {"biz-a/instagram", "biz-b/instagram"}
    assert "biz-a/facebook" not in due_lanes  # it has a saved plan, and a checkpoint
    assert "hand it back" in items(cfg, "blocked", "biz-a/facebook")[0]


def test_a_backoff_says_to_wait_and_never_to_retry(cfg):
    [text] = items(cfg, "blocked", "biz-a/linkedin")
    assert "Do not retry" in text


def test_an_unapproved_program_is_never_due(cfg):
    assert items(cfg, "due", "personal/medium") == []
    # The plan's name and a plain state, never its id or the raw state word.
    assert items(cfg, "programs", "personal/medium") == ["Medium: waiting for your approval."]


def test_a_quiet_day_is_said_out_loud(cfg):
    svc = EngageService(home=cfg.home)
    svc.store.put_plan(day_plan("biz-a/instagram", "biz-a-ig", quiet=True))
    svc.store.close()
    assert items(cfg, "due", "biz-a/instagram") == ["Instagram (steady day 12): a quiet day."]


def test_a_plan_is_named_as_a_person_reads_it(cfg):
    assert plan_name("biz-a/social", "instagram") == "Social on Instagram"
    assert plan_name("biz-a/instagram", "instagram") == "Instagram"  # an account named for its site says it once
    svc = EngageService(home=cfg.home)
    svc.store.close()
    state = collect(cfg, now=NOW)
    [program] = row(state, "personal/medium").programs
    assert program.name == "Medium" and program.id == "personal-medium" and program.state == "draft"
    assert program.phase == "waiting for your approval"


def test_upkeep_lists_stale_signals_and_broken_connectors(cfg):
    medium = items(cfg, "upkeep", "personal/medium")
    assert any("medium_post" in t for t in medium) and any("2026-11-01" in t for t in medium)
    assert items(cfg, "upkeep", "personal/reddit") == ["Check this lane: no signal yet."]
    assert items(cfg, "upkeep", "biz-a/facebook") == []  # blocked lanes are not chased for upkeep


def test_sections_come_in_the_order_a_person_takes_them(cfg):
    order = [i.section for i in draft_day(collect(cfg, now=NOW)).items]
    assert order == sorted(order, key=["blocked", "queue", "due", "programs", "upkeep"].index)


def test_the_seam_is_empty_in_v0_and_a_producer_slots_in_without_changing_anything_else(cfg):
    state = collect(cfg, now=NOW)
    plain = draft_day(state)
    assert plain.content == [] and "a brain task will write this batch" in render_draft(plain)
    seen = {}

    def brain(state, items):
        seen["lanes"] = {i.lane for i in items if i.section == "due"}
        return [ContentDraft(surface="biz-a/instagram", kind="post", text="Fresh beans today.")]

    written = draft_day(state, content=brain)
    assert written.items == plain.items and written.content[0].status == "draft"
    assert "biz-a/facebook" not in seen["lanes"]  # a producer is never asked to write for a held lane
    assert "(post, draft): Fresh beans today." in render_draft(written)
    assert "approval on the commit gate" in render_draft(written)


def test_the_draft_says_whether_sessions_really_run(cfg):
    assert "is a dry run" in render_draft(draft_day(collect(cfg, now=NOW)))
    live = draft_day(collect(cfg.model_copy(update={"engage_sessions": True}), now=NOW))
    assert live.dry_run is False and "sessions are on" in render_draft(live)
    assert "dry run" not in render_draft(live)


def test_a_content_draft_cannot_be_anything_but_a_draft():
    with pytest.raises(ValueError, match="draft"):
        ContentDraft(surface="a/b", kind="post", text="x", status="approved")


# ---- read-only --------------------------------------------------------------------------------


def test_status_and_draft_change_no_file_and_no_row(cfg, monkeypatch):
    monkeypatch.setenv("AGENTOS_HOME", str(cfg.home))
    files, counts = tree(cfg.home), rows_in(cfg.home)
    run = CliRunner().invoke
    for args in (["plan", "status"], ["plan", "draft"], ["plan", "status", "--json"], ["plan", "draft", "--json"]):
        result = run(app, args)
        assert result.exit_code == 0, result.output
    assert tree(cfg.home) == files
    assert rows_in(cfg.home) == counts


def test_drawing_a_persona_is_left_to_the_engine(cfg):
    """`EngageService.plan` would write one for a surface without it, which is why status never calls it."""
    before = rows_in(cfg.home)["engage/engage.db:personas"]
    collect(cfg, now=NOW)
    assert rows_in(cfg.home)["engage/engage.db:personas"] == before


def test_a_node_with_nothing_on_it_stays_that_way(tmp_path, monkeypatch):
    home = tmp_path / "not-yet"
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    run = CliRunner().invoke
    assert "No lanes or programs" in run(app, ["plan", "status"]).output
    assert "Nothing needs you today" in run(app, ["plan", "draft"]).output
    assert not home.exists()


def test_the_connection_refuses_a_write(cfg):
    conn = connect_ro(cfg.db_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
        conn.execute("DELETE FROM lanes")
    conn.close()


def test_the_package_has_no_way_to_act():
    """Reading only: importing it loads no lane manager, browser, runner, brain service or publish
    path, directly or through anything it imports."""
    code = ("import sys, agentos.plan.cli\n"
            "print('\\n'.join(m for m in sys.modules if m.startswith('agentos.')))")
    loaded = set(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                                timeout=60).stdout.split())
    forbidden = {"agentos.node.lanes", "agentos.node.tools", "agentos.node.publish", "agentos.node.commitgate",
                 "agentos.node.interaction", "agentos.node.engine_camoufox", "agentos.node.camoufox.engine",
                 "agentos.runner.runner", "agentos.runner.executor", "agentos.runner.service", "agentos.brains.service",
                 "agentos.policy.service"}
    assert "agentos.plan.state" in loaded and not loaded & forbidden


# ---- the commands -----------------------------------------------------------------------------


def test_the_commands_are_registered_and_print_json(cfg, monkeypatch):
    monkeypatch.setenv("AGENTOS_HOME", str(cfg.home))
    run = CliRunner().invoke
    status = json.loads(run(app, ["plan", "status", "--json", "--workspace", "biz-b"]).output)
    assert [r["lane"] for r in status["lanes"]] == ["biz-b/instagram", "biz-b/reddit"]
    draft = json.loads(run(app, ["plan", "draft", "--json"]).output)
    assert draft["status"] == "draft" and draft["dry_run"] is True and draft["content"] == []
    assert run(app, ["plan"]).exit_code == 2  # no_args_is_help
