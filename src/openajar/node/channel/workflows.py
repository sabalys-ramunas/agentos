"""Workflows over the channel (docs/WORKFLOWS.md, "The node channel").

`workflow` is a person's action, relayed by the controller and signed exactly as `ask_first` is, with
the same `ts` and `nonce` rules. `hook_relay` and `unsubscribe_relay` carry what arrived at the
controller, because a node has no public port: neither is trusted for what it says. A webhook is
checked here against the sender's own signature, and an unsubscribe token is looked up here, so the
controller can forge neither and stores neither.
"""

from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Callable
from typing import Any

from openajar.core.errors import InvalidRequest, OpenajarError, PolicyDenied
from openajar.core.ratelimit import RateLimiter
from openajar.node.channel import wire
from openajar.node.channel.standing import NONCE_KEEP_S, NONCE_RE
from openajar.policy.ask_first import MAX_BY
from openajar.runner.workflows.mirror import mirror
from openajar.runner.workflows.service import Actor, WorkflowInvalid

KIND = "workflow"
NONCES = "workflow_nonces"
ACTIONS = ("save", "enable", "disable", "delete", "run", "retry")
HOOK_HEADERS = ("x-agentos-signature", "x-agentos-timestamp", "x-agentos-delivery")
MAX_HOOK_BODY = 512 * 1024
BACKLOG_RETRY_S = 30.0


class WorkflowSync:
    def __init__(self, service: Any, *, get_meta: Callable[[str, Any], Any], set_meta: Callable[[str, Any], None]):
        self.service, self.get_meta, self.set_meta = service, get_meta, set_meta

    def check_body(self, body: wire.Json) -> str:
        """After the signature: `ts`, a nonce not seen before, and an action this node knows."""
        if wire.parse_time(body.get("ts")) is None:
            raise InvalidRequest("workflow needs ts (ISO 8601 UTC)")
        nonce = body.get("nonce")
        if not isinstance(nonce, str) or not NONCE_RE.match(nonce):
            raise InvalidRequest("workflow needs a nonce (8 to 64 letters, digits, _ -)")
        action = body.get("action")
        if action not in ACTIONS:
            raise InvalidRequest(f"workflow action is one of: {', '.join(ACTIONS)}")
        now = time.time()
        seen = {k: v for k, v in (self.get_meta(NONCES, {}) or {}).items() if v > now - NONCE_KEEP_S}
        if nonce in seen:
            raise PolicyDenied("this workflow message was already applied (its nonce was seen before)")
        seen[nonce] = now
        self.set_meta(NONCES, seen)
        return action

    def apply(self, action: str, body: wire.Json) -> wire.Json:
        """The `workflow_result` body. A refusal is an answer, not a broken channel."""
        by = str(body.get("by") or "a person")[:MAX_BY]
        actor = Actor.owner(by=f"controller:{by}")
        wid = body.get("id")
        try:
            if action == "save":
                out = self.service.save(body.get("definition"), actor, flow_id=wid)
                return {"ok": True, "flow": {k: v for k, v in out.items() if k != "secret"}, **_secret(out)}
            if not isinstance(wid, str):
                raise InvalidRequest("this action needs the workflow's id")
            if action == "enable":
                out = self.service.enable(wid, actor, version=body.get("version"))
                return {"ok": True, "flow": {k: v for k, v in out.items() if k != "secret"}, **_secret(out)}
            if action == "disable":
                return {"ok": True, "flow": self.service.disable(wid, actor)}
            if action == "delete":
                return {"ok": self.service.delete(wid, actor)}
            if action == "run":
                return {"ok": True, "run": mirror(self.service.run(wid, actor, values=body.get("values")))}
            run = body.get("run")
            if not isinstance(run, str):
                raise InvalidRequest("a retry needs the run's id")
            return {"ok": True, "run": mirror(self.service.retry(wid, run, actor, force=body.get("force") is True))}
        except WorkflowInvalid as e:
            return {"ok": False, "error": {"code": e.code, "message": e.message, "problems": e.problems}}
        except OpenajarError as e:
            return {"ok": False, "error": {"code": e.code, "message": e.message}}


def _secret(out: dict[str, Any]) -> dict[str, Any]:
    return {"secret": out["secret"]} if out.get("secret") else {}


class NodeHookGate:
    """What a node takes from relays, whatever the controller in front of it did. `relays` counts
    everything, before anything is decoded. `sender` counts what passed its signature. `backlog`
    says how much is waiting already, so what is accepted cannot pile up without bound."""

    def __init__(self, *, relays: int = 1200, per_token: int = 300, backlog: int = 500, window: float = 60.0,
                 waiting: Callable[[Any], int] | None = None):
        self.all = RateLimiter(per_key=relays, global_max=relays, window=window)
        self.tokens = RateLimiter(per_key=per_token, global_max=max(relays, per_token), window=window)
        self.backlog, self.waiting = backlog, waiting

    def relay(self) -> float | None:
        return self.all.check("relays")

    def sender(self, schedule: Any) -> float | None:
        if self.waiting is not None and self.waiting(schedule) >= self.backlog:
            return BACKLOG_RETRY_S
        return self.tokens.check(schedule.id)


async def relay_hook(scheduler: Any, body: wire.Json, gate: NodeHookGate | None = None) -> wire.Json:
    """`hook_relay`: the same handler a direct POST reaches, on the exact bytes the sender signed."""
    wait = gate.relay() if gate is not None else None
    if wait is not None:
        return {"status": 429, "retry_after": wait,
                "body": {"error": {"code": "rate_limited", "message": "this computer is taking too many webhooks"}}}
    token, raw, headers = body.get("token"), body.get("body_b64"), body.get("headers")
    if not isinstance(token, str) or not isinstance(raw, str) or not isinstance(headers, dict):
        return {"status": 400, "body": {"error": {"code": "invalid", "message": "a relayed hook needs token, body_b64 and headers"}}}
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return {"status": 400, "body": {"error": {"code": "invalid", "message": "body_b64 is not base64"}}}
    if len(data) > MAX_HOOK_BODY:
        return {"status": 413, "body": {"error": {"code": "invalid", "message": "body too large"}}}
    kept = {k.lower(): str(v) for k, v in headers.items() if isinstance(k, str) and k.lower() in HOOK_HEADERS}
    status, payload = await scheduler.handle_webhook(token, data, kept, admit=gate.sender if gate is not None else None)
    retry = payload.pop("retry_after", None)
    return {"status": status, "body": payload, **({"retry_after": retry} if retry is not None else {})}


def relay_unsubscribe(sends: Any, body: wire.Json) -> wire.Json:
    return {"ok": bool(sends.unsubscribe(body.get("token")))}
