"""Caps, the weekly ramp and warm-up stages (docs/design/engagement-engine.md 5.4 and 5.5).

These are hard limits: the governor applies them whatever a program asks for. The `careful`
start is where every account begins, the ramp adds 15 percent a week after a week with no
warning, and growth stops at the risk level's ceiling. The `normal` ceiling is the hard
ceiling: agents can never raise a cap, and the owner can raise one only up to it.

Every number is a quarter to a half of the folklore ceilings collected in section 3.3 (no
platform publishes a safe rate), and the `normal` ceiling stays at or under about half of the
lowest folklore figure for an aged account (FOLKLORE_AGED_LOW, checked by the tests).

Kinds are the effect kinds of the egress rules: `like` covers a LinkedIn reaction and a Medium
clap, `follow` covers a LinkedIn connection request. `profile_edit` (brief B2) is one cap over
every change to the account's own public profile (name, headline, About, photo, cover): the
planner never draws it from a target, it plans the edits a program's `profile:` asks for
(agentos.engage.profile), and it starts with the steady phase (warm-up stages allow none).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    POST = "post"
    COMMENT = "comment"
    LIKE = "like"
    FOLLOW = "follow"
    JOIN = "join"
    STORY_VIEW = "story_view"
    PROFILE_EDIT = "profile_edit"    # the account's own public profile (brief B2): never in `daily`


class Risk(StrEnum):
    CAREFUL = "careful"
    NORMAL = "normal"
    OFF = "off"          # posting and replies on own posts only


# Kinds that act on someone else's content: they count toward the "seen, not acted on" ratio.
ACTS = frozenset({Kind.LIKE, Kind.COMMENT, Kind.FOLLOW})

RAMP_PER_WEEK = 0.15
SOFT_FREEZE_WEEKS = 2


@dataclass(frozen=True)
class Cap:
    """One row of the caps table: per day (`start`, `careful`, `normal`), per rolling hour, and
    optionally per week (a LinkedIn connection request has both)."""
    start: int
    careful: int
    normal: int
    hourly: int
    week_start: int | None = None
    week_careful: int | None = None
    week_normal: int | None = None
    normal_start: int | None = None      # the normal risk's start, when it is not twice the careful one

    def ceiling(self, risk: Risk) -> int:
        return self.normal if risk is Risk.NORMAL else self.careful

    def week_ceiling(self, risk: Risk) -> int | None:
        if self.week_careful is None:
            return None
        return self.week_normal if risk is Risk.NORMAL else self.week_careful


SITES = ("instagram", "facebook", "linkedin", "reddit", "medium", "blog")

# Section 5.5. A feed post is "per calendar" at the start; the planner takes the program's
# target, never above these caps. A Reddit post is also 1 a week per subreddit (the session
# runner keeps that gap, brief E3). Reddit has no `like`: software may not choose votes.
CAPS: dict[str, dict[Kind, Cap]] = {
    "instagram": {
        Kind.LIKE: Cap(10, 40, 80, 15),
        Kind.FOLLOW: Cap(2, 10, 20, 5),
        Kind.COMMENT: Cap(1, 5, 10, 3),
        Kind.STORY_VIEW: Cap(10, 40, 80, 30),
        Kind.POST: Cap(1, 2, 3, 1),
    },
    "facebook": {
        Kind.LIKE: Cap(5, 25, 50, 10),
        Kind.COMMENT: Cap(1, 3, 6, 2),
        Kind.POST: Cap(1, 2, 3, 1),
    },
    "linkedin": {
        Kind.LIKE: Cap(5, 20, 30, 8),
        Kind.COMMENT: Cap(1, 3, 5, 2),
        Kind.FOLLOW: Cap(2, 5, 10, 3, week_start=10, week_careful=25, week_normal=50),
        Kind.POST: Cap(1, 2, 3, 1),
        # one cap over the five profile edits together: changing a public identity often reads as
        # a taken-over account, so it starts at 1 a day at either risk and goes 1 an hour
        Kind.PROFILE_EDIT: Cap(1, 2, 3, 1, normal_start=1),
    },
    "reddit": {
        Kind.COMMENT: Cap(1, 4, 8, 2),
        Kind.JOIN: Cap(1, 2, 3, 1),
        Kind.POST: Cap(1, 1, 1, 1),
    },
    "medium": {
        Kind.LIKE: Cap(3, 10, 20, 5),
        Kind.FOLLOW: Cap(3, 10, 20, 5),
        Kind.POST: Cap(1, 2, 3, 1),
    },
    "blog": {
        Kind.POST: Cap(1, 2, 3, 1),
    },
}

# Section 3.3, the lowest folklore figure for an aged account (per day unless noted). Only used
# to check the table above stays well under it.
FOLKLORE_AGED_LOW: dict[tuple[str, Kind], int] = {
    ("instagram", Kind.FOLLOW): 150,
    ("instagram", Kind.LIKE): 300,
    ("instagram", Kind.COMMENT): 100,
}
FOLKLORE_AGED_LOW_WEEK: dict[tuple[str, Kind], int] = {("linkedin", Kind.FOLLOW): 80}


def site_kinds(site: str) -> tuple[Kind, ...]:
    return tuple(CAPS.get(site, {}))


def cap_row(site: str, kind: Kind) -> Cap | None:
    return CAPS.get(site, {}).get(kind)


def hard_ceiling(site: str, kind: Kind) -> int:
    row = cap_row(site, kind)
    return row.normal if row is not None else 0


def day_limits(site: str, plan_caps: Mapping[Any, int] | None, plan_hourly: Mapping[Any, int] | None,
               owner_caps: Mapping[Any, int] | None) -> tuple[dict[str, int], dict[str, int]]:
    """The day's and the hour's caps as the executor enforces them, by kind: the plan's (drawn by
    the governor), never above the owner's current cap or the site's hard ceiling, and an hour's
    cap never above the day's. A kind the plan has no cap for has none (the executor skips it with
    cap_daily). An owner who lowers a cap after the day was drawn is obeyed at once; a raised cap
    waits for the next day's plan. The executor (Executor._limits) and the digest both use this."""
    caps = {str(k): int(v) for k, v in (plan_caps or {}).items()}
    hourly = {str(k): int(v) for k, v in (plan_hourly or {}).items()}
    for k, v in (owner_caps or {}).items():
        if str(k) in caps:
            caps[str(k)] = min(caps[str(k)], max(0, int(v)))
    for k in list(caps):
        try:
            row = cap_row(site, Kind(k))
        except ValueError:     # a kind with no caps table row (a later kind)
            row = None
        if row is not None:
            caps[k] = min(caps[k], row.normal)
    hourly = {k: min(v, caps[k]) if k in caps else v for k, v in hourly.items()}
    return caps, hourly


