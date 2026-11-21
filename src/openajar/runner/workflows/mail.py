"""Mailboxes: mail in and out over connections the node opens, so no port is ever public.

A mailbox is set up on the node. Its password sits in the vault, bound to the mailbox's two hosts,
and never crosses the channel. The node polls IMAP for a mail trigger and sends over SMTP for
`mail.send`. Both always use verified TLS, through openajar.runner.mail.session: IMAP4_SSL and
SMTP_SSL, or a required STARTTLS.
Unit tests use a fake transport, and tests/unit/test_mail_real_server.py runs this against a real
IMAP and SMTP server in Docker.

Whether a message left decides whether a retry may send it: `SendRefused` means it certainly did
not (anything that happened before the message itself was handed over), and any other failure after
that is `SendInDoubt`, which is never repeated. `SendStopped` is a refusal that says the same would
happen for everyone, so a list stops at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import json
import logging
import re
import smtplib
import ssl
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from openajar.core.errors import InvalidRequest, NotFound
from openajar.core.models import LANE_ID_RE
from openajar.runner.mail import message as parsing
from openajar.runner.mail import session
from openajar.runner.mail.cursor import MailCursors, Seen, compose, fingerprint, is_new, split
from openajar.runner.mail.discovery import all_public
from openajar.runner.mail.net import resolve
from openajar.runner.mail.providers import by_id
from openajar.runner.mail.session import MailFailed, explain
from openajar.runner.mail.wire import Deadline
from openajar.runner.workflows.sends import Unsubscribe, normal_address
from openajar.runner.workflows.store import WorkflowStore
from openajar.vault.errors import VaultSealed

log = logging.getLogger("openajar.runner")
MAIL_OPERATION = "mail.send"
MAX_BODY = parsing.MAX_BODY
MAX_LIST = 50
POLL_BATCH = 50
MAX_REFERENCES = 20
POLL_WAIT_S = session.DEADLINE + 5  # past the transport's own deadline: that one is what ends a poll
SEND_DEADLINE = 120.0
SEED = 20        # the newest messages a new poll remembers as old
HEADER_BREAK_RE = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]+")


class SendRefused(Exception):
    """It certainly did not leave: safe to try again."""


class SendStopped(SendRefused):
    """It did not leave, and nothing else from this mailbox will until a person acts, so a list stops
    here instead of walking on: a sealed vault, a password that is gone, a server it cannot sign in to."""

    def __init__(self, words: str, *, code: str = "send_failed"):
        super().__init__(words)
        self.code = code


class SendInDoubt(Exception):
    """It may have left: never repeated."""


class MessageGone(NotFound):
    """The id is from before the mailbox changed, or the message is no longer there."""
    code = "mail_message_gone"


class Mailbox(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    address: str
    imap_host: str = Field(max_length=253)
    imap_port: int = Field(default=993, ge=1, le=65_535)
    imap_security: Literal["tls", "starttls"] = "tls"
    smtp_host: str = Field(max_length=253)
    smtp_port: int = Field(default=465, ge=1, le=65_535)
    smtp_security: Literal["tls", "starttls"] = "tls"
    username: str = Field(max_length=200)
    secret_ref: str = Field(max_length=200)  # the password's vault entry, never the password
    provider: str | None = None              # a row of openajar.runner.mail.providers
    files_sent: bool = False                 # the server keeps its own copy of what SMTP sent
    private_hosts: list[str] = Field(default_factory=list)  # a person typed these, so they may be private

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "address": self.address}

    def binding(self) -> dict[str, str]:
        """What the vault entry is bound to: another host or port does not get the password."""
        return {"mail_imap": f"{self.imap_host}:{self.imap_port}", "mail_smtp": f"{self.smtp_host}:{self.smtp_port}"}


@dataclass(frozen=True)
class Received:
    uid: int  # opaque: the UID with a generation above it (openajar.runner.mail.cursor)
    sender: str
    subject: str
    body: str
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    seen: bool = False
    auto_reply: bool = False  # Auto-Submitted says a machine wrote it
    to: str = ""
    date: str = ""
    arrived: datetime | None = None
    attachments: tuple[dict[str, str], ...] = field(default=())
    truncated: bool = False
    unreadable: bool = False  # a stub: only what its headers said (openajar.runner.mail.message)


class MailTransport(Protocol):
    async def send(self, box: Mailbox, password: str, message: EmailMessage, *, to: str) -> None: ...
    async def fetch(self, box: Mailbox, password: str, after_uid: int | None) -> tuple[list[Received], int]:
        """New mail after `after_uid`, and the highest uid seen. With None it reads no mail, and
        only says where the mailbox is now, so turning a trigger on does not fire on old mail."""
        ...


class TlsTransport:
    """imaplib and smtplib in a thread. The node dials out, over verified TLS, and listens on nothing.

    `tls` makes the context, and a test gives it one that trusts its own CA. Whatever it makes must
    verify, or nothing is opened (session.context_for)."""

    def __init__(self, *, cursors: MailCursors | None = None, tls: Callable[[], ssl.SSLContext] = ssl.create_default_context,
                 resolver: Callable[[str], list[str]] = resolve, timeout: float = session.TIMEOUT,
                 poll_deadline: float = session.DEADLINE):
        self.cursors = cursors or MailCursors()
        self.tls, self.resolver, self.timeout, self.poll_deadline = tls, resolver, timeout, poll_deadline

    def _dial_check(self, box: Mailbox, host: str) -> None:
        """A host nobody typed must still resolve to a public address, whatever DNS says today."""
        if host in box.private_hosts:
            return
        try:
            addresses = self.resolver(host)
        except OSError:
            raise MailFailed("unreachable") from None
        if not all_public(addresses):
            raise MailFailed("private")

    def _open_imap(self, box: Mailbox, password: str, deadline: Deadline) -> imaplib.IMAP4:
        self._dial_check(box, box.imap_host)
        imap = session.open_imap(box.imap_host, box.imap_port, box.imap_security, session.context_for(self.tls),
                                 timeout=self.timeout, deadline=deadline)
        try:
            session.imap_login(imap, box.username, password)
        except MailFailed:
            with contextlib.suppress(Exception):
                imap.shutdown()
            raise
        return imap

    @contextlib.contextmanager
    def _imap(self, box: Mailbox, password: str, deadline: Deadline | None = None) -> Iterator[imaplib.IMAP4]:
        imap = self._open_imap(box, password, deadline or Deadline(self.poll_deadline))
        try:
            yield imap
        finally:
            session.close_imap(imap)

    def _open_smtp(self, box: Mailbox, password: str) -> smtplib.SMTP:
        self._dial_check(box, box.smtp_host)
        smtp = session.open_smtp(box.smtp_host, box.smtp_port, box.smtp_security, session.context_for(self.tls),
                                 timeout=self.timeout, deadline=Deadline(SEND_DEADLINE))
        try:
            session.smtp_login(smtp, box.username, password)
        except MailFailed:
            smtp.close()
            raise
        return smtp

    # ---- signing in: nothing read, nothing sent ----
    async def verify(self, box: Mailbox, password: str) -> None:
        """IMAP login, then SMTP login. Raises `SignInFailed`, which says which of the two."""
        await asyncio.to_thread(self._verify, box, password)

    def _verify(self, box: Mailbox, password: str) -> None:
        for stage, check in (("imap", self._verify_imap), ("smtp", self._verify_smtp)):
            try:
                check(box, password)
            except MailFailed as e:
                raise SignInFailed(stage, e.kind) from None

    def _verify_imap(self, box: Mailbox, password: str) -> None:
        with self._imap(box, password):
            pass

    def _verify_smtp(self, box: Mailbox, password: str) -> None:
        smtp = self._open_smtp(box, password)
        with contextlib.suppress(smtplib.SMTPException, OSError, MailFailed):
            smtp.quit()
        smtp.close()

    # ---- sending ----
    async def send(self, box: Mailbox, password: str, message: EmailMessage, *, to: str) -> None:
        await asyncio.to_thread(self._send, box, password, message, to)

    @staticmethod
    def _hand_over(smtp: smtplib.SMTP, box: Mailbox, to: str, raw: bytes) -> None:
        """MAIL, RCPT and DATA by hand, with the addresses as given: nothing here reads a header, and
        until the message itself was sent, whatever goes wrong certainly did not send it."""
        try:
            code, _ = smtp.docmd("MAIL", f"FROM:<{box.address}>")
            if code != 250:
                raise SendRefused(f"the server refused this sender ({code})")
            code, _ = smtp.docmd("RCPT", f"TO:<{to}>")
            if code not in (250, 251):
                raise SendRefused("the server refused this recipient")
            code, _ = smtp.docmd("DATA")
            if code != 354:
                raise SendRefused(f"the server refused this message ({code})")
        except (smtplib.SMTPException, OSError, MailFailed) as e:
            raise SendRefused(f"the connection ended before the message ({type(e).__name__})") from None
        try:
            smtp.send(_dot_stuffed(raw))
            code, _ = smtp.getreply()
        except (smtplib.SMTPException, OSError, MailFailed) as e:
            raise SendInDoubt(f"the connection ended during the send ({type(e).__name__})") from None
        if 500 <= code < 600:
            raise SendRefused(f"the server refused this message ({code})")
        if code != 250:
            raise SendInDoubt(f"the server answered {code} after the message")

    def _send(self, box: Mailbox, password: str, message: EmailMessage, to: str) -> None:
        try:
            raw = message.as_bytes(policy=policy.SMTP)
            smtp = self._open_smtp(box, password)
        except MailFailed as e:
            raise SendStopped(explain("smtp", box.smtp_host, e.kind)) from None
        except Exception as e:  # noqa: BLE001 - no server has this message yet
            raise SendRefused(f"it could not be sent ({type(e).__name__})") from None
        try:
            self._hand_over(smtp, box, to, raw)
        finally:
            with contextlib.suppress(smtplib.SMTPException, OSError, MailFailed):
                smtp.quit()
            smtp.close()

    async def file_sent(self, box: Mailbox, password: str, raw: bytes, message_id: str) -> bool:
        return await asyncio.to_thread(self._file_sent, box, password, raw, message_id)

    def _file_sent(self, box: Mailbox, password: str, raw: bytes, message_id: str) -> bool:
        with self._imap(box, password) as imap:
            return session.file_copy(imap, raw, message_id)

    # ---- the poll ----
    async def fetch(self, box: Mailbox, password: str, after_uid: int | None) -> tuple[list[Received], int]:
        return await asyncio.to_thread(self._fetch, box, password, after_uid)

    @staticmethod
    def _seen(rows: list[tuple[int, datetime | None, bytes]]) -> list[tuple[int, Seen]]:
        out = []
        for uid, arrived, headers in rows:
            p = parsing.parse(headers)
            out.append((uid, Seen(fingerprint(p.message_id, p.date), arrived, p.date)))
        return out

    def _pick(self, imap: imaplib.IMAP4, box: Mailbox, from_uid: int, to_uid: int, cursor: Any,
              deadline: Deadline) -> tuple[list[int], int]:
        """After a changed validity: the next new UIDs in this range, and how far that looked, which
        is less than all of it when the poll's time runs short, so a big mailbox still gets through."""
        mark = cursor.mark
        since = session.imap_date(mark - timedelta(days=1)) if mark is not None else None  # SINCE ignores the zone
        criteria = ["UID", f"{from_uid}:{to_uid}"] + (["SINCE", since] if since else [])
        uids = sorted(u for u in session.search(imap, *criteria) if from_uid <= u <= to_uid)
        picked: list[int] = []
        for start in range(0, len(uids), session.HEADER_CHUNK):
            chunk = uids[start:start + session.HEADER_CHUNK]
            for uid, seen in self._seen(session.fetch_headers(imap, chunk)):
                if is_new(cursor, seen):
                    picked.append(uid)
                if len(picked) >= POLL_BATCH:
                    return picked, uid
            if deadline.past_half():
                return picked, chunk[-1]
        return picked, to_uid

    def _fetch(self, box: Mailbox, password: str, after: int | None) -> tuple[list[Received], int]:
        """The whole poll is under one deadline. Past half of it no new message is started, and the
        cursor stands at the last one read, so a slow server still moves forward, a poll at a time."""
        deadline = Deadline(self.poll_deadline)
        with self._imap(box, password, deadline) as imap:
            selected = session.select(imap, readonly=True)
            cursor = self.cursors.observe(box.id, selected.validity)
            top = session.highest_uid(imap, selected)
            if after is None:
                newest = session.newest(imap, selected, unread=False, since=None, limit=SEED)
                self.cursors.seed(box.id, [s for _, s in self._seen(session.fetch_headers(imap, sorted(newest)))])
                return [], compose(cursor.epoch, top)
            epoch, uid = split(after)
            if epoch != cursor.epoch:
                cursor, uid = self.cursors.begin_verify(box.id, top), 0
            if cursor.verify_to and uid >= cursor.verify_to:
                cursor = self.cursors.finish_verify(box.id)
            verifying = bool(cursor.verify_to)
            if verifying:
                picked, reached = self._pick(imap, box, uid + 1, cursor.verify_to, cursor, deadline)
            else:
                found = sorted(u for u in session.search(imap, "UID", f"{uid + 1}:*") if u > uid)
                picked = found[:POLL_BATCH]
                reached = picked[-1] if len(found) > POLL_BATCH else max(top, uid, *picked)
            out: list[Received] = []
            last = uid
            for wanted in picked:
                if deadline.past_half():
                    reached = last
                    break
                got = session.fetch_one(imap, wanted)
                if got is not None:
                    out.append(self._received(cursor.epoch, got))
                last = wanted
            self.cursors.saw(box.id, [Seen(fingerprint(m.message_id or (f"uid:{m.uid}" if m.unreadable else ""), m.date), m.arrived, m.date)
                                      for m in out], settle=not verifying)
            if verifying and reached >= cursor.verify_to:
                self.cursors.finish_verify(box.id)
            return out, compose(cursor.epoch, reached)

    @staticmethod
    def _received(epoch: int, got: session.Fetched) -> Received:
        return parse_message(compose(epoch, got.uid), got.raw, seen=got.seen, arrived=got.internal_date, truncated=got.truncated)

    # ---- reading ----
    async def list_messages(self, box: Mailbox, password: str, *, unread: bool, since: datetime | None,
                            limit: int) -> list[Received]:
        return await asyncio.to_thread(self._list, box, password, unread, since, limit)

    def _list(self, box: Mailbox, password: str, unread: bool, since: datetime | None, limit: int) -> list[Received]:
        with self._imap(box, password) as imap:
            selected = session.select(imap, readonly=True)
            cursor = self.cursors.observe(box.id, selected.validity)
            out = []
            for uid in session.newest(imap, selected, unread=unread, since=since, limit=limit):
                got = session.fetch_one(imap, uid, session.LIST_CAP)
                if got is not None and (since is None or got.internal_date is None or got.internal_date >= since):
                    out.append(self._received(cursor.epoch, got))
            return out

    async def read_message(self, box: Mailbox, password: str, message_id: int) -> Received:
        return await asyncio.to_thread(self._read, box, password, message_id)

    def _uid_of(self, box: Mailbox, imap: imaplib.IMAP4, selected: session.Selected, message_id: int) -> tuple[int, int]:
        cursor = self.cursors.observe(box.id, selected.validity)
        epoch, uid = split(message_id)
        if epoch != cursor.epoch:
            raise MessageGone("That id is from before this mailbox was rebuilt. List the mail again.")
        return epoch, uid

    def _read(self, box: Mailbox, password: str, message_id: int) -> Received:
        with self._imap(box, password) as imap:
            selected = session.select(imap, readonly=True)
            epoch, uid = self._uid_of(box, imap, selected, message_id)
            got = session.fetch_one(imap, uid)
            if got is None:
                raise MessageGone("That message is no longer in this mailbox.")
            return self._received(epoch, got)

    async def mark(self, box: Mailbox, password: str, message_id: int, read: bool) -> None:
        await asyncio.to_thread(self._mark, box, password, message_id, read)

    def _mark(self, box: Mailbox, password: str, message_id: int, read: bool) -> None:
        with self._imap(box, password) as imap:
            selected = session.select(imap, readonly=False)
            _, uid = self._uid_of(box, imap, selected, message_id)
            if uid not in session.search(imap, "UID", str(uid)):
                raise MessageGone("That message is no longer in this mailbox.")
            session.set_read(imap, uid, read)


