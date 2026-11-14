"""Members: the people other than the owner who sign in to the panel.

A member is a person with roles per workspace (viewer, auditor, approver, operator, admin). They
join through a one-time invite (single use, hashed at rest, 24 hours), register a passkey and
from then on sign in with it. Their authority is never a token: each scope they hold roles in
becomes one member grant in the policy (`PolicyService.issue_member_grant`), re-minted on every
role change and revoked together with `revoke_member`. The panel session keeps only the member
id; the grants are looked up again on every request, so a removal or a downgrade takes effect
at the next one.

This module keeps the panel's side in `panel.db` (members, memberships, invites, passkeys and
WebAuthn ceremonies) and `Members` ties it to the policy. What each role may do in the panel is
`CAPABILITIES`; the node enforces the grant underneath either way.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from agentos.api.errors import ApiError
from agentos.core import clock
from agentos.core.errors import InvalidRequest, NotFound

log = logging.getLogger("agentos.api")

OWNER_ID = "owner"
MEMBER_ROLES = ("admin", "approver", "operator", "viewer", "auditor")  # agentos.policy.members.MEMBER_ROLES
INVITE_TTL_S = 24 * 3600
CEREMONY_TTL_S = 300
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
SCOPE_RE = re.compile(r"^(\*|[a-z0-9][a-z0-9-]{0,30})$")
REFRESH_BEFORE_S = 30 * 86400  # a membership without an end gets a fresh grant when this little is left

# What each role may do in the panel, per workspace (section 4.3). Deciding is also checked by the
# node against the member grant's `approve`; watching needs the grant's `control`.
#   see        lanes, their states and signals, runs, flows, Activity of the workspace
#   approvals  approvals and questions and their previews
#   decide     approve and deny (the grant's approve special says which effects a yes covers); a no is a decision too
#   answer     answer agent questions (human_ask)
#   operate    open and close lanes, run flows (routed like an agent unless they may also approve)
#   connect    connect their own agent (an agent grant in the workspace, up to commit)
#   standing   standing approvals; schedules: turn on and off, delete
#   vault      add or remove vault entries for the workspace's lanes (never read them)
#   members    invite and remove members of the workspace (roles up to admin)
CAPABILITIES: dict[str, frozenset[str]] = {
    "viewer": frozenset({"see", "approvals"}),
    "auditor": frozenset(),  # evidence only (phase 3): nothing in the panel yet
    "approver": frozenset({"see", "approvals", "decide", "answer"}),
    "operator": frozenset({"see", "approvals", "answer", "operate", "connect"}),
    "admin": frozenset({"see", "approvals", "decide", "answer", "operate", "connect", "standing", "vault",
                        "members"}),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY,
  handle TEXT NOT NULL,
  display_name TEXT, email TEXT,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  grant_rid TEXT NOT NULL,
  state TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at REAL NOT NULL, removed_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS members_handle ON members(handle) WHERE state != 'removed';
CREATE TABLE IF NOT EXISTS memberships (
  member_id TEXT NOT NULL, scope TEXT NOT NULL, role TEXT NOT NULL,
  options TEXT NOT NULL DEFAULT '{}',
  expires_at REAL, granted_by TEXT NOT NULL, granted_at REAL NOT NULL,
  PRIMARY KEY (member_id, scope, role)
);
CREATE TABLE IF NOT EXISTS invites (
  id TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL,
  handle TEXT NOT NULL, display_name TEXT, email TEXT, roles TEXT NOT NULL,
  member_ttl_s INTEGER, expires_at REAL NOT NULL, used_at REAL, member_id TEXT, revoked_at REAL,
  created_by TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS credentials (
  id TEXT PRIMARY KEY, member_id TEXT NOT NULL, public_key BLOB NOT NULL,
  sign_count INTEGER NOT NULL, label TEXT, aaguid TEXT, created_at REAL NOT NULL, last_used_at REAL
);
CREATE INDEX IF NOT EXISTS credentials_member ON credentials(member_id);
CREATE TABLE IF NOT EXISTS ceremonies (
  challenge_hash TEXT PRIMARY KEY, purpose TEXT NOT NULL,
  invite_id TEXT, member_id TEXT, session_hash TEXT, approval_id TEXT,
  created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL
);
CREATE TABLE IF NOT EXISTS member_notify (
  member_id TEXT PRIMARY KEY, prefs TEXT NOT NULL, updated_at REAL NOT NULL
);
"""


