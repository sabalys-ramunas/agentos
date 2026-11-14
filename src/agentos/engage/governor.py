"""The safety governor (engagement-engine.md 5.5 and 5.8): it has the last word over any plan.

- `budget(...)`: one account's day: its phase (warm-up stage or steady), the caps it gets
  (start, the 15 percent weekly ramp after clean weeks, the risk level's ceiling, the owner's
  override up to the hard ceiling, weekly caps, halvings after a hard signal) and the targets
  the planner draws around.
- `check_cap_change(...)`: agents never raise a cap; the owner may, up to the hard ceiling.
- Company separation: accounts of one company (one workspace) never start sessions within
  COMPANY_GAP of each other; the planner draws each start against what the governor already
  holds for the company.
- `audit(...)`: the nightly lagged-correlation check over 14 days of hourly action counts. A pair
  whose activity moves together (correlation at or above 0.5 at a lag of up to 2 hours) is
  flagged; the service redraws one persona and tells the owner.

Brief E1 has no signals yet (they come with the session runner, E3), so warnings and halvings
are inputs here and empty in a dry run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from agentos.engage.caps import (
    STEADY_SESSIONS,
    Kind,
    Risk,
    Stage,
    cap_row,
    clean_weeks,
    hard_ceiling,
    ramped,
    site_kinds,
    warm_up_stage,
)
from agentos.engage.errors import CapCeiling, OwnerOnly
from agentos.engage.models import DayPlan, Program

COMPANY_GAP = timedelta(minutes=20)
AUDIT_DAYS = 14
AUDIT_THRESHOLD = 0.5         # open question 7
AUDIT_MAX_LAG_H = 2


@dataclass
class Budget:
    phase: str
    phase_day: int
    stage: Stage | None
    ramp_weeks: int
    targets: dict[Kind, int]
    caps: dict[Kind, int]
    hourly: dict[Kind, int]
    sessions: tuple[int, int]
    minutes: tuple[float, float] | None
    notes: list[str] = field(default_factory=list)
    profile: int = 0           # profile edits the day makes: the fields that differ, at most the cap


def is_owner(by: Any) -> bool:
    """The human owner in person (the CLI on the node, the owner's panel session). Agents,
    flows, brains and other humans are not."""
    if by is None:
        return False
    p = by if isinstance(by, dict) else (by.model_dump() if hasattr(by, "model_dump") else vars(by))
    return p.get("kind") == "human" and bool(p.get("id")) and p.get("id") == p.get("owner", "owner")


def start_cap(site: str, kind: Kind, risk: Risk) -> int:
    row = cap_row(site, kind)
    if row is None or (risk is Risk.OFF and kind is not Kind.POST):
        return 0
    if risk is Risk.NORMAL:
        return row.normal_start if row.normal_start is not None else min(row.normal, row.start * 2)
    return row.start


def ceiling(site: str, kind: Kind, risk: Risk, override: int | None = None) -> int:
    row = cap_row(site, kind)
    if row is None or (risk is Risk.OFF and kind is not Kind.POST):
        return 0
    if override is not None:
        return max(0, min(int(override), row.normal))
    return row.ceiling(Risk.CAREFUL if risk is Risk.OFF else risk)


def check_cap_change(by: Any, site: str, kind: Kind, value: int, current: int) -> None:
    """Refuse a cap change an agent asks for, and any value above the hard ceiling."""
    if not is_owner(by):
        raise OwnerOnly("only the owner can change a cap, in person on the node "
                        f"(`agentos engage cap {kind.value} <surface> <n>`). Agents can never raise one")
    row = cap_row(site, kind)
    if row is None:
        raise CapCeiling(f"{site} has no {kind.value} action, so it has no cap")
    if value < 0 or value > row.normal:
        raise CapCeiling(f"{kind.value} on {site} can go up to {row.normal} a day (the hard ceiling), not {value}")


def budget(program: Program, starts_on: date, day: date, *, overrides: dict[Kind, int] | None = None,
           warnings: list[date] | tuple[date, ...] = (), halvings: dict[Kind, int] | None = None,
           week_used: dict[Kind, int] | None = None, profile_due: int = 0) -> Budget:
    """One account's day. `profile_due`: how many profile fields differ from what the program's
    `profile:` asks for (agentos.engage.profile); the day makes at most its profile_edit cap of them."""
    overrides, halvings, week_used = overrides or {}, halvings or {}, week_used or {}
    site, risk = program.site or "", program.risk
    index = (day - starts_on).days               # 0 on the program's first day
    warm_days = program.warm_up_days
    steady = program.steady
    notes: list[str] = []
    if index < warm_days:
        phase, phase_day = "warm_up", index + 1
        stage: Stage | None = warm_up_stage(phase_day)
        weeks = 0
        sessions, minutes = stage.sessions, stage.minutes
    else:
        phase, phase_day, stage = "steady", index - warm_days + 1, None
        weeks = clean_weeks(starts_on + timedelta(days=warm_days), day, warnings)
        sessions, minutes = STEADY_SESSIONS, None
    targets: dict[Kind, int] = {}
    caps: dict[Kind, int] = {}
    hourly: dict[Kind, int] = {}
    for kind in site_kinds(site):
        row = cap_row(site, kind)
        assert row is not None
        top = ceiling(site, kind, risk, overrides.get(kind))
        if overrides.get(kind) is not None:   # the owner set this cap: it holds from day one, no ramp
            cap = ramped(top, top, 0, halvings=halvings.get(kind, 0))
        else:
            cap = ramped(start_cap(site, kind, risk), top, weeks, halvings=halvings.get(kind, 0))
        want = steady.daily.get(kind, 0)
        if stage is not None:
            limit = stage.limits.get(kind, 0)
            if stage.comment_every_other_day and kind is Kind.COMMENT and (phase_day - stage.first_day) % 2:
                limit = 0
            cap = min(cap, limit)
            want = min(want, limit)
        week_top = row.week_ceiling(Risk.CAREFUL if risk is Risk.OFF else risk)
        if week_top is not None and row.week_start is not None:
            week_cap = ramped(row.week_start * (2 if risk is Risk.NORMAL else 1), week_top, weeks,
                              halvings=halvings.get(kind, 0))
            left = max(0, week_cap - week_used.get(kind, 0))
            if left < cap:
                cap = left
                if want:
                    notes.append(f"{kind.value}: {left} left of {week_cap} this week")
        caps[kind] = max(0, cap)
        hourly[kind] = row.hourly
        if want:
            targets[kind] = want
            if want > caps[kind] and stage is None:
                notes.append(f"{kind.value}: target {want}, capped at {caps[kind]} today")
    profile = min(max(0, profile_due), caps.get(Kind.PROFILE_EDIT, 0))
    if profile_due and profile < profile_due:
        notes.append(f"profile: {profile_due} fields to change, {profile} today (profile_edit cap)")
    return Budget(phase, phase_day, stage, weeks, targets, caps, hourly, sessions, minutes, notes, profile)


# ---------------------------------------------------------------- correlation audit

def hourly_series(plans: list[DayPlan], start: datetime, hours: int = AUDIT_DAYS * 24) -> list[float]:
    """Actions per UTC hour from `start`, counting each session's actions in the hour it starts."""
    out = [0.0] * hours
    start = start.astimezone(UTC)
    for plan in plans:
        for s in plan.sessions:
            i = int((s.start.astimezone(UTC) - start).total_seconds() // 3600)
            if 0 <= i < hours:
                out[i] += sum(s.actions.values()) or 0.5   # a session with only reads still shows
    return out


def _pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 3:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / math.sqrt(va * vb)


def lagged_correlation(a: list[float], b: list[float], max_lag: int = AUDIT_MAX_LAG_H) -> tuple[float, int]:
    """The highest Pearson correlation of `a` against `b` shifted by -max_lag..max_lag, and its lag
    (positive when `b` does the same thing `lag` hours after `a`)."""
    best, best_lag = 0.0, 0
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            x, y = a[:len(a) - lag], b[lag:]
        else:
            x, y = a[-lag:], b[:lag]
        r = _pearson(list(x), list(y))
        if r > best:
            best, best_lag = r, lag
    return best, best_lag


@dataclass(frozen=True)
class AuditFlag:
    a: str
    b: str
    correlation: float
    lag_hours: int
    same_company: bool

    def to_dict(self) -> dict[str, Any]:
        return {"a": self.a, "b": self.b, "correlation": round(self.correlation, 3), "lag_hours": self.lag_hours,
                "same_company": self.same_company}


def audit(series: dict[str, list[float]], *, threshold: float = AUDIT_THRESHOLD,
          max_lag: int = AUDIT_MAX_LAG_H) -> list[AuditFlag]:
    """Every pair of accounts whose hourly activity correlates at `threshold` or more."""
    flags = []
    names = sorted(series)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            r, lag = lagged_correlation(series[a], series[b], max_lag)
            if r >= threshold:
                flags.append(AuditFlag(a, b, r, lag, a.split("/", 1)[0] == b.split("/", 1)[0]))
    return flags


def ceiling_table(site: str) -> list[dict[str, Any]]:
    """The caps table of one site, for status and the docs."""
    rows = []
    for kind in site_kinds(site):
        row = cap_row(site, kind)
        assert row is not None
        rows.append({"kind": kind.value, "careful_start": row.start, "careful_ceiling": row.careful,
                     "normal_start": start_cap(site, kind, Risk.NORMAL), "normal_ceiling": row.normal,
                     "hard_ceiling": hard_ceiling(site, kind), "hourly": row.hourly,
                     **({"week": {"careful_start": row.week_start, "careful_ceiling": row.week_careful,
                                  "normal_ceiling": row.week_normal}} if row.week_start is not None else {})})
    return rows
