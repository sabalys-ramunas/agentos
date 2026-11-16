"""MCP tools for agent environments: environment_open, environment, environment_close, environment_save,
environment_forget.

They are in the everyday list (the core toolset). A title names one computer of the agent's grant
family: environment_open with a new title opens one, with an open one's title attaches to it (and says
what differs from what was asked), with a saved one's title resumes it. Terminal and browser tools
called without `on`, and these tools, go to the picked environment (agentos.environments.routing,
EnvironmentManager.pick); `on` names one by its title, its id or env/<id>."""

from __future__ import annotations

import contextlib
from typing import Any

from agentos.core.errors import AgentosError, InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect
from agentos.core.progress import StillWorking
from agentos.core.tools import ToolContext, ToolSpec, current_elicitor, frozen_off, refuse_frozen
from agentos.environments.errors import NoEnvironment, RepoNotConnected
from agentos.environments.manager import EnvironmentManager, binding_of, check_ref, check_title, is_session
from agentos.environments.models import ID_RE, SIZES, WORKSPACE, Environment
from agentos.environments.routing import env_ref

WAIT_MAX_S = 200.0  # a call never runs past about 240 s: a client gives up at 300 (agentos.core.progress)
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


def _find(ctx: ToolContext, on: str | None) -> Environment | None:
    """The environment `on` names (its id, env/<id> or its title), else the picked one: the session's,
    or the family's only open one (which_environment with several). None: none is open."""
    mgr = _mgr(ctx)
    if on:
        ref = " ".join(str(on).split())
        eid = ref.split("/", 1)[1].removesuffix("-browser") if ref.startswith(WORKSPACE + "/") else ref
        env = None
        if ID_RE.match(eid):  # an id names a closed one too (what it cost, what it pushed)
            with contextlib.suppress(NotFound, ValueError):
                env = mgr.get(eid)
            if env is not None and not _mine(ctx, env):
                env = None
        if env is None and "/" not in ref:  # a title (one like "eshop" looks like an id too)
            env = mgr.named(ref, _family(ctx))
        if env is None:
            open_ = [mgr.on_words(e) for e in mgr.open_for_family(_family(ctx)) if _mine(ctx, e)]
            raise NotFound(f"no environment of yours is named \"{ref[:80]}\" (open: {', '.join(open_[:10]) or 'none'}); "
                           "environment() lists the open and saved ones")
    else:
        env = mgr.pick_or_none(_binding(ctx), _family(ctx))
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
    """The environment calls without `on` go to (whoami reads it), or None (none, or several open)."""
    from agentos.environments.errors import WhichEnvironment

    try:
        return _mgr(ctx).pick_or_none(_binding(ctx), _family(ctx))
    except WhichEnvironment:
        return None


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
    """GitHub's form for a pull request of a branch this environment pushed (environment_pull_request
    opens a real one), or None (not on
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


def _keep_next(env: Environment) -> str:
    if env.keep == "save":
        return (". keep is save: when it idles or you close it, the node saves it (disk and files) and deletes the "
                "machine; environment_open with this title resumes it")
    if env.keep == "live":
        return ". keep is live: environment_close() only lets it go (the machine stays); final=true deletes it"
    return ""


def _differs(mgr: EnvironmentManager, env: Environment, *, ref: str | None, size: str | None,
             hours: float | None, keep: str | None) -> tuple[dict, list[str]]:
    """What an environment_open asked for that the environment it got does not have, in words, and
    what it has instead ({} and [] when all of it holds). An attach keeps how it was opened; a
    resume keeps the saved machine and branch."""
    from datetime import timedelta

    applied: dict = {}
    words: list[str] = []
    if keep is not None and keep != env.keep:
        applied["keep"] = env.keep
        words.append(f'keep: you asked for "{keep}", and it stays "{env.keep}" as it was opened')
    if hours is not None:
        wanted = mgr._clock() + timedelta(hours=float(hours))
        if abs((env.expires_at - wanted).total_seconds()) > 600:
            applied["expires_at"] = mgr.summary(env)["expires_at"]
            words.append(f"hours: you asked for {float(hours):g}, and it lives until {applied['expires_at']} as it was "
                         "opened (it closes sooner when idle)")
    if size:
        name = str(size).strip().lower()
        have = env.machine.size if name in SIZES else env.machine.server_type
        if have != name:
            applied["size"] = env.machine.size or env.machine.server_type
            words.append(f"size: you asked for {name}, and it has {env.machine.cpu} CPU and "
                         f"{env.machine.memory_gb:g} GB ({applied['size']})" if env.machine.memory_gb is not None
                         else f"size: you asked for {name}, and it is {applied['size']}")
    if ref is not None and ref not in (env.ref, env.branch):
        applied["ref"] = env.ref or env.branch
        words.append(f"ref: you asked for {ref}, and it was opened at {env.ref or 'the default branch'}; "
                     f"switch with git checkout in {env.workdir}")
    return applied, words


def _on_words(ctx: ToolContext, env: Environment) -> str:
    """How the agent's calls reach this computer. A client with no session id (MCP 2026-07-28, stdio):
    every chat of the agent looks the same, so each call names it."""
    if is_session(_binding(ctx)):
        return ("calls without `on` go here while it is your only open environment, else pass "
                f"on=\"{env.title}\" (or {env.terminal_lane}).")
    return (f"pass on=\"{env.title}\" on every call: this client sends no session id, and other chats of your "
            "agent may have computers too, so a call without `on` is a guess.")


def _with_ssh(mgr: EnvironmentManager, env: Environment, out: dict) -> None:
    """Local mode: the holder's own shell reaches the machine (agentos.environments.sshaccess). Only in
    results for the environment's holder; a server node gives none."""
    ssh = mgr.ssh_of(env)
    if ssh is None:
        return
    out["ssh"] = ssh
    if "next" in out:
        out["next"] += (f". You may also use your own shell: {ssh['command']}, and scp, rsync and git over it "
                        "(ssh.examples); what you run there is not in agentos's call log")


