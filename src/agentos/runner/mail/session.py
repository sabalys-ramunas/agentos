"""Opening IMAP and SMTP over verified TLS, and the IMAP commands the node uses.

Always TLS: implicit, or STARTTLS that is required, so a server (or anyone between) that does not
offer it ends the connection before a password is sent. The certificate and the host name are always
checked. A password only ever reaches the login calls here, and what a server says back is read for
its meaning and dropped, because it can echo what it was sent. No exception raised here carries it.

Every connection is `Imap` or `Smtp`: the standard clients with their reads under a `Deadline` and a
cap on all a server can send (agentos.runner.mail.wire), so a hostile server costs one poll. What was
read before STARTTLS is dropped with the old socket, so nothing sent in the clear reads as encrypted.
Once TLS is verified, what a host resolves to is not pinned: the certificate is what says it is the
right server (docs/EMAIL.md).
"""

from __future__ import annotations

import base64
import contextlib
import imaplib
import re
import smtplib
import ssl
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from agentos.runner.mail.message import MESSAGE_ID_RE
from agentos.runner.mail.providers import GMAIL_WORDS, STARTTLS, Provider
from agentos.runner.mail.wire import Deadline, MailFailed, Wire

TIMEOUT = 30.0
DEADLINE = 60.0             # the whole of one connection's work, whatever a server does
LOCAL_NAME = "[127.0.0.1]"  # says nothing about the node, as a mail client's greeting does
WINDOW = 2000               # messages searched at a time, so no answer outgrows imaplib's line limit
MAX_SCAN = 40_000
HEADER_CHUNK = 100
FETCH_CAP = 2_000_000       # bytes of one message read at most: text comes first, and big parts after
LIST_CAP = 65_536
MAX_LINE = 1_000_000        # a response line: as imaplib's, and a search answer needs it
MAX_LITERAL = FETCH_CAP + 4096
MAX_RESPONSE = 16_000_000   # all that one command's answer may hold
MAX_UNTAGGED = 20_000       # untagged lines in one command's answer
MAX_SMTP_LINE = 8192
MAX_SMTP_LINES = 200        # lines in one multi-line reply
SENT_NAMES = ("sent", "sent items", "sent messages", "sent mail", "inbox.sent", "inbox/sent", "inbox.sent items")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
DATE_RE = re.compile(rb'INTERNALDATE "\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d\d):(\d\d):(\d\d) ([+-])(\d\d)(\d\d)"')
SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
UID_RE = re.compile(rb"\bUID (\d+)")
LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')


class _BoundedImap:
    """Its reads go through a `Wire`, for the socket as it now is, so STARTTLS starts a new one."""

    def __init__(self, *args: Any, deadline: Deadline, **kw: Any):
        self._deadline = deadline
        self._wire: Wire | None = None
        self._untagged = 0
        kw["timeout"] = min(kw.get("timeout") or TIMEOUT, deadline.left())
        super().__init__(*args, **kw)

    def _reader(self) -> Wire:
        if self._wire is None or self._wire.sock is not self.sock:
            self._wire = Wire(self.sock, self._deadline, max_line=MAX_LINE, max_total=MAX_RESPONSE)
        return self._wire

    def read(self, size: int) -> bytes:
        if size > MAX_LITERAL:
            raise MailFailed("too_much")
        return self._reader().read(size)

    def readline(self) -> bytes:
        return self._reader().readline()

    def send(self, data: bytes) -> None:
        self.sock.settimeout(self._deadline.left())
        self.sock.sendall(data)

    def _command(self, name: str, *args: Any) -> Any:
        self._untagged = 0
        self._reader().begin()
        return super()._command(name, *args)  # type: ignore[misc]

    def _append_untagged(self, typ: str, dat: Any) -> None:
        self._untagged += 1
        if self._untagged > MAX_UNTAGGED:
            raise MailFailed("too_much")
        super()._append_untagged(typ, dat)  # type: ignore[misc]


