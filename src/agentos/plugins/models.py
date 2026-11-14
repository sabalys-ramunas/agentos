"""What a plugin is: a declared, typed manifest of reviewed first-party code in this repo.

There are no entry points and nothing is loaded from outside the repo (docs/PRINCIPLES.md,
principle 8). A manifest only names modules of this package tree, by string, the way the daemon's
FEATURE_TOOLS does, so reading a manifest imports no other feature.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from agentos.plugins.requirements import NodeFacts

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
JOB_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
FIRST_PARTY = "agentos."
CHANGE_KIND = "plugin_change"  # the kind of a Change's row in the action log


class Skill(StrEnum):
    """What a job needs its model to be able to do. Brains say what they can do in their
    Capabilities; `agentos.plugins.service.brain_skills` reads that."""
    DECISION = "decision"  # answers typed questions in process
    TEXT = "text"          # writes text, as a task
    TOOLS = "tools"        # works with tools, as a task

    @property
    def words(self) -> str:
        return {"decision": "a decision brain", "text": "a text brain", "tools": "a tool brain"}[self.value]


class Stage(StrEnum):
    WORKS = "works"        # the code is here, or is being built, and the worker can run
    DECLARED = "declared"  # named so the plan is visible; no engine exists, so it cannot be enabled
    LATER = "later"        # planned for later; nothing exists yet


@dataclass(frozen=True)
class Check:
    """One requirement, checked now. `reason` is a plain sentence either way."""
    label: str
    ok: bool
    reason: str


class Requirement(Protocol):
    @property
    def label(self) -> str: ...

    def check(self, facts: NodeFacts) -> Check: ...


@dataclass(frozen=True)
class Job:
    """One piece of a worker's work that a model does. Any one of `skills` is enough. An optional
    job can stay empty, and `without` says what the worker does then."""
    name: str
    description: str
    skills: tuple[Skill, ...]
    optional: bool = False
    without: str = ""

    def __post_init__(self) -> None:
        if not JOB_NAME_RE.match(self.name):
            raise ValueError(f"invalid job name {self.name!r}")
        if not self.skills:
            raise ValueError(f"job {self.name} must say what its model has to be able to do")
        if self.optional != bool(self.without):
            raise ValueError(f"job {self.name}: an optional job says what happens without it, and only that one does")

    @property
    def needs(self) -> str:
        return " or ".join(skill.words for skill in self.skills)


@dataclass(frozen=True)
class Change:
    """One change a person made to a plugin: names only, as the action log keeps it."""
    plugin: str
    what: str  # enabled, disabled, assigned, unassigned, lane_on or lane_off
    by: str
    job: str | None = None
    before: str | None = None  # the brain that had the job
    after: str | None = None   # the brain that has it now
    lane: str | None = None    # the lane a lane_on or lane_off is about

    @property
    def summary(self) -> str:
        if self.lane is not None:
            return f"{self.plugin} turned {'on' if self.what == 'lane_on' else 'off'} on {self.lane}"
        if self.job is None:
            return f"{self.plugin} {self.what}"
        if self.after is None:
            return f"{self.job} job of {self.plugin} cleared (it was {self.before})"
        if self.before is None:
            return f"{self.job} job of {self.plugin} given to {self.after}"
        return f"{self.job} job of {self.plugin} moved from {self.before} to {self.after}"

    def trace(self) -> dict[str, Any]:
        """This change as the fields of an action log row, whoever holds the log."""
        return {"kind": CHANGE_KIND, "tool": "plugins", "status": "ok", "principal": self.by,
                "principal_kind": "human", "summary": self.summary, "args": dataclasses.asdict(self)}


class ChangeLog(Protocol):
    """Where a change is recorded. The composition root provides it, on the node's action log:
    this package does not import agentos.policy."""

    def record(self, change: Change) -> None:
        """Returns once the record is kept, and raises otherwise."""

    def read(self, plugin: str, limit: int) -> list[dict[str, Any]]:
        """The newest changes to one plugin first: at, principal (who), summary, args."""

    def close(self) -> None: ...


@dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    description: str
    stage: Stage
    lane_kinds: tuple[str, ...]        # lane kinds it works on; each one is checked on this node
    services: tuple[str, ...] = ()     # feature modules with `build(runtime)`, loaded only when it is on
    tools: tuple[str, ...] = ()        # feature modules with `tool_specs(runtime)`, loaded only when it is on
    tool_names: tuple[str, ...] = ()   # the MCP tools those modules serve: a call to one while it is off is told why
    cli: str = ""                      # a module with a Typer `app`, mounted as `agentos <id>` whether it is on or not
    needs: tuple[Requirement, ...] = ()
    jobs: tuple[Job, ...] = ()

    def __post_init__(self) -> None:
        if not PLUGIN_ID_RE.match(self.id):
            raise ValueError(f"invalid plugin id {self.id!r}")
        outside = [m for m in (*self.services, *self.tools, *filter(None, (self.cli,))) if not m.startswith(FIRST_PARTY)]
        if outside:
            raise ValueError(f"{self.id} names modules outside this repo: {', '.join(outside)}")
        names = [job.name for job in self.jobs]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.id} declares a job twice")

    def job(self, name: str) -> Job | None:
        return next((job for job in self.jobs if job.name == name), None)
