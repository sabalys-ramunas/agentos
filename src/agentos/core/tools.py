"""How every feature adds MCP tools, framework-agnostic.

A feature package exposes `def tool_specs(runtime) -> list[ToolSpec]`. The MCP layer
(agentos.mcp) registers each spec with the MCP SDK through ONE wrapper that, for every
call: resolves the Principal from the auth token, runs `ctx.authorize(...)` (grants,
effect routing, approvals), executes the handler, and writes a redacted trace. Feature
code never talks to the MCP SDK directly and never skips the wrapper.

Handler shape:

    async def browser_navigate(ctx: ToolContext, *, on: str, url: str) -> dict:
        '''Docstring = the tool description an agent reads.'''
        await ctx.authorize(Effect.READ, lane_id=on)
        ...
        return result.model_dump()

Rules:
- Keyword-only parameters after `ctx`, with type hints; they become the tool's input
  schema. Optional parameters need defaults.
- Return JSON-serializable data (dicts, lists, pydantic `.model_dump()`), or a
  `ToolImage` for an image, or a list mixing text dicts and `ToolImage`s.
- The spec's `effect` is the default effect; a handler may authorize a stricter effect
  for a specific call (for example clicking a "Post" button is COMMIT, not DRAFT).
- Never return secrets. Never put a secret in an error message or a trace field.
- Tool names: lowercase, digits and underscores, at most 64 characters (MCP clients
  reject dots).
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentos.core import globs
from agentos.core.errors import Frozen, InvalidRequest, PolicyDenied
from agentos.core.models import Effect, Principal
from agentos.core.presence import Presence

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class ToolImage:
    data: bytes
    mime_type: str = "image/png"


class ToolContext(Protocol):
    principal: Principal
    runtime: Any  # agentos.core.runtime.Runtime

    async def authorize(
        self,
        effect: Effect,
        *,
        lane_id: str | None = None,
        domain: str | None = None,
        summary: str | None = None,
        content: Any = None,
        cost: Any = None,
        facts: dict | None = None,
    ) -> Any:
        """Raise PolicyDenied, or ApprovalPending (with an approval id) when a human must
        co-sign. `summary` is the node-authored description shown to the human (built from
        what the node will actually do, never from model-written text); `content` is hashed
        so an approval cannot be reused for different content. `cost={tokens, usd}` is
        reserved against the grant's budget; `facts` carries node-observed routing facts
        (preset or isolation, source, ttl_s, ports, provider, tier, credential_owner, metered)."""

    def trace(self, kind: str, **fields: Any) -> None:
        """Append a redacted event to the action log for this call."""

    def taint(self, *labels: str, source: str | None = None) -> None:
        """Label data this call brought into the session ("untrusted", "private", "owner") for
        the lethal-trifecta check. Browser reads should label "untrusted" (page text)."""

    async def charge(self, *, tokens: int = 0, usd: float = 0.0, brain: bool = False,
                     strict: bool = False) -> dict:
        """Record spend after the fact (model tokens, metered dollars)."""

    def can_access_lane(self, lane_id: str) -> bool:
        """Whether the caller's grant covers this lane (for filtering lists)."""


def in_person(ctx: Any) -> bool:
    """Is a person at the node making this call? Then the gates that hold agents off a lane (a
    take over, Stop agent, a freeze, another caller's claim, the grant's egress list) let it
    through. The context says so (`in_person`: a signed-in panel session), or it is a human
    principal on this node's own authority (the owner's grant, run by the node itself). A context
    that carries a presence level (agentos.core.presence: every MCP call has one) is a person only
    at PERSON or above, so the node token on /mcp (NODE_TOKEN) is gated as an agent. A controller's
    member call (its grant marked `member_call` by the policy) never is: a cloud panel member acting
    remotely is gated as an agent."""
    if getattr(ctx, "in_person", False):
        return True
    presence = getattr(ctx, "presence", None)
    if isinstance(presence, Presence):
        return presence >= Presence.PERSON
    if getattr(getattr(ctx, "principal", None), "kind", None) != "human":
        return False
    extra = getattr(getattr(ctx, "grant", None), "extra", None)
    return not (isinstance(extra, dict) and extra.get("member_call"))


