"""Toolsets: the node's tools in named groups, and which groups one MCP connection is shown.

Every connected agent gets the whole tool list (names, descriptions, input schemas) in its
context on every turn. A full node serves about 112 tools, roughly 20,000 tokens, before a single
connector or saved flow adds its own. That costs tokens on every turn, and a model picks tools
less reliably as the list grows, so the everyday loop (open a lane, look, act, read back, wait
for a person) gets worse as agentos grows. So a node shows a small `core` toolset by default,
and an agent loads the rest when it needs them.

- TOOLSETS names every group. A tool belongs to the toolset that names it, else to the first
  toolset sharing one of its tags (so a connector operation `<site>_<op>` lands in `connectors`
  and a saved flow's `flow_<name>` in `flows` when they appear, also when they are added while
  the node runs), else to `other`. A test keeps every built-in tool out of `other`.
- What a connection is shown: `core`, plus the toolsets the node's `mcp_toolsets` setting
  names (node.toml; ["core"] by default), or instead the ones the MCP URL asks for
  (`/mcp?toolsets=files,flows`), plus the toolsets the agent loaded in this MCP session with
  `tools_more`. `full` (in either place) shows every tool and no meta tools, as before toolsets.
- Two meta tools are added to every list that is not full: `tools_more` (the toolsets and,
  for one, its tools with descriptions and argument schemas; it also adds that toolset to this
  session's list and sends notifications/tools/list_changed) and `tools_call` (call any tool by
  name, for clients that do not re-read their list).

A toolset only decides what tools/list shows. It never decides what a caller may call: every
call, direct or through tools_call, takes the same path (the per-token visible filter, the
SDK's argument validation, the policy wrapper with its grants, approvals and redaction), and
tools_more lists only tools that filter lets the caller see.
"""

from __future__ import annotations

import contextvars
import functools
import logging
import re
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

if TYPE_CHECKING:  # kept out of the import: `agentos connect` reads the names and stays light
    from mcp.types import Tool

    from agentos.core.tools import ToolSpec

log = logging.getLogger(__name__)

CORE = "core"
FULL = "full"
OTHER = "other"
FULL_ALIASES = frozenset({FULL, "all"})
QUERY_PARAM = "toolsets"
MAX_SESSIONS = 1024  # sessions whose loaded toolsets are remembered (least recently used dropped)

TOOLS_MORE = "tools_more"
TOOLS_CALL = "tools_call"
META_TOOLS = frozenset({TOOLS_MORE, TOOLS_CALL})


@dataclass(frozen=True)
class Toolset:
    name: str
    summary: str  # one short line an agent reads in tools_more's description
    tools: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()  # a tool no toolset names lands in the first toolset sharing a tag
    hint: str = ""  # a few words for tools_more's description, when the name alone does not say it


