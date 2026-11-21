"""Meters: what each grant and each lane used today, against daily and hourly limits, with a
warning before the cutoff and one plain sentence when it trips (BACKLOG "Per-lane and per-grant
budgets with live meters" and "Cost and action meters that reset daily"; AUD-13, posting pace).

What is counted. Every authorized call debits rows of the policy service's `counters` table, in
the same IMMEDIATE transaction as the grant budgets (PolicyService.reserve), and a call that ends
up waiting for a person or refused is refunded the same way:

  per grant   every block of the caller's chain (a child's use counts against its parents, as
              budgets do): money (micro-dollars), tokens, actions by effect, and commits
  per lane    the lane the call names, else the one lane its grant implies (a block of the chain
              scoped to a single lane id, such as a brain's child grant; PolicyService.implied_lane):
              the same counters. A call that names no lane under a grant reaching several lanes
              (workspace_apply, memory writes, a brain run's own authorize) is grant-metered only

What counts as a commit, per hour and per day:
  - an authorized call whose effect is commit (a post), pay or irreversible, including a flow or
    connector run authorized at its effective effect (openajar.runner.models)
  - a publish an agent's browser_act set off: browser_act is authorized as a draft (what a click
    does is not known before it), and while it runs the lane's egress guard matches every request
    against its commit rules (openajar.node.egress EFFECT_RULES, the canary's verified rules). The
    commit gate (openajar.node.commitgate, AUD-03) holds that request and authorizes its content as
    a commit, which debits it like any commit call (one commit action, refused at a limit before
    anything leaves, refunded while it waits for a person). A publish the gate did not see (a
    redirect hop, a request the route could not hold) is charged by the guard instead: one commit
    on the acting grant's chain and the lane (PolicyService.charge_commit), not a second action;
    past a limit it is counted, because it happened, and holds the lane
A commit refused or waiting for a person is refunded. A tool that fails after it was authorized
keeps its slot: the policy cannot tell whether the outside effect happened. What a person does in
the panel or the live view, in person, is not metered: the meters pace agents.

Spend reported after the fact counts money and tokens, never an action: `ctx.charge`, and a brain
attempt's tokens and metered dollars (PolicyService.charge_spend, called by openajar.brains when an
attempt ends), charged to the task's child grant (else the caller's grant) and its implied lane.
It already happened, so it is never refused; a limit it passed pauses the next call.

Periods. A day is the owner's calendar day in the node's timezone (`[meters] timezone` in
policy.toml, else the host's zone, as GET /api/status shows it): it resets at local midnight. A
DST change moves the UTC instant of midnight, never the local one, so that day has 23 or 25
hours. Where the clocks jump over midnight the day starts at the first local instant after the
jump (01:00); where they fall back across midnight, the repeated hour still belongs to the day
before. The zone the day's first counter was written in is stored (`meter_day`): when the zone
changes (a restart with another setting, the host moved), that day keeps its counters until it
ends, and where the new zone is behind it runs on to the new zone's next midnight, so a zone
change never opens a fresh day. An hour is a clock hour in UTC, so every hour has 60 minutes
whatever the zone does; being clock hours, up to twice an hourly limit can run across the turn of
an hour (the end of one and the start of the next), never more than the daily limit.

Limits (`Limits`, all optional):
  grant  `budget.usd_per_day`, `tokens_per_day`, `commits_per_hour`, `commits_per_day` in a
         grant block; a block's limit holds for it and every grant narrowed from it, and a meter
         shows it under that block (`set_on`, `scope_note`), never as the narrower grant's own use
  lane   the owner's limits for that lane (`PUT /api/meters/lanes/{ws}/{name}`, `openajar grants
         meters --lane`), else the first `[meters.lanes]` pattern in policy.toml matching it

Thresholds. Crossing a `[meters] warn_at` fraction (0.8 by default) sends one warning per limit,
threshold and window (the day, or the hour of an hourly limit): a `meter` trace, a `meter` event
and a push to the node owner and the member the grant belongs to, never another member. Reaching
the limit sends one cutoff notice, and so do a call the limit refuses and a lane limit the owner
sets below today's use. A call that jumps past several thresholds at once sends only the highest.
A notice that could not be traced or published is tried again on the next call.

Cutoff: Denied reason `limit_reached`, checked atomically with the debit and before routing, so an
approval never overrides it:
  money or tokens a day    used up: everything but reads is paused for the grant (or on the
                           lane) until the reset; a call whose stated cost would pass it is refused
  commits an hour or a day reached: commit, pay and irreversible are refused; reads and drafts go on
The message names the limit, its value, what was used and when it resets (`LimitReached.hit`).

When a lane may post at all (its waking hours on the lane's own clock, the gap between posts, posts a
lane-local day) is the posting pace (openajar.policy.pace, AUD-13), checked in the same step as these
limits and refused with its own code, lane_paced.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openajar.core import globs
from openajar.core.models import Effect
from openajar.policy.errors import Denied
from openajar.policy.store import DB
from openajar.policy.util import iso

MICRO = 1_000_000
COMMIT_EFFECTS = frozenset({Effect.COMMIT.value, Effect.PAY.value, Effect.IRREVERSIBLE.value})
LIMIT_NAMES = ("usd_per_day", "tokens_per_day", "commits_per_hour", "commits_per_day")
SPEND_LIMITS = ("usd_per_day", "tokens_per_day")
MAX_THRESHOLDS = 5

# limit -> (counter metric, period, unit)
_LIMIT = {
    "usd_per_day": ("usd", "day", "usd"),
    "tokens_per_day": ("tok", "day", "tokens"),
    "commits_per_hour": ("commit", "hour", "commits"),
    "commits_per_day": ("commit", "day", "commits"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meter_lane_limits (
  lane TEXT PRIMARY KEY, limits TEXT NOT NULL, updated_at REAL NOT NULL, by TEXT);
CREATE TABLE IF NOT EXISTS meter_marks (key TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meter_day (
  id INTEGER PRIMARY KEY CHECK (id = 1), zone TEXT NOT NULL, day TEXT NOT NULL, ends_at REAL NOT NULL);
"""


