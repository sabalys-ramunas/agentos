"""Approve from your phone: the owner's Telegram chat with this node's bot (docs/CONFIRMATIONS.md, Telegram).

Telegram is a way to confirm at the `chat` level (`confirm.TelegramMethod`) and a channel of the owner's
pushes (the notifier's "telegram" channel). It carries exactly what chat may carry: `chat_refusal` decides,
at send time (no Approve button) and again when a tap arrives, and the decision goes through
`ApprovalStore.decide` with a `telegram` proof, which the method registry holds to `chat` once more. So a
tap can never approve what needs `signed_in` or more, a payment, an irreversible action, a question, or what
follows untrusted text. Everything else gets Deny, and a Review link when the node has a public URL.

How it holds:

- One bot per node, one paired account. The token and the paired chat and user live in
  `secrets/notifier.json` (mode 600), written only by the node's CLI and by pairing. The token is part of
  every Bot API URL, so no URL, no exception text and no log line from here ever holds it (`redact_token`,
  `_TokenFilter` on httpx's own logger).
- Pairing: the owner runs `openajar policy notify telegram pair` on the node, which prints a single-use code
  valid for 10 minutes as a t.me link. The node accepts `/start <code>` only in a private chat, from the
  account that chat belongs to. Every later tap must come from that account in that chat, or it is ignored
  and logged.
- The node only dials out: it long-polls `getUpdates` and keeps the update offset in `policy.db`.
- A button holds an opaque random prompt id and nothing else. A prompt is single use, expires, and is bound
  to the approval's content: a tap on content that changed since the message was sent decides nothing.
  Approve is not counted faster than a person reads (`MIN_ANSWER_S`), and an approval gets at most
  `MAX_PROMPTS` messages with an Approve button.
- The message is plain text (no parse mode), so text from a page cannot add formatting. It shows the
  content in full; when that does not fit in one Telegram message, Approve is left out.
- Telegram's servers see the message text, the post included. That is the price of the convenience.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
import time
import unicodedata
from datetime import datetime
from typing import Any

import httpx

from openajar.core.errors import NotFound, OpenajarError
from openajar.policy.approval_models import Approval
from openajar.policy.confirm import ConfirmLevel, Proof, chat_refusal
from openajar.policy.confirm_chat import FAMILY_PROMPTS, FAMILY_WINDOW_S, prompt_text, quoted
from openajar.policy.errors import Conflict
from openajar.policy.notifier import OWNER, TelegramConfig
from openajar.policy.quiet_hours import zone
from openajar.policy.traces import TraceEvent

log = logging.getLogger("openajar.telegram")

METHOD = "telegram"
API = "https://api.telegram.org"
POLL_TIMEOUT_S = 50      # getUpdates waits this long for something to happen
CALL_TIMEOUT_S = 15.0
PAIR_TTL_S = 600         # a pairing code works for 10 minutes, once
PROMPT_TTL_S = 1800      # buttons work for 30 minutes (then: the panel), as a push's links do
MAX_PROMPTS = 3          # messages with an Approve button per approval
MIN_ANSWER_S = 1.0       # an Approve faster than this was not read
MAX_TEXT = 4096          # Telegram's limit for one message, in UTF-16 code units
BACKOFF_MAX_S = 300.0
SYNC_BUDGET_S = 20.0     # one round of message edits never takes longer than this
START_QUIET_S = 600      # an ignored /start is logged once per sender in this window
START_SENDERS = 1000     # senders remembered for that; past it the oldest are forgotten
TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
_ANY_TOKEN = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,64}")
REDACTED = "[redacted]"

SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_prompts (
  prompt_id TEXT PRIMARY KEY, approval_id TEXT NOT NULL, decision TEXT NOT NULL, chat_id INTEGER NOT NULL,
  message_id INTEGER, content_hash TEXT, content_digest TEXT, created_at REAL NOT NULL, sent_at REAL,
  expires_at REAL NOT NULL, used_at REAL, shown TEXT, closed_at REAL
);
CREATE INDEX IF NOT EXISTS telegram_prompts_approval ON telegram_prompts(approval_id);
CREATE INDEX IF NOT EXISTS telegram_prompts_message ON telegram_prompts(chat_id, message_id);
CREATE TABLE IF NOT EXISTS telegram_pairing (
  code_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL
);
CREATE TABLE IF NOT EXISTS telegram_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# how a decision arrived, for the line that replaces the buttons
_HOW = {"telegram": "from Telegram", "session": "in the panel", "passkey": "in the panel with a passkey",
        "code": "in the panel", "push_link": "from the phone link", "chat": "in chat", "cli": "on the node"}


def redact_token(text: Any, token: str | None = None) -> str:
    """Text with no bot token in it: this bot's, and anything shaped like one."""
    out = str(text)
    if token:
        out = out.replace(token, REDACTED)
    return _ANY_TOKEN.sub(REDACTED, out)


