"""The runtime service "git": the gateway's listeners, and the git config lanes and brains get.

- The node's loopback listener (127.0.0.1, a port picked at start) serves brains (which run on
  the node's network as their own user) and lanes on the node's network.
- A lane with a network namespace of its own (network `isolated` or `off`) gets a listener at
  127.0.0.1:LANE_PORT inside it, opened the first time a command there needs git, closed when the
  lane goes cold. A request on it can only come from that lane. In a lane with network `off` it is
  the one way out, to the remotes declared for it.
- A command a grant starts in a lane (lane_exec, lane_job_start, so `agentos run`, a shell) gets a
  gateway token for that lane and grant, and GIT_CONFIG_* variables that point each declared host at
  the listener and add the token to requests to it. A brain task gets the same in its private
  `~/.gitconfig`, and the bundle's clone uses it too. Tokens end with the lane (cold) or the task.
- An agent environment's VM (agentos.environments) gets a listener of its own on the node's loopback,
  which only its SSH reverse tunnel reaches, and one token, written to the VM's git config: it
  fetches and pushes the environment's repository and nothing else, is refused on any other
  listener, and ends when the environment closes (`vm_down`). A VM lane's commands get no token in
  their environment (it would be on the ssh command line on the node).

Nothing here holds a credential: the vault keeps them, and the gateway opens one only for the
operation that uses it (agentos.git.upstream).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentos.core.config import home_from_env
from agentos.git.gateway import TOKEN_HEADER, Caller, GitGateway, Listener
from agentos.git.remotes import Remotes
from agentos.git.upstream import Target, config_env, explain

log = logging.getLogger("agentos.git")

SERVICE_NAME = "git"
LANE_PORT = 3129  # inside an isolated lane, next to the egress forwarder's 3128
HOST_PORT_ENV = "AGENTOS_GIT_PORT"  # the node's loopback port (0 or unset: any free port)
ALLOW_FILE_ENV = "AGENTOS_GIT_ALLOW_FILE"  # "1": remotes declared with a local path are served (dev and tests)


PROBE_TIMEOUT_S = 20.0  # access(): how long `git ls-remote` may take before an environment opens


@dataclass
class RepoAccess:
    """What GitService.access found: can a machine clone `slug`, and how (`via`: "gateway" for a
    declared repository with its credential, "public" for one read with no credential)."""
    slug: str
    ok: bool
    via: str | None
    declared: bool
    credential: str | None          # the vault credential's name, when there is one
    kind: str | None = None         # its type: "ssh" (a deploy key) or "token"
    stderr: bytes = b""
    why: str | None = None          # explain()'s words when it is not ok

    @property
    def unreachable(self) -> bool:
        from agentos.git.upstream import unreachable

        return not self.ok and unreachable(self.stderr)


async def public_access(repo: str, home: Path, *, probe: Any = None, timeout_s: float = PROBE_TIMEOUT_S
                        ) -> RepoAccess:
    """Can the node read `repo` over https with no credential at all (a public repository)?"""
    from agentos.git.upstream import base_env, ls_remote
    from agentos.git.wire import parse_remote

    remote = parse_remote(repo)
    public = Target(remote, f"https://{remote.host}/{remote.repo}.git", None)
    env = {**base_env(Path(home)), **config_env([("credential.helper", "")])}
    ok, err = await (probe or ls_remote)(public.url, env, timeout_s=timeout_s)
    return RepoAccess(remote.slug, ok, via="public" if ok else None, declared=False, credential=None,
                      stderr=err, why=None if ok else explain(err, public))


def agent_dir(home: Path) -> Path:
    """A private directory for the SSH agent's sockets, short enough for a unix socket path."""
    from agentos.ops.layout import SUN_PATH_MAX, check_socket_dir, short_run_dir

    candidate = Path(home) / "run" / "git"
    if len(os.fsencode(str(candidate / "agent-000000000000" / "s"))) > SUN_PATH_MAX:
        candidate = short_run_dir(home) / "git"
        check_socket_dir(candidate.parent, create=True)
    check_socket_dir(candidate, create=True)
    return candidate


def rewrite_pairs(base: str, hosts: list[str], token: str) -> list[tuple[str, str]]:
    """Git config that sends every remote on `hosts` to the gateway at `base`, with the token on
    each request to it (and to nothing else)."""
    pairs: list[tuple[str, str]] = [("credential.helper", "")]
    for h in hosts:
        key = f"url.{base}/{h}/.insteadOf"
        pairs += [(key, f"https://{h}/"), (key, f"git@{h}:"), (key, f"ssh://git@{h}/"), (key, f"http://{h}/")]
    pairs.append((f"http.{base}/.extraHeader", f"{TOKEN_HEADER}: {token}"))
    return pairs