TOOLSETS: tuple[Toolset, ...] = (
    # core: what the howto recipes and the server instructions need for the everyday jobs (a post
    # with an image, a vault login, a hand-off to the human, waiting for an approval or a run),
    # so an agent does those without loading anything. approval_confirm rather than approval_status:
    # an approval_pending result names it, and approval_wait already returns the status. Everything else is one tools_more away.
    Toolset(CORE, "the everyday lane loop: find and open a lane, look, act, wait, upload, log in, ask a person", (
        "whoami", "howto", "lanes_list", "lane_open", "lane_close", "lane_claim", "lane_release",
        "lane_view", "lane_wait_control", "browser_navigate", "browser_snapshot", "browser_act",
        "browser_wait", "browser_screenshot", "browser_tabs", "browser_dialog", "browser_dialog_answer",
        "browser_upload", "files_put", "login", "approval_wait", "approval_confirm", "human_ask",
        "memory_context", "run_status", "environment_open", "environment", "environment_close",
        # an environment's terminal: what nearly every chat with one runs, so it needs no tools_more
        # (about 830 tokens here, against 3,450 for the whole terminals toolset in such a chat)
        "lane_exec", "lane_job_start", "lane_job_read", "lane_job_cancel",
    )),
    Toolset("files", "more for a browser lane: its files (inbox and downloads), switching tabs, scrolling a feed", (
        "files_get", "files_list", "files_delete", "browser_tab", "browser_scroll",
    ), ("files",), "files, tabs, scrolling"),
    Toolset("devtools", "why a page broke: its console messages and page errors, its requests and which "
            "failed, and one response's body", (
        "browser_console", "browser_network",
    ), ("devtools",)),
    Toolset("control", "sharing lanes: who has one, asking a person for it, the queue, a safety check, a freeze", (
        "lane_control", "lane_request_control", "lane_queue", "lane_check", "lane_freeze",
    ), ("control",), "who has a lane, asking for it, queue, freeze"),
    Toolset("vault", "stored logins: what the vault holds, filling one login field, asking for a log in", (
        "vault_list", "vault_fill", "lane_login_request",
    ), ("vault",), "stored logins"),
    Toolset("flows", "saved flows and their runs: list, read, run, record, save, cancel, Ask me first", (
        "flows_list", "flow_get", "flow_run", "flow_save", "flow_record_start", "flow_record_stop", "run_cancel",
        "ask_first_on",
    ), ("flows",), "saved flows and runs"),
    Toolset("workflows", "routines (workflows): a trigger such as a schedule, a webhook or an email, then "
            "steps; save, propose, run, retry, and the leads and opt-outs their mail steps use", (
        "workflow_catalog", "workflow_list", "workflow_get", "workflow_save", "workflow_propose",
        "workflow_run", "workflow_runs", "workflow_retry", "leads_propose", "send_suppress",
    ), ("workflows",), "routines on a schedule or a trigger"),
    Toolset("connectors", "installed connector operations, one tool per site operation (like "
            "instagram_post_photo), and their health", (
        "connectors_list", "connector_status",
    ), ("connectors",), "site operations"),
    Toolset("site_apis", "a site's data as typed endpoints: find what a page repeats, try a draft, ask a "
            "person to turn it on, list them", (
        "site_api_discover", "site_api_try", "site_api_propose", "site_apis_list",
    ), ("site_apis",), "a page's data as an API"),
    Toolset("mail", "mailboxes: list, read, mark, send, and asking a person to connect one", (
        "mail_list", "mail_read", "mail_mark", "mail_send", "mail_connect",
    ), ("mail",)),
    Toolset("memory", "workspace memory: search, read, write, forget", (
        "memory_search", "memory_get", "memory_write", "memory_forget",
    ), ("memory",)),
    Toolset("profiles", "who the work is for: a person's or business's bios and voice, a product's brand and "
            "rules (profile_use)", (
        "profile_list", "profile_get", "profile_use", "profile_propose", "profile_save", "profile_update",
        "profile_attach", "profile_detach", "profile_sync_status", "profile_mark_synced", "profile_photo_put",
        "profile_learn_start", "profile_learn_finish", "profile_learn_brand",
    ), ("profiles",)),
    Toolset("studio", "is a page all right: ui_check (layout, contrast, targets, names, health, design system), "
            "ui_diff (what a change did), system_check, baselines, a finding or an image on request", (
        "ui_check", "ui_diff", "system_check", "ui_baseline", "finding_get", "ui_image", "studio_status",
    ), ("studio",), "check a page you build"),
    Toolset("workspaces", "workspaces: list, plan and apply changes, lane readiness", (
        "workspace_list", "workspace_queue", "workspace_plan", "workspace_apply", "lane_set_readiness",
    ), ("workspaces",)),
    Toolset("worker", "the node's workers: hand a browser job to the cheap worker on the node (lane_do), and "
            "which workers are on", (
        "lane_do", "plugins_list",
    ), ("plugins",), "hand a browser job to the node"),
    Toolset("admin", "your grant, an approval's status without waiting (or all yours still waiting), and the call trail", (
        "grant_info", "approval_status", "traces_query",
    ), ("policy", "approvals", "traces"), "grant, approvals, call trail"),
    Toolset("terminals", "terminal and service lanes: shells, commands, background jobs, synced files, installs, "
            "images, disposable cloud VMs", (
        "env_create", "env_lanes_list", "env_remove", "lane_install", "lane_upload_check",
        "lane_upload", "lane_jobs_list", "shell_open",
        "shell_read", "shell_write", "shell_close", "service_start", "service_stop", "service_status",
        "service_logs", "image_build", "images_list", "vm_create", "vm_destroy", "vm_extend", "vm_clone",
        "vms_list",
    ), ("env",), "shells, commands, services"),
    Toolset("brains", "agent tasks on the node's brains: run, follow, resume, cancel; the git remotes they reach", (
        "brains_list", "brain_run", "brain_status", "brain_tail", "brain_tasks", "brain_resume",
        "brain_cancel", "brain_use", "git_remotes",
    ), ("brains", "git"), "agent tasks on the node"),
    Toolset("automation", "events, triggers, engagement programs (likes, comments, follows, visits) and the agenda "
            "(planned one-off actions, their approval, runs and proof)", (
        "events_list", "triggers_list", "trigger_propose", "engage_program_draft", "engage_status",
        "engage_items", "engage_item_submit", "engage_item_withdraw", "engage_digest",
        "agenda_list", "agenda_get", "agenda_draft", "agenda_update", "agenda_propose", "agenda_cancel",
    ), ("events", "engage", "agenda"), "events, triggers, engagement, agenda"),
)
NAMES: tuple[str, ...] = tuple(t.name for t in TOOLSETS)
_BY_TOOL = {tool: ts.name for ts in TOOLSETS for tool in ts.tools}
_SUMMARY = {ts.name: ts.summary for ts in TOOLSETS} | {OTHER: "tools no other toolset holds"}
_SPLIT_RE = re.compile(r"[\s,]+")


