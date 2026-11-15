"""The local node's home: node.toml (its name, loopback bind, port and token) and the vault's key
file, made once and kept. The port it picked is remembered in node.toml, so the address Claude Code
and Codex were given stays the same across restarts."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from agentos.client.errors import ClientError
from agentos.client.ports import port_in_use
from agentos.commands.base import stamp_fresh_node_db
from agentos.core.config import NodeConfig, config_path, config_problems, load_config, save_config
from agentos.local.paths import AVOIDED_PORTS, DEFAULT_PORT, NAME

PORT_SPAN = 100  # how far past the wanted port it looks for a free one


class LocalError(ClientError):
    """Something about the local node the person can fix; `hint` says how."""


Busy = Callable[[int], bool]  # port -> something listens there (tests pass a fake: no real binds)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def read(home: Path) -> NodeConfig | None:
    """The home's node.toml, or None before the first start."""
    if not config_path(home).exists():
        return None
    try:
        return load_config(home)
    except ValidationError as exc:
        raise LocalError(f"{config_path(home)} has invalid settings: {config_problems(exc)}",
                         "fix it, or move it away and start again") from None
    except (OSError, ValueError) as exc:
        raise LocalError(f"cannot read {config_path(home)}: {exc}", "fix it, or move it away and start again") from None


def free_port(start: int, *, busy: Busy = port_in_use) -> int:
    """`start`, or the next port after it that is free. The ports in AVOIDED_PORTS are never taken
    (and never probed)."""
    for port in range(start, min(start + PORT_SPAN, 65536)):
        if port not in AVOIDED_PORTS and not busy(port):
            return port
    raise LocalError(f"no free port between {start} and {start + PORT_SPAN - 1}",
                     "pass --port with a free one")


def choose_port(home: Path, wanted: int | None, *, busy: Busy = port_in_use) -> int:
    """The port to serve on: the one asked for (it must be free), else the one remembered in
    node.toml while it is free, else the first free one from DEFAULT_PORT (or after the remembered one)."""
    if wanted is not None:
        if wanted in AVOIDED_PORTS:
            raise LocalError(f"port {wanted} is kept for {'agentos connect' if wanted == 7400 else 'agentos dev up'}",
                             f"leave out --port to use {DEFAULT_PORT} or the next free one")
        if busy(wanted):
            raise LocalError(f"port {wanted} is already in use", "stop what listens there, or pick another --port")
        return wanted
    cfg = read(home)
    remembered = cfg.mcp_port if cfg is not None else None
    if remembered and remembered not in AVOIDED_PORTS and not busy(remembered):
        return remembered
    return free_port(remembered + 1 if remembered else DEFAULT_PORT, busy=busy)


def ensure(home: Path, port: int) -> NodeConfig:
    """Make (or update) the home for `port`: node.toml with the local node's name, a loopback bind,
    the port and a token (an existing token is kept), and a vault with a key file. Returns the config."""
    home = Path(home).expanduser()
    _private_dir(home)
    cfg = read(home) or NodeConfig(home=home, name=NAME)
    changes: dict = {"home": home, "mcp_host": "127.0.0.1", "mcp_bind_any": False, "mcp_port": port}
    if not cfg.mcp_token:
        changes["mcp_token"] = secrets.token_urlsafe(32)
    cfg = NodeConfig(**{**cfg.model_dump(), **changes})
    _private_dir(cfg.secrets_dir)
    if read(home) != cfg:
        save_config(cfg)
    stamp_fresh_node_db(home)
    _ensure_vault(home)
    return cfg


def _ensure_vault(home: Path) -> None:
    """A vault sealed with a key file in secrets/ (chmod 600), as `agentos vault init` makes one: the
    git gateway and the repository tokens keep their secrets there."""
    from agentos.vault import crypto
    from agentos.vault.service import keyfile_path, pending_keyfile_path
    from agentos.vault.store import VaultStore

    store = VaultStore(home / "vault.db")
    if store.header() is not None:
        return
    keyfile = keyfile_path(home)
    if keyfile.exists():
        raise LocalError(f"{keyfile} exists but {home / 'vault.db'} has no vault",
                         "move the key file away (it belongs to a vault that is gone), then start again")
    pending = pending_keyfile_path(keyfile)
    master = os.urandom(crypto.KEY_LEN)
    crypto.write_keyfile(pending, master)
    store.initialize(mode="keyfile", master=master, kdf=None)
    os.replace(pending, keyfile)
    crypto.fsync_dir(keyfile.parent)
