"""When a push may go out: quiet hours, digest times, the batch window and urgency (AUD-34).

Pure functions over epoch seconds and IANA zones (`zoneinfo`); the notifier stores the
result as an outbox row's due time. Times are wall-clock times on the recipient's clock:
"21:00" is 21:00 local that day whatever the UTC offset is, so a DST change moves the UTC
instant, never the local one. A time that does not exist on a spring-forward day (02:30 in
a zone that jumps from 02:00 to 03:00) resolves to the same offset past the gap (03:30);
one that happens twice on a fall-back day resolves to the first.

The rules, for one recipient and one event at `now`:

1. Inside quiet hours: held until quiet hours end. That delivery is the morning digest:
   everything held overnight goes out as one card.
2. Outside quiet hours with `digest_at` set: held until the next digest time that is not
   itself inside quiet hours ("collect approvals into one card at a time you choose").
3. Otherwise, with a batch window: the first push goes at once; what follows within the
   window after it collapses into one push at the end of the window.
4. Urgent: when the event says so, or when a held event's `not_after` (the run goes stale)
   comes before its delivery. An urgent event goes out at once, except inside quiet hours
   when the recipient has `urgent_breaks_quiet` off: then it waits like the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
MAX_DIGESTS = 12


def parse_hhmm(value: str) -> tuple[int, int]:
    m = HHMM_RE.match(value)
    if not m:
        raise ValueError(f"{value!r} is not a time of day like 08:00 (24 hour clock)")
    return int(m.group(1)), int(m.group(2))


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"{name!r} is not a time zone name like Europe/Vilnius or UTC") from None


class Timing(BaseModel):
    """When one person wants pushes. The node defaults are MEMBER_DEFAULTS and OWNER_DEFAULTS."""
    model_config = ConfigDict(extra="forbid")

    timezone: str = "UTC"
    quiet_hours: list[str] | None = None        # [start, end], may cross midnight; None: never quiet
    digest_at: list[str] = Field(default_factory=list)  # hold non-urgent pushes until these times
    batch_window_s: int = Field(default=0, ge=0, le=86400)
    urgent_breaks_quiet: bool = True

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        zone(v)
        return v

    @field_validator("quiet_hours")
    @classmethod
    def _quiet(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if len(v) != 2:
            raise ValueError("quiet hours are two times: when they start and when they end")
        if parse_hhmm(v[0]) == parse_hhmm(v[1]):
            raise ValueError("quiet hours must start and end at different times")
        return v

    @field_validator("digest_at")
    @classmethod
    def _digests(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_DIGESTS:
            raise ValueError(f"at most {MAX_DIGESTS} digest times")
        for t in v:
            parse_hhmm(t)
        return sorted(set(v))

    @model_validator(mode="after")
    def _some_digest_outside_quiet(self) -> Timing:
        if self.digest_at and self.quiet_hours and all(_minute_in_quiet(_minutes(t), self) for t in self.digest_at):
            raise ValueError("every digest time is inside quiet hours; pick one outside them")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return zone(self.timezone)


# A member's defaults: quiet from 21:00 to
# 08:00, a 10 minute batch window, urgent pushes wait for the morning.
MEMBER_DEFAULTS = Timing(quiet_hours=["21:00", "08:00"], batch_window_s=600, urgent_breaks_quiet=False)
# The owner's defaults keep a solo node as it was: no quiet hours until the owner sets them;
# once set, urgent pushes break them (open question 6).
OWNER_DEFAULTS = Timing()


def _minutes(hhmm: str) -> int:
    h, m = parse_hhmm(hhmm)
    return h * 60 + m


def _minute_in_quiet(minute: int, timing: Timing) -> bool:
    if not timing.quiet_hours:
        return False
    start, end = (_minutes(t) for t in timing.quiet_hours)
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # across midnight


def local(t: float, timing: Timing) -> datetime:
    return datetime.fromtimestamp(t, timing.tz)


def in_quiet(t: float, timing: Timing) -> bool:
    lt = local(t, timing)
    return _minute_in_quiet(lt.hour * 60 + lt.minute, timing)


def _instant(day: date, hhmm: str, tz: ZoneInfo) -> float:
    h, m = parse_hhmm(hhmm)
    return datetime.combine(day, time(h, m), tzinfo=tz).timestamp()


def next_at(t: float, hhmm: str, timing: Timing) -> float:
    """The first instant after `t` when the recipient's clock shows `hhmm`."""
    day = local(t, timing).date()
    for d in range(-1, 3):
        at = _instant(day + timedelta(days=d), hhmm, timing.tz)
        if at > t:
            return at
    raise AssertionError("unreachable: a time of day comes round within two days")


def quiet_end(t: float, timing: Timing) -> float | None:
    """When the quiet hours `t` falls in end, or None outside quiet hours."""
    if not timing.quiet_hours or not in_quiet(t, timing):
        return None
    return next_at(t, timing.quiet_hours[1], timing)


def next_digest(t: float, timing: Timing) -> float | None:
    """The next digest time after `t` that is not inside quiet hours, or None without digests."""
    if not timing.digest_at:
        return None
    day = local(t, timing).date()
    slots = sorted(_instant(day + timedelta(days=d), hhmm, timing.tz)
                   for d in range(-1, 3) for hhmm in timing.digest_at)
    return next((at for at in slots if at > t and not in_quiet(at, timing)), None)


@dataclass(frozen=True)
class Plan:
    due: float            # when the push may go out
    held: str | None      # None (now), "quiet", "digest" or "batch"
    urgent: bool


def plan(now: float, timing: Timing, *, urgent: bool = False, not_after: float | None = None,
         last_push: float | None = None, open_batch: float | None = None) -> Plan:
    """When to push one event to one recipient. `last_push`: when this recipient's last push on
    the channel went out; `open_batch`: the due time of a batch already waiting for them."""
    held: str | None = None
    due = now
    end = quiet_end(now, timing)
    if end is not None:
        held, due = "quiet", end
    elif timing.digest_at:
        nxt = next_digest(now, timing)
        if nxt is not None:
            held, due = "digest", nxt
    elif timing.batch_window_s:
        if open_batch is not None and open_batch >= now:
            held, due = "batch", open_batch
        elif last_push is not None and now - last_push < timing.batch_window_s:
            held, due = "batch", last_push + timing.batch_window_s
    stale = held is not None and not_after is not None and not_after < due
    urgent = urgent or stale
    if urgent and (held != "quiet" or timing.urgent_breaks_quiet):
        return Plan(now, None, True)
    return Plan(due, held, urgent)
