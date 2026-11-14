"""Lane health: the checks the node already runs, turned into one grade with plain findings.

`build_health(...)` is pure. It takes the lane's latest lane check (agentos.stealth.lanecheck), the
`agentos lane audit` pin findings that name the lane or its device (agentos.stealth.stealth), the
lane's hold and quarantine events (`lane_events`), and the pin's age, and gives a `LaneHealth`:

    grade      "Looks normal", "Check this" or "Stop"
    headline   "No known problems" for a good grade; otherwise how many things to look at or fix
    facts      countable facts only: "8 of 9 checks pass", "2 challenges in 30 days", ...
    findings   each with an id, a level (fail, warn, unknown or note), one plain sentence and a fix

How the grade is decided:

    Stop          any finding at fail (a failing check or pin finding), or the lane is quarantined now
    Check this    any finding at warn, a challenge in the last 14 days, 2 or more challenges in the
                  window, no lane check yet, or not one check that passed
    Looks normal  none of the above

A check that could not run (unknown) is listed and never counted as a pass; on its own it neither
raises nor lowers the grade. A challenge is a hold the node set because a site showed a captcha, a
checkpoint, a block page or a rate limit (`needs_human:<signal>`, `backoff:rate_limited`). A hold
written again after a hand back (the same challenge still on screen) is not counted twice.

What this never does: call a lane safe or undetectable, score how human it looks, or ask a site
anything. It reports consistency, leaks and the challenges the node actually received. What it
cannot see is in CANNOT_SEE, shown next to every grade.

`gather(...)` and `gather_all(...)` collect those inputs on a node (the lane check's last report
or a fresh one, the lane manager's events or agentos.db read-only, the pin files) and call it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentos.stealth.lanecheck import CHECK_IDS, FAIL, OK, PIN_META, UNKNOWN, WARN, LaneReport

log = logging.getLogger("agentos.stealth.health")

NOTE = "note"
FINDING_LEVELS = (FAIL, WARN, UNKNOWN, NOTE)        # the order findings are listed in

NORMAL, CHECK, STOP = "normal", "check", "stop"
GRADES = {NORMAL: "Looks normal", CHECK: "Check this", STOP: "Stop"}
GOOD_HEADLINE = "No known problems"

DEFAULT_DAYS = 30
MAX_DAYS = 365
RECENT_DAYS = 14             # one challenge this recent is enough for "Check this"
MANY_CHALLENGES = 2          # this many in the window too
SESSION_WARN_DAYS = 7        # a session that ends this soon is a warning
SESSION_NOTE_DAYS = 14       # and this soon a note
EVENT_LIMIT = 2000

# What a site showed, as the hold's reason names it, in words.
CHALLENGES = {
    "captcha": "a captcha",
    "checkpoint": "a checkpoint",
    "blocked": "a block page",
    "rate_limited": "a rate limit",
}
OTHER_HOLDS = {
    "logged_out": "signed out",
    "wrong_account": "signed in to another account",
}
QUARANTINE_HOLD = "quarantined:"    # agentos.node.lanes.base.QUARANTINE_HOLD

DOCS = "docs/RESPONSIBLE-USE.md"
WHAT_IT_CHECKS = (
    "The lane check: proxy, exit IP, location, WebRTC, DNS, profile and pin, signed in, account and hold.",
    "Pin findings from agentos lane audit that name this lane or its device.",
    "Challenges sites showed (captcha, checkpoint, block page, rate limit) and quarantines on this node.",
    ("How old the pin is, how far the pinned browser is behind the installed one, and when the session "
     "cookie ends."),
)
CANNOT_SEE = (
    "The site's own risk model, and the account's history with the site.",
    "How the site rates the exit IP address.",
    "How the lane behaves over weeks, beyond the holds recorded here.",
    "Whether the site's terms allow automation.",
)
SCOPE_NOTE = ("A good grade means these checks found no problem. It is not a promise about what a site "
              f"decides. Read {DOCS} before you automate an account.")

PIN_FIXES = {
    "pin": "See agentos lane audit and docs/design/stealth.md. A new pin makes sites see a new computer, so "
           "change it only on purpose.",
    "collision": "Give each company its own device and proxy (agentos device create, then agentos lane "
                 "set-device).",
    "device": "Give each company its own device and proxy (agentos device create, then agentos lane "
              "set-device).",
    "location": "Use a proxy that exits where the pin says the computer is (docs/PROXIES.md).",
}


# ---- the result ------------------------------------------------------------------------------

@dataclass
class HealthFinding:
    id: str
    level: str                  # fail, warn, unknown or note
    sentence: str
    fix: str | None = None
    source: str = "check"       # check, pin, events, session

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "level": self.level, "sentence": self.sentence, "fix": self.fix,
                "source": self.source}


@dataclass
class LaneHealth:
    lane: str
    grade: str                  # normal, check or stop
    headline: str
    facts: list[str]
    findings: list[HealthFinding]
    days: int
    now: datetime
    checked_at: datetime | None = None
    state: str | None = None
    counts: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return GRADES[self.grade]

    def facts_line(self) -> str:
        return " · ".join(self.facts)

    def to_dict(self) -> dict[str, Any]:
        return {"lane": self.lane, "grade": self.grade, "label": self.label, "headline": self.headline,
                "facts": list(self.facts), "facts_line": self.facts_line(),
                "findings": [f.to_dict() for f in self.findings], "days": self.days,
                "at": self.now.isoformat(timespec="seconds"),
                "checked_at": self.checked_at.isoformat(timespec="seconds") if self.checked_at else None,
                "state": self.state, "counts": dict(self.counts),
                "checks": list(WHAT_IT_CHECKS), "cannot_see": list(CANNOT_SEE), "note": SCOPE_NOTE,
                "docs": DOCS}

    def brief(self) -> dict[str, Any]:
        """What a list shows next to the lane check's brief."""
        return {"grade": self.grade, "label": self.label, "headline": self.headline,
                "facts_line": self.facts_line()}