def _hash(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_member_id() -> str:
    return "m_" + base64.b32encode(secrets.token_bytes(10)).decode().lower().rstrip("=")


@dataclass(frozen=True)
class Role:
    scope: str             # "*" (every workspace) or a workspace id
    role: str
    options: dict = field(default_factory=dict)
    expires_at: float | None = None

    def public(self) -> dict:
        return {"scope": self.scope, "role": self.role, "options": self.options,
                "expires_at": clock.iso_or_none(self.expires_at)}


@dataclass(frozen=True)
class Member:
    id: str
    handle: str
    display_name: str | None
    email: str | None
    timezone: str
    state: str             # invited | active | suspended | removed
    created_by: str
    created_at: float
    removed_at: float | None = None

    @property
    def owner(self) -> bool:
        return self.id == OWNER_ID

    def public(self) -> dict:
        return {"id": self.id, "handle": self.handle, "display_name": self.display_name, "email": self.email,
                "timezone": self.timezone, "state": self.state, "owner": self.owner,
                "created_by": self.created_by, "created_at": clock.iso_or_none(self.created_at),
                "removed_at": clock.iso_or_none(self.removed_at)}


@dataclass(frozen=True)
class Credential:
    id: str                # base64url credential id
    member_id: str
    public_key: bytes
    sign_count: int
    label: str | None
    created_at: float
    last_used_at: float | None

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "created_at": clock.iso_or_none(self.created_at),
                "last_used_at": clock.iso_or_none(self.last_used_at)}


@dataclass(frozen=True)
class Invite:
    id: str
    handle: str
    display_name: str | None
    email: str | None
    roles: list[Role]
    member_ttl_s: int | None
    expires_at: float
    used_at: float | None
    member_id: str | None
    revoked_at: float | None
    created_by: str
    created_at: float

    @property
    def state(self) -> str:
        if self.revoked_at is not None:
            return "revoked"
        if self.used_at is not None:
            return "used"
        return "expired" if self.expires_at <= time.time() else "pending"

    def public(self) -> dict:
        return {"id": self.id, "handle": self.handle, "display_name": self.display_name, "email": self.email,
                "roles": [r.public() for r in self.roles], "member_ttl_s": self.member_ttl_s,
                "expires_at": clock.iso_or_none(self.expires_at), "state": self.state, "used_at": clock.iso_or_none(self.used_at),
                "member_id": self.member_id, "created_by": self.created_by, "created_at": clock.iso_or_none(self.created_at)}


@dataclass(frozen=True)
class Ceremony:
    challenge: bytes
    purpose: str           # register | sign_in | step_up | add_passkey
    invite_id: str | None
    member_id: str | None
    session_hash: str | None
    approval_id: str | None


def capabilities(roles: list[str] | set[str]) -> frozenset[str]:
    out: set[str] = set()
    for role in roles:
        out |= CAPABILITIES.get(role, frozenset())
    return frozenset(out)


def validate_role(scope: str, role: str, options: Any) -> Role:
    if not isinstance(scope, str) or not SCOPE_RE.match(scope):
        raise InvalidRequest(f"scope is a workspace id or \"*\" (every workspace), not {str(scope)[:40]!r}")
    if role not in MEMBER_ROLES:
        raise InvalidRequest(f"role is one of {', '.join(MEMBER_ROLES)}; the owner role is not given to members")
    if not isinstance(options, dict):
        raise InvalidRequest("options is a JSON object")
    try:
        json.dumps(options)
    except (TypeError, ValueError):
        raise InvalidRequest("options must be plain JSON") from None
    return Role(scope=scope, role=role, options=dict(options))


