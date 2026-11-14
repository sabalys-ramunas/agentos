"""MCP tools for the agenda (toolset "agenda"). Agents read the timeline, draft entries, edit
them, propose them and cancel them. Approving is not an agent tool: a person approves in Needs
you, in the panel's Agenda, or with `agentos agenda approve`; a standing approval or a routing
rule that does not ask can also let an entry run, exactly as it would let its run go."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agentos.agenda.models import EntryState
from agentos.agenda.service import SERVICE_NAME, AgendaService
from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Effect
from agentos.core.tools import ToolContext, ToolSpec

DEFAULT_DAYS = 7
MAX_ITEMS = 300
STATES = tuple(s.value for s in EntryState) + ("projected", "planned")


def _svc(ctx: ToolContext) -> AgendaService:
    return ctx.runtime.require(SERVICE_NAME)


def _grant(ctx: Any) -> str | None:
    gid = getattr(getattr(ctx, "grant", None), "id", None)
    return gid or getattr(getattr(ctx, "principal", None), "grant_id", None)


def _visible(ctx: ToolContext):
    """A lane's entries need that lane in the grant; a lane-less one needs the whole workspace."""
    def see(target: str | None, workspace: str | None) -> bool:
        if target:
            return ctx.can_access_lane(target)
        return bool(workspace) and ctx.can_access_lane(f"{workspace}/0")
    return see


def _time(value: str | None, name: str, default: datetime) -> datetime:
    if not value:
        return default
    try:
        t = datetime.fromisoformat(value.strip())
    except ValueError:
        raise InvalidRequest(f"{name} is an ISO 8601 time like 2026-11-08T00:00:00+02:00") from None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


async def _entry(ctx: ToolContext, entry_id: str) -> Any:
    """The entry, when the caller's grant sees its lane (else not found, so ids do not leak)."""
    e = _svc(ctx).get(entry_id)
    if not _visible(ctx)(e.action.acts_on, e.workspace):
        raise NotFound(f"agenda entry {entry_id!r} not found")
    return e


async def agenda_list(ctx: ToolContext, *, workspace: str | None = None, start: str | None = None,
                      end: str | None = None, lane: str | None = None, kind: str | None = None,
                      state: str | None = None, managed: bool = True) -> dict:
    """The workspace's agenda: one timeline of planned actions (posts, reminders, renewals, form
    submissions, exports, agent tasks), the upcoming fires of its recurring schedules (projected,
    not stored) and the runs that already happened, with their proof. It also shows the work
    other services plan on lanes on their own (type managed, state planned: keepalive's next
    visit per lane, at an approximate time; engagement's sessions drawn for today and tomorrow).
    Those are read only: they cannot be approved or cancelled here, and `managed_by` says what
    each service can and cannot show. Default range: from now for 7 days. Filter by lane, kind (flow,
    connector, route, keepalive, engage) or state (draft, proposed, scheduled, running, done,
    failed, skipped, expired, cancelled, projected, planned). Shows only what your grant's lanes
    cover.

    Args:
        workspace: One workspace, like biz-a; leave it out for every workspace your grant covers.
        start: The range's start, ISO 8601 with a timezone (default: an hour ago).
        end: The range's end, ISO 8601 with a timezone (default: 7 days after the start; at most 93 days).
        lane: Only items on this lane or surface, like biz-a/instagram.
        kind: Only this kind of action: flow, connector, route (a trigger), keepalive or engage.
        state: Only items in this state, like proposed (waiting for a person), projected (a schedule's next fire)
            or planned (another service's own plan).
        managed: Include what keepalive and engagement plan on their own (default true); false hides them.
    """
    now = _svc(ctx).clock.now()
    s = _time(start, "start", now - timedelta(hours=1))
    e = _time(end, "end", s + timedelta(days=DEFAULT_DAYS))
    if state and state not in STATES:
        raise InvalidRequest(f"state is one of {', '.join(STATES)}")
    await ctx.authorize(Effect.READ, lane_id=lane)
    out = _svc(ctx).timeline(start=s, end=e, workspace=workspace, lane=lane, kind=kind,
                             states=[state] if state else None, visible=_visible(ctx), limit=MAX_ITEMS,
                             managed=managed)
    for item in out["items"]:
        item.pop("history", None)
    return out


async def agenda_get(ctx: ToolContext, *, id: str) -> dict:
    """One agenda entry: its action and inputs, timing, state and why, the approval it waits for
    or ran under, its run and the proof the run left, and its history.

    Args:
        id: The entry's id, like ag_1f2e3d4c5b6a (from agenda_list or agenda_draft).
    """
    e = await _entry(ctx, id)
    await ctx.authorize(Effect.READ, lane_id=e.action.acts_on)
    _svc(ctx).sync()
    return _svc(ctx).describe(_svc(ctx).get(id))