def _open_result(ctx: ToolContext, mgr: EnvironmentManager, env: Environment, bigger: list[dict] | None = None,
                 *, opened: str = "new", applied: dict | None = None, differs: list[str] | None = None) -> dict:
    s = mgr.summary(env)
    machine = {**env.machine.model_dump(), "price_hourly_eur": env.price_hourly_eur, "bigger": bigger or []}
    if bigger:
        machine["next"] = (f"environment_open(title=<a new title>, size=\"{bigger[0]['size']}\") opens a bigger "
                           "machine")
    out = {"id": env.id, "title": env.title, "opened": opened, "state": "ready" if env.state == "ready" else env.state,
           **({} if is_session(_binding(ctx)) else {"on": env.title}),
           "workdir": env.workdir, "repo": env.repo, "branch": env.branch, "machine": machine,
           "expires_at": s["expires_at"], "lanes": env.lanes, "links": _links(ctx, env), "keep": env.keep}
    if differs:
        out["applied"], out["differs"] = applied or {}, differs
    if env.resumed:
        out["resumed"] = env.resumed
    if env.state == "ready":
        out["next"] = ("Give the human links.browser, the live view of this environment's browser; if your client "
                       "can open a page beside the chat (T3 Code: preview_open), open it there. "
                       "lane_file_read, lane_file_edit and lane_file_write work on files (a relative path starts in "
                       f'{env.workdir}); lane_exec(cmd="npm test", cwd="{env.workdir}") runs a command '
                       f'and lane_job_start(cmd="npm run dev", cwd="{env.workdir}") a dev server (lane_job_read reads '
                       "its output). They are in your list; " + _on_words(ctx, env) + " The other "
                       "terminal tools: tools_more(\"terminals\"). "
                       "http://localhost:<port> in browser_navigate and in ui_check (tools_more(\"studio\")) is the "
                       "machine's own port; browser_screenshot(proof=\"...\") keeps a shot with a link for the human. "
                       "Push a branch, environment_pull_request(title) opens its pull request (give the human the url), then "
                       "environment_close(). environment_port(3000) gives the human a link to a dev server. "
                       "Nothing here waits for a person, a push to any branch included; a delete or a force "
                       "push of a branch this environment did not create is refused, so push your work to a new "
                       "branch")
        # code from the agent's own checkout, uncommitted work included (agentos.environments.uploads)
        out["next"] += ('. Code in your own checkout: environment_upload() gives a link and the '
                        "commands that pack and send it")
        if env.resumed:
            out["next"] = ("Resumed: read resumed.note (git, files, services) and resumed.missing first. " + out["next"])
        out["next"] += _keep_next(env)
        _with_ssh(mgr, env, out)
    if opened == "attached":
        words = ("Attached: an environment with this title was already open, so you got it as it is; other "
                 "chats of your agent reach it by this title too. ")
        if differs:
            words += ("Not as you asked: " + "; ".join(differs) + ". For a computer with what you asked, call "
                      "environment_open with another title. ")
        out["next"] = words + out.get("next", "")
    elif differs:
        out["next"] = "Resumed as it was saved. Not as you asked: " + "; ".join(differs) + ". " + out.get("next", "")
    if env.state == "starting" and env.restore_from:
        out["next"] = out.get("next", "") + (f"resuming from save {env.restore_from} (a new machine, then the saved "
                                             "work): call environment() in a minute; it shows resumed.note once it "
                                             "is ready")
    elif env.state == "starting":
        out["next"] = out.get("next", "") + ("still starting (the machine boots and installs its tools): call "
                                             "environment() in a minute; it gives links.browser for the human once "
                                             "the browser is up")
    return out


