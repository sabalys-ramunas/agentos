"""Panel auth: one-time login codes, session cookies, CSRF, bearer tokens, login rate limit.

Login codes and sessions live in `$OPENAJAR_HOME/panel.db` (SQLite, WAL), so
`openajar panel login` (a separate process) can mint a code the daemon accepts. Only
SHA-256 hashes of codes and session ids are stored, so a copy of the file does not
open the panel.

A session is the owner's (a one-time code or the owner's passkey) or a member's (a passkey,
openajar.api.members). A member's session has an idle limit (12 hours) on top of its absolute
one (7 days), and remembers when the person last proved themselves with their passkey
(`last_verified_at`, for step-up) and, for a single pay or irreversible decision, which
approval that fresh check was for (`step_up_for`, used once).

The owner's device credential (`POST /api/session/device`, made by `openajar connect` with the
node token) is an owner session too, with `via = "device"`, the computer's name as its device
label and a long life (90 days by default). The CLI tools on that computer send it as the
session cookie with the CSRF header; it is a session id, never a bearer token, so /mcp refuses it.
It is never a person (openajar.core.presence: DEVICE), so it does not approve.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openajar.api.errors import ApiError
from openajar.core import clock
from openajar.core.models import Principal
from openajar.core.presence import Presence
from openajar.core.sqlite import SqliteStore, _is_busy

SESSION_COOKIE = "agentos_session"
CSRF_HEADER = "x-agentos-csrf"
# the node's CLI: a one-time code it wrote into panel.db, sent beside the node token, makes that one
# request a person's (openajar.commands.base.operator_presence; docs/THREAT-MODEL.md)
PRESENCE_HEADER = "x-agentos-presence"
CODE_TTL_SECONDS = 600
MEMBER_IDLE_SECONDS = 12 * 3600
MEMBER_SESSION_SECONDS = 7 * 24 * 3600
OWNER = Principal(kind="human", id="owner", owner="owner", grant_id="owner")
# the node's own token as a bearer: the owner's authority, but not a person (an agent may hold it)
NODE = Principal(kind="node", id="owner", owner="owner", grant_id="owner")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS login_codes (
  code_hash TEXT PRIMARY KEY,
  expires_at REAL NOT NULL,
  used_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
  id_hash TEXT PRIMARY KEY,
  csrf TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  color TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS node_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""
# columns added to sessions for members and passkeys (older panel.db files get them on open)
_SESSION_COLUMNS = (("member_id", "TEXT"), ("via", "TEXT"), ("credential_id", "TEXT"), ("device_label", "TEXT"),
                    ("last_verified_at", "REAL"), ("last_seen_at", "REAL"), ("idle_s", "INTEGER"),
                    ("step_up_for", "TEXT"), ("step_up_at", "REAL"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Session:
    csrf: str
    principal_id: str
    expires_at: float
    member_id: str | None = None        # None or "owner": the owner; else a member id (m_...)
    via: str = "code"                   # how they signed in: code | passkey | device (openajar connect)
    credential_id: str | None = None    # the passkey they signed in with
    device_label: str | None = None
    created_at: float = 0.0
    last_verified_at: float | None = None
    last_seen_at: float | None = None
    idle_s: int | None = None
    step_up_for: str | None = None
    step_up_at: float | None = None
    id_hash: str = ""

    @property
    def is_member(self) -> bool:
        return self.member_id not in (None, "owner")

    def public(self) -> dict:
        return {"id": self.id_hash[:16], "via": self.via, "device_label": self.device_label,
                "created_at": clock.iso_or_none(self.created_at), "last_seen_at": clock.iso_or_none(self.last_seen_at or self.created_at),
                "last_verified_at": clock.iso_or_none(self.last_verified_at), "expires_at": clock.iso_or_none(self.expires_at),
                "credential_id": self.credential_id}


@dataclass(frozen=True)
class LoginLink:
    code: str
    url: str
    expires_at: str


class _Guard:
    """`with store._lock:` around every statement group of PanelStore and MemberStore. On a file
    each thread has its own connection, so there is nothing to share; but a group that fails
    (a conflict, a lock timeout) must never leave its implicit transaction open on the thread's
    connection, where it would keep the write lock or a stale view until that thread writes
    again. With no path, it is also the lock of the one shared connection."""

    def __init__(self, store: PanelStore, lock: threading.Lock | None = None):
        self._store = store
        self._inner = lock

    def __enter__(self) -> None:
        if self._inner is not None:
            self._inner.acquire()

    def __exit__(self, exc_type: object, *exc: object) -> None:
        try:
            if exc_type is not None:
                conn = self._store._conn()
                if conn.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        conn.rollback()
        finally:
            if self._inner is not None:
                self._inner.release()


class PanelStore(SqliteStore):
    """Small SQLite store owned by the API: login codes, sessions, workspace names and colors.

    On a file, each thread keeps its own connection (openajar.core.sqlite), in the same implicit
    transactions as always (each write ends with commit()), so a write on the store's worker
    thread waiting for the CLI's lock never holds up a session read on the event loop. On the
    event loop, a session's touch and the removal of an expired one never wait for a lock; the
    writes a person waits for (a sign-in, a sign-out, a step-up) go through
    `await store.run(...)` and are committed before the call returns. With no path (tests, the
    demo) it is one connection in memory behind a lock."""

    def __init__(self, path: Path | None):
        # told when sessions are deleted, so live sockets opened on them close (openajar.core.live)
        self.on_ended: Callable[..., None] | None = None
        self._memory: sqlite3.Connection | None = None
        self._lock: Any = _Guard(self)
        if path is None:
            self._memory = sqlite3.connect(":memory:", check_same_thread=False)
            self._lock = _Guard(self, threading.Lock())
        super().__init__(path if path is not None else Path(":memory:"), timeout=5.0, private=False)
        if path is not None:
            try:
                path.chmod(0o600)
            except OSError:
                pass
        self._db.executescript(_SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(sessions)")}
        for name, kind in _SESSION_COLUMNS:
            if name not in cols:
                self._db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {kind}")
        self._db.commit()

    def _conn(self) -> sqlite3.Connection:
        if self._memory is not None:
            return self._memory
        conn = super()._conn()
        if conn.isolation_level is None:  # the base opens in autocommit: this store commits itself
            conn.isolation_level = ""
        return conn

    @property
    def _db(self) -> sqlite3.Connection:
        """This thread's connection (MemberStore writes through it too)."""
        return self._conn()

    def _nowait(self, sql: str, args: tuple) -> None:
        """A write nothing waits for (a touch, an expired row), done on the event loop only when
        no other process holds the lock: else left for the next request (or the next sign-in)."""
        conn = self._db
        if self._memory is not None or not _on_loop():
            conn.execute(sql, args)
            conn.commit()
            return
        conn.execute("PRAGMA busy_timeout=0")
        try:
            conn.execute(sql, args)
            conn.commit()
        except sqlite3.OperationalError as e:
            conn.rollback()
            if not _is_busy(e):
                raise
        finally:
            conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")

    @classmethod
    def for_home(cls, home: Path | None) -> PanelStore:
        return cls(home / "panel.db" if home else None)

    # ---- login codes ----
    def add_code(self, code: str, ttl_seconds: int = CODE_TTL_SECONDS) -> float:
        expires = time.time() + ttl_seconds
        with self._lock:
            self._db.execute("DELETE FROM login_codes WHERE expires_at < ?", (time.time() - 86400,))
            self._db.execute("INSERT INTO login_codes (code_hash, expires_at) VALUES (?, ?)",
                             (_hash(code), expires))
            self._db.commit()
        return expires

    def use_code(self, code: str) -> bool:
        """Consume a code once. False when unknown, used, or expired."""
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "UPDATE login_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
                (now, _hash(code), now))
            self._db.commit()
            return cur.rowcount == 1

    # ---- sessions ----
    def create_session(self, principal_id: str, ttl_seconds: int, *, member_id: str | None = None,
                       via: str = "code", credential_id: str | None = None, device_label: str | None = None,
                       idle_seconds: int | None = None) -> tuple[str, Session]:
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = time.time()
        verified = now if via == "passkey" else None
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            self._db.execute(
                "INSERT INTO sessions (id_hash, csrf, principal_id, created_at, expires_at, member_id, via, "
                "credential_id, device_label, last_verified_at, last_seen_at, idle_s) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_hash(sid), csrf, principal_id, now, now + ttl_seconds, member_id, via, credential_id,
                 (device_label or "")[:80] or None, verified, now, idle_seconds))
            self._db.commit()
        return sid, self.get_session(sid)  # type: ignore[return-value]

    _SESSION_COLS = ("csrf, principal_id, expires_at, member_id, via, credential_id, device_label, created_at, "
                     "last_verified_at, last_seen_at, idle_s, step_up_for, step_up_at, id_hash")

    @staticmethod
    def _session(row: Any) -> Session:
        return Session(csrf=row[0], principal_id=row[1], expires_at=row[2], member_id=row[3], via=row[4] or "code",
                       credential_id=row[5], device_label=row[6], created_at=row[7], last_verified_at=row[8],
                       last_seen_at=row[9], idle_s=row[10], step_up_for=row[11], step_up_at=row[12],
                       id_hash=row[13])

    def get_session(self, sid: str, *, touch: bool = True) -> Session | None:
        """The live session for this cookie value, else None (expired, idle too long, deleted).
        Reading it counts as activity for the idle limit, unless `touch` is False (a live socket's
        recheck, which must not keep an idle session alive)."""
        now = time.time()
        with self._lock:
            row = self._db.execute(f"SELECT {self._SESSION_COLS} FROM sessions WHERE id_hash = ?",
                                   (_hash(sid),)).fetchone()
            if not row:
                return None
            session = self._session(row)
            if session.expires_at <= now or (session.idle_s and (session.last_seen_at or session.created_at)
                                             + session.idle_s <= now):
                self._nowait("DELETE FROM sessions WHERE id_hash = ?", (session.id_hash,))  # gone either way
                return None
            if touch and (session.last_seen_at is None or now - session.last_seen_at > 30):
                self._nowait("UPDATE sessions SET last_seen_at = ? WHERE id_hash = ?", (now, session.id_hash))
        return session

    def delete_session(self, sid: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE id_hash = ?", (_hash(sid),))
            self._db.commit()
        self._ended(sid=sid)

    def _ended(self, **keys: str) -> None:
        if self.on_ended is not None:
            try:
                self.on_ended(**keys)
            except Exception:
                logging.getLogger("openajar.api").exception("closing live sockets after a sign-out failed")

    def sessions_of(self, member_id: str) -> list[Session]:
        now = time.time()
        with self._lock:
            rows = self._db.execute(f"SELECT {self._SESSION_COLS} FROM sessions WHERE member_id = ? AND expires_at > ? "
                                    "ORDER BY created_at", (member_id, now)).fetchall()
        return [s for s in map(self._session, rows)
                if not (s.idle_s and (s.last_seen_at or s.created_at) + s.idle_s <= now)]

    def delete_sessions(self, *, member_id: str, credential_id: str | None = None,
                        session_prefix: str | None = None) -> int:
        """End a member's sessions: all, the ones signed in with one passkey, or one by its public id."""
        sql, params = "DELETE FROM sessions WHERE member_id = ?", [member_id]
        if credential_id is not None:
            sql += " AND credential_id = ?"
            params.append(credential_id)
        if session_prefix is not None:
            sql += " AND substr(id_hash, 1, 16) = ?"
            params.append(session_prefix)
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
        if cur.rowcount:  # which sessions is not known here: the member's sockets are rechecked
            self._ended(member_id=member_id, partial="1" if credential_id or session_prefix else "")
        return cur.rowcount

    def delete_device_sessions(self) -> int:
        """End every device credential (`via = "device"`): each was made with the node token, so a new
        node token ends them too (`openajar node rotate-token`)."""
        with self._lock:
            cur = self._db.execute("DELETE FROM sessions WHERE via = 'device'")
            self._db.commit()
            return cur.rowcount

    def verified(self, session: Session, *, approval_id: str | None = None) -> None:
        """The person just proved themselves with their passkey (step-up): for every decision in the
        next window, and, with `approval_id`, for that one pay or irreversible decision."""
        now = time.time()
        with self._lock:
            self._db.execute("UPDATE sessions SET last_verified_at = ?, step_up_for = ?, step_up_at = ? "
                             "WHERE id_hash = ?", (now, approval_id, now if approval_id else None, session.id_hash))
            self._db.commit()

    def use_step_up(self, session: Session, approval_id: str, *, max_age: float) -> bool:
        """Consume the fresh check made for this one approval. False when there is none, it was for
        another approval, it is older than `max_age` seconds, or it was used already."""
        now = time.time()
        with self._lock:
            cur = self._db.execute("UPDATE sessions SET step_up_for = NULL, step_up_at = NULL WHERE id_hash = ? "
                                   "AND step_up_for = ? AND step_up_at > ?", (session.id_hash, approval_id, now - max_age))
            self._db.commit()
            return cur.rowcount == 1

    # ---- workspaces (names and colors, until a workspaces service exists) ----
    def list_workspaces(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT id, name, color FROM workspaces ORDER BY created_at, id").fetchall()
        return [{"id": r[0], "name": r[1], "color": r[2]} for r in rows]

    # ---- the organisation this node is, as the app shows it (its name and first job) ----
    def setting(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM node_settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_setting(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self._db.execute("DELETE FROM node_settings WHERE key = ?", (key,))
            else:
                self._db.execute("INSERT OR REPLACE INTO node_settings (key, value) VALUES (?, ?)", (key, value))
            self._db.commit()

    def create_workspace(self, *, id: str, name: str, color: str | None) -> dict:
        with self._lock:
            try:
                self._db.execute("INSERT INTO workspaces (id, name, color, created_at) VALUES (?, ?, ?, ?)",
                                 (id, name, color, time.time()))
                self._db.commit()
            except sqlite3.IntegrityError:
                raise ApiError("conflict", f"Workspace {id} already exists.") from None
        return {"id": id, "name": name, "color": color}


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def tokens_equal(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode(), b.encode())


def origin_allowed(origin: str | None, host: str | None, extra: tuple[str, ...] = ()) -> bool:
    """Browsers send Origin on cross-origin and unsafe requests and on every WebSocket.
    No Origin (curl, the Mac app) is fine; a present one must be this host or allowlisted."""
    if not origin:
        return True
    if origin == "null":
        return False
    if origin.rstrip("/") in extra:
        return True
    parts = urlsplit(origin)
    return bool(host) and parts.netloc.lower() == (host or "").lower()


def new_login_code() -> str:
    return secrets.token_urlsafe(18)


def create_login_code(home: Path | None = None, *, base_url: str | None = None,
                      ttl_seconds: int = CODE_TTL_SECONDS) -> LoginLink:
    """What `openajar panel login` does: mint a one-time code in panel.db and return the URL.

    The code rides in the URL fragment (`/login#code=...`), so it never reaches a server log
    or a Referer header; the panel posts it to `POST /api/session` once.
    """
    from openajar.core.config import home_from_env, load_config

    home = home or home_from_env()
    if base_url is None:
        cfg = load_config(home)
        base_url = f"http://localhost:{cfg.mcp_port}"
    code = new_login_code()
    expires = PanelStore.for_home(home).add_code(code, ttl_seconds)
    return LoginLink(code=code, url=f"{base_url.rstrip('/')}/login#code={code}", expires_at=clock.iso_or_none(expires))


def presence_of(request: object, home: object = None) -> Presence:
    """The presence of a request the panel API authenticated (`request.state.auth.access`). A request
    with none came through the bare node-token check (no panel), so it is the node token, or a person
    when it carries a one-time code from the node's CLI that this node's panel.db (under `home`) holds."""
    auth = getattr(getattr(request, "state", None), "auth", None)
    presence = getattr(getattr(auth, "access", None), "presence", None)
    if isinstance(presence, Presence):
        return presence
    headers = getattr(request, "headers", None) or {}
    code = (headers.get(PRESENCE_HEADER) or "").strip()
    if code and home is not None and PanelStore.for_home(home).use_code(code):  # type: ignore[arg-type]
        return Presence.PERSON
    return Presence.NODE_TOKEN