async def agenda_draft(ctx: ToolContext, *, title: str, action: dict, when: dict | None = None,
                       notes: str | None = None, propose: bool = False) -> dict:
    """Plan an action on the workspace's agenda, as a draft. `action` names what runs: a saved
    flow ({"flow": "biz-a.billing.remind", "params": {...}, "on": lane}), a connector operation
    ({"connector": "instagram.post_photo", "params": {...}, "on": surface}) or a trigger that wakes
    a flow, a brain or an agent ({"trigger": "rt_...", "on": lane}; put what it should do in
    notes). `when`: {"at": time} (runs then, with 30 minutes of grace), {"not_before": time,
    "not_after": time} (a window), or {} (as soon as approved, optionally not_after). Times are
    ISO 8601 with a timezone. Your grant must cover the lane and the action's effect. Nothing runs
    until it is proposed and approved: pass propose=true, or call agenda_propose. Recurring work
    belongs in a schedule, not here.

    Args:
        title: What it is, in the user's words, like "Send the November invoice reminders".
        action: What runs: {"flow": id, "params", "on"}, {"connector": "site.operation", "params", "on": surface}, or {"trigger": "rt_...", "on"}.
        when: {"at": time}, {"not_before": time, "not_after": time}, or {} for as soon as approved. Times are ISO 8601 with a timezone, at most a year ahead.
        notes: Anything a person (or, for a trigger, the woken agent) should know; shown, never run.
        propose: Also propose it now (as agenda_propose does).
    """
    svc = _svc(ctx)
    probe = svc._action(action, grant_id=_grant(ctx), principal=ctx.principal.model_dump())
    await ctx.authorize(Effect.DRAFT, lane_id=probe.acts_on, summary=f"Draft agenda entry {title[:80]!r}")
    e = svc.draft(title=title, action=action, when=when, notes=notes, principal=ctx.principal, grant_id=_grant(ctx))
    ctx.trace("agenda_draft", entry=e.id, lane=e.action.acts_on, target=e.action.target)
    if propose:
        e = await svc.propose(e.id, ctx=ctx, by=ctx.principal.id)
    return svc.describe(e)


async def agenda_update(ctx: ToolContext, *, id: str, title: str | None = None, notes: str | None = None,
                        action: dict | None = None, when: dict | None = None) -> dict:
    """Edit an entry. Title and notes only change what a person reads. New params (merged over the
    old ones), a new lane or a new time change what runs: an entry that was approved or is waiting
    is asked again (back to proposed), and one that failed, was skipped or expired goes back to
    draft. A running, done or cancelled entry cannot be edited.

    Args:
        id: The entry's id, like ag_1f2e3d4c5b6a.
        title: A new title (changes only what a person reads).
        notes: New notes (changes only what a person reads).
        action: New params (merged over the old ones), a new `on`, or a whole new action as in agenda_draft.
        when: A new time, as in agenda_draft. An approved entry is asked again.
    """
    e = await _entry(ctx, id)
    await ctx.authorize(Effect.DRAFT, lane_id=e.action.acts_on, summary=f"Edit agenda entry {id}")
    out = await _svc(ctx).update(id, title=title, notes=notes, action=action, when=when, by=ctx.principal.id,
                                 ctx=ctx)
    ctx.trace("agenda_update", entry=id, state=out.state.value)
    return _svc(ctx).describe(out)


async def agenda_propose(ctx: ToolContext, *, id: str) -> dict:
    """Ask for permission to run a draft entry. The node asks its policy the same question the run
    would ask: if the routing lets it run without asking, or a standing approval covers it, it is
    scheduled now; otherwise it waits in Needs you for a person (state proposed). Tell the user
    what waits. You cannot approve it yourself.

    Args:
        id: The draft entry's id, like ag_1f2e3d4c5b6a.
    """
    e = await _entry(ctx, id)
    out = await _svc(ctx).propose(e.id, ctx=ctx, by=ctx.principal.id)
    ctx.trace("agenda_propose", entry=id, state=out.state.value)
    return _svc(ctx).describe(out)


async def agenda_cancel(ctx: ToolContext, *, id: str, reason: str | None = None) -> dict:
    """Call off an entry that has not finished: a waiting approval is withdrawn and a run that has
    not finished is cancelled (a running flow stops at its next step).

    Args:
        id: The entry's id, like ag_1f2e3d4c5b6a.
        reason: Why, in a few words; shown on the entry.
    """
    e = await _entry(ctx, id)
    await ctx.authorize(Effect.DRAFT, lane_id=e.action.acts_on, summary=f"Cancel agenda entry {id}")
    out = await _svc(ctx).cancel(id, by=ctx.principal.id, reason=reason)
    ctx.trace("agenda_cancel", entry=id)
    return _svc(ctx).describe(out)


STATIC = [
    ("agenda_list", agenda_list, Effect.READ, "lane"),
    ("agenda_get", agenda_get, Effect.READ, None),
    ("agenda_draft", agenda_draft, Effect.DRAFT, None),
    ("agenda_update", agenda_update, Effect.DRAFT, None),
    ("agenda_propose", agenda_propose, Effect.DRAFT, None),
    ("agenda_cancel", agenda_cancel, Effect.DRAFT, None),
]


def tool_specs(runtime: Any) -> list[ToolSpec]:
    return [ToolSpec(name=n, handler=h, effect=e, lane_arg=lane, tags=("agenda",), hidden_without=SERVICE_NAME)
            for n, h, e, lane in STATIC]
