"""Members: people other than the owner, and the grants they hold.

Part of PolicyService (openajar.policy.service), as a mixin. A member holds one grant per scope,
made by `issue_member_grant` from fixed roles (MEMBER_ROLES, `member_specials`). Their subject is
`kind: human`; they carry `approve` and `control`, are never bearer tokens, are never narrowed, and
die together with `revoke_member` (one row in `revoked` that check_live reads on every call). The
approvals a member may decide are read here too (`person_grant`, `members_who_can_decide`), so the
notifier and the panel ask the same question the decision itself does.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openajar.core import globs
from openajar.core.errors import InvalidRequest
from openajar.core.models import Effect
from openajar.policy.approval_models import Approval, decision_problem
from openajar.policy.errors import Conflict, Denied
from openajar.policy.grants import ApproveCap, ControlCap, Grant, Specials, Subject, coerce_effect

if TYPE_CHECKING:
    from openajar.policy.context import PolicyContext

# member roles; "owner" is the node owner's own grant
ROLES = ("owner", "admin", "approver", "operator", "viewer", "auditor")
MEMBER_ROLES = ROLES[1:]
MEMBER_OPTIONS = {"effects", "max_usd", "watch", "lanes", "max_session_s"}
DECIDABLE_EFFECTS = [e for e in Effect if e != Effect.READ]
DEFAULT_TAKEOVER_S = 3600  # a take over ends by itself after this, unless the role sets another
MEMBER_ID_RE = re.compile(r"^m_[A-Za-z0-9._-]{1,100}$")
# A controller's per-call token for a member lapses seconds after the call's deadline (the hosted controller:
# at most 300 s for a call retried after its approval, plus 10 s); one that lives longer than this
# is a standing credential, which a member never holds as a token.
MEMBER_CALL_MAX_TTL_S = 360


def member_revocation(member_id: str) -> str:
    """The revocation key of every grant of one member (a row in `revoked`, checked on every call)."""
    return f"member:{member_id}"


class MembersMixin:
    """The member grants of PolicyService (the attributes it reads, `db`, `cfg`, `approvals`, `bus`,
    `clock`, are set by PolicyService.__init__)."""

    def issue_member_grant(self, member_id: str, workspace: str, role: str | list[str], options: dict | None = None,
                           exp: float | None = None, *, by: str = "owner", handle: str | None = None,
                           replace: bool = True) -> Grant:
        """The member grant for `member_id` in one scope: `workspace` ("*" for every workspace),
        `role` (one of MEMBER_ROLES, or several held together in that scope) and `options`
        (effects, max_usd, watch, lanes, max_session_s; see member_specials). `exp` is the
        membership's end (Unix seconds; default the longest grant life, `max_grant_ttl_s`).
        With `replace`, the member's earlier grants for the same scope are revoked once this one
        exists (a role change). Returns the Grant; its token is never handed out: a panel session
        keeps the grant id and loads it with grant_by_id (or member_grants)."""
        if not isinstance(member_id, str) or not MEMBER_ID_RE.match(member_id):
            raise InvalidRequest("a member id looks like m_<letters and digits>; \"owner\" is the node owner")
        roles = [role] if isinstance(role, str) else list(dict.fromkeys(role or []))
        if not roles:
            raise InvalidRequest("a member grant needs a role")
        if "owner" in roles:
            raise InvalidRequest("the owner role belongs to the node owner; it is not given to a member")
        workspace = globs.validate_segment(workspace)
        if workspace != "*" and "*" in workspace:
            raise InvalidRequest("a member's scope is one workspace or \"*\" (every workspace)")
        if self.db.one("SELECT 1 FROM revoked WHERE grant_id = ?", (member_revocation(member_id),)) is not None:
            raise Denied("revoked", f"member {member_id} was removed; invite them again as a new member")
        ceiling, specials = member_specials(roles, dict(options or {}), workspace)
        now = self.clock()
        ttl = self.cfg.max_grant_ttl_s if exp is None else int(exp - now)
        if ttl <= 0:
            raise InvalidRequest("the membership has already ended")
        label = (handle or member_id)[:60]
        g, _token = self.issue(subject=Subject(kind="human", id=member_id, owner=member_id), workspaces=[workspace],
                               ceiling=ceiling, specials=specials, ttl_s=min(ttl, self.cfg.max_grant_ttl_s), depth=0,
                               fanout=0, note=f"member {label}: {'+'.join(roles)}@{workspace}", by=by, _member=True)
        if replace:
            for old in self._member_rows(member_id):
                if old["id"] != g.id and json.loads(old["block"]).get("workspaces") == [workspace]:
                    with contextlib.suppress(Conflict):
                        self.revoke(old["id"], by=by, reason="member role changed")
        return g

    def _member_rows(self, member_id: str) -> list[Any]:
        return self.db.all("SELECT id, block FROM grants WHERE parent_id IS NULL "
                           "AND json_extract(subject, '$.kind') = 'human' AND json_extract(subject, '$.id') = ? "
                           "ORDER BY issued_at DESC, id", (member_id,))

    def member_grants(self, member_id: str) -> list[Grant]:
        """The member's live grants, newest first (one per scope after role changes). Empty when
        the member was removed, or every grant expired or was revoked."""
        out = []
        for row in self._member_rows(member_id):
            with contextlib.suppress(Denied):
                out.append(self.grant_by_id(row["id"]))
        return out

    def revoke_member(self, member_id: str, *, by: str = "owner", reason: str | None = None) -> dict:
        """Remove a member: every grant they hold stops at the next call or decision, and no new
        one can be made for this member id (a person invited again gets a new member id).
        Their agents' grants (`sub.owner` = this member) are revoked too (open question 4)."""
        if member_id == "owner" or not MEMBER_ID_RE.match(member_id or ""):
            raise InvalidRequest("the node owner is not a member that can be removed")
        key = member_revocation(member_id)
        with self.db.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO revoked (grant_id, rid, revoked_at, revoked_by, reason) "
                         "VALUES (?, NULL, ?, ?, ?)", (key, self.clock(), by, (reason or "member removed")[:300]))
        revoked = [r["id"] for r in self._member_rows(member_id)]
        agents = [r["id"] for r in self.db.all(
            "SELECT id FROM grants WHERE parent_id IS NULL AND json_extract(subject, '$.kind') != 'human' "
            "AND json_extract(subject, '$.owner') = ?", (member_id,))]
        done = []
        for gid in [*revoked, *agents]:
            with contextlib.suppress(Conflict):
                self.revoke(gid, by=by, reason=reason or "member removed")
                done.append(gid)
        self.bus.publish("grant", {"state": "member_removed", "member_id": member_id, "grants": done})
        return {"member_id": member_id, "removed": True, "grants": revoked, "agent_grants": agents}

    def member_ids(self) -> list[str]:
        """Every member id that holds a grant on this node and was not removed (the owner is not one)."""
        rows = self.db.all("SELECT DISTINCT json_extract(subject, '$.id') AS m FROM grants WHERE parent_id IS NULL "
                           "AND json_extract(subject, '$.kind') = 'human' ORDER BY m")
        removed = {r["grant_id"] for r in self.db.all("SELECT grant_id FROM revoked WHERE grant_id LIKE 'member:%'")}
        return [r["m"] for r in rows if r["m"] and member_revocation(r["m"]) not in removed]

    def members_who_can_decide(self, approval: Approval | str, decision: str = "approve") -> list[str]:
        """The members (not the owner) whose live grants may make `decision` on this approval, with
        this node's four-eyes policy: the notifier's recipients for a new approval (Brief B3),
        with the owner added when this is empty. Routing never widens it."""
        ap = self.approvals.get(approval) if isinstance(approval, str) else approval
        return [m for m in self.member_ids()
                if any(self.approvals.can_decide(ap, g, decision) for g in self.member_grants(m))]

    def person_grant(self, ap: Approval, person: str) -> Grant | None:
        """The grant `person` would approve `ap` with, if any: the owner's, or a member's whose `approve`
        covers it. In person, because the person decides, not a token. Four-eyes holds."""
        grants = [self.owner_grant()] if person == "owner" else self.member_grants(person)
        rules = self.cfg.approvals.four_eyes
        return next((g for g in grants if decision_problem(ap, g, "approve", four_eyes=rules, in_person=True) is None),
                    None)

    def member_context(self, grant: Grant | str, *, tool: str | None = None, lane_id: str | None = None,
                       tags: Iterable[str] = ()) -> PolicyContext:
        """A ToolContext for a member in person (a signed-in panel session) under one of their
        grants: their scope and ceiling, no taint, and `ctx.cosigns(...)` says when their own
        `approve` lets the panel skip the approval routing."""
        g = self.grant_by_id(grant) if isinstance(grant, str) else grant
        if not g.human or g.owner_token:
            raise InvalidRequest("member_context is for a member's grant")
        self.check_live(g)
        return self.context(g, principal=g.principal(), tool=tool, lane_id=lane_id, tags=tags, in_person=True)

    def decide(self, approval_id: str, decision: str, *, decider: Grant | str | None = None,
               member_id: str | None = None, via: str | None = None, by: str | None = None,
               note: str | None = None, answer: str | None = None, in_person: bool = True,
               seen: str | None = None) -> Approval:
        """Approve or deny (or answer) an approval as a person: `decider` (a Grant or a grant id) or
        `member_id` (the member grant that may decide it is chosen). The decider grant is checked
        live (revoked, expired) and for `approve` coverage, the family rule and four-eyes (see
        approvals.decision_problem). `by` is the member's handle for Activity; `via` how they
        proved it (passkey, code). Without either, the owner decides (as the CLI does)."""
        if member_id is not None:
            if decider is not None:
                raise InvalidRequest("name the decider or the member, not both")
            decider = self._member_decider(member_id, approval_id, decision)
        elif isinstance(decider, str):
            decider = self.grant_by_id(decider)
        elif decider is None:
            decider = self.owner_grant()
        return self.approvals.decide(approval_id, decision, decider=decider, via=via, by=by, note=note,
                                     answer=answer, in_person=in_person, seen=seen)

    def _member_decider(self, member_id: str, approval_id: str, decision: str) -> Grant:
        live: list[Grant] = []
        problem: Denied | None = None
        for row in self._member_rows(member_id):
            try:
                live.append(self.grant_by_id(row["id"]))
            except Denied as e:
                problem = problem or e
        if not live:
            if self.db.one("SELECT 1 FROM revoked WHERE grant_id = ?", (member_revocation(member_id),)):
                raise Denied("revoked", f"member {member_id} was removed")
            raise problem or Denied("not_approver", f"member {member_id} holds no grant on this node")
        ap = self.approvals.get(approval_id)
        return next((g for g in live if self.approvals.can_decide(ap, g, decision)), live[0])


