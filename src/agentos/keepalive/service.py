"""The keepalive service (runtime service "keepalive"): plans each lane's next visit and makes one at a time.

    build(runtime)               -> KeepaliveService, registered as "keepalive"
    status(lane_id)              the plan: next visit, why not now, the last visits, the switches
    planned(start, end)          the next visit of each lane that falls between start and end (the agenda)
    visit_now(lane_id)           one visit now, in the caller's time (`agentos lane keepalive <lane> --now`)
    set_opt_out(lane_id, bool)   the lane's own switch, a file in the lane's directory (the CLI writes it too)
    run_round(now=)              what the loop does every TICK_S

What a round reads, all records the node already keeps (agentos.keepalive.planner says how they combine):
the lane manager's lanes (state, last_used, control, claim, queue, last signal), the freeze, the lane
check's last report (signed in, and when the session cookie expires; health rounds refresh it without
asking any site), the lane's pinned time zone, and this
service's own visits.

Who goes first, and when: lanes whose slot has come, earliest slot first; one visit at a time on the
node, and at least GAP_MINUTES (drawn) between two (kept in keepalive.db, so a restart keeps it). A visit
starts only while a warm slot is free and memory stays above the floor with room for one more lane
(LANE_MB), checked again under the one-visit lock right before the lane opens, so it never makes the
lane manager close another lane; otherwise it waits for a later round, inside the lane's waking hours.
The lane it woke goes back to sleep after the visit, unless someone else started using it meanwhile or
a live view of it is open or was opened during the visit.

A visit is the node's only while control of the lane is the agents': a take-over or a hold cancels it
at once (a control listener), and every step asks first (visit.ReadOnly.may_act).

Each lane's marks (signed_out, challenged, wrong_account) tell the owner once per episode. A challenge
and a wrong account hold the lane through the lane manager's hold policy; their mark ends only with a
visit that goes well (which the hold allows only after a human handed the lane back). A signed-out mark
also ends when the lane check sees the lane signed in again.

keepalive.db missing or unreadable while the switch is on: the round plans nothing, opens a new one and
rebuilds it from the lanes' own records (the opt-out files; the lane manager's last_used), then waits a
gap before the first visit.

The node switch is `keepalive` in node.toml, read again every round so `agentos lane keepalive --node
on` takes effect without a restart. Off by default: it opens real accounts unattended.

Every agent call, flow and brain uses the lane through the lane manager, which stamps the lane's
last_used; that is the activity the planner counts, so a lane kept busy is never visited.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentos.core import clock
from agentos.core.config import config_path, home_from_env, load_config
from agentos.core.errors import AgentosError, InvalidRequest, LaneNotFound, ServiceUnavailable
from agentos.core.models import ControlState, LaneInfo, LaneKind, LaneState
from agentos.keepalive import planner, visit
from agentos.keepalive.sites import Entry, entry_for
from agentos.keepalive.store import STARTED, KeepaliveStore, Visit, lane_opted_out, set_lane_opt_out
from agentos.node import holds
from agentos.node.sites import WRONG_ACCOUNT

log = logging.getLogger("agentos.keepalive")

SERVICE_NAME = "keepalive"
TICK_S = 60.0
FIRST_DELAY_S = 120.0
GAP_MINUTES = (10.0, 25.0)       # between two visits on the node
LANE_MB = 1000                   # what one more warm lane takes (docs/design/stealth.md: 0.8 to 1 GB)
NOW_WAIT_S = 300.0               # --now waits this long for a free slot and memory
NOW_POLL_S = 5.0
HISTORY = 5
PLAN_ROWS = 20                   # the visits a plan reads back (the last that counted, the attempts since)
USED_SLACK = timedelta(seconds=30)  # a last_used this close to an attempt that did not count was its own
MARKS = ("signed_out", "challenged", "wrong_account")
NOT_BEFORE = "not_before"        # keepalive.db meta: no visit on the node before this time


class _NoRoom(Exception):
    """No warm slot or memory for one more lane, found under the lock right before a visit."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def pinned_timezone(config: Any, lane_id: str) -> tuple[ZoneInfo, str]:
    """The lane's pinned time zone (its fingerprint pin), else UTC; and where it came from."""
    name = None
    try:
        pin = json.loads(Path(config.fingerprint_path(lane_id)).read_text())
        name = pin.get("timezone") if isinstance(pin, dict) else None
    except (OSError, ValueError, AttributeError):
        name = None
    try:
        return (ZoneInfo(name), "pin") if name else (ZoneInfo("UTC"), "default")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC"), "default"


