"""`/v1/node/connect`: the controller end of the node channel handshake and session.

On every accepted socket the controller sends `challenge { nonce }` at once. Then:
- `enroll { join_token, node_pubkey, name, ..., proof? }` from a node with no identity: a `proof`
  (the node key's signature over `agentos-node-enroll:v1:{nonce}:{node_pubkey}` with this socket's
  nonce) must verify, or the enroll is refused with `invalid` and the token is not spent; an
  enroll without one is accepted and logged. Then the join token is checked and consumed, the
  node recorded, `enrolled { node_id, org_id, grant_pubkey, controller_pubkey, config }` sent,
  and the socket closed; the node dials again to authenticate.
  Refusals are `error` with `denied`, `already_used`, `expired`, `conflict` or `invalid`.
- `hello { node_id, nonce, sig, epoch?, ceiling? }`: the nonce must be the one this socket was
  given and must never have been used, and `sig` must verify against the node key recorded at
  enrollment. A new `epoch` means the node's seq counter started over, so its dedupe is reset.
  `ceiling` is what the node's owner lets this controller do there, kept for the panel.
  Then `welcome { node_id, server_time, config, revoked_grants, mesh_freeze, standing? }` and the
  session runs; `standing` (the standing approvals the node should hold, and the revokes it has
  not confirmed) goes to a node whose hello `supports` it.
Anything else before authentication gets an `error` and the socket stays open until the
handshake times out. After welcome, unknown message types get `error unsupported`; a text
frame over 1 MiB gets `error invalid`; neither closes the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from openajar.controller.hub import NodeSession, clean_ceiling
from openajar.controller.routing import NODE_NAME_RE
from openajar.controller.store import EnrollRefused
from openajar.core import signing
from openajar.node.channel import wire

log = logging.getLogger("openajar.controller.connect")

MAX_HANDSHAKE_MESSAGES = 5
EPOCH_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
SUPPORTS = ("standing", "confirmations")  # hello.supports this controller knows


async def _receive(websocket: WebSocket, timeout: float | None) -> dict:
    if timeout is None:
        return await websocket.receive()
    async with asyncio.timeout(timeout):
        return await websocket.receive()


def _enroll_fields(body: dict) -> dict:
    token, pubkey, name = body.get("join_token"), body.get("node_pubkey"), body.get("name")
    if not isinstance(token, str) or not 1 <= len(token) <= 200:
        raise EnrollRefused("invalid", "enroll needs a join_token")
    try:
        signing.public_key(pubkey)
    except ValueError:
        raise EnrollRefused("invalid", "node_pubkey must be an unpadded base64url Ed25519 key") from None
    if not isinstance(name, str) or not NODE_NAME_RE.match(name):
        raise EnrollRefused("invalid", "the node name must be lowercase letters, digits and dashes (at most 31)")
    caps = body.get("capabilities") if isinstance(body.get("capabilities"), list) else []
    warm = body.get("max_warm_lanes")
    return {"token": token, "pubkey": pubkey, "name": name,
            "version": body["version"][:40] if isinstance(body.get("version"), str) else None,
            "os_name": body["os"][:40] if isinstance(body.get("os"), str) else None,
            "arch": body["arch"][:40] if isinstance(body.get("arch"), str) else None,
            "capabilities": sorted({c for c in caps if c in wire.CAPABILITIES}),
            "max_warm_lanes": warm if isinstance(warm, int) and not isinstance(warm, bool) and 0 < warm <= 1000
            else None}


class NodeEndpoint:
    def __init__(self, controller: Any):
        self.controller = controller
        self.handshake_timeout = wire.HANDSHAKE_TIMEOUT
        self.dead_after = wire.DEAD_AFTER

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        nonce = secrets.token_urlsafe(24)
        try:
            await websocket.send_text(wire.Envelope("challenge", {"nonce": nonce}).encode())
            node = await self._handshake(websocket, nonce)
        except (WebSocketDisconnect, TimeoutError, RuntimeError):
            node = None
        if node is None:
            with contextlib.suppress(Exception):
                await websocket.close(code=1008)
            return
        await self._session(websocket, node)

    async def _reply_error(self, websocket: WebSocket, code: str, message: str, re: str | None = None) -> None:
        await websocket.send_text(wire.error_envelope(code, message, re).encode())

    async def _handshake(self, websocket: WebSocket, nonce: str) -> dict | None:
        deadline = time.monotonic() + self.handshake_timeout
        for _ in range(MAX_HANDSHAKE_MESSAGES):
            message = await _receive(websocket, max(0.1, deadline - time.monotonic()))
            if message["type"] == "websocket.disconnect":
                return None
            text = message.get("text")
            if text is None:
                await self._reply_error(websocket, "invalid", "authenticate before sending stream data")
                continue
            try:
                envelope = wire.decode(text)
            except wire.WireError as exc:
                await self._reply_error(websocket, exc.code, exc.message, exc.re)
                continue
            if envelope.type == "enroll":
                await self._enroll(websocket, envelope, nonce)
                return None
            if envelope.type == "hello":
                return await self._hello(websocket, envelope, nonce)
            await self._reply_error(websocket, "invalid", "send hello (or enroll) first", envelope.id or None)
        return None

    async def _enroll(self, websocket: WebSocket, envelope: wire.Envelope, nonce: str) -> None:
        store, keys = self.controller.store, self.controller.keys
        try:
            fields = _enroll_fields(envelope.body)
            proof = envelope.body.get("proof")
            if proof is None:  # every node of this version sends it; the join token stays unspent
                raise EnrollRefused("invalid", "enroll needs proof: node_pubkey's signature over this "
                                               "connection's challenge (update the node)")
            if not signing.verify(fields["pubkey"], proof, wire.enroll_message(nonce, fields["pubkey"])):
                raise EnrollRefused("invalid", "the proof does not match node_pubkey and this connection's challenge")
            node = await store.run(store.enroll, fields.pop("token"), **fields)
        except EnrollRefused as exc:
            log.info("refused an enrollment: %s", exc.code)
            await self._reply_error(websocket, exc.code, exc.message, envelope.id or None)
            return
        body = {"node_id": node["id"], "org_id": node["org_id"], "grant_pubkey": keys.grant_pubkey(node["org_id"]),
                "controller_pubkey": keys.controller_pubkey, "config": self.controller.node_config(node)}
        await websocket.send_text(wire.Envelope("enrolled", body, re=envelope.id or None).encode())
        log.info("enrolled node %s (%s) in org %s", node["name"], node["id"], node["org_id"])
        with contextlib.suppress(Exception):
            await websocket.close(code=1000)

    async def _hello(self, websocket: WebSocket, envelope: wire.Envelope, nonce: str) -> dict | None:
        body = envelope.body
        node_id, given, sig = body.get("node_id"), body.get("nonce"), body.get("sig")
        node = self.controller.store.node(node_id) if isinstance(node_id, str) and len(node_id) <= 64 else None
        ok = (node is not None and node.get("removed_at") is None and given == nonce
              and signing.verify(node["pubkey"], sig, wire.hello_message(nonce, node_id)))
        store = self.controller.store
        if ok and not await store.run(store.use_nonce, nonce, node_id):
            ok = False  # a replayed hello
        if ok and (store.node(node_id) or {}).get("removed_at") is not None:
            ok = False  # removed while this hello waited for the store
        if not ok:
            log.warning("refused a hello for %s", str(node_id)[:64])
            await self._reply_error(websocket, "denied", "this node could not be authenticated", envelope.id or None)
            return None
        epoch = body.get("epoch")
        if isinstance(epoch, str) and EPOCH_RE.match(epoch):
            changed = await store.run(store.set_seq_epoch, node_id, epoch)
            if changed and node.get("seq_epoch") is not None:
                log.warning("node %s started its seq over (epoch %s); its dedupe starts again", node["name"], epoch)
        supports = body.get("supports")
        supports = [s for s in supports if s in SUPPORTS] if isinstance(supports, list) else []
        return {**node, "ceiling": clean_ceiling(body.get("ceiling")), "supports": supports}

    async def _session(self, websocket: WebSocket, node: dict) -> None:
        hub, store = self.controller.hub, self.controller.store
        session = NodeSession(websocket, node)
        mesh = store.mesh_freeze(node["org_id"])  # a node offline at a mesh freeze gets it here, first
        welcome = {"node_id": node["id"], "server_time": wire.iso(), "config": self.controller.node_config(node),
                   "revoked_grants": store.revoked_ids(node["org_id"]),
                   "mesh_freeze": hub.mesh_freeze_body(node["id"], mesh)}
        if "standing" in node["supports"]:  # everything it should hold, and the revokes it missed
            welcome["standing"] = hub.standing_body(node["org_id"], node["id"])
        await websocket.send_text(wire.Envelope("welcome", welcome).encode())
        if mesh is not None:
            await store.run(store.mesh_freeze_applied, node["org_id"], node["id"], mesh["freeze_id"])
        await hub.register(session)
        log.info("node %s connected", session.name)
        reader = asyncio.create_task(self._read(websocket, session))
        watchdog = asyncio.create_task(self._watchdog(websocket, session))
        try:
            await hub.resend_pending(session)
            await hub.catch_up(session, mesh=mesh, revoked=welcome["revoked_grants"], standing_sent=welcome.get("standing"))
            await asyncio.wait({reader, watchdog}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (reader, watchdog):
                task.cancel()
            await asyncio.gather(reader, watchdog, return_exceptions=True)
            hub.unregister(session)
            log.info("node %s disconnected", session.name)

    async def _read(self, websocket: WebSocket, session: NodeSession) -> None:
        hub = self.controller.hub
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                session.last_rx = time.monotonic()
                data = message.get("bytes")
                if data is not None:
                    await session.streams.on_binary(data)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                try:
                    envelope = wire.decode(text)
                except wire.WireError as exc:
                    await session.send(wire.error_envelope(exc.code, exc.message, exc.re))
                    continue
                try:
                    await hub.handle(session, envelope)
                except (ValueError, TypeError, KeyError) as exc:
                    await session.send(wire.error_envelope("invalid", str(exc)[:300] or "invalid message",
                                                           envelope.id or None))
        except (WebSocketDisconnect, RuntimeError, OSError):
            return

    async def _watchdog(self, websocket: WebSocket, session: NodeSession) -> None:
        while True:
            await asyncio.sleep(min(5.0, self.dead_after / 4))
            if time.monotonic() - session.last_rx > self.dead_after:
                log.warning("node %s sent nothing for %.0f s; closing", session.name, self.dead_after)
                with contextlib.suppress(Exception):
                    await websocket.close(code=1001)
                return
