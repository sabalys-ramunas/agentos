"""The tools' names as an agent reads them, and the old names they had until 0.3 (aliases for one release).

The 2026-11-19 surface (docs/design/tool-surface-review.md): agents think in a terminal, files and a
browser, not in lanes, so no tool name says "lane" any more. The node registers every tool under its new
name. An old name stays callable for one release (agentos.mcp.registry resolves it before anything else,
so grants, the policy, freezes and traces only ever see the new name), is never listed, and its result
carries a one-line `renamed` note so an agent learns the new name. The same for an argument a renamed
tool took under another name (`wait_ms` is `wait_s`).

Internal code keeps the word lane (the lane manager, `env/<id>-browser` ids, module names): only what an
agent or a person reads changes. Traces written before the rename keep the old names, so whatever reads
a trace's tool name for a person goes through `current()` first.

The aliases go in the next minor release (CHANGELOG.md, "Deprecated").
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# old name -> new name
RENAMED: dict[str, str] = {
    # an environment's terminal and files: the words a model knows from its own Bash, Read, Write, Edit
    "lane_exec": "terminal_exec",
    "lane_job_start": "terminal_job_start",
    "lane_job_read": "terminal_job_read",
    "lane_job_cancel": "terminal_job_cancel",
    "lane_jobs_list": "terminal_jobs",
    "lane_file_read": "file_read",
    "lane_file_write": "file_write",
    "lane_file_edit": "file_edit",
    # the human handed the browser back
    "lane_wait_control": "browser_wait_handback",
    # the owner's account browsers
    "lanes_list": "browsers_list",
    "lane_open": "browser_open",
    "lane_close": "browser_close",
    "lane_claim": "browser_claim",
    "lane_release": "browser_release",
    "lane_view": "browser_view",
    "lane_control": "browser_control",
    "lane_request_control": "browser_request_control",
    "lane_queue": "browser_queue",
    "lane_check": "browser_check",
    "lane_freeze": "browser_freeze",
    "lane_login_request": "browser_login_request",
    "lane_set_readiness": "browser_set_readiness",
    "lane_do": "browser_task",
    # a browser's inbox and downloads, not the machine's files
    "files_put": "browser_file_put",
    "files_list": "browser_files",
    "files_get": "browser_file_get",
    "files_delete": "browser_file_delete",
    # terminals the owner keeps on the node (`env_` read as "environment")
    "env_create": "terminal_create",
    "env_remove": "terminal_remove",
    "env_lanes_list": "terminals_list",
    "lane_install": "terminal_install",
    "lane_upload": "terminal_upload",
    "lane_upload_check": "terminal_upload_check",
}
NEW_NAMES: frozenset[str] = frozenset(RENAMED.values())
REMOVED_IN = "the next minor release"


def _ms_to_s(value: Any) -> Any:
    return value / 1000 if isinstance(value, int | float) and not isinstance(value, bool) else value


# new tool -> {old argument: (new argument, how an old value becomes a new one)}: one name per meaning
# on the renamed tools. `wait_s` is how long this call waits; `timeout_s` stays the command's own limit.
RENAMED_ARGS: dict[str, dict[str, tuple[str, Callable[[Any], Any] | None]]] = {
    "terminal_job_read": {"wait_ms": ("wait_s", _ms_to_s)},
    "browser_wait_handback": {"timeout_s": ("wait_s", None)},
}


def current(name: str) -> str:
    """A tool's name today: an old name's new one, any other name as it is (a trace's, a call's)."""
    return RENAMED.get(name, name)


def old_name(name: str) -> str | None:
    """The name a tool had before the rename, or None."""
    return _OLD.get(name)


_OLD = {new: old for old, new in RENAMED.items()}


def rename_arguments(tool: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """A call's arguments with the old names of `tool`'s arguments as their new ones, and what was renamed
    (`["wait_ms is now wait_s"]`). A new name given too wins over its old one."""
    table = RENAMED_ARGS.get(tool)
    if not table or not isinstance(arguments, dict) or not any(k in table for k in arguments):
        return arguments, []
    out = dict(arguments)
    notes = []
    for old, (new, convert) in table.items():
        if old not in out:
            continue
        value = out.pop(old)
        notes.append(f"{old} is now {new}" + (" (in seconds)" if convert is _ms_to_s else ""))
        out.setdefault(new, convert(value) if convert else value)
    return out, notes


def renamed_note(called: str, tool: str, arg_notes: list[str]) -> str | None:
    """The one line a call by an old name (or with an old argument) gets back, or None."""
    parts = [f"{called} is now {tool}"] if called != tool else []
    parts += arg_notes
    if not parts:
        return None
    return f"{'; '.join(parts)}. The old name works until {REMOVED_IN}."