# ---- events ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class LaneEventSeen:
    at: datetime
    kind: str                   # challenge, quarantine or hold
    what: str                   # the signal (captcha, ...), or the quarantine's reason


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            return _when(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def _event_parts(event: Any) -> tuple[datetime | None, str, dict[str, Any]]:
    """An event as the lane manager keeps it (a LaneEvent, or the dev node's dict)."""
    if isinstance(event, Mapping):
        at, kind, detail = event.get("at"), event.get("kind"), event.get("detail")
    else:
        at, kind, detail = getattr(event, "at", None), getattr(event, "kind", None), getattr(event, "detail", None)
    return _when(at), str(kind or ""), dict(detail) if isinstance(detail, Mapping) else {}


def classify(event: Any) -> LaneEventSeen | None:
    """A challenge, a quarantine or another hold the node set, or None for any other event."""
    at, kind, detail = _event_parts(event)
    if at is None:
        return None
    if kind == "quarantined":
        return LaneEventSeen(at, "quarantine", str(detail.get("reason") or "").strip())
    if kind != "held" or detail.get("after") == "hand_back":
        return None
    head = str(detail.get("reason") or "").split(" on ", 1)[0].strip()
    if head.startswith(QUARANTINE_HOLD):
        return None                 # the quarantine's own hold: counted as the quarantine
    prefix, _, signal = head.partition(":")
    if prefix not in ("needs_human", "backoff"):
        return None                 # a hold a person set by hand
    if signal in CHALLENGES:
        return LaneEventSeen(at, "challenge", signal)
    return LaneEventSeen(at, "hold", signal)


@dataclass
class EventCounts:
    challenges: list[LaneEventSeen]
    recent: list[LaneEventSeen]
    quarantines: list[LaneEventSeen]
    holds: list[LaneEventSeen]


def count_events(events: Iterable[Any], *, now: datetime, days: int = DEFAULT_DAYS) -> EventCounts:
    """The window is (now - days, now]; `recent` is the challenges of the last RECENT_DAYS of it."""
    start = now - timedelta(days=days)
    recent_start = now - timedelta(days=min(days, RECENT_DAYS))
    seen = sorted((e for e in (classify(x) for x in events) if e is not None and start < e.at <= now),
                  key=lambda e: e.at, reverse=True)
    challenges = [e for e in seen if e.kind == "challenge"]
    return EventCounts(challenges=challenges, recent=[e for e in challenges if e.at > recent_start],
                       quarantines=[e for e in seen if e.kind == "quarantine"],
                       holds=[e for e in seen if e.kind == "hold"])


# ---- building the grade ------------------------------------------------------------------------

def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def _day(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%d")


def _sentence(text: str) -> str:
    """Passed-through text as one plain sentence: capitalised, ending in a period, no em dash."""
    text = " ".join(str(text).replace("\u2014", ", ").split())
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def _days_between(later: datetime, earlier: datetime) -> int:
    return math.floor((later - earlier).total_seconds() / 86400)


def _pin_finding(f: Any) -> HealthFinding | None:
    level = getattr(f, "level", None)
    check = str(getattr(f, "check", "") or "")
    detail = str(getattr(f, "detail", "") or "")
    if level not in (FAIL, WARN):
        return None
    if check == "freshness":
        return None                 # the profile check and the browser fact say this already
    if check == "pin" and detail.startswith("a version ") and detail.endswith("next launch"):
        return None                 # the profile check says this already
    fix = PIN_FIXES.get(check, PIN_FIXES["pin"])
    return HealthFinding(f"pin.{check}", level, _sentence(detail), fix, "pin")


def _names_lane(f: Any, lane: str, device: str | None) -> bool:
    unit = getattr(f, "lane", None)
    if unit == lane or (device and unit == f"device:{device}"):
        return True
    data = getattr(f, "data", None) or {}
    named = list(data.get("lanes") or []) + list(data.get("units") or [])
    return lane in named or bool(device and f"device:{device}" in named)


def pin_findings_for(findings: Iterable[Any], lane: str, device: str | None = None) -> list[Any]:
    """The audit's findings that name this lane, or the device it is on."""
    return [f for f in findings if _names_lane(f, lane, device)]


def build_health(lane: str, *, report: LaneReport | Mapping[str, Any] | None, pin_findings: Iterable[Any] = (),
                 events: Iterable[Any] = (), pinned_at: datetime | str | None = None, now: datetime,
                 days: int = DEFAULT_DAYS, quarantined: bool | None = None) -> LaneHealth:
    """One lane's health from what the node already knows. Pure: the same inputs give the same answer.

    `report` is the latest lane check (None when the lane was never checked); `pin_findings` the
    `agentos lane audit` findings that name this lane or its device; `events` the lane's events (any
    order; only holds and quarantines inside the window count); `pinned_at` when the pin was made;
    `quarantined` overrides what the report says about a quarantine now."""
    if isinstance(report, Mapping):
        report = LaneReport.from_dict(report)
    now = _when(now) or datetime.now(UTC)
    days = max(1, min(int(days), MAX_DAYS))
    findings: list[HealthFinding] = []
    facts: list[str] = []
    counts: dict[str, Any] = {}

    # (a) the lane check
    checks = list(report.checks) if report is not None else []
    by_id = {c.id: c for c in checks}
    passed = sum(1 for c in checks if c.level == OK)
    unknown = sum(1 for c in checks if c.level == UNKNOWN)
    counts.update(checks_total=len(checks), checks_ok=passed, checks_unknown=unknown,
                  checks_warn=sum(1 for c in checks if c.level == WARN),
                  checks_fail=sum(1 for c in checks if c.level == FAIL))
    if report is None:
        findings.append(HealthFinding("not_checked", WARN, "This lane has not been checked yet, so nothing about "
                                      "it is known.", f"Run agentos lane check {lane}, or wait for the next "
                                      "health round (about every 5 minutes).", "check"))
        facts.append("not checked yet")
    else:
        for c in checks:
            if c.level == OK:
                continue
            fix = _sentence(c.fix) if c.fix and c.level in (FAIL, WARN) else None
            findings.append(HealthFinding(f"check.{c.id}", c.level, _sentence(c.detail), fix, "check"))
        facts.append(f"{passed} of {_plural(len(checks), 'check')} pass")
        if unknown:
            facts.append(f"{unknown} could not run")
        if checks and passed and unknown * 2 > len(checks):  # most of it is not known: that is not "normal"
            findings.append(HealthFinding("little_verified", WARN, f"{unknown} of {_plural(len(checks), 'check')} "
                                          "could not run, so most of this lane is not known.", "Look at why each "
                                          f"check could not run (agentos lane check {lane}).", "check"))
        if checks and passed == 0 and not any(c.level in (FAIL, WARN) for c in checks):
            findings.append(HealthFinding("nothing_verified", WARN, "No check could confirm anything about this "
                                          "lane.", "Look at why each check could not run (agentos lane check "
                                          f"{lane}).", "check"))

    # an active quarantine is a stop, whatever else the report says
    hold = by_id.get("hold")
    active = quarantined if quarantined is not None else bool(
        report is not None and (report.state == "quarantined"
                                or (hold is not None and (hold.data or {}).get("kind") == "quarantine")))
    counts["quarantined_now"] = active
    if active and not (hold is not None and hold.level == FAIL):
        findings.append(HealthFinding("quarantine", FAIL, "The lane is quarantined: nothing runs there until a "
                                      "person clears it.", f"A person fixes the cause, then hands the lane back "
                                      f"(agentos lane handback {lane}).", "check"))

    # (b) pin findings
    seen_pin: set[tuple[str, str]] = set()
    for f in pin_findings:
        made = _pin_finding(f)
        if made is not None and (made.id, made.sentence) not in seen_pin:
            seen_pin.add((made.id, made.sentence))
            findings.append(made)

    # (c) holds and quarantines in the window
    seen = count_events(events, now=now, days=days)
    n = len(seen.challenges)
    counts.update(challenges=n, challenges_recent=len(seen.recent), quarantines=len(seen.quarantines),
                  other_holds=len(seen.holds))
    facts.append(f"{_plural(n, 'challenge')} in {days} days" if n else f"no challenges in {days} days")
    if n:
        listed = ", ".join(f"{CHALLENGES[e.what]} on {_day(e.at)}" for e in seen.challenges[:3])
        more = f", and {n - 3} more" if n > 3 else ""
        sentence = f"Sites showed {listed}{more} ({_plural(n, 'challenge')} in {days} days)."
        if seen.recent or n >= MANY_CHALLENGES:
            findings.append(HealthFinding(
                "challenges", WARN, sentence,
                f"Let the lane rest and lower its pace. Look at what agents did just before each one "
                f"(agentos lane view {lane}).", "events"))
        else:
            findings.append(HealthFinding("challenges", NOTE, sentence, None, "events"))
    if seen.quarantines:
        q = len(seen.quarantines)
        counts["last_quarantine"] = _day(seen.quarantines[0].at)
        facts.append(f"{_plural(q, 'quarantine')} in {days} days")
        if not active:
            reason = seen.quarantines[0].what
            findings.append(HealthFinding("quarantines", NOTE, f"Quarantined {_plural(q, 'time')} in {days} days, "
                                          f"the last on {_day(seen.quarantines[0].at)}"
                                          + (f" ({reason})." if reason else "."), None, "events"))
    if seen.holds:
        words = sorted({OTHER_HOLDS.get(e.what, e.what.replace("_", " ")) for e in seen.holds})
        findings.append(HealthFinding("other_holds", NOTE, f"Held {_plural(len(seen.holds), 'time')} in {days} days "
                                      f"for other reasons: {', '.join(words)}.", None, "events"))

    # (d) the pin's age, the browser version, the session's end
    pinned = _when(pinned_at)
    if pinned is not None and pinned <= now:
        age = _days_between(now, pinned)
        counts["pin_age_days"] = age
        facts.append(f"pin {_plural(age, 'day')} old")
    profile = by_id.get("profile")
    pdata = (profile.data or {}) if profile is not None else {}
    pin_v, engine_v = pdata.get("pinned_firefox"), pdata.get("engine_firefox")
    if isinstance(pin_v, int) and isinstance(engine_v, int):
        lag = engine_v - pin_v
        counts["browser_lag"] = lag
        if lag > 0:
            facts.append(f"pinned browser {_plural(lag, 'version')} behind")
    session = by_id.get("session")
    if session is not None and session.level == OK:
        ends = [t for t in (_when(s.get("expires_at")) for s in ((session.data or {}).get("sites") or {}).values()
                            if isinstance(s, Mapping)) if t is not None]
        if ends:
            left = _days_between(min(ends), now)
            counts["session_days_left"] = left
            facts.append(f"session ends in {_plural(left, 'day')}" if left > 0 else "session ends today")
            if left <= SESSION_NOTE_DAYS:
                findings.append(HealthFinding(
                    "session_ends", WARN if left <= SESSION_WARN_DAYS else NOTE,
                    f"The session cookie ends on {_day(min(ends))}" + (f", in {_plural(left, 'day')}." if left > 0
                                                                       else ", today."),
                    f"A person signs in again before then, through the live view (agentos lane view {lane} "
                    "--control).", "session"))

    # the grade
    fails = [f for f in findings if f.level == FAIL]
    warns = [f for f in findings if f.level == WARN]
    if fails or active:
        grade = STOP
        k = max(1, len(fails))
        headline = f"{_plural(k, 'problem')} to fix before agents use this lane"
    elif warns:
        grade = CHECK
        headline = f"{_plural(len(warns), 'thing')} to look at"
    else:
        grade, headline = NORMAL, GOOD_HEADLINE
    order = {level: i for i, level in enumerate(FINDING_LEVELS)}
    check_order = {f"check.{cid}": i for i, cid in enumerate(CHECK_IDS)}
    findings.sort(key=lambda f: (order.get(f.level, len(order)), check_order.get(f.id, len(check_order))))
    return LaneHealth(lane=lane, grade=grade, headline=headline, facts=facts, findings=findings, days=days,
                      now=now, checked_at=report.at if report is not None else None,
                      state=report.state if report is not None else None, counts=counts)


def format_health(health: LaneHealth | Mapping[str, Any]) -> str:
    """A lane's health as short lines a phone can show."""
    h = health.to_dict() if isinstance(health, LaneHealth) else dict(health)
    lines = [f"{h['lane']}: {h['label']}. {h['headline']}.", f"  {h['facts_line']}"]
    checked = _when(h.get("checked_at"))
    at = _when(h.get("at"))
    if checked is not None and at is not None:
        minutes = max(0, round((at - checked).total_seconds() / 60))
        lines[1] += f" · checked {'just now' if minutes == 0 else _plural(minutes, 'min', 'min') + ' ago'}"
    for f in h.get("findings", []):
        lines.append(f"  {f['level'].upper():<7} {f['sentence']}")
        if f.get("fix") and f["level"] in (FAIL, WARN):
            lines.append(f"          fix: {f['fix']}")
    return "\n".join(lines)


# ---- gathering the inputs on a node ------------------------------------------------------------

def read_events(db_path: Path, lane_id: str, *, since: datetime, limit: int = EVENT_LIMIT) -> list[dict[str, Any]]:
    """Hold and quarantine events of one lane from agentos.db, read-only (no daemon running)."""
    from agentos.ops.layout import connect_ro

    if not Path(db_path).exists():
        return []
    try:
        conn = connect_ro(Path(db_path))
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "lane_events" not in tables:
                return []
            rows = conn.execute("SELECT at, kind, detail FROM lane_events WHERE lane_id = ? AND kind IN "
                                "('held', 'quarantined') ORDER BY seq DESC LIMIT ?", (lane_id, limit)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    out = []
    for at, kind, detail in rows:
        when = _when(at)
        if when is None or when <= since:
            continue
        try:
            data = json.loads(detail) if detail else {}
        except ValueError:
            data = {}
        out.append({"at": when, "kind": kind, "detail": data if isinstance(data, dict) else {}})
    return out


def _lane_events(checker: Any, lane_id: str, since: datetime) -> list[Any]:
    db_path = getattr(getattr(checker, "config", None), "db_path", None)
    if db_path is not None:
        found = read_events(db_path, lane_id, since=since)  # only holds and quarantines, however busy the lane
        if found:
            return found
    lanes = checker.lanes  # a node that keeps events in memory (the dev node), or none in the window
    events = getattr(lanes, "events", None) if lanes is not None else None
    if callable(events):
        try:
            return list(events(lane_id, limit=EVENT_LIMIT))
        except Exception:  # noqa: BLE001 - no events is "none recorded", and the check still stands
            return []
    return []


def for_member(health: dict[str, Any]) -> dict[str, Any]:
    """`LaneHealth.to_dict()` for someone who is not the owner. The fingerprint audit compares lanes, so
    its sentences can name other lanes, devices, proxies and exit addresses: a member reads that
    there is something, and the owner reads what."""
    out = dict(health)
    findings = list(health.get("findings") or [])
    pins = [f for f in findings if f.get("source") == "pin"]
    kept = [f for f in findings if f.get("source") != "pin"]
    if pins:
        worst = FAIL if any(f.get("level") == FAIL for f in pins) else pins[0].get("level")
        at = findings.index(pins[0])
        kept.insert(min(at, len(kept)), {
            "id": "pin", "level": worst, "source": "pin", "fix": "Ask the owner to run agentos lane audit.",
            "sentence": "The node's fingerprint audit found something to look at on this lane or its device."})
    out["findings"] = kept
    return out


def _device(checker: Any, lane_id: str) -> str | None:
    lanes = checker.lanes
    if lanes is not None:
        try:
            return getattr(lanes.get(lane_id).spec, "device", None)
        except Exception:  # noqa: BLE001 - a lane gone since the check
            return None
    from agentos.ops.layout import lane_specs

    spec = lane_specs(checker.home).get(lane_id)
    return spec.device if spec is not None else None


def read_pinned_at(home: Path, lane_id: str) -> datetime | None:
    """When the lane's pin (its device's, for a lane on a device) was made, from its PIN_META."""
    from agentos.ops.layout import lane_pin_path

    try:
        data = json.loads(lane_pin_path(Path(home), lane_id).read_text())
    except (OSError, ValueError):
        return None
    meta = data.get(PIN_META) if isinstance(data, dict) else None
    return _when(meta.get("pinned_at")) if isinstance(meta, dict) else None


def _installed(reports: Iterable[LaneReport]) -> int | None:
    for r in reports:
        for c in r.checks:
            if c.id == "profile" and isinstance((c.data or {}).get("engine_firefox"), int):
                return c.data["engine_firefox"]
    return None


async def _audit(home: Path, reports: list[LaneReport]) -> list[Any]:
    from agentos.stealth import stealth

    try:
        found, _count = await asyncio.to_thread(stealth.audit_pins, Path(home), installed_major=_installed(reports))
    except Exception:  # noqa: BLE001 - the pins are read again on the next call; the check still stands
        return []
    return found


async def _one(checker: Any, report: LaneReport, *, days: int, now: datetime, audit: list[Any]) -> LaneHealth:
    lane_id = report.lane
    since = now - timedelta(days=days)
    events = await asyncio.to_thread(_lane_events, checker, lane_id, since)
    device = await asyncio.to_thread(_device, checker, lane_id)
    pinned_at = await asyncio.to_thread(read_pinned_at, checker.home, lane_id)
    return build_health(lane_id, report=report, pin_findings=pin_findings_for(audit, lane_id, device),
                        events=events, pinned_at=pinned_at, now=now, days=days)


async def grade_reports(checker: Any, reports: Iterable[LaneReport], *, days: int = DEFAULT_DAYS,
                        now: datetime | None = None) -> list[LaneHealth]:
    """The health of lanes that have a report already (a list's grades): runs no check."""
    reports = list(reports)
    if not reports:
        return []
    now = now or datetime.fromtimestamp(checker.clock(), UTC)
    audit = await _audit(checker.home, reports)
    return [await _one(checker, r, days=days, now=now, audit=audit) for r in reports]


async def gather(checker: Any, lane_id: str, *, days: int = DEFAULT_DAYS, fresh: bool = False,
                 now: datetime | None = None) -> LaneHealth:
    """One lane's health on a node: the lane check's last report (run now when there is none, or with
    `fresh`), its events and its pin. Reads only, as the lane check does. Raises LaneNotFound."""
    last = getattr(checker, "last", {}) or {}
    report = None if fresh else last.get(lane_id)
    if report is None:
        report = await checker.check(lane_id)
    now = now or datetime.fromtimestamp(checker.clock(), UTC)
    return await _one(checker, report, days=days, now=now, audit=await _audit(checker.home, [report]))


async def gather_all(checker: Any, lane_ids: Iterable[str] | None = None, *, days: int = DEFAULT_DAYS,
                     fresh: bool = False, now: datetime | None = None) -> list[LaneHealth]:
    """Every lane's health (or those of `lane_ids`), with one read of the pins for all of them."""
    ids = list(lane_ids) if lane_ids is not None else await asyncio.to_thread(checker.lane_ids)
    last = getattr(checker, "last", {}) or {}
    missing = [i for i in ids if fresh or i not in last]
    ran = {r.lane: r for r in await checker.check_all(missing)} if missing else {}
    reports = [ran.get(i) or last.get(i) for i in ids]
    return await grade_reports(checker, [r for r in reports if r is not None], days=days, now=now)
