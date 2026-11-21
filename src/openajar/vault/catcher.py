"""Code catcher: read a one-time code from email, on the node, for a login step.

Per entry config (non-secret, MAC-authenticated with the entry):

    {"catcher": {"kind": "imap", "account": "<vault entry with the mailbox login>",
                 "host": "imap.example.com", "port": 993, "mailbox": "INBOX",
                 "sender": "security@mail.instagram.com" | "@mail.instagram.com",
                 "pattern": "(?<!\\d)(\\d{6})(?!\\d)", "window_seconds": 600,
                 "timeout_seconds": 120, "poll_seconds": 5}}

The mailbox password is a vault entry too; it resolves only into the IMAP login for a
host on that entry's domain (policy sink IMAP_LOGIN). Mail is read with BODY.PEEK on a
read-only SELECT, so nothing is marked read. The code is returned as a `Secret` and
typed by the node; it never reaches the model.

The other kind is the code card (docs/design/phones.md, Brief 1a):

    {"catcher": {"kind": "relay", "window_seconds": 300, "timeout_seconds": 300}}

At the code step the node opens a code request for that one lane and site, pushes the
owner a link to the panel, and the owner types the code into a sealed field there
(openajar.vault.relay). Both kinds are a `CodeSource`: `arm()` before the step that needs
the code, then `wait()` on the returned `PendingCode`.
"""

from __future__ import annotations

import asyncio
import email
import email.message
import email.policy
import imaplib
import re
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Protocol

from openajar.vault.domains import is_loopback, normalize_host
from openajar.vault.errors import CatcherError, EntryInvalid
from openajar.vault.secret import Secret

DEFAULT_PATTERN = r"(?<!\d)(\d{6})(?!\d)"
MAX_MESSAGE_BYTES = 1_000_000
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class CatcherRule:
    sender: str                       # exact address, or "@domain" for any sender on it
    pattern: str = DEFAULT_PATTERN
    window_seconds: int = 600
    mailbox: str = "INBOX"
    timeout_seconds: float = 120.0
    poll_seconds: float = 5.0


RELAY_LIMITS = (("window_seconds", 60, 900, 300), ("timeout_seconds", 30, 900, 300))


def _validate_relay(raw: dict[str, Any]) -> dict[str, Any]:
    unknown = set(raw) - {"kind", "window_seconds", "timeout_seconds"}
    if unknown:
        raise EntryInvalid(f"unknown code card keys: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {"kind": "relay"}
    for key, lo, hi, default in RELAY_LIMITS:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float) or not lo <= value <= hi:
            raise EntryInvalid(f"code card {key} must be {lo} to {hi}")
        out[key] = value
    return out


def validate_catcher_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EntryInvalid("catcher config must be an object")
    if raw.get("kind") == "relay":
        return _validate_relay(raw)
    allowed = {"kind", "account", "host", "port", "mailbox", "sender", "pattern", "window_seconds",
               "timeout_seconds", "poll_seconds"}
    unknown = set(raw) - allowed
    if unknown:
        raise EntryInvalid(f"unknown catcher keys: {', '.join(sorted(unknown))}")
    if raw.get("kind", "imap") != "imap":
        raise EntryInvalid("a code source is imap (email) or relay (the code card)")
    account = raw.get("account")
    if not isinstance(account, str) or not account.strip():
        raise EntryInvalid("catcher needs `account`, the vault entry holding the mailbox login")
    try:
        host = normalize_host(str(raw.get("host", "")))
    except ValueError:
        raise EntryInvalid("catcher host is not a valid host") from None
    port = raw.get("port", 993)
    if not isinstance(port, int) or not 0 < port < 65536:
        raise EntryInvalid("catcher port must be 1 to 65535")
    sender = str(raw.get("sender", "")).strip().lower()
    if not re.match(r"^([^@\s]+@|@)[a-z0-9.-]+\.[a-z0-9-]+$", sender):
        raise EntryInvalid("catcher sender must be an address or @domain")
    pattern = raw.get("pattern", DEFAULT_PATTERN)
    if not isinstance(pattern, str) or not 0 < len(pattern) <= 200:
        raise EntryInvalid("catcher pattern must be a regular expression up to 200 characters")
    try:
        re.compile(pattern)
    except re.error:
        raise EntryInvalid("catcher pattern is not a valid regular expression") from None
    out = {"kind": "imap", "account": account.strip(), "host": host, "port": port,
           "mailbox": str(raw.get("mailbox", "INBOX")), "sender": sender, "pattern": pattern}
    for key, lo, hi, default in (("window_seconds", 30, 86400, 600), ("timeout_seconds", 5, 900, 120),
                                 ("poll_seconds", 1, 60, 5)):
        value = raw.get(key, default)
        if not isinstance(value, int | float) or not lo <= value <= hi:
            raise EntryInvalid(f"catcher {key} must be {lo} to {hi}")
        out[key] = value
    if any(c in out["mailbox"] for c in '\r\n"\\') or len(out["mailbox"]) > 200:
        raise EntryInvalid("catcher mailbox name is invalid")
    return out


