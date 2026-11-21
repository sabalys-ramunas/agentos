"""Lane control of the lane manager (openajar.node.lanes, "Control" and "Holds" there; docs/API.md,
"Lane control"): who may act on a lane (AGENT, HUMAN or PAUSED), and the holds that keep agents off.

`_check_control` is the gate every non-human caller passes, behind the gates added with add_gate
(the freeze). A control change is a plain synchronous update of `_controls`, persisted, then
announced to listeners (sync ones in the same tick, async ones for up to LISTENER_TIMEOUT, which a
test patches here). report_signal turns a bad signal into a hold through the hold policy
(default_hold_policy: openajar.node.holds.hold_for when that module is in the build; a broken one
holds every bad signal for a human), and a policy that fails holds the lane for a human. A hand-back reads the page first, so a challenge still on
screen is never handed to agents.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from openajar.core.errors import AgentPaused, HumanInControl, InvalidRequest, LaneNotFound
from openajar.core.models import ControlState, LaneControl, LaneInfo, LaneState, Signal
from openajar.core.pauses import paused_words
from openajar.core.signals import QUIET
from openajar.node import interrupts
from openajar.node.lanes.base import (
    HUMAN,
    IDLE_HANDBACK_BY,
    NODE_BY,
    QUARANTINE_HOLD,
    ControlListener,
    Gate,
    HoldPolicy,
    NoticeListener,
    _host,
    _who,
    clean_reason,
)
from openajar.node.store import LaneRecord, SignalRecord

log = logging.getLogger("openajar.lanes")

LISTENER_TIMEOUT = 5.0             # async control listeners get this long before we move on
INPUT_PERSIST_SECONDS = 5.0        # last_human_input is written at most this often per lane
REQUEST_COOLDOWN_SECONDS = 30.0    # after a refusal, an agent must wait this long to ask again
HOLD_EXPIRED_BY = "hold-expired"   # `by` when a self-clearing hold ends
HANDBACK_READ_S = 3.0              # how long a hand-back reads the page for a challenge still on screen
HANDBACK_NOTE_CHARS = 300          # a human's note on hand back
SIGNAL_PERSIST_SECONDS = 60.0      # an unchanged signal is written at most this often


# ---- holds ----------------------------------------------------------------------------

def default_hold_policy() -> HoldPolicy | None:
    """openajar.node.holds.hold_for when that module is part of this build, else None (no holds).
    A module that is there but broken (it fails to import, or has no hold_for) never means no holds:
    every bad signal then holds the lane for a human (hold_for_a_human), as a policy that fails does."""
    try:
        module = importlib.import_module("openajar.node.holds")
    except ModuleNotFoundError as e:
        if e.name in ("openajar.node.holds", "openajar.node"):
            return None
        log.exception("openajar.node.holds needs %s, which is missing; every bad signal holds its lane for a "
                      "human", e.name)
        return hold_for_a_human
    except Exception:
        log.exception("openajar.node.holds failed to import; every bad signal holds its browser for a human")
        return hold_for_a_human
    policy = getattr(module, "hold_for", None)
    if callable(policy):
        return policy
    log.error("openajar.node.holds has no hold_for; every bad signal holds its browser for a human")
    return hold_for_a_human


def hold_for_a_human(signal: Any, *, now: datetime) -> str:
    """The hold policy of a build whose openajar.node.holds is broken: a hold only a human clears."""
    return f"needs_human:{getattr(signal, 'value', signal)}"


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _as_hold(result: Any, now: datetime) -> tuple[str, datetime | None] | None:
    """What a hold policy returned, as (reason, until). Accepts None or False (no hold), a
    reason string, a (reason, until) tuple, a dict or an object with `reason` and `until` (or
    `hold_until`); `until` may be a datetime, a timedelta or seconds from now."""
    if result is None or result is False:
        return None
    if isinstance(result, str):
        reason, until = result, None
    elif isinstance(result, tuple):
        reason, until = (*result, None)[:2]
    elif isinstance(result, dict):
        reason, until = result.get("reason"), result.get("until", result.get("hold_until"))
    else:
        reason = getattr(result, "reason", None)
        until = getattr(result, "until", None)
        if until is None:
            until = getattr(result, "hold_until", None)
    if not reason:
        return None
    if isinstance(until, timedelta):
        until = now + until
    elif isinstance(until, int | float) and not isinstance(until, bool):
        until = now + timedelta(seconds=float(until))
    elif isinstance(until, datetime):
        until = _aware(until)
    else:
        until = None
    return str(getattr(reason, "value", reason)), until


def _merge_hold(old: LaneControl, reason: str, until: datetime | None) -> tuple[str, datetime | None]:
    """A new hold on top of an existing one: one only a human clears stays as it is (its first
    reason is what the human needs to see); self-clearing holds extend to the later end. A
    quarantine's hold and another hold stack as the other one, held for a human."""
    if old.state == ControlState.AGENT or not old.reason:
        return reason, until
    if old.reason.startswith(QUARANTINE_HOLD) != reason.startswith(QUARANTINE_HOLD):
        # The quarantine record shuts the lane by itself, so its hold never hides another one:
        # the other reason stays on the control (what clearing the quarantine leaves), and the
        # pair ends only with a human (a backoff stacked on a quarantine no longer ends on its own).
        return (old.reason if reason.startswith(QUARANTINE_HOLD) else reason), None
    if old.hold_until is None:
        return old.reason, None
    if until is None:
        return reason, None
    return reason, max(old.hold_until, until)


