"""The lane tools: whoami, lanes_list, lane_open, lane_close, lane_view and the claim tools
(lane_claim, lane_release, lane_queue).
"""

from __future__ import annotations

import inspect
import math
import os
import socket
from collections import Counter
from typing import Any

from agentos import __version__
from agentos.core.config import NodeConfig, load_proxies
from agentos.core.errors import InvalidRequest, ServiceUnavailable
from agentos.core.models import Effect, LaneInfo, LaneState, ViewerInfo
from agentos.core.tools import ToolContext, gate_kind
from agentos.node.claims import DEFAULT_TTL_S, MAX_TTL_S, MIN_TTL_S
from agentos.node.tools.inputs import lane_id
from agentos.node.tools.session import _takes, caller, check_control


def ssh_host(config: NodeConfig | None) -> str:
    """The SSH alias printed in tunnel hints: AGENTOS_SSH_HOST wins, then node.toml, then the
    machine's hostname (what another computer most likely calls it)."""
    return (os.environ.get("AGENTOS_SSH_HOST") or (config.ssh_host if config else "")
            or socket.gethostname() or "your-node")


def viewer_dict(viewer: ViewerInfo, host: str) -> dict[str, Any]:
    if viewer.viewer_port is None:  # no websockify any more: the live view is the signed link
        return {"display": viewer.display}
    return {
        "display": viewer.display,
        "viewer_port": viewer.viewer_port,
        "url": f"http://localhost:{viewer.viewer_port}{viewer.url_path}",
        "tunnel": viewer.tunnel_hint(ssh_host=host),
    }


def lane_summary(info: LaneInfo) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": info.spec.id,
        "kind": info.spec.kind.value,
        "engine": info.spec.engine,
        "state": info.state.value,
        "proxy": info.spec.proxy_label,
        "device": info.spec.device,  # lanes on one device are one computer to a site (None: its own)
        "last_used": info.last_used.isoformat() if info.last_used else None,
    }
    if info.quarantine_reason:
        out["quarantine_reason"] = info.quarantine_reason
    return out


def proxy_public(config: NodeConfig, label: str | None) -> dict[str, Any] | None:
    if not label:
        return None
    proxy = load_proxies(config).get(label)
    return proxy.public() if proxy else {"label": label}


def take_over(viewer: ViewerInfo, host: str) -> str:
    return (f"Give the human this to take over the browser: {viewer.tunnel_hint(ssh_host=host)}. "
            "Wait until they say they are done, then take a new snapshot.")


# ---- tools ----

NO_ENVIRONMENT_NEXT = ("No environment picked for calls without `on`. environment_open(title=...) gives you a cloud "
                       "computer with a terminal, a browser and your repo (the title of an open one attaches to it).")


async def _bound_environment(ctx: ToolContext) -> dict[str, Any] | None:
    """The environment bound to this chat, when the node runs environments (agentos.environments
    registers the "environments" service). None: no service, or none bound."""
    service = ctx.runtime.get("environments") if callable(getattr(ctx.runtime, "get", None)) else None
    if service is None:
        return None
    from agentos.environments.tools import bound_environment

    env = bound_environment(ctx)
    if inspect.isawaitable(env):
        env = await env
    if env is None:
        return None
    row = env if isinstance(env, dict) else (env.model_dump() if hasattr(env, "model_dump") else vars(env))
    return {key: row[key] for key in ("id", "title", "state", "workdir", "repo", "branch") if row.get(key) is not None}


async def whoami(ctx: ToolContext) -> dict[str, Any]:
    """Which agentos node this is and which environment (your cloud computer) calls without `on` go
    to, or how to open one: name, version, lane counts by state, and limits (max warm lanes, idle and
    busy timeouts). No lane ids (see lanes_list). Call it once. Cheap: reads no page. Effect: read."""
    await ctx.authorize(Effect.READ, summary="read the node's name, version, lane counts and limits")
    config = ctx.runtime.config
    lanes = ctx.runtime.lanes.list()
    counts = Counter(info.state.value for info in lanes)
    env = await _bound_environment(ctx)
    return {
        "node": config.name,
        "version": __version__,
        "environment": env,
        "next": (f"Calls without `on` go to environment {env.get('id')} ({env.get('title', '')}). "
                 "If you did not open it, environment_open(title=...) with your own title opens yours. "
                 "environment() shows its ports, git and cost." if env else NO_ENVIRONMENT_NEXT),
        "lanes": {"total": len(lanes), **{state.value: counts.get(state.value, 0) for state in LaneState}},
        "limits": {
            "max_warm_lanes": config.max_warm_lanes,
            "idle_seconds": config.idle_seconds,
            "busy_timeout": config.busy_timeout,
        },
    }