def toolset_of(spec: ToolSpec) -> str:
    """The toolset a tool belongs to: the one naming it, else the first sharing a tag, else other."""
    named = _BY_TOOL.get(spec.name)
    if named is not None:
        return named
    tags = set(spec.tags)
    for ts in TOOLSETS:
        if tags.intersection(ts.tags):
            return ts.name
    return OTHER


def parse_toolsets(value: str | Iterable[str] | None) -> frozenset[str] | None:
    """A toolset choice (`"files,flows"`, a list, or None) -> the toolsets shown, core always
    among them; None means full (every tool). Unknown names are left out with a warning."""
    if value is None:
        return frozenset({CORE})
    words = _SPLIT_RE.split(value) if isinstance(value, str) else [str(v) for v in value]
    chosen = {CORE}
    for word in (w.strip().lower() for w in words):
        if not word:
            continue
        if word in FULL_ALIASES:
            return None
        if word in NAMES or word == OTHER:
            chosen.add(word)
        else:
            log.warning("unknown toolset %r left out (toolsets: %s)", word[:40], ", ".join(NAMES))
    return frozenset(chosen)


@functools.lru_cache(maxsize=128)
def parse_query(value: str) -> frozenset[str] | None:
    """parse_toolsets for a URL's `?toolsets=`, read on every request: parsed (and an unknown
    name warned about) once per distinct value."""
    return parse_toolsets(value)


# ---- what the current request's connection asked for ----

@dataclass(frozen=True)
class Connection:
    """The MCP session id and the `toolsets` query parameter of the current HTTP request."""
    session: str | None = None
    toolsets: str | None = None  # None: the URL did not ask; the node's default applies


request_connection: contextvars.ContextVar[Connection | None] = contextvars.ContextVar(
    "agentos_mcp_request_connection", default=None)


@contextmanager
def bind_connection(*, session: str | None, query_string: bytes | str = b"") -> Iterator[None]:
    """Bind the request's session id and `?toolsets=` for one request (agentos.mcp.http does this
    next to the bearer token), so tools/list and tools_more see what this connection asked for."""
    raw = query_string.decode("latin-1") if isinstance(query_string, bytes) else query_string
    values = parse_qs(raw).get(QUERY_PARAM)
    reset = request_connection.set(Connection(session=(session or None) and session[:200],
                                              toolsets=",".join(values)[:400] if values else None))
    try:
        yield
    finally:
        request_connection.reset(reset)


def current_connection() -> Connection:
    return request_connection.get() or Connection()


class Loaded:
    """The toolsets agents loaded with tools_more, per (token, MCP session id), in memory. A
    session id is only known to its own client; the token is part of the key anyway, so a
    leaked id alone changes nothing. It only ever adds to what tools/list shows."""

    def __init__(self, limit: int = MAX_SESSIONS):
        self.limit = limit
        self._sets: OrderedDict[tuple[str | None, str], frozenset[str]] = OrderedDict()

    def get(self, token: str | None, session: str | None) -> frozenset[str]:
        if session is None:
            return frozenset()
        key = (token, session)
        found = self._sets.get(key)
        if found is None:
            return frozenset()
        self._sets.move_to_end(key)
        return found

    def add(self, token: str | None, session: str | None, toolset: str) -> bool:
        """Remember `toolset` for this session; False when there is no session to remember it in."""
        if session is None:
            return False
        key = (token, session)
        self._sets[key] = self._sets.get(key, frozenset()) | {toolset}
        self._sets.move_to_end(key)
        while len(self._sets) > self.limit:
            self._sets.popitem(last=False)
        return True


# ---- the meta tools ----

