"""Browser version drift in pinned fingerprints, and the keep-device refresh.

A lane's fingerprint is pinned on first launch and replayed on every launch after it, so the
site sees the same device. Camoufox does not rewrite a pinned config, so after a Camoufox
update the lane keeps announcing the old Firefox version on the new engine, and a UA that lags
the engine it runs on is itself a signal. `refresh` bumps only the browser version and keeps
every other pinned value. agentosd does the same on a lane's first launch after a Camoufox
upgrade (engine_camoufox.CamoufoxEngine.bump_firefox, recorded in the pin's firefox_updates and
the lane's events); `openajar lane refresh-fingerprint --keep-device` is for doing it by hand.

Exactly what refresh changes (VERSION_KEYS), and only when the key is present and a string:
  navigator.userAgent   the "rv:<N>.0" and "Firefox/<N>.0" tokens
  headers.User-Agent    the same two tokens
No pin holds navigator.buildID (Firefox reports a fixed one). Everything else stays byte for
byte: navigator.appVersion (Firefox puts no version in it),
platform, oscpu, hardwareConcurrency, screen and window geometry, fonts, voices, WebGL,
media devices, geolocation, timezone, locale, WebRTC and every other key.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openajar.core.layout import write_private

VERSION_KEYS = ("navigator.userAgent", "headers.User-Agent")
_FIREFOX_RE = re.compile(r"Firefox/(\d+)")
_RV_RE = re.compile(r"rv:(\d+)")
_BUMP_RES = (re.compile(r"(rv:)\d+(?:\.\d+)*"), re.compile(r"(Firefox/)\d+(?:\.\d+)*"))


def ua_major(user_agent: str | None) -> int | None:
    """The Firefox major version a UA announces ("Firefox/158.0" -> 158)."""
    if not isinstance(user_agent, str):
        return None
    m = _FIREFOX_RE.search(user_agent) or _RV_RE.search(user_agent)
    return int(m.group(1)) if m else None


def pinned_major(fp: dict) -> int | None:
    return ua_major(fp.get("navigator.userAgent"))


def browser_major(verstr: str | None) -> int | None:
    """Camoufox's version string ("158.0.1-beta.41") -> 158."""
    if not verstr:
        return None
    m = re.match(r"(\d+)", verstr)
    return int(m.group(1)) if m else None


def compare(installed: int | None, pinned: int | None) -> str:
    """"same", "older" (the pin lags the engine), "newer" (the pin is ahead) or "unknown"."""
    if installed is None or pinned is None:
        return "unknown"
    if pinned == installed:
        return "same"
    return "older" if pinned < installed else "newer"


def bump(fp: dict, major: int) -> tuple[dict, list[str]]:
    """A copy of fp with only the browser version fields set to `major`; and the keys changed."""
    out = dict(fp)
    changed: list[str] = []
    for key in VERSION_KEYS:
        value = out.get(key)
        if not isinstance(value, str):
            continue
        new = value
        for pattern in _BUMP_RES:
            new = pattern.sub(lambda m: f"{m.group(1)}{major}.0", new)
        if new != value:
            out[key] = new
            changed.append(key)
    return out, changed


@dataclass
class RefreshResult:
    lane: str
    old: int | None
    new: int
    changed: list[str] = field(default_factory=list)
    backup: Path | None = None
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {"lane": self.lane, "old_version": self.old, "new_version": self.new, "changed_keys": self.changed,
                "backup": str(self.backup) if self.backup else None, "dry_run": self.dry_run}


def refresh(path: Path, lane: str, major: int, *, dry_run: bool = False) -> RefreshResult:
    """Bump the browser version in one pinned fingerprint file (600, atomic), keeping a copy of
    the old file next to it (`fingerprint.json.v<old>.bak`, 600)."""
    path = Path(path)
    raw = path.read_bytes()
    fp = json.loads(raw)
    if not isinstance(fp, dict):
        raise TypeError(f"{path} does not hold a JSON object")
    old = pinned_major(fp)
    new_fp, changed = bump(fp, major)
    result = RefreshResult(lane=lane, old=old, new=major, changed=changed, dry_run=dry_run)
    if not changed or dry_run:
        return result
    backup = path.with_name(f"{path.name}.v{old if old is not None else 'unknown'}.bak")
    if not backup.exists():
        write_private(backup, raw)
    write_private(path, json.dumps(new_fp, indent=1).encode())
    result.backup = backup
    return result


# ---- the OS an identity claims (openajar.stealth.stealth judges the rest of a pin) ----------------

def ua_os(user_agent: Any) -> str | None:
    ua = str(user_agent or "")
    if "Windows NT" in ua:
        return "windows"
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "macos"
    if "Linux" in ua or "X11" in ua:
        return "linux"
    return None


def platform_os(platform: Any) -> str | None:
    p = str(platform or "")
    return "windows" if p.startswith("Win") else "macos" if p.startswith("Mac") else \
        "linux" if "Linux" in p else None