async def lanes_list(ctx: ToolContext) -> dict[str, Any]:
    """Every lane you may use on this node: id, kind, engine, state (cold, warming, warm, active,
    quarantined), proxy label, device (lanes on one device share one computer's fingerprint and
    proxy; null: a device of its own) and last use. Start here: the human creates lanes, tools cannot.
    Call it once a session, or not at all when the user named the lane. Cheap: reads no page. Effect: read."""
    await ctx.authorize(Effect.READ, summary="list the lanes on this node")
    allowed = getattr(ctx, "can_access_lane", None)  # a grant sees only the lanes it covers
    expose = ctx.runtime.get("expose") if callable(getattr(ctx.runtime, "get", None)) else None
    rows = []
    for info in ctx.runtime.lanes.list():
        if allowed is not None and not allowed(info.spec.id):
            continue
        row = lane_summary(info)
        notes = expose.lane_notes(info.spec.id) if expose is not None else []
        if notes:
            row["exposed"] = notes
        rows.append(row)
    return {"lanes": rows}


async def lane_open(ctx: ToolContext, *, on: str) -> dict[str, Any]:
    """Warm a lane: start its browser, display and live view. Browser tools warm a lane on their own,
    so call this only to get ready early or to retry a quarantined lane. Takes a few seconds and may
    close the least recently used idle lane. A failed network self-check is not an error: the
    result has state quarantined and a reason, so tell the human. Effect: read. Errors: busy,
    lane_not_found, engine_error."""
    on = lane_id(on)
    await ctx.authorize(Effect.READ, lane_id=on, summary=f"warm lane {on}")
    config = ctx.runtime.config
    info = await ctx.runtime.lanes.open(on)
    out = lane_summary(info)
    out["proxy"] = proxy_public(config, info.spec.proxy_label)
    out["viewer"] = viewer_dict(info.viewer, ssh_host(config)) if info.viewer else None
    return out


async def lane_close(ctx: ToolContext, *, on: str) -> dict[str, Any]:
    """Send a lane to cold: close its browser gracefully (cookies and logins stay on disk), to free
    a warm slot when you are done with it for a while. One call is enough. It acts on the lane, so it
    waits for the call in flight and, while another caller claims the lane, in the lane's queue. A call stuck on
    the lane is stopped and the browser closed anyway (listed under stuck_calls_stopped). Effect: read. Errors: lane_not_found, human_in_control,
    agent_paused or busy (leave the lane open then)."""
    on = lane_id(on)
    await ctx.authorize(Effect.READ, lane_id=on, summary=f"close lane {on}")
    lanes = ctx.runtime.lanes
    check_control(lanes.get(on), gate_kind(ctx))
    if _takes(lanes.close, "holder"):
        out = lane_summary(await lanes.close(on, holder=caller(ctx)))
    else:
        out = lane_summary(await lanes.close(on))
    take_note = getattr(lanes, "take_close_note", None)
    note = take_note(on) if callable(take_note) else None
    if note:
        out["stuck_calls_stopped"] = note["stuck_calls"]
        out["note"] = note["detail"]
    return out


def _caller_route(ctx: ToolContext) -> dict[str, Any] | None:
    """How the computer behind this call's grant reaches the node, when `agentos connect` recorded it."""
    from agentos.node import viewroutes

    ids = list(getattr(getattr(ctx, "grant", None), "chain_ids", None) or [])
    home = getattr(getattr(ctx.runtime, "config", None), "home", None)
    if not ids or home is None:
        return None
    try:
        return viewroutes.find(home, reversed(ids))
    except Exception:  # noqa: BLE001  (a broken file: today's link, never a failed call)
        return None


ENV_TAKE_OVER = ("This is your environment's browser, with no account behind it: the human can take control, "
                 "use it and hand it back from this link alone, no sign-in. While they have control, wait with "
                 "lane_wait_control.")


async def lane_view(ctx: ToolContext, *, on: str) -> dict[str, Any]:
    """A live-view link for a warm lane, so a human can watch and take over: log in, pass a CAPTCHA
    or a security check. Returns the URL, the SSH tunnel command and what to tell the human. Never
    type credentials yourself: give the human the link, then wait with lane_wait_control. The link
    only watches until the human signs in to the panel, and it ends within 30 minutes. Call it only
    to hand the lane to a human. A cold lane has no view: call lane_open first. Effect: read."""
    on = lane_id(on)
    await ctx.authorize(Effect.READ, lane_id=on, summary=f"live-view link for {on}")
    lanes = ctx.runtime.lanes
    info = lanes.get(on)
    viewer = lanes.view(on)
    out: dict[str, Any] = {"id": info.spec.id, "state": info.state.value}
    if viewer is None:
        out["viewer"] = None
        out["note"] = "The lane is not warm, so there is no live view. Call lane_open first."
    elif callable(getattr(lanes, "take_control", None)):
        # A signed link through the node's own port: the relay passes input only while the human
        # holds control, and checks the link, its lifetime and the page origin (no raw websockify).
        from agentos.node.control_tools import take_over_link

        # a grant from the controller's org carries the owner's ceiling (Grant.limit): without
        # control in it, the link only watches, so the controller cannot take the lane through it
        limit = getattr(getattr(ctx, "grant", None), "limit", None)
        control = limit is None or "control" in limit.streams
        route = _caller_route(ctx)  # the caller's own computer: its SSH host and the port its tunnel uses
        out["viewer"] = take_over_link(ctx.runtime, on, control=control, **(
            {"ssh_host": route["ssh_host"], "local_port": route["local_port"]} if route else {}))
        if control:
            from agentos.node.tools.shots import environment_of

            if environment_of(ctx, on) is not None:  # no account behind it: the link alone may hand back
                out["viewer"]["take_over"] = ENV_TAKE_OVER
    else:
        host = ssh_host(ctx.runtime.config)
        out["viewer"] = viewer_dict(viewer, host)
        out["take_over"] = take_over(viewer, host)
    return out