class SignInFailed(Exception):
    """A login did not work. `stage` is imap or smtp, and `kind` the word for why."""

    def __init__(self, stage: str, kind: str):
        super().__init__(f"{stage}: {kind}")
        self.stage, self.kind = stage, kind


def _failure_words(box: Mailbox, e: Exception) -> str:
    """Why a mailbox cannot be read, in words of our own: nothing a server said."""
    if isinstance(e, MailFailed):
        return explain("imap", box.imap_host, e.kind, by_id(box.provider))
    if isinstance(e, SendRefused):
        return str(e)
    return f"{box.imap_host} could not be read."


def _dot_stuffed(raw: bytes) -> bytes:
    """A message as DATA carries it: a leading dot doubled, and the line that ends it."""
    body = re.sub(rb"(?m)^\.", b"..", raw)
    return body + (b"" if body.endswith(b"\r\n") else b"\r\n") + b".\r\n"


def parse_message(uid: int, raw: bytes, *, seen: bool = False, arrived: datetime | None = None,
                  truncated: bool = False) -> Received:
    p = parsing.parse(raw)
    return Received(uid=uid, sender=p.sender, subject=p.subject, body=p.body, message_id=p.message_id,
                    in_reply_to=p.in_reply_to, references=p.references, seen=seen, auto_reply=p.auto_reply,
                    to=p.to, date=p.date, arrived=arrived, attachments=p.attachments, truncated=truncated, unreadable=p.unreadable)


