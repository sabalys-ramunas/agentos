"""MCP tools for workspaces: see them, read the login queue, plan and apply templates, set readiness.

Every handler reads the service from the runtime when it runs, so `tool_specs` works before the service
exists (the tools are hidden without it). Lists are filtered by `ctx.can_access_lane`, and anything that
creates lanes is checked lane by lane against the grant before the approval is asked for.
"""

from __future__ import annotations

from typing import Any

from agentos.core.errors import InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect, parse_lane_id
from agentos.core.tools import ToolContext, ToolSpec, covers_workspace
from agentos.workspaces.errors import WorkspaceConflict
from agentos.workspaces.models import ApplyResult, Plan
from agentos.workspaces.planner import approval_summary, refusal
from agentos.workspaces.service import SERVICE_NAME, WorkspaceService

TAG = "workspaces"


def _svc(ctx: ToolContext) -> WorkspaceService:
    return ctx.runtime.require(SERVICE_NAME)


def _not_there(workspace: str, ctx: ToolContext | None = None) -> NotFound:
    """What a workspace outside the grant answers: the words a workspace that is not there gets. With
    `ctx`, a close id among the workspaces this grant reaches is named first (never one outside it)."""
    if ctx is not None and isinstance(workspace, str):
        from agentos.core.text import close_match

        try:
            reached = [ws.id for ws in _svc(ctx).list_workspaces(allowed=ctx.can_access_lane)]
        except Exception:  # noqa: BLE001  (no workspace service: the plain answer)
            reached = []
        found = close_match(workspace, reached)
        if found:
            return NotFound(f"no workspace {workspace!r}. Did you mean {found!r}? (workspace_list shows yours)")
    return NotFound(f"no workspace {workspace!r}; create it with: agentos workspace create {workspace}")


def _seen(ctx: ToolContext, workspace: Any) -> bool:
    """A workspace the grant reaches: all of it, or a lane of it that is there now."""
    if not isinstance(workspace, str):
        return False
    if covers_workspace(ctx, workspace):
        return True
    prefix = f"{workspace}/"
    return any(lane.id.startswith(prefix) for ws in _svc(ctx).list_workspaces(allowed=ctx.can_access_lane)
               if ws.id == workspace for lane in ws.lanes)


def _plan(ctx: ToolContext, template: str, workspaces: Any, **options: Any) -> Plan:
    """Plan only inside the grant: every lane the template would make in every workspace named is checked
    before anything about those workspaces is read, and a conflict never names a lane outside it."""
    svc = _svc(ctx)
    tpl = svc.get_template(template)
    if isinstance(workspaces, list):
        if not all(isinstance(ws, str) for ws in workspaces):
            raise InvalidRequest("workspaces is a list of workspace ids")
        outside = sorted({lane for ws in workspaces for lane in (f"{ws}/{s.name}" for s in tpl.surfaces)
                          if not ctx.can_access_lane(lane)})
        if outside:  # the lanes the caller itself named: nothing about the workspace is read yet
            raise PolicyDenied(f"this grant does not cover: {', '.join(outside)}")
    return svc.plan(template, workspaces, shown=ctx.can_access_lane, **options)


def _check_grant(ctx: ToolContext, plan: Plan) -> None:
    outside = sorted({step.lane for step in plan.steps if not ctx.can_access_lane(step.lane)})
    if outside:
        raise PolicyDenied(f"this grant does not cover: {', '.join(outside)}")


async def workspace_list(ctx: ToolContext, *, workspace: str | None = None) -> dict:
    """List workspaces (one per business or person) with their lanes, readiness and login state. Each
    workspace has a summary such as "18 of 29 ready" and a health: ok (all lanes ready), attention (a lane
    has a note for the owner), blocked (a lane is quarantined) or idle (waiting for logins, or no lanes).
    Pass `workspace` to see one. Lanes outside your grant are left out."""
    svc = _svc(ctx)
    if workspace is not None:
        if not _seen(ctx, workspace):
            raise _not_there(workspace, ctx)
        return svc.get_workspace(workspace, allowed=ctx.can_access_lane).model_dump(mode="json")
    return {"workspaces": [ws.model_dump(mode="json")
                           for ws in svc.list_workspaces(allowed=ctx.can_access_lane)]}


async def workspace_queue(ctx: ToolContext, *, workspace: str | None = None) -> dict:
    """The login queue: lanes that are not ready yet, the ones needing attention first. Each lane carries
    its login URL (the owner opens it in the live view to log in) and the note the owner left. Read this to
    see which account the owner should log into next. Lanes outside your grant are left out."""
    if workspace is not None and not _seen(ctx, workspace):
        raise NotFound(f"no workspace {workspace!r}")  # as for one that is not there
    return _svc(ctx).queue(workspace, allowed=ctx.can_access_lane).model_dump(mode="json")


