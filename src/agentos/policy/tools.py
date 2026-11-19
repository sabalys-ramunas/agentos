"""MCP tools from the policy package: approvals, questions, the action log, and the caller's grant."""

from __future__ import annotations

from typing import Any

from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Effect
from agentos.core.tools import ToolSpec, frozen_off, refuse_frozen
from agentos.policy.approval_models import MAX_WAIT_S
from agentos.policy.errors import Denied
from agentos.policy.explain import explain
from agentos.policy.memory_rules import describe as describe_memory  # memory: grant_info


def _policy(ctx: Any) -> Any:
    return ctx.runtime.require("policy")


def _visible_approval(ctx: Any, approval_id: str) -> Any:
    policy = _policy(ctx)
    ap = policy.approvals.get(approval_id)
    if not policy.approvals.visible_to(ap, ctx.grant.id, ctx.grant.admin):
        raise NotFound(f"no approval {approval_id}")  # do not reveal other principals' approvals
    refuse_frozen(ctx, lanes=[ap.lane])  # an approval on a frozen lane: stop and tell the user
    return ap


PENDING_LIST_MAX = 50


async def approval_status(ctx: Any, *, approval_id: str | None = None) -> dict:
    """The state of an approval or question you created: pending, approved, denied, expired
    or consumed. For a question, `answer` holds the owner's reply. After an approval, call the
    original tool again with the same arguments. Without `approval_id`: the approvals and questions
    of your own grant still waiting (newest first, at most 50), for a new session that lost track of
    one; it decides nothing."""
    await ctx.authorize(Effect.READ)
    if approval_id is None:
        policy = _policy(ctx)  # the caller's own grant family only, an admin grant's too
        waiting = policy.approvals.list(state="pending", viewer=ctx.grant.id, limit=PENDING_LIST_MAX)
        return {"pending": [ap.status() for ap in waiting if not frozen_off(ctx, lanes=[ap.lane])],
                "next": "wait on one with approval_wait; once approved, call its tool again with the same arguments"}
    return _visible_approval(ctx, approval_id).status()


async def approval_wait(ctx: Any, *, approval_id: str, timeout_s: float = 60) -> dict:
    """Wait (long-poll, at most 120 seconds) until an approval or question is decided or
    expires, then return its status. Call it again if it is still pending."""
    await ctx.authorize(Effect.READ)
    if timeout_s < 0:
        raise InvalidRequest("timeout_s cannot be negative")
    _visible_approval(ctx, approval_id)
    ap = await _policy(ctx).approvals.wait(approval_id, min(float(timeout_s), MAX_WAIT_S))
    return ap.status()


async def approval_confirm(ctx: Any, *, approval_id: str) -> dict:
    """Ask the person to answer an approval your own calls are waiting on. Call it when
    approval_pending says `can_answer_in_chat`: their chat client shows them the node's own prompt
    and they answer there, not you. You get `status`: approved (call the original tool again with the
    same arguments), denied, or no_answer (tell them what waits; do not ask again at once). When chat
    cannot answer it (it needs more than chat, or the client cannot show a prompt) you get status
    review and a `review_url`: show them that link, and wait with approval_wait. The link opens it
    in the panel; it approves nothing by itself."""
    await ctx.authorize(Effect.READ)
    _visible_approval(ctx, approval_id)  # not found, or frozen, before a person is asked
    return await _policy(ctx).chat.confirm(ctx, approval_id)


async def human_ask(ctx: Any, *, question: str, options: list[str] | None = None) -> dict:
    """Ask the owner a question on their phone. Returns at once with an approval_id; use
    approval_wait to read the answer (approval_status, in the admin toolset, reads it without waiting). With `options`, the answer is one of them."""
    await ctx.authorize(Effect.READ)
    policy = _policy(ctx)
    if policy.approvals.count_pending(ctx.grant.root_id) >= policy.cfg.max_pending_per_grant:
        raise Denied("too_many_pending", "too many questions and approvals are already waiting")
    ap = policy.approvals.create_question(question=question, options=options, requester=ctx.principal,
                                          grant_id=ctx.grant.id, grant_path=ctx.grant.path,
                                          ttl_s=policy.cfg.question_ttl_s, workspace=ctx.grant.workspace())
    ctx.trace("owner_question", summary="asked the owner a question", approval_id=ap.id)
    return {"status": "question_pending", "approval_id": ap.id, "check_with": "approval_wait"}