def build_message(box: Mailbox, *, to: str, subject: str, body: str, unsubscribe: Unsubscribe | None,
                  in_reply_to: str = "", references: str = "") -> EmailMessage:
    """The node adds the footer and the headers, so a workflow cannot leave them out, and a subject
    that came from outside cannot carry a header of its own. `in_reply_to` and `references` came from
    outside too: only whole message ids from them are kept. `unsubscribe` is None only for a reply
    the node itself found to be one (openajar.runner.mail.outbox)."""
    msg = EmailMessage()
    msg["From"] = box.address
    msg["To"] = to
    msg["Subject"] = HEADER_BREAK_RE.sub(" ", subject).strip()[:300]
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = make_msgid(domain=box.address.rpartition("@")[2])
    parents = parsing.message_ids(in_reply_to)[:1]
    if parents:
        chain = [*parsing.message_ids(references), parents[0]]
        msg["In-Reply-To"] = parents[0]
        msg["References"] = " ".join(dict.fromkeys(chain[-MAX_REFERENCES:]))
    for name, value in (unsubscribe.headers() if unsubscribe else {}).items():
        msg[name] = value
    msg.set_content(f"{body[:MAX_BODY]}{unsubscribe.footer() if unsubscribe else ''}")
    return msg


class MailService:
    def __init__(self, store: WorkflowStore, *, vault: Any = None, transport: MailTransport | None = None):
        self.store, self.vault = store, vault
        self.cursors = MailCursors(store)
        self.transport: Any = transport or TlsTransport(cursors=self.cursors)
        self.connect_jobs: Any = None  # openajar.runner.mail.connect, for the daemon's forms
        self._polling: set[str] = set()

    def add(self, data: dict[str, Any], *, person: bool) -> Mailbox:
        from openajar.core.errors import PolicyDenied

        if not person:
            raise PolicyDenied("only a person sets up a mailbox")
        box = Mailbox.model_validate(data)
        if not LANE_ID_RE.match(box.id):
            raise InvalidRequest(f"a mailbox id looks like a lane id, 'harbor/mail': {box.id!r}")
        address = normal_address(box.address)
        if address is None:
            raise InvalidRequest("that is not an email address")
        box = box.model_copy(update={"address": address})
        self.store.put_mailbox(box.id, box.model_dump())
        return box

    def get(self, mailbox_id: str) -> Mailbox | None:
        doc = self.store.mailbox(mailbox_id)
        return Mailbox.model_validate(doc) if doc else None

    def list(self) -> list[Mailbox]:
        return [Mailbox.model_validate(d) for d in self.store.mailboxes()]

    def require(self, mailbox_id: str) -> Mailbox:
        box = self.get(mailbox_id)
        if box is None:
            raise NotFound(f"there is no mailbox {mailbox_id} on this node")
        return box

    async def remove(self, mailbox_id: str, *, person: bool) -> bool:
        """The mailbox, its password and where its poll stood: a new mailbox under this id starts clean."""
        from openajar.core.errors import PolicyDenied

        if not person:
            raise PolicyDenied("only a person removes a mailbox")
        box = self.require(mailbox_id)
        forget = getattr(self.vault, "delete_secret", None)
        if callable(forget):
            await forget(box.secret_ref)
        self.cursors.drop(box.id)
        for key in (f"mail_uid:{box.id}", f"mail_health:{box.id}", f"mail_unreadable:{box.id}"):
            self.store._exec("DELETE FROM meta WHERE key = ?", (key,))
        return self.store.delete_mailbox(box.id)

    async def _password(self, box: Mailbox) -> str:
        """The password, only for the hosts it was saved for: a mailbox whose host was changed
        afterwards does not get it. The vault's seal covers the binding. Every failure here is one
        before any server was reached, and is the same for whoever the message was for."""
        try:
            listing = getattr(self.vault, "list_secrets", None)
            if callable(listing):
                entry = next((s for s in await listing() if s["ref"] == box.secret_ref), None)
                scope = (entry or {}).get("scope") or {}
                if entry is not None and any(scope.get(k) != v for k, v in box.binding().items()):
                    raise SendStopped(f"the password for {box.id} was saved for other servers. Connect the mailbox again.")
            fn = getattr(self.vault, "get_secret", None)
            value = await fn(box.secret_ref, purpose=f"mailbox {box.id}") if callable(fn) else None
        except VaultSealed:
            raise SendStopped("the vault is sealed. Unlock the vault, then try again.", code="vault_sealed") from None
        except SendStopped:
            raise
        except Exception as e:  # noqa: BLE001
            raise SendStopped(f"the password for {box.id} could not be read ({type(e).__name__})") from None
        if not value:
            raise SendStopped(f"the password for {box.id} is not in the vault")
        return value

    async def send(self, box: Mailbox, message: EmailMessage, *, to: str) -> bool:
        """Send to `to`, then put a copy in Sent. The copy can fail: by then the message has left, so
        that changes nothing about the send. Says whether the Sent folder holds it. `to` is what the
        server is told, and is never read back from a header."""
        if normal_address(to) != to:
            raise SendRefused("that is not a plain email address")
        password = await self._password(box)
        await self.transport.send(box, password, message, to=to)
        if box.files_sent:
            return True
        file_sent = getattr(self.transport, "file_sent", None)
        if not callable(file_sent):
            return False
        try:
            return await file_sent(box, password, message.as_bytes(policy=policy.SMTP), str(message.get("Message-ID") or ""))
        except Exception as e:  # noqa: BLE001 - it left, whatever happens to the copy
            log.warning("mailbox %s: the Sent copy was not filed (%s)", box.id, type(e).__name__)
            return False

    async def fetch(self, box: Mailbox, after_uid: int | None) -> tuple[list[Received], int]:
        """New mail, and where the poll now stands. A stub moves the poll on and starts nothing: it
        counts in the mailbox's health, and an agent or a person can still list it."""
        try:
            got, highest = await self.transport.fetch(box, await self._password(box), after_uid)
        except Exception as e:
            self.note_health(box.id, False, _failure_words(box, e))
            raise
        self.note_health(box.id, True)
        self._note_unreadable(box.id, [m.uid for m in got if m.unreadable])
        return [m for m in got if not m.unreadable], highest

    async def poll_due(self, box: Mailbox, handle: Callable[[Mailbox], Awaitable[Any]], *, approvals: Any = None,
                       wait: float = POLL_WAIT_S) -> bool:
        """One poll of one mailbox, to be started as a background task. `handle` does the poll
        (`WorkflowService.poll_mailbox`). A second call while one runs returns False at once, nothing
        here raises, and the poll cannot outlast `wait`. `approvals` closes the card of a mailbox
        connected at the CLI, which has no way to reach it. Says whether a poll ran to its end."""
        if box.id in self._polling:
            return False
        self._polling.add(box.id)
        try:
            from openajar.runner.mail.connect import close_requests

            close_requests(self, approvals, box.id)
            await asyncio.wait_for(handle(box), wait)
            return True
        except TimeoutError:
            self.note_health(box.id, False, f"{box.imap_host} took too long, so this poll stopped.")
            log.warning("mailbox %s: a poll ran out of time", box.id)
        except Exception as e:  # noqa: BLE001 - one mailbox's poll, and the next one tries again
            log.warning("mailbox %s could not be read (%s)", box.id, type(e).__name__)
        finally:
            self._polling.discard(box.id)
        return False

    def _note_unreadable(self, mailbox_id: str, uids: list[int]) -> None:
        """Each message once: a poll that runs again after a crash finds the same ones."""
        key = f"mail_unreadable:{mailbox_id}"
        rows = self.store._rows("SELECT value FROM meta WHERE key = ?", (key,))
        known = json.loads(rows[0]["value"]) if rows else {"count": 0, "last": 0}
        new = [u for u in uids if u > known["last"]]
        if new:
            doc = {"count": known["count"] + len(new), "last": max(new)}
            self.store._exec("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(doc)))

    def note_health(self, mailbox_id: str, ok: bool, words: str = "") -> None:
        """What a person sees about a mailbox. Written when it changes, not on every poll."""
        key = f"mail_health:{mailbox_id}"
        rows = self.store._rows("SELECT value FROM meta WHERE key = ?", (key,))
        known = json.loads(rows[0]["value"]) if rows else None
        if known is None or known.get("ok") != ok or known.get("words") != words:
            doc = {"ok": ok, "words": words, "at": datetime.now(UTC).isoformat()}
            self.store._exec("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(doc)))

    def health(self, mailbox_id: str) -> dict[str, Any] | None:
        rows = self.store._rows("SELECT value FROM meta WHERE key = ?", (f"mail_health:{mailbox_id}",))
        if not rows:
            return None
        unreadable = self.store._rows("SELECT value FROM meta WHERE key = ?", (f"mail_unreadable:{mailbox_id}",))
        return {**json.loads(rows[0]["value"]), **({"unreadable": json.loads(unreadable[0]["value"])["count"]} if unreadable else {})}

    def view(self, box: Mailbox) -> dict[str, Any]:
        """A mailbox for its owner: where it is and how it is doing, never a password."""
        return {"id": box.id, "address": box.address, "provider": box.provider,
                "imap": {"host": box.imap_host, "port": box.imap_port, "security": box.imap_security},
                "smtp": {"host": box.smtp_host, "port": box.smtp_port, "security": box.smtp_security},
                "health": self.health(box.id)}

    async def test(self, box: Mailbox) -> None:
        """Sign in to both again, reading nothing and sending nothing."""
        await self.transport.verify(box, await self._password(box))

    async def list_messages(self, box: Mailbox, *, unread: bool = False, since: datetime | None = None,
                            limit: int = 20) -> list[Received]:
        return await self.transport.list_messages(box, await self._password(box), unread=unread, since=since,
                                                  limit=max(1, min(limit, MAX_LIST)))

    async def read_message(self, box: Mailbox, message_id: int) -> Received:
        return await self.transport.read_message(box, await self._password(box), message_id)

    async def mark(self, box: Mailbox, message_id: int, read: bool) -> None:
        await self.transport.mark(box, await self._password(box), message_id, read)
