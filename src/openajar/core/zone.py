"""The node's timezone: the IANA name the panel and the dashboard show times in.

An explicit `ApiSettings.timezone` wins. Otherwise the zone the host's clock is set to: TZ, then
/etc/localtime (macOS and Linux), then /etc/timezone. UTC when none of them names a zone.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FALLBACK = "UTC"


def valid_zone(name: str | None) -> str | None:
    """The name when it is an IANA zone this machine knows, else None."""
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def _from_localtime() -> str | None:
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    marker = "zoneinfo/"
    if marker not in target:
        return None
    name = target.split(marker, 1)[1]
    return name.removeprefix("posix/")


def _from_etc_timezone() -> str | None:
    try:
        return Path("/etc/timezone").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def host_zone() -> str:
    """The zone the host's clock is set to, or UTC."""
    for candidate in (os.environ.get("TZ", "").lstrip(":"), _from_localtime(), _from_etc_timezone()):
        name = valid_zone(candidate)
        if name:
            return name
    return FALLBACK


def node_zone(configured: str | None = None) -> str:
    """The configured zone (validated), else the host's."""
    if configured is None:
        return host_zone()
    name = valid_zone(configured)
    if name is None:
        raise ValueError(f"unknown timezone {configured!r}")
    return name
