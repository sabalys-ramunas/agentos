"""`/v1/sites`: the developer surface of site APIs (docs/SITE-APIS.md, HTTP), and the owner's routes for
keys and usage. docs/API.md has the routes; this is how a call is decided.

A call is the MCP tool call under another name. The key resolves to its backing grant, a policy
context is built from that grant with the tool's own name, and the run goes through
`connectors.tools.run_operation`: binding, health, pace, Ask me first, approvals, meters, taint and
Activity are not written again here. What this file adds is around that call, in this order:

    1  a key in the query string is refused, whatever else was sent
    2  who: a key by its hash, or the owner's session with its CSRF header; nothing else
    3  the key's rate limit
    4  scope: an API or endpoint this key cannot see is the same 404 as a missing one
    5  a paused API, the surface, then the grant's own live check, the ceiling and the freeze
    6  params against the input schema
    7  the cache, and only now: auth first, cache second, on every hit
    8  the lane's state, then the shared run, and a wait of at most `wait` seconds

A hit therefore passes 1 to 6 like any call, and is labelled and written to Activity like one: only the
lane, pace and the meters stay out of it, because a hit starts no run.

A hit while the lane is held still answers. A hold keeps work off the lane, so the account is not
retried on; a hit touches nothing on the lane, and is a value read in good standing within the
`cache_s` the owner chose (contract: "A hit starts no run and does not touch the lane"). The hold is
what a miss runs into: it answers `needs_human` or `rate_limited`, and `stale_ok` is then the caller's
way to the last good value past its time. A freeze is different: a person said stop, so a hit stops.

`max_age=0`, a slow read, and a run that outlives its caller all share one run per cache key, and a
result is stored when the run ends, not when somebody reads it. A commit is never shared or cached:
a client that goes away leaves its run going, and its `Idempotency-Key` finds that run again.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from openajar.api.app import Api, endpoint, query_int, read_json_object
from openajar.api.auth import CSRF_HEADER, SESSION_COOKIE, tokens_equal
from openajar.api.errors import ApiError
from openajar.api.jsonable import jsonable
from openajar.core.errors import InvalidRequest, NotFound, OpenajarError, ServiceUnavailable
from openajar.core.logs import update as update_log_context
from openajar.core.models import ControlState, Effect, LaneState, RunStatus
from openajar.policy.errors import Denied, GrantFrozen
from openajar.runner.connectors.models import BINDING_PARAMS
from openajar.runner.connectors.taint import label_context
from openajar.runner.connectors.tools import _surfaces_for, run_operation, tool_name
from openajar.runner.flowdoc import FlowVersion
from openajar.runner.names import CONNECTOR_PREFIX
from openajar.runner.records import SUCCEEDED, Run
from openajar.runner.siteapis import openapi as oas
from openajar.runner.siteapis.cache import (
    STALE_MAX_S,
    SiteCache,
    below_expected,
    cache_key,
    canonical,
    http_of,
    is_local,
)
from openajar.runner.siteapis.http_store import HttpStore
from openajar.runner.siteapis.keys import KEY_CHANGES, KEY_RE, KeyGate, Rate, SiteKey, SiteKeys
from openajar.runner.siteapis.keys import Api as KeyApi
from openajar.runner.siteapis.models import CONTROLS
from openajar.runner.siteapis.problems import (
    LANE_BUSY_RETRY_S,
    RUN_GONE,
    Problem,
    from_exception,
    from_run,
    seconds_until,
)
from openajar.runner.siteapis.usage import DEFAULT_HOURS, MAX_HOURS, Usage
from openajar.runner.util import Clock

log = logging.getLogger("openajar.api")

PREFIX = "/v1/sites"
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
DEFAULT_WAIT_S = 25.0
MAX_WAIT_S = 60.0
MAX_BODY = 256 * 1024
BODY_TIMEOUT_S = 10.0   # a sender that dribbles its body holds a slot for no longer than this
MAX_DEPTH = 100         # past about 150 a run's params fail further on, where it would answer 500
MAX_IDEMPOTENCY = 128
STALE_CODES = frozenset({"needs_human", "pace_limited", "rate_limited"})
OWNER_ID = "owner"
WAIT_SLICE_S = 0.5
HOLD_CONTROLS = frozenset({"human", "paused", "connector_broken"})
PENDING_WHY = "The run is still going. Poll it."
APPROVAL_WHY = "A person must approve this call before it runs."
DECISION_WHY = "The run stopped where it may have acted, so a person decides. Poll it."
HOLD_WHY = "The lane is held. The run waits for a person and goes on after that."
QUERY_KEY_DETAIL = "A key in the query string is refused. Send it as Authorization: Bearer, and replace this key."
CSRF_DETAIL = "A session needs the X-Agentos-CSRF header here."
TRY_AGAIN_DETAIL = "This key's source tried too many bad keys."


@dataclass(frozen=True)
class Controls:
    on: str | None
    wait: float
    max_age: int | None
    stale_ok: bool


def _control(request: Request, name: str) -> str | None:
    value = request.query_params.get(name)
    return value if value is not None else request.headers.get("agentos-" + name.replace("_", "-"))


def read_controls(request: Request) -> Controls:
    """`on`, `wait`, `max_age`, `stale_ok`, each from the query or an `Openajar-*` header."""
    wait, max_age, stale = _control(request, "wait"), _control(request, "max_age"), _control(request, "stale_ok")
    try:
        seconds = DEFAULT_WAIT_S if wait is None else float(wait)
        age = None if max_age is None else int(max_age)
    except ValueError:
        raise Problem("invalid_params", "wait and max_age must be numbers.") from None
    if not math.isfinite(seconds) or not 0 <= seconds <= MAX_WAIT_S:
        raise Problem("invalid_params", f"wait must be from 0 to {int(MAX_WAIT_S)} seconds.")
    if age is not None and age < 0:
        raise Problem("invalid_params", "max_age must not be negative.")
    if stale not in (None, "0", "1"):
        raise Problem("invalid_params", "stale_ok must be 1 or 0.")
    return Controls(_control(request, "on") or None, seconds, age, stale == "1")


def _schema_problem(schema: dict[str, Any], params: dict[str, Any]) -> Problem | None:
    """The first way `params` break the input schema, in names only: a schema's message quotes the
    value, and a value does not go in `detail`."""
    errors = sorted(Draft202012Validator(schema).iter_errors(params), key=lambda e: list(e.path))
    if not errors:
        return None
    where = ".".join(str(p) for p in errors[0].path) or "params"
    return Problem("invalid_params", f"{where} does not fit the input schema ({errors[0].validator}).")


def _too_deep(value: Any) -> bool:
    """Level by level, so a body made to be deep cannot run this out of stack."""
    level = [value]
    for _ in range(MAX_DEPTH):
        level = [c for v in level if isinstance(v, dict | list) for c in (v.values() if isinstance(v, dict) else v)]
        if not level:
            return False
    return True


def local_flows(flows: Any) -> dict[str, dict[str, FlowVersion]]:
    """api -> endpoint -> its installed flow. Only `local` site APIs: a catalog connector is not here."""
    out: dict[str, dict[str, FlowVersion]] = {}
    for fv in flows.list(CONNECTOR_PREFIX.rstrip(".")) if flows is not None else ():
        if is_local(fv):
            meta = fv.definition.connector or {}
            out.setdefault(str(meta["connector"]), {})[str(meta["operation"])] = fv
    return out


def catalog_of(flows: Any, runner: Any) -> dict[str, KeyApi]:
    """What a key can be scoped to, by API."""
    out = {}
    for api, eps in local_flows(flows).items():
        bound = {s.id: s.lane for fv in eps.values() for s in _surfaces_for(runner, fv, lambda _s: True)} if runner else {}
        out[api] = KeyApi(api, tuple(sorted(eps)), bound,
                          tuple(sorted({fv.definition.domain for fv in eps.values() if fv.definition.domain})))
    return out


def etag_of(data: Any) -> str:
    return '"' + hashlib.sha256(canonical(data).encode()).hexdigest()[:32] + '"'


def _matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    return header.strip() == "*" or any(t.strip().removeprefix("W/") == etag for t in header.split(","))


def _z(value: str | None) -> str | None:
    return value.replace("+00:00", "Z") if value else value


def _pending(code: str, run: Run, why: str) -> Response:
    return JSONResponse({"code": code, "status": str(run.status), "run": run.id, "why": why}, status_code=202,
                        headers={"Agentos-Run": run.id, "Cache-Control": "no-store"})


def _held(run: Run) -> bool:
    """Back in the queue because of a hold or the site's rate limit: it waits for a person or for hours."""
    err = run.error or {}
    return run.status == RunStatus.QUEUED and (err.get("control") in HOLD_CONTROLS or err.get("code") == "rate_limited")


