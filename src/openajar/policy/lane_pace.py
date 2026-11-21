"""Posting pace: how often, and in which waking hours, a lane may publish (part of step 5).

Part of PolicyService (openajar.policy.service), as a mixin; the rules and the ledger are
openajar.policy.pace. `reserve` checks a commit's pace in the same transaction as its budget, and
writes its row on the lane's ledger. Whose clock the waking hours are read on comes from the lane's
pinned fingerprint when there is one; a pin that exists but cannot be read refuses a paced publish
(fail closed) rather than read the hours on another clock.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from openajar.core.errors import InvalidRequest
from openajar.core.models import parse_lane_id
from openajar.policy import pace as pacing
from openajar.policy.grants import Grant
from openajar.policy.pace import LanePaced, Pace, Zone
from openajar.policy.traces import TraceEvent
from openajar.policy.util import iso

log = logging.getLogger("openajar.policy")


class LanePaceMixin:
    """The posting pace methods of PolicyService (the attributes it reads, `pace`, `meters`,
    `node_config`, `runtime`, `traces`, `bus`, `db`, `clock`, are set by PolicyService.__init__)."""

    # ================================================================ posting pace (openajar.policy.pace)
    def pace_lane(self, target: str) -> str:
        """The lane whose posting pace a publish on `target` (a lane, or a connector surface) uses."""
        return self._pace_lane(self.owner_grant(), target) or target

    def _pace_lane(self, g: Grant, lane_id: str | None) -> str | None:
        """The lane a commit publishes through: the lane it names (a connector surface's own lane).
        None for a call that names none (a workspace plan applied is a commit, not a post), even
        under a grant that implies one lane: a post always names its lane."""
        if not lane_id:
            return None
        surfaces = self.runtime.get("surfaces") if self.runtime is not None else None
        find = getattr(surfaces, "find", None)
        surface = find(lane_id) if callable(find) else None
        return getattr(surface, "lane", None) or lane_id

    def _pinned_zone(self, lane_id: str) -> str | None:
        """The timezone pinned in the lane's fingerprint (its proxy's exit region when pinned); None
        when there is no pin (or it names no zone). A pin that exists but cannot be read or parsed
        keeps the last zone read for the lane; with none, _PinUnreadable (fail closed: a paced
        publish is refused rather than read on another clock)."""
        cfg = self.node_config
        if cfg is None:
            return None
        try:
            path = Path(cfg.fingerprint_path(lane_id))
        except (AttributeError, ValueError):
            return None
        if not path.exists():
            self._pin_zones.pop(lane_id, None)
            return None
        from openajar.core.zone import valid_zone

        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("the fingerprint is not a JSON object")  # noqa: TRY004 (caught as unreadable)
            name = data.get("timezone")
            if name is not None and (not isinstance(name, str) or valid_zone(name) is None):
                raise ValueError(f"the fingerprint's timezone {name!r} is not a zone")
        except (OSError, ValueError) as e:
            last = self._pin_zones.get(lane_id)
            log.warning("%s: the pinned fingerprint could not be read (%s); %s", lane_id, e,
                        f"its last zone {last} is kept" if last else "a paced publish is refused")
            if last is None:
                raise _PinUnreadable(lane_id) from e
            return last
        if name is None:
            self._pin_zones.pop(lane_id, None)
            return None
        zone = valid_zone(name)
        self._pin_zones[lane_id] = zone
        return zone

    def _exit_zone(self, lane_id: str) -> str | None:
        """Where the lane check last saw the proxy's exit (its location check), when it looked."""
        checker = self.runtime.get("lane_check") if self.runtime is not None else None
        report = getattr(checker, "last", {}).get(lane_id) if checker is not None else None
        for check in getattr(report, "checks", ()) or ():
            if getattr(check, "id", None) == "location":
                tz = (getattr(check, "data", None) or {}).get("exit_timezone")
                return tz if isinstance(tz, str) and tz else None
        return None

    def pace_zone(self, lane_id: str, pace: Pace | None = None) -> Zone:
        """Whose clock the lane's waking hours are read on: the lane's own override, else its pinned
        fingerprint's zone, else the workspace default, else the node's (openajar.policy.pace)."""
        pace = pace if pace is not None else self.pace.get(lane_id)
        if pace is not None and pace.timezone:
            return Zone(pace.timezone, "lane")
        try:
            pinned = self._pinned_zone(lane_id)
        except _PinUnreadable:
            return Zone(self.meters.timezone, "pin", unreadable=True)
        if pinned:
            exit_tz = self._exit_zone(lane_id)
            return Zone(pinned, "pin", exit_tz if exit_tz and exit_tz != pinned else None)
        ws = self.pace.workspace_zone(lane_id.split("/", 1)[0])
        if ws:
            return Zone(ws, "workspace")
        return Zone(self.meters.timezone, "node")

    def pace_verdict(self, lane_id: str, at: float | None = None, *, conn: Any = None,
                     pace: Pace | None = None, exclude: tuple[int, ...] = ()
                     ) -> tuple[Pace | None, Zone, pacing.Verdict | None]:
        """(pace, zone, verdict) of one more publish on the lane at `at` (now); the verdict is None
        when the lane has no pace. `exclude`: ledger rows not to count (a run's own row)."""
        pace = pace if pace is not None else self.pace.get(lane_id)
        zone = self.pace_zone(lane_id, pace)
        if pace is None or not pace.paces():
            return pace, zone, None
        t = self.clock() if at is None else at
        if zone.unreadable:
            return pace, zone, pacing.Verdict("timezone", None, 0, None)
        v = pacing.verdict(pace, zone.tz, t,
                           last_at=lambda x: self.pace.last_at(lane_id, x, conn, exclude=exclude),
                           next_at=lambda x: self.pace.next_at(lane_id, x, conn, exclude=exclude),
                           count_day=lambda a, b: self.pace.count(lane_id, a, b, conn, exclude=exclude))
        return pace, zone, v

    def pace_check(self, lane_id: str, at: float, *, conn: Any = None, exclude: tuple[int, ...] = ()) -> None:
        """Raise LanePaced when the lane's pace does not let a publish go at `at`."""
        pace, zone, v = self.pace_verdict(lane_id, at, conn=conn, exclude=exclude)
        if pace is not None and v is not None and v.rule is not None:
            raise pacing.refuse(lane_id, pace, zone, v, self.clock())

    def pace_claim(self, lane_id: str) -> int:
        """A commit run that holds no ledger row yet (the owner started it, or a person approved it
        after it parked): check the lane's whole pace now and record the publish, in one
        transaction like reserve. Returns the row id; raises LanePaced."""
        lane_id = self.pace_lane(lane_id)
        with self.db.tx() as conn:
            self.pace_check(lane_id, self.clock(), conn=conn)
            return self.pace.record(conn, lane_id, self.clock())

    def pace_record(self, lane_id: str) -> int:
        """A publish a person made by hand on the lane (in control of it): recorded on the pace
        ledger with no check, so an agent's post right after it waits for the gap."""
        with self.db.tx() as conn:
            return self.pace.record(conn, self.pace_lane(lane_id), self.clock())

    def attach_lanes(self, runtime: Any) -> bool:
        """Composition hook: a person's own publish on a lane goes on its pace ledger (pace_record)."""
        lanes = runtime.get("lanes") if runtime is not None else None
        add = getattr(lanes, "add_commit_listener", None)
        if not callable(add):
            return False
        add(self.pace_record)
        return True

    def pace_forget(self, ids: list[int]) -> None:
        """Give back ledger rows a run held and no longer uses (it asks a person again)."""
        self.pace.forget([i for i in ids if isinstance(i, int)])

    def pace_window(self, lane_id: str, at: float | None = None) -> LanePaced | None:
        """Only the waking hours: a run that holds a ledger row (Run.pace_id) and runs now (the gap
        and the day's count were checked when the row was written)."""
        pace = self.pace.get(lane_id)
        if pace is None or not pace.active_hours:
            return None
        zone = self.pace_zone(lane_id, pace)
        t = self.clock() if at is None else at
        if zone.unreadable:
            return pacing.refuse(lane_id, pace, zone, pacing.Verdict("timezone", None, 0, None),  # type: ignore[return-value]
                                 self.clock())
        windows = pace.windows()
        if pacing.is_open(windows, zone.tz, t):
            return None
        v = pacing.Verdict("active_hours", pacing.next_open(windows, zone.tz, t), 0, None)
        return pacing.refuse(lane_id, pace, zone, v, self.clock())  # type: ignore[return-value]

    def lane_pace(self, lane_id: str) -> dict:
        """What the panel, the CLI and grant_info show: the lane's pace, whose clock it reads (and
        why), today's posts, the last one and when it may post next."""
        parse_lane_id(lane_id)
        now = self.clock()
        pace, zone, v = self.pace_verdict(lane_id, now)
        tz = zone.tz
        local = datetime.fromtimestamp(now, tz)
        out: dict[str, Any] = {
            "lane": lane_id, "pace": pace.given() if pace is not None else None, "paced": v is not None,
            "zone": zone.public(), "local_now": local.isoformat(timespec="minutes"),
            "workspace_timezone": self.pace.workspace_zone(lane_id.split("/", 1)[0]),
            "preset": pacing.PRESET.given(), "set": self.pace.meta(lane_id),
        }
        start, end = pacing.lane_day(pace.windows() if pace is not None else [], tz, now)
        last = self.pace.last_at(lane_id, now)
        out["posts_today"] = self.pace.count(lane_id, start, end)
        out["last_post_at"] = iso(last) if last is not None else None
        if v is None:
            out.update(may_post_now=True, next_allowed_at=None, next_allowed_local=None, blocked_by=None,
                       explain=None)
            return out
        nxt = v.next_ts
        out.update(may_post_now=v.rule is None, blocked_by=v.rule,
                   next_allowed_at=iso(nxt) if nxt is not None else None,
                   next_allowed_local=datetime.fromtimestamp(nxt, tz).isoformat(timespec="minutes") if nxt else None,
                   explain=pacing.sentence(lane_id, pace, zone, v, now) if v.rule and pace else None)
        return out

    def set_lane_pace(self, lane_id: str, pace: Pace | dict | None, *, by: str = "owner") -> dict:
        """The owner's pace for one lane (None or empty: no pace). Traced as `lane_pace`."""
        parse_lane_id(lane_id)
        p = Pace.model_validate(pace) if isinstance(pace, dict) else pace
        self.pace.set(lane_id, p, by=by)
        given = p.given() if p is not None else {}
        words = ", ".join(f"{k} {v}" for k, v in given.items()) or "removed"
        self.traces.write(TraceEvent(kind="lane_pace", status="ok", principal=by, principal_kind="human",
                                     lane=lane_id, summary=f"posting pace for {lane_id}: {words}",
                                     args={"pace": given or None}))
        self.bus.publish("meter", {"scope": "lane", "id": lane_id, "kind": "pace"})
        return self.lane_pace(lane_id)

    def set_workspace_zone(self, workspace: str, name: str | None, *, by: str = "owner") -> dict:
        """The default zone of a workspace's lanes that have no pin and no zone of their own."""
        from openajar.core.models import WORKSPACE_ID_RE

        if not WORKSPACE_ID_RE.fullmatch(workspace or ""):
            raise InvalidRequest(f"{workspace!r} is not a workspace id like biz-a")
        if name:
            Pace(timezone=name)  # validates the zone name
        self.pace.set_workspace_zone(workspace, name or None, by=by)
        self.traces.write(TraceEvent(kind="lane_pace", status="ok", principal=by, principal_kind="human",
                                     summary=f"default timezone for workspace {workspace}: {name or 'removed'}",
                                     args={"workspace": workspace, "timezone": name or None}))
        self.bus.publish("meter", {"scope": "workspace", "id": workspace, "kind": "pace"})
        return {"workspace": workspace, "timezone": name or None}


class _PinUnreadable(Exception):
    """A lane's pinned fingerprint exists but cannot be read, and no earlier zone is known."""