def zone_name(configured: str | None) -> str:
    """The node's zone: `[meters] timezone`, else the host's (openajar.core.zone, as the panel shows it)."""
    from openajar.core.zone import node_zone

    return node_zone(configured)


class Limits(BaseModel):
    """A lane's limits, or the daily and hourly part of a grant's budget."""
    model_config = ConfigDict(extra="forbid")
    usd_per_day: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tokens_per_day: int | None = Field(default=None, ge=0)
    commits_per_hour: int | None = Field(default=None, ge=0)
    commits_per_day: int | None = Field(default=None, ge=0)

    def given(self) -> dict[str, float | int]:
        return {k: v for k in LIMIT_NAMES if (v := getattr(self, k)) is not None}

    @classmethod
    def of(cls, budget: Any) -> Limits | None:
        """The daily and hourly limits of a grant block's budget, or None when it sets none."""
        if budget is None:
            return None
        out = cls(**{k: getattr(budget, k, None) for k in LIMIT_NAMES})
        return out if out.given() else None


class MeterPolicy(BaseModel):
    """The `[meters]` table of policy.toml."""
    model_config = ConfigDict(extra="forbid")
    timezone: str | None = None
    warn_at: list[float] = Field(default_factory=lambda: [0.8])
    lanes: dict[str, Limits] = Field(default_factory=dict)  # lane id or pattern (biz-a/*) -> limits
    keep_days: int = Field(default=8, ge=2, le=400)          # counters and marks older than this go

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is not None:
            from openajar.core.zone import valid_zone

            if valid_zone(v) is None:
                raise ValueError(f"{v!r} is not a time zone name like Europe/Vilnius or UTC")
        return v

    @field_validator("warn_at")
    @classmethod
    def _warn(cls, v: list[float]) -> list[float]:
        if len(v) > MAX_THRESHOLDS:
            raise ValueError(f"at most {MAX_THRESHOLDS} warning thresholds")
        if any(not (0 < x < 1) or not math.isfinite(x) for x in v):
            raise ValueError("warning thresholds are fractions between 0 and 1, like 0.8")
        return sorted(set(v))

    @field_validator("lanes")
    @classmethod
    def _lanes(cls, v: dict[str, Limits]) -> dict[str, Limits]:
        for pattern in v:
            globs.validate_lane_pattern(pattern)
        return v


@dataclass(frozen=True)
class Scope:
    kind: str     # grant | lane
    id: str       # a grant block id, or a lane id
    label: str = ""

    @property
    def key(self) -> str:
        return f"{'g' if self.kind == 'grant' else 'l'}:{self.id}"


