"""What one flow execution shares: its limits, Outcome, Journal, RunControl, StepFailure, the
per-run state and the classifier that turns a driver's error into a StepFailure.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from openajar.core.driver import (
    AmbiguousLocator,
    DriverError,
    DriverTimeout,
    ElementNotFound,
    FlowDriver,
    ResponseWatch,
)
from openajar.core.errors import (
    AgentPaused,
    ApprovalPending,
    EngineError,
    HumanInControl,
    LaneBusy,
    LaneQuarantined,
    PolicyDenied,
)
from openajar.core.locator import Locator
from openajar.core.models import RunStatus, Signal
from openajar.core.signals import RUN_SIGNAL_CODES
from openajar.core.template import TemplateError
from openajar.runner.flowdoc import FlowDefinition
from openajar.runner.proof import ResolvedCheck
from openajar.runner.records import Run, StepRecord, StepStatus
from openajar.runner.steps import (
    ACTION_TYPES,
    may_publish,
)
from openajar.runner.store import RunnerStore
from openajar.runner.util import Clock, Scrubber, clip

log = logging.getLogger("openajar.runner")

HOLDS = {HumanInControl.code: "human", AgentPaused.code: "paused"}  # a human holds the lane

NEEDS_YOU = {
    Signal.CAPTCHA: "the page shows a captcha. Solve it in the lane's live view (browser_view), then run again",
    Signal.CHECKPOINT: ("the site asks you to confirm it's you. Answer it in the lane's live view (browser_view), "
                        "then run again"),
    Signal.LOGGED_OUT: "the lane is logged out. Log in through the live view (browser_view), then run again",
    Signal.BLOCKED: "the site is blocking this lane. Check it in the live view (browser_view)",
}

SIGNAL_CODES = RUN_SIGNAL_CODES  # the run's error code
# connectors.health.CONTRACT_CAUSES
CONTRACT_CAUSES = frozenset({"element_not_found", "ambiguous_locator", "json_status", "json_invalid", "json_too_large"})
IDENTITY_CODES = {"account": "wrong_account"}  # any other acting_as kind: wrong_surface
# what a URL names before the site resolves it to the account's own id (LinkedIn's /in/me/)
SELF_ALIASES = frozenset({"me"})
MAX_FIELD_CHARS = 20_000
MAX_JSON_INTEGER = 10**18  # what an extract's integer holds
WATCH_POLL_S = 0.1
BODY_WAIT_S = 25.0  # past `timeout_ms`, for a response that came and whose body is being read: the driver stops at 20
READ_KINDS = frozenset({"url", "attribute", "text", "value"})  # a readback whose `found` is what the site said
NOT_READ_RE = re.compile(r"no element at the locator|\d+ matches; ")  # proof.evaluate's own words for such a check
MIN_READ_CHARS = 8  # a saved text shorter than this is hidden from nothing: it would eat ordinary words
TOLD = ("message", "expected", "found", "locator", "locator_text", "check", "check_text", "note", "url")  # of a failure


@dataclass
class Outcome:
    action: Literal["finish", "retry", "defer", "park", "decide", "requeue", "cancel"]
    status: RunStatus | None = None
    proof: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    approval_id: str | None = None
    reason: str | None = None
    recheck: bool = False
    path: str | None = None
    hold: str | None = None  # defer without counting: "human", "paused", "connector_broken", "pace", "lane_pace"
    until: datetime | None = None  # defer until then (a connector's pace)
    hold_reason: str | None = None  # hold the lane for a human (wrong account): the hold's reason
    signal: Signal | None = None  # report this to the lane, as a page's signal is: it rests or holds the lane
    output: dict[str, Any] | None = None  # a connector run's typed result (Run.output)
    paced: dict[str, Any] | None = None  # a defer for the lane's posting pace: rule, next_at, moved_to (AUD-13)


class Journal:
    """The durable step log of one run. Each call is its own committed transaction."""

    def __init__(self, store: RunnerStore, run_id: str, clock: Clock):
        self.store, self.run_id, self.clock = store, run_id, clock

    def load(self) -> dict[str, StepRecord]:
        """The journal, with each step's private resume state (raw url and saved values)
        merged back over its shown result."""
        private = self.store.step_states(self.run_id)
        records = {}
        for r in self.store.steps(self.run_id):
            resume = {k: v for k, v in (private.get(r.path) or {}).items() if k in ("url", "vars")}
            if resume and r.result is not None:
                r.result = {**r.result, **resume}
            records[r.path] = r
        return records

    def start(self, path: str, idx: int, type_: str) -> None:
        self.store.step_start(self.run_id, path, idx, type_, self.clock.now())

    def done(self, path: str, result: dict[str, Any], private: dict[str, Any] | None = None) -> None:
        if private:
            self.store.put_step_state(self.run_id, path, private)
        self.store.step_end(self.run_id, path, StepStatus.DONE, self.clock.now(), result=result)

    def failed(self, path: str, error: dict[str, Any], result: dict[str, Any] | None = None) -> None:
        self.store.step_end(self.run_id, path, StepStatus.FAILED, self.clock.now(), result=result, error=error)

    def skip(self, path: str, result: dict[str, Any] | None = None) -> None:
        self.store.step_set(self.run_id, path, StepStatus.SKIPPED, result=result)

    def reset(self, path: str) -> None:
        self.store.step_set(self.run_id, path, StepStatus.PENDING)

    def cancel_requested(self) -> bool:
        return self.store.cancel_requested(self.run_id)


class StopRun(Exception):
    def __init__(self, why: Literal["cancel", "drain"]):
        super().__init__(why)
        self.why = why


class RunControl:
    def __init__(self, journal: Journal, stopping: Callable[[], bool], cancelled: Callable[[], bool]):
        self.journal, self._stopping, self._cancelled = journal, stopping, cancelled

    def check(self) -> None:
        if self._cancelled() or self.journal.cancel_requested():
            raise StopRun("cancel")
        if self._stopping():
            raise StopRun("drain")


class StepFailure(Exception):
    def __init__(self, code: str, message: str, *, acted: bool | None = False, transient: bool = False,
                 locator: Locator | None = None, expected: Any = None, found: Any = None,
                 approval_id: str | None = None, readback: dict[str, Any] | None = None,
                 hold: str | None = None, signal: Signal | None = None):
        super().__init__(message)
        self.code, self.message, self.acted, self.transient = code, message, acted, transient
        self.locator, self.expected, self.found = locator, expected, found
        self.approval_id, self.readback = approval_id, readback
        self.hold = hold  # the lane hold this failure calls for ("needs_human:wrong_account")
        self.signal = signal  # what the site said, when no page did: the lane's hold policy decides
        self.path = ""
        self.index = 0
        self.step_type = ""
        self.screenshot: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return clip({
            "code": self.code, "message": self.message, "step": self.path, "index": self.index,
            "type": self.step_type,
            "locator": self.locator.model_dump(exclude_none=True) if self.locator else None,
            "locator_text": self.locator.describe() if self.locator else None,
            "expected": self.expected, "found": self.found, "screenshot": self.screenshot,
            "acted": self.acted, "transient": self.transient,
            "repair": ("Fix the step with flow_save (it creates a new version), then run again."
                       if self.code in ("element_not_found", "ambiguous_locator", "readback_failed") else None),
        }, max_str=500)


@dataclass
class ExecState:
    run: Run
    flow: FlowDefinition
    records: dict[str, StepRecord]
    scope: dict[str, Any]
    driver: FlowDriver | None = None
    readbacks: list[dict[str, Any]] = field(default_factory=list)  # as shown (scrubbed)
    writes_done: int = 0  # acted steps flagged writes: true
    acted: int = 0  # acted input steps that can publish (any of them makes a failure partial)
    last_url: str | None = None
    owner_saved: bool = False
    # a learned operation's run (saved_by kind `learned`): its commit scope is strict (FlowCommit.strict)
    strict_scope: bool = False
    scrub: Scrubber = field(default_factory=Scrubber)
    labels: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # save_as -> untrusted/private fields
    truncated: set[str] = field(default_factory=set)  # save_as names whose values were capped
    journal: Journal | None = None
    watches: dict[str, ResponseWatch] = field(default_factory=dict)  # read_json step path -> its open watch
    picked: set[str] = field(default_factory=set)  # read_json step paths whose page a caller's param chose

    @property
    def meta(self) -> dict[str, Any]:
        return self.flow.connector or {}

    @property
    def commit_label(self) -> str | None:
        return (self.meta.get("commit") or {}).get("step")

    def unread(self, failure: dict[str, Any]) -> dict[str, Any]:
        """A failure of a connector run with what the run saved taken out of its text: a template can
        carry a saved value into a URL, a locator or a check, and from there into the failure."""
        saved = {k: _texts(v) for k, v in self.scope["vars"].items()} if self.flow.connector is not None else {}
        reads = Scrubber({k: v for k, v in saved.items() if v})
        return {**failure, **{k: reads(failure[k]) for k in TOLD if failure.get(k) is not None}} if reads else failure

    def publishes(self, step: Any) -> bool:
        """An input step that may publish; a connector's commit step always does."""
        if step.type in ACTION_TYPES and self.commit_label and getattr(step, "label", None) == self.commit_label:
            return True
        return may_publish(step, owner_saved=self.owner_saved)


