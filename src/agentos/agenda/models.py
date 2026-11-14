"""What an agenda entry is: an action agentos can run, when it should run, and where it stands.

An entry is a plan, not a run. It names something the node already knows how to do (the action),
the inputs it runs with, the lane or surface it acts on, and its timing. It moves through one
lifecycle:

    draft -> proposed -> scheduled -> running -> done | failed | skipped
                                               \\-> expired (its window passed before it ran)
    any open state -> cancelled

`proposed` waits for a person. `scheduled` means someone may let it run: a person approved it,
a standing approval covered it, or the routing table lets this action run without asking. The
approval binds to `action_hash`, a hash of the action, its inputs, its target and its timing, so
an edit after approval sends the entry back to `proposed`. Title and notes are not in the hash:
they change what a person reads, never what runs.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ENTRY_ID_RE = re.compile(r"^ag_[0-9a-f]{12}$")
MAX_TITLE = 200
MAX_NOTES = 4000
MAX_HISTORY = 40


class EntryState(StrEnum):
    DRAFT = "draft"            # being written; nothing is asked yet
    PROPOSED = "proposed"      # waiting for a person (an approval in Needs you)
    SCHEDULED = "scheduled"    # may run when its time comes
    RUNNING = "running"        # its run (or its trigger's delivery) is under way
    DONE = "done"              # finished, with the proof its run left
    FAILED = "failed"          # its run failed, or it could not be started
    SKIPPED = "skipped"        # the lane's pace, its waking hours or a hold kept it out of its window
    EXPIRED = "expired"        # its window passed before it ran (the node was down, nobody approved)
    CANCELLED = "cancelled"    # someone called it off

    @property
    def open(self) -> bool:
        return self in OPEN


OPEN = frozenset({EntryState.DRAFT, EntryState.PROPOSED, EntryState.SCHEDULED, EntryState.RUNNING})
FINISHED = frozenset({EntryState.DONE, EntryState.FAILED, EntryState.SKIPPED, EntryState.EXPIRED,
                      EntryState.CANCELLED})
# what an edit of the action or timing is allowed from; a finished entry (not done or cancelled)
# can be planned again, and goes back to draft
EDITABLE = frozenset({EntryState.DRAFT, EntryState.PROPOSED, EntryState.SCHEDULED, EntryState.FAILED,
                      EntryState.SKIPPED, EntryState.EXPIRED})


class ActionKind(StrEnum):
    FLOW = "flow"              # a saved flow on a lane
    CONNECTOR = "connector"    # an installed connector operation on a surface
    ROUTE = "route"            # a trigger (events route) that wakes a flow, a brain or an agent


class Action(BaseModel):
    """What the entry runs. `target` is a flow id (`biz-a.billing.remind`), a connector operation's
    flow id (`connector.instagram.post_photo`) or a trigger id (`rt_...`). `version` pins the flow
    version that was drafted, so the approval covers the steps a person saw. `on` is what the
    caller named (a lane, a surface); `lane` is where it acts, resolved when drafted."""
    kind: ActionKind
    target: str
    version: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    on: str | None = None
    lane: str | None = None
    surface: str | None = None
    effect: str = "read"

    @property
    def acts_on(self) -> str | None:
        """What grants, approvals and pace see: the surface of a connector operation, else the lane."""
        return self.surface or self.lane


class When(BaseModel):
    """`at`: an exact time, with a short grace (not_after) for a node that was busy or down.
    `window`: not before / not after. `asap`: as soon as it is scheduled, optionally not after."""
    kind: Literal["at", "window", "asap"]
    not_before: datetime | None = None
    not_after: datetime | None = None

    def words(self) -> str:
        def t(d: datetime | None) -> str:
            return d.isoformat(timespec="minutes") if d else ""
        if self.kind == "at":
            return f"at {t(self.not_before)}"
        if self.kind == "window":
            if self.not_before and self.not_after:
                return f"between {t(self.not_before)} and {t(self.not_after)}"
            return f"not before {t(self.not_before)}" if self.not_before else f"not after {t(self.not_after)}"
        return "as soon as approved" + (f", not after {t(self.not_after)}" if self.not_after else "")


class Entry(BaseModel):
    id: str
    workspace: str
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    notes: str | None = Field(default=None, max_length=MAX_NOTES)
    action: Action
    when: When
    state: EntryState = EntryState.DRAFT
    reason: str | None = None              # why it is in its state, in words (skipped, failed, waiting)
    # {"via": "person" | "standing" | "auto" | "approved", "id": approval id, "state", "by", "at",
    #  "hash": the action_hash it covers, "standing": the standing approval's id}
    approval: dict[str, Any] | None = None
    approved_hash: str | None = None       # the action_hash a person (or the routing) let run
    run_id: str | None = None
    event_id: str | None = None
    retry_at: datetime | None = None       # the lane's pace said not yet: try again then, inside the window
    proof: dict[str, Any] | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    principal: dict[str, Any] = Field(default_factory=dict)   # who drafted it
    grant_id: str | None = None            # the grant it was drafted under; every fire runs under it
    # when that grant had expired (never when it was revoked) and a person approved it: the one-run grant
    # derived from the approver's authority it ran under {"grant_id", "approver", "hash", "at", "why"}
    authority: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ENTRY_ID_RE.match(v):
            raise ValueError("an agenda entry id looks like ag_<12 hex>")
        return v

    def note(self, at: datetime, what: str, **extra: Any) -> None:
        """Add a line to the entry's history (the last MAX_HISTORY are kept)."""
        self.history.append({"at": at.isoformat(), "what": what[:300], **extra})
        del self.history[:-MAX_HISTORY]


def action_hash(action: Action, when: When) -> str:
    """sha256 over what runs and when: the approval binds to it. Title and notes are left out."""
    body = {"kind": action.kind.value, "target": action.target, "version": action.version,
            "inputs": action.inputs, "on": action.on, "lane": action.lane, "surface": action.surface,
            "effect": action.effect, "when": {"kind": when.kind,
                                              "not_before": when.not_before.isoformat() if when.not_before else None,
                                              "not_after": when.not_after.isoformat() if when.not_after else None}}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
