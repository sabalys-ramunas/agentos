"""A day plan from the current state: deterministic rules, and one seam where a brain will write.

v0 ranks what the stores already say, in the order a person should take it: what is blocked and
only you can clear, the approval queue, the programs due today on lanes that can run them, then
upkeep. A lane that cannot act gets no due work: a held account is told to wait, never given a
list of things to do. The numbers are the engagement engine's own plans, so this says what is
planned, never what will happen, and whether sessions run at all is the node's `engage_sessions`.

The content seam is `no_content`. See its docstring before wiring a model into it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from agentos.plan.state import BLOCKED, BROKEN, NO_LANE, LaneRow, PlanState

SECTIONS = {
    "blocked": "Blocked",
    "queue": "In the queue",
    "due": "Due today",
    "programs": "Programs to look at",
    "upkeep": "Upkeep",
}
BLOCK_ACTION = {
    "frozen": "Lift the freeze when it is safe: agentos unfreeze",
    "quarantined": "Check why, then hand it back: agentos lane handback",
    "needs_human": "Clear it in the live view, then hand it back: agentos lane handback",
    "paused": "Hand it back when ready: agentos lane handback",
    "human": "Someone holds this lane. Agents wait until it is handed back",
    "backoff": "Nothing to do: it lifts by itself. Do not retry before then",
    NO_LANE: "Bind the surface or fix the program",
}


class DraftItem(BaseModel):
    section: Literal["blocked", "queue", "due", "programs", "upkeep"]
    lane: str | None = None
    text: str
    # A blocked item's readiness key, so a screen can say it in its own words. `text` stays the CLI's sentence.
    readiness: str | None = None


class ContentDraft(BaseModel):
    """Something for a lane to say. Always a draft: no field here can make it publish."""
    surface: str
    kind: str
    text: str
    status: Literal["draft"] = "draft"


class DayDraft(BaseModel):
    day: date
    status: Literal["draft"] = "draft"
    dry_run: bool = True             # engage_sessions is off: the engine plans and reports, and acts on nothing
    items: list[DraftItem] = Field(default_factory=list)
    content: list[ContentDraft] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)


ContentProducer = Callable[[PlanState, list[DraftItem]], list[ContentDraft]]


def no_content(state: PlanState, items: list[DraftItem]) -> list[ContentDraft]:
    """SEAM: a brain task produces the content batch here. v0 writes nothing.

    To wire one in, pass a ContentProducer to `draft_day`: a bounded brain job (`brain_run` with
    `requires: {"tools": false}`, a `max_usd` and a low `model_tier` grant, so it can land on a
    chat-completions brain) per due program, returning ContentDraft objects. `items` already leaves
    out every lane that cannot act, so a producer never writes for a held account.

    What comes back stays a draft. It is shown here and then queued the normal way, as an approval
    on the commit gate that the owner decides. No producer may publish, click or call a lane, and
    nothing in this package can: `agentos plan draft` itself stays read-only, so the queueing
    belongs to a separate, approved step that goes through the gate.
    """
    return []


def _named(program) -> str:
    """A program as a person names it: its plan name, else its id for a row written before names."""
    return program.name or program.id


def _counts(due: dict[str, int]) -> str:
    return ", ".join(f"{n} {kind.replace('_', ' ')}" for kind, n in sorted(due.items()))


def _blocked(row: LaneRow) -> DraftItem:
    reason = f"{row.readiness.replace('_', ' ')}" + (f" ({row.detail})" if row.detail else "")
    return DraftItem(section="blocked", lane=row.lane, text=f"{reason}. {BLOCK_ACTION[row.readiness]}.",
                     readiness=row.readiness)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _rules(state: PlanState) -> list[DraftItem]:
    items: list[DraftItem] = []
    for row in state.lanes:
        blocked = row.readiness in BLOCKED
        if blocked:
            items.append(_blocked(row))
        if row.pending.approvals:
            items.append(DraftItem(section="queue", lane=row.lane,
                                   text=f"{_plural(row.pending.approvals, 'approval')} waiting."))
        if row.pending.runs:
            items.append(DraftItem(section="queue", lane=row.lane,
                                   text=f"{_plural(row.pending.runs, 'run')} queued or in flight."))
        for p in row.programs:
            if p.state != "approved":
                items.append(DraftItem(section="programs", lane=row.lane, text=f"{_named(p)}: {p.phase}."))
            elif blocked:
                continue  # already listed above, and a lane that cannot act gets no work
            elif not p.planned:
                items.append(DraftItem(section="programs", lane=row.lane,
                                       text=f"{_named(p)}: no plan saved for today. The nightly job has not run yet."))
            elif p.quiet:
                items.append(DraftItem(section="due", lane=row.lane, text=f"{_named(p)} ({p.phase}): a quiet day."))
            elif p.due:
                items.append(DraftItem(section="due", lane=row.lane, text=f"{_named(p)} ({p.phase}): {_counts(p.due)}."))
        if row.readiness == BROKEN:
            items.append(DraftItem(section="upkeep", lane=row.lane,
                                   text=f"Connector broken: {', '.join(row.pending.broken)}. "
                                        "Its runs wait until it is repaired."))
        if not blocked and row.readiness != NO_LANE and row.signal_stale:
            seen = "no signal yet" if row.last_signal is None else f"last signal {row.signal_at:%Y-%m-%d}"
            items.append(DraftItem(section="upkeep", lane=row.lane, text=f"Check this lane: {seen}."))
    for workspace, n in sorted(state.unattached_approvals.items()):
        items.append(DraftItem(section="queue", text=f"{workspace}: {_plural(n, 'question')} waiting for an answer."))
    order = list(SECTIONS)
    return sorted(items, key=lambda i: order.index(i.section))  # stable: lanes stay in order within a section


def draft_day(state: PlanState, *, content: ContentProducer = no_content) -> DayDraft:
    items = _rules(state)
    return DayDraft(day=state.today, dry_run=not state.sessions_on, items=items, content=content(state, items),
                    unavailable=state.unavailable)
