"""Live node sessions in the controller, and everything that goes through them.

One `NodeSession` per connected node (the newest connection wins). The hub routes `invoke`
to a node and waits for its `result`; when the node drops before answering, the call is sent
again with the same call_id once the node is back (the node answers a retry from its
at-most-once cache), until the call's deadline. Image content arrives on streams the node
pushes right after its `result`, and is collected with credit flow control.

Everything a node sends is untrusted input: sizes, types and ids are checked, unknown fields
are dropped, and strings are cut short before anything is stored or shown.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentos.controller import guide
from agentos.controller.routing import NodeView, ToolDef, parse_tools
from agentos.core import ceiling
from agentos.core.errors import AgentosError, Conflict, InvalidRequest, NotFound, ServiceUnavailable
from agentos.core.models import LANE_ID_RE, SPECIALS
from agentos.node.channel import standing, wire
from agentos.node.channel.streams import StreamPipe, StreamTable
from agentos.node.channel.workflows import HOOK_HEADERS

log = logging.getLogger("agentos.controller")

MAX_IMAGE_BYTES = 20 << 20
MAX_TOOL_PARTS = 16
REQUEST_TIMEOUT = 15.0
HOOK_TIMEOUT = 10.0  # a relay holds one of a node's few places: it gives it back sooner
DECISION_WAIT = 10.0
MAX_LANES = 2000
MAX_USAGE = 2**31 - 1
MAX_REPORT_BYTES = 512 * 1024
NODE_REPORTS = ("inventory", "system", "programs", "services", "doctor")
ID_RE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:")
TRACE_FIELDS = {"id": 64, "at": 40, "kind": 40, "tool": 64, "lane": 80, "workspace": 40, "principal": 120,
                "principal_kind": 20, "effect": 40, "status": 20, "summary": 1000, "error": 1000, "run_id": 64,
                "parent_id": 64, "grant_id": 64}
APPROVAL_TEXT = {"id": 64, "kind": 20, "state": 20, "effect": 40, "tool": 64, "lane": 80, "domain": 255,
                 "summary": 2000, "content_hash": 100, "answer": 2000, "reason": 500, "created_at": 40,
                 "expires_at": 40, "decided_by": 120, "decided_at": 40, "note": 1000, "requires": 20}
LANE_TEXT = {"id": 80, "workspace": 40, "name": 60, "kind": 20, "engine": 40, "state": 20, "signal": 40,
             "last_used": 40, "quarantine_reason": 300, "node": 40}


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _id(value: Any, limit: int = 64) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= limit and set(value) <= ID_RE_CHARS:
        return value
    return None


def clean_trace(event: Any) -> dict | None:
    if not isinstance(event, dict):
        return None
    out: dict[str, Any] = {key: _text(event.get(key), limit) for key, limit in TRACE_FIELDS.items()}
    cost = event.get("cost") if isinstance(event.get("cost"), dict) else {}
    tokens, usd = cost.get("tokens"), cost.get("usd")
    out["cost"] = {"tokens": tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else 0,
                   "usd": float(usd) if isinstance(usd, int | float) and not isinstance(usd, bool) else 0.0}
    duration = event.get("duration_ms")
    out["duration_ms"] = duration if isinstance(duration, int) and not isinstance(duration, bool) else None
    return out


def clean_approval(approval: Any) -> dict | None:
    if not isinstance(approval, dict) or _id(approval.get("id")) is None:
        return None
    out: dict[str, Any] = {key: _text(approval.get(key), limit) for key, limit in APPROVAL_TEXT.items()}
    preview = approval.get("preview")
    out["preview"] = {"text": preview["text"][:4000]} if isinstance(preview, dict) and isinstance(
        preview.get("text"), str) else None
    options = approval.get("options")
    out["options"] = [o[:200] for o in options[:10] if isinstance(o, str)] if isinstance(options, list) else None
    methods = approval.get("methods")  # a node with "confirmations": how the owner could say yes
    out["methods"] = [m[:40] for m in methods[:20] if isinstance(m, str)] if isinstance(methods, list) else None
    who = approval.get("requested_by")
    out["requested_by"] = {k: _text(who.get(k), 120) for k in ("kind", "id", "thread", "grant_id")} \
        if isinstance(who, dict) else None
    out["state"] = out["state"] or "pending"
    return out


def clean_lane(lane: Any, node_name: str) -> dict | None:
    if not isinstance(lane, dict) or not isinstance(lane.get("id"), str) or not LANE_ID_RE.fullmatch(lane["id"]):
        return None
    out: dict[str, Any] = {key: _text(lane.get(key), limit) for key, limit in LANE_TEXT.items()}
    control = lane.get("control")
    out["control"] = {"state": _text(control.get("state"), 20), "by": _text(control.get("by"), 120),
                      "since": _text(control.get("since"), 40),
                      "takeback_requested": bool(control.get("takeback_requested"))} \
        if isinstance(control, dict) else None
    proxy = lane.get("proxy")
    out["proxy"] = {"label": _text(proxy.get("label"), 40)} if isinstance(proxy, dict) else None
    out["exit_ip"] = None  # never kept, whatever a node sends
    out["node"] = node_name
    return out


def clean_heartbeat(body: dict) -> dict:
    load = body.get("load") if isinstance(body.get("load"), dict) else {}
    numbers = {}
    for key in ("cpu_pct", "mem_used_mb", "mem_total_mb", "disk_free_mb"):
        value = load.get(key)
        numbers[key] = value if isinstance(value, int | float) and not isinstance(value, bool) else None
    lanes = []
    for lane in (body.get("lanes") if isinstance(body.get("lanes"), list) else [])[:MAX_LANES]:
        if isinstance(lane, dict) and isinstance(lane.get("id"), str) and LANE_ID_RE.fullmatch(lane["id"]):
            item = {"id": lane["id"], "kind": _text(lane.get("kind"), 20), "state": _text(lane.get("state"), 20),
                    "signal": _text(lane.get("signal"), 40), "last_used": _text(lane.get("last_used"), 40)}
            for key in ("cpu_pct", "mem_mb"):  # additive: what the lane uses, when the node measured it
                value = lane.get(key)
                if isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= MAX_USAGE:
                    item[key] = value
            lanes.append(item)
    return {"load": numbers, "lanes": lanes, "degraded": bool(body.get("degraded")),
            "version": _text(body.get("version"), 40)}


def clean_ceiling(value: Any) -> dict | None:
    """`hello.ceiling`: what the node's owner lets this controller do there (agentos.policy.ceiling).
    Shown, never enforced here: the node enforces it."""
    if not isinstance(value, dict):
        return None
    effect = value.get("max_effect")
    lists = {name: value.get(name) if isinstance(value.get(name), list) else [] for name in ("specials", "streams")}
    return {"max_effect": effect if effect in ceiling.EFFECTS else None,
            "specials": [s for s in SPECIALS if s in lists["specials"]],
            "streams": [s for s in ceiling.STREAMS if s in lists["streams"]]}


class NodeSession:
    def __init__(self, ws: Any, node: dict):
        self.ws = ws
        self.node_id: str = node["id"]
        self.org_id: str = node["org_id"]
        self.name: str = node["name"]
        self.ceiling: dict | None = node.get("ceiling")  # from hello; None for a node that does not say
        self.supports: list[str] = list(node.get("supports") or [])  # from hello, what this controller knows
        self.connected_at = time.time()
        self.last_rx = time.monotonic()
        self.last_seq = 0
        self.heartbeat: dict = {}
        self.tools: dict[str, ToolDef] = {}
        self.tool_parts: list[list] = []  # a tool list announced in parts, until its last part
        self.degraded = False
        self.reports: dict[str, Any] = {}  # the node's latest `node` reports (system, programs, ...), kept in memory
        self.guide: dict | None = None  # its MCP instructions and prompts, served from /mcp (agentos.controller.guide)
        self.closed = asyncio.Event()
        self.request_ids: set[str] = set()  # the requests sent on this connection that await a reply
        self._lock = asyncio.Lock()
        self.streams = StreamTable(self.send, self.send_binary)

    async def send(self, envelope: wire.Envelope) -> None:
        text = envelope.encode()
        async with self._lock:
            await self.ws.send_text(text)

    async def send_binary(self, frame: bytes) -> None:
        async with self._lock:
            await self.ws.send_bytes(frame)

    @property
    def lane_ids(self) -> set[str]:
        return {lane["id"] for lane in self.heartbeat.get("lanes", [])}

    def view(self) -> NodeView:
        return NodeView(self.node_id, self.name, self.tools, self.lane_ids)

    def summary(self) -> dict:
        return {"connected": True, "connected_at": wire.iso(self.connected_at), "degraded": self.degraded,
                "load": self.heartbeat.get("load"), "lanes": self.heartbeat.get("lanes", []),
                "version": self.heartbeat.get("version"), "tools": len(self.tools),
                "ceiling": self.ceiling, "reports": sorted(self.reports)}


@dataclass
class PendingCall:
    call_id: str
    org_id: str
    node_id: str
    body: dict
    deadline: float
    future: asyncio.Future
    messages: set[str] = field(default_factory=set)


class Hub:
    def __init__(self, store: Any, keys: Any, *, clock: Callable[[], float] = time.time):
        self.store = store
        self.keys = keys
        self.clock = clock
        self.sessions: dict[str, NodeSession] = {}
        self.pending: dict[str, PendingCall] = {}
        self.by_message: dict[str, str] = {}
        self.requests: dict[str, tuple[str, asyncio.Future]] = {}
        self.approval_waiters: dict[tuple[str, str, str], list[asyncio.Future]] = {}
        self.listeners: list[Callable[[str, str, dict], None]] = []

    # ---- sessions ----
    async def register(self, session: NodeSession) -> None:
        old = self.sessions.get(session.node_id)
        self.sessions[session.node_id] = session
        session.last_seq = self.store.last_seq(session.node_id)
        if old is not None and old is not session:
            log.info("node %s connected again; closing its older connection", session.name)
            with contextlib.suppress(Exception):
                await old.ws.close(code=1000, reason="replaced by a newer connection")
        self.store.soon(self.store.seen, session.node_id)  # a touch: never waits for a lock
        self._notify(session.org_id, "node", {"id": session.node_id, "connected": True})

    async def resend_pending(self, session: NodeSession) -> None:
        for call in list(self.pending.values()):
            if call.node_id == session.node_id and not call.future.done():
                await self._send_invoke(session, call)

    def unregister(self, session: NodeSession) -> None:
        """The end of one connection. A connection a newer one replaced only fails the requests it
        carried itself; the node stays online on the newer one."""
        current = self.sessions.get(session.node_id) is session
        if current:
            self.sessions.pop(session.node_id, None)
        session.closed.set()
        session.streams.close_all("disconnected")
        for message_id, (node_id, future) in list(self.requests.items()):
            mine = message_id in session.request_ids or (current and node_id == session.node_id)
            if mine and not future.done():
                future.set_exception(ServiceUnavailable(f"node {session.name} disconnected"))
                self.requests.pop(message_id, None)
        session.request_ids.clear()
        if current:
            self._notify(session.org_id, "node", {"id": session.node_id, "connected": False})

    def online(self, org_id: str) -> list[NodeSession]:
        return [s for s in self.sessions.values() if s.org_id == org_id]

    def views(self, org_id: str) -> list[NodeView]:
        return [s.view() for s in self.online(org_id)]

    def session(self, org_id: str, node_id: str) -> NodeSession:
        session = self.sessions.get(node_id)
        if session is None or session.org_id != org_id:
            if self.store.org_node(org_id, node_id) is None:
                raise NotFound(f"no node {node_id}")
            raise ServiceUnavailable("that node is offline")
        return session

    def _notify(self, org_id: str, kind: str, payload: dict) -> None:
        for listener in list(self.listeners):
            try:
                listener(org_id, kind, payload)
            except Exception:
                log.exception("hub listener failed")

    # ---- invoke ----
    async def _send_invoke(self, session: NodeSession, call: PendingCall) -> None:
        envelope = wire.Envelope("invoke", call.body)
        call.messages.add(envelope.id)
        self.by_message[envelope.id] = call.call_id
        try:
            await session.send(envelope)
        except Exception:  # noqa: BLE001  (the node went away; the call is sent again when it is back)
            log.info("invoke %s: node %s went away before it was sent", call.call_id, session.name)

    async def invoke(self, org_id: str, node_id: str, tool: str, args: dict, *, principal: dict, grant_token: str,
                     timeout: float) -> tuple[dict, list[bytes | None]]:
        """(result body, image bytes in content order). Never raises for a node-side failure."""
        session = self.session(org_id, node_id)
        deadline = self.clock() + timeout
        call_id = wire.prefixed("call")
        body = {"call_id": call_id, "tool": tool, "args": args, "principal": principal,
                "grant_token": grant_token, "deadline": wire.iso(deadline)}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        call = PendingCall(call_id, org_id, node_id, body, deadline, future)
        self.pending[call_id] = call
        try:
            await self._send_invoke(session, call)
            try:
                result, pipes = await asyncio.wait_for(future, timeout)
            except TimeoutError:
                current = self.sessions.get(node_id)
                if current is not None:
                    with contextlib.suppress(Exception):
                        await current.send(wire.Envelope("cancel", {"call_id": call_id}))
                return _failure("deadline_exceeded", f"{tool} did not finish within {int(timeout)} seconds"), []
            images = []
            for pipe in pipes:
                images.append(await self._collect(pipe))
                current = self.sessions.get(node_id)
                if current is not None:
                    current.streams.drop(pipe.id)
            return result, images
        finally:
            self.pending.pop(call_id, None)
            for message_id in call.messages:
                self.by_message.pop(message_id, None)

    async def _collect(self, pipe: StreamPipe) -> bytes | None:
        chunks: list[bytes] = []
        size = 0
        try:
            async with asyncio.timeout(60):
                while True:
                    data = await pipe.read()
                    if data is None:
                        break
                    size += len(data)
                    if size > MAX_IMAGE_BYTES:
                        await pipe.close("too_large")
                        return None
                    chunks.append(data)
                    await pipe.consumed(len(data))
        except TimeoutError:
            return None
        return b"".join(chunks) if pipe.close_reason in ("eof", None) else None

    def _on_result(self, session: NodeSession, envelope: wire.Envelope) -> None:
        call_id = self.by_message.get(envelope.reply_to or "")
        call = self.pending.get(call_id or "")
        if call is None or call.node_id != session.node_id or call.future.done():
            return
        body = envelope.body
        error = body.get("error") if isinstance(body.get("error"), dict) else None
        content: list[dict] = []
        pipes: list[StreamPipe | None] = []
        for item in (body.get("content") if isinstance(body.get("content"), list) else [])[:50]:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                content.append({"type": "text", "text": item["text"]})
            elif item.get("type") == "image" and wire.is_ulid(item.get("stream_id")):
                try:
                    pipe = session.streams.new(item["stream_id"], "file")
                except (ValueError, OverflowError):
                    continue
                pipes.append(pipe)
                content.append({"type": "image", "mime_type": _text(item.get("mime_type"), 40) or "image/png",
                                "image": len(pipes) - 1})
        result = {"ok": bool(body.get("ok")), "content": content}
        if error is not None:
            result["error"] = {"code": _text(error.get("code"), 40) or "error",
                               "message": _text(error.get("message"), 600) or "",
                               **({"approval_id": error["approval_id"]} if _id(error.get("approval_id")) else {})}
        call.future.set_result((result, pipes))

    # ---- requests that expect a reply or an error ----
    async def _request(self, session: NodeSession, envelope: wire.Envelope, *, wait: float = REQUEST_TIMEOUT,
                       quiet_ok: bool = False) -> wire.Envelope | None:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.requests[envelope.id] = (session.node_id, future)
        session.request_ids.add(envelope.id)
        try:
            await session.send(envelope)
            try:
                return await asyncio.wait_for(future, wait)
            except TimeoutError:
                if quiet_ok:
                    return None
                raise ServiceUnavailable("the node did not answer in time") from None
        finally:
            self.requests.pop(envelope.id, None)
            session.request_ids.discard(envelope.id)

    def _on_reply(self, session: NodeSession, envelope: wire.Envelope) -> bool:
        entry = self.requests.get(envelope.reply_to or "")
        if entry is None or entry[0] != session.node_id or entry[1].done():
            return False
        entry[1].set_result(envelope)
        return True

    async def open_stream(self, org_id: str, node_id: str, kind: str, lane: str, params: dict) -> StreamPipe:
        session = self.session(org_id, node_id)
        stream_id = wire.ulid()
        pipe = session.streams.new(stream_id, kind, lane=lane)
        envelope = wire.Envelope("stream_open", {"stream_id": stream_id, "kind": kind, "lane": lane, "params": params})
        try:
            reply = await self._request(session, envelope)
        except BaseException:
            session.streams.drop(stream_id)
            raise
        if reply is None or reply.type != "stream_ready":
            session.streams.drop(stream_id)
            raise _coded(reply.body if reply is not None else {})
        return pipe

    async def send_resize(self, org_id: str, node_id: str, stream_id: str, cols: int, rows: int) -> None:
        session = self.session(org_id, node_id)
        await session.send(wire.Envelope("resize", {"stream_id": stream_id, "cols": cols, "rows": rows}))

    # ---- approvals ----
    async def decide(self, org_id: str, node_id: str, approval_id: str, decision: str, *, decided_by: str,
                     answer: str | None = None, proof: dict | None = None) -> dict:
        if decision not in ("approved", "denied"):
            raise InvalidRequest("decision must be approved or denied")
        approval = self.store.approval(org_id, node_id, approval_id)
        if approval is None:
            raise NotFound(f"no approval {approval_id}")
        if approval["state"] != "pending":
            raise Conflict(f"approval {approval_id} is already {approval['state']}")
        session = self.session(org_id, node_id)
        body = {"approval_id": approval_id, "decision": decision, "content_hash": approval.get("content_hash"),
                "decided_by": decided_by}
        if answer is not None:
            body["answer"] = answer[:2000]
        if proof is not None and decision == "approved" and "confirmations" in session.supports:
            body["proof"] = proof
        key = (org_id, node_id, approval_id)
        changed: asyncio.Future = asyncio.get_running_loop().create_future()
        self.approval_waiters.setdefault(key, []).append(changed)
        envelope = wire.Envelope("approval_decision", body)
        refused: asyncio.Future = asyncio.get_running_loop().create_future()
        self.requests[envelope.id] = (node_id, refused)
        session.request_ids.add(envelope.id)
        try:
            await session.send(envelope)
            done, _ = await asyncio.wait({changed, refused}, timeout=DECISION_WAIT,
                                         return_when=asyncio.FIRST_COMPLETED)
            if refused in done:
                raise _coded(refused.result().body)
            if changed in done:
                return self.store.approval(org_id, node_id, approval_id) or approval
            return {**approval, "state": "pending", "sent": True}
        finally:
            self.requests.pop(envelope.id, None)
            session.request_ids.discard(envelope.id)
            waiters = self.approval_waiters.get(key, [])
            if changed in waiters:
                waiters.remove(changed)
            if not waiters:
                self.approval_waiters.pop(key, None)

    # ---- controller-signed pushes ----
    async def push_signed(self, session: NodeSession, kind: str, body: dict, *, wait_error: float = 0.0) -> None:
        # dated and with a fresh nonce, so a node refuses a copy replayed later (PROTOCOL.md "Freshness and replay")
        body = {**body, "ts": wire.iso(self.clock()), "nonce": secrets.token_urlsafe(12)}
        envelope = wire.Envelope(kind, self.keys.sign(kind, session.node_id, body))
        if wait_error <= 0:
            await session.send(envelope)
            return
        reply = await self._request(session, envelope, wait=wait_error, quiet_ok=True)
        if reply is not None and reply.type == "error":
            raise _coded(reply.body)

    async def push_revoke(self, org_id: str, ids: list[str]) -> int:
        sent = 0
        for session in self.online(org_id):
            with contextlib.suppress(Exception):
                await self.push_signed(session, "revoke", {"revocation_ids": ids})
                sent += 1
        return sent

    def mesh_freeze_body(self, node_id: str, state: dict | None) -> dict:
        """The signed mesh freeze state for one node (sent live, and in every welcome)."""
        body: dict = {"frozen": False}
        if state is not None:
            body = {"frozen": True, "freeze_id": state["freeze_id"], "reason": state["reason"], "by": state["by"]}
        return self.keys.sign("mesh_freeze", node_id, {**body, "ts": wire.iso(self.clock()),
                                                       "nonce": secrets.token_urlsafe(12)})

    async def push_mesh_freeze(self, org_id: str, state: dict | None, *, wait_error: float = 1.0) -> dict[str, str]:
        """Send the mesh freeze state to every online node at once. {node_id: "sent" | "<code>: <message>"}:
        a node answers only a refusal, so "sent" after `wait_error` without one."""
        results = await asyncio.gather(*(self._push_mesh_freeze_one(org_id, s, state, wait_error)
                                         for s in self.online(org_id)))
        return dict(results)

    async def _push_mesh_freeze_one(self, org_id: str, session: NodeSession, state: dict | None,
                                    wait_error: float = 1.0) -> tuple[str, str]:
        body = self.mesh_freeze_body(session.node_id, state)
        try:
            reply = await self._request(session, wire.Envelope("mesh_freeze", body), wait=wait_error, quiet_ok=True)
        except Exception as exc:  # noqa: BLE001  (offline in between: it gets it at its next welcome)
            return session.node_id, f"{getattr(exc, 'code', 'unavailable')}: {type(exc).__name__}"
        if reply is not None and reply.type == "error":
            return session.node_id, f"{reply.body.get('code')}: {_text(reply.body.get('message'), 200)}"
        if state is not None:
            await self.store.run(self.store.mesh_freeze_applied, org_id, session.node_id, state["freeze_id"])
        return session.node_id, "sent"

    async def catch_up(self, session: NodeSession, *, mesh: dict | None, revoked: list[str],
                       standing_sent: dict | None = None) -> None:
        """After a node registers: what changed since its welcome was built. A mesh freeze (or its lift),
        a revocation or a standing approval change in that gap was queued rather than sent, so it is
        sent here."""
        try:
            now = self.store.mesh_freeze(session.org_id)
            if (now or {}).get("freeze_id") != (mesh or {}).get("freeze_id"):
                await self._push_mesh_freeze_one(session.org_id, session, now)
            known = set(revoked)
            fresh = [i for i in self.store.revoked_ids(session.org_id) if i and i not in known]
            if fresh:
                await self.push_signed(session, "revoke", {"revocation_ids": fresh})
            if standing_sent is not None and (
                    standing_sent.get("set") != self.store.standing(session.org_id, session.node_id)
                    or (standing_sent.get("revoke") or []) != self.store.standing_revokes(session.org_id, session.node_id)):
                body = self.standing_body(session.org_id, session.node_id)
                await self._request(session, wire.Envelope("standing", body), quiet_ok=True)
        except Exception:  # the next welcome carries it again
            log.warning("node %s: could not send what changed since its welcome", session.name, exc_info=True)

    async def lane_control(self, org_id: str, node_id: str, lane: str, action: str, *, by: str) -> None:
        session = self.session(org_id, node_id)
        await self.push_signed(session, "lane_control", {"lane": lane, "action": action, "by": by}, wait_error=2.0)

    async def push_standing(self, org_id: str, node_id: str, *, whole: list | None = None,
                            upsert: list | None = None, revoke: list | None = None) -> dict:
        """Standing approvals to one node (docs/PROTOCOL.md `standing`): signed, dated and with a fresh
        nonce. `whole` is the `set` (everything the node should hold from here). The node checks each;
        this controller keeps what the node should hold and the revokes it has not confirmed, and
        sends both in every welcome (`welcome.standing`), so a node offline now, or one that missed a
        revoke, is reconciled when it connects. Returns the node's `standing_result { applied,
        refused, revoked, withdrawn }`, or `queued: true` when the node is offline. An item the node
        refuses is dropped from what it should hold. A node that does not know the message answers
        `unsupported`, raised here like any refusal."""
        if self.store.org_node(org_id, node_id) is None:
            raise NotFound(f"no node {node_id}")
        items = (whole if whole is not None else upsert) or []
        if any(not isinstance(i, dict) or not isinstance(i.get("id"), str) or not standing.ID_RE.match(i["id"])
               for i in items) or any(not isinstance(i, str) or not standing.ID_RE.match(i) for i in revoke or []):
            raise InvalidRequest("each standing approval, and each id to revoke, needs an id like sa_<a-z 0-9 _>")
        held = {i["id"] for i in self.store.standing(org_id, node_id)} if whole is None else set()
        if len(held | {i["id"] for i in items}) > standing.MAX_ITEMS:
            raise InvalidRequest(f"a node holds at most {standing.MAX_ITEMS} standing approvals from this controller")
        self.store.add_standing_revokes(org_id, node_id, list(revoke or []))
        self.store.put_standing(org_id, node_id, items, whole=whole is not None)
        session = self.sessions.get(node_id)
        if session is None or session.org_id != org_id:
            log.info("node %s is offline: its standing approvals go in its next welcome", node_id)
            return {"applied": [], "refused": [], "revoked": [], "withdrawn": [], "queued": True}
        body: dict = {"ts": wire.iso(self.clock()), "nonce": secrets.token_urlsafe(12)}
        for key, value in (("set", whole), ("upsert", upsert), ("revoke", self.store.standing_revokes(org_id, node_id))):
            if value is not None and (value or key != "revoke"):
                body[key] = value
        reply = await self._request(session, wire.Envelope("standing", self.keys.sign("standing", node_id, body)))
        if reply is None or reply.type != "standing_result":
            raise _coded(reply.body if reply is not None else {})
        return {key: reply.body.get(key) or [] for key in ("applied", "refused", "revoked", "withdrawn")}

    async def push_ask_first(self, org_id: str, node_id: str, changes: list[dict]) -> dict:
        """A person's "Ask me first" changes to one node (docs/PROTOCOL.md `ask_first`): signed, dated
        and with a fresh nonce. Returns the node's `ask_first_result { applied, refused }`. The node
        holds the switches, so nothing is kept here; a node offline raises, and a node that does not
        know the message answers `unsupported`, raised here like any refusal."""
        session = self.session(org_id, node_id)
        body = {"ts": wire.iso(self.clock()), "nonce": secrets.token_urlsafe(12), "changes": changes}
        reply = await self._request(session, wire.Envelope("ask_first", self.keys.sign("ask_first", node_id, body)))
        if reply is None or reply.type != "ask_first_result":
            raise _coded(reply.body if reply is not None else {})
        return {key: reply.body.get(key) or [] for key in ("applied", "refused")}

    async def push_workflow(self, org_id: str, node_id: str, body: dict) -> dict:
        """A person's action on a workflow (docs/WORKFLOWS.md `workflow`): signed, dated and with a
        fresh nonce. The node holds the workflow, so nothing is kept here."""
        session = self.session(org_id, node_id)
        signed = {**body, "ts": wire.iso(self.clock()), "nonce": secrets.token_urlsafe(12)}
        reply = await self._request(session, wire.Envelope("workflow", self.keys.sign("workflow", node_id, signed)))
        if reply is None or reply.type != "workflow_result":
            raise _coded(reply.body if reply is not None else {})
        return reply.body

    async def relay_hook(self, node_id: str, token: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
        """An inbound webhook to its node, byte for byte: the node verifies it, and nothing is stored
        here. An offline node is a 503, and the sender tries again with a fresh timestamp."""
        session = self.sessions.get(node_id)
        if session is None:
            return 503, {"error": {"code": "unavailable", "message": "that computer is offline; try again"}}
        kept = {k: headers[k] for k in HOOK_HEADERS if k in headers}
        envelope = wire.Envelope("hook_relay", {"token": token, "body_b64": base64.b64encode(body).decode(), "headers": kept})
        try:
            reply = await self._request(session, envelope, wait=HOOK_TIMEOUT)
        except AgentosError:
            return 504, {"error": {"code": "unavailable", "message": "that computer did not answer in time; try again"}}
        if reply is None or reply.type != "hook_relay_result":
            return 502, {"error": {"code": "unsupported", "message": "that computer does not take webhooks yet"}}
        status = reply.body.get("status")
        payload = reply.body.get("body") or {}
        retry = reply.body.get("retry_after")
        return (status if isinstance(status, int) else 502), ({**payload, "retry_after": retry} if retry else payload)

    async def relay_unsubscribe(self, node_id: str, token: str) -> None:
        """An unsubscribe link to its node. Whatever the token means, the person sees the same page."""
        session = self.sessions.get(node_id)
        if session is not None:
            await self._request(session, wire.Envelope("unsubscribe_relay", {"token": token}), quiet_ok=True)

    def standing_body(self, org_id: str, node_id: str) -> dict:
        """The signed `standing` for a welcome: the whole set the node should hold, and the revokes it
        has not confirmed."""
        body: dict = {"ts": wire.iso(self.clock()), "nonce": secrets.token_urlsafe(12),
                      "set": self.store.standing(org_id, node_id)}
        revokes = self.store.standing_revokes(org_id, node_id)
        if revokes:
            body["revoke"] = revokes
        return self.keys.sign("standing", node_id, body)

    def _on_standing_result(self, session: NodeSession, body: dict) -> None:
        """What the node did with a `standing` (sent live or in its welcome): a revoke it confirmed (or
        refused, which it will always do) is no longer pending, and an item it refused is no longer
        one it should hold."""
        refused = [r.get("id") for r in body.get("refused") or [] if isinstance(r, dict)]
        refused = [i for i in refused if isinstance(i, str) and i]
        revoked = [i for i in body.get("revoked") or [] if isinstance(i, str)]
        self.store.drop_standing(session.org_id, session.node_id, refused)
        self.store.clear_standing_revokes(session.org_id, session.node_id, [*revoked, *refused])
        if refused:
            log.info("node %s refused %d standing approval(s)", session.name, len(refused))

    async def config_update(self, org_id: str, node_id: str, config: dict) -> None:
        session = self.session(org_id, node_id)
        await self.push_signed(session, "config_update", {"config": config}, wait_error=2.0)

    # ---- what nodes send ----
    async def handle(self, session: NodeSession, envelope: wire.Envelope) -> None:
        kind = envelope.type
        if kind == "heartbeat":
            hb = clean_heartbeat(envelope.body)
            session.heartbeat = hb
            session.degraded = hb["degraded"]
            self.store.soon(self.store.seen, session.node_id,  # a touch: never waits for a lock
                            {"load": hb["load"], "lanes": hb["lanes"], "degraded": hb["degraded"]},
                            version=hb["version"])
            self._notify(session.org_id, "heartbeat", {"id": session.node_id})
        elif kind in wire.OUTBOX_TYPES:
            await self._outboxed(session, envelope)
        elif kind == "result":
            self._on_result(session, envelope)
        elif kind == "progress":
            log.debug("progress from %s: %s", session.name, _text(envelope.body.get("message"), 200))
        elif kind == "standing_result":
            self._on_standing_result(session, envelope.body)
            self._on_reply(session, envelope)
        elif kind in ("stream_ready", "ask_first_result", "workflow_result", "hook_relay_result",
                      "unsubscribe_relay_result"):
            self._on_reply(session, envelope)
        elif kind == "stream_close":
            session.streams.on_close(envelope.body)
        elif kind == "stream_credit":
            session.streams.on_credit(envelope.body)
        elif kind == "error":
            self._on_error(session, envelope)
        elif kind in ("audit_anchor", "audit_anchors_get"):
            await self._on_audit_anchor(session, envelope)
        else:
            await session.send(wire.error_envelope("unsupported", f"unknown message type {kind[:40]!r}",
                                                   envelope.id or None))

    # ---- audit anchors (agentos.policy.anchors) ----
    async def _on_audit_anchor(self, session: NodeSession, envelope: wire.Envelope) -> None:
        """Keep a node's checkpoint heads, or hand them back, each with a receipt this controller
        signs. Never raises: a bad batch is answered with an error, and the read loop goes on."""
        try:
            if envelope.type == "audit_anchors_get" and "after" in envelope.body:
                reply = wire.Envelope("audit_anchors_result", self.anchors_page(session, envelope.body), re=envelope.id)
            elif envelope.type == "audit_anchors_get":  # a node from before paging: its newest heads, once
                limit = envelope.body.get("limit")
                limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and 0 < limit <= 2000 else 2000  # < 1 MiB
                receipts = self.store.anchors(session.org_id, session.node_id, limit=limit)
                reply = wire.Envelope("audit_anchors_result", {"receipts": receipts}, re=envelope.id)
            else:
                reply = wire.Envelope("audit_anchor_receipt", self.take_anchors(session, envelope.body.get("anchors")),
                                      re=envelope.id)
        except Exception:
            log.exception("node %s: audit anchors could not be stored", session.name)
            reply = wire.error_envelope("unavailable", "the controller could not store audit anchors right now",
                                        envelope.id or None)
        await session.send(reply)

    ANCHOR_PAGE = 1000  # receipts per audit_anchors_result page: about 400 bytes each, so well under 1 MiB

    def anchors_page(self, session: NodeSession, body: dict) -> dict:
        """One page of the node's heads after the cursor `after` ("" for the first page), and
        `next`: the cursor for the page after it, or null when this was the last."""
        size = body.get("page")
        size = size if isinstance(size, int) and not isinstance(size, bool) and 0 < size <= self.ANCHOR_PAGE \
            else self.ANCHOR_PAGE
        after = None
        cursor = body.get("after")
        if isinstance(cursor, str) and ":" in cursor:
            n, _, chain = cursor.partition(":")
            if 0 < len(n) <= 18 and n.isascii() and n.isdigit():  # SQLite's integers, ASCII digits only
                after = (chain, int(n))
        receipts, nxt = self.store.anchors_page(session.org_id, session.node_id, after=after, limit=size)
        return {"receipts": receipts, "next": f"{nxt[1]}:{nxt[0]}" if nxt else None}

    def take_anchors(self, session: NodeSession, items: Any) -> dict:
        from agentos.policy import anchors as anchor_rules

        receipts, conflicts, refused = [], [], 0
        received_at = wire.iso(self.clock())

        def sig_for(anchor: dict, at: str) -> str:
            body = {"anchor": anchor, "received_at": at}
            return self.keys.sign(anchor_rules.RECEIPT_KIND, session.node_id, body)["sig"]

        for item in (items if isinstance(items, list) else [])[:50]:
            if anchor_rules.valid(item) is not None:
                refused += 1
                continue
            anchor = anchor_rules.core({**item, "v": anchor_rules.ANCHOR_V})
            receipt, how = self.store.put_anchor(session.org_id, session.node_id, anchor, received_at, sig_for)
            receipts.append(receipt)
            if how == "conflict":
                conflicts.append(anchor["n"])
                log.error("node %s sent another head for audit checkpoint %s of chain %s than it anchored before: "
                          "its log changed after it was anchored", session.name, anchor["n"], anchor["chain"])
                self._notify(session.org_id, "audit_anchor_conflict", {"id": session.node_id, "n": anchor["n"]})
        return {"receipts": receipts, "conflicts": conflicts, "refused": refused}

    def _on_error(self, session: NodeSession, envelope: wire.Envelope) -> None:
        if self._on_reply(session, envelope):
            return
        call_id = self.by_message.get(envelope.reply_to or "")
        call = self.pending.get(call_id or "")
        if call is not None and not call.future.done() and call.node_id == session.node_id:
            body = envelope.body
            call.future.set_result((_failure(_text(body.get("code"), 40) or "error",
                                             _text(body.get("message"), 600) or ""), []))
            return
        stream_id = envelope.body.get("stream_id")
        if isinstance(stream_id, str):
            session.streams.on_close({"stream_id": stream_id, "reason": "error"})
        log.info("node %s: %s: %s", session.name, _text(envelope.body.get("code"), 40),
                 _text(envelope.body.get("message"), 200))

    async def _outboxed(self, session: NodeSession, envelope: wire.Envelope) -> None:
        seq = envelope.seq
        if seq is None or seq > session.last_seq:
            try:
                self._apply(session, envelope)
            except Exception:
                log.exception("node %s: dropped a %s that could not be applied", session.name, envelope.type)
            if seq is not None:
                session.last_seq = seq
                self.store.set_last_seq(session.node_id, seq)
        if seq is not None:
            await session.send(wire.Envelope("ack", {"upto_seq": session.last_seq}))

    def _apply(self, session: NodeSession, envelope: wire.Envelope) -> None:
        body = envelope.body
        org, node = session.org_id, session.node_id
        if envelope.type == "traces":
            events = [e for e in (clean_trace(x) for x in (body.get("events") or [])[:wire.MAX_TRACES_PER_BATCH]) if e]
            if events:
                self.store.add_traces(org, node, events)
                self._notify(org, "trace", {"count": len(events)})
        elif envelope.type == "approval_request":
            approval = clean_approval(body.get("approval"))
            if approval is not None and self.store.put_approval(org, node, approval):
                self._notify(org, "approval", {"id": approval["id"], "node_id": node, "state": approval["state"]})
        elif envelope.type == "event":
            self._event(session, body.get("kind"), body.get("payload"))

    def _event(self, session: NodeSession, kind: Any, payload: Any) -> None:
        org, node = session.org_id, session.node_id
        if not isinstance(payload, dict):
            return
        if kind == "lane":
            lane = clean_lane(payload, session.name)
            if lane is not None:
                self.store.put_lane(org, node, lane)
                self._notify(org, "lane", {"id": lane["id"], "node_id": node})
        elif kind == "approval":
            approval_id, state = _id(payload.get("id")), _text(payload.get("state"), 20)
            if approval_id and state:
                self.store.set_approval_state(org, node, approval_id, state, _text(payload.get("decided_by"), 120))
                for waiter in self.approval_waiters.get((org, node, approval_id), []):
                    if not waiter.done():
                        waiter.set_result(state)
                self._notify(org, "approval", {"id": approval_id, "node_id": node, "state": state})
        elif kind == "node":
            if "tools" in payload:
                tools = self._tool_list(session, payload)
                if tools is not None:
                    session.tools = parse_tools(tools)
                    self._notify(org, "tools", {"id": node, "count": len(session.tools)})
            if "guide" in payload:
                session.guide = guide.clean(payload.get("guide"))
            if "degraded" in payload:
                session.degraded = bool(payload.get("degraded"))
                self.store.add_event(org, node, "node", {"degraded": session.degraded,
                                                         "since": _text(payload.get("since"), 40)})
            if isinstance(payload.get("inventory"), dict):
                self.store.add_event(org, node, "inventory", {"keys": sorted(map(str, payload["inventory"]))[:20]})
            for report in NODE_REPORTS:  # additive (docs/PROTOCOL.md): kept as sent, shown as text, never run
                value = payload.get(report)
                if isinstance(value, dict | list) and len(json.dumps(value, default=str)) <= MAX_REPORT_BYTES:
                    session.reports[report] = value
        elif kind == "standing":  # a use or an end of a standing approval this controller passed through
            used, remaining = payload.get("used"), payload.get("remaining")
            standing_id = _id(payload.get("id"))
            if standing_id and payload.get("state") in ("revoked", "expired"):  # it ended on the node
                self.store.drop_standing(org, node, [standing_id])
            elif standing_id and isinstance(used, int) and not isinstance(used, bool):
                self.store.standing_used(org, node, standing_id, used)
            self.store.add_event(org, node, "standing", {
                "id": _id(payload.get("id")), "state": _text(payload.get("state"), 20),
                "used": used if isinstance(used, int) and not isinstance(used, bool) else None,
                "remaining": remaining if isinstance(remaining, int) and not isinstance(remaining, bool) else None})
        elif kind == "run":
            run = {key: _text(payload.get(key), 300) for key in ("id", "lane", "flow", "status", "summary",
                                                                 "started_at", "finished_at")}
            self.store.add_event(org, node, "run", run)


    def _tool_list(self, session: NodeSession, payload: dict) -> list | None:
        """The whole tool list once it has arrived: at once, or as `tools_part { index, count }`
        parts in order (outbox items arrive in seq order). A part out of order drops what was
        gathered; the node announces its tools again after every welcome."""
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        part = payload.get("tools_part")
        if part is None:
            session.tool_parts = []
            return tools
        index = part.get("index") if isinstance(part, dict) else None
        count = part.get("count") if isinstance(part, dict) else None
        if not all(isinstance(n, int) and not isinstance(n, bool) for n in (index, count)) \
                or not 0 <= index < count <= MAX_TOOL_PARTS:
            log.info("node %s: ignored a malformed tools_part", session.name)
            return None
        if index == 0:
            session.tool_parts = []
        if index != len(session.tool_parts):
            log.info("node %s: tools part %d arrived out of order; waiting for a new list", session.name, index)
            session.tool_parts = []
            return None
        session.tool_parts.append(tools)
        if index < count - 1:
            return None
        whole, session.tool_parts = [t for chunk in session.tool_parts for t in chunk], []
        return whole


def _failure(code: str, message: str) -> dict:
    return {"ok": False, "content": [], "error": {"code": code, "message": message}}


def _coded(body: dict) -> AgentosError:
    error = AgentosError(_text(body.get("message"), 300) or "the node refused this")
    error.code = _text(body.get("code"), 40) or "error"
    return error
