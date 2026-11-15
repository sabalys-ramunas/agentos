"""Where the local node keeps its files. Nothing here touches the disk."""

from __future__ import annotations

from pathlib import Path

DEFAULT_PORT = 7410
# Ports the local node never takes, even when they are free now: 7400 is the usual `agentos connect`
# tunnel, 7450 is `agentos dev up`.
AVOIDED_PORTS = frozenset({7400, 7450})
NAME = "local"  # the node's name, the connection's name, and `agentos-local` in Claude Code and Codex
LOG_FILE = "local.log"
CATCH_UP_FILE = "local-catch-up.json"  # what the reapers closed after the last wake or start


def default_home() -> Path:
    """~/.agentos: the local node's AGENTOS_HOME."""
    return Path.home() / ".agentos"


def log_path(home: Path) -> Path:
    return Path(home) / "logs" / LOG_FILE


def catch_up_path(home: Path) -> Path:
    return Path(home) / CATCH_UP_FILE


def url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"
