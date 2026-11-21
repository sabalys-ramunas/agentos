"""Register a connection with Claude Code, and the equivalent lines for other MCP clients.

The entry is a user-scope HTTP server with a `headersHelper` instead of a static header:

    claude mcp add-json --scope user openajar-<name> '{"type": "http", "url": ...,
        "headersHelper": "~/.config/openajar/bin/openajar-mcp-headers <name>"}'

`claude mcp add --header "Authorization: Bearer <token>"` would put the token on Claude
Code's command line (visible in `ps`) and in ~/.claude.json. The helper reads it from the
Keychain each time Claude Code connects, and again after a 401, so a rotated token only
needs `openajar connect` to run again.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openajar.client.errors import ClientError
from openajar.client.state import Connection

CLAUDE_TIMEOUT = 60


def entry_json(conn: Connection, helper: str) -> str:
    return json.dumps({"type": "http", "url": conn.url, "headersHelper": helper})


def manual_lines(conn: Connection, helper: str) -> list[str]:
    return [
        f"claude mcp remove --scope user {conn.claude_name}",
        f"claude mcp add-json --scope user {conn.claude_name} {shlex.quote(entry_json(conn, helper))}",
    ]


LEGACY_HELPER = "agentos-mcp-headers"  # legacy-name: the headers helper an entry before the rename runs


class ClaudeCode:
    def __init__(self, exe: str, home: Path):
        self.exe = exe
        self.home = home  # run from $HOME so no project's .mcp.json or local scope gets involved

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.exe, "mcp", *args], check=False, capture_output=True, text=True,
                                  timeout=CLAUDE_TIMEOUT, cwd=self.home, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ClientError(f"`claude mcp {args[0]}` did not finish in {CLAUDE_TIMEOUT} seconds") from None
        except OSError as exc:
            raise ClientError(f"could not run claude: {exc.strerror}") from None

    def register(self, conn: Connection, helper: str) -> None:
        """Replace any user-scope entry with the same name, then add ours."""
        self._run("remove", "--scope", "user", conn.claude_name)
        proc = self._run("add-json", "--scope", "user", conn.claude_name, entry_json(conn, helper))
        if proc.returncode != 0:
            text = (proc.stderr or proc.stdout).strip()
            raise ClientError(f"claude mcp add-json failed: {text.splitlines()[-1] if text else proc.returncode}",
                              "add it by hand with the lines below")

    def get(self, name: str) -> str | None:
        """What `claude mcp get` says about the entry, or None when Claude Code has none."""
        proc = self._run("get", name)
        return proc.stdout if proc.returncode == 0 else None

    def unregister(self, name: str) -> bool:
        return self._run("remove", "--scope", "user", name).returncode == 0

    def user_entry(self, name: str) -> dict[str, Any]:
        """The user-scope entry as ~/.claude.json holds it, or {}: `claude mcp get` does not print an
        entry's headersHelper, so that is where the helper an entry runs is read."""
        try:
            data = json.loads((self.home / ".claude.json").read_text())
        except (OSError, ValueError):
            return {}
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        entry = servers.get(name) if isinstance(servers, dict) else None
        return entry if isinstance(entry, dict) else {}

    def unregister_legacy(self, name: str, url: str | None = None) -> bool:
        """Remove an entry an older version wrote under its old name: only one that runs through our
        headers helper or points at our node's `url` (an entry the owner made under that name for
        something else stays). Whether or not the new entry was there already."""
        found = self.get(name)
        if not found:
            return False
        entry = self.user_entry(name)
        urls = [entry.get("url"), *(line.strip().removeprefix("URL:").strip() for line in found.splitlines()
                                    if line.strip().startswith("URL:"))]
        ours = LEGACY_HELPER in found or LEGACY_HELPER in str(entry.get("headersHelper") or "") or (
            url is not None and any(same_node(url, other) for other in urls))
        return self.unregister(name) if ours else False


_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def same_node(url: str, other: Any) -> bool:
    """Whether two MCP URLs reach the same node: equal, or both on this computer at one port and path
    (an older version wrote 127.0.0.1 where this one writes localhost)."""
    if not isinstance(other, str) or not other:
        return False
    if url == other:
        return True
    a, b = urlsplit(url), urlsplit(other)
    try:
        return (a.hostname in _LOOPBACK and b.hostname in _LOOPBACK and a.port == b.port
                and a.path.rstrip("/") == b.path.rstrip("/"))
    except ValueError:  # a port that is not a number
        return False
