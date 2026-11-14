"""Standing approvals a controller sends (docs/PROTOCOL.md v1 clarifications, `standing`).

The owner sets "20 posts on aster/instagram until Friday" in the controller's panel, bound to the
targets and content digests they saw. The controller sends it signed, as `standing` or in
`welcome.standing`; this module checks each one and stores it in the node's own approvals
(agentos.policy.approvals), where a matching call consumes one use exactly as a node-local one does.

Each item is checked with the rules the node's own standing approvals follow, and more:

- only a ladder effect up to `commit` (`read`, `draft`, `commit`) that the node's routing lets a
  standing approval cover: never `pay` or `irreversible`, and never a special (`exec`, `secret`,
  `service`, `brain`, the installs), which binds no target, so a controller could not name what it
  covers (`effect_not_allowed`);
- within the owner's controller ceiling: an effect above `controller_max_effect` is refused
  (`controller_ceiling`), and the ceiling is checked again on every use;
- within the owner's limits in node.toml, which bound what a breached controller can do: at most
  `controller_standing_max_days` days (30 by default, never over 90; `too_long`), not over
  (`expired`), at most `controller_standing_max_count` uses (50; `over_limit`), and at most
  `controller_standing_max_active` unused uses held from this controller per workspace across
  its active ones (200; `over_limit`). An update is checked again, so a controller cannot refill
  a count or push `until` past them;
- one workspace by name, and lane patterns inside it (`outside_workspace`);
- a content-carrying effect (commit) names its targets, and a `tool:` target carries the content
  digests the owner saw (`unbound`); a flow version or connector operation with no contents is a
  template, as on the node;
- an id revoked on this node never comes back (`revoked`), and an id the node holds as its own or
  from another issuer is not touched (`conflict`). An id revoked before it arrived is kept as a
  revoked row of its issuer, so no number of later revocations lets it in.

Each stored one records its issuer (`org:<org id>`), covers calls only while the node trusts that
issuer and within `controller_standing_confirm_hours` of its controller last confirming it (an item
carrying it, or a whole `set` this session while the channel stays up: node.py), and is listed with it in `agentos approvals standing list`, where the owner can revoke it.
A freeze revokes it like a node-local one, and while a freeze is in force no add, upsert or set
item reaches what it covers (`frozen`): the node or mesh freeze refuses every one, a workspace or
lane freeze the ones over its scope. What a freeze revoked stays revoked after the unfreeze;
revokes always apply. An add or update that is refused withdraws what the node held from this controller under that id
(it fails safe: the old version does not stay live), and `standing_result` lists the id both in
`refused` and in `withdrawn`. A body is refused whole (`error`) when its signature
is wrong, its `ts` is missing or stale, or its `nonce` was seen before.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from agentos.core.errors import AgentosError, InvalidRequest, PolicyDenied
from agentos.core.models import Effect
from agentos.node.channel import wire
from agentos.policy import globs
from agentos.policy.ceiling import ControllerCeiling
from agentos.policy.issuers import TrustedIssuers

log = logging.getLogger("agentos.channel")

KIND = "standing"
LADDER = ("read", "draft", "commit")  # the only effects a controller may pre-approve
MAX_DAYS = 90
MAX_ITEMS = 500
MAX_COUNT = 1_000_000
ID_RE = re.compile(r"^sa_[0-9a-z_]{1,64}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
NONCE_KEEP_S = 900.0  # longer than the signed freshness window, so a replay is caught either way
REASONS = ("invalid", "effect_not_allowed", "controller_ceiling", "too_long", "expired", "outside_workspace",
           "unbound", "revoked", "conflict", "frozen", "over_limit")
NAMED = ("flow:", "connector:")


@dataclass(frozen=True)
class Limits:
    """The owner's bounds on a controller's standing approvals (node.toml)."""
    max_count: int = 50
    max_active: int = 200
    max_days: int = 30

    @classmethod
    def from_config(cls, config: Any) -> Limits:
        if config is None:
            return cls()
        return cls(getattr(config, "controller_standing_max_count", cls.max_count),
                   getattr(config, "controller_standing_max_active", cls.max_active),
                   min(MAX_DAYS, getattr(config, "controller_standing_max_days", cls.max_days)))


DEFAULT_LIMITS = Limits()


class Refused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Item:
    id: str
    workspace: str
    effect: str
    lanes: list[str]
    count: int
    used: int
    per_day: int | None
    until: float
    targets: list[str]
    contents: list[str]
    note: str | None
    created_by: str


def org_issuer(config: Any) -> str:
    """The issuer this node trusts for its controller's org (agentos.policy.issuers)."""
    return f"org:{getattr(config, 'org_id', '') or 'controller'}"