def _is_hold(control: LaneControl) -> bool:
    return bool(control.reason) and control.state in (ControlState.PAUSED, ControlState.HUMAN)


class ControlMixin:
    """Control and holds of the lane manager (openajar.node.lanes.LaneManagerImpl)."""

    def _control(self, lane_id: str) -> LaneControl:
        control = self._controls.get(lane_id)
        if control is None:
            control = self._controls[lane_id] = self.store.require(lane_id).control or LaneControl()
        return control

    def _agents_hold(self, lane_id: str) -> bool:
        return self._control(lane_id).state == ControlState.AGENT

    def _held_by_human(self, lane_id: str) -> bool:
        control = self._controls.get(lane_id)
        return control is not None and control.state == ControlState.HUMAN

    def _check_control(self, lane_id: str, principal_kind: str) -> None:
        """The gate: raise for a non-human caller unless agents have control."""
        if principal_kind == HUMAN:
            return
        self._check_gates(lane_id, principal_kind)
        control = self._control(lane_id)
        if control.state == ControlState.HUMAN:
            since = f" since {control.since:%H:%M} UTC" if control.since else ""
            raise HumanInControl(
                f"a human has control of {lane_id}{since}. Do not retry: wait with browser_wait_handback, "
                "or ask for it back once with browser_request_control.")
        if control.state == ControlState.PAUSED:
            raise AgentPaused(paused_words(lane_id, control))

    def _check_gates(self, lane_id: str, principal_kind: str) -> None:
        if principal_kind == HUMAN:
            return
        for gate in list(self._gates):
            gate(lane_id, principal_kind)

    def _kept_warm(self, lane_id: str) -> bool:
        for keep in list(self._keep_warm):
            try:
                if keep(lane_id):
                    return True
            except Exception:  # a broken check keeps the lane (the safe side for a frozen lane)
                log.exception("keep-warm check failed for %s", lane_id)
                return True
        return False

    def add_gate(self, check: Gate, *, keep_warm: Callable[[str], bool] | None = None) -> None:
        """check(lane_id, principal_kind) runs for every non-human caller before control is read
        (raise to refuse). keep_warm(lane_id) True keeps a lane from being reaped or evicted."""
        if check not in self._gates:
            self._gates.append(check)
        if keep_warm is not None and keep_warm not in self._keep_warm:
            self._keep_warm.append(keep_warm)

    def agent_tasks(self, lane_id: str) -> list[asyncio.Task]:
        """The non-human calls inside using() on this lane right now."""
        return [t for t in self._agent_tasks.get(lane_id, ()) if not t.done()]

    def interrupt_agents(self, lane_id: str, reason: str) -> list[asyncio.Task]:
        """Cancel every non-human call inside using() on this lane at its next await; each one
        raises the gate's error (or AgentPaused). Ends the lane's claim (a freeze ends them all).
        Returns the tasks interrupted (await them to know when they stopped). Human work is never
        touched."""
        lane = self._lanes.get(lane_id)
        if lane is not None:
            self._end_claim(lane_id, lane, "frozen", f"agents were stopped on the browser ({clean_reason(reason)})")
        return [t for t in self.agent_tasks(lane_id) if interrupts.interrupt(t, reason)]

    @staticmethod
    def interrupt_task(task: asyncio.Task, reason: str) -> bool:
        """Interrupt one task (an MCP call outside using()); see openajar.node.interrupts."""
        return interrupts.interrupt(task, reason)

    @staticmethod
    def take_interrupt(task: asyncio.Task | None = None) -> str | None:
        """For the code that owns a call: the interrupt's reason (and the cancel undone), or None."""
        return interrupts.take(task)

    async def _apply(self, lane_id: str, new: LaneControl, *, why: str) -> LaneInfo:
        """Swap in a new control record, persist it and announce it. No await happens
        between reading the old record and writing the new one."""
        old = self._control(lane_id)
        if new == old:
            return self.get(lane_id)
        self._controls[lane_id] = new
        self._input_dirty.discard(lane_id)
        if new.state != ControlState.AGENT:  # a take-over, a pause or a hold overrides any claim
            lane = self._lanes.get(lane_id)
            self._end_drive(lane, f"control is {new.state.value}")
            if lane is not None:
                self._end_claim(lane_id, lane, "human" if new.state == ControlState.HUMAN else "paused",
                                "a human took control of the browser" if new.state == ControlState.HUMAN
                                else f"agents were held off the browser ({new.reason or 'stopped by a human'})")
        # Durable: queued now, so sync listeners still run in this tick, and done before this returns.
        saved = self.store.submit(self.store.set_control, lane_id, new)
        log.info("%s: control %s -> %s (%s)", lane_id, old.state.value, new.state.value, why)
        try:
            await self._announce(lane_id, old, new)
        finally:
            try:
                await saved
            except Exception:  # the change still holds in memory; a restart would lose it
                log.exception("%s: could not save control %s", lane_id, new.state.value)
        return self.get(lane_id)

    async def _announce(self, lane_id: str, old: LaneControl, new: LaneControl) -> None:
        pending = []
        for callback in list(self._listeners):  # sync listeners run in this very tick
            try:
                result = callback(lane_id, old, new)
            except Exception:
                log.exception("control listener %r failed", callback)
                continue
            if inspect.isawaitable(result):
                pending.append(asyncio.ensure_future(result))
        async with self._control_cond:
            self._control_cond.notify_all()
        if not pending:
            return
        done, slow = await asyncio.wait(pending, timeout=LISTENER_TIMEOUT)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                log.error("control listener failed: %s", task.exception(), exc_info=task.exception())
        for task in slow:
            log.warning("%s: a control listener is still running after %gs; leaving it in the background",
                        lane_id, LISTENER_TIMEOUT)
            self._background.add(task)
            task.add_done_callback(self._listener_done)

    def _listener_done(self, task: asyncio.Task) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("control listener failed: %s", task.exception(), exc_info=task.exception())

    def add_control_listener(self, callback: ControlListener) -> None:
        """callback(lane_id, old, new) -> None | Awaitable, on every control change (not on
        each human input event, which only moves last_human_input)."""
        self._listeners.append(callback)

    def remove_control_listener(self, callback: ControlListener) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def add_notice_listener(self, callback: NoticeListener) -> None:
        """callback(lane_id, notice) on each block notice the egress guard sends while a human has
        control (openajar.node.egress.LaneGuard._notice: rate-limited, host and path only)."""
        self._notice_listeners.append(callback)

    def add_commit_listener(self, callback: Callable[[str], Any]) -> None:
        """callback(lane_id) on each publish a person makes by hand while in control of the lane
        (the egress guard matched a commit rule): the policy records it on the lane's posting pace."""
        self._commit_listeners.append(callback)

    def add_effect_listener(self, callback: Callable[[str, str, str | None, str], Any]) -> None:
        """callback(lane_id, kind, rule_id, actor) on each request an effect rule names that the site
        answered, whoever sent it (agent, flow or person): the action ledger counts it."""
        self._effect_listeners.append(callback)

    def _effect(self, lane_id: str, kind: str, rule: str | None, actor: str) -> None:
        for callback in list(self._effect_listeners):
            try:
                callback(lane_id, kind, rule, actor)
            except Exception:
                log.exception("%s: an effect listener failed", lane_id)

    def _human_commit(self, lane_id: str) -> None:
        for callback in list(self._commit_listeners):
            try:
                callback(lane_id)
            except Exception:
                log.exception("%s: a commit listener failed", lane_id)

    def control(self, lane_id: str) -> LaneControl:
        """The lane's current control record (raises LaneNotFound)."""
        self.store.require(lane_id)
        return self._control(lane_id)

    async def take_control(self, lane_id: str, *, by: str) -> LaneInfo:
        """A human takes control. Agent work already inside using() finishes its current step;
        every later agent call raises HumanInControl, including callers waiting for the lane.
        A hold stays on the record: if the human walks away, it comes back (not agents)."""
        self.store.require(lane_id)
        by = _who(by)
        old = self._control(lane_id)
        if old.state == ControlState.HUMAN and old.by == by:
            return self.get(lane_id)
        keep_request = old.state == ControlState.HUMAN  # another human taking over keeps the ask
        held = _is_hold(old)
        new = LaneControl(
            state=ControlState.HUMAN, by=by, since=self.now(),
            takeback_requested=old.takeback_requested and keep_request,
            takeback_reason=old.takeback_reason if keep_request else None,
            takeback_by=old.takeback_by if keep_request else None,
            last_human_input=old.last_human_input,
            reason=old.reason if held else None,
            hold_until=old.hold_until if held else None,
        )
        lane = self._lanes.get(lane_id)
        if lane is not None:
            lane.last_active = self.clock()
        self._denied_at.pop(lane_id, None)
        return await self._apply(lane_id, new, why=f"taken by {by}")

    async def hand_back(self, lane_id: str, *, by: str, note: str | None = None) -> LaneInfo:
        """A human gives agents the lane back: clears a pause, a hold, a quarantine and a
        pending request. (The automatic hand-back after control_idle_seconds is
        reap_idle_control; it never clears a hold.) `note` is what the human tells the agents;
        the hand-back note carries it (openajar.node.handback)."""
        rec = self.store.require(lane_id)
        by = _who(by)
        said = clean_reason(note)[:HANDBACK_NOTE_CHARS] or None if note else None
        if by == IDLE_HANDBACK_BY:
            return await self._idle_hand_back(lane_id)
        if rec.quarantine is not None or self._lane_quarantined(lane_id):
            await self._clear_quarantine(lane_id, by=by, release_hold=False)  # the hand-back clears every hold
        old = self._control(lane_id)
        # the page is read first, even when agents have the lane already: a challenge on screen that no
        # read has seen yet is held now, so the person learns of it here and no agent acts on it blind
        still = await self._challenge_on_screen(lane_id)
        old = self._control(lane_id)  # the read may have taken a moment
        if still is None and old.state == ControlState.AGENT and not old.takeback_requested and not old.reason:
            return self.get(lane_id)
        if _is_hold(old):
            self._event(lane_id, "hold_cleared", {"by": by, "reason": old.reason})
        if still is not None:  # the page still asks a person: agents never get it, not even for a moment
            reason, signal = still
            self._event(lane_id, "held", {"reason": reason, "until": None, "by": NODE_BY, "after": "hand_back"})
            new = LaneControl(state=ControlState.PAUSED, by=NODE_BY, since=self.now(), reason=reason,
                              last_human_input=old.last_human_input)
            return await self._apply(lane_id, new, why=f"handed back by {by}, but the page still shows {signal}")
        new = LaneControl(state=ControlState.AGENT, by=by, since=self.now(),
                          last_human_input=old.last_human_input, handback_note=said)
        self._denied_at.pop(lane_id, None)
        return await self._apply(lane_id, new, why=f"handed back by {by}")

    async def _challenge_on_screen(self, lane_id: str) -> tuple[str, str] | None:
        """(hold reason, signal) when the lane's open tab still shows a page only a person may pass
        (captcha, checkpoint, signed out, blocked), read at the hand-back; None otherwise, and when the
        lane has no browser or the read fails (the next watched read decides then)."""
        reader = getattr(self.handback.interaction, "signal", None)
        lane = self._lanes.get(lane_id)
        if (reader is None or self.hold_policy is None or lane is None or lane.session is None
                or lane.state not in (LaneState.WARM, LaneState.ACTIVE)):
            return None
        from openajar.node.human import quietly
        from openajar.node.tools.outline import active_page

        page = await quietly(lane.session.page(), None, timeout=2.0)
        if page is None:
            return None
        page = active_page(page)
        value = await quietly(reader(page), None, timeout=HANDBACK_READ_S)
        try:
            signal = Signal(getattr(value, "value", value))
        except ValueError:
            return None
        if signal in QUIET:
            return None
        now = self.now()
        try:
            result = self.hold_policy(signal, now=now)
            if inspect.isawaitable(result):
                result = await result
            hold = _as_hold(result, now)
        except Exception:
            log.exception("%s: the hold policy failed on %s at a hand-back", lane_id, signal.value)
            hold = (f"needs_human:{signal.value}", None)
        if hold is None or hold[1] is not None:  # a backoff the person chose to end stays ended
            return None
        rec = self.store.require(lane_id)
        note = clean_reason(_host(getattr(page, "url", None)) or "")[:120] or None
        self._remember_signal(lane_id, rec, signal, now, note)
        return (f"{hold[0]} on {note}" if note else hold[0]), signal.value

    def _lane_quarantined(self, lane_id: str) -> bool:
        lane = self._lanes.get(lane_id)
        return (lane is not None and lane.state == LaneState.QUARANTINED) or lane_id in self._unsaved_quarantines

    async def _idle_hand_back(self, lane_id: str, *, idle_for: float | None = None) -> LaneInfo | None:
        """Give a HUMAN lane back (to agents, or to the hold it had). With idle_for, only when the
        current record still shows no human input for that long; returns None when it does not."""
        old = self._control(lane_id)
        if old.state != ControlState.HUMAN:
            return None if idle_for is not None else self.get(lane_id)
        if idle_for is not None:
            marks = [t for t in (old.since, old.last_human_input) if t is not None]
            if marks and (self.now() - max(marks)).total_seconds() < idle_for:
                return None
        if _is_hold(old):  # the lane was held when the human took it: the hold comes back
            new = LaneControl(state=ControlState.PAUSED, by=IDLE_HANDBACK_BY, since=self.now(), reason=old.reason,
                              hold_until=old.hold_until, last_human_input=old.last_human_input)
            return await self._apply(lane_id, new, why=f"no human input; held again ({old.reason})")
        new = LaneControl(state=ControlState.AGENT, by=IDLE_HANDBACK_BY, since=self.now(),
                          last_human_input=old.last_human_input)
        self._denied_at.pop(lane_id, None)
        return await self._apply(lane_id, new, why="no human input; back with agents")

    async def stop_agent(self, lane_id: str, *, by: str) -> LaneInfo:
        """PAUSED: nobody acts until a human takes control or hands back. Listeners (runner,
        brains) cancel the work bound to this lane. Never returns to agents on its own (a
        self-clearing hold the human stops on top of becomes one only a human clears)."""
        self.store.require(lane_id)
        by = _who(by)
        old = self._control(lane_id)
        if old.state == ControlState.PAUSED and old.hold_until is None:
            return self.get(lane_id)
        new = LaneControl(state=ControlState.PAUSED, by=by, since=self.now(),
                          last_human_input=old.last_human_input, reason=old.reason if _is_hold(old) else None)
        return await self._apply(lane_id, new, why=f"agent stopped by {by}")

    async def hold(self, lane_id: str, *, reason: str, until: datetime | None = None, by: str = NODE_BY) -> LaneInfo:
        """Hold agents off the lane: control PAUSED with `reason`, persisted and announced to
        control listeners. Cleared by a human's hand back, or at `until` for a self-clearing
        hold (a rate limit). A second hold never weakens the first (see _merge_hold). With a
        human in control, the hold is recorded and comes back if they walk away."""
        self.store.require(lane_id)
        text = clean_reason(reason)
        if not text:
            raise InvalidRequest("a hold needs a short reason, for example \"needs_human:captcha\"")
        now = self.now()
        if until is not None:
            until = _aware(until)
            if until <= now:
                return self.get(lane_id)
        old = self._control(lane_id)
        merged, merged_until = _merge_hold(old, text, until)
        if old.state == ControlState.HUMAN:
            new = old.model_copy(update={"reason": merged, "hold_until": merged_until})
        else:
            human_stop = old.state == ControlState.PAUSED and not old.reason  # only a human ends that
            paused = old.state == ControlState.PAUSED
            new = LaneControl(
                state=ControlState.PAUSED,
                by=old.by if human_stop else _who(by),
                since=old.since if paused else now,
                takeback_requested=old.takeback_requested if paused else False,
                takeback_reason=old.takeback_reason if paused else None,
                takeback_by=old.takeback_by if paused else None,
                last_human_input=old.last_human_input,
                reason=merged,
                hold_until=None if human_stop else merged_until,
            )
        if new != old:
            self._event(lane_id, "held", {"reason": merged, "until": merged_until, "by": _who(by)})
        return await self._apply(lane_id, new, why=f"held by {_who(by)}: {merged}")

    async def report_signal(self, lane_id: str, signal: Signal | str, detail: str | None = None) -> LaneInfo:
        """A tool saw `signal` in the lane. Recorded as the lane's last signal; a bad one
        becomes a hold through hold_policy (none when no policy is installed). A policy that
        fails on a bad signal holds the lane for a human rather than letting agents retry."""
        rec = self.store.require(lane_id)
        try:
            value = Signal(getattr(signal, "value", signal))
        except ValueError:
            raise InvalidRequest(f"unknown signal {signal!r}") from None
        now = self.now()
        note = clean_reason(detail)[:120] or None if detail else None
        self._remember_signal(lane_id, rec, value, now, note)
        if value in QUIET or self.hold_policy is None:
            return self.get(lane_id)
        try:
            result = self.hold_policy(value, now=now)
            if inspect.isawaitable(result):
                result = await result
            hold = _as_hold(result, now)
        except Exception:
            log.exception("%s: the hold policy failed on %s; holding the lane for a human", lane_id, value.value)
            hold = (f"needs_human:{value.value}", None)
        if hold is None:
            return self.get(lane_id)
        reason, until = hold
        if note:
            reason = f"{reason} on {note}"
        return await self.hold(lane_id, reason=reason, until=until, by=NODE_BY)

    def _remember_signal(self, lane_id: str, rec: LaneRecord, value: Signal, now: datetime, note: str | None) -> None:
        last = self._signals.get(lane_id)
        stored = rec.signal.signal if rec.signal else None
        fresh = last is not None and last[0] == value and self.clock() - last[1] < SIGNAL_PERSIST_SECONDS
        if fresh and stored == value:
            return
        try:  # informative: queued when another process holds the lock
            self.store.soon(self.store.set_signal, lane_id, SignalRecord(signal=value, at=now, detail=note))
        except Exception:
            log.warning("%s: could not save the last signal", lane_id, exc_info=True)
            return
        self._signals[lane_id] = (value, self.clock())

    async def _expire_hold(self, lane_id: str) -> None:
        """End a self-clearing hold whose time has come."""
        control = self._control(lane_id)
        until = control.hold_until
        if until is None or self.now() < until:
            return
        if control.state == ControlState.PAUSED:
            new = LaneControl(state=ControlState.AGENT, by=HOLD_EXPIRED_BY, since=self.now(),
                              last_human_input=control.last_human_input)
        else:
            new = control.model_copy(update={"reason": None, "hold_until": None})
        self._event(lane_id, "hold_cleared", {"by": HOLD_EXPIRED_BY, "reason": control.reason})
        await self._apply(lane_id, new, why=f"hold ended ({control.reason})")

    async def reap_holds(self) -> list[str]:
        """End every self-clearing hold whose time has come. Returns the lane ids."""
        ended = []
        now = self.now()
        for lane_id, control in list(self._controls.items()):
            if control.hold_until is None or now < control.hold_until:
                continue
            try:
                await self._expire_hold(lane_id)
            except LaneNotFound:
                self._controls.pop(lane_id, None)
                continue
            ended.append(lane_id)
        return ended

    async def request_control(self, lane_id: str, *, by: str, reason: str) -> LaneInfo:
        """An agent asks for control back. A no-op while agents already have control. Asking
        again only updates the reason; listeners fire once per real change."""
        self.store.require(lane_id)
        text = clean_reason(reason)
        if not text:
            raise InvalidRequest("give a short reason the human will see, for example "
                                 "\"the login is done, I need to post the reply\"")
        old = self._control(lane_id)
        if old.state == ControlState.AGENT:
            return self.get(lane_id)
        denied = self._denied_at.get(lane_id)
        if denied is not None and self.clock() - denied < REQUEST_COOLDOWN_SECONDS:
            wait = REQUEST_COOLDOWN_SECONDS - (self.clock() - denied)
            raise HumanInControl(
                f"the human just refused to hand {lane_id} back. Do not ask again for {wait:.0f}s; "
                "wait with browser_wait_handback instead.")
        new = old.model_copy(update={"takeback_requested": True, "takeback_reason": text,
                                     "takeback_by": _who(by)})
        return await self._apply(lane_id, new, why=f"requested by {_who(by)}")

    async def deny_request(self, lane_id: str, *, by: str) -> LaneInfo:
        """The human refuses an agent's request; control stays as it is."""
        self.store.require(lane_id)
        old = self._control(lane_id)
        if not old.takeback_requested:
            return self.get(lane_id)
        new = old.model_copy(update={"takeback_requested": False, "takeback_reason": None,
                                     "takeback_by": None})
        self._denied_at[lane_id] = self.clock()
        return await self._apply(lane_id, new, why=f"request refused by {_who(by)}")

    def note_human_input(self, lane_id: str) -> None:
        """The viewer relay forwarded a human input event: restart the idle hand-back timer
        and keep the lane from being reaped or evicted. Cheap: written to disk at most every
        few seconds."""
        try:
            control = self._control(lane_id)
        except LaneNotFound:
            return
        self._controls[lane_id] = control.model_copy(update={"last_human_input": self.now()})
        lane = self._lanes.get(lane_id)
        if lane is not None:
            lane.last_active = lane.human_at = self.clock()
        self._input_dirty.add(lane_id)
        if self.clock() - self._input_persisted.get(lane_id, float("-inf")) >= INPUT_PERSIST_SECONDS:
            self._persist_input(lane_id)

    def viewer_attached(self, lane_id: str) -> None:
        """A human opened a live view of the lane (a 2FA hunt can outlast idle_seconds): until
        the matching viewer_detached, it is never reaped or evicted (AUD-16)."""
        lane = self._lanes.get(lane_id)
        if lane is not None:
            lane.viewers += 1
            lane.views_opened += 1
            lane.last_active = self.clock()

    def viewer_detached(self, lane_id: str) -> None:
        lane = self._lanes.get(lane_id)
        if lane is not None and lane.viewers > 0:
            lane.viewers -= 1
            lane.last_active = self.clock()

    def view_opened(self, lane_id: str) -> None:
        """Any live view of the lane opened, a view-only link too: until the matching
        view_closed, nothing the node does on its own may show on the lane's screen."""
        lane = self._lanes.get(lane_id)
        if lane is not None:
            lane.watchers += 1
            lane.views_opened += 1

    def view_closed(self, lane_id: str) -> None:
        lane = self._lanes.get(lane_id)
        if lane is not None and lane.watchers > 0:
            lane.watchers -= 1

    def views(self, lane_id: str) -> tuple[int, int]:
        """(live views open now, live views ever opened): a viewer_attached and its view_opened are
        one view, so the larger count of the two is what is open."""
        lane = self._lanes.get(lane_id)
        if lane is None:
            return 0, 0
        return max(lane.viewers, lane.watchers), lane.views_opened

    def driven(self, lane_id: str) -> bool:
        """Is an agent driving the lane now: its egress guard's drive is on (an agent acted through the
        commit gate, and control has not left the agent since). Keepalive never visits such a lane."""
        lane = self._lanes.get(lane_id)
        guard = getattr(getattr(lane, "session", None), "guard", None)
        return bool(guard is not None and guard.driving())

    def _persist_input(self, lane_id: str) -> None:
        self._input_dirty.discard(lane_id)
        self._input_persisted[lane_id] = self.clock()
        control = self._controls.get(lane_id)
        if control is None:
            return
        try:  # only the last human input time changed: queued when another process holds the lock
            self.store.soon(self.store.set_control, lane_id, control)
        except Exception:
            log.warning("%s: could not save the last human input time", lane_id, exc_info=True)

    def _flush_human_input(self) -> None:
        for lane_id in list(self._input_dirty):
            self._persist_input(lane_id)

    async def wait_for_agent_control(self, lane_id: str, timeout: float) -> LaneInfo:
        """Return as soon as agents may act again (a hand back, or the end of a self-clearing
        hold), or after `timeout` seconds with the current state (check info.control.state)."""
        self.store.require(lane_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)

        def agents_may_act() -> bool:
            try:
                return self._control(lane_id).state == ControlState.AGENT
            except LaneNotFound:
                return True  # the lane is gone: stop waiting, get() below raises

        while True:
            try:
                await self._expire_hold(lane_id)
            except LaneNotFound:
                break
            if agents_may_act():
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            until = self._controls.get(lane_id, LaneControl()).hold_until
            if until is not None:
                remaining = min(remaining, max(0.05, (until - self.now()).total_seconds()))
            try:
                async with self._control_cond:
                    await asyncio.wait_for(self._control_cond.wait_for(agents_may_act), remaining)
            except TimeoutError:
                pass
        return self.get(lane_id)

    async def reap_idle_control(self) -> list[str]:
        """Give lanes back after control_idle_seconds without human input. Only HUMAN control
        idles back: to agents, or to the hold that was in place when the human took over. A
        stopped agent (PAUSED) stays stopped."""
        limit = self.config.control_idle_seconds
        if limit <= 0:
            return []
        now = self.now()
        handed = []
        for lane_id, control in list(self._controls.items()):
            if control.state != ControlState.HUMAN:
                continue
            marks = [t for t in (control.since, control.last_human_input) if t is not None]
            if marks and (now - max(marks)).total_seconds() < limit:
                continue
            try:
                # decided again on the current record: an earlier hand-back's listeners may have
                # let a human type or take over in the meantime
                info = await self._idle_hand_back(lane_id, idle_for=limit)
            except LaneNotFound:
                self._controls.pop(lane_id, None)
                continue
            if info is None:
                continue
            log.info("%s: no human input for %ds; control is back with %s", lane_id, limit,
                     "the hold" if _is_hold(control) else "agents")
            handed.append(lane_id)
        return handed