def require_lanes(ctx: Any, lanes: Any, effect: Effect) -> None:
    """Refuse unless the caller's grant reaches every lane in `lanes` for `effect`: for a tool that names
    lanes for later work (a brain task, a trigger) and must check them before it asks, saves or charges
    anything. Uses the context's side-effect-free precheck (scope and ceiling) when it has one, else
    `can_access_lane`; a lane id it cannot read is refused."""
    precheck = getattr(ctx, "precheck", None)
    for lane in lanes or ():
        if not isinstance(lane, str) or not lane:
            raise InvalidRequest("a lane is a string like 'workspace/name'")
        if callable(precheck):
            precheck(effect, lane_id=lane)
            continue
        allowed = getattr(ctx, "can_access_lane", None)
        try:
            ok = bool(allowed(lane)) if callable(allowed) else False
        except Exception:  # noqa: BLE001 - a lane the policy cannot parse is outside every scope
            ok = False
        if not ok:
            raise PolicyDenied(f"your grant does not cover lane {lane}")


def _grant_of(ctx: Any) -> Any:
    """The verified grant of a policy context, or None for a context with none (a test's fake)."""
    grant = getattr(ctx, "grant", None)
    return grant if callable(getattr(grant, "covers", None)) else None


def frozen_off(ctx: Any, *, lanes: Any = (), workspace: str | None = None) -> bool:
    """Is this call held off one of `lanes`, or `workspace`, by a freeze (agentos.freeze)? For a tool that
    reaches a workspace without naming its lane: an id whose workspace is its prefix, a definition's
    workspace, a list filtered by workspace. The context notes it, so a listing that leaves the rows
    out says so and a refusal reads `frozen` (agentos.policy.wrap). False in person, and for a context
    with no freeze (a test's fake)."""
    check = getattr(ctx, "frozen_scope", None)
    if not callable(check):
        return False
    held = False
    for lane in ([lanes] if isinstance(lanes, str) else list(lanes or ())):
        if isinstance(lane, str) and lane and check(lane_id=lane) is not None:
            held = True
    if isinstance(workspace, str) and workspace and check(workspace=workspace) is not None:
        held = True
    return held


def refuse_frozen(ctx: Any, *, lanes: Any = (), workspace: str | None = None) -> None:
    """frozen_off for a call that acts on, or returns, one thing: raise `frozen` when it is in a frozen
    scope. The error's words are the freeze service's own (its `message`)."""
    if not frozen_off(ctx, lanes=lanes, workspace=workspace):
        return
    freeze = ctx.runtime.get("freeze")
    raise frozen_error(freeze, ctx.frozen_hits)


def frozen_error(freeze: Any, hits: list[tuple[str, Any]]) -> Exception:
    """The `frozen` error for a call that acted on, or read one thing in, a frozen scope."""
    what, rec = hits[0]
    return Frozen(freeze.message(what or None, rec))


def _pattern_workspace(pattern: str) -> str | None:
    """The one workspace a lane pattern can match in (`biz-a/*`, `biz-a/insta*`), or None (`*`, `*/x`)."""
    ws = pattern.split("/", 1)[0] if "/" in pattern else None
    return ws if ws and not any(c in ws for c in "*?[") else None


def covers_workspace(ctx: Any, workspace: str | None) -> bool:
    """Does the caller's grant reach every lane of `workspace`, the lanes it has now and any made later?
    For what belongs to a workspace rather than to one lane (its workflows, lead lists, suppressions, an
    event with no lane). A pattern or a malformed id is never a workspace. A context without a grant
    asks `can_access_lane` about one lane of the workspace. A workspace frozen for this caller is not
    covered while it is frozen (frozen_off: the call then says `frozen`)."""
    if not isinstance(workspace, str) or not workspace or "*" in workspace or "/" in workspace:
        return False
    grant = _grant_of(ctx)
    if grant is not None:
        return bool(grant.owner_token or grant.covers(None, workspace)) and not frozen_off(ctx, workspace=workspace)
    allowed = getattr(ctx, "can_access_lane", None)
    try:
        return bool(allowed(f"{workspace}/0")) if callable(allowed) else False
    except Exception:  # noqa: BLE001 - an id the policy cannot parse is outside every scope
        return False


