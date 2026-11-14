"""Site API keys (docs/SITE-APIS.md, Keys).

A key is `aos_` and 43 base64url characters: 256 random bits from `secrets`. It is shown once. Only its
SHA-256 is stored, with an 8 character prefix for showing and for finding the row; the hashes under a
prefix are then compared in constant time.

Each key is backed by a grant from `policy.issue`: subject `site-key:<id>`, lanes the key's surfaces and
the lanes behind them, domains its APIs' domains, ceiling the key's. A call runs under that grant, so the policy treats it as
it treats an agent's call. The grant's token is dropped where it is issued, here, and is nowhere else:
what a key holder has can only ever open /v1/sites, and nothing else can act as the key.

A key's two rows are in two databases, so "the same step" cannot be one transaction. The grant goes
first: a key whose row could not be written after that is refused anyway, since every call checks the
grant too.

A key with an end has a grant that outlives it, so that grant is never replaced. A key with no end, or
one past the longest grant, gets a new grant near the old one's end, or on its next call once that has
simply run out. The old one stays live until its own end, so what is queued or parked under it finishes,
and the key remembers it (`old_grants`) so that revoking the key ends them all. A new grant is a new
taint family, so each one starts with what the old family holds: a renewal never clears the labels.

Only a person makes, edits or revokes a key: the owner in the panel or at the node's CLI, never an
agent, whatever its grant.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from agentos.core import clock
from agentos.core.errors import Conflict, InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect, Principal
from agentos.core.ratelimit import RateLimiter
from agentos.runner.owner import is_owner
from agentos.runner.siteapis.http_store import HttpStore

KEY_RE = re.compile(r"aos_[A-Za-z0-9_-]{43}")
SHOWN_CHARS = 8                # `aos_ab12`
SUBJECT = "site-key:"
OWNER = "owner"
KEY_CHANGES = frozenset({"label", "apis", "endpoints", "surfaces", "ceiling", "rate_per_min", "expires_at"})
CEILINGS = (Effect.READ, Effect.DRAFT, Effect.COMMIT)  # pay and irreversible are never on a key
DEFAULT_RATE = 60
MAX_RATE = 6000
MAX_LABEL = 80
RENEW_S = 30 * 86400           # a key that outlives its grant gets a new one this close to the end,
                               # or half way through a shorter grant, or it would renew on every call
TOUCH_S = 60.0                 # last_used_at is written at most this often per key
WINDOW_S = 60.0
WAITERS_PER_KEY = 8            # calls of one key held open for their run at once
WAITERS_ALL = 64


def new_token() -> str:
    return "aos_" + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def iso(ts: float | None) -> str | None:
    return clock.iso(ts, z=True, timespec="seconds") if ts else None  # 0 is "never", as before


def parse_time(value: Any) -> float | None:
    """An ISO 8601 time with a timezone, or Unix seconds, as Unix seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        raise InvalidRequest("expires_at must be an ISO 8601 time like 2027-01-31T00:00:00Z") from None
    if when.tzinfo is None:
        raise InvalidRequest("expires_at needs a timezone, like 2027-01-31T00:00:00Z")
    return when.timestamp()


@dataclass(frozen=True)
class Api:
    """What a key can be scoped to: one site API on this node."""
    key: str
    endpoints: tuple[str, ...]
    surfaces: Mapping[str, str]   # surface id: the lane behind it
    domains: tuple[str, ...]


Catalog = Callable[[], Mapping[str, Api]]


