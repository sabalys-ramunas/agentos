"""MCP tools for agent environments: environment_open, environment, environment_close.

They are in the everyday list (the core toolset). Terminal and browser tools called without `on`
then go to the environment (agentos.environments.routing)."""

from __future__ import annotations

import contextlib
from typing import Any

from agentos.core.errors import AgentosError, NotFound, PolicyDenied
from agentos.core.models import Effect
from agentos.core.tools import ToolContext, ToolSpec, current_elicitor, frozen_off, refuse_frozen
from agentos.environments.errors import NoEnvironment
from agentos.environments.manager import EnvironmentManager, binding_of, check_ref, check_title
from agentos.environments.models import Environment

WAIT_MAX_S = 280.0
CALLS_SHOWN = 10


def _mgr(ctx: ToolContext) -> EnvironmentManager:
    return ctx.runtime.require("environments")


def _grant(ctx: ToolContext) -> Any:
    return getattr(ctx, "grant", None)


def _grant_id(ctx: ToolContext) -> str:
    g = _grant(ctx)
    gid = getattr(g, "id", None)
    if gid:
        return str(gid)
    pid = getattr(getattr(ctx, "principal", None), "id", None)
    return f"agent:{pid}" if pid else "owner"


def _family(ctx: ToolContext) -> str | None:
    """The caller's grant family (its root grant), whose environments it reaches. None: the owner
    with no policy on the node, who reaches every one."""
    g = _grant(ctx)
    if g is None:
        return None
    return str(getattr(g, "root_id", None) or _grant_id(ctx))


def _binding(ctx: ToolContext) -> str:
    meta = getattr(ctx, "meta", None)
    return binding_of(getattr(meta, "session", None), _grant_id(ctx))


def _subject(ctx: ToolContext) -> Any:
    g = _grant(ctx)
    try:
        return g.subject if g is not None else None
    except Exception:  # noqa: BLE001 - a grant without a subject block reads as "an agent"
        return None


def _agent(ctx: ToolContext) -> str:
    name = getattr(_subject(ctx), "id", None) or getattr(getattr(ctx, "principal", None), "id", None)
    return str(name) if name and name != "owner" else "an agent"


def _member(ctx: ToolContext) -> str | None:
    """The member whose agent this is (the grant's subject owner), or None for the owner's agents."""
    sub = _subject(ctx)
    owner = getattr(sub, "owner", None)
    if getattr(sub, "kind", None) == "human" or not owner or owner == "owner":
        return None
    return str(owner)


def _client() -> str | None:
    e = current_elicitor()
    return getattr(e, "client", None) if e is not None else None


def _mine(ctx: ToolContext, env: Environment) -> bool:
    """Is it this caller's grant family's? Over MCP nobody else's is, the node's token included."""
    return EnvironmentManager.reaches(env, _grant(ctx), mcp=True)


def _find(ctx: ToolContext, env_id: str | None) -> Environment | None:
    mgr = _mgr(ctx)
    if env_id:
        try:
            env = mgr.get(env_id)
        except (NotFound, ValueError):
            raise NotFound(f"no environment {env_id}") from None
        if not _mine(ctx, env):
            raise NotFound(f"no environment {env_id}")
    else:
        env = mgr.bound(_binding(ctx), _family(ctx))
    if env is not None:
        refuse_frozen(ctx, lanes=env.lanes.values())  # a freeze of its lanes or of `env` holds it as is
    return env


