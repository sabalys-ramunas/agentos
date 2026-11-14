"""MCP tools for lane control (docs/API.md, "Lane control").

`tool_specs(runtime)` gives agents four tools, all READ effect (they never act on a page):

  lane_control(on)                    who may act in the lane right now
  lane_request_control(on, reason)    ask the human for control back (it only asks)
  lane_wait_control(on, timeout_s)    long-poll until agents may act again (at most 300 s)
  lane_login_request(on, why)         ask the owner to log the lane in (agentos.node.login_assist; it only asks)

`take_over_link(runtime, on)` is what lane_view returns when a human has to take over: a
signed control-mode viewer link plus the SSH tunnel that reaches it.
"""

from __future__ import annotations

import asyncio
import math
from typing import Annotated, Any

from pydantic import Field

from agentos.core.errors import InvalidRequest
from agentos.core.models import ControlState, Effect, LaneControl, parse_lane_id
from agentos.core.tools import ToolContext, ToolSpec
from agentos.node.handback import claim_note
from agentos.node.viewer_state import control_dict
from agentos.node.viewer_tokens import ViewMode, ViewTokens

MAX_WAIT_SECONDS = 300.0
DEFAULT_WAIT_SECONDS = 60.0

OnArg = Annotated[str, Field(description='Lane id as "workspace/name", for example "personal/reddit".')]
ReasonArg = Annotated[str, Field(
    description="One short sentence the human sees, for example \"The login is done; I need the lane to "
                "post the reply.\" Never put secrets here.", max_length=300)]
WhyArg = Annotated[str, Field(
    description="One short sentence the owner sees, for example \"Reddit signed this lane out; I need it to post "
                "the reply.\" Never put a password, a code or an email address here.", max_length=300)]
TimeoutArg = Annotated[float, Field(
    description=f"How long to wait, in seconds (0 to {MAX_WAIT_SECONDS:.0f}). Call again to keep waiting.",
    ge=0, le=MAX_WAIT_SECONDS)]

NEXT = {
    ControlState.AGENT: "Agents have control. You may act on this lane.",
    ControlState.HUMAN: ("A human has control. Do not act on this lane and do not retry in a loop. Wait with "
                         "lane_wait_control, or ask for it back once with lane_request_control."),
    ControlState.PAUSED: ("The human stopped agents on this lane. Do not act on it and do not ask again in a loop. "
                          "Wait with lane_wait_control or tell your user."),
}
NEXT_AFTER_HANDBACK = ("Agents have control again. A human had the lane: read `handback` for what changed, then "
                       "take a new browser_snapshot before you act (your old refs are stale).")
NEXT_REQUESTED = ("You asked for control back; the human has not answered yet. Wait with lane_wait_control "
                  "instead of asking again.")


def _lane(on: str) -> str:
    try:
        parse_lane_id(on)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from None
    return on


def control_result(lane_id: str, control: LaneControl, freeze: Any = None) -> dict[str, Any]:
    """The LaneControl as a dict, plus what the agent should do next. On a frozen lane (`freeze`, the
    freeze service) no agent may act, whoever holds control."""
    out: dict[str, Any] = {"on": lane_id, **control_dict(control)}
    out["agent_may_act"] = control.state == ControlState.AGENT
    out["next"] = NEXT_REQUESTED if control.takeback_requested and control.state != ControlState.AGENT \
        else NEXT[control.state]
    rec = freeze.frozen_for(lane_id) if freeze is not None else None
    if rec is not None:
        out["agent_may_act"] = False
        out["frozen"] = True
        out["next"] = freeze.message(lane_id, rec)
    return out


async def with_note(ctx: ToolContext, lane_id: str, out: dict[str, Any]) -> dict[str, Any]:
    """`out` with the hand-back note (once per agent session) when agents may act again."""
    if not out.get("agent_may_act"):
        return out
    from agentos.node.tools.session import label_page_text, who

    note = await claim_note(ctx.runtime.require("lanes"), lane_id, who(ctx))
    if note is None:
        return out
    label_page_text(ctx, "handback")
    return {**out, "handback": note, "next": NEXT_AFTER_HANDBACK}


