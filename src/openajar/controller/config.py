"""Controller settings: `<home>/controller.toml`, mode 600.

The controller home is OPENAJAR_CONTROLLER_HOME, by default `$OPENAJAR_HOME/controller`.
Next to controller.toml the controller keeps a small `node.toml` with only `name`, `kind`,
`mcp_port` and `mcp_token` (the agent token): that is what `openajar connect <ssh-host>
--remote-home <controller home>` reads on the Mac, so connecting an agent to the controller
works exactly like connecting it to a node.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, Field, model_validator

from openajar.core.config import home_from_env, is_loopback_host, write_private
from openajar.node.channel import wire

CONFIG_NAME = "controller.toml"
CONNECT_STUB = "node.toml"
DEFAULT_PORT = 7401


class ControllerConfig(BaseModel):
    name: str = "controller"
    home: Path
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    public_url: str = ""            # what remote nodes dial, for example wss://controller.example.com
    behind_tls_proxy: bool = False  # bind beyond loopback without certificates: a TLS proxy is in front
    tls_certfile: str = ""
    tls_keyfile: str = ""
    agent_token: str = ""           # bearer for the owner's agents on /mcp (openajar connect reads it)
    owner_token: str = ""           # bearer for /api scripts; people sign in with one-time codes
    extra_allowed_hosts: list[str] = Field(default_factory=list)
    invoke_timeout_s: int = 300
    # what /mcp lists an agent that does not ask in its URL (openajar.mcp.toolsets); "full": every tool
    mcp_toolsets: list[str] = Field(default_factory=lambda: ["core", "accounts"])  # a controller fronts account browsers
    session_hours: int = 12

    @model_validator(mode="after")
    def _sane(self) -> ControllerConfig:
        problems = []
        if not 0 <= self.port <= 65535:
            problems.append("port must be between 0 and 65535")
        if not 5 <= self.invoke_timeout_s <= 3600:
            problems.append("invoke_timeout_s must be between 5 and 3600")
        if not 1 <= self.session_hours <= 24 * 30:
            problems.append("session_hours must be between 1 and 720")
        if bool(self.tls_certfile) != bool(self.tls_keyfile):
            problems.append("set both tls_certfile and tls_keyfile, or neither")
        if self.public_url:
            try:
                wire.connect_url(self.public_url)
            except ValueError as exc:
                problems.append(f"public_url: {exc}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def tls(self) -> bool:
        return bool(self.tls_certfile and self.tls_keyfile)

    @property
    def node_url(self) -> str:
        """The URL nodes dial."""
        if self.public_url:
            return wire.connect_url(self.public_url)
        scheme = "wss" if self.tls else "ws"
        return f"{scheme}://localhost:{self.port}{wire.CONNECT_PATH}"

    @property
    def local_url(self) -> str:
        return f"{'https' if self.tls else 'http'}://localhost:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.local_url}/mcp"

    @property
    def db_path(self) -> Path:
        return self.home / "controller.db"

    @property
    def secrets_dir(self) -> Path:
        return self.home / "secrets"


def controller_home(node_home: Path | None = None) -> Path:
    explicit = os.environ.get("OPENAJAR_CONTROLLER_HOME")
    if explicit:
        return Path(explicit)
    return (node_home or home_from_env()) / "controller"


def config_file(home: Path) -> Path:
    return home / CONFIG_NAME


def load_controller_config(home: Path) -> ControllerConfig:
    path = config_file(home)
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    data["home"] = str(home)
    return ControllerConfig(**data)


def save_controller_config(cfg: ControllerConfig) -> Path:
    cfg.home.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.home, 0o700)
    data = cfg.model_dump(mode="json")
    data.pop("home", None)
    path = config_file(cfg.home)
    write_private(path, tomli_w.dumps(data))
    stub = {"name": cfg.name, "kind": "controller", "mcp_port": cfg.port, "mcp_token": cfg.agent_token}
    write_private(cfg.home / CONNECT_STUB,
                  "# Read by `openajar connect` on your Mac. The controller's settings are in controller.toml.\n"
                  + tomli_w.dumps(stub))
    return path


def check_bind(cfg: ControllerConfig) -> None:
    """TLS is required outside localhost: a non-loopback bind needs certificates, or an explicit
    `behind_tls_proxy = true` when a reverse proxy terminates TLS in front of the controller."""
    if is_loopback_host(cfg.host) or cfg.tls or cfg.behind_tls_proxy:
        return
    raise RuntimeError(f"host {cfg.host!r} is not a loopback address and no TLS is configured. Set tls_certfile "
                       "and tls_keyfile in controller.toml, or behind_tls_proxy = true when a TLS proxy is in front")
