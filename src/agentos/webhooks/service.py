"""The node's signed webhooks: which events a receiver is sent, what each message says, and when it is
tried again.

The node listens on its event bus (runtime service "events") for an approval that needs a person,
one that was decided or expired, and a run that failed. For each webhook that asked for that event, it
records one delivery, then sends a POST with a JSON body `{ type, at, data }` and an HMAC over the
timestamp and the body. A delivery that fails is tried again after 30 seconds, 2 minutes and 10
minutes, then it stops. The log keeps one row per delivery, with how many tries it took and what the last
one said. Redirects are never followed, and every address is checked before the request leaves
(agentos.webhooks.outbound).

The signing secret is shown once, when a webhook is made (or its secret rotated), and never again.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from agentos.core.config import home_from_env
from agentos.core.errors import InvalidRequest, NotFound
from agentos.policy.redact import redact_text
from agentos.policy.util import iso, new_id
from agentos.webhooks.outbound import (
    Outbound,
    OutboundRefused,
    OutboundUnresolved,
    Reply,
    Resolver,
    check_url,
    public_addresses,
    system_resolver,
)
from agentos.webhooks.store import Hook, Store

log = logging.getLogger("agentos.webhooks")

SERVICE_NAME = "webhooks"
EVENTS = ("approval.requested", "approval.decided", "approval.expired", "run.failed")
PING = "ping"
PING_MESSAGE = "This endpoint is connected to agentos."
SECRET_PREFIX = "whsec_"
RETRY_DELAYS = (30, 120, 600)  # after the first try: 30 s, 2 min, 10 min, then it stops
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
RETRY_AFTER_LIMIT = 3600
RETRYABLE_STATUS = frozenset({408, 429})
SUMMARY_LIMIT = 500
USER_AGENT = "agentos-webhooks/1"
TICK_SECONDS = 5.0
MAX_LOG_ROWS = 200

Sender = Callable[[str, bytes, dict[str, str]], Awaitable[Reply]]


def signature(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def encode(kind: str, at: float, data: Mapping[str, Any]) -> bytes:
    return json.dumps({"type": kind, "at": iso(at), "data": dict(data)}, sort_keys=True,
                      separators=(",", ":")).encode()


def signed_headers(secret: str, delivery_id: str, kind: str, body: bytes, now: float) -> dict[str, str]:
    """Fresh for every try: the timestamp is new, so a receiver can refuse an old one."""
    timestamp = str(int(now))
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-Agentos-Event": kind,
        "X-Agentos-Delivery": delivery_id,
        "X-Agentos-Timestamp": timestamp,
        "X-Agentos-Signature": signature(secret, timestamp, body),
    }


def retry_delay(attempts: int, retry_after: str | None) -> float:
    """`attempts` counts tries so far. A `Retry-After` in seconds is honoured up to a limit, and
    never below our own schedule."""
    delay = float(RETRY_DELAYS[min(attempts, MAX_ATTEMPTS - 1) - 1])
    if retry_after and retry_after.isdigit():
        delay = max(delay, float(min(int(retry_after), RETRY_AFTER_LIMIT)))
    return delay


def summary(value: Any) -> str | None:
    if not value:
        return None
    text = redact_text(str(value)).strip()
    return text if len(text) <= SUMMARY_LIMIT else f"{text[: SUMMARY_LIMIT - 3].rstrip()}..."


APPROVAL_STATES = {
    "pending": ("approval.requested", "requested"),
    "approved": ("approval.decided", "decided"),
    "denied": ("approval.decided", "decided"),
    "expired": ("approval.expired", "expired"),
}


def approval_event(payload: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """The webhook event for one approval as the bus publishes it, with its event id (one per
    approval and outcome, so a repeat publish is recorded once). Standing approvals and the rest
    of an approval's life are not events."""
    mapped = APPROVAL_STATES.get(str(payload.get("state")))
    if mapped is None or payload.get("kind") not in ("action", "question") or not payload.get("id"):
        return None
    kind, outcome = mapped
    data = {key: payload.get(key) for key in ("id", "kind", "state", "effect", "workspace", "lane",
                                              "expires_at", "decided_by", "decided_at")}
    data["summary"] = summary(payload.get("summary"))
    return kind, f"approval:{payload['id']}:{outcome}", data


