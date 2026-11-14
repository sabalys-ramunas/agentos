"""The daily digest (brief B5): one place to see a surface's day.

    rec = day_record(store, "biz-a/instagram", date(2026, 11, 4), tz=ZoneInfo("Europe/Vilnius"))
    rec.public()                      # the JSON the MCP tool, the API and `--json` return (schema 1)
    render_text(rec)                  # the same data as plain text (the CLI, format="text")
    digest(service, surface=None)     # every visible surface's yesterday, as the tool and API return it
    week(service, surface, end_day)   # the seven days up to end_day, one line each (the panel's drilldown)

A DayRecord is a pure read of engage.db: nothing is drawn, saved or changed, so it works with
`engage_sessions` off (then it mostly shows what was planned) and for a surface with no data.
`day` is a calendar day in the surface's time zone, the zone the executor runs it in (the
program's, else the lane's pinned one, else UTC). Its parts:

- sessions: the day's windows (the plan's, then the ones a reschedule added) and what came of
  each. `planned` counts the plan's windows, `rescheduled` the ones a reschedule added, and `run`
  the windows that started.
- operations, per kind: `planned` is the plan's count and `moved_in` the operations a reschedule
  moved into this day; `attempted` counts every operation that reached a connector (the
  executor's SPENT statuses), split into verified, unverified, failed and awaiting_approval
  (`no_outcome`: written right before the connector call, with no outcome yet); `skipped` with the
  reasons counted; `pending` operations still waiting for their window.
- caps, per kind: `used` counts this engine's own operations (store.count_ops: every SPENT
  operation of the local day); `account` is the action ledger's count of that kind on the
  surface's account that day, split by actor (agent, flow, person; agentos.policy.ledger), and
  `counted` is what the executor holds against the cap: the larger of `used` and agent + flow (a
  person's own clicks never count). Both are None on a node with no ledger. `cap` is the day's cap and `hourly`
  the rolling hour's cap as the executor applies them (caps.day_limits, the executor's own rule:
  the plan's, never above the owner's current cap or the hard ceiling, the hour's never above the
  day's; None when the day has no saved plan), and `ceiling` the site's hard ceiling.
- drafts awaiting the owner's approval (operations of the day still `awaiting_approval` whose words
  came from the draft provider), with ids; the drafts the provider wrote that day.
- awaiting_now: every operation of the surface awaiting approval right now, whatever its day
  (Home's count), with the waiting drafts' ids.
- drafts that failed (a run may have acted) or expired that day, and whether the draft provider
  is paused now (a `draft_provider` hold: text then comes only from items, nothing else is held).
- items submitted, used, failed, expired and withdrawn that day, and how many are queued and
  awaiting an approval now.
- holds in force at any time of the day (with their scope: the surface, one kind, or only the
  draft provider; whether each was created that day and is still
  in force now), the signals sites showed that day, and the day's notices (`at` in UTC ending in
  Z). A notice can name another account (the sync audit names both of a pair): `digest` and
  `record_for` replace the accounts a caller may not see with "another account".

Draft text is untrusted (a provider wrote it) and can be sensitive, so a record carries none by
default. `include_text=True` adds a short preview per waiting draft, for the owner's panel only;
the MCP tool and the CLI never ask for it.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from agentos.core.errors import InvalidRequest, NotFound
from agentos.engage.caps import Kind, day_limits, hard_ceiling, site_kinds
from agentos.engage.models import DayPlan, Program, ProgramState, ProgramVersion
from agentos.engage.store import SESSION_STATUSES, SPENT, EngageStore, iso

SCHEMA = 1
PREVIEW_CHARS = 80
WEEK_DAYS = 7
KIND_ORDER = [k.value for k in Kind]
SIGNAL_PREFIX = "signal: "
DAYS_BACK = 400                  # how far back a day may be asked for
DAYS_AHEAD = 1                   # and ahead (a zone east of UTC is already on tomorrow)
DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
OTHER_ACCOUNT = "another account"


# ---------------------------------------------------------------- the record

class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SessionLine(_Model):
    idx: int
    origin: str                      # "plan" or "reschedule"
    planned_start: str | None        # UTC ISO 8601
    minutes: float | None
    started_at: str | None
    ended_at: str | None
    status: str                      # one of SESSION_STATUSES ("scheduled" when it never opened)
    end_reason: str | None
    operations: int


class Sessions(_Model):
    planned: int = 0                 # the plan's windows
    rescheduled: int = 0             # windows a reschedule added
    run: int = 0
    scheduled: int = 0
    running: int = 0
    done: int = 0
    ended_early: int = 0
    aborted: int = 0
    refused: int = 0
    missed: int = 0
    windows: list[SessionLine] = Field(default_factory=list)


class KindOps(_Model):
    kind: str
    planned: int = 0
    moved_in: int = 0
    attempted: int = 0
    verified: int = 0
    unverified: int = 0
    failed: int = 0
    awaiting_approval: int = 0
    no_outcome: int = 0
    held: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = Field(default_factory=dict)
    pending: int = 0
    failed_reasons: dict[str, int] = Field(default_factory=dict)   # the run's error code, or "signal: <x>"
    held_reasons: dict[str, int] = Field(default_factory=dict)     # why the node held the run


class Totals(_Model):
    planned: int = 0
    attempted: int = 0
    verified: int = 0
    failed: int = 0
    awaiting_approval: int = 0
    skipped: int = 0


class AccountUse(_Model):
    """The account's actions of a kind that day on every path (agentos.policy.ledger), by who drove it."""
    agent: int = 0                   # an MCP agent or a pilot run
    flow: int = 0                    # a flow, a connector run, a workflow step, an engagement operation
    person: int = 0                  # the owner in control: shown, never counted against a cap