class _TokenFilter(logging.Filter):
    """httpx logs each request's URL, and a Bot API URL holds the token: this takes it out."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a record that cannot format is not ours to fix
            return True
        if _ANY_TOKEN.search(message):
            record.msg, record.args = redact_token(message), None
        return True


for _name in ("httpx", "httpcore"):
    _logger = logging.getLogger(_name)
    if not any(isinstance(f, _TokenFilter) for f in _logger.filters):
        _logger.addFilter(_TokenFilter())


def text_length(text: str) -> int:
    """How Telegram counts a message: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def cut(text: str, limit: int | None = None) -> str:
    """`text` cut to what one Telegram message holds, counted as Telegram counts it."""
    limit = MAX_TEXT if limit is None else limit
    if text_length(text) <= limit:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if used + width > limit:
            break
        out.append(ch)
        used += width
    return "".join(out)


def _clean(value: Any, limit: int = 80) -> str:
    """A short value on one line, with nothing that would change how it reads."""
    text = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in str(value or ""))
    return " ".join(text.split())[:limit]


def _code_hash(code: str) -> str:
    return hashlib.sha256(f"agentos-telegram-pair|{code}".encode()).hexdigest()


class TelegramError(Exception):
    """A Bot API call that did not work. Its text never holds the token."""

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None,
                 maybe_sent: bool = False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        # the request went out and no answer came back: Telegram may have done it
        self.maybe_sent = maybe_sent


class BotApi:
    """The few Bot API calls the node makes. Every call is a POST of JSON, out to Telegram."""

    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None, base: str = API):
        self._token = token
        self.transport = transport
        self.base = base.rstrip("/")

    def __repr__(self) -> str:
        return "BotApi(token=[redacted])"

    async def call(self, method: str, payload: dict | None = None, *, timeout_s: float = CALL_TIMEOUT_S) -> Any:
        unreachable: str | None = None
        maybe_sent = False
        resp: httpx.Response | None = None
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=timeout_s) as client:
                resp = await client.post(f"{self.base}/bot{self._token}/{method}", json=payload or {})
        except httpx.HTTPError as exc:  # its text may hold the URL: only its kind leaves here
            unreachable = type(exc).__name__
            maybe_sent = isinstance(exc, httpx.ReadTimeout | httpx.ReadError | httpx.RemoteProtocolError)
        if resp is None:
            raise TelegramError(f"Telegram could not be reached ({unreachable or 'no answer'})", maybe_sent=maybe_sent)
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("ok") is True:
            return body.get("result")
        desc = body.get("description") if isinstance(body, dict) else None
        params = body.get("parameters") if isinstance(body, dict) else None
        retry = params.get("retry_after") if isinstance(params, dict) else None
        text = f"Telegram answered {resp.status_code}" + (f": {str(desc)[:200]}" if desc else "")
        raise TelegramError(redact_token(text, self._token), status=resp.status_code,
                            retry_after=float(retry) if isinstance(retry, int | float) else None)