class LimitReached(Denied):
    """A meter's hard cutoff. `hit`: code, scope, id, limit, value, used, unit, period, resets_at, explain.
    `scope` and `units` (what was used, in counter units) let the policy send the cutoff notice."""

    def __init__(self, hit: dict, *, scope: Scope | None = None, units: int = 0):
        super().__init__("limit_reached", hit["explain"])
        self.hit = hit
        self.scope = scope
        self.units = units


def _micros_down(usd: float) -> int:
    return int((Decimal(repr(usd)) * MICRO).to_integral_value(rounding=ROUND_FLOOR))


def _units(name: str, value: float) -> int:
    return _micros_down(float(value)) if name == "usd_per_day" else int(value)


def _amount(name: str, used: int) -> float | int:
    return round(used / MICRO, 6) if name == "usd_per_day" else used


def _words(name: str, value: float) -> str:
    """"3 commits a day", "$2.00 a day", "50,000 tokens a day"."""
    if name == "usd_per_day":
        return f"${float(value):,.2f} a day"
    if name == "tokens_per_day":
        return f"{int(value):,} tokens a day"
    n = int(value)
    return f"{n} {'commit' if n == 1 else 'commits'} {'an hour' if name == 'commits_per_hour' else 'a day'}"


def _used_words(name: str, used: int) -> str:
    if name == "usd_per_day":
        return f"${used / MICRO:,.2f} used today"
    if name == "tokens_per_day":
        return f"{used:,} used today"
    return f"{used} used {'this hour' if name == 'commits_per_hour' else 'today'}"


