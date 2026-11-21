"""Where the client keeps things, and the connection records (no secrets in them).

Everything lives under the owner's home, so tests point HOME at a temp dir:

- `~/.config/openajar/connections/<name>.json`: one record per node connection (chmod 600)
- `~/.config/openajar/tokens/<name>`: the agent's grant token, only with the file store (chmod 600)
- `~/.config/openajar/owner/<name>`: the owner's device credential, only with the file store (chmod 600)
- `~/.config/openajar/bin/openajar-mcp-headers`: prints the auth header for Claude Code
- `~/Library/LaunchAgents/dev.agentos.tunnel.<name>.plist` and `~/Library/Logs/openajar/` (macOS)
- `~/.config/systemd/user/agentos-tunnel-<name>.service` (Linux)
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from openajar.client.errors import ClientError
from openajar.core import fs

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEFAULT_REMOTE_HOME = "/var/lib/agentos"
LEGACY_CONFIG_DIR = "agentos"  # legacy-name: ~/.config/agentos, the client's folder before the rename

TokenStoreKind = Literal["keychain", "file"]
# "none": the node runs on this computer (`openajar up --here`), so there is no tunnel to keep up
ServiceKind = Literal["launchd", "systemd", "none"]
# What the agents of a connection hold: their own grant (openajar connect since the split), the
# node's own token (connections made before it; `openajar connect --upgrade` moves them), or a
# controller's agent token (a controller has its own owner token and panel).
CredentialKind = Literal["agent_grant", "node_token", "controller"]


def current_platform() -> str:
    """"macos" or "linux". Tests monkeypatch this to run the Linux path on a Mac."""
    return "macos" if sys.platform == "darwin" else "linux"


def shown(path: Path) -> str:
    """A path for messages: under the home dir it starts with ~."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def valid_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ClientError(
            f"connection name {name!r} is not valid",
            "use lowercase letters, digits, dash or underscore (up to 32), for example mynode",
        )
    return name


def default_name(ssh_host: str) -> str:
    """A connection name from an SSH host: `root@192.0.2.10` becomes `192-0-2-10`."""
    host = ssh_host.rsplit("@", 1)[-1].lower()
    name = re.sub(r"[^a-z0-9_-]+", "-", host).strip("-_")[:32].strip("-_")
    return name or "node"


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def current(cls) -> Paths:
        return cls(Path.home())

    @property
    def config_dir(self) -> Path:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg and os.path.isabs(xdg) else self.home / ".config"
        new, old = base / "openajar", base / LEGACY_CONFIG_DIR
        if (old / "connections").is_dir() and not (new / "connections").is_dir():  # connections made before
            return old  # the rename, used where they are
        if not new.exists() and old.is_dir():
            return old
        return new

    @property
    def connections_dir(self) -> Path:
        return self.config_dir / "connections"

    @property
    def tokens_dir(self) -> Path:
        return self.config_dir / "tokens"

    @property
    def owner_tokens_dir(self) -> Path:
        return self.config_dir / "owner"

    @property
    def headers_helper(self) -> Path:
        return self.config_dir / "bin" / "openajar-mcp-headers"

    @property
    def launch_agents_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    @property
    def logs_dir(self) -> Path:
        return self.home / "Library" / "Logs" / "openajar"

    @property
    def systemd_user_dir(self) -> Path:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg and os.path.isabs(xdg) else self.home / ".config"
        return base / "systemd" / "user"


class Connection(BaseModel):
    """What `openajar connect` set up for one node. No token or credential is ever stored here: the
    agent's grant token and the owner's device credential are in the token store; this record keeps
    their ids, so `openajar nodes` can say which grant a connection uses and disconnect can revoke it."""

    name: str
    ssh_host: str
    remote_home: str = DEFAULT_REMOTE_HOME
    remote_port: int
    local_port: int
    node_name: str | None = None
    token_store: TokenStoreKind
    service: ServiceKind
    claude_registered: bool = False
    credential: CredentialKind = "node_token"  # records written before the split held the node token
    agent_grant_id: str | None = None
    agent_subject: str | None = None           # for example claude-code@macbook
    agent_ceiling: str | None = None
    agent_specials: list[str] = []
    agent_expires_at: str | None = None
    owner_device_id: str | None = None         # the device session's public id on the node
    owner_device_expires_at: str | None = None
    toolsets: list[str] | None = None          # what the MCP URL asks the node to list; None: its default
    codex_registered: bool = False             # our table in Codex's config.toml
    skills_installed: bool = False             # the Claude Code skills, copied by this connection
    updated_at: str = ""

    @property
    def local(self) -> bool:
        """The node is this computer: no SSH tunnel, the node's own loopback port."""
        return self.service == "none"

    @property
    def host(self) -> str:
        return "127.0.0.1" if self.local else "localhost"

    @property
    def url(self) -> str:
        query = f"?toolsets={','.join(self.toolsets)}" if self.toolsets else ""
        return f"http://{self.host}:{self.local_port}/mcp{query}"

    @property
    def claude_name(self) -> str:
        return f"openajar-{self.name}"

    @property
    def legacy_claude_name(self) -> str:
        """The entry's name in Claude Code and Codex before the rename."""
        return f"agentos-{self.name}"  # legacy-name


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def write_private(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    """Write through a temp file created with `mode`, then rename, so the file is never wider."""
    ensure_private_dir(path.parent)
    fs.write_private(path, data, mode=mode)


class ConnectionStore:
    def __init__(self, paths: Paths):
        self.paths = paths

    def _path(self, name: str) -> Path:
        return self.paths.connections_dir / f"{name}.json"

    def get(self, name: str) -> Connection | None:
        path = self._path(name)
        if not path.exists():
            return None
        return Connection.model_validate_json(path.read_text())

    def all(self) -> list[Connection]:
        if not self.paths.connections_dir.is_dir():
            return []
        out = []
        for path in sorted(self.paths.connections_dir.glob("*.json")):
            try:
                out.append(Connection.model_validate_json(path.read_text()))
            except ValueError:
                continue  # a hand-edited, broken record must not hide the others
        return out

    def save(self, conn: Connection) -> Path:
        conn.updated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = self._path(conn.name)
        write_private(path, json.dumps(conn.model_dump(), indent=2) + "\n")
        return path

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.exists():
            path.unlink()
            return True
        return False


def connections_at(home: Path | None = None) -> list[Connection]:
    """The connections `openajar connect` made for `home` (default: this user's), as the doctor reads them."""
    return ConnectionStore(Paths(home) if home else Paths.current()).all()