class CapUse(_Model):
    kind: str
    used: int
    cap: int | None
    ceiling: int
    hourly: int | None = None        # the rolling hour's cap the executor applies (None: no plan)
    account: AccountUse | None = None   # the ledger's count for the account (None: no ledger on this node)
    counted: int | None = None       # what the cap counts: the larger of `used` and agent + flow


class WaitingDraft(_Model):
    id: str
    kind: str
    approval_id: str | None
    target: str | None
    created_at: str
    run_id: str | None
    preview: str | None = None       # only with include_text (the owner in person in the panel)
    day: str | None = None           # the operation's local day (in awaiting_now)


class Drafts(_Model):
    awaiting_approval: int = 0
    waiting: list[WaitingDraft] = Field(default_factory=list)
    created: int = 0
    failed: int = 0                  # settled that day: a run may have acted, so never offered again
    expired: int = 0                 # settled that day: nobody used it in time, or its card expired
    provider_paused: bool = False    # a draft_provider hold is in force now: text comes only from items


class AwaitingNow(_Model):
    """Every operation of the surface awaiting approval now, whatever its day."""
    count: int = 0
    days: dict[str, int] = Field(default_factory=dict)
    drafts: list[WaitingDraft] = Field(default_factory=list)


class Items(_Model):
    submitted: int = 0
    used: int = 0
    failed: int = 0                  # settled that day: its run may have acted, so it is left for the owner
    expired: int = 0
    withdrawn: int = 0
    queued_now: int = 0
    awaiting_now: int = 0            # taken, and its run waits for an approval card now


class HoldLine(_Model):
    id: int
    scope: str                       # "surface", "kind" (one operation kind) or "drafts" (the draft provider)
    kind: str | None
    signal: str | None
    reason: str
    detail: str | None = None        # node-written: for no_valid_rule each kind and its problem code
    since: str
    until: str | None
    cleared_at: str | None
    created_that_day: bool
    active_now: bool


class SignalSeen(_Model):
    signal: str
    at: str
    kind: str | None
    session: int | None


class NoticeLine(_Model):
    at: str
    kind: str
    text: str


class ProgramRef(_Model):
    id: str
    version: int
    approved: bool


class DayRecord(_Model):
    """One surface's day. Stable JSON (`schema` 1): fields are only ever added."""
    schema_version: Literal[1] = Field(SCHEMA, alias="schema")
    surface: str
    site: str | None
    day: date
    timezone: str
    timezone_fallback: str | None = None   # why the day runs in UTC (no zone in the program or the lane)
    program: ProgramRef | None
    sessions_on: bool | None = None  # the node switch engage_sessions, when the caller knows it
    plan_saved: bool = False
    quiet: bool = False
    phase: str | None = None
    sessions: Sessions = Field(default_factory=Sessions)
    totals: Totals = Field(default_factory=Totals)
    operations: list[KindOps] = Field(default_factory=list)
    caps: list[CapUse] = Field(default_factory=list)
    drafts: Drafts = Field(default_factory=Drafts)
    awaiting_now: AwaitingNow = Field(default_factory=AwaitingNow)
    items: Items = Field(default_factory=Items)
    holds: list[HoldLine] = Field(default_factory=list)
    signals: list[SignalSeen] = Field(default_factory=list)
    notices: list[NoticeLine] = Field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------- helpers

def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    t = datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _within(ts: str | None, start: datetime, end: datetime) -> bool:
    t = _parse(ts)
    return t is not None and start <= t < end


def _z(ts: str) -> str:
    """A stored timestamp as UTC ISO 8601 with millisecond precision and a Z, which every
    browser parses (Safari refuses microseconds)."""
    t = _parse(ts)
    return ts if t is None else t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_day(text: str | None) -> date | None:
    """A YYYY-MM-DD day, or None for the default (yesterday). Only that form: no 20261104, no ISO
    week. `check_day` keeps it within the range the digest reads."""
    if text is None or not str(text).strip():
        return None
    raw = str(text).strip()
    try:
        if not DAY_RE.fullmatch(raw):
            raise ValueError(raw)
        return date.fromisoformat(raw)
    except ValueError:
        raise InvalidRequest(f"day is a calendar day as YYYY-MM-DD, not {raw[:40]!r}") from None


