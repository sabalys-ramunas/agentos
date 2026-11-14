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
import shlex
import string
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentos.core.errors import AgentosError, InvalidRequest, LaneExists, NotFound
from agentos.core.models import LaneSpec, LaneState, ProxyConfig, utcnow
from agentos.environments import machine
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
    SIZES,
    WORKSPACE,
    Call,
    Environment,
    EnvSettings,
    Handoff,
    Machine,
    Push,
    load_settings,
)
from agentos.environments.store import EnvironmentStore
from agentos.environments.tunnels import Tunnel

log = logging.getLogger("agentos.environments")

TITLE_MAX = 80
SUMMARY_MAX = 200
SETUP_POLL_S = 5.0
VM_POLL_S = 0.25
CHECK_TIMEOUT_S = 30.0
CLONE_TIMEOUT_S = 900.0
UNSAVED_LIST_MAX = 50
OPEN_NEXT = "environment_open(title=...)"
# Ids are e + ID_LENGTH characters people can read aloud (no 0/o, 1/l/i): 31^10, about 8e14, so one
# cannot be guessed, and a lane name (env/<id>-browser) stays short.
_ID_CHARS = "".join(c for c in string.ascii_lowercase + string.digits if c not in "0o1li")
ID_LENGTH = 10
OPENS_LISTED = 10
OPENING_KEPT = 256

# One command over SSH that says, for every repository under /work (four folders down, past
# node_modules and .venv), its branch, the commits on any local branch (or a detached HEAD) that no
# remote has, its stashes, and the files git status lists, untracked ones included. That is the
# work close and the reaper never delete without discard. A repository git cannot read says
# `error=`, which counts as unsaved. safe.directory: one made by another user still answers.
GIT_STATE = r"""
find /work -maxdepth 4 \( -name node_modules -o -name .venv \) -prune -o -name .git -print -prune 2>/dev/null | sort |
while IFS= read -r gitdir; do
  d=$(dirname "$gitdir")
  g() { git -c safe.directory='*' -C "$d" "$@"; }
  echo "repo=$d"
  if ! g status --porcelain=v1 >/dev/null 2>&1; then echo "error=git cannot read it"; continue; fi
  echo "branch=$(g rev-parse --abbrev-ref HEAD 2>/dev/null)"
  echo "ahead=$(g rev-list --count HEAD --branches --not --remotes 2>/dev/null \
    || g rev-list --count --branches --not --remotes 2>/dev/null || echo 0)"
  echo "dirty_count=$(g status --porcelain=v1 --untracked-files=normal | wc -l | tr -d ' ')"
  g status --porcelain=v1 --untracked-files=normal | head -n 50 | sed 's/^/dirty=/'
  { g log --format='unpushed=%h %s' HEAD --branches --not --remotes 2>/dev/null \
    || g log --format='unpushed=%h %s' --branches --not --remotes 2>/dev/null; } | head -n 50
  echo "stash_count=$(g stash list 2>/dev/null | wc -l | tr -d ' ')"
  g stash list --format='stash=%gd %s' 2>/dev/null | head -n 20
done
"""


# Writes the VM's git config for the gateway, read from stdin (agentos.git.service.vm_gitconfig), to
# a file of its own that ~/.gitconfig includes once. Nothing secret is on the command line.
GIT_SETUP = ("umask 077 && mkdir -p ~/.config/agentos && cat > ~/.config/agentos/gitconfig && "
             "(git config --global --get-all include.path 2>/dev/null | grep -qx '~/.config/agentos/gitconfig' || "
             "git config --global --add include.path '~/.config/agentos/gitconfig')")


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z") if dt else None


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


