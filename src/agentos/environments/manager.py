"""EnvironmentManager: an agent's own cloud computer (runtime service "environments").

An environment is a VM from agentos.vms (its terminal lane is `env/<id>`), a scratch browser lane on
the node (`env/<id>-browser`) whose traffic leaves from the VM through an SSH SOCKS tunnel, the
node's git gateway reached from the VM through a reverse tunnel, and optionally a repository
cloned into /work/<name>. One per chat: it is bound to the MCP session that opened it (or, with no
session, to the grant), and terminal and browser calls without `on` go to it (agentos.environments
.routing).

Opening: plan (the VM the node would rent, the cheapest that fits), the node's limits (check_limits:
open at once, the longest life, the month's budget; opening never asks a person) and the caller's
`vm` special, then the VM, its setup (agentos.environments.machine), the tunnels, the browser lane and the clone.
The environment is `starting` until all of that is done, then `ready`.

Closing: refused while the repository has changes that are not committed or commits that are not
pushed (unsaved_work), unless the caller discards them; then the tunnels, the browser lane and the
VM go. The reaper closes an environment idle for `idle_minutes` (never one with unsaved work: the
owner is told and it lives to its time limit) and one past its time limit, and marks one whose VM
is gone. A restart of the node keeps every environment: the reaper starts their tunnels again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import string
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentos.core import clock
from agentos.core.errors import AgentosError, InvalidRequest, LaneExists, NotFound
from agentos.core.models import LaneState, ProxyConfig, utcnow
from agentos.environments.bringup import BringUp
from agentos.environments.errors import (
    EnvironmentFailed,
    EnvironmentInUse,
    EnvironmentLimit,
    NoEnvironment,
    UnsavedWork,
)
from agentos.environments.models import (
    CALLS_KEPT,
    HANDOFFS_KEPT,
    LIVE_STATES,
    QUIET,
    WORKSPACE,
    Call,
    Environment,
    EnvSettings,
    Handoff,
    Machine,
    Push,
    load_settings,
)
from agentos.environments.sizing import Sizing
from agentos.environments.store import EnvironmentStore
from agentos.environments.tunnels import Tunnel
from agentos.environments.views import Views

log = logging.getLogger("agentos.environments")

TITLE_MAX = 80
SUMMARY_MAX = 200
OPEN_NEXT = "environment_open(title=...)"
# Ids are e + ID_LENGTH characters people can read aloud (no 0/o, 1/l/i): 31^10, about 8e14, so one
# cannot be guessed, and a lane name (env/<id>-browser) stays short.
_ID_CHARS = "".join(c for c in string.ascii_lowercase + string.digits if c not in "0o1li")
ID_LENGTH = 10
OPENS_LISTED = 10
OPENING_KEPT = 256


def _when(dt: datetime) -> str:
    """A time in a message for an agent: 2026-11-13 16:47 UTC."""
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def new_id() -> str:
    return "e" + "".join(secrets.choice(_ID_CHARS) for _ in range(ID_LENGTH))


def check_title(title: str) -> str:
    t = " ".join(str(title or "").split())
    if not t:
        raise InvalidRequest('give the environment a short title people will see, like "Black Friday page"')
    if len(t) > TITLE_MAX:
        raise InvalidRequest(f"the title is at most {TITLE_MAX} characters")
    return t


def check_ref(ref: str | None) -> str | None:
    if ref is None or ref == "":
        return None
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$", ref) or ".." in ref:
        raise InvalidRequest("ref is a branch, tag or commit, like main or v1.2.0")
    return ref


class EnvironmentManager(Sizing, BringUp, Views):
    """The environments service. Its sizing and cost (Sizing), its bring-up (BringUp) and its read side
    (Views) are mixins in their own modules; lookups, opening, closing and the reaper are here."""

    def __init__(self, home: Path, *, runtime: Any = None, settings: EnvSettings | None = None, clock=utcnow):
        self.home = Path(home)
        self.runtime = runtime
        self.settings = settings or load_settings(self.home)
        self.store = EnvironmentStore(self.home / "environments.db")
        self._clock = clock
        self._tunnels: dict[str, Tunnel] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._planned: dict[tuple[str, str], str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._opening: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None
        self._loader_wrapped = False
        self.last_reap: dict = {}
        self.repo_probe: Any = None  # how check_repo reads a repository (None: `git ls-remote`; tests set one)

    # ================================================================ wiring
    def _get(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    def vms(self) -> Any:
        vms = self._get("vms")
        if vms is None:
            raise EnvironmentFailed("this node has no VMs (agentos.vms), so it cannot open environments")
        return vms

    def envs(self) -> Any:
        envs = self._get("envs")
        if envs is None:
            raise EnvironmentFailed("this node has no terminal lanes (agentos.env), so it cannot open environments")
        return envs

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def proxies(self) -> dict[str, ProxyConfig]:
        """The SOCKS proxy of each open environment's tunnel, by the label its browser lane names."""
        return {f"env-{eid}": ProxyConfig(label=f"env-{eid}", host="127.0.0.1", port=t.socks_port, scheme="socks5",
                                           check_exit=False)
                for eid, t in self._tunnels.items()}

    def proxy_for(self, env: Environment) -> ProxyConfig | None:
        return self.proxies().get(f"env-{env.id}")

    def live_proxy_for(self, env: Environment) -> ProxyConfig | None:
        """proxy_for, only while its tunnel is up (studio's checks must never fall back to the node)."""
        tunnel = self._tunnels.get(env.id)
        return self.proxy_for(env) if tunnel is not None and tunnel.alive else None

    def _wrap_proxies_loader(self) -> None:
        """Let the lane manager find each environment's tunnel as a proxy label (`env-<id>`), next to
        the owner's own proxies. Nothing is written to the owner's proxies file."""
        lanes = self._get("lanes")
        if self._loader_wrapped or lanes is None or not callable(getattr(lanes, "proxies_loader", None)):
            return
        own = lanes.proxies_loader

        def loader() -> dict[str, ProxyConfig]:
            return {**own(), **self.proxies()}

        lanes.proxies_loader = loader
        self._loader_wrapped = True

    # ================================================================ lifecycle
    async def start(self) -> None:
        self._wrap_proxies_loader()
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop(), name="environment-reaper")

    async def shutdown(self) -> None:
        """Stop the reaper, the bring-ups and the tunnels. The VMs stay: a restart of the node must
        not end an agent's work. The next start brings the tunnels back."""
        tasks = [t for t in (self._reaper, *self._tasks.values()) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._reaper = None
        self._tasks.clear()
        for tunnel in list(self._tunnels.values()):
            await tunnel.stop()
        self._tunnels.clear()

    async def stop(self) -> None:
        await self.shutdown()

    async def _reap_loop(self) -> None:
        await asyncio.sleep(1)
        while True:
            try:
                self.last_reap = await self.reap_once()
            except Exception as e:  # noqa: BLE001
                log.warning("environment reaper: %s", getattr(e, "message", e))
                self.last_reap = {"at": clock.iso_or_none(self._clock(), z=True), "error": str(getattr(e, "message", e))}
            await asyncio.sleep(self.settings.reap_interval_s)

    # ================================================================ lookups
    def get(self, env_id: str) -> Environment:
        return self.store.get(env_id)

    def bound(self, binding: str, family: str | None = None) -> Environment | None:
        """The open environment bound to this chat; with `family`, only one of that grant family (a
        session id another agent sends gets nothing of someone else's)."""
        env = self.store.bound(binding)
        if env is not None and family is not None and env.family != family:
            return None
        return env

    def open_for_family(self, family: str | None) -> list[Environment]:
        """The open environments of a grant family (None: every one, for the owner with no policy)."""
        return [e for e in self.store.open() if family is None or e.family == family]

    def env_of_lane(self, lane_id: str) -> tuple[Environment, str] | None:
        """(environment, "terminal" | "browser") for any environment's lane, open or not, else None."""
        if not isinstance(lane_id, str) or not lane_id.startswith(WORKSPACE + "/"):
            return None
        name = lane_id.split("/", 1)[1]
        kind = "browser" if name.endswith("-browser") else "terminal"
        eid = name.removesuffix("-browser")
        try:
            env = self.store.get(eid)
        except (NotFound, ValueError):
            return None
        return env, kind

    def lane_env(self, lane_id: str) -> tuple[Environment, str] | None:
        """(environment, "terminal" | "browser") for an open environment's lane, else None."""
        found = self.env_of_lane(lane_id)
        return found if found is not None and found[0].open else None

    @staticmethod
    def reaches(env: Environment, grant: Any, *, mcp: bool) -> bool:
        """May this grant act on, read or close this environment? Its own grant family (the grant
        that opened it, and every grant narrowed from the same root: a brain or a routine of that
        agent). Besides them only a person outside MCP: the owner (the web app, the CLI, the node's
        token or an admin grant over the API), or the member whose agent opened it."""
        if grant is None:
            return True  # no policy on this node: every caller is the owner
        if getattr(grant, "root_id", None) == env.family or env.grant_id in (getattr(grant, "chain_ids", None) or ()):
            return True
        if mcp:
            return False
        if getattr(grant, "owner_token", False) or getattr(grant, "admin", False):
            return True
        try:
            sub = grant.subject
        except Exception:  # noqa: BLE001 - a grant without a subject is nobody's
            return False
        return getattr(sub, "kind", None) == "human" and env.member is not None and getattr(sub, "id", None) == env.member

    def may_reach(self, lane_id: str, grant: Any, *, mcp: bool) -> bool:
        """The policy asks this for every lane a call names (agentos.policy.service): False for an
        environment's lane outside the caller's family, which it then refuses as not found. Any other
        lane: True (the usual rules hold)."""
        found = self.env_of_lane(lane_id)
        return found is None or self.reaches(found[0], grant, mcp=mcp)

    def holds_terminal(self, lane_id: str) -> bool:
        """Is this the terminal lane of an open environment? The git gateway reads it: a VM's token
        dies with its environment."""
        found = self.lane_env(lane_id)
        return found is not None and found[1] == "terminal"

    def own_lane(self, lane_id: str, grant: Any) -> bool:
        """Is this a lane of an open environment (its terminal or its browser) that `grant` reaches:
        its grant family's, or the owner's and the member's outside MCP (over MCP the policy has
        already refused another family's as not found)? The policy reads it as a node fact, and
        nothing aimed at such a lane waits for a person. The VM holds no account and no key, its
        browser no login and no vault entry, and its one token fetches and pushes the environment's
        own repository through the git gateway, whose refusals still hold (docs/design/environments.md)."""
        found = self.lane_env(lane_id)
        return found is not None and self.reaches(found[0], grant, mcp=False)

    @staticmethod
    def _last_call_words(env: Environment) -> str:
        return f"last call {_when(env.last_call_at)}" if env.last_call_at is not None else "no call yet"

    def no_environment(self, binding: str, family: str | None, kind: str | None = None) -> NoEnvironment:
        """What a call without `on` hears in a chat with no environment: that another chat took it
        over, or the agent's open ones (a reconnected chat takes its own back by title). A browser
        call also hears that an account lane needs `on`."""
        mine = self.open_for_family(family)
        quiet = int(QUIET.total_seconds() // 60)
        lanes = (" A browser call meant for a logged-in account lane the human made needs `on` "
                 "(on=\"personal/reddit\"; lanes_list lists them).") if kind == "browser" else ""
        for env in mine:
            gone = [h for h in env.handoffs if h.binding == binding]
            if gone and env.binding != binding:
                return NoEnvironment(
                    f"another chat of this agent took environment {env.id} (\"{env.title}\") over at "
                    f"{_when(gone[-1].at)}, after this chat had made no call for {quiet} minutes. "
                    f"environment_open(title=\"{env.title}\") takes it back once that chat has been quiet as "
                    "long; or open a new one with environment_open(title=...)." + lanes)
        if mine:
            listed = "; ".join(f"{e.id} \"{e.title}\" ({e.repo + ', ' if e.repo else ''}{self._last_call_words(e)})"
                               for e in mine[:OPENS_LISTED])
            return NoEnvironment(f"this chat has no environment. Open ones of this agent: {listed}. "
                                 "environment_open(title=...) with one of these titles (and its repo) takes yours "
                                 "back (after a reconnect), a new title opens a new one; or name a lane with `on`."
                                 + lanes)
        if kind == "browser":
            return NoEnvironment("this chat has no environment, and a browser call without `on` goes to one. To "
                                 "build and test, open one first with environment_open(title=...)." + lanes)
        return NoEnvironment("this chat has no environment. Open one first with environment_open(title=...), "
                             "or name a lane with `on`")

    def resolve(self, binding: str, kind: str, family: str | None = None) -> str:
        """The lane a call without `on` goes to: the bound environment's terminal or browser lane."""
        env = self.bound(binding, family)
        if env is None:
            raise self.no_environment(binding, family, kind)
        if env.state == "closing":
            raise NoEnvironment(f"environment {env.id} is closing; open a new one with environment_open(title=...)")
        return env.browser_lane if kind == "browser" else env.terminal_lane

    def _scrub(self, text: str, extra: list[str] | tuple[str, ...] = (), *, max_len: int = SUMMARY_MAX) -> str:
        """A call's summary (or a command) as the audit log keeps it: vault values and secret shapes
        redacted, on one line."""
        from agentos.policy.redact import redact_text

        policy = self._get("policy")
        value_scrub = getattr(getattr(policy, "traces", None), "value_scrub", None)
        if callable(value_scrub):
            text = value_scrub(text)
        else:
            from agentos.policy.scrub import runtime_scrub

            text = runtime_scrub(self.runtime)(text)
        return redact_text(" ".join(text.split()), extra=extra, max_len=max_len)

    def record_call(self, lane_id: str, tool: str, *, ok: bool, summary: str = "",
                    scrub: list[str] | tuple[str, ...] = ()) -> None:
        found = self.lane_env(lane_id)
        if found is None:
            return
        now = self._clock()
        call = Call(at=now, tool=tool, summary=self._scrub(summary, scrub), ok=ok)

        def change(env: Environment) -> bool:
            if not env.open:
                return False
            env.last_call_at = now
            env.calls.append(call)
            del env.calls[:-CALLS_KEPT]
            if env.unsaved_noticed and ok:
                env.unsaved_noticed = False  # someone works on it again: a later idle stretch tells the owner again
            return True

        self.store.update(found[0].id, change)

    # ================================================================ open
    async def check_repo(self, repo: str) -> Any:
        """Can a machine clone `repo`? Asked before anything is rented: declared for env with the
        vault's credential (checked as the git gateway fetches it), or public. Else
        repo_not_connected, with the fix the human runs on the node (agentos.environments.repos)."""
        from agentos.environments import repos
        from agentos.git.service import public_access

        git = self._get("git")
        if git is not None and callable(getattr(git, "access", None)):
            access = await git.access(repo, [WORKSPACE], probe=self.repo_probe)
        else:  # a node without the git gateway: only a public repository clones
            access = await public_access(repo, self.home, probe=self.repo_probe)
        if access.ok:
            return access
        if access.unreachable:
            raise repos.unreachable(access)
        raise repos.not_connected(access)

    def planned_id(self, binding: str, title: str) -> str:
        """The id an environment will get, the same for a chat and title until one with it exists (a
        call that failed, on a limit or a boot, and is tried again keeps the id it named)."""
        key = (binding, title.lower())
        eid = self._planned.get(key)
        while eid is None or self.store.has(eid):
            eid = new_id()
            self._planned[key] = eid
        return eid

    def opening(self, binding: str) -> asyncio.Lock:
        """One open at a time per chat: a second environment_open in the same chat waits for the
        first, then gets the same environment (never a second machine; create() checks it too)."""
        if len(self._opening) > OPENING_KEPT:  # chats come and go: keep only the locks in use
            self._opening = {k: v for k, v in self._opening.items() if v.locked()}
        return self._opening.setdefault(binding, asyncio.Lock())

    def live_bound(self, binding: str, family: str | None) -> Environment | None:
        """The environment this chat has that is alive (starting or ready), else None: never one that
        is closing, failed or closed. environment_open hands back only this one."""
        env = self.bound(binding, family)
        return env if env is not None and env.state in LIVE_STATES else None

    @staticmethod
    def _repo_words(repo: str | None) -> str:
        return repo if repo else "no repository"

    def same_chat(self, env: Environment, repo: str | None) -> Environment:
        """This chat's live environment, for an environment_open that asks for `repo`: itself when
        the repository is the same. Another repository is refused (environment_in_use): one chat has
        one environment, and handing back a machine with another repository on it (a real chat got the
        starting one with a private repository back when it asked for none, 2026-11-13) misleads."""
        if (env.repo or None) == (repo or None):
            return env
        state = "still starting" if env.state == "starting" else "open"
        raise EnvironmentInUse(
            f"this chat already has environment {env.id} (\"{env.title}\", {state}) with "
            f"{self._repo_words(env.repo)}, and you asked for {self._repo_words(repo)}. A chat has one environment "
            "at a time: close it with environment_close() (add discard=true if it has work you do not need), "
            "then call environment_open again")

    async def reattach(self, binding: str, title: str, family: str | None, repo: str | None = None
                       ) -> Environment | None:
        """An open environment of this grant family with this title, bound to `binding` now: a
        resumed or reconnected chat gets its environment back. Only a live one (starting or ready),
        never one closing, failed or closed, and only with the same repository: a title held by a live
        environment with another repository is environment_in_use (a second environment with the same
        title would make the next reattach by title a guess). Taken from another chat only when that
        chat has made no call for QUIET (10 minutes); else environment_in_use, with its last call."""
        now = self._clock()
        quiet = int(QUIET.total_seconds() // 60)
        for env in self.open_for_family(family):
            if env.title.lower() != title.lower() or env.state not in LIVE_STATES:
                continue
            if (env.repo or None) != (repo or None):
                raise EnvironmentInUse(
                    f"the title \"{env.title}\" names environment {env.id}, which is open with "
                    f"{self._repo_words(env.repo)}, not {self._repo_words(repo)}. To get it back, call "
                    f"environment_open(title=\"{env.title}\"" + (f", repo=\"{env.repo}\"" if env.repo else "")
                    + "); for a new one, use another title")
            if env.binding == binding:
                return env
            last = env.last_activity()
            if now - last < QUIET:
                raise EnvironmentInUse(
                    f"environment {env.id} (\"{env.title}\") is in use by another chat of this agent (its last "
                    f"call {_when(last)}); it moves to this chat only after {quiet} minutes with no call there. "
                    "Open your own with another title: environment_open(title=...)")
            moved_from = env.binding

            def take(e: Environment, old: str = moved_from) -> bool:
                if e.binding != old or not e.open:
                    return False  # it moved, or closed, meanwhile
                e.binding = binding
                e.handoffs.append(Handoff(binding=old, at=now))
                del e.handoffs[:-HANDOFFS_KEPT]
                return True

            env = self.store.update(env.id, take)
            if env.binding != binding:
                raise EnvironmentInUse(f"environment {env.id} (\"{env.title}\") just moved to another chat; "
                                       "open your own with another title: environment_open(title=...)")
            log.info("environment %s taken over by %s from %s", env.id, binding, moved_from)
            return env
        return None

    async def create(self, env_id: str, plan: dict, *, binding: str, title: str, grant_id: str,
                     grant_root: str | None, agent: str, client: str | None, repo: str | None,
                     ref: str | None, subject: str | None = None, member: str | None = None) -> Environment:
        """Record the environment and start bringing it up (after the limits and the grant's check). A chat
        that has one open already gets that one back."""
        if repo:
            from agentos.git.wire import parse_remote

            parse_remote(repo)
        now = self._clock()
        specs = plan.get("specs") or {}
        env = Environment(
            id=env_id, title=title, grant_id=grant_id, grant_root=grant_root, binding=binding, agent=agent,
            subject=subject, member=member, client=client, repo=repo, ref=ref, image=plan["image"], created_at=now,
            expires_at=now + timedelta(minutes=plan["ttl_minutes"]), price_hourly_eur=plan.get("price_hourly_eur"),
            machine=Machine(provider=getattr(self.vms().provider(), "name", ""), server_type=plan["server_type"],
                            cpu=specs.get("cores"), memory_gb=specs.get("memory_gb"), location=plan["location"],
                            size=self.size_of(specs.get("cores"), specs.get("memory_gb"))))
        async with self._lock("create"):
            existing = self.live_bound(binding, env.family)
            if existing is not None:  # this chat opened one meanwhile: that one, never a second machine
                return existing
            if self.store.has(env_id):
                raise LaneExists(f"environment {env_id} exists already")
            self.check_limits(plan)
            self.store.put(env)
        self._planned = {k: v for k, v in self._planned.items() if v != env_id}
        self._spawn(env_id, self._bring_up(env_id, plan))
        log.info("environment %s (%s) opening for %s", env_id, title, agent)
        return env


    # ---------------------------------------------------------------- bring-up


    # ================================================================ close
    async def close(self, env_id: str, *, discard: bool = False, reason: str = "closed") -> Environment:
        env = self.store.get(env_id)
        if not env.open:
            return env
        async with self._lock(f"close:{env_id}"):
            env = self.store.get(env_id)
            if not env.open:
                return env
            if not discard and env.state == "ready":
                repos = await self.git_repos(env)
                if repos is None:
                    raise UnsavedWork(f"environment {env.id}'s machine does not answer, so its work cannot be "
                                      "checked; close it with discard=true to delete it anyway",
                                      {"dirty_files": [], "unpushed": [], "unreachable": True})
                unsaved = self.unsaved_of(repos)
                if unsaved is not None:
                    raise UnsavedWork(f"environment {env.id} has work that is not saved: "
                                      f"{self.unsaved_words(unsaved)}. Commit it and push every branch "
                                      "(git push origin <branch>; a stash goes onto a branch first), or close "
                                      "with discard=true to delete it", unsaved)
            task = self._tasks.get(env_id)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            return await self._teardown(self.store.get(env_id), reason=reason)

    async def _teardown(self, env: Environment, *, reason: str, final: str = "closed") -> Environment:
        pushed = [Push(branch=p["branch"], commit=p["commit"], at=datetime.fromisoformat(p["at"]))
                  for p in self.pushes(env)][:50]
        why = env.closed_reason

        def closing(e: Environment) -> None:
            e.pushed, e.state = pushed, "closing"
            e.closed_reason = e.closed_reason or why

        env = self.store.update(env.id, closing)
        tunnel = self._tunnels.pop(env.id, None)
        if tunnel is not None:
            await tunnel.stop()
        git = self._get("git")
        if git is not None and callable(getattr(git, "vm_down", None)):
            try:  # its gateway token ends here, with the listener its tunnel reached
                await git.vm_down(env.terminal_lane)
            except Exception:  # the gateway checks that the environment is open as well
                log.exception("environment %s: could not close its git listener", env.id)
        from agentos.node.interaction import VM_PROXIED_LANES

        VM_PROXIED_LANES.discard(env.browser_lane)
        lanes = self._get("lanes")
        if lanes is not None:
            with contextlib.suppress(Exception):
                lanes.get(env.browser_lane)
                with contextlib.suppress(Exception):
                    await lanes.close(env.browser_lane)
                await lanes.delete(env.browser_lane, by="node")
        cost = None
        if env.vm_id:
            vms = self.vms()
            with contextlib.suppress(NotFound):
                rec = vms.store.get(env.vm_id)
                if rec.live:
                    rec = await vms.destroy(rec.id, reason=f"environment {env.id}: {reason}")
                cost = rec.cost_eur(self._clock())
        def closed(e: Environment) -> None:
            e.state = final  # type: ignore[assignment]
            e.closed_at = self._clock()
            e.closed_reason = e.closed_reason or reason
            e.cost_eur = cost if cost is not None else (0.0 if e.price_hourly_eur == 0 else e.cost_eur)

        env = self.store.update(env.id, closed)
        log.info("environment %s %s (%s)", env.id, final, reason)
        return env

    async def extend(self, env_id: str, minutes: int) -> Environment:
        env = self.store.get(env_id)
        if env.state not in ("starting", "ready"):
            raise InvalidRequest(f"environment {env.id} is {env.state}")
        if minutes < 1:
            raise InvalidRequest("extend by at least one minute")
        vms = self.vms()
        longest = self._longest()
        cap = env.created_at + timedelta(minutes=longest)
        new = min(max(env.expires_at, self._clock()) + timedelta(minutes=minutes), cap)
        if new <= env.expires_at:
            raise EnvironmentLimit(f"environment {env.id} is already at this node's longest life of {longest / 60:g} "
                                   f"hours; {self._longest_raise()}")
        budget = self.settings.monthly_eur
        if budget:
            booked = self.booked_eur(until={env.id: new})
            if booked > budget:
                raise EnvironmentLimit(f"with environment {env.id} booked that long, environments could cost EUR "
                                       f"{booked:.2f} this month, past the monthly budget of EUR {budget:.2f}. Extend "
                                       "it by less, or close another; the owner raises the budget with "
                                       "`agentos environments allow --monthly-eur E`")
        if env.vm_id:
            rec = vms.store.get(env.vm_id)
            if rec.live and rec.expires_at < new:
                try:
                    await vms.extend_to(rec.id, new)  # the node's own VM limits: max_ttl_minutes, monthly budget
                except AgentosError as e:
                    if e.code == "vm_limit":
                        raise EnvironmentLimit(e.message) from None
                    raise
        env = self.store.get(env_id)
        env.expires_at = new
        env.unsaved_noticed = False
        self.store.put(env)
        return env

    # ================================================================ reaper
    def _busy(self, env: Environment) -> bool:
        envs = self._get("envs")
        if envs is None:
            return False
        with contextlib.suppress(Exception):
            if envs.has(env.terminal_lane) and envs.state(env.terminal_lane) == LaneState.ACTIVE:
                return True
            if any(j.state == "running" for j in envs.job_list(env.terminal_lane)):
                return True
        return False

    def _last_used(self, env: Environment) -> datetime:
        """The last sign of use: an agent's call, a command on the terminal lane, or a person's input
        in the browser's live view (the viewer relay notes each one on the lane's control)."""
        last = env.last_activity()
        envs = self._get("envs")
        with contextlib.suppress(Exception):
            used = envs.store.last_used(env.terminal_lane) if envs is not None else None
            if used is not None:
                used = used if used.tzinfo else used.replace(tzinfo=UTC)
                last = max(last, used)
        lanes = self._get("lanes")
        with contextlib.suppress(Exception):
            human = lanes.get(env.browser_lane).control.last_human_input if lanes is not None else None
            if human is not None:
                last = max(last, human if human.tzinfo else human.replace(tzinfo=UTC))
        return last

    async def _notify(self, title: str, body: str) -> None:
        notifier = self._get("notifier")
        fn = getattr(notifier, "notify", None)
        if fn is None:
            log.warning("%s: %s", title, body)
            return
        try:
            out = fn(title=title, body=body, level="warning", urgent=False, data={})
            if asyncio.iscoroutine(out):
                await out
        except Exception:
            log.exception("could not send an environment notification")

    async def reap_once(self, now: datetime | None = None) -> dict:
        now = now or self._clock()
        out: dict[str, Any] = {"at": clock.iso_or_none(now, z=True), "closed": [], "kept_unsaved": [], "tunnels": [], "gone": []}
        vms = self._get("vms")
        for env in self.store.open():
            if env.id in self._tasks:
                continue  # still coming up, or closing
            if env.state == "closing":
                await self._teardown(env, reason=env.closed_reason or "closed")
                out["closed"].append(env.id)
                continue
            if env.state == "starting":
                self._spawn(env.id, self._bring_up(env.id, None))  # the node restarted while it came up
                continue
            rec = None
            if vms is not None and env.vm_id:
                with contextlib.suppress(NotFound):
                    rec = vms.store.get(env.vm_id)
            if rec is None or not rec.live:
                env.closed_reason = "its machine was deleted"
                await self._teardown(env, reason="its machine was deleted")
                out["gone"].append(env.id)
                continue
            if now >= env.expires_at:
                await self._teardown(env, reason="time limit")
                out["closed"].append(env.id)
                continue
            tunnel = self._tunnels.get(env.id)
            if rec.state == "ready" and (tunnel is None or not tunnel.alive):
                try:
                    await self._start_tunnel(env)
                    await self._make_browser(env)  # a lane manager that keeps lanes in memory lost it on a restart
                    out["tunnels"].append(env.id)
                except Exception as e:  # noqa: BLE001
                    log.warning("environment %s: tunnel did not start: %s", env.id, e)
            idle = not self._busy(env) and now - self._last_used(env) >= timedelta(minutes=self.settings.idle_minutes)
            near_end = env.expires_at - now <= timedelta(minutes=self.settings.unsaved_notice_minutes)
            if not idle and not near_end:
                continue
            repos = await self.git_repos(env)
            unsaved = self.unsaved_of(repos) if repos is not None else {"unreachable": True}
            if unsaved is None and idle:
                await self._teardown(env, reason=f"idle for {self.settings.idle_minutes} minutes")
                out["closed"].append(env.id)
                continue
            if unsaved is not None and not env.unsaved_noticed:
                def noticed(e: Environment) -> None:
                    e.unsaved_noticed = True

                env = self.store.update(env.id, noticed)
                left = max(0, int((env.expires_at - now).total_seconds() // 60))
                await self._notify(f"Environment \"{env.title}\" has work that is not pushed",
                                   f"{env.agent} left environment {env.id} with changes that are not committed or "
                                   f"pushed. It stays open for {left} more minutes, to its time limit, then it is "
                                   "deleted with everything on it. Open it to look, or extend it.")
                out["kept_unsaved"].append(env.id)
        return out

    # ================================================================ report
    def report(self) -> dict:
        return {"open": len(self.store.open()), "tunnels": sum(1 for t in self._tunnels.values() if t.alive),
                "month_spend_eur": self.month_spend(), "last_reap": self.last_reap,
                "settings": self.settings.model_dump()}

    def resolve_or_none(self, binding: str, family: str | None = None) -> Environment | None:
        return self.bound(binding, family)

    def binding_of(self, session: str | None, grant_id: str | None) -> str:
        return binding_of(session, grant_id)


def binding_of(session: str | None, grant_id: str | None) -> str:
    """What an environment is bound to: the MCP session (one chat), else the grant (stdio)."""
    if session:
        return f"session:{session}"
    return f"grant:{grant_id or 'owner'}"


__all__ = ["OPEN_NEXT", "EnvironmentManager", "binding_of", "check_ref", "check_title"]
