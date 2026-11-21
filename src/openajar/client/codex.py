"""Register a connection with Codex: one `[mcp_servers.openajar-<name>]` table in its config.toml.

Codex (and T3 Code's Codex threads, which T3 starts without our environment) reads
`$CODEX_HOME/config.toml`, by default `~/.codex/config.toml`. The entry is a stdio server that
runs this computer's `openajar mcp-bridge <name>`:

    [mcp_servers.openajar-mynode]
    command = "/Users/me/.local/bin/openajar"
    args = ["mcp-bridge", "mynode"]

The bridge reads the grant token from the same store as Claude Code's headersHelper, so no
token is ever written here and no environment variable is needed.

The file is edited as text, not parsed and dumped again, so the owner's comments, order and
other servers stay exactly as they were: only our own table (and its sub-tables, such as
`[mcp_servers.openajar-mynode.env]`) is replaced or removed. The previous file is copied to
`config.toml.agentos-backup` before the first change of each run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from openajar.client.errors import ClientError
from openajar.core import fs

# A tool call can wait on a person (approval_wait, browser_wait_handback): Codex's default of 60
# seconds would cut those off. The bridge's own timeout is a little shorter, so the agent sees
# the node's answer (or the bridge's own clear error) instead of Codex's generic one.
TOOL_TIMEOUT_SEC = 600
STARTUP_TIMEOUT_SEC = 30  # the bridge may reopen the tunnel first
HEADER_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")
BACKUP_SUFFIX = ".agentos-backup"


def codex_home(home: Path) -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else home / ".codex"


def config_path(home: Path) -> Path:
    return codex_home(home) / "config.toml"


def installed(home: Path) -> bool:
    """Codex is here when its CLI is on PATH or its home folder exists (the desktop app, T3 Code)."""
    return bool(shutil.which("codex")) or codex_home(home).is_dir()


def _key_parts(header: str) -> list[str]:
    """`mcp_servers."openajar-x".env` -> ["mcp_servers", "openajar-x", "env"] (enough for our names)."""
    parts, buf, quote = [], "", ""
    for ch in header:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf += ch
        elif ch in "\"'":
            quote = ch
        elif ch == ".":
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    parts.append(buf.strip())
    return parts


def _ours(header: str, server: str) -> bool:
    parts = _key_parts(header)
    return len(parts) >= 2 and parts[0] == "mcp_servers" and parts[1] == server


def _strip_table(text: str, server: str) -> tuple[str, bool]:
    """The text without our table and its sub-tables (comments right above it go with it only
    when they are ours)."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    pending: list[str] = []  # comments after our last key: they belong to the next table
    skipping, found = False, False
    for line in lines:
        m = HEADER_RE.match(line)
        if m:
            was = skipping
            skipping = _ours(m.group(1), server)
            found = found or skipping
            if was and not skipping:
                out += [p for p in pending if p.strip() not in MARKS]
            pending = []
            if skipping:
                while out and out[-1].strip() in MARKS:
                    out.pop()
        if not skipping:
            out.append(line)
        elif not m:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                pending.append(line)
            else:
                pending = []
    if skipping:  # the file ended inside our table: comments after its last key are the owner's
        out += [p for p in pending if p.strip() and p.strip() not in MARKS]
    return "".join(out), found


MARK = "added by `openajar connect`; `openajar connect --remove NAME` removes it"
LEGACY_MARK = "added by `agentos connect`; `agentos connect --remove NAME` removes it"  # legacy-name
MARKS = {f"# {MARK}", f"# {LEGACY_MARK}"}


def entry_toml(server: str, command: str, args: list[str]) -> str:
    return (f"# {MARK}\n"
            f"[mcp_servers.{server}]\n"
            f"command = {json.dumps(command)}\n"
            f"args = [{', '.join(json.dumps(a) for a in args)}]\n"
            f"startup_timeout_sec = {STARTUP_TIMEOUT_SEC}\n"
            f"tool_timeout_sec = {TOOL_TIMEOUT_SEC}\n")


@dataclass
class CodexConfig:
    home: Path

    @property
    def path(self) -> Path:
        return config_path(self.home)

    def _read(self) -> str:
        try:
            with self.path.open(newline="") as f:  # keep CRLF files CRLF
                return f.read()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise ClientError(f"could not read {self.path}: {exc.strerror}") from None

    def entry(self, server: str) -> dict | None:
        """Our table as Codex will read it, or None. A file that does not parse is an error."""
        text = self._read()
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ClientError(f"{self.path} is not valid TOML ({exc})",
                              "fix it by hand (Codex cannot read it either), then run connect again") from None
        found = (data.get("mcp_servers") or {}).get(server)
        return found if isinstance(found, dict) else None

    def _write(self, before: str, after: str) -> None:
        try:
            tomllib.loads(after)
        except tomllib.TOMLDecodeError as exc:  # never leave Codex a file it cannot read
            raise ClientError(f"refusing to write {self.path}: the result would not parse ({exc})") from None
        target = self.path.resolve()  # a config.toml symlinked from a dotfiles repo stays a symlink
        target.parent.mkdir(parents=True, exist_ok=True)
        if before:
            fs.write_private(self.path.with_name(self.path.name + BACKUP_SUFFIX), before)
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
        fs.write_private(target, after, mode=mode)

    def register(self, server: str, command: str, args: list[str]) -> bool:
        """Write our table; True when the file changed (False: already registered)."""
        self.entry(server)  # a broken file: say so before touching it
        before = self._read()
        nl = "\r\n" if "\r\n" in before else "\n"
        rest, _ = _strip_table(before, server)
        rest = rest.rstrip("\r\n")
        after = (rest + nl + nl if rest else "") + entry_toml(server, command, args).replace("\n", nl)
        if after == before:
            return False
        self._write(before, after)
        return True

    def unregister_legacy(self, server: str) -> bool:
        """Remove a table an older version wrote under its old name: only one that runs our bridge."""
        try:
            entry = self.entry(server)
        except ClientError:
            return False
        if not entry or "mcp-bridge" not in (entry.get("args") or []):
            return False
        return self.unregister(server)

    def unregister(self, server: str) -> bool:
        before = self._read()
        if not before:
            return False
        after, found = _strip_table(before, server)
        if not found:
            return False
        nl = "\r\n" if "\r\n" in before else "\n"
        after = re.sub(r"(?:\r?\n){3,}", nl + nl, after).rstrip("\r\n") + nl if after.strip() else ""
        self._write(before, after)
        return True