def gitconfig_text(pairs: list[tuple[str, str]]) -> str:
    """The same as a ~/.gitconfig file (for a brain, whose CLI may scrub its environment)."""
    sections: dict[tuple[str, str | None], list[tuple[str, str]]] = {}
    for key, value in pairs:
        section, _, rest = key.partition(".")
        sub, _, name = rest.rpartition(".")
        sections.setdefault((section, sub or None), []).append((name, value))
    out = []
    for (section, subsection), items in sections.items():
        out.append(f'[{section} "{subsection}"]' if subsection else f"[{section}]")
        for name, value in items:
            out.append(f"\t{name} = {_quote(value)}")
    return "\n".join(out) + "\n"


def _quote(value: str) -> str:
    if value == "" or value != value.strip() or any(c in value for c in ';#"\\'):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


class GitService:
    name = SERVICE_NAME

    def __init__(self, runtime: Any, home: Path, *, allow_file: bool = False, host_port: int | None = None,
                 lane_port: int = LANE_PORT):
        self.runtime = runtime
        self.home = Path(home)
        self.remotes = Remotes(self.home / "git" / "remotes.json")
        self.gateway = GitGateway(home=self.home, remotes=self.remotes, runtime=runtime, allow_file=allow_file,
                                  agent_dir=None)
        self.lane_port = lane_port
        self._want_port = host_port if host_port is not None else int(os.environ.get(HOST_PORT_ENV) or 0)
        self.host_port: int | None = None
        self._host = Listener(self.gateway, None)
        self._start_lock: asyncio.Lock | None = None
        self._lane_locks: dict[str, asyncio.Lock] = {}
        self._vm_listeners: dict[str, tuple[Listener, int]] = {}  # VM lane -> its listener and port

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if self.host_port is not None:
                return
            self.gateway.agent_dir = await asyncio.to_thread(agent_dir, self.home)
            self.host_port = await self._host.listen("127.0.0.1", self._want_port)
            log.info("git gateway on 127.0.0.1:%s", self.host_port)

    async def stop(self) -> None:
        await self._host.close()
        self.host_port = None

    @property
    def host_base(self) -> str:
        return f"http://127.0.0.1:{self.host_port}"

    # ------------------------------------------------------------------ env lanes
    async def lane_env(self, rt: Any, grant_id: str | None) -> dict[str, str]:
        """GIT_CONFIG_* for a command `grant_id` starts in the warm lane `rt` (agentos.env), or {}
        when the lane has no declared remote or no grant behind the command. A lane with its own
        network namespace (network `isolated`, or `off`: a declared remote is the owner's yes to
        reach that one repository, and nothing else, through the node) gets the listener there."""
        lane = rt.lane
        if not grant_id or getattr(lane, "remote", None) is not None:
            # a VM lane: its git config is on the VM (an environment's, from vm_token), never in a
            # command's environment, which would put the token on the ssh command line on the node
            return {}
        hosts = self.remotes.hosts_for([lane.id, lane.workspace])
        if not hosts:
            return {}
        await self.start()
        if getattr(rt, "netns", None) is not None:
            lock = self._lane_locks.setdefault(lane.id, asyncio.Lock())
            async with lock:
                if getattr(rt, "git_port", None) is None:
                    listener = Listener(self.gateway, lane.id)
                    rt.git_port = await rt.listen(listener, self.lane_port)
                    rt.side_servers.append(listener)
                    rt.on_down.append(lambda lane_id=lane.id: self.lane_down(lane_id))
            base = f"http://127.0.0.1:{rt.git_port}"
            caller_lane = lane.id
        else:
            base = self.host_base
            caller_lane = lane.id
        token = self.gateway.token_for(Caller(grant_id=grant_id, workspace=lane.workspace, lane=caller_lane,
                                              lanes=(), label=f"lane {lane.id}"))
        return {**config_env(rewrite_pairs(base, hosts, token)), "GIT_TERMINAL_PROMPT": "0"}

    # ------------------------------------------------------------------ environment VMs
    async def vm_listener(self, lane_id: str) -> int:
        """The node loopback port of the listener for VM lane `lane_id` (opened once): its reverse
        tunnel forwards the VM's 127.0.0.1:3129 here, and a request on it is that VM's alone."""
        lock = self._lane_locks.setdefault(lane_id, asyncio.Lock())
        async with lock:
            held = self._vm_listeners.get(lane_id)
            if held is not None:
                return held[1]
            self.gateway.agent_dir = await asyncio.to_thread(agent_dir, self.home)
            listener = Listener(self.gateway, lane_id)
            port = await listener.listen("127.0.0.1", 0)
            self._vm_listeners[lane_id] = (listener, port)
            return port

    def vm_token(self, lane_id: str, *, grant_id: str, repo: str) -> str | None:
        """The one gateway token an environment's VM holds: `repo` only (fetch, and push under the
        gateway's policy), on the VM's own listener only. None when `repo` is not declared for the
        lane or workspace env, or the VM has no listener: then the VM holds no token at all."""
        from agentos.git.wire import parse_remote

        if lane_id not in self._vm_listeners:
            return None
        remote = parse_remote(repo)
        workspace = lane_id.split("/", 1)[0]
        if self.remotes.find(remote, [lane_id, workspace]) is None:
            return None
        return self.gateway.token_for(Caller(grant_id=grant_id, workspace=workspace, lane=lane_id, lanes=(),
                                             label=f"environment {lane_id}", repos=(remote.slug,), bound=True))

    async def vm_down(self, lane_id: str) -> None:
        """The environment closed: its token ends, and its listener closes."""
        self.gateway.revoke_where(lambda c: c.lane == lane_id)
        held = self._vm_listeners.pop(lane_id, None)
        self._lane_locks.pop(lane_id, None)
        if held is not None:
            await held[0].close()

    def lane_down(self, lane_id: str) -> None:
        """The lane went cold: its tokens end with it."""
        self.gateway.revoke_where(lambda c: c.lane == lane_id)
        self._lane_locks.pop(lane_id, None)

    async def access(self, repo: str, scopes: list[str], *, probe: Any = None,
                     timeout_s: float = PROBE_TIMEOUT_S) -> RepoAccess:
        """Can a machine in `scopes` clone `repo`, checked from the node before anything is rented
        (agentos.environments): declared for one of the scopes, with the vault's credential for it
        (the gateway's way, `git ls-remote` with that credential, as a fetch would), or readable with
        no credential at all (a public repository, cloned straight from the machine). `probe` is
        upstream.ls_remote (tests pass their own)."""
        from agentos.git import keys as keymod
        from agentos.git.upstream import UpstreamError, credential_env, ls_remote, target_for
        from agentos.git.wire import parse_remote

        probe = probe or ls_remote
        remote = parse_remote(repo)
        rule = self.remotes.find(remote, scopes)
        if rule is None:
            return await public_access(remote.slug, self.home, probe=probe, timeout_s=timeout_s)
        cred = keymod.pick(await self.gateway.credentials(), remote)
        target = target_for(rule, remote, cred)
        if self.gateway.agent_dir is None:
            self.gateway.agent_dir = await asyncio.to_thread(agent_dir, self.home)
        try:
            async with credential_env(target, home=self.home, agent_dir=self.gateway.agent_dir,
                                      read_secret=self.gateway._read_secret,
                                      purpose=f"git check {remote.slug} before an environment opens") as auth:
                ok, err = await probe(target.url, auth.env, timeout_s=timeout_s, allow_file=self.gateway.allow_file)
                why = None if ok else explain(err, target, scrub=auth.scrub)
        except UpstreamError as e:
            ok, err, why = False, b"", e.message
        return RepoAccess(remote.slug, ok, via="gateway" if ok else None, declared=True,
                          credential=cred.name if cred else None, kind=cred.type if cred else None, stderr=err,
                          why=why)

    # ------------------------------------------------------------------ brain tasks
    async def task_pairs(self, task_id: str, *, grant_id: str | None, workspace: str, lanes: list[str],
                         ttl_s: float) -> list[tuple[str, str]]:
        """Git config for brain task `task_id` (its ~/.gitconfig, and the bundle's clone), or [] when
        its workspace and lanes have no declared remote."""
        hosts = self.remotes.hosts_for([*lanes, workspace])
        if not hosts or not grant_id:
            return []
        await self.start()
        caller = Caller(grant_id=grant_id, workspace=workspace, lane=None, lanes=tuple(lanes),
                        label=f"brain task {task_id}", expires=self.gateway.clock() + ttl_s, task=task_id)
        return rewrite_pairs(self.host_base, hosts, self.gateway.token_for(caller))

    def task_end(self, task_id: str) -> None:
        self.gateway.revoke_where(lambda c: c.task == task_id)

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        """The loopback port and how many remotes are declared (no secret, nothing per caller)."""
        return {"port": self.host_port, "remotes": len(self.remotes.list())}


