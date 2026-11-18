"""MCP tools for checkpoints inside an environment: environment_checkpoint and environment_rollback
(agentos.environments.checkpoints). They sit next to environment_save in the environments tool set, and
in tools_more("terminals"): the everyday list (core) holds only what nearly every environment chat
needs, by its token budget (tests/unit/test_toolsets.py), and the skill says where these are."""

from __future__ import annotations

from typing import Any

from agentos.core.errors import AgentosError
from agentos.core.models import Effect
from agentos.core.tools import ToolContext, ToolSpec
from agentos.environments.errors import NoEnvironment
from agentos.environments.models import Environment
from agentos.environments.routing import env_ref


def _open_env(ctx: ToolContext, on: str | None, what: str) -> tuple[Any, Environment]:
    """The caller's environment, open: a handle of one since closed hears how to come back."""
    from agentos.environments.comeback import closed_words
    from agentos.environments.tools import _find, _mgr

    mgr = _mgr(ctx)
    env = _find(ctx, on)
    if env is None:
        raise NoEnvironment(f"this chat has no environment to {what}; environment_open(title=...) first")
    if not env.open:
        raise NoEnvironment(closed_words(mgr, env))
    return mgr, env


async def environment_checkpoint(ctx: ToolContext, *, note: str | None = None, on: str | None = None) -> dict:
    """A cheap undo point before a risky step, in seconds and at no cost: every repository under /work
    as it is now (tracked and new files git does not ignore), kept as git's
    refs/agentos/checkpoints/<n>. Your index, stash and branches are not touched; files git ignores
    (node_modules, .env) are not in it. About 20 are kept. Returns checkpoint, repos (head,
    files_changed from HEAD), compare (the git diff to run yourself) and rollback. Effect: read.
    Errors: checkpoint_failed.

    Args:
        note: What you are about to try, like "before the big refactor".
        on: The on your environment_open gave (a client with a session id may leave it out).
    """
    mgr, env = _open_env(ctx, on, "checkpoint")
    await ctx.authorize(Effect.READ, lane_id=None, summary=f"checkpoint environment {env.id} ({env.title})")
    try:
        out = await mgr.checkpoint(env, note)
    except AgentosError as e:
        mgr.record_call(env.terminal_lane, "environment_checkpoint", ok=False, error=e.code)
        raise
    n = out["checkpoint"]
    mgr.record_call(env.terminal_lane, "environment_checkpoint", ok=True, summary=f"{n} {out['note'] or ''}".strip())
    ctx.trace("environment_checkpoint", lane=env.terminal_lane, environment=env.id, checkpoint=n)
    dirs = ", ".join(r["dir"] for r in out["repos"])
    out["environment"] = env_ref(env)
    out["next"] = (f"checkpoint {n} keeps {dirs}. Try the change; `{out['compare']}` in a repository shows what "
                   "changed since (git status lists new files, which that diff shows as deleted). "
                   f"environment_rollback(checkpoint={n}) puts it all back, and checkpoints itself first")
    return out


async def environment_rollback(ctx: ToolContext, *, checkpoint: int, on: str | None = None) -> dict:
    """Put every repository under /work back to a checkpoint: the branch it was on at the checkpoint's
    HEAD, the index at that HEAD, the files as they were (new files git does not ignore are removed).
    It checkpoints the work as it is first, so `undo` takes the rollback back. Files git ignores
    (node_modules, .env, build output) are not touched: after a dependency change, run the install
    again. Returns checkpoint, repos (files_restored, files_removed, branch, head, was), undo.
    Effect: read. Errors: checkpoint_not_found, rollback_refused (mid merge or rebase).

    Args:
        checkpoint: Its number, from environment_checkpoint or environment()'s checkpoints.
        on: The on your environment_open gave (a client with a session id may leave it out).
    """
    mgr, env = _open_env(ctx, on, "roll back")
    await ctx.authorize(Effect.READ, lane_id=None,
                        summary=f"roll environment {env.id} ({env.title}) back to checkpoint {checkpoint}")
    try:
        out = await mgr.rollback(env, checkpoint)
    except AgentosError as e:
        mgr.record_call(env.terminal_lane, "environment_rollback", ok=False, error=e.code)
        raise
    mgr.record_call(env.terminal_lane, "environment_rollback", ok=True, summary=str(checkpoint))
    ctx.trace("environment_rollback", lane=env.terminal_lane, environment=env.id, checkpoint=checkpoint,
              undo=out["undo"]["checkpoint"])
    out["environment"] = env_ref(env)
    done = "; ".join(f"{r['dir']}: {r['files_restored']} restored, {r['files_removed']} removed, "
                     f"{r['branch'] or 'detached'} at {r['head'] or 'no commit'}" for r in out["repos"])
    words = f"back at checkpoint {checkpoint} ({done}). "
    if any(r.get("commits_undone") for r in out["repos"]):
        words += "Commits made since are off the branch, and checkpoint " \
                 f"{out['undo']['checkpoint']} still has them. "
    if out.get("failed"):
        words += "Not rolled back: " + "; ".join(f"{f['dir']} ({f['why']})" for f in out["failed"]) + ". "
    out["next"] = (words + f"{out['undo']['rollback']} undoes this. Files git ignores were left as they are: run the "
                   "install again if the dependencies changed. A dev server may need a restart")
    return out


def tool_specs(runtime: Any) -> list[ToolSpec]:
    t = ("environments",)
    return [
        ToolSpec("environment_checkpoint", environment_checkpoint, Effect.READ, lane_arg=None,
                 tags=(*t, "changes_state"), hidden_without="environments"),
        ToolSpec("environment_rollback", environment_rollback, Effect.READ, lane_arg=None,
                 tags=(*t, "changes_state"), hidden_without="environments"),
    ]
