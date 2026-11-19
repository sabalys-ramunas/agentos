"""Routines and flow runs under a lane's egress guard (LaneGuard): a mixin, like GuardPrevention,
GuardRecords and GuardLearn. While a flow runs, its requests are its own, and its publishes pass
the flow's identity check.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

from agentos.core.text import first_line
from agentos.node.egress.constants import FLOW_RELOAD_S, SAFE_METHODS
from agentos.node.egress.helpers import _post_data
from agentos.node.egress.rules import EffectRule
from agentos.node.egress.sites import host_of, norm_path
from agentos.node.egress.verdicts import FlowCommit, Refusal

log = logging.getLogger("agentos.node.egress")


class GuardFlows:
    """Routines and flow runs on the lane: who owns a request while one runs, and the flow's own
    publish check (its identity check, AUD-01)."""

    @contextlib.contextmanager
    def routine(self, page: Any = None) -> Iterator[None]:
        """A flow run holds the lane: it was authorized at its own effect (it asked first when the
        switch was on, its approval or proof binds the flow's version and parameters), so `ask_first`
        does not refuse what it publishes, and the agent's grant does not judge what it sends. With
        no drive on, that is every request while the flow holds the lane. During an agent's drive
        only the requests of `page`, the page the flow drives once flow() reloaded it clean, are the
        flow's (_flow_owns): they go to the listener (never gated, held or charged as the agent's
        publish). Everything else stays gated, so a timer the agent's presses armed on another page,
        or on a page that could not be reloaded (`page` None), cannot publish in the flow's window.
        The drive's route stays on, and gates everything again once the flow lets go of the lane."""
        self._learn_close("a flow took the browser")
        self._routines += 1
        if page is not None:
            self._flow_pages.append(page)
        try:
            yield
        finally:
            self._routines -= 1
            if page is not None:
                with contextlib.suppress(ValueError):
                    self._flow_pages.remove(page)

    @contextlib.asynccontextmanager
    async def flow(self, page: Any) -> AsyncIterator[None]:
        """What the flow driver enters when a flow run takes the lane (routine). During an agent's
        drive the page is reloaded first, while the drive's route still gates (its unload's beacons
        included), so a timer the agent's presses armed dies with its document before the flow's
        requests go ungated. A page that does not reload within FLOW_RELOAD_S is not the flow's:
        the drive keeps gating all it sends (a publish of the flow's is then held too, fail closed)."""
        clean = page
        if self._drive_route is not None:
            try:
                await asyncio.wait_for(page.reload(wait_until="commit"), FLOW_RELOAD_S)
            except Exception as e:  # noqa: BLE001 - its old document may still run: keep gating it
                log.warning("%s: a flow could not reload the page an agent drove (%s); the drive keeps gating it",
                            self.lane_id, first_line(e))
                clean = None
        try:
            with self.routine(clean):
                yield
        finally:
            await self._end_flow_scope()

    async def scope_flow(self, commit: FlowCommit) -> None:
        """A connector run holding the lane (flow()) declared its commit request: from now until it
        lets go of the lane, its page's commit and write requests leave only when they are that
        request (FlowCommit.problem), and anything else is aborted before it leaves. The rule hosts'
        route (gate_route) is bound for that; when it cannot be, this raises EngineError and the run
        does not start (fail closed). An agent's own browsing is not touched: only the flow's
        requests (_flow_owns) are judged here."""
        if self._flow_commit_route is None:
            pattern = self.gate_route()
            await self._bind(pattern, "the connector run's commit route")
            self._flow_commit_route = pattern
        self._flow_commit = commit

    async def _end_flow_scope(self) -> None:
        self._flow_commit = None
        pattern, self._flow_commit_route = self._flow_commit_route, None
        if pattern is not None and self.dispatcher is not None:
            with contextlib.suppress(Exception):
                await self.dispatcher.remove_guard(self._route_handler, pattern)

    def _flow_refusal(self, rule: EffectRule | None, request: Any, url: str, method: str,
                      headers: dict[str, str]) -> Refusal | None:
        """The connector run's scope (scope_flow) for one of its page's non-GETs."""
        commit = self._flow_commit
        if commit is None:
            return None
        if commit.strict:  # a learned operation's run: a known read may also name the owner's promoted reads
            why = commit.problem(rule, url, method, headers, lambda: _post_data(request),
                                 read_ops=self._read_ops.get((host_of(url) or "", norm_path(url))))
            if why is None and method.upper() not in SAFE_METHODS:
                why = self._strict_form_problem(commit, url, method, headers, _post_data(request))
        else:
            why = commit.problem(rule, url, method, headers, lambda: _post_data(request))
        return Refusal("flow_commit", why) if why else None

    @contextlib.asynccontextmanager
    async def flow_identity(self, check: Callable[[EffectRule, Any, str, str], Awaitable[Refusal | None]]
                            ) -> AsyncIterator[None]:
        """While a flow runs on its own on a lane whose login can post as others (agentos.node.acting):
        each publish its page sends (a commit rule's request it owns, _flow_owns) is first given to
        `check`, which refuses one that acts as an identity agentos does not know (nobody is there to
        ask). The gate's route is bound for it when no drive or other flow has bound it already, and
        stays for as long as any flow's check is on; a lane whose route cannot be bound refuses the
        flow (EngineError), as an act would be."""
        await self._settle_removal()
        if self._drive_route is None and self._flow_route is None:
            pattern = self.gate_route()
            await self._bind(pattern, "the flow identity route")
            self._flow_route = pattern
        self._flow_checks.append(check)
        try:
            yield
        finally:
            with contextlib.suppress(ValueError):
                self._flow_checks.remove(check)
            if not self._flow_checks and self._flow_route is not None:
                pattern, self._flow_route = self._flow_route, None
                with contextlib.suppress(Exception):
                    await self.dispatcher.remove_guard(self._route_handler, pattern)

    async def _check_flow(self, rule: EffectRule, request: Any, url: str, method: str) -> Refusal | None:
        for check in list(self._flow_checks):
            try:
                refusal = await check(rule, request, url, method)
            except Exception:  # fail closed: a publish whose identity cannot be judged is not let go
                log.warning("%s: the flow identity check failed on %s; the publish is aborted", self.lane_id,
                            rule.id, exc_info=True)
                return Refusal("identity_unrecognized", f"this publishes ({rule.id}) and who it posts as could "
                                                        "not be checked")
            if refusal is not None:
                return refusal
        return None

    def _flow_owns(self, request: Any) -> bool:
        """Is this request a flow's (routine)? With no drive on, any while a flow holds the lane;
        during a drive, only one from a page a flow reloaded clean (flow()). A request whose page
        cannot be read (a service worker's) is not."""
        if not self._routines:
            return False
        if self._drive_route is None:
            return True
        try:
            page = request.frame.page
        except Exception:  # noqa: BLE001
            return False
        return any(page is p for p in self._flow_pages)
