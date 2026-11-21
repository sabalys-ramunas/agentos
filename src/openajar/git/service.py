"""The runtime service "git": the gateway's listeners, and the git config lanes and brains get.

- The node's loopback listener (127.0.0.1, a port picked at start) serves brains (which run on
  the node's network as their own user) and lanes on the node's network.
- A lane with a network namespace of its own (network `isolated` or `off`) gets a listener at
  127.0.0.1:LANE_PORT inside it, opened the first time a command there needs git, closed when the
  lane goes cold. A request on it can only come from that lane. In a lane with network `off` it is
  the one way out, to the remotes declared for it.
- A command a grant starts in a lane (terminal_exec, terminal_job_start, so `openajar run`, a shell) gets a
  gateway token for that lane and grant, and GIT_CONFIG_* variables that point each declared host at
  the listener and add the token to requests to it. A brain task gets the same in its private
  `~/.gitconfig`, and the bundle's clone uses it too. Tokens end with the lane (cold) or the task.
- An agent environment's VM (openajar.environments) gets a listener of its own on the node's loopback,
  which only its SSH reverse tunnel reaches, and one token, written to the VM's git config: it
  fetches and pushes the environment's repository and nothing else, is refused on any other
  listener, and ends when the environment closes (`vm_down`). A VM lane's commands get no token in
  their environment (it would be on the ssh command line on the node).

Nothing here holds a credential: the vault keeps them, and the gateway opens one only for the
operation that uses it (openajar.git.upstream).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openajar.core.config import home_from_env
from openajar.git.gateway import TOKEN_HEADER, Caller, GitGateway, Listener
from openajar.git.login import LoginRemotes
from openajar.git.upstream import Target, config_env, explain

log = logging.getLogger("openajar.git")

SERVICE_NAME = "git"
LANE_PORT = 3129  # inside an isolated lane, next to the egress forwarder's 3128
HOST_PORT_ENV = "OPENAJAR_GIT_PORT"  # the node's loopback port (0 or unset: any free port)
ALLOW_FILE_ENV = "OPENAJAR_GIT_ALLOW_FILE"  # "1": remotes declared with a local path are served (dev and tests)


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
    wide: bool = False              # a token that sees more than this repository: the GitHub login, or owner/*
    login: str | None = None        # the GitHub login's account name, when that is the credential

    @property
    def unreachable(self) -> bool:
        from openajar.git.upstream import unreachable

        return not self.ok and unreachable(self.stderr)

    @property
    def not_found(self) -> bool:
        """The host answered that the repository is not there (GitHub says so for one a token cannot see)."""
        low = self.stderr.decode("utf-8", "replace").lower()
        return not self.ok and ("repository not found" in low or ("fatal: repository" in low and "not found" in low))


async def public_access(repo: str, home: Path, *, probe: Any = None, timeout_s: float = PROBE_TIMEOUT_S
                        ) -> RepoAccess:
    """Can the node read `repo` over https with no credential at all (a public repository)?"""
    from openajar.git.upstream import base_env, ls_remote
    from openajar.git.wire import parse_remote

    remote = parse_remote(repo)
    public = Target(remote, f"https://{remote.host}/{remote.repo}.git", None)
    env = {**base_env(Path(home)), **config_env([("credential.helper", "")])}
    ok, err = await (probe or ls_remote)(public.url, env, timeout_s=timeout_s)
    return RepoAccess(remote.slug, ok, via="public" if ok else None, declared=False, credential=None,
                      stderr=err, why=None if ok else explain(err, public))


def agent_dir(home: Path) -> Path:
    """A private directory for the SSH agent's sockets, short enough for a unix socket path."""
    from openajar.core.layout import SUN_PATH_MAX, check_socket_dir, short_run_dir

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
        # the declared remotes, and any github.com repository for env while the GitHub login says so
        self.remotes = LoginRemotes(self.home / "git" / "remotes.json", self.home)
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
        """GIT_CONFIG_* for a command `grant_id` starts in the warm lane `rt` (openajar.env), or {}
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

    def vm_token(self, lane_id: str, *, grant_id: str, repo: str, fetch_only: bool = False) -> str | None:
        """The one gateway token an environment's VM holds: `repo` only (fetch, and push under the
        gateway's policy; with `fetch_only`, never a push), on the VM's own listener only. None when
        `repo` is not declared for the lane or workspace env, or the VM has no listener: then the VM
        holds no token at all."""
        if lane_id not in self._vm_listeners:
            return None
        caller = self.vm_caller(lane_id, grant_id=grant_id, repo=repo, fetch_only=fetch_only)
        return None if caller is None else self.gateway.token_for(caller)

    def vm_caller(self, lane_id: str, *, grant_id: str, repo: str, fetch_only: bool = False) -> Caller | None:
        """Who an environment's VM is to the gateway: `repo` only, bound to its own listener. None when
        `repo` is not declared for the lane or its workspace."""
        from openajar.git.wire import parse_remote

        remote = parse_remote(repo)
        workspace = lane_id.split("/", 1)[0]
        if self.remotes.find(remote, [lane_id, workspace]) is None:
            return None
        return Caller(grant_id=grant_id, workspace=workspace, lane=lane_id, lanes=(),
                      label=f"environment {lane_id}", repos=(remote.slug,), bound=True, fetch_only=fetch_only)

    async def warm(self, lane_id: str, *, grant_id: str, repo: str) -> bool:
        """Bring the mirror of an environment's repository up to date while its VM boots, under the same
        checks as the VM's own fetch (GitGateway.warm). False when it is not declared or was refused."""
        from openajar.git.wire import parse_remote

        caller = self.vm_caller(lane_id, grant_id=grant_id, repo=repo)
        if caller is None:
            return False
        self.gateway.agent_dir = await asyncio.to_thread(agent_dir, self.home)
        return await self.gateway.warm(caller, parse_remote(repo))

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
        (openajar.environments): declared for one of the scopes, with the vault's credential for it
        (the gateway's way, `git ls-remote` with that credential, as a fetch would), or readable with
        no credential at all (a public repository, cloned straight from the machine). `probe` is
        upstream.ls_remote (tests pass their own)."""
        from openajar.git import login
        from openajar.git.upstream import UpstreamError, credential_env, ls_remote, target_for
        from openajar.git.wire import parse_remote

        probe = probe or ls_remote
        remote = parse_remote(repo)
        rule = self.remotes.find(remote, scopes)
        if rule is None:
            return await public_access(remote.slug, self.home, probe=probe, timeout_s=timeout_s)
        cred = login.pick(await self.gateway.credentials(), remote, rule.scope)
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
        wide = cred is not None and cred.type == "token" and (cred.any_repo or not cred.exact(remote))
        saved = login.load(self.home) if cred is not None and cred.any_repo else None
        return RepoAccess(remote.slug, ok, via="gateway" if ok else None, declared=True,
                          credential=cred.name if cred else None, kind=cred.type if cred else None, stderr=err,
                          why=why, wide=wide, login=(saved.login or None) if saved else None)

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


def vm_gitconfig(repo: str, token: str, *, fetch_direct: bool = False) -> str:
    """The git config an environment's VM gets (openajar.environments, through stdin): `repo`, in each
    way it can be written, goes to the gateway at the VM's 127.0.0.1:LANE_PORT, with the token on
    requests there and nowhere else. Every other repository is fetched straight from the VM.

    `fetch_direct` (a public repository): only pushes go to the gateway (pushInsteadOf), with all its
    rules; fetches go straight to the host over https, which needs no credential (the ssh forms are
    rewritten to it). The VM still holds no credential for the host, so a push can only go through the
    gateway."""
    from openajar.git.wire import parse_remote

    remote = parse_remote(repo)
    base = f"http://127.0.0.1:{LANE_PORT}"
    to = f"{base}/{remote.host}/{remote.repo}"
    forms = [f"https://{remote.host}/{remote.repo}", f"http://{remote.host}/{remote.repo}",
             f"git@{remote.host}:{remote.repo}", f"ssh://git@{remote.host}/{remote.repo}"]
    if fetch_direct:
        pairs = [(f"url.{to}.pushInsteadOf", f) for f in forms]
        pairs += [(f"url.https://{remote.host}/{remote.repo}.insteadOf", f) for f in forms[1:]]
    else:
        pairs = [(f"url.{to}.insteadOf", f) for f in forms]
    pairs += [(f"http.{base}/.extraHeader", f"{TOKEN_HEADER}: {token}"), (f"credential.{base}.helper", "")]
    return gitconfig_text(pairs)


def build(runtime: Any) -> GitService:
    cfg = runtime.get("config") if runtime is not None else None
    home = Path(cfg.home) if cfg is not None else home_from_env()
    # OPENAJAR_GIT_ALLOW_FILE=1: a remote declared with a local path (a bare repository on this
    # computer), for a dev node and its tests; never on a node that serves others
    service = GitService(runtime, home, allow_file=os.environ.get(ALLOW_FILE_ENV) == "1")
    if runtime is not None:
        runtime.register(SERVICE_NAME, service)
    return service
