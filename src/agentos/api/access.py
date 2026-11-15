"""What one panel request may do: an Access, built when the request is authenticated.

Without a policy service (M1 nodes, the demo and tests with fakes) only the owner can sign
in, so every request gets `Access`: the owner, every lane, nothing traced.

With the policy service every request gets a `PolicyAccess` around the policy's context for
the caller's grant:
- owner-only routes (settings, grants, the vault, nodes, brains, schedules, creating lanes)
  need an admin grant: the owner token, a signed-in panel session, or `grants issue --admin`;
- lane, approval, activity and run routes are limited to what the grant covers (its lanes,
  and the approvals and traces of its own grant family);
- every action above read goes through the policy and is traced (kind "panel", tool
  "panel.<action>"). A signed-in human is the co-signer, so a session gets the policy's scope,
  ceiling and special checks without the approval routing; a bearer token (an agent, an app)
  gets the full routing, approvals included, exactly as an MCP call would. The reads an open page polls
  (ROUTINE_READS) are logged once per thing, then again only after ROUTINE_READ_S: polling is not news.

A member's session (agentos.api.members) gets a `MemberAccess`: what their roles allow per
workspace (`can(capability, workspace)`, `require(...)`), on lanes their member grants cover,
with Activity, approvals and events filtered by workspace instead of grant family. They co-sign
their own action only where their grant's `approve` covers it (`PolicyContext.cosigns`);
otherwise it is routed exactly like an agent's.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any

from agentos.api.errors import ApiError
from agentos.api.members import Member, Role, capabilities
from agentos.core.errors import AgentosError, ApprovalPending
from agentos.core.models import Effect, Principal
from agentos.core.presence import NEEDS_A_PERSON, Presence
from agentos.core.tools import covers_workspace

log = logging.getLogger("agentos.api")

OWNER_ONLY = ("This needs the owner: sign in to the panel with `agentos panel login`, or use the owner token "
              "or an admin grant.")
# what a member reads: a person is never told about the command line, tokens or grants
OWNER_ONLY_MEMBER = "Only the owner of this computer can do this."
NOT_YOUR_ROLE = "Your role does not allow this here. Ask the owner or an admin of this workspace."
__all__ = ["NEEDS_A_PERSON", "Access", "MemberAccess", "PolicyAccess", "Presence"]

# A person's open page polls what it shows (an environment, a job's output) every few seconds. Their reads
# are logged once per page and thing, then again only after this long, so the action log keeps real actions
# instead of 190 rows of "the owner looked" in nine minutes (2026-11-15 test, P2-11). Denials, errors and
# everything above read are always logged.
ROUTINE_READ_S = 600.0
# The reads an open page polls. Only these are folded: a lane open is authorized as a read too, and is a real
# action every time.
ROUTINE_READS = frozenset({"environment.read", "job.read"})
_READS_SEEN: OrderedDict[tuple[Any, ...], float] = OrderedDict()
_READS_KEPT = 4096


def _effect(effect: Effect | str | None) -> str | None:
    if effect is None:
        return None
    return str(getattr(effect, "value", effect))


class Access:
    """The owner with no policy service: every lane, every route."""

    owner = True
    grant_id: str | None = None
    viewer: str | None = None  # a grant id: approvals and activity limited to its family; None sees all

    def __init__(self, *, presence: Presence = Presence.AGENT):
        # the least sure level unless the caller says otherwise: a request nobody vouched for is an agent's
        self.presence = presence

    @property
    def human(self) -> bool:
        """A person in a session (PERSON or more): the co-signer of what they do, so their actions are
        prechecked rather than routed. The device credential and every token are not."""
        return self.presence >= Presence.PERSON

    def require_presence(self, need: Presence, action: str, *, lane: str | None = None,
                         step_up: bool = False) -> None:
        """Refuse (403, traced) unless this request is at least `need`. `step_up`: a member also needs a
        passkey check inside the step-up window (VERIFIED), as for any of their decisions."""
        if self.presence < need:
            self.trace(action, status="denied", lane=lane, summary=f"needs {need.word}, not {self.presence.word}")
            raise ApiError("presence_required", NEEDS_A_PERSON)
        if step_up and self.member is not None and self.presence < Presence.VERIFIED:
            self.trace(action, status="denied", lane=lane, summary="the last passkey check is too old")
            raise ApiError("step_up_required", "Confirm with your passkey first.")

    def lane_allowed(self, lane_id: str) -> bool:
        return True

    def workspace_allowed(self, workspace: str) -> bool:
        """Does the caller reach all of `workspace` (what belongs to it rather than to one lane: its
        workflows, lists, suppressions)? A member: their role's grant for it, as for its lanes."""
        return bool(workspace) and self.lane_allowed(f"{workspace}/0")

    member: Member | None = None  # set for a member's session (MemberAccess)

    def require_owner(self, action: str) -> None:
        if not self.owner:
            self.trace(action, status="denied", summary="owner only")
            raise ApiError("denied", OWNER_ONLY_MEMBER if self.member is not None else OWNER_ONLY)

    def can(self, capability: str, workspace: str | None = None) -> bool:
        """May this caller do `capability` (agentos.api.members.CAPABILITIES) in `workspace` (None: in
        some workspace)? The owner may do everything; a grant only what its own routes allow."""
        return self.owner

    def require(self, capability: str, workspace: str | None = None, *, action: str,
                lane: str | None = None) -> None:
        """A member: refuse (403, traced) unless their role has `capability` in `workspace` (None: in
        some workspace). Everyone else: nothing here (the route's own rules apply to them)."""
        if self.member is not None and not self.can(capability, workspace):
            self.trace(action, status="denied", lane=lane, summary=f"role: no {capability} in {workspace or 'any'}")
            raise ApiError("denied", NOT_YOUR_ROLE)

    def require_role(self, capability: str, *, action: str) -> None:
        """A route for the owner and for members whose role has `capability` somewhere; a grant is
        refused as on an owner-only route."""
        if self.member is None:
            self.require_owner(action)
        else:
            self.require(capability, action=action)

    def schedule_visible(self, schedule: dict) -> bool:
        """The owner sees every schedule; a grant its own (the grant_id they were created under)."""
        return self.viewer is None or schedule.get("grant_id") == self.viewer

    def principal_for(self, principal: Principal, lane_id: str | None = None) -> Principal:
        """The principal an action on `lane_id` runs as (a member acts under the grant for that lane)."""
        return principal

    async def authorize(self, effect: Effect | str, *, action: str, lane_id: str | None = None,
                        summary: str | None = None, content: Any = None, facts: dict | None = None,
                        ask_person: str | None = None) -> Any:
        """Raise when the caller may not do this; trace it either way. Returns the policy's Decision
        when the policy authorized (and metered) it, else None (the owner, a person's precheck).
        `ask_person` is only for a member (MemberAccess.authorize): why a commit waits for a yes."""
        log.debug("panel %s: %s on %s allowed (owner, no policy)", action, _effect(effect), lane_id or "-")

    def trace(self, action: str, *, status: str = "ok", lane: str | None = None,
              effect: Effect | str | None = None, summary: str | None = None, **fields: Any) -> None:
        return None

    # An allowed action is traced when its route ends, with how it ended: `ok`, or `error` when the
    # action failed after it was allowed. A route the panel's handler does not wrap (a websocket)
    # never defers, so its trace is written at once as before.
    defer_done: bool = False

    def _read_again(self, action: str, fields: dict[str, Any]) -> bool:
        """A person's read of the same thing within ROUTINE_READ_S of the last one logged: a poll, not news."""
        if not self.human or action not in ROUTINE_READS or _effect(fields.get("effect")) != "read":
            return False
        member = getattr(self.member, "id", None)
        key = (self.grant_id, member, self.viewer, action, fields.get("lane"))
        now = time.monotonic()
        last = _READS_SEEN.get(key)
        if last is not None and now - last < ROUTINE_READ_S:
            return True
        _READS_SEEN[key] = now
        _READS_SEEN.move_to_end(key)
        while len(_READS_SEEN) > _READS_KEPT:
            _READS_SEEN.popitem(last=False)
        return False

    def done(self, action: str, **fields: Any) -> None:
        """Trace an allowed action: now, or when the route ends (`settle`) while deferring."""
        if not self.defer_done:
            if not self._read_again(action, fields):
                self.trace(action, **fields)
            return
        if not hasattr(self, "_pending_done"):
            self._pending_done: list[tuple[str, dict[str, Any]]] = []
        self._pending_done.append((action, fields))

    def settle(self, error: BaseException | None = None) -> None:
        """Write the deferred traces: `ok`, or `error` with the failure's words."""
        pending, self._pending_done = getattr(self, "_pending_done", []), []
        self.defer_done = False
        for action, fields in pending:
            if error is None or isinstance(error, ApprovalPending):  # it waits for a person: traced there
                if error is None and self._read_again(action, fields):
                    continue
                self.trace(action, **fields)
            else:
                words = getattr(error, "message", None) or str(error) or type(error).__name__
                self.trace(action, **{**fields, "status": "error", "summary": str(words)[:300]})

    def filter_event(self, kind: str, data: Any) -> Any:
        """The event as this caller may see it on GET /api/events, or None to drop it."""
        return data