def pattern_within(ctx: Any, pattern: str) -> bool:
    """Is every lane `pattern` can match inside the caller's grant? `biz-a/insta*` is within a grant for
    `biz-a/*`; `*` and `biz-b/*` are not. A concrete lane id is a pattern that matches one lane."""
    try:
        globs.validate_lane_pattern(pattern)
    except InvalidRequest:
        return False
    grant = _grant_of(ctx)
    if grant is not None:
        if not grant.owner_token:
            for i, block in enumerate(grant.blocks):
                pats = block.lane_patterns(authority=i == 0)
                if pats is not None and not any(globs.lane_pattern_within(pattern, p) for p in pats):
                    return False
        if "*" not in pattern and "?" not in pattern and "[" not in pattern:
            return not frozen_off(ctx, lanes=[pattern])
        return not frozen_off(ctx, workspace=_pattern_workspace(pattern))
    if "*" in pattern:
        return False  # a context without a grant can only answer for one lane
    allowed = getattr(ctx, "can_access_lane", None)
    try:
        return bool(allowed(pattern)) if callable(allowed) else False
    except Exception:  # noqa: BLE001
        return False


def pattern_reaches(ctx: Any, pattern: str) -> bool:
    """Can `pattern` match at least one lane inside the caller's grant? For what a grant may see because
    it could use it somewhere (a flow bound to `*/instagram`, seen by a grant for `biz-a/*`). A context
    without a grant answers for a concrete lane only."""
    try:
        globs.validate_lane_pattern(pattern)
    except InvalidRequest:
        return False
    if "*" not in pattern:
        return pattern_within(ctx, pattern)
    grant = _grant_of(ctx)
    if grant is None:
        return False
    if frozen_off(ctx, workspace=_pattern_workspace(pattern)):  # it can match only in a frozen workspace
        return False
    if grant.owner_token:
        return True

    def overlap(a: str, b: str) -> bool:
        return globs.segment_within(a, b) or globs.segment_within(b, a)

    pws, pname = ("*", "*") if pattern == "*" else pattern.split("/", 1)
    for i, block in enumerate(grant.blocks):
        pats = block.lane_patterns(authority=i == 0)
        if pats is None:
            continue
        found = False
        for q in pats:
            qws, qname = ("*", "*") if q == "*" else q.split("/", 1)
            if overlap(pws, qws) and overlap(pname, qname):
                found = True
                break
        if not found:
            return False
    return True


def require_lane_patterns(ctx: Any, patterns: Any, effect: Effect) -> None:
    """require_lanes for lane patterns (a trigger's lane, a flow bound to `biz-a/*`): every pattern must
    be within the grant as a pattern (pattern_within), and the grant must hold `effect`. A concrete lane
    is checked as require_lanes checks it."""
    for pattern in patterns or ():
        if not isinstance(pattern, str) or not pattern:
            raise InvalidRequest("a lane is a string like 'workspace/name', or a pattern like 'workspace/*'")
        if "*" not in pattern:
            require_lanes(ctx, [pattern], effect)
            continue
        if not pattern_within(ctx, pattern):
            raise PolicyDenied(f"your grant does not cover every lane {pattern} can match")
        precheck = getattr(ctx, "precheck", None)
        if callable(precheck):
            precheck(effect, lane_id=None)


def gate_kind(ctx: Any) -> str:
    """The principal kind a lane's gates see for this call: "human" only in person (in_person); a
    person acting remotely is "member", which the gates treat as an agent."""
    if in_person(ctx):
        return "human"
    kind = str(getattr(getattr(ctx, "principal", None), "kind", None) or "agent")
    return "member" if kind == "human" else kind