def run_event(payload: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    if payload.get("status") != "failed" or not payload.get("id"):
        return None
    data = {"id": payload["id"], "status": "failed", "workspace": payload.get("workspace"),
            "lane": payload.get("lane"), "flow": payload.get("flow_id") or payload.get("flow"),
            "summary": summary(payload.get("summary"))}
    return "run.failed", f"run:{payload['id']}", data


def checked_events(events: list[str]) -> list[str]:
    """The events asked for, each once, in a fixed order. At least one, and only known ones."""
    unknown = sorted(set(events) - set(EVENTS))
    if unknown:
        raise InvalidRequest(f"Events are: {', '.join(EVENTS)}.")
    if not events:
        raise InvalidRequest("Choose at least one event to send.")
    return [event for event in EVENTS if event in events]


def hook_public(hook: Hook, last: Any | None) -> dict[str, Any]:
    """The shape the app reads. The address and the secret never come back: only the origin."""
    return {"id": hook.id, "origin": hook.origin, "events": list(hook.events), "enabled": hook.enabled,
            "created_at": iso(hook.created_at), "last_delivery": None if last is None else delivery_public(last)}


def delivery_public(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "kind": row["kind"], "state": row["state"], "attempts": int(row["attempts"]),
            "status": row["last_status"], "error": row["last_error"],
            "next_attempt_at": iso(row["next_attempt_at"]) if row["state"] == "pending" else None,
            "created_at": iso(row["created_at"]), "delivered_at": iso(row["delivered_at"])}


class WebhookService:
    """Runtime service "webhooks". `resolver` and `sender` are for tests: the first answers the
    addresses a name has, the second sends a request (the default checks every address and sends
    through the guarded client)."""

    def __init__(self, home: Path, *, clock: Callable[[], float] = time.time, resolver: Resolver = system_resolver,
                 sender: Sender | None = None):
        self.home = home
        self.clock = clock
        self.resolver = resolver
        self.store = Store(home)
        self.sender = sender
        self._outbound: Outbound | None = None
        self._task: asyncio.Task[None] | None = None
        self._unlisten: Callable[[], None] | None = None

    # ---- the webhooks, as the owner makes and changes them ----
    async def create(self, url: str, events: list[str]) -> dict[str, Any]:
        """A new webhook, with its secret. Refused unless the address is https and public."""
        events = checked_events(events)
        parts = check_url(url)
        await public_addresses(parts.hostname or "", parts.port or 443, self.resolver)
        hook_id = new_id("wh")
        secret = SECRET_PREFIX + secrets.token_urlsafe(32)
        self.store.add_hook(hook_id, f"{parts.scheme}://{parts.netloc}", url, secret, events, self.clock())
        return {**hook_public(self.get(hook_id), None), "secret": secret}

    def list(self) -> list[dict[str, Any]]:
        return [hook_public(hook, self.store.last(hook.id)) for hook in self.store.hooks()]

    def get(self, hook_id: str) -> Hook:
        hook = self.store.hook(hook_id)
        if hook is None:
            raise NotFound("This computer has no such webhook.")
        return hook

    def update(self, hook_id: str, *, enabled: bool | None = None, events: list[str] | None = None) -> dict[str, Any]:
        self.get(hook_id)
        if events is not None:
            events = checked_events(events)
        self.store.set_hook(hook_id, enabled=enabled, events=events)
        return hook_public(self.get(hook_id), self.store.last(hook_id))

    def delete(self, hook_id: str) -> None:
        if not self.store.exists(hook_id):  # by id alone: removing a webhook never needs its fields read
            raise NotFound("This computer has no such webhook.")
        self.store.remove_hook(hook_id)

    def rotate(self, hook_id: str) -> str:
        """A new secret. The old one stops working at once, so the receiver must be updated."""
        self.get(hook_id)
        secret = SECRET_PREFIX + secrets.token_urlsafe(32)
        self.store.set_secret(hook_id, secret)
        return secret

    def test(self, hook_id: str) -> str:
        """Queue a ping to this webhook alone. Returns the delivery's id."""
        hook = self.get(hook_id)
        if not hook.enabled:
            raise InvalidRequest("Turn the webhook on to send it a test.")
        now = self.clock()
        delivery_id = new_id("whd")
        body = encode(PING, now, {"message": PING_MESSAGE}).decode()
        self.store.add_delivery(delivery_id, hook_id, f"ping:{new_id('ping')}", PING, body, now)
        return delivery_id

    def deliveries(self, hook_id: str, limit: int = 50) -> list[dict[str, Any]]:
        self.get(hook_id)
        return [delivery_public(row) for row in self.store.recent(hook_id, max(1, min(limit, MAX_LOG_ROWS)))]

    # ---- what happened on the node ----
    def attach(self, bus: Any) -> None:
        """Listen to the node's event bus. Once: a second call changes nothing."""
        listen = getattr(bus, "listen", None)
        if self._unlisten is None and callable(listen):
            self._unlisten = listen(self.on_event)

    def on_event(self, event: Any) -> None:
        """The bus calls this for every event it publishes. Only the three kinds a webhook can name
        are looked at, and recording one never sends anything."""
        if event.kind == "approval":
            mapped = approval_event(event.data or {})
        elif event.kind == "run":
            mapped = run_event(event.data or {})
        else:
            return
        if mapped is not None:
            kind, event_id, data = mapped
            self.record(kind, event_id, data)

    def record(self, kind: str, event_id: str, data: Mapping[str, Any]) -> int:
        """One delivery for each webhook that asked for `kind`. Returns how many were recorded."""
        now = self.clock()
        body = encode(kind, now, data).decode()
        return sum(
            1 for hook_id in self.store.wanting(kind)
            if self.store.add_delivery(new_id("whd"), hook_id, event_id, kind, body, now)
        )

    # ---- sending ----
    async def deliver_due(self) -> int:
        """Try every delivery that is due. Returns how many were tried."""
        rows = self.store.due(self.clock())
        for row in rows:
            try:
                await self._attempt(row)
            except Exception:  # a delivery that fails in an unforeseen way is recorded, and the rest still go
                log.exception("a webhook delivery failed on this computer")
                self._stop(row, int(row["attempts"]) + 1, status=None,
                           error="The delivery failed on this computer, so it stopped.")
        return len(rows)

    async def _attempt(self, row: Any) -> None:
        now = self.clock()
        hook = self.store.hook(row["webhook_id"])
        if hook is None or not hook.enabled:
            self.store.update_delivery(row["id"], state="dropped", next_attempt_at=None,
                                       last_error="The webhook was turned off, removed or could not be read, so this was not sent.")
            return
        body = row["body"].encode()
        attempts = int(row["attempts"]) + 1
        headers = signed_headers(hook.secret, row["id"], row["kind"], body, now)
        try:
            reply = await self._post(hook.url, body, headers)
        except OutboundUnresolved as refused:  # a name that is not answering now: a hiccup, tried again
            self._retry(row, attempts, now, status=None, error=str(refused), retry_after=None)
            return
        except OutboundRefused as refused:  # an address that is not allowed: final, and said plainly
            self._stop(row, attempts, status=None, error=str(refused))
            return
        except (TimeoutError, OSError, httpx.HTTPError) as failed:
            self._retry(row, attempts, now, status=None, error=f"The endpoint could not be reached ({type(failed).__name__}).",
                        retry_after=None)
            return
        if 200 <= reply.status < 300:
            self.store.update_delivery(row["id"], state="delivered", attempts=attempts, last_status=reply.status,
                                       last_error=None, next_attempt_at=None, delivered_at=now)
        elif 300 <= reply.status < 400:
            self._stop(row, attempts, status=reply.status,
                       error="The endpoint redirected, and redirects are not followed.")
        elif reply.status in RETRYABLE_STATUS or reply.status >= 500:
            self._retry(row, attempts, now, status=reply.status, error=f"The endpoint answered {reply.status}.",
                        retry_after=reply.retry_after)
        else:
            self._stop(row, attempts, status=reply.status,
                       error=f"The endpoint answered {reply.status}, so the delivery stopped.")

    def _stop(self, row: Any, attempts: int, *, status: int | None, error: str) -> None:
        self.store.update_delivery(row["id"], state="failed", attempts=attempts, last_status=status,
                                   last_error=error, next_attempt_at=None)

    def _retry(self, row: Any, attempts: int, now: float, *, status: int | None, error: str,
               retry_after: str | None) -> None:
        if attempts >= MAX_ATTEMPTS:
            self._stop(row, attempts, status=status, error=f"{error} It was tried {attempts} times, so it stops here.")
            return
        self.store.update_delivery(row["id"], state="pending", attempts=attempts, last_status=status,
                                   last_error=error, next_attempt_at=now + retry_delay(attempts, retry_after))

    async def _post(self, url: str, body: bytes, headers: dict[str, str]) -> Reply:
        check_url(url)  # the address rules, before anything is sent
        if self.sender is not None:
            return await self.sender(url, body, headers)
        if self._outbound is None:
            self._outbound = Outbound(resolve=self.resolver)
        return await self._outbound.post(url, content=body, headers=headers)

    # ---- the background loop ----
    async def start(self, *, tick_s: float = TICK_SECONDS) -> None:
        if self._task is not None:
            return

        async def loop() -> None:
            while True:
                try:
                    await self.deliver_due()
                except Exception:
                    log.exception("a webhook delivery round failed")
                await asyncio.sleep(tick_s)

        self._task = asyncio.get_running_loop().create_task(loop())

    async def stop(self) -> None:
        if self._unlisten is not None:
            self._unlisten()
            self._unlisten = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._outbound is not None:
            await self._outbound.aclose()
            self._outbound = None
        self.store.close()


def build(runtime: Any) -> WebhookService:
    """Composition-root hook: the node's webhooks, under its home, listening on the event bus."""
    cfg = runtime.get("config") if runtime is not None else None
    home = Path(cfg.home) if cfg is not None else home_from_env()
    svc = WebhookService(home)
    if runtime is not None:
        svc.attach(runtime.get("events"))
        runtime.register(SERVICE_NAME, svc)
    return svc
