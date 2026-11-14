"""Drafts (brief B3): programs that need words ask a configured provider for candidates.

    provider = HttpDraftProvider("https://drafts.example.com/v1/candidates",
                                 secret_ref="engage/draft-provider/token", timeout_s=20, vault=vault)
    texts = await provider.candidates(DraftRequest(surface, "comment", ("coffee",), ("en",), 140, 3))
    content = ChainedContent([ItemsContent(store), DraftsContent(store, provider)])   # items first
    book = DraftsBook(store)                 # list a surface's drafts, sweep them

**The provider** is named in node.toml (`engage_draft_provider`: off, passthrough or http). The
http provider POSTs one JSON request and reads one JSON answer:

    -> {"kind": "comment", "topics": [...], "languages": ["en"], "max_length": 140, "n": 3,
        "target": null, "voice": "memory:biz-a/voice"}
    <- {"candidates": ["...", "...", "..."]}

Its endpoint is https (plain http only to this machine). Its bearer token is a machine secret in
the vault (`engage_draft_secret_ref`, else DEFAULT_SECRET_REF when the vault has it), read for
each request and never logged, printed or put in an error; the client ignores proxy settings
from the environment. The whole request has one deadline (`timeout_s`), and the answer is read
with a running cap of MAX_RESPONSE_BYTES. The request body carries no secret and does not name
the surface. The answer is checked strictly: a JSON object whose `candidates` is a list of at
most `n` strings, or the whole answer is refused; each candidate is NFC-normalized and trimmed,
and one that is empty, longer than `max_length` code points or carries a control or hidden
character is dropped (never fixed). An answer with no usable candidate counts as
unavailable. Candidate text is untrusted: it is stored and shown, never interpreted, and logs
never carry it.

`passthrough` makes deterministic test candidates from the request (for tests and offline runs).
They always get an approval card (`Content.review`), and the provider is not wired while an
approved program sends text in auto mode.

**DraftsContent** is the executor's ContentPort for drafts (origin `draft`). `atake(surface, kind,
n)` first offers drafts a window handed back in the last 24 hours (once again only, and never
one an operation already tried), then asks the provider with
the surface's approved program (comments.topics and voice, filters.language, the length limit of
the site and, for comments, comments.max_chars). It keeps one candidate per operation (`pending`)
and the rest as `alternative` rows the owner can see (`agentos engage drafts`). Content it
returns is never approved: in approve mode the owner gets an approval card for it, as for any
text. `settle` marks the draft `used` (with the run id and, when the runner knows it, the
approval id), `failed` (its operation failed or stopped mid-run: it may have acted) or
`returned` (its run provably did not act), and writes the surface's history. The sweep marks
used drafts `approved` (verified), `denied`, `expired` or `failed` once their operation's outcome
is known, fails drafts a window left `pending` mid-run and hands back the others, and expires
what nobody used.

When the provider cannot be reached, `atake` raises ProviderUnavailable (a ContentUnavailable):
the executor skips that window's text operations, notes it in the history (and a notice), and
the next window asks again. Plain `take` never calls out.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import logging
import re
import secrets
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from agentos.core.errors import AgentosError, InvalidRequest
from agentos.engage.items import limits_for
from agentos.engage.ports import Content, ContentOrigin, ContentUnavailable
from agentos.engage.store import EngageStore, _history, iso

log = logging.getLogger("agentos.engage.drafts")

PROVIDERS = ("off", "passthrough", "http")
DEFAULT_SECRET_REF = "engage/draft-provider/token"
PURPOSE = "engage draft provider"
CANDIDATES = 3                         # one draft and two alternatives (design 5.6)
MAX_CANDIDATES = 5
MAX_REQUEST_CHARS = 5000               # the longest text a provider is asked for, whatever the site takes
MAX_RESPONSE_BYTES = 256 * 1024
TIMEOUT_RANGE = (1.0, 120.0)
REUSE_FOR = timedelta(hours=24)        # a draft handed back is offered again this long after, then expires
MAX_OFFERS = 2                         # a draft goes to at most two windows: it is offered again once
ABANDONED_AFTER = timedelta(hours=2)   # a pending draft whose window never settled it
DRAFT_ID_RE = re.compile(r"^dr_[0-9a-f]{16}$")
DRAFT_STATUSES = ("pending", "alternative", "used", "approved", "denied", "returned", "expired", "failed")
TEXT_KINDS_WITH_COMMENT_LIMIT = ("comment", "reply")
HOLD_KIND = "draft_provider"           # the hold row that notes a provider that gave nothing (text from it only)
# Invisible characters outside Unicode's format category (Cf, refused as a whole): Hangul fillers.
_HIDDEN = frozenset([0x115F, 0x1160, 0x3164, 0xFFA0])
_ZWJ = 0x200D
_EMOJI_VS = (0xFE0E, 0xFE0F)           # text and emoji presentation selectors
# What the operation that carried a used draft says of its fate, once it is known.
_FATE = {"denied": "denied", "approval_expired": "expired"}


class ProviderUnavailable(ContentUnavailable):
    """The draft provider gave nothing usable now. `reason` is a short code (unreachable, timeout,
    http_503, bad_response, no_candidates, no_token, vault, ...); the message never carries a
    secret or candidate text."""


# ---------------------------------------------------------------- the request and its checks

@dataclass(frozen=True)
class DraftRequest:
    surface: str
    kind: str
    topics: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    max_chars: int = 140
    n: int = CANDIDATES
    target: str | None = None
    voice: str | None = None

    def body(self) -> dict[str, Any]:
        """What goes to the provider: no secret, and not the surface."""
        return {"kind": self.kind, "topics": list(self.topics), "languages": list(self.languages),
                "max_length": self.max_chars, "n": self.n, "target": self.target, "voice": self.voice}


def profile_voice(store: Any, surface: str) -> str | None:
    """The voice of the profile the account is attached to (agentos.profiles), else None: plain
    instructions written by the owner (at most agentos.profiles.service.MAX_VOICE_CHARS), which
    name the profile's display name but never the surface."""
    source = getattr(store, "profiles", None)
    if source is None:
        return None
    try:
        return source.voice(surface)
    except Exception:  # no voice is better than no drafts
        log.warning("profiles: no voice for %s", surface, exc_info=True)
        return None


