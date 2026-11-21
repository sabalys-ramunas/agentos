"""Posting pace and waking hours per lane (AUD-13): how often, how close together and at what
local hours an agent may publish through one lane. The meters (openajar.policy.meters) count what
was used; the pace says when the next publish may go, in the lane's own time.

What the owner sets per lane (`Pace`, all optional, off until set):
  commits_per_day   publishes a lane day (midnight to midnight in the lane's zone; a waking window
                    that runs over midnight counts whole toward the day it starts on, so with
                    "22:00-02:00" and 1 a day, 22:30 and 00:05 are the same day). At least 1: to
                    stop a lane posting, pause it
  min_gap_minutes   at least this long between two publishes, before or after: a run queued for
                    10:00 holds a publish at 09:50 as much as one made at 09:50 holds the run
  active_hours      the lane's waking hours: "08:00-22:00", "mon-fri 08:00-22:00",
                    "sat,sun 10:00-23:30". A window that ends at or before it starts runs over
                    midnight ("22:00-02:00": 22:00 to 02:00 the next morning, its day being the
                    day it starts). "24:00" ends a window at midnight. Several windows join.
  timezone          an IANA zone that overrides where the lane's hours are read

A lane with no pace is never paced: nothing changes on deploy until the owner sets one. PRESET is
what the panel and `openajar lane pace <lane> --preset` apply: 08:00-22:00 lane time, 45 minutes
apart, 3 a day (a new account's pace).

Whose clock (`zone_for`, the `source` every surface shows):
  lane       the owner's `timezone` on the lane's pace
  pin        the lane's pinned fingerprint timezone, from its proxy's exit region when it was
             pinned: what every site the lane visits sees, so a lane on a US proxy wakes on US time
  workspace  the owner's default for the lane's workspace (a lane with no pin yet)
  node       the node's zone (`[meters] timezone`, else the host's)
When the lane check last saw the exit in another zone than the pin (the proxy moved), the pin
still decides, because that is the zone the site sees; `exit_timezone` and `note` say so.

When a publish is refused (`LanePaced`, code lane_paced): outside the waking hours, within
min_gap_minutes of another publish (made or queued), or with commits_per_day used up that day. The message names the
rule and when the lane may next post, in lane time and in UTC (`next_allowed`). The check runs in
PolicyService.reserve, before approval routing, so an approval never overrides it, for every
commit, pay or irreversible call that names a lane: a publish the commit gate holds during an
agent's browser_act, a flow or connector run (agent, schedule, API), and a typed tool.

Who is paced: a person driving the lane by hand is never paced; runs and agents are, including runs
the owner starts. A person in control of the lane, or the owner in person (the owner token in the
panel), publishes without a check. A member is a person but not the owner, so a member's publish is
paced, in person or not, and so is every grant. An approval never exempts anything: a run the owner
approves goes when the pace allows, and the owner's own runs (`openajar flows run`, Run in the panel)
are runs like any other.

What is recorded: each publish that goes on a paced or unpaced lane is one row of `pace_commits`
(lane, time), written in the same transaction as the meters' debit and given back with it when the
call waits for a person or is refused (Reservation.pace_ids). A queued run records the time it is
queued for (`at`, a flow_run's not_before) and keeps the row's id (Run.pace_id). A run with no row
(one the owner started, or one a person approved after it parked) is checked and recorded in one
transaction right before it runs (PolicyService.pace_claim). A person's own publish on the lane
(by hand, in control) is recorded without a check, so an agent's post right after it waits for
the gap. Rows older than KEEP_DAYS go.

If the lane's pinned fingerprint cannot be read, the last zone read for it is kept; with none, a
paced publish is refused rather than read on another clock.

Keepalive visits honor the waking hours too (openajar.keepalive.service): a session active at 3 am
local time is a signal of its own, so a visit is moved into the lane's active_hours when they are set.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openajar.core.errors import OpenajarError
from openajar.policy.errors import Denied
from openajar.policy.meters import LimitReached
from openajar.policy.store import DB
from openajar.policy.util import iso

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_WINDOWS = 14
MAX_GAP_MINUTES = 7 * 24 * 60
KEEP_DAYS = 8
LOOKAHEAD_DAYS = 9
JITTER_S = (30.0, 240.0)   # a moved slot goes this long after the lane may post again
SOURCES = ("lane", "pin", "workspace", "node")
SOURCE_WORDS = {"lane": "set on the lane", "pin": "from its proxy's region (the pinned fingerprint)",
                "workspace": "the workspace default", "node": "the node's zone"}

_WINDOW_RE = re.compile(r"^(?:(?P<days>[a-z,\-]+)\s+)?(?P<start>\d{1,2}:\d{2})\s*-\s*(?P<end>\d{1,2}:\d{2})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pace_lanes (
  lane TEXT PRIMARY KEY, pace TEXT NOT NULL, updated_at REAL NOT NULL, by TEXT);
CREATE TABLE IF NOT EXISTS pace_workspaces (
  workspace TEXT PRIMARY KEY, timezone TEXT NOT NULL, updated_at REAL NOT NULL, by TEXT);
CREATE TABLE IF NOT EXISTS pace_commits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lane TEXT NOT NULL, at REAL NOT NULL, recorded_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS pace_commits_lane_at ON pace_commits (lane, at);
"""


