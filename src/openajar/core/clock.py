"""Time: now as an aware UTC datetime, and the ISO strings the stores and the wire already write.

One set of helpers in place of the private copies (`_now`, `_iso`, `iso`, `_stamp`) packages kept.
Every store and message keeps its own format byte for byte, so the format is a parameter, never a
guess: pick the call that writes what that place wrote before.

    iso(t)                                 2026-11-14T09:30:00.123456+00:00 (no micros when zero)
    iso(t, z=True)                         2026-11-14T09:30:00.123456Z
    iso(t, timespec="seconds")             2026-11-14T09:30:00+00:00
    iso(t, z=True, timespec="seconds")     2026-11-14T09:30:00Z
    iso(t, timespec="microseconds")        fixed width, so SQLite orders the strings by time
    stamp(dt)                              20261114T093000123456Z (file and folder names)

`t` is an aware datetime (converted to UTC) or a POSIX timestamp (seconds, float or int), or None
for now. A naive datetime is taken as local time, as `datetime.astimezone` takes it.

`Clock` is the wall and monotonic time a service reads, so a test can swap in a fake one.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Literal

Timespec = Literal["auto", "hours", "minutes", "seconds", "milliseconds", "microseconds"]


def now() -> datetime:
    """Now, as an aware UTC datetime."""
    return datetime.now(UTC)


def utc(moment: datetime | float) -> datetime:
    """A datetime in UTC, from an aware datetime or a POSIX timestamp."""
    if isinstance(moment, datetime):
        return moment.astimezone(UTC)
    return datetime.fromtimestamp(moment, UTC)


def iso(moment: datetime | float | None = None, *, z: bool = False, timespec: Timespec = "auto") -> str:
    """ISO 8601 in UTC: `+00:00` at the end, or `Z` with `z=True`. None is now."""
    text = (now() if moment is None else utc(moment)).isoformat(timespec=timespec)
    return text.replace("+00:00", "Z") if z else text


def iso_or_none(moment: datetime | float | None, *, z: bool = False,
                timespec: Timespec = "auto") -> str | None:
    """`iso` for a value that may be missing: None stays None (0 is the epoch, not missing)."""
    return None if moment is None else iso(moment, z=z, timespec=timespec)


def stamp(moment: datetime | None = None) -> str:
    """A compact UTC stamp for file and folder names, `20261114T093000123456Z`. None is now."""
    return (moment or now()).strftime("%Y%m%dT%H%M%S%fZ")


def parse(value: str | None) -> datetime | None:
    """An ISO 8601 string (`Z` or an offset) as an aware datetime; one with no offset is UTC.
    Empty is None. Raises ValueError for text that is not a date."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Clock:
    """Wall time for records and schedules, monotonic time for waits. Tests swap in a fake."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))