async def environment_open(ctx: ToolContext, *, title: str | None = None, repo: str | None = None,
                           ref: str | None = None, size: str | None = None, hours: float | None = None,
                           wait_s: float = 240, keep: str | None = None, signins: bool | None = None) -> dict:
    """Your own cloud computer: a VM with a terminal, a browser and the repo, to build and test on
    instead of the user's laptop. The title names it: a new title opens one, an open one's title
    attaches to it (your other chats reach it by title too), a saved one's resumes it (`resumed`).
    Without a title, repo finds that repo's environment. Calls without `on` go to it while it is your
    only open one; else pass on="<title>". localhost there is the VM. Nothing in it waits for a person.

    Returns id, title, opened (new, attached, resumed), state (starting: call environment()),
    workdir, branch, machine (`bigger`: sizes and prices), expires_at, keep, links.browser (its live
    view, for the human); applied and differs when an attach or resume is not what you asked. A
    private repo the node cannot read: approval_pending with review_url for the human. Errors:
    environment_limit, environment_already_open (open with another repo: pick another title).

    Args:
        title: A few words people see, like "Black Friday page". It names the computer. Without it,
            repo finds that repo's environment (several: a list to pick from).
        repo: To clone, like github.com/me/shop. Leave it out for an empty /work.
        ref: A branch, tag or commit (default: the default branch).
        size: small (2 CPU, 4 GB), medium (4, 8), large (8, 16), xlarge (16, 32), or a server type
            like cpx42. Only when the work needs more.
        hours: How long it may live (default 2); it closes sooner when idle.
        wait_s: Seconds to wait for ready, 0 to 200.
        keep: none (deleted when done), save (saved when idle or closed; its title resumes it) or live
            (kept running; the owner allows it).
        signins: false: saves keep no browser sign-ins.
    """
    mgr = _mgr(ctx)
    title = check_title(title if title else title_for_repo(ctx, mgr, repo))
    ref = check_ref(ref)
    binding = _binding(ctx)
    family = _family(ctx)
    wait = max(0.0, min(float(wait_s), WAIT_MAX_S))
    if repo:
        from agentos.git.wire import parse_remote

        repo = parse_remote(repo).slug
    # the call's own argument errors first: a size past the limit is never hidden by an attach
    await mgr.check_open_args(size=size, hours=hours, keep=keep)
    async with mgr.opening(family, title):  # one open at a time per title: a second one attaches to the first
        got = await _open_locked(ctx, mgr, binding, family, title=title, repo=repo, ref=ref, size=size, hours=hours,
                                 keep=keep, signins=signins)
    if isinstance(got, dict):  # a card for the human to connect the repository: nothing was rented
        return got
    env, opened = got
    applied, differs = ({}, []) if opened == "new" else _differs(
        mgr, env, ref=ref, size=size, hours=hours if opened == "attached" else None,
        keep=keep if opened == "attached" else None)
    if env.state == "starting":
        env = await mgr.wait_ready(env.id, wait)
    if env.state == "failed":  # an error, never a result that looks like an open (environment() lists it)
        from agentos.environments.failures import error_for

        raise error_for(env)
    bigger: list[dict] = []
    with contextlib.suppress(Exception):  # a convenience: the environment works without it
        bigger = await mgr.bigger_sizes(env)
    return _open_result(ctx, mgr, env, bigger, opened=opened, applied=applied, differs=differs)