async def workspace_plan(ctx: ToolContext, *, template: str, workspaces: list[str],
                         proxy_per_workspace: dict[str, str] | None = None,
                         proxy_per_lane: dict[str, str | None] | None = None,
                         override_proxy_rules: bool = False, reason: str | None = None) -> dict:
    """Preview applying a template (for example 'social-6': instagram, facebook, reddit, medium, blog and
    linkedin) to workspaces. Creates nothing. Each step is create, exists (already there and matching) or
    conflict, with the reason. A proxy label is one account's IP, so a lane is refused when it would share
    a proxy with another workspace, or repeat a platform already on that proxy. A lane with no proxy is
    allowed with a warning. proxy_per_workspace sets one label per workspace, and proxy_per_lane overrides
    single lanes (null means no proxy). Overriding the proxy rules needs an explicit reason and is recorded."""
    plan = _plan(ctx, template, workspaces, proxy_per_workspace=proxy_per_workspace,
                 proxy_per_lane=proxy_per_lane, override_proxy_rules=override_proxy_rules, reason=reason)
    _check_grant(ctx, plan)
    return plan.public()


async def workspace_apply(ctx: ToolContext, *, template: str, workspaces: list[str],
                          proxy_per_workspace: dict[str, str] | None = None,
                          proxy_per_lane: dict[str, str | None] | None = None,
                          override_proxy_rules: bool = False, reason: str | None = None) -> dict:
    """Create the lanes of a template in workspaces. The owner approves first: the approval shows the exact
    lanes and their proxies, and it binds to that plan. Refused, with nothing created, when the plan has
    conflicts: fix them, or ask the owner whether to override the proxy rules. New lanes start as needs_login,
    so the owner logs in through the live view and then lane_set_readiness marks them ready. Safe to run again:
    it only creates what is missing."""
    svc = _svc(ctx)
    plan = _plan(ctx, template, workspaces, proxy_per_workspace=proxy_per_workspace,
                 proxy_per_lane=proxy_per_lane, override_proxy_rules=override_proxy_rules, reason=reason)
    _check_grant(ctx, plan)
    if plan.conflicts:
        raise WorkspaceConflict(refusal(plan))
    if not plan.creates:
        return ApplyResult(status="complete", existing=[step.lane for step in plan.steps]).model_dump(mode="json")
    await ctx.authorize(Effect.COMMIT, summary=approval_summary(plan), content=plan.approval_content())
    result = await svc.execute(plan, by=ctx.principal.id)
    ctx.trace("workspace_apply", template=plan.template, created=len(result.created), status=result.status)
    return result.model_dump(mode="json")


async def lane_set_readiness(ctx: ToolContext, *, on: str, state: str, note: str | None = None) -> dict:
    """Record a lane's login state once the owner says so: ready (the owner logged in), needs_login, or
    needs_attention with a short note saying what is wrong (a code was asked, a warning appeared, the account
    is locked). Do not mark a lane ready on a guess: ask the owner first. Login state is never detected
    automatically, and nothing here visits the site."""
    try:
        parse_lane_id(on)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from None
    await ctx.authorize(Effect.DRAFT, lane_id=on, summary=f"Set {on} to {state}",
                        content={"lane": on, "state": state, "note": note})
    view = _svc(ctx).set_readiness(on, state, note=note, by=ctx.principal.id)
    ctx.trace("lane_readiness", lane=on, state=state)
    return view.model_dump(mode="json")


def tool_specs(runtime: Any) -> list[ToolSpec]:
    return [
        ToolSpec(name="workspace_list", handler=workspace_list, effect=Effect.READ, lane_arg=None,
                 tags=(TAG,), hidden_without=SERVICE_NAME),
        ToolSpec(name="workspace_queue", handler=workspace_queue, effect=Effect.READ, lane_arg=None,
                 tags=(TAG,), hidden_without=SERVICE_NAME),
        ToolSpec(name="workspace_plan", handler=workspace_plan, effect=Effect.READ, lane_arg=None,
                 tags=(TAG,), hidden_without=SERVICE_NAME),
        ToolSpec(name="workspace_apply", handler=workspace_apply, effect=Effect.COMMIT, lane_arg=None,
                 tags=(TAG,), hidden_without=SERVICE_NAME),
        ToolSpec(name="lane_set_readiness", handler=lane_set_readiness, effect=Effect.DRAFT, lane_arg="on",
                 tags=(TAG,), hidden_without=SERVICE_NAME),
    ]
