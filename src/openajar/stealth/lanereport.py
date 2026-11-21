"""The lane check's report (openajar.stealth.lanecheck): the four levels, one Check per question, the
LaneReport that holds them, and the plain-text form `openajar lane check` prints.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"


LEVELS = (OK, UNKNOWN, WARN, FAIL)          # worst last


_RANK = {level: i for i, level in enumerate(LEVELS)}


CHECK_IDS = ("proxy", "exit_ip", "location", "webrtc", "dns", "profile", "session", "account", "hold")


TITLES = {"proxy": "Proxy", "exit_ip": "Exit IP", "location": "Location", "webrtc": "WebRTC", "dns": "DNS",
          "profile": "Profile", "session": "Signed in", "account": "Account", "hold": "Hold"}


@dataclass
class Check:
    id: str
    level: str
    detail: str
    fix: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return TITLES.get(self.id, self.id)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "level": self.level, "detail": self.detail,
                "fix": self.fix, "data": self.data}


def worst(levels: Iterable[str]) -> str:
    """The lane's level: fail, then warn, else ok. An unknown check never makes a lane unsafe."""
    present = set(levels)
    return FAIL if FAIL in present else WARN if WARN in present else OK


@dataclass
class LaneReport:
    lane: str
    state: str
    at: datetime
    checks: list[Check]

    @property
    def level(self) -> str:
        return worst(c.level for c in self.checks)

    @property
    def safe(self) -> bool:
        return self.level != FAIL

    def failing(self) -> list[str]:
        return [c.id for c in self.checks if c.level == FAIL]

    def summary(self) -> str:
        bad = [c for c in self.checks if c.level == FAIL] or [c for c in self.checks if c.level == WARN]
        if bad:
            return " ".join(c.detail for c in bad)
        unknown = [c.title for c in self.checks if c.level == UNKNOWN]
        if unknown:
            return (f"No known problems: nothing failed, but {len(unknown)} of {len(self.checks)} checks could not "
                    f"run ({', '.join(unknown)}).")
        return "No known problems: every check passed."

    def to_dict(self) -> dict[str, Any]:
        counts = {level: 0 for level in LEVELS}
        for c in self.checks:
            counts[c.level] += 1
        return {"lane": self.lane, "level": self.level, "safe": self.safe, "state": self.state,
                "at": self.at.isoformat(timespec="seconds"), "summary": self.summary(), "counts": counts,
                "checks": [c.to_dict() for c in self.checks]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LaneReport:
        checks = [Check(c["id"], c["level"], c["detail"], c.get("fix"), dict(c.get("data") or {}))
                  for c in data.get("checks", [])]
        return cls(str(data["lane"]), str(data.get("state", "unknown")), as_datetime(data.get("at")) or datetime.now(UTC),
                   checks)

    def brief(self) -> dict[str, Any]:
        """What a list shows: the worst level per lane and which checks fail or warn."""
        return {"lane": self.lane, "level": self.level, "safe": self.safe, "state": self.state,
                "at": self.at.isoformat(timespec="seconds"), "failing": self.failing(),
                "warning": [c.id for c in self.checks if c.level == WARN]}


def as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            return as_datetime(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def stamp(value: Any) -> str | None:
    when = as_datetime(value)
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if when else None


def human_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit in ("B", "KiB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def format_report(report: LaneReport, *, color: bool = False) -> str:
    """A report as short lines a phone can show."""
    colors = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", UNKNOWN: "\033[2m"}

    def label(level: str) -> str:
        text = level.upper().ljust(7)
        return f"{colors[level]}{text}\033[0m" if color else text

    lines = [(f"{report.lane}: {label(report.level).strip()} ({"device" if report.lane.startswith("device:") else "lane"} {report.state}, checked "
             f"{report.at.strftime('%H:%M UTC')})")]
    for c in report.checks:
        lines.append(f"  {label(c.level)} {c.title}: {c.detail}")
        if c.fix and c.level in (WARN, FAIL):
            lines.append(f"          fix: {c.fix}")
    return "\n".join(lines)
