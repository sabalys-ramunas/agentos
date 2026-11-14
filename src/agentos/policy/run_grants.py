"""A person's approval as one run's authority (the agenda's).

Part of PolicyService (agentos.policy.service), as a mixin. The agenda (agentos.agenda) plans an
action weeks ahead. The drafting agent's grant may expire before then, while the person who approved
that exact action still stands behind it. These methods let that approval carry the one run: where
the drafter's grant stands (grant_standing), who decided the approval (approver_of), whether they
still may (approver_problem), and a grant for that run alone (approved_run_grant), which the runner
checks against the run it carries (check_run_binding).
"""

from __future__ import annotations

from typing import Any

from agentos.core.models import Effect
from agentos.policy import freeze as freeze_gate
from agentos.policy import globs
from agentos.policy.errors import Denied
from agentos.policy.grants import (
    OWNER_GRANT_ID,
    SUBJECT_ID_MAX,
    Grant,
    LaneCap,
    Specials,
    Subject,
    coerce_effect,
)
from agentos.policy.util import content_hash

# The grant's pins name no tool, so it opens no MCP tool; the runner checks these two against the run
# it carries (check_run_binding)
RUN_PIN = "run.approved"          # flow, version, target and idempotency key of the one run
RUN_INPUTS_PIN = "run.inputs"     # the inputs it runs with, as the caller passed them
APPROVED_RUN_ROOM_S = 3600        # a run that started inside its window gets this long to finish


def run_pin(flow_id: str | None, version: str | None, target: str | None, key: str | None) -> str:
    """RUN_PIN's value: sha256 over the one run a grant from approved_run_grant may carry."""
    return content_hash({"flow": flow_id, "version": version, "target": target, "key": key})


def inputs_pin(params: dict[str, Any] | None) -> str:
    """RUN_INPUTS_PIN's value: sha256 over the inputs that run was approved with."""
    return content_hash({"params": params or {}})


def _why(e: Denied) -> tuple[str, str]:
    """A denial's reason and words (a freeze's GrantFrozen carries only its message)."""
    return getattr(e, "reason", None) or getattr(e, "code", "denied"), getattr(e, "detail", None) or str(e)