def classify(e: BaseException, phase: str) -> StepFailure:
    """Turn a driver exception into a structured failure. phase 'find' = nothing done yet."""
    acted: bool | None = False if phase == "find" else None
    if isinstance(e, StepFailure):
        return e
    if isinstance(e, HumanInControl | AgentPaused):
        # the lane's gate raises these before any input of the step, so nothing happened
        return StepFailure(e.code, e.message, acted=False, transient=True)
    if isinstance(e, ElementNotFound):
        return StepFailure("element_not_found", e.message, acted=False, found="no match")
    if isinstance(e, AmbiguousLocator):
        return StepFailure("ambiguous_locator", e.message, acted=False, found=f"{e.count} matches")
    if isinstance(e, ApprovalPending):
        return StepFailure("approval_pending", e.message, acted=False, approval_id=e.approval_id)
    if isinstance(e, PolicyDenied):
        return StepFailure("denied", e.message, acted=False)
    if isinstance(e, DriverTimeout | TimeoutError):
        return StepFailure("timeout", str(e) or "timed out", acted=acted, transient=True)
    if isinstance(e, LaneBusy | LaneQuarantined | EngineError):
        return StepFailure(e.code, e.message, acted=acted, transient=True)
    if isinstance(e, DriverError):
        return StepFailure(e.code, e.message, acted=acted, transient=e.transient)
    if isinstance(e, ConnectionError | OSError):
        return StepFailure("connection", f"{type(e).__name__}: {e}", acted=acted, transient=True)
    if isinstance(e, TemplateError):
        return StepFailure("missing_param", e.message, acted=False)
    return StepFailure("driver_error", f"{type(e).__name__}: {e}", acted=acted)


