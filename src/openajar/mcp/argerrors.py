"""The words for a call's argument mistakes: an argument the tool does not take (with the one most
likely meant and the ones it does take), a tool name the caller has none by, and pydantic's refusals,
naming fields and never the rejected values. Moved out of openajar.mcp.registry, which re-exports them;
registry's own helpers (_cut, tool_params, with_catalog_lines) are imported where they are used, so the
two modules do not import each other at load time."""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import ValidationError

    from openajar.core.tools import ToolSpec

MAX_UNKNOWN_SHOWN = 5
MAX_KNOWN_SHOWN = 12
# names agents reach for that a close-spelling match misses: (wrong, right), used when the tool takes `right`
ARG_SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": ("title",), "label": ("title",), "repository": ("repo",), "repo_url": ("repo",), "git_url": ("repo",),
    "url": ("repo",), "branch": ("ref",), "tag": ("ref",), "commit": ("ref",), "machine": ("size",),
    "server_type": ("size",), "duration": ("hours",), "ttl": ("hours",), "timeout": ("wait_s", "timeout_s"),
    "mode": ("keep",), "persist": ("keep",), "id": ("on",), "env": ("on",), "env_id": ("on",),
    "environment": ("on",), "environment_id": ("on",), "lane": ("on",), "lane_id": ("on",), "command": ("cmd",),
    "dir": ("cwd",), "workdir": ("cwd",), "file": ("path",), "filename": ("path",), "text": ("content",),
}


# an argument agents pass to the wrong tool, with what to do instead (the evals, 2026-11-18: Haiku closed
# with keep="save" twice, since keep is environment_open's)
ARG_ELSEWHERE: dict[tuple[str, str], str] = {
    ("environment_close", "keep"): ("keep is chosen at environment_open, not at the close. To keep this one's "
                                    "work now: push it, or environment_save, then close"),
}


def _closest(key: str, known: frozenset[str]) -> str | None:
    """The argument an agent most likely meant: a known synonym, a close spelling, or one that starts
    the other (`repository` -> `repo`)."""
    low = key.lower()
    for right in ARG_SYNONYMS.get(low, ()):
        if right in known:
            return right
    close = difflib.get_close_matches(low, sorted(known), n=1, cutoff=0.6)
    if close:
        return close[0]
    prefix = [k for k in sorted(known) if len(k) >= 3 and (low.startswith(k) or k.startswith(low))]
    return prefix[0] if len(prefix) == 1 else None


def unknown_arguments(spec: ToolSpec, known: frozenset[str] | None, arguments: Any) -> str | None:
    """`invalid: ...` naming every argument the tool does not take, with the one most likely meant
    (`repository` -> `repo`, `name` -> `title`), and the arguments it does take, or None. The SDK would
    drop them silently, and the tool would run on its default. A typed flow tool on a fixed lane still
    takes `on` and ignores it, as it always has."""
    from openajar.mcp.registry import _cut, tool_params

    if known is None or not isinstance(arguments, dict):
        return None
    allowed = known | ({"on"} if spec.lane_arg is None and "flow" in spec.tags else set())
    extra = [k for k in arguments if k not in allowed]
    if not extra:
        return None
    misplaced = next((k for k in extra if (spec.name, k) in ARG_ELSEWHERE), None)
    if misplaced is not None:
        return _cut(f"invalid: {spec.name} has no argument {misplaced!r}: {ARG_ELSEWHERE[(spec.name, misplaced)]}.")
    parts = []
    for key in extra[:MAX_UNKNOWN_SHOWN]:
        shown = str(key) if len(str(key)) <= 40 else str(key)[:37] + "..."
        close = _closest(str(key), known)
        parts.append(f"{shown!r} (did you mean {close!r}?)" if close else repr(shown))
    more = f" and {len(extra) - MAX_UNKNOWN_SHOWN} more" if len(extra) > MAX_UNKNOWN_SHOWN else ""
    word = "argument" if len(extra) == 1 else "arguments"
    try:
        names = [n for n in (p.name for p in tool_params(spec)) if n in known] or sorted(known)
    except ValueError:  # a tool whose schema is not built from its handler
        names = sorted(known)
    takes = ", ".join(names[:MAX_KNOWN_SHOWN]) + (", ..." if len(names) > MAX_KNOWN_SHOWN else "")
    return _cut(f"invalid: {spec.name} has no {word} {', '.join(parts)}{more}. It takes: {takes}. "
                f"{shows_arguments(spec.name)}")


def shows_arguments(tool: str) -> str:
    """Where an agent reads a tool's arguments: tools_more with the tool's name works for every tool,
    listed or reached through tools_call (tools/list shows only the tools in its list)."""
    return f'tools_more(toolset="{tool}") describes each one'


def unknown_tool(name: str, known: Iterable[str]) -> str:
    """not_found for a tool this caller has no tool by, naming a close one it does have, and a fix line
    that works: tools_more lists every tool of every toolset, tools/list only the loaded ones."""
    shown = name if len(name) <= 64 else name[:61] + "..."
    close = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.6) if len(name) <= 64 else []
    mean = f" (did you mean {close[0]!r}?)" if close else ""
    return (f"not_found: no tool named {shown!r}{mean}\n"
            "fix: Call tools_more() for every tool you have, then call one with tools_call(name, arguments).")


def invalid_arguments(exc: ValidationError, tool: str | None = None) -> str:
    """Field names and pydantic's messages only: the rejected values are the caller's data. With
    `tool`, where its arguments are shown, then the catalog's fix line."""
    from openajar.mcp.registry import _cut, with_catalog_lines

    parts = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        where = ".".join(str(part) for part in err["loc"]) or "arguments"
        parts.append(f"{where}: {err['msg']}")
    message = _cut("invalid: " + "; ".join(parts))
    if tool is None:
        return message
    return with_catalog_lines(_cut(f"{message}; {shows_arguments(tool)}"))
