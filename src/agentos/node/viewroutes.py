"""How a connected computer reaches this node: its SSH host and its local port, per grant.

`agentos connect` knows both (the host it tunnels through, the port the tunnel listens on there)
and records them with the agent grant it issued or kept (`POST /api/grants/{id}/route`). browser_view
then gives an agent holding that grant (or one narrowed from it) a take-over link and a tunnel
command that are right for its own computer, instead of this node's port and hostname, which on a
second node point at the wrong machine. Without a record, browser_view says what it said before.

    $AGENTOS_HOME/connect-routes.json   {grant_id: {ssh_host, local_port, at}}  (600)

Only the owner writes it. It holds no secret: a host alias and a port.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

FILE = "connect-routes.json"
MAX_ROUTES = 200
HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@:%+-]{0,252}$")  # an ssh host or alias, as connect takes it


def check(ssh_host: Any, local_port: Any) -> tuple[str, int]:
    """(ssh_host, local_port), or ValueError."""
    if not isinstance(ssh_host, str) or not HOST_RE.match(ssh_host):
        raise ValueError("ssh_host must be an SSH host or alias, like mynode or root@203.0.113.7")
    if isinstance(local_port, bool) or not isinstance(local_port, int) or not 1 <= local_port <= 65535:
        raise ValueError("local_port must be a port number")
    return ssh_host, local_port


def _path(home: Path) -> Path:
    return Path(home) / FILE


def load(home: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def save(home: Path, grant_id: str, ssh_host: str, local_port: int, *, now: float | None = None) -> dict[str, Any]:
    ssh_host, local_port = check(ssh_host, local_port)
    routes = load(home)
    routes[grant_id] = {"ssh_host": ssh_host, "local_port": local_port, "at": now or time.time()}
    if len(routes) > MAX_ROUTES:  # the oldest go first
        for gid, _ in sorted(routes.items(), key=lambda kv: kv[1].get("at") or 0)[:len(routes) - MAX_ROUTES]:
            routes.pop(gid, None)
    path = _path(home)
    tmp = path.with_name(f".{FILE}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=1)
    os.replace(tmp, path)
    return routes[grant_id]


def find(home: Path, grant_ids: Iterable[str]) -> dict[str, Any] | None:
    """The route recorded for the nearest of these grants (a leaf first, then its parents), or None."""
    routes = load(home)
    for gid in grant_ids:
        route = routes.get(gid)
        if route is None:
            continue
        with contextlib.suppress(ValueError):
            ssh_host, port = check(route.get("ssh_host"), route.get("local_port"))
            return {"ssh_host": ssh_host, "local_port": port}
    return None
