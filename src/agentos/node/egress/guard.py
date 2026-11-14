"""The egress guard of one lane's browser context (LaneGuard): its settings, attaching, the strict
route, the listener and the vault's login window. The package docstring (agentos.node.egress) says
what it guards and how."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import weakref
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

from agentos.node import publish
from agentos.node.egress import rules as _rules
from agentos.node.egress import sites as _sites
from agentos.node.egress.classify import classify, effects_of, never_sent
from agentos.node.egress.constants import (
    APPROVED_WINDOW_S,
    FLOW_RELOAD_S,
    LIVE_TYPES,
    NOTES_KEPT,
    OBSERVE_EVERY_S,
    OBSERVE_NAME,
    POPUP_WINDOW_S,
    SAFE_METHODS,
    SETTLE_COLD_AGE_S,
    SETTLE_COLD_S,
    SETTLE_GRACE_S,
    SETTLE_MAX_S,
    SETTLE_QUIET_S,
    SITES_TTL_S,
    STOP_TIMEOUT_S,
    UPLOADS_KEPT,
)
from agentos.node.egress.guard_learn import GuardLearn
from agentos.node.egress.guard_prevent import GuardPrevention
from agentos.node.egress.guard_records import GuardRecords
from agentos.node.egress.helpers import _first_line, _in_subframe, _path, _post_data, _short, printable_key
from agentos.node.egress.learn_window import LearnWindow
from agentos.node.egress.rules import ABORTED_KEEP, LOGIN_WINDOW_S, SENT_KEEP, EffectRule
from agentos.node.egress.shapes import (
    _PUT_LIKE,
    TypedStream,
    body_covered,
    gql_verdict,
    operations,
    read_observe,
    request_shape,
    unknown_rule,
)
from agentos.node.egress.sites import host_of, norm_path
from agentos.node.egress.trace import EgressTrace
from agentos.node.egress.verdicts import Block, CommitMeter, Driver, FlowCommit, PublishGate, Refusal, judge

log = logging.getLogger("agentos.node.egress")


# ---- the guard ---------------------------------------------------------------------------

Events = Callable[[str, str, dict[str, Any]], None]

# context -> its LaneGuard, so tools find the guard of the page they act on
_GUARDS: weakref.WeakKeyDictionary[Any, LaneGuard] = weakref.WeakKeyDictionary()


def guard_for(page_or_context: Any) -> LaneGuard | None:
    """The guard of a page's (or a context's) lane, or None for a browser without one."""
    ctx = getattr(page_or_context, "context", page_or_context)
    try:
        return _GUARDS.get(ctx)
    except TypeError:
        return None


class LaneGuard(GuardPrevention, GuardRecords, GuardLearn):
    """The egress guard of one lane's browser context. See the module docstring."""

    def __init__(self, lane_id: str, *, sites: list[str] | tuple[str, ...] = (), strict: bool = False,
                 trace: EgressTrace | None = None, clock: Callable[[], float] = time.monotonic):
        self.lane_id = lane_id
        self._sites: tuple[str, ...] = tuple(sites)
        self.strict = strict
        self.trace = trace
        self.clock = clock
        self.driver: Driver | None = None
        # bound by the lane manager: is a human in control, the lane's events, a hold, the store
        self.human_in_control: Callable[[], bool] = lambda: False
        self.events: Events | None = None
        self.on_violation: Callable[[Block], Any] | None = None
        self.settings: Callable[[], tuple[list[str], bool]] | None = None
        self.on_notice: Callable[[dict[str, Any]], Any] | None = None  # the live view's block notices
        # a publish a person made by hand (in control): recorded on the lane's posting pace, never judged
        self.on_human_commit: Callable[[], Any] | None = None
        # any request an effect rule names that the site answered, whoever sent it: on_effect(rule,
        # actor), actor agent, flow or person. The lane manager hands it to the action ledger
        # (agentos.policy.ledger), so every path's likes and posts count against the account's caps
        self.on_effect: Callable[[EffectRule, str], Any] | None = None
        # the hosts of the owner's dev servers exposed to this lane right now (agentos.node.expose, bound
        # when the lane launches): inside every host list while they last, never past their end
        self.exposed: Callable[[], frozenset[str]] = frozenset
        # bound with the driver (agentos.node.tools.driven_session): is "Ask me first" on for this
        # lane, and how a person hears of a publish it refused (a card in Needs you)
        self.asks_first: Callable[[], bool] = lambda: False
        self.on_ask_first: Callable[[Block], Any] | None = None
        self._routines = 0  # flow runs holding the lane now
        # who a flow's publish acts as (flow_identity): checks for the flows holding the lane, and the
        # gate's route while it is theirs. One route, one owner: a drive that ends under a flow hands
        # the route to it (end_drive), a drive that begins takes it over (_begin_drive), and the last
        # flow out removes it
        self._flow_checks: list[Callable[[EffectRule, Any, str, str], Awaitable[Refusal | None]]] = []
        self._flow_route: re.Pattern[str] | None = None
        self._flow_pages: list[Any] = []  # the pages those flows drive, reloaded clean (flow())
        # the commit request the connector run holding the lane declared (scope_flow), and the route
        # that refuses its page's other commits and writes before they leave
        self._flow_commit: FlowCommit | None = None
        self._flow_commit_route: re.Pattern[str] | None = None
        self._approved_until: float | None = None  # person_approved()
        self._told_at: float | None = None
        self._notice_keys: dict[tuple[str, str, str], list[float]] = {}  # key -> [last notice, folded]
        self._notice_times: deque[float] = deque()
        self._settings_at: float | None = None
        self.dispatcher: Any = None
        self._context: Any = None
        self._notes: deque[Block] = deque(maxlen=NOTES_KEPT)
        self._seq = 0
        self._limits: dict[tuple[str, str, str], list[float]] = {}  # key -> [last log, last trace, repeats]
        self._windows = 0
        self._popup_blocks: deque[tuple[float, str]] = deque(maxlen=8)
        self._tasks: set[asyncio.Task] = set()
        self.stats: dict[str, int] = {"blocked": 0, "prevented": 0, "observed_commits": 0, "unblocked": 0,
                                      "observed_requests": 0, "gated": 0, "unmatched_publishes": 0, "uploads": 0,
                                      "unruled_requests": 0}
        self.observe_path: Path | None = trace.path.with_name(OBSERVE_NAME) if trace is not None else None
        self._observe: tuple[float, float | None] | None = None  # (read at, expiry)
        self._observed: dict[tuple[Any, ...], float] = {}
        self._meter: CommitMeter | None = None  # set while an agent's act runs (acting)
        self._base_rules: tuple[EffectRule, ...] = _rules.EFFECT_RULES  # the dev node adds its fake site's rules
        self._rules: tuple[EffectRule, ...] = _rules.EFFECT_RULES
        # the owner's promoted shapes (agentos.node.gateshapes, bound by the lane manager): read when the
        # guard starts and again when they change, at most every SITES_TTL_S
        self.owner_shapes: Callable[[], tuple[dict[str, Any], ...]] | None = None
        self._owner_seen: tuple[dict[str, Any], ...] | None = None
        self._owner_at: float | None = None
        # the node's learned profile rules (agentos.node.learned_rules, bound by the lane manager):
        # compiled after the built-in commit rules and before the reads, reread when they change
        self.learned_rules: Callable[[], tuple[dict[str, Any], ...]] | None = None
        self._learned_seen: tuple[dict[str, Any], ...] | None = None
        # the learn window (open_learn): at most one per lane, and it releases at most one request
        self._learn: LearnWindow | None = None
        self._learn_done: dict[str, dict[str, Any]] = {}  # window id -> what it released (learn_result)
        # the form's current values the edit pages loaded (a reviewed endpoint's form_data), in memory only
        self._form_data: dict[tuple[str, str], dict[str, Any]] = {}
        # the agent's acts with a gate on this browser: how many, when the latest began, its grant and call
        self._act_seq = 0
        self._act_at: float | None = None
        self._act_grant: str | None = None
        self._act_call: str | None = None
        self._read_ops: dict[tuple[str, str], dict[str, frozenset[str]]] = {}
        self._gate: PublishGate | None = None  # set while an agent's act runs with the commit gate
        self._gate_route: tuple[tuple[EffectRule, ...], re.Pattern[str]] | None = None
        # the agent's drive: the latest act's gate and meter, judging on the gate's route from the
        # first gated act until end_drive (control leaves the agent)
        self._drive: tuple[PublishGate, CommitMeter | None] | None = None
        self._drive_route: re.Pattern[str] | None = None
        self._drive_since: float | None = None
        self._drive_removal: asyncio.Task | None = None
        self._acted_seq = 0  # the block sequence when the agent's last act ended
        self._route_handler = self._route_guard  # one handler for every route of this guard (it runs once)
        self._inflight: set[Any] = set()  # non-GETs an act started that have not finished (settle)
        self._aborted: dict[int, Any] = {}  # requests this guard aborted (refused): their failure is no action
        self._counted: dict[int, Any] = {}  # the first request of each redirect chain the ledger counted
        self._sent_by: dict[int, tuple[Any, str]] = {}  # non-GETs in flight: who drove the lane when they left
        self.uploads: deque[publish.Upload] = deque(maxlen=UPLOADS_KEPT)
        self.registrations: deque[publish.Registration] = deque(maxlen=UPLOADS_KEPT)
        self.settle_quiet_s = SETTLE_QUIET_S
        self.settle_max_s = SETTLE_MAX_S
        self.settle_grace_s = SETTLE_GRACE_S
        self.settle_cold_s = SETTLE_COLD_S
        # when the browser first told the driver of a fetch (LIVE_TYPES) since attach: until then a
        # content blocker may still hold the page's requests (SETTLE_COLD_S); None: not yet
        self._live_at: float | None = None
        self.settle_cold_age_s = SETTLE_COLD_AGE_S
        self._attached_at = time.monotonic()
        self._cold_spent = False  # a press paid the cold wait on this browser
        self._act_non_get = 0
        self._act_last = 0.0
        # while an agent drives with the gate: a POST to a rule host that no rule names is held as
        # unknown (fail closed). False lets it go, traced, as before (docs/design/egress-guard.md 8.6)
        self.gate_unknown_posts = True
        # what agents entered on this lane's browser (browser_act type, fill and printable keys), for as
        # long as the browser lives: the gate asks a person about an unknown request only when it
        # carries some of it (carries_typed). In memory only, never traced or logged; a vault fill is
        # never here (agentos.node.egress.shapes.TypedStream)
        self.typed = TypedStream(self.clock)
        # the vault's login windows: an entry's registrable domain -> when it closes (open_login)
        self._login_until: dict[str, float] = {}
        self.login_window_s = LOGIN_WINDOW_S
        # keepalive visits running with the route in its effects mode (acting(effects_route=True))
        self._effect_windows = 0
        self._effect_route: re.Pattern[str] | None = None

    # ---- settings ------------------------------------------------------------------------

    @property
    def sites(self) -> tuple[str, ...]:
        self._refresh()
        return self._sites

    def set_sites(self, sites: list[str] | tuple[str, ...], strict: bool | None = None) -> None:
        self._sites = tuple(sites)
        if strict is not None:
            self.strict = strict
        self._settings_at = self.clock()

    def _refresh(self) -> None:
        if self.settings is None:
            return
        now = self.clock()
        if self._settings_at is not None and now - self._settings_at < SITES_TTL_S:
            return
        self._settings_at = now
        try:
            sites, strict = self.settings()
        except Exception:
            log.warning("%s: could not read the lane's site list; keeping the last one", self.lane_id, exc_info=True)
            return
        self._sites, self.strict = tuple(sites), bool(strict)

    @property
    def rules(self) -> tuple[EffectRule, ...]:
        """The effect rules this guard classifies by: the owner's promoted commit rules, the
        built-in ones (and the dev site's), then the owner's promoted reads (gateshapes)."""
        self._refresh_owner()
        return self._rules

    @rules.setter
    def rules(self, rules: tuple[EffectRule, ...]) -> None:
        self._base_rules = tuple(rules)
        self._owner_seen = None
        self._learned_seen = None
        self._owner_at = None
        self._refresh_owner()

    def _refresh_owner(self) -> None:
        """Read the owner's promotions and the node's learned rules again (at most every
        SITES_TTL_S) and compile them. Fail closed: when a source or a compile fails, its rules do
        not apply, and the next read tries again (they are recorded as seen only once they compiled)."""
        now = self.clock()
        if self._owner_at is not None and now - self._owner_at < SITES_TTL_S:
            return
        self._owner_at = now
        learned_entries = self._learned_entries()
        try:
            shapes = self.owner_shapes() if self.owner_shapes is not None else ()
        except Exception:  # the built-in rules alone: the gate stays fail closed
            log.warning("%s: could not read the owner's gate shapes; no promotion applies", self.lane_id,
                        exc_info=True)
            shapes = None
        if shapes is not None and shapes is self._owner_seen and learned_entries is self._learned_seen:
            return
        owner_commit: tuple[EffectRule, ...] = ()
        owner_read: tuple[EffectRule, ...] = ()
        ops: dict[tuple[str, str], dict[str, frozenset[str]]] = {}
        if shapes:
            from agentos.node import gateshapes

            try:
                owner = gateshapes.compile_rules(shapes, self._base_rules, lane=self.lane_id)
                owner_commit, owner_read, ops = owner.commit, owner.read, owner.read_ops
            except Exception:
                log.warning("%s: could not compile the owner's gate shapes; no promotion applies", self.lane_id,
                            exc_info=True)
                shapes = None
        learned: tuple[EffectRule, ...] = ()
        if learned_entries:
            from agentos.node import learned_rules

            try:
                learned = learned_rules.compile_rules(learned_entries, _rules.EFFECT_RULES)
            except Exception:
                log.warning("%s: could not compile the learned rules; none applies", self.lane_id, exc_info=True)
                learned_entries = None
        if learned:
            from agentos.node.learned_rules import ordered

            self._rules = ordered(owner_commit, self._base_rules, learned, owner_read)
        else:
            self._rules = owner_commit + self._base_rules + owner_read
        self._read_ops = ops
        self._owner_seen = shapes
        self._learned_seen = learned_entries

    def _learned_entries(self) -> tuple[dict[str, Any], ...] | None:
        """The learned rules' entries now; None when they cannot be read (none applies)."""
        try:
            return self.learned_rules() if self.learned_rules is not None else ()
        except Exception:
            log.warning("%s: could not read the learned rules; none applies", self.lane_id, exc_info=True)
            return None

    def observing(self) -> bool:
        """Is the owner observing this lane's requests (OBSERVE_NAME, reread every SITES_TTL_S)?"""
        now = self.clock()
        if self._observe is None or now - self._observe[0] >= SITES_TTL_S:
            self._observe = (now, read_observe(self.observe_path))
        return self._observe[1] is not None

    def drive(self, driver: Driver | None) -> None:
        """Who acts in the lane from now on. The last agent stays the driver between its calls, so
        a page that navigates on its own later is judged by the same grant; a take over switches to
        the lane's list alone (human_in_control)."""
        if driver != self.driver:
            log.debug("%s: driven by %s", self.lane_id, driver.public() if driver else None)
        self.driver = driver
        self._learn_driver(driver)
        if driver is None or driver.human:
            self.end_drive("a person drives the lane")

    def _exposed(self) -> frozenset[str]:
        """The exposed hosts now; none when they cannot be read (fail closed: the host lists apply)."""
        try:
            return frozenset(self.exposed())
        except Exception:
            log.warning("%s: could not read the exposed hosts; none apply", self.lane_id, exc_info=True)
            return frozenset()

    def _human(self) -> bool:
        try:
            return bool(self.human_in_control())
        except Exception:  # noqa: BLE001
            return False

    def _asks_first(self) -> bool:
        """Does "Ask me first" hold an agent's publish on this lane now? When the switch cannot be
        read the answer is yes: a publish is refused rather than let past a person's "on"."""
        if self._routines or (self._approved_until is not None and self.clock() < self._approved_until):
            return False
        try:
            return bool(self.asks_first())
        except Exception:
            log.warning("%s: could not read Ask me first; refusing the publish", self.lane_id, exc_info=True)
            return True

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
        self._learn_close("a flow took the lane")
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
                            self.lane_id, _first_line(e))
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

    def note_typed(self, text: str | None) -> None:
        """An agent types `text` into this lane's page (browser_act type or fill): remember it, so a
        request no rule reads that carries it can be told from the page's own traffic (carries_typed)."""
        self.typed.note(text)
        self._learn_typed(text)

    def note_key(self, key: str | None) -> None:
        """An agent presses `key` (browser_act press): a printable key is text it entered, one
        character at a time ("a", "Shift+A", "Space"); anything else (Enter, Tab, Control+a) is not."""
        char = printable_key(key)
        if char is not None:
            self.typed.note(char, piece=False)
            self._learn_typed(char, key=True)

    def typed_texts(self) -> list[str]:
        """The whole pieces agents typed on this lane, newest first (the check is carries_typed)."""
        return self.typed.pieces()

    def carries_body(self, body: bytes | None) -> str | None:
        """What of the agent's typed text this body carries (TypedStream.found_in), or None."""
        return self.typed.found_in(body)

    def carries_typed(self, request: Any) -> str | None:
        """What of the agent's typed text this request's body carries, or None."""
        return self.carries_body(_post_data(request)) if self.typed else None

    def person_approved(self) -> None:
        """A person approved the commit that happens next (a confirm dialog): what it sends is not
        refused for `ask_first`, for a moment and only until the agent's next action."""
        self._approved_until = self.clock() + APPROVED_WINDOW_S

    def active(self) -> bool:
        """Is there anything to enforce right now (a list, or a grant with a list or a ceiling)?"""
        d = self.driver
        return bool(self.sites) or (d is not None and not d.human and (d.lists is not None or d.ceiling is not None))

    # ---- attaching -----------------------------------------------------------------------

    def attach(self, context: Any, dispatcher: Any = None) -> LaneGuard:
        self._context = context
        self._live_at = None  # a browser just launched: its content blocker may hold requests for a moment
        self._attached_at, self._cold_spent = time.monotonic(), False
        self.dispatcher = dispatcher
        guard = self

        def on_request(request: Any) -> None:
            guard._on_request(request)

        def on_page(page: Any) -> None:
            guard._adopt(page)

        def on_finished(request: Any) -> None:
            guard._done(request)
            guard._learn_finished(request)
            guard._effect_done(request)
            guard._sent_by.pop(id(request), None)

        def on_failed(request: Any) -> None:
            guard._done(request)
            guard._effect_failed(request)
            guard._sent_by.pop(id(request), None)

        context.on("request", on_request)
        context.on("requestfinished", on_finished)
        context.on("requestfailed", on_failed)
        context.on("page", on_page)
        for page in list(getattr(context, "pages", ())):
            self._adopt(page)
        _GUARDS[context] = self
        return self

    def _adopt(self, page: Any) -> None:
        guard = self

        def on_nav(frame: Any) -> None:
            guard._on_committed(page, frame)

        page.on("framenavigated", on_nav)
        if self._popup_blocks and self.clock() - self._popup_blocks[-1][0] < POPUP_WINDOW_S:
            self._spawn(self._close_popup(page))

    # ---- the strict route ----------------------------------------------------------------

    async def _route_guard(self, route: Any, request: Any) -> bool:
        """RouteDispatcher guard: True when it aborted the request (owners never see it). It judges
        with the drive's gate and meter while an agent's drive is on (between acts too), else with
        the current act's. A person in control is never gated, charged or aborted here."""
        gate, meter = self._drive if self._drive is not None else (self._gate, self._meter)
        try:
            url, method, nav = request.url, request.method, request.is_navigation_request()
            headers = request.headers
        except Exception:  # noqa: BLE001 - fail closed: a request the guard cannot read is not let go
            log.warning("%s: the egress route could not read a request; it is aborted", self.lane_id)
            await self._abort(route, request, "blockedbyclient")
            return True
        unsafe = method.upper() not in SAFE_METHODS
        if not unsafe and not self._windows:
            return False  # only the commit gate's route is on: a GET goes on at once (the listener judges it)
        rule = classify(url, method, headers, body=lambda: _post_data(request), rules=self.rules) if unsafe else None
        if unsafe and self._flow_commit is not None and not self._human() and self._flow_owns(request):
            # a connector run's page: only the commit request it declared may write (scope_flow)
            refusal = self._flow_refusal(rule, request, url, method, headers)
            if refusal is not None:
                self._record(url, method, refusal, kind=self._kind_of(request, nav), prevented=True, rule=rule)
                await self._abort(route, request, "aborted" if nav else "blockedbyclient")
                return True
        if self._effect_windows and not self._windows:
            return await self._effects_only(route, request, url, method, nav, rule, headers)
        if self._flow_checks and rule is not None and rule.effect == "commit" and self._flow_owns(request):
            refusal = await self._check_flow(rule, request, url, method)  # who a flow's publish acts as
            if refusal is not None:
                self._record(url, method, refusal, kind=self._kind_of(request, nav), prevented=True, rule=rule)
                await self._abort(route, request, "aborted" if nav else "blockedbyclient")
                return True
        if not self._windows and self._flow_owns(request):
            return False  # a flow run's own page: authorized at its own effect, the listener judges it
        if self._human():
            if rule is not None and rule.effect == "commit":
                self._observed_commit(request, url, method, rule)
            return False
        frame = nav and _in_subframe(request)
        if gate is not None and unsafe:
            rule = self._gate_rule(rule, request, url, method, headers)
        # with a gate the switch is read by the gate's authorize (the publish waits for a person);
        # without one, a lane that asks first refuses an agent's publish here
        refusal = judge(url, method, navigation=nav, sites=self.sites, driver=self.driver, human=False,
                        effect=rule, frame=frame, asks_first=None if gate is not None else self._asks_first,
                        exposed=self._exposed())
        if refusal is None and rule is not None and rule.effect == "commit":
            released = await self._learn_release(gate, rule, request, url, method, headers, meter=meter) \
                if gate is not None and self._learn is not None else False
            if isinstance(released, Refusal):
                refusal = released  # the window's own request, refused: Ask me first is on, or a limit
            elif released:
                pass  # the one request an open learn window lets go (open_learn): no card, metered
            elif gate is not None:  # the commit gate: authorized (and metered) as a commit
                refusal = await self._hold(gate, rule, request, url, method)
            else:
                refusal = self._charge(rule, sent=False, meter=meter)
        if refusal is None:
            if rule is not None and rule.effect == "commit":
                self._observed_commit(request, url, method, rule)
            elif unsafe and self.observing():
                self._observed_request(request, url, method, rule)
            if rule is not None and rule.effect == "write" and gate is not None and gate is self._gate:
                with contextlib.suppress(Exception):  # the act's report says it left (a like, a follow)
                    gate.left(rule)  # type: ignore[attr-defined]
            return False
        self._record(url, method, refusal, kind=self._kind_of(request, nav), prevented=True, rule=rule, frame=frame)
        await self._abort(route, request, "aborted" if nav else "blockedbyclient")
        return True

    async def _effects_only(self, route: Any, request: Any, url: str, method: str, nav: bool,
                            rule: EffectRule | None, headers: dict[str, str]) -> bool:
        """The route's effects mode (a keepalive visit, acting(effects_route=True)): abort what the gate
        would hold, classified the same way (_gate_rule): a write or commit the grant refuses, a PUT or a
        POST no rule names. Let everything else go to the listener untouched. Nothing is gated, charged
        or observed here: no approval is asked for the node, and the listener records what goes."""
        rule = self._gate_rule(rule, request, url, method, headers)
        if rule is None or rule.effect not in ("commit", "write"):
            return False
        frame = nav and _in_subframe(request)
        refusal = judge(url, method, navigation=nav, sites=self.sites, driver=self.driver, human=self._human(),
                        effect=rule, frame=frame, asks_first=self._asks_first, exposed=self._exposed())
        if refusal is None:
            return False
        self._record(url, method, refusal, kind=self._kind_of(request, nav), prevented=True, rule=rule, frame=frame)
        await self._abort(route, request, "aborted" if nav else "blockedbyclient")
        return True

    # ---- the listener (containment) ------------------------------------------------------

    def _on_request(self, request: Any) -> None:
        if self._live_at is None:
            with contextlib.suppress(Exception):
                if request.resource_type in LIVE_TYPES:
                    self._live_at = time.monotonic()
        try:
            method = request.method
            nav = request.is_navigation_request()
            if method.upper() not in SAFE_METHODS:
                self._note_sender(request)  # the action ledger's actor is who drove the lane now
            if self._gate is not None and method.upper() not in SAFE_METHODS:
                self._act_non_get += 1  # what settle() waits on
                self._act_last = time.monotonic()
                self._inflight.add(request)
            if not nav and method.upper() in SAFE_METHODS:
                return
            url = request.url
            hop = request.redirected_from is not None
        except Exception:  # noqa: BLE001 - a request that went away
            return
        rule = None
        headers: dict[str, str] = {}
        if method.upper() not in SAFE_METHODS:
            try:
                headers = request.headers
            except Exception:  # noqa: BLE001
                headers = {}
            rule = classify(url, method, headers, body=lambda: _post_data(request), rules=self.rules)
        if rule is not None and rule.kind == "draft":
            self._learn_upload()
            self._upload(request, url, headers, rule)  # on every path (a strict or capped act too): media binds by file
        if rule is not None and rule.effect == "write":
            self._hit(rule, url, method)  # commits leave observed_commit or blocked rows; writes leave this one
        if self._windows and not hop:
            return  # the strict route judges it (redirect hops are never routed)
        routine = self._flow_owns(request)
        if routine and not hop and self._flow_commit_route is not None and method.upper() not in SAFE_METHODS \
                and self._flow_commit_route.match(url) and not self._human() \
                and self._flow_refusal(rule, request, url, method, headers) is not None:
            return  # the connector run's route aborted it before it left (scope_flow)
        if self._drive_route is not None and not hop and not routine and method.upper() not in SAFE_METHODS \
                and self._drive_route.match(url):
            return  # the commit gate's route judges it
        human = self._human()
        if hop and not human and not routine and self._drive is not None and rule is not None \
                and rule.effect == "commit":
            # a redirect hop is never routed: a publish that arrived by one went past the gate
            block = self._record(url, method, Refusal(
                "commit_gate", f"a redirect carried a publish ({rule.id}) past the commit gate, so the lane is held"),
                kind="redirect" if nav else "request", prevented=False, rule=rule)
            self._observed_commit(request, url, method, rule)
            self.stats["unblocked"] += 1
            if nav:  # the navigation it carried is stopped, as the listener stops any refused one
                self._spawn(self._stop(request, url))
            if self.on_violation is not None:
                self._spawn(self._violation(block))
            return
        frame = nav and _in_subframe(request)
        # a flow run is not the agent: it was authorized at its own effect, so the agent's grant (its
        # ceiling, its egress list) does not judge what it sends; the lane's site list still does, for
        # its documents and its non-GETs alike (flow=True: no driver, yet the list holds)
        refusal = judge(url, method, navigation=nav, sites=self.sites, driver=None if routine else self.driver,
                        human=human, effect=rule, frame=frame, asks_first=self._asks_first, flow=routine,
                         exposed=self._exposed())
        if refusal is not None and self._effect_route is not None and not hop and rule is not None \
                and rule.effect in ("commit", "write") and self._effect_route.match(url):
            return  # the route's effects mode aborted it before it left (_effects_only)
        if refusal is None and rule is not None and rule.effect == "commit" and not human:
            over = self._charge(rule, sent=True)
            if over is not None:  # it left past a limit: counted, recorded and the lane held
                self._observed_commit(request, url, method, rule)
                block = self._record(url, method, over, kind=self._kind_of(request, nav), prevented=False, rule=rule)
                self.stats["unblocked"] += 1
                if self.on_violation is not None:
                    self._spawn(self._violation(block))
                return
        if refusal is None:
            if rule is not None and rule.effect == "commit":
                self._observed_commit(request, url, method, rule)
            elif method.upper() not in SAFE_METHODS and self.observing():
                self._observed_request(request, url, method, rule)
            gate = self._gate
            if rule is None and not human and method.upper() not in SAFE_METHODS:
                host = host_of(url) or ""
                if gate is not None and gate.submit_like and self._lane_host(host):
                    self._unmatched(request, url, method)
                elif self._unruled(host):
                    self._unruled_request(request, url, method, flow=routine)
            if human and nav and self.sites:
                self._note_human(url, method, self._kind_of(request, nav), frame=frame)
            return
        kind = self._kind_of(request, nav)
        block = self._record(url, method, refusal, kind=kind, prevented=False, rule=rule, frame=frame)
        if kind == "popup":  # its page may show up before or after this: close it either way
            self._popup_blocks.append((self.clock(), url))
        if nav:
            self._spawn(self._stop(request, url))
        else:
            self.stats["unblocked"] += 1
        if refusal.rule in ("commit", "write", "ask_first") and self.on_violation is not None:
            self._spawn(self._violation(block))

    def _done(self, request: Any) -> None:
        """A request finished or failed: settle stops waiting for it, and counts quiet from now (what
        a page sends next, such as the post after its upload, follows the answer)."""
        if request in self._inflight:
            self._inflight.discard(request)
            self._act_last = time.monotonic()

    def _effect_done(self, request: Any) -> None:
        """A request finished (the site answered): each action it carries (effects_of: every
        operation of a batch) goes to on_effect."""
        if self.on_effect is None:
            return
        try:
            method = request.method
            if method.upper() in SAFE_METHODS:
                return
            # a redirect chain is one action: whichever of its requests finishes first and names one
            # counts (a hop's requestfinished can come before its redirected_to is set)
            root, hops = request, 0
            while getattr(root, "redirected_from", None) is not None and hops < 20:
                root, hops = root.redirected_from, hops + 1
            if self._counted.get(id(root)) is root:
                return
            url, headers = request.url, request.headers
            found = effects_of(url, method, headers, body=lambda: _post_data(request), rules=self.rules)
            if not found:
                return
            self._counted[id(root)] = root
            while len(self._counted) > ABORTED_KEEP:
                self._counted.pop(next(iter(self._counted)))
            # who drove the lane when it was sent (_on_request noted it), not when it finished: a
            # person taking the lane mid-request does not make an agent's like theirs
            sent = self._sent_by.get(id(request))
            actor = sent[1] if sent is not None and sent[0] is request else self._actor(request)
            for rule in found:
                self.on_effect(rule, actor)
        except Exception:
            log.warning("%s: could not count a finished request", self.lane_id, exc_info=True)

    def _actor(self, request: Any) -> str:
        return "person" if self._human() else "flow" if self._flow_owns(request) else "agent"

    def _note_sender(self, request: Any) -> None:
        if self.on_effect is None:
            return
        try:
            self._sent_by[id(request)] = (request, self._actor(request))
        except Exception:  # noqa: BLE001 - it is decided at the answer instead
            return
        while len(self._sent_by) > SENT_KEEP:
            self._sent_by.pop(next(iter(self._sent_by)))

    def _effect_failed(self, request: Any) -> None:
        """A request failed. The site may still have acted on it (the page navigated away or the
        connection dropped after the body left), so it is counted like a finished one, unless this
        guard aborted it itself (a refusal: it never left) or its failure proves it never reached
        the site (never_sent: no name, no connection). Over-counting ends a day early; missing one
        would let the account past the owner's cap."""
        if self.on_effect is None:
            return
        if self._aborted.pop(id(request), None) is request:
            return
        try:
            failure = request.failure
            if callable(failure):
                failure = failure()
        except Exception:  # noqa: BLE001 - no failure text: in doubt, it counts
            failure = None
        if never_sent(failure if isinstance(failure, str) else None):
            return
        self._effect_done(request)

    async def _abort(self, route: Any, request: Any, reason: str) -> None:
        """Abort a request this guard refused, and remember it: its requestfailed is not an action."""
        self._aborted[id(request)] = request
        while len(self._aborted) > ABORTED_KEEP:
            self._aborted.pop(next(iter(self._aborted)))
        with contextlib.suppress(Exception):
            await route.abort(reason)

    def _gate_rule(self, rule: EffectRule | None, request: Any, url: str, method: str,
                   headers: dict[str, str]) -> EffectRule | None:
        """While an agent drives with the commit gate: classify fail closed. A request a strict_ops
        read rule matched stays a read only when every operation in it is a known read
        (gql_verdict); a non-GET to a rule host that no rule names (a PUT, PATCH or DELETE always, a
        POST while `gate_unknown_posts`) is held as `<site>.unknown`, which the gate treats as a
        possible publish. A read the owner promoted for a request that names no operation stays one
        only for a body it covers (body_covered: its field names, no operation). Inside the vault's
        login window a POST that would be held only for those reasons goes on (_in_login). Each
        decision about a shape no rule lists is traced (gate_classified)."""
        host = host_of(url) or ""
        login = self._in_login(rule, request, url, method, headers)
        if rule is not None and rule.effect == "read" and rule.strict_ops:
            read, why = gql_verdict(rule.site, headers, _post_data(request),
                                    extra=self._read_ops.get((host, norm_path(url))))
            if why != "a known read":
                self._classified(request, url, method, rule, "read" if read else "login" if login else "gated", why)
            if read or login:
                return rule
            return unknown_rule(self.rules, host, url, method) or rule
        if rule is not None and rule.effect == "read" and rule.fields is not None:
            covered, why = body_covered(rule.fields, headers, _post_data(request))
            if not covered:
                self._classified(request, url, method, rule, "login" if login else "gated", why)
                return rule if login else unknown_rule(self.rules, host, url, method) or rule
        if rule is not None and rule.effect == "read" and rule.untyped and self.carries_typed(request) is not None:
            # a telemetry or plumbing read that carries the agent's words is no longer only that
            self._classified(request, url, method, rule, "gated", "a read's body carries text the agent typed")
            return unknown_rule(self.rules, host, url, method) or rule
        if rule is not None and rule.effect == "read" and rule.fields is not None:
            return rule
        if rule is None and method.upper() not in SAFE_METHODS:
            unknown = unknown_rule(self.rules, host, url, method)
            if unknown is None:
                return None
            if login:
                self._classified(request, url, method, None, "login", f"a {method.upper()} no rule names")
                return None
            if method.upper() in _PUT_LIKE or self.gate_unknown_posts:
                self._classified(request, url, method, None, "gated", f"a {method.upper()} no rule names")
                return unknown
            self._classified(request, url, method, None, "let go", f"a {method.upper()} no rule names")
        return rule

    # ---- the vault's login window ----------------------------------------------------------

    def open_login(self, domain: str) -> None:
        """The vault is filling a login field of `domain`, its entry's registrable domain, on this lane:
        open that domain's login window, or renew it (a login of several steps). Called by the typist
        (agentos.node.typist) only, after the entry passed the vault's checks, so a page, a model or a
        tool argument cannot reach it. See the module docstring."""
        self._login_until[domain] = self.clock() + self.login_window_s

    def close_login(self, domain: str) -> None:
        """The fill failed: no window stays open for it, an earlier fill's included."""
        self._login_until.pop(domain, None)

    def login_open(self, host: str) -> bool:
        """Is a login window open for this host's site now? The devtools capture asks, so the answer
        to a post sent inside one (it can carry the session) is never returned (agentos.node.devtools)."""
        now = self.clock()
        open_for = [d for d, until in self._login_until.items() if now < until]
        if not open_for or not host:
            return False
        try:
            site = _sites.registrable_domain(host)
        except ValueError:
            return True  # a host the window cannot be told apart from: treated as inside it
        if site is None:  # no public suffix to read a site from: the domain itself or below it
            return any(host == d or host.endswith("." + d) for d in open_for)
        return any(_sites.same_site(site, domain) for domain in open_for)

    def _in_login(self, rule: EffectRule | None, request: Any, url: str, method: str,
                  headers: dict[str, str]) -> bool:
        """Is this POST inside an open login window, so the gate need not hold it for a shape it cannot
        read? Never for what a commit or write rule names: classify reads one operation, so every
        operation of a batch is checked here. A body that cannot be read as operations is a login's
        only where no read catch-all matched: on a GraphQL endpoint it could carry anything."""
        method = method.upper()
        if method != "POST" or not self._login_until:
            return False
        now = self.clock()
        self._login_until = {d: until for d, until in self._login_until.items() if now < until}
        host = host_of(url) or ""
        try:
            site = _sites.registrable_domain(host)
        except ValueError:
            return False
        if not any(_sites.same_site(site, domain) for domain in self._login_until):
            return False
        ops = operations(headers, _post_data(request))
        if ops is None:
            return rule is None
        low, path = {k.lower(): v for k, v in headers.items()}, norm_path(url)
        return not any(r.effect in ("commit", "write") and r.matches(host, path, method, low, lambda n=op["name"]: n)
                       for op in ops for r in self.rules)

    def _classified(self, request: Any, url: str, method: str, rule: EffectRule | None, decision: str,
                    why: str) -> None:
        """Trace how the gate classified a shape no rule lists (`gate_classified`: names only, one row
        per shape a second), so the owner can add the rule."""
        body, headers = self._body(request)
        shape = request_shape(headers, body)
        host, path = host_of(url) or "?", _path(url)
        key = ("classified", host, path, decision, str(shape.get("op") or ""), hash(tuple(shape.get("fields") or ())))
        now = self.clock()
        if now - self._observed.get(key, -1e9) < OBSERVE_EVERY_S:
            return
        self._observed[key] = now
        log.info("%s: the commit gate classified %s %s%s as %s: %s", self.lane_id, method.upper(), host, _short(path),
                 decision, why)
        if self.trace is not None:
            self.trace.write({"kind": "gate_classified", "lane": self.lane_id, "host": host, "path": path,
                              "method": method.upper(), "decision": decision, "why": why,
                              "rule": rule.id if rule is not None else None, "size": len(body) if body else 0,
                              "shape": shape, "driver": self.driver.public() if self.driver else None})

    def _charge(self, rule: EffectRule, *, sent: bool, meter: CommitMeter | None = None) -> Refusal | None:
        """A publish during an agent's act: one commit on its meter (CommitMeter.charge, `meter` or
        the current act's). A refusal when it passes a limit; None within the limits, or outside an act."""
        meter = meter if meter is not None else self._meter
        if meter is None:
            return None
        try:
            why = meter.charge(rule, sent=sent)
        except Exception:  # metering never breaks the guard
            log.warning("%s: could not charge a commit (%s)", self.lane_id, rule.id, exc_info=True)
            return None
        if not why:
            return None
        code = "lane_paced" if getattr(meter, "code", None) == "lane_paced" else "limit_reached"
        return Refusal(code, f"this publishes ({rule.id}): {why.rstrip('.')}")

    async def _hold(self, gate: PublishGate, rule: EffectRule, request: Any, url: str, method: str) -> Refusal | None:
        """Ask the commit gate about a publish. A gate that fails refuses: nothing leaves unjudged."""
        self.stats["gated"] += 1
        try:
            return await gate.hold(rule, request, url, method)
        except Exception:
            log.warning("%s: the commit gate failed on %s; the publish is aborted", self.lane_id, rule.id,
                        exc_info=True)
            return Refusal("commit_gate", f"this publishes ({rule.id}) and the commit gate could not judge it")

    def _kind_of(self, request: Any, nav: bool) -> str:
        if not nav:
            return "request"
        try:
            if request.redirected_from is not None:
                return "redirect"
            frame = request.frame
        except Exception:  # noqa: BLE001 - a popup's first request comes before its frame
            return "popup"
        return "navigation" if frame.parent_frame is None else "subframe"

    async def _stop(self, request: Any, url: str) -> None:
        try:
            frame = request.frame
        except Exception:  # noqa: BLE001 - a popup: close it if it is already open (_adopt closes later ones)
            for page in list(getattr(self._context, "pages", ())):
                if page.url in ("about:blank", "") and page.main_frame is not None:
                    await self._close_popup(page)
            return
        try:
            await asyncio.wait_for(frame.evaluate("window.stop()"), STOP_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - the frame went away; on_committed is the backstop
            log.debug("%s: stopping a refused navigation: %s", self.lane_id, _first_line(e))

    async def _close_popup(self, page: Any) -> None:
        try:
            opener = await asyncio.wait_for(page.opener(), STOP_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            opener = None
        if opener is None:
            return  # a tab the lane opened itself, not a popup a page opened
        if page.url not in ("about:blank", "") and not any(page.url == u for _, u in self._popup_blocks):
            return
        with contextlib.suppress(Exception):
            await page.close()
            log.info("%s: closed a popup that opened a refused host", self.lane_id)

    def _on_committed(self, page: Any, frame: Any) -> None:
        """Backstop: a refused document that committed anyway (the stop lost the race)."""
        try:
            if frame.parent_frame is not None:
                return
            url = frame.url
        except Exception:  # noqa: BLE001
            return
        refusal = judge(url, "GET", navigation=True, sites=self.sites, driver=self.driver, human=self._human(),
                        exposed=self._exposed())
        if refusal is None:
            return
        self._record(url, "GET", refusal, kind="committed", prevented=False)
        self._spawn(self._leave(page))

    async def _leave(self, page: Any) -> None:
        try:
            back = await page.go_back(timeout=10_000)
            if back is None:
                await page.goto("about:blank", timeout=10_000)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: could not leave a refused page: %s", self.lane_id, _first_line(e))

    async def _violation(self, block: Block) -> None:
        try:
            result = self.on_violation(block) if self.on_violation is not None else None
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.warning("%s: could not hold the lane after a refused %s", self.lane_id, block.rule, exc_info=True)

    def _event(self, kind: str, detail: dict[str, Any]) -> None:
        if self.events is None:
            return
        try:
            self.events(self.lane_id, kind, detail)
        except Exception:
            log.warning("%s: could not record a %s event", self.lane_id, kind, exc_info=True)

    def _spawn(self, coro: Any) -> asyncio.Task | None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no loop (a sync test): nothing to stop
            coro.close()
            return None
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def summary(self) -> dict[str, Any]:
        return {"sites": list(self.sites), "strict": self.strict, "driver": self.driver.public() if self.driver else None,
                **self.stats}
