"""Signed viewer links: HMAC-SHA256 over (lane, mode, expiry, nonce[, owner]) with
AGENTOS_HOME/secrets/viewer.key. Replacing the key revokes every link, open ones included.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agentos.core import clock
from agentos.core.config import NodeConfig
from agentos.core.models import parse_lane_id

log = logging.getLogger("agentos.viewer")


KEY_BYTES = 32
TOKEN_CONTEXT = b"agentos-view-v2\n"  # v2 signs the hard end: a v1 link no longer verifies
BLOB_CONTEXT = b"agentos-blob-v1\n"  # a signed link to one thing that is not a lane (an environment's proof)
MIN_TTL = 30
MAX_TTL = 24 * 3600
# The hard end of a link, from when it was first made: renewing moves its expiry, never this. A link
# anything but a person made (lane_view, a grant, the node token, the device credential) may be an
# agent's, so it is short; an owner link is made in person.
AGENT_LINK_MAX = 30 * 60
OWNER_LINK_MAX = 8 * 3600
MAX_TOKEN_CHARS = 512
OWNER_FLAG = "owner"


# ---- signed links -----------------------------------------------------------------------

class ViewMode(StrEnum):
    VIEW = "view"
    CONTROL = "control"


class TokenError(Exception):
    """A viewer token was refused. `message` is safe to show to the human."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ViewLink:
    lane_id: str
    mode: ViewMode
    expires_at: float  # unix seconds
    nonce: str
    token: str
    owner: bool = False  # made in person (an owner link): may do what only a person may
    hard_until: float = 0.0  # unix seconds: no renew moves the link past this

    @property
    def can_control(self) -> bool:
        return self.mode == ViewMode.CONTROL

    @property
    def expires_iso(self) -> str:
        return iso(self.expires_at)

    def path(self, base: str = "") -> str:
        workspace, name = parse_lane_id(self.lane_id)
        return f"{base}/view/{workspace}/{name}?t={quote(self.token)}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_or_create_key(path: Path) -> bytes:
    """The viewer key: 32 random bytes, chmod 600. Created atomically on first use (a
    concurrent creator never sees a half-written file); a short or empty key is replaced."""
    try:
        key = path.read_bytes()
        exists = True
    except FileNotFoundError:
        key, exists = b"", False
    if len(key) >= KEY_BYTES:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            log.warning("%s was readable by others (mode %o); fixing it to 600", path, mode)
            path.chmod(0o600)
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, secrets.token_bytes(KEY_BYTES))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if exists:  # present but empty or too short: replace it
            log.warning("%s is too short; replacing it (old viewer links stop working)", path)
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)  # fails if another process created it first
            except FileExistsError:
                pass
    finally:
        tmp.unlink(missing_ok=True)
    key = path.read_bytes()
    if len(key) < KEY_BYTES:
        raise RuntimeError(f"{path} is unusable; delete it and try again")
    return key