def _until(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if h and m else f"{h} h" if h else f"{m} min"


class Meters:
    """The counters, limits, periods and notices. The policy service owns one; its `db` is
    policy.db, so counters share the budgets' table and transaction."""

    def __init__(self, db: DB, *, cfg: MeterPolicy, clock: Callable[[], float]):
        self.db = db
        self.cfg = cfg
        self.clock = clock
        self.timezone = zone_name(cfg.timezone)
        self.zone = ZoneInfo(self.timezone)
        with db.lock:
            db.conn.executescript(SCHEMA)
        self._held = self._held_day()
        self._recorded: str | None = None

    def _held_day(self) -> tuple[date, float] | None:
        """The day the counters were last written in, when the zone changed since (a restart with
        another `[meters] timezone`, or the host's zone moved): that day keeps its key until it
        ends, so a zone change never opens a fresh day. Where the new zone is behind, the day runs
        on to the new zone's next midnight after it, so its date never starts over."""
        row = self.db.one("SELECT zone, day, ends_at FROM meter_day WHERE id = 1")
        if row is None or row["zone"] == self.timezone or self.clock() >= row["ends_at"]:
            return None
        held, end = date.fromisoformat(row["day"]), float(row["ends_at"])
        for _ in range(3):
            if datetime.fromtimestamp(end, self.zone).date() > held:
                break
            end = self._next_midnight(end)
        return held, end

    # ---- periods ----
    def day(self, t: float) -> date:
        if self._held is not None and t < self._held[1]:
            return self._held[0]
        return datetime.fromtimestamp(t, self.zone).date()

    def day_start(self, d: date) -> float:
        # fold=0 on a midnight the clocks jump over is read with the offset before the jump, which
        # is the first instant of the new day (01:00 local)
        return datetime.combine(d, time(0), tzinfo=self.zone).timestamp()

    def day_reset(self, t: float) -> float:
        """The next local midnight after `t` (the end of a held day, see _held_day)."""
        if self._held is not None and t < self._held[1]:
            return self._held[1]
        return self._next_midnight(t)

    def _next_midnight(self, t: float) -> float:
        d = datetime.fromtimestamp(t, self.zone).date()
        for k in range(1, 4):
            at = self.day_start(d + timedelta(days=k))
            if at > t:
                return at
        raise AssertionError("unreachable: a local midnight comes round within three days")

    @staticmethod
    def hour_reset(t: float) -> float:
        return (math.floor(t / 3600) + 1) * 3600.0

    def bucket(self, period: str, t: float) -> str:
        if period == "hour":
            return "h" + datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H")
        return "d" + self.day(t).isoformat()

    def reset_at(self, period: str, t: float) -> float:
        return self.hour_reset(t) if period == "hour" else self.day_reset(t)

    def local_time(self, t: float) -> str:
        return datetime.fromtimestamp(t, self.zone).strftime("%H:%M")

    @staticmethod
    def key(scope: Scope, metric: str, bucket: str) -> str:
        return f"meter:{scope.key}:{metric}:{bucket}"

    # ---- limits ----
    def lane_limits(self, lane_id: str) -> tuple[Limits | None, str | None]:
        """The lane's limits and where they come from ("owner" or the policy.toml pattern)."""
        row = self.db.one("SELECT limits FROM meter_lane_limits WHERE lane = ?", (lane_id,))
        if row is not None:
            lim = Limits.model_validate(json.loads(row["limits"]))
            return (lim if lim.given() else None), "owner"
        for pattern, lim in self.cfg.lanes.items():
            if globs.lane_match(pattern, lane_id):
                return (lim if lim.given() else None), f"policy.toml {pattern}"
        return None, None

    def set_lane_limits(self, lane_id: str, limits: Limits | None, *, by: str) -> None:
        """The owner's limits for one lane; None removes them (policy.toml applies again). An empty
        Limits stores "no limits", which also overrides a policy.toml pattern."""
        if limits is None:
            self.db.execute("DELETE FROM meter_lane_limits WHERE lane = ?", (lane_id,))
            return
        self.db.execute("INSERT INTO meter_lane_limits (lane, limits, updated_at, by) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(lane) DO UPDATE SET limits = excluded.limits, updated_at = excluded.updated_at, "
                        "by = excluded.by", (lane_id, limits.model_dump_json(exclude_none=True), self.clock(), by))

    def limited_lanes(self) -> list[str]:
        return [r["lane"] for r in self.db.all("SELECT lane FROM meter_lane_limits ORDER BY lane")]

    # ---- debits ----
    def entries(self, scopes: Iterable[Scope], effect: str | None, tokens: int, usd_m: int,
                now: float, *, action: bool = True) -> list[tuple[str, int]]:
        """The counter rows one call adds to: (key, amount). `action` False: a commit that is not an
        action of its own (a publish the egress guard saw during an act already counted as a draft)."""
        day, hour = self.bucket("day", now), self.bucket("hour", now)
        out: list[tuple[str, int]] = []
        for s in scopes:
            if effect is not None:
                if action:
                    out.append((self.key(s, f"act.{effect}", day), 1))
                if effect in COMMIT_EFFECTS:
                    out += [(self.key(s, "commit", day), 1), (self.key(s, "commit", hour), 1)]
            if tokens:
                out.append((self.key(s, "tok", day), tokens))
            if usd_m:
                out.append((self.key(s, "usd", day), usd_m))
        if out:
            self._record_day(now)
        return out

    def _record_day(self, now: float) -> None:
        """Remember the zone the day's first counter was written in (see _held_day). A held day is
        already the stored one, so it is never rewritten."""
        day = self.day(now).isoformat()
        if self._recorded == day:
            return
        self.db.execute("INSERT INTO meter_day (id, zone, day, ends_at) VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE "
                        "SET zone = excluded.zone, day = excluded.day, ends_at = excluded.ends_at "
                        "WHERE meter_day.day <> excluded.day", (self.timezone, day, self.day_reset(now)))
        self._recorded = day

    def check(self, used: Callable[[str], int], limited: Iterable[tuple[Scope, Limits]], effect: str | None,
              tokens: int, usd_m: int, now: float) -> None:
        """Raise LimitReached when this call may not run under a limit. `used(key)` reads inside the
        caller's transaction, so the check and the debit are one step."""
        for scope, lim in limited:
            for name, value in lim.given().items():
                metric, period, _ = _LIMIT[name]
                limit = _units(name, value)
                if metric == "commit":
                    if effect not in COMMIT_EFFECTS:
                        continue
                    amount = 1
                else:
                    amount = usd_m if metric == "usd" else tokens
                u = used(self.key(scope, metric, self.bucket(period, now)))
                paused = metric != "commit" and effect not in (None, Effect.READ.value) and u >= limit
                if (amount and u + amount > limit) or paused:
                    # a commit past the limit is "reached"; a stated cost that would pass it is named
                    cost = amount if metric != "commit" and not paused and u < limit else 0
                    raise LimitReached(self.hit(scope, name, value, u, now, cost=cost), scope=scope, units=u)

    def hit(self, scope: Scope, name: str, value: float, used: int, now: float, *, cost: int = 0) -> dict:
        _, period, unit = _LIMIT[name]
        reset = self.reset_at(period, now)
        return {"code": f"{scope.kind}.{name}", "scope": scope.kind, "id": scope.id, "limit": name,
                "value": value, "used": _amount(name, used), "unit": unit, "period": period,
                "resets_at": iso(reset), "explain": self.sentence(scope, name, value, used, now, cost=cost)}

    def sentence(self, scope: Scope, name: str, value: float, used: int, now: float, *, cost: int = 0) -> str:
        """What tripped, in one plain sentence: which limit, its value, what was used, when it resets."""
        _, period, _ = _LIMIT[name]
        reset = self.reset_at(period, now)
        who = f"Browser {scope.id}" if scope.kind == "lane" else f"Grant {scope.label or scope.id}"
        when = f"{self.local_time(reset)} ({self.timezone}), in {_until(reset - now)}"
        if cost:
            spent = f"${cost / MICRO:,.2f}" if name == "usd_per_day" else f"{cost:,} tokens"
            return (f"This call's cost ({spent}) would pass the limit of {_words(name, value)} on "
                    f"{who.lower() if scope.kind == 'lane' else who} ({_used_words(name, used)}). "
                    f"It resets at {when}.")
        what = ("so posting, paying and irreversible steps are paused" if _LIMIT[name][0] == "commit"
                else "so everything but reads is paused")
        return (f"{who} reached its limit of {_words(name, value)} ({_used_words(name, used)}), {what} "
                f"until it resets at {when}.")

    # ---- what the panel and grant_info show ----
    def used(self, keys: list[str]) -> dict[str, int]:
        if not keys:
            return {}
        rows = self.db.all(f"SELECT key, used FROM counters WHERE key IN ({','.join('?' * len(keys))})", keys)
        return {r["key"]: int(r["used"]) for r in rows}

    def state(self, scope: Scope, limited: list[tuple[Scope, Limits]], *, source: str | None = None,
              now: float | None = None) -> dict:
        """One meter: today's use of `scope` and every limit over it (`limited`: the scope itself,
        or each block of a grant's chain that sets one). Each limit is shown under the block that
        set it: a limit set on a parent block (`set_on`) counts what every grant narrowed from that
        block used, so its `used` is the block's, its `scope_note` says so, and its sentence names
        that block, never this grant."""
        now = self.clock() if now is None else now
        day, hour = self.bucket("day", now), self.bucket("hour", now)
        effects = [e.value for e in Effect]
        keys = [self.key(scope, m, day) for m in ("usd", "tok", "commit")] + [self.key(scope, "commit", hour)]
        keys += [self.key(scope, f"act.{e}", day) for e in effects]
        rows: list[tuple[Scope, str, float | int, str]] = []  # (scope, name, value, key)
        for s, lim in limited:
            for name, value in lim.given().items():
                metric, period, _ = _LIMIT[name]
                rows.append((s, name, value, self.key(s, metric, self.bucket(period, now))))
        got = self.used(keys + [k for *_, k in rows])
        actions = {e: got[k] for e in effects if (k := self.key(scope, f"act.{e}", day)) in got}
        usage = {"usd": got.get(keys[0], 0) / MICRO, "tokens": got.get(keys[1], 0),
                 "commits_today": got.get(keys[2], 0), "commits_this_hour": got.get(keys[3], 0),
                 "actions": actions, "actions_today": sum(actions.values())}
        limits = []
        order = {name: i for i, name in enumerate(LIMIT_NAMES)}
        rows.sort(key=lambda r: (r[0].key != scope.key, order[r[1]]))  # its own limits first
        for s, name, value, k in rows:
            limit = _units(name, value)
            u = got.get(k, 0)
            own = s.key == scope.key
            item = {"name": name, "value": value, "used": _amount(name, u), "left": _amount(name, max(0, limit - u)),
                    "fraction": round(u / limit, 4) if limit else 1.0, "unit": _LIMIT[name][2],
                    "period": _LIMIT[name][1], "resets_at": iso(self.reset_at(_LIMIT[name][1], now)),
                    "set_on": None if own else s.id,
                    "scope_note": None if own else (
                        f"Set on grant {s.id}, which this grant was narrowed from: it counts what every grant "
                        f"narrowed from {s.id} used, not only this one.")}
            item["state"] = "cut_off" if u >= limit else (
                "warning" if self.cfg.warn_at and item["fraction"] >= self.cfg.warn_at[0] else "ok")
            who = s if own else Scope(s.kind, s.id, f"{s.id} (which this grant was narrowed from)")
            item["explain"] = (self.sentence(who, name, value, u, now) if item["state"] == "cut_off" else
                               self.warning_sentence(who, name, value, u, now) if item["state"] == "warning"
                               else None)
            limits.append(item)
        worst = next((st for st in ("cut_off", "warning") if any(i["state"] == st for i in limits)), "ok")
        return {"scope": scope.kind, "id": scope.id, "label": scope.label or scope.id, "state": worst,
                "explain": next((i["explain"] for i in limits if i["state"] == worst), None),
                "usage": usage, "limits": limits, "limits_from": source if scope.kind == "lane" else None,
                "day": self.day(now).isoformat(), "timezone": self.timezone, "warn_at": list(self.cfg.warn_at),
                "resets_at": iso(self.day_reset(now)), "hour_resets_at": iso(self.hour_reset(now))}

    def warning_sentence(self, scope: Scope, name: str, value: float, used: int, now: float) -> str:
        _, period, _ = _LIMIT[name]
        reset = self.reset_at(period, now)
        limit = _units(name, value)
        pct = math.floor(100 * used / limit) if limit else 100
        who = f"Browser {scope.id}" if scope.kind == "lane" else f"Grant {scope.label or scope.id}"
        return (f"{who} used {pct}% of its limit of {_words(name, value)} ({_used_words(name, used)}). "
                f"It resets at {self.local_time(reset)} ({self.timezone}).")

    # ---- thresholds ----
    def mark_key(self, scope: Scope, name: str, threshold: float | str, now: float) -> str:
        """One notice's mark: per limit, threshold and window (the day, or the hour of an hourly limit)."""
        return f"{scope.key}:{name}:{threshold}:{self.bucket(_LIMIT[name][1], now)}"

    def _claim(self, key: str, now: float) -> bool:
        return bool(self.db.execute("INSERT OR IGNORE INTO meter_marks (key, at) VALUES (?, ?)", (key, now)).rowcount)

    def release(self, keys: Iterable[str]) -> None:
        """A notice that could not be sent: its marks go, so the next call sends it again."""
        for key in keys:
            self.db.execute("DELETE FROM meter_marks WHERE key = ?", (key,))

    def crossed(self, limited: Iterable[tuple[Scope, Limits]], now: float) -> list[dict]:
        """The thresholds crossed for the first time in their window (the day, or the hour of an
        hourly limit), at most one notice per limit (the highest). Each notice's `marks` are claimed
        here; the sender releases them when it could not send (Meters.release), so a notice is
        sent once per threshold and window, and a failed one is tried again on the next call."""
        out = []
        for scope, lim in limited:
            given = lim.given()
            if not given:
                continue
            keys = {name: self.key(scope, _LIMIT[name][0], self.bucket(_LIMIT[name][1], now)) for name in given}
            got = self.used(list(keys.values()))
            for name, value in given.items():
                limit = _units(name, value)
                if limit <= 0:  # a zero limit is the owner saying "none of this today": nothing to warn about
                    continue
                u = got.get(keys[name], 0)
                fresh: list[tuple[float, str]] = []
                for t in [*self.cfg.warn_at, 1.0]:
                    if u < t * limit:
                        break
                    mark = self.mark_key(scope, name, t, now)
                    if self._claim(mark, now):
                        fresh.append((t, mark))
                if not fresh:
                    continue
                t = fresh[-1][0]
                hit = self.hit(scope, name, value, u, now)
                if t < 1.0:
                    hit["explain"] = self.warning_sentence(scope, name, value, u, now)
                out.append({**hit, "kind": "cutoff" if t >= 1.0 else "warning", "threshold": t,
                            "label": scope.label or scope.id, "at": iso(now), "marks": [m for _, m in fresh]})
        return out

    def refused(self, e: LimitReached, now: float) -> dict | None:
        """The cutoff notice for a call a limit refused, once per limit and window: under the limit's
        own cutoff mark when it is reached, else (a stated cost that would pass it) under a mark of
        its own. None when it was sent already."""
        if e.scope is None:
            return None
        name = e.hit["limit"]
        reached = e.units >= _units(name, e.hit["value"])
        mark = self.mark_key(e.scope, name, 1.0 if reached else "refused", now)
        if not self._claim(mark, now):
            return None
        return {**e.hit, "kind": "cutoff", "threshold": 1.0, "label": e.scope.label or e.scope.id, "at": iso(now),
                "marks": [mark]}

    def purge(self) -> int:
        cutoff = self.clock() - self.cfg.keep_days * 86400
        self.db.execute("DELETE FROM meter_marks WHERE at < ?", (cutoff,))
        return self.db.execute("DELETE FROM counters WHERE key LIKE 'meter:%' AND updated_at < ?", (cutoff,)).rowcount
