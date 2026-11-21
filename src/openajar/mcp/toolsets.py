"""Toolsets: the node's tools in named groups, and which groups one MCP connection is shown.

Every connected agent gets the whole tool list (names, descriptions, input schemas) in its
context on every turn. A full node serves about 112 tools, roughly 20,000 tokens, before a single
connector or saved flow adds its own. That costs tokens on every turn, and a model picks tools
less reliably as the list grows, so the everyday loop (open a lane, look, act, read back, wait
for a person) gets worse as openajar grows. So a node shows a small `core` toolset by default,
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

from openajar.core import toolnames
from openajar.core.toolsets import CORE, FULL_ALIASES, NAMES, OTHER, TOOLSET_ALIASES, TOOLSETS

if TYPE_CHECKING:  # kept out of the import: `openajar connect` reads the names and stays light
    from mcp.types import Tool

    from openajar.core.tools import ToolSpec

log = logging.getLogger(__name__)

QUERY_PARAM = "toolsets"
MAX_SESSIONS = 1024  # sessions whose loaded toolsets are remembered (least recently used dropped)

TOOLS_MORE = "tools_more"
TOOLS_CALL = "tools_call"
META_TOOLS = frozenset({TOOLS_MORE, TOOLS_CALL})

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
        word = TOOLSET_ALIASES.get(word, word)
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
    "openajar_mcp_request_connection", default=None)


@contextmanager
def bind_connection(*, session: str | None, query_string: bytes | str = b"") -> Iterator[None]:
    """Bind the request's session id and `?toolsets=` for one request (openajar.mcp.http does this
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
    return ("More openajar tools, by toolset: "
            f"{names}. tools_more(toolset) adds its tools to your list and returns their arguments (a tool's "
            "name works too); with none, it lists them all. tools_call runs any tool, listed or not.")


CALL_DESCRIPTION = ("Call any openajar tool by name with its arguments, also one not in your list (see "
                    "tools_more): the same call as calling it directly, with your grant, approvals and "
                    "argument checks, and the tool's own result.")


def meta_tools() -> list[Tool]:
    from mcp.types import Tool, ToolAnnotations

    return [
        Tool(name=TOOLS_MORE, description=more_description(), annotations=ToolAnnotations(read_only_hint=True),
             input_schema={"type": "object", "properties": {"toolset": {
                 "type": "string", "description": "A toolset name, or a tool's name to load its toolset."}}}),
        Tool(name=TOOLS_CALL, description=CALL_DESCRIPTION,
             annotations=ToolAnnotations(read_only_hint=False),
             input_schema={"type": "object", "required": ["name"], "properties": {
                 "name": {"type": "string", "description": "The tool to call, for example terminal_jobs (terminals toolset)."},
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
    key = toolnames.current(TOOLSET_ALIASES.get(wanted.strip().lower(), wanted.strip().lower()))
    tool = None  # a tool's name: only that tool's description and arguments (its toolset is loaded)
    if key not in groups:
        owner = next((name for name, tools in groups.items() if any(t.name == key for t in tools)), None)
        if owner is None:
            known = ", ".join(n for n in (*NAMES, OTHER) if n in groups)
            shown_name = key if len(key) <= 64 else key[:61] + "..."
            return None, f"invalid: toolset: no toolset or tool of yours named {shown_name!r} (yours: {known})", False
        key, tool = owner, key
    tools = [t for t in groups[key] if tool is None or t.name == tool]
    if key in everything:
        return detail(key, tools, state="listed"), None, False
    if load(key):
        return detail(key, tools, state="loaded"), None, True
    return detail(key, tools, state="not_listed"), None, False


def call_target(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]] | str:
    """tools_call's arguments -> (tool name, its arguments), or the error message."""
    name, args = arguments.get("name"), arguments.get("arguments")
    args = {} if args is None else args
    if not isinstance(name, str) or not name:
        return "invalid: name: give the tool to call, for example terminal_jobs"
    if not isinstance(args, dict):
        return "invalid: arguments: must be an object of the tool's arguments"
    if name in META_TOOLS:
        return f"invalid: name: call {name} directly, not through tools_call"
    return name, args