def leave_org(home: Path, issuer: str, *, issuers: Any = None, by: str = "node:joined another org") -> list[str]:
    """This node joined another org: the old org's issuer is no longer trusted, so its grants stop
    verifying (`unknown_issuer`), every standing approval it sent is revoked, since the new
    controller cannot revoke them (`conflict`), and its confirmation layer is dropped, since it can
    no longer take back what it asked for. Returns the ids revoked. `issuers` is the running
    TrustedIssuers; without one the node's grant_issuers.db is changed directly. A daemon already
    running keeps the old key in memory until it restarts, which a join asks for."""
    trusted = issuers if issuers is not None else TrustedIssuers(home)
    try:
        trusted.forget(issuer)
    finally:
        if issuers is None:
            trusted.close()
    path = Path(home) / "policy.db"
    revoked: list[str] = []
    if path.exists():
        db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                revoked = [r[0] for r in db.execute(
                    "SELECT id FROM standing WHERE issuer = ? AND revoked_at IS NULL", (issuer,))]
                db.execute("UPDATE standing SET revoked_at = ? WHERE issuer = ? AND revoked_at IS NULL",
                           (time.time(), issuer))
            except sqlite3.OperationalError:  # a policy.db from before standing approvals had an issuer
                revoked = []
            try:
                db.execute("DELETE FROM confirmation_controller WHERE set_by = ?", (f"controller:{issuer}"[:120],))
            except sqlite3.OperationalError:  # a policy.db from before a controller had a layer
                pass
            db.execute("COMMIT")
        finally:
            db.close()
    log.warning("left %s (%s): its grant key is no longer trusted and %d standing approval(s) it sent were "
                "revoked", issuer, by, len(revoked))
    return revoked


def _int(raw: dict, key: str, *, low: int, high: int, default: int | None = None) -> int | None:
    value = raw.get(key, default)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise Refused("invalid", f"{key} must be a whole number from {low} to {high}")
    return value


def _strings(raw: dict, key: str, limit: int = 200) -> list[str]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(v, str) for v in value):
        raise Refused("invalid", f"{key} must be a list of at most {limit} strings")
    return list(value)


