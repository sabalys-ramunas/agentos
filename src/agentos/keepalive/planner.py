"""When a lane's next keepalive visit is, and why a lane is skipped. Pure functions over facts the node
already keeps, so every rule is tested without a clock, a browser or a daemon.

When a visit is due:

    activity  the lane's last use: the latest of its last_used (every agent call, flow and connector
              run and brain goes through the lane manager's using(), which stamps
              it), the last human input in its live view, and the last keepalive visit that counts
              (`counts`: one that went well, one cut short after the feed loaded, or one agentosd
              did not live to finish). A lane never used counts from its creation.
    due       activity plus a cadence drawn inside the site's range (agentos.keepalive.sites), from a
              generator keyed by the lane's own seed and that activity, so a plan is stable between
              rounds and two lanes never share a rhythm. When the lane check saw the session cookie's
              expiry, the visit is due a day before it at the latest.
    when      the first moment from `due` inside the lane's waking hours, in the lane's pinned time
              zone (DST included: hours are local wall-clock hours, the arithmetic is in UTC). Each day
              draws its own minute and second inside that day's window, and moves it when it falls within
              REPEAT_MIN of the last visit's time of day, so visits never land on a fixed clock. A
              missed slot (the lane was busy, memory was short, agentosd was down) goes at once while
              the lane is awake, else LATE_MIN (drawn) after its waking hours next begin.
    retry     after an attempt that did not count, RETRY_MIN (drawn) later; after one agentosd did
              not live to finish, at least RESTART_GAP later (`retry_at`). A slot that came before it
              counts as missed at that time (next_visit with `now` the retry time).

Waking hours come from a persona drawn from the lane's keepalive seed (agentos.keepalive.persona:
its wake time, waking span and weekend shift).
When the owner set a posting pace on the lane (agentos.policy.pace, AUD-13), its timezone holds and a
slot outside its active_hours moves to a little after they next open (KeepaliveService._waking): a
session at 3 am lane time is a signal of its own, so visits keep to the hours posts keep to.

`blocked` says why a due lane is not visited now: the owner's switches, a quarantine, a freeze, a human
in control, a hold, an agent's drive on the lane, a claim or a queue, a lane already warm (it is in use or
was a moment ago), a bad last
signal (a challenge or checkpoint, a login page), a hold for a wrong account, or a lane check that found
it signed out. Those last ones tell the owner instead (`Block.notify`), once per episode whatever they
are called (agentos.keepalive.service).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from agentos.core.models import ControlState, LaneInfo, LaneState, Signal
from agentos.core.signals import NEEDS_HUMAN
from agentos.keepalive.persona import Persona, draw_persona, rng
from agentos.keepalive.sites import Entry

EXPIRY_MARGIN = timedelta(days=1)    # visit at least this long before the session cookie expires
VISIT_ROOM = timedelta(minutes=10)   # a slot leaves this much of the window for the visit itself
REPEAT_MIN = timedelta(minutes=20)   # a slot moves when this close to the last visit's time of day
LATE_MIN = (2.0, 12.0)              # a missed slot goes this many minutes after the next waking hours begin
RETRY_MIN = (30.0, 90.0)            # minutes after an attempt that did not count
RESTART_GAP = timedelta(hours=6)    # at least this after a visit agentosd did not live to finish
LOOKAHEAD_DAYS = 10
BAD_SIGNALS = NEEDS_HUMAN
HOT = frozenset({LaneState.WARMING, LaneState.WARM, LaneState.ACTIVE})
WENT_WELL = frozenset({"ok", "yielded"})
UNFINISHED = frozenset({"started", "interrupted"})   # agentos.keepalive.store STARTED, INTERRUPTED
WRONG_ACCOUNT_HOLD = "needs_human:wrong_account"     # agentos.node.holds for sites.WRONG_ACCOUNT


def _aware(when: datetime | None) -> datetime | None:
    if when is None:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def latest(*whens: datetime | None) -> datetime | None:
    found = [w for w in map(_aware, whens) if w is not None]
    return max(found) if found else None


def counts(outcome: str, detail: dict | None = None) -> bool:
    """Does a visit with this outcome count as one (activity, the next cadence)? One that went well,
    one cut short after the feed loaded signed in, or one agentosd did not live to finish (it may have
    run: never repeat it at once)."""
    if outcome in WENT_WELL or outcome in UNFINISHED:
        return True
    return outcome == "cut" and bool((detail or {}).get("loaded")) and (detail or {}).get("session") != "signed_out"


def retry_at(seed: str, outcome: str, started: datetime, finished: datetime,
             detail: dict | None = None) -> datetime | None:
    """The earliest next attempt after the last one: RESTART_GAP after one never finished, RETRY_MIN
    (drawn, stable between rounds) after one that did not count, None after one that did."""
    if outcome in UNFINISHED:
        return _aware(started) + RESTART_GAP
    if counts(outcome, detail):
        return None
    r = rng(seed, "keepalive", "retry", _aware(started).isoformat())
    return _aware(finished) + timedelta(minutes=r.uniform(*RETRY_MIN))


def persona_for(lane_id: str, seed: str) -> Persona:
    """The lane's habits, drawn from its keepalive seed."""
    return draw_persona(lane_id, seed)


