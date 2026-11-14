"""The tasks of the studio eval: one file each, `TASK = Task(...)`.

A task is a prompt as a user would give it, the pages it starts from, an optional setup the
runner does first (a baseline, a profile the owner approved), a checker that reads the final
state (the page files, a fresh ui_check by the runner, the profile, the agent's answer), and two
scripted agents that prove the checker: `perfect` does what the skill teaches and must pass,
`careless` takes a plausible wrong turn and must fail.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from studio_eval.harness import Mcp, Site, Studio, ToolFailed, Trace

DARK_CSS = """html { font: 16px/1.5 Arial, Helvetica, sans-serif; color: #111111; background: #ffffff; }
body { margin: 0; padding: 16px; }
h1, h2, h3, p { margin: 0 0 12px; }
button { font: inherit; padding: 8px 16px; min-height: 44px; }
@media (prefers-color-scheme: dark) { html { color: #eeeeee; background: #111111; } }"""


def html(title: str, body: str, css: str = "", *, base: str = DARK_CSS) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>{title}</title><style>\n{base}\n{css}\n</style>'
            f"</head>\n<body>\n{body}\n</body></html>\n")


@dataclass
class Env:
    """What a task's setup, checker and scripted agents see."""

    task_id: str
    folder: Path
    base: str
    studio: Studio
    site: Site
    mcp: Mcp                                   # the runner's own calls (not an agent's)
    agent: Trace | None = None                 # a scripted agent's recorded calls
    answer: dict[str, Any] = field(default_factory=dict)
    rows: list[Any] = field(default_factory=list)
    saved: dict[str, Any] = field(default_factory=dict)   # what setup wants the checker to know

    def url(self, page: str) -> str:
        return self.base + page

    def read(self, page: str) -> str:
        return (self.folder / page).read_text()

    def write(self, page: str, text: str) -> None:
        (self.folder / page).write_text(text)

    def edit(self, page: str, old: str, new: str) -> None:
        text = self.read(page)
        if old not in text:
            raise AssertionError(f"{old[:60]!r} is not in {page}")
        self.write(page, text.replace(old, new, 1))

    async def check(self, page: str, **args: Any) -> dict[str, Any]:
        """The runner's own ui_check, for the ground truth."""
        return await self.mcp.call("ui_check", {"url": self.url(page), **args})

    def summary(self) -> str:
        return str(self.answer.get("summary", "")).lower()

    def called(self, tool: str) -> list[Any]:
        return [r for r in self.rows if r.tool == tool]


Check = tuple[str, bool]


@dataclass
class Task:
    id: str
    title: str
    prompt: str                                          # {url}, {base} and setup's saved values filled in
    pages: dict[str, str]
    check: Callable[[Env], Awaitable[list[Check]]]
    perfect: Callable[[Env], Awaitable[dict[str, Any]]]
    careless: Callable[[Env], Awaitable[dict[str, Any]]]
    page: str = "index.html"                             # the page {url} names
    setup: Callable[[Env], Awaitable[None]] | None = None
    images: str = "never"                                # never, crop or any (harness.wrong_turns)
    url_override: Callable[[Env], str] | None = None     # a url that is not on the site (a server that is down)

    def url(self, env: Env) -> str:
        return self.url_override(env) if self.url_override else env.url(self.page)

    def render_prompt(self, env: Env) -> str:
        return self.prompt.format(url=self.url(env), base=env.base, **env.saved)


def counts(result: dict[str, Any]) -> dict[str, int]:
    return result.get("counts", {}).get("by_check", {})


async def still_has(env: Env, page: str, check: str, **args: Any) -> bool:
    try:
        return check in counts(await env.check(page, **args))
    except ToolFailed:
        return True


def all_tasks() -> list[Task]:
    out = []
    for mod in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if mod.name.startswith("t"):
            out.append(importlib.import_module(f"{__name__}.{mod.name}").TASK)
    return out


__all__ = ["DARK_CSS", "Check", "Env", "Task", "ToolFailed", "all_tasks", "counts", "html", "still_has"]