async def guarded(coro: Any, phase: str, locator: Locator | None = None) -> Any:
    try:
        return await coro
    except StepFailure:
        raise
    except Exception as e:  # every driver failure becomes structured
        f = classify(e, phase)
        f.locator = f.locator or locator
        if f.code in ("element_not_found", "ambiguous_locator") and locator is not None:
            f.expected = f"exactly one {locator.describe()}"
        raise f from e


_MATCH_PARAM = re.compile(r"\$\{([a-z][a-z0-9_]{0,40})\}")


def match_for(match: dict[str, str], params: dict[str, Any]) -> dict[str, str]:
    """A strict run's `match` patterns with each `${param}` replaced by the run's value of it,
    escaped (a learned save: `^${value}$` becomes exactly this run's value). A param the run does
    not have raises: the run does not start rather than send an unpinned save."""
    def sub(m: re.Match[str]) -> str:
        if m.group(1) not in params or params[m.group(1)] is None:
            raise ValueError(f"the commit request pins {m.group(1)}, which this run has no value for")
        return re.escape(str(params[m.group(1)]))

    return {path: _MATCH_PARAM.sub(sub, rx) for path, rx in match.items()}


def learned_flow(fv: Any) -> bool:
    """Was this flow version installed by the node from a learned profile edit (saved_by kind
    `learned`, written only by the node's own installer)? Its runs are strictly scoped."""
    saved = getattr(fv, "saved_by", None)
    return isinstance(saved, dict) and saved.get("kind") == "learned"


def connector_found(check: ResolvedCheck, found: Any, passed: bool | None) -> Any:
    """What a connector's readback says it found: never a value the site gave."""
    if check.condition.kind not in READ_KINDS or found is None or NOT_READ_RE.match(str(found)):
        return found
    return "the value it expected" if passed else "another value"


def _texts(value: Any) -> list[str]:
    """The long enough strings in a saved value."""
    if isinstance(value, str):
        return [value] if len(value) >= MIN_READ_CHARS else []
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts(v)]
    return [t for v in value for t in _texts(v)] if isinstance(value, list) else []


def public_readback(rb: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in rb.items() if not k.startswith("_")}


def check_pattern(pattern: str, what: str) -> None:
    try:
        re.compile(pattern)
    except re.error as e:
        raise StepFailure("invalid_pattern", f"{what} is not a valid regex once params are filled in: {e}",
                          acted=False) from e


def check_patterns(loc: Locator) -> None:
    """A regex (a name or an attr) built from a param is checked before the driver sees it."""
    for pattern, what in loc.regexes():
        check_pattern(pattern, what)


class NoControl:
    def check(self) -> None:
        pass


def norm_text(text: str) -> str:
    return " ".join(text.split())
