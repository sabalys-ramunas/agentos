"""The dispatcher: every control of design section 4.3.6, each decision recorded with a reason.

When an event matches a route (`offer`):

  loop guard   cause.depth above 3 is dropped; a route never fires on events its own
               target caused unless `allow_self`; turn.finished never wakes the same lane
  dedupe       the same `facts.subject` within `dedupe_window_s` is one delivery
  backpressure past 50 open deliveries for one target: merge into the newest, else drop
               only `low` priority (one notification); normal events are never dropped
  digest       quiet hours with mode `digest`: one delivery at the end of the window
  batching     deliveries within `batch.window_s` merge into one, up to `max_items`

When a delivery is due (`tick`), in this order:

  1 the route is enabled and not paused; the target's lanes are not held
  4 quiet hours (route, else node default 22:00 to 08:00 Europe/Vilnius): delay or drop;
    `urgent` ignores them
  5 rate limits per route and per agent lane (6 per hour, 48 per day): wait, never drop
  7 concurrency: `concurrency` active deliveries per route (default 1), one turn per agent lane
  then deliver; a failed attempt retries with backoff up to `max_attempts`, then a dead letter

Circuit breaker: more than 30 events a minute on the node, or 3 failed deliveries in a row on
one route or agent lane, pauses the routes involved with one push; only the owner resumes.
Priority orders what is due: urgent (owner messages), approvals decided, normal, low.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agentos.events.models import (
    MAX_DEPTH,
    OPEN_STATES,
    PRIORITY_RANK,
    AgentTarget,
    Delivery,
    DeliveryState,
    Event,
    QuietHours,
    Route,
    WorkflowTarget,
)
from agentos.events.store import EventStore
from agentos.events.targets import Finish, Handoff, Targets
from agentos.runner.util import maybe_await, new_id

log = logging.getLogger("agentos.events")

HOLD_STATES = {"human": "a human has it", "paused": "agents are stopped there"}


@dataclass
class EventSettings:
    quiet_hours: QuietHours = field(default_factory=QuietHours)  # open question 8
    max_depth: int = MAX_DEPTH
    backlog_max: int = 50
    storm_per_minute: int = 30
    failures_to_pause: int = 3
    lane_turns_per_hour: int = 6   # open question 9
    lane_turns_per_day: int = 48
    content_ttl_days: int = 7
    retention_days: int = 30
    retry_base_s: float = 60.0
    retry_max_s: float = 3600.0
    digest_max_items: int = 200
    purge_every_s: float = 3600.0
    renotify_s: float = 6 * 3600.0


def _hhmm(when: datetime, tz: str) -> str:
    return when.astimezone(ZoneInfo(tz)).strftime("%H:%M")


class Dispatcher:
    def __init__(self, store: EventStore, targets: Targets, *, runtime: Any, clock: Any,
                 settings: EventSettings | None = None):
        self.store, self.targets, self.runtime, self.clock = store, targets, runtime, clock
        self.settings = settings or EventSettings()
        self._last_purge: datetime | None = None
        self._background: set[asyncio.Task] = set()
        self.notifications: list[dict[str, Any]] = []  # what was pushed, newest last (for the panel and tests)

    # ------------------------------------------------------------ helpers

    def _svc(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    def quiet(self, route: Route) -> QuietHours:
        return route.controls.quiet_hours or self.settings.quiet_hours

    @staticmethod
    def _quiet_end(route: Route, q: QuietHours, now: datetime) -> datetime | None:
        """A workflow does not wait for the node's quiet hours (they keep agents from waking at
        night): an order at 23:00 is handled at 23:00, unless its own route says otherwise."""
        if isinstance(route.target, WorkflowTarget) and route.controls.quiet_hours is None:
            return None
        return q.window_end(now)

    @staticmethod
    def target_key(route: Route) -> str:
        return route.target_lane or route.id

    def _new(self, route: Route, event: Event, now: datetime, *, state: DeliveryState = DeliveryState.PENDING,
             reason: str | None = None, due_at: datetime | None = None, open_until: datetime | None = None,
             max_items: int = 1) -> Delivery | None:
        prio = "urgent" if event.priority == "urgent" else route.priority
        rank = 1 if event.source == "approval" and prio == "normal" else PRIORITY_RANK[prio]
        d = Delivery(id=new_id("dl"), route_id=route.id, event_ids=[event.id], key=f"{route.id}:{event.id}",
                     due_at=due_at or now, open_until=open_until, max_items=max_items, priority=prio, rank=rank,
                     subject=event.subject, target_kind=route.target.kind, created_at=now, updated_at=now)
        d.note(now, state, reason)
        if state.terminal:
            d.finished_at = now
        if not self.store.insert_delivery(d, target_lane=route.target_lane):
            return None  # this event already reached this route
        return d

    def _save(self, d: Delivery, route: Route | None, *, expect: Any = None) -> bool:
        lane = route.target_lane if route is not None else None
        return self.store.save_delivery(d, target_lane=lane, expect=expect)

    def _join(self, route: Route, d: Delivery, event: Event, now: datetime, why: str) -> Delivery | None:
        if not self.store.add_item(d, event.id, target_lane=route.target_lane):
            return None
        d.history.append({"at": now.isoformat(), "state": d.state.value, "reason": f"{event.id} {why}"})
        del d.history[:-30]
        self._save(d, route)
        return d

    # ------------------------------------------------------------ offer: match-time controls

    def offer(self, route: Route, event: Event, now: datetime) -> Delivery | None:
        drop = self._loop_guard(route, event)
        if drop:
            return self._new(route, event, now, state=DeliveryState.DROPPED, reason=drop)
        c = route.controls
        if c.dedupe_window_s and event.subject is not None:
            prev = self.store.recent_subject(route.id, event.subject, now - timedelta(seconds=c.dedupe_window_s))
            if prev is not None:
                return self._new(route, event, now, state=DeliveryState.DROPPED,
                                 reason=f"dedupe: same subject as {prev.id} within {c.dedupe_window_s}s")
        key = self.target_key(route)
        backlog = (self.store.count_deliveries(states=OPEN_STATES, target_lane=key) if route.target_lane
                   else self.store.count_deliveries(states=OPEN_STATES, route_id=route.id))
        if backlog >= self.settings.backlog_max:
            newest = self.store.newest_open(route.id)
            if newest is not None and len(newest.event_ids) < self.settings.digest_max_items:
                joined = self._join(route, newest, event, now, "merged by backpressure")
                if joined is not None:
                    return joined
            if route.priority == "low" and event.priority != "urgent":
                self._notify_once(f"backpressure:{route.id}", f"{route.workspace}: trigger events dropped",
                                  f'Trigger "{route.name}" has {backlog} deliveries waiting, so low priority '
                                  "events are being dropped. Review it in Needs you.", route)
                return self._new(route, event, now, state=DeliveryState.DROPPED,
                                 reason=f"backpressure: {backlog} deliveries already wait for {key}")
        q = self.quiet(route)
        end = self._quiet_end(route, q, now) if event.priority != "urgent" and route.priority != "urgent" else None
        if end is not None and q.mode == "digest":
            open_ = self.store.open_batch(route.id, now)
            if open_ is not None and open_.open_until == end and len(open_.event_ids) < open_.max_items:
                joined = self._join(route, open_, event, now, "joined the quiet hours digest")
                if joined is not None:
                    return joined
            return self._new(route, event, now, due_at=end, open_until=end, max_items=self.settings.digest_max_items,
                             reason=f"digest at the end of quiet hours ({_hhmm(end, q.tz)})")
        if c.batch is not None:
            open_ = self.store.open_batch(route.id, now)
            if open_ is not None and len(open_.event_ids) < open_.max_items:
                joined = self._join(route, open_, event, now, "joined the batch")
                if joined is not None:
                    return joined
            until = now + timedelta(seconds=c.batch.window_s)
            return self._new(route, event, now, due_at=until, open_until=until, max_items=c.batch.max_items,
                             reason=f"batch window open until {until.isoformat(timespec='seconds')}")
        return self._new(route, event, now)

    def _loop_guard(self, route: Route, event: Event) -> str | None:
        cause = event.cause
        if cause.depth > self.settings.max_depth:
            return f"loop guard: cause depth {cause.depth} is above {self.settings.max_depth}"
        if route.controls.allow_self:
            return None
        lane = route.target_lane
        if cause.route == route.id:
            return "loop guard: caused by this trigger's own delivery"
        if lane and cause.lane == lane:
            return f"loop guard: caused by this trigger's own target {lane}"
        if event.source == "agent" and lane and event.lane == lane:
            return f"loop guard: a turn on {lane} never wakes {lane} again"
        return None

    # ------------------------------------------------------------ circuit breaker

    def storm_check(self, now: datetime, matched: list[str]) -> None:
        """More than storm_per_minute events in a minute (owner messages excluded) pauses the
        routes that took deliveries in that minute, with one push per storm."""
        n = self.store.count_events_since(now - timedelta(seconds=60), exclude_source="owner")
        if n <= self.settings.storm_per_minute:
            return
        ids = set(self.store.recent_route_ids(now - timedelta(seconds=60))) | set(matched)
        # a workflow has no general limit, and its steps cannot wake it: a busy shop is not a storm
        paused = [r for r in (self.store.get_route(i) for i in sorted(ids))
                  if r is not None and not r.state.paused and not isinstance(r.target, WorkflowTarget)]
        for r in paused:
            self.pause(r, f"event storm: {n} events in a minute", now)
        if paused:
            names = ", ".join(f'"{r.name}"' for r in paused)
            self._notify_once("storm", "Triggers paused: event storm",
                              f"{n} events arrived within a minute, so {names} paused. Review and resume them.",
                              paused[0], window_s=600)

    def pause(self, route: Route, reason: str, now: datetime) -> Route:
        route.state.paused, route.state.reason, route.state.at = True, reason, now
        route.updated_at = now
        self.store.put_route(route)
        return route

    def _failure(self, route: Route, now: datetime) -> None:
        route.state.failures_in_row += 1
        self.store.put_route(route)
        lane = route.target_lane if isinstance(route.target, AgentTarget) else None
        lane_n = 0
        if lane:
            lane_n = int(self.store.get_meta(f"lane_failures:{lane}") or 0) + 1
            self.store.set_meta(f"lane_failures:{lane}", lane_n)
        limit = self.settings.failures_to_pause
        hit: list[Route] = []
        if route.state.failures_in_row >= limit and not route.state.paused:
            hit.append(self.pause(route, f"{route.state.failures_in_row} failed deliveries in a row", now))
        if lane and lane_n >= limit:
            for r in self.store.list_routes(workspace=route.workspace):
                if r.target_lane == lane and not r.state.paused:
                    hit.append(self.pause(r, f"{lane_n} failed turns in a row on {lane}", now))
        if hit:
            n = max(route.state.failures_in_row, lane_n)
            self._notify_once(f"failures:{lane or route.id}", f"{route.workspace} triggers paused",
                              f"{', '.join(chr(34) + r.name + chr(34) for r in hit)} paused: {n} failed "
                              "deliveries in a row. Review and resume them.", route)

    def _success(self, route: Route | None) -> None:
        if route is None:
            return
        if route.state.failures_in_row:
            route.state.failures_in_row = 0
            self.store.put_route(route)
        if isinstance(route.target, AgentTarget):
            self.store.set_meta(f"lane_failures:{route.target.lane}", 0)

    # ------------------------------------------------------------ notifications

    def _notify_once(self, key: str, title: str, body: str, route: Route | None, *, window_s: float | None = None) -> bool:
        """One push per incident: the same key is quiet for `renotify_s` (or `window_s`)."""
        now = self.clock.now()
        last = self.store.get_meta(f"notified:{key}")
        if last and (now - datetime.fromisoformat(last)).total_seconds() < (window_s or self.settings.renotify_s):
            return False
        self.store.set_meta(f"notified:{key}", now.isoformat())
        self.notify(title, body, route)
        return True

    def notify(self, title: str, body: str, route: Route | None) -> None:
        data = {"route_id": route.id, "trigger": route.name, "workspace": route.workspace} if route else {}
        self.notifications.append({"title": title, "body": body, "data": data})
        del self.notifications[:-100]
        notifier = self._svc("notifier")
        try:
            if callable(getattr(notifier, "notify", None)):
                out = notifier.notify(title=title, body=body, level="warning", data=data)
                if inspect.isawaitable(out):
                    try:
                        task = asyncio.get_running_loop().create_task(maybe_await(out))
                    except RuntimeError:  # no loop (a CLI call): the push is lost, the log keeps it
                        out.close()
                        log.warning("%s: %s", title, body)
                    else:
                        self._background.add(task)
                        task.add_done_callback(self._background.discard)
            elif callable(getattr(notifier, "send", None)):
                notifier.send({"type": "trigger.alert", "title": title, "body": body, **data})
            else:
                log.warning("%s: %s", title, body)
        except Exception:
            log.exception("notifier failed")

    # ------------------------------------------------------------ tick: dispatch-time controls

    async def tick(self) -> dict[str, int]:
        now = self.clock.now()
        stats = {"delivered": 0, "waiting": 0, "dropped": 0, "finished": 0, "dead": 0}
        for d in self.store.list_deliveries(states=[DeliveryState.DELIVERED], limit=500):
            fin = self.targets.poll(d)
            if fin is not None:
                self.finish(d, fin, now)
                stats["finished"] += 1
        self._max_wait(now)
        for d in self.store.list_deliveries(states=OPEN_STATES, due_before=now, by_due=True, limit=200):
            try:
                state = await self._dispatch(d, now)
            except Exception:
                log.exception("delivery %s failed to dispatch", d.id)
                continue
            if state == DeliveryState.DELIVERED:
                stats["delivered"] += 1
            elif state == DeliveryState.WAITING:
                stats["waiting"] += 1
            elif state == DeliveryState.DROPPED:
                stats["dropped"] += 1
            elif state in (DeliveryState.DEAD, DeliveryState.FAILED):
                stats["dead"] += 1
        self._purge(now)
        return stats

    def _wait(self, d: Delivery, route: Route | None, reason: str, until: datetime, now: datetime) -> DeliveryState:
        if d.reason != reason or d.state != DeliveryState.WAITING:
            d.note(now, DeliveryState.WAITING, reason)
        d.due_at, d.updated_at = until, now
        d.open_until = None
        self._save(d, route, expect=OPEN_STATES)
        return DeliveryState.WAITING

    def _end(self, d: Delivery, route: Route | None, state: DeliveryState, reason: str | None, now: datetime,
             *, expect: Any = OPEN_STATES) -> DeliveryState:
        d.note(now, state, reason)
        d.finished_at = now
        d.open_until = None
        self._save(d, route, expect=expect)
        return state

    async def _dispatch(self, d: Delivery, now: datetime) -> DeliveryState:
        route = self.store.get_route(d.route_id)
        if route is None:
            return self._end(d, None, DeliveryState.DROPPED, "the trigger was removed", now)
        if not route.enabled:
            return self._end(d, route, DeliveryState.DROPPED, "the trigger is turned off", now)
        if route.state.paused:
            return self._wait(d, route, f"the trigger is paused ({route.state.reason}); the owner resumes it",
                              now + timedelta(days=1), now)
        events = self.store.events(d.event_ids)
        if not events:
            return self._end(d, route, DeliveryState.DROPPED, "its events are no longer on this node", now)
        held = self._held(route)
        if held:
            return self._wait(d, route, held, now + timedelta(minutes=5), now)
        if d.priority != "urgent":
            q = self.quiet(route)
            end = self._quiet_end(route, q, now)
            if end is not None:
                if q.mode == "drop":
                    return self._end(d, route, DeliveryState.DROPPED, "quiet hours (this trigger drops events then)",
                                     now)
                return self._wait(d, route, f"quiet hours until {_hhmm(end, q.tz)}", end, now)
        rate = self._rate(route, events, now)
        if rate is not None:
            return self._wait(d, route, rate[0], rate[1], now)
        active = self.store.count_deliveries(states=[DeliveryState.DELIVERED], route_id=route.id)
        if active >= route.controls.concurrency:
            return self._wait(d, route, f"{active} delivery of this trigger is still running",
                              now + timedelta(seconds=30), now)
        if isinstance(route.target, AgentTarget):
            busy = self.store.count_deliveries(states=[DeliveryState.DELIVERED], target_lane=route.target.lane)
            if busy:
                return self._wait(d, route, f"a turn is already running on {route.target.lane}",
                                  now + timedelta(seconds=30), now)
        d.attempts += 1
        try:
            h = await self.targets.deliver(route, d, events)
        except Exception as e:  # a target bug is a failed attempt, not a stuck queue
            log.exception("target failed for delivery %s", d.id)
            h = Handoff("retry", f"the target raised {type(e).__name__}")
        return self._apply(route, d, h, now)

    def _apply(self, route: Route, d: Delivery, h: Handoff, now: datetime) -> DeliveryState:
        if h.kind == "delivered":
            d.target_ref, d.grant_id, d.delivered_at = h.ref, h.grant_id or d.grant_id, now
            d.note(now, DeliveryState.DELIVERED, h.reason or f"handed to the {route.target.kind} target "
                   f"(attempt {d.attempts})")
            d.open_until = None
            self._save(d, route, expect=OPEN_STATES)
            return DeliveryState.DELIVERED
        if h.kind == "wait":
            d.attempts -= 1
            return self._wait(d, route, h.reason or "the target is not ready", h.until or now + timedelta(minutes=1),
                              now)
        if h.kind == "fail":
            self._failure(route, now)
            self._dead_letter(route, d, h.reason or "the target refused", now, DeliveryState.FAILED)
            return DeliveryState.FAILED
        return self._retry(route, d, h.reason or "the attempt failed", now, expect=OPEN_STATES)

    def _retry(self, route: Route, d: Delivery, reason: str, now: datetime, *, expect: Any) -> DeliveryState:
        self._failure(route, now)
        if d.attempts >= route.controls.max_attempts:
            self._dead_letter(route, d, f"attempt {d.attempts} failed: {reason}", now, DeliveryState.DEAD, expect=expect)
            return DeliveryState.DEAD
        delay = min(self.settings.retry_max_s, self.settings.retry_base_s * 2 ** (d.attempts - 1))
        d.note(now, DeliveryState.WAITING, f"attempt {d.attempts} failed: {reason}; retrying")
        d.due_at, d.target_ref = now + timedelta(seconds=delay), None
        self._save(d, route, expect=expect)
        return DeliveryState.WAITING

    def _dead_letter(self, route: Route, d: Delivery, reason: str, now: datetime, state: DeliveryState,
                     *, expect: Any = OPEN_STATES) -> None:
        self._end(d, route, state, reason, now, expect=expect)
        self._notify_once(f"dead:{route.id}", f"{route.workspace}: a trigger needs you",
                          f'Trigger "{route.name}" could not deliver an event: {reason[:200]}. It is in Needs you.',
                          route)

    def finish(self, d: Delivery, fin: Finish, now: datetime) -> None:
        """A delivered delivery's target finished (polled, or reported by an agent lane)."""
        route = self.store.get_route(d.route_id)
        d.outcome = fin.outcome
        if fin.kind == "done":
            self._end(d, route, DeliveryState.DONE, fin.reason or f"the target finished: {fin.outcome}", now,
                      expect=[DeliveryState.DELIVERED])
            self._success(route)
            return
        if route is None:
            self._end(d, None, DeliveryState.DEAD, fin.reason, now, expect=[DeliveryState.DELIVERED])
            return
        if fin.kind == "fail":
            self._failure(route, now)
            self._dead_letter(route, d, fin.reason or fin.outcome, now, DeliveryState.FAILED,
                              expect=[DeliveryState.DELIVERED])
            return
        self._retry(route, d, fin.reason or fin.outcome, now, expect=[DeliveryState.DELIVERED])

    def _held(self, route: Route) -> str | None:
        """Why the target must wait because a lane it acts on is held, else None."""
        lanes_svc = self._svc("lanes")
        control = getattr(lanes_svc, "control", None)
        if not callable(control):
            return None
        lanes = [route.target_lane] if route.target_lane else []
        lanes += [lane for lane in route.grant.lanes if not any(ch in lane for ch in "*?[")]
        for lane in dict.fromkeys(lanes):
            try:
                c = control(lane)
            except Exception:  # noqa: BLE001, S112 - not a browser lane, or no record: nothing to enforce
                continue
            st = getattr(getattr(c, "state", None), "value", getattr(c, "state", None))
            if st in HOLD_STATES:
                why = getattr(c, "reason", None)
                return f"lane {lane} is held ({why or HOLD_STATES[st]}); it continues after hand back"
        return None

    def _rate(self, route: Route, events: list[Event], now: datetime) -> tuple[str, datetime] | None:
        """(reason, until) when a rate limit says wait. Owner messages are not paced."""
        if all(e.source == "owner" for e in events):
            return None
        limits: list[tuple[str, int | None, timedelta, dict[str, Any]]] = []
        r = route.controls.rate
        if r is not None:
            limits += [("this trigger", r.per_hour, timedelta(hours=1), {"route_id": route.id}),
                       ("this trigger", r.per_day, timedelta(days=1), {"route_id": route.id})]
        if isinstance(route.target, AgentTarget):
            lane = route.target.lane
            per_hour, per_day = self._lane_limits(lane)
            limits += [(lane, per_hour, timedelta(hours=1), {"target_lane": lane}),
                       (lane, per_day, timedelta(days=1), {"target_lane": lane})]
        for who, limit, window, key in limits:
            if not limit:
                continue
            times = self.store.delivered_since(now - window, **key)
            if len(times) >= limit:
                until = times[len(times) - limit] + window
                unit = "hour" if window == timedelta(hours=1) else "day"
                return f"rate limit: {limit} per {unit} for {who}", until
        return None

    def _lane_limits(self, lane: str) -> tuple[int, int]:
        agents = self._svc("agents")
        fn = getattr(agents, "lane_limits", None)
        if callable(fn):
            try:
                got = fn(lane)
                if got:
                    return int(got[0]), int(got[1])
            except Exception:
                log.debug("lane_limits failed for %s; using the node defaults", lane, exc_info=True)
        return self.settings.lane_turns_per_hour, self.settings.lane_turns_per_day

    def _max_wait(self, now: datetime) -> None:
        """A normal delivery waiting longer than its route's max_wait notifies once."""
        by_route: dict[str, list[Delivery]] = {}
        for d in self.store.list_deliveries(states=[DeliveryState.WAITING], limit=500):
            if d.priority == "low" or "max_wait" in d.notified:
                continue
            route = self.store.get_route(d.route_id)
            if route is None or (now - d.created_at).total_seconds() <= route.controls.max_wait_s:
                continue
            by_route.setdefault(route.id, []).append(d)
        for rid, items in by_route.items():
            route = self.store.get_route(rid)
            hours = route.controls.max_wait_s // 3600
            self.notify(f"{route.workspace}: trigger events are waiting",
                        f'{len(items)} event(s) of trigger "{route.name}" have waited over {hours} hours: '
                        f"{items[0].reason}.", route)
            for d in items:
                d.notified.append("max_wait")
                self._save(d, route)

    def wake_lane(self, lane: str, now: datetime) -> int:
        """A lane was handed back: deliveries waiting on its hold are due now."""
        n = 0
        for d in self.store.list_deliveries(states=[DeliveryState.WAITING], limit=500):
            if (d.reason or "").startswith(f"lane {lane} is held"):
                d.due_at = now
                route = self.store.get_route(d.route_id)
                if self._save(d, route, expect=[DeliveryState.WAITING]):
                    n += 1
        return n

    def resume(self, route: Route, now: datetime) -> int:
        """The owner resumed a paused route: its waiting deliveries are due now."""
        route.state.paused, route.state.reason, route.state.failures_in_row = False, None, 0
        route.updated_at = now
        self.store.put_route(route)
        if isinstance(route.target, AgentTarget):
            self.store.set_meta(f"lane_failures:{route.target.lane}", 0)
        n = 0
        for d in self.store.list_deliveries(states=[DeliveryState.WAITING], route_id=route.id, limit=1000):
            d.due_at = now
            if self._save(d, route, expect=[DeliveryState.WAITING]):
                n += 1
        return n

    def _purge(self, now: datetime) -> None:
        if self._last_purge and (now - self._last_purge).total_seconds() < self.settings.purge_every_s:
            return
        self._last_purge = now
        s = self.settings
        self.store.purge_content(now - timedelta(days=s.content_ttl_days))
        self.store.purge_deliveries(now - timedelta(days=s.retention_days))
        self.store.purge_events(now - timedelta(days=s.retention_days))