def rule_from_config(cfg: dict[str, Any]) -> CatcherRule:
    return CatcherRule(sender=cfg["sender"], pattern=cfg["pattern"], window_seconds=int(cfg["window_seconds"]),
                       mailbox=cfg["mailbox"], timeout_seconds=float(cfg["timeout_seconds"]),
                       poll_seconds=float(cfg["poll_seconds"]))


class CodeCatcher(Protocol):
    async def wait_for_code(self, rule: CatcherRule, *, since: datetime) -> Secret | None:
        """Poll until a message from `rule.sender`, sent at or after `since` and inside
        the window, carries a code matching `rule.pattern`; None after the timeout."""


class PendingCode(Protocol):
    request_id: str | None   # the code request ("cr_..."), for sources that open one

    async def wait(self) -> Secret | None:
        """The code, or None at timeout. Raises CodeDeclined when the owner said no code is
        coming, CatcherError on transport faults."""

    async def cancel(self) -> None: ...


class CodeSource(Protocol):
    kind: str  # "imap" | "relay"

    async def arm(self, ctx: Any, entry: Any, *, lane_id: str, site: str, not_before: datetime) -> PendingCode:
        """Get ready before the step that makes the site send a code. For imap this resolves
        the mailbox login and records not_before; for relay it opens a code card and pushes
        the owner."""


class _ImapPending:
    request_id = None

    def __init__(self, catcher: CodeCatcher, rule: CatcherRule, since: datetime):
        self._catcher, self._rule, self._since = catcher, rule, since

    async def wait(self) -> Secret | None:
        return await self._catcher.wait_for_code(self._rule, since=self._since)

    async def cancel(self) -> None:
        return None


class ImapCodeSource:
    """The email code catcher as a CodeSource. `connect` resolves the mailbox entry through
    the vault policy (sink IMAP_LOGIN) and builds the catcher."""

    kind = "imap"

    def __init__(self, cfg: dict[str, Any], connect: Callable[[], Awaitable[CodeCatcher]]):
        self.rule = rule_from_config(cfg)
        self._connect = connect

    async def arm(self, ctx: Any, entry: Any, *, lane_id: str, site: str, not_before: datetime) -> PendingCode:
        return _ImapPending(await self._connect(), self.rule, not_before)


def sender_matches(rule_sender: str, from_header: str) -> bool:
    addresses = [a.lower() for _, a in getaddresses([from_header or ""]) if a]
    if len(addresses) != 1:
        return False
    addr = addresses[0]
    if rule_sender.startswith("@"):
        return addr.endswith(rule_sender) and addr.count("@") == 1
    return addr == rule_sender


