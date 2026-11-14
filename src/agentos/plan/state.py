"""Read the node's stores without changing them, and join them per lane.

The stores' own constructors are not read-only: they create directories, run migrations, take a
backup and add columns. `agentos plan` must leave a node exactly as it found it, so each store is
subclassed here with a constructor that only opens its file (mode=ro, query_only) and keeps the
parent's read methods, so rows parse one way everywhere. Live lane states (warm, cold) are in
agentosd's memory, not in a store, so readiness is what the stores can say: who may act on a lane.

A store that is missing is an empty source; one that cannot be read (permissions, an older schema)
is named in `unavailable`, and the rest of the view still works.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agentos.core.config import NodeConfig, config_path, load_config
from agentos.core.models import ControlState, LaneControl, parse_lane_id
from agentos.engage.migrations import DB as ENGAGE_DB
from agentos.engage.models import DayPlan, ProgramState, ProgramVersion
from agentos.engage.service import plan_name, program_timezone
from agentos.engage.store import EngageStore
from agentos.freeze.store import SUFFIX, parse_key
from agentos.node.holds import BACKOFF, NEEDS_HUMAN
from agentos.node.store import LaneRecord, LaneStore
from agentos.runner.store import RunnerStore

RUNNER_DB = "runner.db"  # agentos.runner.service.DB_NAME, which would import the whole runner to say this
STALE_SIGNAL = timedelta(days=3)  # keepalive looks in every few days: older than that says nothing new
ACTIVITY_WINDOW = timedelta(hours=24)
RUN_STATES = ("queued", "running", "waiting_approval")

# In the order that decides a lane's readiness: the first that applies is what you see.
FROZEN, QUARANTINED, NEEDS_YOU, PAUSED = "frozen", "quarantined", "needs_human", "paused"
HUMAN, BACKING_OFF, BROKEN, NO_LANE, READY = "human", "backoff", "connector_broken", "no_lane", "ready"
BLOCKED = frozenset({FROZEN, QUARANTINED, NEEDS_YOU, PAUSED, HUMAN, BACKING_OFF, NO_LANE})


def connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5, isolation_level=None,
                           check_same_thread=False)
    conn.execute("PRAGMA query_only=ON")
    return conn


class ReadOnlyLaneStore(LaneStore):
    def __init__(self, path: Path):  # no super(): it creates the file and alters tables
        self.path = path
        self.timeout = 5.0
        self._unreadable: set[str] = set()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = connect_ro(self.path)
        try:
            yield conn
        finally:
            conn.close()


class ReadOnlyEngageStore(EngageStore):
    def __init__(self, home: Path):  # no super(): it runs migrations and takes a backup
        self.path = home / ENGAGE_DB
        self._lock = threading.Lock()
        self._conn = connect_ro(self.path)
        self._conn.row_factory = sqlite3.Row


class ReadOnlyRunnerStore(RunnerStore):
    def __init__(self, path: Path):  # no super(): it creates tables and migrates
        self.path = path
        self._lock = threading.RLock()
        self._db = connect_ro(path)
        self._db.row_factory = sqlite3.Row
        self._bad_rows: set[str] = set()

    def close(self) -> None:
        self._db.close()


class ProgramRow(BaseModel):
    id: str
    surface: str
    state: str                       # draft | approved
    phase: str                       # for a person: "steady day 12", "warm-up day 3 of 10"
    name: str = ""                   # the plan's name as a person reads it: "Social on Instagram"
    planned: bool = False            # today has a saved plan
    quiet: bool = False
    due: dict[str, int] = Field(default_factory=dict)   # today's planned actions not done yet
    done: dict[str, int] = Field(default_factory=dict)


class Pending(BaseModel):
    approvals: int = 0
    runs: int = 0
    broken: list[str] = Field(default_factory=list)     # connector operations broken on this lane

    @property
    def total(self) -> int:
        return self.approvals + self.runs + len(self.broken)


class LaneRow(BaseModel):
    lane: str
    workspaces: list[str]
    readiness: str
    detail: str | None = None
    last_signal: str | None = None
    signal_at: datetime | None = None
    signal_stale: bool = False
    programs: list[ProgramRow] = Field(default_factory=list)
    pending: Pending = Field(default_factory=Pending)
    calls_24h: int = 0
    errors_24h: int = 0


class PlanState(BaseModel):
    at: datetime
    today: date
    sessions_on: bool = False        # engage_sessions: approved programs act on their accounts
    lanes: list[LaneRow] = Field(default_factory=list)
    unattached_approvals: dict[str, int] = Field(default_factory=dict)  # per workspace, no lane (questions)
    unavailable: list[str] = Field(default_factory=list)                # sources that could not be read


# ---- the sources ------------------------------------------------------------------------------


def _lanes(cfg: NodeConfig) -> list[LaneRecord]:
    return ReadOnlyLaneStore(cfg.db_path).list() if cfg.db_path.exists() else []


def _approvals(home: Path, now: datetime) -> list[sqlite3.Row]:
    path = home / "policy.db"
    if not path.exists():
        return []
    conn = connect_ro(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT lane, workspace FROM approvals WHERE state = 'pending' AND expires_at > ?",
                            (now.timestamp(),)).fetchall()
    finally:
        conn.close()


def _activity(home: Path, now: datetime) -> dict[str, tuple[int, int]]:
    """Per lane, over the last day: finished tool calls, and how many ended in an error."""
    path = home / "traces.db"
    if not path.exists():
        return {}
    conn = connect_ro(path)
    try:
        rows = conn.execute(
            "SELECT lane, COUNT(*), SUM(status = 'error') FROM traces "
            "WHERE at >= ? AND lane IS NOT NULL AND kind != 'tool_start' GROUP BY lane",
            ((now - ACTIVITY_WINDOW).timestamp(),)).fetchall()
    finally:
        conn.close()
    return {lane: (calls, errors or 0) for lane, calls, errors in rows}


def _frozen(home: Path) -> dict[str, str]:
    """What is frozen, as {"node": why, "workspace:<ws>": why, "lane:<id>": why}. Read from the marker
    files' names, with the store's rule that anything unclear freezes the whole node."""
    root = home / "freeze"
    if not root.exists():
        return {}
    out: dict[str, str] = {}
    try:
        names = [p.name for p in root.iterdir() if p.name.endswith(SUFFIX)]
    except OSError:
        return {"node": "the freeze directory cannot be read"}
    for name in names:
        parsed = parse_key(name.removesuffix(SUFFIX))
        scope, target = parsed if parsed else ("node", "")
        key = "node" if scope in ("node", "mesh") else f"{scope}:{target}"
        out[key] = "this node is frozen" if key == "node" else f"{scope} {target} is frozen"
    return out


def _phase(pv: ProgramVersion, plan: DayPlan | None, today: date) -> str:
    program = pv.definition
    if pv.state is not ProgramState.APPROVED:
        return "waiting for your approval" if pv.state is ProgramState.DRAFT else "an older version, not running"
    if plan is not None:
        if plan.phase == "warm_up":
            return f"warm-up day {plan.phase_day} of {program.warm_up_days}"
        return f"steady day {plan.phase_day}"
    if pv.starts_on is None or pv.starts_on > today:
        return f"starts {pv.starts_on}" if pv.starts_on else "not started"
    day = (today - pv.starts_on).days + 1
    if day <= program.warm_up_days:
        return f"warm-up day {day} of {program.warm_up_days}"
    return f"steady day {day - program.warm_up_days}"


def _program_row(store: EngageStore, pv: ProgramVersion, cfg: NodeConfig, now: datetime) -> ProgramRow:
    """Reads what the nightly job saved, and draws nothing: drawing a plan can write a persona."""
    program = pv.definition
    today = now.astimezone(program_timezone(program, cfg)).date()
    saved = store.plans(program.surface, today, today) if pv.state is ProgramState.APPROVED else []
    plan = saved[0] if saved else None
    row = ProgramRow(id=program.id, surface=program.surface, state=pv.state.value, phase=_phase(pv, plan, today),
                     name=plan_name(program.surface, program.site or ""), planned=plan is not None,
                     quiet=bool(plan and plan.quiet))
    if plan is None:
        return row
    counters = store.counters(program.surface, today)
    done = {k.value: c["done"] for k, c in counters.items() if c["done"]}
    due = {k.value: n - done.get(k.value, 0) for k, n in plan.totals().items()}
    return row.model_copy(update={"done": done, "due": {k: n for k, n in due.items() if n > 0}})


def _programs(cfg: NodeConfig, now: datetime) -> list[ProgramRow]:
    if not (cfg.home / ENGAGE_DB).exists():
        return []
    store = ReadOnlyEngageStore(cfg.home)
    try:
        latest = {pv.definition.surface: pv for pv in store.programs()}
        out = []
        for surface, pv in sorted(latest.items()):
            out.append(_program_row(store, store.active(surface) or pv, cfg, now))
        return out
    finally:
        store.close()


def _runner(home: Path) -> tuple[dict[str, str], list[dict[str, Any]], list[Any]]:
    """surface -> lane, the broken connector operations, and the runs still in flight."""
    path = home / RUNNER_DB
    if not path.exists():
        return {}, [], []
    store = ReadOnlyRunnerStore(path)
    try:
        bindings = {s.id: s.lane for s in store.list_surfaces()}
        broken = [h for h in store.list_health() if h.get("state") == "broken"]
        return bindings, broken, store.list_runs(status=list(RUN_STATES), limit=1000)
    finally:
        store.close()


# ---- the join ---------------------------------------------------------------------------------


def _hold_time(control: LaneControl) -> datetime | None:
    until = control.hold_until
    return until.replace(tzinfo=UTC) if until is not None and until.tzinfo is None else until


def _readiness(rec: LaneRecord, frozen: dict[str, str], broken: list[str], now: datetime) -> tuple[str, str | None]:
    workspace = parse_lane_id(rec.spec.id)[0]
    why = frozen.get("node") or frozen.get(f"workspace:{workspace}") or frozen.get(f"lane:{rec.spec.id}")
    if why:
        return FROZEN, why
    if rec.quarantine is not None:
        return QUARANTINED, rec.quarantine.reason
    control = rec.control
    if control is not None and control.state == ControlState.PAUSED:
        reason = control.reason or ""
        if reason.startswith(NEEDS_HUMAN):
            return NEEDS_YOU, reason.removeprefix(NEEDS_HUMAN)
        if reason.startswith(BACKOFF):
            until = _hold_time(control)
            if until is None or until > now:  # past its time, agentosd lifts it on its next sweep
                return BACKING_OFF, f"{reason.removeprefix(BACKOFF)} until {until:%Y-%m-%d %H:%M} UTC" if until else None
        else:
            return PAUSED, reason or None
    if control is not None and control.state == ControlState.HUMAN:
        return HUMAN, control.by
    if broken:
        return BROKEN, ", ".join(broken)
    return READY, None


def _lane_of(surface: str, bindings: dict[str, str]) -> str:
    return bindings.get(surface, surface)  # a surface on its own login has the lane's id


def _attempt(unavailable: list[str], name: str, read: Any, default: Any) -> Any:
    try:
        return read()
    except (sqlite3.Error, OSError, ValueError) as exc:
        unavailable.append(f"{name}: {type(exc).__name__}")
        return default


def collect(cfg: NodeConfig, *, now: datetime | None = None, workspace: str | None = None) -> PlanState:
    now = now or datetime.now().astimezone()  # the node's zone: its date is the plan's day
    home = cfg.home
    missed: list[str] = []
    records = _attempt(missed, "lanes", lambda: _lanes(cfg), [])
    bindings, broken, runs = _attempt(missed, "runner", lambda: _runner(home), ({}, [], []))
    programs = _attempt(missed, "engage", lambda: _programs(cfg, now), [])
    approvals = _attempt(missed, "approvals", lambda: _approvals(home, now), [])
    activity = _attempt(missed, "traces", lambda: _activity(home, now), {})
    frozen = _frozen(home)

    known = {r.spec.id: r for r in records}
    by_lane: dict[str, list[ProgramRow]] = {}
    for p in programs:
        by_lane.setdefault(_lane_of(p.surface, bindings), []).append(p)
    broken_on: dict[str, list[str]] = {}
    for h in broken:
        broken_on.setdefault(_lane_of(h["surface"], bindings), []).append(h["operation"])
    unattached: dict[str, int] = {}
    approvals_on: dict[str, int] = {}
    for a in approvals:
        if a["lane"]:
            lane = _lane_of(a["lane"], bindings)
            approvals_on[lane] = approvals_on.get(lane, 0) + 1
        elif a["workspace"]:
            unattached[a["workspace"]] = unattached.get(a["workspace"], 0) + 1
    runs_on: dict[str, int] = {}
    for run in runs:
        if run.lane:
            runs_on[run.lane] = runs_on.get(run.lane, 0) + 1

    rows: list[LaneRow] = []
    for lane in sorted({*known, *by_lane}):
        rec = known.get(lane)
        lane_programs = by_lane.get(lane, [])
        spaces = {parse_lane_id(p.surface)[0] for p in lane_programs}
        spaces.add(parse_lane_id(lane)[0])
        if workspace and workspace not in spaces:
            continue
        lane_broken = sorted(broken_on.get(lane, []))
        if rec is None:
            readiness, detail = NO_LANE, "no lane is bound to this program's surface"
        else:
            readiness, detail = _readiness(rec, frozen, lane_broken, now)
        signal = rec.signal if rec else None
        at = signal.at if signal else None
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        calls, errors = activity.get(lane, (0, 0))
        rows.append(LaneRow(
            lane=lane, workspaces=sorted(spaces), readiness=readiness, detail=detail,
            last_signal=signal.signal.value if signal else None, signal_at=at,
            signal_stale=at is None or now - at > STALE_SIGNAL, programs=lane_programs,
            pending=Pending(approvals=approvals_on.get(lane, 0), runs=runs_on.get(lane, 0), broken=lane_broken),
            calls_24h=calls, errors_24h=errors))
    if workspace:
        unattached = {k: v for k, v in unattached.items() if k == workspace}
    return PlanState(at=now, today=now.date(), sessions_on=cfg.engage_sessions, lanes=rows, unattached_approvals=unattached,
                     unavailable=missed)


def load(home: Path) -> NodeConfig:
    """node.toml when there is one. Nothing is created: without it the view is simply empty."""
    return load_config(home) if config_path(home).exists() else NodeConfig(home=home)