def vm_gitconfig(repo: str, token: str) -> str:
    """The git config an environment's VM gets (agentos.environments, through stdin): `repo`, in each
    way it can be written, goes to the gateway at the VM's 127.0.0.1:LANE_PORT, with the token on
    requests there and nowhere else. Every other repository is fetched straight from the VM."""
    from agentos.git.wire import parse_remote

    remote = parse_remote(repo)
    base = f"http://127.0.0.1:{LANE_PORT}"
    to = f"{base}/{remote.host}/{remote.repo}"
    key = f"url.{to}.insteadOf"
    pairs = [(key, f"https://{remote.host}/{remote.repo}"), (key, f"http://{remote.host}/{remote.repo}"),
             (key, f"git@{remote.host}:{remote.repo}"), (key, f"ssh://git@{remote.host}/{remote.repo}"),
             (f"http.{base}/.extraHeader", f"{TOKEN_HEADER}: {token}"), (f"credential.{base}.helper", "")]
    return gitconfig_text(pairs)


def build(runtime: Any) -> GitService:
    cfg = runtime.get("config") if runtime is not None else None
    home = Path(cfg.home) if cfg is not None else home_from_env()
    # AGENTOS_GIT_ALLOW_FILE=1: a remote declared with a local path (a bare repository on this
    # computer), for a dev node and its tests; never on a node that serves others
    service = GitService(runtime, home, allow_file=os.environ.get(ALLOW_FILE_ENV) == "1")
    if runtime is not None:
        runtime.register(SERVICE_NAME, service)
    return service