@dataclass(frozen=True)
class SiteKey:
    id: str
    label: str
    prefix: str
    hash: str
    apis: list[str]
    endpoints: list[str] | None   # `<api>.<endpoint>`; None: every endpoint of `apis`
    surfaces: list[str] | None    # None: every surface bound when the key was made or last edited
    ceiling: str
    rate_per_min: int
    grant_id: str
    old_grants: dict[str, float]  # grants a renewal replaced that may still be live: id, its end
    expires_at: float | None
    last_used_at: float | None
    created_at: float
    created_by: str
    revoked_at: float | None
    revoked_by: str | None

    @property
    def subject(self) -> str:
        return SUBJECT + self.id

    def expired(self, now: float) -> bool:
        return self.expires_at is not None and self.expires_at <= now

    def sees_api(self, api: str) -> bool:
        return api in self.apis

    def sees_endpoint(self, api: str, endpoint: str) -> bool:
        return self.sees_api(api) and (self.endpoints is None or f"{api}.{endpoint}" in self.endpoints)

    def sees_surface(self, surface: str) -> bool:
        return self.surfaces is None or surface in self.surfaces

    def public(self, *, state: str | None = None) -> dict[str, Any]:
        """What the panel and the CLI show: the label and the prefix, never the hash."""
        d = {k: v for k, v in asdict(self).items() if k not in ("hash", "grant_id", "old_grants")}
        for name in ("expires_at", "last_used_at", "created_at", "revoked_at"):
            d[name] = iso(d[name])
        return {**d, "grant": self.grant_id, "state": state or ("revoked" if self.revoked_at else "active")}


def _person(by: Principal | Mapping[str, Any] | None) -> str:
    """The owner in person: never an agent, a flow or an install record, whatever its grant allows, and
    never a member, who `is_owner` would pass, since a member is the owner of their own principal."""
    p = by.model_dump() if isinstance(by, Principal) else dict(by or {})
    if p.get("kind") != "human" or p.get("id") != OWNER or not is_owner(p):
        raise PolicyDenied("only a person makes, edits or revokes a site API key (the panel or the node's CLI)")
    return str(p["id"])


def _names(value: Iterable[str] | None, what: str) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str) or not all(isinstance(v, str) and v for v in value):
        raise InvalidRequest(f"{what} must be a list of names")
    return sorted(set(value))