# ---- windows ---------------------------------------------------------------------------------

def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    hours, minutes = int(h), int(m)
    if minutes > 59 or hours > 24 or (hours == 24 and minutes):
        raise ValueError(f"{hhmm} is not a time of day (00:00 to 24:00)")
    return hours * 60 + minutes


def _days(spec: str | None) -> frozenset[int]:
    if not spec:
        return frozenset(range(7))
    out: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            if a not in WEEKDAYS or b not in WEEKDAYS:
                raise ValueError(f"{part!r}: days are mon, tue, wed, thu, fri, sat, sun")
            i, j = WEEKDAYS.index(a), WEEKDAYS.index(b)
            out.update((i + k) % 7 for k in range((j - i) % 7 + 1))
        elif part in WEEKDAYS:
            out.add(WEEKDAYS.index(part))
        else:
            raise ValueError(f"{part!r}: days are mon, tue, wed, thu, fri, sat, sun")
    return frozenset(out)


@dataclass(frozen=True)
class Window:
    days: frozenset[int]   # the weekdays it starts on (0 Monday)
    start: int             # minutes after local midnight
    end: int               # minutes after local midnight of the start day, or of the next day (overnight)

    @property
    def overnight(self) -> bool:
        return self.end <= self.start

    @classmethod
    def parse(cls, text: str) -> Window:
        m = _WINDOW_RE.match(" ".join(str(text).strip().lower().split()))
        if m is None:
            raise ValueError(f"{text!r} is not a window like 08:00-22:00 or mon-fri 09:00-18:00")
        start, end = _minutes(m["start"]), _minutes(m["end"])
        if start == 24 * 60:
            raise ValueError(f"{text!r}: a window starts before 24:00")
        if start == end:
            raise ValueError(f"{text!r}: a window must not start and end at the same time")
        return cls(_days(m["days"]), start, end)

    def text(self) -> str:
        def hhmm(x: int) -> str:
            return f"{x // 60:02d}:{x % 60:02d}"
        return f"{_days_text(self.days)}{hhmm(self.start)}-{hhmm(self.end)}"


def _days_text(days: frozenset[int]) -> str:
    if len(days) == 7:
        return ""
    s = sorted(days)
    if s == list(range(s[0], s[-1] + 1)) and len(s) > 2:
        return f"{WEEKDAYS[s[0]]}-{WEEKDAYS[s[-1]]} "
    return ",".join(WEEKDAYS[d] for d in s) + " "


def local_instant(day: date, minutes: int, tz: ZoneInfo) -> float:
    """`minutes` after local midnight of `day`, as a UTC timestamp. A wall time the clocks skip
    (spring forward) comes out at the same distance after the jump; a repeated one (fall back)
    is its first occurrence."""
    wall = datetime.combine(day, time()) + timedelta(minutes=minutes)
    return wall.replace(tzinfo=tz, fold=0).timestamp()