def check_roles_with_policy(roles: list[Role]) -> None:
    """Refuse options the policy would refuse when minting the grant (at invite time, not later)."""
    from agentos.policy.members import member_specials

    for scope, rows in group_by_scope(roles).items():
        member_specials([r.role for r in rows], merged_options(rows), scope)


def group_by_scope(roles: list[Role]) -> dict[str, list[Role]]:
    out: dict[str, list[Role]] = {}
    for r in roles:
        out.setdefault(r.scope, []).append(r)
    return out


def merged_options(rows: list[Role]) -> dict:
    """One options object for every role a member holds in one scope (the grant has one)."""
    out: dict = {}
    for r in rows:
        for key, value in r.options.items():
            if key == "effects" and "effects" in out:
                out["effects"] = list(dict.fromkeys([*out["effects"], *value]))
            elif key == "watch":
                out["watch"] = bool(out.get("watch")) or bool(value)
            else:
                out[key] = value
    return out


class MemberStore:
    """The panel's member tables in panel.db (writes through the PanelStore's connection for the
    calling thread, and its lock)."""

    def __init__(self, store: Any):
        self._store = store
        self._lock = store._lock
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT OR IGNORE INTO members (id, handle, display_name, grant_rid, state, created_by, created_at) "
                "VALUES ('owner', 'owner', 'Owner', 'owner', 'active', 'owner', ?)", (time.time(),))
            self._db.commit()

    @property
    def _db(self) -> sqlite3.Connection:
        return self._store._db

    # ---- members ----
    def member(self, member_id: str) -> Member | None:
        with self._lock:
            row = self._db.execute("SELECT id, handle, display_name, email, timezone, state, created_by, created_at, "
                                   "removed_at FROM members WHERE id = ?", (member_id,)).fetchone()
        return Member(*row) if row else None

    def require(self, member_id: str) -> Member:
        m = self.member(member_id)
        if m is None:
            raise NotFound(f"No member {member_id}.")
        return m

    def members(self, *, include_removed: bool = False) -> list[Member]:
        sql = ("SELECT id, handle, display_name, email, timezone, state, created_by, created_at, removed_at "
               "FROM members")
        if not include_removed:
            sql += " WHERE state != 'removed'"
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id = 'owner' DESC, created_at, id").fetchall()
        return [Member(*r) for r in rows]

    def set_display_name(self, member_id: str, name: str) -> None:
        with self._lock:
            self._db.execute("UPDATE members SET display_name = ? WHERE id = ?", (name, member_id))
            self._db.commit()

    def handle_taken(self, handle: str) -> bool:
        with self._lock:
            member = self._db.execute("SELECT 1 FROM members WHERE handle = ? AND state != 'removed'",
                                      (handle,)).fetchone()
            invite = self._db.execute("SELECT 1 FROM invites WHERE handle = ? AND used_at IS NULL AND revoked_at IS NULL "
                                      "AND expires_at > ?", (handle, time.time())).fetchone()
        return member is not None or invite is not None

    def set_state(self, member_id: str, state: str) -> None:
        with self._lock:
            self._db.execute("UPDATE members SET state = ?, removed_at = ? WHERE id = ? AND id != 'owner'",
                             (state, time.time() if state == "removed" else None, member_id))
            self._db.commit()

    def handles(self) -> dict[str, str]:
        """member id -> handle, removed members included (Activity names them long after)."""
        with self._lock:
            return dict(self._db.execute("SELECT id, handle FROM members").fetchall())

    # ---- memberships ----
    def roles(self, member_id: str, *, now: float | None = None, include_expired: bool = False) -> list[Role]:
        now = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute("SELECT scope, role, options, expires_at FROM memberships WHERE member_id = ? "
                                    "ORDER BY scope, role", (member_id,)).fetchall()
        out = [Role(scope=r[0], role=r[1], options=json.loads(r[2] or "{}"), expires_at=r[3]) for r in rows]
        return out if include_expired else [r for r in out if r.expires_at is None or r.expires_at > now]

    def set_roles(self, member_id: str, scope: str, roles: list[Role], *, by: str) -> None:
        """Replace the member's roles in one scope (empty: none left there)."""
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM memberships WHERE member_id = ? AND scope = ?", (member_id, scope))
            for r in roles:
                self._db.execute("INSERT INTO memberships (member_id, scope, role, options, expires_at, granted_by, "
                                 "granted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (member_id, scope, r.role, json.dumps(r.options, sort_keys=True), r.expires_at, by,
                                  now))
            self._db.commit()

    def drop_roles(self, member_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM memberships WHERE member_id = ?", (member_id,))
            self._db.commit()

    # ---- invites ----
    def add_invite(self, *, handle: str, display_name: str | None, email: str | None, roles: list[Role],
                   member_ttl_s: int | None, ttl_s: int, by: str) -> tuple[Invite, str]:
        code = secrets.token_urlsafe(24)
        invite_id = "inv_" + secrets.token_hex(8)
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO invites (id, code_hash, handle, display_name, email, roles, member_ttl_s, expires_at, "
                "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (invite_id, _hash(code), handle, display_name, email,
                 json.dumps([{"scope": r.scope, "role": r.role, "options": r.options} for r in roles]),
                 member_ttl_s, now + ttl_s, by, now))
            self._db.commit()
        return self.invite(invite_id), code  # type: ignore[return-value]

    _INVITE_COLS = ("id, handle, display_name, email, roles, member_ttl_s, expires_at, used_at, member_id, "
                    "revoked_at, created_by, created_at")

    def _invite(self, row: Any) -> Invite:
        roles = [Role(scope=r["scope"], role=r["role"], options=r.get("options") or {}) for r in json.loads(row[4])]
        return Invite(row[0], row[1], row[2], row[3], roles, *row[5:])

    def invite(self, invite_id: str) -> Invite | None:
        with self._lock:
            row = self._db.execute(f"SELECT {self._INVITE_COLS} FROM invites WHERE id = ?", (invite_id,)).fetchone()
        return self._invite(row) if row else None

    def invite_by_code(self, code: str) -> Invite | None:
        """A pending invite (unused, not revoked, not expired) for this code, else None."""
        with self._lock:
            row = self._db.execute(f"SELECT {self._INVITE_COLS} FROM invites WHERE code_hash = ? AND used_at IS NULL "
                                   "AND revoked_at IS NULL AND expires_at > ?", (_hash(code), time.time())).fetchone()
        return self._invite(row) if row else None

    def invites(self, *, pending_only: bool = True) -> list[Invite]:
        sql = f"SELECT {self._INVITE_COLS} FROM invites"
        params: tuple = ()
        if pending_only:
            sql += " WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at > ?"
            params = (time.time(),)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created_at DESC", params).fetchall()
        return [self._invite(r) for r in rows]

    def use_invite(self, invite_id: str, member_id: str) -> bool:
        """Consume an invite once. False when it was used, revoked or expired meanwhile."""
        now = time.time()
        with self._lock:
            cur = self._db.execute("UPDATE invites SET used_at = ?, member_id = ? WHERE id = ? AND used_at IS NULL "
                                   "AND revoked_at IS NULL AND expires_at > ?", (now, member_id, invite_id, now))
            self._db.commit()
            return cur.rowcount == 1

    def revoke_invite(self, invite_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE invites SET revoked_at = ? WHERE id = ? AND used_at IS NULL "
                                   "AND revoked_at IS NULL", (time.time(), invite_id))
            self._db.commit()
            return cur.rowcount == 1

    def create_member(self, *, member_id: str, handle: str, display_name: str | None, email: str | None,
                      timezone: str, by: str, roles: list[Role], member_ttl_s: int | None) -> Member:
        now = time.time()
        exp = now + member_ttl_s if member_ttl_s else None
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO members (id, handle, display_name, email, timezone, grant_rid, state, created_by, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                    (member_id, handle, display_name, email, timezone, f"member:{member_id}", by, now))
            except sqlite3.IntegrityError:
                self._db.rollback()
                raise ApiError("conflict", f"The handle {handle} is taken. Ask for a new invite.") from None
            for r in roles:
                self._db.execute("INSERT OR REPLACE INTO memberships (member_id, scope, role, options, expires_at, "
                                 "granted_by, granted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (member_id, r.scope, r.role, json.dumps(r.options, sort_keys=True), exp, by, now))
            self._db.commit()
        return self.require(member_id)

    # ---- passkeys ----
    def add_credential(self, *, member_id: str, cred_id: bytes, public_key: bytes, sign_count: int,
                       label: str | None, aaguid: bytes) -> Credential:
        cid = b64url(cred_id)
        with self._lock:
            try:
                self._db.execute("INSERT INTO credentials (id, member_id, public_key, sign_count, label, aaguid, "
                                 "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (cid, member_id, public_key, sign_count, label, aaguid.hex(), time.time()))
                self._db.commit()
            except sqlite3.IntegrityError:
                self._db.rollback()
                raise ApiError("conflict", "This passkey is already registered.") from None
        return self.credential(cid)  # type: ignore[return-value]

    _CRED_COLS = "id, member_id, public_key, sign_count, label, created_at, last_used_at"

    def credential(self, cred_id: str) -> Credential | None:
        with self._lock:
            row = self._db.execute(f"SELECT {self._CRED_COLS} FROM credentials WHERE id = ?", (cred_id,)).fetchone()
        return Credential(*row) if row else None

    def credentials(self, member_id: str) -> list[Credential]:
        with self._lock:
            rows = self._db.execute(f"SELECT {self._CRED_COLS} FROM credentials WHERE member_id = ? "
                                    "ORDER BY created_at", (member_id,)).fetchall()
        return [Credential(*r) for r in rows]

    def used_credential(self, cred_id: str, sign_count: int) -> None:
        with self._lock:
            self._db.execute("UPDATE credentials SET sign_count = ?, last_used_at = ? WHERE id = ?",
                             (sign_count, time.time(), cred_id))
            self._db.commit()

    def delete_credential(self, member_id: str, cred_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM credentials WHERE id = ? AND member_id = ?", (cred_id, member_id))
            self._db.commit()
            return cur.rowcount == 1

    def delete_credentials(self, member_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM credentials WHERE member_id = ?", (member_id,))
            self._db.commit()

    # ---- WebAuthn ceremonies (one challenge, one use, five minutes) ----
    def begin(self, challenge: bytes, purpose: str, *, invite_id: str | None = None, member_id: str | None = None,
              session_hash: str | None = None, approval_id: str | None = None) -> None:
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM ceremonies WHERE expires_at < ?", (now - 3600,))
            self._db.execute("INSERT INTO ceremonies (challenge_hash, purpose, invite_id, member_id, session_hash, "
                             "approval_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (_hash(challenge), purpose, invite_id, member_id, session_hash, approval_id, now,
                              now + CEREMONY_TTL_S))
            self._db.commit()

    def finish(self, challenge: bytes, purpose: str) -> Ceremony | None:
        """Consume the ceremony this challenge started, once. None: unknown, used, expired or another kind."""
        now = time.time()
        h = _hash(challenge)
        with self._lock:
            row = self._db.execute("SELECT invite_id, member_id, session_hash, approval_id FROM ceremonies "
                                   "WHERE challenge_hash = ? AND purpose = ? AND used_at IS NULL AND expires_at > ?",
                                   (h, purpose, now)).fetchone()
            if row is None:
                return None
            cur = self._db.execute("UPDATE ceremonies SET used_at = ? WHERE challenge_hash = ? AND used_at IS NULL",
                                   (now, h))
            self._db.commit()
            if cur.rowcount != 1:
                return None
        return Ceremony(challenge, purpose, *row)

    # ---- notification preferences (until the notifier keeps them itself) ----
    def notify_prefs(self, member_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT prefs FROM member_notify WHERE member_id = ?", (member_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_notify_prefs(self, member_id: str, prefs: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO member_notify (member_id, prefs, updated_at) VALUES (?, ?, ?)",
                             (member_id, json.dumps(prefs, sort_keys=True), time.time()))
            self._db.commit()

    def drop_notify_prefs(self, member_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM member_notify WHERE member_id = ?", (member_id,))
            self._db.commit()


class Members:
    """Members tied to the policy: memberships in panel.db, member grants in the policy."""

    def __init__(self, store: MemberStore, runtime: Any):
        self.store = store
        self.runtime = runtime

    @property
    def policy(self) -> Any:
        policy = self.runtime.get("policy")
        if policy is None or not callable(getattr(policy, "issue_member_grant", None)):
            raise ApiError("unavailable", "Members need the policy service on this node.")
        return policy

    @property
    def enabled(self) -> bool:
        return callable(getattr(self.runtime.get("policy"), "issue_member_grant", None))

    # ---- grants ----
    def grants(self, member_id: str) -> dict[str, Any]:
        """scope -> the member's live grant there (newest when there are several)."""
        out: dict[str, Any] = {}
        for g in self.policy.member_grants(member_id):
            ws = (g.blocks[0].workspaces or [None])[0]
            if ws is not None and ws not in out:
                out[ws] = g
        return out

    def snapshot(self, member_id: str) -> tuple[frozenset, frozenset] | None:
        """What a member may do now, read only: their roles as (scope, role) and their live grant per scope
        (by grant id). None once they are no longer an active member (sync refuses them too)."""
        if not self.enabled:
            return None
        member = self.store.member(member_id)
        if member is None or member.state != "active":
            return None
        roles = frozenset((r.scope, r.role) for r in self.store.roles(member_id))
        return roles, frozenset((scope, g.id) for scope, g in self.grants(member_id).items())

    def sync(self, member_id: str, *, scopes: set[str] | None = None, by: str = "owner",
             force: bool = False) -> dict[str, Any]:
        """Make the member's grants match their memberships: one grant per scope with roles (re-minted
        when `force`, missing, or close to its end while the membership goes on), none elsewhere.
        Returns the live grants afterwards (scope -> grant)."""
        member = self.store.require(member_id)
        if member.state != "active":
            return {}
        now = time.time()
        wanted = group_by_scope(self.store.roles(member_id, now=now))
        live = self.grants(member_id)
        changed = False
        for scope in (scopes if scopes is not None else set(wanted) | set(live)):
            rows = wanted.get(scope)
            g = live.get(scope)
            if not rows:
                if g is not None:
                    self.policy.revoke(g.id, by=by, reason="member role removed")
                    changed = True
                continue
            ends = [r.expires_at for r in rows if r.expires_at is not None]
            exp = min(ends) if ends else None
            g_exp = getattr(g, "expires_at", None) if g is not None else None
            stale = g is not None and g_exp is not None and g_exp < now + REFRESH_BEFORE_S and (
                exp is None or exp > g_exp + 60)
            if force or g is None or stale:
                self.policy.issue_member_grant(member_id, scope, [r.role for r in rows], merged_options(rows),
                                               exp, by=by, handle=member.handle, replace=True)
                changed = True
        return self.grants(member_id) if changed else live

    def remove(self, member_id: str, *, by: str) -> dict:
        member = self.store.require(member_id)
        if member.owner:
            raise InvalidRequest("The owner cannot be removed.")
        result: dict = {}
        if member.state != "removed":
            result = self.policy.revoke_member(member_id, by=by, reason="member removed")
        self.store.set_state(member_id, "removed")
        self.store.drop_roles(member_id)
        self.store.delete_credentials(member_id)
        notifier = self.runtime.get("notifier")
        if callable(getattr(notifier, "remove_member", None)):
            try:
                notifier.remove_member(member_id)
            except Exception:  # noqa: BLE001  (the grants are gone either way; the notifier is told best effort)
                log.warning("the notifier could not retire member %s's channel", member_id)
        self.store.drop_notify_prefs(member_id)
        return {"member_id": member_id, "removed": True, "grants": result.get("grants", []),
                "agent_grants": result.get("agent_grants", [])}
