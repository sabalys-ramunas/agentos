"""MCP tools for brains: offload work to the node and reattach to it later.

Every tool that spends model capacity calls `ctx.authorize(Effect.BRAIN, ...)` first, with a
summary the node writes from what it will actually do. Tools never return credentials,
child grant tokens or prompt bodies.
"""

from __future__ import annotations

import hashlib
from typing import Any

from openajar.brains.models import Budget, ContextBundle, Requirements
from openajar.core.errors import InvalidRequest, NotFound, PolicyDenied
from openajar.core.models import Effect
from openajar.core.tools import (
    ToolContext,
    ToolSpec,
    covers_workspace,
    frozen_off,
    pattern_reaches,
    pattern_within,
    refuse_frozen,
    require_lanes,
)

NO_BRAIN = ("No brain is registered on this node. Tell the user: the owner adds one on the node, for example "
            "openajar brains add claude --kind subscription --provider anthropic (it asks for the token from "
            "claude setup-token; --no-credential registers it now and openajar brains auth signs it in later), "
            "or a local model with --kind local --provider ollama --base-url URL.")

def _svc(ctx: ToolContext, runtime: Any):
    rt = runtime if runtime is not None else ctx.runtime
    return rt.require("brains")


def _parse(model, value, what: str):
    if value is None:
        return None
    try:
        return model.model_validate(value)
    except Exception as exc:
        raise InvalidRequest(f"{what} is not valid: {exc.errors()[0]['msg'] if hasattr(exc, 'errors') else exc}") \
            from exc


def owns_task(ctx: Any, task: dict) -> bool:
    """Is this the caller's own task, or is the caller an admin grant? Its own: started under the
    caller's grant or a grant narrowed from it (the task's grant path starts with the caller's), so a
    brain's child grant never steers or reads what its parent started. Steering a task (cancel,
    resume) needs this: a resumed task runs on the child grant its starter's grant made, so reaching
    its browsers is not enough. Never the subject id: two grants may share one. A task from before grant
    paths were recorded has none, and only an admin grant steers it."""
    grant = getattr(ctx, "grant", None)
    if getattr(grant, "admin", False) is True:
        return True
    mine = getattr(grant, "path", None)
    started = task.get("grant_path")
    return isinstance(mine, str) and mine.startswith("/") and isinstance(started, str) and started.startswith(mine)


def sees_task(ctx: Any, task: dict) -> bool:
    """May this caller see a brain task (status, transcript, the list)? Every agent grant of the owner
    shares the owner, so the owner alone is not enough: its own tasks (owns_task), and the tasks whose
    browsers it all reaches."""
    if owns_task(ctx, task):
        return True
    lanes = [lane for lane in task.get("lanes") or [] if isinstance(lane, str)]
    allowed = getattr(ctx, "can_access_lane", None)
    try:
        return bool(lanes) and callable(allowed) and all(allowed(lane) for lane in lanes)
    except Exception:  # noqa: BLE001 - a lane the policy cannot read is outside every scope
        return False


def _seen(ctx: Any, svc: Any, task_id: str, *, steer: bool = False) -> dict:
    """The task's status, or NotFound (as for a task of another owner) when the caller may not see it,
    or, with `steer`, may not cancel or resume it."""
    task = svc.status(task_id, ctx.principal)
    if not (owns_task if steer else sees_task)(ctx, task):
        raise NotFound(f"no brain task {task_id}")
    refuse_frozen(ctx, **_task_scope(task))  # its own task too: a frozen workspace is not read or steered
    return _shown(task)


def _task_scope(task: dict) -> dict:
    """The browsers and workspace a brain task works in, for the freeze."""
    ws = task.get("workspace")
    return {"lanes": [x for x in task.get("lanes") or [] if isinstance(x, str)],
            "workspace": ws if isinstance(ws, str) else None}


def _a_workspace(workspace: Any) -> bool:
    """A well-formed workspace id (the service refuses any other with its own words)."""
    from openajar.core import globs

    try:
        return isinstance(workspace, str) and "*" not in workspace and bool(globs.validate_segment(workspace))
    except InvalidRequest:
        return False


