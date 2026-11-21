"""The tools' names as an agent reads them, and the names they had before 0.4.0 (for errors and old records).

The 2026-11-19 surface (docs/design/tool-surface-review.md): agents think in a terminal, files and a
browser, not in lanes, so no tool name says "lane" any more. 0.4.0 kept the old names callable as hidden
aliases for one release; 0.5.0 removed them. A call by an old tool name, an old merged name or an old
argument name now fails like any unknown tool or argument, and this table exists so that error names the
tool (and the argument) to use instead: `lane_exec is terminal_exec`, `browser_tab is browser_tabs(select=...)`,
`wait_ms is wait_s (in seconds)` (openajar.mcp.argerrors, openajar.mcp.toolsets). Nothing resolves an old
name to a tool, so grants, the policy, freezes and traces only ever see the new names.

Internal code keeps the word lane (the lane manager, `env/<id>-browser` ids, module names): only what an
agent or a person reads changes. Traces written before the rename keep the old names, so whatever reads
a trace's tool name for a person goes through `current()` first: that reads old records, it calls nothing.

Three tools merged into another the same day, where two competed for one intent (the review's
"Merges"): `browser_tab` is `browser_tabs(select=...)`, `browser_dialog_answer` is `browser_dialog(accept=...)`,
`approval_status` is `approval_wait(wait_s=0)` (MERGED).

The CLI's client still calls a node from before 0.4.0 by the old names when such a node says it has no tool
by the new one (`for_older_node`): a person's CLI can be upgraded before their node is. That is the client
speaking an older node's language, not an alias on the node.
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

# old tool -> (the tool it merged into, how a call by the old name is made with it): one tool with an
# argument where two competed for one intent
MERGED: dict[str, tuple[str, str]] = {
    "browser_tab": ("browser_tabs", "browser_tabs(select=...)"),           # its `index` is `select`
    "browser_dialog_answer": ("browser_dialog", "browser_dialog(accept=...)"),  # without accept, a read
    "approval_status": ("approval_wait", "approval_wait(wait_s=0)"),       # 0 reads it now, without waiting
}
REMOVED = "0.5.0"  # the release that removed the old names (0.4.0 renamed the tools)


def _ms_to_s(value: Any) -> Any:
    return value / 1000 if isinstance(value, int | float) and not isinstance(value, bool) else value


# new tool -> {old argument: (new argument, how an old value becomes a new one)}: one name per meaning
# on the renamed tools and the default list. `wait_s` is how long this call waits; `timeout_s` stays a
# command's own limit (terminal_exec, terminal_job_start).
RENAMED_ARGS: dict[str, dict[str, tuple[str, Callable[[Any], Any] | None]]] = {
    "terminal_job_read": {"wait_ms": ("wait_s", _ms_to_s)},
    "browser_wait_handback": {"timeout_s": ("wait_s", None)},
    "browser_wait": {"timeout_s": ("wait_s", None)},
    "approval_wait": {"timeout_s": ("wait_s", None)},
}


def current(name: str) -> str:
    """A tool's name today: an old name's new one (or the tool it merged into), any other name as it is
    (a trace's, a call's)."""
    if name in MERGED:
        return MERGED[name][0]
    return RENAMED.get(name, name)


def use_instead(name: str) -> str | None:
    """For a tool name removed in 0.5.0, how to make the same call today (`lane_exec is terminal_exec`,
    `browser_tab is browser_tabs(select=...)`), or None."""
    if name in MERGED:
        return f"{name} is {MERGED[name][1]}"
    if name in RENAMED:
        return f"{name} is {RENAMED[name]}"
    return None


def call_instead(name: str) -> str | None:
    """The fix line's call for a removed tool name: the new tool and, where they changed, its arguments
    (`terminal_job_read, with wait_s for wait_ms (in seconds)`), or None."""
    if name in MERGED:
        return MERGED[name][1]
    new = RENAMED.get(name)
    if new is None:
        return None
    args = [old_argument(new, old) for old in RENAMED_ARGS.get(new, {})]
    return new + (f" with the same arguments ({'; '.join(a for a in args if a)})" if args else
                  " with the same arguments")


def old_argument(tool: str, argument: str) -> str | None:
    """For an argument name of `tool` removed in 0.5.0, its new name (`wait_ms is wait_s, in seconds`), or None."""
    entry = RENAMED_ARGS.get(tool, {}).get(argument)
    if entry is None:
        return None
    new, convert = entry
    return f"{argument} is {new}" + (", in seconds" if convert is _ms_to_s else "")


_OLD = {new: old for old, new in RENAMED.items()}


def for_older_node(tool: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The same call as a node from before 0.4.0 takes it (its old name and argument names), or None
    when neither `tool` nor an argument it is given was renamed. The CLI's client tries it when such a node
    says it has no `tool`, or no such argument (approval_wait's wait_s was timeout_s)."""
    old = _OLD.get(tool, tool)
    out = dict(arguments)
    for old_arg, (new_arg, convert) in RENAMED_ARGS.get(tool, {}).items():
        if new_arg in out:
            value = out.pop(new_arg)
            out[old_arg] = int(value * 1000) if convert is _ms_to_s and isinstance(value, int | float) else value
    if old == tool and out == arguments:
        return None
    return old, out
