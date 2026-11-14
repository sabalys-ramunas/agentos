"""PolicyContext: one tool call's view of the policy (its grant, principal and tool), the object a
handler calls to authorize an effect, write a trace, add taint or charge a cost. Decision is what
authorize returns, CallMeta what the MCP request carried. The decision itself is
agentos.policy.authorize.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agentos.core.errors import AgentosError, ApprovalPending, AuditUnwritable, InvalidRequest
from agentos.core.models import Effect, Principal
from agentos.core.presence import Presence
from agentos.policy import freeze as freeze_gate
from agentos.policy import memory_rules
from agentos.policy.approval_models import follows_words
from agentos.policy.budgets import _parse_cost
from agentos.policy.errors import Denied
from agentos.policy.grants import Grant, coerce_effect
from agentos.policy.taint import TAINT_LABELS, family_key
from agentos.policy.traces import Cost, TraceEvent

if TYPE_CHECKING:
    from agentos.policy.service import PolicyService

log = logging.getLogger("agentos.policy")


@dataclass
class Decision:
    """How an authorize() call was resolved."""
    effect: str
    route: str          # auto | standing | approved
    reason: str = ""
    via: str | None = None  # standing approval id or approval id
    pace_ids: list[int] = field(default_factory=list)  # the pace ledger rows this commit holds (a queued run keeps them)


@dataclass
class CallMeta:
    session: str | None = None
    run_id: str | None = None
    parent_id: str | None = None


class PolicyContext:
    """core.tools.ToolContext for one tool call, bound to a principal and a grant."""

    def __init__(self, policy: PolicyService, principal: Principal, grant: Grant, *, tool: str | None = None,
                 tags: Iterable[str] = (), lane_id: str | None = None, meta: CallMeta | None = None,
                 token: str | None = None, in_person: bool = False):
        self.policy = policy
        # the owner in person (a signed-in panel session): no model reads what they see, so their
        # calls neither add taint nor wait on the family's taint
        self.in_person = in_person
        # how sure the node is that a person made this call (agentos.core.presence); None outside MCP
        # and the panel, where `in_person` and the principal say it. wrap.py sets it on every MCP call.
        self.presence: Presence | None = None
        # an MCP call (wrap.py sets it): an agent's, whatever its token, so another agent's environment
        # is out of its reach even with the node's token (agentos.environments)
        self.mcp = False
        self.commit_code: str | None = None  # the code of the last publish charge_commit refused
        self.principal = principal
        self.grant = grant
        self.runtime = policy.runtime
        self.tool = tool
        self.tags = tuple(tags)
        self.lane_id = lane_id
        self.meta = meta or CallMeta()
        self.start_id: str | None = None
        self.cost = Cost()
        self.effect: Effect | None = None
        self.decisions: list[Decision] = []
        self.scrub = [token] if token else []
        self._context_key: str | None = None
        self.follows: list[dict] | None = None  # set by authorize: the untrusted origins the call follows
        # set by the runner for a step of a workflow (agentos.runner.workflows). Both only add a question
        self.routine: str | None = None  # the workflow: its "Ask me first" counts too
        # set by the runner for a run a schedule started (cron or every): nobody re-asked, so a denial
        # of an earlier call does not refuse it without asking (finding 33 of 2026-11-10); it asks again
        self.scheduled: bool = False
        # set by the panel for a member who may not decide in this workspace (agentos.api.access): why a
        # commit or more they start waits for someone who may, even where an agent's would run on its own
        self.ask_person: str | None = None
        self.origins: list[dict] = []  # where the outside data in this call's inputs came from
        # the frozen scopes this call was held off (frozen_scope): a listing that left their rows out
        # says so, and a "not found" or "not covered" because of one says "frozen" (agentos.policy.wrap)
        self.frozen_hits: list[tuple[str, Any]] = []

    @property
    def taint_key(self) -> str:
        """Where this call's taint labels live: the grant family, never the transport session."""
        return family_key(self.grant)

    @property
    def context_key(self) -> str:
        """The taint context the untrusted-content downgrade reads: the grant's turn (a turn token
        and the grants narrowed from it), else its family."""
        if self._context_key is None:
            self._context_key = self.policy.taint.context_of(self.grant)
        return self._context_key

    session_key = taint_key  # the name before SEC-4; kept for callers

    async def authorize(self, effect: Effect, *, lane_id: str | None = None, domain: str | None = None,
                        summary: str | None = None, content: Any = None, cost: Any = None,
                        facts: dict | None = None, at: float | None = None,
                        approval_expires_at: float | None = None) -> Decision:
        """Raise Denied, or ApprovalPending when a human must co-sign. Beyond the core
        protocol: `cost={tokens, usd}` is reserved atomically against the budget, and `facts`
        carries node-observed routing facts (isolation or preset, source, ttl_s, ports,
        provider, tier, credential_owner, metered, tokens). `facts={"sink": True}` says the
        call sends data to `domain` whatever its effect (a navigation carries its URL there),
        so the lethal-trifecta check applies to it. `at`: when a commit will happen, when that is
        later than now (a run queued for a slot): the lane's posting pace is read at that time.
        `approval_expires_at` (Unix seconds): when a request this call opens in Needs you closes,
        instead of the node's approval_ttl_s. Only node code sets it, never model text: the agenda
        keeps the request for an entry weeks ahead open until the entry's start (agentos.agenda).
        It changes nothing else: what the approval binds, who may decide, and a request that was
        already waiting keep their own rules and times."""
        receipt = memory_rules.commit_receipt(self, effect, lane_id)  # memory: decisions_loaded on commits
        self.follows = None
        try:
            # on policy.db's worker thread: a budget debit, a standing use or a new approval that
            # waits for another process's write lock never holds up the event loop. Each is done
            # (committed) before this returns
            d = await self.policy.db.run(self.policy.authorize_call, self, effect, lane_id=lane_id,
                                         domain=domain, summary=summary, content=content, cost=cost,
                                         facts=facts, at=at, approval_expires_at=approval_expires_at)
        except (Denied, ApprovalPending) as e:
            self._trace_decision(effect, lane_id, e, **receipt, **self._taint_fields())
            raise
        self.decisions.append(d)
        eff = coerce_effect(effect)
        self.acted(eff)
        tokens_, usd = _parse_cost(cost)
        self.cost.tokens += tokens_
        self.cost.usd += usd
        if d.route != "auto" or receipt or eff == Effect.COMMIT:  # what ran on its own stays on the record
            self.trace("authorize", status="ok", lane=lane_id, effect=str(getattr(effect, "value", effect)),
                       summary=f"{d.route} via {d.via}" if d.via else d.route, route=d.route, via=d.via,
                       **receipt, **self._taint_fields())  # route and via: the replay reads them (audit)
        return d

    def acted(self, effect: Effect) -> None:
        """This call did something of `effect`: its tool_end row carries the highest one."""
        if self.effect is None or (effect.rank or 0) >= (self.effect.rank or 0):
            self.effect = effect

    def _taint_fields(self) -> dict:
        """For the authorize trace: the call followed untrusted content (kind and host) in this context."""
        if not self.follows:
            return {}
        return {"after_untrusted": follows_words(self.follows),
                "taint_context": "turn" if self.context_key.startswith("turn:") else "family"}

    def _trace_decision(self, effect: Any, lane_id: str | None, err: AgentosError, **extra: Any) -> None:
        status = "pending" if isinstance(err, ApprovalPending) else "denied"
        self.trace("authorize", status=status, lane=lane_id or self.lane_id,
                   effect=str(getattr(effect, "value", effect))[:40], summary=err.message, **extra)

    def precheck(self, effect: Effect, *, lane_id: str | None = None, lane_later: bool = False,
                 facts: dict | None = None) -> None:
        """Scope and eligibility only (no routing, no approval): used before the handler runs. A
        frozen lane (or its workspace, or the node) refuses an agent here too (require_lanes).
        lane_later: the handler picks the lane and checks it there (environment_open), so a special
        held for some lanes only is checked then, not here. `facts`: also check the special's
        parameters against them (environment_open: the VM profile)."""
        freeze_gate.check_call(self.policy, self, lane_id)
        self.policy.check_environment(self, lane_id)
        self.policy.precheck(self.grant, effect, lane_id=lane_id, lane_later=lane_later, facts=facts)

    def frozen_scope(self, *, lane_id: str | None = None, workspace: str | None = None) -> Any:
        """The freeze that holds this agent's call off `lane_id` or `workspace` (agentos.freeze), noted on
        the context, or None: in person, for a tool the freeze exempts, or with no freeze service. The
        one question every scope helper asks (can_access_lane, core.tools.covers_workspace,
        pattern_within, pattern_reaches, frozen_off, refuse_frozen), so a tool that reaches a
        workspace through an id, a definition or a filtered list is held off a frozen one as surely as
        one that names a lane."""
        if getattr(self, "in_person", False) or (lane_id is None and workspace is None):
            return None
        return freeze_gate.frozen_scope(self.policy, self, lane_id=lane_id, workspace=workspace)

    def acts_alone(self, effect: Effect, *, lane_id: str | None = None) -> bool:
        """Would `effect` on `lane_id` run now with no person? A question only: nothing is reserved,
        asked or traced. For a tool that may do a little more when its caller could have done it
        anyway (browser_navigate clicking a link), never a substitute for `authorize`."""
        return self.policy.acts_alone(self, effect, lane_id=lane_id)

    def can_access_lane(self, lane_id: str) -> bool:
        """For tools that list lanes: is this lane inside the caller's grant, and not frozen for it? A
        frozen one is left out (and noted: the answer says so)."""
        return (self.grant.lane_allowed(lane_id) and self.frozen_scope(lane_id=lane_id) is None
                and self.policy.environment_reachable(self, lane_id))

    def cosigns(self, effect: Effect | str, *, lane_id: str | None = None, amount_usd: float | None = None) -> bool:
        """Does the person in this context co-sign `effect` on `lane_id` themselves, so the panel may
        skip the approval routing? The owner in person always; a member in person (member_context)
        only when their `approve` covers it and no four-eyes rule makes them ask someone else.
        Everyone else, and every bearer token, goes through the routing like an agent."""
        g = self.grant
        if g.owner_token:
            return self.in_person
        if not (self.in_person and g.human):
            return False
        eff = coerce_effect(effect)
        if g.approve_problem(eff.value, lane_id, amount_usd=amount_usd) is not None:
            return False
        ws = lane_id.split("/", 1)[0] if lane_id else None
        return not any(r.matches(ws, eff.value) for r in self.policy.cfg.approvals.four_eyes)

    @property
    def admin(self) -> bool:
        """The owner, or an admin grant: may manage grants, approvals, the vault and settings."""
        return self.grant.admin

    def trace(self, kind: str, **fields: Any) -> None:
        """Append a redacted event for this call. Kinds in policy.taint_kinds (web_content,
        private_data, owner_input, ...) and a `taint=[...]` field label the session. A label that
        cannot be recorded refuses the call (AuditUnwritable): the downgrade after untrusted content
        reads those labels, so a call whose read is not on the ledger must not hand its result on.
        A trace row that cannot be written is only logged (audit_health refuses acts while nothing
        can be recorded)."""
        labels = set(self.policy.cfg.taint_kinds.get(kind, []))
        explicit = fields.pop("taint", None)
        if explicit:
            labels |= {explicit} if isinstance(explicit, str) else set(explicit)
        if labels:
            host = fields.get("host") if isinstance(fields.get("host"), str) else None
            try:
                self.taint(*labels, source=f"{self.tool}:{kind}", kind=kind, host=host)
            except AgentosError:
                raise
            except Exception as e:
                log.exception("ctx.trace could not record the taint of a %s", kind)
                raise AuditUnwritable("the node could not record what this call read, so its result is not "
                                      "used; tell the user, and try again once the node's database is "
                                      "writable") from e
        try:
            status = str(fields.pop("status", "ok"))
            lane = fields.pop("lane", None) or self.lane_id
            effect = fields.pop("effect", None)
            summary = fields.pop("summary", None)
            principal_kind = fields.pop("principal_kind", None) or self.principal.kind  # the panel's node token
            self.policy.traces.write(TraceEvent(
                kind=kind, status=status, lane=lane, effect=effect, summary=str(summary) if summary else None,
                args=fields or None, tool=self.tool, principal=self.principal.id,
                principal_kind=principal_kind, grant_id=self.grant.id, grant_path=self.grant.path,
                run_id=self.meta.run_id, parent_id=self.start_id or self.meta.parent_id,
            ), extra_scrub=self.scrub)
        except Exception:
            log.exception("ctx.trace failed")

    def taint(self, *labels: str, source: str | None = None, kind: str | None = None,
              host: str | None = None) -> None:
        """Label this session's data origin: "untrusted", "private" or "owner". For untrusted
        content, `kind` (web_page, email, webhook, memory_note, ...) and `host` say where it came
        from; an approval that follows it names them (never the text)."""
        bad = set(labels) - TAINT_LABELS
        if bad:
            raise InvalidRequest(f"unknown taint labels: {sorted(bad)}")
        if self.in_person:
            return
        self.policy.taint.add(self.taint_key, labels, source or self.tool, context=self.context_key,
                              kind=kind, host=host)

    def charge_commit(self, lane_id: str, *, sent: bool) -> str | None:
        """A publish this call's act set off on `lane_id` (PolicyService.charge_commit): None when it
        is within the limits, else the sentence that says which limit it passes."""
        e = self.policy.charge_commit(self.grant, lane_id, sent=sent)
        self.commit_code = e.code if e is not None else None  # lane_paced or limit_reached (ActMeter)
        return e.hit["explain"] if e is not None else None

    def commits_capped(self, lane_id: str) -> bool:
        """Is a commit on `lane_id` past a limit now (PolicyService.commits_open)?"""
        return self.policy.commits_open(self.grant, lane_id) is not None

    async def charge(self, *, tokens: int = 0, usd: float = 0.0, brain: bool = False,
                     strict: bool = False) -> dict:
        """Record spend after the fact. strict=True refuses (nothing debited) past the budget."""
        res = await self.policy.db.run(self.policy.reserve, self.grant, tokens=tokens, usd=usd, brain=brain,
                                       strict=strict, lane_id=self.lane_id)  # the debit, committed off the loop
        if res.entries:
            self.cost.tokens += int(tokens)
            self.cost.usd += float(usd)
            self.policy.meter_notices(self.grant, self.lane_id)
        return self.policy.remaining(self.grant)