@dataclass(frozen=True)
class ClientAnswer:
    """What a client said to a prompt: `accept`, `decline` or `cancel` (the MCP actions), or the node's own
    `timeout` and `failed`. `state` is the value the prompt was asked with."""
    state: str | None
    action: str
    content: dict | None = None


class Elicitor(Protocol):
    """The MCP client of this call, for a prompt only the client can answer (agentos.policy.confirm_chat).
    Feature code never talks to the SDK; agentos.mcp.registry binds one for each tool call."""
    client: str | None  # what the client calls itself: its own word

    def can_ask(self) -> bool:
        """This client said it can show a prompt, and this call has a way to reach it."""

    def answered(self) -> ClientAnswer | None:
        """The answer this very request carries, on a client that answers in a second round."""

    async def ask(self, message: str, schema: dict, *, state: str, timeout_s: float) -> Any:
        """Ask: a ClientAnswer, or the result to hand back so the client answers in a second round."""


elicitor_var: contextvars.ContextVar[Elicitor | None] = contextvars.ContextVar("agentos_elicitor", default=None)


def current_elicitor() -> Elicitor | None:
    return elicitor_var.get()


Handler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ToolView:
    """What one caller sees of a tool (ToolSpec.view): `choices` narrows an argument to the
    values this caller may use (it becomes that argument's `enum` in tools/list), and
    `description` replaces the tool's description for them. The handler still checks every
    call; a view only keeps a caller from being offered what it cannot use."""
    choices: dict[str, tuple[str, ...]] = field(default_factory=dict)
    description: str | None = None


LaneCheck = Callable[[str], bool]  # does the caller's grant cover this lane (or surface) id?


@dataclass
class ToolSpec:
    name: str
    handler: Handler
    effect: Effect = Effect.READ
    description: str | None = None      # defaults to the handler's docstring
    lane_arg: str | None = "on"         # which argument names the lane, if any
    tags: tuple[str, ...] = field(default_factory=tuple)  # e.g. ("browser",), ("vault",)
    hidden_without: str | None = None   # omit the tool when this runtime service is missing
    input_schema: dict | None = None    # explicit JSON schema (e.g. a saved flow's params); else derived
    # per caller: view(allowed) -> ToolView, or None to hide the tool from that caller (a
    # connector tool whose surfaces the grant cannot see); `seen` is a view already applied
    view: Callable[[LaneCheck], ToolView | None] | None = None
    seen: ToolView | None = None

    def __post_init__(self) -> None:
        if not TOOL_NAME_RE.match(self.name):
            raise ValueError(f"invalid tool name {self.name!r}")
        if self.description is None:
            self.description = (self.handler.__doc__ or "").strip()


@dataclass(frozen=True)
class PromptArg:
    name: str
    description: str
    required: bool = False


@dataclass(frozen=True)
class PromptSpec:
    """An MCP prompt. `render(**arguments)` returns the text of its one user message; arguments
    the user left out (or left empty) are not passed."""
    name: str
    description: str
    render: Callable[..., str]
    arguments: tuple[PromptArg, ...] = ()
    title: str | None = None
    hidden_without: str | None = None


request_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("agentos_mcp_request_token",
                                                                           default=None)


@contextmanager
def bind_request_token(token: str | None) -> Iterator[None]:
    """Bind the caller's bearer token for one request. agentos.mcp.http does this for every HTTP
    request; the SDK carries the context into the tool call it dispatches."""
    reset = request_token.set(token)
    try:
        yield
    finally:
        request_token.reset(reset)


def current_request_token() -> str | None:
    return request_token.get()


TOOL_CATALOG = "tool_catalog"  # the runtime service a ToolCatalog is registered as


class ToolCatalog(Protocol):
    """The tools agents see, for a feature that reads their text (the text review): the composition
    registers one as `tool_catalog` (agentos.mcp.providers.ProviderCatalog)."""

    def specs(self, *, served_only: bool) -> list[ToolSpec]:
        """`served_only`: what this daemon serves now. Otherwise every tool this build has."""

    def params(self, spec: ToolSpec) -> list[Any]:
        """The tool's arguments as the MCP server reads them, each with `.name` and `.annotation`."""