async def _open_locked(ctx: ToolContext, mgr: EnvironmentManager, binding: str, family: str | None, *, title: str,
                       repo: str | None, ref: str | None, size: str | None, hours: float | None,
                       keep: str | None = None, signins: bool | None = None) -> tuple[Environment, str] | dict:
    # the open one with this title (live: starting or ready), and only with the repository asked for
    found = await mgr.reattach(binding, title, family, repo)
    if found is not None:
        refuse_frozen(ctx, lanes=found.lanes.values())
        await ctx.authorize(Effect.READ, summary=f"use environment {found.id} ({found.title})")
        return found, "attached"
    env_id = mgr.planned_id(binding, title)
    saved = mgr.save_to_resume(family or _grant_id(ctx), title, repo)
    if saved is not None:  # this family saved this title: resume it (its repository, branch and machine)
        keep = mgr.check_keep(keep if keep is not None else saved.keep)
        repo, ref = saved.repo, saved.ref
        plan = await mgr.plan_restore(env_id, saved, hours=hours, keep=keep)
    else:
        keep = mgr.check_keep(keep)
        plan = await mgr.plan(env_id, size=size, hours=hours, keep=keep)
    mgr.check_limits(plan)
    sp = plan.get("specs") or {}
    specs = f" ({sp.get('cores')} vCPU, {sp.get('memory_gb'):g} GB)" if sp.get("cores") and sp.get("memory_gb") else ""
    summary = (("Resume" if saved is not None else "Open") +
               f" environment {env_id} \"{title}\": rent a {plan['server_type']} machine{specs} in "
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
        from agentos.environments import connect

        with contextlib.suppress(Exception):  # a card this agent raised for another repo: a name it got wrong
            connect.withdraw(mgr.store, ctx.runtime.get("approvals"), _grant_id(ctx), instead=repo)
        try:
            await mgr.check_repo(repo)
        except RepoNotConnected as e:  # a card the human answers in the panel (agentos.environments.connect)
            from agentos.environments.connect import card_result

            card = await card_result(ctx, mgr, repo, e)
            if card is None:
                raise
            return card
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
                           repo=repo, ref=ref, subject=getattr(_subject(ctx), "id", None), member=_member(ctx),
                           keep=keep, resumed=mgr.resuming(saved, plan) if saved is not None else None)
    keep_signins = signins if signins is not None else (saved.keep_signins if saved is not None else True)
    if not keep_signins:  # before its bring-up runs: a resume then puts no sign-ins back either
        env = mgr.store.update(env.id, lambda e: setattr(e, "keep_signins", False))
    ctx.trace("environment_open", lane=env.terminal_lane, environment=env.id, server_type=plan["server_type"])
    if env.id != env_id:  # another process opened this title meanwhile: that one
        return env, "attached"
    return env, "resumed" if saved is not None else "new"


def title_for_repo(ctx: ToolContext, mgr: EnvironmentManager, repo: str | None) -> str:
    """environment_open's title when it names none: this chat's environment's, else the one environment
    of the caller's family for `repo`, open or saved (agentos.environments.comeback.by_repo), else the
    repository's name for a new one. Raises environment_choice with several."""
    from agentos.environments import comeback

    # a client with a session id: its own computer; without one, every chat looks the same, so only the repo
    mine = mgr.live_bound(_binding(ctx), _family(ctx)) if is_session(_binding(ctx)) else None
    if mine is not None:
        return mine.title
    if not repo:
        from agentos.core.errors import InvalidRequest

        raise InvalidRequest('environment_open needs a title, like "Black Friday page", or a repo to find its '
                             "environment by: environment() lists the open and saved ones")
    from agentos.git.wire import parse_remote

    return comeback.by_repo(mgr, _family(ctx), parse_remote(repo).slug, saves_of=_family(ctx) or _grant_id(ctx),
                            reaches=lambda e: _mine(ctx, e) and not frozen_off(ctx, lanes=e.lanes.values()))


def _family_lists(ctx: ToolContext, mgr: EnvironmentManager, here: Environment | None = None) -> dict:
    """The caller's family's open environments (`this_chat` on the one bound here) and newest saves."""
    mine = [e for e in mgr.open_for_family(_family(ctx))
            if _mine(ctx, e) and not frozen_off(ctx, lanes=e.lanes.values())]
    saved = [{k: r[k] for k in ("title", "repo", "branch", "saved_at", "snapshot_gb", "cost_eur_month", "note")}
             for r in mgr.saved_for(_family(ctx) or _grant_id(ctx)) if r["latest"]]
    open_ = []
    for e in mine:
        row = {"id": e.id, "title": e.title, "on": e.terminal_lane, "repo": e.repo, "state": e.shown_state(mgr._clock()),
               "last_call_at": mgr.summary(e)["last_call_at"]}
        if here is not None and e.id == here.id and is_session(_binding(ctx)) and e.binding == _binding(ctx):
            row["this_chat"] = True  # only with a real session: without one, every chat of the agent looks alike
        open_.append(row)
    from agentos.environments.failures import failed_rows

    out: dict = {"open": open_, "saved": saved}
    failed = failed_rows(mgr, _family(ctx), lambda e: _mine(ctx, e))
    if failed:
        out["failed"] = failed  # opens that did not start in the last hour, with why (environment_open raised it)
    return out


