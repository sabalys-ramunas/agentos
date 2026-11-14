"""Brains settings: `$AGENTOS_HOME/brains/settings.toml` (owner-edited, all optional)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, Field

from agentos.brains.models import Privacy

DEFAULT_ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit", "Bash", "WebFetch", "WebSearch",
    "TodoWrite", "mcp__agentos",
]


class BrainsSettings(BaseModel):
    # where the CLIs live (install_cli.sh puts them in /opt/agentos/node/bin)
    bin_dirs: list[str] = Field(default_factory=lambda: ["/opt/agentos/node/bin"])
    claude_bin: str | None = None      # explicit path wins over bin_dirs and PATH
    codex_bin: str | None = None

    # the agentos MCP endpoint a brain calls back into (with its CHILD grant token)
    mcp_url: str | None = None         # default http://127.0.0.1:<mcp_port>/mcp

    # privacy for workspaces the owner has not configured
    default_privacy: Privacy = Privacy.FULL_CLOUD

    # task defaults and hard caps (a caller's budget is clamped to the caps)
    default_max_turns: int = 40
    cap_max_turns: int = 400
    default_max_minutes: float = 60.0
    cap_max_minutes: float = 24 * 60.0
    default_max_tokens: int = 3_000_000
    cap_max_tokens: int = 50_000_000
    default_max_queue_minutes: float = 12 * 60.0
    max_attempts: int = 4
    stall_seconds: float = 900.0       # no output for this long counts as a hang
    cancel_grace_seconds: float = 10.0
    max_line_bytes: int = 1_000_000
    transcript_segment_bytes: int = 8_000_000
    transcript_segments: int = 6       # older segments are dropped (the tail is what matters)
    max_output_bytes: int = 400_000_000  # runaway output stops the task

    # metered spend without a policy grant: needs a human co-sign unless allowed here
    metered_without_policy: bool = False

    # process isolation for the brain subprocess
    isolation: str = "auto"            # auto | scope | none (scope = systemd transient scope)
    run_as: str | None = "agentos-brain"  # unprivileged user brains run as
    # agentosd not root: the agentos-runas helper starts brains as run_as (deploy/agentos-runas.service)
    runas_socket: str = "/run/agentos-runas/runas.sock"
    # AUD-08: before a brain starts, check from its own identity that it cannot reach the node's
    # secrets, and refuse it otherwise. auto = on Linux (every node), off on a Mac dev machine.
    confinement: str = "auto"          # auto | require | off
    memory_mb: int = 1536
    tasks_max: int = 512
    cpu_quota_pct: int = 150
    scrub_subprocess_env: bool = True  # claude: keep tokens out of Bash tool env (needs bwrap)

    # claude defaults
    permission_mode: str = "dontAsk"   # deny anything not on the allowlist, never prompt
    allowed_tools: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    setting_sources: str = "user"      # ignore settings and hooks shipped inside a cloned repo

    # codex defaults
    codex_sandbox: str = "workspace-write"

    # chat-completions brains (agentos.brains.chatcli)
    chat_python: str | None = None     # the interpreter for the runner; default: the system Python (brain user)
    chat_timeout_seconds: float = 300.0   # one call; past this the endpoint counts as down
    chat_max_output_tokens: int = 4096    # a reply's ceiling, lowered further to what the budget still pays for

    # typed decisions (agentos.brains.decisions): one in-process HTTP call, hundreds per task
    decide_timeout_seconds: float = 20.0  # one try; past this the brain counts as down, with no retry
    decide_retries: int = 2               # further tries on 429, 500, 502, 503 and 529
    decide_backoff_seconds: float = 0.25  # the first wait, doubled each try
    decide_max_wait_seconds: float = 2.0  # a longer Retry-After ends the tries: a caller is waiting

    # rate limits
    cooldown_base_seconds: float = 120.0
    cooldown_max_seconds: float = 6 * 3600.0
    window_saturation: float = 0.95    # treat a window this full as busy

    # monitor and health
    poll_seconds: float = 1.0
    online_check_host: str = "api.anthropic.com"
    online_check_ttl: float = 30.0
    notify_on_finish: bool = True


def settings_path(home: Path) -> Path:
    return home / "brains" / "settings.toml"


def load_settings(home: Path) -> BrainsSettings:
    path = settings_path(home)
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    return BrainsSettings(**data)


def save_settings(home: Path, settings: BrainsSettings) -> Path:
    path = settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = settings.model_dump(mode="json", exclude_none=True)
    path.write_text(tomli_w.dumps(data))
    path.chmod(0o600)
    return path
