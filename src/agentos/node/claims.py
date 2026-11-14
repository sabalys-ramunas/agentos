"""Shared lanes: claims, a fair queue, and reads that share.

Several callers may want one lane at once: agents (Claude Code, Codex), the node brain, flows and
connector runs. Every call on a lane passes LaneAccess, one per lane, in LaneManagerImpl.using().

Modes of a waiter:

  read    a tool that cannot disturb anyone: it reads the current page and changes nothing the
          next caller would notice (browser_snapshot, browser_screenshot, browser_tabs,
          browser_dialog, browser_wait, browser_console, browser_network). Reads share the lane
          with each other.
  act     anything that changes the page, the active tab or the lane: navigate, act, upload,
          tab switch, dialog answers, vault login and fill, lane_close. One at a time.
  claim   lane_claim: becomes the lane's claim when it reaches the front of the queue.
  run     a flow or connector run: an implicit claim for the whole run. Other callers' reads
          still pass (they cannot disturb the run); every other act and claim waits.
  system  the node's own work (quarantine, shutdown, the exit IP probe): waits for the calls in
          flight, then goes first, whatever the claim.

The queue is FIFO per lane. A waiter is admitted when every earlier waiter that could run now has
run (so a read never overtakes an act that arrived before it), except that a waiter the claim
keeps out (another caller's act or claim) is skipped and does not hold up those behind it: the
holder's own calls and everybody's reads go on. Humans pass any claim (a take-over ends claims
anyway), but still wait for the call in flight.

A claim is a lease: it renews on every call by its holder, for ttl_s, and expires on its own
when the holder goes quiet (a disconnect). While others wait, renewals stop at max_total_s from
the claim's start (the starvation guard). A claim the holder did not end itself (expired, a human
took over, a freeze, a revoked grant, the lane closed for memory, a restart) leaves a note, and the
holder's next call on the lane fails once with claim_lost, so a multi-step job never carries on
over a page someone else may have changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import secrets
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agentos.core.errors import ClaimLost  # noqa: F401 - raised by the lane manager

log = logging.getLogger("agentos.lanes.claims")

READ, ACT, CLAIM, RUN, SYSTEM = "read", "act", "claim", "run", "system"
# Run.parent of a flow or connector run that acts inside a claim held by the node itself (an
# engagement session, agentos.engage): "claim:<holder key>". The flow driver enters the lane as that
# holder, so the run passes the claim instead of waiting behind it. Only the node sets a run's parent.
CLAIM_PARENT = "claim:"
MODES = (READ, ACT, CLAIM, RUN, SYSTEM)

DEFAULT_TTL_S = 120.0
MIN_TTL_S = 5.0
MAX_TTL_S = 900.0
DEFAULT_MAX_TOTAL_S = 900.0     # the starvation guard: no renewal past this while others wait
DEFAULT_CALL_S = 5.0            # the estimate for a call before any call was timed on the lane
NOTE_KEEP_S = 6 * 3600.0        # a claim_lost note waits this long for its holder's next call
MAX_NOTES = 200                 # per lane
MAX_PURPOSE_CHARS = 120
TICK_S = 1.0                    # waiters look at the claim (expiry, grant) this often


# ---- who ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Holder:
    """Who calls. `key` tells callers apart (an agent session, a flow run); `identity` is the key
    without the session. A claim belongs to the identity (`owner`): an agent's subject, grant and
    thread, so the same agent reconnecting on a new MCP session still holds, renews and releases
    its claim and gets its claim_lost note, while another agent (another grant) never passes it.
    Two sessions on one grant are one agent to the lane; give agents their own grants to keep them
    apart. `label` is what others see: never a token. `check` (optional) returns why the holder may no
    longer hold a claim (its grant was revoked or expired), else None."""
    key: str
    label: str
    kind: str = "agent"
    identity: str = ""
    grant_id: str | None = None
    run_id: str | None = None
    check: Callable[[], str | None] | None = field(default=None, compare=False, hash=False, repr=False)

    @property
    def ref(self) -> str:
        """A short stable id for this caller, so it can tell its own claim and waits apart."""
        return hashlib.sha256(self.key.encode()).hexdigest()[:8]

    @property
    def human(self) -> bool:
        return self.kind == "human"

    @property
    def owner(self) -> str:
        """Whose a claim is: the identity (the agent across its sessions), else the key."""
        return self.identity or self.key

    def same_identity(self, other_identity: str) -> bool:
        return bool(other_identity) and (self.identity or self.key) == other_identity

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"label": self.label, "kind": self.kind, "ref": self.ref}
        if self.run_id:
            out["run_id"] = self.run_id
        return out

    @classmethod
    def anonymous(cls, kind: str = "agent") -> Holder:
        """A caller that did not say who it is: never the holder of anything."""
        key = f"anon:{secrets.token_hex(6)}"
        return cls(key=key, label=kind, kind=kind, identity=key)

    @classmethod
    def for_run(cls, run_id: str, *, kind: str = "flow", flow: str | None = None) -> Holder:
        key = f"run:{run_id}"
        label = f"{kind} run {run_id}" + (f" ({flow})" if flow else "")
        return cls(key=key, label=label, kind=kind, identity=key, run_id=run_id)

    @classmethod
    def for_principal(cls, principal: Any, *, session: str | None = None,
                      check: Callable[[], str | None] | None = None, kind: str | None = None) -> Holder:
        """An MCP caller: its subject, grant and thread, plus the MCP session when known. `kind`
        is the kind the lane's gates see (agentos.core.tools.gate_kind), else the principal's."""
        kind = kind or str(getattr(principal, "kind", None) or "agent")
        subject = str(getattr(principal, "id", None) or kind)
        grant = str(getattr(principal, "grant_id", None) or "")
        thread = str(getattr(principal, "thread", None) or "")
        identity = f"{subject}|{grant}|{thread}"
        key = f"{identity}|{session or ''}"
        return cls(key=key, label=f"{kind} {subject}"[:80], kind=kind, identity=identity,
                   grant_id=grant or None, check=check)


# ---- the claim ------------------------------------------------------------------------

@dataclass
class Claim:
    lease: str
    holder: Holder
    purpose: str
    ttl_s: float
    since: datetime          # wall clock, for people
    started: float           # monotonic
    expires: float           # monotonic; an implicit claim never expires on its own
    implicit: bool = False   # a flow or connector run's claim, for the length of the run
    renewals: int = 0
    capped: bool = False     # renewals stopped at max_total_s because others wait

    def public(self, now: float, max_total_s: float) -> dict[str, Any]:
        out: dict[str, Any] = {
            "holder": self.holder.label, "kind": self.holder.kind, "ref": self.holder.ref,
            "since": self.since.isoformat(), "purpose": self.purpose,
            "held_s": round(now - self.started, 1), "implicit": self.implicit,
        }
        if self.holder.run_id:
            out["run_id"] = self.holder.run_id
        if not self.implicit:
            out["ttl_s"] = self.ttl_s
            out["expires_in_s"] = round(max(0.0, self.expires - now), 1)
            out["renewable_until_s"] = round(max(0.0, self.started + max_total_s - now), 1)
        return out


@dataclass(frozen=True)
class ClaimEnd:
    """Why a claim ended without its holder: shown once, on the holder's next call."""
    reason: str      # expired | max_total | human | paused | frozen | grant | closed | restart | lane_gone
    message: str
    at: float        # monotonic
    lease: str = ""
    identity: str = ""   # set for notes that match any session of the holder (a restart)