class TelegramService:
    """One per policy service: the bot, pairing, the outbound messages, the taps, and the poller."""

    def __init__(self, policy: Any, *, transport: httpx.AsyncBaseTransport | None = None):
        self.policy = policy
        self.transport = transport
        self.db = policy.db
        with self.db.lock:
            self.db.conn.executescript(SCHEMA)
        self._stamp: tuple | None = None
        self._cfg: TelegramConfig | None = None
        self._task: asyncio.Task | None = None
        self._errors = 0
        self._starts: dict[Any, float] = {}  # sender of an ignored /start -> when it was last logged

    # ---- config ----
    @property
    def notifier(self) -> Any:
        return self.policy.notifier

    @property
    def approvals(self) -> Any:
        return self.policy.approvals

    def clock(self) -> float:
        return self.policy.clock()

    def config(self) -> TelegramConfig | None:
        """The Telegram section of notifier.json as it is now: the CLI writes it from another process."""
        path = self.notifier.config_path
        try:
            st = path.stat()
            stamp: tuple | None = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            self._cfg = self.notifier.telegram_on_disk() if stamp is not None else None
            self._stamp = stamp
        return self._cfg

    def api(self, cfg: TelegramConfig) -> BotApi:
        return BotApi(cfg.token, transport=self.transport)

    def paired(self) -> bool:
        cfg = self.config()
        return cfg is not None and cfg.paired

    def off(self, workspace: str | None = None) -> bool:
        """Turned off in the confirmation setting: then it sends nothing and takes no tap."""
        setting = self.policy.confirmation.current()
        return METHOD in (setting.off_in(workspace) if workspace else setting.methods_off)

    def sending(self) -> bool:
        """Is the owner's Telegram a channel now (the notifier asks)."""
        return self.paired() and not self.off()

    def _state(self, key: str) -> str | None:
        row = self.db.one("SELECT value FROM telegram_state WHERE key = ?", (key,))
        return row["value"] if row is not None else None

    def _set_state(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO telegram_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                        "value = excluded.value", (key, value))

    def _trace(self, status: str, summary: str, *, approval_id: str | None = None, error: str | None = None,
               owner: bool = True) -> None:
        cfg = self.config()
        args: dict[str, Any] = {"via": METHOD}
        if approval_id:
            args["approval_id"] = approval_id
        self.policy.traces.write(TraceEvent(kind="approval_telegram", status=status, summary=summary, error=error,
                                            principal=OWNER if owner else None, principal_kind="human" if owner else None,
                                            args=args), extra_scrub=[cfg.token] if cfg else [])

    # ---- the owner's CLI ----
    async def set_token(self, token: str) -> str:
        """Check a token with Telegram and keep it. A new bot starts unpaired. Returns the bot's username."""
        token = token.strip()
        if not TOKEN_RE.match(token):
            raise ValueError("that does not look like a bot token from BotFather (digits, a colon, then letters)")
        me = await BotApi(token, transport=self.transport).call("getMe")
        if not isinstance(me, dict) or not me.get("is_bot") or not isinstance(me.get("username"), str):
            raise ValueError("Telegram did not say this token belongs to a bot")
        self.notifier.save_telegram(TelegramConfig(token=token, bot_username=me["username"]))
        self._forget()
        # a webhook left on the bot (another program used it) makes every getUpdates answer 409
        try:
            await BotApi(token, transport=self.transport).call("deleteWebhook", {"drop_pending_updates": False})
        except TelegramError as exc:
            said = redact_token(exc, token)
            log.warning("the bot's webhook could not be removed: %s", said)
            self._set_state("last_error", f"the bot's webhook could not be removed: {said}"[:300])
        return me["username"]

    def start_pairing(self) -> str:
        """A single-use code for `/start`, valid for PAIR_TTL_S. Only its hash is kept."""
        if self.config() is None:
            raise ValueError("set the bot token first: openajar policy notify telegram set-token")
        code = secrets.token_urlsafe(18)
        now = self.clock()
        with self.db.tx() as conn:
            conn.execute("DELETE FROM telegram_pairing")
            conn.execute("INSERT INTO telegram_pairing (code_hash, created_at, expires_at) VALUES (?, ?, ?)",
                         (_code_hash(code), now, now + PAIR_TTL_S))
        return code

    def pair_link(self, code: str) -> str:
        cfg = self.config()
        return f"https://t.me/{cfg.bot_username if cfg else 'your_bot'}?start={code}"

    def pairing_pending(self) -> bool:
        return self.db.one("SELECT 1 FROM telegram_pairing WHERE used_at IS NULL AND expires_at > ?",
                           (self.clock(),)) is not None

    def remove(self) -> None:
        """Forget the bot and the paired account. Nothing more is sent, and no tap is taken."""
        self.notifier.save_telegram(None)
        self._forget()

    def _forget(self) -> None:
        now = self.clock()
        with self.db.tx() as conn:
            conn.execute("DELETE FROM telegram_pairing")
            conn.execute("DELETE FROM telegram_state")
            conn.execute("UPDATE telegram_prompts SET used_at = COALESCE(used_at, ?), closed_at = ? "
                         "WHERE closed_at IS NULL", (now, now))
        self._stamp = None

    def status(self) -> dict:
        cfg = self.config()
        return {"bot": cfg.bot_username if cfg else None, "paired": bool(cfg and cfg.paired),
                "username": cfg.username if cfg else None, "user_id": cfg.user_id if cfg else None,
                "pairing": self.pairing_pending(), "turned_off": self.off(),
                "last_error": self._state("last_error"), "conflict": self._state("conflict") is not None}

    # ---- what may be approved here ----
    def approve_refusal(self, ap: Approval) -> str | None:
        """Why a tap on Approve cannot approve this now, or None. What chat may carry and no more, read as
        things are now: the setting, the content, and what the asking agent has read since."""
        setting = self.policy.confirmation.current()
        if METHOD in setting.methods_off:
            return "Telegram is turned off on this node"
        if METHOD in setting.off_in(ap.workspace):
            return "Telegram is turned off in this workspace"
        why = chat_refusal(ap)
        if why:
            return why
        if METHOD not in (ap.methods or []):
            return "Telegram was not offered for this approval when it was made"
        if (ap.requester_owner or OWNER) != OWNER:
            return "a member's agent asked for it, so a person decides it in the panel"
        if ap.needs_whole_view():
            return "only someone who read every message in the panel approves it"
        if ap.is_publish_card():
            if ap.follows and not setting.publish_takes_chat(ap.workspace):
                return "publishing in chat is turned off"
        elif self._read_untrusted_since(ap):
            return "the agent that asked has read untrusted text since"
        if self.policy.person_grant(ap, OWNER) is None:
            return "the owner cannot approve this one (four-eyes)"
        return None

    def _read_untrusted_since(self, ap: Approval) -> bool:
        """Has the asking grant family read untrusted text (chat asks it of the session that answers; a tap
        here has no session, so the whole family is asked). No family to ask: as if it had."""
        parts = (ap.grant_path or "").split("/")
        if len(parts) < 3 or not parts[1]:
            return True
        family = f"family:{parts[1]}"
        taint = self.policy.taint
        if "untrusted" in taint.get(family):
            return True
        return any((o.get("last") or 0) >= ap.created_at for o in taint.family_origins(family))

    # ---- what the owner reads ----
    def _when(self, ts: float) -> str:
        try:
            tz = self.notifier.timing_for(OWNER).timezone
            return datetime.fromtimestamp(ts, zone(tz)).strftime("%Y-%m-%d %H:%M ") + tz
        except (ValueError, OSError, OverflowError):
            return datetime.fromtimestamp(ts).isoformat(timespec="minutes")

    def _facts(self, ap: Approval) -> list[str]:
        who = _clean(ap.requester.get("id")) or "an agent"
        if ap.kind == "question":
            return [f"{who} asks you a question. Answer it in the panel.",
                    f"Expires: {self._when(ap.expires_at)}"]
        lines = [f"{who} asks to {ap.effect or 'act'}" + (f" on {ap.lane}" if ap.lane else "") + "."]
        if ap.reason:
            lines.append(f"Why it asks: {_clean(ap.reason, 300)}")
        after = ap.follows_words()
        if after:
            lines.append(f"This follows untrusted text from {after}. Check that it is what you want, "
                         "not what the text asked for.")
        lines.append(f"Expires: {self._when(ap.expires_at)}")
        return lines

    def _body(self, ap: Approval) -> str:
        """The node's facts and the whole content."""
        if ap.kind == "question":
            options = f"\n\nOptions:\n{quoted(chr(10).join(ap.options))}" if ap.options else ""
            return "\n".join(self._facts(ap)) + "\n\nThe question, as the agent wrote it:\n" + quoted(ap.summary) + options
        return "\n".join(self._facts(ap)) + "\n\n" + prompt_text(ap)

    def compose(self, ap: Approval) -> tuple[str, bool]:
        """(text, offer Approve) for a waiting approval."""
        if ap.kind == "question":
            text = self._body(ap)
            return (text if text_length(text) <= MAX_TEXT else "\n".join(self._facts(ap))), False
        why = self.approve_refusal(ap)
        if why is None and self._approve_prompts(ap.id) >= MAX_PROMPTS:
            why = "it was sent here enough times"
        if why is None and self._family_prompts(ap) >= FAMILY_PROMPTS:  # new wording is a new approval: the real limit
            why = "this agent asked here too often in the last hour"
        review = self._review_url(ap) is not None
        text = self._body(ap) + "\n\n" + self._footer(why, review)
        if text_length(text) > MAX_TEXT:  # cut content is never approved here, as in chat
            why = why or "the content is too long for one Telegram message"
            text = ("\n".join(self._facts(ap)) + "\n\nThe content is too long for one Telegram message. Open it in the "
                    "panel to read it.\n\n" + self._footer(why, review))
        return text, why is None

    def _footer(self, why: str | None, review: bool) -> str:
        if why is None:
            return "Approve only if you want exactly this. Deny is always safe."
        where = "open it in the panel to approve it" if review else "approve it in the panel on your computer"
        return f"Telegram cannot approve this: {why}. You can deny it here, or {where}."

    def _review_url(self, ap: Approval) -> str | None:
        """The signed Review link, only with a public URL: a phone cannot open the node's own address."""
        if not self.notifier.config.public_base_url or not self.notifier.link_signer:
            return None
        return self.notifier.review_url(ap.id, ap.expires_at)

    def _outcome(self, ap: Approval) -> str:
        if ap.state == "expired":
            return "Expired: nobody decided in time."
        verb = {"approved": "Approved", "consumed": "Approved", "denied": "Denied", "answered": "Answered",
                "cancelled": "Withdrawn"}.get(ap.state, ap.state.capitalize())
        how = _HOW.get(ap.decided_via or ("cli" if ap.decided_by == "owner:cli" else ""), "")
        return f"{verb} {how}.".replace(" .", ".")

    # ---- sending ----
    def _approve_prompts(self, approval_id: str) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM telegram_prompts WHERE approval_id = ? AND decision = 'approve' "
                          "AND (message_id IS NOT NULL OR shown = 'unknown')", (approval_id,))
        return int(row["n"]) if row else 0

    def _family_prompts(self, ap: Approval) -> int:
        """Messages with an Approve button sent for this grant family in the last hour (chat's FAMILY_PROMPTS)."""
        parts = (ap.grant_path or "").split("/")
        if len(parts) < 3 or not parts[1]:
            return 0  # no family: approve_refusal already refused it
        family = parts[1].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        row = self.db.one("SELECT COUNT(DISTINCT p.message_id) AS n FROM telegram_prompts p JOIN approvals a "
                          "ON a.id = p.approval_id WHERE p.decision = 'approve' AND p.message_id IS NOT NULL "
                          "AND p.sent_at > ? AND a.grant_path LIKE ? ESCAPE '\\'",
                          (self.clock() - FAMILY_WINDOW_S, f"/{family}/%"))
        return int(row["n"]) if row else 0

    async def deliver(self, event: dict) -> tuple[bool, str | None]:
        """One outbox row for the owner's Telegram (the notifier calls this). An approval is written from
        the store as it is now, never from the queued event."""
        cfg = self.config()
        if cfg is None or not cfg.paired:
            return False, "telegram is not paired"
        if self.off():
            return False, "telegram is turned off"
        ap = None
        if isinstance(event.get("approval_id"), str):
            with contextlib.suppress(NotFound):
                ap = self.approvals.get(event["approval_id"])
        if ap is not None and self.off(ap.workspace):  # off in its workspace: nothing of it goes to Telegram
            return False, "telegram is turned off in this workspace"
        try:
            if ap is not None and ap.state == "pending" and not event.get("missed"):
                await self._send_approval(cfg, ap)
            else:
                title, body = self.notifier._message(event)
                await self.api(cfg).call("sendMessage", {"chat_id": cfg.chat_id, "text": cut(f"{title}\n\n{body}".strip()),
                                                         "link_preview_options": {"is_disabled": True}})
        except TelegramError as exc:
            return False, redact_token(exc, cfg.token)
        return True, None

    def _markup(self, ap: Approval, ids: dict[str, str]) -> dict | None:
        row = [{"text": label, "callback_data": ids[key]} for key, label in (("approve", "Approve"), ("deny", "Deny"))
               if key in ids]
        rows = [row] if row else []
        review = self._review_url(ap)
        if review:
            rows.append([{"text": "Review in the panel", "url": review}])
        return {"inline_keyboard": rows} if rows else None

    async def _send_approval(self, cfg: TelegramConfig, ap: Approval) -> None:
        text, approve = self.compose(ap)
        now = self.clock()
        expires = min(ap.expires_at, now + PROMPT_TTL_S)
        kinds = (["approve"] if approve else []) + (["deny"] if ap.kind == "action" else []) or ["none"]
        ids = {k: secrets.token_urlsafe(24) for k in kinds}
        with self.db.tx() as conn:
            for kind, pid in ids.items():
                conn.execute("INSERT INTO telegram_prompts (prompt_id, approval_id, decision, chat_id, content_hash, "
                             "content_digest, created_at, expires_at, shown) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'buttons')",
                             (pid, ap.id, kind, cfg.chat_id, ap.content_hash, ap.content_digest, now, expires))
        payload: dict[str, Any] = {"chat_id": cfg.chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        markup = self._markup(ap, {k: v for k, v in ids.items() if k != "none"})
        if markup:
            payload["reply_markup"] = markup
        try:
            sent = await self.api(cfg).call("sendMessage", payload)
            message_id = sent.get("message_id") if isinstance(sent, dict) else None
            if not isinstance(message_id, int):
                raise TelegramError("Telegram did not say which message it sent")
        except TelegramError as exc:
            marks = ",".join("?" * len(ids))
            if exc.maybe_sent:  # it may be in the chat: its buttons keep working, and a tap says which message it is
                self.db.execute(f"UPDATE telegram_prompts SET shown = 'unknown', sent_at = ? WHERE prompt_id IN "
                                f"({marks})", (self.clock(), *ids.values()))
            else:
                self.db.execute(f"DELETE FROM telegram_prompts WHERE prompt_id IN ({marks})", tuple(ids.values()))
            raise
        marks = ",".join("?" * len(ids))
        self.db.execute(f"UPDATE telegram_prompts SET message_id = ?, sent_at = ? WHERE prompt_id IN ({marks})",
                        (message_id, self.clock(), *ids.values()))

    async def _edit(self, cfg: TelegramConfig, chat_id: int, message_id: int, text: str,
                    review: Approval | None = None) -> bool:
        """Show `text` in place of a message, with no button but Review (when given). False: try later."""
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": cut(text),
                                   "link_preview_options": {"is_disabled": True}}
        url = self._review_url(review) if review is not None else None
        if url:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "Review in the panel", "url": url}]]}
        try:
            await self.api(cfg).call("editMessageText", payload)
        except TelegramError as exc:
            if exc.status is not None and 400 <= exc.status < 500 and exc.status != 429:
                return True  # gone, or already shows this: nothing more to do
            log.warning("a Telegram message could not be updated: %s", redact_token(exc, cfg.token))
            return False
        return True

    def _fit(self, head: str, ap: Approval) -> str:
        text = f"{head}\n\n{self._body(ap)}"
        return text if text_length(text) <= MAX_TEXT else head + "\n\n" + "\n".join(self._facts(ap))

    async def _show(self, cfg: TelegramConfig, chat_id: int, message_id: int, ap: Approval, view: str,
                    head: str) -> bool:
        """Replace a message's buttons with `head` and the approval, once per view. False: Telegram did
        not take it, so it is tried again later."""
        final = ap.state != "pending"
        if not await self._edit(cfg, chat_id, message_id, self._fit(head, ap), None if final else ap):
            return False
        now = self.clock()
        self.db.execute("UPDATE telegram_prompts SET shown = ?, used_at = COALESCE(used_at, ?), closed_at = ? "
                        "WHERE chat_id = ? AND message_id = ?", (view, now, now if final else None, chat_id, message_id))
        return True

    async def sync_outcomes(self, budget_s: float = SYNC_BUDGET_S) -> int:
        """Every open message whose approval was decided anywhere, or whose buttons expired, shows that now.
        Returns the messages changed. It stops at the first message Telegram does not take and after
        `budget_s`, so an outage at Telegram never holds up whoever called it; the rest waits for the next call."""
        cfg = self.config()
        if cfg is None:
            return 0
        now = self.clock()
        deadline = time.monotonic() + budget_s
        rows = self.db.all("SELECT approval_id, chat_id, message_id, MAX(expires_at) AS until, MAX(shown) AS shown "
                           "FROM telegram_prompts WHERE message_id IS NOT NULL AND closed_at IS NULL "
                           "GROUP BY approval_id, chat_id, message_id")
        changed = 0
        for r in rows:
            try:
                ap = self.approvals.get(r["approval_id"])
            except NotFound:
                self.db.execute("UPDATE telegram_prompts SET closed_at = ? WHERE approval_id = ?", (now, r["approval_id"]))
                continue
            if time.monotonic() > deadline:
                break
            if ap.state != "pending":
                shown = await self._show(cfg, r["chat_id"], r["message_id"], ap, "outcome", self._outcome(ap))
            elif r["until"] <= now and r["shown"] == "buttons":
                shown = await self._show(cfg, r["chat_id"], r["message_id"], ap, "expired", "These buttons have "
                                         "expired. Nothing was decided here. Open the panel to decide.")
            else:
                continue
            if not shown:
                break
            changed += 1
        return changed

    # ---- what comes back ----
    async def handle_update(self, update: dict) -> None:
        if isinstance(update.get("callback_query"), dict):
            await self._on_tap(update["callback_query"])
        elif isinstance(update.get("message"), dict):
            await self._on_message(update["message"])

    async def _on_message(self, msg: dict) -> None:
        """Only `/start <code>`, in a private chat, from the account the chat belongs to."""
        text = msg.get("text") if isinstance(msg.get("text"), str) else ""
        if not text.startswith("/start"):
            return
        cfg = self.config()
        if cfg is None:
            return
        chat, sender = msg.get("chat") or {}, msg.get("from") or {}
        parts = text.split()
        code = parts[1] if len(parts) == 2 and parts[0] == "/start" else ""
        private = chat.get("type") == "private" and isinstance(chat.get("id"), int) and \
            chat.get("id") == sender.get("id") and not sender.get("is_bot")
        if not private:
            if self._first_start(sender.get("id") or chat.get("id")):
                log.warning("a Telegram /start outside a private chat was ignored (chat %s)", chat.get("id"))
                self._trace("denied", "ignored a Telegram /start outside a private chat", owner=False)
            return
        pending = self.pairing_pending()
        if not code or len(code) > 64 or not self._claim_code(code):
            if not self._first_start(sender.get("id")):
                return  # this sender was logged and answered a moment ago: a flood writes one row
            log.warning("a Telegram /start with no valid pairing code was ignored (user %s)", sender.get("id"))
            self._trace("denied", f"ignored a Telegram /start with no valid pairing code (user {sender.get('id')})",
                        owner=False)
            if pending:
                await self._say(cfg, chat["id"], "This link is not valid or has expired. Run openajar policy notify "
                                                 "telegram pair on the node for a new one.")
            return
        old = cfg.chat_id if cfg.paired else None
        username = _clean(sender.get("username"), 64) or None
        self.notifier.save_telegram(cfg.model_copy(update={"chat_id": chat["id"], "user_id": sender["id"],
                                                           "username": username, "paired_at": self.clock()}))
        self._stamp = None
        who = f"@{username}" if username else f"user {sender['id']}"
        self._trace("ok", f"paired the Telegram account {who}")
        await self._say(cfg, chat["id"], "This chat is now paired with your openajar node. What waits for you comes "
                                         "here. Telegram can approve only what a chat prompt can; everything else "
                                         "has Deny and a link to the panel.")
        if old is not None and old != chat["id"]:
            await self._say(cfg, old, "Approvals from this openajar node now go to another Telegram account. If that "
                                      "was not you, run openajar policy notify telegram remove on the node.")
        self.notifier.send({"type": "telegram.paired", "title": "Telegram paired", "node_text": True,
                            "body": f"Approvals now also go to the Telegram account {who}. If that was not you, run "
                                    "openajar policy notify telegram remove on the node."})

    def _first_start(self, sender: Any) -> bool:
        """Is this the first ignored /start from `sender` in START_QUIET_S? Anyone can message a bot, so
        each sender is logged (and answered) once a window, in memory: a restart only logs it again."""
        now = time.monotonic()
        key = sender if isinstance(sender, int | str) else None
        last = self._starts.get(key)
        if last is not None and now - last < START_QUIET_S:
            return False
        self._starts[key] = now
        if len(self._starts) > START_SENDERS:
            for old in sorted(self._starts, key=self._starts.__getitem__)[: len(self._starts) - START_SENDERS]:
                del self._starts[old]
        return True

    def _claim_code(self, code: str) -> bool:
        now = self.clock()
        with self.db.tx() as conn:
            cur = conn.execute("UPDATE telegram_pairing SET used_at = ? WHERE code_hash = ? AND used_at IS NULL "
                               "AND expires_at > ?", (now, _code_hash(code), now))
            return cur.rowcount == 1

    async def _say(self, cfg: TelegramConfig, chat_id: int, text: str) -> None:
        try:
            await self.api(cfg).call("sendMessage", {"chat_id": chat_id, "text": text})
        except TelegramError as exc:
            log.warning("a Telegram message could not be sent: %s", redact_token(exc, cfg.token))

    async def _answer(self, cfg: TelegramConfig, query_id: Any, text: str) -> None:
        if not isinstance(query_id, str):
            return
        try:
            await self.api(cfg).call("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:200]})
        except TelegramError as exc:
            log.warning("a Telegram tap could not be answered: %s", redact_token(exc, cfg.token))

    async def _on_tap(self, q: dict) -> None:
        """A tap on a button. Who, where, which prompt, whether it is still good, then the store decides."""
        cfg = self.config()
        if cfg is None:
            return
        sender, msg = q.get("from") or {}, q.get("message") or {}
        chat = msg.get("chat") or {}
        if not cfg.paired or sender.get("id") != cfg.user_id or chat.get("id") != cfg.chat_id or \
                chat.get("type") != "private":
            log.warning("a Telegram tap from an account or chat that is not paired was ignored (user %s, chat %s)",
                        sender.get("id"), chat.get("id"))
            self._trace("denied", f"ignored a Telegram tap from an account or chat that is not paired "
                                  f"(user {sender.get('id')})", owner=False)
            return
        qid, data = q.get("id"), q.get("data")
        if self.off():
            self._trace("denied", "a Telegram tap was refused: Telegram is turned off on this node")
            await self._answer(cfg, qid, "Telegram is turned off on this node. Open the panel to decide.")
            return
        row = self.db.one("SELECT * FROM telegram_prompts WHERE prompt_id = ?", (data,)) \
            if isinstance(data, str) and 0 < len(data) <= 64 else None
        if row is not None and row["message_id"] is None and row["shown"] == "unknown" and \
                row["chat_id"] == chat.get("id") and isinstance(msg.get("message_id"), int):
            row = self._adopt(row, msg["message_id"])
        if row is None or row["decision"] not in ("approve", "deny") or row["chat_id"] != chat.get("id") or \
                row["message_id"] is None or row["message_id"] != msg.get("message_id"):
            self._trace("denied", "a Telegram tap on a button this node did not make was refused")
            await self._answer(cfg, qid, "This button is not one this node made.")
            return
        approval_id, decision, now = row["approval_id"], row["decision"], self.clock()
        if row["used_at"] is not None:
            await self._answer(cfg, qid, "This message was already answered.")
            return
        if row["expires_at"] <= now:
            await self._answer(cfg, qid, "These buttons have expired. Open the panel to decide.")
            await self.sync_outcomes()
            return
        if decision == "approve" and now - float(row["sent_at"] or now) < MIN_ANSWER_S:
            self._trace("denied", "a Telegram Approve faster than a person reads did not count", approval_id=approval_id)
            await self._answer(cfg, qid, "That was faster than a person reads. Read the message, then tap Approve again.")
            return
        if not self._claim(row["prompt_id"], chat["id"], row["message_id"], now):
            await self._answer(cfg, qid, "This message was already answered.")
            return
        try:
            said = await self._decide(cfg, row, decision)
        except Exception:  # the node failed, not the person: the buttons work again and nothing was decided here
            log.exception("a Telegram tap could not be decided")
            self.db.execute("UPDATE telegram_prompts SET used_at = NULL WHERE chat_id = ? AND message_id = ? "
                            "AND closed_at IS NULL", (chat["id"], row["message_id"]))
            self._trace("error", "a Telegram tap could not be decided: the node failed", approval_id=approval_id)
            await self._answer(cfg, qid, "The node could not record that. Nothing was decided. Tap again, or open "
                                         "the panel.")
            return
        await self._answer(cfg, qid, said)
        await self.sync_outcomes()

    def _adopt(self, row: Any, message_id: int) -> Any:
        """A message whose send timed out did arrive: the paired account tapped one of its buttons, whose
        random id only that message holds. Its prompts get the message's id, so it works as any other."""
        self.db.execute("UPDATE telegram_prompts SET message_id = ?, shown = 'buttons' WHERE approval_id = ? "
                        "AND chat_id = ? AND created_at = ? AND shown = 'unknown' AND message_id IS NULL",
                        (message_id, row["approval_id"], row["chat_id"], row["created_at"]))
        return self.db.one("SELECT * FROM telegram_prompts WHERE prompt_id = ?", (row["prompt_id"],))

    def _claim(self, prompt_id: str, chat_id: int, message_id: int, now: float) -> bool:
        """Spend this prompt and every other button on its message, once."""
        with self.db.tx() as conn:
            cur = conn.execute("UPDATE telegram_prompts SET used_at = ? WHERE prompt_id = ? AND used_at IS NULL",
                               (now, prompt_id))
            if cur.rowcount != 1:
                return False
            conn.execute("UPDATE telegram_prompts SET used_at = ? WHERE chat_id = ? AND message_id = ? "
                         "AND used_at IS NULL", (now, chat_id, message_id))
        return True

    async def _decide(self, cfg: TelegramConfig, row: Any, decision: str) -> str:
        """Decide through the store, as every route does, so audit, freezes and standing rules hold."""
        try:
            ap = self.approvals.get(row["approval_id"])
        except NotFound:
            return "This approval is gone."
        if ap.state != "pending":
            return f"It was already {ap.state}."
        if (ap.content_hash, ap.content_digest) != (row["content_hash"], row["content_digest"]):
            return await self._not_decided(cfg, row, ap, "conflict", "the content changed after this message was sent")
        if self.off(ap.workspace):  # turned off in its workspace after the message went: no tap decides it
            return await self._not_decided(cfg, row, ap, "refused", "Telegram is turned off in this workspace")
        if decision == "approve":
            why = self.approve_refusal(ap)
            if why:
                return await self._not_decided(cfg, row, ap, "refused", f"Telegram cannot approve this: {why}")
        who = f"@{cfg.username}" if cfg.username else f"user {cfg.user_id}"
        proof = Proof(METHOD, ConfirmLevel.CHAT, self.clock(), {"account": who})
        try:
            done = self.approvals.decide(ap.id, decision, by="telegram:owner", via=METHOD, in_person=True, proof=proof,
                                         content=(row["content_hash"], row["content_digest"]))
        except Conflict:
            now = self.approvals.get(ap.id)
            if now.state == "pending":
                return await self._not_decided(cfg, row, now, "conflict", "the content changed after this message was sent")
            return f"It was already {now.state}."
        except OpenajarError as exc:
            return await self._not_decided(cfg, row, ap, "refused", f"it was not {decision}d: {exc.message}")
        self._trace("ok", f"{decision} through Telegram ({who})", approval_id=ap.id)
        return "Approved." if done.state == "approved" else "Denied."

    async def _not_decided(self, cfg: TelegramConfig, row: Any, ap: Approval, view: str, why: str) -> str:
        self._trace("denied", f"nothing was decided through Telegram: {why}", approval_id=ap.id)
        await self._show(cfg, row["chat_id"], row["message_id"], ap, view,
                         f"Nothing was decided: {why}. Open the panel to decide.")
        return f"Nothing was decided: {why}."[:200]

    # ---- the poller ----
    def should_poll(self) -> bool:
        cfg = self.config()
        return cfg is not None and (cfg.paired or self.pairing_pending())

    def _offset(self) -> int:
        try:
            return int(self._state("offset") or 0)
        except ValueError:
            return 0

    async def poll_once(self) -> float:
        """One getUpdates round. Returns how long to wait before the next (0: at once)."""
        cfg = self.config()
        if cfg is None:
            return 5.0
        try:
            updates = await self.api(cfg).call(
                "getUpdates", {"offset": self._offset(), "timeout": POLL_TIMEOUT_S,
                               "allowed_updates": ["message", "callback_query"]}, timeout_s=POLL_TIMEOUT_S + 15)
        except TelegramError as exc:
            self._errors += 1
            said = redact_token(exc, cfg.token)
            self._set_state("last_error", said[:300])
            if exc.status == 409:  # a webhook on the bot, or another program reading its updates
                self._set_state("conflict", said[:300])
            log.warning("reading Telegram failed: %s", said)
            return exc.retry_after or min(BACKOFF_MAX_S, 2.0 ** min(self._errors, 9))
        self._errors = 0
        if self._state("conflict") is not None:
            self.db.execute("DELETE FROM telegram_state WHERE key = 'conflict'")
        for update in updates if isinstance(updates, list) else []:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if not isinstance(update_id, int):
                continue
            try:
                await self.handle_update(update)
            except Exception:  # one bad update never stops the next
                log.exception("a Telegram update could not be handled")
            self._set_state("offset", str(update_id + 1))
        await self.sync_outcomes()
        return 0.0

    async def run(self) -> None:
        while True:
            try:
                delay = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("the Telegram poller failed a round")
                delay = 30.0
            if delay:
                await asyncio.sleep(delay)

    def supervise(self) -> None:
        """Start the poller when there is a bot with a paired account or a pairing waiting; stop it when not.
        Called from the policy service's background loop, so the CLI's changes reach a running node."""
        want = self.should_poll()
        running = self._task is not None and not self._task.done()
        if want and not running:
            self._task = asyncio.get_running_loop().create_task(self.run())
        elif not want and running:
            assert self._task is not None
            self._task.cancel()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def describe(status: dict) -> str:
    """`openajar policy notify telegram status` for a person."""
    if not status.get("bot"):
        return "Telegram is not set up. Start with: openajar policy notify telegram set-token"
    lines = [f"bot: @{status['bot']}"]
    if status.get("paired"):
        who = f"@{status['username']}" if status.get("username") else f"user {status.get('user_id')}"
        lines.append(f"paired with: {who}")
    else:
        lines.append("paired with: nobody yet" + (" (a pairing link is waiting)" if status.get("pairing") else ""))
    lines.append("turned off in the confirmation setting" if status.get("turned_off") else "on")
    if status.get("conflict"):
        lines.append("not reading: Telegram says another program reads this bot's updates (a webhook, or a "
                     "second node with the same token). Run openajar policy notify telegram set-token again to "
                     "remove a webhook, or stop the other program.")
    if status.get("last_error"):
        lines.append(f"last error: {status['last_error']}")
    return "\n".join(lines)

