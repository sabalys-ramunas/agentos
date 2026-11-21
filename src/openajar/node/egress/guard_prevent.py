"""The egress guard before an agent acts: a press's target, the acting window, the gate's drive, settling."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from openajar.core.errors import EgressRefused, EngineError
from openajar.core.models import ActionKind
from openajar.core.text import first_line
from openajar.node.egress.classify import classify, gate_pattern
from openajar.node.egress.constants import ACTION_TIMEOUT_MS, SAFE_METHODS, STOP_TIMEOUT_S
from openajar.node.egress.helpers import QUIET_SCHEMES, _press_mode, _tab_before_newline
from openajar.node.egress.page_js import COVER_JS, COVER_POINTS, TARGET_JS
from openajar.node.egress.rules import EffectRule
from openajar.node.egress.sites import host_of
from openajar.node.egress.verdicts import Block, CommitMeter, PublishGate, Refusal, judge, refusal_message
from openajar.node.routes import GUARD_PATTERN

log = logging.getLogger("openajar.node.egress")


class GuardPrevention:
    """LaneGuard's prevention, a mixin: what is checked before an agent's act, and the drive it runs in."""

    # ---- before an agent acts (prevention) -----------------------------------------------

    def check_url(self, url: str, *, method: str = "GET", kind: str = "action") -> None:
        """Raise EgressRefused if the guard would refuse loading this document now."""
        refusal = judge(url, method, navigation=True, sites=self.sites, driver=self.driver, human=self._human(),
                        exposed=self._exposed())
        if refusal is not None:
            block = self._record(url, method, refusal, kind=kind, prevented=True)
            raise EgressRefused(refusal_message(block))

    async def check_action(self, page: Any, ref: str, action: ActionKind | str, text: str | None) -> None:
        """Before a click, a key press, or typing a newline (which is an Enter): refuse when what it
        would follow or submit goes where the guard would refuse. Only on a lane that limits its
        hosts (a site list, or a driving grant with an egress list) or that asks first and has no
        commit gate to hold the publish (`_click_asks_first`); elsewhere nothing is read and the
        listener judges as before. On a lane that limits its hosts, what cannot be read is refused:
        a link whose address cannot be read, a javascript: or data: link, a frame the click would land in, a
        type with a tab before a newline. A ref that does not resolve is left to the act. A script
        handler that navigates by itself is not seen here (the listener contains it)."""
        kind = action.value if isinstance(action, ActionKind) else str(action)
        mode = _press_mode(kind, text)
        if mode is None:
            return
        limits = self._limits_hosts()
        if not limits and not self._agent_asks_first():
            return
        if limits and kind in (ActionKind.TYPE.value, ActionKind.FILL.value) and _tab_before_newline(text or ""):
            self._refuse("", "GET", Refusal(
                "unreadable", "the text moves focus with a tab and then presses Enter, so the form it would "
                              "submit cannot be read"))
        locator = page.locator(f"aria-ref={ref}")
        try:
            target = await locator.evaluate(TARGET_JS, mode, timeout=ACTION_TIMEOUT_MS)
        except Exception as e:  # noqa: BLE001 - the act itself reports a ref that does not resolve
            log.debug("%s: could not read the target of %s: %s", self.lane_id, ref, e)
            return
        if not isinstance(target, dict):
            return
        targets = list(target.get("targets") or [])
        if limits and mode == "click" and target.get("framed"):
            targets += await self._frame_covers(locator)
        for t in targets:
            if isinstance(t, dict):
                if limits:
                    self._check_target(t)
                else:
                    self._check_publish(t)

    def _agent_asks_first(self) -> bool:
        d = self.driver
        return d is not None and not d.human and not self._human() and self._click_asks_first() is not None \
            and self._asks_first()

    def _click_asks_first(self) -> Callable[[], bool] | None:
        """The switch a check before the press reads, or None while the commit gate holds the act's
        publishes (its route is on): then the press goes, and the gate asks the person about
        exactly what it sends (its authorize reads the same switch)."""
        return None if self._gate is not None and self._drive_route is not None else self._asks_first

    def _check_publish(self, t: dict[str, Any]) -> None:
        """A target on a lane that asks first but limits no host: only whether it publishes."""
        url, method = t.get("url"), str(t.get("method") or "get").upper()
        if t.get("kind") != "form" or not isinstance(url, str) or method in SAFE_METHODS or method == "DIALOG":
            return
        rule = classify(url, method)
        refusal = judge(url, method, navigation=True, sites=(), driver=self.driver, human=self._human(),
                        effect=rule, asks_first=self._click_asks_first())
        if refusal is not None:
            self._refuse(url, method, refusal, rule=rule)

    def _limits_hosts(self) -> bool:
        """Does a host list apply to an agent here (the lane's, or the driving grant's)?"""
        d = self.driver
        agent = d is not None and not d.human and not self._human()
        return bool(self.sites) or (agent and d.lists is not None)

    def _check_target(self, t: dict[str, Any]) -> None:
        kind, url = t.get("kind"), t.get("url")
        if kind == "unreadable":
            why = t.get("why") or "what the guard cannot read"
            self._refuse("", "GET", Refusal("unreadable", f"the press could land on {why}"))
        what = "form" if kind == "form" else "link"
        method = str(t.get("method") or "get").upper() if kind == "form" else "GET"
        if method == "DIALOG":
            return
        if not isinstance(url, str):
            self._refuse("", method, Refusal("unreadable", f"a {what} whose address cannot be read"))
        if host_of(url) is None:
            scheme = url.split(":", 1)[0].lower() if ":" in url else ""
            if scheme not in QUIET_SCHEMES:
                named = f"a {scheme}: {what}" if scheme else f"a {what} with no web address"
                self._refuse(url, method, Refusal(
                    "scheme", f"{named} can run code or open what the guard cannot judge, and this browser limits "
                              "its hosts"))
            return
        rule = classify(url, method, rules=self.rules) if method not in SAFE_METHODS else None
        refusal = judge(url, method, navigation=True, sites=self.sites, driver=self.driver, human=self._human(),
                        effect=rule, asks_first=self._click_asks_first(), exposed=self._exposed())
        if refusal is not None:
            self._refuse(url, method, refusal, rule=rule)

    def _refuse(self, url: str, method: str, refusal: Refusal, *, rule: EffectRule | None = None) -> None:
        block = self._record(url, method, refusal, kind="action", prevented=True, rule=rule)
        raise EgressRefused(refusal_message(block))

    async def _frame_covers(self, locator: Any) -> list[dict[str, Any]]:
        """For a target inside a frame: what covers its press points in each parent document (a
        link in the parent page laid over the frame takes the click). Unreadable is refused."""
        try:
            handle = await locator.element_handle(timeout=ACTION_TIMEOUT_MS)
            frame = await handle.owner_frame()
            box = await asyncio.wait_for(handle.bounding_box(), STOP_TIMEOUT_S)
            if box is None:
                return []  # not visible: the act reports it
            points = [[box["x"] + box["width"] * fx, box["y"] + box["height"] * fy]
                      for fx in COVER_POINTS for fy in COVER_POINTS]
            frames = []
            while frame is not None and frame.parent_frame is not None:
                frames.append(frame)
                frame = frame.parent_frame
            out: list[dict[str, Any]] = []
            for child in reversed(frames):  # from the top document down to the target's
                if not points:
                    break
                holder = await asyncio.wait_for(child.frame_element(), STOP_TIMEOUT_S)
                hits = await asyncio.wait_for(holder.evaluate(COVER_JS, points), STOP_TIMEOUT_S)
                points = []
                for hit in hits:
                    if hit.get("state") == "frame":
                        points.append([hit["x"], hit["y"]])
                    elif hit.get("state") == "cover":
                        out.extend(hit.get("targets") or [])
            return out
        except Exception as e:  # noqa: BLE001 - refuse when in doubt
            log.debug("%s: could not read what covers a framed target: %s", self.lane_id, first_line(e))
            return [{"kind": "unreadable", "why": "a frame whose position cannot be read"}]

    @contextlib.asynccontextmanager
    async def acting(self, *, route: bool = True, meter: CommitMeter | None = None,
                     gate: PublishGate | None = None, effects_route: bool = False) -> AsyncIterator[int]:
        """Wrap an agent's action: yields a mark for blocks_since(). On a strict lane (and with
        `route`), a route blocks refused requests before they leave for as long as the action runs.
        `meter`: what a publish during the action is charged to; past a commit limit the route is
        on too, so the publish is aborted before it leaves (see the module docstring). `gate`: the
        commit gate (AUD-03). The first act with a gate starts the agent's drive: the gate's route
        (gate_pattern) is bound from then until control leaves the agent (end_drive), and judges
        with the latest act's gate and meter, between acts too. Never for a human in control.
        `effects_route` (keepalive only): control is the node's, so any drive ends, and on any lane
        the same route handler is bound on the gate's pattern in its effects mode: it aborts only
        the write and commit requests the guard refuses, and never gates (no approval is asked for
        the node). Not when the strict route is on, which aborts those already."""
        if effects_route:
            self.end_drive("a keepalive visit holds the browser")
        # during a drive the mark is where the last act ended: a publish held between the agent's
        # calls (a page's timer) is reported by its next act
        mark = self._acted_seq if self._drive_route is not None else self._seq
        await self._settle_removal()  # the last drive's route is going: let it go before this act begins
        d = self.driver
        self._approved_until = None  # a person's approval covered what it sent, not this action
        agent = d is not None and not d.human and not self._human()
        previous, self._meter = self._meter, (meter if agent else None)
        previous_gate, self._gate = self._gate, (gate if agent else None)
        self._learn_act(d, agent, gate if route else None)  # type: ignore[attr-defined]
        self._act_non_get, self._act_last = 0, time.monotonic()
        self._inflight.clear()
        try:  # an act that needs a route it cannot get is refused before it runs: nothing is left to the listener
            installed, effects = await self._act_routes(route=route, agent=agent, meter=meter, gate=gate,
                                                        effects_route=effects_route)
        except BaseException:
            self._meter, self._gate = previous, previous_gate
            raise
        try:
            yield mark
        finally:
            self._meter = previous
            self._gate = previous_gate
            if route and not effects_route:  # a visit leaves the drive's report where the agent's act left it
                self._acted_seq = self._seq
            await self._end_act_routes(installed, effects)

    async def _act_routes(self, *, route: bool, agent: bool, meter: CommitMeter | None, gate: PublishGate | None,
                          effects_route: bool) -> tuple[Any, re.Pattern[str] | None]:
        """The routes an agent's act runs under: the drive's gate route, the strict route (a strict
        lane, a capped meter, or Ask me first without a gate), or a keepalive's effects route.
        (the strict pattern it bound or None, the effects pattern it bound or None)."""
        installed: Any = None
        effects: re.Pattern[str] | None = None
        if route and agent and gate is not None:
            await self._begin_drive(gate, meter)
        capped = False
        if route and agent and meter is not None and not (self.strict and self.active()):
            capped = self._capped(meter)
        # without a gate (no drive to hold a publish), a lane that asks first aborts what it refuses
        full = ((self.strict and self.active()) or capped
                or (gate is None and not effects_route and self._asks_first()))
        if route and agent and full:
            await self._bind(GUARD_PATTERN, "the strict egress route")
            installed = GUARD_PATTERN
            self._windows += 1
        if effects_route and route and agent and installed is None:
            effects = self.gate_route()
            await self._bind(effects, "the effect route")
            self._effect_route = effects
            self._effect_windows += 1
        return installed, effects

    def _capped(self, meter: CommitMeter) -> bool:
        try:
            return meter.capped()
        except Exception:  # the meter still counts what leaves
            log.warning("%s: could not read the commit meter", self.lane_id, exc_info=True)
            return False

    async def _end_act_routes(self, installed: Any, effects: re.Pattern[str] | None) -> None:
        """Remove the routes _act_routes bound, once the act is over."""
        if installed is not None:
            self._windows -= 1
            with contextlib.suppress(Exception):
                await self.dispatcher.remove_guard(self._route_handler, installed)
        if effects is not None:
            self._effect_windows -= 1
            if not self._effect_windows:
                self._effect_route = None
            with contextlib.suppress(Exception):
                await self.dispatcher.remove_guard(self._route_handler, effects)

    async def _bind(self, pattern: Any, what: str) -> None:
        """Bind this guard's route handler on `pattern`. The lane's route must exist for the act to run:
        without a dispatcher, or when the browser refuses the route, the act is refused (EngineError)."""
        if self.dispatcher is None:
            raise self._unbound(what, "the browser has no route dispatcher")
        try:
            await self.dispatcher.add_guard(self._route_handler, pattern)
        except Exception as e:
            log.warning("%s: could not install %s", self.lane_id, what, exc_info=True)
            raise self._unbound(what, first_line(e)) from e

    def _unbound(self, what: str, why: str) -> EngineError:
        return EngineError(f"the egress guard could not bind {what} on {self.lane_id} ({why}), so the act was "
                           "refused before it ran and nothing was sent; tell the human if it keeps failing")

    async def _begin_drive(self, gate: PublishGate, meter: CommitMeter | None) -> None:
        """Bind this act's gate and meter to the drive, and install the gate's route if it is not
        on yet. Measured (scripts/spikes/commit_gate.py): Playwright lets a request go when the
        route that paused it is gone by the time the driver hands it over, and a page publishes
        when it likes (a captcha first, a timer, an upload that finishes); so the route is not
        removed between acts, only when control leaves the agent. Raises EngineError when the route
        cannot be bound (_bind): the drive is then not on, and the act does not run."""
        if self._drive_route is None:
            pattern = self.gate_route()
            if self._flow_route == pattern:  # a flow's route: the drive takes it over, one route
                self._flow_route = None
            else:
                await self._bind(pattern, "the commit gate's route")
            self._drive_route = pattern
            self._drive_since = self.clock()
            log.info("%s: the commit gate's route is on for the agent's drive", self.lane_id)
        self._drive = (gate, meter)

    def end_drive(self, why: str) -> None:
        """Control left the agent (a person took over, a pause or a hold, the claim ended, the lane
        sleeps or closes): nothing is gated from now on, and the gate's route goes, so the HTTP
        cache works again. Safe to call at any time, from sync code. A login window ends with it."""
        self._login_until.clear()
        self._drive = None
        pattern, self._drive_route = self._drive_route, None
        if pattern is None:
            return
        if self._flow_checks and self._flow_route is None:  # a flow still needs it to read who it posts as
            self._flow_route = pattern
            log.info("%s: the commit gate's route stays on for a running flow (%s)", self.lane_id, why)
            return
        log.info("%s: the commit gate's route is off (%s, after %.0f s)", self.lane_id, why,
                 self.clock() - (self._drive_since or self.clock()))
        if self.dispatcher is not None:
            self._drive_removal = self._spawn(self.dispatcher.remove_guard(self._route_handler, pattern))

    def driving(self) -> bool:
        """Is an agent's drive on (the gate's route bound)?"""
        return self._drive_route is not None

    def drive_state(self) -> tuple[PublishGate, CommitMeter | None] | None:
        """The drive as it stands (the gate and meter of the latest act), or None when no drive is on."""
        return self._drive if self._drive_route is not None else None

    async def restore_drive(self, state: tuple[PublishGate, CommitMeter | None] | None) -> None:
        """Bind a drive again that a keepalive visit ended (acting(effects_route=True) ends any drive), so
        the gate judges what the page sends before control returns to the agent. Not while a person has
        control: a take over ends a drive for good."""
        if state is None or self._drive_route is not None or self._human():
            return
        await self._settle_removal()
        gate, meter = state
        try:
            await self._begin_drive(gate, meter)
        except EngineError:  # the visit has ended: the agent's next act binds the route, or is refused
            log.warning("%s: the agent's drive could not be bound again after a keepalive visit", self.lane_id,
                        exc_info=True)

    async def _settle_removal(self) -> None:
        """Let the last drive's route removal finish, before a route is bound again (the same pattern and
        handler could be added and then removed by that pending removal)."""
        removal, self._drive_removal = self._drive_removal, None
        if removal is not None:
            with contextlib.suppress(Exception):
                await removal

    def gate_route(self) -> re.Pattern[str]:
        """The commit gate's route for this lane's rules (gate_pattern), built once."""
        if self._gate_route is None or self._gate_route[0] is not self.rules:
            self._gate_route = (self.rules, gate_pattern(self.rules))
        return self._gate_route[1]

    async def settle(self, gate: PublishGate | None) -> None:
        """After an agent's press: when it sent a non-GET, wait until every non-GET it started has
        finished and requests have gone quiet (settle_quiet_s), at most settle_max_s, so what the
        click publishes (a second request included, one behind a captcha or an upload) is judged
        inside the act and the tool reports it. A press that sent nothing but GETs returns after
        settle_grace_s, or, on the first press of a browser younger than settle_cold_age_s that has
        told of no fetch since it launched (a content blocker holding requests until its lists
        load), once they come, at most settle_cold_s (paid once a browser). The
        gate's route stays on after the act either way (the drive), so a publish that comes later
        is held all the same; settling only decides what this act reports."""
        if gate is None or self._gate is not gate:
            return
        start = time.monotonic()
        # the cold wait is paid once a browser: its first press, while it is young (SETTLE_COLD_AGE_S)
        cold = self._live_at is None and not self._cold_spent and start - self._attached_at < self.settle_cold_age_s
        self._cold_spent = True
        while not self._act_non_get:
            # a browser that has not told of a fetch since it launched may still be holding them
            # (SETTLE_COLD_S): wait for them, and once they come, the usual grace after that
            live = self._live_at
            until = start + (self.settle_cold_s if cold and (live is None or live > start) else self.settle_grace_s)
            if live is not None and live > start:
                until = min(until, max(start, live) + self.settle_grace_s)
            if time.monotonic() >= until:
                break
            await asyncio.sleep(0.02)
        if not self._act_non_get:
            return
        while True:
            now = time.monotonic()
            if now - start >= self.settle_max_s:
                return
            if not self._inflight and now - self._act_last >= self.settle_quiet_s:
                return
            await asyncio.sleep(0.05)

    def blocks_since(self, mark: int) -> list[Block]:
        return [b for b in self._notes if b.seq > mark]
