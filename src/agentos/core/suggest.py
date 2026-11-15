""""Did you mean" for an owner's CLI: a misspelt lane or workspace id names the close one before the
"create it" hint, so a typo does not become an empty lane.

Only the owner's CLI on the node uses this (catalog.cli_error): it reads the node's own lane and
workspace ids, all of them. MCP and web errors never pass through here, so an agent or a member is
never shown an id outside what they may see.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from agentos.core.text import close_match

LANE_MISSING = re.compile(r"^no lane named (?P<id>[^\s.]+(?:\.[^\s.]+)*)\.?(?: Create it with: .*)?$")
WORKSPACE_MISSING = re.compile(r"^no workspace '(?P<id>[^']+)'(?:; create it with: .*)?$")


def _ids(path: Path, sql: str) -> list[str]:
    """Ids from a node database, read only (never creates or migrates it); none when it is not there."""
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            return [str(row[0]) for row in conn.execute(sql)]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _home() -> Path | None:
    from agentos.core.config import config_path, home_from_env

    home = home_from_env()
    return home if config_path(home).exists() else None


def lane_ids(home: Path) -> list[str]:
    return _ids(home / "agentos.db", "SELECT id FROM lanes")  # NodeConfig.db_path


def workspace_ids(home: Path) -> list[str]:
    lanes = {lane.split("/", 1)[0] for lane in lane_ids(home) if "/" in lane}
    return sorted(lanes | set(_ids(home / "workspaces.db", "SELECT id FROM workspaces")))


def owner_message(code: str, message: str) -> str:
    """`message` with "Did you mean <id>?" in place of the create hint when a close id exists on this
    node; `message` unchanged otherwise (off a node, or nothing is close)."""
    if code not in ("lane_not_found", "not_found"):
        return message
    lane = LANE_MISSING.match(message)
    ws = None if lane else WORKSPACE_MISSING.match(message)
    if lane is None and ws is None:
        return message
    home = _home()
    if home is None:
        return message
    if lane is not None:
        found = close_match(lane["id"], lane_ids(home))
        return (f"no lane named {lane['id']}. Did you mean {found}? (`agentos lane list` shows every lane)"
                if found else message)
    assert ws is not None
    found = close_match(ws["id"], workspace_ids(home))
    return (f"no workspace {ws['id']!r}. Did you mean {found!r}? (`agentos workspace list` shows them all)"
            if found else message)