class RunGrantsMixin:
    """The agenda's grant methods of PolicyService (it reads `clock`, `issue`, `check_live`,
    `grant_by_id`, `_stored_chain` and `_revocations`, all set up by PolicyService)."""

    def grant_standing(self, grant_id: str | None) -> dict[str, str]:
        """Where a stored grant stands, revocation first: {"state": live | revoked | expired |
        not_yet_valid | unknown_grant | ..., "reason": words}. A grant that was revoked and has since
        expired is `revoked`: an explicit stop is never read as a lapse (the agenda cancels what a
        revoked grant drafted, and lets a person's approval stand in only for an expired one)."""
        if grant_id == OWNER_GRANT_ID:
            return {"state": "live", "reason": "the owner"}
        try:
            g = self._stored_chain(grant_id)
        except Denied as e:
            state, words = _why(e)
            return {"state": state, "reason": words}
        rows = self._revocations(g, limit=1)
        if rows:
            row = rows[0]
            why = f": {row['reason']}" if row["reason"] else ""
            return {"state": "revoked", "reason": f"grant {grant_id} was revoked by {row['revoked_by']}{why}"}
        try:
            self.check_live(g)
        except Denied as e:
            state, words = _why(e)
            return {"state": state, "reason": words}
        return {"state": "live", "reason": "live"}

    def approver_of(self, decided_grant: str | None) -> dict[str, str] | None:
        """The person behind an approval's decision (Approval.decided_grant), as the agenda records
        it: {"kind": "owner", "id": "owner", "grant_id": "owner"} or {"kind": "member", "id": m_...,
        "grant_id": ...}. None for an admin or any other non-person grant, and for a decision that
        proves no person (a token's): only a person's own decision can stand in for an agent's lapsed
        grant."""
        if decided_grant == OWNER_GRANT_ID:
            return {"kind": "owner", "id": "owner", "grant_id": OWNER_GRANT_ID}
        if not decided_grant:
            return None
        try:
            g = self._stored_chain(decided_grant)
        except Denied:
            return None
        if not g.human or g.owner_token:
            return None
        return {"kind": "member", "id": g.subject.id, "grant_id": g.id}

    def approver_problem(self, approver: dict[str, Any] | None, effect: Effect | str, lane_id: str,
                         workspace: str | None = None) -> str | None:
        """Why this approver may not stand behind `effect` on `lane_id` now, in words, or None. The
        owner always may (the owner credential is the node's own). A member must still hold the very
        grant they decided with, live (not revoked, not expired, not removed as a member), and its
        `approve` must still cover the effect and lane: a role removed or narrowed since means no."""
        if not approver:
            return "nobody approved it in person"
        if approver.get("kind") == "owner":
            return None
        gid = approver.get("grant_id")
        who = approver.get("id") or "the approver"
        try:
            g: Grant = self.grant_by_id(gid)
        except Denied as e:
            reason, words = _why(e)
            return f"{who}'s grant {gid} no longer stands ({reason}: {words})"
        if not g.human or g.subject.id != approver.get("id"):
            return f"grant {gid} is not {who}'s"
        problem = g.approve_problem(coerce_effect(effect).value, lane_id, workspace=workspace)
        return f"{who} may no longer approve this: {problem}" if problem else None

    def approved_run_grant(self, *, approver: dict[str, Any], entry: str, flow_id: str, version: str | None,
                           lane: str, target: str, params: dict[str, Any], key: str, effect: Effect | str,
                           not_before: float | None, not_after: float | None, drafted_under: str | None,
                           by: str = "agenda") -> Grant:
        """A grant for one run a person approved, derived from that person's authority: a root grant
        (issue) whose subject is `agenda:<entry>`, a flow owned by the approver (so removing that
        member revokes it with their other agents' grants), covering only `lane` and `target`, at
        most `effect`, no delegation (depth and fanout 0), from `not_before` until the window closes
        (`not_after`, plus APPROVED_RUN_ROOM_S for a run that started inside it), and pinned to the
        run: RUN_PIN binds flow, version, target and idempotency key, RUN_INPUTS_PIN the inputs. Its
        pins name no tool, so it opens no MCP tool. The `secret` special is kept for these lanes only
        when the grant the action was drafted under had it there. The token is never returned: the
        runner loads the grant by id. Raises PolicyDenied when the approver no longer may (see
        approver_problem) or a lane is frozen. Never wider than the drafter's grant either: its sites
        (`domains`) and egress allowlist carry over, so the run reaches no site the agent could not."""
        problem = self.approver_problem(approver, effect, target, lane.split("/", 1)[0])
        if problem:
            raise Denied("approver", problem)
        lanes = list(dict.fromkeys([lane, target]))
        freeze_gate.check_child_lanes(self, lanes)
        secret, domains, egress = None, None, None
        if drafted_under == OWNER_GRANT_ID:
            secret = LaneCap(lanes=lanes)
        elif drafted_under:
            # fail closed: without the drafter's chain its sites and egress list are unknown, and a
            # grant minted without them would reach sites the agent never could
            try:
                drafter = self._stored_chain(drafted_under)
            except Denied as e:
                raise Denied("approver", "the grant this entry was drafted under is not known on this node, so "
                                         "the sites it may reach are not known; draft and approve it again") from e
            caps = drafter.caps("secret")
            if caps is not None and all(c.lanes is None or all(any(globs.lane_match(p, x) for p in c.lanes)
                                                                for x in lanes) for c in caps):
                secret = LaneCap(lanes=lanes)
            # a narrowed block's lists sit inside its parent's, so the last one set is the narrowest
            domains = next((b.domains for b in reversed(drafter.blocks) if b.domains is not None), None)
            egress = next((b.egress for b in reversed(drafter.blocks) if b.egress is not None), None)
        now = self.clock()
        end = (not_after if not_after is not None else now + 86400) + APPROVED_RUN_ROOM_S
        g, _token = self.issue(
            subject=Subject(kind="flow", id=f"agenda:{entry}"[:SUBJECT_ID_MAX], owner="owner"),
            owner=None if approver.get("kind") == "owner" else approver.get("id"),
            lanes=lanes, domains=domains, egress=egress, ceiling=coerce_effect(effect),
            specials=Specials(secret=secret) if secret else None,
            ttl_s=max(60, int(end - now)), not_before=not_before if not_before and not_before > now else None,
            depth=0, fanout=0,
            pins={RUN_PIN: run_pin(flow_id, version, target, key), RUN_INPUTS_PIN: inputs_pin(params)},
            note=f"agenda entry {entry}: one run, approved by {approver.get('id')}"[:300], by=by)
        return g

    def check_run_binding(self, grant_id: str | None, *, flow_id: str | None, version: str | None,
                          target: str | None, key: str | None, params: dict[str, Any] | None = None) -> None:
        """For a grant pinned to one run (approved_run_grant): raise Denied `run_binding` unless this
        run is that run: the same flow, version, target and idempotency key, and, when `params` is
        given (queueing it), the same inputs. Any other grant passes."""
        if not grant_id or grant_id == OWNER_GRANT_ID:
            return
        try:
            pins = self._stored_chain(grant_id).pins()
        except Denied:
            return  # check_grant refuses it
        bound = [p for p in pins if RUN_PIN in p]
        if not bound:
            return
        if any(p[RUN_PIN] != run_pin(flow_id, version, target, key) for p in bound):
            raise Denied("run_binding", "this grant carries one approved run only (that flow, version, lane and "
                                        "idempotency key); it cannot start another")
        if params is not None and any(p.get(RUN_INPUTS_PIN) != inputs_pin(params) for p in bound):
            raise Denied("run_binding", "this grant carries one approved run with the inputs a person approved; "
                                        "these inputs differ")
