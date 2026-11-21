"""Who is pushed about something:.

Routing is a notification concern and never widens authority: a push only says "something
needs you", and the node checks the decider's member grant when they decide. So the checks
here are the same rules `ApprovalStore.decide` applies (section 4.4, checks 1 to 4), used to
pick recipients, not to allow anything. A wrong answer here asks the wrong person; it cannot
let them decide.

Phase 1 rule: a new approval goes to every member who could decide it and has a channel; the
owner gets it when nobody else can (a solo node: always the owner). Something about a lane (a
hold, a health alert) goes to the members who can take the lane over (`control`), else the
owner. Everything else goes to the owner. Phase 2 replaces this with routing rules.

Member data comes from the members' grants (`sub.kind = "human"`, one grant per scope, with
the `approve` and `control` specials from section 4.4). A MemberDirectory is the seam: the
default one reads live human grants from the policy registry; the panel's members table (B2)
can supply another.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from openajar.core import globs
from openajar.policy.grants import Grant, parse_block
from openajar.policy.store import DB

log = logging.getLogger("openajar.notify")

OWNER = "owner"
HUMAN = "human"


@dataclass(frozen=True)
class ApprovalFacts:
    """What the node computed about an approval (never text the model wrote; section 4.5)."""
    id: str
    kind: str = "action"                 # action | question
    state: str = "pending"
    lane: str | None = None
    effect: str | None = None
    workspace: str | None = None
    amount_usd: float | None = None      # node-parsed, pay only
    not_after: float | None = None       # when the run goes stale
    expires_at: float | None = None
    requester_root: str | None = None    # root grant id of the requesting family
    requester_owner: str | None = None   # sub.owner of the requesting family

    @classmethod
    def from_row(cls, row: Any) -> ApprovalFacts:
        """From an approvals row; the routing columns B1 adds are read when present."""
        d = dict(row)
        lane = d.get("lane")
        try:
            requester = json.loads(d.get("requester") or "{}")
        except (TypeError, ValueError):
            requester = {}
        path = [p for p in (d.get("grant_path") or "").split("/") if p]
        return cls(
            id=d["id"], kind=d.get("kind") or "action", state=d.get("state") or "pending", lane=lane,
            effect=d.get("effect"),
            workspace=d.get("workspace") or (lane.split("/", 1)[0] if lane and "/" in lane else None),
            amount_usd=_num(d.get("amount_usd")), not_after=_num(d.get("not_after")),
            expires_at=_num(d.get("expires_at")),
            requester_root=path[0] if path else d.get("grant_id"),
            requester_owner=d.get("requester_owner") or (requester.get("owner") if isinstance(requester, dict)
                                                          else None))


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _cap(grant: Any, name: str) -> Any:
    """A special from the grant's authority block (human grants are never attenuated)."""
    blocks = getattr(grant, "blocks", None) or []
    specials = getattr(blocks[0], "specials", None) if blocks else None
    return getattr(specials, name, None) if specials is not None else None


def _value(x: Any) -> str:
    return str(getattr(x, "value", x))


def _lanes_ok(cap: Any, lane: str) -> bool:
    lanes = getattr(cap, "lanes", None)
    return lanes is None or any(globs.lane_match(p, lane) for p in lanes)


def is_human(grant: Any) -> bool:
    sub = getattr(grant, "subject", None)
    return getattr(sub, "kind", None) == HUMAN


def could_decide(grant: Any, facts: ApprovalFacts) -> bool:
    """Whether this live member grant could decide the approval (checks 1 to 4 of section 4.4).
    A question (`human_ask`) needs no effect: an approver of the lane may answer it."""
    if not is_human(grant) or not facts.lane or not grant.lane_allowed(facts.lane):
        return False
    cap = _cap(grant, "approve")
    if cap is None or not _lanes_ok(cap, facts.lane):
        return False
    if facts.requester_root and facts.requester_root == getattr(grant, "root_id", None):
        return False  # a grant family never decides its own requests
    if facts.kind == "question":
        return True
    if facts.effect is None or facts.effect not in {_value(e) for e in getattr(cap, "effects", None) or []}:
        return False
    if facts.effect == "pay":
        limit = getattr(cap, "max_usd", None)
        return limit is not None and facts.amount_usd is not None and facts.amount_usd <= limit
    return True


def could_control(grant: Any, lane: str) -> bool:
    """Whether this live member grant may take over the lane (`control`, not watch only)."""
    if not is_human(grant) or not grant.lane_allowed(lane):
        return False
    cap = _cap(grant, "control")
    return cap is not None and not getattr(cap, "watch_only", False) and _lanes_ok(cap, lane)


class MemberDirectory(Protocol):
    def member_ids(self) -> list[str]:
        """Members other than the owner who hold a live grant."""
        ...

    def can_decide(self, member_id: str, facts: ApprovalFacts) -> bool: ...

    def can_control(self, member_id: str, lane: str) -> bool: ...


class NoMembers:
    """A solo node: nobody but the owner."""

    def member_ids(self) -> list[str]:
        return []

    def can_decide(self, member_id: str, facts: ApprovalFacts) -> bool:
        return False

    def can_control(self, member_id: str, lane: str) -> bool:
        return False


class GrantDirectory:
    """Members from the live human grants in the policy registry (policy.db), grouped by
    subject id. Revoked (by id or revocation id), expired and unparsable grants are skipped,
    so `revoke_member` stops a member's pushes at the next event."""

    def __init__(self, db: DB, *, clock: Callable[[], float],
                 decide: Callable[[Any, ApprovalFacts], bool] = could_decide,
                 control: Callable[[Any, str], bool] = could_control):
        self.db = db
        self.clock = clock
        self.decide = decide
        self.control = control

    def grants(self) -> dict[str, list[Grant]]:
        try:
            rows = self.db.all(
                "SELECT id, rid, subject, block FROM grants WHERE depth = 0 AND (expires_at IS NULL OR expires_at > ?) "
                "AND id NOT IN (SELECT grant_id FROM revoked) "
                "AND (rid = '' OR rid NOT IN (SELECT rid FROM revoked WHERE rid IS NOT NULL))", (self.clock(),))
        except Exception:  # noqa: BLE001 - no registry (a bare notifier in tests): no members
            return {}
        out: dict[str, list[Grant]] = {}
        for r in rows:
            try:
                if json.loads(r["subject"]).get("kind") != HUMAN:
                    continue
                g = Grant([parse_block(json.loads(r["block"]), rid=r["rid"] or None)])
            except Exception:  # noqa: BLE001 - one bad row never hides the others
                log.warning("member grant %s could not be read for notifications", r["id"])
                continue
            if g.subject.id != OWNER:
                out.setdefault(g.subject.id, []).append(g)
        return out

    def member_ids(self) -> list[str]:
        return sorted(self.grants())

    def can_decide(self, member_id: str, facts: ApprovalFacts) -> bool:
        return any(self.decide(g, facts) for g in self.grants().get(member_id, []))

    def can_control(self, member_id: str, lane: str) -> bool:
        return any(self.control(g, lane) for g in self.grants().get(member_id, []))
