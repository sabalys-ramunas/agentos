"""The lab's named sets, from split.toml. A set is a list of (task, seed, variant)."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from lanebench.lab.hard import hard
from lanebench.lab.harvest import twin
from lanebench.tasks import Task, load_all

SPLIT = tomllib.loads((Path(__file__).resolve().parent / "split.toml").read_text())
SETS = ("quick", "tuning", "hard-tuning", "heldout", "heldout-tasks", "heldout-twins", "hard-heldout")


def jobs(names: list[str], tasks: list[str] | None = None) -> list[tuple[Task, int, str]]:
    everything = [t for t in load_all() if t.id not in SPLIT["excluded_tasks"]]
    seen = [t for t in everything if t.id not in SPLIT["heldout_tasks"]]
    unseen = [t for t in everything if t.id in SPLIT["heldout_tasks"]]
    plans: dict[str, tuple[list[Task], list[int], str]] = {
        "quick": (seen, SPLIT["quick_seeds"], "base"),
        "tuning": (seen, SPLIT["tuning_seeds"], "base"),
        "heldout": (seen, SPLIT["heldout_seeds"], "base"),
        "heldout-tasks": (unseen, SPLIT["heldout_seeds"], "base"),
        "heldout-twins": ([twin(t) for t in everything if t.locale != "lt"], SPLIT["twin_seeds"], "twin"),
        "hard-tuning": ([hard(t) for t in seen], SPLIT["quick_seeds"], "hard"),
        "hard-heldout": ([hard(t) for t in everything], SPLIT["twin_seeds"], "hard"),
    }
    out: list[tuple[Task, int, str]] = []
    for name in names:
        chosen, seeds, variant = plans[name]
        out += [(t, s, variant) for t in chosen for s in seeds if not tasks or t.id in tasks]
    return out


def describe(job: tuple[Task, int, str]) -> dict[str, Any]:
    task, seed, variant = job
    return {"task": task.id, "seed": seed, "variant": variant}
