"""Screenshots kept after an agent's commit (opt-in, per lane): what the page looked like right after
the agent posted, sent or submitted something, for the replay.

Off by default. The owner turns it on for a lane, or a whole workspace (`agentos audit capture`); the
setting lives in `$AGENTOS_HOME/audit.db` with the share links. After a browser call from an agent
whose effect was a commit, on a lane that has it on, the node takes one screenshot of the lane's tab,
keeps it under `$AGENTOS_HOME/audit/captures/` named by its SHA-256, and writes a child row in the
action log holding `{sha256, url, title, bytes, format}`. The hash chain covers that row, so the image
is bound to the log without being stored in it, and the replay checks the file against the hash.

Never while the vault's login window is open on the lane (a login form, a one-time code field), never
for a person's own call, and never for anything but a commit. No new encoder: the image is kept as
the browser returns it (PNG). Files go with trace retention (older than the log keeps rows) and past a
total size cap, oldest first.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any

from agentos.policy.capture_files import capture_file, prune
from agentos.policy.store import DB

log = logging.getLogger("agentos.audit")

CAPTURE_KIND = "screenshot_kept"
MAX_ONE_BYTES = 8 * 1024 * 1024       # one screenshot bigger than this is not kept
PRUNE_EVERY_S = 300                   # the folder is swept at most this often, off the event loop

SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_lanes (
  pattern TEXT PRIMARY KEY, set_by TEXT NOT NULL, set_at REAL NOT NULL
);
"""


def _valid_pattern(pattern: str) -> str:
    from agentos.core.errors import InvalidRequest

    p = (pattern or "").strip()
    ws, _, name = p.partition("/")
    if not ws or not name or "/" in name or (("*" in name) and name != "*") or "*" in ws:
        raise InvalidRequest("name a lane (workspace/name) or a whole workspace (workspace/*)")
    return p


class CaptureSettings:
    """Which lanes keep a screenshot after an agent's commit: lane ids and `workspace/*`."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self._db: DB | None = None

    @property
    def db(self) -> DB:
        if self._db is None:
            self._db = DB(self.home / "audit.db", SCHEMA)
        return self._db

    def patterns(self) -> list[str]:
        if not (self.home / "audit.db").exists():
            return []  # nothing turned on yet: never create the file just to read it
        return [r["pattern"] for r in self.db.all("SELECT pattern FROM capture_lanes ORDER BY pattern")]

    def enabled(self, lane: str | None) -> bool:
        if not lane or "/" not in lane:
            return False
        ws = lane.split("/", 1)[0]
        return any(p == lane or p == f"{ws}/*" for p in self.patterns())

    def set(self, pattern: str, on: bool, *, by: str = "owner") -> list[str]:
        p = _valid_pattern(pattern)
        if on:
            self.db.execute("INSERT INTO capture_lanes (pattern, set_by, set_at) VALUES (?, ?, ?) "
                            "ON CONFLICT(pattern) DO NOTHING", (p, by, time.time()))
        else:
            self.db.execute("DELETE FROM capture_lanes WHERE pattern = ?", (p,))
        return self.patterns()


_settings: dict[Path, CaptureSettings] = {}


def settings_for(home: Path) -> CaptureSettings:
    key = Path(home).resolve()
    if key not in _settings:
        _settings[key] = CaptureSettings(key)
    return _settings[key]


def _login_window_open(page: Any, host: str) -> bool:
    """The vault's login window is open on this lane (agentos.node.egress guard): for this page's site,
    or for any site while one is open at all (a login that moved to another host). A lane with no guard
    has no login window. A guard that cannot be read is treated as open."""
    try:
        from agentos.node import egress

        guard = egress.guard_for(page)
    except Exception:  # noqa: BLE001
        return True
    if guard is None:
        return False
    try:
        if guard.login_open(host):
            return True
        windows = getattr(guard, "_login_until", None)
        if not isinstance(windows, dict):  # the guard changed shape: not knowing counts as open
            return True
        now = guard.clock()
        return any(now < until for until in windows.values())
    except Exception:  # noqa: BLE001
        return True


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


async def after_commit(ctx: Any, on: str, page: Any) -> dict | None:
    """Keep one screenshot of the lane's tab after an agent's commit, when the lane has it on. Returns
    what the row recorded, or None when nothing was kept. Never raises: the act is already done."""
    try:
        return await _after_commit(ctx, on, page)
    except Exception:
        log.warning("the screenshot after a commit on %s was not kept", on, exc_info=True)
        return None


async def _after_commit(ctx: Any, on: str, page: Any) -> dict | None:
    runtime = getattr(ctx, "runtime", None)
    principal = getattr(ctx, "principal", None)
    if getattr(ctx, "in_person", False) or getattr(principal, "kind", None) == "human":
        return None
    cfg = runtime.get("config") if runtime is not None and hasattr(runtime, "get") else None
    home = getattr(cfg, "home", None)
    if home is None or not settings_for(home).enabled(on):
        return None
    url = str(getattr(page, "url", "") or "")
    if _login_window_open(page, _host(url)):
        return None
    interaction = getattr(runtime, "interaction", None)
    if interaction is None:
        return None
    data = await interaction.screenshot(page)
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_ONE_BYTES:
        return None
    data = bytes(data)
    fmt = "png" if data.startswith(b"\x89PNG") else "webp" if data[8:12] == b"WEBP" else \
        "jpeg" if data.startswith(b"\xff\xd8\xff") else None
    if fmt is None:
        return None
    title = ""
    get_title = getattr(page, "title", None)
    if callable(get_title):
        try:
            got = get_title()
            title = str(await got if hasattr(got, "__await__") else got)[:300]
        except Exception:  # noqa: BLE001
            title = ""
    sha = "sha256:" + hashlib.sha256(data).hexdigest()
    path = capture_file(home, sha, fmt)
    if path is None:
        return None
    for d in (path.parent.parent, path.parent):  # screenshots of the account: this user only
        d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    if not path.exists():
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    else:
        os.utime(path)  # the same image again: it counts from now for retention
    record = {"sha256": sha, "url": url[:2000], "title": title, "bytes": len(data), "format": fmt}
    ctx.trace(CAPTURE_KIND, lane=on, status="ok", summary="kept a screenshot of the page after a commit", **record)
    traces = getattr(getattr(ctx, "policy", None), "traces", None)
    await _prune_soon(home, retention_days=getattr(traces, "retention_days", 90))
    return record


_last_prune: dict[Path, float] = {}


async def _prune_soon(home: Path, *, retention_days: float) -> None:
    """Sweep the folder in a worker thread, at most every PRUNE_EVERY_S per home: a sweep reads every
    file's size, and must not hold the event loop after each commit."""
    key = Path(home)
    now = time.monotonic()
    if now - _last_prune.get(key, -PRUNE_EVERY_S) < PRUNE_EVERY_S:
        return
    _last_prune[key] = now
    await asyncio.to_thread(prune, home, retention_days=retention_days)
