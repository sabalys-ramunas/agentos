"""The engage service (runtime service "engage"): programs, personas, plans and the governor.

    build(runtime)                          -> EngageService, registered as "engage"
    draft(surface, text, by=)               validate a program (YAML or JSON), save it as a draft version
    approve(program_id, version, by=)       owner only: the version becomes the surface's program
    set_cap(surface, kind, value, by=)      owner only, never above the hard ceiling
    plan(surface, days=, start=)            a dry run: the drawn days, nothing saved, nothing clicked
    nightly(now=)                           draws and saves tomorrow's plans, runs the correlation audit
    day_plan(surface, day)                  the saved plan of a day, drawn and saved when it has none
    status(surface=, workspace=)            today's plan, caps, phase, holds, notices
    tick(now=)                              fire the windows that are due (only with engage_sessions on)
    planned(start, end, workspace=)         the saved sessions between start and end (the agenda)

Sessions (brief B1, agentos.engage.executor): `build` wires an Executor on the node's runner and
lanes (agentos.engage.adapters). Its windows fire from this service's loop, and only while the
node switch `engage_sessions` is on (off by default, so no real account acts until the owner
turns it on). With the switch off this is the dry run brief E1 built: plans are drawn, saved and
reported, and nothing is clicked.

Agents draft; only the owner approves. An agent's draft may not choose `normal` risk, `auto`
comments or replies, or the `followers_of` source (section 5.10): those are owner-only and
refused with `owner_only`. There is no tool that approves a program or changes a cap.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import ValidationError

from agentos.core.config import config_path, home_from_env, load_config
from agentos.core.errors import InvalidRequest, NotFound
from agentos.engage import profile
from agentos.engage.adapters import node_registry
from agentos.engage.caps import SITES, Kind, Risk, cap_row, warm_up_stage
from agentos.engage.drafts import DraftsBook
from agentos.engage.errors import OwnerOnly, ProgramInvalid
from agentos.engage.governor import (
    AUDIT_DAYS,
    audit,
    budget,
    ceiling_table,
    check_cap_change,
    hourly_series,
    is_owner,
)
from agentos.engage.items import ItemsInbox, lane_file_check
from agentos.engage.models import DayPlan, Program, ProgramState, ProgramVersion
from agentos.engage.persona import Persona, draw_persona
from agentos.engage.planner import Account, plan_days
from agentos.engage.store import EngageStore

log = logging.getLogger("agentos.engage")

SERVICE_NAME = "engage"
MAX_PLAN_DAYS = 60
NIGHTLY_POLL_S = 3600.0
SESSION_POLL_S = 30.0
MAX_PROGRAM_CHARS = 20_000


def _who(by: Any) -> dict[str, Any] | None:
    if by is None:
        return None
    p = by if isinstance(by, dict) else (by.model_dump() if hasattr(by, "model_dump") else dict(vars(by)))
    return {k: p.get(k) for k in ("kind", "id", "owner") if p.get(k) is not None}


def validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        where = ".".join(str(x) for x in err.get("loc", ()) if not str(x).startswith("function-"))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{where}: {msg}" if where else msg)
    return "; ".join(parts)


def parse_program(text: str | dict[str, Any], *, surface: str | None = None) -> Program:
    """A Program from YAML or JSON text (or a dict). `surface`, when given, fills a missing
    surface and must match a stated one."""
    if isinstance(text, dict):
        data: Any = dict(text)
    else:
        if len(text) > MAX_PROGRAM_CHARS:
            raise ProgramInvalid(f"the program is longer than {MAX_PROGRAM_CHARS} characters")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ProgramInvalid(f"the program is not valid YAML or JSON: {str(e).splitlines()[0]}") from None
        if not isinstance(data, dict):
            raise ProgramInvalid("write the program as YAML (or JSON) with keys such as program, surface, "
                                 "risk and phases. Plain sentences are turned into YAML by the drafter, which "
                                 "comes later; for now pass the YAML")
    if surface is not None:
        stated = data.get("surface")
        if stated is not None and stated != surface:
            raise ProgramInvalid(f"the program says surface {stated!r}, but it was drafted for {surface!r}")
        data["surface"] = surface
    try:
        return Program.model_validate(data)
    except ValidationError as e:
        raise ProgramInvalid(validation_message(e)) from None


def to_yaml(program: Program) -> str:
    return yaml.safe_dump(program.canonical(), sort_keys=False, allow_unicode=True, width=100)


def plan_name(surface: str, site: str) -> str:
    """A plan's name as a person reads it: "Social on Instagram" for biz-a/social, and "Instagram" when the account
    is named for its site. Never the surface's id ("biz-a/social"), so Home and the plan say the same words."""
    account = surface.rsplit("/", 1)[-1].replace("-", " ")
    account, site = f"{account[:1].upper()}{account[1:]}", f"{site[:1].upper()}{site[1:]}"
    return account if account.lower() == site.lower() else f"{account} on {site}"