def message_text(msg: email.message.Message) -> str:
    parts: list[str] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_content()
        except (LookupError, ValueError, AttributeError):
            continue
        if not isinstance(payload, str):
            continue
        parts.append(_TAG_RE.sub(" ", payload) if ctype == "text/html" else payload)
    subject = str(msg.get("Subject", ""))
    return subject + "\n" + "\n".join(parts)


def extract_code(rule: CatcherRule, raw: bytes, *, since: datetime, now: datetime) -> str | None:
    """The code in one raw RFC 822 message, if it is from the sender and inside the window."""
    if len(raw) > MAX_MESSAGE_BYTES:
        return None
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    if not sender_matches(rule.sender, str(msg.get("From", ""))):
        return None
    try:
        sent = parsedate_to_datetime(str(msg.get("Date", "")))
    except (TypeError, ValueError, IndexError):
        return None
    if sent is None:
        return None
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=UTC)
    skew = timedelta(seconds=60)
    if sent < since - skew or sent < now - timedelta(seconds=rule.window_seconds) or sent > now + skew:
        return None
    m = re.search(rule.pattern, message_text(msg))
    if not m:
        return None
    return m.group(1) if m.groups() else m.group(0)


class ImapCodeCatcher:
    """Polls one IMAP mailbox over TLS. `connect` is injectable for tests."""

    def __init__(self, host: str, port: int, username: Secret, password: Secret, *,
                 connect: Callable[[str, int], Any] | None = None,
                 clock: Callable[[], float] = time.time, sleep=asyncio.sleep):
        self.host, self.port = host, port
        self._username, self._password = username, password
        self._connect = connect or self._tls_connect
        self._clock, self._sleep = clock, sleep

    def __repr__(self) -> str:
        return f"ImapCodeCatcher(host={self.host!r}, port={self.port})"

    def _tls_connect(self, host: str, port: int):
        if port == 143 and is_loopback(host):
            return imaplib.IMAP4(host, port, timeout=20)
        return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=20)

    def _poll_once(self, rule: CatcherRule, since: datetime) -> str | None:
        now = datetime.fromtimestamp(self._clock(), UTC)
        try:
            conn = self._connect(self.host, self.port)
        except (OSError, imaplib.IMAP4.error):
            raise CatcherError(f"could not reach the mailbox at {self.host}:{self.port}") from None
        try:
            try:
                conn.login(self._username.reveal(), self._password.reveal())
            except imaplib.IMAP4.error:
                raise CatcherError(f"the mailbox at {self.host} refused the login") from None
            typ, _ = conn.select(f'"{rule.mailbox}"', readonly=True)
            if typ != "OK":
                raise CatcherError(f"mailbox {rule.mailbox!r} not found on {self.host}")
            day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
            sender = rule.sender.lstrip("@")
            typ, data = conn.search(None, "FROM", f'"{sender}"', "SINCE", day)
            if typ != "OK" or not data or not data[0]:
                return None
            for mid in reversed(data[0].split()[-20:]):
                typ, msg_data = conn.fetch(mid, "(BODY.PEEK[])")
                if typ != "OK":
                    continue
                raw = next((p[1] for p in msg_data if isinstance(p, tuple) and len(p) > 1), None)
                if not isinstance(raw, bytes):
                    continue
                code = extract_code(rule, raw, since=since, now=now)
                if code:
                    return code
            return None
        except (OSError, imaplib.IMAP4.error):
            raise CatcherError(f"reading the mailbox at {self.host} failed") from None
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001, S110 - best effort; nothing secret in here
                pass

    async def wait_for_code(self, rule: CatcherRule, *, since: datetime) -> Secret | None:
        deadline = self._clock() + rule.timeout_seconds
        while True:
            code = await asyncio.to_thread(self._poll_once, rule, since)
            if code:
                return Secret(code)
            if self._clock() + rule.poll_seconds > deadline:
                return None
            await self._sleep(rule.poll_seconds)