def request_for(program: Any, kind: str, *, n: int = CANDIDATES, target: str | None = None,
                voice: str | None = None) -> DraftRequest:
    """The request for one kind of text on a program's surface. The program's own
    `comments.voice` wins; else `voice` (the attached profile's, `profile_voice`)."""
    kind = str(kind)
    limit = limits_for(program.site or "", kind).text
    if kind in TEXT_KINDS_WITH_COMMENT_LIMIT:
        limit = min(limit, program.comments.max_chars)
    languages = tuple(program.steady.filters.language)
    return DraftRequest(program.surface, kind, tuple(program.comments.topics), languages,
                        min(limit, MAX_REQUEST_CHARS), max(1, min(int(n), MAX_CANDIDATES)), target,
                        program.comments.voice or voice)


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_endpoint(url: str) -> str:
    """The provider endpoint, if it is a full https URL (plain http only to this machine)."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        scheme, host = parts.scheme, parts.hostname
        _ = parts.port
    except ValueError:
        scheme, host = "", None
    if not url or scheme not in ("https", "http") or not host:
        raise InvalidRequest("engage_draft_endpoint must be a full https:// URL")
    if parts.username or parts.password:
        raise InvalidRequest("engage_draft_endpoint must not carry a user or a password; the token goes in the "
                             "vault (engage_draft_secret_ref)")
    if parts.scheme == "http" and not _loopback(host):
        raise InvalidRequest("engage_draft_endpoint must use https (plain http only to this machine, such as "
                             "http://127.0.0.1:8080/candidates)")
    return url


def _emoji_part(ch: str) -> bool:
    """A character an emoji sequence joins: a symbol, a skin tone modifier or a presentation selector."""
    o = ord(ch)
    return unicodedata.category(ch) == "So" or 0x1F3FB <= o <= 0x1F3FF or o in _EMOJI_VS


def clean_candidate(text: str, max_chars: int) -> str | None:
    """The candidate, NFC-normalized and trimmed, or None when it is empty, longer than
    `max_chars` code points, or carries a control character (newlines and tabs are text) or a
    character that hides or reorders text: every format character (Unicode category Cf: zero
    width spaces and joiners, bidi marks, overrides and isolates, the word joiner and invisible
    operators, the byte order mark, tag characters), except a zero width joiner between two
    parts of an emoji; a variation selector, except the emoji or text one right after a symbol
    or a keycap digit; and the Hangul fillers."""
    t = unicodedata.normalize("NFC", text.replace("\r\n", "\n")).strip()
    if not t or len(t) > max_chars:
        return None
    for i, ch in enumerate(t):
        if ch in "\n\t":
            continue
        o, cat = ord(ch), unicodedata.category(ch)
        if cat in ("Cc", "Cs", "Co") or o in _HIDDEN:
            return None
        if cat == "Cf":
            if o == _ZWJ and 0 < i < len(t) - 1 and _emoji_part(t[i - 1]) and _emoji_part(t[i + 1]):
                continue
            return None
        if o in _EMOJI_VS:
            if i == 0 or not (unicodedata.category(t[i - 1]) == "So" or t[i - 1] in "0123456789#*"):
                return None
        elif 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF:
            return None
    return t


def validate_response(data: Any, n: int, max_chars: int) -> list[str]:
    """The usable candidates of a provider's answer, in its order, without repeats."""
    raw = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ProviderUnavailable("bad_response", "the draft provider's answer has no list of candidates")
    if len(raw) > n:
        raise ProviderUnavailable("bad_response", f"the draft provider sent {len(raw)} candidates; {n} were asked for")
    if any(not isinstance(c, str) for c in raw):
        raise ProviderUnavailable("bad_response", "a candidate in the draft provider's answer is not text")
    out: list[str] = []
    for c in raw:
        t = clean_candidate(c, max_chars)
        if t is not None and t not in out:
            out.append(t)
    if not out:
        raise ProviderUnavailable("no_candidates", f"none of the draft provider's {len(raw)} candidates passed the "
                                                   "checks")
    return out


