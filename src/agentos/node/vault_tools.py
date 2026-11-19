"""MCP tools of the vault: login, vault_fill, vault_list.

Secrets never appear in arguments or results. `login` and `vault_fill` are effect
SECRET and authorize with a node-written summary before anything is typed. That needs the
grant's `secret` special and never a person: a password in the vault is the owner's
permission to use it (docs/PRINCIPLES.md, 3).
"""

from __future__ import annotations

from typing import Any

from agentos.core.errors import InvalidRequest
from agentos.core.models import Effect, LaneKind, parse_lane_id
from agentos.core.tools import ToolContext, ToolSpec
from agentos.vault.login import fill_flow, login_flow
from agentos.vault.policy import env_scope_allows, lane_bound


def _browser_lane(ctx: ToolContext, on: str) -> Any:
    try:
        parse_lane_id(on)
    except ValueError as e:
        raise InvalidRequest(str(e)) from None
    lanes = ctx.runtime.require("lanes")
    info = lanes.get(on)
    if info.spec.kind is not LaneKind.BROWSER:
        raise InvalidRequest(f"{on} is a {info.spec.kind.value}, not a browser; the vault types only into browsers")
    return lanes


def _session(ctx: ToolContext, lanes: Any, on: str) -> Any:
    """The browser as this caller, acting (agentos.node.claims): it waits while another caller claims it."""
    from agentos.node.tools import ctx_session

    return ctx_session(ctx, on, lanes=lanes)


async def login(ctx: ToolContext, *, on: str, entry: str | None = None) -> dict:
    """Sign the browser in with a login from the vault. An environment's browser opens the login
    page itself when needed; on an account browser, browser_navigate there first. The node checks the
    login is bound to this browser and site, types it with human input and submits, with a code from
    its TOTP seed, its mail or the owner (waits a few minutes). You never see a
    secret. It asks nobody: call it again whenever a site logs you out. `needs` not null ("totp",
    "code", "captcha", "human"): the human's turn, in the live view.

    Args:
        entry: A login's name, only when several match (environment(), or vault_list in the vault
            toolset, names them).
    """
    vault = ctx.runtime.require("vault")
    lanes = _browser_lane(ctx, on)
    factory = vault.typist_for(ctx.runtime)
    envs = ctx.runtime.get("environments") if hasattr(ctx.runtime, "get") else None
    env_login = getattr(envs, "env_login", None)
    async with _session(ctx, lanes, on) as page:
        if callable(env_login):  # an environment's browser (agentos.environments.logins); None for any other lane
            result = await env_login(ctx, vault, factory(page), page, lane_id=on, entry=entry)
            if result is not None:
                return result
        return await login_flow(ctx, vault, factory(page), lane_id=on, entry=entry)


async def vault_fill(ctx: ToolContext, *, on: str, field: str, entry: str) -> dict:
    """Fill one login field from a vault entry. `field` is a snapshot ref (for example
    "e12") of a password, username/email or one-time-code input; the node decides from
    the field's structure what goes in it and refuses any other kind of field, any page
    not on the entry's domain, and any browser the entry is not bound to. Does not submit:
    click the form's button soon after, because the node treats the page's submit as the
    login only for about 30 seconds after a fill. It asks nobody. You never see the value."""
    vault = ctx.runtime.require("vault")
    lanes = _browser_lane(ctx, on)
    factory = vault.typist_for(ctx.runtime)
    async with _session(ctx, lanes, on) as page:
        return await fill_flow(ctx, vault, factory(page), lane_id=on, field_ref=field, entry=entry)


def _reached(ctx: ToolContext, pattern: str, known: list[str], entry: Any = None, vault: Any = None) -> bool:
    """Does the caller's grant reach a browser binding: the browser itself, or for `workspace/*` a browser
    of that workspace on this node that the grant covers (and, for a login bound to environments,
    one of the environments it covers). Fails closed."""
    allowed = getattr(ctx, "can_access_lane", None)
    if not callable(allowed):
        return False
    facts = getattr(vault, "facts_for", None)
    try:
        if not pattern.endswith("/*"):
            return bool(allowed(pattern))
        return any(lane_bound([pattern], lane) and allowed(lane)
                   and (entry is None or env_scope_allows(entry, lane, facts(lane) if callable(facts) else None))
                   for lane in known)
    except Exception:  # noqa: BLE001 - a binding the policy cannot read is outside every scope
        return False


def _known_lanes(ctx: ToolContext) -> list[str]:
    lanes = ctx.runtime.get("lanes") if hasattr(ctx.runtime, "get") else None
    try:
        return [i.spec.id for i in lanes.list()] if lanes is not None else []
    except Exception:  # noqa: BLE001 - without a lane list only exact bindings are matched
        return []


async def vault_list(ctx: ToolContext, *, on: str | None = None) -> dict:
    """List the vault entries bound to browsers your grant reaches: name, kind, domain and those
    bound browsers only (never usernames or secrets). With `on`, only entries bound to that browser."""
    if on is not None:
        try:
            parse_lane_id(on)
        except ValueError as e:
            raise InvalidRequest(str(e)) from None
    await ctx.authorize(Effect.READ, lane_id=on)
    vault = ctx.runtime.require("vault")
    entries = await vault.list_entries(lane=on)
    if getattr(getattr(ctx, "grant", None), "admin", False) is True:  # the node token, an admin grant: the whole vault
        return {"state": vault.state(), "entries": [e.listing() for e in entries]}
    known = _known_lanes(ctx)
    out = []
    for e in entries:  # an entry bound to no lane the grant reaches is the owner's business, not this agent's
        item = e.listing()
        item["lanes"] = [p for p in item["lanes"] if _reached(ctx, p, known, e, vault)]
        if item["lanes"]:
            out.append(item)
    return {"state": vault.state(), "entries": out}


def tool_specs(runtime: Any) -> list[ToolSpec]:
    return [
        ToolSpec(name="login", handler=login, effect=Effect.SECRET, tags=("vault", "browser"),
                 hidden_without="lanes"),
        ToolSpec(name="vault_fill", handler=vault_fill, effect=Effect.SECRET, tags=("vault", "browser"),
                 hidden_without="lanes"),
        ToolSpec(name="vault_list", handler=vault_list, effect=Effect.READ, lane_arg=None, tags=("vault",),
                 hidden_without="vault"),
    ]