def parse_ports(text: str) -> list[dict]:
    """`ss -ltnpH` lines -> [{port, process}], each port once, without the node's own (ssh, the
    gateway's reverse tunnel)."""
    out: dict[int, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] != "LISTEN":
            continue
        local = parts[3]
        try:
            port = int(local.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        m = re.search(r'users:\(\("([^"]+)"', line)
        proc = m.group(1) if m else None
        if proc in ("sshd", "sshd-session") or port in (22, 3129):
            continue
        out.setdefault(port, {"port": port, "process": proc, "link": None})
    return [out[p] for p in sorted(out)]


def parse_git_state(text: str) -> list[dict]:
    repos: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key == "repo":
            cur = {"dir": value, "branch": None, "ahead": 0, "dirty": 0, "dirty_files": [], "unpushed": [],
                   "stash_count": 0, "stashes": [], "error": None}
            repos.append(cur)
        elif cur is None:
            continue
        elif key == "branch":
            cur["branch"] = value or None
        elif key in ("ahead", "dirty_count", "stash_count"):
            cur["dirty" if key == "dirty_count" else key] = int(value) if value.isdigit() else 0
        elif key == "dirty":
            cur["dirty_files"].append(value[3:] if len(value) > 3 else value)
        elif key == "unpushed":
            cur["unpushed"].append(value)
        elif key == "stash":
            cur["stashes"].append(value)
        elif key == "error":
            cur["error"] = value or "git cannot read it"
    return repos


def main_repo(repos: list[dict] | None, workdir: str) -> dict | None:
    """The repository an environment was opened with (its workdir), else the first one."""
    if not repos:
        return None
    return next((r for r in repos if r["dir"] == workdir), repos[0])


class EnvironmentManager:
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
                self.last_reap = {"at": _iso(self._clock()), "error": str(getattr(e, "message", e))}
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
            from agentos.vault.scrub import runtime_scrub

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

    def _size(self, size: str | None) -> tuple[str | None, dict | None]:
        """`size`: small, medium, large or xlarge (SIZES: the cheapest server type with at least
        that much, or on the docker provider a container with those limits), a vms.toml profile
        name, or a server type. None: the environments' profile, else the VM default (the cheapest
        that fits)."""
        if not size:
            return self.settings.profile, None
        size = str(size).strip().lower()
        vms = self.vms()
        if size in SIZES:
            self._check_size(size)
            if getattr(vms.provider(), "name", "") == "docker":
                return self.settings.profile, {"server_type": f"docker-{size}"}
            cpu, memory = SIZES[size]
            return self.settings.profile, {"min_cpu": cpu, "min_memory_gb": memory}
        if size in vms.settings.profiles:
            return size, None
        return self.settings.profile, {"server_type": size}

    def _check_size(self, size: str) -> None:
        names = list(SIZES)
        top = self.settings.max_size
        if names.index(size) > names.index(top):
            raise EnvironmentLimit(f"the biggest environment on this node is {top} ({self._size_words(top)}), so "
                                   f"{size} is past the limit. Ask for {top} or less; the owner raises it with "
                                   "`agentos environments allow --max-size xlarge`")

    @staticmethod
    def _size_words(size: str) -> str:
        cpu, memory = SIZES[size]
        return f"{cpu} CPU, {memory} GB"

    def _check_planned(self, plan: dict) -> None:
        """A server type or a profile asked for by name is bound by max_size too, by what it has."""
        specs = plan.get("specs") or {}
        top = self.settings.max_size
        cpu, memory = SIZES[top]
        if (specs.get("cores") or 0) > cpu or (specs.get("memory_gb") or 0) > memory:
            raise EnvironmentLimit(f"{plan['server_type']} has {specs.get('cores')} CPU and {specs.get('memory_gb'):g} "
                                   f"GB, more than this node's biggest environment, {top} ({self._size_words(top)}). "
                                   "Ask for a smaller size; the owner raises it with "
                                   "`agentos environments allow --max-size xlarge`")

    @staticmethod
    def size_of(cpu: float | None, memory_gb: float | None) -> str | None:
        """The biggest of SIZES a machine with this much has (None: less than small, or unknown)."""
        got = None
        for name, (c, m) in SIZES.items():
            if (cpu or 0) >= c and (memory_gb or 0) >= m - 0.5:  # a few hundred MB short still counts
                got = name
        return got

    async def bigger_sizes(self, env: Environment) -> list[dict]:
        """The sizes above the one `env` has, up to max_size, with what each would rent here and its
        price: what an agent reads before it asks for a bigger one. [] when none, or the provider
        does not answer."""
        names = list(SIZES)
        top = names.index(self.settings.max_size)
        got = env.machine.size
        start = names.index(got) + 1 if got in names else 0
        out = []
        for name in names[start:top + 1]:
            cpu, memory = SIZES[name]
            if (env.machine.cpu or 0) >= cpu and (env.machine.memory_gb or 0) >= memory:
                continue
            try:
                profile, custom = self._size(name)
                plan = await self.vms().plan(f"{WORKSPACE}/{env.id}", profile=profile, ttl_minutes=60, size=custom)
            except AgentosError:
                continue
            specs = plan.get("specs") or {}
            out.append({"size": name, "server_type": plan["server_type"], "cpu": specs.get("cores"),
                        "memory_gb": specs.get("memory_gb"), "price_hourly_eur": plan.get("price_hourly_eur")})
        return out

    def _hours(self, hours: float | None) -> int:
        h = float(hours if hours is not None else self.settings.default_hours)
        if h <= 0:
            raise InvalidRequest("hours is how long the environment may live, more than 0")
        cap = self._longest()
        minutes = max(5, round(h * 60))
        if minutes > cap:
            raise EnvironmentLimit(f"an environment lives at most {cap / 60:g} hours on this node, so {h:g} hours "
                                   f"is past the limit. Ask for fewer hours; {self._longest_raise()}")
        return minutes

    def _longest(self) -> float:
        """The longest life of an environment here, in minutes: max_hours, and the VMs' max_ttl_minutes."""
        return min(self.settings.max_hours * 60, self.vms().settings.max_ttl_minutes)

    def _longest_raise(self) -> str:
        """Which setting holds the longest life, and how the owner raises it."""
        if self.settings.max_hours * 60 <= self.vms().settings.max_ttl_minutes:
            return "the owner raises it with `agentos environments allow --max-hours H`"
        return "the owner raises it with max_ttl_minutes in vms.toml (the longest life of any VM on this node)"

    async def _image(self) -> str:
        if self.settings.image != "auto":
            return self.settings.image
        try:
            await self.vms().provider().resolve_image(machine.SNAPSHOT_IMAGE)
            return machine.SNAPSHOT_IMAGE
        except AgentosError:
            return machine.BASE_IMAGE

    def month_spend(self, now: datetime | None = None) -> float:
        now = now or self._clock()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = 0.0
        for env in self.store.recent(1000):
            if env.closed_at is not None and env.closed_at < start:
                continue
            total += self.cost_now(env) or 0.0
        return round(total, 4)

    def booked_eur(self, now: datetime | None = None, *, until: dict[str, datetime] | None = None) -> float:
        """This month's spend on environments with every open one counted to its time limit (or to
        `until[id]`): what they can still cost if none closes early. The budget is checked against it."""
        now = now or self._clock()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = 0.0
        for env in self.store.recent(1000):
            if env.closed_at is not None and env.closed_at < start:
                continue
            spent = self.cost_now(env) or 0.0
            if env.open and env.price_hourly_eur:
                end = (until or {}).get(env.id, env.expires_at)
                hours = max(1, -(-int(max(0.0, (end - env.created_at).total_seconds())) // 3600))
                spent = max(spent, hours * env.price_hourly_eur)
            total += spent
        return round(total, 4)

    def cost_now(self, env: Environment) -> float | None:
        if env.cost_eur is not None and not env.open:
            return env.cost_eur
        if not env.vm_id:
            return 0.0 if not env.open else None
        try:
            return self.vms().store.get(env.vm_id).cost_eur(self._clock())
        except (NotFound, AgentosError):
            return env.cost_eur

    async def plan(self, env_id: str, *, size: str | None, hours: float | None) -> dict:
        """What open would rent, for the limits and the record of the open (agentos.vms plan, with
        the image the environment boots)."""
        profile, custom = self._size(size)
        minutes = self._hours(hours)
        plan = await self.vms().plan(f"{WORKSPACE}/{env_id}", profile=profile, ttl_minutes=minutes, size=custom)
        if size and str(size).strip().lower() not in SIZES:
            self._check_planned(plan)  # a server type or a profile, by what it has (a named size: by its name)
        plan["image"] = await self._image()
        return plan

    def check_limits(self, plan: dict) -> None:
        vms = self.vms()
        live = len(self.store.open())
        cap = self.settings.max_live if self.settings.max_live is not None else vms.settings.max_live
        if live >= cap:
            raise EnvironmentLimit(f"this node already has {live} open environment{'s' if live != 1 else ''}, and "
                                   f"its limit is {cap} at once. Close one you no longer need with environment_close, "
                                   "or tell the user: the owner raises the limit with "
                                   "`agentos environments allow --max-live N`")
        budget = self.settings.monthly_eur
        price = plan.get("price_hourly_eur")
        if budget and price is not None:
            worst = max([price, *[q or 0 for _n, q in plan.get("fallbacks") or []]]) * -(-plan["ttl_minutes"] // 60)
            booked = self.booked_eur()
            if booked + worst > budget:
                raise EnvironmentLimit(f"this environment could cost up to EUR {worst:.2f}; with EUR {booked:.2f} "
                                       "spent and booked by open environments this month, that passes the monthly "
                                       f"budget of EUR {budget:.2f}. Close one, or ask for fewer hours; the owner "
                                       "raises the budget with `agentos environments allow --monthly-eur E`")

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

    def _spawn(self, env_id: str, coro: Any) -> None:
        task = asyncio.create_task(coro, name=f"environment-{env_id}")
        self._tasks[env_id] = task
        task.add_done_callback(lambda _t, i=env_id: self._tasks.pop(i, None) if self._tasks.get(i) is _t else None)

    async def wait_ready(self, env_id: str, wait_s: float) -> Environment:
        task = self._tasks.get(env_id)
        if task is not None and wait_s > 0:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=wait_s)
        return self.store.get(env_id)

    # ---------------------------------------------------------------- bring-up
    async def _bring_up(self, env_id: str, plan: dict | None) -> None:
        env = self.store.get(env_id)
        try:
            vms = self.vms()
            if env.vm_id is None:
                if plan is None:
                    raise EnvironmentFailed("the node restarted before the machine was rented")
                cloud = machine.cloud_config() if plan["image"] == machine.BASE_IMAGE else ""
                try:
                    rec = await vms.create(env.terminal_lane, plan=plan, purpose=f"environment {env.id}: {env.title}",
                                           created_by=env.grant_id, wait_s=0, cloud_config=cloud, environment=env.id)
                except AgentosError as e:
                    if e.code == "vm_limit":
                        raise EnvironmentLimit(e.message) from None
                    raise
                def rented(e: Environment) -> None:
                    e.vm_id = rec.id
                    if rec.price_hourly_eur is not None:
                        e.price_hourly_eur = rec.price_hourly_eur
                    e.machine.server_type = rec.server_type

                env = self.store.update(env_id, rented)
            await self._wait_vm(env)
            await self._wait_setup(env)
            await self._start_tunnel(env)
            await self._make_browser(env)
            if env.repo:
                await self._clone(env)
            else:
                await self.envs().exec(env.terminal_lane, f"mkdir -p {machine.WORK}", timeout_s=CHECK_TIMEOUT_S)
            def ready(e: Environment) -> bool:
                if e.state != "starting":
                    return False
                e.state, e.ready_at = "ready", self._clock()
                return True

            env = self.store.update(env_id, ready)
            if env.state == "ready":
                log.info("environment %s ready (%s)", env.id, env.workdir)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(getattr(e, "message", e)) or type(e).__name__
            log.warning("environment %s failed to come up: %s", env_id, msg)
            await self._fail(env_id, msg, code=e.code if isinstance(e, AgentosError) else None)

    async def _fail(self, env_id: str, why: str, *, code: str | None = None) -> None:
        def failed(e: Environment) -> None:
            e.error = why[:1200]
            e.error_code = code or "environment_failed"

        env = self.store.update(env_id, failed)
        await self._teardown(env, reason=f"failed to start: {why[:200]}", final="failed")

    async def _wait_vm(self, env: Environment) -> None:
        vms = self.vms()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + vms.settings.boot_timeout_s + 30
        while True:
            rec = vms.store.get(env.vm_id)
            if rec.state == "ready":
                return
            if not rec.live:
                raise EnvironmentFailed(f"the machine did not come up: {rec.error or rec.state}")
            if loop.time() > deadline:
                raise EnvironmentFailed(f"the machine did not answer SSH within {vms.settings.boot_timeout_s} s")
            await asyncio.sleep(VM_POLL_S)

    async def _wait_setup(self, env: Environment) -> None:
        envs = self.envs()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.setup_timeout_s
        said = ""
        ran = False
        while loop.time() < deadline:
            try:
                res = await envs.exec(env.terminal_lane, machine.SETUP_STATE, timeout_s=CHECK_TIMEOUT_S)
                said = (res.stdout or res.stderr or "").strip()
            except AgentosError as e:
                said = e.message
            first = said.split("\n", 1)[0]
            if first == "ready":
                return
            if "error" in first:
                raise EnvironmentFailed(f"the machine's setup failed: {said[-600:]}")
            if not ran and machine.boot_done_without_setup(first):
                # booted and done, but nothing ran the setup: a snapshot made another way (`agentos vm
                # image build` without the environment's script) or an image with no cloud-init
                ran = True
                left = max(60.0, deadline - loop.time())
                try:
                    out = await envs.exec(env.terminal_lane, "sh -s", stdin=machine.setup_script(), timeout_s=left)
                except AgentosError as e:
                    raise EnvironmentFailed(f"the machine's setup could not run: {e.message}") from None
                if out.exit_code != 0:
                    tail = (out.stderr or out.stdout or "").strip()[-600:]
                    raise EnvironmentFailed(f"the machine's setup failed (exit {out.exit_code}): {tail}")
                continue
            await asyncio.sleep(SETUP_POLL_S)
        raise EnvironmentFailed(f"the machine's setup did not finish within {self.settings.setup_timeout_s} s "
                                f"({said[-200:] or 'no answer'})")

    async def _start_tunnel(self, env: Environment) -> None:
        vms = self.vms()
        rec = vms.store.get(env.vm_id)
        old = self._tunnels.get(env.id)
        if old is not None and old.alive:
            return
        git = self._get("git")
        gateway = None
        if git is not None and callable(getattr(git, "vm_listener", None)):
            try:  # the gateway's listener for this VM alone: its token is good only through its own tunnel
                gateway = await git.vm_listener(env.terminal_lane)
            except Exception as e:  # noqa: BLE001 - the environment works without git through the node
                log.warning("environment %s: no git gateway listener: %s", env.id, e)
        tunnel = Tunnel(vms.remote(rec), gateway_port=gateway, socks_port=old.socks_port if old else None)
        await tunnel.start()
        self._tunnels[env.id] = tunnel
        self._wrap_proxies_loader()
        from agentos.node.interaction import VM_PROXIED_LANES

        VM_PROXIED_LANES.add(env.browser_lane)  # its localhost is the VM's: a page there may open it
        if gateway is not None:
            await self._git_setup(env)

    async def _git_setup(self, env: Environment) -> None:
        """Give the VM the one token it holds: a gateway token that fetches and pushes env.repo and
        nothing else, good only through this VM's own tunnel, revoked when the environment closes.
        No declared repository: no token at all. It reaches the VM's git config through the
        command's stdin, never a command line (which `ps` on the node shows)."""
        git = self._get("git")
        if git is None or not env.repo or not callable(getattr(git, "vm_token", None)):
            return
        from agentos.git.service import vm_gitconfig

        token = git.vm_token(env.terminal_lane, grant_id=env.grant_id, repo=env.repo)
        if token is None:
            return  # not declared for env: a public repository, cloned straight from the VM
        res = await self.envs().exec(env.terminal_lane, GIT_SETUP, stdin=vm_gitconfig(env.repo, token),
                                     timeout_s=CHECK_TIMEOUT_S)
        if res.exit_code != 0:
            raise EnvironmentFailed(f"setting up git on the machine failed: {(res.stderr or res.stdout).strip()[-300:]}")

    async def _make_browser(self, env: Environment) -> None:
        """A scratch browser lane made by the node: no account, no vault entry, no keepalive; its
        proxy is the environment's tunnel, so every request leaves from the VM."""
        lanes = self._get("lanes")
        if lanes is None:
            return
        with contextlib.suppress(Exception):
            lanes.get(env.browser_lane)
            return
        spec = LaneSpec(id=env.browser_lane, proxy_label=f"env-{env.id}")
        try:
            await lanes.create(spec)
        except LaneExists:
            return

    async def _clone(self, env: Environment) -> None:
        from agentos.git.wire import parse_remote

        remote = parse_remote(env.repo or "")
        name = remote.repo.rsplit("/", 1)[-1]
        dest = f"{machine.WORK}/{name}"
        git = self._get("git")
        declared = git is not None and git.remotes.find(remote, [env.terminal_lane, WORKSPACE]) is not None
        url = f"https://{remote.host}/{remote.repo}.git"
        q = shlex.quote
        checkout = f" && git -C {q(dest)} checkout -q {q(env.ref)}" if env.ref else ""
        cmd = (f"mkdir -p {machine.WORK} && if [ ! -d {q(dest)}/.git ]; then "
               f"git clone -q {q(url)} {q(dest)}{checkout}; fi && cd {q(dest)} && "
               "(git config --global user.name >/dev/null || git config --global user.name "
               f"{q(env.agent[:60] or 'agentos agent')}) && (git config --global user.email >/dev/null || "
               "git config --global user.email agent@agentos.invalid) && git rev-parse --abbrev-ref HEAD")
        # a declared repository goes through the gateway by the VM's own git config (_git_setup)
        res = await self.envs().exec(env.terminal_lane, cmd, timeout_s=CLONE_TIMEOUT_S)
        if res.exit_code != 0:
            from agentos.environments.repos import clone_failure

            raise clone_failure(remote.slug, (res.stderr or "") + "\n" + (res.stdout or ""), via_gateway=declared)
        branch = (res.stdout.strip().splitlines() or [None])[-1]

        def cloned(e: Environment) -> None:
            e.workdir, e.via_gateway, e.branch = dest, declared, branch

        self.store.update(env.id, cloned)

    # ================================================================ read
    async def ports(self, env: Environment) -> list[dict]:
        try:
            res = await self.envs().exec(env.terminal_lane, "ss -ltnpH 2>/dev/null || ss -ltnp",
                                         timeout_s=CHECK_TIMEOUT_S)
        except AgentosError:
            return []
        return parse_ports(res.stdout)

    async def git_repos(self, env: Environment) -> list[dict] | None:
        """Every repository under /work, the cloned one and any other the agent made: None when the
        machine does not answer."""
        try:
            res = await self.envs().exec(env.terminal_lane, GIT_STATE, timeout_s=CHECK_TIMEOUT_S)
        except AgentosError:
            return None
        if res.exit_code not in (0, None) and not res.stdout:
            return None
        return parse_git_state(res.stdout)

    def pushes(self, env: Environment) -> list[dict]:
        """What the git gateway pushed from this environment (its push traces), newest first."""
        policy = self._get("policy")
        traces = getattr(policy, "traces", None)
        out: list[dict] = []
        if traces is not None:
            with contextlib.suppress(Exception):
                events, _ = traces.query(kind="git_push", lane=env.terminal_lane, limit=50)
                for ev in events:
                    args = ev.args if isinstance(ev.args, dict) else {}
                    if args.get("result") != "pushed":
                        continue
                    at = datetime.fromtimestamp(ev.at or 0, UTC)
                    for ref in args.get("refs") or []:
                        name = str(ref.get("ref") or "")
                        commit = str(ref.get("new") or "").split(":", 1)[-1]
                        out.append({"branch": name.removeprefix("refs/heads/"), "commit": commit[:12], "at": _iso(at)})
        known = {(p["branch"], p["commit"]) for p in out}
        for p in env.pushed:
            if (p.branch, p.commit[:12]) not in known:
                out.append({"branch": p.branch, "commit": p.commit[:12], "at": _iso(p.at)})
        return out

    async def git_state(self, env: Environment) -> dict:
        repos = await self.git_repos(env) if env.open and env.vm_id else None
        main = main_repo(repos, env.workdir)
        return {"branch": (main or {}).get("branch") or env.branch, "ahead": (main or {}).get("ahead", 0),
                "dirty": (main or {}).get("dirty", 0), "pushed": self.pushes(env),
                "reachable": repos is not None}

    @staticmethod
    def unsaved_of(repos: list[dict] | None) -> dict | None:
        """{dirty_files, unpushed, stashes} of every repository (each named by its folder when
        there are several), or None when all of it is committed and on a remote. A repository git
        could not read counts as unsaved (`unreadable`)."""
        if not repos:
            return None
        many = len(repos) > 1
        dirty = [f"{r['dir']}/{f}" if many else f for r in repos for f in r["dirty_files"]]
        unpushed = [f"{r['dir']}: {c}" if many else c for r in repos for c in r["unpushed"]]
        stashes = [f"{r['dir']}: {s}" if many else s for r in repos for s in r.get("stashes") or []]
        unreadable = [r["dir"] for r in repos if r.get("error")]
        if not (dirty or unpushed or stashes or unreadable) and not any(
                r["dirty"] or r["ahead"] or r.get("stash_count") for r in repos):
            return None
        out = {"dirty_files": dirty[:UNSAVED_LIST_MAX], "unpushed": unpushed[:UNSAVED_LIST_MAX],
               "stashes": stashes[:UNSAVED_LIST_MAX]}
        if unreadable:
            out["unreadable"] = unreadable[:UNSAVED_LIST_MAX]
        return out

    @staticmethod
    def unsaved_words(unsaved: dict) -> str:
        """What unsaved_of found, for an error or a notification."""
        parts = []
        if unsaved.get("dirty_files"):
            parts.append("changed or new files: " + ", ".join(unsaved["dirty_files"][:10]))
        if unsaved.get("unpushed"):
            parts.append("commits not on a remote: " + "; ".join(unsaved["unpushed"][:10]))
        if unsaved.get("stashes"):
            parts.append("stashes: " + "; ".join(unsaved["stashes"][:5]))
        if unsaved.get("unreadable"):
            parts.append("repositories git cannot read: " + ", ".join(unsaved["unreadable"][:5]))
        return "; ".join(parts) or "changes git counts but did not list"

    def summary(self, env: Environment) -> dict:
        """EnvSummary of the contract (docs/design/environments.md)."""
        now = self._clock()
        return {"id": env.id, "title": env.title, "state": env.shown_state(now), "repo": env.repo,
                "branch": env.branch, "workdir": env.workdir, "created_at": _iso(env.created_at),
                "ready_at": _iso(env.ready_at), "last_call_at": _iso(env.last_call_at),
                "closed_at": _iso(env.closed_at), "expires_at": _iso(env.expires_at),
                "minutes_left": max(0, int((env.expires_at - now).total_seconds() // 60)) if env.open else None,
                "machine": env.machine.model_dump(), "price_hourly_eur": env.price_hourly_eur,
                "cost_eur": self.cost_now(env), "agent": {"name": env.agent, "client": env.client},
                "lanes": env.lanes, "closed_reason": env.closed_reason, "error": env.error,
                "error_code": env.error_code}

    def _commands(self, env: Environment) -> list[dict]:
        out = []
        envs = self._get("envs")
        if envs is not None and callable(getattr(envs, "job_list", None)):
            with contextlib.suppress(Exception):
                for j in envs.job_list(env.terminal_lane):
                    out.append({"id": j.id, "cmd": self._scrub(j.cmd, max_len=500), "state": j.state,
                                "started_at": _iso(j.started_at), "exit_code": j.exit_code})
        jobs = {self._scrub(r["cmd"]) for r in out}  # as record_call writes a summary
        for i, c in enumerate(env.calls):
            summary = self._scrub(c.summary)
            if c.tool == "lane_exec":
                out.append({"id": f"exec-{i}", "cmd": summary, "state": "exited" if c.ok else "failed",
                            "started_at": _iso(c.at), "exit_code": None})
            elif c.tool == "lane_job_start" and c.ok and summary not in jobs:
                # a job whose record went with its lane (the machine was deleted, or the node restarted)
                out.append({"id": f"start-{i}", "cmd": summary, "state": "ended", "started_at": _iso(c.at),
                            "exit_code": None})
        out.sort(key=lambda r: r["started_at"] or "", reverse=True)
        return out[:50]

    def _viewer(self, env: Environment) -> dict | None:
        lanes = self._get("lanes")
        if lanes is None:
            return None
        try:
            if lanes.view(env.browser_lane) is None:
                return None
        except Exception:  # noqa: BLE001
            return None
        from agentos.api.web.accounts import lane_viewer_path

        return {"path": lane_viewer_path(WORKSPACE, f"{env.id}-browser")}

    async def detail(self, env: Environment, *, calls: int = 50) -> dict:
        """EnvDetail of the contract, and what environment() returns to an agent."""
        out = self.summary(env)
        repos = await self.git_repos(env) if env.open and env.state != "starting" and env.vm_id else None
        main = main_repo(repos, env.workdir)
        out["ports"] = await self.ports(env) if env.state in ("ready",) else []
        out["git"] = {"branch": (main or {}).get("branch") or env.branch, "ahead": (main or {}).get("ahead", 0),
                      "dirty": (main or {}).get("dirty", 0), "pushed": self.pushes(env)}
        out["branch"] = out["git"]["branch"]  # the branch checked out now, not the one it was cloned on
        out["calls"] = [{"at": _iso(c.at), "tool": c.tool, "summary": self._scrub(c.summary), "ok": c.ok}
                        for c in reversed(env.calls[-calls:])]
        out["commands"] = self._commands(env)
        out["viewer"] = self._viewer(env)
        out["unsaved"] = self.unsaved_of(repos)
        return out

    def links(self, env: Environment) -> dict:
        """The links an agent gives the human: `browser`, the browser's live view on the node itself
        (agentos.environments.tools fills it with a signed take-over link once the browser lane
        exists). No page of the web app: a node from the public repository has none."""
        return {"browser": None}

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
        out: dict[str, Any] = {"at": _iso(now), "closed": [], "kept_unsaved": [], "tunnels": [], "gone": []}
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


__all__ = ["OPEN_NEXT", "EnvironmentManager", "binding_of", "check_ref", "check_title", "parse_git_state",
           "parse_ports"]
