"""Controller-signed messages (wire.SIGNED_TYPES): each must carry a valid signature by the
controller key pinned at enrollment, a fresh `ts` and a nonce not seen before, and then changes
what its kind says (revocations, config, lane control, the mesh freeze, standing approvals, Ask
me first).
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from agentos.core.errors import InvalidRequest, PolicyDenied, ServiceUnavailable
from agentos.core.signing import verify_controller
from agentos.node.channel import ask_first, node_streams, standing, wire
from agentos.node.channel.connection import Connection

log = logging.getLogger("agentos.channel")


CONFIG_KEYS = {"heartbeat_seconds": (5, 60)}  # what a controller may tune; anything else is kept, not applied
SIGNED_FRESHNESS = 300.0   # a signed message dated (`ts`) further than this from the controller's now is stale
SIGNED_NONCES = "signed_nonces"  # store meta: `kind:nonce` -> when it was seen (wire.NONCE_KEEP_S)
OWN_NONCES = frozenset({"standing", "ask_first", "workflow"})  # these keep their nonces with their bodies
IDEMPOTENT_SIGNED = frozenset({"revoke"})  # safe to apply twice: an undated one is still applied


class SignedMixin:
    """Controller-signed messages: the signature, freshness and nonce check, and what each kind changes."""

    # ---- controller-signed messages ----
    def _signed(self, kind: str, body: wire.Json) -> None:
        """Every controller-signed message (wire.SIGNED_TYPES): the signature, then a `ts` within
        SIGNED_FRESHNESS of the controller's now, then a `nonce` not seen in the last NONCE_KEEP_S.
        The nonce is kept from here on, so a copy replayed later is refused whatever it carries
        (OWN_NONCES keep theirs in their own checks: standing.py, ask_first.py, workflows.py)."""
        if kind not in wire.SIGNED_TYPES:
            raise InvalidRequest(f"{kind} is not a controller-signed message")
        if not verify_controller(self.config.controller_pubkey, kind, self.node_id, body):
            log.warning("channel: refused a %s with a bad signature", kind)
            raise PolicyDenied("the signature does not match the controller key pinned at enrollment")
        if kind in IDEMPOTENT_SIGNED and "ts" not in body and "nonce" not in body:
            return  # a replayed revoke only revokes again; a sender that does not date it must still reach us
        dated = wire.parse_time(body.get("ts"))
        if dated is None:
            raise InvalidRequest(f"{kind} needs ts (ISO 8601 UTC)")
        if abs(self.controller_time() - dated) > SIGNED_FRESHNESS:
            log.warning("channel: refused a %s dated %s: too far from the controller's now", kind,
                        str(body.get("ts"))[:40])
            raise PolicyDenied(f"this {kind} is stale (dated more than {int(SIGNED_FRESHNESS)} s from now)")
        nonce = body.get("nonce")
        if not isinstance(nonce, str) or not wire.NONCE_RE.fullmatch(nonce):
            raise InvalidRequest(f"{kind} needs a nonce (8 to 64 letters, digits, _ -)")
        if kind in OWN_NONCES:  # checked and kept with the body, only once it is applied (standing.py and co.)
            return
        now = time.time()
        seen = {k: v for k, v in (self.store.get_meta(SIGNED_NONCES, {}) or {}).items()
                if isinstance(v, int | float) and v > now - wire.NONCE_KEEP_S}
        key = f"{kind}:{nonce}"
        if key in seen:
            log.warning("channel: refused a replayed %s", kind)
            raise PolicyDenied(f"this {kind} message was already applied (its nonce was seen before)")
        seen[key] = now
        self.store.set_meta(SIGNED_NONCES, seen)

    async def _on_revoke(self, conn: Connection, message: wire.Envelope) -> None:
        self._signed("revoke", message.body)
        ids = message.body.get("revocation_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise InvalidRequest("revoke needs a list of revocation_ids")
        self._apply_revocations(ids)  # in effect at once
        if self.issuers is not None:  # and on disk before the controller is answered (raises if refused)
            await self.issuers.run(self.issuers.save_revoked, ids)

    def _apply_revocations(self, ids: list[str]) -> None:
        if self.issuers is not None:
            self.issuers.revoke(ids)
        gone = set(ids)
        for call in list(self.calls.values()):
            if call.chain & gone and call.task is not None and not call.task.done():
                call.cancel_reason = "the grant was revoked"
                call.task.cancel()
        for call_id, call in list(self.resumable.items()):
            if call.chain & gone:
                self.resumable.pop(call_id, None)

    async def _on_config_update(self, conn: Connection, message: wire.Envelope) -> None:
        self._signed("config_update", message.body)
        config = message.body.get("config")
        if not isinstance(config, dict):
            raise InvalidRequest("config_update needs a config object")
        config = dict(config)
        sent = "confirmation" in config  # null drops the layer; leaving the key out keeps it
        confirmation = config.pop("confirmation", None)  # signed here only: never from `welcome`, never kept in meta
        self._set_config(config, replace=False)
        if sent:
            self._set_confirmation(confirmation)

    def _set_confirmation(self, layer: Any) -> None:
        """A controller's layer (docs/CONFIRMATIONS.md, The node channel): apart from the node's own
        setting, so it can only ask for more. What cannot be read is left out, and logged."""
        setting = self.runtime.get("confirmation")
        if setting is None:
            log.warning("channel: this node has no confirmation setting; ignored the controller's")
            return
        result = setting.apply_controller(layer, by=f"controller:{self.issuer}"[:120])
        if result["refused"]:
            fields = ", ".join(r["field"] for r in result["refused"])
            log.warning("channel: could not read part of the controller's confirmation setting: %s", fields)
            self._trace("config", status="denied", summary=f"Could not read the controller's confirmation setting for {fields}")

    def _set_config(self, config: dict, *, replace: bool) -> None:
        """Apply the controller's config. A value out of its range is not applied: the value in
        force stays (docs/PROTOCOL.md, config_update)."""
        def valid(key: str, value: Any) -> bool:
            low, high = CONFIG_KEYS[key]
            return isinstance(value, int | float) and not isinstance(value, bool) and low <= value <= high

        merged = dict(config) if replace else {**self.controller_config, **config}
        for key in CONFIG_KEYS:
            value = merged.get(key)
            if value is not None and not valid(key, value):
                log.warning("channel: ignored %s=%r from the controller (out of range)", key, str(value)[:40])
                before = self.controller_config.get(key)
                if before is not None and valid(key, before):
                    merged[key] = before
                else:
                    merged.pop(key)
        self.controller_config = merged
        self.store.set_meta("controller_config", merged)
        self._beat_wake.set()

    async def _on_lane_control(self, conn: Connection, message: wire.Envelope) -> None:
        """A human at the controller takes or hands back a lane (v1 clarification, signed)."""
        self._signed("lane_control", message.body)
        lanes = self.runtime.get("lanes")
        lane, action = message.body.get("lane"), message.body.get("action")
        if action == "take":
            self.ceiling.check_take()
        by = node_streams.holder_name(message.body.get("by"))
        if self._terminal_lane(lane):
            if action == "take":
                self.terminal_holds.take(lane, by)
            elif action == "hand_back":
                self.terminal_holds.hand_back(lane)
            else:
                raise InvalidRequest("a terminal lane takes take and hand_back only")
            self._lane_wake.set()
            return
        if lanes is None or not callable(getattr(lanes, "take_control", None)):
            raise ServiceUnavailable("this node has no lane control")
        from agentos.node.viewer_actions import apply_action

        await apply_action(lanes, str(lane), action, by=by)
        self._lane_wake.set()

    def _terminal_lane(self, lane: Any) -> bool:
        """An env lane (agentos.env) the browser lane manager does not have."""
        envs = self.runtime.get("envs")
        if not isinstance(lane, str) or envs is None or not callable(getattr(envs, "has", None)):
            return False
        lanes = self.runtime.get("lanes")
        with contextlib.suppress(Exception):
            if lanes is not None:
                lanes.get(lane)
                return False
        try:
            return bool(envs.has(lane))
        except Exception:  # noqa: BLE001
            return False

    async def _on_mesh_freeze(self, conn: Connection, message: wire.Envelope) -> None:
        """The controller freezes (or lifts its freeze on) every node of the mesh (signed, additive). As with
        lane_control, the node answers only a refusal (`error`)."""
        self._apply_mesh_freeze(message.body)
        self._lane_wake.set()

    def _apply_mesh_freeze(self, body: wire.Json) -> str:
        self._signed("mesh_freeze", body)
        freeze = self.runtime.get("freeze")
        if freeze is None or not callable(getattr(freeze, "apply_mesh", None)):
            if body.get("frozen") is False:
                return "not_frozen"  # nothing to lift on a node without the freeze service
            raise ServiceUnavailable("this node has no freeze service")
        state = freeze.apply_mesh(body)
        if state == "frozen":  # calls the controller sent before the freeze stop with it
            for call in list(self.calls.values()):
                if call.task is not None and not call.task.done():
                    call.cancel_reason = "the mesh is frozen"
                    call.task.cancel()
        if state == "frozen":
            log.warning("channel: mesh freeze from the controller: %s", state)
        else:  # not frozen, or lifted: the normal state, not a warning
            log.info("channel: mesh freeze from the controller: %s", state)
        return state

    async def _on_standing(self, conn: Connection, message: wire.Envelope) -> None:
        """Standing approvals from the controller (signed, additive): answered with `standing_result
        { applied, refused, revoked, withdrawn }`, or `error` when the whole body is refused."""
        result = self._apply_standing(message.body)
        if message.body.get("set") is not None:
            self._standing_synced = True
        await conn.send(wire.Envelope("standing_result", result, re=message.id or None))

    def _apply_standing(self, body: wire.Json) -> dict:
        self._signed("standing", body)
        approvals = self.runtime.get("approvals")
        if approvals is None or not callable(getattr(approvals, "put_issued_standing", None)):
            raise ServiceUnavailable("this node has no approvals")
        sync = standing.Standing(approvals, issuer=self.issuer, ceiling=self.ceiling,
                                 get_meta=self.store.get_meta, set_meta=self.store.set_meta,
                                 freeze=self.runtime.get("freeze"), limits=standing.Limits.from_config(self.config))
        sync.check_body(body)
        result = sync.apply(body)
        if result["refused"]:
            log.warning("channel: refused %d standing approval(s) from the controller: %s", len(result["refused"]),
                        ", ".join(f"{r['id']} ({r['code']})" for r in result["refused"][:5]))
        return result

    async def _on_ask_first(self, conn: Connection, message: wire.Envelope) -> None:
        """A person in the controller's panel turned "Ask me first" on or off (signed, additive):
        answered with `ask_first_result { applied, refused }`, or `error` when the body is refused."""
        if self._ask_first() is None:  # as a node that never heard of it answers
            await conn.send(wire.error_envelope("unsupported", "this node has no Ask me first switches",
                                                message.id or None))
            return
        result = self._apply_ask_first(message.body)
        await conn.send(wire.Envelope("ask_first_result", result, re=message.id or None))

    def _ask_first(self) -> Any:
        store = self.runtime.get("ask_first")
        return store if callable(getattr(store, "set", None)) else None

    def _apply_ask_first(self, body: wire.Json) -> dict:
        self._signed("ask_first", body)
        sync = ask_first.AskFirstSync(self._ask_first(), ceiling=self.ceiling, get_meta=self.store.get_meta,
                                      set_meta=self.store.set_meta)
        result = sync.apply(sync.check_body(body))
        if result["refused"]:
            log.warning("channel: refused %d Ask me first change(s) from the controller: %s", len(result["refused"]),
                        ", ".join(f"{r['scope']} {r['id']} ({r['code']})" for r in result["refused"][:5]))
        return result
