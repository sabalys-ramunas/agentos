"""Reading a page outline (browser_snapshot, or snapshot="full" on another call) in a scripted agent: the ref
of the element that holds some words. Merged from the evals/ suite of 2026-11-20."""

from __future__ import annotations

import json
import re
from typing import Any

REF = re.compile(r"\[ref=([a-z0-9]+)\]")


def outline_of(result: Any) -> str:
    """The outline in a result: the text itself, or after the first JSON line when snapshot was asked."""
    if isinstance(result, dict):
        return str(result.get("outline") or result.get("snapshot") or json.dumps(result))
    return str(result)


def ref(outline: str, *needles: str, role: str | None = None, nth: int = 0) -> str:
    """The ref of the line that holds every needle (any case) and, when given, names the role."""
    found = []
    for line in outline.splitlines():
        low = line.lower()
        if all(n.lower() in low for n in needles) and (role is None or re.search(rf"\b{role}\b", low)):
            hit = REF.search(line)
            if hit:
                found.append(hit.group(1))
    if len(found) <= nth:
        raise LookupError(f"no element with {needles} (role {role}) in the outline:\n{outline[:1500]}")
    return found[nth]


async def goto(agent: Any, url: str) -> str:
    return outline_of(await agent.on_call("browser_navigate", url=url, snapshot="full"))


async def act(agent: Any, outline: str, action: str, *needles: str, text: str | None = None, role: str | None = None,
              nth: int = 0, snapshot: str = "full") -> str:
    args: dict[str, Any] = {"ref": ref(outline, *needles, role=role, nth=nth), "action": action, "snapshot": snapshot}
    if text is not None:
        args["text"] = text
    return outline_of(await agent.on_call("browser_act", **args))