# ---- shared lanes: claims and the queue ----

CLAIM_WAIT_MAX_S = 120.0
CLAIM_NEXT = ("Do the job now: every call you make on the lane renews the claim. Call lane_release as soon as "
              "it is done, so the callers waiting can go on.")


def _claims(lanes: Any) -> Any:
    if not callable(getattr(lanes, "claim", None)):
        raise ServiceUnavailable("this node's lane manager does not share lanes (no claims)")
    return lanes


def _seconds(value: Any, name: str, lo: float, hi: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise InvalidRequest(f"{name} must be a number of seconds") from None
    if math.isnan(n):
        raise InvalidRequest(f"{name} must be a number of seconds")
    return max(lo, min(hi, n))


async def lane_claim(ctx: ToolContext, *, on: str, purpose: str, ttl_s: float = DEFAULT_TTL_S,
                     wait_s: float = 30.0) -> dict[str, Any]:
    """Claim a lane for a job of several acting calls (a post, a login, a form), so no other agent,
    flow or brain acts on it in between. One act alone needs no claim. Returns a lease with ttl_s: each of your calls on the lane
    renews it; ttl_s without one ends it. Call lane_release as soon as the job is
    done. Other callers' reads still run between your calls; their acting calls wait in the lane's
    queue. While others wait it is not renewed past claim_max_seconds (15 minutes by default). A
    human taking control, a hold, a freeze or a revoked grant ends it, and your next call on the
    lane then fails once with claim_lost. Claiming again renews it; it warms a cold lane. Flows and
    connector runs claim their own lane. Effect: read. Errors: busy (the message says who holds it
    and your place: do other work and claim again later, never in a tight loop), human_in_control,
    agent_paused, quarantined, lane_not_found.

    Args:
        purpose: Required. What the job is, in a few words other callers see, for example "posting a reply".
        ttl_s: Seconds the claim lasts after your last call on the lane, 5 to 900.
        wait_s: Seconds to wait in the queue for the lane, 0 to 120.
    """
    on = lane_id(on)
    ttl = _seconds(ttl_s, "ttl_s", MIN_TTL_S, MAX_TTL_S)
    wait = _seconds(wait_s, "wait_s", 0.0, CLAIM_WAIT_MAX_S)
    text = " ".join(str(purpose or "").split())
    if not text:
        raise InvalidRequest('give a short purpose other callers see, for example "posting a reply"')
    await ctx.authorize(Effect.READ, lane_id=on, summary=f"claim lane {on} for {ttl:g} s: {text[:80]}")
    lanes = _claims(ctx.runtime.lanes)
    out = await lanes.claim(on, caller(ctx), purpose=text, ttl_s=ttl, wait_s=wait, principal_kind=gate_kind(ctx))
    out["next"] = CLAIM_NEXT
    if out.get("ended_earlier"):
        out["next"] += (" Your earlier claim ended before this one (see ended_earlier): take a new snapshot "
                        "before you act, the page may have changed.")
    return out


async def lane_release(ctx: ToolContext, *, on: str) -> dict[str, Any]:
    """End your claim on a lane (from lane_claim), so the callers waiting for it go on. Having no
    claim is not an error: released is false. Effect: read. Errors: lane_not_found."""
    on = lane_id(on)
    await ctx.authorize(Effect.READ, lane_id=on, summary=f"release the claim on lane {on}")
    return await _claims(ctx.runtime.lanes).release(on, caller(ctx))


async def lane_queue(ctx: ToolContext, *, on: str) -> dict[str, Any]:
    """Who has a lane and who waits for it: the claim (holder, purpose, since when, when it expires,
    and whether it is yours), the calls on it now, and the queue in order, each with its mode (read,
    act, claim, run), how long it has waited and an estimate of when it gets in. Cheap: it does not
    use the browser. Effect: read. Errors: lane_not_found."""
    on = lane_id(on)
    await ctx.authorize(Effect.READ, lane_id=on, summary=f"read the queue of lane {on}")
    lanes = _claims(ctx.runtime.lanes)
    out = {"on": on, **lanes.queue_info(on, viewer=caller(ctx))}
    claim = out.get("claim")
    if claim is None:
        out["next"] = "Nobody claims the lane: acting calls take turns, reads share it."
    elif claim.get("yours"):
        out["next"] = "You hold the claim. Release it with lane_release when the job is done."
    else:
        out["next"] = ("Another caller holds the claim: your reads still run, your acting calls wait. Do other work "
                       "and come back, or claim it with lane_claim to be next in line.")
    return out