def more_description() -> str:
    names = ", ".join(f"{ts.name} ({ts.hint})" if ts.hint else ts.name for ts in TOOLSETS if ts.name != CORE)
    return ("More agentos tools, by toolset: "
            f"{names}. tools_more(toolset) returns its tools with their arguments and adds them to your list "
            "(a tool's name works too); with no toolset it lists every toolset's tools. tools_call runs any "
            "tool, listed or not.")


CALL_DESCRIPTION = ("Call any agentos tool by name with its arguments, including tools not in your list "
                    "(see tools_more). It is the same call as calling the tool directly: your grant, "
                    "approvals and argument checks apply, and the result is the tool's own.")


def meta_tools() -> list[Tool]:
    from mcp.types import Tool, ToolAnnotations

    return [
        Tool(name=TOOLS_MORE, description=more_description(), annotations=ToolAnnotations(read_only_hint=True),
             input_schema={"type": "object", "properties": {"toolset": {
                 "type": "string", "description": "A toolset name, or a tool's name to load its toolset."}}}),
        Tool(name=TOOLS_CALL, description=CALL_DESCRIPTION,
             annotations=ToolAnnotations(read_only_hint=False),
             input_schema={"type": "object", "required": ["name"], "properties": {
                 "name": {"type": "string", "description": "The tool to call, for example files_put."},
                 "arguments": {"type": "object", "description": "The tool's arguments, as you would pass them."}}}),
    ]


def overview(groups: dict[str, list[Any]], shown: frozenset[str]) -> dict[str, Any]:
    """tools_more with no toolset: every toolset this caller has tools in, with their names."""
    rows = [{"toolset": name, "summary": _SUMMARY[name], "loaded": name in shown,
             "tools": [t.name for t in groups[name]]}
            for name in (*NAMES, OTHER) if groups.get(name)]
    return {"toolsets": rows,
            "next": "Call tools_more with a toolset to get its tools' arguments, or tools_call to run one."}


def detail(name: str, tools: list[Any], *, state: str) -> dict[str, Any]:
    """tools_more(toolset): its tools as an agent needs them to call one. `state`: "listed" (they
    already were), "loaded" (added to this session's list now) or "not_listed" (no session)."""
    if state == "listed":
        how = "These tools are already in your list."
    elif state == "loaded":
        how = ("These tools are in your list now if your client re-reads it (notifications/tools/list_changed "
               "was sent). If they do not appear, call them with tools_call(name, arguments).")
    else:
        how = "Call them with tools_call(name, arguments)."
    return {"toolset": name, "summary": _SUMMARY.get(name, ""),
            "tools": [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                      for t in tools],
            "next": how}


def more(groups: dict[str, list[Any]], shown: frozenset[str] | None, wanted: Any,
         load: Callable[[str], bool]) -> tuple[dict[str, Any] | None, str | None, bool]:
    """tools_more, for any server: `groups` is every tool the caller may use, by toolset; `shown`
    what its list shows (None: every tool); `load(toolset)` remembers a toolset for this session
    (False: no session). Returns (result, error message, whether the list changed)."""
    everything = frozenset(groups) if shown is None else shown
    if wanted is None or (isinstance(wanted, str) and not wanted.strip()):
        return overview(groups, everything), None, False
    if not isinstance(wanted, str):
        return None, "invalid: toolset: must be a string", False
    key = wanted.strip().lower()
    if key not in groups:
        owner = next((name for name, tools in groups.items() if any(t.name == key for t in tools)), None)
        if owner is None:
            known = ", ".join(n for n in (*NAMES, OTHER) if n in groups)
            shown_name = key if len(key) <= 64 else key[:61] + "..."
            return None, f"invalid: toolset: no toolset or tool of yours named {shown_name!r} (yours: {known})", False
        key = owner
    if key in everything:
        return detail(key, groups[key], state="listed"), None, False
    if load(key):
        return detail(key, groups[key], state="loaded"), None, True
    return detail(key, groups[key], state="not_listed"), None, False


def call_target(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]] | str:
    """tools_call's arguments -> (tool name, its arguments), or the error message."""
    name, args = arguments.get("name"), arguments.get("arguments")
    args = {} if args is None else args
    if not isinstance(name, str) or not name:
        return "invalid: name: give the tool to call, for example files_put"
    if not isinstance(args, dict):
        return "invalid: arguments: must be an object of the tool's arguments"
    if name in META_TOOLS:
        return f"invalid: name: call {name} directly, not through tools_call"
    return name, args
