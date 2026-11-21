"""Telling the owner something needs them: ntfy pushes, signed webhooks, the owner's Telegram chat, and a
log line. Telegram (openajar.policy.telegram) is the owner's alone, and its messages are built from the
approval at delivery time, with buttons that can approve what a chat prompt can.

`send(event)` only writes to the outbox (SQLite) and returns; delivery happens in
`flush()`, from the background loop or explicitly. A channel that is down keeps its rows
with exponential backoff, so a failing phone push never blocks or loses an approval (the
approval itself lives in the approvals table regardless). Each channel is rate limited;
when several ntfy messages are due at once they go out as one batched push.

Never in a notification: tokens, secrets, raw content. Events are redacted when queued.

No link ever approves (SEC-8, AUD-44). When a public base URL is configured, an actionable
push carries two buttons, built at delivery time and never stored: "Review", a signed link
that opens that approval in the panel, where the owner approves it signed in; and "Deny", a
one-tap signed POST for that one approval (denying is always safe). Both expire after
LINK_TTL_S. Without a public base URL the push only deep-links to the panel.

A code request (`code.requested`, docs/design/phones.md) only links to its code card in the
panel, where the owner types the code; it never carries a code and has no action buttons. It is
urgent (top ntfy priority), because the login waits for it.

A push topic on a public ntfy server is readable by anyone who knows its name, so an enabled
ntfy channel needs a long random topic (at least 32 characters, 16 of them different, with
letters and digits; `generate_topic()` and `openajar policy notify ntfy` without a topic make
one). Better still, run ntfy yourself on the tailnet with access control and a token, or use
the signed webhook to a push service you own (docs: policy/ROUTING.md, "Approvals on the phone").

Members: each member has their own channel and topic
(`NotifyPrefs`), their own time zone, quiet hours, digest times and batch window
(`openajar.policy.quiet_hours`); outbox rows are per member and carry the time they are due.
Who gets an event is `openajar.policy.recipients` (phase 1: whoever could decide it, else the
owner). A push to a member carries no content: the lane and the effect, never the summary.
Removing a member retires their topic and drops what was still waiting for them. A solo node
behaves as before: the owner gets everything, at once, until the owner sets quiet hours.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator

from openajar.policy.quiet_hours import MEMBER_DEFAULTS, OWNER_DEFAULTS, Plan, Timing, plan
from openajar.policy.recipients import OWNER, ApprovalFacts, GrantDirectory, MemberDirectory
from openajar.policy.redact import redact, redact_text
from openajar.policy.store import DB
from openajar.policy.util import iso

log = logging.getLogger("openajar.notify")

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,
  event TEXT NOT NULL,
  created_at REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL,
  last_error TEXT,
  delivered_at REAL,
  dead_at REAL
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(channel, delivered_at, dead_at, next_attempt_at);
"""
# columns added for members (B3): who a row is for, why it waits, urgency, and rows dropped
# before delivery (decided meanwhile, member removed)
_OUTBOX_MIGRATIONS = (("member_id", f"TEXT NOT NULL DEFAULT '{OWNER}'"), ("held", "TEXT"),
                      ("urgent", "INTEGER NOT NULL DEFAULT 0"), ("dropped_at", "REAL"), ("drop_reason", "TEXT"))

CHANNELS = ("ntfy", "webhook", "telegram", "log")
NODE = "node"  # the member id of log rows: a line in the node's log, not a push to anyone
LINK_TTL_S = 1800  # Review and Deny links in a push work for 30 minutes (then: open the panel)
TOPIC_MIN_CHARS = 32
TOPIC_MIN_DISTINCT = 16
MEMBER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
MAX_RETIRED_TOPICS = 1000
DECIDED = ("approved", "denied", "consumed", "cancelled", "answered")
_NOTIFY_KEYS = ("lane", "run_id", "schedule_id", "flow", "severity", "key")


def weak_topic(topic: str) -> str | None:
    """Why an ntfy topic is guessable, or None. Anyone who knows a topic on a public server can
    read its pushes, so the topic must be long and random (AUD-44)."""
    if len(topic) < TOPIC_MIN_CHARS:
        return f"the topic must be at least {TOPIC_MIN_CHARS} characters"
    if len(set(topic)) < TOPIC_MIN_DISTINCT or not any(c.isdigit() for c in topic) or not any(
            c.isalpha() for c in topic):
        return "the topic must look random (letters and digits, at least 16 different characters)"
    return None


def generate_topic() -> str:
    """A random topic (about 190 bits) that passes weak_topic."""
    while True:
        topic = "openajar-" + secrets.token_urlsafe(24)
        if weak_topic(topic) is None:
            return topic


def topic_hash(topic: str) -> str:
    return hashlib.sha256(topic.encode()).hexdigest()


def _topic_error(reason: str) -> str:
    return (f"{reason}; anyone who knows the topic can read the pushes. Leave the topic out to generate one, "
            "or run ntfy on your tailnet with access control")


class NtfyConfig(BaseModel):
    enabled: bool = True
    server: str = "https://ntfy.sh"
    topic: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    token: str | None = Field(default=None, repr=False)
    priority: int = Field(default=4, ge=1, le=5)

    @model_validator(mode="after")
    def _strong_topic(self) -> NtfyConfig:
        reason = weak_topic(self.topic) if self.enabled else None
        if reason:
            raise ValueError(_topic_error(reason))
        return self


class WebhookConfig(BaseModel):
    enabled: bool = True
    url: str
    secret: str = Field(min_length=16, repr=False)


