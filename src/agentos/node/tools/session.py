"""How a node tool takes a lane: the caller as the lane's queue knows it, the grant check, the
session a tool runs in (agentos.node.claims), and the page-text label for the trifecta check.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from agentos.core.errors import AgentPaused, HumanInControl
from agentos.core.models import ActionKind, ControlState, Effect, LaneInfo
from agentos.core.pauses import paused_words
from agentos.core.runtime import Runtime
from agentos.core.tools import ToolContext, gate_kind
from agentos.node import deadlines, egress
from agentos.node.claims import ACT, Holder
from agentos.node.tools.inputs import url_host

log = logging.getLogger(__name__)


def _recording(runtime: Runtime, lane: str) -> bool:
    """Whether a flow recording is active on the lane (agentos.runner's recorder)."""
    flows = runtime.get("flows")
    recorder = getattr(flows, "recorder", None)
    try:
        return bool(recorder is not None and recorder.active(lane))
    except Exception:
        log.exception("recorder check failed")
        return False


def _takes(fn: Any, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def _acts_alone(ctx: ToolContext, effect: Effect, lane: str) -> bool:
    """Whether `effect` would run on `lane` with no person. No: a context that cannot say."""
    ask = getattr(ctx, "acts_alone", None)
    return ask is not None and ask(effect, lane_id=lane) is True


def _link_click_recorder(ctx: ToolContext, on: str, target: str) -> Callable[[], None]:
    """What browser_navigate does right before the engine presses a link: the press goes on the
    record as what it is, a click at browser_act's level, before anything can come of it. A
    `link_click` row under the call names the link's address, and the call's tool_end reads draft."""

    def clicked() -> None:
        ctx.acted(Effect.DRAFT)  # type: ignore[attr-defined]  # _acts_alone: a context that has it
        ctx.trace("link_click", lane=on, effect=Effect.DRAFT.value, summary=f"click link to {target} on {on}",
                  action=ActionKind.CLICK.value, url=target)
    return clicked


def _takes_principal_kind(using: Any) -> bool:
    return _takes(using, "principal_kind")


def _host_of(url: Any) -> str | None:
    try:
        return url_host(url) if isinstance(url, str) and url else None
    except Exception:  # noqa: BLE001 - about:blank, a data: URL: no host to name
        return None


def label_page_text(ctx: Any, tool: str, url: str | None = None) -> None:
    """Page text (titles, outlines, screenshots) is written by anyone who can post on the page, so
    it is untrusted in the agent's context: label the grant family for the lethal-trifecta check
    (AUD-31) and the untrusted-content downgrade, naming the page's host (an approval that follows
    says "after reading a web page at <host>"). A ToolContext without taint labels (the M1 owner
    wrapper) is left alone."""
    taint = getattr(ctx, "taint", None)
    if not callable(taint):
        return
    if _takes(taint, "host"):
        taint("untrusted", source=tool, kind="web_page", host=_host_of(url))
    else:
        taint("untrusted", source=tool)


def grant_check(ctx: Any) -> Any:
    """For a claim: why the caller's grant may no longer hold it (revoked or expired), else None.
    A grant this node has no record of (the owner token) is never ended this way."""
    grant_id = getattr(getattr(ctx, "principal", None), "grant_id", None)
    runtime = getattr(ctx, "runtime", None)
    policy = runtime.get("policy") if runtime is not None and callable(getattr(runtime, "get", None)) else None
    record = getattr(policy, "grant_record", None)
    if not grant_id or not callable(record):
        return None

    def check() -> str | None:
        try:
            state = record(grant_id).get("state")
        except Exception:  # noqa: BLE001 - not a grant on record: nothing to end the claim for
            return None
        return None if state in (None, "active") else f"your grant is {state}"

    return check


def caller(ctx: Any) -> Holder:
    """Who calls, for the lane's queue and claims: the principal, its grant and thread, and the MCP
    session when the policy wrapper knows it (two sessions on one token are two callers)."""
    principal = getattr(ctx, "principal", None)
    session = getattr(getattr(ctx, "meta", None), "session", None)
    return Holder.for_principal(principal, session=session, check=grant_check(ctx), kind=gate_kind(ctx))


def lane_session(lanes: Any, on: str, principal_kind: str, *, holder: Holder | None = None,
                 mode: str = ACT, deadline_s: float | None = None,
                 tool: str | None = None) -> AbstractAsyncContextManager[Any]:
    """`lanes.using(on, principal_kind=..., holder=..., mode=..., deadline_s=..., tool=...)`; a
    LaneManager from before the control contract gets `using(on)` (it has no take-over to enforce
    yet), and one from before shared lanes or deadlines gets no holder, mode or deadline."""
    kwargs: dict[str, Any] = {}
    if _takes_principal_kind(lanes.using):
        kwargs["principal_kind"] = principal_kind
    if holder is not None and _takes(lanes.using, "holder"):
        kwargs["holder"] = holder
    if mode != ACT and _takes(lanes.using, "mode"):
        kwargs["mode"] = mode
    if deadline_s is not None and _takes(lanes.using, "deadline_s"):
        kwargs["deadline_s"] = deadline_s
        kwargs["tool"] = tool
    return lanes.using(on, **kwargs)


def ctx_session(ctx: Any, on: str, *, mode: str = ACT, lanes: Any = None,
                extra_s: float = 0.0) -> AbstractAsyncContextManager[Any]:
    """lane_session for a tool call: the caller's principal kind and holder, and the tool's overall
    deadline (agentos.node.deadlines; `extra_s` is what this call needs on top: a wait's timeout_s,
    an act's typing)."""
    lanes = lanes if lanes is not None else ctx.runtime.require("lanes")
    tool = getattr(ctx, "tool", None)
    tool = tool if isinstance(tool, str) else None
    return lane_session(lanes, on, gate_kind(ctx), holder=caller(ctx), mode=mode,
                        deadline_s=deadlines.deadline_for(tool, extra_s=extra_s), tool=tool)


@asynccontextmanager
async def driven_session(ctx: ToolContext, on: str, *, mode: str = ACT, extra_s: float = 0.0) -> AsyncIterator[Any]:
    """lane_session, and the lane's egress guard learns who drives the lane (the caller's grant)."""
    async with ctx_session(ctx, on, mode=mode, lanes=ctx.runtime.lanes, extra_s=extra_s) as page:
        guard = egress.guard_for(page)
        if guard is not None:
            guard.drive(egress.Driver.from_context(ctx))
            guard.asks_first = functools.partial(lane_asks_first, ctx.runtime, on)
            guard.on_ask_first = functools.partial(tell_ask_first, ctx, on)
        yield page


def lane_asks_first(runtime: Any, lane_id: str) -> bool:
    """Is "Ask me first" on for this lane, or for a connector surface that posts through it?"""
    get: Any = getattr(runtime, "get", None)
    store = get("ask_first") if callable(get) else None
    if store is None:
        return False
    if store.is_on("lane", lane_id):
        return True
    surfaces = get("surfaces")
    return surfaces is not None and any(s.lane == lane_id and store.is_on("lane", s.id) for s in surfaces.list())


def tell_ask_first(ctx: Any, on: str, block: egress.Block) -> None:
    """A card in Needs you for a publish the guard refused because the account asks first."""
    policy, grant = ctx.runtime.get("policy"), getattr(ctx, "grant", None)
    if policy is None or grant is None:
        return
    done = "was stopped before it left" if block.prevented else "reached the site before it could be stopped"
    policy.approvals.create_question(
        question=f"An agent tried to publish on {on} by clicking, and it {done}, because this account asks you "
                 f"first. It was told to use a routine or to ask you. What it tried: {block.effect_rule}.",
        options=None, requester=ctx.principal, grant_id=grant.id, grant_path=grant.path,
        ttl_s=policy.cfg.question_ttl_s, workspace=grant.workspace())


def check_control(info: LaneInfo, principal_kind: str) -> None:
    """The using() gate, for tools that act on a lane without entering it (browser_close)."""
    if principal_kind == "human":
        return
    if info.control.state is ControlState.HUMAN:
        raise HumanInControl(f"a human has taken control of {info.spec.id}; wait until they hand it back")
    if info.control.state is ControlState.PAUSED:
        raise AgentPaused(paused_words(info.spec.id, info.control))


def who(ctx: ToolContext) -> str:
    """Whose outline memory a call reads and writes: one per agent session, per tab. A session is
    the principal's subject, the grant it calls under and its thread (the MCP session), because
    every session on one agent token shares the subject and must still get deltas against the
    outline it was shown itself."""
    principal = getattr(ctx, "principal", None)
    parts = (getattr(principal, name, None) for name in ("id", "grant_id", "thread"))
    subject, grant, thread = (str(part) if part else "" for part in parts)
    return f"{subject or 'agent'}|{grant}|{thread}"