async def environment(ctx: ToolContext, *, on: str | None = None) -> dict:
    """Your environment now: state, minutes_left, cost_eur, ports listening on the VM, git (branch,
    ahead, dirty, pushed, pull_requests), proofs (fresh links), unsaved work, your note and the last
    10 calls (with the client that made each). Cheap: call it to see if it is ready or what is left to
    push. It always lists your agent's open and saved ones (`open`, `saved`; environment_open with a
    title gets one back). Effect: read.

    Args:
        on: Which one: its title, its id or env/<id>. Leave it out when you have one open.
    """
    await ctx.authorize(Effect.READ, summary="read the state of an environment")
    mgr = _mgr(ctx)
    env = _find(ctx, on)
    if env is None:
        lists = _family_lists(ctx, mgr)
        return {"environment": None, **lists,
                "next": "environment_open(title=...)" + (" with one of these titles gets it back; a new title opens "
                                                         "your own" if lists["open"] or lists["saved"] else "")}
    out = await mgr.detail(env, calls=CALLS_SHOWN)
    out.pop("viewer", None)
    out.pop("commands", None)
    out["links"] = _links(ctx, env)
    git = out.get("git") or {}
    git["pushed"], compare = _with_compare(env, git.get("pushed") or [])
    if compare:
        git["compare"] = compare
    out["proofs"] = _proofs(mgr, env)
    if env.pull_requests:
        git["pull_requests"] = env.pull_requests
    if env.resumed:
        out["resumed"] = env.resumed
    out["note"] = env.note
    out.update(_family_lists(ctx, mgr, env))
    if (ssh := mgr.ssh_of(env)) is not None:
        out["ssh"] = ssh
    if not on and not is_session(_binding(ctx)):  # picked as the agent's only open one: maybe another chat's
        out["on"] = env.title
        out["picked"] = (f"this client sends no session id, so this is your agent's only open computer, which "
                         f"another chat of yours may have opened. If you opened it, pass on=\"{env.title}\" on "
                         "every call; else open your own with environment_open(title=...)")
    if env.state == "starting":
        out["next"] = "still starting: call environment() again in a minute"
    elif out.get("unsaved") and env.keep == "none":
        out["next"] = "commit and push before environment_close (git push origin <branch>)"
    elif git.get("compare") and not env.pull_requests:
        out["next"] = ("environment_pull_request(title) opens the pull request for your pushed branch and returns "
                       "its url (git.compare is only GitHub's form); give the human that url and the proofs' links, "
                       "then environment_close()")
    return out


