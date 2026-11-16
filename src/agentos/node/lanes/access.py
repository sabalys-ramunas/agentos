"""Who is on a lane, for the lane manager (agentos.node.lanes, "Locks" and "Claims" there):
using(), the one way in for a tool call, a flow step or a person, and the FIFO queue and claims
behind it (the state itself is agentos.node.claims.LaneAccess).

using() passes the control gate before it takes a place on the lane and again right before it
yields the page, so a take-over or a freeze that lands while a caller waits wins. A claim is a lease
for a multi-step job; it renews on each of its holder's calls, ends on its own, and its holder hears
claim_lost once when something else ended it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from agentos.core.errors import (
    AgentosError,
    AgentPaused,
    EngineError,
    InvalidRequest,
    LaneBusy,
    LaneQuarantined,
)
from agentos.core.models import LaneState
from agentos.core.progress import abandon_after, notice
from agentos.node import human, interrupts
from agentos.node.claims import (
    ACT,
    CLAIM,
    READ,
    RUN,
    SYSTEM,
    TICK_S,
    Claim,
    ClaimEnd,
    ClaimLost,
    Holder,
    Waiter,
    clamp_ttl,
    clean_purpose,
)
from agentos.node.deadlines import CLOSE_WAIT_S, PROBE_S, WALL_FOR_S, WALL_S

VERDICT_WAIT_S = PROBE_S + 5.0  # a stopped call waits this long for the probe's verdict, never for the close
HINT_WAIT_S = 8.0
from agentos.node.interfaces import Page
from agentos.node.lanes.base import HUMAN, _Call, _Lane, _new_lease

log = logging.getLogger("agentos.lanes")

class AccessMixin:
    """using(), the queue and claims of the lane manager (agentos.node.lanes.LaneManagerImpl)."""

    @asynccontextmanager
    async def using(self, lane_id: str, *, principal_kind: str = "agent", holder: Holder | None = None,
                    mode: str = ACT, purpose: str | None = None, deadline_s: float | None = None,
                    tool: str | None = None) -> AsyncIterator[Page]:
        """Warm the lane if needed, take a place on it, mark it active; touch last_used on exit.
        Non-human callers pass the control gate before taking the lane and again right
        before the page is yielded (nothing awaits between that check and the yield).

        `holder` says who calls (agentos.node.claims.Holder; none: a caller nobody can tell
        apart, which never holds a claim). `mode` is "read" (shares the lane with other reads,
        and passes another caller's claim), "act" (one at a time; waits while another caller
        claims the lane) or "run" (a flow or connector run: an implicit claim for the whole
        session, `purpose` naming it). A wait longer than queue_timeout raises LaneBusy with
        the caller's place in the queue and who holds the lane. A claim of this holder that
        ended without it raises ClaimLost once, before anything is done.

        `deadline_s` (agentos.node.deadlines, for tool calls; `tool` names the call): the call may
        hold the lane this long from the moment the lane is open for it. Then it is interrupted at
        its next await, its turn is released and it fails with engine_error; a browser that does
        not answer a probe then is closed (_deadline_passed)."""
        if mode not in (READ, ACT, RUN):
            raise InvalidRequest(f"unknown lane use {mode!r}")
        rec = self.store.require(lane_id)  # a WAL read on a kept connection: never waits for a writer
        self._ensure_running()
        self._refuse_quarantined(rec)
        await self._expire_hold(lane_id)
        self._check_control(lane_id, principal_kind)
        lane = self._lane(lane_id)
        holder = self._caller(holder, principal_kind)
        self._tell(lane_id, lane, holder)
        lane.users += 1
        task = asyncio.current_task() if principal_kind != HUMAN else None
        if task is not None:
            self._agent_tasks.setdefault(lane_id, set()).add(task)
        waiter: Waiter | None = None
        call: _Call | None = None
        wall = None
        if task is not None and deadline_s is not None and 0 < deadline_s <= WALL_FOR_S:
            wall = asyncio.get_running_loop().call_later(WALL_S, interrupts.interrupt, task, interrupts.DEADLINE)
        try:
            if principal_kind != HUMAN:
                await self._free_own_stuck(lane_id, lane, holder)
            waiter = await self._enter(lane_id, lane, holder, mode, principal_kind, purpose=purpose)
            self._ensure_running()
            self._check_gates(lane_id, principal_kind)  # a freeze while we waited: never warm it
            if lane.state == LaneState.QUARANTINED:
                raise LaneQuarantined(self._quarantine_message(lane_id, lane))
            info = await self._open(rec, principal_kind)
            if info.state == LaneState.QUARANTINED:
                raise LaneQuarantined(self._quarantine_message(lane_id, lane))
            if lane.session is None or info.state not in (LaneState.WARM, LaneState.ACTIVE):
                self._ensure_running()
                raise EngineError(f"{lane_id}: the lane closed while it was opening; try again")
            if task is not None:
                call = self._start_call(lane_id, lane, task, tool or mode, deadline_s, waiter)
            page = await lane.session.page()
            if call is not None:
                call.page = page
            self._check_control(lane_id, principal_kind)  # a take-over while we waited wins
            self._register_page(lane_id, page)
            if principal_kind != HUMAN and mode != READ:
                # a fresh tab's pointer starts at the window's corner: move it in while no site can see it
                await human.place_pointer(page)
            lane.state = LaneState.ACTIVE
            try:
                yield page
            finally:
                if lane.state == LaneState.ACTIVE and not lane.access.inside_except(waiter):
                    lane.state = LaneState.WARM
                lane.last_active = self.clock()
                self.store.soon(self.store.touch, lane_id)  # never waits for a lock another process holds
        except asyncio.CancelledError:
            reason = interrupts.take(task) if task is not None else None
            if reason is None:
                raise
            if reason in interrupts.OWN:
                raise await self._stopped_error(lane_id, call, reason) from None
            self._check_control(lane_id, principal_kind)  # the gate's own error (frozen)
            raise AgentPaused(f"agents were stopped on {lane_id} ({reason}). Do not retry; tell your user.") from None
        except Exception as e:
            if call is not None and call.stopped_by is not None:  # the browser closed under it: say why
                raise await self._stopped_error(lane_id, call, call.stopped_by) from e
            raise
        finally:
            if wall is not None:
                wall.cancel()
            if call is not None:
                self._end_call(lane, call)
            if waiter is not None:
                self._leave(lane_id, lane, waiter)
            if task is not None:
                tasks = self._agent_tasks.get(lane_id)
                if tasks is not None:
                    tasks.discard(task)
                    if not tasks:
                        self._agent_tasks.pop(lane_id, None)
            lane.users -= 1
            await self._notify()

    # ---- deadlines and stuck calls (agentos.node.deadlines) ----------------------------

    def _start_call(self, lane_id: str, lane: _Lane, task: asyncio.Task, tool: str, deadline_s: float | None,
                    waiter: Waiter | None) -> _Call:
        loop = asyncio.get_running_loop()
        call = _Call(task=task, tool=tool, started=loop.time(), deadline_s=deadline_s, waiter=waiter)
        if deadline_s is not None and deadline_s > 0:
            call.timer = loop.call_later(deadline_s, self._deadline_passed, lane_id, lane, call)
        lane.calls.append(call)
        return call

    @staticmethod
    def _end_call(lane: _Lane, call: _Call) -> None:
        if call.timer is not None:
            call.timer.cancel()
        if call in lane.calls:
            lane.calls.remove(call)

    @staticmethod
    def _ran_s(call: _Call) -> float:
        return max(0.0, asyncio.get_running_loop().time() - call.started)

    def _deadline_passed(self, lane_id: str, lane: _Lane, call: _Call) -> None:
        """The loop's timer: the call ran past its deadline. Interrupt it, log it, and probe the page."""
        if call.task.done() or call not in lane.calls:
            return
        call.overdue = True
        ran = self._ran_s(call)
        log.warning("%s: %s ran %.0f s, past its %g s deadline; stopping it and freeing the lane",
                    lane_id, call.tool, ran, call.deadline_s or 0)
        self._event(lane_id, "call_deadline", {"tool": call.tool, "ran_s": round(ran, 1),
                                               "deadline_s": call.deadline_s})
        if interrupts.interrupt(call.task, interrupts.DEADLINE):
            call.stopped_by = call.stopped_by or interrupts.DEADLINE
        call.check = asyncio.ensure_future(self._check_after_deadline(lane_id, lane, call))

    async def _check_after_deadline(self, lane_id: str, lane: _Lane, call: _Call) -> bool:
        """Whether the page still answers; when it does not, close the lane's browser. True when it
        closed it. Then, when the call still has not ended, its place on the lane is taken from it."""
        epoch = lane.epoch
        # first, so the lane is free CLOSE_WAIT_S after the deadline however long a close takes (on an
        # overloaded machine closing its browser took minutes, and every caller waited behind it)
        call.free = asyncio.ensure_future(self._free_stuck(lane_id, lane, [call]))
        closed = False
        if not await self._page_answers(lane, call.page):
            closed = await self._close_unanswering(
                lane_id, lane, f"{call.tool} hung past its {call.deadline_s:g} s deadline and the page did not "
                f"answer within {PROBE_S:g} s", epoch=epoch)
        return closed

    async def _page_answers(self, lane: _Lane, page: Page | None) -> bool:
        """A trivial probe: does the page evaluate `1` within PROBE_S? A page that answers with an error
        (it navigated) answers; only silence, or a browser that is gone, counts as not answering."""
        session = lane.session
        if session is None or not getattr(session, "alive", True):
            return False
        async def probe() -> None:
            target = page if page is not None else await session.page()
            try:
                await target.evaluate("1")
            except Exception:  # noqa: BLE001 - the tab went away: ask the lane's current page
                await (await session.page()).evaluate("1")

        try:
            await abandon_after(probe(), PROBE_S)  # never waits on a probe stuck on a dead connection
            return True
        except TimeoutError:
            return False
        except Exception:  # noqa: BLE001 - it answered, with an error
            return True

    async def _close_unanswering(self, lane_id: str, lane: _Lane, why: str, *, epoch: int | None = None) -> bool:
        """Close the browser of a lane whose page stopped answering (graceful first, then its process
        group is killed: BrowserSession.close), so the lane goes cold and the next call opens it fresh.
        False when it was already closed (or reopened) meanwhile."""
        epoch = lane.epoch if epoch is None else epoch
        async with lane.op:
            if lane.epoch != epoch or lane.session is None:
                return False
            log.warning("%s: closing its browser because %s (the profile is saved where the browser still "
                        "can; the next call opens it again)", lane_id, why)
            await self._teardown(lane_id, lane)
            if lane.claimed():
                self._end_claim(lane_id, lane, "closed", f"{why}, so the lane's browser was closed")
        self._event(lane_id, "browser_unresponsive", {"detail": why, "closed": True})
        await self._notify()
        return True

    async def _free_stuck(self, lane_id: str, lane: _Lane, calls: list[_Call]) -> None:
        """Wait up to CLOSE_WAIT_S for interrupted calls to end; one that still has not (it ignores the
        cancellation) loses its place on the lane, so the next caller gets it."""
        pending = [c.task for c in calls if not c.task.done() and c.task is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=CLOSE_WAIT_S)
        for c in calls:
            if c.task.done() or c not in lane.calls:
                continue
            log.error("%s: %s did not stop %g s after it was interrupted; taking its place on the lane from it",
                      lane_id, c.tool, CLOSE_WAIT_S)
            self._end_call(lane, c)
            if c.waiter is not None:
                self._leave(lane_id, lane, c.waiter)
            if lane.state == LaneState.ACTIVE and not lane.access.busy():
                lane.state = LaneState.WARM
        await self._notify()

    async def _stopped_error(self, lane_id: str, call: _Call | None, reason: str) -> AgentosError:
        """The error a call stopped by the node fails with (interrupts.OWN)."""
        if reason == interrupts.CLIENT_GONE:
            return EngineError(f"the MCP client that made this call went away, so it was stopped and {lane_id} "
                               "is free again. Take a new snapshot before you act on the lane.")
        if reason == interrupts.CLOSED:
            return EngineError(f"{lane_id} was closed while this call was stuck on it: the page stopped answering, "
                               "so the browser was closed; the next call opens it again (logins are kept, the page "
                               "is gone). Navigate to the page again.")
        if call is None and reason == interrupts.DEADLINE:  # the wall: it never got the browser
            hint = await self.why_slow(lane_id)
            text = (f"this call could not get the browser of {lane_id} within {WALL_S:g} s (it was opening, or "
                    "another call held it), so it was stopped. Call it again once; if it fails the same way, tell "
                    "the user.")
            return EngineError(f"{text} {hint}" if hint else text)
        tool = call.tool if call is not None else "the call"
        ran = self._ran_s(call) if call is not None else 0.0
        limit = f"{call.deadline_s:g} s" if call is not None and call.deadline_s else "its"
        closed: bool | None = False
        if call is not None and call.check is not None:
            done, _ = await asyncio.wait({call.check}, timeout=VERDICT_WAIT_S)  # never the whole close
            try:
                closed = bool(call.check.result()) if done else None
            except Exception:
                log.warning("%s: the check after a deadline failed", lane_id, exc_info=True)
        text = f"{tool} on {lane_id} ran {ran:.0f} s, past its {limit} limit, so it was stopped and the lane is free again. "
        if closed is None:
            text += ("The page did not answer, so agentos is closing the browser; the next call opens it again "
                     "(logins are kept, the page is gone). Navigate to the page again.")
        elif closed:
            text += ("The page stopped answering, so the browser was closed; the next call opens it again (logins "
                     "are kept, the page is gone). Navigate to the page again.")
        else:
            text += ("The page still answers but was too slow. Check with browser_snapshot what the call did before "
                     "you call it again.")
        hint = await self.why_slow(lane_id)
        return EngineError(f"{text} {hint}" if hint else text)

    async def why_slow(self, lane_id: str) -> str | None:
        """What a lane's `slow_hints` (callbacks other packages add: an environment's machine load) say
        about a slow or stuck call, within HINT_WAIT_S; None when none says anything."""
        for hint in list(getattr(self, "slow_hints", ()) or ()):
            try:
                said = await abandon_after(hint(lane_id), HINT_WAIT_S)
            except Exception:
                log.debug("%s: a slow-call hint failed", lane_id, exc_info=True)
                continue
            if said:
                return str(said)
        return None

    def _own_calls(self, lane: _Lane, holder: Holder) -> list[_Call]:
        """The calls on the lane by the same agent (the holder's identity: its grant and thread, across
        its MCP sessions) that are still running."""
        return [c for c in lane.calls if not c.task.done() and c.waiter is not None
                and getattr(c.waiter, "holder", None) is not None and c.waiter.holder.owner == holder.owner
                and not c.waiter.holder.human]

    async def _free_own_stuck(self, lane_id: str, lane: _Lane, holder: Holder) -> None:
        """A call of this same agent holds the lane past its deadline (stuck: it ignored its interrupt):
        stop it and close the browser now, rather than queue behind it, and say so in this call's result."""
        stuck = [c for c in self._own_calls(lane, holder) if c.overdue]
        if not stuck:
            return
        c = stuck[0]
        since = self.now() - timedelta(seconds=self._ran_s(c))
        why = f"your own {c.tool} from {since:%H:%M} UTC was stuck ({self._ran_s(c):.0f} s)"
        await self.close_stuck(lane_id, stuck, why)
        notice(f"{why}, so agentos stopped it and closed the browser. This call opened it fresh: the page from "
               "before is gone, so navigate first.")

    def _own_running(self, lane: _Lane, holder: Holder) -> str | None:
        """For a busy error: the same agent's own call that holds the lane, in words."""
        mine = self._own_calls(lane, holder)
        if not mine:
            return None
        c = mine[0]
        since = self.now() - timedelta(seconds=self._ran_s(c))
        end = (f"; agentos stops it at its {c.deadline_s:g} s limit, then the lane is free"
               if c.deadline_s and not c.overdue else "")
        return f"your own {c.tool} from {since:%H:%M:%S} UTC is still running ({self._ran_s(c):.0f} s){end}"

    async def stuck_calls(self, lane_id: str) -> list[_Call]:
        """The calls holding the lane that will not end by themselves: every call past its deadline, and,
        when the page does not answer a probe, every agent call on it. None while a person is on the lane."""
        lane = self._lanes.get(lane_id)
        if lane is None:
            return []
        calls = [c for c in lane.calls if not c.task.done()]
        if not calls or any(w.holder.human for w in lane.access.inside()):
            return []
        overdue = [c for c in calls if c.overdue]
        if overdue:
            return overdue
        return [] if await self._page_answers(lane, calls[0].page) else calls

    async def close_stuck(self, lane_id: str, calls: list[_Call], why: str) -> None:
        """Stop stuck calls and close the lane's browser (lane_close, the memory watchdog)."""
        lane = self._lane(lane_id)
        for c in calls:
            interrupts.interrupt(c.task, interrupts.CLOSED)  # a call already interrupted keeps its first reason
            c.stopped_by = c.stopped_by or interrupts.CLOSED
        log.warning("%s: %s; stopping %s and closing the browser", lane_id, why,
                    ", ".join(f"{c.tool} ({self._ran_s(c):.0f} s)" for c in calls))
        if lane.session is not None:
            await self._close_unanswering(lane_id, lane, why)
        await self._free_stuck(lane_id, lane, calls)

    # ---- the queue and claims (agentos.node.claims) ---------------------------------

    @staticmethod
    def _caller(holder: Holder | None, principal_kind: str) -> Holder:
        if principal_kind == HUMAN:
            if holder is None:
                return Holder.anonymous(HUMAN)
            return Holder(key=holder.key, label=holder.label, kind=HUMAN, identity=holder.identity)
        return holder if holder is not None else Holder.anonymous(principal_kind)

    def _tell(self, lane_id: str, lane: _Lane, holder: Holder) -> None:
        """A claim of this holder ended without it: say so, once, before this call does anything."""
        self._expire_claim(lane_id, lane)
        note = lane.access.take_note(holder, self.clock())
        if note is not None:
            raise ClaimLost(self._lost_message(lane_id, note))

    @staticmethod
    def _lost_message(lane_id: str, note: ClaimEnd) -> str:
        return (f"your claim on {lane_id} ended ({note.message}), so this call did nothing. Someone else may "
                "have changed the page: take a new snapshot, and claim the lane again with lane_claim if the "
                "job still needs it to itself.")

    async def _enter(self, lane_id: str, lane: _Lane, holder: Holder, mode: str, principal_kind: str, *,
                     timeout: float | None = None, purpose: str | None = None, ttl_s: float = 0.0,
                     quiet: bool = False) -> Waiter:
        """Wait for a place on the lane. LaneBusy after `timeout` (default queue_timeout); with
        quiet=True a TimeoutError instead (the node's own work, which goes on without it)."""
        loop = asyncio.get_running_loop()
        access = lane.access
        w = Waiter(holder=holder, mode=mode, since=self.clock(), granted=loop.create_future(),
                   purpose=clean_purpose(purpose), ttl_s=ttl_s)
        access.enqueue(w)
        started = loop.time()
        deadline = started + (self.queue_timeout if timeout is None else max(0.0, timeout))
        try:
            self._dispatch(lane_id, lane)
            while not w.done:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    if quiet:
                        raise TimeoutError
                    raise self._busy(lane_id, lane, w, deadline - started)
                await asyncio.wait([w.granted], timeout=min(remaining, TICK_S))
                if w.done:
                    break
                self._expire_claim(lane_id, lane)
                self._check_control(lane_id, principal_kind)  # a take-over or a freeze while we wait
        except BaseException:
            access.leave(w, self.clock())
            self._dispatch(lane_id, lane)
            raise
        if access.renew(holder, self.clock(), self.claim_max_total_s) and mode == RUN:
            self._save_claims()
            self._event(lane_id, "claimed", {"holder": holder.label, "purpose": w.purpose, "implicit": True})
        return w

    async def _enter_system(self, lane_id: str, lane: _Lane, timeout: float) -> Waiter | None:
        """The node's own work (quarantine, shutdown, the exit IP probe): first in the queue, after
        the calls in flight. None when they did not finish within `timeout`."""
        try:
            return await self._enter(lane_id, lane, Holder(key="node", label="the node", kind="node"), SYSTEM,
                                     HUMAN, timeout=timeout, quiet=True)
        except TimeoutError:
            return None

    def _leave(self, lane_id: str, lane: _Lane, w: Waiter) -> None:
        access = lane.access
        implicit = access.claim is not None and access.claim.implicit and access.runner is w
        now = self.clock()
        access.renew(w.holder, now, self.claim_max_total_s)  # every call renews, at its end too
        access.leave(w, now)
        if implicit:
            self._save_claims()
            self._event(lane_id, "claim_released", {"holder": w.holder.label, "implicit": True})
        self._dispatch(lane_id, lane)

    def _dispatch(self, lane_id: str, lane: _Lane) -> None:
        before = lane.access.claim
        lane.access.dispatch(self.clock(), new_lease=_new_lease, wall=self.now)
        after = lane.access.claim
        if after is not None and after is not before and not after.implicit:
            self._save_claims()

    def _busy(self, lane_id: str, lane: _Lane, w: Waiter, waited: float) -> LaneBusy:
        access = lane.access
        now = self.clock()
        position, estimate = access.position(w), access.estimate(w, now)
        claim = access.claim
        inside = [x for x in access.inside() if x is not w]
        if claim is not None and access.kept_out(w):
            who: dict[str, Any] = {**claim.holder.public(), "since": claim.since.isoformat(), "claimed": True,
                                   "purpose": claim.purpose}
            text = f"{claim.holder.label} has claimed it since {claim.since:%H:%M:%S} UTC"
            if claim.purpose:
                text += f" ({claim.purpose})"
        elif inside:
            first = inside[0]
            since = self.now() - timedelta(seconds=max(0.0, now - access.started.get(first, now)))
            who = {**first.holder.public(), "since": since.isoformat(), "claimed": False}
            own = self._own_running(lane, w.holder) if first.holder.owner == w.holder.owner else None
            who["yours"] = own is not None
            text = own or f"{first.holder.label} is using it since {since:%H:%M:%S} UTC"
        else:
            who, text = {}, "other callers are ahead of you"
        if who.get("yours"):
            e = LaneBusy(f"lane {lane_id} is busy: {text}. You waited {waited:.0f} s. Call again in a minute: if it "
                         "is stuck by then, agentos stops it and closes the browser for you.")
        else:
            e = LaneBusy(f"lane {lane_id} is busy: {text}. You were number {position} in its queue, about "
                         f"{estimate:g} s from your turn, after {waited:.0f} s of waiting. Call again later; "
                         f"lane_queue(on=\"{lane_id}\") shows the queue.")
        e.details = {"lane": lane_id, "position": position, "estimate_s": estimate, "waited_s": round(waited, 1),
                     "queue_length": len(access.queue), "holder": who}
        return e

    def _expire_claim(self, lane_id: str, lane: _Lane) -> None:
        """End the claim whose lease ran out, whose holder's grant ended, or whose holder the gate
        now refuses (a freeze). Never while the holder is inside a call."""
        access = lane.access
        claim = access.claim
        if claim is None or claim.implicit or access.holder_inside():
            return
        why = None
        if claim.holder.check is not None:
            try:
                why = claim.holder.check()
            except Exception:  # a broken check never ends a claim by itself
                log.warning("%s: the claim holder check failed", lane_id, exc_info=True)
        if why:
            self._end_claim(lane_id, lane, "grant", why)
            return
        try:
            self._check_gates(lane_id, claim.holder.kind)
        except AgentosError as e:
            self._end_claim(lane_id, lane, "frozen", e.message.split(". ")[0])
            return
        if self.clock() >= claim.expires:
            if claim.capped:
                self._end_claim(lane_id, lane, "max_total", f"others were waiting and it was held for "
                                f"{self.claim_max_total_s:g} s, the most a claim may be renewed")
            else:
                self._end_claim(lane_id, lane, "expired", f"no call from you for {claim.ttl_s:g} s, its ttl")

    def _end_claim(self, lane_id: str, lane: _Lane, reason: str, message: str, *, note: bool = True) -> Claim | None:
        if lane.access.claim is None or lane.access.claim.implicit:
            return None
        claim = lane.access.end(reason, message, self.clock(), note=note)
        if claim is None:
            return None
        self._end_drive(lane, f"the claim ended ({reason})")
        self._save_claims()
        self._event(lane_id, "claim_released" if not note else "claim_ended",
                    {"holder": claim.holder.label, "reason": reason, "detail": message,
                     "held_s": round(self.clock() - claim.started, 1)})
        log.info("%s: claim of %s ended (%s: %s)", lane_id, claim.holder.label, reason, message)
        self._dispatch(lane_id, lane)
        return claim

    def _save_claims(self) -> None:
        self._claims_file.write({lid: lane.access.claim for lid, lane in self._lanes.items()
                                 if lane.access.claim is not None})

    def _load_claim_notes(self) -> None:
        """Claims held when the node last stopped: their holders hear on their next call."""
        now = self.clock()
        for row in self._claims_file.take():
            lane_id = str(row["lane"])
            if not self.store.exists(lane_id):
                continue
            identity = str(row["identity"])
            self._lane(lane_id).access.notes[identity] = ClaimEnd(
                reason="restart", message="the node restarted, and claims do not survive a restart", at=now,
                lease=str(row.get("lease") or ""), identity=identity)

    async def claim(self, lane_id: str, holder: Holder, *, purpose: str | None = None, ttl_s: float | None = None,
                    wait_s: float | None = None, principal_kind: str = "agent") -> dict[str, Any]:
        """Claim the lane for a multi-step job. Waits in the lane's queue for up to `wait_s`
        (default queue_timeout), then LaneBusy. Claiming a lane you already hold renews it (and
        takes the new ttl and purpose). Warms a cold lane once. Returns the claim."""
        rec = self.store.require(lane_id)
        self._ensure_running()
        self._refuse_quarantined(rec)
        await self._expire_hold(lane_id)
        self._check_control(lane_id, principal_kind)
        if principal_kind == HUMAN:
            raise InvalidRequest("a person does not claim a lane: taking control of it ends every claim")
        lane = self._lane(lane_id)
        access = lane.access
        self._expire_claim(lane_id, lane)
        earlier = access.take_note(holder, self.clock())  # this claim is how the holder carries on
        ttl = clamp_ttl(ttl_s)
        text = clean_purpose(purpose)
        if access.holds(holder):
            claim = access.claim
            assert claim is not None
            claim.ttl_s, claim.purpose = ttl, text or claim.purpose
            access.renew(holder, self.clock(), self.claim_max_total_s)
            return self._claim_result(lane_id, lane, holder, renewed=True, earlier=earlier)
        waiter = await self._enter(lane_id, lane, holder, CLAIM, principal_kind,
                                   timeout=self.queue_timeout if wait_s is None else wait_s, purpose=text, ttl_s=ttl)
        access.leave(waiter, self.clock())  # a claim takes no place on the lane, only the lease
        self._dispatch(lane_id, lane)
        if not access.holds(holder):  # ended between the grant and here (a take-over)
            self._check_control(lane_id, principal_kind)
            raise LaneBusy(f"lane {lane_id}: the claim ended as soon as it was granted; call again")
        self._save_claims()
        self._event(lane_id, "claimed", {"holder": holder.label, "purpose": text, "ttl_s": ttl})
        log.info("%s: claimed by %s for %gs (%s)", lane_id, holder.label, ttl, text or "no purpose given")
        try:
            if lane.state not in (LaneState.WARM, LaneState.ACTIVE) or lane.session is None:
                info = await self._open(rec, principal_kind)  # a claim on a cold lane warms it, once
                if info.state == LaneState.QUARANTINED:
                    raise LaneQuarantined(self._quarantine_message(lane_id, lane))
        except BaseException:
            if access.holds(holder):
                self._end_claim(lane_id, lane, "failed", "the lane could not be opened", note=False)
            raise
        access.renew(holder, self.clock(), self.claim_max_total_s)
        return self._claim_result(lane_id, lane, holder, renewed=False, earlier=earlier)

    async def release(self, lane_id: str, holder: Holder) -> dict[str, Any]:
        """End the holder's claim (nothing to end is not an error)."""
        self.store.require(lane_id)
        lane = self._lane(lane_id)
        self._expire_claim(lane_id, lane)
        note = lane.access.take_note(holder, self.clock())
        released = lane.access.holds(holder) and lane.access.claim is not None and not lane.access.claim.implicit
        if released:
            self._end_claim(lane_id, lane, "released", "released by its holder", note=False)
        out: dict[str, Any] = {"on": lane_id, "released": released}
        if note is not None and not released:
            out["ended_earlier"] = {"reason": note.reason, "detail": note.message}
        elif not released:
            out["note"] = "you held no claim on this lane"
        out.update(self.queue_info(lane_id, viewer=holder))
        return out

    def claimed_by(self, lane_id: str, key: str) -> bool:
        """Whether the holder with this key holds an explicit (lane_claim) claim on the lane now."""
        lane = self._lanes.get(lane_id)
        if lane is None or not key:
            return False
        self._expire_claim(lane_id, lane)
        claim = lane.access.claim
        return claim is not None and not claim.implicit and claim.holder.key == key  # the key: it carries a nonce

    def queue_info(self, lane_id: str, *, viewer: Holder | None = None) -> dict[str, Any]:
        """The lane's claim and queue: who holds it and since when, who waits, in which order,
        with an estimate each (lane_queue, and `claim` / `queue_length` in the panel's lane JSON)."""
        self.store.require(lane_id)
        lane = self._lanes.get(lane_id)
        if lane is None:
            return {"claim": None, "queue_length": 0, "queue": [], "inside": [], "call_estimate_s": None}
        self._expire_claim(lane_id, lane)
        return lane.access.snapshot(self.clock(), self.claim_max_total_s, viewer=viewer)

    def _claim_result(self, lane_id: str, lane: _Lane, holder: Holder, *, renewed: bool,
                      earlier: ClaimEnd | None) -> dict[str, Any]:
        claim = lane.access.claim
        assert claim is not None
        out: dict[str, Any] = {"on": lane_id, "lease": claim.lease, "renewed": renewed,
                               **claim.public(self.clock(), self.claim_max_total_s),
                               "queue_length": len(lane.access.queue), "state": lane.state.value}
        if earlier is not None:
            out["ended_earlier"] = {"reason": earlier.reason, "detail": earlier.message}
        return out

    async def reap_claims(self) -> list[str]:
        """End every claim whose lease ran out (a holder that went away). Returns the lane ids."""
        ended = []
        for lane_id, lane in list(self._lanes.items()):
            if lane.access.claim is None:
                continue
            self._expire_claim(lane_id, lane)
            if lane.access.claim is None:
                ended.append(lane_id)
        return ended

    def _quarantine_message(self, lane_id: str, lane: _Lane) -> str:
        rec = self.store.get(lane_id)
        if rec is not None and rec.quarantine is not None:
            try:
                self._refuse_quarantined(rec)
            except LaneQuarantined as e:
                return e.message
        return (f"lane {lane_id} is quarantined ({lane.quarantine_reason or 'failed self-check'}). "
                "Fix the cause, then open it again with lane_open.")
