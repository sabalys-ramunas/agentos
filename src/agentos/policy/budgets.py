"""Budgets and meters: step 5 of the authorization order.

Part of PolicyService (agentos.policy.service), as a mixin. `reserve` debits, in one policy.db
transaction, the budget of every block in a grant's chain, a brain's daily quota, the day's meters
of the chain and the lane (agentos.policy.meters) and, for a commit, a row of the lane's posting
pace (agentos.policy.lane_pace): all or nothing, and with `strict` a call that would pass any of
them is refused before anything is debited. Money is counted in whole micro-dollars, a cost rounded
up and a limit rounded down, so rounding never grows a budget.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from agentos.core.errors import InvalidRequest
from agentos.core.models import Effect, Principal, parse_lane_id
from agentos.policy.errors import Denied
from agentos.policy.grants import OWNER_GRANT_ID, Grant, tier_rank
from agentos.policy.meters import COMMIT_EFFECTS, LimitReached, Limits, Scope
from agentos.policy.traces import Cost, TraceEvent
from agentos.policy.util import utc_day

log = logging.getLogger("agentos.policy")

_MICRO = 1_000_000
MAX_COST_TOKENS = 10**15     # far above any real call; keeps counters inside SQLite's 64-bit integers
MAX_COST_USD = 10**9


def micros_up(usd: float) -> int:
    """Micro-dollars for a cost, rounded up: a cost under half a micro-dollar is still debited
    (BUG-12). Decimal on the shortest repr, so 0.1 is exactly 100000, not 100001."""
    return int((Decimal(repr(usd)) * _MICRO).to_integral_value(rounding=ROUND_CEILING))


def micros_down(usd: float) -> int:
    """Micro-dollars for a limit, rounded down (a budget never grows by rounding)."""
    return int((Decimal(repr(usd)) * _MICRO).to_integral_value(rounding=ROUND_FLOOR))


@dataclass
class Reservation:
    entries: list[tuple[str, int]] = field(default_factory=list)
    pace_ids: list[int] = field(default_factory=list)  # pace ledger rows (agentos.policy.pace) it wrote


def _parse_cost(cost: Any) -> tuple[int, float]:
    if cost is None:
        return 0, 0.0
    if isinstance(cost, Cost):
        tokens_, usd = cost.tokens, cost.usd
    elif isinstance(cost, dict):
        tokens_, usd = cost.get("tokens", 0) or 0, cost.get("usd", 0.0) or 0.0
    else:
        raise InvalidRequest("cost must be {tokens, usd}")
    try:
        usd = float(usd)
        if not math.isfinite(usd) or (isinstance(tokens_, float) and not math.isfinite(tokens_)):
            raise InvalidRequest("cost must be a finite number (not NaN or Infinity)")
        tokens_ = int(tokens_)
    except (TypeError, ValueError, OverflowError):
        raise InvalidRequest("cost must be {tokens, usd}") from None
    if tokens_ < 0 or usd < 0:
        raise InvalidRequest("cost cannot be negative")
    if tokens_ > MAX_COST_TOKENS or usd > MAX_COST_USD:
        raise InvalidRequest("cost is far too large for one call")
    return tokens_, usd


class BudgetsMixin:
    """Budget, quota and meter methods of PolicyService (the attributes it reads, `db`, `cfg`,
    `meters`, `pace`, `traces`, `bus`, `notifier`, `clock`, are set by PolicyService.__init__)."""

    def brain_allowance(self, principal: Principal | dict) -> dict:
        """What the principal's grant allows for brains (agentos.brains GrantView): metered spend
        only with a pay ceiling (metered use is treated like pay), the remaining budget and daily
        brain quota, and the highest model tier (1 small, 2 standard, 3 frontier)."""
        g = self.grant_for(principal)
        caps = g.caps("brain")
        if caps is None:
            return {"allow_metered": False, "remaining_usd": 0.0, "remaining_tokens": 0, "max_tier": None,
                    "brains": []}
        rem = self.remaining(g)
        tokens_left, usd_left = rem["tokens"], rem["usd"]
        for key, limit, _ in self._quota_entries(g, 0, 0.0):
            left = max(0, limit - self._used(key))
            if key.startswith("quota:tok:"):
                tokens_left = left if tokens_left is None else min(tokens_left, left)
            else:
                usd_left = left / _MICRO if usd_left is None else min(usd_left, left / _MICRO)
        ranks = [tier_rank(c.tier, self.cfg.brain_tiers, self.cfg.brain_tier_aliases) for c in caps if c.tier]
        max_tier = None
        if ranks:
            max_tier = 0 if any(r is None for r in ranks) else min(ranks) + 1  # an unknown tier allows none
        return {"allow_metered": g.ceiling.rank >= Effect.PAY.rank, "remaining_usd": usd_left,
                "remaining_tokens": tokens_left, "max_tier": max_tier, "brains": None}

    # ================================================================ budgets
    def _budget_entries(self, g: Grant, tokens_: int, usd: float) -> list[tuple[str, int, int]]:
        usd_m = micros_up(usd)
        out = []
        for bid, b in g.budgets():
            if b.tokens is not None:
                out.append((f"budget:tok:{bid}", b.tokens, tokens_))
            if b.usd is not None:
                out.append((f"budget:usd:{bid}", micros_down(b.usd), usd_m))
        return out

    def _quota_entries(self, g: Grant, tokens_: int, usd: float) -> list[tuple[str, int, int]]:
        day = utc_day(self.clock())
        out = []
        for b in g.blocks:
            cap = b.specials.brain if b.specials is not None else None
            if cap is None:
                continue
            if cap.quota_tokens is not None:
                out.append((f"quota:tok:{b.id}:{day}", cap.quota_tokens, tokens_))
            if cap.quota_usd is not None:
                out.append((f"quota:usd:{b.id}:{day}", micros_down(cap.quota_usd), micros_up(usd)))
        return out

    def _used(self, key: str, conn: Any = None) -> int:
        q = "SELECT used FROM counters WHERE key = ?"
        row = (conn.execute(q, (key,)).fetchone() if conn is not None else self.db.one(q, (key,)))
        return int(row["used"]) if row else 0

    def reserve(self, g: Grant, *, tokens: int = 0, usd: float = 0.0, brain: bool = False,
                strict: bool = True, effect: Effect | None = None, lane_id: str | None = None,
                action: bool = True, pace: bool = True, posts: bool = True,
                at: float | None = None) -> Reservation:
        """Atomically debit the budget of every block in the chain (and the brain quota when
        `brain`), and the day's meters of the chain and `lane_id` (else the lane the grant implies,
        implied_lane), an action of `effect` when given (`action` False: only its commit counters).
        strict: all or nothing, Denied("budget") when any budget would be passed, and LimitReached
        (reason limit_reached) when a meter's limit is reached. A commit on a lane is also one row of
        the lane's pace ledger at `at` (else now), and with `strict` and `pace` its posting pace is
        checked first (LanePaced, agentos.policy.pace); `pace` False: the owner in person (a
        member in person, and every grant, is paced). `posts` False: a call that changes only the
        node (_node_local), which is neither checked nor a row."""
        tokens, usd = _parse_cost({"tokens": tokens, "usd": usd})
        entries = [(k, lim, amt) for k, lim, amt in self._budget_entries(g, tokens, usd) if amt]
        quota = [(k, lim, amt) for k, lim, amt in self._quota_entries(g, tokens, usd) if amt] if brain else []
        now = self.clock()
        eff = effect.value if effect is not None else None
        meter = self.meters.entries(self._meter_scopes(g, lane_id), eff, tokens, micros_up(usd), now, action=action)
        paced_lane = self._pace_lane(g, lane_id) if eff in COMMIT_EFFECTS and posts else None
        when = max(now, at) if at is not None else now
        if not entries and not quota and not meter and paced_lane is None:
            return Reservation()
        limited = self._meter_limited(g, lane_id) if strict else []
        with self.db.tx() as conn:
            if strict:
                for key, limit, amt in entries:
                    if self._used(key, conn) + amt > limit:
                        raise Denied("budget", "this call would go past the grant's budget")
                self.meters.check(lambda k: self._used(k, conn), limited, eff, tokens, micros_up(usd), now)
                if paced_lane is not None and pace:
                    self.pace_check(paced_lane, when, conn=conn)
            rows = [(k, a) for k, _, a in [*entries, *quota]] + meter
            for key, amt in rows:
                conn.execute("INSERT INTO counters (key, used, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) "
                             "DO UPDATE SET used = used + excluded.used, updated_at = excluded.updated_at",
                             (key, amt, now))
            ids = [self.pace.record(conn, paced_lane, when)] if paced_lane is not None else []
        return Reservation(rows, ids)

    def refund(self, res: Reservation) -> None:
        if res.pace_ids:
            self.pace.forget(res.pace_ids)
        if not res.entries:
            return
        with self.db.tx() as conn:
            for key, amt in res.entries:
                conn.execute("UPDATE counters SET used = MAX(0, used - ?), updated_at = ? WHERE key = ?",
                             (amt, self.clock(), key))

    def _check_budget_open(self, g: Grant) -> None:
        for key, limit, _ in self._budget_entries(g, 0, 0.0):
            if self._used(key) >= limit:
                raise Denied("budget", "this grant's budget is spent")

    def remaining(self, g: Grant) -> dict:
        out: dict[str, Any] = {"tokens": None, "usd": None}
        for key, limit, _ in self._budget_entries(g, 0, 0.0):
            left = max(0, limit - self._used(key))
            if key.startswith("budget:tok:"):
                out["tokens"] = left if out["tokens"] is None else min(out["tokens"], left)
            else:
                usd_left = left / _MICRO
                out["usd"] = usd_left if out["usd"] is None else min(out["usd"], usd_left)
        return out

    # ================================================================ meters (agentos.policy.meters)
    @staticmethod
    def _grant_label(g: Grant) -> str:
        if g.owner_token:
            return "the owner token"
        sub = g.subject
        return f"{sub.kind} {sub.id} ({g.id})"

    @staticmethod
    def implied_lane(g: Grant) -> str | None:
        """The one lane a grant can reach, when a block of its chain scopes it to a single lane id (a
        brain's child grant for one lane, a grant issued for one lane): a call that names no lane
        counts against that lane's meter too. A grant that reaches several lanes implies none, so
        its calls without a lane (workspace_apply, memory writes, brain runs) are grant-metered only."""
        for i, b in enumerate(g.blocks):
            pats = b.lane_patterns(authority=i == 0)
            if pats is not None and len(pats) == 1 and not any(c in pats[0] for c in "*?[") \
                    and g.lane_allowed(pats[0]):
                return pats[0]
        return None

    def _meter_scopes(self, g: Grant, lane_id: str | None, *, implied: bool = True) -> list[Scope]:
        """What one call counts against: every block of the chain, and the lane it names (else the
        lane the grant implies). A surface counts on the lane it posts through as well: that lane's
        limits hold whoever the login posts as."""
        label = self._grant_label(g)
        out = [Scope("grant", b.id, label if b.id == g.id else b.id) for b in g.blocks]
        lane_id = lane_id or (self.implied_lane(g) if implied and not g.owner_token else None)
        through = self._pace_lane(g, lane_id)
        return out + [Scope("lane", s) for s in dict.fromkeys((lane_id, through)) if s]

    def _meter_limited(self, g: Grant, lane_id: str | None, *, implied: bool = True) -> list[tuple[Scope, Limits]]:
        """The scopes of one call that carry a daily or hourly limit, with their limits."""
        out = []
        for s in self._meter_scopes(g, lane_id, implied=implied):
            if s.kind == "lane":
                lim, _ = self.meters.lane_limits(s.id)
            else:
                lim = Limits.of(next(b.budget for b in g.blocks if b.id == s.id))
            if lim is not None:
                out.append((s, lim))
        return out

    def meter_notices(self, g: Grant, lane_id: str | None) -> list[dict]:
        """After a call went through: a warning or cutoff notice for each threshold crossed for the
        first time in its window (a `meter` trace, a `meter` event and a push). Never raises."""
        try:
            limited = self._meter_limited(g, lane_id)
            notices = self.meters.crossed(limited, self.clock()) if limited else []
        except Exception:
            log.exception("meter thresholds could not be checked")
            return []
        return self._send_meter_notices(notices, g, lane_id)

    def meter_refused(self, g: Grant, lane_id: str | None, e: LimitReached) -> list[dict]:
        """A call a limit refused: the cutoff notice, once per limit and window (Meters.refused).
        Never raises."""
        try:
            n = self.meters.refused(e, self.clock())
        except Exception:
            log.exception("a refused call's meter notice could not be checked")
            return []
        return self._send_meter_notices([n] if n is not None else [], g, lane_id)

    def _meter_recipients(self, g: Grant | None) -> list[str]:
        """Who a meter notice goes to: the node owner, and the member the grant belongs to (its
        subject's `owner`), never another member, whatever lanes they control."""
        out = ["owner"]
        sub_owner = getattr(getattr(g, "subject", None), "owner", None) if g is not None and not g.owner_token else None
        if isinstance(sub_owner, str) and sub_owner and sub_owner not in out:
            out.append(sub_owner)
        return out

    def _send_meter_notices(self, notices: list[dict], g: Grant | None, lane_id: str | None) -> list[dict]:
        """Trace, publish and push each notice. A notice whose trace or event fails gives its marks
        back (Meters.release), so the next call sends it again."""
        sent = []
        for n in notices:
            marks = n.pop("marks", [])
            lane = n["id"] if n["scope"] == "lane" else lane_id
            try:
                self.traces.write(TraceEvent(kind="meter", status=n["kind"], principal="node", principal_kind="node",
                                             grant_id=g.id if g is not None else None, lane=lane,
                                             summary=n["explain"], args=n))
                self.bus.publish("meter", n)
                title = (f"{n['label']} is paused by a limit" if n["kind"] == "cutoff"
                         else f"{n['label']} is near a limit")
                self.notifier.send({"type": f"meter.{n['kind']}", "lane": lane, "title": title[:80],
                                    "body": n["explain"], "node_text": True, "to": self._meter_recipients(g),
                                    "key": f"meter:{n['code']}:{n['id']}:{n['threshold']}"})
            except Exception:
                log.exception("a meter notice could not be sent; it is tried again on the next call")
                try:
                    self.meters.release(marks)
                except Exception:
                    log.exception("a meter notice's marks could not be released")
                continue
            sent.append(n)
        return sent

    def commits_open(self, g: Grant, lane_id: str | None) -> LimitReached | None:
        """Would one more commit on `lane_id` pass a commit limit (or a spent money or token limit)
        of the grant's chain or the lane? The egress guard routes an act through its strict route
        then, so a publish is stopped before it leaves (agentos.node.egress)."""
        limited = self._meter_limited(g, lane_id)
        paced = self._pace_lane(g, lane_id)
        try:
            if limited:
                self.meters.check(self._used, limited, Effect.COMMIT.value, 0, 0, self.clock())
            if paced is not None:
                self.pace_check(paced, self.clock())
        except LimitReached as e:
            return e
        return None

    def charge_commit(self, g: Grant, lane_id: str | None, *, sent: bool) -> LimitReached | None:
        """A publish an agent's act set off on `lane_id` (the egress guard matched a commit rule):
        one commit on the meters of the grant's chain and the lane, never an action of its own (the
        act was authorized, and counted, as a draft). Returns the LimitReached when it passes a
        limit: `sent` False (the strict route holds the request) debits nothing, so the request is
        aborted; `sent` True (it already left) is counted anyway, because it happened."""
        try:
            self.reserve(g, effect=Effect.COMMIT, lane_id=lane_id, action=False)
        except LimitReached as e:
            if sent:
                self.reserve(g, effect=Effect.COMMIT, lane_id=lane_id, action=False, strict=False)
            self.meter_refused(g, lane_id, e)
            return e
        self.meter_notices(g, lane_id)
        return None

    def charge_spend(self, grant_id: str | None, *, tokens: int = 0, usd: float = 0.0) -> bool:
        """Spend reported after the fact by a service that ran under a grant (a brain attempt's
        tokens and metered dollars, agentos.brains): debited to the budgets, the brain quota and
        the day's meters of the grant's chain and of the lane it implies (implied_lane). It
        already happened, so nothing is refused; a limit it passed pauses the next call. The grant
        may be revoked or expired by now (a brain's child grant ends with its task); its chain is
        still charged. False when the grant is not known here."""
        if not grant_id or not (tokens or usd):
            return False
        try:
            g = self.owner_grant() if grant_id == OWNER_GRANT_ID else self._stored_chain(grant_id)
        except Denied:
            return False
        res = self.reserve(g, tokens=tokens, usd=usd, brain=True, strict=False)
        if res.entries:
            self.meter_notices(g, None)
        return True

    def lane_meter(self, lane_id: str) -> dict:
        lim, source = self.meters.lane_limits(lane_id)
        scope = Scope("lane", lane_id)
        return self.meters.state(scope, [(scope, lim)] if lim is not None else [], source=source)

    def grant_meter(self, g: Grant) -> dict:
        """The grant's own use today, under the tightest of its chain's limits."""
        scope = Scope("grant", g.id, self._grant_label(g))
        limited = [(s if s.id != g.id else scope, lim) for s, lim in self._meter_limited(g, None, implied=False)]
        out = self.meters.state(scope, limited)
        out["subject"] = None if g.owner_token else g.subject.model_dump()
        return out

    def meters_for(self, g: Grant, lanes: Iterable[str] = ()) -> dict:
        """What grant_info shows an agent: its own meter and those of its lanes that have limits."""
        known = set(lanes) | set(self.meters.limited_lanes())
        return {"grant": self.grant_meter(g),
                "lanes": [self.lane_meter(lane) for lane in sorted(known)
                          if g.lane_allowed(lane) and self.meters.lane_limits(lane)[0] is not None]}

    def set_lane_limits(self, lane_id: str, limits: Limits | dict | None, *, by: str = "owner") -> dict:
        """The owner's limits for one lane (None: back to policy.toml). Traced as `meter_limits`."""
        parse_lane_id(lane_id)
        lim = Limits.model_validate(limits) if isinstance(limits, dict) else limits
        self.meters.set_lane_limits(lane_id, lim, by=by)
        words = ", ".join(f"{k} {v}" for k, v in lim.given().items()) if lim is not None else "removed"
        self.traces.write(TraceEvent(kind="meter_limits", status="ok", principal=by, principal_kind="human",
                                     lane=lane_id, summary=f"limits for {lane_id}: {words or 'none'}",
                                     args={"limits": lim.given() if lim is not None else None}))
        self.bus.publish("meter", {"scope": "lane", "id": lane_id, "kind": "limits"})
        if lim is not None:  # a limit set below today's use cuts off now: say so, once per window
            try:
                notices = self.meters.crossed([(Scope("lane", lane_id), lim)], self.clock())
            except Exception:
                log.exception("meter thresholds could not be checked")
                notices = []
            self._send_meter_notices(notices, None, lane_id)
        return self.lane_meter(lane_id)

    def _over_quota(self, g: Grant, tokens_: int, usd: float) -> bool:
        threshold = self.cfg.routing.brain_quota_threshold
        for key, limit, _ in self._quota_entries(g, 0, 0.0):
            amount = tokens_ if key.startswith("quota:tok:") else micros_up(usd)
            used = self._used(key)
            cap = limit * threshold
            if used + amount > cap or (amount == 0 and used >= cap):
                return True
        return False