class Imap(_BoundedImap, imaplib.IMAP4):
    pass


class ImapTls(_BoundedImap, imaplib.IMAP4_SSL):
    pass


class _BoundedSmtp:
    """`getreply` is smtplib's, with a cap on the lines of a reply, which it does not have."""

    def __init__(self, *args: Any, deadline: Deadline, **kw: Any):
        self._deadline = deadline
        self._wire: Wire | None = None
        kw["timeout"] = min(kw.get("timeout") or TIMEOUT, deadline.left())
        super().__init__(*args, **kw)

    def _reader(self) -> Wire:
        if self._wire is None or self._wire.sock is not self.sock:
            self._wire = Wire(self.sock, self._deadline, max_line=MAX_SMTP_LINE, max_total=MAX_SMTP_LINE * MAX_SMTP_LINES)
        return self._wire

    def send(self, s: Any) -> None:
        if self.sock:
            self.sock.settimeout(self._deadline.left())
        super().send(s)  # type: ignore[misc]

    def getreply(self) -> tuple[int, bytes]:
        wire, lines = self._reader(), []
        wire.begin()
        while True:
            if len(lines) >= MAX_SMTP_LINES:
                raise MailFailed("too_much")
            try:
                line = wire.readline()
            except OSError as e:
                raise smtplib.SMTPServerDisconnected(type(e).__name__) from None
            if not line:
                raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
            lines.append(line[4:].strip(b" \t\r\n"))
            try:
                code = int(line[:3])
            except ValueError:
                return -1, b"\n".join(lines)
            if line[3:4] != b"-":
                return code, b"\n".join(lines)


class Smtp(_BoundedSmtp, smtplib.SMTP):
    pass


class SmtpTls(_BoundedSmtp, smtplib.SMTP_SSL):
    pass


def explain(stage: str, host: str, kind: str, provider: Provider | None = None) -> str:
    """What a person reads: which of the two failed, and why, in plain words."""
    what = "Reading mail (IMAP)" if stage == "imap" else "Sending mail (SMTP)"
    wants_app = provider is not None and provider.needs_app_password
    if kind == "app_password" or (kind == "sign_in" and wants_app):
        name = provider.name if provider else "This provider"
        if provider is not None and provider.id in ("gmail", "google-workspace"):
            return f"{what}: {name} did not accept this password. {GMAIL_WORDS}"
        link = f" Make one at {provider.help_url}." if provider and provider.help_url else ""
        return f"{what}: {name} did not accept this password. It wants an app password, not your normal one.{link}"
    words = {
        "unreachable": f"could not reach {host}. Check the server name and the port.",
        "certificate": f"{host} did not show a valid certificate for that name, so no password was sent.",
        "no_starttls": f"{host} did not offer the encrypted connection it must use, so no password was sent.",
        "tls": f"an encrypted connection to {host} could not be set up, so no password was sent.",
        "private": f"{host} does not resolve to a public address, so no password was sent. Type the server in if it is yours.",
        "sign_in": f"{host} did not accept this username and password.",
        "imap_off": "IMAP is turned off for this account. Turn it on in the account's settings, or ask its admin.",
        "web_login": f"{host} wants a browser sign-in first. Sign in to the account once in a browser, then try again.",
        "bad_password": "that password has characters a mail server cannot take.",
        "too_slow": f"{host} took too long, so this stopped.",
        "too_much": f"{host} sent more than a mail server should, so this stopped.",
    }
    return f"{what}: {words.get(kind, f'{host} gave an answer that could not be understood.')}"


def classify_login(text: str) -> str:
    """What a refused login means. Gmail says AUTHENTICATIONFAILED or 535 for a wrong password, and
    names application-specific passwords when it wants one."""
    low = text.lower()
    if "application-specific password" in low or "app password" in low or "5.7.9" in low:
        return "app_password"
    if "not enabled for imap" in low or "imap access is disabled" in low or "imap is disabled" in low or \
            "imap use" in low:
        return "imap_off"
    if "web browser" in low or "webloginrequired" in low or "5.7.14" in low:
        return "web_login"
    return "sign_in"