def check_day(day: date | None, now: datetime) -> date | None:
    """`day`, when it is at most DAYS_BACK days before today (UTC) and DAYS_AHEAD after it."""
    if day is None:
        return None
    today = now.astimezone(UTC).date()
    if not today - timedelta(days=DAYS_BACK) <= day <= today + timedelta(days=DAYS_AHEAD):
        raise InvalidRequest(f"day is within the last {DAYS_BACK} days, not {day.isoformat()}")
    return day


def program_of(store: EngageStore, surface: str) -> ProgramVersion | None:
    """The surface's approved program, else its latest draft (as the service's program_for)."""
    active = store.active(surface)
    if active is not None:
        return active
    found = store.programs(surface)
    return found[-1] if found else None


def day_caps(store: EngageStore, program: Program, plan: DayPlan | None) -> tuple[dict[str, int], dict[str, int]]:
    """The day's and the hour's caps exactly as the executor enforces them (caps.day_limits, the
    rule Executor._limits uses): the plan's, never above the owner's current cap or the site's hard
    ceiling, the hour's never above the day's."""
    return day_limits(program.site or "", plan.caps if plan else None, plan.hourly if plan else None,
                      store.caps(program.surface))


def _kind_key(kind: str) -> tuple[int, str]:
    return (KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER), kind)


def _ceiling(site: str, kind: str) -> int:
    try:
        return hard_ceiling(site, Kind(kind))
    except ValueError:
        return 0


