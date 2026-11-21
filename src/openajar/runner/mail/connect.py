"""Connecting a mailbox: sign in to both, then save (docs/EMAIL.md, "Connecting a mailbox").

The order is the contract's. Nothing is kept until both logins worked, and a failure says which of
the two failed and why. A result only DNS or a guess named waits for a person's yes. The password is
a `Secret` here, revealed at the login and at the vault, and in no message, log, trace or return value.

An agent never holds a way in. `mail_connect` makes a card that names the address, and that is all it
gets. The link is a job: a page the node serves for this one mailbox for a few minutes, opened from
the card by the owner signed in to this node's own panel, and it ends when the mailbox is connected.
The card itself reaches the cloud, so it carries no token and no link, only the line to run at the
computer. A token proves someone holds a link, never that a person is there, so a job can only add a
mailbox: changing one that exists takes `in_person`, which only the panel and the CLI can say.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from openajar.core.errors import InvalidRequest, NotFound, OpenajarError, PolicyDenied
from openajar.core.models import LANE_ID_RE, Principal
from openajar.runner.mail import discovery, providers
from openajar.runner.mail.discovery import Found, typed_server
from openajar.runner.mail.session import explain
from openajar.runner.workflows.mail import Mailbox, MailService, SignInFailed
from openajar.vault.secret import Secret

log = logging.getLogger("openajar.runner")

JOB_TTL_S = 15 * 60
JOB_TRIES = 5
MAX_JOBS = 20
MAX_SHOWN = 50
MAX_PASSWORD = 1024
SECRET_KIND = "mail_password"
REQUEST_KEY = "mail_request:"
REQUEST_TTL_S = 24 * 3600
DONE, NOT_NOW = "Done", "Not now"


class MailConnectFailed(InvalidRequest):
    """A login did not work, and nothing was saved. `stage` is imap or smtp."""
    code = "mail_connect_failed"

    def __init__(self, message: str, *, stage: str, kind: str):
        super().__init__(message)
        self.stage, self.kind = stage, kind


class MailConfirmHosts(InvalidRequest):
    """Only DNS or a guess named these servers, and no person said yes yet."""
    code = "mail_confirm_hosts"


class MailboxTaken(InvalidRequest):
    """The id already holds another address."""
    code = "mail_id_taken"


class MailJobEnded(OpenajarError):
    """This link was used, or ran out of time or tries."""
    code = "mail_job_ended"


def secret_ref(mailbox_id: str) -> str:
    return f"mail/{mailbox_id}"


def prepared(password: Secret, provider: providers.Provider | None) -> Secret:
    """As a login takes it. A pasted line end is not part of a password, and an app password shown in
    groups of four has no spaces in it."""
    value = password.reveal().strip("\r\n")
    if provider is not None and provider.spaced_password:
        value = value.replace(" ", "")
    if not value or len(value) > MAX_PASSWORD:
        raise InvalidRequest(f"A password is 1 to {MAX_PASSWORD} characters.")
    return Secret(value)


def mailbox_for(found: Found, mailbox_id: str) -> Mailbox:
    provider = providers.by_id(found.provider)
    private = sorted({s.host for s in (found.imap, found.smtp) if s.typed})
    return Mailbox(id=mailbox_id, address=found.address, imap_host=found.imap.host, imap_port=found.imap.port,
                   imap_security=found.imap.security, smtp_host=found.smtp.host, smtp_port=found.smtp.port,
                   smtp_security=found.smtp.security, username=found.imap.username, secret_ref=secret_ref(mailbox_id),
                   provider=found.provider, files_sent=bool(provider and provider.files_sent), private_hosts=private)


def taken(mailbox_id: str) -> MailboxTaken:
    return MailboxTaken(f"{mailbox_id} is already connected. Only a person changes it, in this node's own panel or with "
                        "`openajar mail connect`.")


async def connect(mail: MailService, found: Found, password: Secret, *, mailbox_id: str, confirmed: bool = False,
                  in_person: bool = False) -> Mailbox:
    """Sign in to both, then keep the password in the vault, bound to these two hosts, and the mailbox
    in the store. `found` is what the person was shown, and `confirmed` their yes to that. `in_person`
    is the CLI or the owner at the node's panel, and only that changes a mailbox that exists."""
    if not LANE_ID_RE.match(mailbox_id or ""):
        raise InvalidRequest(f"A mailbox id looks like a lane id, 'harbor/mail': {mailbox_id!r}")
    known = mail.get(mailbox_id)
    if known is not None and known.address != found.address:
        raise MailboxTaken(f"{mailbox_id} is already {known.address}. Remove it first, or pick another id.")
    if known is not None and not in_person:
        raise taken(mailbox_id)
    if found.needs_yes and not confirmed:
        raise MailConfirmHosts(f"Only DNS named {found.imap.host} and {found.smtp.host}. Look at them, and say yes "
                               "before a password goes there.")
    vault = mail.vault
    if vault is None:
        raise PolicyDenied("This node has no vault to keep a password in.")
    await vault.ensure_unsealed()  # a sealed vault says so now, before a person's password is used
    provider = providers.by_id(found.provider)
    box = mailbox_for(found, mailbox_id)
    clean = prepared(password, provider)
    try:
        await mail.transport.verify(box, clean.reveal())
    except SignInFailed as e:
        host = box.imap_host if e.stage == "imap" else box.smtp_host
        raise MailConnectFailed(explain(e.stage, host, e.kind, provider), stage=e.stage, kind=e.kind) from None
    await vault.put_secret(box.secret_ref, clean, kind=SECRET_KIND, label=f"Mailbox {box.id}", scope=box.binding())
    try:
        saved = mail.add(box.model_dump(), person=True)
    except Exception:
        if known is None:
            await vault.delete_secret(box.secret_ref)
        raise
    mail.note_health(saved.id, True)
    log.info("mailbox %s is connected", saved.id)
    return saved