def context_for(factory: Any) -> ssl.SSLContext:
    """A context that verifies, or nothing: a weakened one is never used for mail."""
    context = factory()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise MailFailed("tls")
    context.minimum_version = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)
    return context


def _tls_kind(e: BaseException) -> str:
    return "certificate" if isinstance(e, ssl.SSLCertVerificationError) else "tls"


def open_imap(host: str, port: int, security: str, context: ssl.SSLContext, *, timeout: float = TIMEOUT,
              deadline: Deadline | None = None) -> imaplib.IMAP4:
    deadline = deadline or Deadline(DEADLINE)
    try:
        if security != STARTTLS:
            return ImapTls(host, port, ssl_context=context, timeout=timeout, deadline=deadline)
        imap = Imap(host, port, timeout=timeout, deadline=deadline)
    except ssl.SSLError as e:
        raise MailFailed(_tls_kind(e)) from None
    except (OSError, imaplib.IMAP4.error):
        raise MailFailed("unreachable") from None
    try:
        imap.starttls(ssl_context=context)
    except ssl.SSLError as e:
        with contextlib.suppress(Exception):
            imap.shutdown()
        raise MailFailed(_tls_kind(e)) from None
    except (OSError, imaplib.IMAP4.error):
        with contextlib.suppress(Exception):
            imap.shutdown()
        raise MailFailed("no_starttls") from None
    return imap


def open_smtp(host: str, port: int, security: str, context: ssl.SSLContext, *, timeout: float = TIMEOUT,
              deadline: Deadline | None = None) -> smtplib.SMTP:
    deadline = deadline or Deadline(DEADLINE)
    try:
        if security != STARTTLS:
            return SmtpTls(host, port, timeout=timeout, context=context, local_hostname=LOCAL_NAME, deadline=deadline)
        smtp = Smtp(host, port, timeout=timeout, local_hostname=LOCAL_NAME, deadline=deadline)
    except ssl.SSLError as e:
        raise MailFailed(_tls_kind(e)) from None
    except (OSError, smtplib.SMTPException):
        raise MailFailed("unreachable") from None
    try:
        smtp.ehlo()
        if not smtp.has_extn("starttls"):
            raise MailFailed("no_starttls")
        smtp.starttls(context=context)
        smtp.ehlo()
    except ssl.SSLError as e:
        smtp.close()
        raise MailFailed(_tls_kind(e)) from None
    except (OSError, smtplib.SMTPException):
        smtp.close()
        raise MailFailed("no_starttls") from None
    except MailFailed:
        smtp.close()
        raise
    return smtp


def close_imap(imap: imaplib.IMAP4) -> None:
    """Whatever state it is in: a LOGOUT past the deadline raises before imaplib shuts the socket."""
    with contextlib.suppress(Exception):
        imap.logout()
    with contextlib.suppress(Exception):
        imap.shutdown()


def _printable_password(password: str) -> None:
    if not password or any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise MailFailed("bad_password")


def imap_login(imap: imaplib.IMAP4, username: str, password: str) -> None:
    _printable_password(password)
    try:
        if password.isascii() and username.isascii():
            imap.login(username, password)
        else:  # LOGIN is ASCII only: SASL PLAIN carries UTF-8
            imap.authenticate("PLAIN", lambda _: f"\0{username}\0{password}".encode())
    except imaplib.IMAP4.abort:
        raise MailFailed("unreachable") from None
    except imaplib.IMAP4.error as e:
        raise MailFailed(classify_login(str(e))) from None
    except (OSError, UnicodeError):
        raise MailFailed("unreachable") from None


def _smtp_plain(smtp: smtplib.SMTP, username: str, password: str) -> None:
    """AUTH PLAIN by hand: smtplib encodes it as ASCII, and SASL PLAIN carries UTF-8."""
    smtp.ehlo_or_helo_if_needed()
    token = base64.b64encode(f"\0{username}\0{password}".encode()).decode("ascii")
    code, text = smtp.docmd("AUTH", f"PLAIN {token}")
    if code != 235:
        raise smtplib.SMTPAuthenticationError(code, text)


