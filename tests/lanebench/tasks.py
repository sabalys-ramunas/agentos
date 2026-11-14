"""Tasks: one TOML file each under tasks/, loaded and checked here.

A task names its goal (what an executor is told), its mode and effect ceiling, how the site
serves it (seed, mutations, adversarial fixtures, login variant, signed in or not), its success
checks (checkers.py, read through the checker), the effects it may cause, and the hand-written
replay steps (executors/replay.py). A repair task also holds the stale locator and the parity
cases its patch must pass. Every URL in a task is a path on the fixture site: a task cannot name
another host, so it can never point a lane at a real site.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lanebench import adversarial, mutations

TASKS_DIR = Path(__file__).resolve().parent / "tasks"
Mode = Literal["goal", "resolve", "repair", "login_assist"]
Ceiling = Literal["read", "draft", "commit"]


def _relative(url: str) -> str:
    if not url.startswith("/") or url.startswith("//"):
        raise ValueError(f"{url!r}: task URLs are paths on the fixture site (start with one '/')")
    return url


class Repair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stale: dict[str, Any]                       # the connector step's locator that no longer works
    target: str                                 # the data-lb key of the element it must find
    page: str = "/feed"
    parity: list[dict[str, Any]] = Field(default_factory=list)  # [{mutations = [...]}, ...]


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")
    title: str
    goal: str
    mode: Mode = "goal"
    ceiling: Ceiling = "draft"
    tags: list[str] = Field(default_factory=list)
    start: str = "/feed"
    signed_in: bool = True
    seed: int | None = None
    cookie_wall: bool = False
    after_login: Literal["feed", "save_login", "consent", "device"] = "feed"
    mutation: list[dict[str, Any]] = Field(default_factory=list)
    adversarial: list[str] = Field(default_factory=list)
    allowed_effects: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)  # staged in the lane's inbox (sites.app.FIXTURE_FILES)
    check: list[dict[str, Any]] = Field(default_factory=list)
    replay: list[dict[str, Any]] = Field(default_factory=list)
    repair: Repair | None = None
    path: str | None = None

    @field_validator("start")
    @classmethod
    def _start(cls, v: str) -> str:
        return _relative(v)

    @model_validator(mode="after")
    def _valid(self) -> Task:
        mutations.validate(self.mutation)
        adversarial.fixtures(self.adversarial)
        from lanebench.sites.app import FIXTURE_FILES

        for step in self.replay:
            if step.get("do") == "navigate":
                _relative(str(step.get("url", "")))
            if step.get("do") == "upload" and step.get("file") not in self.files:
                raise ValueError(f"{self.id}: the replay uploads {step.get('file')!r}, which files does not stage")
        unknown_files = [f for f in self.files if f not in FIXTURE_FILES]
        if unknown_files:
            raise ValueError(f"{self.id}: no fixture file {unknown_files}; there are {sorted(FIXTURE_FILES)}")
        if not self.check:
            raise ValueError(f"{self.id}: a task needs at least one [[check]]")
        from lanebench.checkers import CHECKS

        unknown = [c.get("kind") for c in self.check if c.get("kind") not in CHECKS]
        if unknown:
            raise ValueError(f"{self.id}: unknown check {unknown}")
        if self.mode == "repair" and self.repair is None:
            raise ValueError(f"{self.id}: a repair task needs a [repair] table")
        return self

    @property
    def locale(self) -> str:
        return mutations.lang(self.mutation)

    @property
    def adversarial_task(self) -> bool:
        return bool(self.adversarial)

    def plan(self, seed: int) -> dict[str, Any]:
        return {"seed": self.seed if self.seed is not None else seed, "mutations": self.mutation,
                "adversarial": self.adversarial, "cookie_wall": self.cookie_wall, "after_login": self.after_login,
                "signed_in": self.signed_in}


def load(path: Path) -> Task:
    data = tomllib.loads(path.read_text())
    return Task.model_validate({**data, "path": str(path)})


def load_all(names: list[str] | None = None, directory: Path = TASKS_DIR) -> list[Task]:
    tasks = [load(p) for p in sorted(directory.glob("*.toml"))]
    ids = [t.id for t in tasks]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"two tasks share an id: {sorted(dupes)}")
    if not names:
        return tasks
    chosen: list[Task] = []
    for name in names:
        hits = [t for t in tasks if t.id == name or name in t.tags or (name.endswith("*") and t.id.startswith(name[:-1]))]
        if not hits:
            raise ValueError(f"no task or tag {name!r}; tasks: {', '.join(ids)}")
        chosen += [t for t in hits if t not in chosen]
    return chosen
