"""Where the local node keeps its files. Nothing here touches the disk."""

from __future__ import annotations

from pathlib import Path

from openajar.core.config import local_home

DEFAULT_PORT = 7410
# Ports the local node never takes, even when they are free now: 7400 is the usual `openajar connect`
# tunnel, 7450 is `openajar dev up`.
AVOIDED_PORTS = frozenset({7400, 7450})
NAME = "local"  # the node's name, the connection's name, and `openajar-local` in Claude Code and Codex
LOG_FILE = "local.log"
CATCH_UP_FILE = "local-catch-up.json"  # what the reapers closed after the last wake or start
# "yes" when `openajar local start` registered Claude Code and Codex (and the skills) for this home, "no" when
# it was told not to (--no-register, --no-skills): only a home registered for them writes into their folders
REGISTERED_FILE = "clients-registered"


def default_home() -> Path:
    """~/.openajar (or the ~/.agentos of an older install, as it is; legacy-name): the local node's OPENAJAR_HOME."""
    return local_home()


def is_default(home: Path) -> bool:
    """Is `home` ~/.openajar (the person's own local node), not a second one (a test's, a builder's)?"""
    try:
        return Path(home).expanduser().resolve() == default_home().resolve()
    except OSError:
        return False


def log_path(home: Path) -> Path:
    return Path(home) / "logs" / LOG_FILE


def catch_up_path(home: Path) -> Path:
    return Path(home) / CATCH_UP_FILE


def url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"