class PolicyAccess(Access):
    """A caller under a policy grant (agentos.policy.PolicyContext, used by duck typing)."""

    def __init__(self, ctx: Any, *, presence: Presence = Presence.AGENT, bearer: bool = False):
        super().__init__(presence=presence)
        self.ctx = ctx
        self.bearer = bearer  # a bearer token, not a panel session: a person this way is the node's CLI
        # the context follows the request's level: below a person (the device credential) it is never in
        # person, so it gets the taint, pace and memory rules an agent gets (core.tools.in_person)
        if hasattr(ctx, "in_person") and presence < Presence.PERSON:
            ctx.in_person = False
        if hasattr(ctx, "presence"):
            ctx.presence = presence
        self.owner = bool(getattr(ctx, "admin", False))
        grant = getattr(ctx, "grant", None)
        self.grant_id = getattr(grant, "id", None)
        self.viewer = None if self.owner else self.grant_id

    def lane_allowed(self, lane_id: str) -> bool:
        try:
            return bool(self.ctx.can_access_lane(lane_id))
        except Exception:  # noqa: BLE001  (a lane id the policy cannot parse is outside every scope)
            return False

    def workspace_allowed(self, workspace: str) -> bool:
        """A grant reaches a workspace only when its lane scope covers every lane of it, never by one lane."""
        return covers_workspace(self.ctx, workspace)

    async def authorize(self, effect: Effect | str, *, action: str, lane_id: str | None = None,
                        summary: str | None = None, content: Any = None, facts: dict | None = None) -> Any:
        self.ctx.tool = f"panel.{action}"
        if self.human:
            try:
                self.ctx.precheck(effect, lane_id=lane_id)
            except AgentosError as exc:
                self.trace(action, status="denied", lane=lane_id, effect=effect, summary=exc.message)
                raise
            decision = None
        else:
            # scope, ceiling, budget, routing and approvals; denials and pending approvals are traced there
            decision = await self.ctx.authorize(effect, lane_id=lane_id, summary=summary, content=content, facts=facts)
        self.done(action, lane=lane_id, effect=effect, summary=summary)
        return decision

    def trace(self, action: str, *, status: str = "ok", lane: str | None = None,
              effect: Effect | str | None = None, summary: str | None = None, **fields: Any) -> None:
        try:
            self.ctx.tool = f"panel.{action}"
            self.ctx.trace("panel", status=status, lane=lane, effect=_effect(effect), summary=summary,
                           via=self.trace_via, **({"principal_kind": kind} if (kind := self.principal_kind) else {}),
                           **fields)
        except Exception:  # noqa: BLE001  (tracing never fails a request)
            log.warning("panel trace failed for %s", action)

    @property
    def trace_via(self) -> str:
        """How the request arrived, for its traces: a panel session, the node's CLI with its one-time code,
        the owner's device credential, the node's token, or a grant's token (QA-2 #8)."""
        if self.human:
            return "cli" if self.bearer else "session"
        if self.presence == Presence.DEVICE:
            return "device"
        return "node_token" if self.presence == Presence.NODE_TOKEN else "token"

    @property
    def principal_kind(self) -> str | None:
        """What its traces call the caller when it is not the context's own principal: the node's token
        and the device credential are no person, so never a human."""
        return {Presence.NODE_TOKEN: "node", Presence.DEVICE: "device"}.get(Presence(self.presence))

    def filter_event(self, kind: str, data: Any) -> Any:
        if self.owner:
            return data
        if self.member is not None:
            return self._member_event(kind, data)
        if kind in ("lane", "run"):
            lane = data.get("id" if kind == "lane" else "lane") if isinstance(data, dict) else None
            return data if isinstance(lane, str) and self.lane_allowed(lane) else None
        if kind in ("approval", "trace", "question", "meter"):
            return {}  # "something changed": the panel refetches through the filtered routes
        if kind == "login":  # an assisted login is the owner's: a grant never follows one
            return None
        if kind == "schedule":  # a grant's own schedules; a deleted one keeps its grant_id, so it is still its own
            return data if isinstance(data, dict) and data.get("grant_id") == self.viewer else None
        if kind == "resync":
            return data
        return None


