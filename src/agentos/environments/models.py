"""Data for agent environments: the record of one environment, and the node's settings for them."""

from __future__ import annotations

import re
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SETTINGS_FILE = "environments.toml"
WORKSPACE = "env"                  # every environment's lanes live in this workspace
ID_RE = re.compile(r"^e[a-z0-9]{3,15}$")   # new ones are e + 10 (agentos.environments.manager.new_id)

# What is stored. "working" and "idle" are read from the last call when a person or an agent asks.
StoredState = Literal["starting", "ready", "closing", "closed", "failed"]
OPEN_STATES = ("starting", "ready", "closing")
LIVE_STATES = ("starting", "ready")   # what environment_open hands back (shown as starting, ready, working, idle)
WORKING = timedelta(minutes=2)     # a call this recent: the environment is "working"
IDLE = timedelta(minutes=10)       # no call for this long: "idle"
QUIET = timedelta(minutes=10)      # no call for this long: another chat of the same agent may take it over
CALLS_KEPT = 50
HANDOFFS_KEPT = 5
BRANCHES_KEPT = 200   # branches an environment created, as the git gateway reads them
# The sizes an agent asks for by name: at least this many CPUs and GB of memory. On Hetzner the
# cheapest server type in the node's location that has them (agentos.vms plans it); on the docker
# provider a container with these limits, at most the computer's (agentos.vms.docker.SIZE_TYPES).
SIZES: dict[str, tuple[int, int]] = {"small": (2, 4), "medium": (4, 8), "large": (8, 16), "xlarge": (16, 32)}
SizeName = Literal["small", "medium", "large", "xlarge"]


class EnvSettings(BaseModel):
    """`$AGENTOS_HOME/environments.toml`. The VM limits of vms.toml (max_live, max_ttl_minutes,
    monthly_budget_eur) hold as well; these are the environments' own, and the tighter one wins."""
    model_config = ConfigDict(extra="forbid")
    idle_minutes: int = Field(default=60, ge=5)           # no call, command or shell this long: closed (when saved)
    default_hours: float = Field(default=2.0, gt=0, le=72)
    # The limits that bound environment_open, which never asks a person (docs/design/environments.md).
    # They hold without `agentos environments allow`, which only changes them.
    max_hours: float = Field(default=8.0, gt=0, le=72)    # the longest life, extend included (and vms.toml max_ttl_minutes)
    max_live: int | None = Field(default=3, ge=0, le=50)  # open at once; None: vms.toml max_live
    monthly_eur: float | None = Field(default=50.0, ge=0)  # booked a month, open ones to their time limit; 0 or None: none
    profile: str | None = None                            # the vms.toml profile to rent; None: its default
    max_size: SizeName = "xlarge"                         # the biggest machine an agent may ask for (SIZES)
    image: str = "auto"                                   # auto: snapshot:dev when the node has it, else Ubuntu
    setup_timeout_s: int = Field(default=900, ge=60)      # the machine's setup (cloud-init) must end in this
    unsaved_notice_minutes: int = Field(default=30, ge=1)  # before the time limit, tell the owner of unsaved work
    reap_interval_s: int = Field(default=60, ge=5)


def load_settings(home: Path) -> EnvSettings:
    path = Path(home) / SETTINGS_FILE
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    return EnvSettings(**data)


class Call(BaseModel):
    at: datetime
    tool: str
    summary: str = ""
    ok: bool = True
    exit_code: int | None = None              # a command's exit code (lane_exec): ok is the tool's own answer
    error: str | None = None                  # the error's code when the call was refused or failed


class Push(BaseModel):
    branch: str
    commit: str                               # "" for a delete
    at: datetime
    deleted: bool = False                     # the push deleted the branch on the remote


class Handoff(BaseModel):
    """Another chat took the environment over (environment_open with its title): the chat that had
    it is told so on its next call."""
    binding: str                              # the chat that lost it
    at: datetime


class Machine(BaseModel):
    provider: str = ""
    server_type: str = ""
    cpu: int | None = None
    memory_gb: float | None = None
    location: str = ""
    size: str | None = None                   # small, medium, large, xlarge: the biggest of SIZES it has


class Environment(BaseModel):
    id: str                                   # e7k2
    title: str
    state: StoredState = "starting"
    grant_id: str                             # the grant that opened it (owner: "owner")
    grant_root: str | None = None             # its family (the grant's root): only that family reaches it
    binding: str                              # "session:<mcp-session-id>", else "grant:<grant id>"
    agent: str = "an agent"                   # who opened it, in words (the grant's subject)
    subject: str | None = None                # the grant's subject id, as the policy has it
    member: str | None = None                 # the member whose agent opened it; None: the owner's agent
    client: str | None = None                 # the MCP client's own name, when it said one
    vm_id: str | None = None
    repo: str | None = None                   # github.com/me/shop
    ref: str | None = None
    branch: str | None = None
    workdir: str = "/work"
    via_gateway: bool = False                 # cloned through the node's git gateway (a declared remote)
    image: str = ""
    machine: Machine = Field(default_factory=Machine)
    price_hourly_eur: float | None = None
    created_at: datetime
    ready_at: datetime | None = None
    last_call_at: datetime | None = None
    expires_at: datetime
    closed_at: datetime | None = None
    closed_reason: str | None = None
    cost_eur: float | None = None             # set when it closes; while open, read from its VM
    error: str | None = None
    error_code: str | None = None             # the error's code when it failed (repo_not_connected, environment_failed)
    calls: list[Call] = Field(default_factory=list)   # newest last, at most CALLS_KEPT
    pushed: list[Push] = Field(default_factory=list)
    unsaved_noticed: bool = False             # the owner was told it has unsaved work and will not close idle
    handoffs: list[Handoff] = Field(default_factory=list)  # chats it was taken from, newest last
    # The branches it created on the remote (pushed first, not there before): the only existing branches
    # it may delete or force-push (agentos.git.gateway). Kept with the record, so a restart keeps them.
    own_branches: list[str] = Field(default_factory=list)
    # The commit each of those branches had when it last pushed it there: a branch is its own only while
    # the remote still has that commit (someone else may delete and recreate the name, or add to it).
    own_heads: dict[str, str] = Field(default_factory=dict)
    vm_state: str | None = None               # its machine at close: deleted (the provider says it is gone),
                                              # deleting (the node still retries), none (none was rented)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError(f"not an environment id: {v!r}")
        return v

    @property
    def family(self) -> str:
        """The grant family it belongs to: the root of the grant that opened it."""
        return self.grant_root or self.grant_id

    @property
    def open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def terminal_lane(self) -> str:
        return f"{WORKSPACE}/{self.id}"

    @property
    def browser_lane(self) -> str:
        return f"{WORKSPACE}/{self.id}-browser"

    @property
    def lanes(self) -> dict[str, str]:
        return {"terminal": self.terminal_lane, "browser": self.browser_lane}

    def shown_state(self, now: datetime) -> str:
        """What people see: starting, ready, working, idle, closing, closed or failed."""
        if self.state != "ready":
            return self.state
        last = self.last_call_at
        if last is not None and now - last <= WORKING:
            return "working"
        if now - (last or self.ready_at or self.created_at) >= IDLE:
            return "idle"
        return "ready"

    def last_activity(self) -> datetime:
        return max(t for t in (self.last_call_at, self.ready_at, self.created_at) if t is not None)