def _price(plan: dict) -> str:
    price = plan.get("price_hourly_eur")
    if price is None:
        return ""
    if price == 0:
        return ", free (a machine on this computer)"
    hours = -(-plan["ttl_minutes"] // 60)
    worst = max([price, *[q or 0 for _n, q in plan.get("fallbacks") or []]])
    return f", EUR {price:.4f} an hour (at most EUR {worst * hours:.2f} for {plan['ttl_minutes']} min)"


def bound_environment(ctx: ToolContext) -> Environment | None:
    """The environment bound to this chat (whoami reads it), or None."""
    return _mgr(ctx).bound(_binding(ctx), _family(ctx))


def _links(ctx: ToolContext, env: Environment) -> dict:
    """`browser`: a signed live-view link the human can open (in a browser or T3 Code's preview
    pane). The node's own viewer, so it works without the web app; before the browser's first page
    it says the browser is not running yet and shows it once it is. None until the browser lane
    exists (environment() gives it then)."""
    links = _mgr(ctx).links(env)
    lanes = getattr(ctx.runtime, "lanes", None)
    if lanes is None or not callable(getattr(lanes, "take_control", None)):
        return links
    try:
        lanes.get(env.browser_lane)  # LaneNotFound: no browser lane yet
        from agentos.node.control_tools import take_over_link

        link = take_over_link(ctx.runtime, env.browser_lane, control=True)
    except Exception:  # noqa: BLE001 - a link is a convenience; the environment works without one
        return links
    if link.get("url"):
        links["browser"] = link["url"]
    return links


def compare_link(env: Environment, branch: str) -> str | None:
    """The GitHub page that opens a pull request for a branch this environment pushed, or None (not on
    GitHub, or the default branch it was opened on)."""
    host, _, repo = (env.repo or "").partition("/")
    if host != "github.com" or repo.count("/") != 1 or not branch or (env.ref is None and branch == env.branch):
        return None
    from urllib.parse import quote

    return f"https://github.com/{repo}/compare/{quote(branch, safe='/._-')}?expand=1"


def _with_compare(env: Environment, pushed: list[dict]) -> tuple[list[dict], str | None]:
    """`pushed` with each branch's compare link, and the newest one's (pushes are newest first)."""
    out, newest = [], None
    for p in pushed:
        link = compare_link(env, str(p.get("branch") or "")) if set(str(p.get("commit") or "")) != {"0"} else None
        out.append({**p, "compare": link} if link else p)
        newest = newest or link
    return out, newest


def _proofs(mgr: EnvironmentManager, env: Environment) -> list[dict]:
    try:
        return mgr.proofs(env)
    except Exception:  # noqa: BLE001 - the environment's state without its proofs rather than none
        return []


def _open_result(ctx: ToolContext, mgr: EnvironmentManager, env: Environment, bigger: list[dict] | None = None
                 ) -> dict:
    s = mgr.summary(env)
    machine = {**env.machine.model_dump(), "price_hourly_eur": env.price_hourly_eur, "bigger": bigger or []}
    if bigger:
        machine["next"] = (f"environment_open(size=\"{bigger[0]['size']}\") in a new chat or after environment_close "
                           "gets a bigger machine")
    out = {"id": env.id, "title": env.title, "state": "ready" if env.state == "ready" else env.state,
           "workdir": env.workdir, "repo": env.repo, "branch": env.branch, "machine": machine,
           "expires_at": s["expires_at"], "lanes": env.lanes, "links": _links(ctx, env)}
    if env.state == "ready":
        out["next"] = ("Give the human links.browser, the live view of this environment's browser; if your client "
                       "can open a page beside the chat (T3 Code: preview_open), open it there. "
                       "lane_file_read, lane_file_edit and lane_file_write work on files (a relative path starts in "
                       f'{env.workdir}); lane_exec(cmd="npm test", cwd="{env.workdir}") runs a command '
                       f'and lane_job_start(cmd="npm run dev", cwd="{env.workdir}") a dev server (lane_job_read reads '
                       "its output). They are in your list, and calls without `on` use this environment; the other "
                       "terminal tools: tools_more(\"terminals\"). "
                       "http://localhost:<port> in browser_navigate and in ui_check (tools_more(\"studio\")) is the "
                       "machine's own port; browser_screenshot(proof=\"...\") keeps a shot with a link for the human. "
                       "Push a branch, give the human environment()'s git.compare link, then environment_close(). "
                       "Nothing here waits for a person, a push to any branch included; a delete or a force "
                       "push of a branch this environment did not create is refused, so push your work to a new "
                       "branch")
    elif env.state == "starting":
        out["next"] = ("still starting (the machine boots and installs its tools): call environment() in a minute; "
                       "it gives links.browser for the human once the browser is up")
    elif env.state == "failed":
        out["error"], out["error_code"] = env.error, env.error_code or "environment_failed"
        if env.error_code == "repo_not_connected":
            out["next"] = ("its machine was deleted because the repository could not be cloned. Give the human the "
                           "fix in `error` word for word; do not retry until they say it is done")
        else:
            out["next"] = ("it did not start and its machine was deleted; open a new one with "
                           "environment_open(title=...)")
    return out


async def environment_open(ctx: ToolContext, *, title: str, repo: str | None = None, ref: str | None = None,
                           size: str | None = None, hours: float | None = None, wait_s: float = 240) -> dict:
    """Your own cloud computer for this chat: a fresh VM with a terminal, a browser and the repo, to
    build and test on instead of the user's laptop. Call it once, before work that runs code or
    serves a page. Then terminal and browser calls without `on` go to it: lane_exec(cmd, cwd),
    lane_job_start for a dev server, browser_navigate("http://localhost:3000"), ui_check (the studio
    toolset); localhost there is the VM. Nothing in it waits for a person, opening and pushes
    included. Commit and `git push origin <branch>` on the VM; then environment_close.

    Returns id, state (ready, or starting: call environment() again), workdir (/work/<name>), branch,
    machine (cpu, memory_gb, size; `bigger`: what each bigger size costs an hour), expires_at, lanes,
    and links.browser (its live view) for the human. The same chat gets the same one back; an open
    one's title gets it back in a new chat (environment_in_use if another chat used it in the last 10
    minutes). Errors: repo_not_connected (a private repo the human has not connected: pass its
    message on), environment_limit (a node limit: close one, fewer hours, a smaller size, or tell the
    user), environment_already_open (this chat has one with another repo), environment_failed (it
    did not come up).

    Args:
        title: A few words people see, like "Black Friday page".
        repo: To clone, like github.com/me/shop. Leave it out for an empty /work.
        ref: A branch, tag or commit (default: the default branch).
        size: small (2 CPU, 4 GB), medium (4, 8), large (8, 16), xlarge (16, 32), or a server type
            like cpx42. Leave it out unless the work needs more (a big type check, a large build).
        hours: How long it may live (default 2); it closes sooner when idle.
        wait_s: Seconds to wait for ready, 0 to 280.
    """
    mgr = _mgr(ctx)
    title = check_title(title)
    ref = check_ref(ref)
    binding = _binding(ctx)
    family = _family(ctx)
    wait = max(0.0, min(float(wait_s), WAIT_MAX_S))
    async with mgr.opening(binding):  # one open at a time in a chat: a second one gets the first's
        env = await _open_locked(ctx, mgr, binding, family, title=title, repo=repo, ref=ref, size=size, hours=hours)
    if env.state == "starting":
        env = await mgr.wait_ready(env.id, wait)
    bigger: list[dict] = []
    if env.state != "failed":
        with contextlib.suppress(Exception):  # a convenience: the environment works without it
            bigger = await mgr.bigger_sizes(env)
    return _open_result(ctx, mgr, env, bigger)


async def _open_locked(ctx: ToolContext, mgr: EnvironmentManager, binding: str, family: str | None, *, title: str,
                       repo: str | None, ref: str | None, size: str | None, hours: float | None) -> Environment:
    if repo:
        from agentos.git.wire import parse_remote

        repo = parse_remote(repo).slug
    # only a live one comes back (starting or ready), and only with the repository asked for
    mine = mgr.live_bound(binding, family)
    found = mgr.same_chat(mine, repo) if mine is not None else await mgr.reattach(binding, title, family, repo)
    if found is not None:
        refuse_frozen(ctx, lanes=found.lanes.values())
        await ctx.authorize(Effect.READ, summary=f"use environment {found.id} ({found.title})")
        return found
    env_id = mgr.planned_id(binding, title)
    plan = await mgr.plan(env_id, size=size, hours=hours)
    mgr.check_limits(plan)
    sp = plan.get("specs") or {}
    specs = f" ({sp.get('cores')} vCPU, {sp.get('memory_gb'):g} GB)" if sp.get("cores") and sp.get("memory_gb") else ""
    summary = (f"Open environment {env_id} \"{title}\": rent a {plan['server_type']} machine{specs} in "
               f"{plan['location']} for up to {plan['ttl_minutes']} min{_price(plan)}, with a terminal and a browser"
               + (f", and clone {repo}" + (f" at {ref}" if ref else "") if repo else "") + ".")
    # Opening one never asks a person (the owner's decision of 2026-11-13, docs/design/environments.md):
    # the node's limits above bound it instead (how many at once, the longest life, the month's budget),
    # and they hold whatever the agent read. The grant must still hold the vm special for this lane
    # (and its VM profiles); the call is then recorded as a read of the lane, with what it rents.
    lane = f"env/{env_id}"
    check = getattr(ctx, "precheck", None)
    if callable(check):
        check(Effect.VM, lane_id=lane, facts={"profile": plan["profile"]})
    if repo:  # after the limits and the grant (both local), before anything is rented: repo_not_connected
        await mgr.check_repo(repo)
    await ctx.authorize(Effect.READ, lane_id=lane, summary=summary,
                        content={"environment_open": title, "repo": repo, "ref": ref,
                                 "server_type": plan["server_type"], "ttl_minutes": plan["ttl_minutes"],
                                 "price_hourly_eur": plan.get("price_hourly_eur"), "image": plan["image"]})
    acted = getattr(ctx, "acted", None)
    if callable(acted):
        acted(Effect.VM)  # the call's record says what it did: it rented a machine
    g = _grant(ctx)
    env = await mgr.create(env_id, plan, binding=binding, title=title, grant_id=_grant_id(ctx),
                           grant_root=getattr(g, "root_id", None), agent=_agent(ctx), client=_client(),
                           repo=repo, ref=ref, subject=getattr(_subject(ctx), "id", None), member=_member(ctx))
    ctx.trace("environment_open", lane=env.terminal_lane, environment=env.id, server_type=plan["server_type"])
    return env


async def environment(ctx: ToolContext, *, id: str | None = None) -> dict:
    """Your environment now: state (starting, ready, working, idle, closed, failed), minutes_left,
    cost_eur, ports listening on the VM (your dev server shows here), git (branch, ahead, dirty,
    pushed; compare: the pull request link of your pushed branch), proofs (with fresh links),
    unsaved work and the last 10 calls. Cheap: call it to see if it is ready, if your server
    listens, or what is left to push. With none in this chat, it lists your grant's open ones by
    title and last call (environment_open with that title gets one back). Effect: read.

    Args:
        id: An environment id of your grant; leave it out for this chat's.
    """
    await ctx.authorize(Effect.READ, summary="read the state of this chat's environment")
    mgr = _mgr(ctx)
    env = _find(ctx, id)
    if env is None:
        mine = [e for e in mgr.open_for_family(_family(ctx))
                if _mine(ctx, e) and not frozen_off(ctx, lanes=e.lanes.values())]
        return {"environment": None,
                "open": [{"id": e.id, "title": e.title, "repo": e.repo, "state": e.shown_state(mgr._clock()),
                          "last_call_at": mgr.summary(e)["last_call_at"]} for e in mine],
                "next": "environment_open(title=...)" + (" with one of these titles gets it back" if mine else "")}
    out = await mgr.detail(env, calls=CALLS_SHOWN)
    out.pop("viewer", None)
    out.pop("commands", None)
    out["links"] = _links(ctx, env)
    git = out.get("git") or {}
    git["pushed"], compare = _with_compare(env, git.get("pushed") or [])
    if compare:
        git["compare"] = compare
    out["proofs"] = _proofs(mgr, env)
    if env.state == "starting":
        out["next"] = "still starting: call environment() again in a minute"
    elif out.get("unsaved"):
        out["next"] = "commit and push before environment_close (git push origin <branch>)"
    elif git.get("compare"):
        out["next"] = ("give the human git.compare (it opens a pull request for your branch) and the proofs' links, "
                       "then environment_close()")
    return out


async def environment_close(ctx: ToolContext, *, id: str | None = None, discard: bool = False) -> dict:
    """Delete your environment when the work is done: its VM and everything on it, and its browser.
    Refused with unsaved_work (which lists them) while changes are not committed or commits not
    pushed: push first, or discard=true if the user agreed to lose them. Returns id, state closed,
    machine ("deleted" once the provider says it is gone; "deleting" while the node still deletes
    it), cost_eur, minutes, pushed (each branch as it is on the remote now, with its compare link),
    deleted (branches it deleted) and your proofs. If you forget, the node closes it after an idle hour
    (never with unsaved work) or at its time limit. Effect: read. Errors: no_environment, unsaved_work.

    Args:
        id: An environment id of your grant; leave it out for this chat's.
        discard: Delete it even with work that is not pushed.
    """
    mgr = _mgr(ctx)
    env = _find(ctx, id)
    if env is None:
        raise NoEnvironment("this chat has no environment to close; environment() lists the open ones of your grant")
    await ctx.authorize(Effect.READ, lane_id=None,
                        summary=f"close environment {env.id} ({env.title})" + (", discarding unsaved work"
                                                                              if discard else ""))
    if not _mine(ctx, env):
        raise PolicyDenied(f"environment {env.id} is not yours")
    try:
        env = await mgr.close(env.id, discard=discard,
                              reason="closed by the agent" + (" (discarded)" if discard else ""))
    except AgentosError as e:  # a refusal is one of its last calls too (environment() lists it)
        mgr.record_call(env.terminal_lane, "environment_close", ok=False, error=e.code,
                        summary="discard" if discard else "")
        raise
    ctx.trace("environment_close", lane=env.terminal_lane, environment=env.id, discard=discard)
    end = env.closed_at or mgr._clock()
    lists = mgr.push_lists(env)
    pushed, _ = _with_compare(env, lists["pushed"])
    return {"id": env.id, "state": env.state, "machine": env.vm_state or "unknown", "cost_eur": env.cost_eur,
            "minutes": int((end - env.created_at).total_seconds() // 60),
            "pushed": pushed, "deleted": lists["deleted"], "proofs": _proofs(mgr, env)}


def tool_specs(runtime: Any) -> list[ToolSpec]:
    t = ("environments",)
    return [
        ToolSpec("environment_open", environment_open, Effect.VM, lane_arg=None, tags=(*t, "changes_state"),
                 hidden_without="environments"),
        ToolSpec("environment", environment, Effect.READ, lane_arg=None, tags=t, hidden_without="environments"),
        ToolSpec("environment_close", environment_close, Effect.READ, lane_arg=None,
                 tags=(*t, "changes_state", "idempotent"), hidden_without="environments"),
    ]