class MemberAccess(PolicyAccess):
    """A member in person: their roles per workspace, under their member grants (one per scope)."""

    def __init__(self, policy: Any, member: Member, roles: list[Role], grants: dict[str, Any], *, via: str,
                 verified_recently: bool = False):
        primary = grants.get("*") or grants[min(grants)]
        super().__init__(policy.member_context(primary, tool="panel"),
                         presence=Presence.VERIFIED if verified_recently else Presence.PERSON)
        self.policy = policy
        self.member = member
        self.via = via
        self.verified_recently = verified_recently  # a passkey check inside the step-up window
        self.owner = False
        self.grants = grants                    # scope -> live member grant
        self.roles: dict[str, set[str]] = {}    # scope -> roles, only where a live grant backs them
        for r in roles:
            if r.scope in grants:
                self.roles.setdefault(r.scope, set()).add(r.role)
        self.grant_id = primary.id
        self.viewer = f"member:{member.id}"     # matches no grant family: services that filter by family show nothing
        self._contexts: dict[str, Any] = {(grants.get("*") and "*") or min(grants): self.ctx}

    # ---- roles ----
    def _scopes_for(self, workspace: str | None) -> list[str]:
        if workspace is None:
            return list(self.roles)
        return [s for s in (workspace, "*") if s in self.roles]

    def can(self, capability: str, workspace: str | None = None) -> bool:
        return any(capability in capabilities(self.roles[s]) for s in self._scopes_for(workspace))

    def scopes_with(self, capability: str) -> list[str]:
        return [s for s, roles in self.roles.items() if capability in capabilities(roles)]

    def workspaces_with(self, capability: str) -> set[str] | None:
        """The workspaces where the member may do `capability`; None means every workspace."""
        scopes = self.scopes_with(capability)
        return None if "*" in scopes else set(scopes)

    def grant_for(self, workspace: str | None, capability: str | None = None) -> Any | None:
        for s in self._scopes_for(workspace):
            if capability is None or capability in capabilities(self.roles[s]):
                return self.grants[s]
        return None

    def grants_with(self, capability: str) -> list[Any]:
        return [self.grants[s] for s in self.scopes_with(capability)]

    def _ctx(self, grant: Any) -> Any:
        scope = (grant.blocks[0].workspaces or ["*"])[0]
        ctx = self._contexts.get(scope)
        if ctx is None:
            ctx = self._contexts[scope] = self.policy.member_context(grant, tool="panel")
        return ctx

    # ---- lanes ----
    def lane_allowed(self, lane_id: str) -> bool:
        ws = lane_id.split("/", 1)[0] if isinstance(lane_id, str) else None
        g = self.grant_for(ws, "see") if ws else None
        try:
            return g is not None and bool(g.lane_allowed(lane_id))
        except Exception:  # noqa: BLE001  (a lane id the policy cannot parse is outside every scope)
            return False

    def watch_terms(self, lane_id: str) -> dict | None:
        """{"watch_only", "max_session_s"} when a grant's `control` covers this lane, else None."""
        for g in self.grants.values():
            try:
                terms = g.control_terms(lane_id)
            except Exception:  # noqa: BLE001
                terms = None
            if terms is not None:
                return terms
        return None

    def principal_for(self, principal: Principal, lane_id: str | None = None) -> Principal:
        g = self.grant_for(lane_id.split("/", 1)[0] if lane_id else None) or self.grants[min(self.grants)]
        return Principal(kind="human", id=self.member.id, owner=self.member.id, grant_id=g.id)  # type: ignore[union-attr]

    # ---- actions ----
    async def authorize(self, effect: Effect | str, *, action: str, lane_id: str | None = None,
                        summary: str | None = None, content: Any = None, facts: dict | None = None,
                        ask_person: str | None = None) -> Any:
        """Their own approve co-signs it (scope and ceiling checked, no routing) when they proved
        themselves recently and it is below pay and irreversible; otherwise it is routed like an
        agent's call, approvals included (ApprovalPending), and deciding that approval asks for the
        passkey check (agentos.api.app.decision_proof). `ask_person` (why it waits): a commit they
        start never runs on its own, and nothing co-signs it, when they may not decide here."""
        ws = lane_id.split("/", 1)[0] if lane_id else None
        covering = [self.grants[s] for s in self._scopes_for(ws)]
        if not covering:
            self.trace(action, status="denied", lane=lane_id, effect=effect, summary="out of scope")
            raise ApiError("denied", "This lane is outside your workspaces.")
        amount = (facts or {}).get("amount_usd") if isinstance(facts, dict) else None
        may_cosign = self.verified_recently and _effect(effect) not in ("pay", "irreversible") and ask_person is None
        for g in covering if may_cosign else ():
            ctx = self._ctx(g)
            if ctx.cosigns(effect, lane_id=lane_id, amount_usd=amount):
                ctx.tool = f"panel.{action}"
                try:
                    ctx.precheck(effect, lane_id=lane_id)
                except AgentosError as exc:
                    self.trace(action, status="denied", lane=lane_id, effect=effect, summary=exc.message)
                    raise
                self.done(action, lane=lane_id, effect=effect, summary=summary, cosigned=True)
                return None
        ctx = self._ctx(covering[0])
        ctx.tool = f"panel.{action}"
        ctx.ask_person = ask_person  # set on every call, so a later call never inherits it
        try:
            decision = await ctx.authorize(effect, lane_id=lane_id, summary=summary, content=content, facts=facts)
        finally:
            ctx.ask_person = None
        self.done(action, lane=lane_id, effect=effect, summary=summary)
        return decision

    def trace(self, action: str, *, status: str = "ok", lane: str | None = None,
              effect: Effect | str | None = None, summary: str | None = None, **fields: Any) -> None:
        try:
            ws = lane.split("/", 1)[0] if lane else None
            g = self.grant_for(ws) if ws else None
            ctx = self._ctx(g) if g is not None else self.ctx
            ctx.tool = f"panel.{action}"
            ctx.trace("panel", status=status, lane=lane, effect=_effect(effect), summary=summary, via=self.via,
                      member=self.member.handle, **fields)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001  (tracing never fails a request)
            log.warning("panel trace failed for %s", action)

    # ---- what they see ----
    def schedule_visible(self, schedule: dict) -> bool:
        lane = schedule.get("lane")
        return isinstance(lane, str) and "/" in lane and self.can("standing", lane.split("/", 1)[0])

    def _member_event(self, kind: str, data: Any) -> Any:
        if kind == "login":  # the members who may run a lane's login form follow it
            lane = data.get("lane") if isinstance(data, dict) else None
            terms = self.watch_terms(lane) if isinstance(lane, str) and self.lane_allowed(lane) else None
            return data if terms is not None and not terms.get("watch_only") else None
        if kind in ("lane", "run"):
            lane = data.get("id" if kind == "lane" else "lane") if isinstance(data, dict) else None
            return data if isinstance(lane, str) and self.lane_allowed(lane) else None
        if kind in ("approval", "question", "trace"):
            return {} if self.scopes_with("approvals") or self.scopes_with("see") else None
        if kind == "meter":  # the panel refetches GET /api/meters, which shows only what they may see
            return {}
        if kind == "schedule":
            return data if isinstance(data, dict) and self.schedule_visible(data) else None
        if kind == "member":
            return {} if self.scopes_with("members") else None
        if kind == "resync":
            return data
        return None
