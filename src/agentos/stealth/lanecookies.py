"""Reading a cold lane's cookies for the lane check without touching the profile: copy cookies.sqlite
and its -wal until both were stable during the copy, then open only the copy. Values never leave
this module's callers in a report; only names, hosts and expiry times do.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COOKIE_COPY_TRIES = 5


def _stat(path: Path) -> tuple[int, int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns, st.st_ino


def host_matches(cookie_host: str, domains: Iterable[str]) -> bool:
    from agentos.node.sites import host_is

    host = cookie_host.lstrip(".")
    return any(host_is(host, d) for d in domains)


def read_cookie_db(profile: Path, domains: Iterable[str], *, now: float | None = None,
                   scratch: Path | None = None, tries: int = COOKIE_COPY_TRIES) -> list[dict[str, Any]] | None:
    """The unexpired cookies for `domains`, or None when they cannot be read (read_cookies says why)."""
    return read_cookies(profile, domains, now=now, scratch=scratch, tries=tries)[0]


BUSY = "the cookie file could not be copied while the browser wrote it"


DAMAGED = "the cookie file is damaged or is not a cookie database"


def read_cookies(profile: Path, domains: Iterable[str], *, now: float | None = None, scratch: Path | None = None,
                 tries: int = COOKIE_COPY_TRIES) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The unexpired cookies for `domains` (and their subdomains) in a profile's cookies.sqlite, as
    [{name, value, domain, path}], or None when they cannot be read. [] when the profile has no
    cookie file yet.

    The live file is never opened with SQLite (Firefox may hold it and must never meet a lock or a
    -shm of ours): cookies.sqlite and its -wal are copied with plain reads into a private temporary
    folder, again until neither changed during the copy, and only the copy is opened (it replays the
    WAL into the copy). Values stay in this process: callers report names and booleans only."""
    db = Path(profile) / "cookies.sqlite"
    if not db.exists():
        return [], None
    domains = list(domains)
    files = [db, db.with_name(db.name + "-wal")]
    now = time.time() if now is None else now
    why: str | None = BUSY
    for attempt in range(max(1, tries)):
        before = [_stat(f) for f in files]
        try:
            with tempfile.TemporaryDirectory(prefix="agentos-cookies-", dir=scratch) as tmp:
                for f in files:
                    if before[files.index(f)] is not None:
                        shutil.copyfile(f, Path(tmp) / f.name)
                if [_stat(f) for f in files] != before:
                    why = BUSY
                    time.sleep(0.05 * (attempt + 1))
                    continue
                conn = sqlite3.connect(Path(tmp) / db.name, timeout=1.0)
                try:
                    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        why = DAMAGED
                        continue
                    rows = conn.execute("SELECT name, value, host, path, expiry FROM moz_cookies").fetchall()
                finally:
                    conn.close()
        except sqlite3.Error:
            why = DAMAGED      # a stable copy that SQLite cannot read: the file itself is bad
            time.sleep(0.05 * (attempt + 1))
            continue
        except OSError as e:
            why = f"the cookie file could not be read ({type(e).__name__})"
            time.sleep(0.05 * (attempt + 1))
            continue
        out = []
        for name, value, host, path, expiry in rows:
            if not host_matches(str(host or ""), domains):
                continue
            exp = float(expiry or 0)
            if exp > 1e11:  # Firefox stores milliseconds since version 135, seconds before
                exp /= 1000.0
            if exp and exp < now:
                continue
            out.append({"name": str(name), "value": str(value or ""), "domain": str(host or ""), "path": path,
                        "expires": exp or -1})
        return out, None
    return None, why


def session_expiry(s: Any, cookies: Iterable[Mapping[str, Any]]) -> str | None:
    """When the first of a site's session cookies expires (ISO, UTC), or None when none is there or
    they all live for the browser session only. A time, never a value (agentos.keepalive plans by it)."""
    cookies = list(cookies)
    ends = []
    for c in s.session:
        found = c.find(cookies)
        exp = as_number(found.get("expires")) if found is not None else 0.0
        if exp > 0:
            ends.append(exp)
    return datetime.fromtimestamp(min(ends), UTC).isoformat(timespec="seconds") if ends else None


def as_number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