# ---------------------------------------------------------------- providers

@runtime_checkable
class DraftProvider(Protocol):
    name: str

    async def candidates(self, request: DraftRequest) -> list[str]: ...


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class HttpDraftProvider:
    """A provider behind an https endpoint, with a bearer token from the vault."""

    def __init__(self, endpoint: str, *, secret_ref: str = "", timeout_s: float = 20.0, vault: Any = None,
                 get_vault: Callable[[], Any] | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.endpoint = check_endpoint(endpoint)
        self.secret_ref = (secret_ref or "").strip()
        self.timeout_s = float(timeout_s)
        self._vault = vault
        self._get_vault = get_vault
        self.transport = transport
        self.name = "http:" + (urlsplit(self.endpoint).hostname or "")
        self.token_ref: str | None = None      # the vault ref the last request's token came from

    async def _token(self) -> str | None:
        """The bearer token: from `secret_ref`, else from DEFAULT_SECRET_REF (where `agentos engage
        provider token` stores it) when the vault has it. A ref that was named and is missing is
        an error; the default one missing means no token. Sets `token_ref` to the ref it used."""
        named = bool(self.secret_ref)
        ref = self.secret_ref or DEFAULT_SECRET_REF
        self.token_ref = None
        vault = self._vault if self._vault is not None else (self._get_vault() if self._get_vault else None)
        if vault is None:
            if not named:
                return None
            raise ProviderUnavailable("no_vault", "the draft provider's token is kept in the vault, and this node "
                                                  "has none")
        try:
            token = await _maybe_await(vault.get_secret(ref, purpose=PURPOSE))
        except AgentosError as e:
            raise ProviderUnavailable("vault", f"the vault did not give the draft provider's token ({e.code})") from None
        if not token:
            if not named:
                return None
            raise ProviderUnavailable("no_token", f"the vault has no secret {ref} (agentos engage provider "
                                                  "token stores it)")
        if any(c in token for c in "\r\n\x00"):
            raise ProviderUnavailable("bad_token", "the draft provider's token in the vault is not one line")
        self.token_ref = ref
        return str(token)

    async def candidates(self, request: DraftRequest) -> list[str]:
        """One request, all of it (the token, the connection, the answer) inside `timeout_s`."""
        try:
            return await asyncio.wait_for(self._candidates(request), self.timeout_s)
        except TimeoutError:
            raise ProviderUnavailable("timeout", f"the draft provider did not answer in {self.timeout_s:g} s") from None

    async def _candidates(self, request: DraftRequest) -> list[str]:
        token = await self._token()
        headers = {"Accept": "application/json", "User-Agent": "agentos-engage-drafts"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        t0 = time.monotonic()
        body = bytearray()
        try:
            # trust_env=False: no proxy from the environment ever sees the bearer token
            async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout_s, follow_redirects=False,
                                         trust_env=False) as client, \
                    client.stream("POST", self.endpoint, json=request.body(), headers=headers) as resp:
                ms = int((time.monotonic() - t0) * 1000)
                if resp.status_code != 200:
                    log.warning("engage: draft provider %s answered %s in %d ms", self.name, resp.status_code, ms)
                    raise ProviderUnavailable(f"http_{resp.status_code}",
                                              f"the draft provider answered {resp.status_code}")
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ProviderUnavailable("bad_response", "the draft provider's answer is too large")
        except httpx.TimeoutException:
            raise ProviderUnavailable("timeout", f"the draft provider did not answer in {self.timeout_s:g} s") from None
        except httpx.HTTPError as e:
            raise ProviderUnavailable("unreachable", f"the draft provider could not be reached "
                                                     f"({type(e).__name__})") from None
        ms = int((time.monotonic() - t0) * 1000)
        try:
            data = json.loads(bytes(body))
        except (ValueError, RecursionError):
            raise ProviderUnavailable("bad_response", "the draft provider's answer is not JSON") from None
        out = validate_response(data, request.n, request.max_chars)
        log.info("engage: draft provider %s gave %d candidates for a %s in %d ms", self.name, len(out), request.kind, ms)
        return out


class PassthroughProvider:
    """Deterministic test candidates built from the request, for tests and offline runs. They say
    they are test text, and each one gets an approval card whatever the program's mode (never
    sent on its own, even in auto mode)."""
    name = "passthrough"

    async def candidates(self, request: DraftRequest) -> list[str]:
        topics = list(request.topics) or ["the topic"]
        lang = f" ({request.languages[0]})" if request.languages else ""
        out = [f"Test {request.kind} {i + 1} about {topics[i % len(topics)]}{lang}"[:request.max_chars]
               for i in range(request.n)]
        return validate_response({"candidates": out}, request.n, request.max_chars)


def provider_from_config(config: Any, *, vault: Any = None, get_vault: Callable[[], Any] | None = None,
                         transport: httpx.AsyncBaseTransport | None = None) -> DraftProvider | None:
    """The provider node.toml names, or None when it is off (the default)."""
    kind = str(getattr(config, "engage_draft_provider", "off") or "off") if config is not None else "off"
    if kind == "passthrough":
        return PassthroughProvider()
    if kind == "http":
        return HttpDraftProvider(config.engage_draft_endpoint, secret_ref=config.engage_draft_secret_ref,
                                 timeout_s=config.engage_draft_timeout_s, vault=vault, get_vault=get_vault,
                                 transport=transport)
    return None


def sends_without_review(program: Any) -> bool:
    """Whether the program lets text out without an approval card: `auto` comments or replies on a
    site that allows auto (never LinkedIn or Reddit)."""
    from agentos.engage.models import APPROVE_ONLY_SITES, Mode

    if (program.site or "") in APPROVE_ONLY_SITES:
        return False
    return Mode.AUTO in (program.comments.mode, program.community.reply_to_comments.mode)


def passthrough_problems(provider: str, programs: list[Any]) -> list[str]:
    """The passthrough provider writes test text: it is refused while an approved program would
    send text without a card (`programs` are the approved ones)."""
    if provider != "passthrough":
        return []
    auto = sorted({p.surface for p in programs if sends_without_review(p)})
    if not auto:
        return []
    verb = "runs" if len(auto) == 1 else "run"
    problem = (f"engage_draft_provider = passthrough writes test text, and {', '.join(auto)} {verb} in auto "
               "mode, which sends text without asking you. Use the http provider, or set those programs' comments "
               "to approve")
    return [problem]


def config_problems(provider: str, endpoint: str, secret_ref: str, timeout_s: float) -> list[str]:
    """What is wrong with the draft provider settings (NodeConfig's check)."""
    out = []
    if provider not in PROVIDERS:
        out.append("engage_draft_provider is off, passthrough or http")
    if provider == "http" or endpoint:
        if provider == "http" and not endpoint:
            out.append("engage_draft_provider = http needs engage_draft_endpoint")
        elif endpoint:
            try:
                check_endpoint(endpoint)
            except InvalidRequest as e:
                out.append(e.message)
    if secret_ref:
        from agentos.vault.service import _MACHINE_REF_RE

        if not _MACHINE_REF_RE.match(secret_ref) or ".." in secret_ref.split("/"):
            out.append("engage_draft_secret_ref is a vault machine secret ref: letters, digits and . _ : / -")
    if not TIMEOUT_RANGE[0] <= timeout_s <= TIMEOUT_RANGE[1]:
        out.append(f"engage_draft_timeout_s must be between {TIMEOUT_RANGE[0]:g} and {TIMEOUT_RANGE[1]:g}")
    return out


# ---------------------------------------------------------------- the drafts table

def _now(clock: Callable[[], float]) -> datetime:
    return datetime.fromtimestamp(clock(), UTC)


# A draft an operation already tried: its newest operation has a run, or is still `attempted`
# (the window stopped mid-run). Such a draft may have gone out, so it is never offered again.
_TRIED = ("EXISTS (SELECT 1 FROM ops o WHERE o.id = (SELECT MAX(id) FROM ops WHERE content_origin = 'draft' "
          "AND content_id = drafts.id) AND (o.run_id IS NOT NULL OR o.status = 'attempted'))")


class DraftsBook:
    """The `drafts` table (engage.db schema 4)."""

    def __init__(self, store: EngageStore, *, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock

    def now(self) -> datetime:
        return _now(self.clock)

    def add_batch(self, surface: str, kind: str, texts: list[str], *, provider: str, target: str | None,
                  chosen: int, now: datetime) -> tuple[str, list[dict[str, Any]]]:
        """Save one provider answer: the first `chosen` candidates `pending`, the rest `alternative`.
        Returns (batch id, the pending rows)."""
        batch = "db_" + secrets.token_hex(8)
        ids = []
        with self.store._tx() as conn:
            for i, text in enumerate(texts):
                did = "dr_" + secrets.token_hex(8)
                ids.append(did)
                conn.execute("INSERT INTO drafts (id, surface, kind, batch, text, target, status, provider, created_at, "
                             "taken_at, offers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (did, surface, str(kind), batch, text, target,
                              "pending" if i < chosen else "alternative", provider, iso(now),
                              iso(now) if i < chosen else None, 1 if i < chosen else 0))
        return batch, [d for d in (self.get(i) for i in ids[:chosen]) if d is not None]

    def reserve_returned(self, surface: str, kind: str, n: int, *, now: datetime) -> list[dict[str, Any]]:
        """Up to `n` drafts handed back in the last REUSE_FOR (from when they came back), oldest
        first, as `pending` again. Only a draft offered fewer than MAX_OFFERS times, and never one
        that an operation already tried (one with a run, or still `attempted`)."""
        if n <= 0:
            return []
        with self.store._tx() as conn:
            rows = conn.execute("SELECT id FROM drafts WHERE surface = ? AND kind = ? AND status = 'returned' "
                                "AND settled_at > ? AND offers < ? AND NOT " + _TRIED +
                                " ORDER BY created_at, rowid LIMIT ?",
                                (surface, str(kind), iso(now - REUSE_FOR), MAX_OFFERS, n)).fetchall()
            got = [r["id"] for r in rows
                   if conn.execute("UPDATE drafts SET status = 'pending', taken_at = ?, settled_at = NULL, "
                                   "offers = offers + 1 WHERE id = ? AND status = 'returned'",
                                   (iso(now), r["id"])).rowcount == 1]
        return [d for d in (self.get(i) for i in got) if d is not None]

    def settle(self, draft_id: str, to: str, *, now: datetime, run_id: str | None = None,
               approval_id: str | None = None) -> bool:
        """A pending draft becomes `used` (with its run and approval), `failed` (its run may have
        acted and did not go out: never offered again) or `returned`. False when it was not
        pending."""
        assert to in ("used", "failed", "returned")
        with self.store._tx() as conn:
            if to == "returned":
                cur = conn.execute("UPDATE drafts SET status = 'returned', settled_at = ? WHERE id = ? AND "
                                   "status = 'pending'", (iso(now), draft_id))
            else:
                cur = conn.execute("UPDATE drafts SET status = ?, run_id = COALESCE(?, run_id), approval_id = ?, "
                                   "settled_at = ? WHERE id = ? AND status = 'pending'",
                                   (to, run_id, approval_id, iso(now), draft_id))
            return cur.rowcount == 1

    def get(self, draft_id: str) -> dict[str, Any] | None:
        rows = self.store._dicts("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        return rows[0] if rows else None

    def list(self, surface: str, *, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """A surface's drafts, newest batch first, each batch's chosen draft before its alternatives."""
        if status is not None and status not in DRAFT_STATUSES:
            raise InvalidRequest(f"status is one of {', '.join(DRAFT_STATUSES)}")
        sql, args = "SELECT * FROM drafts WHERE surface = ?", [surface]
        if status is not None:
            sql, args = sql + " AND status = ?", [*args, status]
        return self.store._dicts(sql + " ORDER BY created_at DESC, batch, status = 'alternative', rowid LIMIT ?",
                                 (*args, limit))

    def counts(self, surface: str) -> dict[str, int]:
        rows = self.store._rows("SELECT status, COUNT(*) n FROM drafts WHERE surface = ? GROUP BY status", (surface,))
        return {r["status"]: r["n"] for r in rows}

    def sweep(self, now: datetime | None = None) -> dict[str, int]:
        """Pending drafts whose window is over: `failed` when an operation already tried them (it
        may have acted), else back (`returned`). Used drafts follow their operation once its
        outcome is known: `approved` when it was verified, `denied` or `expired` when the owner
        refused the card or it expired, `failed` when it failed otherwise. Alternatives older than
        REUSE_FOR, and returned drafts handed back longer ago than that, expire."""
        now = now or self.now()
        out = {"returned": 0, "failed": 0, "approved": 0, "denied": 0, "expired": 0}
        over = ("status = 'pending' AND taken_at < ? AND (taken_at < ? OR NOT EXISTS (SELECT 1 FROM sessions s "
                "WHERE s.surface = drafts.surface AND s.status = 'running'))")
        args = (iso(now - timedelta(minutes=1)), iso(now - ABANDONED_AFTER))
        with self.store._tx() as conn:
            tried = conn.execute(f"SELECT id, surface FROM drafts WHERE {over} AND {_TRIED}", args).fetchall()
            for r in tried:
                conn.execute("UPDATE drafts SET status = 'failed', settled_at = ? WHERE id = ? AND status = 'pending'",
                             (iso(now), r["id"]))
                _history(conn, r["surface"], "draft_failed", {"id": r["id"], "reason": "its window stopped while "
                                                              "an operation was trying it"}, at=now)
            out["failed"] = len(tried)
            out["returned"] = conn.execute(f"UPDATE drafts SET status = 'returned', settled_at = ? WHERE {over}",
                                           (iso(now), *args)).rowcount
            done = conn.execute(
                "SELECT d.id, o.status, o.reason FROM drafts d JOIN ops o ON o.id = (SELECT MAX(id) FROM ops "
                "WHERE content_origin = 'draft' AND content_id = d.id) WHERE d.status = 'used'").fetchall()
            for r in done:
                new = "approved" if r["status"] == "verified" else \
                    _FATE.get(r["reason"] or "", "failed") if r["status"] == "failed" else None
                if new is not None:
                    conn.execute("UPDATE drafts SET status = ?, settled_at = ? WHERE id = ? AND status = 'used'",
                                 (new, iso(now), r["id"]))
                    out[new] += 1
            out["expired"] += conn.execute(
                "UPDATE drafts SET status = 'expired', settled_at = ? WHERE (status = 'alternative' AND created_at <= ?) "
                "OR (status = 'returned' AND COALESCE(settled_at, created_at) <= ?)",
                (iso(now), iso(now - REUSE_FOR), iso(now - REUSE_FOR))).rowcount
        return out


# ---------------------------------------------------------------- the ContentPort

class DraftsContent:
    """The executor's ContentPort over the draft provider. Never returns approved content."""
    origin = ContentOrigin.DRAFT

    def __init__(self, store: EngageStore, provider: DraftProvider, *, clock: Callable[[], float] = time.time,
                 candidates: int = CANDIDATES, approval_of: Callable[[str], str | None] | None = None):
        self.store = store
        self.provider = provider
        self.clock = clock
        self.candidates = candidates
        self.approval_of = approval_of
        self.book = DraftsBook(store, clock=clock)

    def now(self) -> datetime:
        return _now(self.clock)

    @staticmethod
    def _content(d: dict[str, Any]) -> Content:
        # test text from the passthrough provider always gets a card, whatever the program's mode
        return Content(id=d["id"], origin=ContentOrigin.DRAFT, text=d["text"],
                       targets=(d["target"],) if d.get("target") else (), approved=False,
                       review=d.get("provider") == PassthroughProvider.name)

    def take(self, surface: str, kind: str, n: int) -> list[Content]:
        """Drafts handed back earlier only: this never calls the provider."""
        return [self._content(d) for d in self.book.reserve_returned(surface, str(kind), n, now=self.now())]

    async def atake(self, surface: str, kind: str, n: int) -> list[Content]:
        out = self.take(surface, kind, n)
        pv = self.store.active(surface)
        if len(out) >= n or pv is None:
            return out
        want = n - len(out)
        req = request_for(pv.definition, str(kind), n=max(self.candidates, want), voice=profile_voice(self.store, surface))
        try:
            texts = await self.provider.candidates(req)
        except ProviderUnavailable:
            if out:
                return out
            raise
        except Exception as e:  # noqa: BLE001  (a broken provider is an unavailable one, never a broken window)
            log.warning("engage: draft provider %s failed: %s", self.provider.name, type(e).__name__)
            if out:
                return out
            raise ProviderUnavailable("error", f"the draft provider failed ({type(e).__name__})") from None
        now = self.now()
        batch, rows = self.book.add_batch(surface, str(kind), texts, provider=self.provider.name, target=req.target,
                                          chosen=min(want, len(texts)), now=now)
        self.store.add_history(surface, "drafts_received", {"kind": str(kind), "batch": batch, "candidates": len(texts),
                                                            "provider": self.provider.name}, at=now)
        return out + [self._content(d) for d in rows]

    def settle(self, surface: str, content: Content, *, used: bool, run_id: str | None = None) -> None:
        """used=False hands the draft back (the executor does so only when its run provably did not
        act). used=True follows the operation that carried it (the executor records it first):
        failed, or still `attempted` because the window stopped mid-run, it may have acted, so the
        draft is `failed` and left for the owner, never offered again; otherwise it is `used`."""
        now = self.now()
        approval_id = None
        to = "returned"
        if used:
            op = self.store.op_by_content(ContentOrigin.DRAFT.value, content.id) or {}
            to = "failed" if op.get("status") in ("failed", "attempted") else "used"
            run_id = run_id or op.get("run_id")
        if to == "used" and run_id and self.approval_of is not None:
            try:
                approval_id = self.approval_of(run_id)
            except Exception:  # the approval id is a convenience, never a reason to lose the settle
                log.debug("engage: no approval id for run %s", run_id, exc_info=True)
        if not self.book.settle(content.id, to, now=now, run_id=run_id, approval_id=approval_id):
            log.warning("engage: draft %s was not pending when its window settled it", content.id)
            return
        self.store.add_history(surface, {"used": "draft_used", "failed": "draft_failed", "returned": "draft_returned"}[to],
                               {"id": content.id, "run_id": run_id, "approval_id": approval_id}, at=now)


def wire(service: Any, runtime: Any) -> DraftsContent | None:
    """Put drafts after items in the service executor's content chain when node.toml names a
    provider. Returns the DraftsContent, or None (no executor, or the provider is off)."""
    from agentos.engage.items import ChainedContent, ItemsContent

    executor = getattr(service, "executor", None)
    config = runtime.get("config") if runtime is not None else None
    if executor is None or config is None:
        return None
    provider = provider_from_config(config, get_vault=lambda: runtime.get("vault"))
    if provider is None:
        return None
    problems = passthrough_problems(provider.name, [pv.definition for pv in service.store.approved()])
    if problems:     # test text never goes out without a card: the provider is not started
        log.error("engage: the draft provider is not started: %s", problems[0])
        service.store.notice("drafts", f"The draft provider is not started: {problems[0]}.")
        return None
    runner = runtime.get("runner")

    def approval_of(run_id: str) -> str | None:
        return getattr(runner.get(run_id), "approval_id", None) if runner is not None else None

    drafts = DraftsContent(service.store, provider, clock=service.clock, approval_of=approval_of)
    executor.content = ChainedContent([ItemsContent(service.store, clock=service.clock), drafts])
    return drafts