async def environment_close(ctx: ToolContext, *, on: str | None = None, discard: bool = False,
                            final: bool = False, note: str | None = None, forget: bool = False) -> dict:
    """Stop or finish your environment. keep save: saved (`saved`; `pause` says what it costs and how
    fast it resumes), then the VM is deleted; its title resumes it. keep live: only lets it go. keep
    none: deletes the VM, refused with unsaved_work while work is not pushed (push, or discard=true if
    the user agreed). Returns id, title, state, machine (deleted, or deleting), cost_eur, pushed, proofs
    and `saves` (what became of them). A long save goes on in the background (state closing).
    Effect: read. Errors: no_environment, unsaved_work, save_failed.

    Args:
        on: Which one: its title, its id or env/<id>. Leave it out when you have one open.
        discard: Delete it even with work that is not pushed (no save).
        final: keep live: delete it too.
        note: keep save: what you did and what is next, for the next chat (resumed.note).
        forget: Done for good: the VM and every save of its title go.
    """
    mgr = _mgr(ctx)
    if (discard or final) and not on and not is_session(_binding(ctx)):
        # every chat of a client with no session id looks the same: deleting work needs the computer named
        raise InvalidRequest("discard and final delete work for good, so name the environment: "
                             "environment_close(on=\"<its title>\", discard=true). Only for one you opened and "
                             "the human agreed to lose; environment() shows what it has")
    env = _find(ctx, on)
    if env is None:
        raise NoEnvironment("no environment is open to close; environment() lists the open and saved ones")
    what = ", forgetting its saves" if forget else (", discarding unsaved work" if discard else "")
    await ctx.authorize(Effect.READ, lane_id=None, summary=f"close environment {env.id} ({env.title}){what}")
    if not _mine(ctx, env):
        raise PolicyDenied(f"environment {env.id} is not yours")
    if note is not None:
        from agentos.environments.saves import NOTE_MAX

        env = mgr.store.update(env.id, lambda e: setattr(e, "note", note[:NOTE_MAX] or None))
    if forget and not discard and env.state == "ready":
        await _refuse_unsaved(mgr, env, "forget=true deletes every save too")
    try:
        env = await mgr.close_bounded(env.id, discard=discard or forget, final=final or forget,
                                      reason="closed by the agent" + (" (forgotten)" if forget else
                                                                      " (discarded)" if discard else ""))
    except StillWorking as still:  # a save with a snapshot goes on in the background
        return still.result
    except AgentosError as e:  # a refusal is one of its last calls too (environment() lists it)
        mgr.record_call(env.terminal_lane, "environment_close", ok=False, error=e.code,
                        summary="forget" if forget else "discard" if discard else "")
        raise
    ctx.trace("environment_close", lane=env.terminal_lane, environment=env.id, discard=discard, forget=forget)
    if env.open and env.keep == "live":
        price = env.price_hourly_eur
        return {"id": env.id, "title": env.title, "state": env.state, "keep": "live", "machine": "kept",
                "cost_eur": mgr.cost_now(env),
                "pause": "kept running: it bills" + (f" EUR {price:.4f} an hour" if price else "") + " until a final close",
                "next": (f"this chat let it go and its machine stays (it bills). environment_open(title=\"{env.title}\") "
                         "gets it back in any chat; environment_close(final=true) deletes it")}
    end = env.closed_at or mgr._clock()
    lists = mgr.push_lists(env)
    pushed, _ = _with_compare(env, lists["pushed"])
    out = {"id": env.id, "title": env.title, "state": env.state, "machine": env.vm_state or "unknown",
           "cost_eur": env.cost_eur,
           "minutes": int((end - env.created_at).total_seconds() // 60),
           "pushed": pushed, "deleted": lists["deleted"], "proofs": _proofs(mgr, env)}
    if forget:
        try:
            gone = await mgr.forget(env.family, env.title)
        except NotFound:
            gone = {"forgot": [], "snapshots_deleted": 0}
        out["saves"] = {"made": None, **gone}
        out["next"] = (f"done for good: the machine is {out['machine']} and no save of \"{env.title}\" is left. "
                       f"environment_open(title=\"{env.title}\") would open a new one")
        return out
    if env.keep == "save" and not discard and env.last_save_id:
        out["keep"] = "save"
        out["saved"] = _saved_result(mgr, env.last_save_id)
        out["pause"] = _pause(mgr, out["saved"])
        out["saves"] = {"made": env.last_save_id}
        out["next"] = (f"saved as {env.last_save_id}, and the machine is {out['machine']}. {out['pause']}. "
                       f"Tell the human: say \"continue {env.title} in agentos\"; environment_open(title=\"{env.title}\") "
                       "resumes it in any chat of your agent")
        return out
    left = [{"id": r["id"], "saved_at": r["saved_at"], "snapshot_gb": r["snapshot_gb"],
             "cost_eur_month": r["cost_eur_month"]}
            for r in mgr.saved_for(env.family) if r["title"].lower() == env.title.lower()]
    out["saves"] = {"made": None, "left": left}
    if left:
        out["next"] = (f"deleted without a save{' (discard)' if discard else ''}. {len(left)} earlier "
                       f"save{'s' if len(left) != 1 else ''} of \"{env.title}\" stay, and environment_open with this "
                       f"title resumes the newest; environment_forget(title=\"{env.title}\") removes "
                       f"{'them' if len(left) != 1 else 'it'}")
    return out


async def _refuse_unsaved(mgr: EnvironmentManager, env: Environment, why: str) -> None:
    """forget=true without discard: the work that is not pushed would be lost, so it is refused as a close is."""
    from agentos.environments.errors import UnsavedWork

    repos = await mgr.git_repos(env)
    unsaved = mgr.unsaved_of(repos) if repos is not None else None
    if unsaved is not None:
        raise UnsavedWork(f"environment {env.id} has work that is not pushed: {mgr.unsaved_words(unsaved)}. "
                          f"{why}: push it first, or pass discard=true as well if the user agreed to lose it",
                          unsaved)


def _pause(mgr: EnvironmentManager, saved: dict) -> str:
    from agentos.environments import comeback

    snap = saved.get("snapshot") or None
    gb = (snap or {}).get("gb")
    cost = round(gb * mgr.settings.snapshot_eur_per_gb_month, 4) if gb else 0.0
    provider = getattr(mgr.vms().provider(), "name", "") if snap else ""
    return comeback.pause_words(cost, snapshot=snap is not None, provider=provider)


def _saved_result(mgr: EnvironmentManager, save_id: str) -> dict:
    """What environment_save returns, and environment_close's `saved`."""
    from agentos.core import clock

    m, _ = mgr.saves().find(save_id)
    snap = {"id": m.snapshot.id, "gb": m.snapshot.gb} if m.snapshot is not None else None
    return {"save_id": m.id, "saved_at": clock.iso_or_none(m.saved_at, z=True), "snapshot": snap, "files": m.files,
            "bytes": m.bytes, "commits": m.commits, "verified": True}


async def environment_save(ctx: ToolContext, *, note: str | None = None, on: str | None = None) -> dict:
    """Save the environment now, without closing it: commits and stashes no remote has, changed and
    new files git does not ignore, running services, and with keep "save" a disk snapshot (services
    pause for it). environment_open with its title resumes the newest save in any chat. Returns
    save_id, saved_at, snapshot, files, bytes, verified. Effect: read. Errors: save_failed (nothing
    was lost: the machine works on), save_limit.

    Args:
        note: Your own words for the next chat that resumes it: what you were doing and what is next.
        on: Which one: its title, its id or env/<id>. Leave it out when you have one open.
    """
    mgr = _mgr(ctx)
    env = _find(ctx, on)
    if env is None:
        raise NoEnvironment("no environment is open to save; environment() lists the open and saved ones")
    await ctx.authorize(Effect.READ, lane_id=None, summary=f"save environment {env.id} ({env.title})")
    try:
        m = await mgr.save_bounded(env, kind="save", reason="saved by the agent", note=note)
    except StillWorking as still:
        return still.result
    except AgentosError as e:
        mgr.record_call(env.terminal_lane, "environment_save", ok=False, error=e.code)
        raise
    mgr.record_call(env.terminal_lane, "environment_save", ok=True, summary=m.id)
    ctx.trace("environment_save", lane=env.terminal_lane, environment=env.id, save=m.id)
    out = {**_saved_result(mgr, m.id), "environment": env_ref(env)}
    if out.get("snapshot"):
        out["pause"] = _pause(mgr, out)
    out["next"] = (f"saved. Work on; environment_open(title=\"{env.title}\") resumes the newest save in a new chat"
                   + (" once this one is closed" if env.keep != "save" else ""))
    return out


async def environment_forget(ctx: ToolContext, *, title: str, save_id: str | None = None) -> dict:
    """Delete saves and their disk snapshots (they bill each month): every save of `title`, or one
    save_id. Its title then opens a new environment. Returns forgot and snapshots_deleted. Effect: read.

    Args:
        title: The environment's title, as environment() lists it under saved.
        save_id: Only this save (s and 10 letters or digits).
    """
    mgr = _mgr(ctx)
    title = check_title(title)
    family = _family(ctx) or _grant_id(ctx)
    # a freeze of an environment's lanes (or of `env`) holds its saves as they are
    refuse_frozen(ctx, lanes=[f"{WORKSPACE}/{m.env_id}" for m in mgr.saves().of_title(family, title)])
    await ctx.authorize(Effect.READ, lane_id=None, summary=f"forget the saves of \"{title}\""
                        + (f" ({save_id})" if save_id else ""))
    out = await mgr.forget(family, title, save_id)
    ctx.trace("environment_forget", title=title, forgot=out["forgot"])
    out["next"] = f"forgotten. environment_open(title=\"{title}\") now opens a new environment"
    return out


async def environment_port(ctx: ToolContext, *, port: int, on: str | None = None) -> dict:
    """Open a dev server of your environment on the human's own computer, as a preview link. The
    VM's port is forwarded over the environment's own SSH connection to a free 127.0.0.1 port; it
    closes with the environment. Returns port, local_port and url (on agentos local the human opens it
    as is; on a server node they first run `tunnel`, the ssh -L line). environment()'s ports show it
    as `link`. Effect: read. Errors: no_environment, environment_not_ready.

    Args:
        port: The port your server listens on in the VM, like 3000 or 5173 (environment() lists them).
        on: Which environment: its title, its id or env/<id>. Leave it out when you have one open.
    """
    from agentos.environments import ports as env_ports

    mgr = _mgr(ctx)
    env = _find(ctx, on)
    if env is None:
        raise NoEnvironment("this chat has no environment; environment_open first, then start your server")
    await ctx.authorize(Effect.READ, lane_id=None, summary=f"forward port {port} of environment {env.id} ({env.title})")
    out = await env_ports.forward(mgr, env, port)
    listening = {p.get("port") for p in await mgr.ports(env)}
    mgr.record_call(env.terminal_lane, "environment_port", ok=True, summary=str(port))
    ctx.trace("environment_port", lane=env.terminal_lane, environment=env.id, port=port, local=out["local_port"])
    first = f"run `{out['tunnel']}` on their computer, then " if "tunnel" in out else ""
    out["listening"] = port in listening
    out["next"] = (f"give the human the url: they {first}open {out['url']} in their own browser"
                   + ("" if out["listening"] else f". Nothing listens on port {port} yet: start the server "
                      "(lane_job_start, with --host 0.0.0.0 or 127.0.0.1) and the link works once it does"))
    return out


async def environment_pull_request(ctx: ToolContext, *, title: str, body: str | None = None,
                                   base: str | None = None, draft: bool = False,
                                   branch: str | None = None, on: str | None = None) -> dict:
    """Open a pull request on GitHub for the branch your environment pushed, with the node's stored
    GitHub token (the VM has no gh and no credential). Only a branch this environment created; it
    never merges. If one is open for that branch already, it returns that one. Returns url (give it
    to the human), number, branch, base, draft and created. Effect: read. Errors: no_environment,
    denied (not a branch it pushed), pull_request_failed (no token on the node, or GitHub refused).

    Args:
        title: The pull request's title.
        body: Its description (Markdown): what changed, how it was tested, proof links.
        base: The branch to merge into; the repository's default branch if left out.
        draft: Open it as a draft.
        branch: Which pushed branch; the newest one this environment pushed if left out.
        on: Which environment: its title, its id or env/<id>. Leave it out when you have one open.
    """
    from agentos.environments import pulls

    mgr = _mgr(ctx)
    env = _find(ctx, on)
    if env is None:
        raise NoEnvironment("this chat has no environment; environment_open first, push a branch, then call this")
    await ctx.authorize(Effect.READ, lane_id=None,
                        summary=f"open a pull request for environment {env.id}'s branch on {env.repo}")
    try:
        out = await pulls.open_pull_request(mgr, env, title=title, body=body, base=base, draft=draft, branch=branch)
    except AgentosError as e:
        mgr.record_call(env.terminal_lane, "environment_pull_request", ok=False, error=e.code)
        raise
    row = {"url": out["url"], "number": out["number"], "branch": out["branch"], "base": out["base"]}

    def add(e: Environment) -> None:
        e.pull_requests = [p for p in e.pull_requests if p.get("url") != row["url"]][-9:] + [row]

    mgr.store.update(env.id, add)
    mgr.record_call(env.terminal_lane, "environment_pull_request", ok=True, summary=str(out["url"]))
    ctx.trace("environment_pull_request", lane=env.terminal_lane, environment=env.id, url=out["url"])
    out["next"] = (("opened. " if out["created"] else "it was open already. ") +
                   f"Give the human {out['url']}; do not delete the branch {out['branch']} while it is open")
    return out


def tool_specs(runtime: Any) -> list[ToolSpec]:
    t = ("environments",)
    return [
        ToolSpec("environment_open", environment_open, Effect.VM, lane_arg=None, tags=(*t, "changes_state"),
                 hidden_without="environments"),
        ToolSpec("environment", environment, Effect.READ, lane_arg=None, tags=t, hidden_without="environments"),
        ToolSpec("environment_close", environment_close, Effect.READ, lane_arg=None,
                 tags=(*t, "changes_state", "idempotent"), hidden_without="environments"),
        ToolSpec("environment_save", environment_save, Effect.READ, lane_arg=None, tags=(*t, "changes_state"),
                 hidden_without="environments"),
        ToolSpec("environment_forget", environment_forget, Effect.READ, lane_arg=None,
                 tags=(*t, "changes_state", "idempotent"), hidden_without="environments"),
        ToolSpec("environment_port", environment_port, Effect.READ, lane_arg=None, tags=(*t, "idempotent"),
                 hidden_without="environments"),
        ToolSpec("environment_pull_request", environment_pull_request, Effect.READ, lane_arg=None,
                 tags=(*t, "changes_state", "idempotent"), hidden_without="environments"),
    ]
