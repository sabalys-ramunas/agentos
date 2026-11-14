"""MCP tools for the engagement engine: levers, never clicks (engagement-engine.md 5.9).

engage_program_draft saves a draft version (a `draft` effect: a private write on this node) that
does nothing until the owner approves it on the node. engage_status reads. No tool approves a
program, changes a cap, clears a hold, starts a session, or acts on a site.

Items (brief B4, agentos.engage.items): engage_item_submit queues approved content for a surface's
schedule. Every item is recorded as submitted by the owner and runs without a second approval, so
an agent's item must first be approved by the owner: the tool authorizes `commit` with no lane and
the exact item as content. A commit on no account always routes to approval (never auto, and no
standing approval covers it, since standing approvals need a lane), only a one-off approval of
that exact item lets the retry queue it, and a grant whose ceiling is under commit is refused. The
owner in person (a signed-in panel session: `ctx.in_person`) authorizes `draft` (a private write)
and the item is queued at once. A bearer token is never in person, the node's own token included:
an agent holding it asks the owner like any other agent.
engage_items reads; engage_item_withdraw takes a queued item back (the owner, or whoever asked).

engage_digest (brief B5, agentos.engage.digest) reads one day per surface. It never returns draft
text: a provider wrote it, so it is untrusted, and only the owner's panel shows a preview.
"""

from __future__ import annotations

from typing import Any

from agentos.core.errors import InvalidRequest, PolicyDenied
from agentos.core.models import Effect
from agentos.core.tools import ToolContext, ToolSpec
from agentos.engage.governor import is_owner
from agentos.engage.service import SERVICE_NAME, EngageService

TAG = "engage"


def _svc(ctx: ToolContext) -> EngageService:
    return ctx.runtime.require(SERVICE_NAME)


async def engage_program_draft(ctx: ToolContext, *, surface: str, program: str) -> dict:
    """Draft an engagement program for one surface (an account such as "biz-a/instagram"). The
    program is YAML or JSON with: program (a name), risk (careful, or off for posting only),
    phases (an optional {kind: warm_up, days} first, then {kind: steady, daily: {like, comment,
    follow, join, story_view, post}, targets: [{source, values}], filters}), comments ({mode:
    approve, topics, max_chars}), community, posting and report. Returns the validated YAML, a
    plain summary, the caps the account will really get (the engine gives less than a target when
    a cap is lower), and warnings. Nothing runs: only the owner approves a program, on the node.
    Agents cannot choose risk normal, auto comments or replies, or the followers_of source, and
    cannot raise caps. Fails with program_invalid (fix the field it names) or owner_only.

    Args:
        surface: The account the program is for, as "workspace/name", for example "biz-a/instagram".
        program: The program as YAML or JSON text.
    """
    await ctx.authorize(Effect.DRAFT, lane_id=surface, summary=f"draft an engagement program for {surface}")
    return _svc(ctx).draft(surface, program, by=ctx.principal, as_owner=_owner_here(ctx))


async def engage_status(ctx: ToolContext, *, surface: str | None = None, workspace: str | None = None) -> dict:
    """Read where engagement stands: per account, its program and whether the owner approved it,
    its phase (warm-up day or steady ramp week), today's planned sessions and actions per kind,
    actions done, today's caps, holds (what stopped an account, and until when), recent notices,
    and the window of the next session (a part of the day, never an exact time). `dry_run` says
    whether this node only draws and reports plans (the default) or runs sessions; either way an
    agent cannot start, clear or change anything here.

    Args:
        surface: One account, as "workspace/name". Leave out for every account you may see.
        workspace: Only the accounts of this workspace.
    """
    await ctx.authorize(Effect.READ, lane_id=surface, summary="read engagement status")
    can_see: Any = None
    check = getattr(ctx, "can_access_lane", None)
    if callable(check) and surface is None:
        can_see = check
    return _svc(ctx).status(surface=surface, workspace=workspace, can_see=can_see)