def smtp_login(smtp: smtplib.SMTP, username: str, password: str) -> None:
    _printable_password(password)
    try:
        if password.isascii() and username.isascii():
            smtp.login(username, password)
        else:
            _smtp_plain(smtp, username, password)
    except smtplib.SMTPAuthenticationError as e:
        text = e.smtp_error.decode("utf-8", "replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
        raise MailFailed(classify_login(f"{e.smtp_code} {text}")) from None
    except smtplib.SMTPServerDisconnected:
        raise MailFailed("unreachable") from None
    except (smtplib.SMTPException, UnicodeError):
        raise MailFailed("other") from None
    except OSError:
        raise MailFailed("unreachable") from None


@dataclass(frozen=True)
class Selected:
    exists: int
    validity: int
    uidnext: int | None


@dataclass(frozen=True)
class Fetched:
    uid: int
    raw: bytes
    seen: bool
    internal_date: datetime | None
    size: int

    @property
    def truncated(self) -> bool:
        return self.size > len(self.raw)


def _number(value: Any) -> int | None:
    try:
        return int(value[0]) if value and value[0] is not None else None
    except (TypeError, ValueError):
        return None


def select(imap: imaplib.IMAP4, folder: str = "INBOX", *, readonly: bool = True) -> Selected:
    typ, data = imap.select(folder, readonly=readonly)
    if typ != "OK":
        raise MailFailed("other")
    validity = _number(imap.response("UIDVALIDITY")[1])
    if validity is None:
        raise MailFailed("other")
    return Selected(_number(data) or 0, validity, _number(imap.response("UIDNEXT")[1]))


def search(imap: imaplib.IMAP4, *criteria: str) -> list[int]:
    typ, data = imap.uid("SEARCH", None, *criteria)
    if typ != "OK":
        raise MailFailed("other")
    return [int(x) for x in (data[0] or b"").split()] if data else []


def highest_uid(imap: imaplib.IMAP4, selected: Selected) -> int:
    if selected.exists == 0:
        return (selected.uidnext or 1) - 1
    if selected.uidnext is not None:
        return selected.uidnext - 1
    return max(search(imap, "UID", "*"), default=0)


def imap_date(when: datetime) -> str:
    return f"{when.day:02d}-{MONTHS[when.month - 1]}-{when.year}"


def records(parts: list[Any]) -> Iterator[tuple[bytes, bytes | None]]:
    """imaplib's FETCH answer as (the words of one message, its literal). A literal is what the
    stranger wrote, so it is kept apart: nothing in it can read as a flag or a UID."""
    meta, literal, started = b"", None, False
    for part in parts or []:
        if isinstance(part, tuple):
            if started:
                yield meta, literal
            meta, literal, started = part[0], part[1], True
        elif isinstance(part, bytes):
            if started and len(part) < 200 and part.lstrip()[:1] not in (b"", b"*") and not part[:1].isdigit():
                meta += b" " + part
            elif part.strip() != b")":
                if started:
                    yield meta, literal
                meta, literal, started = part, None, True
    if started:
        yield meta, literal


def internal_date(meta: bytes) -> datetime | None:
    found = DATE_RE.search(meta)
    if found is None:
        return None
    day, month, year, hour, minute, second, sign, zh, zm = found.groups()
    try:
        offset = timedelta(hours=int(zh), minutes=int(zm))
        zone = timezone(offset if sign == b"+" else -offset)
        when = datetime(int(year), MONTHS.index(month.decode().title()) + 1, int(day), int(hour), int(minute), int(second),
                        tzinfo=zone)
        return when.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def fetch_one(imap: imaplib.IMAP4, uid: int, cap: int = FETCH_CAP) -> Fetched | None:
    """One message, up to `cap` bytes, without marking it read. None when it is gone."""
    typ, parts = imap.uid("FETCH", str(uid), f"(FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[]<0.{cap}>)")
    if typ != "OK":
        return None
    for meta, literal in records(parts):
        if literal is None:
            continue
        flags = FLAGS_RE.search(meta)
        size = SIZE_RE.search(meta)
        return Fetched(uid, literal, bool(flags) and b"\\Seen" in flags.group(1), internal_date(meta),
                       int(size.group(1)) if size else len(literal))
    return None


def fetch_headers(imap: imaplib.IMAP4, uids: list[int]) -> list[tuple[int, datetime | None, bytes]]:
    """(uid, arrival, the Message-ID and Date headers) for many messages, oldest first."""
    out: list[tuple[int, datetime | None, bytes]] = []
    for start in range(0, len(uids), HEADER_CHUNK):
        chunk = ",".join(str(u) for u in uids[start:start + HEADER_CHUNK])
        typ, parts = imap.uid("FETCH", chunk, "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE)])")
        if typ != "OK":
            raise MailFailed("other")
        for meta, literal in records(parts):
            uid = UID_RE.search(meta)
            if uid is not None and literal is not None:
                out.append((int(uid.group(1)), internal_date(meta), literal))
    return sorted(out, key=lambda row: row[0])


def newest(imap: imaplib.IMAP4, selected: Selected, *, unread: bool, since: datetime | None, limit: int) -> list[int]:
    """UIDs newest first, searched from the top a window at a time, so a mailbox of any size answers
    the same way and no answer outgrows a line."""
    found: list[int] = []
    top, scanned = selected.exists, 0
    while top > 0 and len(found) < limit and scanned < MAX_SCAN:
        low = max(1, top - WINDOW + 1)
        criteria = [f"{low}:{top}"]
        if unread:
            criteria.append("UNSEEN")
        if since is not None:
            criteria += ["SINCE", imap_date(since)]
        found += sorted(search(imap, *criteria), reverse=True)
        scanned += top - low + 1
        top = low - 1
    return found[:limit]


def set_read(imap: imaplib.IMAP4, uid: int, read: bool) -> None:
    typ, _ = imap.uid("STORE", str(uid), "+FLAGS.SILENT" if read else "-FLAGS.SILENT", "(\\Seen)")
    if typ != "OK":
        raise MailFailed("other")


def sent_folder(imap: imaplib.IMAP4) -> str | None:
    """The folder a mail client shows as Sent, as the server spells it: the `\\Sent` mark first, then
    the usual names. Nothing is decoded, so a name goes back exactly as it came."""
    typ, rows = imap.list()
    if typ != "OK":
        return None
    named: list[tuple[str, str]] = []
    for row in rows or []:
        found = LIST_RE.match(row) if isinstance(row, bytes) else None
        if found is None:
            continue
        name = found.group("name").decode("ascii", "replace")
        if b"\\sent" in found.group("flags").lower():
            return name
        named.append((name.strip('"').lower(), name))
    for wanted in SENT_NAMES:
        for low, name in named:
            if low == wanted:
                return name
    return None


def file_copy(imap: imaplib.IMAP4, raw: bytes, message_id: str) -> bool:
    """A copy in the Sent folder, unless a copy with this id is already there: some servers file what
    SMTP sent by themselves. Made when no such folder exists yet."""
    folder = sent_folder(imap)
    if folder is None:
        folder = '"Sent"'
        typ, _ = imap.create(folder)
        if typ != "OK":
            return False
    typ, _ = imap.select(folder, readonly=True)
    if typ != "OK":
        return False
    if MESSAGE_ID_RE.fullmatch(message_id or "") and '"' not in message_id and "\\" not in message_id and \
            search(imap, "HEADER", "Message-ID", f'"{message_id}"'):
        return True
    typ, _ = imap.append(folder, "(\\Seen)", imaplib.Time2Internaldate(time.time()), raw)
    return typ == "OK"
