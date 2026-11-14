"""connect, disconnect, nodes and status: the steps, in an order that is safe to repeat.

connect: read the node over SSH (its port and its own token, kept in memory) -> install or keep
the tunnel -> save the record -> check an MCP initialize through the tunnel -> with the node
token, ask the node for the owner's device credential and the connection's agent grant -> store
the grant token (for the agents) and the device credential (for the owner's CLI tools) in two
separate items -> check the grant with an MCP initialize -> write the headers helper -> register
with Claude Code. The node token is never stored on this computer.

Running it again converges on the same state: an unchanged, healthy tunnel is left alone, a
working grant and device credential are kept (a changed ceiling, client or `--new-grant` issues a
new grant and revokes the old one), and the Claude Code entry is replaced rather than duplicated.
A connection made before the split (it holds the node token) is moved to a grant by running
connect again, or `agentos connect --upgrade` for all of them.
"""

from __future__ import annotations

import re
import shutil
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from agentos.client import skills
from agentos.client.bridge import bridge_command
from agentos.client.claude import ClaudeCode, manual_lines
from agentos.client.codex import CodexConfig
from agentos.client.codex import installed as codex_installed
from agentos.client.errors import ClientError, NodeMissing, NotConnected, VerifyError
from agentos.client.install import RemoteInstaller, install_lines, install_source, refusal
from agentos.client.mcp_http import Failure, McpError, McpSession, ToolError, probe
from agentos.client.owner import NodePanel, PanelError
from agentos.client.ports import DEFAULT_LOCAL_PORT, pick_port, wait_free
from agentos.client.remote import NodeSettings, read_node, valid_host
from agentos.client.state import (
    DEFAULT_REMOTE_HOME,
    Connection,
    ConnectionStore,
    Paths,
    ServiceKind,
    TokenStoreKind,
    current_platform,
    default_name,
    shown,
    valid_name,
)
from agentos.client.tokens import (
    OWNER_SERVICE,
    FileStore,
    KeychainStore,
    TokenStore,
    helper_command,
    install_helper,
)
from agentos.client.tunnel import LaunchdService, NoTunnel, ServiceState, SystemdService, TunnelSpec
from agentos.mcp import toolsets

VERIFY_SECONDS = 25.0
PROBE_TIMEOUT = 5.0
DEFAULT_CEILING = "commit"
DEFAULT_AGENT_TTL = "90d"
DEFAULT_CLIENT = "claude-code"
# memory_read: the agent reads a workspace's memory first (memory_context), as the node's instructions
# ask; writing memory stays a special the human adds with --special memory. environments: the agent
# may open its own cloud computer for the chat (environment_open, which never asks: the node's limits
# bound it) and run commands there, on lanes env/* only
DEFAULT_SPECIALS = ["environments", "memory_read"]
NEWER_SPECIALS = ("environments", "memory_read")  # names an older node refuses
CEILINGS = ("read", "draft", "commit", "pay", "irreversible")
CLIENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
TTL_RE = re.compile(r"^[1-9][0-9]{0,4}[mhdw]$")

Service = LaunchdService | SystemdService | NoTunnel


@dataclass
class Tools:
    ssh: str | None = None
    security: str | None = None
    launchctl: str | None = None
    systemctl: str | None = None
    journalctl: str | None = None
    claude: str | None = None
    rsync: str | None = None

    @classmethod
    def find(cls) -> Tools:
        return cls(**{name: shutil.which(name) for name in cls.__dataclass_fields__})


def computer_name() -> str:
    """This computer, for the agent grant's subject and the device label: `Robin-MacBook.local`
    becomes `robin-macbook`."""
    host = socket.gethostname().split(".")[0].lower()
    return re.sub(r"[^a-z0-9-]+", "-", host).strip("-")[:40] or "computer"


def parse_toolsets(value: str) -> list[str] | None:
    """`--toolsets files,flows` -> ["core", "files", "flows"] (core is always listed), "full" or
    "all" -> ["full"], "default" -> None (whatever the node lists by default)."""
    words = [w for w in re.split(r"[\s,]+", value.strip().lower()) if w]
    if not words:
        raise ClientError("--toolsets needs toolset names, full or default",
                          f"toolsets: {', '.join(toolsets.NAMES)}")
    if words == ["default"]:
        return None
    if set(words) & toolsets.FULL_ALIASES:
        return [toolsets.FULL]
    known = (*toolsets.NAMES, toolsets.OTHER)
    unknown = [w for w in words if w not in known]
    if unknown:
        raise ClientError(f"--toolsets: no toolset named {', '.join(unknown[:3])}",
                          f"toolsets: {', '.join(known)}, or full (every tool) or default (the node's)")
    return [name for name in known if name == toolsets.CORE or name in words]


@dataclass
class AgentOptions:
    """What the agent grant of a connection says. None: what the connection had, else the default."""
    ceiling: str | None = None
    ttl: str | None = None
    client: str | None = None
    specials: list[str] | None = None
    new_grant: bool = False
    toolsets: str | None = None  # `--toolsets`: names, "full", or "default"; None keeps what it had

    def chosen_toolsets(self, previous: list[str] | None) -> list[str] | None:
        """The toolsets the connection's URL asks for: None is the node's own default."""
        if self.toolsets is None:
            return previous
        return parse_toolsets(self.toolsets)

    def check(self) -> None:
        if self.toolsets is not None:
            parse_toolsets(self.toolsets)
        if self.ceiling is not None and self.ceiling not in CEILINGS:
            raise ClientError(f"--ceiling must be one of {', '.join(CEILINGS)}")
        if self.ttl is not None and not TTL_RE.match(self.ttl):
            raise ClientError("--agent-ttl looks like 30d, 12w or 8760h")
        if self.client is not None and not CLIENT_RE.match(self.client):
            raise ClientError("--client is a short lowercase name, for example claude-code or codex")


@dataclass
class ClientChoice:
    """Which of this computer's agent clients `connect` sets up (`--no-claude`, `--no-codex`,
    `--no-skills`). Each one is set up only when it is installed here."""
    claude: bool = True
    codex: bool = True
    skills: bool = True


@dataclass
class InstallChoice:
    """What connect does on a host with no node: ask once (`confirm`, None when nobody can answer),
    install without asking (`--install`), or never."""
    mode: Literal["ask", "yes", "no"] = "ask"
    confirm: Callable[[str], bool] | None = None