def tool_specs(runtime: Any) -> list[ToolSpec]:
    def lanes() -> Any:
        return runtime.require("lanes")

    async def lane_control(ctx: ToolContext, *, on: OnArg) -> dict[str, Any]:
        """Who may act in a lane right now: "agent" (you may act), "human" (a person took over; wait) or
        "paused" (the person stopped agents, or agentos holds the lane after a challenge signal; wait).
        Browser tools on a lane you do not control fail with human_in_control or agent_paused; call this
        to see who has it, since when, and whether your request for control back is still pending.
        Cheap: it does not use the browser. Effect: read. Errors: lane_not_found (see lanes_list)."""
        lane_id = _lane(on)
        await ctx.authorize(Effect.READ, lane_id=lane_id)
        return await with_note(ctx, lane_id, control_result(lane_id, lanes().get(lane_id).control,
                                                          runtime.get("freeze")))

    async def lane_request_control(ctx: ToolContext, *, on: OnArg, reason: ReasonArg) -> dict[str, Any]:
        """Ask the human who has taken over a lane to hand it back. It only asks: the human sees your reason
        and hands back or refuses, and control also returns on its own after a while without human input.
        Ask once, then wait with lane_wait_control. Effect: read. Errors: invalid (give a short reason),
        human_in_control (you asked again right after a refusal: wait instead), lane_not_found."""
        lane_id = _lane(on)
        await ctx.authorize(Effect.READ, lane_id=lane_id)
        info = await lanes().request_control(lane_id, by=ctx.principal.id, reason=reason)
        return control_result(lane_id, info.control, runtime.get("freeze"))

    async def lane_wait_control(ctx: ToolContext, *, on: OnArg,
                                timeout_s: TimeoutArg = DEFAULT_WAIT_SECONDS) -> dict[str, Any]:
        """Wait until agents may act in a lane again (the human handed it back, or it went back after a while
        without human input). Returns as soon as that happens, or after timeout_s (at most 300 s) with the
        current state: call again while agent_may_act is false. Use this instead of retrying browser tools.
        Once the lane is back, `handback` says what changed while the human had it (url, title, signal,
        tabs, files, changed fields, the human's note); your old refs are stale, so take a new
        browser_snapshot before you act. Effect: read. Errors: invalid, lane_not_found."""
        lane_id = _lane(on)
        await ctx.authorize(Effect.READ, lane_id=lane_id)
        try:
            timeout = float(timeout_s)
        except (TypeError, ValueError):
            raise InvalidRequest("timeout_s must be a number of seconds") from None
        if math.isnan(timeout):
            raise InvalidRequest("timeout_s must be a number of seconds")
        timeout = max(0.0, min(MAX_WAIT_SECONDS, timeout))
        loop = asyncio.get_running_loop()
        started = loop.time()
        info = await lanes().wait_for_agent_control(lane_id, timeout)
        out = control_result(lane_id, info.control, runtime.get("freeze"))
        out["waited_s"] = round(loop.time() - started, 1)
        out["timed_out"] = not out["agent_may_act"]
        return await with_note(ctx, lane_id, out)

    async def lane_login_request(ctx: ToolContext, *, on: OnArg, why: WhyArg) -> dict[str, Any]:
        """Ask the owner to log a lane in, when it is signed out (signal logged_out) or was never signed in
        and the vault holds no login for it (with one, use login: it asks nobody). It only notifies: the owner gets a push and logs in through agentos's login form, typing into a form
        that agentos types into the site. You never see or type the values, and you cannot start the login
        yourself. Ask once; asking again within two minutes does nothing. The owner's login takes the lane
        from agents while it runs, so then wait with lane_wait_control and take a new browser_snapshot after.
        Effect: read. Errors: invalid (give a short reason; a terminal lane), lane_not_found."""
        from agentos.node.login_assist import service_for

        lane_id = _lane(on)
        await ctx.authorize(Effect.READ, lane_id=lane_id)
        out = service_for(runtime).request(lane_id, why=why, by=ctx.principal.id)
        if out.get("running"):
            out["next"] = ("The owner is logging this lane in right now. Wait with lane_wait_control, then take a "
                           "new browser_snapshot.")
        elif out.get("already_asked"):
            out["next"] = "You already asked a moment ago. Wait with lane_wait_control instead of asking again."
        else:
            out["next"] = ("The owner was asked to log in. They have the lane while they do it: wait with "
                           "lane_wait_control, then take a new browser_snapshot. Do not type a password yourself.")
        return out

    tags = ("lanes", "control")
    return [
        ToolSpec("lane_control", lane_control, Effect.READ, tags=tags),
        ToolSpec("lane_request_control", lane_request_control, Effect.READ, tags=(*tags, "changes_state")),
        ToolSpec("lane_wait_control", lane_wait_control, Effect.READ, tags=tags),
        ToolSpec("lane_login_request", lane_login_request, Effect.READ, tags=(*tags, "changes_state")),
    ]


def take_over_link(runtime: Any, on: str, *, ttl_s: float | None = None, ssh_host: str | None = None,
                   port: int | None = None, base: str = "", control: bool = True,
                   local_port: int | None = None) -> dict[str, Any]:
    """A control-mode viewer link for lane_view: the human opens it (through the SSH tunnel in M1) to watch,
    take control, and hand back. Uses the runtime's `viewer_links` (a ViewTokens) when registered.
    `control=False` issues a link to watch only (a controller's caller whose ceiling has no control).
    `ssh_host` and `local_port`: how the caller's computer reaches this node (agentos.node.viewroutes), so
    the link and the tunnel are right there; without them, this node's port and hostname."""
    lane_id = _lane(on)
    config = runtime.require("config")
    tokens = runtime.get("viewer_links") or ViewTokens.for_config(config)
    link = tokens.issue(lane_id, ViewMode.CONTROL if control else ViewMode.VIEW, ttl_s)
    if ssh_host is None:
        from agentos.node.tools.lanes import ssh_host as default_ssh_host
        ssh_host = default_ssh_host(config)
    port = port or config.mcp_port
    local = local_port or port
    path = link.path(base)
    return {
        "mode": link.mode.value,
        "url": f"http://localhost:{local}{path}",
        "path": path,
        "expires_at": link.expires_iso,
        "tunnel": f"ssh -N -L {local}:localhost:{port} {ssh_host}",
        "take_over": ("Give the human the tunnel command and the url. In the viewer they watch, and, signed in "
                      "to this node's panel in that browser, take control, clear the challenge and hand it back. "
                      "The link alone cannot hand back or clear a hold, so do not try it yourself. While they "
                      "have control, wait with lane_wait_control.")
        if control else
        ("This link can only watch: this node's owner does not let a controller take a lane over. A person "
         "at the node takes it over from the node's own panel."),
    }