def member_specials(roles: list[str], options: dict, workspace: str) -> tuple[Effect, Specials]:
    """The ceiling and specials of a member grant for `roles` in one scope (section 4.3):

    viewer    read; live view only with options.watch (control, watch only)
    auditor   read; nothing else (evidence, phase 3)
    approver  read; approve options.effects, else commit (and pay when options.max_usd is set),
              on options.lanes, pay up to options.max_usd; watch with options.watch
    operator  commit (routed like an agent); control: take over for options.max_session_s
    admin     irreversible; approve every effect but read (pay up to options.max_usd); control
    Several roles in one scope combine (the widest of each)."""
    opts = _member_options(options, workspace)
    ceiling, approve, control = _role_rights(roles, opts)
    order = list(Effect)
    specials = Specials(
        approve=ApproveCap(effects=sorted(approve, key=order.index), lanes=opts.lanes, max_usd=opts.max_usd)
        if approve else None,
        control=ControlCap(lanes=opts.lanes, watch_only=True if all(control) else None,
                           max_session_s=None if all(control) else opts.takeover) if control else None)
    return ceiling, specials


@dataclass(frozen=True)
class _MemberOptions:
    """A member grant's options, checked."""
    lanes: list[str] | None
    max_usd: float | None
    takeover: int
    watch: bool
    effects: list[Effect] | None