def _label(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > MAX_LABEL or not text.isprintable():
        raise InvalidRequest(f"a key needs a label of at most {MAX_LABEL} characters")
    return text


def _rate(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RATE:
        raise InvalidRequest(f"rate_per_min must be a whole number from 1 to {MAX_RATE}")
    return value


def _ceiling(value: Any) -> str:
    text = str(getattr(value, "value", value))
    if text not in {e.value for e in CEILINGS}:
        raise InvalidRequest("ceiling must be read, draft or commit; pay and irreversible are never on a key")
    return text


class SiteKeys:
    def __init__(self, store: HttpStore, policy: Any, catalog: Catalog, *, clock: Callable[[], float] = time.time,
                 frozen: Callable[[SiteKey], bool] = lambda _key: False):
        self.store, self.policy, self.catalog, self.clock, self.frozen = store, policy, catalog, clock, frozen
        self._touched: dict[str, float] = {}

    # ---- reading ----
    def all(self) -> list[SiteKey]:
        return [SiteKey(**r) for r in self.store.all_keys()]

    def get(self, key_id: str) -> SiteKey:
        row = self.store.key(key_id)
        if row is None:
            raise NotFound(f"no site API key {key_id}")
        return SiteKey(**row)

    def lookup(self, token: str) -> SiteKey | None:
        """The key this token is, by a constant time compare of the hashes, or None."""
        if not KEY_RE.fullmatch(token):
            return None
        digest, found = token_hash(token), None
        for row in self.store.keys_by_prefix(token[:SHOWN_CHARS]):
            if hmac.compare_digest(row["hash"], digest):
                found = row
        return SiteKey(**found) if found else None

    def state(self, key: SiteKey) -> str:
        """active, revoked, expired, or `grant_ended` when a freeze or the grants page ended its grant."""
        if key.revoked_at:
            return "revoked"
        if key.expired(self.clock()):
            return "expired"
        try:
            self.policy.grant_by_id(key.grant_id)
        except PolicyDenied as e:
            return "active" if self._lapsed(key, e) else "grant_ended"  # its next call gets a new grant
        return "active"

    def public(self, key: SiteKey) -> dict[str, Any]:
        return key.public(state=self.state(key))

    # ---- writing (a person) ----
    def add(self, *, label: str, apis: Iterable[str], endpoints: Iterable[str] | None = None,
            surfaces: Iterable[str] | None = None, ceiling: Any = Effect.READ, rate_per_min: int = DEFAULT_RATE,
            expires_at: Any = None, by: Principal | Mapping[str, Any] | None) -> tuple[SiteKey, str]:
        """Make a key. Returns (key, the key's text): this is the only time the text exists."""
        who = _person(by)
        now = self.clock()
        scope, reach = self._scope(apis, endpoints, surfaces, ceiling)
        when = self._expiry(expires_at, now)
        token = new_token()
        row = {"id": "key_" + secrets.token_hex(8), "label": _label(label), "prefix": token[:SHOWN_CHARS],
               "hash": token_hash(token), "rate_per_min": _rate(rate_per_min), "old_grants": {}, "expires_at": when,
               "last_used_at": None,
               "created_at": now, "created_by": who, "revoked_at": None, "revoked_by": None, **scope}
        row["grant_id"] = self._issue(row["id"], row["label"], scope["ceiling"], reach, when, who)
        try:
            self.store.add_key(row)
        except BaseException:
            self.policy.revoke(row["grant_id"], by=who, reason="the key could not be saved")
            raise
        return SiteKey(**row), token

    def edit(self, key_id: str, *, by: Principal | Mapping[str, Any] | None, **changes: Any) -> SiteKey:
        """Change a key. A change of scope or of its end is a new grant: every old one is revoked, and a
        run still queued under one stops, because the person just took that scope away. What the key
        read still counts: only clearing the taint, in person, starts it clean."""
        who = _person(by)
        key = self.get(key_id)
        if key.revoked_at:
            raise Conflict(f"site API key {key_id} is revoked; make a new one")
        unknown = set(changes) - KEY_CHANGES
        if unknown:
            raise InvalidRequest(f"a key has no {', '.join(sorted(unknown))}")
        fields: dict[str, Any] = {}
        if "label" in changes:
            fields["label"] = _label(changes["label"])
        if "rate_per_min" in changes:
            fields["rate_per_min"] = _rate(changes["rate_per_min"])
        when = self._expiry(changes["expires_at"], self.clock()) if "expires_at" in changes else key.expires_at
        scoped = {"apis", "endpoints", "surfaces", "ceiling", "expires_at"} & set(changes)
        if scoped:
            scope, reach = self._scope(changes.get("apis", key.apis), changes.get("endpoints", key.endpoints),
                                       changes.get("surfaces", key.surfaces), changes.get("ceiling", key.ceiling))
            grant = self._issue(key.id, fields.get("label", key.label), scope["ceiling"], reach, when, who)
            for old in (key.grant_id, *key.old_grants):
                self._carry(old, grant)
            fields |= {**scope, "expires_at": when, "grant_id": grant, "old_grants": {}}
        if fields:
            self.store.update_key(key.id, **fields)
        if scoped:
            self._end_grants(key, who, "the key was edited")
        return self.get(key.id)

    def revoke(self, key_id: str, *, by: Principal | Mapping[str, Any] | None) -> SiteKey:
        """Revoke the key and every grant of it. From the next call on it answers 401."""
        who = _person(by)
        key = self.get(key_id)
        if key.revoked_at:
            return key
        self._end_grants(key, who, "the key was revoked")
        self.store.update_key(key.id, revoked_at=self.clock(), revoked_by=who, old_grants={})
        return self.get(key.id)

    # ---- calls ----
    def principal(self, key: SiteKey) -> Principal:
        return Principal(kind="agent", id=key.subject, grant_id=key.grant_id)

    def context(self, key: SiteKey, *, tool: str | None = None, lane_id: str | None = None) -> Any:
        """The policy context of one call: the backing grant, checked live. Raises Denied when it is
        revoked or past its end."""
        return self.policy.context_for_grant(key.grant_id, principal=self.principal(key), tool=tool, lane_id=lane_id)

    def used(self, key: SiteKey) -> SiteKey:
        """Note that a call came in, and give a long lived key a new grant when its old one is near its
        end or has run out."""
        now = self.clock()
        if now - self._touched.get(key.id, 0.0) >= TOUCH_S:
            self._touched[key.id] = now
            self.store.soon(self.store.update_key, key.id, last_used_at=now)  # a touch: never waits for a lock
        key = self._renewed(key, now)
        for old, end in key.old_grants.items():  # a call or a run still under it labels the old family
            if end > now:
                self._carry(old, key.grant_id)
        return key

    def _renewed(self, key: SiteKey, now: float) -> SiteKey:
        try:
            end = self.policy.grant_by_id(key.grant_id).expires_at
            near = min(RENEW_S, self.policy.cfg.max_grant_ttl_s // 2)
            if end is None or end - now > near or (key.expires_at is not None and key.expires_at <= end):
                return key
        except PolicyDenied as e:
            if not self._lapsed(key, e) or self.frozen(key):  # the sweep passes over a grant that ran out
                raise
            end = None
        block = self.policy.grant_record(key.grant_id)["scope"]["blocks"][0]
        reach = {"lanes": block.get("lanes") or [], "domains": block.get("domains") or []}
        new = self._issue(key.id, key.label, key.ceiling, reach, key.expires_at, key.created_by)
        self._carry(key.grant_id, new)
        skew = self.policy.cfg.clock_skew_s
        old = {g: e for g, e in key.old_grants.items() if e + skew > now} | ({key.grant_id: end} if end else {})
        self.store.update_key(key.id, grant_id=new, old_grants=old)
        return self.get(key.id)

    def _lapsed(self, key: SiteKey, denied: PolicyDenied) -> bool:
        """Its grant simply ran out. The grant check reads the time first, so a revoked grant past its
        end says `expired` too; the record tells them apart, and a revocation wins there."""
        return getattr(denied, "reason", None) == "expired" and self.policy.grant_record(key.grant_id)["state"] == "expired"

    # ---- the grant ----
    def _expiry(self, value: Any, now: float) -> float | None:
        when = parse_time(value)
        if when is not None and when <= now:
            raise InvalidRequest("expires_at must be in the future")
        return when

    def _scope(self, apis: Iterable[str], endpoints: Iterable[str] | None, surfaces: Iterable[str] | None,
               ceiling: Any) -> tuple[dict[str, Any], dict[str, list[str]]]:
        """The key's scope, checked against the APIs on this node, and the lanes and domains of its grant."""
        names = _names(apis, "apis")
        if not names:
            raise InvalidRequest("a key needs at least one site API in apis")
        known = self.catalog()
        missing = [a for a in names if a not in known]
        if missing:
            raise InvalidRequest(f"no site API {', '.join(missing)} on this node")
        wanted = _names(endpoints, "endpoints")
        if wanted is not None:
            wanted = sorted({e if "." in e or len(names) > 1 else f"{names[0]}.{e}" for e in wanted})
            every = {f"{a}.{e}" for a in names for e in known[a].endpoints}
            if not wanted or set(wanted) - every:
                raise InvalidRequest("endpoints must name endpoints of the key's APIs, like harbor-supplier.open_orders")
        bound = {s: lane for a in names for s, lane in known[a].surfaces.items()}
        chosen = _names(surfaces, "surfaces")
        if chosen is not None and (not chosen or set(chosen) - set(bound)):
            raise InvalidRequest("surfaces must name surfaces the key's APIs are bound to")
        reached = chosen if chosen is not None else list(bound)
        if not reached:
            raise InvalidRequest("these site APIs are bound to no surface yet, so a key could reach nothing")
        # a call is checked on the surface, and a freeze's sweep finds a grant by the lane behind it
        return ({"apis": names, "endpoints": wanted, "surfaces": chosen, "ceiling": _ceiling(ceiling)},
                {"lanes": sorted({*reached, *(bound[s] for s in reached)}),
                 "domains": sorted({d for a in names for d in known[a].domains})})

    def _issue(self, key_id: str, label: str, ceiling: str, reach: dict[str, list[str]], end: float | None,
               by: str) -> str:
        now = self.clock()
        ttl = self.policy.cfg.max_grant_ttl_s
        if end is not None:
            ttl = min(ttl, math.ceil(end - now) + 1)  # past the key's end: `issue` rounds its own time down
        grant, _token = self.policy.issue(subject={"kind": "agent", "id": SUBJECT + key_id}, _reserved=True,
                                          lanes=reach["lanes"],
                                          domains=reach["domains"] or None, ceiling=ceiling, ttl_s=ttl, depth=0,
                                          note=f"site API key: {label}"[:200], by=by)
        return grant.id  # and the token ends here: nothing can present this grant

    def _carry(self, old: str, new: str) -> None:
        """What a family read goes with the key: a key's grant is a root, so its family is its own id
        (policy.taint.family_key), and one that ran out can no longer be asked for."""
        family = f"family:{old}"
        self.policy.taint.inherit(family, family, f"family:{new}", source=f"site key grant {old}")

    def _end_grants(self, key: SiteKey, by: str, reason: str) -> None:
        for grant_id in (key.grant_id, *key.old_grants):
            self._end_grant(grant_id, by, reason)

    def _end_grant(self, grant_id: str, by: str, reason: str) -> None:
        try:
            self.policy.revoke(grant_id, by=by, reason=reason)
        except (Conflict, NotFound):  # a freeze or the grants page ended it first
            pass


# ---------------------------------------------------------------- limits


class FailedLookups(RateLimiter):
    """Bad keys per source, in the style of agentos.controller.hooks: a source that used up its tries
    is refused before any lookup, whatever it sends. The source is the peer address, because a sender
    can write X-Forwarded-For."""

    def blocked(self, source: str) -> float | None:
        """Seconds to wait, or None. Records nothing."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(source)
            if q is None:
                return None
            self._trim(q, now)
            return max(1.0, q[0] + self.window - now) if len(q) >= self.per_key else None


@dataclass(frozen=True)
class Rate:
    """One key's limit as of this call, for the RateLimit-* headers."""
    limit: int
    remaining: int
    reset_s: int
    retry_after: int | None = None   # set when this call is over the limit

    def headers(self) -> dict[str, str]:
        out = {"RateLimit-Limit": str(self.limit), "RateLimit-Remaining": str(self.remaining),
               "RateLimit-Reset": str(self.reset_s)}
        return out | ({"Retry-After": str(self.retry_after)} if self.retry_after is not None else {})


class KeyGate:
    """How much one key and one source may do: calls per minute, failed lookups, and calls held open."""

    def __init__(self, *, failed_per_source: int = 20, failed_everyone: int = 20_000, window: float = WINDOW_S,
                 waiters_per_key: int = WAITERS_PER_KEY, waiters_all: int = WAITERS_ALL,
                 clock: Callable[[], float] = time.monotonic):
        self.failed = FailedLookups(per_key=failed_per_source, global_max=failed_everyone, window=window)
        self.window, self.clock = window, clock
        self.waiters_per_key, self.waiters_all = waiters_per_key, waiters_all
        self._calls: dict[str, deque[float]] = {}
        self._waiting: dict[str, int] = {}
        self._lock = threading.Lock()

    def take(self, key_id: str, limit: int) -> Rate:
        """Count one call against the key, unless it is over the limit: a refused call costs nothing."""
        now = self.clock()
        with self._lock:
            q = self._calls.setdefault(key_id, deque())
            while q and q[0] <= now - self.window:
                q.popleft()
            over = len(q) >= limit
            if not over:
                q.append(now)
            reset = max(1, int(q[0] + self.window - now + 0.999)) if q else 0
            return Rate(limit, max(0, limit - len(q)), reset, reset if over else None)

    def enter(self, key_id: str) -> bool:
        """A place among the calls held open for their run, or False at once: such a call answers 202."""
        with self._lock:
            if self._waiting.get(key_id, 0) >= self.waiters_per_key or sum(self._waiting.values()) >= self.waiters_all:
                return False
            self._waiting[key_id] = self._waiting.get(key_id, 0) + 1
            return True

    def leave(self, key_id: str) -> None:
        with self._lock:
            left = self._waiting.get(key_id, 1) - 1
            if left > 0:
                self._waiting[key_id] = left
            else:
                self._waiting.pop(key_id, None)