@dataclass(eq=False)
class Waiter:
    holder: Holder
    mode: str
    since: float                  # monotonic
    granted: asyncio.Future
    purpose: str = ""
    ttl_s: float = DEFAULT_TTL_S

    @property
    def done(self) -> bool:
        return self.granted.done()


# ---- one lane ---------------------------------------------------------------------------

class LaneAccess:
    """Who is on one lane and who waits. Synchronous: callers await the waiter's future."""

    def __init__(self) -> None:
        self.claim: Claim | None = None
        self.writer: Waiter | None = None
        self.runner: Waiter | None = None
        self.system: Waiter | None = None
        self.readers: set[Waiter] = set()
        self.queue: list[Waiter] = []
        self.notes: dict[str, ClaimEnd] = {}      # holder key (or identity) -> why its claim ended
        self.durations: deque[float] = deque(maxlen=20)  # recent act durations, for the estimate
        self.started: dict[Waiter, float] = {}

    # ---- state ----

    def inside(self) -> list[Waiter]:
        return [w for w in (self.writer, self.runner, self.system) if w is not None] + list(self.readers)

    def busy(self) -> bool:
        return bool(self.inside())

    def inside_except(self, w: Waiter) -> list[Waiter]:
        return [x for x in self.inside() if x is not w]

    def holds(self, holder: Holder) -> bool:
        return self.claim is not None and self.claim.holder.owner == holder.owner

    def holder_inside(self) -> bool:
        if self.claim is None:
            return False
        return any(w.holder.owner == self.claim.holder.owner for w in self.inside())

    def kept_out(self, w: Waiter) -> bool:
        """The claim keeps this waiter out: another caller's act, claim or run."""
        if self.claim is None or w.mode in (READ, SYSTEM) or w.holder.human:
            return False
        return self.claim.holder.owner != w.holder.owner

    def others_waiting(self) -> bool:
        return any(self.kept_out(w) for w in self.queue)

    # ---- admission ----

    def enqueue(self, w: Waiter) -> None:
        if w.mode == SYSTEM:  # the node's own work goes first
            self.queue.insert(sum(1 for q in self.queue if q.mode == SYSTEM), w)
        else:
            self.queue.append(w)

    def dispatch(self, now: float, *, new_lease: Callable[[], str], wall: Callable[[], datetime]) -> list[Waiter]:
        """Admit every waiter that may run now, in order. Returns those admitted."""
        admitted: list[Waiter] = []
        for w in list(self.queue):
            if w.done:  # cancelled or timed out meanwhile
                self.queue.remove(w)
                continue
            if self.kept_out(w):
                continue
            if not self._admit(w, now, new_lease, wall):
                break  # strict FIFO among the waiters that could run
            self.queue.remove(w)
            self.started[w] = now
            w.granted.set_result(True)
            admitted.append(w)
        return admitted

    def _admit(self, w: Waiter, now: float, new_lease: Callable[[], str], wall: Callable[[], datetime]) -> bool:
        no_act = self.writer is None and self.system is None
        if w.mode == READ:
            if not no_act:
                return False
            self.readers.add(w)
            return True
        if w.mode == ACT:
            if not no_act or self.readers or (self.runner is not None and self.runner.holder.key != w.holder.key):
                return False
            self.writer = w
            return True
        if w.mode == SYSTEM:
            if self.busy():
                return False
            self.system = w
            return True
        if w.mode == CLAIM:
            if self.claim is None:
                self.claim = Claim(lease=new_lease(), holder=w.holder, purpose=w.purpose, ttl_s=w.ttl_s,
                                   since=wall(), started=now, expires=now + w.ttl_s)
            return True
        if w.mode == RUN:
            if not no_act or self.runner is not None:
                return False
            if self.claim is None:
                self.claim = Claim(lease=new_lease(), holder=w.holder, purpose=w.purpose, ttl_s=0.0,
                                   since=wall(), started=now, expires=float("inf"), implicit=True)
            self.runner = w
            return True
        raise ValueError(f"unknown mode {w.mode!r}")

    def leave(self, w: Waiter, now: float) -> None:
        """A waiter is done: out of the queue, or out of the lane."""
        if w in self.queue:
            self.queue.remove(w)
        started = self.started.pop(w, None)
        if self.writer is w:
            self.writer = None
            if started is not None:
                self.durations.append(max(0.0, now - started))
        elif self.runner is w:
            self.runner = None
            if self.claim is not None and self.claim.implicit and self.claim.holder.key == w.holder.key:
                self.claim = None
        elif self.system is w:
            self.system = None
        else:
            self.readers.discard(w)

    # ---- the claim ----

    def renew(self, holder: Holder, now: float, max_total_s: float) -> bool:
        """The holder called: extend its lease (up to max_total_s from the start while others
        wait). True when the holder holds the claim."""
        c = self.claim
        if c is None or c.holder.owner != holder.owner:
            return False
        if c.implicit:
            return True
        until = now + c.ttl_s
        if self.others_waiting():
            limit = c.started + max_total_s
            if until > limit:
                until = limit
                c.capped = True
        if until > c.expires:
            c.expires = until
            c.renewals += 1
        return True

    def end(self, reason: str, message: str, now: float, *, note: bool = True,
            identity: bool = False) -> Claim | None:
        """End the claim; unless the holder ended it, leave a note for its next call."""
        c = self.claim
        if c is None:
            return None
        self.claim = None
        if note and not c.implicit:  # for the owner, on whichever session it calls next
            self.notes[c.holder.owner] = ClaimEnd(reason=reason, message=message, at=now, lease=c.lease,
                                                  identity=c.holder.identity if identity else "")
            self._trim_notes(now)
        return c

    def take_note(self, holder: Holder, now: float) -> ClaimEnd | None:
        self._trim_notes(now)
        found = self.notes.pop(holder.key, None)
        if found is None and (holder.identity or holder.key) in self.notes:
            found = self.notes.pop(holder.identity or holder.key)
        return found

    def _trim_notes(self, now: float) -> None:
        for key, n in list(self.notes.items()):
            if now - n.at > NOTE_KEEP_S:
                del self.notes[key]
        while len(self.notes) > MAX_NOTES:
            del self.notes[next(iter(self.notes))]

    # ---- what callers see ----

    def call_estimate(self) -> float:
        return sum(self.durations) / len(self.durations) if self.durations else DEFAULT_CALL_S

    def position(self, w: Waiter) -> int:
        """1 for the next one in line."""
        try:
            return self.queue.index(w) + 1
        except ValueError:
            return 0

    def estimate(self, w: Waiter, now: float) -> float:
        """Seconds until `w` is likely let in: the calls ahead of it, plus what is left of a
        claim that keeps it out (a run: unknown, counted as one call)."""
        per = self.call_estimate()
        ahead = 0
        for q in self.queue:
            if q is w:
                break
            if q.mode != READ:
                ahead += 1
        wait = per * ahead + (per if self.writer is not None or self.runner is not None else 0.0)
        if self.kept_out(w) and self.claim is not None and not self.claim.implicit:
            wait += max(0.0, self.claim.expires - now)
        return round(wait, 1)

    def snapshot(self, now: float, max_total_s: float, *, viewer: Holder | None = None) -> dict[str, Any]:
        """The lane's claim and queue, as lane_queue and the panel show them."""
        claim = self.claim.public(now, max_total_s) if self.claim else None
        if claim is not None and viewer is not None:
            claim["yours"] = self.claim is not None and self.claim.holder.owner == viewer.owner
        waiting = []
        for i, w in enumerate(self.queue, start=1):
            item = {"position": i, "mode": w.mode, **w.holder.public(), "waited_s": round(now - w.since, 1),
                    "estimate_s": self.estimate(w, now), "kept_out_by_claim": self.kept_out(w)}
            if viewer is not None:
                item["yours"] = w.holder.owner == viewer.owner
            waiting.append(item)
        inside = [{"mode": w.mode, **w.holder.public(),
                   "for_s": round(now - self.started.get(w, now), 1)} for w in self.inside()]
        return {"claim": claim, "queue_length": len(self.queue), "queue": waiting, "inside": inside,
                "call_estimate_s": round(self.call_estimate(), 1)}


