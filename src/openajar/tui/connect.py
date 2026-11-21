"""Where the dashboard finds the node and which credential it sends.

Each of the URL and the token resolves on its own, first match wins:
  1. the `--url` / `--token` flag
  2. the OPENAJAR_URL / OPENAJAR_TOKEN environment variable
  3. node.toml in OPENAJAR_HOME, when the dashboard runs on the node itself
  4. on the owner's computer: a connection made by `openajar connect` (`--node`, or the only
     one), through its tunnel, with the owner's device credential from the Keychain. That is a
     panel session, so the dashboard decides as the owner in person: it can approve what the
     connection's agents asked, which the agents' own grant never can
  5. the URL defaults to http://127.0.0.1:7400 (the SSH tunnel's local end)

Neither the token nor the session is ever part of a repr, a log line or anything drawn on screen.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:7400"
DEFAULT_PORT = 7400


@dataclass(frozen=True)
class Connection:
    url: str
    token: str = field(repr=False)
    url_source: str = "default"
    token_source: str = "none"
    tunnel_hint: str | None = None  # how to reach this node from the Mac, when known
    session: str = field(default="", repr=False)  # the owner's device credential (sent as the session cookie)
    home: Path | None = None  # on the node, with its own token from node.toml: decisions carry a one-time
                              # code from this home's panel.db, so they are a person's (openajar.commands.base.operator_presence)

    @property
    def has_token(self) -> bool:
        return bool(self.token or self.session)

    def describe(self) -> str:
        """Safe one-liner for the help screen: where we connect and where the credential came from."""
        if self.session:
            return f"{self.url} (from {self.url_source}), owner credential from {self.token_source}"
        token = f"token from {self.token_source}" if self.has_token else "no token"
        return f"{self.url} (from {self.url_source}), {token}"


# (url, device credential, where it came from) for a connection made by `openajar connect`, or None
OwnerLookup = Callable[[str | None], "tuple[str, str, str] | None"]


def normalize_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    url = url.rstrip("/")
    url = url.removesuffix("/api")
    return url


def _node_toml(env: Mapping[str, str]):
    """The node's own config, when this process can read it. None otherwise."""
    from openajar.core.config import DEFAULT_HOME, config_path, load_config

    home = Path(env["OPENAJAR_HOME"]) if env.get("OPENAJAR_HOME") else DEFAULT_HOME
    path = config_path(home)
    try:
        if not path.is_file():
            return None, path
        return load_config(home), path
    except (OSError, ValueError):
        # unreadable (root-owned, chmod 600) or malformed: fall back to flags and env
        return None, path


def _local_host(host: str) -> str:
    if host in ("", "0.0.0.0", "::"):
        return "127.0.0.1"
    return f"[{host}]" if ":" in host else host


def resolve(url: str | None = None, token: str | None = None,
            env: Mapping[str, str] | None = None, *, node: str | None = None,
            owner: OwnerLookup | None = None) -> Connection:
    env = os.environ if env is None else env
    cfg, path = (None, None)
    if not (url or env.get("OPENAJAR_URL")) or not (token or env.get("OPENAJAR_TOKEN")):
        cfg, path = _node_toml(env)
    if owner is not None and not (url or token or env.get("OPENAJAR_URL") or env.get("OPENAJAR_TOKEN")) \
            and (node is not None or cfg is None):
        found = owner(node)
        if found is not None:
            owner_url, session, source = found
            return Connection(url=normalize_url(owner_url), token="", url_source=f"openajar connect ({source})",
                              token_source="the Keychain", session=session)

    if url:
        resolved_url, url_source = url, "--url"
    elif env.get("OPENAJAR_URL"):
        resolved_url, url_source = env["OPENAJAR_URL"], "OPENAJAR_URL"
    elif cfg is not None:
        resolved_url, url_source = f"http://{_local_host(cfg.mcp_host)}:{cfg.mcp_port}", str(path)
    else:
        resolved_url, url_source = DEFAULT_URL, "default"

    operator_home = None
    if token:
        resolved_token, token_source = token, "--token"
    elif env.get("OPENAJAR_TOKEN"):
        resolved_token, token_source = env["OPENAJAR_TOKEN"], "OPENAJAR_TOKEN"
    elif cfg is not None and cfg.mcp_token:
        resolved_token, token_source = cfg.mcp_token, str(path)
        operator_home = cfg.home
    else:
        resolved_token, token_source = "", "none"

    hint = None
    if cfg is not None:
        from openajar.node.tools import ssh_host
        hint = f"ssh -N -L {cfg.mcp_port}:localhost:{cfg.mcp_port} {ssh_host(cfg)}"
    return Connection(url=normalize_url(resolved_url), token=resolved_token, url_source=url_source,
                      token_source=token_source, tunnel_hint=hint, home=operator_home)
