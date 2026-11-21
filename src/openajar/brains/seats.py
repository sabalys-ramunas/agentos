"""Subscription seats: a concurrency semaphore plus a rolling-window usage estimate.

One subscription cannot do unlimited concurrent work, and over-driving it risks throttling
or a ban on the owner's real account, so each brain is a limited seat:

- occupancy is the number of its tasks in `starting`/`running` (task rows in SQLite), so it
  is shared by every process on the node and survives restarts; acquiring happens inside an
  IMMEDIATE transaction, so two tasks can never take the last seat at once;
- the provider's rolling window is not published, so we estimate it: usage events are
  summed over `window_hours`, and the window capacity is learned from what had been used
  when a 429 or usage-limit message arrived (or set by the owner, or reported directly by
  Claude's rate_limit_event utilization);
- on a rate limit the brain cools down until the reset time the CLI reported, otherwise
  with exponential backoff (base, 2x, 4x, ... capped); one clean run resets the backoff.
"""

from __future__ import annotations

import time
from typing import Any

from openajar.brains.models import BrainKind, BrainSpec, BrainState, TaskRecord, TaskStatus
from openajar.brains.settings import BrainsSettings
from openajar.brains.store import Store


class SeatManager:
    def __init__(self, store: Store, settings: BrainsSettings, clock=time.time):
        self.store = store
        self.settings = settings
        self.clock = clock

    # ---- occupancy ----
    def in_use(self, name: str) -> int:
        return self.store.active_count(name)

    def try_acquire(self, spec: BrainSpec, task: TaskRecord, attempt_n: int, attempt_dir) -> bool:
        """Atomically move `task` to STARTING on `spec` if a seat is free. Returns False when
        the seats are taken (the caller queues or routes onward)."""
        snapshot = task.model_copy(deep=True)
        try:
            with self.store.tx() as db:
                if self.store.active_count(spec.name, db) >= spec.seats:
                    return False
                self._take(db, spec, task, attempt_n, attempt_dir)
        except _Raced:
            for k, v in snapshot:
                setattr(task, k, v)
            return False
        return True

    def _take(self, db, spec: BrainSpec, task: TaskRecord, attempt_n: int, attempt_dir) -> None:
        task.status = TaskStatus.STARTING
        task.brain = spec.name
        task.attempt = attempt_n
        task.status_reason = f"starting on {spec.name}"
        if not self.store.save_task(task, expect=[TaskStatus.QUEUED], db=db):
            raise _Raced()
        self.store.add_attempt(task.id, attempt_n, spec.name, spec.runner.value, attempt_dir, db=db)

    # ---- window ----
    def window_tokens(self, spec: BrainSpec) -> int:
        if spec.window_hours <= 0:
            return 0
        used, _ = self.store.usage_since(spec.name, self.clock() - spec.window_hours * 3600)
        return used

    def window_capacity(self, spec: BrainSpec) -> float | None:
        if spec.window_tokens:
            return float(spec.window_tokens)
        return self.store.brain_state(spec.name).get("window_capacity")

    def window_pct(self, spec: BrainSpec) -> float | None:
        st = self.store.brain_state(spec.name)
        util = st.get("window_utilization")
        cap = self.window_capacity(spec)
        if cap:
            return round(100.0 * self.window_tokens(spec) / cap, 1)
        if util is not None:
            return round(100.0 * float(util), 1)
        return None

    def window_saturated(self, spec: BrainSpec) -> bool:
        if spec.kind != BrainKind.SUBSCRIPTION:
            return False
        pct = self.window_pct(spec)
        return pct is not None and pct >= 100.0 * self.settings.window_saturation

    def record_usage(self, name: str, task_id: str | None, tokens: int, cost_usd: float) -> None:
        if tokens or cost_usd:
            self.store.add_usage(name, task_id, tokens, cost_usd, at=self.clock())

    def note_utilization(self, name: str, utilization: float | None) -> None:
        if utilization is not None:
            self.store.set_brain_state(name, window_utilization=float(utilization))

    # ---- state ----
    def mark_rate_limited(self, spec: BrainSpec, *, resets_at: float | None, detail: str) -> float:
        now = self.clock()
        st = self.store.brain_state(spec.name)
        level = int(st.get("backoff_level") or 0)
        backoff = min(self.settings.cooldown_base_seconds * (2 ** level), self.settings.cooldown_max_seconds)
        until = resets_at if resets_at and resets_at > now else now + backoff
        until = min(until, now + max(self.settings.cooldown_max_seconds, 7 * 86400))
        fields: dict[str, Any] = {"state": BrainState.COOLING_DOWN.value, "cooldown_until": until,
                                  "backoff_level": level + 1, "last_rate_limit_at": now,
                                  "reason": detail[:300]}
        used = self.window_tokens(spec)
        if spec.kind == BrainKind.SUBSCRIPTION and used > 0 and not spec.window_tokens:
            # learned: this is roughly what the window holds
            prev = st.get("window_capacity")
            fields["window_capacity"] = float(used if prev is None else min(prev * 1.5, max(used, prev * 0.5)))
        self.store.set_brain_state(spec.name, **fields)
        return until

    def mark_success(self, name: str) -> None:
        st = self.store.brain_state(name)
        if st.get("backoff_level") or st.get("state") == BrainState.COOLING_DOWN.value:
            self.store.set_brain_state(name, backoff_level=0, state=None, cooldown_until=None, reason=None)

    def mark_needs_auth(self, name: str, reason: str) -> None:
        self.store.set_brain_state(name, state=BrainState.NEEDS_AUTH.value, reason=reason[:300])

    def clear_auth(self, name: str) -> None:
        st = self.store.brain_state(name)
        if st.get("state") == BrainState.NEEDS_AUTH.value:
            self.store.set_brain_state(name, state=None, reason=None)

    def override(self, name: str) -> tuple[BrainState | None, str | None, float | None]:
        """Persisted state override: (state, reason, cooldown_until), expiring cooldowns."""
        st = self.store.brain_state(name)
        state = st.get("state")
        if state == BrainState.COOLING_DOWN.value:
            until = st.get("cooldown_until") or 0
            if until <= self.clock():
                self.store.set_brain_state(name, state=None, reason=None)
                return None, None, None
            return BrainState.COOLING_DOWN, f"until {_hhmm(until)}", until
        if state == BrainState.NEEDS_AUTH.value:
            return BrainState.NEEDS_AUTH, st.get("reason"), None
        return None, None, None


class _Raced(Exception):
    """The task changed state under us (another process took it)."""


def _hhmm(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(ts))