class LogConfig(BaseModel):
    enabled: bool = True


class TelegramConfig(BaseModel):
    """The node's Telegram bot and the one account paired with it (openajar.policy.telegram). Written
    only by the node's CLI and by pairing, never by a settings PUT. The token is never shown."""
    token: str = Field(min_length=20, max_length=200, repr=False)
    bot_username: str | None = None
    chat_id: int | None = None   # the private chat with the paired account
    user_id: int | None = None   # the paired account: every button must come from it, in that chat
    username: str | None = None
    paired_at: float | None = None

    @property
    def paired(self) -> bool:
        return self.chat_id is not None and self.user_id is not None

    def public(self) -> dict:
        return {"bot": self.bot_username, "paired": self.paired, "username": self.username,
                "user_id": self.user_id, "paired_at": iso(self.paired_at)}


class NotifyPrefs(Timing):
    """One member's channel and timing. The topic is theirs alone and is retired on removal.
    `server` None: the node's ntfy server (and its token), else https://ntfy.sh."""
    channel: Literal["ntfy", "webhook", "none"] = "ntfy"
    topic: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    server: str | None = None
    webhook: WebhookConfig | None = None
    show_summary: bool = False  # members' pushes carry no content unless the owner turns this on

    @model_validator(mode="after")
    def _channel_ready(self) -> NotifyPrefs:
        if self.channel == "ntfy":
            if not self.topic:
                raise ValueError("an ntfy channel needs a topic (leave it out to generate one)")
            reason = weak_topic(self.topic)
            if reason:
                raise ValueError(_topic_error(reason))
        if self.channel == "webhook" and self.webhook is None:
            raise ValueError("a webhook channel needs a url and a secret")
        return self

    def timing(self) -> Timing:
        return Timing.model_validate(self.model_dump(include=set(Timing.model_fields)))

    def public(self, *, show_topic: bool) -> dict:
        topic = self.topic if show_topic else (f"...{self.topic[-4:]}" if self.topic else None)
        return {**self.timing().model_dump(), "channel": self.channel, "topic": topic, "server": self.server,
                "webhook": None if self.webhook is None else {
                    "enabled": self.webhook.enabled, "url": redact_text(self.webhook.url), "has_secret": True},
                "show_summary": self.show_summary}


LEGACY_PANEL_URL = "http://localhost:7400"  # what every config held before links used the node's own port


class NotifierConfig(BaseModel):
    ntfy: NtfyConfig | None = None
    webhook: WebhookConfig | None = None
    log: LogConfig = Field(default_factory=LogConfig)
    telegram: TelegramConfig | None = None  # set by `openajar policy notify telegram`, never by a PUT
    public_base_url: str | None = None  # e.g. https://openajar.example.com (enables action buttons)
    # the panel's address in links when there is no public URL; None: this node's own (Notifier.panel_url)
    panel_url: str | None = None
    rate_per_min: int = Field(default=6, ge=1, le=600)
    max_attempts: int = Field(default=12, ge=1)
    timeout_s: float = Field(default=10.0, gt=0)
    # members (B3); a config saved before them loads with the owner's timing unchanged
    owner: Timing = Field(default_factory=lambda: OWNER_DEFAULTS.model_copy())
    member_defaults: Timing = Field(default_factory=lambda: MEMBER_DEFAULTS.model_copy())
    members: dict[str, NotifyPrefs] = Field(default_factory=dict)
    retired_topics: list[str] = Field(default_factory=list)  # sha256 of topics never to push to again

    @model_validator(mode="before")
    @classmethod
    def _old_default_panel_url(cls, data: Any) -> Any:
        """A config saved before the link followed the node holds the old default on every node:
        it was never chosen, so the node's own address wins."""
        if isinstance(data, dict) and data.get("panel_url") == LEGACY_PANEL_URL:
            data = {**data, "panel_url": None}
        return data

    def public(self) -> dict:
        """What GET /api/settings/notifications returns: no token, no secret, no member topic."""
        return {
            "ntfy": None if self.ntfy is None else {
                "enabled": self.ntfy.enabled, "server": self.ntfy.server, "topic": self.ntfy.topic,
                "has_token": bool(self.ntfy.token), "priority": self.ntfy.priority},
            "webhook": None if self.webhook is None else {
                "enabled": self.webhook.enabled, "url": redact_text(self.webhook.url), "has_secret": True},
            "log": {"enabled": self.log.enabled},
            "telegram": None if self.telegram is None else self.telegram.public(),
            "public_base_url": self.public_base_url, "panel_url": self.panel_url,
            "rate_per_min": self.rate_per_min,
            "owner": self.owner.model_dump(), "member_defaults": self.member_defaults.model_dump(),
            "members": {m: p.public(show_topic=False) for m, p in sorted(self.members.items())},
        }


