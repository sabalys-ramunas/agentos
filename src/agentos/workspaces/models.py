"""Shapes shared by the store, the planner, the service, the tools, the CLI and the API.

This module is data only: nothing here touches the lane manager, the disk or the network.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentos.core.errors import InvalidRequest

# The workspace part of a lane id (`workspace/name`) and the name part, as core.models.LANE_ID_RE
# spells them. Each side is checked on its own, so any valid workspace with any valid surface name
# makes a valid lane id. Templates are validated against LANE_NAME_RE for the same reason.
WORKSPACE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
LANE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
PLATFORM_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
TEMPLATE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")

NAME_MAX = 60
NOTE_MAX = 2000
READINESS_NOTE_MAX = 200
URL_MAX = 500
PLAN_MAX_LANES = 200


class Readiness(StrEnum):
    """Whether the owner has logged the account in. Set explicitly, never detected."""
    NEEDS_LOGIN = "needs_login"          # the default: nobody has said the account is ready
    READY = "ready"
    NEEDS_ATTENTION = "needs_attention"  # something is wrong; the note says what


def _shown(value: object) -> str:
    text = value if isinstance(value, str) else type(value).__name__
    return repr(text[:40] + ("..." if len(text) > 40 else ""))


def _match(pattern: re.Pattern[str], value: object, message: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise InvalidRequest(f"{message}: {_shown(value)}")
    return value


def check_workspace_id(value: object) -> str:
    return _match(WORKSPACE_ID_RE, value, "workspace id must be 1 to 31 characters (lowercase letters, "
                                          "digits and dashes, starting with a letter or digit)")


def workspace_id_from(name: str) -> str:
    """A valid workspace id made from a name a person typed: lowercase ASCII, digits and dashes, at most 31
    characters. "Harbor Copy" is `harbor-copy`, "Šiauliai café" is `siauliai-cafe`, and a name with no letter
    or digit in it is `workspace`."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:31].rstrip("-")
    return slug or "workspace"


def unique_workspace_id(name: str, taken: Callable[[str], bool]) -> str:
    """The id a typed name makes, with `-2`, `-3`... added until `taken` says no: never an id already here."""
    base = workspace_id_from(name)
    candidate, number = base, 2
    while taken(candidate):
        suffix = f"-{number}"
        candidate = f"{base[: 31 - len(suffix)].rstrip('-')}{suffix}"
        number += 1
    return check_workspace_id(candidate)


def check_lane_name(value: object) -> str:
    return _match(LANE_NAME_RE, value, "lane name must be 1 to 41 characters (lowercase letters, "
                                       "digits and dashes, starting with a letter or digit)")


def check_platform(value: object) -> str:
    return _match(PLATFORM_RE, value, "platform must be 1 to 41 characters (lowercase letters, "
                                      "digits and dashes)")


def check_template_id(value: object) -> str:
    return _match(TEMPLATE_ID_RE, value, "template id must be 1 to 41 characters (lowercase letters, "
                                         "digits and dashes)")


class TemplateLane(BaseModel):
    """One lane that a template creates in each workspace it is applied to. Written as
    `[[surfaces]]` in template files; not an AUD-01 surface (core.models.Surface: who you post
    as), which is why the class is TemplateLane (docs/design/connectors.md, open question 9)."""
    model_config = ConfigDict(frozen=True)

    name: str                        # the lane's name: <workspace>/<name>
    platform: str                    # key for the proxy rules: one account per platform per IP
    engine: str = "camoufox"
    home_url: str | None = None      # per company where not fixed (a blog), so it may be unset
    login_url: str | None = None
    sites: list[str] | None = None   # the lane's site list (agentos.node.egress); None: none set


class Template(BaseModel):
    id: str
    title: str
    description: str = ""
    surfaces: list[TemplateLane]
    builtin: bool = False
    source: str | None = None        # the file name for a user template


class TemplateProblem(BaseModel):
    """Why a user template file cannot be used. Reported by `templates`, never raised by it."""
    file: str
    template: str
    message: str


