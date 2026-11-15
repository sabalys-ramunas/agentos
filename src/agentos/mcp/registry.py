"""Every feature's ToolSpecs on the MCP SDK, through one wrapper (see agentos/core/tools.py).

    server = build_mcp(runtime, [node_tools.tool_specs, vault_tools.tool_specs], wrap=allow_all_wrap)

For each spec the registry:
- derives the input schema from the handler's keyword-only parameters: type hints, defaults,
  and a description from `Annotated[T, Field(description=...)]` or the docstring's `Args:`
  section (the lane argument gets a standard description when it has none);
- sets MCP tool annotations from the effect (read only, destructive) and tags (open world);
- leaves out a spec whose `hidden_without` service is not on the runtime, and refuses two
  specs with the same name;
- builds the per-call function once with `wrap(spec, runtime)`; every call runs through it;
- maps errors to results whose text starts with the code, followed by the error catalog's next
  step for the agent (`fix: ...`) and, when waiting helps, `retry_after: <seconds>`
  (agentos.core.catalog); turns ApprovalPending into a structured non-error result, and masks
  known secrets in everything it returns.

A provider may also return PromptSpecs: MCP prompts, short fixed recipes a user picks in their
client (Claude Code lists them as /mcp__agentos__<name>). They carry no handler and touch no
lane; `hidden_without` and duplicate names work as for tools.

The wrapper is the policy seam. `allow_all_wrap` (M1) runs every call as the owner and allows
every effect; agentos.policy provides `wrap_tool(spec, runtime)` with the same signature. The
HTTP layer (agentos.mcp.http) binds the request's bearer token with `bind_request_token` for
the duration of each request; a `visible` filter `(token, specs) -> specs` narrows tools/list,
and calls, per token.

`toolsets` (agentos.mcp.toolsets) narrows tools/list further, per connection, to the toolsets
it asked for, and adds the meta tools tools_more and tools_call. It changes what is listed,
never what may be called: a call to any tool, listed or not, direct or through tools_call, goes
through the same visible filter, argument validation and wrapper.

The tool list may change while the node runs (a connector installed, a flow saved): the server
keeps the providers and the wrapper it was built with, `unregister` takes a tool away, and
`ToolServer.tools_changed()` tells connected clients to list their tools again
(`notifications/tools/list_changed`). agentos.mcp.reload does the diff and calls them.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import difflib
import inspect
import logging
import re
import traceback
import weakref
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Annotated, Any, get_args, get_origin, get_type_hints

import anyio
import pydantic_core
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.prompts.base import Prompt, PromptArgument
from mcp.shared.exceptions import MCPError
from mcp.shared.subscriptions import ToolsListChanged
from mcp.types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ImageContent,
    InputRequiredResult,
    TextContent,
    ToolAnnotations,
    ToolListChangedNotification,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import Field, ValidationError
from pydantic.fields import FieldInfo

from agentos import __version__
from agentos.core import catalog, logs
from agentos.core.errors import AgentosError, ApprovalPending, EngineError
from agentos.core.models import Effect, Principal
from agentos.core.runtime import Runtime
from agentos.core.tools import (
    ClientAnswer,
    PromptSpec,
    ToolImage,
    ToolSpec,
    ToolView,
    current_request_token,
    elicitor_var,
)
from agentos.environments import routing as env_routing
from agentos.mcp import toolsets as ts
from agentos.node import interrupts
from agentos.policy.redactor import Redactor
from agentos.policy.scrub import runtime_scrub

log = logging.getLogger(__name__)

Call = Callable[..., Awaitable[Any]]
Wrap = Callable[[ToolSpec, Runtime], Call]
"""`wrap(spec, runtime)` is called once per tool at registration and returns `async call(**arguments)`.
The call builds the ToolContext, runs `spec.handler(ctx, **arguments)` and returns its result; it may
raise AgentosError (or any exception with string `code` and `message` attributes) to fail the call."""

Provider = Callable[[Runtime], "list[ToolSpec | PromptSpec]"]
VisibleFilter = Callable[[str | None, list[ToolSpec]], list[ToolSpec]]
"""`visible(token, specs)` returns the specs this bearer token may see and call (None: no token)."""

MAX_ERROR_CHARS = 600
TRACE_VALUE_CHARS = 200
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
SECRETISH_RE = re.compile(r"pass|secret|token|cookie|credential|auth|otp|seed|key", re.IGNORECASE)
LANE_ARG_DESCRIPTION = 'Lane id: "workspace/name".'
APPROVAL_NEXT = ("A human must approve this call first. Tell the user what is waiting, then call this tool "
                 "again with the same arguments once they approve. Do not retry it in a loop.")

DESTRUCTIVE = frozenset({Effect.COMMIT, Effect.PAY, Effect.IRREVERSIBLE, Effect.EXEC, Effect.INSTALL_BASE})
OPEN_WORLD_TAGS = frozenset({"browser", "web"})
# A READ-effect tool that still changes something (opens a lane, moves a page, asks a person, runs
# a flow): its grant ceiling stays READ, but clients must not treat it as read only.
CHANGES_STATE = "changes_state"


# ---- the request's bearer token ----


# ---- the M1 wrapper: the owner may do everything ----

OWNER = Principal(kind="agent", id="owner", owner="owner")


def safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Trace fields fit for a log: values under secret-looking keys masked, long strings cut."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if SECRETISH_RE.search(key):
            out[key] = "***"
        elif isinstance(value, str) and len(value) > TRACE_VALUE_CHARS:
            out[key] = value[:TRACE_VALUE_CHARS] + "..."
        else:
            out[key] = value
    return out


@dataclass
class OwnerContext:
    """The ToolContext of `allow_all_wrap`: an agent acting for the owner, every effect allowed.
    The principal is an agent, never a human, so lanes a human has taken over still refuse it."""
    runtime: Runtime
    tool: str
    principal: Principal = field(default_factory=lambda: OWNER.model_copy())

    async def authorize(
        self,
        effect: Effect,
        *,
        lane_id: str | None = None,
        domain: str | None = None,
        summary: str | None = None,
        content: Any = None,
    ) -> None:
        log.debug("authorize %s: %s on %s allowed (owner)", self.tool, Effect(effect).value, lane_id or "-")

    def trace(self, kind: str, **fields: Any) -> None:
        if log.isEnabledFor(logging.DEBUG):
            log.debug("trace %s %s %s", self.tool, kind, safe_fields(fields))


def grant_hint(spec: ToolSpec, has_special: bool = False) -> str:
    """Why a tool this node serves is not open to a grant, from what it needs: a special the grant
    leaves out, or more than its ceiling. `has_special`: the grant already uses another tool of the
    same special (it can write memory), so the gap is the ceiling (memory_forget is irreversible)."""
    effect = getattr(spec.effect, "value", str(spec.effect))
    if "memory" in getattr(spec, "tags", ()) and not has_special:  # memory_rules.MEMORY_TAG: memory tools need the memory special, whatever their effect
        special = "memory_read" if getattr(spec.effect, "value", None) == "read" else "memory"
        what = "reads workspace memory" if special == "memory_read" else "writes workspace memory"
        return (f"{spec.name} {what}, which your grant does not include. Tell the human: "
                f"`agentos connect <node> --special {special} --new-grant` on their computer gives one that does")
    if spec.name.startswith("environment"):  # the vm special on env/* lanes, as `connect` gives by default
        return (f"{spec.name} needs the environments special, which your grant does not include. Tell the "
                "human: `agentos connect <node> --special environments --new-grant` on their computer gives one "
                "that does (keep the other --special flags it has)")
    if getattr(spec.effect, "rank", 0) is None:
        return (f"{spec.name} needs the {effect} special, which your grant does not include. Tell the human: "
                f"`agentos connect <node> --special {effect} --new-grant` on their computer gives one that does")
    return (f"{spec.name} needs a grant that allows {effect}, and yours does not. Tell the human: "
            f"`agentos connect <node> --ceiling {effect} --new-grant` on their computer gives one that does")


def allow_all_wrap(spec: ToolSpec, runtime: Runtime) -> Call:
    """The default Wrap until agentos.policy is configured: every call runs as the owner."""

    async def call(**arguments: Any) -> Any:
        ctx = OwnerContext(runtime=runtime, tool=spec.name)
        ctx.trace("call", **arguments)
        env_routing.route(runtime, spec.name, arguments, session=None, grant_id=None)  # no `on`: the environment
        freeze = runtime.get("freeze")  # agentos.freeze stops every MCP caller, the owner's agent too
        # a studio check goes through the environment's tunnel, or fails while it is down
        with env_routing.studio_proxy(runtime, spec.name, arguments, session=None, grant_id=None):
            if freeze is None:
                return await spec.handler(ctx, **arguments)
            lane_id = arguments.get(spec.lane_arg) if spec.lane_arg else None
            ws = arguments.get("workspace")
            extra = {"workspace": ws} if isinstance(ws, str) and ws else {}
            with freeze.guard(tool=spec.name, lane_id=lane_id if isinstance(lane_id, str) else None, chain=(),
                              **extra):
                return await spec.handler(ctx, **arguments)

    return call


# ---- schema derivation ----

SECTION_RE = re.compile(r"^(Args|Arguments|Parameters|Keyword Args|Keyword Arguments):\s*$")
ARG_LINE_RE = re.compile(r"^\*{0,2}(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$")
LIST_ITEM_RE = re.compile(r"^(?:[-*+]\s|\d+[.)]\s)")


def _unwrap(text: str) -> str:
    """Join the hard-wrapped lines of each paragraph; keep paragraph breaks, list items and
    indented lines (a section's entries) on lines of their own."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip("\n")):
        lines = [line for line in block.splitlines() if line.strip()]
        joined = ""
        base = len(lines[0]) - len(lines[0].lstrip()) if lines else 0
        for line in lines:
            indented = len(line) - len(line.lstrip()) > base
            line = line.strip()
            if not joined:
                joined = line
            elif indented or LIST_ITEM_RE.match(line):
                joined += "\n" + line
            else:
                joined += " " + line
        if joined:
            paragraphs.append(joined)
    return "\n\n".join(paragraphs)


def split_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """A handler docstring -> (tool description, {parameter: description}).

    The `Args:` section (Google style) becomes parameter descriptions and is left out of the
    tool description; everything else is kept, with hard-wrapped lines joined.
    """
    if not doc:
        return "", {}
    lines = inspect.cleandoc(doc).splitlines()
    kept: list[str] = []
    args: dict[str, str] = {}
    i = 0
    while i < len(lines):
        if not SECTION_RE.match(lines[i]):
            kept.append(lines[i])
            i += 1
            continue
        i += 1
        indent: int | None = None
        current: str | None = None
        while i < len(lines):
            raw = lines[i]
            if raw.strip() and not raw[0].isspace():
                break  # the next paragraph or section starts at column 0
            if raw.strip():
                width = len(raw) - len(raw.lstrip())
                indent = width if indent is None else indent
                match = ARG_LINE_RE.match(raw.strip())
                if width == indent and match:
                    current = match.group(1)
                    args[current] = match.group(2).strip()
                elif current is not None:
                    args[current] = f"{args[current]} {raw.strip()}".strip()
            i += 1
    return _unwrap("\n".join(kept)), args


def _field_description(annotation: Any) -> str | None:
    if get_origin(annotation) is Annotated:
        for meta in get_args(annotation)[1:]:
            if isinstance(meta, FieldInfo) and meta.description:
                return meta.description
    return None


@dataclass(frozen=True)
class ToolParam:
    name: str
    annotation: Any
    default: Any = inspect.Parameter.empty
    description: str | None = None

    @property
    def required(self) -> bool:
        return self.default is inspect.Parameter.empty

    def sdk_parameter(self) -> inspect.Parameter:
        annotation = self.annotation
        if self.description and _field_description(annotation) is None:
            annotation = Annotated[annotation, Field(description=self.description)]
        return inspect.Parameter(self.name, inspect.Parameter.KEYWORD_ONLY, default=self.default,
                                 annotation=annotation)


def tool_description(spec: ToolSpec) -> str:
    """An explicit `description` is used as given; the docstring default loses its Args section."""
    doc = spec.handler.__doc__ or ""
    if spec.description and spec.description != doc.strip():
        return spec.description
    return split_docstring(doc)[0]


def tool_params(spec: ToolSpec) -> list[ToolParam]:
    """The handler's keyword-only parameters after `ctx`, as the tool's arguments."""
    handler = spec.handler
    try:
        hints = get_type_hints(handler, include_extras=True)
    except Exception as exc:
        raise ValueError(f"tool {spec.name}: cannot resolve the handler's type hints ({exc})") from exc
    params = list(inspect.signature(handler).parameters.values())
    positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    if not params or params[0].kind not in positional:
        raise ValueError(f"tool {spec.name}: the handler's first parameter must be the ToolContext")
    _, arg_docs = split_docstring(handler.__doc__)
    out: list[ToolParam] = []
    for p in params[1:]:
        if p.kind is not inspect.Parameter.KEYWORD_ONLY:
            raise ValueError(f"tool {spec.name}: parameter {p.name!r} must be keyword-only (after `*`)")
        if p.name.startswith("_"):
            raise ValueError(f"tool {spec.name}: parameter {p.name!r} must not start with an underscore")
        if p.name not in hints:
            raise ValueError(f"tool {spec.name}: parameter {p.name!r} needs a type hint")
        annotation = hints[p.name]
        description = _field_description(annotation) or arg_docs.get(p.name)
        default = p.default
        if p.name == spec.lane_arg and p.name == "on" and env_routing.routes(spec.name):
            # leave `on` out and the call goes to the chat's environment (agentos.environments.routing)
            annotation, default = annotation | None, None
            description = env_routing.LANE_ARG_TEXT
        if description is None and p.name == spec.lane_arg:
            description = LANE_ARG_DESCRIPTION
        out.append(ToolParam(p.name, annotation, default, description))
    return out


def tool_annotations(spec: ToolSpec) -> ToolAnnotations:
    """MCP hints from the effect: READ is read only; COMMIT, PAY, IRREVERSIBLE, EXEC and
    INSTALL_BASE are destructive; tags "browser" or "web" mark the open world, "idempotent"
    marks a call that is safe to repeat, CHANGES_STATE keeps a READ tool from reading as read only."""
    tags = set(spec.tags)
    open_world = True if tags & OPEN_WORLD_TAGS else None
    idempotent = True if "idempotent" in tags else None
    if spec.effect is Effect.READ and CHANGES_STATE not in tags:
        return ToolAnnotations(read_only_hint=True, idempotent_hint=idempotent, open_world_hint=open_world)
    return ToolAnnotations(read_only_hint=False, destructive_hint=spec.effect in DESTRUCTIVE,
                           idempotent_hint=idempotent, open_world_hint=open_world)


# ---- results and errors ----

def error_code(exc: BaseException) -> tuple[str, str] | None:
    """(code, message) for an AgentosError, or any error carrying string `code` and `message`
    (the policy wrapper's own error type); None for an unexpected exception."""
    if isinstance(exc, AgentosError):
        return exc.code, exc.message
    code, message = getattr(exc, "code", None), getattr(exc, "message", None)
    if isinstance(code, str) and CODE_RE.match(code) and isinstance(message, str):
        return code, message
    return None


def _cut(message: str) -> str:
    return message if len(message) <= MAX_ERROR_CHARS else message[: MAX_ERROR_CHARS - 3] + "..."


def error_message(exc: BaseException, redactor: Redactor) -> str:
    """`<code>: <message>`; anything unexpected becomes engine_error with its type and first line."""
    coded = error_code(exc)
    if coded is not None:
        message = f"{coded[0]}: {coded[1]}"
    else:
        lines = str(exc).strip().splitlines()
        message = f"{EngineError.code}: {type(exc).__name__}" + (f": {lines[0]}" if lines else "")
    return _cut(redactor.text(message))


def with_catalog_lines(message: str) -> str:
    """The error text an agent gets: `<code>: <message>`, then the error catalog's next step
    (`fix: ...`) and, when waiting helps, `retry_after: <seconds>`."""
    code, _, text = message.partition(": ")
    return "\n".join([message, *catalog.mcp_lines(code, text)])


def error_result(message: str) -> CallToolResult:
    # Returned rather than raised: the SDK prefixes raised ToolErrors with "Error executing tool",
    # and agents should see the code first.
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


MAX_UNKNOWN_SHOWN = 5


def unknown_arguments(spec: ToolSpec, known: frozenset[str] | None, arguments: Any) -> str | None:
    """`invalid: ...` naming every argument the tool does not take, with the closest parameter when one
    is close (`acton` -> `action`), or None. The SDK would drop them silently, and the tool would run on
    its default. A typed flow tool on a fixed lane still takes `on` and ignores it, as it always has."""
    if known is None or not isinstance(arguments, dict):
        return None
    allowed = known | ({"on"} if spec.lane_arg is None and "flow" in spec.tags else set())
    extra = [k for k in arguments if k not in allowed]
    if not extra:
        return None
    parts = []
    for key in extra[:MAX_UNKNOWN_SHOWN]:
        shown = str(key) if len(str(key)) <= 40 else str(key)[:37] + "..."
        close = difflib.get_close_matches(str(key), sorted(known), n=1, cutoff=0.6)
        parts.append(f"{shown!r} (did you mean {close[0]!r}?)" if close else repr(shown))
    more = f" and {len(extra) - MAX_UNKNOWN_SHOWN} more" if len(extra) > MAX_UNKNOWN_SHOWN else ""
    word = "argument" if len(extra) == 1 else "arguments"
    return _cut(f"invalid: {spec.name} has no {word} {', '.join(parts)}{more}; {shows_arguments(spec.name)}")


def shows_arguments(tool: str) -> str:
    """Where an agent reads a tool's arguments: tools_more with the tool's name works for every tool,
    listed or reached through tools_call (tools/list shows only the tools in its list)."""
    return f'tools_more(toolset="{tool}") shows what it takes'


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
    parts = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        where = ".".join(str(part) for part in err["loc"]) or "arguments"
        parts.append(f"{where}: {err['msg']}")
    message = _cut("invalid: " + "; ".join(parts))
    if tool is None:
        return message
    return with_catalog_lines(_cut(f"{message}; {shows_arguments(tool)}"))


def _image(image: ToolImage) -> ImageContent:
    return ImageContent(type="image", data=base64.b64encode(image.data).decode(), mime_type=image.mime_type)


def _json(value: Any) -> str:
    return pydantic_core.to_json(value, fallback=str, indent=2).decode()


def _jsonable(value: Any, redactor: Redactor) -> Any:
    """Plain JSON data (pydantic models and enums dumped), with known secrets masked."""
    return redactor.value(pydantic_core.to_jsonable_python(value, fallback=str))


def _block(item: Any, redactor: Redactor) -> TextContent | ImageContent:
    if isinstance(item, ToolImage):
        return _image(item)
    if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
        return TextContent(type="text", text=redactor.text(item["text"]))
    if isinstance(item, str):
        return TextContent(type="text", text=redactor.text(item))
    return TextContent(type="text", text=_json(_jsonable(item, redactor)))


def to_result(value: Any, redactor: Redactor) -> CallToolResult | InputRequiredResult:
    """A handler's return value as an MCP result: a dict is structured content plus its JSON text,
    a string is text, a ToolImage is an image, a list holding a ToolImage is a list of blocks."""
    if isinstance(value, CallToolResult | InputRequiredResult):
        return value
    if isinstance(value, ToolImage):
        return CallToolResult(content=[_image(value)])
    if isinstance(value, list | tuple) and any(isinstance(item, ToolImage) for item in value):
        return CallToolResult(content=[_block(item, redactor) for item in value])
    if isinstance(value, str):
        return CallToolResult(content=[TextContent(type="text", text=redactor.text(value))])
    data = _jsonable(value, redactor)
    text = TextContent(type="text", text=_json(data))
    if isinstance(data, dict):
        return CallToolResult(content=[text], structured_content=data)
    return CallToolResult(content=[text])


def approval_result(spec: ToolSpec, exc: ApprovalPending, redactor: Redactor) -> CallToolResult:
    """Not a failure: the call waits for a human co-sign, so the agent gets the id and what waits."""
    summary = getattr(exc, "summary", None) or exc.message
    log.info("tool %s: waiting for approval %s", spec.name, exc.approval_id)
    return to_result({"status": "approval_pending", "approval_id": exc.approval_id, "tool": spec.name,
                      "summary": _cut(summary), "next": APPROVAL_NEXT, **(getattr(exc, "confirm", None) or {})}, redactor)


def failure_result(spec: ToolSpec, exc: BaseException, redactor: Redactor) -> CallToolResult:
    message = error_message(exc, redactor)
    if error_code(exc) is not None:
        log.info("tool %s: %s", spec.name, message)
    else:
        log.error("tool %s failed: %s", spec.name, message)
        if log.isEnabledFor(logging.DEBUG):
            # The raw traceback may quote a secret (a proxy URL), so it is redacted like the result.
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            log.debug("tool %s traceback:\n%s", spec.name, redactor.text(tb))
    return error_result(with_catalog_lines(message))


# ---- a prompt only the client can answer ----

ELICIT_KEY = "confirm"  # the one input a tool asks for on a client that answers in a second round


class SessionElicitor:
    """agentos.core.tools.Elicitor over the SDK. A client on the session protocols is asked on the
    open call (the request rides the POST's stream, so this needs this repo's stateful streamable
    HTTP); a 2026-07-28 client has no way back on a call, so it gets an `InputRequiredResult` and sends
    the call again with its answer, the state sealed by the SDK. Neither passes through the model."""

    def __init__(self, context: Any):
        self.context = context
        self.session = context.request_context.session
        params = self.session.client_params
        info = params.client_info if params is not None else None
        self.client = f"{info.name} {info.version}".strip() if info is not None else None

    def _second_round(self) -> bool:
        return self.session.protocol_version in MODERN_PROTOCOL_VERSIONS

    def can_ask(self) -> bool:
        capabilities = self.session.client_capabilities
        elicitation = capabilities.elicitation if capabilities is not None else None
        if elicitation is None or (elicitation.form is None and elicitation.url is not None):
            return False  # a client that takes only links cannot show a form
        return self._second_round() or self.session.can_send_request

    def answered(self) -> ClientAnswer | None:
        responses, state = self.context.input_responses, self.context.request_state
        response = (responses or {}).get(ELICIT_KEY)
        if response is None or state is None or not hasattr(response, "action"):
            return None
        return ClientAnswer(state, response.action, response.content)

    async def ask(self, message: str, schema: dict, *, state: str, timeout_s: float) -> Any:
        if self._second_round():
            request = ElicitRequest(params=ElicitRequestFormParams(message=message, requested_schema=schema))
            return InputRequiredResult(input_requests={ELICIT_KEY: request}, request_state=state)
        with anyio.move_on_after(timeout_s):
            try:
                result = await self.session.elicit_form(message, schema, related_request_id=self.context.request_id)
            except MCPError as exc:  # it said it could, then could not
                log.info("the client refused a prompt: %s", type(exc).__name__)
                return ClientAnswer(state, "failed")
            return ClientAnswer(state, result.action, result.content)
        return ClientAnswer(state, "timeout")


def _elicitor(context: Any) -> SessionElicitor | None:
    return SessionElicitor(context) if getattr(context, "_request_context", None) is not None else None


# ---- a client that went away ----

CLIENT_GONE_KEY = "agentos.client_gone"  # in an MCP request's scope: an asyncio.Event set once nobody waits for it


def watch_client(context: Any, tool: str) -> asyncio.Task | None:
    """Cancel the current tool call once its HTTP request has no client any more (agentos.mcp.http
    sets CLIENT_GONE_KEY's event on a disconnect or when the request ends), so a call nobody waits
    for does not hold its lane: LaneManagerImpl.using() releases the turn and fails the call. The
    task is marked interrupts.CLIENT_GONE, so call_tool can tell this cancel from any other. None
    without an HTTP request to watch (stdio, a test)."""
    try:
        request = context.request_context.request
    except Exception:  # noqa: BLE001 - no request context (outside a request, an older SDK)
        return None
    gone = getattr(request, "scope", {}).get(CLIENT_GONE_KEY) if request is not None else None
    task = asyncio.current_task()
    if not isinstance(gone, asyncio.Event) or task is None:
        return None

    async def cancel_when_gone() -> None:
        await gone.wait()
        if not task.done() and interrupts.interrupt(task, interrupts.CLIENT_GONE):
            log.warning("the MCP client of a %s call went away; stopping the call", tool)

    return asyncio.ensure_future(cancel_when_gone())


# ---- the server ----

class ToolServer(MCPServer):
    """An MCPServer whose tools come from ToolSpecs. tools/list and tools/call honor the
    per-token `visible` filter (fail closed: a filter that raises shows no tools); tools/list
    also honors the connection's toolsets (`toolsets`: the node's default, None for every tool;
    see agentos.mcp.toolsets)."""

    def __init__(self, *args: Any, visible: VisibleFilter | None = None,
                 toolsets: str | Iterable[str] | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.specs: dict[str, ToolSpec] = {}
        self.arg_names: dict[str, frozenset[str]] = {}  # each tool's parameters, so an unknown key is refused
        self.visible = visible
        self.default_toolsets = None if toolsets is None else ts.parse_toolsets(toolsets)
        self.loaded = ts.Loaded()
        self.redactor = Redactor(None)  # register() puts the node's own in place
        self.providers: list[Provider] = []  # what build_mcp built it from, for agentos.mcp.reload
        self.wrap: Wrap = allow_all_wrap
        self.missing_hint: Callable[[str], str | None] | None = None  # why a known tool is not served here
        # Connections on the handshake protocol (before 2026-07-28) that listed tools: they hear
        # list_changed on their own stream. Newer clients listen through subscriptions/listen.
        self._listers: weakref.WeakSet[Any] = weakref.WeakSet()
        advertise_tools_changed(self)

    async def _handle_list_tools(self, ctx: Any, params: Any) -> Any:
        if getattr(ctx, "protocol_version", None) not in MODERN_PROTOCOL_VERSIONS:
            # the session is a per-request proxy; the connection behind it lives as long as the client
            connection = getattr(getattr(ctx, "session", None), "_connection", None)
            if callable(getattr(connection, "send_tool_list_changed", None)):
                with suppress(TypeError):  # one that cannot be weakly referenced
                    self._listers.add(connection)
        return await super()._handle_list_tools(ctx, params)

    async def tools_changed(self) -> int:
        """Tell connected clients that the tool list changed: subscribers on the 2026-07-28
        protocol through the subscription bus, older sessions that listed tools on their own
        stream. Returns how many older connections were told; a closed one is skipped."""
        await self._subscriptions.publish(ToolsListChanged())
        told = 0
        for connection in list(self._listers):
            try:
                await connection.send_tool_list_changed()
                told += 1
            except Exception:  # noqa: BLE001  (a closed connection; the client lists again when it reconnects)
                self._listers.discard(connection)
        return told

    def visible_specs(self) -> dict[str, ToolSpec] | None:
        """The specs the current request's token may use (with their per-caller view applied);
        None when no filter is configured."""
        if self.visible is None:
            return None
        try:
            return {spec.name: spec for spec in self.visible(current_request_token(), list(self.specs.values()))}
        except Exception:
            log.exception("tool visibility filter failed; showing no tools")
            return {}

    def _has_same_special(self, spec: ToolSpec, names: set[str] | None) -> bool:
        """Does this grant use another memory tool of the same kind (one that writes, for a writer)?
        Then what it lacks for `spec` is the ceiling, not the memory special (finding 34 of 2026-11-10)."""
        if names is None or "memory" not in getattr(spec, "tags", ()):
            return False
        writes = spec.effect != Effect.READ
        return any("memory" in getattr(other, "tags", ()) and (other.effect != Effect.READ) == writes
                   for n in names if n != spec.name and (other := self.specs.get(n)) is not None)

    def visible_names(self) -> set[str] | None:
        """Names the current request's token may use; None when no filter is configured."""
        specs = self.visible_specs()
        return None if specs is None else set(specs)

    def shown_toolsets(self) -> frozenset[str] | None:
        """The toolsets tools/list shows this connection (None: every tool): what its URL asked
        for, else the node's default, plus what it loaded with tools_more in this session."""
        connection = ts.current_connection()
        base = self.default_toolsets if connection.toolsets is None else ts.parse_query(connection.toolsets)
        if base is None:
            return None
        return base | self.loaded.get(current_request_token(), connection.session)

    async def visible_tools(self) -> list[tuple[Any, ToolSpec | None]]:
        """Every tool this token may use, as it sees it (views applied), with its spec; a tool
        added to the SDK without a spec has None. No toolset narrowing."""
        tools = await super().list_tools()
        specs = self.visible_specs()
        out: list[tuple[Any, ToolSpec | None]] = []  # input schemas leaned (lean_schema)
        for tool in tools:
            spec = self.specs.get(tool.name) if specs is None else specs.get(tool.name)
            if spec is None:
                if specs is None:
                    out.append((tool, None))
                continue
            seen = spec.seen
            if specs is None and spec.view is not None:  # no filter: everything the node has
                seen = spec.view(lambda _lane: True)
                if seen is None:
                    continue
            tool = apply_view(tool, seen) if seen is not None else tool
            out.append((tool.model_copy(update={"input_schema": lean_schema(tool.input_schema or {})}), spec))
        return out

    async def list_tools(self) -> list[Any]:
        shown = self.shown_toolsets()
        tools = await self.visible_tools()
        if shown is None:
            return [tool for tool, _ in tools]
        out = [tool for tool, spec in tools if spec is None or ts.toolset_of(spec) in shown]
        return out + ts.meta_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        if name == ts.TOOLS_MORE:  # register() keeps both names free of feature tools
            return await self.tools_more(arguments or {}, context)
        if name == ts.TOOLS_CALL:
            return await self.tools_call(arguments or {}, context)
        names = self.visible_names()
        if name not in self.specs or (names is not None and name not in names):
            hint = self.missing_hint(name) if self.missing_hint is not None and name not in self.specs else None
            if hint:  # a worker's tool while the worker is off: say what it needs, not just not_found
                return error_result(f"unavailable: {hint}")
            shown = name if len(name) <= 64 else name[:61] + "..."
            if name in self.specs:  # served here, but not to this grant: say what it needs
                spec = self.specs[name]
                return error_result(f"not_found: no tool named {shown!r} for your grant. "
                                    f"{grant_hint(spec, self._has_same_special(spec, names))}\n"
                                    "fix: Do not look for another way to do it. Tell the human what the "
                                    "message says, and carry on without it.")
            return error_result(unknown_tool(name, self.specs if names is None else names))
        unknown = unknown_arguments(self.specs[name], self.arg_names.get(name), arguments)
        if unknown is not None:
            return error_result(with_catalog_lines(unknown))
        if self.specs[name].lane_arg is None and "on" in (arguments or {}) and "flow" in self.specs[name].tags:
            arguments = {k: v for k, v in arguments.items() if k != "on"}  # a fixed-lane flow: see unknown_arguments
        reset = elicitor_var.set(_elicitor(context))
        watch = watch_client(context, name)
        try:
            return await super().call_tool(name, arguments, context)
        except asyncio.CancelledError:
            if interrupts.take() != interrupts.CLIENT_GONE:
                raise
            return error_result("engine_error: the MCP client that made this call went away, so it was stopped")
        except ToolError as exc:
            if isinstance(exc.__cause__, ValidationError):
                return error_result(invalid_arguments(exc.__cause__, name))
            raise
        finally:
            if watch is not None:
                watch.cancel()
            elicitor_var.reset(reset)

    async def tools_call(self, arguments: dict[str, Any], context: Any = None) -> Any:
        """The dispatcher: the named tool through call_tool itself, so the visible filter, the
        SDK's argument validation and the policy wrapper run exactly as for a direct call."""
        target = ts.call_target(arguments)
        if isinstance(target, str):
            return error_result(with_catalog_lines(target))
        return await self.call_tool(*target, context)

    async def tools_more(self, arguments: dict[str, Any], context: Any = None) -> CallToolResult:
        """The toolsets this caller has tools in, or one toolset's tools (loaded into the session)."""
        groups: dict[str, list[Any]] = {}
        for tool, spec in await self.visible_tools():
            if spec is not None:
                groups.setdefault(ts.toolset_of(spec), []).append(tool)
        session = ts.current_connection().session
        result, error, changed = ts.more(groups, self.shown_toolsets(), arguments.get("toolset"),
                                         lambda key: self.loaded.add(current_request_token(), session, key))
        if error is not None:
            return error_result(with_catalog_lines(error))
        if changed:
            await tools_changed(context)
        return to_result(result, self.redactor)


def advertise_tools_changed(server: MCPServer) -> None:
    """Advertise tools.listChanged: tools_more sends it when it loads a toolset into a session,
    and agentos.mcp.reload when a tool is added or removed while the node runs."""
    lowlevel = server._lowlevel_server
    initialization_options = lowlevel.create_initialization_options

    def with_tools_changed(notification_options: NotificationOptions | None = None, *args: Any,
                           **kwargs: Any) -> Any:
        return initialization_options(notification_options or NotificationOptions(tools_changed=True),
                                      *args, **kwargs)

    lowlevel.create_initialization_options = with_tools_changed  # type: ignore[method-assign]


async def tools_changed(context: Any) -> None:
    """Tell the client its tool list changed (best effort: a client that does not listen still
    has tools_call). On the request's own stream, which every client reads, for handshake-era
    sessions; on the subscriptions/listen bus for the 2026-07-28 protocol."""
    try:
        request = context.request_context
    except (AttributeError, ValueError):  # no request: a call from inside the node
        return
    try:
        if request.protocol_version in MODERN_PROTOCOL_VERSIONS:
            await context.notify_tools_changed()
        else:
            await request.session.send_notification(ToolListChangedNotification(),
                                                    related_request_id=request.request_id)
    except Exception:  # the result still says how to call the tools
        log.debug("could not send tools/list_changed", exc_info=True)


_NOT_SCHEMAS = frozenset({"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"})
_VALUES = frozenset({"default", "const", "enum", "examples", "required"})
_NULL = {"type": "null"}


def lean_schema(schema: Any) -> Any:
    """An input schema without what costs an agent tokens and tells it nothing: pydantic's
    generated `title` on every property (the name already says it), and on an optional
    argument whose default is null the null branch of its `anyOf` (leaving it out means null).
    Only the listed schema changes; arguments are still validated by the handler's signature,
    so an explicit null is accepted as before."""
    if isinstance(schema, list):
        return [lean_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in _NOT_SCHEMAS and isinstance(value, dict):
            out[key] = {name: lean_schema(sub) for name, sub in value.items()}
        elif key in _VALUES:
            out[key] = value
        else:
            out[key] = lean_schema(value)
    branches = out.get("anyOf")
    if isinstance(branches, list) and _NULL in branches and "default" in out and out["default"] is None:
        others = [b for b in branches if b != _NULL]
        rest = {k: v for k, v in out.items() if k not in ("anyOf", "default")}
        if len(others) == 1 and isinstance(others[0], dict):
            out = {**others[0], **rest}
        elif others:
            out = {"anyOf": others, **rest}
    return out


def apply_view(tool: Any, view: ToolView) -> Any:
    """The SDK tool as one caller sees it: argument choices as enums, its own description."""
    schema = copy.deepcopy(tool.input_schema or {})
    props = schema.get("properties") or {}
    for arg, values in view.choices.items():
        if isinstance(props.get(arg), dict):
            props[arg]["enum"] = list(values)
    update: dict[str, Any] = {"input_schema": schema}
    if view.description:
        update["description"] = view.description
    return tool.model_copy(update=update)


def _sdk_function(spec: ToolSpec, params: list[ToolParam], call: Call, redactor: Redactor) -> Call:
    """What the SDK registers: the derived signature (so it builds the schema and validates the
    arguments), and a body that runs the wrapped call and turns its outcome into a result."""

    async def run(**arguments: Any) -> CallToolResult:
        lane = arguments.get(spec.lane_arg) if spec.lane_arg else None
        # every log line of this call carries its ids (agentos.core.logs); the policy adds the grant
        with logs.log_context(request=logs.new_request_id(), tool=spec.name,
                              lane=lane if isinstance(lane, str) else None):
            try:
                result = await call(**arguments)
            except ApprovalPending as exc:
                return approval_result(spec, exc, redactor)
            except Exception as exc:  # noqa: BLE001  (every failure becomes an error result; logged there)
                return failure_result(spec, exc, redactor)
            return to_result(result, redactor)

    run.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [p.sdk_parameter() for p in params], return_annotation=CallToolResult)
    run.__name__ = run.__qualname__ = spec.name
    return run


def collect(runtime: Runtime, providers: Iterable[Provider]) -> tuple[list[ToolSpec], list[PromptSpec]]:
    """Every provider's tool and prompt specs; duplicate names are refused, hidden ones are left out."""
    tools: list[ToolSpec] = []
    prompts: list[PromptSpec] = []
    seen: set[tuple[str, str]] = set()
    for provider in providers:
        for spec in provider(runtime):
            kind = "prompt" if isinstance(spec, PromptSpec) else "tool"
            if (kind, spec.name) in seen:
                raise ValueError(f"duplicate {kind} name {spec.name!r}")
            seen.add((kind, spec.name))
            if spec.hidden_without and not runtime.has(spec.hidden_without):
                log.info("%s %s hidden: no %s service on this node", kind, spec.name, spec.hidden_without)
                continue
            if isinstance(spec, PromptSpec):
                prompts.append(spec)
            else:
                tools.append(spec)
    return tools, prompts


def collect_specs(runtime: Runtime, providers: Iterable[Provider]) -> list[ToolSpec]:
    """Every provider's tool specs; duplicate names are refused, hidden ones are left out."""
    return collect(runtime, providers)[0]


def register(server: ToolServer, runtime: Runtime, specs: Sequence[ToolSpec], wrap: Wrap = allow_all_wrap) -> None:
    redactor = Redactor(runtime.get("config"), scrub=runtime_scrub(runtime, lane_codes=True))
    server.redactor = redactor
    from agentos.plugins.service import missing_tool

    server.missing_hint = lambda name: missing_tool(runtime, name)
    for spec in specs:
        if spec.name in server.specs:
            raise ValueError(f"duplicate tool name {spec.name!r}")
        if spec.name in ts.META_TOOLS:
            raise ValueError(f"tool name {spec.name!r} is reserved for the toolset meta tools")
        params = tool_params(spec)
        server.add_tool(
            _sdk_function(spec, params, wrap(spec, runtime), redactor),
            name=spec.name,
            description=tool_description(spec),
            annotations=tool_annotations(spec),
            structured_output=False,
        )
        server.specs[spec.name] = spec
        server.arg_names[spec.name] = frozenset(p.name for p in params)


def unregister(server: ToolServer, name: str) -> None:
    """Take a tool away from the server (a call already running finishes)."""
    server.arg_names.pop(name, None)
    if server.specs.pop(name, None) is not None:
        server.remove_tool(name)


def register_prompts(server: ToolServer, runtime: Runtime, prompts: Sequence[PromptSpec]) -> None:
    """Each PromptSpec as an SDK prompt; its text is masked like every tool result."""
    redactor = Redactor(runtime.get("config"), scrub=runtime_scrub(runtime))
    for spec in prompts:
        server.add_prompt(sdk_prompt(spec, redactor))


def sdk_prompt(spec: PromptSpec, redactor: Redactor) -> Prompt:
    async def render(**arguments: Any) -> str:
        given = {k: v for k, v in arguments.items() if isinstance(v, str) and v.strip()}
        return redactor.text(spec.render(**given))

    render.__name__ = spec.name
    return Prompt(name=spec.name, title=spec.title, description=spec.description, fn=render,
                  arguments=[PromptArgument(name=a.name, description=a.description, required=a.required)
                             for a in spec.arguments])


def build_mcp(
    runtime: Runtime,
    providers: Iterable[Provider],
    wrap: Wrap = allow_all_wrap,
    *,
    visible: VisibleFilter | None = None,
    instructions: str | None = None,
    name: str = "agentos",
    toolsets: str | Iterable[str] | None = None,
) -> ToolServer:
    """The MCP server with every provider's tools, each call through `wrap`, and their prompts.
    `toolsets` is what tools/list shows a connection that does not ask (agentos.mcp.toolsets);
    None lists every tool, with no meta tools."""
    server = ToolServer(name, version=__version__, instructions=instructions, log_level="WARNING",
                        visible=visible, toolsets=toolsets)
    server.providers, server.wrap = list(providers), wrap
    tools, prompts = collect(runtime, server.providers)
    register(server, runtime, tools, wrap)
    register_prompts(server, runtime, prompts)
    return server