def _owner_here(ctx: ToolContext) -> bool:
    """The owner at the node: a signed-in panel session (the context says `in_person`). Not
    core.tools.in_person, which also counts a human principal on a bearer token: the node's MCP
    token resolves to the owner, and agents connect with it."""
    return is_owner(ctx.principal) and getattr(ctx, "in_person", False) is True


def _may_see(ctx: ToolContext, surface: str) -> bool:
    check = getattr(ctx, "can_access_lane", None)
    return not callable(check) or bool(check(surface))


async def engage_item_submit(ctx: ToolContext, *, surface: str, kind: str, text: str, title: str | None = None,
                             targets: list[str] | None = None, attachments: list[str] | None = None,
                             expires_at: str | None = None) -> dict:
    """Add approved content made outside agentOS (a post, a comment) to a surface's engagement
    schedule. The next session that already plans that kind takes the oldest waiting item, under
    the same caps: items add no extra posts or comments. The surface needs an approved program, and
    the site must have the kind (for example post or comment on reddit). An item goes out without
    asking again, so over MCP (any token, the node's own included) it returns status
    approval_pending: the owner approves this exact item, attachments included, then call again
    with the same arguments to queue it. Returns the item (id it_...,
    status queued) and warnings, such as a program that plans none of this kind. Items not used by
    expires_at expire. Fails with invalid (the message names what to fix) or denied.

    Args:
        surface: The account, as "workspace/name", for example "biz-a/reddit".
        kind: What it is: post or comment (what the site takes).
        text: The words to publish, within the site's limit and the program's comments.max_chars.
        title: A title, for sites that take one (a Reddit post). Optional.
        targets: At most one: where it goes (a subreddit such as r/coffee for a Reddit post, a post
            link for a comment). Optional.
        attachments: File ids on the surface's lane (from files_put), images or videos. Optional.
        expires_at: When it stops waiting, ISO 8601 with a time zone (such as
            2026-11-12T18:00:00+02:00 or ...Z), at most 30 days away. Default: 7 days after it is
            queued.
    """
    svc = _svc(ctx)
    plan = svc.items.check(surface, kind, text, title=title, targets=targets, attachments=attachments,
                           expires_at=expires_at)
    if _owner_here(ctx):
        await ctx.authorize(Effect.DRAFT, lane_id=surface, summary=plan.summary, content=plan.canonical)
        return svc.items.submit(plan, in_person=True)
    if not _may_see(ctx, surface):
        raise PolicyDenied(f"this grant does not reach {surface}")
    # no lane: a commit on no account always waits for the owner, and no standing approval covers it
    d = await ctx.authorize(Effect.COMMIT, summary=plan.summary, content=plan.canonical)
    if getattr(d, "route", None) != "approved" or not getattr(d, "via", None):
        raise PolicyDenied("an item goes out without asking again, so the owner approves each one first; "
                           "this node's routing did not ask the owner, so nothing was queued")
    return svc.items.submit(plan, requested_by=_asker(ctx), approval_id=d.via)


def _asker(ctx: Any) -> dict[str, Any]:
    """Who asks, with their grant's path: what decides that a later withdraw is theirs."""
    p = ctx.principal
    d = p.model_dump(exclude_none=True) if hasattr(p, "model_dump") else dict(vars(p))
    path = getattr(getattr(ctx, "grant", None), "path", None)
    return {**{k: d[k] for k in ("kind", "id", "owner") if k in d}, **({"grant_path": path} if path else {})}


async def engage_items(ctx: ToolContext, *, surface: str | None = None, status: str | None = None,
                       item_id: str | None = None) -> dict:
    """Read the items waiting for (or used by) engagement sessions: per item its id, surface, kind,
    status (queued, reserved while a session has it, awaiting while its run waits for an approval,
    used with the run id, failed when its run failed and may have posted, expired or withdrawn),
    text, targets, attachments (with each file's name, type, size and sha256) and expiry. Pass
    item_id for one item.

    Args:
        surface: One account, as "workspace/name". Leave out for every account you may see.
        status: Only items in this status.
        item_id: One item, by its id (it_...).
    """
    svc = _svc(ctx)
    if item_id is not None:
        item = svc.items.get(item_id)
        await ctx.authorize(Effect.READ, lane_id=item["surface"], summary=f"read item {item_id}")
        return {"items": [svc.items.public(item)]}
    await ctx.authorize(Effect.READ, lane_id=surface, summary="read engagement items")
    return {"items": svc.items.list(surface, status=status,
                                    can_see=None if surface is not None else (lambda s: _may_see(ctx, s)))}


