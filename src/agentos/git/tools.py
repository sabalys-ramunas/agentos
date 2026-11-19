"""MCP tool `git_remotes`: which git remotes the node can clone and push for a workspace.

Read only, and never a secret: the declarations (agentos.git.remotes) and, for each, whether the
vault holds a credential and of which kind, with a deploy key's public key and fingerprint. An agent
(the offload skill) asks before it hands work over, so it can tell the person what to run on the
node when a remote is missing. Declaring a remote or adding a key is the owner's, on the node's CLI.
"""

from __future__ import annotations

from typing import Any

from agentos.core.errors import InvalidRequest, PolicyDenied
from agentos.core.models import Effect
from agentos.core.tools import ToolContext, ToolSpec, pattern_reaches
from agentos.git import keys as keymod
from agentos.git.remotes import WORKSPACE_RE
from agentos.git.wire import Remote, parse_remote


def tool_specs(runtime: Any) -> list[ToolSpec]:
    async def git_remotes(ctx: ToolContext, *, workspace: str, remote: str | None = None) -> dict:
        """The git remotes the node can fetch from and push to for this workspace's browsers and brain
        tasks, and whether it holds a credential for each (never the credential itself). With
        `remote` (a URL or host/owner/name), says whether that one is declared, whether pushes are
        allowed, and what the owner runs on the node when it is not. Git in a terminal or a brain task
        reaches only these, through the node, which asks a person before a force-push, a deletion or a
        push to a protected branch (and before any push after untrusted content)."""
        if not isinstance(workspace, str) or not WORKSPACE_RE.match(workspace):
            raise InvalidRequest("workspace is a workspace name, such as personal")
        if not pattern_reaches(ctx, f"{workspace}/*"):
            raise PolicyDenied(f"your grant does not reach workspace {workspace}")
        await ctx.authorize(Effect.READ)
        svc = (runtime if runtime is not None else ctx.runtime).require("git")
        creds = await svc.gateway.credentials()
        rules = [r for r in svc.remotes.list(workspace)
                 if not r.on_lane or ctx.can_access_lane(r.scope)]

        def cred_view(c: keymod.Credential | None) -> dict | None:
            if c is None:
                return None
            return {"name": c.name, "type": c.type, "fingerprint": c.fingerprint, "public_key": c.public_key}

        out: list[dict] = []
        for r in rules:
            probe = Remote(r.host, r.repo.replace("/*", "/x"))
            c = keymod.pick(creds, probe)
            out.append({"scope": r.scope, "remote": r.slug, "push": r.push, "protected": r.protect,
                        "default_branch_asks": not r.allow_default_branch, "credential": cred_view(c)})
        result: dict[str, Any] = {"workspace": workspace, "remotes": out}
        if remote is not None:
            target = parse_remote(remote)
            match = svc.remotes.find(target, [r.scope for r in rules if r.on_lane] + [workspace])
            c = keymod.pick(creds, target)
            todo = []
            if match is None:
                todo.append(f"agentos git remote allow {workspace} {target.slug}")
            if c is None:
                todo.append(f"agentos git key add --host {target.host} --repo {target.repo}")
            result["check"] = {
                "remote": target.slug, "declared_for": match.scope if match else None,
                "can_fetch": match is not None, "can_push": bool(match and match.push and c is not None),
                "credential": cred_view(c),
                "owner_runs_on_the_node": todo,
            }
        return result

    return [ToolSpec(name="git_remotes", handler=git_remotes, effect=Effect.READ, tags=("git",), lane_arg=None,
                     hidden_without="git")]
