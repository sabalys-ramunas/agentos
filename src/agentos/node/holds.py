"""Lane holds after a bad signal (AUD-07), as pure functions the lane manager calls.

Retrying on a challenged account is how accounts get locked, so the first bad signal holds the lane
for every agent, flow, schedule and brain at once (LaneManager.using() refuses non-human callers while
control is PAUSED):

    signal                                    hold
    captcha, checkpoint, blocked, logged_out  until a human hands the lane back
    wrong_account (sites.AccountCheck)        until a human hands the lane back
    rate_limited                              a backoff (RATE_LIMIT_BACKOFF, 6 hours), never shorter
    ok, unknown                               none

The lane manager writes the hold (agentos.node.lanes.LaneManager.hold, through its hold policy, which
is `hold_for` here): `LaneControl(state=PAUSED, by=NODE_BY, reason=..., hold_until=...)`, where
NODE_BY is "node" for every hold the node sets itself, a quarantine's hold included. A hold is a
control with a reason; `by` says who set it, never whether it is a hold. The reason is
`needs_human:<signal>` for the first kind and `backoff:rate_limited` for the second. The manager
merges holds (a needs-human hold wins, a backoff only extends) and ends an expired backoff itself; a
human hand-back clears any hold. This module only says which hold a signal calls for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agentos.core.models import Signal
from agentos.core.signals import HOLD_FOR_HUMAN
from agentos.node.sites import WRONG_ACCOUNT, SignalReport

RATE_LIMIT_BACKOFF = timedelta(hours=6)
NEEDS_HUMAN = "needs_human:"
BACKOFF = "backoff:"

HUMAN_SIGNALS: frozenset[str] = HOLD_FOR_HUMAN


@dataclass(frozen=True, slots=True)
class Hold:
    signal: str                           # "checkpoint", "rate_limited", "wrong_account", ...
    reason: str                           # "needs_human:checkpoint" or "backoff:rate_limited"
    since: datetime
    until: datetime | None                # None: until a human hands the lane back
    detail: str | None = None             # the rule that matched, for the notification

    @property
    def needs_human(self) -> bool:
        return self.until is None


def hold_for(signal: Signal | str, *, now: datetime, detail: str | None = None,
             backoff: timedelta = RATE_LIMIT_BACKOFF) -> Hold | None:
    """The hold a signal (or "wrong_account") calls for, or None. A backoff shorter than
    RATE_LIMIT_BACKOFF is raised to it."""
    value = getattr(signal, "value", signal)
    if value in HUMAN_SIGNALS:
        return Hold(value, NEEDS_HUMAN + value, now, None, detail)
    if value == Signal.RATE_LIMITED.value:
        return Hold(value, BACKOFF + value, now, now + max(backoff, RATE_LIMIT_BACKOFF), detail)
    return None


def hold_for_report(report: SignalReport | None, *, now: datetime,
                    backoff: timedelta = RATE_LIMIT_BACKOFF) -> Hold | None:
    """The hold for a page's report: a needs-human signal first, then a wrong account, then a backoff."""
    if report is None:
        return None
    hold = hold_for(report.signal, now=now, detail=report.detail, backoff=backoff)
    if hold is not None and hold.needs_human:
        return hold
    if report.wrong_account:
        return hold_for(WRONG_ACCOUNT, now=now, detail=report.detail)
    return hold