@dataclass
class Caller:
    """Who this call is: a key, or the owner's session."""
    key: SiteKey | None
    rate: Rate | None = None

    @property
    def id(self) -> str:
        return self.key.id if self.key else OWNER_ID

    def sees_endpoint(self, api: str, name: str) -> bool:
        return self.key is None or self.key.sees_endpoint(api, name)

    def can_call(self, fv: FlowVersion) -> bool:
        rank, ceiling = fv.effect.rank, Effect(self.key.ceiling).rank if self.key else None
        return self.key is None or (rank is not None and ceiling is not None and rank <= ceiling)

    def owns(self, run: Run) -> bool:
        return self.key is None or run.principal.get("id") == self.key.subject


@dataclass
class Call:
    """One request, for what gets written when it ends."""
    caller: Caller | None = None
    ctx: Any = None
    api: str | None = None
    endpoint: str | None = None
    surface: str | None = None
    effect: str | None = None
    cached: bool = False
    started: float = field(default_factory=time.monotonic)

    def headers(self) -> dict[str, str]:
        return self.caller.rate.headers() if self.caller and self.caller.rate else {}


class SiteHttp:
    def __init__(self, api: Api, *, home: Path, clock: Clock | None = None, gate: KeyGate | None = None,
                 cache_bytes: int | None = None):
        self.api, self.runtime = api, api.runtime
        self.clock = clock or Clock()
        self.gate = gate or KeyGate()
        self.store = HttpStore(home)
        self.policy = self.runtime.get("policy")
        self.keys = SiteKeys(self.store, self.policy, self.catalog, frozen=self._key_frozen)
        self.usage = Usage(self.store, clock=self.clock)
        flows = self.runtime.get("flows")
        self.cache = SiteCache(self.store, flows, clock=self.clock, paused=self.paused,
                               **({"max_bytes": cache_bytes} if cache_bytes is not None else {}))
        runner = self.runtime.get("runner")
        if runner is not None:
            runner.on_finished.append(self.cache.record)

    # ---- what is on this node ----
    def _runner(self) -> Any:
        runner = self.runtime.get("runner")
        if runner is None or runner.surfaces is None or self.policy is None:
            raise Problem("node_offline")
        return runner

    def catalog(self) -> dict[str, KeyApi]:
        return catalog_of(self.runtime.get("flows"), self.runtime.get("runner"))

    def _flows(self) -> dict[str, dict[str, FlowVersion]]:
        return local_flows(self.runtime.get("flows"))

    def paused(self, api: str) -> bool:
        svc = self.runtime.get("site_apis")
        state = getattr(svc, "state_of", None)
        return callable(state) and state(api) == "paused"

    def api_dropped(self, api: str) -> int:
        """A person paused or removed `api`: what the cache holds of it goes."""
        return self.cache.drop_api(api)

    # ---- who ----
    def _caller(self, request: Request) -> Caller:
        for name, value in request.query_params.multi_items():
            if KEY_RE.search(value) or KEY_RE.search(name):
                raise Problem("unauthorized", QUERY_KEY_DETAIL)
        header = request.headers.get("authorization")
        if self.policy is None:
            raise Problem("node_offline" if header is not None else "unauthorized")
        return self._by_key(request, header) if header is not None else self._by_session(request)

    def _by_key(self, request: Request, header: str) -> Caller:
        source = request.client.host if request.client else "unknown"
        wait = self.gate.failed.blocked(source)
        if wait is not None:
            raise Problem("rate_limited_key", TRY_AGAIN_DETAIL, retry_after=math.ceil(wait))
        scheme, _, token = header.partition(" ")
        key = self.keys.lookup(token.strip()) if scheme.lower() == "bearer" else None
        if key is None or key.revoked_at or key.expired(self.keys.clock()):
            self.gate.failed.check(source)
            raise Problem("unauthorized")
        update_log_context(grant=key.grant_id)
        try:
            key = self.keys.used(key)
        except Denied as e:
            if self._key_frozen(key):  # the sweep revoked it: say why, until the owner unfreezes
                raise Problem("frozen") from None
            if isinstance(e, GrantFrozen):  # unfrozen since: a key the sweep ended is a revoked key
                raise Problem("unauthorized") from None
            raise from_exception(e, self.clock.now()) from None
        return Caller(key)

    def _limit(self, caller: Caller) -> None:
        """The key's own rate limit, before anything about the API is answered."""
        if caller.key is not None:
            caller.rate = self.gate.take(caller.key.id, caller.key.rate_per_min)
            if caller.rate.retry_after is not None:
                raise Problem("rate_limited_key")

    def _key_frozen(self, key: SiteKey) -> bool:
        freeze = self.runtime.get("freeze")
        if freeze is None:
            return False
        known = self.catalog()
        bound = {s: lane for a in key.apis if a in known for s, lane in known[a].surfaces.items()}
        surfaces = key.surfaces or list(bound)
        lanes = {*surfaces, *(bound[s] for s in surfaces if s in bound)}
        return freeze.frozen_for(None) is not None or any(freeze.frozen_for(lane) is not None for lane in lanes)

    def _by_session(self, request: Request) -> Caller:
        sid = request.cookies.get(SESSION_COOKIE)
        session = self.api.store.get_session(sid) if sid else None
        if session is None or session.is_member:
            raise Problem("unauthorized")
        if session.via == "device":  # the device credential sits where an agent can read it: never the owner in person
            raise Problem("unauthorized", "This computer's saved credential is not a person: use a site API key, "
                                          "or sign in to the panel.")
        if not self.api.origin_ok(request) or not tokens_equal(request.headers.get(CSRF_HEADER), session.csrf):
            raise Problem("unauthorized", CSRF_DETAIL)
        return Caller(None)

    def _context(self, caller: Caller, *, tool: str, lane_id: str | None = None) -> Any:
        if caller.key is None:
            ctx = self.policy.owner_context(tool=tool)
            ctx.lane_id = lane_id
            return ctx
        return self.keys.context(caller.key, tool=tool, lane_id=lane_id)

    def _gate(self, ctx: Any, effect: Effect, on: str) -> None:
        """The grant is live, covers the surface and reaches this effect, and no freeze covers it. What
        a hit has instead of authorize: that also spends the meters, and a hit spends nothing."""
        ctx.precheck(effect, lane_id=on)
        freeze = self.runtime.get("freeze")
        if freeze is not None and not ctx.in_person:
            surface = self._runner().surfaces.find(on)
            for lane in (on, surface.lane if surface is not None else on):  # a surface's id need not be its lane's
                freeze.check_call(lane, tool=ctx.tool)

    @contextlib.contextmanager
    def _frozen_guard(self, ctx: Any, on: str) -> Iterator[None]:
        """As the MCP wrapper: refuse a call on a frozen scope, and let a freeze interrupt it in flight."""
        freeze = self.runtime.get("freeze")
        if freeze is None or ctx.in_person:
            yield
            return
        with freeze.guard(tool=ctx.tool, lane_id=on, chain=ctx.grant.chain_ids):
            yield

    # ---- the routes of /v1/sites ----
    def _route(self, fn: Callable[[Request, Call], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        async def route(request: Request) -> Response:
            call = Call()
            try:
                response = await fn(request, call)
            except Exception as e:
                if not isinstance(e, Problem | OpenajarError):
                    log.exception("site API call failed")
                problem = from_exception(e, self.clock.now())
                response = problem.response(call.headers())
            else:
                response.headers.update(call.headers())
            self._written(call, response)
            return response

        return route

    def _written(self, call: Call, response: Response) -> None:
        """The usage row and the Activity row of an endpoint call: names, a status and a code, never
        params, data, the caller's address or the key: its label and prefix."""
        if call.caller is None or call.api is None or call.endpoint is None:
            return
        code = None
        if response.status_code >= 400 or response.status_code == 202:
            with contextlib.suppress(ValueError, TypeError):
                code = json.loads(bytes(response.body)).get("code")
        ms = int((time.monotonic() - call.started) * 1000)
        try:
            self.usage.record(key_id=call.caller.id, api=call.api, endpoint=call.endpoint, surface=call.surface,
                              status=response.status_code, code=code, duration_ms=ms, cached=call.cached)
        except Exception:
            log.exception("could not write a site API usage row")
        if call.ctx is not None:
            key = call.caller.key
            who = f"{key.label} ({key.prefix})" if key else "the owner"
            call.ctx.trace("site_api_call", status="ok" if response.status_code < 400 else "denied", lane=call.surface,
                           effect=call.effect, summary=f"{who}: {call.api}.{call.endpoint} answered "
                           f"{response.status_code}" + (f" {code}" if code else ""),
                           code=code, cached=call.cached, key=key.label if key else None, key_prefix=key.prefix if key else None)

    def _visible(self, caller: Caller, api: str, eps: dict[str, FlowVersion]) -> dict[str, FlowVersion]:
        return {n: fv for n, fv in eps.items() if caller.sees_endpoint(api, n) and caller.can_call(fv)}

    def _surfaces(self, caller: Caller, fvs: Any, held: set[str] | None = None) -> list[str]:
        """The surfaces of `fvs` this caller reaches now. A frozen one is left out; with `held`, the ones
        the key and its grant reach but a freeze holds are added to it, so the answer can say `frozen`."""
        runner = self.runtime.get("runner")
        if runner is None or runner.surfaces is None:
            return []
        ctx = self._context(caller, tool="site_apis") if caller.key else None
        if not (caller.key and ctx):
            return sorted({s.id for fv in fvs for s in _surfaces_for(runner, fv, lambda _s: True)})

        def allowed(s: str) -> bool:
            if not caller.key.sees_surface(s):
                return False
            if ctx.can_access_lane(s):
                return True
            if held is not None and ctx.grant.lane_allowed(s) and ctx.frozen_scope(lane_id=s) is not None:
                held.add(s)
            return False

        return sorted({s.id for fv in fvs for s in _surfaces_for(runner, fv, allowed)})

    async def list_apis(self, request: Request, call: Call) -> Response:
        caller = call.caller = self._caller(request)
        self._limit(caller)
        if request.method != "GET":
            raise Problem("method_not_allowed", headers={"Allow": "GET"})
        rows = []
        for api, eps in sorted(self._flows().items()):
            mine = self._visible(caller, api, eps)
            if not mine:
                continue
            meta = next(iter(mine.values())).definition.connector or {}
            rows.append({"api": api, "version": meta.get("version"), "state": "paused" if self.paused(api) else "active",
                         "surfaces": self._surfaces(caller, mine.values()), "openapi": f"{PREFIX}/{api}/openapi.json",
                         "endpoints": [{"endpoint": n, "title": fv.definition.title, "effect": str(fv.effect),
                                        "cache_s": int(http_of(fv).get("cache_s") or 0),
                                        "methods": ["GET", "POST"] if fv.effect == Effect.READ else ["POST"]}
                                       for n, fv in sorted(mine.items())]})
        return JSONResponse({"apis": rows}, headers={"Cache-Control": "no-store"})

    async def openapi(self, request: Request, call: Call) -> Response:
        caller = call.caller = self._caller(request)
        self._limit(caller)
        api = request.path_params["api"]
        mine = self._visible(caller, api, self._flows().get(api) or {})
        if not mine:
            raise Problem("not_found")
        if request.method != "GET":
            raise Problem("method_not_allowed", headers={"Allow": "GET"})
        meta = next(iter(mine.values())).definition.connector or {}
        doc = oas.document(api=api, title=api, version=str(meta.get("version")), endpoints=mine,
                           surfaces=self._surfaces(caller, mine.values()), server=str(request.base_url).rstrip("/"))
        return JSONResponse(doc, headers={"Cache-Control": "private, no-cache"})

    async def run_status(self, request: Request, call: Call) -> Response:
        caller = call.caller = self._caller(request)
        self._limit(caller)
        api = request.path_params["api"]
        eps = self._flows().get(api) or {}
        if not any(caller.sees_endpoint(api, n) for n in eps):
            raise Problem("not_found")
        if request.method != "GET":
            raise Problem("method_not_allowed", headers={"Allow": "GET"})
        runner = self._runner()
        try:
            run = runner.get(request.path_params["run"])
        except NotFound:
            raise Problem("not_found") from None
        name = (run.flow_id or "").rsplit(".", 1)[-1]
        head = eps.get(name)
        if head is None or head.id != run.flow_id or not caller.sees_endpoint(api, name) or not run.surface:
            raise Problem("not_found")
        fv = runner.flows.get(run.flow_id, run.flow_version)
        # a read is shared by every key that sees it; a draft or a commit only by the key that made it
        if (caller.key is not None and not caller.key.sees_surface(run.surface)) or (
                fv.effect != Effect.READ and not caller.owns(run)):
            raise Problem("not_found")
        if fv.effect == Effect.READ and self.paused(api):
            raise Problem("paused")
        ctx = self._context(caller, tool=tool_name(fv), lane_id=run.surface)
        self._gate(ctx, Effect.READ, run.surface)
        return await self._settled(request, run, api, name, fv, ctx)

    async def call_endpoint(self, request: Request, call: Call) -> Response:
        caller = call.caller = self._caller(request)
        api, name = request.path_params["api"], request.path_params["endpoint"]
        fv = (self._flows().get(api) or {}).get(name)
        if fv is not None and caller.sees_endpoint(api, name):
            call.api, call.endpoint, call.effect = api, name, str(fv.effect)  # a real name: it may go in a usage row
        self._limit(caller)
        if fv is None or call.api is None:
            raise Problem("not_found")
        allowed = "GET, POST" if fv.effect == Effect.READ else "POST"
        if request.method not in allowed.split(", "):
            raise Problem("method_not_allowed", headers={"Allow": allowed})
        if self.paused(api):
            raise Problem("paused")
        runner = self._runner()
        controls = read_controls(request)
        ctx = call.ctx = self._context(caller, tool=tool_name(fv))
        on = call.surface = self._surface(caller, fv, controls.on)
        ctx.lane_id = on
        self._gate(ctx, fv.effect, on)
        idempotency = self._idempotency(request, fv, caller)
        params = await self._params(request, fv)
        target = runner.connector_target(fv, on, params)
        key = cache_key(fv.id, fv.version, on, target.lane, target.params) if fv.effect == Effect.READ else None
        if key is not None:
            cache_s = int(http_of(fv).get("cache_s") or 0)
            ttl = cache_s if controls.max_age is None else min(cache_s, controls.max_age)
            entry = self.cache.fresh(key, ttl)
            if entry is not None:
                call.cached = True
                label_context(ctx, entry.value, source=f"{ctx.tool}:cache")
                return self._answer(request, api, name, fv, on, entry.value, hit=True,
                                    age=entry.age(self.clock.now().timestamp()))
        try:
            return await self._fresh(request, runner, ctx, api, name, fv, on, target, key, controls, idempotency)
        except Exception as e:  # noqa: BLE001 - becomes a problem, or a stale answer
            problem = from_exception(e, self.clock.now())
            stale = self.cache.fresh(key, STALE_MAX_S) if key is not None and controls.stale_ok and \
                problem.code in STALE_CODES else None
            if stale is None:
                raise problem from None
            call.cached = True
            label_context(ctx, stale.value, source=f"{ctx.tool}:cache")
            return self._answer(request, api, name, fv, on, stale.value, hit=True, stale=True,
                                age=stale.age(self.clock.now().timestamp()))

    def _surface(self, caller: Caller, fv: FlowVersion, wanted: str | None) -> str:
        held: set[str] = set()  # reached but frozen: those answer `frozen`, never `denied`
        mine = self._surfaces(caller, [fv], held)
        if wanted is not None:
            if wanted not in mine:
                raise Problem("frozen" if wanted in held else "denied")
            return wanted
        if len(mine) == 1:
            return mine[0]
        if not mine:
            raise Problem("frozen" if held else "denied")
        raise Problem("invalid_params", "This key sees more than one surface here: pass on.")

    def _idempotency(self, request: Request, fv: FlowVersion, caller: Caller) -> str | None:
        """A commit needs one. It is kept under the key's own id, so two keys can never meet on one."""
        if fv.effect == Effect.READ:
            return None  # a repeat of a read is what the cache is for
        value = request.headers.get("idempotency-key")
        if value is None:
            if fv.effect == Effect.COMMIT:
                raise Problem("invalid_params", "A commit needs an Idempotency-Key header.")
            return None
        if not 0 < len(value) <= MAX_IDEMPOTENCY or not value.isprintable():
            raise Problem("invalid_params", f"Idempotency-Key must be 1 to {MAX_IDEMPOTENCY} printable characters.")
        return f"{caller.id}:{value}"

    async def _params(self, request: Request, fv: FlowVersion) -> dict[str, Any]:
        schema = oas.input_schema(fv)
        query = [(k, v) for k, v in request.query_params.multi_items() if k not in CONTROLS]
        try:
            if request.method == "GET":
                params = oas.coerce_query(schema, query)
            else:
                if query:
                    raise Problem("invalid_params", "A POST takes its params as a JSON object in the body.")
                params = await self._body(request)
                unknown = sorted(set(params) - set(schema["properties"]))
                if unknown:
                    raise Problem("invalid_params", f"{unknown[0]}: this endpoint has no such param.")
        except oas.BadParam as e:
            raise Problem("invalid_params", f"{e.message}.") from None
        given = {k: v for k, v in params.items() if k not in BINDING_PARAMS}
        filled = {**{k: p["default"] for k, p in schema["properties"].items() if isinstance(p, dict) and "default" in p},
                  **given}
        problem = _schema_problem(schema, filled)
        if problem is not None:
            raise problem
        return given

    async def _body(self, request: Request) -> dict[str, Any]:
        length = request.headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > MAX_BODY:
            raise Problem("too_large")

        async def read() -> bytes:
            raw = bytearray()
            async for chunk in request.stream():
                raw += chunk
                if len(raw) > MAX_BODY:
                    raise Problem("too_large")
            return bytes(raw)

        try:
            raw = await asyncio.wait_for(read(), BODY_TIMEOUT_S)
        except TimeoutError:
            raise Problem("invalid_params", "The request body took too long to arrive.") from None
        try:
            data = json.loads(raw or b"{}")
            if _too_deep(data):
                raise Problem("invalid_params", f"The body is nested more than {MAX_DEPTH} levels deep.")
            canonical(data).encode()  # a lone surrogate parses, and then cannot be hashed or stored
        except (ValueError, RecursionError):
            raise Problem("invalid_params", "The body must be JSON.") from None
        if not isinstance(data, dict):
            raise Problem("invalid_params", "The body must be a JSON object.")
        return data

    def _lane_problem(self, lane: str) -> Problem | None:
        """What a run on this lane would meet now: a hold says so at once, rather than sit in the queue."""
        lanes = self.runtime.get("lanes")
        control = None
        fn = getattr(lanes, "control", None)
        if callable(fn):
            with contextlib.suppress(OpenajarError):
                control = fn(lane)
        state = getattr(control, "state", None)
        if state == ControlState.HUMAN:
            return Problem("lane_busy", retry_after=LANE_BUSY_RETRY_S)
        if state == ControlState.PAUSED:
            until = control.hold_until  # type: ignore[union-attr]
            if str(control.reason or "").startswith("backoff:"):  # type: ignore[union-attr]
                return Problem("rate_limited", retry_at=until, retry_after=seconds_until(until, self.clock.now()))
            return Problem("needs_human")
        get = getattr(lanes, "get", None)
        if callable(get):
            with contextlib.suppress(OpenajarError):
                if getattr(get(lane), "state", None) == LaneState.QUARANTINED:
                    return Problem("needs_human")
        return None

    async def _fresh(self, request: Request, runner: Any, ctx: Any, api: str, name: str, fv: FlowVersion, on: str,
                     target: Any, key: str | None, controls: Controls, idempotency: str | None) -> Response:
        existing = runner.existing_run(idempotency, flow_id=fv.id, lane=on) if idempotency else None
        if existing is None:
            held = self._lane_problem(target.lane)
            if held is not None:
                raise held

        async def start() -> str:
            out = await run_operation(ctx, fv.id, on, target.caller_params, 0, idempotency)
            return str(out["id"])

        def alive(run_id: str) -> bool:
            try:
                return not runner.get(run_id).terminal
            except NotFound:
                return False

        holder = ctx.principal.id
        with self._frozen_guard(ctx, on):
            while True:
                run_id, mine = await self.cache.flights.share(key, start, alive) if key is not None else (await start(), True)
                run = runner.get(run_id)
                # no place among the waiters: this call answers 202 at once, and the run goes on either way
                if controls.wait > 0 and run.status in (RunStatus.QUEUED, RunStatus.RUNNING) and self.gate.enter(holder):
                    try:
                        run = await self._waited(runner, run_id, controls.wait)
                    finally:
                        self.gate.leave(holder)
                # the first caller's grant ended under the run, and that says nothing of this caller's
                if mine or (run.error or {}).get("code") not in RUN_GONE:
                    break
        return await self._settled(request, run, api, name, fv, ctx)

    async def _waited(self, runner: Any, run_id: str, seconds: float) -> Run:
        """Until the run has an answer or `seconds` pass. A run that a hold put back in the queue has one
        now: nothing it waits for will come within a call."""
        left = seconds
        while True:
            step = min(left, WAIT_SLICE_S)
            run: Run = await runner.wait(run_id, step)
            left -= step
            if left <= 0 or run.status not in (RunStatus.QUEUED, RunStatus.RUNNING) or _held(run):
                return run

    async def _settled(self, request: Request, run: Run, api: str, name: str, fv: FlowVersion, ctx: Any) -> Response:
        """The answer for a run as it stands now."""
        out = run.output or {}
        if run.status in SUCCEEDED:
            if below_expected(fv, out.get("data")):
                raise Problem("empty_result", run=run.id)
            label_context(ctx, out, source=f"{ctx.tool}:{run.id}")
            value = {**out, "status": str(run.status), "run": run.id,
                     "read_at": (run.finished_at or self.clock.now()).isoformat(),
                     "version": (fv.definition.connector or {}).get("version")}
            return self._answer(request, api, name, fv, run.surface or "", value, hit=False, age=0)
        if run.status == RunStatus.WAITING_APPROVAL:
            return _pending("approval_pending", run, APPROVAL_WHY)
        if run.status == RunStatus.PARTIAL and run.needs_decision:
            return _pending("approval_pending", run, DECISION_WHY)
        if run.terminal:
            raise self._ended(run, ctx)
        if _held(run):
            if fv.effect == Effect.COMMIT:  # it may yet post: never say it failed while it can still run
                return _pending("pending", run, HOLD_WHY)
            problem = self._lane_problem(run.lane or "") or (
                Problem("paused") if self.paused(api) else
                Problem("connector_broken") if (run.error or {}).get("control") == "connector_broken" else
                Problem("rate_limited", retry_after=seconds_until(None, self.clock.now())))
            await self._runner().cancel(run.id, by="the site API call, because nothing asked it to wait")
            problem.run = run.id
            raise problem
        return _pending("pending", run, PENDING_WHY)

    def _ended(self, run: Run, ctx: Any) -> Problem:
        freeze = self.runtime.get("freeze")
        frozen = run.status == RunStatus.CANCELLED and freeze is not None and \
            any(freeze.frozen_for(lane) is not None for lane in (run.surface, run.lane))
        held = self._lane_problem(run.lane or "") if run.status == RunStatus.CANCELLED else None
        if held is not None and not frozen:
            held.run = run.id
            return held
        if (run.error or {}).get("code") in RUN_GONE and run.principal.get("id") != ctx.principal.id:
            return Problem("run_failed", run=run.id)  # another key's grant ended: this one is fine
        lanes = self.runtime.get("lanes")
        control = None
        fn = getattr(lanes, "control", None)
        if callable(fn) and run.lane:
            with contextlib.suppress(OpenajarError):
                control = fn(run.lane)
        return from_run(run, frozen=frozen, rest_until=getattr(control, "hold_until", None), now=self.clock.now())

    def _answer(self, request: Request, api: str, name: str, fv: FlowVersion, on: str, value: dict[str, Any], *,
                hit: bool, age: int, stale: bool = False) -> Response:
        data = value.get("data")
        private = value.get("private_fields") or []
        body = {"data": data, "status": value.get("status"), "untrusted_fields": value.get("untrusted_fields") or [],
                "private_fields": private, "truncated": bool(value.get("truncated")), "stale": stale,
                "cache": {"hit": hit, "age_s": age},
                "source": {"api": api, "endpoint": name, "version": value.get("version"), "surface": on,
                           "read_at": _z(value.get("read_at")), "run": value.get("run")}}
        etag = etag_of(data)
        # `no-cache`, never a lifetime: whoever stores this must come back, and so through the key check
        headers = {"Age": str(age), "ETag": etag, "Agentos-Run": str(value.get("run")),
                   "Cache-Control": "no-store" if private or http_of(fv).get("private_fields") else "private, no-cache"}
        if request.method == "GET" and _matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return JSONResponse(jsonable(body), headers=headers)

    async def unknown(self, request: Request, call: Call) -> Response:
        call.caller = self._caller(request)
        self._limit(call.caller)
        raise Problem("not_found")

    def routes(self) -> list[Route]:
        wrap = self._route
        return [Route(PREFIX, wrap(self.list_apis), methods=ALL_METHODS),
                Route(PREFIX + "/{api}/openapi.json", wrap(self.openapi), methods=ALL_METHODS),
                Route(PREFIX + "/{api}/runs/{run}", wrap(self.run_status), methods=ALL_METHODS),
                Route(PREFIX + "/{api}/{endpoint}", wrap(self.call_endpoint), methods=ALL_METHODS),
                Route(PREFIX + "/{rest:path}", wrap(self.unknown), methods=ALL_METHODS)]

    # ---- the owner's routes ----
    def owner_routes(self) -> list[Route]:
        api = self.api

        def person(request: Request) -> Any:
            """The owner signed in: never a token, the node's own included, and never a member."""
            auth = request.state.auth
            if (auth.via != "cookie" or auth.session is None or auth.access.member is not None or not auth.access.owner
                    or not auth.access.human):  # the device credential is a cookie too, and below a person
                raise ApiError("denied", "Only a person makes, edits or revokes a site API key: sign in to the panel, "
                                         "or use `openajar site-api keys` on the node.")
            if self.policy is None:
                raise ServiceUnavailable("site API keys need the policy service, which this node lacks")
            return auth.principal

        async def key_fields(request: Request) -> dict[str, Any]:
            data = await read_json_object(request)
            unknown = set(data) - KEY_CHANGES
            if unknown:
                raise InvalidRequest(f"a key has no {', '.join(sorted(unknown))}")
            return data

        def traced(request: Request, action: str, key: SiteKey) -> None:
            request.state.auth.access.trace(action, summary=f"site API key {key.label} ({key.prefix})")

        async def list_keys(request: Request) -> Any:
            return {"keys": [self.keys.public(k) for k in self.keys.all()]}

        async def add_key(request: Request) -> Any:
            who = person(request)
            key, secret = self.keys.add(**{"label": "", "apis": [], **await key_fields(request)}, by=who)
            traced(request, "site_key.create", key)
            return JSONResponse({"key": self.keys.public(key), "secret": secret}, status_code=201)

        async def edit_key(request: Request) -> Any:
            who = person(request)
            key = self.keys.edit(request.path_params["id"], by=who, **await key_fields(request))
            traced(request, "site_key.edit", key)
            return self.keys.public(key)

        async def revoke_key(request: Request) -> Any:
            key = self.keys.revoke(request.path_params["id"], by=person(request))
            traced(request, "site_key.revoke", key)
            return self.keys.public(key)

        async def usage(request: Request) -> Any:
            name = request.path_params["api"]
            drafted = self.runtime.get("site_apis")
            known = callable(getattr(drafted, "state_of", None)) and drafted.state_of(name) is not None
            if name not in self._flows() and not known:
                raise NotFound(f"no site API {name}")
            return self.usage.chart(name, hours=query_int(request, "hours", DEFAULT_HOURS, 1, MAX_HOURS),
                                    endpoint=request.query_params.get("endpoint") or None)

        return [Route("/api/site-keys", endpoint(api, list_keys, owner=True, action="site_key.list"), methods=["GET"]),
                Route("/api/site-keys", endpoint(api, add_key, owner=True, action="site_key.create"), methods=["POST"]),
                Route("/api/site-keys/{id}", endpoint(api, edit_key, owner=True, action="site_key.edit"), methods=["PATCH"]),
                Route("/api/site-keys/{id}", endpoint(api, revoke_key, owner=True, action="site_key.revoke"),
                      methods=["DELETE"]),
                Route("/api/site-apis/{api}/usage", endpoint(api, usage, owner=True, action="site_api.usage"),
                      methods=["GET"])]


class KeysStayOnV1:
    """A site key opens /v1/sites and nothing else. /api and /mcp refuse it on their own; this makes it so
    for the panel's public pages too, so a key in the wrong place fails loudly instead of loading a page."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and path != PREFIX and not path.startswith(PREFIX + "/"):
            header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            if KEY_RE.fullmatch(header.partition(" ")[2].strip()):
                await Problem("unauthorized", "A site API key opens /v1/sites and nothing else.").response()(
                    scope, receive, send)
                return
        await self.app(scope, receive, send)


def routes(api: Api) -> list[Route]:
    """/v1/sites and the owner's routes, on a node with a home for their database."""
    home = api.settings.home or getattr(api.config, "home", None)
    if home is None:
        return []
    try:
        site = SiteHttp(api, home=Path(home))
    except ServiceUnavailable as e:  # the panel still comes up: /v1/sites says what it can
        log.warning("site API keys and cache are not available: %s", e.message)

        async def offline(_request: Request) -> Response:
            return Problem("node_offline").response()

        return [Route(PREFIX, offline, methods=ALL_METHODS), Route(PREFIX + "/{rest:path}", offline, methods=ALL_METHODS)]
    api.runtime.register("site_http", site)
    return [*site.routes(), *site.owner_routes()]
