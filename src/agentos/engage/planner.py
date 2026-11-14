"""The planner: one account's day, drawn from its persona (engagement-engine.md 5.5).

    plan = draw_day(program, persona, budget, day, tz)
    plans = plan_days(accounts, start, days)       # several accounts, one company rule

For each day, in the lane's time zone:

1. Quiet day? With the persona's rate (0.08 to 0.2), the account does nothing but reply to
   comments on its own posts. Never more than 2 quiet days in a row.
2. Volume per kind: the program's target times a log-normal draw around 1 (sigma 0.3), clipped
   to the governor's cap. "15 likes" is 9 one day and 19 another, never above the cap. While the
   cap is below the target (early in the ramp), the draw centers on 80 percent of the cap.
3. Session starts: 2 to 4 (fewer in the first warm-up days) from a non-homogeneous Poisson
   process whose rate is the persona's daily curve. Given how many starts a day has, such a
   process places them independently with density proportional to the rate, so each start is
   drawn by thinning against the curve, then kept only if it is at least 45 minutes from the
   day's other starts, not on a round minute (:00, :05, ...), not within 10 minutes of a start
   of yesterday's, and at least 20 minutes from any start of another account of the same company.
4. Split: actions go to sessions by a Dirichlet draw per kind, so one session may hold most of
   the day's likes. A session never holds more than a kind's hourly cap, nor do sessions close
   enough to share an hour together; what does not fit is dropped, not moved to another day.
5. Each session gets a length (log-normal, median 9 minutes times the persona's scale, 3 to 30)
   and a count of posts it scrolls past or opens, so that most posts seen get no action.
6. Profile edits (brief B2): when the program's `profile:` differs from what the account last
   showed, the budget allows up to the day's profile_edit cap of them. They are not drawn: the
   first session takes them, one more session at a time where the hourly cap does not fit them
   (in session order, no random draw, so a day without them is drawn exactly as before).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from agentos.engage.caps import ACTS, Kind
from agentos.engage.governor import COMPANY_GAP, Budget, budget
from agentos.engage.models import DayPlan, Program, Session
from agentos.engage.persona import Persona, rng, week_of

MIN_GAP = timedelta(minutes=45)
YESTERDAY_GAP_S = 10 * 60
VOLUME_SIGMA = 0.3
CLIPPED_CENTER = 0.8
LENGTH_MEDIAN_MIN = 9.0
LENGTH_SIGMA = 0.45
LENGTH_RANGE = (3.0, 30.0)
DIRICHLET_ALPHA = 0.7
MAX_QUIET_RUN = 2
ITEMS_PER_MINUTE = 3.0
MAX_ACT_RATIO = 0.35          # a session that acts on more of what it sees is refused
START_TRIES = 4000


def _round_minute(t: datetime) -> bool:
    return t.minute % 5 == 0


def _draw_starts(persona: Persona, day: date, tz: ZoneInfo, n: int, r, *, yesterday: Iterable[datetime],
                 avoid: Iterable[datetime]) -> list[datetime]:
    lo, hi = persona.window(day)
    peak = max(persona.intensity(day, lo + (hi - lo) * i / 200) for i in range(201)) or 1.0
    y_secs = [t.hour * 3600 + t.minute * 60 + t.second for t in yesterday]
    avoid = sorted(avoid)
    midnight = datetime.combine(day, time(0), tzinfo=tz)
    starts: list[datetime] = []
    tries = 0
    while len(starts) < n and tries < START_TRIES:
        tries += 1
        hour = r.uniform(lo, hi)
        if r.random() * peak > persona.intensity(day, hour):
            continue                                        # thinning against the daily curve
        t = midnight + timedelta(seconds=int(hour * 3600))
        if _round_minute(t):
            continue
        if any(abs(t - s) < MIN_GAP for s in starts):
            continue
        secs = t.hour * 3600 + t.minute * 60 + t.second
        if any(abs(secs - y) < YESTERDAY_GAP_S for y in y_secs):
            continue
        if any(abs(t - a) < COMPANY_GAP for a in avoid):
            continue
        starts.append(t)
    return sorted(starts)


def _dirichlet(r, k: int, alpha: float = DIRICHLET_ALPHA) -> list[float]:
    g = [r.gammavariate(alpha, 1.0) for _ in range(k)]
    total = sum(g) or 1.0
    return [x / total for x in g]


def _groups(sessions: list[Session]) -> list[tuple[int, int]]:
    """Every run of sessions i..j (i < j) close enough that one rolling hour can hold actions
    of all of them: the last starts less than an hour after the first ends."""
    out = []
    for i in range(len(sessions)):
        for j in range(i + 1, len(sessions)):
            if (sessions[j].start - sessions[i].end) < timedelta(hours=1):
                out.append((i, j))
    return out


def _fits(counts: list[int], groups: list[tuple[int, int]], limit: int) -> bool:
    return all(c <= limit for c in counts) and all(sum(counts[i:j + 1]) <= limit for i, j in groups)


def _split(r, total: int, sessions: list[Session], hourly: int) -> tuple[list[int], int]:
    """Spread `total` over the sessions unevenly; returns (counts, dropped)."""
    k = len(sessions)
    weights = _dirichlet(r, k)
    counts = [0] * k
    groups = _groups(sessions)
    dropped = 0
    for _ in range(total):
        order = sorted(range(k), key=lambda i: -weights[i] * r.random())
        for i in order:
            counts[i] += 1
            if _fits(counts, groups, hourly):
                break
            counts[i] -= 1
        else:
            dropped += 1
    return counts, dropped


def draw_day(program: Program, persona: Persona, plan_budget: Budget, day: date, tz: ZoneInfo, *,
             yesterday: Iterable[datetime] = (), avoid: Iterable[datetime] = (),
             quiet_run: int = 0) -> DayPlan:
    p = persona.for_day(day)
    r = rng(p.seed, p.redraws, "day", day.isoformat())
    plan = DayPlan(surface=program.surface, day=day, timezone=tz.key, program=program.id,
                   program_hash=program.content_hash(), phase=plan_budget.phase, phase_day=plan_budget.phase_day,
                   stage=plan_budget.stage.name if plan_budget.stage else None, ramp_weeks=plan_budget.ramp_weeks,
                   targets=dict(plan_budget.targets), caps=dict(plan_budget.caps), hourly=dict(plan_budget.hourly),
                   notes=list(plan_budget.notes))
    quiet_roll = r.random()
    if quiet_run < MAX_QUIET_RUN and quiet_roll < p.quiet_p:
        plan.quiet = True
        plan.notes.append("quiet day: only replies to comments on own posts")
        return plan

    volumes: dict[Kind, int] = {}
    for kind in sorted(plan_budget.targets):
        draw = r.lognormvariate(0.0, VOLUME_SIGMA)
        cap = plan_budget.caps.get(kind, 0)
        # a target above the cap is drawn around 80 percent of the cap, so the day is not the
        # cap exactly, day after day (the regularity a detector looks for)
        want = min(plan_budget.targets[kind], CLIPPED_CENTER * cap)
        volumes[kind] = max(0, min(cap, round(want * draw)))

    lo, hi = plan_budget.sessions
    n = max(lo, min(hi, p.sessions + r.choice((-1, 0, 0, 1))))
    starts = _draw_starts(p, day, tz, n, r, yesterday=yesterday, avoid=avoid)
    if len(starts) < n:
        plan.notes.append(f"{len(starts)} of {n} sessions fit the waking hours and the gaps")
    if not starts:
        return plan

    sessions: list[Session] = []
    for t in starts:
        if plan_budget.minutes is not None:
            minutes = r.uniform(*plan_budget.minutes)
        else:
            minutes = r.lognormvariate(math.log(LENGTH_MEDIAN_MIN * p.length_scale), LENGTH_SIGMA)
            minutes = max(LENGTH_RANGE[0], min(LENGTH_RANGE[1], minutes))
        sessions.append(Session(start=t, minutes=round(minutes, 1)))

    for kind, total in volumes.items():
        if not total:
            continue
        counts, dropped = _split(r, total, sessions, plan_budget.hourly.get(kind, total))
        for s, c in zip(sessions, counts, strict=True):
            if c:
                s.actions[kind] = c
        if dropped:
            plan.notes.append(f"{kind.value}: {dropped} dropped to stay under {plan_budget.hourly[kind]} an hour")

    if plan_budget.profile:
        _place_profile(plan, sessions, plan_budget.profile, plan_budget.hourly.get(Kind.PROFILE_EDIT, 1))

    for s in sessions:
        s.actions = dict(sorted(s.actions.items()))
        acts = sum(n for k, n in s.actions.items() if k in ACTS)
        browse = s.minutes * ITEMS_PER_MINUTE * p.scroll_speed
        s.seen = max(round(browse), math.ceil(acts / p.liker) if acts else 0)
        assert not s.seen or acts / s.seen <= MAX_ACT_RATIO, "a session must not act on most of what it sees"
    plan.sessions = sessions
    return plan


def _place_profile(plan: DayPlan, sessions: list[Session], total: int, hourly: int) -> None:
    """Profile edits in session order, deterministic: the first session first, each under the
    hourly cap (sessions close enough to share an hour count together)."""
    counts = [0] * len(sessions)
    groups = _groups(sessions)
    left = total
    for i in range(len(sessions)):
        while left and _fits([c + (1 if j == i else 0) for j, c in enumerate(counts)], groups, hourly):
            counts[i] += 1
            left -= 1
    for s, c in zip(sessions, counts, strict=True):
        if c:
            s.actions[Kind.PROFILE_EDIT] = c
    if left:
        plan.notes.append(f"profile_edit: {left} left for another day, to stay under {hourly} an hour")


# ---------------------------------------------------------------- several days, several accounts

@dataclass
class Account:
    program: Program
    persona: Persona
    tz: ZoneInfo
    starts_on: date
    overrides: dict[Kind, int] = field(default_factory=dict)
    warnings: list[date] = field(default_factory=list)
    halvings: dict[Kind, int] = field(default_factory=dict)
    profile_due: int = 0          # profile fields that differ from the program's (agentos.engage.profile)

    @property
    def surface(self) -> str:
        return self.program.surface


def plan_days(accounts: list[Account], start: date, days: int, *,
              history: dict[str, list[DayPlan]] | None = None) -> dict[str, list[DayPlan]]:
    """Plans for `days` local days from `start`, for every account. Accounts are drawn in surface
    order each day, and each start is kept 20 minutes from every start of its company drawn
    before it (in any time zone). `history` holds earlier plans (yesterday's starts, quiet runs,
    weekly counts); it is read, never changed."""
    history = history or {}
    out: dict[str, list[DayPlan]] = {a.surface: [] for a in accounts}
    company: dict[str, list[datetime]] = {}
    for a in accounts:
        for plan in history.get(a.surface, []):
            company.setdefault(a.program.workspace, []).extend(s.start for s in plan.sessions)
    for offset in range(days):
        for a in sorted(accounts, key=lambda x: x.surface):
            day = start + timedelta(days=offset)
            past = [*history.get(a.surface, []), *out[a.surface]]
            prev = next((p for p in reversed(past) if p.day == day - timedelta(days=1)), None)
            quiet_run = 0
            for p in reversed(past):
                if p.day >= day or not p.quiet:
                    break
                quiet_run += 1
            monday = week_of(day)
            week_used: dict[Kind, int] = {}
            for p in past:
                if monday <= p.day < day:
                    for k, n in p.totals().items():
                        week_used[k] = week_used.get(k, 0) + n
            planned = sum(p.totals().get(Kind.PROFILE_EDIT, 0) for p in out[a.surface])
            b = budget(a.program, a.starts_on, day, overrides=a.overrides, warnings=a.warnings,
                       halvings=a.halvings, week_used=week_used, profile_due=max(0, a.profile_due - planned))
            ws = a.program.workspace
            others = [t for t in company.get(ws, []) if abs((t.date() - day).days) <= 1]
            plan = draw_day(a.program, a.persona, b, day, a.tz,
                            yesterday=[s.start for s in prev.sessions] if prev else (),
                            avoid=others, quiet_run=quiet_run)
            out[a.surface].append(plan)
            company.setdefault(ws, []).extend(s.start for s in plan.sessions)
    return out