class KeepaliveService:
    def __init__(self, runtime: Any = None, *, home: Path | None = None, clock: Any = time.time,
                 tick: float = TICK_S, first_delay: float = FIRST_DELAY_S, pace: visit.Pace | None = None,
                 rng: random.Random | None = None, sleep: Any = asyncio.sleep):
        cfg = runtime.get("config") if runtime is not None else None
        self.runtime = runtime
        self.config = cfg
        self.home = Path(home) if home is not None else (Path(cfg.home) if cfg is not None else home_from_env())
        self.clock = clock
        self.tick = tick
        self.first_delay = first_delay
        self.pace = pace or visit.Pace()
        self.rng = rng or random.Random()
        self.sleep = sleep
        self._lock = asyncio.Lock()         # one visit at a time on the node
        self._not_before: datetime | None = None
        self._task: asyncio.Task | None = None
        self.waiting: str | None = None    # why the last due visit did not start (resources)
        self.current: str | None = None    # the lane being visited
        self._began = False                 # the current visit wrote its started row
        self.store: KeepaliveStore | None = None
        self.store_error: str | None = None
        if self._open_store():
            done = self.store.interrupt_started()
            if done:
                log.warning("keepalive: %d visit(s) were cut by a restart; they count as visits", done)

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="agentos-keepalive")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None
        if self.store is not None:
            self.store.close()

    async def _loop(self) -> None:
        await asyncio.sleep(self.first_delay)
        while True:
            try:
                await self.run_round()
            except Exception:
                log.exception("keepalive round failed")
            await asyncio.sleep(self.tick)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    # ---------------------------------------------------------------- keepalive.db
    def _open_store(self) -> bool:
        try:
            self.store = KeepaliveStore(self.home)
        except ServiceUnavailable as e:
            self.store, self.store_error = None, e.message
            log.warning("keepalive: %s", e.message)
            return False
        self.store_error = None
        self._adopt_opt_outs()
        when = self.store.get_meta(NOT_BEFORE)
        self._not_before = datetime.fromisoformat(when) if when else None
        return True

    def _store_ready(self, now: datetime) -> bool:
        """keepalive.db is there and answers. When it is not, plan nothing this round: open a new one
        (rebuilt from the lanes' own records) and wait a gap before the first visit."""
        if self.store is not None and self.store.readable():
            return True
        log.warning("keepalive: %s is missing or unreadable; no visit until it is rebuilt", self.home / "keepalive")
        if self.store is not None:
            with contextlib.suppress(Exception):
                self.store.close()
            self.store = None
        if self._open_store():
            self._set_not_before(now + timedelta(minutes=self.rng.uniform(*GAP_MINUTES)))
            self.store_error = "keepalive.db was rebuilt from the lanes' own records; visits resume after a gap"
        return False

    def _db(self) -> KeepaliveStore:
        if self.store is None:
            raise ServiceUnavailable(self.store_error or "keepalive.db is not open")
        return self.store

    def _set_not_before(self, when: datetime) -> None:
        self._not_before = when
        try:
            self._db().set_meta(NOT_BEFORE, clock.iso_or_none(when, timespec="seconds"))
        except Exception:  # the gap still holds in memory
            log.warning("keepalive: could not save the next free time", exc_info=True)

    def _lane_dir(self, lane_id: str) -> Path | None:
        lane_dir = getattr(self.config, "lane_dir", None)
        return Path(lane_dir(lane_id)) if callable(lane_dir) else None

    def opted_out(self, lane_id: str, mirrored: bool = False) -> bool:
        """The lane's own opt-out file; keepalive.db's column only when there is no lane directory."""
        lane_dir = self._lane_dir(lane_id)
        return lane_opted_out(lane_dir) if lane_dir is not None else mirrored

    def _adopt_opt_outs(self) -> None:
        """An opt-out keepalive.db holds and the lane's directory does not: write the lane's file."""
        for lane_id in self._db().opted_out_lanes():
            lane_dir = self._lane_dir(lane_id)
            if lane_dir is not None and lane_dir.is_dir() and not lane_opted_out(lane_dir):
                set_lane_opt_out(lane_dir, True)

    # ---------------------------------------------------------------- what it reads
    def enabled(self) -> bool:
        """The node switch, read from node.toml each time (falls back to the config agentosd started with)."""
        try:
            if config_path(self.home).exists():
                return bool(load_config(self.home).keepalive)
        except Exception:
            log.debug("node.toml not read", exc_info=True)
        return bool(getattr(self.config, "keepalive", False))

    def _svc(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    def _lanes(self) -> Any:
        lanes = self._svc("lanes")
        if lanes is None:
            raise AgentosError("the lane manager is not running in this daemon")
        return lanes

    def _frozen(self, lane_id: str) -> bool:
        freeze = self._svc("freeze")
        check = getattr(freeze, "is_frozen", None)
        return bool(check(lane_id)) if callable(check) else False

    def _driven(self, lane_id: str) -> bool:
        """Is an agent driving the lane now (the lane manager's record: LaneManager.driven)?"""
        check = getattr(self._svc("lanes"), "driven", None)
        return bool(check(lane_id)) if callable(check) else False

    def _persona(self, lane_id: str, seed: str) -> Any:
        return planner.persona_for(lane_id, seed)

    def _session(self, lane_id: str, entry: Entry) -> tuple[bool | None, datetime | None]:
        """(signed in, session cookie expiry) from the lane check's last report; (None, None) unknown."""
        checker = self._svc("lane_check")
        report = getattr(checker, "last", {}).get(lane_id) if checker is not None else None
        for check in getattr(report, "checks", ()) or ():
            if check.id != "session":
                continue
            site = (check.data.get("sites") or {}).get(entry.key)
            if not site:
                return None, None
            expires = site.get("expires_at")
            return bool(site.get("signed_in")), datetime.fromisoformat(expires) if expires else None
        return None, None

    def plan(self, info: LaneInfo, now: datetime, *, enabled: bool | None = None,
             asked: bool = False) -> dict[str, Any]:
        """The lane's plan: everything status() shows and run_round() decides on. `asked`: the owner asked
        for a visit now, so the node switch and the lane's opt-out do not block it (the gates still do)."""
        lane_id = info.spec.id
        store = self._db()
        row = store.lane(lane_id)
        opted_out = self.opted_out(lane_id, row.opted_out)
        entry = entry_for(info.spec)
        visits = store.visits(lane_id, PLAN_ROWS)
        last = visits[0] if visits else None        # the last attempt, whatever came of it
        counted = next((v for v in visits if planner.counts(v.outcome, v.detail)), None)
        signed_in, expires = self._session(lane_id, entry) if entry is not None else (None, None)
        enabled = self.enabled() if enabled is None else enabled
        block = planner.blocked(info, enabled=enabled or asked, opted_out=opted_out and not asked,
                                frozen=self._frozen(lane_id), driven=self._driven(lane_id),
                                entry=entry, signed_in=signed_in, now=now)
        out: dict[str, Any] = {"lane": lane_id, "enabled": enabled, "opted_out": opted_out,
                               "site": entry.name if entry else None, "home": entry.home if entry else None,
                               "status": row.status, "status_reason": row.reason,
                               "signed_in": signed_in, "session_expires_at": clock.iso_or_none(expires, timespec="seconds"),
                               "blocked": None if block is None else {"code": block.code, "reason": block.reason},
                               "last_visit": last.public() if last else None, "next_at": None, "due_at": None,
                               "_block": block, "_entry": entry, "_row": row}
        if entry is None:
            return out
        tz, tz_source = pinned_timezone(self.config, lane_id)
        pace = self._pace(lane_id)
        if pace is not None:  # the owner set a posting pace: its zone and waking hours hold for visits too
            tz, tz_source = ZoneInfo(pace["zone"].name), pace["zone"].source
        used = planner.latest(info.last_used)
        if used is not None and any(v.started_at - USED_SLACK <= used <= v.finished_at + USED_SLACK
                                    for v in visits if not planner.counts(v.outcome, v.detail)):
            used = None  # the lane manager stamped it for an attempt of ours that did not count
        activity = planner.latest(used, info.control.last_human_input, counted.started_at if counted else None)
        # never used: count from the lane's creation, so the plan holds still between rounds
        since = activity or planner.latest(info.spec.created_at)
        persona = self._persona(lane_id, row.seed)
        due = planner.due_at(entry, row.seed, since, expires, now)
        retry = planner.retry_at(row.seed, last.outcome, last.started_at, last.finished_at, last.detail) \
            if last else None
        # a slot before the retry time was missed then: the retry goes at that time, not a later slot
        at = planner.next_visit(row.seed, persona, tz, due, max(now, retry) if retry else now,
                                counted.started_at if counted else None)
        due = max(due, retry) if retry else due
        if at is not None and pace is not None and pace["windows"]:
            at = self._waking(lane_id, at, tz, pace["windows"])
        first, end = planner.window(persona, now.astimezone(tz).date(), tz)
        out.update({"timezone": tz.key, "timezone_source": tz_source, "activity_at": clock.iso_or_none(activity, timespec="seconds"),
                    "due_at": clock.iso_or_none(due, timespec="seconds"), "next_at": clock.iso_or_none(at, timespec="seconds"), "retry_at": clock.iso_or_none(retry, timespec="seconds"),
                    "window_today": [first.astimezone(tz).strftime("%H:%M"), end.astimezone(tz).strftime("%H:%M")],
                    "active_hours": pace["active_hours"] if pace is not None else None, "_at": at})
        return out

    def _pace(self, lane_id: str) -> dict[str, Any] | None:
        """The lane's posting pace (agentos.policy.pace) when the owner set one: its zone (and where
        it comes from) and its active hours. A visit is a session the site sees, so a session at 3 am
        lane time is a signal too: visits keep to the same waking hours as posts."""
        policy = self._svc("policy")
        store = getattr(policy, "pace", None)
        get, zone = getattr(store, "get", None), getattr(policy, "pace_zone", None)
        if not callable(get) or not callable(zone):
            return None
        try:
            pace = get(lane_id)
            if pace is None:
                return None
            z = zone(lane_id, pace)
            if getattr(z, "unreadable", False):  # the pin cannot be read: plan as with no pace
                return None
            return {"zone": z, "windows": pace.windows(), "active_hours": pace.active_hours}
        except Exception:  # the plan goes on with the lane's pinned zone
            log.warning("%s: could not read the lane's posting pace", lane_id, exc_info=True)
            return None

    @staticmethod
    def _waking(lane_id: str, at: datetime, tz: ZoneInfo, windows: list[Any]) -> datetime | None:
        """`at` moved into the lane's active hours (a little after they open), or as it is."""
        from agentos.policy import pace as pacing

        t = at.timestamp()
        if pacing.is_open(windows, tz, t):
            return at
        nxt = pacing.next_open(windows, tz, t)
        if nxt is None:
            return None
        until = pacing.open_until(windows, tz, nxt)
        return datetime.fromtimestamp(pacing.jitter(f"keepalive:{lane_id}", nxt, until), UTC)

    @staticmethod
    def public(plan: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in plan.items() if not k.startswith("_")}

    def _browser_lanes(self) -> list[LaneInfo]:
        return [i for i in self._lanes().list() if getattr(i.spec, "kind", LaneKind.BROWSER) == LaneKind.BROWSER]

    def room(self) -> str | None:
        """Why one more lane cannot warm now without closing another, or None."""
        lanes = self._lanes()
        cap = getattr(self.config, "max_warm_lanes", None) if self.config is not None else None
        hot = [i.spec.id for i in lanes.list() if i.state in planner.HOT]
        if cap is not None and len(hot) >= cap:
            return f"all {cap} warm slots are in use ({', '.join(sorted(hot))})"
        status = getattr(lanes, "memory_status", None)
        mem = status() if callable(status) else {}
        available, floor = mem.get("available_mb"), mem.get("floor_mb")
        if available is not None and floor:
            left = available - (mem.get("reserved_mb") or 0)
            if left < floor + LANE_MB:
                return (f"only {max(left, 0)} MB of memory is free (agentos keeps {floor} MB free, and a lane "
                        f"needs about {LANE_MB} MB)")
        return None

    # ---------------------------------------------------------------- the round
    async def run_round(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or self.now()
        if not self.enabled():
            return {"enabled": False}
        if self._lock.locked():
            return {"enabled": True, "busy": self.current}
        if not self._store_ready(now):
            return {"enabled": True, "store": self.store_error}
        plans = []
        for info in self._browser_lanes():
            try:
                plan = self.plan(info, now, enabled=True)
            except LaneNotFound:
                continue
            await self._follow_marks(plan)
            plans.append(plan)
        due = sorted((p for p in plans if p["_block"] is None and p.get("_at") is not None and p["_at"] <= now),
                     key=lambda p: p["_at"])
        if not due or (self._not_before is not None and now < self._not_before):
            return {"enabled": True, "due": [p["lane"] for p in due]}
        self.waiting = self.room()
        if self.waiting is not None:
            log.info("keepalive: %s is due, waiting: %s", due[0]["lane"], self.waiting)
            return {"enabled": True, "due": [p["lane"] for p in due], "waiting": self.waiting}
        try:
            done = await self._visit(due[0]["lane"], due[0]["_entry"], trigger="planned", enabled=True)
        except _NoRoom as e:
            self.waiting = e.reason
            return {"enabled": True, "due": [p["lane"] for p in due], "waiting": self.waiting}
        return {"enabled": True, "due": [p["lane"] for p in due], "visited": done}

    async def _follow_marks(self, plan: dict[str, Any]) -> None:
        """Tell the owner once per episode when a lane is signed out, challenged or on a wrong account,
        whatever a later round calls it (a visit's signed_out is a held lane's signed_out next round).
        Only a signed-out mark clears here, once the lane check sees the lane signed in again; the
        others end with a visit that goes well (_visit_locked), never with a round that finds no block."""
        block, row = plan["_block"], plan["_row"]
        lane_id = plan["lane"]
        if block is not None and block.notify:
            if row.status is None or not row.notified:
                db = self._db()
                await db.run(db.set_status, lane_id, block.code, block.reason, notified=True)
                await self._notify(lane_id, block.code, block.reason, plan.get("site"))
            return
        if row.status == "signed_out" and block is None and plan["signed_in"]:
            db = self._db()
            await db.run(db.set_status, lane_id, None)
            log.info("keepalive: %s is signed in again (signed_out cleared)", lane_id)

    # ---------------------------------------------------------------- a visit
    def _someone_waits(self, lane_id: str) -> bool:
        try:
            info = self._lanes().get(lane_id)
        except AgentosError:
            return True
        return bool(info.queue_length or info.claim or info.control.state != ControlState.AGENT)

    def _node_has(self, lane_id: str) -> bool:
        """Is control of the lane still the agents' (so the node's visit may take its next step)?"""
        try:
            return self._lanes().get(lane_id).control.state == ControlState.AGENT
        except AgentosError:
            return False

    def _views(self, lane_id: str) -> tuple[int, int]:
        views = getattr(self._lanes(), "views", None)
        return tuple(views(lane_id)) if callable(views) else (0, 0)

    async def _sleep_lane(self, lane_id: str, views_before: int) -> None:
        """Send the lane this visit woke back to cold, unless someone else is on it now, a human has it
        or holds it, or a live view of it is open or was opened during the visit."""
        lanes = self._lanes()
        try:
            info = lanes.get(lane_id)
            if info.state != LaneState.WARM or info.claim or info.queue_length \
                    or info.control.state != ControlState.AGENT:
                return
            open_now, opened = self._views(lane_id)
            if open_now or opened != views_before:
                log.info("keepalive: %s left warm: someone opened its live view during the visit", lane_id)
                return
            await lanes.close(lane_id, principal_kind=visit.PRINCIPAL_KIND)
        except AgentosError as e:
            log.info("keepalive: %s left warm (%s); the idle reaper closes it", lane_id, e.code)

    async def _visit(self, lane_id: str, entry: Entry, *, trigger: str, enabled: bool) -> dict[str, Any]:
        async with self._lock:
            self.current, self._began = lane_id, False
            try:
                return await self._visit_locked(lane_id, entry, trigger=trigger, enabled=enabled)
            finally:
                self.current = None
                if self._began:  # the gap follows every visit that started, however it ended
                    self._set_not_before(self.now() + timedelta(minutes=self.rng.uniform(*GAP_MINUTES)))

    async def _visit_locked(self, lane_id: str, entry: Entry, *, trigger: str, enabled: bool) -> dict[str, Any]:
        lanes = self._lanes()
        now = self.now()
        info = lanes.get(lane_id)
        block = self.plan(info, now, enabled=enabled, asked=trigger == "now")["_block"]
        if block is not None:
            raise InvalidRequest(f"no keepalive visit: {block.reason}")
        if info.state not in planner.HOT and (why := self.room()) is not None:
            raise _NoRoom(why)  # never let the lane manager close another lane for a visit
        woke = info.state == LaneState.COLD
        views_before = self._views(lane_id)[1]
        log.info("keepalive: visiting %s on %s (%s)", lane_id, entry.name, trigger)
        db = self._db()
        record = await db.run(db.start_visit, Visit(lane_id=lane_id, started_at=now, finished_at=now,
                                                    trigger=trigger, site=entry.name, outcome=STARTED))
        self._began = True
        try:
            out = await self._run(lanes, lane_id, entry)
        finally:
            if woke:
                await self._sleep_lane(lane_id, views_before)
        record.finished_at, record.outcome, record.reason = self.now(), out.outcome, out.reason
        record.detail = out.detail()
        db = self._db()
        await db.run(db.finish_visit, record)
        self._record(lane_id, record, out)
        if out.outcome in MARKS:
            await self._mark(lane_id, out, entry)
        elif out.outcome in planner.WENT_WELL:  # a visit went well: whatever was marked is fixed
            db = self._db()
            await db.run(db.set_status, lane_id, None)
        return record.public()

    async def _run(self, lanes: Any, lane_id: str, entry: Entry) -> visit.Outcome:
        """The visit in the lane, cancelled the moment a human takes the lane over or stops agents on it.
        A hold the node sets (the visit's own page showed a challenge) is judged by the visit itself,
        which then stops before its next step (may_act)."""
        task: asyncio.Task | None = None
        taken = False

        def on_control(changed: str, old: Any, new: Any) -> None:
            nonlocal taken
            state = getattr(new, "state", None)
            # a pause with a reason is a hold; one without is a person's stop (LaneManager.stop_agent)
            human = state == ControlState.HUMAN or (state == ControlState.PAUSED and not getattr(new, "reason", None))
            if changed == lane_id and human and task is not None and not task.done():
                taken = True
                task.cancel()

        add = getattr(lanes, "add_control_listener", None)
        if callable(add):
            add(on_control)
        try:
            async with visit.lane_session(lanes, lane_id) as page:
                task = asyncio.create_task(visit.run(
                    page, self._svc("interaction"), entry, pace=self.pace, r=random.Random(self.rng.random()),
                    sleep=self.sleep, waiting=lambda: self._someone_waits(lane_id),
                    may_act=lambda: self._node_has(lane_id)), name=f"agentos-keepalive-{lane_id}")
                try:
                    return await task
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if not taken or (current is not None and current.cancelling()):
                        raise
                    return visit.Outcome(outcome="stopped", reason=visit.TAKEN)
        except AgentosError as e:
            return visit.Outcome(outcome="failed", reason=e.message)
        finally:
            remove = getattr(lanes, "remove_control_listener", None)
            if callable(remove):
                remove(on_control)

    async def _mark(self, lane_id: str, out: visit.Outcome, entry: Entry) -> None:
        """Mark the lane, hold it as the hold policy says, and tell the owner once per episode."""
        reason = out.reason or out.outcome
        db = self._db()

        def mark() -> bool:  # read and mark in one call on the worker: one notice per episode
            row = db.lane(lane_id)
            db.set_status(lane_id, out.outcome, reason, notified=True)
            return row.status is None or not row.notified

        first = await db.run(mark)
        lanes = self._lanes()
        if out.outcome == "wrong_account":
            hold = holds.hold_for(WRONG_ACCOUNT, now=self.now(), detail=reason)
            where = f" on {out.url_host}" if out.url_host else ""
            with contextlib.suppress(AgentosError):
                await lanes.hold(lane_id, reason=f"{hold.reason}{where}", until=hold.until)
        elif out.signal is not None:  # the watched interaction reported it already; this is the same hold
            with contextlib.suppress(AgentosError):
                await lanes.report_signal(lane_id, out.signal, f"keepalive visit on {entry.name}")
        if first:
            await self._notify(lane_id, out.outcome, reason, entry.name)

    def _record(self, lane_id: str, record: Visit, out: visit.Outcome) -> None:
        summary = f"{record.site}: {record.outcome}" + (f" ({record.reason})" if record.reason else "")
        store = getattr(self._svc("lanes"), "store", None)
        add = getattr(store, "add_event", None)
        if callable(add):
            try:
                add(lane_id, "keepalive", {"outcome": record.outcome, "site": record.site, "trigger": record.trigger,
                                           "reason": record.reason, **out.detail()})
            except Exception:
                log.warning("%s: could not record the keepalive lane event", lane_id, exc_info=True)
        traces = self._svc("traces")
        if traces is not None:
            from agentos.policy.traces import TraceEvent

            status = "ok" if planner.counts(record.outcome, record.detail) else "error"
            try:
                traces.write(TraceEvent(kind="keepalive", tool="keepalive.visit", principal="node",
                                        principal_kind="node", lane=lane_id, workspace=lane_id.split("/", 1)[0],
                                        effect="read", status=status, summary=summary[:200], args=out.detail(),
                                        duration_ms=int(out.seconds * 1000)))
            except Exception:
                log.warning("%s: could not trace the keepalive visit", lane_id, exc_info=True)
        log.log(logging.INFO if planner.counts(record.outcome, record.detail) else logging.WARNING,
                "keepalive: %s %s", lane_id, summary)

    async def _notify(self, lane_id: str, code: str, reason: str, site: str | None) -> None:
        notifier = self._svc("notifier")
        send = getattr(notifier, "send", None)
        if send is None:
            return
        where = site or "its site"
        if code == "signed_out":
            title = f"{lane_id} is signed out of {where}"
        elif code == "wrong_account":
            title = f"{lane_id} is signed in to the wrong account on {where}"
        else:
            title = f"{lane_id} needs a human on {where}"
        fix = ("Sign in to the lane's own account through the live view" if code == "wrong_account"
               else "Log in again through the live view")
        body = (f"{reason[:1].upper()}{reason[1:]}. Keepalive stopped visiting it. {fix} (agentos lane view "
                f"{lane_id} --control), then hand the lane back.")
        try:
            result = send({"type": f"keepalive.{code}", "title": title, "body": body, "severity": "warn",
                           "key": f"keepalive:{lane_id}", "lane": lane_id})
            if inspect.isawaitable(result):
                await result
        except Exception as e:  # noqa: BLE001 - notifications never break the round
            log.warning("notifier failed: %s", type(e).__name__)

    # ---------------------------------------------------------------- what the CLI and the panel call
    def status(self, lane_id: str) -> dict[str, Any]:
        info = self._lanes().get(lane_id)
        plan = self.public(self.plan(info, self.now()))
        plan["visits"] = [v.public() for v in self._db().visits(lane_id, HISTORY)]
        plan["visiting"] = self.current == lane_id
        if self.waiting and plan.get("next_at") and plan["next_at"] <= clock.iso_or_none(self.now(), timespec="seconds"):
            plan["waiting"] = self.waiting
        return plan

    def planned(self, start: datetime, end: datetime, *, workspace: str | None = None) -> dict[str, Any]:
        """What keepalive plans between `start` and `end`, for the agenda's timeline (read only).

        Built from plan(), the same plan run_round() decides on, so nothing is planned twice. Keepalive
        knows one visit ahead per lane: the next. The one after it is drawn from how this one goes and
        from the lane's use since, so it is never shown. The time is approximate: any use of the lane
        (an agent call, a flow, a person in the live view) moves the visit later, and a due visit waits
        for a free warm slot, enough memory and a gap after the node's last visit.

        Returns {"items": [...], "note", "enabled", "skipped": [{"lane", "reason"}]}. An item has
        `at` (the planned time, or now for a visit due and waiting), `lane`, `site`, `approx: true`
        and, when a gate holds it for now (a human has control, the lane is in use), `waits` with why.
        A lane keepalive does not visit (the node switch off, the lane opted out, no site, quarantined,
        frozen, signed out or challenged) has no item; `skipped` says why."""
        now = self.now()
        out: dict[str, Any] = {"items": [], "skipped": [], "enabled": self.enabled(),
                               "note": "keepalive plans one visit ahead per lane (the next); its time is "
                                       "approximate, since any use of the lane moves it later and a due visit "
                                       "waits for a free warm slot and memory"}
        if not out["enabled"]:
            out["note"] = "keepalive is off on this node (agentos lane keepalive --node on), so it plans no visits"
            return out
        if self.store is None or not self.store.readable():
            out["note"] = f"keepalive cannot read its plan now: {self.store_error or 'keepalive.db is not open'}"
            return out
        for info in self._browser_lanes():
            lane_id = info.spec.id
            if workspace is not None and not lane_id.startswith(f"{workspace}/"):
                continue
            try:
                plan = self.plan(info, now, enabled=True)
            except (LaneNotFound, ServiceUnavailable):
                continue
            block, at = plan["_block"], plan.get("_at")
            if block is not None and not block.waits:
                out["skipped"].append({"lane": lane_id, "reason": block.reason})
                continue
            if at is None:
                out["skipped"].append({"lane": lane_id, "reason": "no waking hours open in the next days"})
                continue
            at = max(at, now)
            if not start <= at < end:
                continue
            waits = block.reason if block is not None else (
                self.waiting if at <= now and self.waiting else None)
            out["items"].append({
                "lane": lane_id, "workspace": lane_id.split("/", 1)[0], "at": clock.iso_or_none(at, timespec="seconds"), "approx": True,
                "site": plan.get("site"), "title": f"Keepalive visit to {plan.get('site') or 'its site'}",
                "effect": "read", "due_now": at <= now, "timezone": plan.get("timezone"),
                **({"waits": waits} if waits else {}),
                "manage": f"agentos lane keepalive {lane_id}"})
        return out

    def set_opt_out(self, lane_id: str, opted_out: bool) -> dict[str, Any]:
        self._lanes().get(lane_id)
        lane_dir = self._lane_dir(lane_id)
        if lane_dir is not None:
            set_lane_opt_out(lane_dir, opted_out)
        self._db().set_opt_out(lane_id, opted_out)
        return self.status(lane_id)

    async def visit_now(self, lane_id: str, *, wait_s: float = NOW_WAIT_S) -> dict[str, Any]:
        """One visit now, whatever the plan and the node switch say (the owner asked in person). The lane
        gates still apply. Waits up to `wait_s` for a free slot and memory (checked again under the
        one-visit lock), then fails without closing any lane."""
        info = self._lanes().get(lane_id)
        entry = entry_for(info.spec)
        if entry is None:
            raise InvalidRequest(f"{lane_id} names no site, so there is no session to keep alive")
        waited = 0.0
        while True:
            info = self._lanes().get(lane_id)
            why = self.room() if info.state not in planner.HOT else None
            if why is None:
                try:
                    return await self._visit(lane_id, entry, trigger="now", enabled=True)
                except _NoRoom as e:
                    why = e.reason
            if waited >= wait_s:
                raise InvalidRequest(f"no keepalive visit: {why}. Try again once a lane closes.")
            await self.sleep(NOW_POLL_S)
            waited += NOW_POLL_S


def build(runtime: Any, *, home: Path | None = None) -> KeepaliveService:
    service = KeepaliveService(runtime, home=home)
    if runtime is not None:
        runtime.register(SERVICE_NAME, service)
    return service
