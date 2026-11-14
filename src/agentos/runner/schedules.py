"""Schedules and triggers, pinned to this node: cron, page watches, webhooks, intervals, dead-man.

They run inside the node daemon with no agent or controller connected. Every fire goes
through RunnerService.submit with an idempotency key derived from the trigger (the cron
slot in UTC, the watch change number, the webhook delivery id), so a duplicate tick, a
restart between "fired" and "recorded", or a replayed webhook never creates a second run.

Cron semantics (wall clock in the schedule's timezone, default Europe/Vilnius):
- each local time fires at most once: in the autumn repeated hour, 03:30 fires on its
  first occurrence only;
- a local time that does not exist (the spring gap) fires once, shifted forward by the
  gap (03:30 becomes 04:30);
- missed slots (node down, clock jumped forward): `skip` fires nothing when the latest
  slot is later than the grace period, `catch_up_once` fires one run for the latest slot;
- a slot never overlaps a run of the same schedule that is still active;
- a backward clock jump resets the cursor; slots that already ran are deduplicated.

An interval (`every`, what a workflow's `site_change` trigger reads with) runs its flow every
`every_s`, one at a time: a run that would overlap the last one is skipped, and however long the
node was off, what is late runs once.

A schedule fires a flow (`flow_id`) or an events route (`route_id`). A route schedule never
queues a run itself: each fire calls the events layer's `emit` (runtime service
"event_queue") with the same key a flow run would get, so the events layer dedupes it the
same way, and the route's controls (quiet hours, rate, batching) decide what it wakes. Page
text a watch reads and a webhook's body travel as the event's untrusted content, never as a
fact.

One-off planned actions (the agenda, agentos.agenda) are not a second scheduler: they register
in `one_offs`, and each tick calls their `tick(stats)` after the schedules, so one loop fires
everything timed on the node, and their fires go through the same RunnerService.submit with an
idempotency key.

A schedule records the grant of whoever created it (the owner's grant for the panel and the
CLI); every fire, including a webhook, runs under that grant, checked when the run is queued
and again before it runs. A webhook run is deduplicated by its signed content as well as by
X-Agentos-Delivery, so a captured request replayed with a new delivery id runs nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import itertools
import json
import logging
import math
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import ValidationError

from agentos.core.errors import (
    AgentosError,
    InvalidRequest,
    LaneBusy,
    NotFound,
    PolicyDenied,
    ServiceUnavailable,
)
from agentos.core.models import Effect, Principal, RunStatus, parse_lane_id
from agentos.runner.flows import FlowService, validation_message
from agentos.runner.models import (
    ACTIVE,
    CONNECTOR_PREFIX,
    SUCCEEDED,
    TERMINAL,
    CronConfig,
    EveryConfig,
    Schedule,
    ScheduleKind,
    WatchConfig,
    WebhookConfig,
    is_connector_id,
    is_owner,
)
from agentos.runner.proof import BAD_SIGNALS
from agentos.runner.runner import OWNER_GRANT, RunnerService
from agentos.runner.store import RunnerStore
from agentos.runner.util import TEMPLATE_RE, Clock, clip_text, maybe_await, new_id, render, url_problem

# a grant refusal that means the grant is gone for good: its schedules are turned off
GRANT_ENDED = {"revoked": "was revoked", "expired": "has expired", "unknown_grant": "is not known on this node"}

log = logging.getLogger("agentos.runner")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise InvalidRequest(f"unknown timezone {name!r}") from e


def cron_slots(expr: str, tz: ZoneInfo, after: datetime, until: datetime, limit: int = 3000) -> list[datetime]:
    """UTC instants of the cron's local wall times in (after, until], deduplicated.

    Walks backwards from `until`, so the most recent slots are always present even after a
    long outage. Local times map to UTC with fold=0: a repeated time fires on its first
    occurrence, and a time inside a spring gap maps forward by the gap."""
    if until <= after:
        return []
    margin = timedelta(hours=3)  # the local to UTC mapping is not monotonic around DST changes
    it = croniter(expr, until.astimezone(tz).replace(tzinfo=None) + margin)
    out: set[datetime] = set()
    for _ in range(limit):
        local = it.get_prev(datetime)
        utc = local.replace(tzinfo=tz, fold=0).astimezone(UTC)
        if utc <= after - margin:
            break
        if after < utc <= until:
            out.add(utc)
    return sorted(out)


def _jitter(sid: str, slot: datetime, jitter_s: int) -> timedelta:
    if jitter_s <= 0:
        return timedelta(0)
    h = int(hashlib.sha256(f"{sid}:{slot.isoformat()}".encode()).hexdigest(), 16)
    return timedelta(seconds=h % (jitter_s + 1))


def shortest_gap(expr: str, tz: ZoneInfo, start: datetime, n: int = 50) -> float | None:
    """The shortest time in seconds between two of the next `n` slots, or None when it cannot be read."""
    try:
        it = croniter(expr, start.astimezone(tz).replace(tzinfo=None))
        times = [it.get_next(datetime).replace(tzinfo=tz, fold=0).astimezone(UTC) for _ in range(n)]
    except (ValueError, KeyError):
        return None
    gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(times) if b > a]
    return min(gaps) if gaps else None


def cron_can_fire(expr: str, tz: ZoneInfo, start: datetime) -> bool:
    """False for a valid-looking cron that never matches a real date (30 February)."""
    try:
        croniter(expr, start.astimezone(tz).replace(tzinfo=None)).get_next(datetime)
    except (ValueError, KeyError):
        return False
    return True


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def _webhook_extras(cfg: WebhookConfig) -> dict[str, Any]:
    """What the sender of a webhook needs, returned once when its secret is made."""
    return {"url_path": f"/api/hooks/{cfg.token}", "secret": cfg.secret,
            "signature": "X-Agentos-Signature: sha256=HMAC(secret, timestamp + '.' + body)",
            "headers": ["X-Agentos-Timestamp (unix seconds)", "X-Agentos-Delivery (optional id)"],
            "note": "The secret is shown once. Store it in the sender."}


class Scheduler:
    def __init__(self, store: RunnerStore, runner: RunnerService, flows: FlowService, *,
                 runtime: Any = None, clock: Clock | None = None, tick_s: float = 15.0):
        self.store, self.runner, self.flows, self.runtime = store, runner, flows, runtime
        self.tick_hooks: list[Any] = []  # called on every tick (a workflow's mailboxes)
        self.fire_checks: list[Any] = []  # may refuse an interval's fire, as a freeze does (a watch that stopped being a read)
        self.clock = clock or Clock()
        self.tick_s = tick_s
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._background: set[asyncio.Task] = set()
        self.one_offs: list[Any] = []  # services with `async tick(stats)`, ticked after the schedules (the agenda)

    # ------------------------------------------------------------ CRUD

    def add(self, *, flow: str | None = None, params: dict[str, Any] | None = None, on: str | None = None,
            version: str | None = None, cron: str | dict | None = None, timezone: str | None = None,
            missed: str | None = None, jitter_s: int | None = None, watch: dict | None = None,
            webhook: bool | dict | None = None, every: dict | None = None, deadman_minutes: int | None = None,
            max_failures: int = 3,
            principal: Principal | dict | None = None, enabled: bool = True,
            grant_id: str | None = None, route: str | None = None) -> tuple[Schedule, dict[str, Any]]:
        """Create a schedule. Returns (schedule, extras); extras carry the webhook secret,
        shown once. `grant_id` (default: the principal's grant, or the owner's grant for the
        human owner or an anonymous call from inside the node) is what every fire runs under;
        it must cover the lane and the flow's effect now, or PolicyDenied is raised.

        With `route` (an events route id) instead of `flow`, every fire emits an event to that
        route. A route schedule takes no params (a route fills its target from the event's
        facts); a watch on one needs `on` and a literal URL."""
        kinds = [k for k, v in (("cron", cron), ("watch", watch), ("webhook", webhook), ("every", every)) if v]
        if len(kinds) != 1:
            raise InvalidRequest("a schedule needs exactly one of cron, watch, webhook or every")
        if (flow is None) == (route is None):
            raise InvalidRequest("a schedule fires exactly one of a flow or a trigger (route)")
        if params is not None and not isinstance(params, dict):
            raise InvalidRequest("params must be a JSON object of name: value")
        params = dict(params or {})
        surface: str | None = None
        if isinstance(principal, Principal):
            principal = principal.model_dump()
        principal = principal or {}
        grant_id = grant_id or principal.get("grant_id") or (
            OWNER_GRANT if not principal.get("id") or is_owner(principal) else None)
        if route is not None:
            lane = self._check_route(route, kinds[0], on, params, watch)
            self.runner.check_grant(grant_id, principal, effect=Effect.READ if lane else None, lane=lane)
        else:
            fv = self.flows.get(flow, version)
            if is_connector_id(fv.id):  # `on` names a surface; its binding decides the lane at every fire
                surface, lane = self._check_surface(fv, on, params)
                self.runner.check_grant(grant_id, principal, effect=fv.effect, lane=surface)
            else:
                lane = self.flows.resolve_lane(fv, on)
                self._validate_params(fv, params)
                self.runner.check_grant(grant_id, principal, effect=fv.effect, lane=lane)
        now = self.clock.now()
        sid = new_id("sch")
        extras: dict[str, Any] = {}
        try:
            cron_cfg = watch_cfg = hook_cfg = every_cfg = None
            if cron:
                data = dict(cron) if isinstance(cron, dict) else {"cron": cron}
                if timezone:
                    data["timezone"] = timezone
                if missed:
                    data["missed"] = missed
                if jitter_s is not None:
                    data["jitter_s"] = jitter_s
                cron_cfg = CronConfig.model_validate(data)
                if not croniter.is_valid(cron_cfg.cron):
                    raise InvalidRequest(f"invalid cron expression {cron_cfg.cron!r}")
                if not cron_can_fire(cron_cfg.cron, _zone(cron_cfg.timezone), now):
                    raise InvalidRequest(f"cron expression {cron_cfg.cron!r} never matches a real date")
                gap = shortest_gap(cron_cfg.cron, _zone(cron_cfg.timezone), now)
                if cron_cfg.jitter_s and gap is not None and cron_cfg.jitter_s >= gap:
                    raise InvalidRequest(f"jitter_s {cron_cfg.jitter_s} is as long as the {int(gap)} s between two "
                                         "slots of this cron, so a slot would be dropped whenever the next one came "
                                         "due first. Use a smaller jitter_s.")
            elif watch:
                watch_cfg = WatchConfig.model_validate(watch)
            elif every:
                every_cfg = EveryConfig.model_validate(every)
            else:
                data = dict(webhook) if isinstance(webhook, dict) else {}
                data.pop("token", None)
                data.pop("secret", None)
                hook_cfg = WebhookConfig.model_validate(
                    {**data, "token": secrets.token_urlsafe(24), "secret": secrets.token_hex(32)})
                extras = _webhook_extras(hook_cfg)
            s = Schedule(id=sid, kind=ScheduleKind(kinds[0]), flow_id=None if route else fv.id, route_id=route,
                         flow_version=None if route else version, lane=lane,
                         surface=surface,
                         params=params, cron=cron_cfg, watch=watch_cfg, webhook=hook_cfg, every=every_cfg,
                         deadman_minutes=deadman_minutes, max_failures=max_failures, enabled=enabled,
                         principal=principal, grant_id=grant_id, created_at=now, updated_at=now,
                         state={"cursor": now.isoformat(), **({} if route else {"made_with": fv.version})})
        except ValidationError as e:
            raise InvalidRequest(f"invalid schedule: {validation_message(e)}") from e
        self.store.put_schedule(s)
        return s, extras

    def _check_surface(self, fv: Any, on: str | None, params: dict[str, Any]) -> tuple[str, str]:
        """(surface, lane) of a connector schedule, after checking its params with the binding's."""
        target = self.runner.connector_target(fv, on, params, validate=False)
        self._validate_params(fv, target.params)  # a templated value is checked when the schedule fires
        return target.surface.id, target.lane

    def connector_hold(self, s: Schedule) -> dict[str, Any] | None:
        """The health record that holds a connector schedule (its operation is broken on its
        surface), or None. A held schedule skips its slots instead of failing a run per slot."""
        health = self.runner.health
        if not s.surface or not s.flow_id or health is None:
            return None
        return health.broken(s.flow_id.removeprefix(CONNECTOR_PREFIX), s.surface)

    def _events(self) -> Any:
        events = self.runtime.get("event_queue") if self.runtime is not None else None
        if events is None:
            raise ServiceUnavailable("the events layer is not running on this node, so a trigger cannot fire")
        return events

    def _check_route(self, route_id: str, kind: str, on: str | None, params: dict[str, Any],
                     watch: dict | None) -> str | None:
        """A route schedule's lane, after checking the route takes this kind of event."""
        r = self._events().get_route(route_id)
        if r.match.source != kind:
            raise InvalidRequest(f"trigger {route_id} wakes on {r.match.source} events, not {kind}")
        if params:
            raise InvalidRequest("a schedule that fires a trigger takes no params; the trigger fills its "
                                 "target from the event's facts")
        if on is not None:
            try:
                ws, _ = parse_lane_id(on)
            except ValueError as e:
                raise InvalidRequest(str(e)) from None
            if ws != r.workspace:
                raise InvalidRequest(f"lane {on} is not in the trigger's workspace {r.workspace}")
        if kind == "watch":
            if on is None:
                raise InvalidRequest("a page watch that fires a trigger needs the lane to read (on)")
            url = str((watch or {}).get("url") or "") if isinstance(watch, dict) else ""
            if TEMPLATE_RE.search(url) or not url.lower().startswith(("https://", "http://")):
                raise InvalidRequest("a page watch that fires a trigger needs a literal http(s) URL")
        return on

    def adopt(self, s: Schedule, *, enabled: bool) -> tuple[Schedule, dict[str, Any]]:
        """Store a schedule carried over from another node (a workspace import, agentos.workspaces.transfer.import_).
        It keeps its config, params, principal and grant. It gets a fresh id when its id is taken here,
        a fresh webhook token and secret (the old ones stay with the old node; the new secret is in
        extras, shown once), and a cursor at now, so nothing it missed is replayed."""
        now = self.clock.now()
        sid = new_id("sch") if self.store.has_schedule(s.id) else s.id
        hook, extras = s.webhook, {}
        if hook is not None:
            hook = hook.model_copy(update={"token": secrets.token_urlsafe(24), "secret": secrets.token_hex(32)})
            extras = _webhook_extras(hook)
        out = s.model_copy(update={"id": sid, "webhook": hook, "enabled": enabled, "updated_at": now,
                                   "state": {"cursor": now.isoformat()}})
        self.store.put_schedule(out)
        return out, extras

    def _validate_params(self, fv: Any, params: dict[str, Any]) -> None:
        """Check params now. A value with a template (`${trigger.value}`) is only known when
        the schedule fires, so only errors about those values wait until then; a missing
        required param, an unknown one or a wrong literal is refused here."""
        templated = {k for k, v in params.items() if isinstance(v, str) and TEMPLATE_RE.search(v)}
        if not templated:
            self.flows.validate_params(fv, params)
            return
        schema = fv.definition.params
        unknown = sorted(k for k in templated if k not in schema.get("properties", {}))
        if unknown and schema.get("additionalProperties") is False:
            raise InvalidRequest(f"invalid params for {fv.id}: {', '.join(unknown)} is not a param of this flow")
        try:
            self.flows.validate_params(fv, {k: v for k, v in params.items() if k not in templated})
        except InvalidRequest as e:
            # drop the complaints that only say a templated param is missing
            parts = [p for p in e.message.split(": ", 1)[-1].split("; ")
                     if not any(f"'{k}' is a required property" in p for k in templated)]
            if parts:
                raise InvalidRequest(f"invalid params for {fv.id}: " + "; ".join(parts)) from None

    def get(self, sid: str) -> Schedule:
        s = self.store.get_schedule(sid)
        if s is None:
            raise NotFound(f"schedule {sid!r} not found")
        return s

    def list(self, kind: str | None = None) -> list[Schedule]:
        return self.store.list_schedules(kind)

    def remove(self, sid: str) -> bool:
        if not self.store.delete_schedule(sid):
            raise NotFound(f"schedule {sid!r} not found")
        return True

    def set_enabled(self, sid: str, enabled: bool) -> Schedule:
        s = self.get(sid)
        s.enabled = enabled
        s.updated_at = self.clock.now()
        if enabled:
            s.state["cursor"] = s.updated_at.isoformat()  # do not replay the paused period
        self.store.put_schedule(s)
        return s

    def describe(self, s: Schedule) -> dict[str, Any]:
        d = s.public()
        if s.kind == ScheduleKind.CRON and s.cron and s.enabled:
            slot = self.next_slot(s)
            d["next_run"] = slot.isoformat()
            paced = self.paced_slot(s, slot)
            if paced is not None:
                d["paced"] = paced
        return d

    def paced_slot(self, s: Schedule, slot: datetime) -> dict[str, Any] | None:
        """Where the lane's posting pace moves a commit slot (agentos.policy.pace, AUD-13): outside
        its waking hours, inside its gap or past its posts for that day, the run waits until the lane
        may post (RunnerService._pace_gate). None when the slot goes as it is."""
        policy = self.runner._service("policy")
        target = s.surface or s.lane  # a connector schedule posts through its surface's lane
        if policy is None or not hasattr(policy, "pace_verdict") or not s.flow_id or not target:
            return None
        try:
            fv = self.flows.get(s.flow_id, s.flow_version)
        except AgentosError:
            return None
        if fv.effect.value not in ("commit", "pay", "irreversible"):
            return None
        lane = policy.pace_lane(target) if hasattr(policy, "pace_lane") else target
        pace, zone, v = policy.pace_verdict(lane, slot.timestamp())
        if pace is None or v is None or v.rule is None:
            return None
        from agentos.policy import pace as pacing

        hit = pacing.hit(lane, pace, zone, v, slot.timestamp())
        return {"slot": slot.isoformat(), "rule": v.rule, "moved_to": hit["next_at"],
                "moved_to_local": hit["next_at_local"], "timezone": zone.name, "timezone_source": zone.source,
                "explain": hit["explain"]}

    def next_slot(self, s: Schedule) -> datetime:
        tz = _zone(s.cron.timezone)
        now = self.clock.now()
        it = croniter(s.cron.cron, now.astimezone(tz).replace(tzinfo=None) - timedelta(hours=3))
        for _ in range(3000):
            utc = it.get_next(datetime).replace(tzinfo=tz, fold=0).astimezone(UTC)
            if utc > now:
                return utc
        return now

    # ------------------------------------------------------------ ticking

    async def tick(self) -> dict[str, int]:
        stats = {"fired": 0, "skipped": 0, "watched": 0, "alerts": 0}
        for s in self.store.list_schedules(enabled_only=True):  # unreadable rows are skipped
            try:
                if self._grant_ended(s) is not None:
                    continue  # turned off: no slot, no page read, nothing under a grant that is gone
                if s.kind == ScheduleKind.CRON:
                    await self._tick_cron(s, stats)
                elif s.kind == ScheduleKind.WATCH:
                    await self._tick_watch(s, stats)
                elif s.kind == ScheduleKind.EVERY:
                    self._tick_every(s, stats)
            except Exception:
                log.exception("schedule %s tick failed", s.id)
            try:  # a schedule that cannot fire is exactly what the dead-man check is for
                await self._deadman(s.id, stats)
            except Exception:
                log.exception("schedule %s dead-man check failed", s.id)
        for hook in list(self.tick_hooks):
            try:
                await maybe_await(hook())
            except Exception:
                log.exception("a schedule tick hook failed")
        for one_off in list(self.one_offs):
            try:
                await one_off.tick(stats)
            except Exception:
                log.exception("one-off tick failed (%s)", type(one_off).__name__)
        return stats

    def _active_run(self, s: Schedule) -> bool:
        if s.route_id:
            return False  # the events layer's concurrency control decides for a trigger
        rid = s.state.get("last_run_id")
        if not rid:
            return False
        run = self.store.get_run(rid)
        return run is not None and run.status in ACTIVE

    def _params(self, s: Schedule, trigger: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
        scope = {"trigger": trigger}
        out = {}
        for k, v in s.params.items():
            out[k] = render(v, scope) if isinstance(v, str) and "${" in v else v
        if extra:
            out.update(extra)
        return out

    def _version_to_run(self, s: Schedule) -> str | None:
        """The flow version a fire runs. A schedule with no pin follows the flow, but only to versions
        the owner saved in person: a version someone else saved later (an agent's flow_save, which
        covers the lane but maybe not the effect) never runs under the schedule's grant, the owner's
        included. The schedule then runs the newest version it may (the one it was made with, or the
        owner's), and says what it held back. None: the head."""
        if s.flow_version or not s.flow_id or is_connector_id(s.flow_id):
            return s.flow_version  # a pin, or a connector (installs are reviewed, never an agent's save)
        made_with = s.state.get("made_with")

        def allowed(v: Any) -> bool:
            if v.owner_saved or v.version == made_with:
                return True
            return made_with is None and v.created_at <= s.created_at  # a row from before made_with

        head = self.flows.get(s.flow_id)
        if allowed(head):
            s.state.pop("held_back", None)
            return None
        ok = [v for v in self.flows.versions(s.flow_id) if allowed(v)]
        s.state["held_back"] = {"version": head.version, "by": (head.saved_by or {}).get("kind"),
                                "reason": "saved by someone other than the owner after this schedule was made; "
                                          "it runs once the owner saves or approves it in the panel"}
        if not ok:
            raise PolicyDenied(f"{s.flow_id} has no version this schedule may run: its current one was saved "
                               "by someone other than the owner. Open it in the panel and save it to run it")
        return max(ok, key=lambda v: v.created_at).version

    def _grant_of(self, s: Schedule) -> str | None:
        return s.grant_id or (OWNER_GRANT if not s.principal or is_owner(s.principal) else None)

    def _grant_ended(self, s: Schedule) -> PolicyDenied | None:
        """The refusal when the grant `s` runs under was revoked, has expired or is not known here, after
        turning `s` off for good (it says why, and the owner is told). A schedule never outlives the grant
        that made it: re-enabling it changes nothing, since the grant is still gone. None while it is live
        (the owner's grant always is)."""
        grant = self._grant_of(s)
        if grant == OWNER_GRANT:
            return None
        try:
            self.runner.check_grant(grant, s.principal)
        except PolicyDenied as e:
            reason = getattr(e, "reason", None) or ("unknown_grant" if grant is None else None)
            if reason not in GRANT_ENDED:
                return None  # not valid yet, or a refusal of another kind: the fire itself reports it
            now = self.clock.now()
            s.enabled = False
            s.updated_at = now
            s.state["stopped"] = {"at": now.isoformat(), "reason": reason,
                                  "message": f"the grant it ran under {GRANT_ENDED[reason]}, so it was turned off"}
            s.state["last_error"] = {"at": now.isoformat(), "code": e.code, "reason": reason,
                                     "message": clip_text(e.message, 300)}
            self.store.put_schedule(s)
            self._notify(s, "A schedule stopped",
                         f"Schedule {s.id} ({s.target}) was turned off: the grant it ran under "
                         f"{GRANT_ENDED[reason]}.")
            log.warning("schedule %s turned off: its grant %s", s.id, GRANT_ENDED[reason])
            return e
        return None

    def _fire(self, s: Schedule, key: str, trigger: dict[str, Any], extra: dict[str, Any] | None = None):
        ended = self._grant_ended(s)
        if ended is not None:
            raise ended
        if s.route_id:
            return self._fire_event(s, key, trigger)
        grant = self._grant_of(s)
        run, created = self.runner.submit(
            flow_id=s.flow_id, version=self._version_to_run(s), params=self._params(s, trigger, extra),
            lane=None if s.surface else s.lane, surface=s.surface,
            idempotency_key=key, principal=s.principal or {"kind": "flow", "id": f"schedule:{s.id}"},
            source=f"{s.kind}:{s.id}", schedule_id=s.id, trigger=trigger, authorized=False, grant_id=grant)
        s.state["last_run_id"] = run.id
        s.state["last_fired_at"] = self.clock.now().isoformat()
        return run, created

    def _fire_event(self, s: Schedule, key: str, trigger: dict[str, Any]):
        """A route schedule's fire: one event, keyed like the run would be. Node facts only in
        `facts`; page text and webhook bodies go to the untrusted `content`."""
        events = self._events()
        route = events.get_route(s.route_id)
        kind = trigger["kind"]
        facts: dict[str, Any] = {"schedule_id": s.id}
        content = None
        if kind == "cron":
            facts.update(slot=trigger["slot"], late=bool(trigger.get("late")))
        elif kind == "watch":
            facts.update(seq=trigger["seq"], url=s.watch.url, detected_at=self.clock.now().isoformat())
            value, previous = trigger.get("value"), trigger.get("previous")
            if isinstance(value, int) and not isinstance(value, bool):
                facts.update(count=value, previous_count=previous if isinstance(previous, int) else None)
                facts = {k: v for k, v in facts.items() if v is not None}
            elif value is not None:
                content = {"text": str(value), "origin": s.watch.url}
        else:
            facts["delivery"] = re.sub(r"[^A-Za-z0-9._:-]", "-", str(trigger.get("delivery", "")))[:64]
            if trigger.get("body"):
                content = {"text": trigger["body"], "origin": "webhook body"}
        names = {"cron": "cron.fired", "watch": "watch.changed", "webhook": "webhook.received"}
        event, created = events.emit({"source": kind, "kind": names[kind], "workspace": route.workspace,
                                      "lane": s.lane, "dedupe_key": key, "facts": facts, "content": content},
                                     route_id=s.route_id)
        s.state["last_event_id"] = event.id
        s.state["last_fired_at"] = self.clock.now().isoformat()
        return event, created

    async def _tick_cron(self, s: Schedule, stats: dict[str, int]) -> None:
        cfg = s.cron
        tz = _zone(cfg.timezone)
        now = self.clock.now()
        cursor = datetime.fromisoformat(s.state.get("cursor") or s.created_at.isoformat())
        if now < cursor - timedelta(seconds=60):
            s.state["cursor"] = now.isoformat()
            s.state["clock_moved_back_at"] = now.isoformat()
            self.store.put_schedule(s)
            return
        slots = cron_slots(cfg.cron, tz, cursor, now)
        due = [sl for sl in slots if sl + _jitter(s.id, sl, cfg.jitter_s) <= now]
        if not due:
            return
        latest = due[-1]
        late = (now - (latest + _jitter(s.id, latest, cfg.jitter_s))).total_seconds() > cfg.grace_s
        # the cursor moves to `latest`: every slot before it never runs, a due one (one fire per tick)
        # and one still waiting out its jitter alike, so say which and why
        dropped = [sl.isoformat() for sl in slots if sl < latest]
        skipped = len(dropped)
        if skipped:
            s.state["last_dropped"] = {"at": now.isoformat(), "count": skipped, "slots": dropped[-20:],
                                       "fired_instead": latest.isoformat(),
                                       "reason": "a later slot was due too (the node was down, or jitter_s is "
                                                 "longer than the time between slots), and a schedule fires once"}
            s.state["dropped_total"] = int(s.state.get("dropped_total", 0)) + skipped
            log.warning("schedule %s dropped %d slot(s) before %s: a later slot was due too", s.id, skipped,
                        latest.isoformat())
        if late and cfg.missed == "skip":
            skipped += 1
            s.state["last_skip"] = {"at": now.isoformat(), "reason": "missed", "slots": len(due)}
        elif self._active_run(s):
            skipped += 1
            s.state["last_skip"] = {"at": now.isoformat(), "reason": "previous run still active",
                                    "slot": latest.isoformat()}
        elif (held := self.connector_hold(s)) is not None:
            skipped += 1
            s.state["held"] = {"at": now.isoformat(), "reason": "connector_broken", "since": held.get("since"),
                               "run_id": held.get("run_id")}
            s.state["last_skip"] = {"at": now.isoformat(), "reason": "connector_broken", "slot": latest.isoformat()}
        else:
            try:
                self._check_frozen(s)  # the same checks an interval's fire takes: a freeze, a site that changed
                for check in self.fire_checks:
                    check(s)
                _, created = self._fire(s, f"cron:{s.id}:{latest.isoformat()}",
                                          {"kind": "cron", "slot": latest.isoformat(), "late": late})
            except AgentosError as e:  # frozen, the flow is gone, the grant was revoked, params went stale
                created = False
                skipped += 1
                s.state["last_error"] = {"at": now.isoformat(), "slot": latest.isoformat(), "code": e.code,
                                         "message": clip_text(e.message, 300)}
                log.warning("schedule %s could not fire: %s", s.id, e.message)
            if created:
                stats["fired"] += 1
                s.state.pop("held", None)
        stats["skipped"] += skipped
        s.state["skipped_total"] = int(s.state.get("skipped_total", 0)) + skipped
        s.state["cursor"] = latest.isoformat()
        s.updated_at = now
        self.store.put_schedule(s)

    # ------------------------------------------------------------ intervals

    def _check_frozen(self, s: Schedule) -> None:
        """A freeze of the surface or of the lane behind it: a person said stop, so this fires nothing."""
        freeze = self.runtime.get("freeze") if self.runtime is not None else None
        if freeze is not None:
            for lane in {s.surface, s.lane} - {None}:
                freeze.check_call(lane, tool="schedule")

    def _tick_every(self, s: Schedule, stats: dict[str, int]) -> None:
        now = self.clock.now()
        st = s.state
        next_at = st.get("next_check_at")
        if next_at:
            wait = (datetime.fromisoformat(next_at) - now).total_seconds()
            if 0 < wait <= s.every.every_s + 60:
                return  # not due yet (a wait longer than the interval means the clock moved back)
        slot = next_at or st.get("cursor") or s.created_at.isoformat()
        st["next_check_at"] = (now + timedelta(seconds=s.every.every_s)).isoformat()
        if self._active_run(s):
            stats["skipped"] += 1
            st["skipped_total"] = int(st.get("skipped_total", 0)) + 1
            st["last_skip"] = {"at": now.isoformat(), "reason": "previous run still active", "slot": slot}
        elif (held := self.connector_hold(s)) is not None:
            stats["skipped"] += 1
            st["skipped_total"] = int(st.get("skipped_total", 0)) + 1
            st["held"] = {"at": now.isoformat(), "reason": "connector_broken", "since": held.get("since"),
                          "run_id": held.get("run_id")}
        else:
            st.pop("held", None)
            try:
                self._check_frozen(s)
                for check in self.fire_checks:
                    check(s)
                _, created = self._fire(s, f"every:{s.id}:{slot}", {"kind": "every", "slot": slot})
            except AgentosError as e:  # paused, over its pace, frozen, the grant ended: it is on record
                created = False
                st["last_error"] = {"at": now.isoformat(), "slot": slot, "code": e.code,
                                    "message": clip_text(e.message, 300)}
                log.warning("schedule %s could not fire: %s", s.id, e.message)
            else:
                st.pop("last_error", None)
            if created:
                stats["fired"] += 1
        s.updated_at = now
        self.store.put_schedule(s)

    # ------------------------------------------------------------ page watches

    async def _tick_watch(self, s: Schedule, stats: dict[str, int]) -> None:
        cfg: WatchConfig = s.watch
        now = self.clock.now()
        st = s.state
        next_at = st.get("next_check_at")
        if next_at:
            wait = (datetime.fromisoformat(next_at) - now).total_seconds()
            if 0 < wait <= max(cfg.every_s, cfg.debounce_s, 30) + 60:
                return  # not due yet (a wait longer than any interval means the clock moved back)
        if self.runner.lane_busy(s.lane) or self.runner.active_count >= self.runner.max_concurrent:
            return
        value = await self._read_watch(s, cfg)
        # the read awaited the page: the owner may have removed, disabled or edited it meanwhile
        fresh = self.store.get_schedule(s.id)
        if fresh is None or not fresh.enabled or fresh.updated_at != s.updated_at:
            return
        interval = cfg.every_s
        if value is None:
            st["errors"] = int(st.get("errors", 0)) + 1
            if st["errors"] == 3:
                self._notify(s, "A page watch keeps failing",
                             f"Watch {s.id} on {s.lane} could not read {cfg.locator.describe()} 3 times in a row.")
                stats["alerts"] += 1
        else:
            stats["watched"] += 1
            st["errors"] = 0
            st["last_checked_at"] = now.isoformat()
            last = st.get("last_value")
            if "last_value" not in st:
                st["last_value"] = value  # the first read is the baseline
            elif value == last:
                st.pop("pending_value", None)
                st.pop("pending_since", None)
            else:
                confirmed = False
                if cfg.debounce_s == 0:
                    confirmed = True
                elif st.get("pending_value") == value:
                    since = datetime.fromisoformat(st["pending_since"])
                    confirmed = (now - since).total_seconds() >= cfg.debounce_s
                else:
                    st["pending_value"], st["pending_since"] = value, now.isoformat()
                if not confirmed and "pending_value" in st:
                    interval = min(interval, max(30, cfg.debounce_s))
                if confirmed and self._active_run(s):
                    confirmed = False  # wait for the running one; the change stays pending
                    st["pending_value"] = value
                    st.setdefault("pending_since", now.isoformat())
                if confirmed:
                    seq = int(st.get("seq", 0)) + 1
                    trigger = {"kind": "watch", "value": value, "previous": last, "seq": seq}
                    try:
                        _, created = self._fire(s, f"watch:{s.id}:{seq}", trigger)
                    except AgentosError as e:  # the change is consumed; the error is on record
                        created = False
                        st["last_error"] = {"at": now.isoformat(), "code": e.code, "message": clip_text(e.message, 300)}
                        log.warning("watch %s could not fire: %s", s.id, e.message)
                    st["seq"] = seq
                    st["last_value"] = value
                    st.pop("pending_value", None)
                    st.pop("pending_since", None)
                    if created:
                        stats["fired"] += 1
        st["next_check_at"] = (now + timedelta(seconds=interval)).isoformat()
        s.updated_at = now
        self.store.put_schedule(s)

    async def _read_watch(self, s: Schedule, cfg: WatchConfig) -> str | int | None:
        try:
            url = render(cfg.url, s.params, url=True)
            domain = self.flows.get(s.flow_id, s.flow_version).definition.domain if s.flow_id else None
            problem = url_problem(cfg.url, url, domain)
            if problem:
                log.warning("watch %s refused its URL: %s", s.id, problem)
                return None
        except AgentosError as e:
            log.warning("watch %s cannot build its URL: %s", s.id, e.message)
            return None
        try:
            async with self.runner.lane_session(s.lane) as driver:
                nav = await driver.navigate(url)  # a fresh load every poll
                if nav.signal in BAD_SIGNALS:
                    return None
                state = await driver.read_state(cfg.locator.render(s.params))
        except LaneBusy:
            return None
        except Exception:
            log.exception("watch %s read failed", s.id)
            return None
        if cfg.read == "count":
            return state.count
        if not state.found:
            return None
        return clip_text(" ".join((state.text or "").split()), 2000)

    # ------------------------------------------------------------ webhooks

    async def handle_webhook(self, token: str, body: bytes, headers: dict[str, str], *,
                             admit: Any = None) -> tuple[int, dict[str, Any]]:
        """`admit(schedule)`, after the signature passed: seconds to wait, or None to go on. It counts
        only what really came from the sender, so a stranger cannot use up a sender's allowance."""
        h = {k.lower(): v for k, v in headers.items()}
        s = self.store.schedule_by_token(token) if token else None
        if s is None or s.kind != ScheduleKind.WEBHOOK or not s.enabled or not s.webhook:
            return 404, {"error": {"code": "not_found", "message": "unknown hook"}}
        cfg = s.webhook
        ts, sig = h.get("x-agentos-timestamp", ""), h.get("x-agentos-signature", "")
        try:
            sent = float(ts)
        except ValueError:
            sent = math.nan
        if not math.isfinite(sent):
            return 401, {"error": {"code": "denied", "message": "missing or bad X-Agentos-Timestamp"}}
        if abs(self.clock.now().timestamp() - sent) > cfg.max_skew_s:
            return 401, {"error": {"code": "denied", "message": "timestamp outside the allowed window"}}
        if not hmac.compare_digest(sign_webhook(cfg.secret, ts, body).encode(), sig.encode("utf-8", "replace")):
            return 401, {"error": {"code": "denied", "message": "bad signature"}}
        wait = admit(s) if admit is not None else None
        if wait is not None:
            return 429, {"error": {"code": "rate_limited", "message": "too many webhooks for this workflow right now"},
                         "retry_after": wait}
        # the delivery header is not signed, so the run is keyed by what is: a replay of a
        # captured request under a new delivery id is still the same run
        signed = hashlib.sha256(ts.encode() + b"." + body).hexdigest()[:32]
        delivery = clip_text(h.get("x-agentos-delivery") or signed, 64)
        if s.route_id:
            return self._webhook_event(s, signed, delivery, body)
        seen = next((r for r in self.store.list_runs(schedule_id=s.id, limit=200)
                     if (r.trigger or {}).get("delivery") == delivery), None)
        if seen is not None:  # the sender retried this delivery (a new timestamp, the same id)
            return 200, {"run_id": seen.id, "status": str(seen.status), "duplicate": True}
        extra = None
        if cfg.accept_params and body:
            try:
                payload = json.loads(body)
            except ValueError:
                return 400, {"error": {"code": "invalid", "message": "body must be JSON"}}
            extra = payload.get("params") if isinstance(payload, dict) else None
            if extra is not None and not isinstance(extra, dict):
                return 400, {"error": {"code": "invalid", "message": "params must be an object"}}
        trigger = {"kind": "webhook", "delivery": delivery}
        try:
            run, created = self._fire(s, f"hook:{s.id}:{signed}", trigger, extra)
        except PolicyDenied as e:
            return 403, {"error": {"code": e.code, "message": e.message}}
        except AgentosError as e:
            return 400, {"error": {"code": e.code, "message": e.message}}
        self.store.put_schedule(s)
        return (202 if created else 200), {"run_id": run.id, "status": str(run.status), "duplicate": not created}

    def _webhook_event(self, s: Schedule, signed: str, delivery: str, body: bytes) -> tuple[int, dict[str, Any]]:
        try:
            events = self._events()
            seen = events.find_event(source="webhook", schedule_id=s.id, delivery=delivery)
            if seen is not None:  # the sender retried this delivery (a new timestamp, the same id)
                return 200, {"event_id": seen.id, "duplicate": True}
            text = body.decode("utf-8", "replace") if body else ""
            event, created = self._fire(s, f"hook:{s.id}:{signed}", {"kind": "webhook", "delivery": delivery,
                                                                     "body": text})
        except PolicyDenied as e:
            return 403, {"error": {"code": e.code, "message": e.message}}
        except AgentosError as e:
            return 400, {"error": {"code": e.code, "message": e.message}}
        self.store.put_schedule(s)
        return (202 if created else 200), {"event_id": event.id, "duplicate": not created}

    # ------------------------------------------------------------ dead-man

    async def _deadman(self, sid: str, stats: dict[str, int]) -> None:
        s = self.store.get_schedule(sid)  # fresh: the tick may have changed it
        if s is None or not s.enabled or s.route_id:
            return  # a trigger's deliveries have their own dead letters and breaker
        now = self.clock.now()
        recent = [r for r in self.store.list_runs(schedule_id=s.id, limit=max(s.max_failures, 10))
                  if r.status in TERMINAL]
        last_ok = next((r for r in recent if r.status in SUCCEEDED), None)
        failing = 0
        for r in recent:
            if r.status in (RunStatus.FAILED, RunStatus.PARTIAL):
                failing += 1
            else:
                break
        st = s.state
        changed = False
        if failing >= s.max_failures:
            if not st.get("failing_alerted"):
                self._notify(s, "A schedule keeps failing",
                             f"Schedule {s.id} ({s.target} on {s.lane}) failed {failing} times in a row. "
                             f"Last error: {(recent[0].error or {}).get('message', 'unknown')}")
                st["failing_alerted"] = True
                stats["alerts"] += 1
                changed = True
        elif st.get("failing_alerted"):
            st["failing_alerted"] = False
            changed = True
        if s.deadman_minutes:
            since = last_ok.finished_at if last_ok and last_ok.finished_at else s.created_at
            overdue = (now - since).total_seconds() > s.deadman_minutes * 60
            if overdue and not st.get("deadman_alerted"):
                self._notify(s, "A scheduled run is missing",
                             f"Schedule {s.id} ({s.target} on {s.lane}) has had no successful run for "
                             f"{s.deadman_minutes} minutes.")
                st["deadman_alerted"] = True
                stats["alerts"] += 1
                changed = True
            elif not overdue and st.get("deadman_alerted"):
                st["deadman_alerted"] = False
                changed = True
        if changed:
            self.store.put_schedule(s)

    def _notify(self, s: Schedule, title: str, body: str) -> None:
        notifier = self.runtime.get("notifier") if self.runtime is not None else None
        fn = getattr(notifier, "notify", None)
        if fn is None:
            log.warning("%s: %s", title, body)
            return
        data = {"schedule_id": s.id, "flow": s.flow_id, "route": s.route_id, "lane": s.lane}

        async def send() -> None:
            try:
                await maybe_await(fn(title=title, body=body, level="warning", data=data))
            except Exception:
                log.exception("notifier failed")

        task = asyncio.get_running_loop().create_task(send())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.tick_s)

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