@dataclass
class ClientLine:
    """One line of connect's summary: a client and what happened to it."""
    client: str   # "Claude Code", "Codex", "Skills"
    state: str    # registered, already registered, not installed, skipped, failed, installed, updated
    detail: str = ""


@dataclass
class ConnectResult:
    conn: Connection
    tunnel_reused: bool
    token_changed: bool
    claude_lines: list[str] = field(default_factory=list)  # set when it must be registered by hand
    claude_error: str | None = None
    grant_issued: bool = False
    notes: list[str] = field(default_factory=list)  # warnings to show after the summary
    clients: list[ClientLine] = field(default_factory=list)
    installed: bool = False                # connect installed the node on the host first
    sign_in: str | None = None             # then: a one-time panel link through the tunnel


@dataclass
class CheckItem:
    what: str
    state: Literal["ok", "fail", "skip"]
    detail: str
    fix: str | None = None


@dataclass
class Health:
    conn: Connection
    service: ServiceState
    mcp: str
    ok: bool = False
    server: dict[str, Any] = field(default_factory=dict)


class Client:
    def __init__(self, paths: Paths | None = None, platform: str | None = None, tools: Tools | None = None,
                 out: Callable[[str], None] = print):
        self.paths = paths or Paths.current()
        self.platform = platform or current_platform()
        self.tools = tools or Tools.find()
        self.out = out
        self.records = ConnectionStore(self.paths)

    # ---- building blocks ----

    def _require(self, tool: str, hint: str) -> str:
        path = getattr(self.tools, tool)
        if not path:
            raise ClientError(f"`{tool}` is not on PATH", hint)
        return path

    def store_kind(self, requested: Literal["auto", "keychain", "file"]) -> TokenStoreKind:
        if requested == "auto":
            return "keychain" if self.platform == "macos" else "file"
        if requested == "keychain" and self.platform != "macos":
            raise ClientError("the Keychain store needs macOS", "use --token-store file")
        return requested

    def tokens(self, kind: TokenStoreKind) -> TokenStore:
        """The agents' item: the grant token Claude Code sends (the node token on old connections)."""
        if kind == "keychain":
            return KeychainStore(self._require("security", "the macOS `security` tool is missing; "
                                                           "use --token-store file"))
        return FileStore(self.paths)

    def owner_tokens(self, kind: TokenStoreKind) -> TokenStore:
        """The owner's item: the device credential for agentos dash, approvals and open."""
        if kind == "keychain":
            return KeychainStore(self._require("security", "the macOS `security` tool is missing; "
                                                           "use --token-store file"), service=OWNER_SERVICE)
        return FileStore(self.paths, owner=True)

    def service_kind(self) -> ServiceKind:
        return "launchd" if self.platform == "macos" else "systemd"

    def service(self, kind: ServiceKind | None = None) -> Service:
        kind = kind or self.service_kind()
        if kind == "none":
            return NoTunnel()
        if kind == "launchd":
            return LaunchdService(self.paths, self._require("launchctl", "launchd is part of macOS; is this a Mac?"))
        systemctl = self._require(
            "systemctl", "no systemd here, so keep the tunnel up yourself, for example with "
                         "`autossh -M 0 -N -L 7400:localhost:7400 <host>`")
        return SystemdService(self.paths, systemctl, self.tools.journalctl)

    def _answers(self, port: int, token: str) -> bool:
        try:
            probe(port, token, timeout=PROBE_TIMEOUT)
        except McpError:
            return False
        return True

    def _wait_for_tunnel(self, service: Service, spec: TunnelSpec, token: str, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        last: McpError | None = None
        while True:
            try:
                probe(spec.local_port, token, timeout=PROBE_TIMEOUT)
                return
            except McpError as exc:
                last = exc
                if exc.failure is Failure.UNAUTHORIZED:
                    raise VerifyError(
                        f"the node refused the token from its own node.toml ({exc.detail})",
                        f"agentosd may still run with an older token: restart it on {spec.host} "
                        "(`agentos node restart`) and run connect again") from None
                if exc.failure is Failure.BAD_RESPONSE:
                    raise VerifyError(
                        f"localhost:{spec.local_port} answered, but not as an agentos node: {exc.detail}",
                        "another program may be forwarding this port; try --local-port") from None
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        log = [f"  {line}" for line in service.log_tail(spec.name)]
        details = (["Last lines of the tunnel log:", *log] if log else [])
        if last and last.failure is Failure.NO_ANSWER:
            raise VerifyError(
                f"the tunnel is up, but nothing answers on port {spec.remote_port} on {spec.host}",
                f"is agentosd running there? (`ssh {spec.host} systemctl status agentosd`). The tunnel "
                "stays installed; run connect again once it runs", details)
        raise VerifyError(
            f"the tunnel did not come up in {int(seconds)} seconds",
            f"the tunnel stays installed and keeps retrying; check {service.describe(spec.name)}, "
            f"or undo with `agentos connect --remove {spec.name}`", details)

    # ---- commands ----

    def connect(self, ssh_host: str, *, name: str | None = None, local_port: int | None = None,
                remote_home: str = DEFAULT_REMOTE_HOME,
                token_store: Literal["auto", "keychain", "file"] = "auto",
                verify_seconds: float | None = None, agent: AgentOptions | None = None,
                clients: ClientChoice | None = None, install: InstallChoice | None = None) -> ConnectResult:
        host = valid_host(ssh_host)
        name = valid_name(name or default_name(host))
        agent = agent or AgentOptions()
        agent.check()
        ssh = self._require("ssh", "install OpenSSH")
        kind = self.store_kind(token_store)
        tokens, owner = self.tokens(kind), self.owner_tokens(kind)
        service = self.service()
        previous = self.records.get(name)
        # read both items first: a locked Keychain fails here, before anything is installed
        held = (tokens.get(name), owner.get(name)) if previous is None or previous.token_store == kind else \
            (self.tokens(previous.token_store).get(name), self.owner_tokens(previous.token_store).get(name))

        self.out(f"Reading the MCP settings of {host} over SSH...")
        installed = False
        try:
            node = read_node(ssh, host, remote_home)
        except NodeMissing as missing:
            self.install_node(ssh, host, remote_home, install or InstallChoice(), missing)
            installed = True
            self.out(f"Reading the MCP settings of {host} again...")
            node = read_node(ssh, host, remote_home)
        self.out(f"  node {node.name or 'unnamed'}, MCP on port {node.mcp_port} there")

        reserved = {c.local_port for c in self.records.all() if c.name != name}
        wanted = local_port or (previous.local_port if previous else DEFAULT_LOCAL_PORT)
        spec = TunnelSpec(name=name, ssh=ssh, host=host, local_port=wanted, remote_port=node.mcp_port)
        reused = (previous is not None and previous.local_port == wanted and service.matches(spec)
                  and service.state(name).running and self._answers(wanted, node.token))
        if reused:
            self.out(f"Tunnel already running: localhost:{wanted} -> {host}:{node.mcp_port}")
        else:
            if previous is not None or service.state(name).loaded:
                service.stop(name)  # frees our own port before we look for one
                if previous is not None:
                    wait_free(previous.local_port)
            port = pick_port(wanted, reserved, explicit=local_port is not None)
            if port != wanted:
                self.out(f"  local port {wanted} is in use, so this tunnel uses {port}")
            spec = TunnelSpec(name=name, ssh=ssh, host=host, local_port=port, remote_port=node.mcp_port)
            service.install(spec)
            self.out(f"Tunnel: {service.describe(name)}")
            self.out(f"  localhost:{port} -> {host}:{node.mcp_port}, restarted by "
                     f"{'launchd' if service.kind == 'launchd' else 'systemd'} whenever it drops")

        conn = (previous.model_copy(deep=True) if previous else
                Connection(name=name, ssh_host=host, remote_port=node.mcp_port, local_port=spec.local_port,
                           token_store=kind, service=service.kind))
        conn = conn.model_copy(update={"ssh_host": host, "remote_home": remote_home, "remote_port": node.mcp_port,
                                       "local_port": spec.local_port, "node_name": node.name, "token_store": kind,
                                       "service": service.kind,
                                       "toolsets": agent.chosen_toolsets(previous.toolsets if previous else None)})
        self.records.save(conn)  # saved before the checks, so disconnect can always clean up

        if not reused:
            self.out(f"Checking {conn.url} with an MCP initialize...")
            self._wait_for_tunnel(service, spec, node.token, verify_seconds or VERIFY_SECONDS)
        self.out("  the node answered")

        result = ConnectResult(conn=conn, tunnel_reused=reused, token_changed=False, installed=installed)
        if node.controller:
            result.token_changed = tokens.put(name, node.token)
            self._forget_agent(conn, "controller")
            self.out(f"Token {'stored in' if result.token_changed else 'already in'} {tokens.describe(name)}")
            self.out("  this is a controller: its token is the agents' own token, not the owner's "
                     "(the owner decides in the controller's panel)")
        else:
            self._credentials(conn, previous, node, tokens, owner, held, agent, result)
        if previous and previous.token_store != kind:
            for store in (self.tokens(previous.token_store), self.owner_tokens(previous.token_store)):
                store.delete(name)  # no stale copy in the store it moved from
        install_helper(self.paths, self.tools.security if self.platform == "macos" else None)
        self.records.save(conn)
        self.register_clients(conn, previous, clients or ClientChoice(), result)
        if installed and not node.controller:
            result.sign_in = RemoteInstaller(ssh, self.out).sign_in_link(host, remote_home, conn.local_port)
        return result

    def connect_local(self, *, port: int, token: str, node_name: str | None, home: str, name: str = "local",
                      clients: ClientChoice | None = None, wait_seconds: float = 60.0) -> ConnectResult:
        """`agentos up --here`: this computer is the node. The clients here get their own agent grant
        and talk to the node's loopback port straight away, with no SSH and no tunnel. Safe to repeat:
        a working grant is kept."""
        name = valid_name(name)
        kind = self.store_kind("auto")
        tokens, owner = self.tokens(kind), self.owner_tokens(kind)
        previous = self.records.get(name)
        held = (tokens.get(name), owner.get(name))
        deadline = time.monotonic() + wait_seconds
        while not self._answers(port, token):  # agentos up calls us just before the node serves
            if time.monotonic() >= deadline:
                raise VerifyError(f"the node did not answer on 127.0.0.1:{port} in {int(wait_seconds)} seconds",
                                  "see `agentos node logs`, then run `agentos up --here` again")
            time.sleep(0.5)
        conn = previous.model_copy(deep=True) if previous else Connection(
            name=name, ssh_host="localhost", remote_port=port, local_port=port, token_store=kind, service="none")
        conn = conn.model_copy(update={"remote_home": home, "remote_port": port, "local_port": port,
                                       "node_name": node_name, "token_store": kind, "service": "none"})
        self.records.save(conn)
        node = NodeSettings(mcp_port=port, token=token, name=node_name)
        result = ConnectResult(conn=conn, tunnel_reused=True, token_changed=False)
        self._credentials(conn, previous, node, tokens, owner, held, AgentOptions(), result)
        install_helper(self.paths, self.tools.security if self.platform == "macos" else None)
        self.records.save(conn)
        self.register_clients(conn, previous, clients or ClientChoice(), result)
        return result

    def connect_dev(self, *, port: int, token: str, subject: str, name: str = "dev",
                    clients: ClientChoice | None = None) -> ConnectResult:
        """`agentos dev up`: the fake node's agent grant, stored like any connection's, and the clients
        here registered at its loopback URL. Each start issues a new grant; the entries stay the same,
        since they read the token from the store."""
        name = valid_name(name)
        kind = self.store_kind("auto")
        tokens = self.tokens(kind)
        previous = self.records.get(name)
        conn = Connection(name=name, ssh_host="localhost", remote_home=DEV_HOME, remote_port=port,
                          local_port=port, node_name="dev", token_store=kind, service="none",
                          credential="agent_grant", agent_subject=subject, agent_ceiling="commit",
                          agent_specials=["memory_read"])
        self.records.save(conn)
        changed = tokens.put(name, token)
        install_helper(self.paths, self.tools.security if self.platform == "macos" else None)
        result = ConnectResult(conn=conn, tunnel_reused=True, token_changed=changed)
        self.register_clients(conn, previous, clients or ClientChoice(), result)
        return result

    def install_node(self, ssh: str, host: str, remote_home: str, choice: InstallChoice,
                     missing: NodeMissing) -> None:
        """A host with no node: check it can take one, say what goes there, ask once, install.
        Never on a host with any sign of agentos, and never without sudo (no password prompts)."""
        installer = RemoteInstaller(ssh, self.out, rsync=self.tools.rsync)
        facts = installer.probe(host, remote_home)
        if facts.has_agentos:
            raise ClientError(f"{host} has agentos ({', '.join(facts.installed) or 'a node.toml'}) but no node.toml "
                              f"at {remote_home}, so connect will not install over it",
                              "pass --remote-home with the node's AGENTOS_HOME, or set it up there with `agentos up`")
        if choice.mode == "no":
            raise missing
        if remote_home.rstrip("/") != DEFAULT_REMOTE_HOME:
            raise ClientError(f"{host} has no node, and the installer puts one in {DEFAULT_REMOTE_HOME}",
                              "leave out --remote-home to install there")
        why = refusal(host, facts)
        if why is not None:
            raise why
        source = install_source()
        self.out(f"{host} has no agentos node yet ({facts.os_label}, {facts.arch}). connect can install one there:")
        for line in install_lines(host, source):
            self.out(f"  {line}")
        if choice.mode == "ask":
            if choice.confirm is None:
                raise ClientError(f"{host} has no agentos node", "pass --install to install one there "
                                                                 "(--dry-run shows every command first)")
            if not choice.confirm(f"Install agentos on {host} now?"):
                raise ClientError("nothing was installed", f"run `agentos connect {host} --install` when you want it")
        self.out(f"Installing agentos on {host} (a few minutes: system packages, Python, the browser)...")
        installer.install(host, source, as_root=facts.sudo == "root")
        self.out(f"Installed: agentosd runs on {host} as the agentos user, in the hardened unit")

    def install_plan(self, ssh_host: str, remote_home: str = DEFAULT_REMOTE_HOME) -> list[str]:
        """`connect --dry-run`: what connect would run on the host, without changing anything. It runs
        only the read-only probe."""
        host = valid_host(ssh_host)
        ssh = self._require("ssh", "install OpenSSH")
        installer = RemoteInstaller(ssh, self.out, rsync=self.tools.rsync)
        self.out(f"Checking {host} over SSH (read only):")
        facts = installer.probe(host, remote_home)
        self.out(f"  {facts.os_label}, {facts.arch or '?'}, sudo: {facts.sudo}, "
                 f"agentos: {'yes' if facts.has_agentos else 'no'}")
        lines: list[str] = []
        if facts.has_agentos:
            lines.append(f"{host} has agentos already, so connect would install nothing there. It would read "
                         "node.toml over SSH, open the tunnel and register this computer's clients.")
            return lines
        why = refusal(host, facts)
        if why is not None:
            raise why
        source = install_source()
        if source.sends:
            self.out(f"connect --install would send this checkout ({source.checkout}) to the node and run, over "
                     "SSH (`sh -s`, each script on stdin):")
        else:
            self.out("connect --install would run, over SSH (`sh -s`, the script on stdin):")
        installer.show_plan(host, source, as_root=facts.sudo == "root")
        lines.append("Then it reads node.toml over SSH, opens the tunnel, issues this computer's agent grant and "
                     "registers the clients, as for any node. Nothing was changed.")
        return lines

    def register_clients(self, conn: Connection, previous: Connection | None, choice: ClientChoice,
                         result: ConnectResult) -> None:
        """Claude Code (a headersHelper entry), Codex (a stdio entry that runs `agentos mcp-bridge`)
        and the Claude Code skills, each only when it is installed here. No token goes into either
        config: both read it from this computer's store when they connect."""
        name = conn.name
        same = previous is not None and previous.url == conn.url
        helper = helper_command(self.paths, name)
        if not choice.claude:
            result.clients.append(ClientLine("Claude Code", "skipped", "--no-claude"))
        elif self.tools.claude:
            try:
                ClaudeCode(self.tools.claude, self.paths.home).register(conn, helper)
                conn.claude_registered = True
                state = "already registered" if same and previous and previous.claude_registered else "registered"
                result.clients.append(ClientLine("Claude Code", state, f"{conn.claude_name} (user scope)"))
            except ClientError as exc:
                result.claude_error = exc.message
                result.claude_lines = manual_lines(conn, helper)
                result.clients.append(ClientLine("Claude Code", "failed", exc.message))
        else:
            result.claude_lines = manual_lines(conn, helper)
            result.clients.append(ClientLine("Claude Code", "not installed", "`claude` is not on PATH"))

        codex = CodexConfig(self.paths.home)
        if not choice.codex:
            result.clients.append(ClientLine("Codex", "skipped", "--no-codex"))
        elif not codex_installed(self.paths.home):
            result.clients.append(ClientLine("Codex", "not installed", "no `codex` on PATH and no ~/.codex"))
        else:
            try:
                command, args = bridge_command(name)
                changed = codex.register(conn.claude_name, command, args)
                conn.codex_registered = True
                result.clients.append(ClientLine("Codex", "registered" if changed else "already registered",
                                                 f"{conn.claude_name} in {shown(codex.path)}"))
            except ClientError as exc:
                result.clients.append(ClientLine("Codex", "failed", exc.message))
                result.notes.append(f"Codex: {exc.message}" + (f" ({exc.hint})" if exc.hint else ""))

        if not choice.skills:
            result.clients.append(ClientLine("Skills", "skipped", "--no-skills"))
        elif self.tools.claude or skills.skills_dir(self.paths.home).parent.is_dir():
            try:
                done = skills.install(self.paths.home)
                conn.skills_installed = True
                result.clients.append(ClientLine("Skills", done, f"{', '.join(skills.NAMES)} in "
                                                                 f"{shown(skills.skills_dir(self.paths.home))}"))
            except OSError as exc:
                result.clients.append(ClientLine("Skills", "failed", f"could not copy them: {exc.strerror}"))
        else:
            result.clients.append(ClientLine("Skills", "not installed", "Claude Code is not here"))
        self.records.save(conn)

    @staticmethod
    def _forget_agent(conn: Connection, credential: str) -> None:
        conn.credential = credential  # type: ignore[assignment]
        for key in ("agent_grant_id", "agent_subject", "agent_ceiling", "agent_expires_at", "owner_device_id",
                    "owner_device_expires_at"):
            setattr(conn, key, None)
        conn.agent_specials = []

    def _credentials(self, conn: Connection, previous: Connection | None, node: NodeSettings, tokens: TokenStore,
                     owner: TokenStore, held: tuple[str | None, str | None], agent: AgentOptions,
                     result: ConnectResult) -> None:
        """With the node token (in memory only): the owner's device credential, then the agent grant.
        A node that has neither route (older agentos, or no policy) keeps the node token, with a note."""
        name = conn.name
        panel = NodePanel(conn.local_port)
        old_agent, old_device = held
        was_grant = previous is not None and previous.credential == "agent_grant"

        device, minted = None, None
        if old_device and was_grant:
            try:
                panel.whoami(old_device)
                device = old_device
            except PanelError as exc:
                if exc.status not in (401, 403):
                    raise
        if device is None:
            try:
                minted = panel.mint_device(node.token, label=computer_name())
            except PanelError as exc:
                if not exc.missing:
                    raise
                return self._keep_node_token(conn, node, tokens, result, exc)
            device = minted["session"]
            conn.owner_device_id, conn.owner_device_expires_at = minted.get("id"), minted.get("expires_at")

        was_client = (previous.agent_subject or "").split("@")[0] if was_grant and previous else ""
        subject = f"{agent.client or was_client or DEFAULT_CLIENT}@{computer_name()}"
        ceiling = agent.ceiling or (previous.agent_ceiling if was_grant and previous else None) or DEFAULT_CEILING
        specials = sorted(agent.specials if agent.specials is not None else
                          (previous.agent_specials if was_grant and previous else DEFAULT_SPECIALS))
        keep = (was_grant and old_agent and not agent.new_grant and agent.ttl is None and previous is not None
                and previous.agent_subject == subject and previous.agent_ceiling == ceiling
                and sorted(previous.agent_specials) == specials and self._answers(conn.local_port, old_agent))
        if keep:
            token = old_agent
            self.out(f"Agent grant {conn.agent_grant_id} kept: {subject}, every lane, up to {ceiling}")
        else:
            try:
                try:
                    issued = panel.issue_agent_grant(node.token, subject=subject, ceiling=ceiling, specials=specials,
                                                     ttl=agent.ttl or DEFAULT_AGENT_TTL)
                except PanelError as exc:
                    # a node older than these names refuses them: issue without them, as before
                    refused = [s for s in NEWER_SPECIALS if s in specials and s in exc.message]
                    if agent.specials is not None or not refused:
                        raise
                    specials = [s for s in specials if s not in refused]
                    issued = panel.issue_agent_grant(node.token, subject=subject, ceiling=ceiling, specials=specials,
                                                     ttl=agent.ttl or DEFAULT_AGENT_TTL)
            except PanelError as exc:
                if minted:
                    with _quiet():
                        panel.end_device(device)
                if not exc.missing:
                    raise
                return self._keep_node_token(conn, node, tokens, result, exc)
            token = issued["token"]
            conn.agent_grant_id = issued.get("id")
            conn.agent_expires_at = (issued.get("grant") or {}).get("expires_at")
            conn.agent_subject, conn.agent_ceiling, conn.agent_specials = subject, ceiling, specials
            result.grant_issued = True
            until = f", until {conn.agent_expires_at[:10]}" if conn.agent_expires_at else ""
            extra = f", specials {', '.join(specials)}" if specials else ", no specials"
            self.out(f"Agent grant {conn.agent_grant_id} issued for {subject}: every lane, up to {ceiling}{extra}{until}")

        try:
            result.token_changed = tokens.put(name, token)
            self.out(f"Token {'stored in' if result.token_changed else 'already in'} {tokens.describe(name)}")
            stored = owner.put(name, device)
            self.out(f"Owner credential {'stored in' if stored else 'already in'} {owner.describe(name)}")
            self.out("  agentos dash, agentos approvals and agentos open use it; MCP refuses it")
            try:
                probe(conn.local_port, token, timeout=PROBE_TIMEOUT)
            except McpError as exc:
                raise VerifyError(f"the node did not accept the agent grant it issued ({exc.detail})",
                                  f"run `agentos connect {conn.ssh_host} --name {name} --new-grant`") from None
        except ClientError:
            # nothing new may stay behind on the node when this computer could not keep it
            if result.grant_issued and conn.agent_grant_id:
                with _quiet():
                    panel.call("POST", f"/api/grants/{conn.agent_grant_id}/revoke", {}, bearer=node.token)
            if minted:
                with _quiet():
                    panel.end_device(device)
            raise
        conn.credential = "agent_grant"
        if conn.agent_grant_id:
            # lane_view then gives this computer's agents a link and tunnel that work here (best effort:
            # a node older than this answers 404 and keeps its own words)
            with _quiet():
                panel.record_route(node.token, conn.agent_grant_id, ssh_host=conn.ssh_host,
                                   local_port=conn.local_port)
        self.out("  the node accepted the agent grant; its own token was used only for these calls "
                 "and is not stored here")

        if result.grant_issued and was_grant and previous and previous.agent_grant_id \
                and previous.agent_grant_id != conn.agent_grant_id:
            try:
                panel.revoke_grant(device, previous.agent_grant_id)
                self.out(f"  revoked the previous agent grant {previous.agent_grant_id}")
            except PanelError as exc:
                if exc.status not in (404, 409):
                    result.notes.append(f"could not revoke the previous agent grant {previous.agent_grant_id}: "
                                        f"{exc.message}; revoke it on the node with `agentos grants revoke "
                                        f"{previous.agent_grant_id}`")
        if old_device and old_device != device:
            with _quiet():
                panel.end_device(old_device)
        if previous is not None and not was_grant:
            self.out("  moved from the node token to its own grant; rotate the node token once every "
                     f"connection is moved: `ssh {conn.ssh_host} agentos node rotate-token`")

    def _keep_node_token(self, conn: Connection, node: NodeSettings, tokens: TokenStore, result: ConnectResult,
                         why: PanelError) -> None:
        result.token_changed = tokens.put(conn.name, node.token)
        self._forget_agent(conn, "node_token")
        self.out(f"Token {'stored in' if result.token_changed else 'already in'} {tokens.describe(conn.name)}")
        result.notes.append(
            f"{conn.ssh_host} cannot issue agent grants yet ({why.message}), so this connection holds the node "
            "token and you cannot approve from here what its agents ask. Update agentos on the node, then run "
            "`agentos connect --upgrade`.")

    def upgrade(self, **kw: Any) -> list[tuple[Connection, ConnectResult | ClientError]]:
        """`agentos connect --upgrade`: every connection still on the node token, connected again."""
        out: list[tuple[Connection, ConnectResult | ClientError]] = []
        for conn in self.legacy():
            self.out(f"== {conn.name}")
            try:
                out.append((conn, self.connect(conn.ssh_host, name=conn.name, remote_home=conn.remote_home,
                                               token_store=conn.token_store, **kw)))
            except ClientError as exc:
                out.append((conn, exc))
        return out

    def legacy(self) -> list[Connection]:
        """Connections whose agents still hold the node's own token."""
        return [c for c in self.records.all() if c.credential == "node_token"]

    def disconnect(self, name: str) -> list[str]:
        """Revoke the agent grant and end the device credential on the node (through the tunnel,
        before it goes), then remove everything here. What could not be undone on the node is
        listed in parentheses, with the command that does it there."""
        name = valid_name(name)
        conn = self.records.get(name)
        removed: list[str] = []
        if conn is not None and conn.credential == "agent_grant":
            removed += self._revoke_on_node(conn)
        service = self.service(conn.service if conn else None)
        if service.uninstall(name):
            removed.append(f"tunnel {service.describe(name)}")
        kinds: list[TokenStoreKind] = [conn.token_store] if conn else []
        if self.platform == "macos" and "keychain" not in kinds and self.tools.security:
            kinds.append("keychain")
        if "file" not in kinds:
            kinds.append("file")
        for kind in kinds:
            store = self.tokens(kind)
            if store.delete(name):
                removed.append(f"token from {store.describe(name)}")
            store = self.owner_tokens(kind)
            if store.delete(name):
                removed.append(f"owner credential from {store.describe(name)}")
        claude_name = f"agentos-{name}"
        manual: str | None = None
        if self.tools.claude:
            if ClaudeCode(self.tools.claude, self.paths.home).unregister(claude_name):
                removed.append(f"Claude Code entry {claude_name}")
        elif conn is not None and conn.claude_registered:
            manual = f"claude mcp remove --scope user {claude_name}"
        codex = CodexConfig(self.paths.home)
        try:
            if codex.unregister(claude_name):
                removed.append(f"Codex entry {claude_name} from {shown(codex.path)}")
        except ClientError as exc:
            removed.append(f"(could not remove the Codex entry {claude_name}: {exc.message})")
        if self.records.delete(name):
            removed.append("connection record")
        if conn is not None and conn.skills_installed and not any(c.skills_installed for c in self.records.all()):
            gone = skills.remove(self.paths.home)
            if gone:
                removed.append(f"Claude Code skills {', '.join(gone)}")
        if not self.records.all() and self.paths.headers_helper.exists():
            self.paths.headers_helper.unlink()
        if not removed:
            raise NotConnected(f"no connection named {name}", "see `agentos status`")
        if manual:
            removed.append(f"(claude is not on PATH; remove its entry with `{manual}`)")
        return removed

    def _revoke_on_node(self, conn: Connection) -> list[str]:
        out: list[str] = []
        grant = conn.agent_grant_id
        on_node = f"`ssh {conn.ssh_host} agentos grants revoke {grant}`"
        try:
            device = self.owner_tokens(conn.token_store).get(conn.name)
        except ClientError as exc:
            device, why = None, exc.message
        else:
            why = "no owner credential is stored here"
        panel = NodePanel(conn.local_port)
        if grant and device:
            try:
                panel.revoke_grant(device, grant)
                out.append(f"agent grant {grant} (revoked on the node)")
            except PanelError as exc:
                if exc.status in (404, 409):
                    out.append(f"agent grant {grant} (already revoked or gone on the node)")
                else:
                    out.append(f"(could not revoke agent grant {grant} on the node: {exc.message}; "
                               f"revoke it there with {on_node})")
        elif grant:
            out.append(f"(could not revoke agent grant {grant} on the node: {why}; revoke it there with {on_node})")
        if device:
            try:
                panel.end_device(device)
                out.append("owner device credential (ended on the node)")
            except PanelError as exc:
                if exc.status != 401:
                    out.append(f"(could not end the owner device credential on the node: {exc.message}; "
                               "it ends by itself when it expires, or remove it from the panel's devices)")
        return out

    def check(self, conn: Connection) -> list[CheckItem]:
        """`agentos connect --check`: what each part of the connection says, with the fix when it fails."""
        import json
        import subprocess

        again = f"agentos connect {conn.ssh_host} --name {conn.name}" if not conn.local else "agentos up --here"
        items: list[CheckItem] = []
        health = self.health(conn)
        if conn.local:
            items.append(CheckItem("Tunnel", "skip", "none needed, the node is this computer"))
        elif health.service.running:
            items.append(CheckItem("Tunnel", "ok", f"{health.service.label}, {conn.url}"))
        else:
            items.append(CheckItem("Tunnel", "fail", health.service.label, f"run `{again}`"))
        if health.ok:
            until = f", until {conn.agent_expires_at[:10]}" if conn.agent_expires_at else ""
            items.append(CheckItem("Grant", "ok", f"{self.credential_label(conn)}{until}; the node answers"))
        else:
            items.append(CheckItem("Grant", "fail", health.mcp, f"run `{again}`"))

        if not self.tools.claude:
            items.append(CheckItem("Claude Code", "skip", "not installed (`claude` is not on PATH)"))
        else:
            proc = ClaudeCode(self.tools.claude, self.paths.home).get(conn.claude_name)
            if proc is None:
                items.append(CheckItem("Claude Code", "fail", f"no {conn.claude_name} entry", f"run `{again}`"))
            elif conn.url not in proc:
                items.append(CheckItem("Claude Code", "fail", f"{conn.claude_name} points somewhere else",
                                       f"run `{again}`"))
            else:
                items.append(CheckItem("Claude Code", "ok", f"{conn.claude_name} at {conn.url}"))

        codex = CodexConfig(self.paths.home)
        if not codex_installed(self.paths.home):
            items.append(CheckItem("Codex", "skip", "not installed"))
        else:
            try:
                entry = codex.entry(conn.claude_name)
            except ClientError as exc:
                entry, broken = None, exc.message
            else:
                broken = None
            if entry is None:
                items.append(CheckItem("Codex", "fail", broken or f"no {conn.claude_name} in {shown(codex.path)}",
                                       f"run `{again}`"))
            else:
                cmd = [str(entry.get("command", "")), *[str(a) for a in entry.get("args") or []]]
                init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "agentos-check", "version": "1"}}}) + "\n"
                try:
                    proc = subprocess.run(cmd, input=init, capture_output=True, text=True, timeout=60, check=False)
                    answer = json.loads(proc.stdout.splitlines()[0]) if proc.stdout.strip() else {}
                except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
                    answer, proc = {"error": {"message": str(exc)}}, None
                if "result" in answer:
                    items.append(CheckItem("Codex", "ok", f"{conn.claude_name}: the bridge answers initialize"))
                else:
                    why = (answer.get("error") or {}).get("message") or \
                        ((proc.stderr.strip().splitlines() or ["no answer"])[-1] if proc else "no answer")
                    items.append(CheckItem("Codex", "fail", f"the bridge did not answer: {why}", f"run `{again}`"))

        if conn.skills_installed or self.tools.claude:
            state = skills.state(self.paths.home)
            items.append(CheckItem("Skills", "ok" if state == "installed" else "fail",
                                   f"{', '.join(skills.NAMES)}: {state}", f"run `{again}`"))
        return items

    def credential_label(self, conn: Connection) -> str:
        """What `agentos nodes` shows: which credential the connection's agents hold."""
        if conn.credential == "agent_grant":
            return f"grant {conn.agent_grant_id} ({conn.agent_subject}, up to {conn.agent_ceiling})"
        if conn.credential == "controller":
            return "controller agent token"
        return "node token (run agentos connect --upgrade)"

    def owner_device(self, conn: Connection) -> str:
        """The owner's device credential for this connection, or a ClientError that says how to get one."""
        device = self.owner_tokens(conn.token_store).get(conn.name) if conn.credential == "agent_grant" else None
        if not device:
            if conn.credential == "controller":
                raise ClientError(f"{conn.name} is a controller: decide in its panel (`agentos controller login`)")
            raise ClientError(f"{conn.name} has no owner credential on this computer",
                              f"run `agentos connect {conn.ssh_host} --name {conn.name}` (or `agentos connect "
                              "--upgrade`) to get one")
        return device

    def _owner_call(self, conn: Connection, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        device = self.owner_device(conn)
        try:
            return fn(device, *args, **kw)
        except PanelError as exc:
            if exc.status == 401:
                raise ClientError(f"{conn.name} no longer accepts the owner credential (expired or ended)",
                                  f"run `agentos connect {conn.ssh_host} --name {conn.name}` for a new one") from None
            if exc.code == "presence_required":  # approving, answering: this computer's credential is not a person
                raise ClientError(f"{conn.name}: approving needs you in person, and the credential saved on this "
                                  "computer is not enough (an agent here could read it); deny still works here",
                                  f"open {self.needs_you_url(conn)} in your browser, sign in with your passkey (or "
                                  f"a link from `ssh {conn.ssh_host} agentos panel login`), and approve there") from None
            raise ClientError(f"{conn.name}: {exc.message}", exc.hint) from None

    @staticmethod
    def needs_you_url(conn: Connection) -> str:
        """The panel's Needs you, through this computer's end of the tunnel."""
        return f"http://localhost:{conn.local_port}/needs-you"

    def approvals(self, conn: Connection, *, state: str | None = "pending", limit: int = 50) -> list[dict]:
        panel = NodePanel(conn.local_port)
        return self._owner_call(conn, panel.approvals, state=state, limit=limit)

    def decide(self, conn: Connection, approval_id: str, decision: str, *, note: str | None = None,
               answer: str | None = None) -> dict:
        """Approve or deny as the owner, with the device credential. Denying works here; approving and
        answering need a person (Presence.PERSON), which this credential is not, since an agent on this
        computer could read it from the Keychain: the node refuses them, and the error says to approve
        in the panel (`needs_you_url`)."""
        panel = NodePanel(conn.local_port)
        if answer is not None and decision == "approve":
            return self._owner_call(conn, panel.answer, approval_id, answer)
        return self._owner_call(conn, panel.decide, approval_id, decision, note=note)

    def connections(self) -> list[Connection]:
        return self.records.all()

    def pick(self, name: str | None) -> Connection:
        """The named connection, or the only one (the owner's CLI tools on this computer)."""
        if name:
            return self.get(name)
        conns = self.connections()
        if not conns:
            raise NotConnected("no connections", "add one with `agentos connect <ssh-host>`")
        if len(conns) > 1:
            raise ClientError(f"there are several connections ({', '.join(c.name for c in conns)})",
                              "choose one with --node")
        return conns[0]

    def get(self, name: str) -> Connection:
        conn = self.records.get(valid_name(name))
        if conn is None:
            raise NotConnected(f"no connection named {name}", "see `agentos status`, or run `agentos connect`")
        return conn

    def _token(self, conn: Connection) -> str | None:
        return self.tokens(conn.token_store).get(conn.name)

    def health(self, conn: Connection) -> Health:
        service = self.service(conn.service).state(conn.name)
        token = self._token(conn)
        if not token:
            return Health(conn, service, f"no token in the {conn.token_store} store; run connect again")
        try:
            info = probe(conn.local_port, token, timeout=PROBE_TIMEOUT)
        except McpError as exc:
            return Health(conn, service, explain(exc, conn))
        return Health(conn, service, "ok", ok=True, server=info)

    def mcp_session(self, conn: Connection, *, timeout: float = 60.0) -> McpSession:
        """An MCP session to the connection's node, with its agents' grant (what `agentos call` and
        `agentos flows push` use: the same grant and policy as an agent on this computer). Opened
        already; close it when done."""
        token = self._token(conn)
        if not token:
            raise ClientError(f"no token for {conn.name} in the {conn.token_store} store",
                              connect_again(conn))
        session = McpSession(conn.local_port, token, timeout=timeout)
        try:
            session.initialize()
        except McpError as exc:
            raise ClientError(f"{conn.name}: {explain(exc, conn)}", reconnect_hint(exc, conn)) from None
        return session

    def status(self, conn: Connection) -> dict[str, Any]:
        token = self._token(conn)
        if not token:
            raise ClientError(f"no token for {conn.name} in the {conn.token_store} store",
                              connect_again(conn))
        try:
            with McpSession(conn.local_port, token, timeout=15) as session:
                return {"whoami": session.call("whoami"), "lanes": session.call("lanes_list").get("lanes", [])}
        except McpError as exc:
            raise ClientError(f"{conn.name}: {explain(exc, conn)}", reconnect_hint(exc, conn)) from None


    def find_lane(self, lane: str) -> Connection:
        """The one connection whose node has this lane (asks each node when there are several)."""
        conns = self.connections()
        if not conns:
            raise NotConnected("no connections", "add one with `agentos connect <ssh-host>`")
        if len(conns) == 1:
            return conns[0]
        holders, unreachable = [], []
        for conn in conns:
            try:
                ids = {lane_["id"] for lane_ in self.status(conn)["lanes"]}
            except ClientError:
                unreachable.append(conn.name)
                continue
            if lane in ids:
                holders.append(conn)
        if len(holders) == 1:
            return holders[0]
        if holders:
            raise ClientError(f"{lane} exists on {', '.join(c.name for c in holders)}",
                              "choose one with --node")
        down = f" ({', '.join(unreachable)} did not answer)" if unreachable else ""
        raise ClientError(f"no connected node has a lane {lane}{down}", "see `agentos status`, or choose one with --node")

    def view_link(self, conn: Connection, lane: str, *, wake: bool = False) -> dict[str, Any]:
        """A signed control link to a warm lane's live view, pointed at this computer's end of the
        tunnel. A sleeping lane is woken only with `wake` (warming may close an idle lane). With the
        owner's device credential (the panel's view-link route); an old connection uses its token."""
        if conn.credential == "agent_grant":
            return self._owner_view_link(conn, lane, wake=wake)
        token = self._token(conn)
        if not token:
            raise ClientError(f"no token for {conn.name} in the {conn.token_store} store",
                              connect_again(conn))
        try:
            with McpSession(conn.local_port, token, timeout=60) as session:
                view = session.call("lane_view", {"on": lane})
                if view.get("viewer") is None and wake:
                    session.call("lane_open", {"on": lane})
                    view = session.call("lane_view", {"on": lane})
        except McpError as exc:
            raise ClientError(f"{conn.name}: {explain(exc, conn)}", reconnect_hint(exc, conn)) from None
        except ToolError as exc:
            first = str(exc).splitlines()[0] if str(exc) else "the node refused"
            raise ClientError(f"{conn.name}: {first}", "see `agentos status` for the lanes on each node") from None
        viewer = view.get("viewer")
        if viewer is None:
            raise ClientError(f"{lane} is asleep, so there is no live view yet",
                              f"wake it with `agentos open {lane} --wake`")
        path = viewer.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ClientError(f"{conn.name} answered without a view link", "is the node older than this client?")
        return {"url": f"http://localhost:{conn.local_port}{path}", "expires_at": viewer.get("expires_at"),
                "state": view.get("state"), "node": conn.name}


    def _owner_view_link(self, conn: Connection, lane: str, *, wake: bool) -> dict[str, Any]:
        panel = NodePanel(conn.local_port)
        info = self._owner_call(conn, panel.lane, lane)
        if info.get("state") not in ("warm", "active"):
            if not wake:
                raise ClientError(f"{lane} is asleep, so there is no live view yet",
                                  f"wake it with `agentos open {lane} --wake`")
            info = self._owner_call(conn, panel.lane_open, lane)
        link = self._owner_call(conn, panel.view_link, lane, "control")
        path = link.get("url") if isinstance(link, dict) else None
        if not isinstance(path, str) or not path.startswith("/"):
            raise ClientError(f"{conn.name} answered without a view link", "is the node older than this client?")
        return {"url": f"http://localhost:{conn.local_port}{path}", "expires_at": link.get("expires_at"),
                "state": info.get("state"), "node": conn.name}


class _quiet:
    """Ignore a PanelError in a clean-up step that must not fail the command."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind: type[BaseException] | None, exc: BaseException | None, tb: object) -> bool:
        return kind is not None and issubclass(kind, PanelError)


DEV_HOME = "(agentos dev up)"  # the remote_home connect_dev records: the fake node on this computer


def is_dev(conn: Connection) -> bool:
    return conn.remote_home == DEV_HOME


def connect_again(conn: Connection) -> str:
    """How to make this connection again: `agentos dev up` for the dev node, connect for the others."""
    if is_dev(conn):
        return "start the dev node with `agentos dev up`: it stores its grant again"
    return f"run `agentos connect {conn.ssh_host} --name {conn.name}` again"


def explain(exc: McpError, conn: Connection) -> str:
    if exc.failure is Failure.UNAUTHORIZED:
        return "the node refused the stored token (rotated?)"
    if exc.failure is Failure.UNREACHABLE and is_dev(conn):
        return f"the dev node is not running (nothing on localhost:{conn.local_port})"
    if exc.failure is Failure.UNREACHABLE and conn.service == "none":
        return f"the node on this computer is not running (nothing on localhost:{conn.local_port})"
    if exc.failure is Failure.UNREACHABLE:
        return f"tunnel down (nothing on localhost:{conn.local_port})"
    if exc.failure is Failure.NO_ANSWER:
        return f"tunnel up, but the node does not answer on port {conn.remote_port}"
    return f"unexpected answer: {exc.detail}"


def reconnect_hint(exc: McpError, conn: Connection) -> str:
    if is_dev(conn) and exc.failure in (Failure.UNREACHABLE, Failure.UNAUTHORIZED):
        return "start it with `agentos dev up` (add --seed for sample lanes); it registers its grant again"
    if conn.service == "none" and exc.failure is Failure.UNREACHABLE:
        return "start it with `agentos up --here`, then run `agentos status` again"
    again = f"`agentos connect {conn.ssh_host} --name {conn.name}`"
    if exc.failure is Failure.UNAUTHORIZED:
        return f"run {again} to read the current token"
    if exc.failure is Failure.NO_ANSWER:
        return f"is agentosd running on {conn.ssh_host}? (`ssh {conn.ssh_host} systemctl status agentosd`)"
    return f"see `agentos status`, or run {again}"