async def engage_item_withdraw(ctx: ToolContext, *, item_id: str) -> dict:
    """Take a queued item back, so no session uses it. The owner can withdraw any item; an agent
    only the items it asked for. An item a session has right now, or one already used, expired or
    withdrawn, cannot be withdrawn. Fails with owner_only, invalid or not_found.

    Args:
        item_id: The item's id (it_...).
    """
    svc = _svc(ctx)
    item = svc.items.get(item_id)
    await ctx.authorize(Effect.DRAFT, lane_id=item["surface"], summary=f"withdraw item {item_id}")
    return svc.items.withdraw(item_id, by=_asker(ctx), as_owner=_owner_here(ctx))


async def engage_digest(ctx: ToolContext, *, surface: str | None = None, day: str | None = None,
                        format: str = "json") -> dict:
    """Read one day of engagement per account (by default yesterday, in each account's time
    zone): sessions planned and run with what came of each, operations per kind (planned,
    attempted, verified, failed, skipped with the reasons, awaiting approval), drafts awaiting the
    owner's approval with their ids (never their text), items used and expired, holds in force
    that day and the signals sites showed, cap use against the day's cap and the hard ceiling, and
    the day's notices. Reads only; an agent cannot change anything here.

    Args:
        surface: One account, as "workspace/name". Leave out for every account you may see.
        day: A calendar day as YYYY-MM-DD. Default: yesterday in each account's time zone.
        format: "json" (default) for the records, or "text" for the same data as plain text.
    """
    from agentos.engage import digest

    if format not in ("json", "text"):
        raise InvalidRequest('format is "json" or "text"')
    when = digest.parse_day(day)
    surface = (surface or "").strip() or None
    await ctx.authorize(Effect.READ, lane_id=surface, summary="read the engagement digest")
    check = getattr(ctx, "can_access_lane", None)
    can_see: Any = check if callable(check) else (lambda _lane: False)  # no check: no other account
    out = digest.digest(_svc(ctx), surface, when, can_see=can_see)
    if format == "text":
        return {"schema": out["schema"], "format": "text",
                "text": "\n\n".join(digest.render_text(r) for r in out["surfaces"]) or "No engagement programs."}
    return out


def tool_specs(runtime: Any) -> list[ToolSpec]:
    return [
        ToolSpec(name="engage_program_draft", handler=engage_program_draft, effect=Effect.DRAFT, lane_arg="surface",
                 tags=(TAG,), hidden_without=SERVICE_NAME),
        ToolSpec(name="engage_status", handler=engage_status, effect=Effect.READ, lane_arg="surface",
                 tags=(TAG,), hidden_without=SERVICE_NAME),
        ToolSpec(name="engage_item_submit", handler=engage_item_submit, effect=Effect.COMMIT, lane_arg="surface",
                 tags=(TAG,), hidden_without=SERVICE_NAME),
        ToolSpec(name="engage_items", handler=engage_items, effect=Effect.READ, lane_arg="surface",
                 tags=(TAG,), hidden_without=SERVICE_NAME),
        ToolSpec(name="engage_item_withdraw", handler=engage_item_withdraw, effect=Effect.DRAFT, lane_arg=None,
                 tags=(TAG,), hidden_without=SERVICE_NAME),
        ToolSpec(name="engage_digest", handler=engage_digest, effect=Effect.READ, lane_arg="surface",
                 tags=(TAG,), hidden_without=SERVICE_NAME),
    ]