def cadence(entry: Entry, seed: str, activity: datetime | None) -> timedelta:
    lo, hi = entry.cadence
    r = rng(seed, "keepalive", "cadence", activity.isoformat() if activity else "-")
    return timedelta(days=r.uniform(lo, hi))


def due_at(entry: Entry, seed: str, activity: datetime | None, expires: datetime | None,
           now: datetime) -> datetime:
    """When the lane wants a visit (it may be in the past). Never used: now."""
    activity, expires = _aware(activity), _aware(expires)
    due = activity + cadence(entry, seed, activity) if activity is not None else now
    if expires is not None:
        due = min(due, expires - EXPIRY_MARGIN)
    return due


def local_moment(day: date, hours: float, tz: ZoneInfo) -> datetime:
    """`hours` after local midnight of `day`, as a wall-clock time in `tz`, in UTC. A wall time that
    does not exist (the hour skipped when clocks go forward) comes out an hour later."""
    wall = datetime.combine(day, time()) + timedelta(hours=hours)
    return wall.replace(tzinfo=tz).astimezone(UTC)


def window(persona: Persona, day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The lane's waking hours on local `day`, in UTC."""
    first, last = persona.for_day(day).window(day)
    return local_moment(day, first, tz), local_moment(day, last, tz)


def _time_of_day(when: datetime, tz: ZoneInfo) -> timedelta:
    local = when.astimezone(tz)
    return timedelta(hours=local.hour, minutes=local.minute, seconds=local.second)


def _slot(seed: str, day: date, start: datetime, end: datetime, tz: ZoneInfo,
          last_visit: datetime | None) -> datetime:
    r = rng(seed, "keepalive", "slot", day.isoformat(), start.isoformat())
    span = max((end - start).total_seconds(), 0.0)
    at = start + timedelta(seconds=r.uniform(0, span))
    if last_visit is not None:
        gap = abs(_time_of_day(at, tz) - _time_of_day(last_visit, tz))
        if gap < REPEAT_MIN:  # move away from yesterday's time, inside the window when there is room
            step = REPEAT_MIN + timedelta(minutes=r.uniform(5, 25))
            later, earlier = at + step, at - step
            at = later if later <= end else earlier if earlier >= start else at
    if at.astimezone(tz).second == 0:  # never on the minute
        at += timedelta(seconds=r.randint(7, 53))
    return min(at, end)


def _windows(persona: Persona, tz: ZoneInfo, due: datetime) -> Iterator[tuple[date, datetime, datetime]]:
    """(local day, start, end) of each waking window from `due`'s day, in order, cut to start at `due`
    and to leave VISIT_ROOM before its end; empty windows are left out."""
    day = due.astimezone(tz).date() - timedelta(days=1)  # a window that started the evening before
    for _ in range(LOOKAHEAD_DAYS + 1):
        start, end = window(persona, day, tz)
        end -= VISIT_ROOM
        start = max(start, due)
        if start < end:
            yield day, start, end
        day += timedelta(days=1)


def slots(seed: str, persona: Persona, tz: ZoneInfo, due: datetime,
          last_visit: datetime | None = None) -> Iterator[tuple[datetime, datetime]]:
    """(slot, end of that day's window) for each local day from `due`'s, in order."""
    for day, start, end in _windows(persona, tz, due):
        yield _slot(seed, day, start, end, tz, last_visit), end


def next_visit(seed: str, persona: Persona, tz: ZoneInfo, due: datetime, now: datetime,
               last_visit: datetime | None = None) -> datetime | None:
    """When to visit: the first slot from `due` while it is still ahead. Once that slot was missed: now
    while the lane is awake, else LATE_MIN (drawn per day) after its waking hours next begin. None when
    no window opens in LOOKAHEAD_DAYS (a persona that never wakes; not possible with drawn personas)."""
    missed = False
    for day, start, end in _windows(persona, tz, due):
        if not missed:
            at = _slot(seed, day, start, end, tz, last_visit)
            if at >= now:
                return at
            missed = True
        if now >= end:
            continue
        if now >= start:
            return now
        late = timedelta(minutes=rng(seed, "keepalive", "late", day.isoformat()).uniform(*LATE_MIN))
        return min(start + late, end)
    return None


# ---- why a lane is not visited ------------------------------------------------------------------

@dataclass(frozen=True)
class Block:
    code: str
    reason: str
    notify: bool = False       # the owner is told (once per episode)
    waits: bool = False        # it clears by itself: the lane is visited once it does


def blocked(info: LaneInfo, *, enabled: bool, opted_out: bool, frozen: bool, entry: Entry | None,
            signed_in: bool | None, now: datetime, driven: bool = False) -> Block | None:
    """Why this lane is not visited now, or None. Checked again right before a visit starts. `driven`: an
    agent's drive is on the lane (it acted through the commit gate and control has not left the agent)."""
    lane = info.spec.id
    if not enabled:
        return Block("off", "keepalive is off on this node (agentos lane keepalive --node on)")
    if opted_out:
        return Block("opted_out", f"{lane} opted out (agentos lane keepalive {lane} --on)")
    if entry is None:
        return Block("no_site", f"{lane} names no site, so there is no session to keep alive")
    if info.state == LaneState.QUARANTINED:
        return Block("quarantined", f"{lane} is quarantined: {info.quarantine_reason or 'see agentos lane check'}")
    if frozen:
        return Block("frozen", f"{lane} is frozen")
    control = info.control
    if control.state == ControlState.HUMAN:
        return Block("human", f"a human has control of {lane}", waits=True)
    if control.state == ControlState.PAUSED:
        until = _aware(control.hold_until)
        if control.reason and until is not None and until > now:
            return Block("held", f"{lane} is held until {until:%Y-%m-%d %H:%M} UTC ({control.reason})", waits=True)
        if (control.reason or "").startswith(WRONG_ACCOUNT_HOLD):
            return Block("wrong_account", f"{entry.name} on {lane} is signed in to another account than the "
                         "lane's own, and a human has to look at it", notify=True)
        if info.signal == Signal.LOGGED_OUT:
            return Block("signed_out", f"{lane} is signed out of {entry.name} (it showed its login page)",
                         notify=True)
        if info.signal in BAD_SIGNALS:
            return Block("challenged", f"{entry.name} showed {info.signal.value} on {lane}, and a human has to "
                         "look at it", notify=True)
        return Block("held", f"agents are held off {lane}" + (f" ({control.reason})" if control.reason else ""))
    if driven:
        return Block("driven", f"an agent is driving {lane}, and its drive ends when control leaves the agent",
                     waits=True)
    if info.claim:
        return Block("claimed", f"{info.claim.get('holder') or 'an agent'} claims {lane}", waits=True)
    if info.queue_length:
        return Block("queued", f"callers wait for {lane}", waits=True)
    if info.state in HOT:
        return Block("in_use", f"{lane} is open (in use, or used a moment ago)", waits=True)
    if info.signal == Signal.LOGGED_OUT:
        return Block("signed_out", f"{lane} is signed out of {entry.name} (the last page it showed was its "
                     "login page)", notify=True)
    if info.signal in BAD_SIGNALS:
        return Block("challenged", f"the last page {lane} showed was {info.signal.value}, so a human looks "
                     "first", notify=True)
    if signed_in is False:
        return Block("signed_out", f"{lane} is signed out of {entry.name} (from its session cookies)", notify=True)
    return None
