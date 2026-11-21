"""invoke over the channel: at most once per call_id. A retry while the call runs joins it, a
retry later gets the cached result; every call runs through the node's own MCP wrapper (invoke.py).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from websockets.exceptions import ConnectionClosed

from openajar.core import signing
from openajar.core.errors import (
    Cancelled,
    DeadlineExceeded,
    InvalidRequest,
    OpenajarError,
    PolicyDenied,
    ServiceUnavailable,
)
from openajar.core.tools import TOOL_NAME_RE
from openajar.node.channel import wire
from openajar.node.channel.connection import Connection
from openajar.node.channel.invoke import channel_call, failure, run_tool
from openajar.node.channel.streams import StreamPipe

log = logging.getLogger("openajar.channel")


CALL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass
class Call:
    call_id: str
    tool: str
    args: dict
    token: str
    session: str | None
    deadline: float | None
    replies: list[str]
    chain: set[str] = field(default_factory=set)
    caller: tuple | None = None  # who the token is (kind, id, owner) and its grant family root
    task: asyncio.Task | None = None
    cancel_reason: str | None = None
    created: float = field(default_factory=time.monotonic)


class CallsMixin:
    """invoke: at most once per call_id, a retry joins the running call or gets the cached result."""

    # ---- invoke ----
    async def _on_invoke(self, conn: Connection, message: wire.Envelope) -> None:
        body = message.body
        call_id, tool = body.get("call_id"), body.get("tool")
        if not isinstance(call_id, str) or not CALL_ID_RE.match(call_id):
            raise InvalidRequest("invoke needs a call_id (1 to 128 letters, digits, _ . : -)")
        if not isinstance(tool, str) or not TOOL_NAME_RE.match(tool):
            raise InvalidRequest("invoke needs a tool name")
        args = body.get("args") if body.get("args") is not None else {}
        if not isinstance(args, dict):
            raise InvalidRequest("invoke args must be an object")
        live = self.calls.get(call_id)
        if live is not None:
            live.replies.append(message.id)
            return
        call = self._new_call(message, call_id, tool, args)
        cached = self.store.get_call(call_id)
        if cached is not None and cached.result is not None:
            fresh = time.time() - cached.updated_at < self.timings.result_ttl
            if fresh and cached.state == "waiting":
                await self._retry_waiting(conn, message, cached, call)
                return
            if fresh:
                await self._send_result(message.id, call_id, cached.result)
                return
        self._start(call)

    def _new_call(self, message: wire.Envelope, call_id: str, tool: str, args: dict) -> Call:
        body = message.body
        deadline_value = body.get("deadline")
        deadline = wire.parse_time(deadline_value)
        if deadline_value is not None and deadline is None:
            raise InvalidRequest("deadline must be ISO 8601 UTC")
        token = body.get("grant_token") if isinstance(body.get("grant_token"), str) else ""
        principal = body.get("principal")
        session = principal.get("thread") if isinstance(principal, dict) else None
        session = session if isinstance(session, str) and len(session) <= 200 else None
        return Call(call_id, tool, args, token, session, deadline, [message.id])

    def _start(self, call: Call) -> None:
        resolved = self._resolved(call.token)
        call.chain = set() if resolved is None else {*resolved[1].chain_ids, *resolved[1].rids}
        call.caller = None if resolved is None else _caller_of(*resolved)
        self.store.drop_waiting(call.call_id)
        self.store.put_call(call.call_id, "running", tool=call.tool)
        self.calls[call.call_id] = call
        call.task = asyncio.create_task(self._perform(call), name=f"invoke-{call.call_id}")

    def _resolved(self, token: str) -> tuple[Any, Any] | None:
        policy = self.runtime.get("policy")
        try:
            with channel_call(policy, token):
                return policy.resolve(token)
        except Exception:  # noqa: BLE001  (the wrapper refuses it with the right code)
            return None

    def _renewed(self, waited: Call, retry: Call) -> bool:
        """May the retry's grant token stand in for the one the call waited under? A controller's
        member call token lapses within minutes, often before the approval, so its retry carries a
        fresh one. It does only when it resolves now, is the same principal under the same grant
        family root, and covers the call's lane at the tool's effect."""
        if not retry.token or retry.token == waited.token or waited.caller is None:
            return False
        resolved = self._resolved(retry.token)
        if resolved is None or _caller_of(*resolved) != waited.caller:
            return False
        spec = getattr(self.runtime.get("mcp"), "specs", {}).get(waited.tool)
        if spec is None:
            return False
        lane = waited.args.get(spec.lane_arg) if spec.lane_arg else None
        try:
            self.runtime.get("policy").precheck(resolved[1], spec.effect, lane_id=lane if isinstance(lane, str) else None)
        except OpenajarError:
            return False
        return True

    async def _retry_waiting(self, conn: Connection, message: wire.Envelope, cached: Any, retry: Call) -> None:
        """A retry of a call that waited for approval: once approved it runs (and uses the
        approval); still pending, the cached answer; denied or expired, a refusal. After a node
        restart the waiting call is gone from memory, so the retry itself runs, once, if it is the
        same call (tool and arguments) that waited."""
        approvals = self.runtime.get("approvals")
        state = "pending"
        if approvals is not None and cached.approval_id:
            with contextlib.suppress(OpenajarError):
                state = approvals.get(cached.approval_id).state
        resumable = self.resumable.get(cached.call_id)
        if state == "approved" and resumable is not None:
            self.resumable.pop(cached.call_id, None)
            resumable.replies = [message.id]
            resumable.task = None
            resumable.cancel_reason = None
            if self._renewed(resumable, retry):
                resumable.token, resumable.deadline = retry.token, retry.deadline
            self._start(resumable)
            return
        if state == "approved":
            waited = self.store.waiting_call(cached.call_id)
            if waited is not None and waited == (retry.tool, args_digest(retry.args)):
                self._start(retry)
                return
        if state in ("denied", "expired"):
            result = failure(PolicyDenied.code, f"approval {cached.approval_id} was {state}")
            self.store.put_call(cached.call_id, "done", result=result)
            self.store.drop_waiting(cached.call_id)
            self.resumable.pop(cached.call_id, None)
            await self._send_result(message.id, cached.call_id, result)
            return
        await self._send_result(message.id, cached.call_id, cached.result)

    async def _perform(self, call: Call) -> None:
        stored: dict
        try:
            if self.runtime.get("policy") is None:
                raise ServiceUnavailable("this node has no policy service, so it refuses calls from a controller")
            remaining = None
            if call.deadline is not None:
                remaining = call.deadline - self.controller_time()
                if remaining <= 0:
                    raise DeadlineExceeded("the call was past its deadline before it started")
            async with asyncio.timeout(remaining):
                stored = await run_tool(self.runtime, call.tool, call.args, token=call.token, session=call.session)
        except TimeoutError:
            stored = failure(DeadlineExceeded.code, "the call ran past its deadline and was stopped")
        except asyncio.CancelledError:
            if call.cancel_reason is None:
                self.calls.pop(call.call_id, None)
                raise  # shutting down: the row stays `running` and reads as interrupted on the next start
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            stored = failure(Cancelled.code, call.cancel_reason)
        except OpenajarError as exc:
            stored = failure(exc.code, exc.message)
        except Exception:
            log.exception("channel: %s failed", call.tool)
            stored = failure("engine_error", f"{call.tool} failed unexpectedly")
        self.calls.pop(call.call_id, None)
        error = stored.get("error") or {}
        if error.get("code") == "approval_pending":
            self.store.put_call(call.call_id, "waiting", result=stored, approval_id=error.get("approval_id"))
            self.store.put_waiting(call.call_id, call.tool, args_digest(call.args))
            self.resumable[call.call_id] = call
        else:
            self.store.put_call(call.call_id, "done", result=stored)
        for reply in call.replies:
            await self._send_result(reply, call.call_id, stored)

    async def _send_result(self, re: str, call_id: str, stored: dict) -> None:
        """Results are not outboxed: with no connection, the cache answers the retry instead."""
        conn = self.conn
        if conn is None:
            return
        content: list[dict] = []
        pushes: list[tuple[StreamPipe, bytes]] = []
        for item in stored.get("content") or []:
            if item.get("type") == "image" and isinstance(item.get("data"), str):
                try:
                    data = base64.b64decode(item["data"])
                    pipe = conn.streams.new(wire.ulid(), "file", stall_after=self.timings.stream_stall)
                except (ValueError, OverflowError):
                    continue
                pushes.append((pipe, data))
                content.append({"type": "image", "mime_type": item.get("mime_type") or "image/png",
                                "stream_id": pipe.id})
            elif item.get("type") == "text":
                content.append({"type": "text", "text": item.get("text", "")})
        body: dict = {"re": re, "ok": bool(stored.get("ok")), "content": content}
        if stored.get("error"):
            body["error"] = stored["error"]
        try:
            envelope = wire.Envelope("result", body, re=re)
            envelope.encode()
        except wire.Oversize:
            for pipe, _ in pushes:
                conn.streams.drop(pipe.id)
            body = {"re": re, **failure("invalid", "the result is larger than the 1 MiB message limit")}
            envelope, pushes = wire.Envelope("result", body, re=re), []
        with contextlib.suppress(ConnectionClosed):
            await conn.send(envelope)
            for pipe, data in pushes:
                pipe.task = asyncio.create_task(self._push(conn, pipe, data), name=f"push-{pipe.id}")

    async def _on_cancel(self, conn: Connection, message: wire.Envelope) -> None:
        call_id = message.body.get("call_id")
        if not isinstance(call_id, str):
            raise InvalidRequest("cancel needs a call_id")
        live = self.calls.get(call_id)
        if live is not None and live.task is not None and not live.task.done():
            live.cancel_reason = "the call was cancelled"
            live.task.cancel()
            return
        cached = self.store.get_call(call_id)
        if self.resumable.pop(call_id, None) is not None or (cached is not None and cached.state == "waiting"):
            self.store.drop_waiting(call_id)
            self.store.put_call(call_id, "done", result=failure(Cancelled.code, "the call was cancelled"))


def _caller_of(principal: Any, grant: Any) -> tuple:
    return (principal.kind, principal.id, principal.owner, grant.root_id)


def args_digest(args: dict) -> str:
    """What identifies a call's arguments, so a retry after a restart runs only the call that waited."""
    return hashlib.sha256(signing.canonical(args)).hexdigest()