def summarize(program: Program) -> str:
    """The program as plain sentences for the owner."""
    s = program.steady
    risk = {"careful": "Careful pace", "normal": "Normal pace", "off": "Engagement off (posting and replies only)"}
    out = [f"{plan_name(program.surface, program.site)}. {risk[program.risk.value]}."]
    if program.warm_up is not None:
        out.append(f"Warms up for {program.warm_up.days} days first: looking around, then a few likes and follows.")
    names = {Kind.STORY_VIEW: "story view", Kind.LIKE: "like", Kind.COMMENT: "comment", Kind.FOLLOW: "follow",
             Kind.JOIN: "join", Kind.POST: "post"}
    wants = [f"{n} {names[k]}{'s' if n != 1 else ''}" for k, n in s.daily.items() if n]
    if wants and program.risk is not Risk.OFF:
        out.append("Then about " + ", ".join(wants) + " a day, spread over 2 to 4 short sessions, mostly browsing.")
    sources = [t.source.value.replace("_", " ") + (f" ({', '.join(t.values[:5])})" if t.values else "")
               for t in s.targets]
    if sources:
        out.append("Finds posts in: " + "; ".join(sources) + ".")
    if program.profile is not None and program.profile.fields():
        out.append(f"Keeps the public profile's {', '.join(program.profile.fields())} as written here; you "
                   "approve every profile edit.")
    if program.comments.mode.value == "approve":
        out.append("You approve every comment before it goes out.")
    elif program.comments.mode.value == "auto":
        out.append("Comments go out without your approval once they pass the checks.")
    else:
        out.append("No comments.")
    return " ".join(out)


def resolve_timezone(program: Program, config: Any = None) -> tuple[ZoneInfo, str | None]:
    """(the zone, why it fell back to UTC or None): the program's, else the lane's pinned one, else UTC."""
    name = program.timezone
    if name is None and config is not None:
        try:
            pin = json.loads(config.fingerprint_path(program.surface).read_text())
            name = pin.get("timezone") if isinstance(pin, dict) else None
        except (OSError, ValueError):
            name = None
    if not name:
        return ZoneInfo("UTC"), "neither the program nor the lane names a time zone"
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC"), f"the time zone {name!r} is not known"


def program_timezone(program: Program, config: Any = None) -> ZoneInfo:
    """The zone alone, and nothing written: `EngageService.timezone` also leaves a notice."""
    return resolve_timezone(program, config)[0]


def profile_notes(running: ProgramVersion | None, program: Program) -> list[str]:
    """What a draft changes in the account's public profile, against the running version, for the
    owner who approves it (agentos.engage.profile.changes)."""
    changed = profile.changes(running.definition if running is not None else None, program)
    if not changed:
        return []
    note = (f"this version changes the account's public profile: {', '.join(changed)}. Each edit waits for your "
            "approval card when it runs, whatever the comments mode")
    return [note]


def window_name(t: datetime) -> str:
    h = t.hour
    if h < 12:
        return "morning (before 12:00)"
    if h < 14:
        return "midday (12:00 to 14:00)"
    if h < 18:
        return "afternoon (14:00 to 18:00)"
    if h < 22:
        return "evening (18:00 to 22:00)"
    return "late evening (after 22:00)"


def lane_gone_check(runtime: Any):
    """surface -> why the lane it acts through is gone (the node's lane manager has no such lane), or
    None. None without a lane manager (tests, a node with no lanes service)."""
    if runtime is None:
        return None

    def gone(surface: str) -> str | None:
        from agentos.core.errors import LaneNotFound
        from agentos.engage.adapters import NodeLanes

        lanes = runtime.get("lanes")
        if lanes is None:
            return None
        lane = NodeLanes(runtime).lane_for(surface)
        try:
            lanes.get(lane)
        except LaneNotFound:
            return f"the lane {lane} that {surface} acts through is gone" if lane != surface else \
                f"the lane {lane} is gone"
        return None

    return gone


