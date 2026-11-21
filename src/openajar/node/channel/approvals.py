"""Approvals over the channel: a decision from the controller's panel, the node's pending
approvals announced to it, and standing approvals reported back as they are used.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from openajar.core.errors import InvalidRequest, NotFound, OpenajarError, ServiceUnavailable
from openajar.node.channel import bridge, standing, wire
from openajar.node.channel.connection import Connection
from openajar.policy.confirm import UNPROVEN_REACH, ConfirmLevel, Proof, controller_proof
from openajar.policy.errors import Denied
from openajar.policy.scrub import runtime_scrub

log = logging.getLogger("openajar.channel")


class ApprovalsMixin:
    """Approvals: decisions from the controller, pending approvals announced to it, standing approvals reported back."""

    # ---- approvals ----
    async def _on_decision(self, conn: Connection, message: wire.Envelope) -> None:
        body = message.body
        approval_id, decision = body.get("approval_id"), body.get("decision")
        if not isinstance(approval_id, str) or decision not in ("approved", "denied"):
            raise InvalidRequest("approval_decision needs approval_id and decision approved or denied")
        approvals = self.runtime.get("approvals")
        if approvals is None:
            raise ServiceUnavailable("this node has no approvals")

        async def refuse(code: str, text: str, requires: str | None = None, state: str | None = None) -> None:
            await conn.send(wire.error_envelope(code, text, message.id or None, approval_id=approval_id,
                                                requires=requires, state=state))

        try:
            approval = approvals.get(approval_id)
        except NotFound:
            await refuse("not_found", f"no approval {approval_id} on this node")
            return
        if approval.state != "pending":
            await refuse("already_decided", f"approval {approval_id} is already {approval.state}", state=approval.state)
            return
        if approval.kind == "action" and body.get("content_hash") != approval.content_hash:
            log.warning("channel: refused a decision on %s: the content hash does not match", approval_id)
            self._trace("approval", status="denied", tool=approval.tool, lane=approval.lane,
                        summary=f"Refused a decision on {approval_id}: the content hash does not match")
            await refuse("invalid", "the content hash does not match this approval")
            return
        if decision == "approved" and approval.kind == "action":
            # the owner's ceiling: approving lets the action run, so the controller may approve only
            # what it could do itself; the rest is decided on the node. The decision names no grant,
            # so the self-approval rule has no family to apply here.
            try:
                self.ceiling.check_approval(approval.effect)
            except Denied as exc:
                log.warning("channel: refused the controller's approval of %s: %s", approval_id, exc.detail)
                self._trace("approval", status="denied", tool=approval.tool, lane=approval.lane,
                            summary=f"Refused the controller's approval of {approval_id}: above the "
                                    "controller ceiling")
                await refuse(exc.code, exc.message)
                return
        who = body.get("decided_by")
        by = f"controller:{who}" if isinstance(who, str) and 0 < len(who) <= 80 else "controller"
        answer = body.get("answer") if isinstance(body.get("answer"), str) else None
        try:
            # a denial needs no proof, so a bad one never stops it: the node records none
            proof = self._relayed_proof(body.get("proof")) if decision == "approved" else Proof(None, None)
            approvals.decide(approval_id, "approve" if decision == "approved" else "deny", by=by, answer=answer,
                             proof=proof)
        except OpenajarError as exc:
            if exc.code in ("proof_too_weak", "method_unavailable"):
                log.warning("channel: refused the controller's approval of %s: %s", approval_id, exc.code)
                self._trace("approval", status="denied", tool=approval.tool, lane=approval.lane,
                            summary=f"Refused the controller's approval of {approval_id}: {exc.code}")
            if exc.code == "conflict":  # someone at the node got there first: say what they decided
                await refuse("already_decided", exc.message, state=approvals.get(approval_id).state)
                return
            await refuse(exc.code, exc.message, getattr(exc, "requires", None))

    def _relayed_proof(self, claim: Any) -> Proof:
        """What the controller's word counts for, up to the limit the owner set on this node."""
        setting = self.runtime.get("confirmation")
        if setting is None:  # no setting on this node: as today, it proves no person
            return Proof(None, None, time.time(), reach=UNPROVEN_REACH)
        limit = ConfirmLevel.of(setting.current().controller_max_level)
        return controller_proof(claim, setting.methods, limit, at=time.time())

    def _announce_pending_approvals(self) -> None:
        approvals = self.runtime.get("approvals")
        if approvals is None:
            return
        try:
            pending = approvals.list(state="pending", limit=500)
        except Exception:  # noqa: BLE001
            return
        for approval in reversed(pending):
            self._on_approval(approval.public())

    def _on_approval(self, public: dict) -> None:
        if isinstance(public.get("standing"), dict):
            self._on_standing_change(public["standing"], public.get("state"))
            return
        approval_id, state = public.get("id"), public.get("state")
        if not isinstance(approval_id, str) or not isinstance(state, str):
            return
        known = self.store.approval_state(approval_id)
        if known == state:
            return
        if known is None and state == "pending":
            self._enqueue("approval_request", {"approval": bridge.approval_payload(public, runtime_scrub(self.runtime))})
        elif known is not None:
            payload = {"id": approval_id, "state": state}
            if public.get("decided_by"):
                payload["decided_by"] = str(public["decided_by"])[:120]
            for key in ("decided_method", "decided_level"):
                if public.get(key):
                    payload[key] = str(public[key])[:40]
            if public.get("note"):  # the decision's note; the approval's own `reason` is why it was asked
                payload["reason"] = str(public["note"])[:300]
            self._enqueue_event("approval", payload)
        self.store.set_approval_state(approval_id, state)

    def _on_standing_change(self, sa: dict, change: Any) -> None:
        """A use or an end of one this controller sent goes back as `event { kind: "standing" }`, so its
        panel's count is the node's. The node's own standing approvals are not the controller's."""
        if sa.get("issuer") != self.issuer or change not in ("used", "revoked", "withdrawn"):
            return
        payload = standing.event_payload(sa, self.controller_time())
        if payload is not None:
            self._report_standing(payload)

    def _report_standing(self, payload: dict) -> None:
        reported = self.store.get_meta("standing_reported", {}) or {}
        mark = [payload["used"], payload["state"]]
        if reported.get(payload["id"]) == mark:
            return
        reported[payload["id"]] = mark
        self.store.set_meta("standing_reported", reported)
        self._enqueue_event("standing", payload)

    def _confirm_standing(self) -> None:
        """While the controller is connected and has sent its whole set this session, what it holds
        here is confirmed: a revoke would reach the node now. Without that, one it sent stops
        covering calls `controller_standing_confirm_hours` after it was last confirmed."""
        approvals = self.runtime.get("approvals")
        if not (self.connected and self._standing_synced) or not callable(getattr(approvals, "confirm_issued", None)):
            return
        try:
            approvals.confirm_issued(self.issuer)
        except Exception:
            log.debug("channel: could not confirm the standing approvals", exc_info=True)

    def check_standing(self) -> None:
        """What changed outside this process (the owner's `openajar approvals standing revoke`, an
        expiry) is reported too: housekeeping compares what the node holds with what it last said."""
        approvals = self.runtime.get("approvals")
        if approvals is None or not callable(getattr(approvals, "issued_standing", None)):
            return
        try:
            held = approvals.issued_standing(self.issuer)
        except Exception:
            log.debug("channel: could not read the standing approvals", exc_info=True)
            return
        now = self.controller_time()
        reported = self.store.get_meta("standing_reported", {}) or {}
        for sa in held:
            payload = standing.held(sa, now)
            if reported.get(sa.id) is None and payload["state"] == "active" and payload["used"] == 0:
                continue  # just applied: the standing_result said so
            self._report_standing(payload)

    def _on_bus(self, event_type: str, payload: dict) -> None:
        """Policy events, from any thread: traces are batched, approvals forwarded in order."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if event_type == "trace" and isinstance(payload, dict):
            loop.call_soon_threadsafe(self._traces.append, payload)
        elif event_type == "approval" and isinstance(payload, dict):
            loop.call_soon_threadsafe(self._on_approval, payload)
