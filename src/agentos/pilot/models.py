"""What a pilot run takes and what it gives back. Plain data: no lane, no model, no policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentos.pilot.anchor import Target

from agentos.pilot.trail import trail


class Status(StrEnum):
    """How a run ended (design 5.1). Never a bare "done"."""
    DONE_VERIFIED = "done_verified"          # both done answers agree, and the node read an effect back
    DONE_UNVERIFIED = "done_unverified"      # it looks done, but something does not line up: `reason` says what
    HELD = "held"                            # a captcha, a checkpoint, a block, or a person took the lane
    REFUSED = "refused"                      # policy or a safety rule ended it
    GAVE_UP = "gave_up"                      # a budget ran out, or nothing moved
    NEEDS_HELP = "needs_help"                # it stopped short of a guess: `needs` says what would carry it on
    WAITING_APPROVAL = "waiting_approval"    # not an end: a commit waits for a person, and the run goes on after


@dataclass(frozen=True)
class Value:
    """Something to enter. `text` is what is typed; `label` is all a model ever reads. `secret`: the
    text never reaches a model. `vault`: a vault entry the node fills by name, with no text here."""
    key: str
    label: str
    text: str | None = None
    secret: bool = False
    vault: str | None = None


@dataclass(frozen=True)
class Limits:
    """Design 5.1: 25 steps, 5 minutes, 4 steps without progress."""
    max_steps: int = 25
    wall_s: float = 300.0
    no_progress: int = 4
    max_usd: float = 0.25
    max_escalations: int = 6


@dataclass(frozen=True)
class Request:
    lane: str
    goal: str
    sites: tuple[str, ...] = ()
    values: tuple[Value, ...] = ()
    files: tuple[str, ...] = ()        # inbox file names or ids
    read: str | None = None            # read one answer off the page under this name, and act on nothing
    may_write: bool = False            # no text was supplied, so a step may ask for words
    seed: str = ""                     # option order; the same seed gives the same questions
    limits: Limits = field(default_factory=Limits)
    target: Target | None = None       # the item (post, comment) to act on, as the caller names it
    login: bool = False                # a person asked for a sign-in: a signed-out page is the job, not a stop

    @property
    def must_not_send(self) -> tuple[str, ...]:
        return tuple(v.text for v in self.values if v.secret and v.text)

    @property
    def safe_goal(self) -> str:
        goal = self.goal
        for text in self.must_not_send:
            goal = goal.replace(text, "[hidden]")
        return goal


@dataclass
class Spend:
    """What one job cost, across the run."""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    latency_ms: list[int] = field(default_factory=list)

    def add(self, *, input_tokens: int = 0, output_tokens: int = 0, usd: float = 0.0, latency_ms: int = 0) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.usd += usd
        self.latency_ms.append(latency_ms)

    def public(self) -> dict[str, Any]:
        ms = sorted(self.latency_ms)
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "usd": round(self.usd, 6), "median_ms": ms[len(ms) // 2] if ms else None}


@dataclass
class Escalation:
    """One pick that went up a level: why, to whom, what came back and what it cost. `outcome` is
    picked, abstained, unsure, failed or no_rescue."""
    step: int
    reason: str
    outcome: str
    to: str | None = None
    picked: dict[str, str] = field(default_factory=dict)
    confidence: float | None = None
    usd: float = 0.0
    tokens: int = 0
    latency_ms: int = 0

    def public(self) -> dict[str, Any]:
        return {"step": self.step, "reason": self.reason, "outcome": self.outcome, "to": self.to,
                "picked": self.picked, "confidence": self.confidence, "usd": round(self.usd, 6),
                "tokens": self.tokens, "latency_ms": self.latency_ms}


@dataclass
class Result:
    status: Status
    reason: str | None = None
    needs: str | None = None           # needs_help: what would carry the run on
    message: str | None = None         # for a person: node-written, never page text
    answer: dict[str, str] = field(default_factory=dict)
    approval_id: str | None = None
    approval_summary: str | None = None
    steps: int = 0
    actions: list[dict[str, Any]] = field(default_factory=list)
    readbacks: list[dict[str, Any]] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)
    spend: dict[str, Spend] = field(default_factory=dict)
    effects: int = 0                   # actions that worked and could have changed something
    rows: list[dict[str, Any]] = field(default_factory=list)  # every step as measured, for lanebench

    @property
    def usd(self) -> float:
        return sum(s.usd for s in self.spend.values())

    def public(self) -> dict[str, Any]:
        """What the caller reads. `answer` and `actions` hold page text, so they are named as
        untrusted, which also labels the caller's own context when it reads them."""
        out: dict[str, Any] = {
            "pilot_status": self.status.value, "reason": self.reason, "needs": self.needs, "message": self.message,
            "steps": self.steps, "effects": self.effects, "actions": self.actions, "readbacks": self.readbacks,
            "trail": trail(self.rows),
            "escalations": [e.public() for e in self.escalations],
            "spend": {**{job: s.public() for job, s in self.spend.items()}, "total_usd": round(self.usd, 6)},
            "untrusted_fields": ["actions", "readbacks", "trail"],
        }
        if self.answer:
            out["answer"] = self.answer
            out["untrusted_fields"] = ["answer", "actions", "readbacks", "trail"]
        return out