def ramped(start: int, ceiling: int, clean_weeks: int, *, halvings: int = 0) -> int:
    """`start` grown 15 percent per clean week, never above `ceiling`; each hard signal halves it."""
    if ceiling <= 0:
        return 0
    value = min(ceiling, math.floor(start * (1 + RAMP_PER_WEEK) ** max(0, clean_weeks) + 1e-9))
    for _ in range(max(0, halvings)):
        value //= 2
    return max(0, value)


def clean_weeks(steady_start: date, day: date, warnings: list[date] | tuple[date, ...] = ()) -> int:
    """Whole steady weeks before `day` that count toward the ramp: a week with a warning (a soft
    signal) does not count, and freezes the ramp for SOFT_FREEZE_WEEKS weeks after it."""
    if day <= steady_start:
        return 0
    weeks_done = (day - steady_start).days // 7
    count = 0
    frozen_until = -1
    for w in range(weeks_done):
        lo, hi = steady_start + timedelta(days=7 * w), steady_start + timedelta(days=7 * (w + 1))
        if any(lo <= d < hi for d in warnings):
            frozen_until = w + SOFT_FREEZE_WEEKS
            continue
        if w <= frozen_until:
            continue
        count += 1
    return count


@dataclass(frozen=True)
class Stage:
    """A warm-up stage (section 5.4): how many sessions, how long, and what is allowed."""
    name: str
    first_day: int                  # 1-based day of the warm-up it starts on
    sessions: tuple[int, int]
    minutes: tuple[float, float] | None
    limits: dict[Kind, int]
    comment_every_other_day: bool = False

    def describe(self) -> str:
        return {"look": "only looking around", "light": "a few likes and follows",
                "first_comments": "likes, follows and a first comment every other day"}[self.name]


WARM_UP_STAGES: tuple[Stage, ...] = (
    Stage("look", 1, (1, 2), (5.0, 12.0), {Kind.STORY_VIEW: 5}),
    Stage("light", 4, (2, 2), None, {Kind.LIKE: 5, Kind.FOLLOW: 1, Kind.JOIN: 1, Kind.STORY_VIEW: 8, Kind.POST: 1}),
    Stage("first_comments", 8, (2, 3), None,
          {Kind.LIKE: 10, Kind.FOLLOW: 3, Kind.JOIN: 1, Kind.STORY_VIEW: 10, Kind.POST: 1, Kind.COMMENT: 1},
          comment_every_other_day=True),
)
STEADY_SESSIONS = (2, 4)


def warm_up_stage(day: int) -> Stage:
    """The stage of 1-based warm-up day `day`; the last stage lasts to the end of the warm-up."""
    stage = WARM_UP_STAGES[0]
    for s in WARM_UP_STAGES:
        if day >= s.first_day:
            stage = s
    return stage