def parse(raw: Any, *, now: float, ceiling: ControllerCeiling, standing_effects: list[str],
          content_effects: list[str], limits: Limits = DEFAULT_LIMITS) -> Item:
    """One item as the controller sent it, checked (Refused otherwise). The store checks targets,
    digests and patterns again when it keeps it."""
    if not isinstance(raw, dict):
        raise Refused("invalid", "a standing approval is an object")
    effect, workspace = raw.get("effect"), raw.get("workspace")
    if not isinstance(effect, str) or effect not in {e.value for e in Effect}:
        raise Refused("invalid", "effect must be one of the node's effects")
    if effect not in LADDER or effect not in standing_effects:
        raise Refused("effect_not_allowed", f"a standing approval cannot cover {effect} on this node; a person "
                                            "decides it every time")
    if not ceiling.allows_effect(Effect(effect)):
        raise Refused("controller_ceiling", f"this node's owner does not let a controller cover {effect}; it is "
                                            "raised in node.toml on the node")
    if not isinstance(workspace, str) or "*" in workspace:
        raise Refused("invalid", "workspace must name one workspace, not a pattern")
    try:
        globs.validate_segment(workspace)
    except AgentosError:
        raise Refused("invalid", "workspace must name one workspace (lowercase letters, digits and dashes)") from None
    lanes = _strings(raw, "lanes")
    for pattern in lanes:
        if pattern == "*" or pattern.split("/", 1)[0] != workspace:
            raise Refused("outside_workspace", f"lane {pattern[:80]!r} is not inside workspace {workspace}")
        try:
            globs.validate_lane_pattern(pattern)
        except AgentosError:
            raise Refused("invalid", f"lane {pattern[:80]!r} is not a lane pattern (workspace/name)") from None
    until = wire.parse_time(raw.get("until"))
    if until is None or isinstance(raw.get("until"), int | float):
        raise Refused("invalid", "until must be ISO 8601 UTC")
    if until <= now:
        raise Refused("expired", "this standing approval has already ended")
    if until - now > limits.max_days * 86400:
        raise Refused("too_long", f"this node's owner lets a controller's standing approval last {limits.max_days} "
                                  "days at most (controller_standing_max_days in node.toml)")
    count = _int(raw, "count", low=1, high=MAX_COUNT)
    if count is None:
        raise Refused("invalid", "count is required (the number of uses)")
    if count > limits.max_count:
        raise Refused("over_limit", f"this node's owner lets a controller's standing approval cover {limits.max_count} "
                                    "uses at most (controller_standing_max_count in node.toml)")
    used = _int(raw, "used", low=0, high=MAX_COUNT, default=0) or 0
    per_day = _int(raw, "per_day", low=1, high=MAX_COUNT)
    targets, contents = sorted(set(_strings(raw, "targets"))), sorted(set(_strings(raw, "contents", 1000)))
    if effect in content_effects:
        if not targets:
            raise Refused("unbound", f"a standing approval for {effect} must name what it covers "
                                     "(flow:<id>@<version>, connector:<site>.<op>@<contract> or tool:<name>)")
        if not contents and not all(t.startswith(NAMED) for t in targets):
            raise Refused("unbound", "a tool target also needs the content digests the owner approved")
    note = raw.get("note")
    created_by = raw.get("created_by")
    standing_id = raw.get("id")
    if not isinstance(standing_id, str) or not ID_RE.match(standing_id):
        raise Refused("invalid", "id must look like sa_<lowercase letters, digits, _>")
    return Item(id=standing_id, workspace=workspace, effect=effect, lanes=sorted(set(lanes)), count=count,
                used=used, per_day=per_day, until=until, targets=targets, contents=contents,
                note=note[:300] if isinstance(note, str) and note else None,
                created_by=created_by[:80] if isinstance(created_by, str) and created_by else "controller")


def _workspace(sa: Any) -> str:
    """The one workspace a controller's standing approval covers (its workspace, or its lanes')."""
    return sa.workspaces[0] if sa.workspaces else (sa.lanes[0].split("/", 1)[0] if sa.lanes else "")


def held(sa: Any, now: float) -> dict:
    """What the controller is told about one it sent: `{ id, used, remaining, state }`."""
    remaining = None if sa.max_uses is None else max(0, sa.max_uses - sa.uses)
    return {"id": sa.id, "used": sa.uses, "remaining": remaining, "state": sa.state(now)}


def event_payload(public: dict, now: float) -> dict | None:
    """`event { kind: "standing" }` from a standing approval's public form (as the approvals store
    publishes it): `{ id, used, remaining, state }`."""
    if not isinstance(public.get("id"), str) or not isinstance(public.get("uses"), int):
        return None
    remaining = public.get("remaining")
    expires = wire.parse_time(public.get("expires_at"))
    if public.get("revoked_at"):
        state = "revoked"
    elif expires is not None and expires <= now:
        state = "expired"
    elif public.get("withdrawn_at"):
        state = "withdrawn"
    else:
        state = "used_up" if remaining == 0 else "active"
    return {"id": public["id"], "used": public["uses"], "remaining": remaining, "state": state}