def intervals(windows: list[Window], tz: ZoneInfo, t: float, *, days: int = LOOKAHEAD_DAYS) -> list[tuple[float, float]]:
    """The windows as UTC intervals, from the local day before `t` to `days` ahead, in order."""
    first = datetime.fromtimestamp(t, tz).date() - timedelta(days=1)
    out = []
    for k in range(days + 2):
        d = first + timedelta(days=k)
        for w in windows:
            if d.weekday() not in w.days:
                continue
            start = local_instant(d, w.start, tz)
            end = local_instant(d + timedelta(days=1) if w.overnight else d, w.end, tz)
            if end > start:
                out.append((start, end))
    return sorted(out)


def is_open(windows: list[Window], tz: ZoneInfo, t: float) -> bool:
    return not windows or any(a <= t < b for a, b in intervals(windows, tz, t, days=1))


def next_open(windows: list[Window], tz: ZoneInfo, t: float) -> float | None:
    """`t` when a window holds it, else the start of the next one; None when none opens ahead."""
    if not windows:
        return t
    for a, b in intervals(windows, tz, t):
        if a <= t < b:
            return t
        if a > t:
            return a
    return None


def open_until(windows: list[Window], tz: ZoneInfo, t: float) -> float | None:
    """When the window holding `t` closes (joined windows count as one), else None: a moved slot's
    jitter never pushes it past the close (`jitter`)."""
    end = None
    for a, b in intervals(windows, tz, t):
        if end is None:
            if a <= t < b:
                end = b
        elif a <= end:
            end = max(end, b)
    return end


def _day_start(windows: list[Window], tz: ZoneInfo, d: date) -> float:
    """Where the lane day `d` starts: local midnight, or later when a waking window that started
    the day before runs over midnight (it belongs whole to the day it started)."""
    start = local_instant(d, 0, tz)
    before = d - timedelta(days=1)
    for w in windows:
        if w.overnight and before.weekday() in w.days:
            start = max(start, local_instant(d, w.end, tz))
    return start


def lane_day(windows: list[Window], tz: ZoneInfo, t: float) -> tuple[float, float]:
    """The lane day holding `t` as [start, end): midnight to midnight in `tz`, except that a window
    running over midnight ("22:00-02:00") counts whole toward the day it starts on."""
    d = datetime.fromtimestamp(t, tz).date()
    start = _day_start(windows, tz, d)
    if t < start:
        return _day_start(windows, tz, d - timedelta(days=1)), start
    return start, _day_start(windows, tz, d + timedelta(days=1))


def next_midnight(tz: ZoneInfo, t: float) -> float:
    d = datetime.fromtimestamp(t, tz).date()
    for k in range(1, 4):
        at = local_instant(d + timedelta(days=k), 0, tz)
        if at > t:
            return at
    raise AssertionError("unreachable: a local midnight comes round within three days")


# ---- what the owner sets -----------------------------------------------------------------------