def apply_typed(found: Found | None, address: str, *, imap: dict[str, Any] | None, smtp: dict[str, Any] | None) -> Found:
    """What a person typed over what was found. With nothing found, they typed all of it."""
    if found is not None:
        return discovery.typed(found, imap=imap, smtp=smtp)
    if not imap or not smtp:
        raise InvalidRequest("No mail servers were found, so both need typing in.")
    address = discovery.split_address(address)[0]
    return Found(address, typed_server(imap, username=address), typed_server(smtp, username=address), "typed", "typed")


@dataclass
class Job:
    token_hash: str
    address: str
    mailbox_id: str
    found: Found | None
    by: str
    expires_at: float
    tries: int = 0
    done: bool = False
    busy: bool = False
    # Who asked for it: "agent" for a card an agent raised; None when a person opened it for themselves.
    asked_by: str | None = None

    @property
    def tries_left(self) -> int:
        return max(0, JOB_TRIES - self.tries)


class ConnectJobs:
    """Live in the daemon's memory, so a restart ends them, and the token is kept as a hash."""

    def __init__(self, mail: MailService, *, clock: Callable[[], float] = time.time):
        self.mail, self.clock = mail, clock
        self._jobs: dict[str, Job] = {}
        self._shown: dict[str, tuple[Found, float]] = {}

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()

    def _sweep(self) -> None:
        now = self.clock()
        for key in [k for k, j in self._jobs.items() if j.expires_at <= now or j.done or j.tries_left == 0]:
            del self._jobs[key]
        for key in [k for k, (_, until) in self._shown.items() if until <= now]:
            del self._shown[key]

    def room(self) -> None:
        """Said before a job's discovery runs, not after."""
        self._sweep()
        if len(self._jobs) >= MAX_JOBS:
            raise MailJobEnded("Too many mailboxes are waiting to be connected. Wait for those links to end.")

    def show(self, found: Found) -> str:
        """Keep what a person is being shown, so a later yes means these hosts."""
        self._sweep()
        digest = found.digest()
        self._shown[digest] = (found, self.clock() + JOB_TTL_S)
        while len(self._shown) > MAX_SHOWN:
            del self._shown[next(iter(self._shown))]
        return digest

    def shown(self, digest: Any, address: str) -> Found | None:
        """What this digest was shown for, or None. Nothing is looked up again."""
        self._sweep()
        held = self._shown.get(digest) if isinstance(digest, str) else None
        return held[0] if held is not None and held[0].address == address else None

    def open(self, address: str, mailbox_id: str, found: Found | None, *, by: str, asked_by: str | None = None) -> tuple[str, Job]:
        self.room()
        token = secrets.token_urlsafe(32)
        job = Job(self._hash(token), address, mailbox_id, found, by, self.clock() + JOB_TTL_S, asked_by=asked_by)
        self._jobs[job.token_hash] = job
        return token, job

    def end(self, token: str) -> bool:
        """A person cancels a link: it ends at once, as one that ran out. False when it had already ended."""
        job = self.get(token)
        if job is None or job.busy:
            return False
        del self._jobs[job.token_hash]
        return True

    def get(self, token: str) -> Job | None:
        """The job behind a link, or None: a link that ended and one that never was look the same."""
        self._sweep()
        return self._jobs.get(self._hash(token))

    async def run(self, token: str, password: Secret, *, shown: Any = None, imap: dict[str, Any] | None = None,
                  smtp: dict[str, Any] | None = None, confirmed: bool = False) -> Mailbox:
        """`shown` is the digest the page carried: a yes here is to the servers this job holds."""
        job = self.get(token)
        if job is None:
            raise MailJobEnded("This link has ended. Open the card again for a new one.")
        if job.busy:
            raise MailJobEnded("This link is being used right now.")
        if job.found is not None and shown != job.found.digest():
            raise MailConfirmHosts("This is not the page that shows these servers. Open it again.")
        job.busy = True
        try:
            found = apply_typed(job.found, job.address, imap=imap, smtp=smtp)
            box = await connect(self.mail, found, password, mailbox_id=job.mailbox_id, confirmed=confirmed)
        except MailConnectFailed:
            job.tries += 1
            raise
        finally:
            job.busy = False
        job.done = True
        self._sweep()
        return box


