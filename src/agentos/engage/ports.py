"""The executor's ports: what it needs from the rest of the node, as small protocols.

The executor (agentos.engage.executor) never imports the runner, the lane manager or a brain. It
talks to four ports, each with a production adapter (agentos.engage.adapters) and a test fake
(agentos.engage.fakes):

    OperationPort   run one connector operation on a surface, and look a run up again later
    TargetPort      the next thing to act on (a post, a profile, a community), or None
    ContentPort     words to publish (a comment, a post): drafts (brief B3) and owner items (B4)
    LanePort        the surface's lane: claim it for a window, release it after

Contracts later briefs build on:

- OperationPort.run never raises for a site's answer: it returns an OpResult. `signal` is set
  for captcha, checkpoint, logged_out, blocked, rate_limited and wrong_account; the executor then
  holds the surface, ends the window and reschedules what is left. `ask=True` means the owner
  must approve this exact operation before it runs (approve mode): the adapter routes it through
  the node's approvals and returns `awaiting_approval` with the run id when it parks.
- ContentPort.take(surface, kind, n) reserves up to `n` items; the executor settles each one it
  took with `settle(surface, content, used=..., run_id=...)`: used=True once a connector took it
  (verified, unverified, waiting for approval, or failed when it may have acted), used=False to
  hand it back (skipped, or failed when the run provably did not act: `OpResult.acted is False`),
  so the provider can offer it again. While it takes, `CONTENT_SESSION` holds the id of the
  session row (the window) it takes for, so a provider can tie a reservation to that window.
- A ContentPort may also have `async atake(surface, kind, n)`, for content that needs a call out
  (the drafter, agentos.engage.drafts). The executor awaits it when it is there; it raises
  ContentUnavailable when the source cannot be reached, and the executor then skips that
  window's text operations that only it could fill (reason `draft_unavailable`). Plain `take`
  never calls out.
- TargetPort.next(surface, kind, program) returns an opaque target string the operation's
  params builder understands (a subreddit, a post URL), or None (the operation is skipped with
  reason `no_target`). It never acts and never waits. A port that reads its candidates from the
  site (agentos.engage.targets.ConnectorTargets) is also a RefillingTargets: when `next` has
  nothing, the executor awaits `refill(surface, kind, program, parent=...)` once, inside the
  window's lane claim (`parent` lets its read runs pass the claim), and asks `next` again. A
  signal met while reading ends the window like an operation's.
- LanePort.claim raises LaneUnavailable (with a short reason) when the lane cannot be had: a
  human has it, it is held, it is busy. The holder key carries a random nonce per window. Connector
  runs started inside a claimed window pass the claim (runs carry `parent = claim_parent(holder)`;
  agentos.node.flow_driver honors it only while that holder holds the claim on the run's lane).
  `renew` extends the claim during a long pause between operations.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agentos.core.signals import ENGAGE_STOP
from agentos.node.claims import CLAIM_PARENT

if TYPE_CHECKING:
    from agentos.engage.models import Program
    from agentos.engage.operations import OpSpec

# Signals that hold the surface and end a window (agentos.node.holds plus the account check).
SIGNALS = ENGAGE_STOP
# OpResult.error of an operation whose idempotency key already names a run the engine did not
# submit: the adapter never adopts it, and the executor records the operation failed
FOREIGN_RUN = "foreign_run"

# The session row id (sessions.id) of the window a ContentPort.take runs for; None outside one.
CONTENT_SESSION: ContextVar[int | None] = ContextVar("engage_content_session", default=None)


def claim_parent(holder: str) -> str:
    """The Run.parent that lets a connector run pass the claim `holder` holds on its lane."""
    return CLAIM_PARENT + holder


class OpStatus(StrEnum):
    VERIFIED = "verified"                    # done, and the readback proved it
    UNVERIFIED = "unverified"                # done, not proved (yet)
    FAILED = "failed"                        # not done (or not known to be done)
    AWAITING_APPROVAL = "awaiting_approval"  # parked until the owner decides
    HELD = "held"                            # the node holds this operation (paced, broken connector)


@dataclass(frozen=True)
class OpResult:
    status: OpStatus
    signal: str | None = None     # one of SIGNALS, else None
    error: str | None = None      # an error code (pace_limited, connector_broken, timeout, ...)
    run_id: str | None = None
    output: dict[str, Any] = field(default_factory=dict)
    # whether the run may have published: False only when it provably did not (it never started,
    # or the runner's proof shows no acted or written step); None when that is not known
    acted: bool | None = None

    @property
    def done(self) -> bool:
        return self.status in (OpStatus.VERIFIED, OpStatus.UNVERIFIED)


class ContentOrigin(StrEnum):
    DRAFT = "draft"   # written by the drafter (brief B3): always waits for the owner in approve mode
    ITEM = "item"     # put in by the owner (brief B4)


@dataclass(frozen=True)
class Content:
    """Words to publish and what they go with. `approved` is True only when the owner already
    approved this exact text (an item the owner added in person); a draft never is. `review`:
    this text always gets an approval card, whatever the program's mode (test text from the
    passthrough draft provider)."""
    id: str
    origin: ContentOrigin
    text: str = ""
    title: str | None = None
    targets: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()
    approved: bool = False
    review: bool = False


class ContentUnavailable(Exception):
    """A content source (the draft provider) cannot be reached now. `reason` is a short code."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


class LaneUnavailable(Exception):
    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


@runtime_checkable
class OperationPort(Protocol):
    async def run(self, surface: str, spec: OpSpec, params: dict[str, Any], *, idempotency_key: str,
                  ask: bool = True, parent: str | None = None) -> OpResult: ...

    async def check(self, run_id: str) -> OpResult | None:
        """The run's outcome now (for an operation that was waiting for approval), or None."""
        ...

    # Optional: `async lookup(idempotency_key, flow_id, surface) -> OpResult | None`, the finished
    # run a key started (Executor.reconcile settles an `attempted` row with it, any kind).
    # Optional: `adopt(surface, *, parent, until) -> list[str]`, the approved runs queued for a
    # session (their slot before `until`) taken into its claim `parent` (Executor.approved_slot).


@runtime_checkable
class TargetPort(Protocol):
    def next(self, surface: str, kind: str, program: Program) -> str | None: ...


@runtime_checkable
class RefillingTargets(Protocol):
    """A TargetPort that can read more candidates (connector read operations) when it has none."""

    def next(self, surface: str, kind: str, program: Program) -> str | None: ...

    async def refill(self, surface: str, kind: str, program: Program, *,
                     parent: str | None = None) -> OpResult | None:
        """Read more candidates for `kind`: the read's result (a signal there stops the window
        like an operation's), or None when there is nothing to read."""
        ...


@runtime_checkable
class ContentPort(Protocol):
    def take(self, surface: str, kind: str, n: int) -> list[Content]: ...

    def settle(self, surface: str, content: Content, *, used: bool, run_id: str | None = None) -> None: ...


@runtime_checkable
class LanePort(Protocol):
    def lane_for(self, surface: str) -> str: ...

    async def claim(self, lane: str, *, holder: str, purpose: str, ttl_s: float) -> None: ...

    async def renew(self, lane: str, *, holder: str, ttl_s: float) -> None:
        """Extend the holder's claim (a long pause between operations). LaneUnavailable when the
        claim is gone and the lane cannot be had again."""
        ...

    async def release(self, lane: str, *, holder: str) -> None: ...