class Pace(BaseModel):
    """A lane's posting pace. Every field optional; an empty Pace paces nothing."""
    model_config = ConfigDict(extra="forbid")
    commits_per_day: int | None = Field(default=None, le=1000)
    min_gap_minutes: int | None = Field(default=None, ge=0, le=MAX_GAP_MINUTES)
    active_hours: list[str] | None = None
    timezone: str | None = None

    @field_validator("active_hours", mode="before")
    @classmethod
    def _hours(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str):
            v = [p for p in re.split(r"[;\n]", v) if p.strip()]
        if not isinstance(v, list):
            raise ValueError("active_hours is a list of windows like 08:00-22:00")  # noqa: TRY004 (pydantic)
        if len(v) > MAX_WINDOWS:
            raise ValueError(f"at most {MAX_WINDOWS} windows")
        return [Window.parse(str(x)).text() for x in v] or None

    @field_validator("commits_per_day")
    @classmethod
    def _per_day(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("commits_per_day is at least 1. To stop a lane posting, pause the lane")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        from openajar.core.zone import valid_zone

        if valid_zone(v) is None:
            raise ValueError(f"{v!r} is not a time zone name like America/New_York or Europe/Vilnius")
        return v

    def windows(self) -> list[Window]:
        return [Window.parse(x) for x in self.active_hours or []]

    def paces(self) -> bool:
        """Does it limit anything (a zone override alone does not)?"""
        return bool(self.commits_per_day is not None or self.min_gap_minutes or self.active_hours)

    def given(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


PRESET = Pace(commits_per_day=3, min_gap_minutes=45, active_hours=["08:00-22:00"])


@dataclass(frozen=True)
class Zone:
    name: str
    source: str                       # lane | pin | workspace | node
    exit_timezone: str | None = None  # where the lane check last saw the exit, when it differs from the pin
    unreadable: bool = False          # the pin exists but could not be read, and no earlier read is known

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.name)

    def words(self) -> str:
        if self.unreadable:
            return "unknown: the lane's pinned fingerprint could not be read"
        return f"{self.name}, {SOURCE_WORDS[self.source]}"

    def note(self) -> str | None:
        if self.unreadable:
            return ("The lane's pinned fingerprint could not be read, so its timezone is not known and a paced "
                    "publish is refused. Check the lane's fingerprint, or set a timezone on its pace.")
        if self.exit_timezone and self.source == "pin":
            return (f"The proxy's exit is now in {self.exit_timezone}, not the pinned {self.name}. The pinned "
                    f"zone is used, because that is what the site sees; check the lane's proxy.")
        return None

    def public(self) -> dict[str, Any]:
        return {"timezone": self.name, "source": self.source, "source_words": SOURCE_WORDS[self.source],
                "exit_timezone": self.exit_timezone, "unreadable": self.unreadable, "note": self.note()}


class LanePaced(LimitReached):
    """A publish the lane's posting pace does not allow now (AUD-13). Never overridden by an
    approval. `hit` carries the rule, `next_at` (UTC) and `next_at_local`; `next_ts` the timestamp."""
    code = "lane_paced"

    def __init__(self, hit: dict, next_ts: float | None):
        OpenajarError.__init__(self, hit["explain"])
        self.reason = "lane_paced"
        self.detail = hit["explain"]
        self.hit = hit
        self.scope = None
        self.units = 0
        self.next_ts = next_ts
        self.next_until: float | None = None  # when the waking window it may next post in closes


# ---- the arithmetic ---------------------------------------------------------------------------

def _until(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    d, rest = divmod(minutes, 24 * 60)
    h, m = divmod(rest, 60)
    parts = [f"{d} d"] * bool(d) + [f"{h} h"] * bool(h) + [f"{m} min"] * bool(m and not d)
    return " ".join(parts) or "1 min"


def local_words(t: float, tz: ZoneInfo, now: float) -> str:
    """"08:00 today", "08:00 tomorrow", "08:00 on Mon 9 Nov", in `tz`."""
    at, today = datetime.fromtimestamp(t, tz), datetime.fromtimestamp(now, tz).date()
    hm = at.strftime("%H:%M")
    if at.date() == today:
        return f"{hm} today"
    if at.date() == today + timedelta(days=1):
        return f"{hm} tomorrow"
    return f"{hm} on {at.strftime('%a')} {at.day} {at.strftime('%b')}"


@dataclass
class Verdict:
    """What the pace says about a publish at `at`: None rule when it may go."""
    rule: str | None          # active_hours | min_gap | commits_per_day | timezone (the lane's zone is unknown)
    next_ts: float | None     # when the lane may next post (None: no window opens within LOOKAHEAD_DAYS)
    used_today: int
    last_at: float | None
    near_at: float | None = None  # min_gap: the publish (made or queued) it is too close to


def verdict(pace: Pace, tz: ZoneInfo, at: float, *, last_at: Callable[[float], float | None],
            count_day: Callable[[float, float], int],
            next_at: Callable[[float], float | None] | None = None) -> Verdict:
    """Whether a publish may go at `at`, and when the lane may next post. `last_at(t)`: the last
    publish at or before `t`; `next_at(t)`: the first one queued after `t`; `count_day(start,
    end)`: publishes in [start, end). The gap holds on both sides."""
    windows = pace.windows()
    gap = (pace.min_gap_minutes or 0) * 60
    near: dict[float, float] = {}

    def blocking(t: float) -> tuple[str | None, float | None]:
        if windows and not is_open(windows, tz, t):
            return "active_hours", next_open(windows, tz, t)
        if pace.commits_per_day is not None:
            start, end = lane_day(windows, tz, t)
            if count_day(start, end) >= pace.commits_per_day:
                return "commits_per_day", end
        if gap:
            last = last_at(t)
            if last is not None and t < last + gap:
                near[t] = last
                return "min_gap", last + gap
            later = next_at(t) if next_at is not None else None
            if later is not None and t > later - gap:
                near[t] = later
                return "min_gap", later + gap
        return None, t

    a, b = lane_day(windows, tz, at)
    used = count_day(a, b)
    rule, candidate = blocking(at)
    if rule is None:
        return Verdict(None, at, used, last_at(at))
    first = rule
    for _ in range(4 * LOOKAHEAD_DAYS):
        if candidate is None:
            break
        r, nxt = blocking(candidate)
        if r is None:
            break
        candidate = nxt
    else:
        candidate = None
    return Verdict(first, candidate, used, last_at(at), near.get(at))


# ---- the store ---------------------------------------------------------------------------------

class PaceStore:
    """The owner's paces, workspace zones and the publish ledger, in policy.db."""

    def __init__(self, db: DB, *, clock: Callable[[], float]):
        self.db = db
        self.clock = clock
        with db.lock:
            db.conn.executescript(SCHEMA)

    def get(self, lane_id: str) -> Pace | None:
        row = self.db.one("SELECT pace FROM pace_lanes WHERE lane = ?", (lane_id,))
        return Pace.model_validate(json.loads(row["pace"])) if row is not None else None

    def meta(self, lane_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT updated_at, by FROM pace_lanes WHERE lane = ?", (lane_id,))
        return {"updated_at": iso(row["updated_at"]), "by": row["by"]} if row is not None else None

    def set(self, lane_id: str, pace: Pace | None, *, by: str) -> None:
        if pace is None or not pace.given():
            self.db.execute("DELETE FROM pace_lanes WHERE lane = ?", (lane_id,))
            return
        self.db.execute("INSERT INTO pace_lanes (lane, pace, updated_at, by) VALUES (?, ?, ?, ?) ON CONFLICT(lane) "
                        "DO UPDATE SET pace = excluded.pace, updated_at = excluded.updated_at, by = excluded.by",
                        (lane_id, pace.model_dump_json(exclude_none=True), self.clock(), by))

    def lanes(self) -> list[str]:
        return [r["lane"] for r in self.db.all("SELECT lane FROM pace_lanes ORDER BY lane")]

    def workspace_zone(self, workspace: str) -> str | None:
        row = self.db.one("SELECT timezone FROM pace_workspaces WHERE workspace = ?", (workspace,))
        return row["timezone"] if row is not None else None

    def set_workspace_zone(self, workspace: str, name: str | None, *, by: str) -> None:
        if not name:
            self.db.execute("DELETE FROM pace_workspaces WHERE workspace = ?", (workspace,))
            return
        self.db.execute("INSERT INTO pace_workspaces (workspace, timezone, updated_at, by) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(workspace) DO UPDATE SET timezone = excluded.timezone, "
                        "updated_at = excluded.updated_at, by = excluded.by", (workspace, name, self.clock(), by))

    # ---- the ledger (read inside the caller's transaction when `conn` is given) ----
    def _q(self, conn: Any, sql: str, args: tuple) -> Any:
        return conn.execute(sql, args).fetchone() if conn is not None else self.db.one(sql, args)

    @staticmethod
    def _not(exclude: tuple[int, ...]) -> tuple[str, tuple]:
        if not exclude:
            return "", ()
        return f" AND id NOT IN ({','.join('?' * len(exclude))})", tuple(exclude)

    def last_at(self, lane_id: str, t: float, conn: Any = None, *, exclude: tuple[int, ...] = ()) -> float | None:
        skip, ids = self._not(exclude)
        row = self._q(conn, f"SELECT MAX(at) AS at FROM pace_commits WHERE lane = ? AND at <= ?{skip}",
                      (lane_id, t, *ids))
        return float(row["at"]) if row is not None and row["at"] is not None else None

    def next_at(self, lane_id: str, t: float, conn: Any = None, *, exclude: tuple[int, ...] = ()) -> float | None:
        """The first publish queued after `t` (a run's slot)."""
        skip, ids = self._not(exclude)
        row = self._q(conn, f"SELECT MIN(at) AS at FROM pace_commits WHERE lane = ? AND at > ?{skip}",
                      (lane_id, t, *ids))
        return float(row["at"]) if row is not None and row["at"] is not None else None

    def count(self, lane_id: str, start: float, end: float, conn: Any = None, *,
              exclude: tuple[int, ...] = ()) -> int:
        skip, ids = self._not(exclude)
        row = self._q(conn, f"SELECT COUNT(*) AS n FROM pace_commits WHERE lane = ? AND at >= ? AND at < ?{skip}",
                      (lane_id, start, end, *ids))
        return int(row["n"]) if row is not None else 0

    def record(self, conn: Any, lane_id: str, at: float) -> int:
        cur = conn.execute("INSERT INTO pace_commits (lane, at, recorded_at) VALUES (?, ?, ?)",
                           (lane_id, at, self.clock()))
        return int(cur.lastrowid)

    def forget(self, ids: list[int]) -> None:
        if ids:
            self.db.execute(f"DELETE FROM pace_commits WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))

    def purge(self) -> int:
        return self.db.execute("DELETE FROM pace_commits WHERE at < ?",
                               (self.clock() - KEEP_DAYS * 86400,)).rowcount


# ---- the words -----------------------------------------------------------------------------

def sentence(lane_id: str, pace: Pace, zone: Zone, v: Verdict, now: float) -> str:
    tz = zone.tz
    if v.rule == "timezone":
        return (f"Browser {lane_id}'s timezone could not be read (its pinned fingerprint is unreadable), so it may "
                "not publish now: its waking hours and its day cannot be told. Check the browser's fingerprint, or "
                "set a timezone on its pace.")
    if v.rule == "active_hours":
        why = (f"Browser {lane_id} is outside its posting hours ({', '.join(pace.active_hours or [])} its time, "
               f"{zone.words()})")
    elif v.rule == "min_gap":
        near = v.near_at if v.near_at is not None else v.last_at
        if near is not None and near > now:
            what = f"has a post queued for {local_words(near, tz, now)} its time"
        else:
            what = f"posted {_until(now - near) if near is not None else 'moments'} ago"
        why = f"Browser {lane_id} {what} and keeps at least {pace.min_gap_minutes} minutes between posts"
    else:
        n = pace.commits_per_day
        why = f"Browser {lane_id} used its {n} {'post' if n == 1 else 'posts'} for that day (its time, {zone.words()})"
    if v.next_ts is None:
        when = "no waking hours open in the next week, so it may not post until the owner changes its pace"
    else:
        utc = datetime.fromtimestamp(v.next_ts, UTC).strftime("%Y-%m-%d %H:%M UTC")
        when = (f"it may next post at {local_words(v.next_ts, tz, now)} its time ({zone.name}), {utc}, "
                f"in {_until(v.next_ts - now)}")
    note = zone.note()
    return f"{why}, so it may not publish now: {when}." + (f" {note}" if note else "")


def hit(lane_id: str, pace: Pace, zone: Zone, v: Verdict, now: float) -> dict[str, Any]:
    nxt = v.next_ts
    return {"code": "lane.pace", "scope": "lane", "id": lane_id, "rule": v.rule,
            "next_at": iso(nxt) if nxt is not None else None,
            "next_at_local": datetime.fromtimestamp(nxt, zone.tz).isoformat(timespec="minutes") if nxt else None,
            "timezone": zone.name, "timezone_source": zone.source, "explain": sentence(lane_id, pace, zone, v, now)}


def jitter(seed: str, at: float, until: float | None = None) -> float:
    """A moved slot goes a little after the lane may post again, never on the minute, and never
    past `until` (the close of the waking window it moves into): a short window keeps it inside."""
    r = random.Random(f"{seed}:{int(at)}")
    lo, hi = JITTER_S
    if until is not None:
        room = max(0.0, (until - at) / 2)
        hi = min(hi, room)
        lo = min(lo, hi / 2)
    return at + r.uniform(lo, hi)


def refuse(lane_id: str, pace: Pace, zone: Zone, v: Verdict, now: float) -> Denied:
    e = LanePaced(hit(lane_id, pace, zone, v, now), v.next_ts)
    windows = pace.windows()
    if windows and v.next_ts is not None:
        e.next_until = open_until(windows, zone.tz, v.next_ts)
    return e