class Standing:
    """The node's half: checks a signed body and applies it to the approvals store. `meta` is the
    channel store's get_meta / set_meta (the nonces seen); `freeze` is the node's freeze service."""

    def __init__(self, approvals: Any, *, issuer: str, ceiling: ControllerCeiling,
                 get_meta: Callable[[str, Any], Any], set_meta: Callable[[str, Any], None],
                 clock: Callable[[], float] = time.time, freeze: Any = None, limits: Limits = DEFAULT_LIMITS):
        self.approvals = approvals
        self.limits = limits
        self.freeze = freeze
        self.issuer = issuer
        self.ceiling = ceiling
        self.get_meta, self.set_meta = get_meta, set_meta
        self.clock = clock

    def _now(self) -> float:
        clock = getattr(self.approvals, "clock", None)
        return clock() if callable(clock) else self.clock()

    def check_body(self, body: wire.Json) -> None:
        """After the signature: `ts` (required here) and a `nonce` not seen before."""
        if wire.parse_time(body.get("ts")) is None:
            raise InvalidRequest("standing needs ts (ISO 8601 UTC)")
        nonce = body.get("nonce")
        if not isinstance(nonce, str) or not NONCE_RE.match(nonce):
            raise InvalidRequest("standing needs a nonce (8 to 64 letters, digits, _ -)")
        now = time.time()
        seen = {k: v for k, v in (self.get_meta("standing_nonces", {}) or {}).items() if v > now - NONCE_KEEP_S}
        if nonce in seen:
            raise PolicyDenied("this standing message was already applied (its nonce was seen before)")
        for key in ("set", "upsert", "revoke"):
            value = body.get(key)
            if value is not None and (not isinstance(value, list) or len(value) > MAX_ITEMS):
                raise InvalidRequest(f"standing {key} must be a list of at most {MAX_ITEMS}")
        if body.get("set") is not None and body.get("upsert") is not None:
            raise InvalidRequest("standing carries set (the whole list) or upsert, not both")
        # kept only for a body that is applied, so a refused one does not use up its nonce
        seen[nonce] = now
        self.set_meta("standing_nonces", seen)

    def apply(self, body: wire.Json) -> dict:
        """Revoke, then set or upsert. Returns the `standing_result` body (without `re`)."""
        now = self._now()
        out: dict[str, list] = {"applied": [], "refused": [], "revoked": [], "withdrawn": []}
        self._keep_old_tombstones()
        for standing_id in body.get("revoke") or []:
            self._revoke(standing_id, out)
        items = body.get("set") if body.get("set") is not None else body.get("upsert") or []
        sent: set[str] = set()
        for raw in items:
            standing_id = raw.get("id") if isinstance(raw, dict) else None
            if isinstance(standing_id, str):
                sent.add(standing_id)
            self._put(raw, now, out)
        if body.get("set") is not None:
            for sa in self.approvals.issued_standing(self.issuer):
                if sa.id not in sent and self.approvals.withdraw_standing(sa.id, issuer=self.issuer):
                    out["withdrawn"].append(sa.id)
        return out

    def _keep_old_tombstones(self) -> None:
        """Ids revoked before they arrived, kept as a list (capped) before they were rows."""
        old = self.get_meta("standing_revoked", None)
        if old:
            for standing_id in old:
                if isinstance(standing_id, str) and ID_RE.match(standing_id):
                    self.approvals.revoke_unseen_standing(standing_id, issuer=self.issuer,
                                                          by=f"controller:{self.issuer}")
            self.set_meta("standing_revoked", None)

    def _revoke(self, standing_id: Any, out: dict) -> None:
        if not isinstance(standing_id, str) or not ID_RE.match(standing_id):
            out["refused"].append({"id": str(standing_id)[:80], "code": "invalid", "message": "not a standing id"})
            return
        mine = {sa.id: sa for sa in self.approvals.issued_standing(self.issuer)}
        sa = mine.get(standing_id)
        if sa is None:
            if self._held_elsewhere(standing_id):
                out["refused"].append({"id": standing_id, "code": "conflict",
                                       "message": "this id is not one this controller sent"})
                return
            # revoked before it arrived: it is never added
            self.approvals.revoke_unseen_standing(standing_id, issuer=self.issuer, by=f"controller:{self.issuer}")
        elif sa.revoked_at is None:
            self.approvals.revoke_standing(standing_id, by=f"controller:{self.issuer}")
        out["revoked"].append(standing_id)

    def _withdraw_refused(self, standing_id: Any, out: dict) -> None:
        """A refused update of one this controller holds: the version held stops covering calls
        (the controller meant to change it, and the change is not in force)."""
        if isinstance(standing_id, str) and self.approvals.withdraw_standing(standing_id, issuer=self.issuer):
            out["withdrawn"].append(standing_id)

    def _held_elsewhere(self, standing_id: str) -> bool:
        return any(sa.id == standing_id for sa in self.approvals.list_standing(active_only=False))

    def _frozen(self, item: Item) -> str | None:
        """Why a freeze in force keeps this item out, or None. A freeze sweeps once, when it starts,
        so one added during it would outlive the unfreeze."""
        if self.freeze is None:
            return None
        if getattr(self.freeze, "problem", None):
            return "this node is frozen (its freeze directory cannot be read); nothing is added until it is fixed"
        for rec in self.freeze.records():
            if rec.scope in ("node", "mesh"):
                where = "the mesh" if rec.scope == "mesh" else "this node"
                return f"{where} is frozen; nothing is added until the owner unfreezes it"
            if rec.scope == "workspace" and rec.target == item.workspace or rec.scope == "lane" and (
                    rec.target.split("/", 1)[0] == item.workspace
                    and (not item.lanes or any(fnmatchcase(rec.target, p) for p in item.lanes))):
                return f"{rec.scope} {rec.target} is frozen; nothing is added over it until the owner unfreezes it"
        return None

    def _check_active(self, item: Item, now: float) -> None:
        """The unused uses this controller would hold in the item's workspace, with it (as it would
        stand: the node keeps the larger of its own use count and the controller's), within the cap."""
        held_uses, others = 0, 0
        for sa in self.approvals.issued_standing(self.issuer):
            if sa.id == item.id:
                held_uses = sa.uses
            elif sa.state(now) == "active" and _workspace(sa) == item.workspace:
                others += max(0, (sa.max_uses or 0) - sa.uses)
        unused = max(0, item.count - max(item.used, held_uses))
        if others + unused > self.limits.max_active:
            raise Refused("over_limit", f"this would hold {others + unused} unused uses from this controller in "
                                        f"{item.workspace}; this node's owner allows {self.limits.max_active} "
                                        "(controller_standing_max_active in node.toml)")

    def _put(self, raw: Any, now: float, out: dict) -> None:
        raw_id = raw.get("id") if isinstance(raw, dict) else None
        label = raw_id[:80] if isinstance(raw_id, str) else ""
        try:
            item = parse(raw, now=now, ceiling=self.ceiling, standing_effects=list(self.approvals.standing_effects),
                         content_effects=list(self.approvals.content_effects), limits=self.limits)
            frozen = self._frozen(item)
            if frozen:
                raise Refused("frozen", frozen)
            self._check_active(item, now)
            sa, state = self.approvals.put_issued_standing(
                item.id, issuer=self.issuer, effects=[item.effect],
                workspaces=[] if item.lanes else [item.workspace], lanes=item.lanes, until=item.until,
                count=item.count, per_day=item.per_day, uses=item.used, by=f"controller:{item.created_by}",
                note=item.note, targets=item.targets, contents=item.contents)
        except Refused as exc:
            out["refused"].append({"id": label, "code": exc.code, "message": exc.message})
            self._withdraw_refused(raw_id, out)
            return
        except AgentosError as exc:
            out["refused"].append({"id": label, "code": "invalid", "message": exc.message[:300]})
            self._withdraw_refused(raw_id, out)
            return
        if state == "conflict" or sa is None:
            out["refused"].append({"id": item.id, "code": "conflict",
                                   "message": "this id is one this node holds as its own or from another issuer"})
        elif state == "revoked":
            out["refused"].append({"id": item.id, "code": "revoked",
                                   "message": "this standing approval was revoked on this node"})
        else:
            out["applied"].append(held(sa, now))