def jobs_for(mail: MailService) -> ConnectJobs:
    if mail.connect_jobs is None:
        mail.connect_jobs = ConnectJobs(mail)
    return mail.connect_jobs


# ---- the card: what an agent asks for, and all it can ----

def _forget(mail: MailService, approval_id: str) -> None:
    mail.store._exec("DELETE FROM meta WHERE key = ?", (f"{REQUEST_KEY}{approval_id}",))


def _requests(mail: MailService) -> list[dict[str, str]]:
    return [json.loads(r["value"]) for r in mail.store._rows("SELECT value FROM meta WHERE key LIKE ?", (f"{REQUEST_KEY}%",))]


def card_words(address: str, mailbox_id: str) -> str:
    """Written here, never in an agent's words. It reaches the cloud, so no token and no link, and the
    address is quoted because a person may paste this line: backticks are legal in an address."""
    return (f"An agent asks you to connect the mailbox {address} as {mailbox_id}. Only a person can do this. "
            f"At the computer this node runs on, run: openajar mail connect {shlex.quote(address)} --id {mailbox_id}")


def waiting(mail: MailService, approvals: Any) -> list[dict[str, str]]:
    """The cards still open, for a mailbox still to connect. Anything else is forgotten here."""
    out = []
    for ask in _requests(mail):
        try:
            open_ = approvals.get(ask["id"]).state == "pending"
        except NotFound:
            open_ = False
        if open_ and mail.get(ask["mailbox"]) is None:
            out.append(ask)
        elif not open_:
            _forget(mail, ask["id"])
    return out


def ask_a_person(mail: MailService, approvals: Any, address: str, mailbox_id: str, *, requester: Principal,
                 grant_id: str | None, grant_path: str | None) -> str:
    """The card for a mailbox that does not exist yet, or the one already open for it."""
    if mail.get(mailbox_id) is not None:
        raise taken(mailbox_id)
    for ask in waiting(mail, approvals):
        if (ask["address"], ask["mailbox"]) == (address, mailbox_id):
            return ask["id"]
    ap = approvals.create_question(question=card_words(address, mailbox_id), options=[DONE, NOT_NOW], requester=requester,
                                   grant_id=grant_id, grant_path=grant_path, ttl_s=REQUEST_TTL_S,
                                   workspace=mailbox_id.split("/", 1)[0])
    mail.store._exec("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     (f"{REQUEST_KEY}{ap.id}", json.dumps({"id": ap.id, "address": address, "mailbox": mailbox_id})))
    return ap.id


def request(mail: MailService, approvals: Any, approval_id: str) -> dict[str, str]:
    ask = next((a for a in waiting(mail, approvals) if a["id"] == approval_id), None)
    if ask is None:
        raise NotFound("That card is closed, or its mailbox is connected.")
    return ask


def close_requests(mail: MailService, approvals: Any, mailbox_id: str) -> None:
    """A connected mailbox leaves nothing waiting in Needs you, however it was connected."""
    if approvals is None:
        return
    for ask in _requests(mail):
        if ask["mailbox"] != mailbox_id:
            continue
        try:
            if approvals.get(ask["id"]).state == "pending":
                approvals.decide(ask["id"], "deny", by="owner", note="The mailbox is connected.")
        except OpenajarError:
            log.info("the card for mailbox %s was closed already", mailbox_id)
        _forget(mail, ask["id"])


def link_for(runtime: Any, token: str, job: Job) -> dict[str, Any]:
    """The link, on the node's own port, as a lane's view link is: it never passes a controller."""
    config = runtime.require("config")
    from openajar.node.tools.lanes import ssh_host

    path = f"/api/mail/form/{token}"
    port = config.mcp_port
    return {"url": f"http://localhost:{port}{path}", "path": path, "tunnel": f"ssh -N -L {port}:localhost:{port} {ssh_host(config)}",
            "expires_at": datetime.fromtimestamp(job.expires_at, UTC).isoformat()}


async def discover_quietly(address: str, net: Any) -> Found | None:
    """What was found, or None when a person will have to type the servers in."""
    try:
        return await discovery.discover(address, net)
    except discovery.DiscoveryFailed:
        return None
