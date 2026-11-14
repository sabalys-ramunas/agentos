"""The program executor (brief B1): approved programs run as scheduled sessions.

    ex = Executor(store, registry=..., operations=..., targets=..., content=..., lanes=...,
                  clock=time.time, plan_for=service.day_plan)
    ex.windows(surface, day)                 # the day's windows: the plan's sessions, then reschedules
    ex.due(now)                              # windows whose start has come (and marks old ones missed)
    ex.preview(surface, day, idx)            # what a window would do, acting on nothing
    await ex.run_window(surface, day, idx)   # run one window; returns its report
    await ex.reconcile()                     # rows whose outcome came after their window
    ex.holds(surface) / ex.clear_hold(surface, by=owner)

A **window** is one session of a DayPlan (`idx` is its index in plan.sessions), or a session a
reschedule added (idx after the plan's). Its operations are the session's action counts,
expanded and shuffled with the persona's seed (`rng(seed, redraws, "order", day, idx)`), so the
same seed always gives the same operations in the same order.

`run_window(surface, day, idx)`:

1. Refuse when the surface has no approved program (InvalidRequest), and return the saved report
   when the window already ran (a replay never acts twice). A surface hold refuses the window:
   its operations are skipped with reason `held`.
   A held window's operations are not carried forward: a `dropped` history entry and a notice say so.
2. Claim the surface's lane (LanePort) as `engage:<surface>:<nonce>` (a fresh random nonce per
   window); a lane that cannot be had refuses the window (`lane_unavailable`). The claim is renewed
   before every operation and every RENEW_EVERY_S of the session's own clock, pauses and the time
   operations take alike (a lost claim ends the window, `lane_lost`, and the operations it had not
   run move to later sessions, LANE_LOST_GAP on at the earliest, as a signal's do), and released in
   a `finally`, whatever happens. A cancelled window (the node stopping) closes as `aborted`;
   `recover()` closes sessions a crash left `running` (`interrupted`).
3. Every operation must be valid (agentos.engage.operations.Registry): its flow installed, its
   egress rule verified and the operation itself verified by a canary. One that is not aborts the
   whole window before anything acts, and holds the surface with reason `no_valid_rule` (the
   detail names each kind's problem: no_flow, op_unverified, ...).
4. Each operation in order, spread over the session's minutes: a kind hold, the day's cap and the
   rolling hour's cap (caps.day_limits: the DayPlan's, which the governor drew, never above the
   owner's current cap or the site's hard ceiling) can skip it, counted as the larger of this
   engine's own operations and the account's actions on every path (agentos.policy.ledger: an
   agent, a pilot run, a flow or a workflow on the same lane; never the owner's own clicks); text operations
   take content (ContentPort) and need a target (the content's, else TargetPort, which may read
   more candidates through the window's claim; a target the operation cannot use is skipped as
   `bad_target`); then it runs
   through the OperationPort with a deterministic idempotency key (tagged with the node's secret). Comments in approve mode, and
   any draft that is not already approved, run with `ask=True`: the owner approves them through
   the node's approvals; `awaiting_approval` is recorded and the window goes on. `auto` is never
   honored on LinkedIn or Reddit.
5. A signal (captcha, checkpoint, logged_out, blocked, rate_limited, wrong_account) holds the
   surface (rate_limited for the 6 hour backoff, the others until the owner clears it), ends the
   window, and reschedules what is left: new windows later the same ISO week, inside the
   persona's waking hours on days that are not quiet, at least 45 minutes from other windows,
   under the day's caps and the hourly caps. What does not fit by Sunday is dropped with reason
   `week_over`.

Every operation, hold, reschedule and window end goes to the surface's history (store.history).

Profile edits (brief B2) come in as `profile_edit` operations; agentos.engage.profile says which
field each one sets (`next_edit` for step 4). Step 3 does not check them with the window's other
kinds: a due field whose operation may not run holds `profile_edit` alone (`unchecked`, reason
`no_valid_rule`, not checked at all when the day's profile_edit cap is 0), and the rest of the
window goes on. The owner's profile_edit cap applies at once (caps.day_limits, as for every kind). An edit of a
program's own `profile:` block asks the owner (`ask=True`); one whose values come from the approved profile the
account is attached to (agentos.profiles, which the owner saved or approved) does not (`ask=False`), and
`reconcile` settles profile rows with every other kind's.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from agentos.core.errors import InvalidRequest, NotFound
from agentos.engage import profile
from agentos.engage.caps import Kind, day_limits
from agentos.engage.governor import is_owner
from agentos.engage.models import APPROVE_ONLY_SITES, DayPlan, Mode, Program
from agentos.engage.operations import BadTarget, Registry
from agentos.engage.persona import Persona, rng, week_of
from agentos.engage.planner import LENGTH_MEDIAN_MIN, LENGTH_RANGE, LENGTH_SIGMA, MIN_GAP
from agentos.engage.ports import (
    CONTENT_SESSION,
    FOREIGN_RUN,
    SIGNALS,
    Content,
    ContentOrigin,
    ContentPort,
    ContentUnavailable,
    LanePort,
    LaneUnavailable,
    OperationPort,
    OpResult,
    OpStatus,
    RefillingTargets,
    TargetPort,
    claim_parent,
)
from agentos.engage.store import SPENT, EngageStore, iso

log = logging.getLogger("agentos.engage.executor")

HOLDER = "engage:"                    # lane claim holder key: engage:<surface>:<a nonce per window>
MISSED_AFTER = timedelta(minutes=30)  # a window not started this long after its start is missed
CLAIM_MARGIN_S = 300.0
RENEW_EVERY_S = 300.0                 # the lane claim is renewed at least this often, by elapsed time
KEY_SECRET = "op_key_secret"          # store meta: the node's secret in operation idempotency keys
RESCHEDULE_PER_DAY = 2                # at most this many added windows a day
SLOT_TRIES = 400
REPLY = "reply"                       # a reply to a comment on the account's own post (a later kind)
TEXT_MODE_KINDS = (str(Kind.COMMENT), REPLY)
HOUR = timedelta(hours=1)
LANE_LOST_GAP = timedelta(minutes=45)    # a session the node ended for a waiting caller resumes no sooner
APPROVED_HELD_MAX = timedelta(days=7)
SESSION_GRACE = timedelta(minutes=20)    # a running session's own runs may still start this long after its end    # an approved operation held this long ends (approved_gate)
RECHECK_UNVERIFIED = timedelta(days=2)   # unverified operations are checked again for this long
DRAFTS_NOTICE_EVERY = timedelta(hours=12)   # one notice in this long for a draft provider that is down
# engage kind -> the action ledger's kind (agentos.policy.ledger); a kind not here (a story view, a
# read) is no action the account shows anyone
LEDGER_KINDS = {"like": "like", "follow": "follow", "comment": "comment", REPLY: "comment", "post": "post",
                "join": "join", "profile_edit": "profile"}


@dataclass
class _Claim:
    lane: str
    holder: str
    ttl_s: float
    renewed_at: float = 0.0       # the clock's time of the claim or its last renewal


@dataclass(frozen=True)
class Window:
    surface: str
    day: date
    idx: int
    start: datetime
    minutes: float
    origin: str                   # "plan" or "reschedule"
    kinds: tuple[str, ...]        # the operations, in the order they run

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)

    def public(self) -> dict[str, Any]:
        return {"surface": self.surface, "day": self.day.isoformat(), "idx": self.idx, "start": self.start.isoformat(),
                "minutes": self.minutes, "origin": self.origin, "operations": list(self.kinds)}


def _parse(ts: str) -> datetime:
    t = datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


class Executor:
    def __init__(self, store: EngageStore, *, registry: Registry, operations: OperationPort, targets: TargetPort,
                 content: ContentPort, lanes: LanePort, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] | None = None,
                 plan_for: Callable[[str, date], DayPlan | None] | None = None,
                 tz_for: Callable[[Program], ZoneInfo] | None = None, ledger: Any = None):
        self.store = store
        self.ledger = ledger    # agentos.policy.ledger.ActionLedger: the account's actions on every path
        self.enabled: Callable[[], bool] | None = None   # the node switch engage_sessions (approved_gate)
        self.registry = registry
        self.operations = operations
        self.targets = targets
        self.content = content
        self.lanes = lanes
        self.clock = clock
        self.sleep = sleep or asyncio.sleep
        self.plan_for = plan_for or self._saved_plan
        self.tz_for = tz_for or (lambda program: ZoneInfo(program.timezone or "UTC"))
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------------------------------------------------------------- reading
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def _saved_plan(self, surface: str, day: date) -> DayPlan | None:
        found = self.store.plans(surface, day, day)
        return found[0] if found else None

    def _persona(self, surface: str) -> Persona:
        p = self.store.persona(surface)
        if p is None:
            raise NotFound(f"{surface} has no persona yet (draft its program first)")
        return p

    @staticmethod
    def order(persona: Persona, day: date, idx: int, actions: dict[Any, int]) -> list[str]:
        """A session's operations in the order they run: drawn from the persona's seed."""
        kinds = [str(k) for k, n in sorted(actions.items(), key=lambda kv: str(kv[0])) for _ in range(n)]
        rng(persona.seed, persona.redraws, "order", day.isoformat(), idx).shuffle(kinds)
        return kinds

    def windows(self, surface: str, day: date) -> list[Window]:
        plan = self.plan_for(surface, day)
        rows = {r["idx"]: r for r in self.store.sessions(surface, day)}
        out: list[Window] = []
        if plan is not None and plan.sessions:
            persona = self._persona(surface)
            for i, s in enumerate(plan.sessions):
                row = rows.get(i)
                start = _parse(row["planned_start"]) if row else s.start
                out.append(Window(surface, day, i, start, s.minutes, "plan", tuple(self.order(persona, day, i, s.actions))))
        for idx, row in sorted(rows.items()):
            if row["origin"] == "reschedule":
                kinds = tuple(o["kind"] for o in self.store.ops(session_id=row["id"]))
                out.append(Window(surface, day, idx, _parse(row["planned_start"]), row["minutes"], "reschedule", kinds))
        return out

    def window(self, surface: str, day: date, idx: int) -> Window:
        for w in self.windows(surface, day):
            if w.idx == idx:
                return w
        raise NotFound(f"{surface} has no session {idx} on {day.isoformat()}")

    def due(self, now: datetime | None = None) -> list[Window]:
        """Windows of approved programs whose start has come, today in each surface's time zone.
        One more than MISSED_AFTER late (the node was down) is marked missed, never run late."""
        now = now or self.now()
        out = []
        for pv in self.store.approved():
            surface = pv.definition.surface
            today = now.astimezone(self.tz_for(pv.definition)).date()
            # yesterday too: a window late in the evening is missed across midnight, not forgotten
            days = [d for d in (today - timedelta(days=1), today) if pv.starts_on is None or d >= pv.starts_on]
            for w in (w for d in days for w in self.windows(surface, d)):
                row = self.store.session(surface, w.day, w.idx)
                if (row and row["status"] != "scheduled") or w.start > now:
                    continue
                if now - w.start > MISSED_AFTER:
                    self._close(w, "missed", "the node did not start it in time", op_reason="missed", now=now)
                    continue
                out.append(w)
        return sorted(out, key=lambda w: w.start)

    def active_holds(self, surface: str, now: datetime | None = None, *, kind: str | None = None) -> list[dict]:
        """Holds in force: the surface's own (kind None), plus the kind's when `kind` is given."""
        now = now or self.now()
        return [h for h in self.store.holds(surface, at=now) if h["kind"] is None or (kind and h["kind"] == kind)]

    def preview(self, surface: str, day: date, idx: int) -> dict[str, Any]:
        """What the window would do now, acting on nothing: each operation with the connector
        flow and rule it would use and why it could not run, and the holds in force."""
        pv = self.store.active(surface)
        if pv is None:
            raise InvalidRequest(f"{surface} has no approved program, so nothing runs there")
        w = self.window(surface, day, idx)
        row = self.store.session(surface, day, idx)
        site = pv.definition.site or ""
        ops = []
        for kind in w.kinds:
            edit = profile.next_edit(self.store, pv.definition) if profile.is_profile(kind) else None
            spec = self.registry.get(site, edit.kind if edit is not None else kind)
            ops.append({"kind": kind, "flow": spec.flow_id if spec else None, "rule": spec.rule_id if spec else None,
                        "problem": self.registry.problem(spec), "reason": self.registry.reason(spec)})
        holds = self.active_holds(surface)
        return {**w.public(), "status": row["status"] if row else "scheduled", "ops": ops, "holds": holds,
                "would_run": not holds and all(o["problem"] is None for o in ops)}

    def report(self, surface: str, day: date, idx: int) -> dict[str, Any]:
        row = self.store.session(surface, day, idx)
        if row is None:
            return {"surface": surface, "day": day.isoformat(), "idx": idx, "status": "scheduled", "ops": []}
        ops = self.store.ops(session_id=row["id"])
        return {"surface": surface, "day": day.isoformat(), "idx": idx, "status": row["status"],
                "end_reason": row["end_reason"], "origin": row["origin"], "planned_start": row["planned_start"],
                "started_at": row["started_at"], "ended_at": row["ended_at"],
                "ops": [{k: o[k] for k in ("seq", "kind", "target", "status", "reason", "run_id", "rule_id",
                                           "content_origin", "content_id", "at")} for o in ops]}

    # ---------------------------------------------------------------- holds
    def holds(self, surface: str, *, include_cleared: bool = False) -> list[dict[str, Any]]:
        return self.store.holds(surface, include_cleared=include_cleared)

    def clear_hold(self, surface: str, *, by: Any, kind: str | None = None, hold_id: int | None = None) -> int:
        """Owner only: clear the surface's holds (one kind's, or one hold). The lane's own hold, if
        the site challenged the account, is cleared separately by handing the lane back."""
        if not is_owner(by):
            from agentos.engage.errors import OwnerOnly

            raise OwnerOnly("only the owner clears an engagement hold, in person on the node")
        now = self.now()
        who = str(by.get("id") if isinstance(by, dict) else getattr(by, "id", None) or "owner")
        n = self.store.clear_holds(surface, by=who, at=now, kind=kind, hold_id=hold_id)
        if n:
            self.store.add_history(surface, "hold_cleared", {"count": n, "kind": kind, "hold_id": hold_id}, at=now,
                                   by=who)
        return n

    def _hold(self, surface: str, *, reason: str, now: datetime, signal: str | None = None, kind: str | None = None,
              until: datetime | None = None, detail: str | None = None, text: str) -> None:
        hid = self.store.add_hold(surface, reason=reason, since=now, signal=signal, kind=kind, until=until,
                                  detail=detail)
        self.store.add_history(surface, "hold", {"id": hid, "reason": reason, "signal": signal, "kind": kind,
                                                 "until": iso(until) if until else None, "detail": detail}, at=now)
        self.store.notice("hold", text, surface=surface)
        log.warning("engage: %s", text)

    # ---------------------------------------------------------------- running
    async def run_window(self, surface: str, day: date, idx: int) -> dict[str, Any]:
        lock = self._locks.setdefault(surface, asyncio.Lock())
        async with lock:
            return await self._run_window(surface, day, idx)

    async def _run_window(self, surface: str, day: date, idx: int) -> dict[str, Any]:
        pv = self.store.active(surface)
        if pv is None:
            raise InvalidRequest(f"{surface} has no approved program, so nothing runs there")
        program = pv.definition
        w = self.window(surface, day, idx)
        row = self.store.session(surface, day, idx)
        if row is not None and row["status"] != "scheduled":
            return self.report(surface, day, idx)          # it ran (or was refused): never twice
        row = self.store.put_session(surface, day, idx, planned_start=w.start, minutes=w.minutes, origin=w.origin)
        sid = row["id"]
        if not self.store.ops(session_id=sid):
            self.store.add_ops(sid, surface, day, list(w.kinds))
        now = self.now()
        held = self.active_holds(surface, now)
        if held:
            self._close(w, "refused", f"held: {held[0]['reason']}", op_reason="held", now=now)
            self._dropped(w, "held", f"the surface is held ({held[0]['reason']})", now=now)
            return self.report(surface, day, idx)
        lane = self.lanes.lane_for(surface)
        # a fresh nonce per window: the claim parent its runs carry can never be guessed from the surface
        holder = f"{HOLDER}{surface}:{secrets.token_hex(16)}"
        ttl_s = w.minutes * 60 + CLAIM_MARGIN_S
        try:
            await self.lanes.claim(lane, holder=holder, purpose=f"engagement session {idx + 1} of {day.isoformat()}",
                                   ttl_s=ttl_s)
        except LaneUnavailable as e:
            self._close(w, "refused", f"lane: {e.reason}", op_reason="lane_unavailable", now=now)
            return self.report(surface, day, idx)
        try:
            self.store.set_session(sid, status="running", started_at=iso(now))
            self._adopt(surface, holder, w)
            await self._run_ops(program, w, sid, _Claim(lane, holder, ttl_s, renewed_at=self.clock()))
        except asyncio.CancelledError:  # the node stopped: the window ends here, and is never resumed
            self._close(w, "aborted", "cancelled: the node stopped during the session", op_reason="interrupted",
                        now=self.now())
            raise
        except Exception as e:  # a broken port ends the window, never the loop
            log.exception("engage: %s session %s of %s failed", surface, idx, day)
            self._close(w, "aborted", f"error: {type(e).__name__}", op_reason="error", now=self.now())
        finally:
            await self.lanes.release(lane, holder=holder)
        return self.report(surface, day, idx)

    def _adopt(self, surface: str, holder: str, w: Window) -> None:
        """Approved comments the runner sent to this session (approved_slot) run inside its claim."""
        adopt = getattr(self.operations, "adopt", None)
        if not callable(adopt):
            return
        try:
            taken = adopt(surface, parent=claim_parent(holder), until=w.end)
        except Exception:
            log.warning("engage: %s could not take approved runs into its session", surface, exc_info=True)
            return
        if taken:
            self.store.add_history(surface, "approved_released", {"day": w.day.isoformat(), "idx": w.idx,
                                                                 "runs": list(taken)}, at=self.now())

    def approved_slot(self, surface: str, now: datetime, key: str = "") -> datetime | None:
        """When an operation the owner approved may go: a moment inside the surface's next session
        that has not begun, so a comment approved at 23:40 goes in the persona's waking hours, paced
        like the rest, and not the moment it is approved. Within the next 8 days; else the start of
        the next waking hours; None when the surface has no approved program (the runner's own rules).
        A hold with an end (the site's rate limit) moves it past that end."""
        pv = self.store.active(surface)
        if pv is None:
            return None
        now = max([now, *[_parse(h["until"]) for h in self.active_holds(surface, now) if h.get("until")]])
        tz = self.tz_for(pv.definition)
        today = now.astimezone(tz).date()
        for d in range(8):
            day = today + timedelta(days=d)
            for w in sorted(self.windows(surface, day), key=lambda x: x.start):
                if w.start <= now:
                    continue
                share = int(hashlib.sha256(f"{key}:{day}:{w.idx}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
                return w.start + timedelta(minutes=w.minutes * (0.1 + 0.6 * share))
        persona = self._persona(surface)
        for d in range(8):
            day = today + timedelta(days=d)
            lo, _hi = persona.for_day(day).window(day)
            start = datetime.combine(day, dtime(0), tz) + timedelta(hours=lo)
            if start > now:
                return start
        return now + timedelta(days=1)

    def approved_gate(self, surface: str, now: datetime, *, key: str = "", run: Any = None) -> dict[str, Any] | None:
        """May an engagement run go now (the runner asks at every dispatch, for every run of this
        engine)? None: yes. Else when it may, or that it ends.

        A run a session started itself (no approval) goes only while a session is on and nothing
        holds the surface; one left over after it (the node stopped mid-session, a challenge ended
        it) ends (`session_over`), never sent later or at night. A run the owner approved goes only
        inside one of the surface's sessions, under the day's and the hour's caps (counted on every
        path, as a session's own operations are), with sessions switched on and nothing holding it:

        - a hold with an end (a rate limit): the first session after it;
        - a hold only the owner clears (a checkpoint, a captcha): it waits and looks again every
          hour, and after APPROVED_HELD_MAX it ends (`engage_held`), never sent on a challenged
          account; the program not approved or sessions switched off: it waits, however long;
        - its slot passed (the node was down, a person had the lane): the next session;
        - the cap used up: the next session with room (the next day's for the day's cap).

        A surface with no session in the next 8 days takes its waking hours instead."""
        pv = self.store.active(surface)
        op = self.store.op_by_run(str(getattr(run, "id", "") or "")) if run is not None else None
        kind = str(op["kind"]) if op and op.get("kind") else None
        holds = self.active_holds(surface, now, kind=kind)
        if pv is None:  # the owner paused the program (or it has none): nothing of it goes until it is back
            holds = [{"reason": "the engagement program is not approved now", "until": None, "owner": True}]
        elif self.enabled is not None and not self.enabled():
            holds = [{"reason": "engagement sessions are switched off on this node", "until": None, "owner": True}]
        if run is not None and not getattr(run, "approval_id", None):
            if not holds and self._session_running(surface, pv.definition if pv else None, now):
                return None
            return {"end": "session_over", "reason": f"the engagement session on {surface} that started it is over "
                                                     "(or held), so it is not sent later"}
        open_ended = [h for h in holds if not h.get("until")]
        if open_ended:
            held = (getattr(run, "error", None) or {}).get("held_since")
            since = _parse(held) if isinstance(held, str) and held else now
            reason = str(open_ended[0].get("reason") or "a hold")
            # the owner's own pause or switch keeps what they approved, however long; a challenge does not
            if now - since >= APPROVED_HELD_MAX and not open_ended[0].get("owner"):
                self.store.notice("approved_ended", f"{surface}: an approved {kind or 'operation'} was not sent, "
                                                    f"because engagement here was held ({reason}) for more than "
                                                    f"{APPROVED_HELD_MAX.days} days.", surface=surface)
                return {"end": "engage_held", "reason": f"engagement on {surface} was held ({reason}) for more than "
                                                        f"{APPROVED_HELD_MAX.days} days, so it was not sent"}
            return {"until": now + HOUR, "hold": True,
                    "reason": f"engagement on {surface} is held ({reason}); it waits until that ends"}
        if holds:
            slot = self.approved_slot(surface, now, key=key) or now + HOUR
            return {"until": slot, "reason": f"engagement on {surface} rests until a hold ends; it moves to the "
                                             f"session at {iso(slot)}"}
        assert pv is not None
        if not self._in_session_time(surface, pv.definition, now):
            slot = self.approved_slot(surface, now, key=key) or now + HOUR
            return {"until": slot, "reason": f"it missed its session (the node was down or a person had the lane), "
                                             f"so it moves to the next one, at {iso(slot)}"}
        full = self._cap_full(pv.definition, kind, op, now) if kind else None
        if full is not None:
            after = full[1]
            slot = self.approved_slot(surface, after, key=key) or after + HOUR
            return {"until": slot, "reason": f"the {full[0]} {kind} cap on {surface} is used up, so it moves to the "
                                             f"session at {iso(slot)}"}
        return None

    def _cap_full(self, program: Program, kind: str, op: dict[str, Any] | None,
                  now: datetime) -> tuple[str, datetime] | None:
        """("day" or "hour", when to look again) when sending one more `kind` now would pass the
        day's or the hour's cap, counted as a session counts (used: engine and ledger alike). The
        operation itself is left out of this engine's own count when it is of today (it was counted
        when it was planned)."""
        tz = self.tz_for(program)
        today = now.astimezone(tz).date()
        caps, hourly = self._limits(program, self.plan_for(program.surface, today))
        if kind not in caps:
            return None
        spent = bool(op and op.get("status") in SPENT)
        mine_today = 1 if spent and op and str(op.get("day")) == today.isoformat() else 0
        own = self.store.count_ops(program.surface, kind, day=today) - mine_today
        if max(own, self._ledger_count(program, kind, day=today)) >= caps[kind]:
            return "day's", datetime.combine(today + timedelta(days=1), dtime(0), tz)
        mine_hour = 1 if spent and op and op.get("at") and _parse(str(op["at"])) >= now - HOUR else 0
        own_hour = self.store.count_ops(program.surface, kind, since=now - HOUR) - mine_hour
        if max(own_hour, self._ledger_count(program, kind, since=now - HOUR)) >= hourly.get(kind, caps[kind]):
            return "hour's", now + HOUR
        return None

    def _session_running(self, surface: str, program: Program | None, now: datetime) -> bool:
        """Is one of the surface's sessions running now (its row says so), or are we inside one's time?"""
        if program is None:
            return False
        today = now.astimezone(self.tz_for(program)).date()
        for day in (today - timedelta(days=1), today):
            for r in self.store.sessions(surface, day):
                # a row a crash left `running` is not a live session after its time
                end = _parse(r["planned_start"]) + timedelta(minutes=float(r["minutes"] or 0)) + SESSION_GRACE
                if r["status"] == "running" and now < end:
                    return True
        return self._in_session_time(surface, program, now)

    def _in_session_time(self, surface: str, program: Program, now: datetime) -> bool:
        """Is `now` inside one of the surface's sessions (today's or yesterday's, local), or, for a
        surface with no session in the next 8 days, inside its waking hours?"""
        tz = self.tz_for(program)
        today = now.astimezone(tz).date()
        for day in (today - timedelta(days=1), today):
            for w in self.windows(surface, day):
                if w.start <= now < w.end:
                    return True
        if any(w.start > now for d in range(8) for w in self.windows(surface, today + timedelta(days=d))):
            return False
        lo, hi = self._persona(surface).for_day(today).window(today)
        start = datetime.combine(today, dtime(0), tz)
        return start + timedelta(hours=lo) <= now < start + timedelta(hours=hi)

    def _dropped(self, w: Window, reason: str, why: str, *, now: datetime) -> None:
        """A refused window's operations are not carried forward: say so in the history and a notice."""
        kinds = list(w.kinds)
        if not kinds:
            return
        self.store.add_history(w.surface, "dropped", {"day": w.day.isoformat(), "idx": w.idx, "reason": reason,
                                                      "ops": kinds}, at=now)
        self.store.notice("dropped", f"{w.surface}: session {w.idx + 1} of {w.day.isoformat()} did not run, because "
                                     f"{why}. Its {len(kinds)} operations were dropped, not moved.",
                          surface=w.surface)

    def recover(self, now: datetime | None = None, *, busy: set[str] | frozenset[str] = frozenset()) -> int:
        """Sessions left `running` by a node that stopped (or crashed) mid-window, past their planned
        end: aborted, their planned operations skipped as `interrupted`, with history and a notice.
        `busy` names surfaces whose window runs in this process right now: never touched."""
        now = now or self.now()
        n = 0
        for row in self.store.running_sessions():
            if row["surface"] in busy:
                continue
            end = _parse(row["planned_start"]) + timedelta(minutes=row["minutes"])
            if end > now:
                continue
            for op in self.store.ops(session_id=row["id"], status="planned"):
                self.store.set_op(op["id"], status="skipped", reason="interrupted", at=iso(now))
            reason = "interrupted: the node stopped during the session"
            self.store.set_session(row["id"], status="aborted", end_reason=reason, ended_at=iso(now))
            self.store.add_history(row["surface"], "session", {"day": row["day"], "idx": row["idx"],
                                                               "status": "aborted", "reason": reason}, at=now)
            self.store.notice("interrupted", f"{row['surface']}: session {row['idx'] + 1} of {row['day']} was cut "
                                             "short when the node stopped. What it had not done yet was dropped.",
                              surface=row["surface"])
            n += 1
        return n

    async def _run_ops(self, program: Program, w: Window, sid: int, claim: _Claim) -> None:
        surface, site = w.surface, program.site or ""
        pending = self.store.ops(session_id=sid, status="planned")
        kinds = [o["kind"] for o in pending]
        bad = sorted({k for k in kinds if not profile.is_profile(k) and not self.registry.valid(self.registry.get(site, k))})
        plan = self.plan_for(surface, w.day)
        self._check_profile(program, plan, w, kinds)       # profile_edit: holds that kind alone
        if bad:
            now = self.now()
            detail = self._problems(site, bad)
            self._hold(surface, reason="no_valid_rule", now=now, detail=detail,
                       text=f"{surface}: a session stopped before acting, because these have no installed connector "
                            f"that a canary verified, with a verified egress rule: {detail}. Nothing runs here until "
                            "you clear the hold.")
            self._close(w, "aborted", "no_valid_rule", op_reason="no_valid_rule", now=now)
            return
        persona = self._persona(surface)
        gaps = self._gaps(persona, w, len(pending))
        for i, op in enumerate(pending):
            try:
                await self._wait(gaps[i], claim)
                await self._renew(claim)   # an operation can take minutes (a connector run): the lease covers it
            except LaneUnavailable as e:
                # the node ended the claim (someone waited past its limit, a freeze): the caller that
                # waited gets the lane now, and the operations not yet run move to later sessions, within
                # the days' caps and waking hours (_reschedule), from LANE_LOST_GAP on
                now = self.now()
                try:
                    moved = self._reschedule(program, persona, w, pending[i:], earliest=now + LANE_LOST_GAP,
                                             now=now)
                except Exception:  # the window still closes, and says what it could not move
                    log.exception("engage: %s could not move a lost session's operations", surface)
                    moved = {"moved": [], "dropped": [o["kind"] for o in pending[i:]]}
                n_moved = sum(len(m["ops"]) for m in moved["moved"])
                n_dropped = len(moved["dropped"])
                self._close(w, "ended_early", f"lane: {e.reason}", op_reason="lane_lost", now=now)
                self.store.notice("lane_lost", f"{surface}: session {w.idx + 1} of {w.day.isoformat()} stopped early, "
                                               f"because its hold on the lane ended ({e.reason}): another caller may "
                                               f"have been waiting for it. {n_moved} operations moved to later "
                                               f"sessions" + (f", and {n_dropped} did not fit this week under the caps"
                                                              if n_dropped else "") + ".",
                                  surface=surface)
                return
            res = await self._one(program, plan, w, op, claim.holder)
            if res is None or res.signal not in SIGNALS:
                continue
            now = self.now()
            # a failed op goes again only when it provably did not act: one that may have is never redone
            rest = pending[i + 1:] + ([op] if res.status is OpStatus.FAILED and res.acted is False else [])
            until = self._signal_hold(surface, res, now)
            earliest = until if until is not None else now + MIN_GAP
            self._reschedule(program, persona, w, rest, earliest=earliest, now=now)
            self._close(w, "ended_early", f"signal: {res.signal}", op_reason="ended_early", now=now)
            return
        self._close(w, "done", None, op_reason="ended_early", now=self.now())

    async def _wait(self, seconds: float, claim: _Claim) -> None:
        """Sleep `seconds`, renewing the lane claim each time RENEW_EVERY_S has passed since its last
        renewal, counted on the clock, so neither a long pause nor slow operations before it let the
        lease lapse and open the lane to others mid-session."""
        left = max(0.0, seconds)
        while True:
            due = max(0.0, claim.renewed_at + RENEW_EVERY_S - self.clock())
            if left <= due:
                break
            if due:
                await self.sleep(due)
                left -= due
            await self._renew(claim)
        await self.sleep(left)

    async def _renew(self, claim: _Claim) -> None:
        await self.lanes.renew(claim.lane, holder=claim.holder, ttl_s=claim.ttl_s)
        claim.renewed_at = self.clock()

    def _check_profile(self, program: Program, plan: DayPlan | None, w: Window, kinds: list[str]) -> None:
        """A window that plans a profile edit whose due field's operation may not run yet holds
        `profile_edit` alone; the window's other operations go on (agentos.engage.profile.unchecked)."""
        now, kind = self.now(), profile.PROFILE_EDIT
        cap = self._limits(program, plan)[0].get(kind, 0)     # the owner's cap wins at once
        held = any(h["kind"] == kind for h in self.active_holds(w.surface, now, kind=kind))
        bad = profile.unchecked(self.store, self.registry, program, kinds, cap=cap, held=held, now=now, day=w.day)
        if bad:
            detail = self._problems(program.site or "", bad)
            self._hold(w.surface, reason="no_valid_rule", now=now, kind=kind, detail=detail,
                       text=f"{w.surface}: profile edits wait, because these have no installed connector that a "
                            f"canary verified, with a verified egress rule: {detail}. The rest of the account's "
                            "engagement goes on; profile edits run again once you clear the hold "
                            "(agentos engage hold clear --kind profile_edit).")

    def _problems(self, site: str, kinds: list[str]) -> str:
        """A no_valid_rule hold's detail: each kind with the registry's problem, and for a mark the
        installed operation no longer matches, why ("post (op_unverified: verified_stale)")."""
        out = []
        for k in kinds:
            spec = self.registry.get(site, k)
            why = self.registry.reason(spec)
            out.append(f"{k} ({self.registry.problem(spec)}{': ' + why if why else ''})")
        return ", ".join(out)

    def _gaps(self, persona: Persona, w: Window, n: int) -> list[float]:
        """Seconds to wait before each operation: the session's minutes split unevenly."""
        r = rng(persona.seed, persona.redraws, "pace", w.day.isoformat(), w.idx)
        weights = [r.expovariate(1.0) for _ in range(n + 1)]
        total = sum(weights) or 1.0
        return [w.minutes * 60 * x / total for x in weights[:n]]

    def _shared_cap(self, surface: str, kind: str, cap: int, day: date) -> None:
        """Once a day per kind: the cap is used up by actions on other paths, so the owner hears why
        the plan's operations did not run."""
        key = f"shared_cap:{surface}:{kind}:{day.isoformat()}"
        if self.store.meta(key):
            return
        self.store.set_meta(key, "1")
        self.store.add_history(surface, "cap_shared", {"day": day.isoformat(), "kind": kind, "cap": cap}, at=self.now())
        self.store.notice("cap_shared", f"{surface}: the day's {kind} cap of {cap} is used up by {kind}s made on this "
                                        "account another way (an agent, a pilot run, a flow or a workflow), so the "
                                        f"engagement program skips its own {kind}s for the rest of {day.isoformat()}.",
                          surface=surface)

    def used(self, program: Program, kind: str, *, day: date | None = None, since: datetime | None = None) -> int:
        """How much of a cap is used: this engine's own operations, or the account's actions of that
        kind on every path (an agent, a pilot run, a flow, a workflow: the action ledger), whichever is
        more. Engage's own runs are on the ledger too, so the larger of the two never counts one twice."""
        own = self.store.count_ops(program.surface, kind, day=day, since=since)
        return max(own, self._ledger_count(program, kind, day=day, since=since))

    def _ledger_count(self, program: Program, kind: str, *, day: date | None = None,
                      since: datetime | None = None) -> int:
        """The account's actions of `kind` on every path (the action ledger); 0 without one."""
        ledger_kind = LEDGER_KINDS.get(str(kind))
        if self.ledger is None or ledger_kind is None:
            return 0
        try:
            account = self.lanes.lane_for(program.surface)
            if day is not None:
                return int(self.ledger.day_count(account, ledger_kind, day, self.tz_for(program)))
            return int(self.ledger.count(account, ledger_kind, since=since or self.now(), until=self.now() + HOUR))
        except Exception:
            log.warning("engage: %s could not read the action ledger", program.surface, exc_info=True)
            return 0

    def _limits(self, program: Program, plan: DayPlan | None) -> tuple[dict[str, int], dict[str, int]]:
        """The day's and the hour's caps now (caps.day_limits, which the digest shows too)."""
        return day_limits(program.site or "", plan.caps if plan else None, plan.hourly if plan else None,
                          self.store.caps(program.surface))

    def op_key(self, surface: str, day: date, idx: int, seq: int) -> str:
        """The operation's idempotency key: readable, plus a tag only this node can compute, so
        nobody can create a run under it ahead of the engine."""
        secret = self.store.meta(KEY_SECRET)
        if not secret:
            secret = secrets.token_hex(32)
            self.store.set_meta(KEY_SECRET, secret)
            secret = self.store.meta(KEY_SECRET) or secret
        base = f"engage:{surface}:{day.isoformat()}:{idx}:{seq}"
        tag = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()[:16]
        return f"{base}:{tag}"

    def _ask(self, program: Program, kind: str, spec_needs_text: bool, content: Content | None) -> bool | None:
        """Whether the owner must approve this operation first; None when the program turns the
        kind off. Text in approve mode asks unless the owner already approved it; `auto` never
        holds on LinkedIn or Reddit. A profile edit asks, as the program says ("you approve every
        profile edit"), unless the account is attached to an approved profile (agentos.profiles):
        then its value comes from the profile the owner saved or approved, which is their consent,
        and the policy still routes it like any commit ("Ask me first" on the lane makes it wait)."""
        if profile.is_profile(kind):
            return not profile.from_profile(self.store, program.surface)
        auto_ok = False
        if kind in TEXT_MODE_KINDS:
            mode = program.comments.mode if kind == str(Kind.COMMENT) else program.community.reply_to_comments.mode
            if mode is Mode.OFF:
                return None
            auto_ok = mode is Mode.AUTO and program.site not in APPROVE_ONLY_SITES
        if not spec_needs_text:
            return False
        if content is not None and content.review:
            return True          # test text (the passthrough provider) always waits for the owner
        if content is not None and content.approved and content.origin is ContentOrigin.ITEM:
            return False
        return not auto_ok

    async def _one(self, program: Program, plan: DayPlan | None, w: Window, op: dict[str, Any],
                   holder: str) -> OpResult | None:
        surface, kind = w.surface, op["kind"]
        spec = self.registry.get(program.site or "", kind)
        assert spec is not None or profile.is_profile(kind)
        now = self.now()

        def skip(reason: str) -> None:
            self.store.set_op(op["id"], status="skipped", reason=reason, at=iso(now))

        if self.active_holds(surface, now, kind=kind):
            skip("held")
            return None
        caps, hourly = self._limits(program, plan)
        if kind not in caps or self.used(program, kind, day=w.day) >= caps[kind]:
            skip("cap_daily")
            if kind in caps and self.store.count_ops(surface, kind, day=w.day) < caps[kind]:
                self._shared_cap(surface, kind, caps[kind], w.day)
            return None
        if self.used(program, kind, since=now - HOUR) >= hourly.get(kind, caps[kind]):
            skip("cap_hourly")
            return None
        edit = profile.next_edit(self.store, program, now=now, day=w.day) if profile.is_profile(kind) else None
        if profile.is_profile(kind):
            spec = self.registry.get(program.site or "", edit.kind) if edit is not None else None
            if edit is None or not self.registry.valid(spec):
                skip(profile.CURRENT if edit is None else "no_valid_rule")
                return None
        assert spec is not None
        if self._ask(program, kind, spec.needs_text, None) is None:
            skip("mode_off")
            return None
        content: Content | None = None
        if spec.needs_text and edit is None:
            token = CONTENT_SESSION.set(op["session_id"])     # the window the content is reserved for
            try:
                got = await self._take(w, kind)
            finally:
                CONTENT_SESSION.reset(token)
            if got is None:
                skip("draft_unavailable")
                return None
            if not got:
                skip("no_content")
                return None
            content = got[0]
        target = content.targets[0] if content is not None and content.targets else None
        if edit is not None:
            target = edit.target          # the value the program asks for (agentos.engage.profile)
        if spec.needs_target and target is None:
            target, read = await self._target(surface, kind, program, holder)
            if target is None:
                if content is not None:
                    self.content.settle(surface, content, used=False)
                if read is not None and read.signal in SIGNALS:      # the site stopped the read
                    reason = f"signal: {read.signal} (reading targets)"
                    self.store.set_op(op["id"], status=OpStatus.FAILED.value, reason=reason, at=iso(now),
                                      run_id=read.run_id)
                    self.store.add_history(surface, "op", {"kind": kind, "status": "failed", "reason": reason,
                                                           "run_id": read.run_id,
                                                           "session": [w.day.isoformat(), w.idx]}, at=now)
                    return OpResult(OpStatus.FAILED, signal=read.signal, error=read.error, run_id=read.run_id)
                skip("no_target")
                return None
        ask = bool(self._ask(program, kind, spec.needs_text, content))
        sent = target
        if edit is not None and edit.field in profile.PHOTO_FIELDS:
            # the row keeps the photo's content hash; the run sends the lane's copy of it
            sent = profile.lane_file(self.store, surface, edit.target)
            if sent is None:
                skip("photo_unavailable")
                return None
        try:
            params = spec.build(content, sent)
        except BadTarget:
            if content is not None:
                self.content.settle(surface, content, used=False)
            self.store.set_op(op["id"], status="skipped", reason="bad_target", at=iso(now), target=target)
            return None
        self.store.set_op(op["id"], status="attempted", at=iso(now), target=target, rule_id=spec.rule_id,
                          content_origin=content.origin.value if content else None,
                          content_id=content.id if content else None)
        key = self.op_key(surface, w.day, w.idx, op["seq"])
        try:
            res = await self.operations.run(surface, spec, params, idempotency_key=key, ask=ask,
                                            parent=claim_parent(holder))
        except BaseException:
            # stopped mid-run (the node stopping, a broken port): the run may have acted, so the
            # content is settled as used (the op stays `attempted`) and never offered again
            if content is not None:
                try:
                    self.content.settle(surface, content, used=True)
                except Exception:
                    log.exception("engage: %s content %s was not settled", surface, content.id)
            raise
        reason = f"signal: {res.signal}" if res.signal else res.error
        self.store.set_op(op["id"], status=res.status.value, run_id=res.run_id, reason=reason)
        if content is not None:
            # handed back only when the run provably did not act; a failure that may have posted is
            # settled as used, so the content never goes out twice under a new idempotency key
            maybe_acted = res.status is OpStatus.FAILED and res.acted is not False
            self.content.settle(surface, content, used=res.done or res.status is OpStatus.AWAITING_APPROVAL
                                or maybe_acted, run_id=res.run_id)
        if res.status is OpStatus.VERIFIED:
            self.store.add_done(surface, w.day, kind)
        self.store.add_history(surface, "op", {"kind": kind, "target": target, "status": res.status.value,
                                               "run_id": res.run_id, "rule": spec.rule_id, "reason": reason,
                                               "ask": ask, "content": content.id if content else None,
                                               "session": [w.day.isoformat(), w.idx]}, at=now)
        if res.error == FOREIGN_RUN:
            self.store.notice("foreign_run", f"{surface}: a {kind} was not done, because another run already "
                                             f"used its key ({res.run_id}). The engine did not adopt it.",
                              surface=surface)
        if edit is not None:
            profile.notify_failures(self.store, surface)
        if res.status is OpStatus.HELD and res.error == "connector_broken":
            self._hold(surface, reason="connector_broken", now=now, kind=kind, detail=spec.flow_id,
                       text=f"{surface}: {kind} stopped, because its connector ({spec.flow_id}) is broken there. "
                            "It runs again once a repaired version is installed and you clear the hold.")
        return res

    def _next_start(self, w: Window, now: datetime) -> datetime:
        """When the surface's next window today starts (else an hour from now)."""
        try:
            later = [x.start for x in self.windows(w.surface, w.day) if x.start > now]
        except Exception:  # noqa: BLE001  (only the end of a hold note: never a reason to stop the window)
            later = []
        return min(later) if later else now + HOUR

    async def _take(self, w: Window, kind: str) -> list[Content] | None:
        """Content for one text operation. A port that calls out (`atake`, the drafter) is awaited;
        when it cannot (ContentUnavailable), the surface's history and a notice say so, and a hold
        of kind `draft_provider` notes it until the surface's next window starts: while it is in
        force, text comes only from what needs no call (items). That hold stops nothing else (no
        operation has its kind), so it shows in the status without holding the surface. None: no
        content, because the drafter was unavailable."""
        from agentos.engage.drafts import HOLD_KIND

        surface = w.surface
        atake = getattr(self.content, "atake", None)
        if atake is None or self.active_holds(surface, kind=HOLD_KIND):
            got = self.content.take(surface, kind, 1)
            return got if got or atake is None else None
        try:
            return await atake(surface, kind, 1)
        except ContentUnavailable as e:
            now = self.now()
            until = self._next_start(w, now)
            hid = self.store.add_hold(surface, reason="draft_provider_unavailable", since=now, kind=HOLD_KIND,
                                      until=until, detail=e.reason)
            self.store.add_history(surface, "hold", {"id": hid, "reason": "draft_provider_unavailable",
                                                     "signal": None, "kind": HOLD_KIND, "until": iso(until),
                                                     "detail": e.reason}, at=now)
            recent = self.store.history(surface, kinds=("drafts_unavailable",), since=now - DRAFTS_NOTICE_EVERY,
                                        limit=1)
            self.store.add_history(surface, "drafts_unavailable", {"kind": kind, "reason": e.reason,
                                                                   "session": [w.day.isoformat(), w.idx]}, at=now)
            if not recent:
                self.store.notice("drafts", f"{surface}: the draft provider gave no drafts ({e.reason}), so this "
                                            "session skips the comments and posts that need one. The rest of the "
                                            "session goes on, and the next session asks again.", surface=surface)
            log.warning("engage: %s drafts unavailable (%s)", surface, e.reason)
            return None

    async def _target(self, surface: str, kind: str, program: Program,
                      holder: str) -> tuple[str | None, OpResult | None]:
        """(the next target from the TargetPort, the read that refilled it or None). A port that
        can refill (RefillingTargets) reads more candidates, inside this window's lane claim,
        when it has none."""
        target = self.targets.next(surface, kind, program)
        read = None
        if target is None and isinstance(self.targets, RefillingTargets):
            read = await self.targets.refill(surface, kind, program, parent=claim_parent(holder))
            target = self.targets.next(surface, kind, program)
        return target, read

    def _signal_hold(self, surface: str, res: OpResult, now: datetime) -> datetime | None:
        """Hold the surface for the signal; returns when the hold ends (None: when the owner clears it)."""
        from agentos.node.holds import hold_for

        sig = res.signal or ""
        hold = hold_for(sig, now=now, detail=res.error)
        until = hold.until if hold is not None else None
        reason = hold.reason if hold is not None else f"needs_human:{sig}"
        if until is not None:
            text = (f"{surface}: the site is rate limiting this account, so engagement rests until "
                    f"{until:%Y-%m-%d %H:%M} UTC. The rest of the session moves to later this week.")
        else:
            text = (f"{surface}: the site showed {sig.replace('_', ' ')}, so engagement stopped. Check the lane, "
                    "then clear the hold (agentos engage hold clear). The rest of the session moves to later "
                    "this week if there is room.")
        self._hold(surface, reason=reason, now=now, signal=sig, until=until, detail=res.error, text=text)
        return until

    def _close(self, w: Window, status: str, reason: str | None, *, op_reason: str, now: datetime) -> None:
        """End the window: its row gets `status`, and operations still planned are skipped."""
        row = self.store.put_session(w.surface, w.day, w.idx, planned_start=w.start, minutes=w.minutes,
                                     origin=w.origin)
        if not self.store.ops(session_id=row["id"]):
            self.store.add_ops(row["id"], w.surface, w.day, list(w.kinds))
        for op in self.store.ops(session_id=row["id"], status="planned"):
            self.store.set_op(op["id"], status="skipped", reason=op_reason, at=iso(now))
        self.store.set_session(row["id"], status=status, end_reason=reason, ended_at=iso(now))
        ops = self.store.ops(session_id=row["id"])
        counts: dict[str, int] = {}
        for o in ops:
            counts[o["status"]] = counts.get(o["status"], 0) + 1
        self.store.add_history(w.surface, "session", {"day": w.day.isoformat(), "idx": w.idx, "status": status,
                                                      "reason": reason, "ops": counts}, at=now)

    # ---------------------------------------------------------------- after the window
    async def reconcile(self) -> int:
        """Settle operation rows whose outcome came after their window. Returns the rows changed.

        - `awaiting_approval` (any age): the run's outcome once the owner approved, denied or let
          the card expire (OperationPort.check).
        - `unverified` (recent): turns `verified` once a later check proves it, else stays as it
          is (a failure after acting is still "may have acted").
        - `attempted` (recent), the window stopped mid-run: the finished run its idempotency key
          started (OperationPort.lookup, when the port has it), whatever came of it.

        Recent is RECHECK_UNVERIFIED by the row's day, or for a profile edit the time its value
        stays blocked (agentos.engage.profile.BLOCK_TTL, twice over). A row of a window that is
        still running is never touched (the window settles it), a held result changes nothing, and
        an operation counts done once, when its row turns verified. Profile failure notices go out
        last (agentos.engage.profile.notify_failures)."""
        now = self.now()
        since = (now - RECHECK_UNVERIFIED).date().isoformat()
        lookup = getattr(self.operations, "lookup", None)
        running = {r["id"] for r in self.store.running_sessions()}
        n = 0
        for op in self.store.ops(status=("awaiting_approval", "unverified", "attempted")):
            prev = op["status"]
            if op["session_id"] in running or (prev != "awaiting_approval" and not self._recent(op, now, since)):
                continue
            res = await self._outcome(op, lookup)
            if res is None or res.status in (OpStatus.AWAITING_APPROVAL, OpStatus.HELD) or res.status.value == prev:
                continue
            if prev == "unverified" and res.status is not OpStatus.VERIFIED:
                continue        # still not proved (or it failed after acting): it stays unverified
            run_id = res.run_id or op["run_id"]
            self.store.set_op(op["id"], status=res.status.value, run_id=run_id,
                              reason=f"signal: {res.signal}" if res.signal else res.error)
            if res.status is OpStatus.VERIFIED:
                self.store.add_done(op["surface"], date.fromisoformat(op["day"]), op["kind"])
            self.store.add_history(op["surface"], "op", {"kind": op["kind"], "status": res.status.value,
                                                         "run_id": run_id, "from": prev,
                                                         "after_approval": prev == "awaiting_approval"}, at=now)
            n += 1
        for surface in {o["surface"] for o in self.store.ops(kind=profile.PROFILE_EDIT,
                                                              status=("attempted", "unverified", "failed"))}:
            profile.notify_failures(self.store, surface)
        return n

    @staticmethod
    def _recent(op: dict[str, Any], now: datetime, since: str) -> bool:
        if profile.is_profile(op["kind"]):
            at = _parse(op["at"]) if op.get("at") else None
            return at is None or now - at < profile.BLOCK_TTL * 2
        return op["day"] >= since

    async def _outcome(self, op: dict[str, Any], lookup: Any) -> OpResult | None:
        """The row's run now: by its run id, or for an `attempted` row with none, by its key."""
        if op["run_id"]:
            return await self.operations.check(op["run_id"])
        if op["status"] != "attempted" or not callable(lookup):
            return None
        session = self.store.session_by_id(op["session_id"])
        pv = self.store.active(op["surface"])
        kind = op["kind"]
        if profile.is_profile(kind):
            field = profile.RULE_FIELDS.get(op.get("rule_id") or "")
            kind = profile.KINDS[field] if field else ""
        spec = self.registry.get((pv.definition.site or "") if pv else "", kind) if kind else None
        if session is None or spec is None:
            return None
        key = self.op_key(op["surface"], date.fromisoformat(op["day"]), session["idx"], op["seq"])
        return await lookup(key, spec.flow_id, op["surface"])

    # ---------------------------------------------------------------- rescheduling
    def _committed(self, w: Window, kind: str) -> int:
        """Operations of `kind` a window has or will have: its rows that are not skipped or held,
        or, before it opened, its planned kinds."""
        row = self.store.session(w.surface, w.day, w.idx)
        ops = self.store.ops(session_id=row["id"]) if row else []
        if ops:
            return sum(1 for o in ops if o["kind"] == kind and o["status"] not in ("skipped", "held"))
        return sum(1 for k in w.kinds if k == kind)

    def _slot(self, persona: Persona, surface: str, day: date, tz: ZoneInfo, earliest: datetime,
              others: list[Window], label: str) -> datetime | None:
        p = persona.for_day(day)
        lo, hi = p.window(day)
        midnight = datetime.combine(day, dtime(0), tzinfo=tz)
        a = max(midnight + timedelta(hours=lo), earliest.astimezone(tz))
        b = midnight + timedelta(hours=hi)
        if a >= b:
            return None
        r = rng(p.seed, p.redraws, "reschedule", day.isoformat(), label)
        span = (b - a).total_seconds()
        for _ in range(SLOT_TRIES):
            t = (a + timedelta(seconds=int(r.random() * span))).replace(microsecond=0)
            if t.minute % 5 == 0 or any(abs(t - o.start) < MIN_GAP for o in others):
                continue
            return t
        return None

    def _fit(self, program: Program, plan: DayPlan, windows: list[Window], start: datetime, minutes: float,
             left: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The operations of `left` (in order) that fit a new window at `start`: under the day's cap
        counting every window of the day, and under the hourly cap counting the windows close
        enough to share an hour with it."""
        end = start + timedelta(minutes=minutes)
        near = [o for o in windows if start - o.end < HOUR and o.start - end < HOUR]
        caps, hourly = self._limits(program, plan)
        day_used: dict[str, int] = {}
        hour_used: dict[str, int] = {}
        take: list[dict[str, Any]] = []
        for op in left:
            k = op["kind"]
            if k not in caps:
                continue
            if k not in day_used:
                day_used[k] = sum(self._committed(o, k) for o in windows)
                hour_used[k] = sum(self._committed(o, k) for o in near)
            if day_used[k] + 1 > caps[k] or hour_used[k] + 1 > hourly.get(k, caps[k]):
                continue
            day_used[k] += 1
            hour_used[k] += 1
            take.append(op)
        return take

    def _reschedule(self, program: Program, persona: Persona, w: Window, rest: list[dict[str, Any]], *,
                    earliest: datetime, now: datetime) -> dict[str, Any]:
        """Move `rest` (operation rows of window `w`) to new windows later in the same ISO week."""
        surface = w.surface
        # the operation that met the signal keeps its own status (it reached the connector); the
        # ones still planned are skipped here and live on as new rows (moved_from) in later windows
        current = {o["id"]: o["status"] for o in self.store.ops(session_id=rest[0]["session_id"])} if rest else {}
        for op in rest:
            if current.get(op["id"]) == "planned":
                self.store.set_op(op["id"], status="skipped", reason="rescheduled", at=iso(now))
        left = list(rest)
        moved: list[dict[str, Any]] = []
        sunday = week_of(w.day) + timedelta(days=6)
        day = w.day
        while left and day <= sunday:
            plan = self.plan_for(surface, day)
            if plan is not None and not plan.quiet:
                tz = ZoneInfo(plan.timezone)
                for n in range(RESCHEDULE_PER_DAY):
                    if not left:
                        break
                    windows = self.windows(surface, day)
                    start = self._slot(persona, surface, day, tz, earliest, windows, f"{w.day}:{w.idx}:{n}")
                    if start is None:
                        break
                    r = rng(persona.seed, persona.redraws, "reschedule-length", day.isoformat(), w.idx, n)
                    minutes = r.lognormvariate(math.log(LENGTH_MEDIAN_MIN * persona.for_day(day).length_scale),
                                               LENGTH_SIGMA)
                    minutes = round(max(LENGTH_RANGE[0], min(LENGTH_RANGE[1], minutes)), 1)
                    take = self._fit(program, plan, windows, start, minutes, left)
                    if not take:
                        break
                    idx = max([o.idx for o in windows] + [len(plan.sessions) - 1]) + 1
                    row = self.store.put_session(surface, day, idx, planned_start=start, minutes=minutes,
                                                 origin="reschedule")
                    self.store.add_ops(row["id"], surface, day, [o["kind"] for o in take],
                                       moved_from=[o["id"] for o in take])
                    moved.append({"day": day.isoformat(), "idx": idx, "start": iso(start),
                                  "ops": [o["kind"] for o in take]})
                    taken = {o["id"] for o in take}
                    left = [o for o in left if o["id"] not in taken]
            day += timedelta(days=1)
        for op in left:
            if current.get(op["id"]) == "planned":
                self.store.set_op(op["id"], status="skipped", reason="week_over", at=iso(now))
            else:
                self.store.set_op(op["id"], reason="signal, then week_over")
        out = {"from": [w.day.isoformat(), w.idx], "moved": moved, "dropped": [o["kind"] for o in left]}
        if rest:
            self.store.add_history(surface, "rescheduled", out, at=now)
        if left:
            self.store.notice("dropped", f"{surface}: {len(left)} operations did not fit before Sunday under the caps "
                                         "and were dropped.", surface=surface)
        return out
