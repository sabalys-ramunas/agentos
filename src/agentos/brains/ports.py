"""Thin, tolerant adapters to the services other packages provide (policy, notifier,
traces). Every one falls back cleanly when the service is absent, so brains work on a node
that has none of them, and nothing here ever passes a secret along.

Contracts brains expects (agentos.policy.service.PolicyService implements the policy ones;
sync or async both work):

  policy.mint_child_token(principal, *, purpose, workspace, lanes, ceiling, specials,
                          ttl_seconds, budget) -> str | {"token", "grant_id"}
  policy.revoke(grant_id) -> None
  policy.brain_allowance(principal) -> {"allow_metered", "remaining_usd", "remaining_tokens",
                                        "max_tier", "brains"}
  policy.taint_for(principal) -> set[str]          # e.g. {"private", "untrusted"}
  policy.is_owner_token(token) -> bool
  policy.charge_spend(grant_id, *, tokens, usd) -> bool   # an attempt's spend, to the grant's meters
  notifier.notify(title, body, *, kind, urgent, data) -> None
  traces.record(kind, **fields) -> None
"""

from __future__ import annotations

import hmac
import inspect
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from agentos.brains.models import GrantView
from agentos.core.models import Principal

log = logging.getLogger("agentos.brains")

OWNER_TOKEN_ENV = ("AGENTOS_TOKEN", "AGENTOS_MCP_TOKEN", "AGENTOS_OWNER_TOKEN")


async def _call(obj: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    fn = getattr(obj, name, None) if obj is not None else None
    if not callable(fn):
        return NotImplemented
    out = fn(*args, **kwargs)
    if inspect.isawaitable(out):
        out = await out
    return out


class Ports:
    def __init__(self, runtime: Any, brains_dir: Path):
        self.runtime = runtime
        self.dir = brains_dir

    def _svc(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    # ---- policy ----
    @property
    def has_policy(self) -> bool:
        return self._svc("policy") is not None

    async def mint_child(self, principal: Principal, *, task_id: str, workspace: str, lanes: list[str],
                         ttl_seconds: int, budget: dict[str, Any] | None) -> tuple[str, str | None] | None:
        """A CHILD grant for the brain: only these lanes, never the brain special (a brain
        cannot spawn brains), never exec, at most commit; the policy intersects it with the
        parent so it can only narrow."""
        out = await _call(self._svc("policy"), "mint_child_token", principal,
                          purpose=f"brain task {task_id}", workspace=workspace, lanes=lanes,
                          ceiling="commit", specials=[], ttl_seconds=ttl_seconds, budget=budget)
        if out is NotImplemented or out is None:
            return None
        if isinstance(out, str):
            return out, None
        if isinstance(out, dict):
            return out["token"], out.get("grant_id")
        return out.token, getattr(out, "grant_id", None)

    async def revoke(self, grant_id: str | None) -> None:
        if grant_id:
            try:
                await _call(self._svc("policy"), "revoke", grant_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not revoke child grant %s: %s", grant_id, type(exc).__name__)

    async def charge(self, grant_id: str | None, *, tokens: int, usd: float) -> None:
        """An attempt's tokens and metered dollars, to the day's meters and budgets of the grant it
        ran under (agentos.policy.meters). Never stops the bookkeeping that called it."""
        if not grant_id or not (tokens or usd):
            return
        try:
            await _call(self._svc("policy"), "charge_spend", grant_id, tokens=tokens, usd=usd)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not charge brain spend to grant %s: %s", grant_id, type(exc).__name__)

    async def grant_view(self, principal: Principal) -> GrantView | None:
        out = await _call(self._svc("policy"), "brain_allowance", principal)
        if out is NotImplemented or out is None:
            return None
        if isinstance(out, GrantView):
            return out
        return GrantView(**{k: v for k, v in dict(out).items() if k in GrantView.model_fields})

    async def taint(self, principal: Principal) -> set[str]:
        out = await _call(self._svc("policy"), "taint_for", principal)
        if out is NotImplemented or not out:
            return set()
        return {str(t) for t in out}

    async def is_owner_token(self, token: str) -> bool:
        """True if `token` is the owner's (or node's) own token, which must never reach a brain."""
        if not token:
            return False
        candidates = [os.environ.get(k, "") for k in OWNER_TOKEN_ENV]
        cfg = self._svc("config")
        candidates.append(getattr(cfg, "mcp_token", "") or "")
        for c in candidates:
            if c and hmac.compare_digest(c.encode(), token.encode()):
                return True
        out = await _call(self._svc("policy"), "is_owner_token", token)
        return out is True

    # ---- notifier ----
    async def notify(self, title: str, body: str, *, kind: str, urgent: bool = False,
                     data: dict[str, Any] | None = None) -> None:
        record = {"at": time.time(), "title": title, "body": body, "kind": kind, "urgent": urgent,
                  "data": data or {}}
        self._append("notifications.jsonl", record)
        try:
            await _call(self._svc("notifier"), "notify", title, body, kind=kind, urgent=urgent, data=data or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("notifier failed: %s", type(exc).__name__)

    # ---- traces ----
    def trace(self, event: str, ctx: Any = None, **fields: Any) -> None:
        """Record a decision. Always lands in brains/decisions.jsonl; also in the call's
        trace (ctx) or the traces service. Callers pass no secrets and no prompt bodies."""
        record = {"at": time.time(), "event": event, **fields}
        self._append("decisions.jsonl", record)
        sink = None
        if ctx is not None and callable(getattr(ctx, "trace", None)):
            sink = ctx.trace
        elif self._svc("traces") is not None and callable(getattr(self._svc("traces"), "record", None)):
            sink = self._svc("traces").record
        if sink is None:
            return
        try:
            out = sink(event, **fields)
            if inspect.isawaitable(out):
                import asyncio
                asyncio.ensure_future(out)
        except Exception as exc:  # noqa: BLE001
            log.warning("trace sink failed: %s", type(exc).__name__)

    def _append(self, name: str, record: dict[str, Any]) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / name
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
            if path.stat().st_size > 20_000_000:
                path.replace(path.with_suffix(".1.jsonl"))
        except OSError as exc:
            log.warning("could not write %s: %s", name, exc)