class EngageService:
    def __init__(self, runtime: Any = None, *, home: Path | None = None, clock: Any = time.time):
        cfg = runtime.get("config") if runtime is not None else None
        self.runtime = runtime
        self.config = cfg
        self.home = Path(home) if home is not None else (Path(cfg.home) if cfg is not None else home_from_env())
        self.clock = clock
        self.store = EngageStore(self.home)
        # brief B4: an item is checked against the operations this node can run, as the executor is
        self.items = ItemsInbox(self.store, registry=node_registry(runtime, self.home), clock=clock,
                                files=lane_file_check(runtime),
                                approvals=(lambda: runtime.get("approvals")) if runtime is not None else None,
                                lane_gone=lane_gone_check(runtime))
        self.drafts = DraftsBook(self.store, clock=clock)                                  # brief B3
        self._task: asyncio.Task | None = None
        self._sessions_task: asyncio.Task | None = None
        self.executor: Any = None                       # agentos.engage.executor.Executor, set by build()
        self.running: dict[str, asyncio.Task] = {}      # surface -> its window in progress

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="agentos-engage-nightly")
        if self.executor is not None:
            try:  # sessions a stopped (or crashed) node left running: closed, with a notice
                self.executor.recover()
            except Exception:
                log.exception("engage: recovering interrupted sessions failed")
        if self.executor is not None and (self._sessions_task is None or self._sessions_task.done()):
            self._sessions_task = asyncio.create_task(self._sessions_loop(), name="agentos-engage-sessions")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, self._sessions_task, *self.running.values()) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        self._task = self._sessions_task = None
        self.running.clear()
        self.store.close()

    def sessions_enabled(self) -> bool:
        """The node switch `engage_sessions`, read from node.toml each time (falls back to the config
        agentosd started with). Off by default."""
        try:
            if config_path(self.home).exists():
                return bool(load_config(self.home).engage_sessions)
        except Exception:
            log.debug("node.toml not read", exc_info=True)
        return bool(getattr(self.config, "engage_sessions", False))

    async def _sessions_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("engage: the session loop failed")
            await asyncio.sleep(SESSION_POLL_S)

    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Start every window that is due, one at a time per surface, and settle operations whose
        outcome came after their window (Executor.reconcile). Nothing happens while the switch is off (items still
        expire, and reservations of windows that ended go back to the queue)."""
        self._sweep_items(now)
        if self.executor is None or not self.sessions_enabled():
            return []
        busy = {s for s, t in self.running.items() if not t.done()}
        self.executor.recover(now, busy=busy)
        if await self.executor.reconcile():
            self._sweep_items(now)       # an item whose approval card was decided follows it now
        started = []
        for w in self.executor.due(now):
            task = self.running.get(w.surface)
            if task is not None and not task.done():
                continue
            self.running[w.surface] = asyncio.create_task(self.executor.run_window(w.surface, w.day, w.idx),
                                                          name=f"agentos-engage-{w.surface}")
            started.append(w.public())
        return started

    def _sweep_items(self, now: datetime | None = None) -> None:
        try:
            self.items.sweep(now)
        except Exception:
            log.exception("engage: the items sweep failed")
        try:
            self.drafts.sweep(now)
        except Exception:
            log.exception("engage: the drafts sweep failed")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.nightly)
            except Exception:
                log.exception("engage: the nightly plan failed")
            await asyncio.sleep(NIGHTLY_POLL_S)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    # ---------------------------------------------------------------- programs
    def draft(self, surface: str | None, text: str | dict[str, Any], *, by: Any,
              as_owner: bool | None = None) -> dict[str, Any]:
        """`as_owner` (by default whether `by` is the owner) lets the draft choose what only the
        owner may: the MCP tool passes it only for the owner in person, never for a bearer token."""
        program = parse_program(text, surface=surface)
        if not (is_owner(by) if as_owner is None else as_owner):
            parts = program.owner_only_parts()
            if parts:
                raise OwnerOnly(f"only the owner can choose {', '.join(parts)}. Draft the program without "
                                "it; the owner can change it before approving")
        self.persona(program.surface)          # drawn once, at the first draft
        fv, created = self.store.put_program(program, created_by=_who(by))
        today = self.today(program)
        b = budget(program, today, today, overrides=self.store.caps(program.surface))
        first_steady = budget(program, today, today + timedelta(days=program.warm_up_days),
                              overrides=self.store.caps(program.surface))
        warnings = list(first_steady.notes)
        if program.risk is Risk.OFF and any(n for k, n in program.steady.daily.items() if k is not Kind.POST):
            warnings.append("risk is off, so only posting and replies on own posts run; the other targets are ignored")
        warnings += profile_notes(self.store.active(program.surface), program)
        return {
            "program": program.id, "version": fv.version, "hash": fv.hash, "state": fv.state.value,
            "created": created, "surface": program.surface, "summary": summarize(program),
            "yaml": to_yaml(program),
            "caps_day_1": {k.value: v for k, v in b.caps.items()},
            "caps_first_steady_day": {k.value: v for k, v in first_steady.caps.items()},
            "warnings": warnings,
            "next": (f"Nothing runs until the owner approves it on the node: agentos engage program approve "
                     f"{program.id} --version {fv.version}. Preview it with agentos engage plan "
                     f"{program.surface} --dry-run."),
        }

    def versions(self, program_id: str) -> list[ProgramVersion]:
        found = self.store.versions(program_id)
        if not found:
            raise NotFound(f"no program {program_id!r} (list them with agentos engage program show)")
        return found

    def program_for(self, surface: str) -> ProgramVersion | None:
        """The surface's approved program, else its latest draft."""
        active = self.store.active(surface)
        if active is not None:
            return active
        drafts = self.store.programs(surface)
        return drafts[-1] if drafts else None

    def approve(self, program_id: str, version: int | None = None, *, by: Any) -> ProgramVersion:
        if not is_owner(by):
            raise OwnerOnly("only the owner approves a program, in person (agentos engage program approve on "
                            "the node). Agents draft programs; they never approve them")
        found = self.versions(program_id)
        fv = found[-1] if version is None else next((v for v in found if v.version == version), None)
        if fv is None:
            raise NotFound(f"program {program_id} has no version {version}")
        if fv.state is ProgramState.APPROVED:
            return fv
        from agentos.engage.drafts import passthrough_problems

        problems = passthrough_problems(str(getattr(self.config, "engage_draft_provider", "off") or "off"),
                                        [fv.definition])
        if problems:       # test text is never sent without a card
            raise ProgramInvalid(problems[0])
        out = self.store.approve(program_id, fv.version, by=str(_who(by).get("id")), starts_on=self.today(fv.definition))
        self.store.notice("approved", f"{program_id} version {fv.version} approved", surface=fv.definition.surface)
        return out

    def set_cap(self, surface: str, kind: Kind | str, value: int | None, *, by: Any) -> dict[str, Any]:
        kind = Kind(kind)
        pv = self.program_for(surface)
        site = pv.definition.site if pv is not None else surface.split("/", 1)[1].split("-", 1)[0]
        current = self.store.caps(surface).get(kind)
        if value is None:
            if not is_owner(by):
                raise OwnerOnly("only the owner can change a cap")
        else:
            check_cap_change(by, site or "", kind, int(value), current or 0)
        self.store.set_cap(surface, kind, value, by=str((_who(by) or {}).get("id", "owner")))
        row = cap_row(site or "", kind)
        return {"surface": surface, "kind": kind.value, "cap": value, "hard_ceiling": row.normal if row else 0}

    def reset_profile(self, surface: str, fields: list[str] | None, *, by: Any) -> dict[str, Any]:
        """Owner only: forget what the account's rows say about these profile fields (every field
        when None), so each is due again and nothing blocks it (agentos.engage.profile.reset)."""
        if not is_owner(by):
            raise OwnerOnly("only the owner resets the profile's state, in person on the node")
        try:
            done = profile.reset(self.store, surface, fields)
        except ValueError as e:
            raise InvalidRequest(str(e)) from None
        self.store.add_history(surface, "profile_reset", {"fields": done}, at=self.now(),
                               by=str((_who(by) or {}).get("id", "owner")))
        return {"surface": surface, "reset": done}

    # ---------------------------------------------------------------- personas and time
    def persona(self, surface: str) -> Persona:
        found = self.store.persona(surface)
        if found is None:
            found = draw_persona(surface, secrets.token_hex(16))
            self.store.put_persona(found)
        return found

    def redraw_persona(self, surface: str) -> Persona:
        old = self.persona(surface)
        new = draw_persona(surface, old.seed, redraws=old.redraws + 1)
        self.store.put_persona(new)
        return new

    def timezone(self, program: Program) -> ZoneInfo:
        """The program's time zone, else the lane's pinned one, else UTC (with a notice, once per
        surface, since sessions then run at UTC hours)."""
        tz, problem = self._timezone(program)
        if problem is not None:
            key = f"tz_fallback:{program.surface}"
            if self.store.meta(key) is None:
                self.store.set_meta(key, problem)
                self.store.notice("timezone", f"{program.surface}: {problem}, so its sessions are planned in UTC. "
                                              "Set timezone in the program to fix it.", surface=program.surface)
        return tz

    def _timezone(self, program: Program) -> tuple[ZoneInfo, str | None]:
        """(the zone, why it fell back to UTC or None)."""
        return resolve_timezone(program, self.config)

    def today(self, program: Program) -> date:
        return self.now().astimezone(self.timezone(program)).date()

    def _account(self, pv: ProgramVersion) -> Account:
        program = pv.definition
        starts = pv.starts_on or self.today(program)
        return Account(program=program, persona=self.persona(program.surface), tz=self.timezone(program),
                       starts_on=starts, overrides=self.store.caps(program.surface),
                       profile_due=profile.due_count(self.store, program, now=self.now()))

    # ---------------------------------------------------------------- plans
    def plan(self, surface: str, *, days: int = 7, start: date | None = None) -> list[DayPlan]:
        """A dry run: `days` drawn days for the surface's program (approved, else the latest draft),
        drawn together with the company's other approved accounts so the 20 minute gap holds.
        Nothing is saved."""
        if not 1 <= days <= MAX_PLAN_DAYS:
            raise ProgramInvalid(f"days must be between 1 and {MAX_PLAN_DAYS}")
        pv = self.program_for(surface)
        if pv is None:
            raise NotFound(f"{surface} has no program yet; draft one with agentos engage program draft")
        me = self._account(pv)
        start = start or me.starts_on
        if pv.starts_on is None:      # a draft: the dry run's first day is its day 1
            me.starts_on = start
        others = [self._account(o) for o in self.store.approved()
                  if o.definition.workspace == pv.definition.workspace and o.definition.surface != surface]
        history = {a.surface: self.store.plans(a.surface, start - timedelta(days=7), start - timedelta(days=1))
                   for a in [me, *others]}
        return plan_days([me, *others], start, days, history=history)[surface]

    def day_plan(self, surface: str, day: date) -> DayPlan | None:
        """The surface's plan for `day`: the saved one, else drawn (with the same history a dry run
        uses, so the plan matches `plan --dry-run`) and saved. None before the program's day 1 or
        without an approved program."""
        saved = self.store.plans(surface, day, day)
        if saved:
            return saved[0]
        pv = self.store.active(surface)
        if pv is None or (pv.starts_on is not None and day < pv.starts_on):
            return None
        plan = self.plan(surface, days=1, start=day)[0]
        self.store.put_plan(plan)
        return plan

    def nightly(self, now: datetime | None = None) -> dict[str, Any]:
        """Save today's and tomorrow's plan for every approved program that has none, then (once a
        day) run the correlation audit. Never claims a lane or calls a connector."""
        now = now or self.now()
        self._sweep_items(now)
        drawn: list[str] = []
        accounts = [self._account(pv) for pv in self.store.approved()]
        for offset in (0, 1):
            by_day: dict[date, list[Account]] = {}
            for a in accounts:
                day = now.astimezone(a.tz).date() + timedelta(days=offset)
                if day < a.starts_on or self.store.plans(a.surface, day, day):
                    continue
                by_day.setdefault(day, []).append(a)
            for day, group in sorted(by_day.items()):
                companies = {a.program.workspace for a in group}
                # every company account's last week (and any plan already saved for `day`) is history,
                # so new starts keep the 20 minute gap from them
                history = {a.surface: self.store.plans(a.surface, day - timedelta(days=7), day)
                           for a in accounts if a.program.workspace in companies}
                plans = plan_days(group, day, 1, history=history)
                for surface, got in plans.items():
                    for p in got:
                        self.store.put_plan(p)
                        drawn.append(f"{surface} {p.day.isoformat()}")
        flags: list[dict[str, Any]] = []
        today = now.date().isoformat()
        if self.store.meta("audit_day") != today:
            flags = self.run_audit(now)
            self.store.set_meta("audit_day", today)
        return {"drawn": drawn, "audit": flags}

    def run_audit(self, now: datetime | None = None, *, redraw: bool = True) -> list[dict[str, Any]]:
        """The lagged correlation of every pair's hourly activity over the last 14 days. A flagged
        pair gets the second account's persona redrawn and a notice for the owner."""
        now = now or self.now()
        start = (now - timedelta(days=AUDIT_DAYS)).replace(minute=0, second=0, microsecond=0)
        series = {}
        for pv in self.store.approved():
            surface = pv.definition.surface
            plans = self.store.plans(surface, start.date() - timedelta(days=1), now.date() + timedelta(days=1))
            series[surface] = hourly_series(plans, start)
        flags = audit(series)
        for f in flags:
            text = (f"{f.a} and {f.b} were active at the same hours ({f.correlation:.2f} over {AUDIT_DAYS} days). "
                    f"{f.b} gets new habits from tomorrow. Nothing for you to do.")
            if redraw:
                self.redraw_persona(f.b)
            self.store.notice("in_sync", text, surface=f.b)
            log.warning("engage: %s", text)
        return [f.to_dict() for f in flags]

    def planned(self, start: datetime, end: datetime, *, workspace: str | None = None) -> dict[str, Any]:
        """The engagement sessions planned between `start` and `end`, for the agenda's timeline (read only).

        Only what nightly() saved: each approved program's day plans, drawn the night before for today
        and tomorrow. A later day is not drawn yet (its draw reads the days before it, the company's
        other accounts and a persona the audit may redraw), so it shows nothing rather than a guess.
        Session times are the drawn starts, in the lane's time zone, with their length. Engagement is a
        dry run on this node: the sessions are drawn and reported, and nothing clicks them yet.

        A program that is paused has no items, and `paused` says why: its lane is held (a hold stops
        the program until the owner hands the lane back, engagement-engine.md 5.11), quarantined or
        frozen. Drafts never run, so they have none either. Returns {"items", "paused", "note",
        "drawn_until": {surface: the last saved day}}."""
        now = self.now()
        lo = max(start, now)
        out: dict[str, Any] = {"items": [], "paused": [], "drawn_until": {}, "dry_run": True,
                               "note": "engagement draws each day's sessions the night before (today and "
                                       "tomorrow); later days are not drawn yet. A dry run on this node: "
                                       "nothing clicks them"}
        if end <= lo:
            return out
        for pv in self.store.approved():
            program = pv.definition
            surface = program.surface
            if workspace is not None and program.workspace != workspace:
                continue
            why = self.paused(surface)
            if why is not None:
                out["paused"].append({"lane": surface, "program": program.id, "reason": why})
                continue
            tz = self.timezone(program)
            plans = self.store.plans(surface, lo.astimezone(tz).date(), end.astimezone(tz).date())
            saved = self.store.plans(surface, now.astimezone(tz).date(), now.astimezone(tz).date() + timedelta(days=7))
            if saved:
                out["drawn_until"][surface] = max(p.day for p in saved).isoformat()
            for plan in plans:
                for i, session in enumerate(plan.sessions):
                    if not lo <= session.start < end:
                        continue
                    acts = ", ".join(f"{n} {k.value.replace('_', ' ')}" for k, n in session.actions.items() if n)
                    out["items"].append({
                        "lane": surface, "workspace": program.workspace, "program": program.id,
                        "at": session.start.astimezone(UTC).isoformat(),
                        "until": session.end.astimezone(UTC).isoformat(), "minutes": session.minutes,
                        "approx": False, "timezone": tz.key, "day": plan.day.isoformat(), "session": i + 1,
                        "title": f"Engagement session on {program.site or surface}" + (f" ({acts})" if acts
                                                                                       else " (browsing)"),
                        "effect": "commit" if any(session.actions.values()) else "read",
                        "manage": f"agentos engage status {surface}"})
        return out

    def paused(self, surface: str) -> str | None:
        """Why the surface's program does not run now, in words, or None: its lane is held, quarantined
        or frozen. A lane the lane manager does not know (or no lane manager here) pauses nothing."""
        runtime = self.runtime
        freeze = runtime.get("freeze") if runtime is not None else None
        frozen = getattr(freeze, "is_frozen", None)
        if callable(frozen) and frozen(surface):
            return f"{surface} is frozen"
        lanes = runtime.get("lanes") if runtime is not None else None
        if lanes is None:
            return None
        try:
            info = lanes.get(surface)
        except Exception:  # noqa: BLE001 - a lane that is gone pauses nothing here
            return None
        if info.state.value == "quarantined":
            return f"{surface} is quarantined: {info.quarantine_reason or 'see agentos lane check'}"
        if info.control.state.value == "paused":
            return f"{surface} is held" + (f" ({info.control.reason})" if info.control.reason else "") + \
                ", so its program is paused until the lane is handed back"
        return None

    # ---------------------------------------------------------------- status
    def status(self, surface: str | None = None, workspace: str | None = None,
               can_see: Any = None) -> dict[str, Any]:
        surfaces: list[str] = []
        seen: set[str] = set()
        for pv in self.store.programs():
            s = pv.definition.surface
            if s in seen or (surface and s != surface) or (workspace and pv.definition.workspace != workspace):
                continue
            if can_see is not None and not can_see(s):
                continue
            seen.add(s)
            surfaces.append(s)
        if surface and not surfaces:
            raise NotFound(f"{surface} has no engagement program")
        live = self.executor is not None and self.sessions_enabled()
        note = ("Engagement sessions run on this node: approved programs act at their planned times, under the caps."
                if live else "Engagement runs as a dry run on this node: plans are drawn and reported, nothing is "
                             "clicked.")
        return {"dry_run": not live, "surfaces": [self._surface_status(s) for s in surfaces], "note": note}

    def _surface_status(self, surface: str) -> dict[str, Any]:
        pv = self.program_for(surface)
        assert pv is not None
        program = pv.definition
        tz = self.timezone(program)
        _, tz_problem = self._timezone(program)
        now = self.now().astimezone(tz)
        today = now.date()
        saved = self.store.plans(surface, today, today)
        plan = saved[0] if saved else self.plan(surface, days=1, start=today)[0]
        counters = self.store.counters(surface, today)
        planned = plan.totals()
        upcoming = [s for s in plan.sessions if s.start > now]
        if plan.phase == "warm_up":
            stage = warm_up_stage(plan.phase_day)
            phase = f"warming up: day {plan.phase_day} of {program.warm_up_days} ({stage.describe()})"
        else:
            phase = f"steady: day {plan.phase_day}, ramp week {plan.ramp_weeks}"
        return {
            "surface": surface, "site": program.site, "risk": program.risk.value,
            "program": {"id": program.id, "version": pv.version, "hash": pv.hash, "state": pv.state.value,
                        "approved": pv.state is ProgramState.APPROVED},
            "phase": phase, "timezone": tz.key, "timezone_fallback": tz_problem,
            "today": {
                "day": today.isoformat(), "quiet": plan.quiet, "sessions": len(plan.sessions),
                "planned": {k.value: n for k, n in planned.items()},
                "done": {k.value: c["done"] for k, c in counters.items()},
                "caps": {k.value: n for k, n in plan.caps.items()},
                "next_session": window_name(upcoming[0].start) if upcoming else None,
                "saved": bool(saved),
            },
            "holds": [{k: h[k] for k in ("id", "kind", "signal", "reason", "since", "until", "detail")}
                      for h in self.store.holds(surface, at=self.now())],
            "notices": self.store.notices(surface, limit=5),
            "items": self.items.counts(surface),
            "drafts": self.drafts.counts(surface),
        }

    def caps_tables(self, sites: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
        return {site: ceiling_table(site) for site in (sites or SITES)}


def build(runtime: Any, *, home: Path | None = None) -> EngageService:
    """The service, with an Executor on the node's runner and lanes when the runtime has them.
    Windows still run only while the node switch `engage_sessions` is on."""
    from agentos.engage import drafts
    from agentos.engage.adapters import production_executor

    service = EngageService(runtime, home=home)
    service.executor = production_executor(service, runtime)
    drafts.wire(service, runtime)       # brief B3: drafts after items, when node.toml names a provider
    if runtime is not None:
        runtime.register(SERVICE_NAME, service)
    return service