class ViewTokens:
    """Signs and checks viewer links for one node."""

    def __init__(self, key_path: Path, *, default_ttl: int = 900, clock: Callable[[], float] = time.time):
        self.key_path = Path(key_path)
        self.default_ttl = default_ttl
        self.clock = clock
        self._key: bytes | None = None
        self._key_stat: tuple[int, int] | None = None

    @classmethod
    def for_config(cls, config: NodeConfig, **kw: Any) -> ViewTokens:
        return cls(config.secrets_dir / "viewer.key", default_ttl=config.view_token_seconds, **kw)

    def _stat(self) -> tuple[int, int] | None:
        try:
            st = self.key_path.stat()
        except FileNotFoundError:
            return None
        return st.st_ino, st.st_mtime_ns

    @property
    def key(self) -> bytes:
        """The key, read again when the file changed: replacing or deleting it revokes every link,
        including those already open (the live sockets recheck them)."""
        stat = self._stat()
        if self._key is None or stat is None or stat != self._key_stat:
            self._key = load_or_create_key(self.key_path)
            self._key_stat = stat if stat is not None else self._stat()  # stat first: a newer key reloads
        return self._key

    def revoke(self) -> None:
        """Revoke every link: a new key replaces the old one. Open viewers close on their next recheck."""
        tmp = self.key_path.with_name(f".{self.key_path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_bytes(KEY_BYTES))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.key_path)
        self._key = None

    def signed(self, link: ViewLink) -> bool:
        """Does the link still carry this node's signature (not revoked)? Expiry is not checked here."""
        body, _, sig = link.token.partition(".")
        try:
            return hmac.compare_digest(_unb64(sig), self._mac(_unb64(body)))
        except ValueError:
            return False

    def _mac(self, payload: bytes) -> bytes:
        return hmac.new(self.key, TOKEN_CONTEXT + payload, hashlib.sha256).digest()

    def issue(self, lane_id: str, mode: ViewMode | str = ViewMode.VIEW, ttl: float | None = None, *,
              owner: bool = False, nonce: str | None = None, hard_until: float | None = None) -> ViewLink:
        """A signed link. `owner` (control links only) is for a link made in person: only the view-link
        route called in person, and a viewer request carrying the owner's panel session, set it. `nonce`
        and `hard_until` keep an old link's (a renew), so the sockets it opened follow the new expiry and
        the link still ends where it first would have: AGENT_LINK_MAX after it was made, OWNER_LINK_MAX
        for an owner link."""
        parse_lane_id(lane_id)
        mode = ViewMode(mode)
        owner = owner and mode == ViewMode.CONTROL
        now = int(self.clock())
        longest = now + (OWNER_LINK_MAX if owner else AGENT_LINK_MAX)
        hard = longest if hard_until is None else min(int(hard_until), longest)
        if hard <= now:
            raise TokenError("expired", "This viewer link reached the end of its life. Ask for a new one.")
        ttl = self.default_ttl if ttl is None else ttl
        ttl = int(max(MIN_TTL, min(MAX_TTL, ttl)))
        expires = min(now + ttl, hard)
        nonce = nonce or _b64(secrets.token_bytes(9))
        payload = f"{lane_id}|{mode.value}|{expires}|{nonce}|{hard}{'|' + OWNER_FLAG if owner else ''}".encode()
        token = f"{_b64(payload)}.{_b64(self._mac(payload))}"
        return ViewLink(lane_id, mode, float(expires), nonce, token, owner, float(hard))

    def sign_blob(self, purpose: str, value: str, ttl: float) -> tuple[str, float]:
        """(token, expiry) for one thing that is not a lane, like an environment's proof image: HMAC over
        purpose, value and expiry with the same key, so replacing the key revokes these too. `value`
        holds no "|"."""
        if "|" in purpose or "|" in value:
            raise ValueError("a signed value holds no |")
        expires = int(self.clock() + max(MIN_TTL, min(MAX_TTL, ttl)))
        payload = f"{purpose}|{value}|{expires}".encode()
        mac = hmac.new(self.key, BLOB_CONTEXT + payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(mac)}", float(expires)

    def open_blob(self, purpose: str, token: str | None) -> str | None:
        """The value a sign_blob token for `purpose` carries, or None: forged, expired, or for another
        purpose."""
        if not token or len(token) > MAX_TOKEN_CHARS or token.count(".") != 1:
            return None
        body, sig = token.split(".")
        try:
            payload, mac = _unb64(body), _unb64(sig)
        except ValueError:
            return None
        if not hmac.compare_digest(mac, hmac.new(self.key, BLOB_CONTEXT + payload, hashlib.sha256).digest()):
            return None
        try:
            kind, value, expires = payload.decode().split("|")
            if kind != purpose or int(expires) <= self.clock():
                return None
        except ValueError:
            return None
        return value

    def verify(self, token: str | None, lane_id: str) -> ViewLink:
        """Raise TokenError unless `token` is a valid, unexpired link for `lane_id`. The
        signature is checked first, so a tampered token is always "invalid"."""
        if not token:
            raise TokenError("missing", "This page needs a viewer link. Ask the agent for one with lane_view.")
        invalid = TokenError("invalid", "This viewer link is not valid. Ask the agent for a new one.")
        if len(token) > MAX_TOKEN_CHARS or token.count(".") != 1:
            raise invalid
        body, sig = token.split(".")
        try:
            payload, mac = _unb64(body), _unb64(sig)
        except ValueError:
            raise invalid from None
        if not hmac.compare_digest(mac, self._mac(payload)):
            raise invalid
        try:
            parts = payload.decode().split("|")
            owner = len(parts) == 6 and parts[5] == OWNER_FLAG
            if owner:
                parts.pop()
            lane, mode, expires, nonce, hard = parts
            link = ViewLink(lane, ViewMode(mode), float(int(expires)), nonce, token, owner, float(int(hard)))
        except ValueError:
            raise invalid from None
        if link.expires_at > link.hard_until:
            raise invalid
        if link.lane_id != lane_id:
            raise TokenError("wrong_lane", "This viewer link is for a different lane.")
        if min(link.expires_at, link.hard_until) <= self.clock():
            raise TokenError("expired", "This viewer link has expired. Ask the agent for a new one with lane_view.")
        return link


def make_view_link(tokens: ViewTokens, lane_id: str, mode: ViewMode | str = ViewMode.VIEW,
                   ttl: float | None = None, *, base: str = "", owner: bool = False) -> dict[str, Any]:
    """`{"url": "/view/ws/name?t=...", "expires_at": "...", "mode": ..., "owner": ...}` (docs/API.md
    view-link)."""
    link = tokens.issue(lane_id, mode, ttl, owner=owner)
    return {"url": link.path(base), "expires_at": link.expires_iso, "mode": link.mode.value, "owner": link.owner}


# ---- serialization ----------------------------------------------------------------------

def iso(value: float | datetime | None) -> str | None:
    return clock.iso_or_none(value, z=True)