def _bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The local day as a UTC [start, end)."""
    return (datetime.combine(day, dtime(0), tzinfo=tz).astimezone(UTC),
            datetime.combine(day + timedelta(days=1), dtime(0), tzinfo=tz).astimezone(UTC))


# ---------------------------------------------------------------- reading one day

def day_record(store: EngageStore, surface: str, day: date | None = None, *, tz: ZoneInfo | None = None,
               now: datetime | None = None, sessions_on: bool | None = None,
               include_text: bool = False, timezone_fallback: str | None = None,
               account: Callable[[str, date, ZoneInfo], dict[str, int] | None] | None = None) -> DayRecord:
    """One surface's `day` (default: yesterday in its time zone). Reads only. `account(kind, day,
    tz)` is the ledger's count of that kind on the surface's account, by actor (ledger_counts)."""
    pv = program_of(store, surface)
    if pv is None:
        raise NotFound(f"{surface} has no engagement program")
    program = pv.definition
    site = program.site or ""
    tz = tz or ZoneInfo(program.timezone or "UTC")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if day is None:
        day = now.astimezone(tz).date() - timedelta(days=1)
    start, end = _bounds(day, tz)

    saved = store.plans(surface, day, day)
    plan = saved[0] if saved else None
    rows = store.sessions(surface, day)
    ops = store.ops(surface, day)
    sessions, operations, totals = _activity(plan, rows, ops)

    # caps: what the executor counts and allows
    limits, hourly = day_caps(store, program, plan)
    cap_kinds = sorted({str(k) for k in site_kinds(site)} | set(limits)
                       | {k.kind for k in operations if k.kind in KIND_ORDER}, key=_kind_key)
    caps = []
    for k in cap_kinds:
        used = store.count_ops(surface, k, day=day)
        seen = account(k, day, tz) if account is not None else None
        use = AccountUse(**seen) if seen is not None else None
        caps.append(CapUse(kind=k, used=used, cap=(limits.get(k, 0) if plan is not None else None),
                           ceiling=_ceiling(site, k), hourly=(hourly.get(k, limits.get(k, 0)) if plan is not None else None),
                           account=use, counted=max(used, use.agent + use.flow) if use is not None else None))

    return DayRecord(
        surface=surface, site=program.site, day=day, timezone=tz.key, timezone_fallback=timezone_fallback,
        program=ProgramRef(id=program.id, version=pv.version, approved=pv.state is ProgramState.APPROVED),
        sessions_on=sessions_on, plan_saved=plan is not None, quiet=bool(plan and plan.quiet),
        phase=plan.phase if plan else None, sessions=sessions, totals=totals, operations=operations, caps=caps,
        drafts=_drafts(store, surface, day, start, end, now, include_text=include_text),
        awaiting_now=_awaiting_now(store, surface, include_text=include_text),
        items=_items(store, surface, start, end),
        holds=_holds(_hold_rows(store, surface, start, end), start, end, now),
        signals=_signals(_signal_rows(store, surface, start, end), day, start, end),
        notices=_notices(store, surface, start, end))


def _activity(plan: DayPlan | None, rows: list[dict[str, Any]],
              ops: list[dict[str, Any]]) -> tuple[Sessions, list[KindOps], Totals]:
    """A day's sessions and operations from its plan, its session rows and its operation rows."""
    by_idx = {r["idx"]: r for r in rows}
    ops_by_session: Counter[int] = Counter(o["session_id"] for o in ops)

    # sessions: the plan's windows, then the reschedules
    windows: list[SessionLine] = []
    for i, s in enumerate(plan.sessions if plan else []):
        r = by_idx.get(i)
        windows.append(_line(i, "plan", r, planned_start=iso(s.start), minutes=s.minutes,
                             n_ops=ops_by_session[r["id"]] if r else sum(s.actions.values())))
    planned_idx = set(range(len(plan.sessions))) if plan else set()
    for idx, r in sorted(by_idx.items()):
        if idx in planned_idx:
            continue
        windows.append(_line(idx, r["origin"], r, planned_start=r["planned_start"], minutes=r["minutes"],
                             n_ops=ops_by_session[r["id"]]))
    statuses = Counter(w.status for w in windows)
    rescheduled = sum(1 for w in windows if w.origin == "reschedule")
    sessions = Sessions(planned=len(windows) - rescheduled, rescheduled=rescheduled,
                        run=sum(1 for w in windows if w.started_at), windows=windows,
                        **{s: statuses.get(s, 0) for s in SESSION_STATUSES})

    # operations per kind
    resched = {r["id"] for r in rows if r["origin"] == "reschedule"}
    planned = {str(k): n for k, n in (plan.totals() if plan else {}).items()}
    kinds = sorted(set(planned) | {o["kind"] for o in ops}, key=_kind_key)
    per: dict[str, KindOps] = {k: KindOps(kind=k, planned=planned.get(k, 0)) for k in kinds}
    for o in ops:
        k = per[o["kind"]]
        st = o["status"]
        if o["session_id"] in resched:
            k.moved_in += 1
        if st in SPENT:
            k.attempted += 1
        if st == "attempted":
            k.no_outcome += 1
        elif st == "skipped":
            k.skipped += 1
            reason = o["reason"] or "unknown"
            k.skip_reasons[reason] = k.skip_reasons.get(reason, 0) + 1
        elif st == "planned":
            k.pending += 1
        elif st in ("verified", "unverified", "failed", "awaiting_approval", "held"):
            setattr(k, st, getattr(k, st) + 1)
            if st in ("failed", "held"):
                why = getattr(k, f"{st}_reasons")
                reason = o["reason"] or st
                why[reason] = why.get(reason, 0) + 1
    operations = [per[k] for k in kinds]
    for k in operations:
        k.skip_reasons = dict(sorted(k.skip_reasons.items()))
        k.failed_reasons = dict(sorted(k.failed_reasons.items()))
        k.held_reasons = dict(sorted(k.held_reasons.items()))
    totals = Totals(**{f: sum(getattr(k, f) for k in operations) for f in Totals.model_fields})
    return sessions, operations, totals


def _line(idx: int, origin: str, row: dict[str, Any] | None, *, planned_start: str | None,
          minutes: float | None, n_ops: int) -> SessionLine:
    return SessionLine(idx=idx, origin=origin, planned_start=row["planned_start"] if row else planned_start,
                       minutes=row["minutes"] if row else minutes,
                       started_at=row["started_at"] if row else None, ended_at=row["ended_at"] if row else None,
                       status=row["status"] if row else "scheduled", end_reason=row["end_reason"] if row else None,
                       operations=n_ops)


def _waiting_drafts(store: EngageStore, surface: str, day: date | None, *, include_text: bool) -> list[WaitingDraft]:
    """Drafts whose operation awaits approval: of `day`, or of any day (then with the day)."""
    sql = ("SELECT d.id, d.kind, d.approval_id, COALESCE(d.target, o.target) AS target, d.created_at, "
           "COALESCE(d.run_id, o.run_id) AS run_id, d.text, o.day FROM ops o JOIN drafts d "
           "ON o.content_origin = 'draft' AND o.content_id = d.id "
           "WHERE o.surface = ? AND o.status = 'awaiting_approval'")
    args: tuple = (surface,)
    if day is not None:
        sql, args = sql + " AND o.day = ?", (surface, day.isoformat())
    out = []
    for d in store._dicts(sql + " ORDER BY o.day, o.session_id, o.seq", args):
        text = " ".join(str(d.pop("text") or "").split())
        op_day = d.pop("day")
        preview = (text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS - 3].rstrip() + "...") \
            if include_text else None
        out.append(WaitingDraft(**d, preview=preview, day=None if day is not None else op_day))
    return out


def _drafts(store: EngageStore, surface: str, day: date, start: datetime, end: datetime, now: datetime, *,
            include_text: bool) -> Drafts:
    from agentos.engage.drafts import HOLD_KIND

    out = _waiting_drafts(store, surface, day, include_text=include_text)
    created = store._rows("SELECT COUNT(*) FROM drafts WHERE surface = ? AND created_at >= ? AND created_at < ?",
                          (surface, iso(start), iso(end)))[0][0]
    settled = {r["status"]: int(r["n"]) for r in store._dicts(
        "SELECT status, COUNT(*) AS n FROM drafts WHERE surface = ? AND status IN ('failed', 'expired') "
        "AND settled_at >= ? AND settled_at < ? GROUP BY status", (surface, iso(start), iso(end)))}
    paused = any(h["kind"] == HOLD_KIND for h in store.holds(surface, at=now))
    return Drafts(awaiting_approval=len(out), waiting=out, created=int(created), failed=settled.get("failed", 0),
                  expired=settled.get("expired", 0), provider_paused=paused)


def _awaiting_now(store: EngageStore, surface: str, *, include_text: bool) -> AwaitingNow:
    days = {r["day"]: int(r["n"]) for r in store._dicts(
        "SELECT day, COUNT(*) AS n FROM ops WHERE surface = ? AND status = 'awaiting_approval' "
        "GROUP BY day ORDER BY day", (surface,))}
    return AwaitingNow(count=sum(days.values()), days=days,
                       drafts=_waiting_drafts(store, surface, None, include_text=include_text))


def _items(store: EngageStore, surface: str, start: datetime, end: datetime) -> Items:
    """Bounded by time in SQL (a day of slack each way for stored formats), exact in Python."""
    lo, hi = iso(start - timedelta(days=1)), iso(end + timedelta(days=1))
    out = Items()
    for r in store._dicts("SELECT status, submitted_at, settled_at FROM items WHERE surface = ? AND "
                          "((submitted_at >= ? AND submitted_at < ?) OR (settled_at >= ? AND settled_at < ?))",
                          (surface, lo, hi, lo, hi)):
        if _within(r["submitted_at"], start, end):
            out.submitted += 1
        if r["status"] in ("used", "failed", "expired", "withdrawn") and _within(r["settled_at"], start, end):
            setattr(out, r["status"], getattr(out, r["status"]) + 1)
    now = {r["status"]: int(r["n"]) for r in store._dicts(
        "SELECT status, COUNT(*) AS n FROM items WHERE surface = ? AND status IN ('queued', 'awaiting') "
        "GROUP BY status", (surface,))}
    out.queued_now, out.awaiting_now = now.get("queued", 0), now.get("awaiting", 0)
    return out


def _hold_rows(store: EngageStore, surface: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Holds that overlap [start, end) (holds keep `iso` timestamps, so text compares exactly)."""
    return store._dicts(
        "SELECT * FROM holds WHERE surface = ? AND since < ? AND (cleared_at IS NULL OR cleared_at > ?) "
        "AND (until IS NULL OR until > ?) ORDER BY id", (surface, iso(end), iso(start), iso(start)))


def _scope(kind: str | None) -> str:
    """What a hold stops: the whole surface (no kind), one operation kind, or only the draft
    provider (kind `draft_provider`: text then comes only from items, nothing else is held)."""
    from agentos.engage.drafts import HOLD_KIND

    return "surface" if kind is None else "drafts" if kind == HOLD_KIND else "kind"


def _holds(rows: list[dict[str, Any]], start: datetime, end: datetime, now: datetime) -> list[HoldLine]:
    out = []
    for h in rows:
        since, until, cleared = _parse(h["since"]), _parse(h["until"]), _parse(h["cleared_at"])
        if since is None or since >= end or (cleared is not None and cleared <= start) \
                or (until is not None and until <= start):
            continue
        active = cleared is None and (until is None or until > now)
        out.append(HoldLine(id=h["id"], scope=_scope(h["kind"]), kind=h["kind"], signal=h["signal"], reason=h["reason"],
                            detail=h.get("detail"), since=h["since"],
                            until=h["until"], cleared_at=h["cleared_at"], created_that_day=start <= since < end,
                            active_now=active))
    return sorted(out, key=lambda h: (h.since, h.id))


def _signal_rows(store: EngageStore, surface: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """The history's operation entries around [start, end) (a day of slack each way: an entry
    is the day's by its session, which can start before the day or end after it)."""
    return store._dicts("SELECT at, payload FROM history WHERE surface = ? AND kind = 'op' AND at >= ? AND at < ? "
                        "ORDER BY id", (surface, iso(start - timedelta(days=1)), iso(end + timedelta(days=1))))


def _signals(rows: list[dict[str, Any]], day: date, start: datetime, end: datetime) -> list[SignalSeen]:
    """Signals from the history's operation entries of the day's sessions (an operation's own row
    can lose its reason when a reschedule drops it)."""
    out = []
    for r in rows:
        p = json.loads(r["payload"] or "{}")
        reason = p.get("reason") or ""
        session = p.get("session")
        if not isinstance(reason, str) or not reason.startswith(SIGNAL_PREFIX):
            continue
        if isinstance(session, list) and len(session) == 2:
            if session[0] != day.isoformat():
                continue
            idx = session[1] if isinstance(session[1], int) else None
        elif _within(r["at"], start, end):
            idx = None
        else:
            continue
        out.append(SignalSeen(signal=reason[len(SIGNAL_PREFIX):], at=r["at"], kind=p.get("kind"), session=idx))
    return out


def _notices(store: EngageStore, surface: str, start: datetime, end: datetime) -> list[NoticeLine]:
    """Bounded by time in SQL (a day of slack each way: stored `at` has microseconds), exact in Python."""
    rows = store._dicts("SELECT at, kind, text FROM notices WHERE surface = ? AND at >= ? AND at < ? ORDER BY id",
                        (surface, iso(start - timedelta(days=1)), iso(end + timedelta(days=1))))
    return [NoticeLine(at=_z(r["at"]), kind=r["kind"], text=r["text"]) for r in rows if _within(r["at"], start, end)]


def redact_notices(rec: DayRecord, surfaces: Iterable[str], can_see: Callable[[str], bool] | None) -> DayRecord:
    """Replace every other account in the record's notices that `can_see` refuses with "another
    account" (None: the caller sees every account). The stored notices stay as they are."""
    if can_see is None:
        return rec
    hidden = sorted({s for s in surfaces if s and s != rec.surface and not _sees(can_see, s)}, key=len, reverse=True)
    if not hidden or not rec.notices:
        return rec
    pattern = re.compile(r"(?<![\w./-])(?:" + "|".join(re.escape(s) for s in hidden) + r")(?![\w/-])")
    for n in rec.notices:
        n.text = pattern.sub(OTHER_ACCOUNT, n.text)
    return rec


def _sees(can_see: Callable[[str], bool], surface: str) -> bool:
    try:
        return bool(can_see(surface))
    except Exception:  # noqa: BLE001  (a check that cannot decide hides the account)
        return False


# ---------------------------------------------------------------- the service's view

def visible_surfaces(service: Any, surface: str | None = None, can_see: Callable[[str], bool] | None = None) -> list[str]:
    """Every surface with a program that the caller may see (as engage_status lists them)."""
    out: list[str] = []
    for pv in service.store.programs():
        s = pv.definition.surface
        if s in out or (surface and s != surface) or (can_see is not None and not _sees(can_see, s)):
            continue
        out.append(s)
    if surface and not out:
        raise NotFound(f"{surface} has no engagement program")
    return out


def _sessions_on(service: Any) -> bool | None:
    """The node switch engage_sessions (node.toml), which the CLI reads as the daemon does."""
    enabled = getattr(service, "sessions_enabled", None)
    return bool(enabled()) if callable(enabled) else None


def _all_surfaces(service: Any) -> set[str]:
    return {pv.definition.surface for pv in service.store.programs()}


def ledger_counts(service: Any, surface: str) -> Callable[[str, date, ZoneInfo], dict[str, int] | None] | None:
    """The action ledger's day count for the surface's account (the lane it acts on), per actor,
    as the executor's caps read it (Executor.used): None when the node has no ledger."""
    runtime = getattr(service, "runtime", None)
    ledger = runtime.get("ledger") if runtime is not None else None
    if ledger is None:
        return None
    from agentos.engage.executor import LEDGER_KINDS

    executor = getattr(service, "executor", None)
    try:
        lane = executor.lanes.lane_for(surface) if executor is not None else surface
    except Exception:  # noqa: BLE001 - a surface the node no longer has: its own name
        lane = surface

    def count(kind: str, day: date, tz: ZoneInfo) -> dict[str, int] | None:
        ledger_kind = LEDGER_KINDS.get(kind)
        if ledger_kind is None:
            return None
        try:
            return {a: ledger.day_count(lane, ledger_kind, day, tz, actors=(a,)) for a in ("agent", "flow", "person")}
        except Exception:  # noqa: BLE001 - the digest still reads without it
            return None

    return count


def record_for(service: Any, surface: str, day: date | None = None, *, include_text: bool = False,
               now: datetime | None = None, can_see: Callable[[str], bool] | None = None,
               surfaces: Iterable[str] | None = None) -> DayRecord:
    """One surface's day through the service: its time zone as the executor resolves it. With
    `can_see`, the accounts the caller may not see are hidden in the notices."""
    pv = program_of(service.store, surface)
    if pv is None:
        raise NotFound(f"{surface} has no engagement program")
    now = now or service.now()
    resolve = getattr(service, "_timezone", None)
    fallback = resolve(pv.definition)[1] if callable(resolve) else None
    rec = day_record(service.store, surface, check_day(day, now), tz=service.timezone(pv.definition), now=now,
                     sessions_on=_sessions_on(service), include_text=include_text, timezone_fallback=fallback,
                     account=ledger_counts(service, surface))
    return redact_notices(rec, _all_surfaces(service) if surfaces is None else surfaces, can_see)


def digest(service: Any, surface: str | None = None, day: date | None = None, *,
           can_see: Callable[[str], bool] | None = None, include_text: bool = False,
           now: datetime | None = None) -> dict[str, Any]:
    """The digest of every visible surface (or one): each surface's `day`, by default its yesterday.
    `can_see` filters the list when no surface is named, and hides other accounts in notices."""
    now = now or service.now()
    check_day(day, now)
    names = visible_surfaces(service, surface, None if surface else can_see)
    known = _all_surfaces(service)
    return {"schema": SCHEMA, "sessions_on": _sessions_on(service),
            "surfaces": [record_for(service, s, day, include_text=include_text, now=now, can_see=can_see,
                                    surfaces=known).public() for s in names]}


def _summary(day: date, sessions: Sessions, totals: Totals, holds: int, signals: int) -> dict[str, Any]:
    return {"day": day.isoformat(), "sessions_planned": sessions.planned,
            "sessions_rescheduled": sessions.rescheduled, "sessions_run": sessions.run,
            "attempted": totals.attempted, "verified": totals.verified,
            "awaiting_approval": totals.awaiting_approval, "holds": holds, "signals": signals}


def _holding(holds: list[HoldLine]) -> int:
    """Holds that stopped something: of the surface or a kind (a draft provider pause holds nothing)."""
    return sum(1 for h in holds if h.scope != "drafts")


def summary(rec: DayRecord) -> dict[str, Any]:
    """One day in one line, for the week strip."""
    return _summary(rec.day, rec.sessions, rec.totals, _holding(rec.holds), len(rec.signals))


def week(service: Any, surface: str, end_day: date | None = None, *, days: int = WEEK_DAYS,
         now: datetime | None = None) -> list[dict[str, Any]]:
    """The `days` days up to and including `end_day` (default: the surface's yesterday), oldest
    first, one summary each. One read per table for the whole week (summary(day_record(...)) of
    each day gives the same lines)."""
    pv = program_of(service.store, surface)
    if pv is None:
        raise NotFound(f"{surface} has no engagement program")
    store = service.store
    tz = service.timezone(pv.definition)
    now = (now or service.now()).astimezone(UTC)
    end_day = check_day(end_day, now) or now.astimezone(tz).date() - timedelta(days=1)
    first = end_day - timedelta(days=days - 1)
    span = (surface, first.isoformat(), end_day.isoformat())
    plans = {p.day: p for p in store.plans(surface, first, end_day)}
    rows = store._dicts("SELECT * FROM sessions WHERE surface = ? AND day >= ? AND day <= ? ORDER BY day, idx", span)
    ops = store._dicts("SELECT * FROM ops WHERE surface = ? AND day >= ? AND day <= ? "
                       "ORDER BY day, session_id, seq", span)
    wstart, wend = _bounds(first, tz)[0], _bounds(end_day, tz)[1]
    hold_rows = _hold_rows(store, surface, wstart, wend)
    signal_rows = _signal_rows(store, surface, wstart, wend)
    out = []
    for n in range(days):
        d = first + timedelta(days=n)
        start, end = _bounds(d, tz)
        sessions, _, totals = _activity(plans.get(d), [r for r in rows if r["day"] == d.isoformat()],
                                        [o for o in ops if o["day"] == d.isoformat()])
        out.append(_summary(d, sessions, totals, _holding(_holds(hold_rows, start, end, now)),
                            len(_signals(signal_rows, d, start, end))))
    return out


# ---------------------------------------------------------------- text

SKIP_WORDS = {
    "held": "held", "cap_daily": "day's cap", "cap_hourly": "hour's cap", "mode_off": "turned off",
    "no_content": "no words", "draft_unavailable": "draft provider down", "no_target": "nothing to act on",
    "lane_unavailable": "lane busy", "no_valid_rule": "no checked rule", "ended_early": "session ended early",
    "rescheduled": "moved", "week_over": "no room this week", "missed": "missed", "error": "error",
    "interrupted": "node stopped", "lane_lost": "lane lost mid-session",
    "bad_target": "target it cannot act on", "profile_current": "profile already current",
    "photo_unavailable": "photo not in the lane yet",
}

# why a run failed or was held (the connector's codes besides these show as they are)
RUN_WORDS = {
    "foreign_run": "another run had its key", "signal, then week_over": "signal, then no room this week",
    "timeout": "timed out", "connector_broken": "connector broken", "pace_limited": "paced by the node",
    "not_started": "not started in time", "failed": "failed", "held": "held",
    "already_current": "page already showed the value", "denied": "you denied it",
    "approval_expired": "approval expired",
}

# why an operation may not run (agentos.engage.operations.Registry.problem and .reason), as a
# no_valid_rule hold's detail names them
PROBLEM_WORDS = {
    "no_spec": "no operation for this site", "no_rule": "no egress rule", "rule_unverified": "rule not verified",
    "no_flow": "connector not installed", "op_unverified": "no canary verified it",
    "verified_stale": "changed since its canary",
}


def detail_words(detail: str) -> str:
    """A hold's detail with each problem code in words; anything else stays as it is."""
    return re.sub(r"[a-z_]+", lambda m: PROBLEM_WORDS.get(m.group(0), m.group(0)), detail)


def run_words(reason: str) -> str:
    if reason.startswith(SIGNAL_PREFIX):
        return "site showed " + reason[len(SIGNAL_PREFIX):].replace("_", " ")
    return RUN_WORDS.get(reason, reason)


# a session status as a count: (one, many), spelled out (a participle has no plural)
SESSION_WORDS = {
    "scheduled": ("not run", "not run"), "running": ("running", "running"), "done": ("done", "done"),
    "ended_early": ("ended early", "ended early"), "aborted": ("aborted", "aborted"),
    "refused": ("refused", "refused"), "missed": ("missed", "missed"),
}


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def render_text(rec: DayRecord | dict[str, Any]) -> str:
    """The record as plain text: the same numbers as the JSON."""
    if isinstance(rec, dict):
        rec = DayRecord.model_validate(rec)
    s, t = rec.sessions, rec.totals
    lines = [f"{rec.surface} on {rec.day.isoformat()} ({rec.timezone})"]
    if rec.timezone_fallback:
        lines.append(f"  Time zone: UTC, because {rec.timezone_fallback}.")
    if rec.program is None or not rec.program.approved:
        lines.append("  No approved program: nothing runs here.")
    if rec.sessions_on is False:
        lines.append("  Sessions are off on this node: plans are drawn, nothing is clicked.")
    if not rec.plan_saved:
        lines.append("  No plan was saved for this day.")
    elif rec.quiet:
        lines.append("  A quiet day: no sessions planned.")
    ends = [_n(getattr(s, k), *SESSION_WORDS[k]) for k in ("done", "ended_early", "aborted", "refused", "missed")
            if getattr(s, k)]
    moved = f", {s.rescheduled} rescheduled" if s.rescheduled else ""
    lines.append(f"  Sessions: {s.run} run of {s.planned} planned{moved}" + (f" ({', '.join(ends)})" if ends else ""))
    for w in s.windows:
        when = (w.started_at or w.planned_start or "").replace("+00:00", "Z")
        why = f", {w.end_reason}" if w.end_reason else ""
        status = SESSION_WORDS.get(w.status, (w.status.replace("_", " "),))[0]
        lines.append(f"    #{w.idx} {w.origin} {when}: {status}{why}, {_n(w.operations, 'operation', 'operations')}")
    lines.append(f"  Operations: {t.verified} verified of {t.attempted} attempted, {t.planned} planned, "
                 f"{t.awaiting_approval} awaiting approval, {t.failed} failed, {t.skipped} skipped")
    for k in rec.operations:
        parts = [f"planned {k.planned}"] + ([f"moved in {k.moved_in}"] if k.moved_in else []) + \
            [f"attempted {k.attempted}", f"verified {k.verified}"]
        for f in ("unverified", "failed", "awaiting_approval", "no_outcome", "held", "pending"):
            if getattr(k, f):
                parts.append(f"{f.replace('_', ' ')} {getattr(k, f)}")
        if k.skipped:
            why = ", ".join(f"{SKIP_WORDS.get(r, r)} {n}" for r, n in k.skip_reasons.items())
            parts.append(f"skipped {k.skipped} ({why})")
        for f in ("failed", "held"):
            reasons = getattr(k, f"{f}_reasons")
            if reasons:
                parts.append(f"{f} because " + ", ".join(f"{run_words(r)} {n}" for r, n in reasons.items()))
        lines.append(f"    {k.kind}: " + ", ".join(parts))
    if rec.caps:
        lines.append("  Caps (used of the day's cap, hard ceiling; the account's own count on every path):")
        for c in rec.caps:
            cap = "no plan" if c.cap is None else str(c.cap)
            line = f"    {c.kind}: {c.used if c.counted is None else c.counted} of {cap}, ceiling {c.ceiling}"
            if c.account is not None:
                a = c.account
                line += (f" (on the account: {a.agent} by agents, {a.flow} by flows and engagement, "
                         f"{a.person} by you, which never count)")
            lines.append(line)
    d = rec.drafts
    lines.append(f"  Drafts: {d.awaiting_approval} awaiting approval, {d.created} written that day"
                 + (f", {d.failed} failed" if d.failed else "") + (f", {d.expired} expired" if d.expired else ""))
    if d.provider_paused:
        lines.append("    The draft provider is paused now: text comes only from items until the next session.")
    for w in d.waiting:
        lines.append(f"    {w.id} {w.kind}" + (f" on {w.target}" if w.target else "")
                     + (f", approval {w.approval_id}" if w.approval_id else ""))
    lines.append(f"  Awaiting approval now (any day): {rec.awaiting_now.count}")
    i = rec.items
    lines.append(f"  Items that day: {i.used} used, {i.failed} failed, {i.expired} expired, {i.submitted} submitted, "
                 f"{i.withdrawn} withdrawn. Waiting now: {i.queued_now}"
                 + (f", {i.awaiting_now} awaiting approval" if i.awaiting_now else ""))
    if rec.holds:
        lines.append("  Holds:")
        for h in rec.holds:
            what = h.reason + (" (draft provider only: nothing else is held)" if h.scope == "drafts"
                               else f" ({h.kind} only: the rest goes on)" if h.kind else "")
            if h.detail and h.reason == "no_valid_rule":
                what += f": {detail_words(h.detail)}"
            state = "in force now" if h.active_now else (f"cleared {h.cleared_at}" if h.cleared_at
                                                         else f"ended {h.until}")
            lines.append(f"    {what}, since {h.since}, {state}")
    else:
        lines.append("  Holds: none")
    if rec.signals:
        lines.append("  Signals: " + ", ".join(f"{g.signal} at {g.at}" + (f" ({g.kind})" if g.kind else "")
                                               for g in rec.signals))
    if rec.notices:
        lines.append("  Notices:")
        lines += [f"    {n.at[:16].replace('T', ' ')} UTC {n.text}" for n in rec.notices]
    return "\n".join(lines)