def _member_options(options: dict, workspace: str) -> _MemberOptions:
    """The options checked, each refused with what it must be."""
    unknown = set(options) - MEMBER_OPTIONS
    if unknown:
        raise InvalidRequest(f"unknown member options: {', '.join(sorted(unknown))} "
                             f"(known: {', '.join(sorted(MEMBER_OPTIONS))})")
    lanes = options.get("lanes")
    if lanes is not None:
        if not isinstance(lanes, list) or not lanes:
            raise InvalidRequest("lanes is a list of lane patterns inside the member's workspace")
        lanes = [globs.validate_lane_pattern(str(p)) for p in lanes]
        bad = [p for p in lanes if not globs.lane_pattern_within(p, f"{workspace}/*")]
        if bad:
            raise InvalidRequest(f"lane {bad[0]} is not inside {workspace}")
    max_usd = options.get("max_usd")
    if max_usd is not None and (isinstance(max_usd, bool) or not isinstance(max_usd, int | float)
                                or not math.isfinite(max_usd) or max_usd < 0):
        raise InvalidRequest("max_usd is a number of US dollars, 0 or more")
    max_usd = float(max_usd) if max_usd is not None else None
    takeover = options.get("max_session_s", DEFAULT_TAKEOVER_S)
    if isinstance(takeover, bool) or not isinstance(takeover, int) or takeover < 1:
        raise InvalidRequest("max_session_s is a whole number of seconds, 1 or more")
    watch = options.get("watch", False)
    if not isinstance(watch, bool):
        raise InvalidRequest("watch is true or false")
    effects = options.get("effects")
    if effects is not None:
        if not isinstance(effects, list) or not effects:
            raise InvalidRequest("effects is a list of effects this member may decide")
        effects = [coerce_effect(e) for e in effects]

    return _MemberOptions(lanes=lanes, max_usd=max_usd, takeover=takeover, watch=watch, effects=effects)


def _role_rights(roles: list[str], opts: _MemberOptions) -> tuple[Effect, set[Effect], list[bool]]:
    """The ceiling, the effects it may approve, and watch_only for each role that grants control."""
    effects, max_usd, watch = opts.effects, opts.max_usd, opts.watch
    ceiling = Effect.READ
    approve: set[Effect] = set()
    control: list[bool] = []  # watch_only per role that grants control
    for role in roles:
        if role == "approver":
            approve |= set(effects or [Effect.COMMIT, *([Effect.PAY] if max_usd is not None else [])])
            if watch:
                control.append(True)
        elif role == "operator":
            ceiling = max(ceiling, Effect.COMMIT, key=lambda e: e.rank or 0)
            control.append(False)
        elif role == "admin":
            ceiling = Effect.IRREVERSIBLE
            approve |= set(effects or DECIDABLE_EFFECTS)
            control.append(False)
        elif role == "viewer":
            if watch:
                control.append(True)
        elif role != "auditor":
            raise InvalidRequest(f"a member's role is one of {', '.join(MEMBER_ROLES)}, not {role!r}")
    return ceiling, approve, control