def webhook_signature(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def _member_id(member_id: str) -> str:
    if member_id in (OWNER, NODE) or not MEMBER_ID_RE.match(member_id or ""):
        raise ValueError(f"{member_id!r} is not a member id")
    return member_id


def _merge_secret(old: dict | None, new: dict | None, field: str) -> dict | None:
    """A PUT body for a section with a write-only secret: an absent secret keeps the old one."""
    if new is None:
        return None
    old = old or {}
    merged = {**old, **new}
    if new.get(field) is None and old.get(field):
        merged[field] = old[field]
    return merged


def _when(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


class Notifier:
    """Runtime service "notifier"."""

    # A node is one org, `local` (docs/WEB-NODE.md). Links name it, so the app opens the page itself rather than
    # reading "needs-you" as an org's name.
    ORG = "local"

    def __init__(self, home: Path, db: DB, *, link_signer: Callable[[str, str, int], str] | None = None,
                 clock: Callable[[], float] = time.time, transport: httpx.AsyncBaseTransport | None = None,
                 directory: MemberDirectory | None = None, panel_url: str = LEGACY_PANEL_URL):
        self.home = home
        self.node_panel_url = panel_url  # where this node serves its panel (PolicyService: its own port)
        self.db = db
        with self.db.lock:
            self.db.conn.executescript(OUTBOX_SCHEMA)
            cols = {r[1] for r in self.db.conn.execute("PRAGMA table_info(outbox)")}
            for col, decl in _OUTBOX_MIGRATIONS:
                if col not in cols:
                    self.db.conn.execute(f"ALTER TABLE outbox ADD COLUMN {col} {decl}")
            self.db.conn.execute("CREATE INDEX IF NOT EXISTS outbox_member ON outbox(member_id, channel, delivered_at)")
        self.clock = clock
        self.transport = transport
        self.link_signer = link_signer
        # openajar.freeze sets this: (event, public base url) -> a signed one-tap Freeze link, or None
        self.freeze_link: Callable[[dict, str], str | None] | None = None
        # openajar.policy.telegram sets this: the owner's Telegram chat, a channel of the owner's only
        self.telegram: Any = None
        self.directory: MemberDirectory = directory or GrantDirectory(db, clock=clock)
        self.config_path = home / "secrets" / "notifier.json"
        self._flush_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None  # the daemon's loop, once seen: a flush from a worker thread asks it
        self._kick: asyncio.TimerHandle | None = None
        self._lanes: Any = None
        self.config = self.load_config()

    # ---- config (secrets/notifier.json, mode 600) ----
    def load_config(self) -> NotifierConfig:
        if not self.config_path.exists():
            return NotifierConfig()
        raw = self.config_path.read_text()
        try:
            return NotifierConfig.model_validate_json(raw)
        except ValidationError:
            # a config saved before topics had to be random: keep it, with ntfy off until fixed
            data = json.loads(raw)
            if isinstance(data.get("ntfy"), dict) and data["ntfy"].get("enabled", True):
                data["ntfy"]["enabled"] = False
                log.warning("ntfy is off: its topic is guessable; set a random one (openajar policy notify ntfy)")
            return NotifierConfig.model_validate(data)

    def save_config(self, cfg: NotifierConfig, *, telegram: bool = False) -> None:
        """Write the config (mode 600). The `telegram` section belongs to the CLI and to pairing
        (`save_telegram`): any other save keeps what the file holds there, so an older copy in a running
        daemon never undoes a pairing or brings back a removed token."""
        if not telegram:
            cfg = cfg.model_copy(update={"telegram": self.telegram_on_disk()})
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config_path.parent, 0o700)
        tmp = self.config_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(cfg.model_dump_json(indent=2))
        os.replace(tmp, self.config_path)
        self.config = cfg

    def telegram_on_disk(self) -> TelegramConfig | None:
        """The Telegram section as the file holds it now (another process may have changed it)."""
        try:
            data = json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            return None
        section = data.get("telegram") if isinstance(data, dict) else None
        if not isinstance(section, dict):
            return None
        try:
            return TelegramConfig.model_validate(section)
        except ValidationError:
            log.warning("the telegram section of notifier.json cannot be read; Telegram is off")
            return None

    def save_telegram(self, section: TelegramConfig | None) -> None:
        """Set or remove the Telegram section, over what the file holds now."""
        cfg = self.load_config().model_copy(update={"telegram": section})
        self.save_config(cfg, telegram=True)

    def _refuse_retired(self, topic: str | None, cfg: NotifierConfig) -> None:
        if topic and topic_hash(topic) in cfg.retired_topics:
            raise ValueError("that topic was retired (someone who lost access may still read it); "
                             "leave the topic out to generate a new one")

    def update_config(self, patch: dict) -> NotifierConfig:
        """Merge a PUT body; token and secret are write-only, so an absent value keeps the old one.
        `owner` and `member_defaults` take Timing fields (quiet hours, digests, batching)."""
        cur = self.config.model_dump()
        for section, secret_field in (("ntfy", "token"), ("webhook", "secret")):
            if section not in patch:
                continue
            merged = _merge_secret(cur.get(section), patch[section], secret_field)
            if section == "ntfy" and merged is not None and merged.get("topic") in (None, "", "generate"):
                merged["topic"] = generate_topic()
            cur[section] = merged
        for key in ("log", "public_base_url", "panel_url", "rate_per_min", "max_attempts", "timeout_s"):
            if key in patch:
                cur[key] = patch[key]
        for key in ("owner", "member_defaults"):
            if key in patch:
                cur[key] = {**cur[key], **(patch[key] or {})}
        cfg = NotifierConfig.model_validate(cur)
        if cfg.ntfy is not None and (self.config.ntfy is None or cfg.ntfy.topic != self.config.ntfy.topic):
            self._refuse_retired(cfg.ntfy.topic, cfg)
        self.save_config(cfg)
        return cfg

    # ---- members: their own channel, topic and hours ----
    def member_settings(self, member_id: str) -> dict:
        """A member's own notification settings (GET /api/me/notify): the topic is shown, since
        they subscribe to it; a webhook secret is not. A member without settings sees the node
        defaults and channel "none"."""
        if member_id == OWNER:
            return {**self.config.owner.model_dump(), "channels": self.channels_for(OWNER)}
        prefs = self.config.members.get(_member_id(member_id))
        if prefs is None:
            return {**self.config.member_defaults.model_dump(), "channel": "none", "topic": None, "server": None,
                    "webhook": None, "show_summary": False}
        return prefs.public(show_topic=True)

    def update_member(self, member_id: str, patch: dict) -> dict:
        """Create or change a member's settings (PUT /api/me/notify). New members start from the
        node defaults with a generated ntfy topic; `topic: "generate"` makes a new one (the old one
        is retired). The webhook secret is write-only. The owner's timing goes through here too."""
        if member_id == OWNER:
            fields = set(Timing.model_fields)
            self.update_config({"owner": {k: v for k, v in patch.items() if k in fields}})
            return self.member_settings(OWNER)
        member_id = _member_id(member_id)
        cfg = self.config.model_copy(deep=True)
        old = cfg.members.get(member_id)
        cur = old.model_dump() if old else {**cfg.member_defaults.model_dump(), "channel": "ntfy"}
        new = dict(patch)
        if "webhook" in new:
            new["webhook"] = _merge_secret(cur.get("webhook"), new["webhook"], "secret")
        merged = {**cur, **new}
        if merged.get("channel") == "ntfy" and merged.get("topic") in (None, "", "generate"):
            merged["topic"] = generate_topic()
        prefs = NotifyPrefs.model_validate(merged)
        if old is not None and old.topic and old.topic != prefs.topic:
            self._retire(cfg, old.topic)
        if old is None or old.topic != prefs.topic:
            self._refuse_retired(prefs.topic, cfg)
        cfg.members[member_id] = prefs
        self.save_config(cfg)
        return self.member_settings(member_id)

    def _retire(self, cfg: NotifierConfig, topic: str) -> None:
        h = topic_hash(topic)
        if h not in cfg.retired_topics:
            cfg.retired_topics = [*cfg.retired_topics, h][-MAX_RETIRED_TOPICS:]

    def rotate_topic(self, member_id: str = OWNER) -> str:
        """A new random topic for a member (or the owner); the old one is retired: nothing is ever
        pushed to it again. Returns the new topic, to subscribe to."""
        if member_id == OWNER:
            if self.config.ntfy is None:
                raise ValueError("ntfy is not configured")
            cfg = self.config.model_copy(deep=True)
            self._retire(cfg, self.config.ntfy.topic)
            self.save_config(cfg)
            return self.update_config({"ntfy": {"topic": "generate"}}).ntfy.topic  # type: ignore[union-attr]
        prefs = self.config.members.get(_member_id(member_id))
        if prefs is None or prefs.channel != "ntfy":
            raise ValueError(f"{member_id} has no ntfy channel")
        return self.update_member(member_id, {"topic": "generate"})["topic"]

    def remove_member(self, member_id: str) -> int:
        """A member lost access (removed, suspended): their topic is retired, their settings are
        deleted and what was still waiting for them is dropped. Returns the rows dropped."""
        member_id = _member_id(member_id)
        cfg = self.config.model_copy(deep=True)
        prefs = cfg.members.pop(member_id, None)
        if prefs is not None and prefs.topic:
            self._retire(cfg, prefs.topic)
        self.save_config(cfg)
        cur = self.db.execute("UPDATE outbox SET dropped_at = ?, drop_reason = 'member removed' WHERE member_id = ? "
                              "AND delivered_at IS NULL AND dead_at IS NULL AND dropped_at IS NULL",
                              (self.clock(), member_id))
        return cur.rowcount

    def timing_for(self, member_id: str) -> Timing:
        if member_id == OWNER:
            return self.config.owner
        prefs = self.config.members.get(member_id)
        return prefs.timing() if prefs is not None else self.config.member_defaults

    def channels_for(self, member_id: str) -> list[str]:
        """The push channels a member (or the owner) receives on; log is the node's, not theirs."""
        c = self.config
        if member_id == OWNER:
            out = []
            if c.ntfy and c.ntfy.enabled:
                out.append("ntfy")
            if c.webhook and c.webhook.enabled:
                out.append("webhook")
            if self.telegram is not None and self.telegram.sending():
                out.append("telegram")
            return out
        prefs = c.members.get(member_id)
        if prefs is None or prefs.channel == "none":
            return []
        if prefs.channel == "webhook" and not (prefs.webhook and prefs.webhook.enabled):
            return []
        return [prefs.channel]

    def enabled_channels(self) -> list[str]:
        c = self.config
        out = self.channels_for(OWNER)
        if c.log.enabled:
            out.append("log")
        return out

    # ---- who gets what ----
    def _facts(self, approval_id: Any) -> ApprovalFacts | None:
        if not isinstance(approval_id, str):
            return None
        try:
            row = self.db.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        except Exception:  # noqa: BLE001 - no approvals table (a bare notifier)
            return None
        return ApprovalFacts.from_row(row) if row is not None else None

    def _pushable(self) -> list[str]:
        try:
            ids = self.directory.member_ids()
        except Exception:  # a broken directory falls back to the owner
            log.exception("member directory failed; pushing the owner")
            return []
        return [m for m in ids if m != OWNER and self.channels_for(m)]

    def recipients(self, event: dict, facts: ApprovalFacts | None = None) -> list[str]:
        """Phase 1 (openajar.policy.recipients): members who could decide the approval, or take
        over the lane the event is about; the owner when nobody else can. An event the node
        addresses itself (`to`: member ids, "owner" for the node owner) goes to those people
        only, whoever controls its lane."""
        if event.get("type") == "test":
            return [event.get("member") or OWNER]
        if isinstance(event.get("to"), list):  # named by the node (meter notices): those people only
            named = {m for m in event["to"] if isinstance(m, str)}
            people = [m for m in self._pushable() if m in named]
            return [OWNER, *people] if OWNER in named or not people else people
        chosen: list[str] = []
        try:
            if facts is not None:
                chosen = [m for m in self._pushable() if self.directory.can_decide(m, facts)]
            elif isinstance(event.get("lane"), str):
                chosen = [m for m in self._pushable() if self.directory.can_control(m, event["lane"])]
        except Exception:
            log.exception("member directory failed; pushing the owner")
            chosen = []
        return chosen or [OWNER]

    def _content_free(self, event: dict, facts: ApprovalFacts | None) -> dict:
        """What a member's push may say: which lane, which effect, never the summary or the
        preview (a routing mistake must not leak content; section 4.12)."""
        keep = {k: event[k] for k in ("type", "at", "approval_id", "actionable", "expires_ts", "lane", "effect",
                                      "urgent", "not_after", "key", "member") if k in event}
        if facts is not None:
            lane = facts.lane or "openajar"
            if facts.kind == "question":
                keep.update(title=f"{lane}: a question needs you", body="Open the panel to answer it.")
            else:
                keep.update(title=f"{lane}: {facts.effect or 'an action'} needs you",
                            body="Open the panel to review it.")
            return keep
        if event.get("node_text"):  # written by the node itself (a lane hold), no content in it
            keep.update(title=event.get("title"), body=event.get("body"))
            return keep
        keep.update(title=str(event.get("title") or "openajar")[:80], body="Open the panel to see it.")
        return keep

    # ---- queueing ----
    def send(self, event: dict) -> list[int]:
        """Queue an event for its recipients' channels and the node log. Never raises, never
        blocks on a network."""
        try:
            return self._send(event)
        except Exception:
            log.exception("notification could not be queued")
            return []

    def _send(self, event: dict, *, immediate: bool = False) -> list[int]:
        now = self.clock()
        safe = redact({**event, "at": event.get("at") or iso(now)})
        facts = self._facts(safe.get("approval_id"))
        if facts is not None:
            safe.setdefault("lane", facts.lane)
            safe.setdefault("effect", facts.effect)
        not_after = facts.not_after if facts and facts.not_after else _when(safe.get("not_after"))
        urgent = bool(safe.get("urgent"))
        people = self.recipients(safe, facts)  # outside the write lock: a directory may be slow
        safe.pop("to", None)
        ids: list[int] = []
        with self.db.tx() as conn:
            if self.config.log.enabled:
                ids.append(self._insert(conn, "log", NODE, safe, Plan(now, None, urgent), now))
            plans = {m: self._plans(conn, m, now, urgent=urgent, not_after=not_after, immediate=immediate)
                     for m in people}
            # stale-aware fallback: an urgent approval nobody chosen gets in time goes to the owner too
            if OWNER not in plans and not_after is not None and any(p.urgent for ps in plans.values() for _, p in ps) \
                    and not any(p.due <= not_after for ps in plans.values() for _, p in ps):
                plans[OWNER] = self._plans(conn, OWNER, now, urgent=True, not_after=not_after, immediate=immediate)
            for member, chs in plans.items():
                body = safe if member == OWNER or self._shows_summary(member) else self._content_free(safe, facts)
                for ch, p in chs:
                    ids.append(self._insert(conn, ch, member, body, p, now))
        self._schedule_flush()
        return ids

    def _shows_summary(self, member: str) -> bool:
        prefs = self.config.members.get(member)
        return bool(prefs and prefs.show_summary)

    def _plans(self, conn: Any, member: str, now: float, *, urgent: bool, not_after: float | None,
               immediate: bool) -> list[tuple[str, Plan]]:
        timing = self.timing_for(member)
        out = []
        for ch in self.channels_for(member):
            if immediate:
                out.append((ch, Plan(now, None, urgent)))
                continue
            last = conn.execute("SELECT MAX(delivered_at) FROM outbox WHERE member_id = ? AND channel = ?",
                                (member, ch)).fetchone()[0]
            batch = conn.execute("SELECT MIN(next_attempt_at) FROM outbox WHERE member_id = ? AND channel = ? "
                                 "AND held = 'batch' AND attempts = 0 AND delivered_at IS NULL AND dead_at IS NULL "
                                 "AND dropped_at IS NULL", (member, ch)).fetchone()[0]
            out.append((ch, plan(now, timing, urgent=urgent, not_after=not_after, last_push=last, open_batch=batch)))
        return out

    def _insert(self, conn: Any, ch: str, member: str, event: dict, p: Plan, now: float) -> int:
        body = json.dumps({**event, "urgent": True} if p.urgent else event, default=str)
        cur = conn.execute("INSERT INTO outbox (channel, event, created_at, next_attempt_at, member_id, held, urgent) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)", (ch, body, now, p.due, member, p.held, int(p.urgent)))
        return int(cur.lastrowid or 0)

    def notify(self, title: str, body: str = "", *, level: str | None = None, kind: str | None = None,
               urgent: bool = False, data: dict | None = None) -> list[int]:
        """The call the runner, schedules and brains make (title, body, level or kind, urgent, data)."""
        extra = {k: v for k, v in (data or {}).items()
                 if k in _NOTIFY_KEYS and isinstance(v, str | int | float) and not isinstance(v, bool)}
        if level and "severity" not in extra:
            extra["severity"] = level
        return self.send({"type": kind or "notice", "title": title, "body": body, "urgent": bool(urgent), **extra})

    # ---- a lane is held: one push (AUD-07) ----
    def on_lane_control(self, lane_id: str, old: Any, new: Any) -> list[int]:
        """Lane control listener: one push when the hold reason changes to a new value (a hold
        starts, or a different one replaces it), none when it clears or stays the same."""
        reason = getattr(new, "reason", None)
        if not reason or reason == getattr(old, "reason", None):
            return []
        clears = getattr(new, "hold_until", None) is not None
        text = (f"Agents are paused on {lane_id}: {reason}. "
                + ("It clears itself later; open the panel to see when." if clears
                   else "Open the panel to take over or hand it back."))
        return self.send({"type": "lane.held", "lane": lane_id, "reason": reason, "title": f"{lane_id} is held",
                          "body": text, "node_text": True, "key": f"lane.held:{lane_id}"})

    def listen_for_lane_control(self, lanes: Any) -> bool:
        """Register on_lane_control with the lanes service, once. True if registered."""
        add = getattr(lanes, "add_control_listener", None)
        if self._lanes is lanes:
            return True
        if not callable(add):
            return False
        add(self.on_lane_control)
        self._lanes = lanes
        return True

    def attach(self, runtime: Any) -> bool:
        """Composition hook: listen to the runtime's lanes (call after they are registered)."""
        lanes = runtime.get("lanes") if runtime is not None else None
        return lanes is not None and self.listen_for_lane_control(lanes)

    def _schedule_flush(self, delay: float = 1.0) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # a store's worker thread (policy.db runs authorize there): ask the loop
            loop = self._loop
            if loop is not None and not loop.is_closed() and loop.is_running():
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._schedule_flush, delay)
            return
        self._loop = loop
        if self._kick is not None and not self._kick.cancelled():
            return

        def run() -> None:
            self._kick = None
            loop.create_task(self.flush())

        self._kick = loop.call_later(delay, run)

    def outbox(self, *, include_delivered: bool = False, limit: int = 100, member: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM outbox WHERE 1 = 1", []
        if not include_delivered:
            sql += " AND delivered_at IS NULL AND dropped_at IS NULL"
        if member is not None:
            sql += " AND member_id = ?"
            args.append(member)
        rows = self.db.all(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [{"id": r["id"], "channel": r["channel"], "member": r["member_id"], "attempts": r["attempts"],
                 "next_attempt_at": iso(r["next_attempt_at"]), "held": r["held"], "urgent": bool(r["urgent"]),
                 "last_error": r["last_error"], "delivered_at": iso(r["delivered_at"]),
                 "dead": r["dead_at"] is not None, "dropped": r["drop_reason"],
                 "event": json.loads(r["event"])} for r in rows]

    # ---- delivery ----
    async def flush(self) -> dict[str, int]:
        """Deliver what is due. Returns delivered counts per channel."""
        async with self._flush_lock:
            out: dict[str, int] = {}
            async with httpx.AsyncClient(transport=self.transport, timeout=self.config.timeout_s) as client:
                for ch in CHANNELS:
                    try:
                        out[ch] = await self._flush_channel(ch, client)
                    except Exception:
                        log.exception("notifier channel %s failed", ch)
                        out[ch] = 0
            return out

    def _rate_room(self, ch: str, member: str, now: float) -> int:
        if ch == "log":
            return 10_000
        # an ntfy batch counts once: count distinct delivery instants
        what = "COUNT(DISTINCT delivered_at)" if ch == "ntfy" else "COUNT(*)"
        row = self.db.one(f"SELECT {what} AS n FROM outbox WHERE channel = ? AND member_id = ? AND delivered_at >= ?",
                          (ch, member, now - 60))
        return max(0, self.config.rate_per_min - (row["n"] if row else 0))

    def _drop(self, ids: list[int], reason: str) -> None:
        if ids:
            self.db.execute(f"UPDATE outbox SET dropped_at = ?, drop_reason = ? WHERE id IN ({','.join('?' * len(ids))})",
                            (self.clock(), reason, *ids))

    def _current(self, rows: list[Any], now: float) -> list[tuple[Any, dict]]:
        """The rows still worth pushing, with their events: an approval decided meanwhile is
        dropped; one that expired while held is kept and marked missed (the morning digest
        lists it)."""
        keep, decided = [], []
        for r in rows:
            ev = json.loads(r["event"])
            facts = self._facts(ev.get("approval_id"))
            if facts is not None:
                if facts.state in DECIDED:
                    decided.append(r["id"])
                    continue
                if facts.state == "expired" or (facts.expires_at is not None and facts.expires_at <= now):
                    ev = {**ev, "missed": True, "actionable": False}
            keep.append((r, ev))
        self._drop(decided, "decided before delivery")
        return keep

    async def _flush_channel(self, ch: str, client: httpx.AsyncClient) -> int:
        now = self.clock()
        rows = self.db.all("SELECT * FROM outbox WHERE channel = ? AND delivered_at IS NULL AND dead_at IS NULL "
                           "AND dropped_at IS NULL AND next_attempt_at <= ? ORDER BY id LIMIT 500", (ch, now))
        if not rows:
            return 0
        by_member: dict[str, list[Any]] = {}
        for r in rows:
            by_member.setdefault(r["member_id"], []).append(r)
        delivered = 0
        for member, group in by_member.items():
            if ch != "log" and ch not in self.channels_for(member):
                # the owner's rows wait for the channel to come back, as before; a Telegram message never
                # does: one turned off or unpaired sends nothing, then or later
                if member != OWNER or ch == "telegram":
                    gone = member != OWNER and member not in self.config.members
                    self._drop([r["id"] for r in group], "member removed" if gone else "channel changed")
                continue
            if ch == "log" and not self.config.log.enabled:
                continue
            delivered += await self._flush_member(ch, member, group, client, now)
        return delivered

    async def _flush_member(self, ch: str, member: str, rows: list[Any], client: httpx.AsyncClient,
                            now: float) -> int:
        room = self._rate_room(ch, member, now)
        if room <= 0:
            return 0
        items = self._current(rows, now)
        if not items:
            return 0
        if ch == "ntfy":
            batch = items[:50]
            ok, err = await self._deliver_ntfy(client, [ev for _, ev in batch], member=member)
            self._mark([r["id"] for r, _ in batch], ok, err, [r["attempts"] for r, _ in batch])
            return len(batch) if ok else 0
        delivered = 0
        if ch == "telegram":  # one message each, built from the approval as it is now (openajar.policy.telegram)
            for r, event in items[:room]:
                ok, err = await self.telegram.deliver(event)
                self._mark([r["id"]], ok, err, [r["attempts"]])
                delivered += int(ok)
            return delivered
        if ch == "webhook":
            held = [(r, ev) for r, ev in items if r["held"]]
            if len(held) > 1:  # a digest or a batch: one POST for all of it
                ok, err = await self._deliver_webhook(client, {"type": "digest", "at": iso(now),
                                                               "events": [ev for _, ev in held]}, member=member)
                self._mark([r["id"] for r, _ in held], ok, err, [r["attempts"] for r, _ in held])
                delivered += len(held) if ok else 0
                room -= 1
                items = [(r, ev) for r, ev in items if not r["held"]]
        for r, event in items[:max(0, room)]:
            if ch == "webhook":
                ok, err = await self._deliver_webhook(client, event, member=member)
            else:
                ok, err = self._deliver_log(event), None
            self._mark([r["id"]], ok, err, [r["attempts"]])
            delivered += int(ok)
        return delivered

    def _mark(self, ids: list[int], ok: bool, err: str | None, attempts: list[int]) -> None:
        now = self.clock()
        with self.db.tx() as conn:
            for oid, n in zip(ids, attempts, strict=True):
                if ok:
                    conn.execute("UPDATE outbox SET delivered_at = ?, attempts = ?, last_error = NULL WHERE id = ?",
                                 (now, n + 1, oid))
                else:
                    n += 1
                    dead = now if n >= self.config.max_attempts else None
                    backoff = min(600.0, 5.0 * (2 ** (n - 1)))
                    conn.execute("UPDATE outbox SET attempts = ?, next_attempt_at = ?, last_error = ?, dead_at = ? "
                                 "WHERE id = ?", (n, now + backoff, redact_text(err or "failed", max_len=300),
                                                  dead, oid))

    def _link_expiry(self, expires_ts: float | None) -> int:
        return int(min(float(expires_ts or 1e18), self.clock() + LINK_TTL_S))

    def review_url(self, approval_id: str, expires_ts: float | None = None) -> str:
        """The signed Review link: it opens the approval in the panel, signed in, and decides nothing
        (SEC-8). Without a public URL, the plain link to the panel."""
        if not self.config.public_base_url or not self.link_signer:
            return self._deep_link({"approval_id": approval_id})
        base = self.config.public_base_url.rstrip("/")
        exp = self._link_expiry(expires_ts)
        return f"{base}/{self.ORG}/needs-you?approval={approval_id}&e={exp}&s={self.link_signer(approval_id, 'review', exp)}#{approval_id}"

    @property
    def panel_url(self) -> str:
        """The panel in a plain link: the owner's setting, else this node's own address."""
        return (self.config.panel_url or self.node_panel_url).rstrip("/")

    def _deep_link(self, event: dict) -> str:
        base = (self.config.public_base_url or self.panel_url).rstrip("/")
        ap = event.get("approval_id") or event.get("code_request_id")
        return f"{base}/{self.ORG}/needs-you#{ap}" if ap else f"{base}/{self.ORG}/needs-you"

    def _message(self, event: dict) -> tuple[str, str]:
        title = event.get("title") or "openajar"
        body = event.get("body") or event.get("summary") or ""
        if event.get("missed"):
            title = f"Missed: {title}"
            body = "It expired before anyone answered."
        elif event.get("urgent"):
            title = f"Urgent: {title}"
        return redact_text(str(title), max_len=120), redact_text(str(body), max_len=1500)

    def _ntfy_target(self, member: str) -> tuple[str, str, str | None, int]:
        """(server, topic, token, priority) for a member's pushes."""
        node = self.config.ntfy
        if member == OWNER:
            assert node is not None
            return node.server, node.topic, node.token, node.priority
        prefs = self.config.members[member]
        assert prefs.topic is not None
        server = prefs.server or (node.server if node else "https://ntfy.sh")
        token = node.token if node and node.server.rstrip("/") == server.rstrip("/") else None
        return server, prefs.topic, token, node.priority if node else 4

    def ntfy_payload(self, events: list[dict], *, member: str = OWNER) -> dict:
        _, topic, _, priority = self._ntfy_target(member)
        urgent = any(ev.get("urgent") and not ev.get("missed") for ev in events)
        tags = ["warning"] if urgent else ["robot"]
        if urgent:
            priority = 5
        if len(events) == 1:
            ev = events[0]
            title, body = self._message(ev)
            payload: dict[str, Any] = {"topic": topic, "title": title, "message": body,
                                       "priority": priority, "tags": tags, "click": self._deep_link(ev)}
            actions: list[dict] = []
            ap = ev.get("approval_id")
            if ap and ev.get("actionable") and self.config.public_base_url and self.link_signer:
                # never a link that approves: Review opens the panel, where the owner approves
                # signed in; Deny is one tap (SEC-8)
                base = self.config.public_base_url.rstrip("/")
                exp = self._link_expiry(ev.get("expires_ts"))
                deny = self.link_signer(ap, "deny", exp)
                actions.append({"action": "view", "label": "Review", "url": self.review_url(ap, ev.get("expires_ts"))})
                actions.append({"action": "http", "label": "Deny", "method": "POST", "clear": True,
                                "url": f"{base}/api/approvals/{ap}/action?d=deny&e={exp}&s={deny}"})
            else:
                actions.append({"action": "view", "label": "Open", "url": self._deep_link(ev)})
            actions += self._freeze_action(ev, member)
            payload["actions"] = actions
            return payload
        lines = []
        for ev in events[:10]:
            title, body = self._message({**ev, "missed": False, "urgent": False})
            text = body if member == OWNER or self._shows_summary(member) else title
            lines.append(("- missed: " if ev.get("missed") else "- ") + text[:140])
        if len(events) > 10:
            lines.append(f"and {len(events) - 10} more")
        return {"topic": topic, "title": f"{len(events)} things need you", "message": "\n".join(lines),
                "priority": priority, "tags": tags, "click": self._deep_link({}),
                "actions": [{"action": "view", "label": "Open", "url": self._deep_link({})},
                            *self._freeze_action({}, member)]}

    def _freeze_url(self, event: dict, member: str) -> str | None:
        """The one-tap Freeze link (openajar.freeze) for the owner's pushes: the event's lane, else the
        node. Signed, single use, short lived, built at delivery and never stored. It only freezes."""
        if member != OWNER or not self.config.public_base_url or self.freeze_link is None:
            return None
        try:
            return self.freeze_link(event, self.config.public_base_url)
        except Exception:
            log.warning("could not make a freeze link", exc_info=True)
            return None

    def _freeze_action(self, event: dict, member: str) -> list[dict]:
        url = self._freeze_url(event, member)
        if url is None:
            return []
        return [{"action": "http", "label": "Freeze", "method": "POST", "clear": True, "url": url}]

    async def _deliver_ntfy(self, client: httpx.AsyncClient, events: list[dict], *,
                            member: str = OWNER) -> tuple[bool, str | None]:
        if member == OWNER and self.config.ntfy is None:
            return False, "ntfy is not configured"
        server, _, token, _ = self._ntfy_target(member)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = await client.post(server.rstrip("/") + "/", headers=headers,
                                     content=json.dumps(self.ntfy_payload(events, member=member)))
        except httpx.HTTPError as e:
            return False, f"ntfy unreachable: {type(e).__name__}"
        if resp.status_code >= 300:
            return False, f"ntfy answered {resp.status_code}"
        return True, None

    async def _deliver_webhook(self, client: httpx.AsyncClient, event: dict, *,
                               member: str = OWNER) -> tuple[bool, str | None]:
        prefs = self.config.members.get(member)
        cfg = self.config.webhook if member == OWNER else (prefs.webhook if prefs else None)
        if cfg is None:
            return False, "webhook is not configured"
        envelope: dict[str, Any] = {"type": event.get("type", "event"), "at": event.get("at"), "data": event}
        freeze = self._freeze_url(event if event.get("type") != "digest" else {}, member)
        if freeze:
            envelope["freeze_url"] = freeze  # POST it to freeze (openajar.freeze)
        body = json.dumps(envelope, sort_keys=True).encode()
        ts = str(int(self.clock()))
        headers = {"Content-Type": "application/json", "X-Agentos-Event": str(event.get("type", "event")),
                   "X-Agentos-Timestamp": ts, "X-Agentos-Signature": webhook_signature(cfg.secret, ts, body)}
        try:
            resp = await client.post(cfg.url, headers=headers, content=body)
        except httpx.HTTPError as e:
            return False, f"webhook unreachable: {type(e).__name__}"
        if resp.status_code >= 300:
            return False, f"webhook answered {resp.status_code}"
        return True, None

    def _deliver_log(self, event: dict) -> bool:
        title, body = self._message(event)
        log.info("notify %s: %s | %s", event.get("type", "event"), title, body)
        return True

    async def test(self, member_id: str = OWNER) -> dict[str, str]:
        """Send a test message on every channel of the owner (or a member), and the node log, now."""
        channels = self.channels_for(member_id) + (["log"] if self.config.log.enabled else [])
        if not channels:
            return {}
        ids = self._send({"type": "test", "title": "openajar test", "body": "Notifications from this node work.",
                          "member": member_id}, immediate=True)
        await self.flush()
        out = {}
        for ch in channels:
            marks = ",".join("?" * len(ids))
            row = self.db.one(f"SELECT delivered_at, last_error FROM outbox WHERE channel = ? AND id IN ({marks}) "
                              "ORDER BY id DESC LIMIT 1", (ch, *ids)) if ids else None
            out[ch] = "delivered" if row and row["delivered_at"] else f"failed: {row['last_error'] if row else '?'}"
        return out