def _shown(task: dict) -> dict:
    """A task as a tool returns it: the starter's grant path stays on the node."""
    return {k: v for k, v in task.items() if k != "grant_path"}


def tool_specs(runtime: Any) -> list[ToolSpec]:
    async def brain_run(ctx: ToolContext, *, prompt: str, workspace: str, lanes: list[str] | None = None,
                        brain: str | None = None, budget: dict | None = None,
                        context_bundle: dict | None = None, requires: dict | None = None) -> dict:
        """Start an agent task on the node's brain (your own subscription first, then a local model,
        a metered API only within budget). Returns at once with a task id; the task keeps running
        when you disconnect or the laptop sleeps. Check it with brain_status / brain_tail.

        prompt: what to do. workspace: the workspace it belongs to (sets the privacy rules).
        lanes: the browsers and terminals ("workspace/name") the brain may drive (it gets a child grant for just these).
        brain: force one brain by name (otherwise the router picks).
        budget: {max_usd, max_tokens, max_turns, max_minutes, max_queue_minutes}.
        context_bundle: hand off a session: {summary, files: [path or {path, content}],
        git: {remote, ref}, archive_b64, taint: ["private", "untrusted"]}.
        requires: {min_context, tools, vision, min_tier}. tools defaults to true; a job that needs one
        reply and no tools (a drafted reply, say) sets it false, which lets a chat-completions brain
        take it."""
        require_lanes(ctx, lanes, Effect.BRAIN)  # every lane in the grant before anything is asked or charged
        if _a_workspace(workspace) and not pattern_reaches(ctx, f"{workspace}/*"):  # a bad id: the service says
            raise PolicyDenied(f"your grant does not reach workspace {workspace}")
        svc = _svc(ctx, runtime)
        b = _parse(Budget, budget, "budget") or Budget()
        bundle = _parse(ContextBundle, context_bundle, "context_bundle")
        req = _parse(Requirements, requires, "requires")
        eff = svc.effective_budget(b)
        spend = f", up to ${b.max_usd:.2f} metered" if b.max_usd else ""
        await ctx.authorize(
            Effect.BRAIN,
            summary=(f"Run a brain task in workspace {workspace} on {brain or 'the routed brain'} "
                     f"(up to {eff.max_turns} turns and {int(eff.max_minutes or 0)} min{spend}"
                     f"{', browsers ' + ', '.join(lanes) if lanes else ''})"),
            content={"workspace": workspace, "brain": brain, "lanes": lanes or [],
                     "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                     "budget": eff.model_dump()})
        return await svc.submit(ctx.principal, prompt=prompt, workspace=workspace, lanes=lanes, brain=brain,
                                budget=b, context_bundle=bundle, requires=req, ctx=ctx)

    async def brain_status(ctx: ToolContext, *, id: str) -> dict:
        """Status of a brain task: state with an honest reason, which brain ran each attempt,
        turns, tokens, cost, and the last transcript sequence number for brain_tail."""
        await ctx.authorize(Effect.READ, summary=f"Read the status of brain task {id}")
        return _seen(ctx, _svc(ctx, runtime), id)

    async def brain_tail(ctx: ToolContext, *, id: str, since: int | None = None,
                         limit: int = 100) -> dict:
        """Transcript events of a brain task (assistant text, tool calls, results, retries).
        Pass `since` = the last `next` you saw to get only newer events; omit it for the latest."""
        await ctx.authorize(Effect.READ, summary=f"Read the transcript of brain task {id}")
        svc = _svc(ctx, runtime)
        _seen(ctx, svc, id)
        return svc.tail(id, ctx.principal, since=since, limit=limit)

    async def brain_cancel(ctx: ToolContext, *, id: str) -> dict:
        """Cancel a brain task: a graceful stop first, then a kill if it does not exit."""
        svc = _svc(ctx, runtime)
        _seen(ctx, svc, id, steer=True)  # before anything is asked: a task the caller may not steer is not there
        await ctx.authorize(Effect.BRAIN, summary=f"Cancel brain task {id}", content={"task": id})
        return await svc.cancel(id, ctx.principal)

    async def brain_resume(ctx: ToolContext, *, id: str, brain: str | None = None) -> dict:
        """Resume an interrupted, failed or cancelled brain task. The same brain continues its own
        session; another brain gets the transcript so far as context."""
        svc = _svc(ctx, runtime)
        task = _seen(ctx, svc, id, steer=True)
        require_lanes(ctx, task.get("lanes"), Effect.BRAIN)  # it runs again on its lanes: the caller must reach them
        await ctx.authorize(Effect.BRAIN, summary=f"Resume brain task {id} on {brain or 'the routed brain'}",
                            content={"task": id, "brain": brain})
        return await svc.resume(id, ctx.principal, brain=brain)

    async def brain_tasks(ctx: ToolContext, *, status: str | None = None, workspace: str | None = None,
                          limit: int = 20) -> list:
        """Recent brain tasks (newest first), to find work to reattach to."""
        await ctx.authorize(Effect.READ, summary="List brain tasks")
        tasks = _svc(ctx, runtime).list_tasks(ctx.principal, status=status, workspace=workspace,
                                              limit=max(1, min(limit, 100)))
        return [_shown(t) for t in tasks if sees_task(ctx, t) and not frozen_off(ctx, **_task_scope(t))]

    async def brains_list(ctx: ToolContext) -> list | dict:
        """Brains on this node: kind (subscription, local, metered), state (ready, cooling_down,
        needs_auth, down) with the reason, seats in use, and how full the usage window is. With none
        registered: {"brains": [], "next": what the owner runs to add one}."""
        await ctx.authorize(Effect.READ, summary="List brains")
        brains = await _svc(ctx, runtime).list_brains()
        if not brains:  # QA 2026-11-10, tester B 5: a bare [] says nothing about the next step
            return {"brains": [], "next": NO_BRAIN}
        return brains

    async def brain_use(ctx: ToolContext, *, name: str, workspace: str | None = None) -> dict:
        """Make a brain the default for a workspace (or for the node when no workspace is given).
        The privacy floor still applies: a local-only workspace never uses a cloud brain. A workspace
        must be one your grant covers whole; the node's default needs a grant that covers every workspace."""
        if workspace is not None and not covers_workspace(ctx, workspace):
            raise PolicyDenied(f"your grant does not cover workspace {str(workspace)[:40]}")
        if workspace is None and not pattern_within(ctx, "*"):
            raise PolicyDenied("only a grant for every workspace sets the node's default brain; name a workspace")
        await ctx.authorize(Effect.BRAIN, summary=f"Use brain {name} by default"
                            + (f" in workspace {workspace}" if workspace else " on this node"),
                            content={"brain": name, "workspace": workspace})
        svc = _svc(ctx, runtime)
        if workspace:
            return svc.use(name, workspace)
        return svc.use_node_default(name)

    common = {"tags": ("brains",), "lane_arg": None, "hidden_without": "brains"}
    return [
        ToolSpec(name="brain_run", handler=brain_run, effect=Effect.BRAIN, **common),
        ToolSpec(name="brain_status", handler=brain_status, effect=Effect.READ, **common),
        ToolSpec(name="brain_tail", handler=brain_tail, effect=Effect.READ, **common),
        ToolSpec(name="brain_cancel", handler=brain_cancel, effect=Effect.BRAIN, **common),
        ToolSpec(name="brain_resume", handler=brain_resume, effect=Effect.BRAIN, **common),
        ToolSpec(name="brain_tasks", handler=brain_tasks, effect=Effect.READ, **common),
        ToolSpec(name="brains_list", handler=brains_list, effect=Effect.READ, **common),
        ToolSpec(name="brain_use", handler=brain_use, effect=Effect.BRAIN, **common),
    ]