class LaneView(BaseModel):
    id: str
    workspace: str
    name: str
    surface: str | None = None       # the template surface, when the lane came from one
    platform: str
    proxy: str | None = None         # the label only, never credentials
    lane_state: str                  # the node's state: cold, warm, active, quarantined, ...
    readiness: Readiness
    note: str | None = None
    login_url: str | None = None
    home_url: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None


class WorkspaceView(BaseModel):
    id: str
    name: str
    color: str | None = None
    default_proxy: str | None = None
    notes: str = ""
    derived: bool                    # True when the workspace only exists as a lane-id prefix
    health: str                      # ok | attention | blocked | idle
    summary: str                     # "18 of 29 ready"
    ready: int
    total: int
    created_at: str | None = None
    updated_at: str | None = None
    lanes: list[LaneView] = Field(default_factory=list)


class WorkspaceCount(BaseModel):
    id: str
    name: str
    summary: str
    ready: int
    total: int


class QueueView(BaseModel):
    """The login queue: what the owner still has to log in or fix, with the login URL for each."""
    workspace: str | None = None
    summary: str
    ready: int
    total: int
    needs_login: int
    needs_attention: int
    workspaces: list[WorkspaceCount] = Field(default_factory=list)
    lanes: list[LaneView] = Field(default_factory=list)


class PlanStep(BaseModel):
    lane: str
    workspace: str
    surface: str
    platform: str
    engine: str
    proxy: str | None = None
    action: Literal["create", "exists", "conflict"]
    reasons: list[str] = Field(default_factory=list)     # why this step is a conflict
    overridden: list[str] = Field(default_factory=list)  # proxy rules bypassed by the override flag
    warnings: list[str] = Field(default_factory=list)
    home_url: str | None = None
    login_url: str | None = None
    sites: list[str] | None = None


class Plan(BaseModel):
    """What applying a template would do. Built with no side effects. Apply re-checks each step
    against the live state before it creates anything, so a plan is a promise to check, not a
    guarantee."""
    template: str
    workspaces: list[str]
    steps: list[PlanStep]
    override_proxy_rules: bool = False
    reason: str | None = None

    def count(self, action: str) -> int:
        return sum(1 for step in self.steps if step.action == action)

    @property
    def creates(self) -> list[PlanStep]:
        return [step for step in self.steps if step.action == "create"]

    @property
    def conflicts(self) -> list[PlanStep]:
        return [step for step in self.steps if step.action == "conflict"]

    def public(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "workspaces": self.workspaces,
            "ok": not self.conflicts,
            "counts": {"create": self.count("create"), "exists": self.count("exists"),
                       "conflict": self.count("conflict")},
            "override_proxy_rules": self.override_proxy_rules,
            "steps": [step.model_dump(mode="json") for step in self.steps],
        }

    def approval_content(self) -> dict[str, Any]:
        """What an approval binds to. Deterministic: the same plan always hashes the same."""
        return {
            "template": self.template,
            "workspaces": self.workspaces,
            "override_proxy_rules": self.override_proxy_rules,
            "reason": self.reason,
            "steps": [{"lane": s.lane, "action": s.action, "proxy": s.proxy, "platform": s.platform,
                       "engine": s.engine, "overridden": s.overridden} for s in self.steps],
        }


class ApplyResult(BaseModel):
    """`complete` when every planned lane exists and matches; `partial` when a step failed. The
    lanes listed in `created` are real; `remaining` says what a second run will still do."""
    status: Literal["complete", "partial"]
    created: list[str] = Field(default_factory=list)
    existing: list[str] = Field(default_factory=list)
    overridden: list[dict[str, Any]] = Field(default_factory=list)
    failed: dict[str, str] | None = None   # {"lane", "code", "message"}
    remaining: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OverrideRecord(BaseModel):
    id: int
    at: str
    by: str
    lane: str
    proxy: str | None = None
    rules: list[str]
    reason: str
