"""`$AGENTOS_HOME/text-review/settings.toml`: when a review runs, what it reads and what it may spend.

Which brain does the review is a person's choice in the plugin registry
(`agentos plugins assign text-review review <brain>`), not a setting here.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("agentos.textreview")

PLUGIN = "text-review"  # agentos.plugins.entries
JOB = "review"
DIR_NAME = "text-review"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_days: float = Field(default=7.0, gt=0, le=365)  # a run starts when the last one is older than this
    window_days: float = Field(default=7.0, gt=0, le=90)     # how far back the action log is read
    check_every_s: float = Field(default=3600.0, ge=60)      # how often the daemon looks at the schedule
    min_calls: int = Field(default=40, ge=1)                 # below this many calls in the evidence, no model is asked
    max_usd: float = Field(default=1.0, gt=0, le=50)         # the whole run, retries included
    timeout_s: float = Field(default=900.0, gt=0, le=7200)   # one answer from the brain
    max_changes: int = Field(default=3, ge=1, le=20)         # asked for; halved when a reply is too long to read back
    workspace: str = Field(default="text-review", pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")  # the brain task's workspace
    # The node's own checks reach the log as ordinary agent calls. Their grants are marked only by
    # the subject they were issued for (scripts/canary_call.py, scripts/preflight_node.py), so those
    # subjects are listed here, with the workspaces only checks and benchmarks use.
    skip_subjects: list[str] = Field(default_factory=lambda: ["canary-agent", "preflight-agent"])
    skip_workspaces: list[str] = Field(default_factory=lambda: ["preflight", "lanebench"])
    max_sessions_shown: int = Field(default=40, ge=1, le=500)
    max_calls_shown: int = Field(default=60, ge=5, le=500)   # per session; patterns count every call
    inbox: str | None = None                                # lanebench reports; default text-review/inbox


def review_dir(home: Path) -> Path:
    return Path(home) / DIR_NAME


def settings_path(home: Path) -> Path:
    return review_dir(home) / "settings.toml"


def inbox_dir(home: Path, settings: Settings) -> Path:
    return Path(settings.inbox) if settings.inbox else review_dir(home) / "inbox"


def load_settings(home: Path | None) -> Settings:
    path = settings_path(home) if home is not None else None
    if path is None or not path.is_file():
        return Settings()
    try:
        return Settings(**tomllib.loads(path.read_text()))
    except (OSError, tomllib.TOMLDecodeError, ValidationError, TypeError) as e:
        log.error("%s cannot be read, so the defaults apply: %s", path, type(e).__name__)
        return Settings()