async def traces_query(ctx: Any, *, lane: str | None = None, tool: str | None = None, status: str | None = None,
                       kind: str | None = None, run_id: str | None = None, before: str | None = None,
                       limit: int = 50) -> dict:
    """Read the action log, newest first. You see only calls made under your grant and the
    grants narrowed from it. `before` is the `next` cursor from a previous page."""
    await ctx.authorize(Effect.READ)
    if lane is not None and not ctx.can_access_lane(lane):  # outside the grant, or frozen (it then says so)
        return {"events": [], "next": None}
    viewer = None if ctx.grant.admin else ctx.grant.id
    events, nxt = _policy(ctx).traces.query(viewer_path=viewer, lane=lane, tool=tool, status=status, kind=kind,
                                            run_id=run_id, before=before, limit=min(int(limit), 200))
    return {"events": [e.public() for e in events if not (e.lane and frozen_off(ctx, lanes=[e.lane]))], "next": nxt}


async def grant_info(ctx: Any) -> dict:
    """What your grant allows: browsers, domains, effect ceiling, specials, expiry and the budget left,
    plus `explain`: the same in plain sentences, including which effects wait for a human. `meters`
    is today's use against the daily and hourly limits of your grant and of your browsers that have
    them (`left` per limit, `resets_at`); a limit reached refuses calls with limit_reached. `pace`
    is the posting pace of your browsers that have one: waking hours in the browser's own timezone (and
    where that zone comes from), the gap between posts and posts a day, with `next_allowed_at` (UTC)
    and `next_allowed_local`; a publish outside it is refused with lane_paced. `taint`
    says whether this session has read untrusted content (a web page, an email, a webhook body, a
    memory note marked untrusted) and from where: after it, publishing, paying, irreversible steps,
    running commands, starting services and installing wait for a person (listed in
    `taint.waits_for_a_person`), unless a standing approval bound to the exact content covers them."""
    await ctx.authorize(Effect.READ)
    policy = _policy(ctx)
    p = ctx.grant.projection()
    p.pop("owner_token", None)
    p["budget_left"] = policy.remaining(ctx.grant)
    p["memory"] = describe_memory(ctx.grant)  # memory: the memory special, None without it
    p["explain"] = explain(ctx.grant, routing=policy.routing_now(), remaining=p["budget_left"])
    runtime = getattr(ctx, "runtime", None)
    lanes = runtime.get("lanes") if runtime is not None and hasattr(runtime, "get") else None
    try:
        known = [i.spec.id for i in lanes.list()] if lanes is not None and hasattr(lanes, "list") else []
    except Exception:  # noqa: BLE001 - the lane list only adds lanes whose limits come from policy.toml
        known = []
    p["meters"] = meters = policy.meters_for(ctx.grant, [lane for lane in known if isinstance(lane, str)])
    for m in [meters["grant"], *meters["lanes"]]:
        if m["explain"]:
            p["explain"].append(m["explain"])
    p["pace"] = paces = [policy.lane_pace(lane) for lane in policy.pace.lanes() if ctx.can_access_lane(lane)]
    for one in paces:
        if one["explain"]:
            p["explain"].append(one["explain"])
    p["taint"] = state = policy.taint_state(ctx.grant, context=getattr(ctx, "context_key", None))
    if state["untrusted"]:
        p["explain"].append(f"This session read untrusted text ({state['after_untrusted']}), so "
                            f"{', '.join(state['waits_for_a_person'])} wait for a person now, even where they "
                            "would run on its own, unless a standing approval bound to the exact content "
                            f"covers them. It is clean again after {state['clean_context']}.")
    elif policy.cfg.taint.downgrade:
        p["explain"].append("After this session reads untrusted text (a web page, an email, a webhook body), "
                            f"{', '.join(policy.cfg.taint.downgrade)} wait for a person.")
    return p


def tool_specs(runtime: Any) -> list[ToolSpec]:
    return [
        ToolSpec(name="approval_status", handler=approval_status, lane_arg=None, tags=("approvals",)),
        ToolSpec(name="approval_wait", handler=approval_wait, lane_arg=None, tags=("approvals",)),
        ToolSpec(name="approval_confirm", handler=approval_confirm, lane_arg=None, tags=("approvals", "changes_state")),
        ToolSpec(name="human_ask", handler=human_ask, lane_arg=None, tags=("approvals", "changes_state")),
        ToolSpec(name="traces_query", handler=traces_query, lane_arg="lane", tags=("traces",)),
        ToolSpec(name="grant_info", handler=grant_info, lane_arg=None, tags=("policy",)),
    ]