# ---- after a restart --------------------------------------------------------------------

class ClaimsFile:
    """The explicit claims held right now, on disk, so that after a restart each holder can be
    told on its next call that its claim is gone. Claims themselves are never restored."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def write(self, claims: dict[str, Claim]) -> None:
        rows = [{"lane": lane, "identity": c.holder.identity or c.holder.key, "lease": c.lease,
                 "purpose": c.purpose} for lane, c in claims.items() if not c.implicit]
        try:
            if not rows:
                self.path.unlink(missing_ok=True)
                return
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # $AGENTOS_HOME/run is private
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(rows, f)
            os.replace(tmp, self.path)
        except OSError:
            log.warning("could not save the lanes' claims to %s", self.path, exc_info=True)

    def take(self) -> list[dict[str, str]]:
        """The claims the last run left (and the file removed)."""
        try:
            rows = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            log.warning("could not read %s; no holder will hear that its claim ended", self.path)
            rows = []
        self.path.unlink(missing_ok=True)
        return [r for r in rows if isinstance(r, dict) and r.get("lane") and r.get("identity")]


HOLDER_WORDS = {"claim": "claimed by", "run": "running", "takeover": "taken over by", "paused": "paused by",
                "drive": "driven by", "call": "a call from", "waiting": "waiting:", "view": "open:"}


def holder_text(holder: dict[str, Any]) -> str:
    """One entry of LaneManagerImpl.holders() in words: "claimed by agent claude-code (a job)"."""
    kind = str(holder.get("kind") or "")
    text = f"{HOLDER_WORDS.get(kind, kind)} {holder.get('who', '')}".strip()
    return text + (f" ({holder['purpose']})" if holder.get("purpose") else "")


def clean_purpose(text: str | None) -> str:
    text = " ".join(str(text or "").split())
    text = text.replace("\u2014", ",")  # UI text rule: no em dashes
    return text[:MAX_PURPOSE_CHARS]


def clamp_ttl(ttl_s: float | None) -> float:
    try:
        ttl = float(DEFAULT_TTL_S if ttl_s is None else ttl_s)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_S
    if math.isnan(ttl):
        ttl = DEFAULT_TTL_S
    return max(MIN_TTL_S, min(MAX_TTL_S, ttl))
