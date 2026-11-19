"""The pilot as a durable run: `browser_task` submits one, the runner runs it, `run_status` reads it.

A pilot run is a runner `Run` of kind `pilot`, so it has an id, `run_status`, `run_cancel` and an
idempotency key like any flow. This service is its executor: it mints the child grant, claims the
lane, runs the loop, and turns the result into the run's honest status.

A commit parks the run. The lane's claim goes back, so nobody else is held up while a person
decides, and the loop's state and the child token stay in this process. After an approval the
runner queues the run again and the same click goes, which the commit gate lets through only if it
sends what was approved. A node restart loses that state on purpose: a run with steps behind it ends
`pilot_interrupted` instead of starting the goal again, which could post twice.

A person can turn the worker off on one lane (`agentos plugins lane`). `browser_task` refuses then, a
queued run ends when it comes up, and a run that is going stops at its next step, because the switch
is read again at each one. Whether a publish asks is the lane's Ask me first, read by the policy.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentos.core.errors import AgentosError, InvalidRequest, NotFound, ServiceUnavailable
from agentos.core.models import Effect, Principal, RunKind, RunStatus, parse_lane_id
from agentos.core.runtime import Runtime
from agentos.node import egress
from agentos.pilot import grant as grants
from agentos.pilot.anchor import Target
from agentos.pilot.day import Day, count
from agentos.pilot.deciders import (
    BrainRescue,
    BrainText,
    Decider,
    JevDecider,
    Rescue,
    Text,
    TextWriter,
    Writer,
)
from agentos.pilot.errors import (
    PilotDayLimit,
    PilotGaveUp,
    PilotHeld,
    PilotInterrupted,
    PilotLaneOff,
    PilotLaneUncovered,
    PilotNeedsHelp,
    PilotRefused,
)
from agentos.pilot.hands import NodeTools, ToolFailed
from agentos.pilot.loop import LOST_LANE, Pilot, State, Stop
from agentos.pilot.models import Limits, Request, Result, Status, Value
from agentos.pilot.modes import GOAL

log = logging.getLogger("agentos.pilot")

SERVICE_NAME = "pilot"
PLUGIN = "browser-worker"  # agentos.plugins.entries: which brain does each job is set there
CLAIM_TTL_S = 300.0
CLAIM_WAIT_S = 30.0
SECRET_PART_RE = re.compile(r"passw|passcode|passphrase|pwd|secret|token|api.?key", re.IGNORECASE)  # anywhere in a name
SECRET_WORDS = frozenset({"pass", "pin", "otp", "totp", "2fa", "mfa", "cvv", "cvc", "ccv", "cookie"})  # only as a word
SECRET_CODES = frozenset({"otp", "sms", "auth", "verification", "verify", "security", "login", "2fa", "mfa", "recovery",
                          "backup"})  # `<word> code`: a promo code or a zip code is no secret
ERROR_CODES = {Status.HELD: PilotHeld.code, Status.REFUSED: PilotRefused.code, Status.GAVE_UP: PilotGaveUp.code,
               Status.NEEDS_HELP: PilotNeedsHelp.code}
RETIRED_SETTINGS = ("written_text_asks",)  # now the lane's Ask me first; an old file may still say it


class Settings(BaseModel):
    """`$AGENTOS_HOME/pilot/settings.toml`: the limits of a run. Which brain does each job is a
    person's choice in the plugin registry (`agentos plugins assign browser-worker <job> <brain>`)."""
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=25, ge=1, le=100)
    wall_s: float = Field(default=300.0, gt=0, le=3600)
    no_progress: int = Field(default=4, ge=1)
    max_usd: float = Field(default=0.25, gt=0)
    max_escalations: int = Field(default=6, ge=0)
    approval_window_s: float = Field(default=7200.0, ge=0)  # as long as a publish approval stays good
    steps_per_day: int = Field(default=500, ge=1)  # across every run on this node, midnight to midnight
    help_per_day: int = Field(default=30, ge=0)    # escalations to a stronger model
    usd_per_day: float = Field(default=2.0, gt=0)

    def limits(self, max_steps: int | None = None) -> Limits:
        return Limits(max_steps=min(max_steps or self.max_steps, self.max_steps), wall_s=self.wall_s,
                      no_progress=self.no_progress, max_usd=self.max_usd, max_escalations=self.max_escalations)


def settings_path(home: Path) -> Path:
    return Path(home) / "pilot" / "settings.toml"


def save_settings(home: Path, changes: dict[str, Any]) -> Settings:
    """Change some settings and keep the rest as they are in the file. A bad value is refused here,
    before anything is written. Only a person calls this: an agent has no route to it."""
    path = settings_path(home)
    current = tomllib.loads(path.read_text()) if path.is_file() else {}
    merged = {**current, **changes}
    settings = Settings(**merged)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(merged))
    return settings


def load_settings(home: Path | None) -> Settings:
    path = settings_path(home) if home is not None else None
    if path is None or not path.is_file():
        return Settings()
    try:
        data = tomllib.loads(path.read_text())
        for key in RETIRED_SETTINGS:
            if data.pop(key, None) is not None:
                log.warning("%s still sets %s, which does nothing now: Ask me first on each lane decides", path, key)
        return Settings(**data)
    except (tomllib.TOMLDecodeError, ValidationError, TypeError) as e:
        log.error("%s cannot be read, so the defaults apply: %s", path, type(e).__name__)
        return Settings()


def looks_secret(name: str) -> bool:
    """Does this value's name say it is a secret? It is read word by word, whichever way it is
    written: password_confirm, passwordConfirm, password1, 2FA code."""
    if SECRET_PART_RE.search(name):
        return True
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    words = [word.rstrip("0123456789") or word for word in re.split(r"[^a-z0-9]+", spaced.lower()) if word]
    pairs = set(pairwise(words))
    return (not SECRET_WORDS.isdisjoint(words) or any(a in SECRET_CODES and b == "code" for a, b in pairs)
            or ("card", "number") in pairs)


def parse_values(values: dict[str, Any] | None) -> list[dict[str, str]]:
    """`browser_task`'s values as stored rows. A string is plain text a model may read. A password has
    no business here: it is named by its vault entry, {"vault": "entry"}, and only a label is shown."""
    rows: list[dict[str, str]] = []
    for name, given in (values or {}).items():
        if not isinstance(name, str) or not name.strip():
            raise InvalidRequest("each value needs a short name, like \"comment\"")
        if isinstance(given, str):
            if not given:
                raise InvalidRequest(f"value {name} is empty")
            if looks_secret(name):
                raise InvalidRequest(f'{name} looks like a secret, and a model reads values. Pass it as '
                                     f'{{"vault": "<entry>"}} with an entry from vault_list, so only a label is shown.')
            rows.append({"name": name.strip(), "text": given})
        elif isinstance(given, dict) and set(given) == {"vault"} and isinstance(given["vault"], str) and given["vault"]:
            rows.append({"name": name.strip(), "vault": given["vault"]})
        else:
            raise InvalidRequest(f'value {name} must be a string, or {{"vault": "<entry>"}} for a stored login')
    return rows


def values_of(rows: list[dict[str, str]]) -> tuple[Value, ...]:
    out = []
    for i, row in enumerate(rows, start=1):
        if "vault" in row:
            out.append(Value(key=f"v{i}", label=f"the {row['name']} (hidden, the node fills it)", vault=row["vault"],
                             secret=True))
        else:
            out.append(Value(key=f"v{i}", label=f"the {row['name']} {row['text']!r}", text=row["text"]))
    return tuple(out)


class AskingWriter:
    """A writer whose words make every later publish of the run wait for a person, unless `mark` finds
    a person chose otherwise. A page can talk a model into words, so they are marked before they are
    typed, and a typed field can submit itself."""

    def __init__(self, writer: Writer, mark: Callable[[], None]):
        self.writer, self.mark, self.name = writer, mark, writer.name

    async def write(self, **kw: Any) -> Text:
        out = await self.writer.write(**kw)
        self.mark()
        return out


@dataclass
class Session:
    """One live run, between its steps and across a wait for a person."""
    request: Request
    state: State
    tools: NodeTools
    decider: Decider
    rescue: Rescue | None
    writer: Writer | None
    child: grants.ChildGrant
    journaled: int = 0
    approval_summary: str | None = None


class PilotService:
    def __init__(self, runtime: Runtime, settings: Settings | None = None, *,
                 wiring: tuple[dict[str, Any], Callable[..., Any], Callable[..., Any]] | None = None):
        self.runtime = runtime
        config = runtime.get("config")
        self.settings = settings or load_settings(getattr(config, "home", None))
        self._wiring = wiring
        self.sessions: dict[str, Session] = {}
        self.rules: tuple[egress.EffectRule, ...] | None = None  # what the lanes' guards read; None: the shipped rules

    def brain_for(self, job: str) -> str | None:
        """Who does a job, from the plugin registry, read at the start of each run. None when no
        brain is assigned or the one assigned no longer fits: `decide` then goes to whichever
        decision brain the router picks, and a run that needs `rescue` or `write` says so."""
        plugins = self.runtime.get("plugins")
        if plugins is None:
            return None
        try:
            return plugins.brain_for(PLUGIN, job)
        except (ServiceUnavailable, InvalidRequest, NotFound) as e:
            log.info("no brain for the pilot's %s job: %s", job, e.message)
            return None

    def attach(self) -> None:
        """Become the runner's executor for pilot runs."""
        from agentos.core.models import RunKind

        runner = self.runtime.require("runner")
        runner.register_executor(RunKind.PILOT, self)
        runner.on_finished.append(self._finished)

    def wiring(self) -> tuple[dict[str, Any], Callable[..., Any], Callable[..., Any]]:
        """The served tools, the policy wrapper and the token binder: what any MCP call goes through."""
        if self._wiring is not None:
            return self._wiring
        server, hooks = self.runtime.require("mcp"), self.runtime.require("mcp_hooks")
        if hooks.wrap is None or hooks.bind_token is None:
            raise InvalidRequest("the pilot needs the policy wrapper, and this node serves tools without one")
        return server.specs, hooks.wrap, hooks.bind_token

    # ---- building a run ------------------------------------------------------------------------

    def worker_stop(self, lane: str) -> str | None:
        """Why the worker may not act on this lane right now, or None. A person's switch, read fresh."""
        plugins = self.runtime.get("plugins")
        return plugins.lane_stop(PLUGIN, lane) if plugins is not None else None

    def check_worker(self, lane: str) -> None:
        why = self.worker_stop(lane)
        if why:
            raise PilotLaneOff(why)
        self.check_day()

    def runs(self, limit: int = 500) -> list[Any]:
        """This worker's runs, newest first."""
        return self.runtime.require("runner").list(kind=RunKind.PILOT, limit=limit)

    def live_rows(self, run_id: str) -> list[dict[str, Any]]:
        """The steps of a run still going, which it writes down only when it ends."""
        session = self.sessions.get(run_id)
        return list(session.state.rows) if session is not None else []

    def today(self) -> Day:
        return count(self.runs(), {run_id: session.state.n for run_id, session in self.sessions.items()})

    def check_day(self) -> None:
        """Today's limits, across every run. A run that starts is held to what is left (`_limits`)."""
        day, settings = self.today(), self.settings
        if day.steps >= settings.steps_per_day:
            raise PilotDayLimit(f"The worker has used today's {settings.steps_per_day} steps. A person can raise "
                                "the limit in Settings, Workers.")
        if day.usd >= settings.usd_per_day:
            raise PilotDayLimit(f"The worker has spent today's ${settings.usd_per_day:.2f}. A person can raise the "
                                "limit in Settings, Workers.")

    def _limits(self, max_steps: int | None) -> Limits:
        day, settings = self.today(), self.settings
        base = settings.limits(max_steps)
        return replace(base, max_steps=max(1, min(base.max_steps, settings.steps_per_day - day.steps)),
                       max_usd=max(0.0, min(base.max_usd, settings.usd_per_day - day.usd)),
                       max_escalations=max(0, min(base.max_escalations, settings.help_per_day - day.helped)))

    def change_limits(self, changes: dict[str, Any]) -> Settings:
        home = getattr(self.runtime.require("config"), "home", None)
        self.settings = save_settings(home, changes)
        return self.settings

    def check_lane(self, lane: str) -> None:
        """Refuse a lane that reaches a site nobody reviewed. No effect rule says `pay`, and a request to
        a host no rule names is never held, so on such a site a checkout would run as a plain draft with
        nobody asked. A list of hosts the gate reads, plus a platform's own reviewed CDNs and sign-in
        hosts, keeps money for a person."""
        sites = list(getattr(self.runtime.require("lanes").get(lane).spec, "sites", None) or ())
        if not sites:
            raise PilotLaneUncovered(f"{lane} has no site list, so the worker could reach a site the commit gate "
                                     "does not read, and pay there with no person. Give it a list of sites the gate "
                                     "knows, or drive it yourself.")
        left = egress.unreviewed_sites(sites, self.rules)
        if left:
            raise PilotLaneUncovered(f"the commit gate does not read {', '.join(left)} on {lane}, so the worker could "
                                     "pay there with no person. Take them off the lane's site list, or drive it "
                                     "yourself.")

    def _request(self, run: Any) -> Request:
        p = run.params
        rows = p.get("values") or []
        info = self.runtime.require("lanes").get(run.lane)
        mask = self._redact()  # a model reads these two, and a caller can paste anything into them
        return Request(lane=run.lane, goal=mask(p["goal"]), sites=tuple(getattr(info.spec, "sites", None) or ()),
                       values=tuple(replace(v, label=mask(v.label)) for v in values_of(rows)),
                       files=tuple(p.get("files") or ()), read=p.get("read"),
                       may_write=not rows and not p.get("read"), seed=run.id,
                       limits=self._limits(p.get("max_steps")),
                       target=Target.parse(p.get("target")), login=bool(p.get("login")))

    def _authorizer(self, run: Any, principal: Principal, brain: str) -> Callable[[str], Awaitable[None]]:
        """A model that writes needs the caller's own `brain` right, as brain_run does."""
        async def authorize(text: str) -> None:
            ctx = self.runtime.require("policy").context_for_grant(run.grant_id, principal=principal, tool="browser_task",
                                                                   lane_id=run.lane)
            await ctx.authorize(Effect.BRAIN, summary=f"Ask {brain} one question for pilot run {run.id}",
                                content={"run": run.id, "brain": brain,
                                         "text_sha256": hashlib.sha256(text.encode()).hexdigest()})

        return authorize

    def _rescue(self, run: Any, brains: Any, principal: Principal, workspace: str) -> Rescue | None:
        name = self.brain_for("rescue")
        if not name:
            return None
        return BrainRescue(brains, principal, workspace=workspace, brain=name,
                           authorize=self._authorizer(run, principal, name))

    def _writer(self, run: Any, brains: Any, principal: Principal, workspace: str, req: Request,
                child: grants.ChildGrant) -> Writer | None:
        name = self.brain_for("write")
        if not name:
            return None
        writer = TextWriter(BrainText(brains, principal, workspace=workspace, brain=name, max_usd=req.limits.max_usd,
                                      authorize=self._authorizer(run, principal, name)))
        return AskingWriter(writer, lambda: self._written(req.lane, child))

    def _written(self, lane: str, child: grants.ChildGrant) -> None:
        """Words were just written. They ask a person unless a person chose "Runs on its own" for this
        lane. No choice yet still asks, and Ask me first on makes the policy ask anyway."""
        policy = self.runtime.require("policy")
        if not policy.ask_first.chosen_off("lane", lane):
            policy.must_ask(child.grant_id, "publish")

    def _redact(self) -> Callable[[Any], Any]:
        from agentos.policy.redactor import Redactor
        from agentos.policy.scrub import runtime_scrub

        return Redactor(self.runtime.get("config"), scrub=runtime_scrub(self.runtime, lane_codes=True)).value

    def _open(self, run: Any) -> Session:
        self.check_worker(run.lane)  # again, and the lane's list too: both can change while a run waits in the queue
        self.check_lane(run.lane)
        request = self._request(run)
        principal = Principal(**run.principal)
        workspace, _ = parse_lane_id(run.lane)
        brains = self.runtime.require("brains")
        specs, wrap, bind = self.wiring()
        child = grants.mint(self.runtime.get("policy"), principal, run_id=run.id, lane=run.lane, mode=GOAL, specs=specs,
                            ttl_s=request.limits.wall_s + self.settings.approval_window_s,
                            max_usd=request.limits.max_usd,
                            specials=["secret"] if any(v.vault for v in request.values) else [])
        try:
            tools = NodeTools(self.runtime, lane=run.lane, token=child.token, grant_id=child.grant_id, run_id=run.id,
                              allowed=GOAL.tools, specs=specs, wrap=wrap, bind=bind, redact=self._redact())
            decider = JevDecider(brains, principal, workspace=workspace, job=run.id, max_usd=request.limits.max_usd,
                                 brain=self.brain_for("decide"))
            return Session(request, State(), tools, decider, self._rescue(run, brains, principal, workspace),
                           self._writer(run, brains, principal, workspace, request, child), child)
        except BaseException:
            self._revoke(child.grant_id)
            raise

    # ---- running it ------------------------------------------------------------------------------

    async def execute(self, run: Any, journal: Any, control: Any) -> Any:
        from agentos.runner.execution import Outcome

        session = self.sessions.get(run.id)
        if session is None:
            rows = journal.load()
            if rows:
                started = rows.get("0")
                if started is not None and started.result:
                    self._revoke(started.result["grant"])  # its token died with the process, and the run ends here
                return Outcome("finish", RunStatus.PARTIAL, error={
                    "code": PilotInterrupted.code, "message": "the node restarted during this run, so some of it may "
                    "have happened; it did not start the goal again, because that could do something twice"})
            try:
                session = self.sessions[run.id] = self._open(run)
            except AgentosError as e:
                return Outcome("finish", RunStatus.FAILED, error={"code": e.code, "message": e.message})
        try:
            await session.tools.call("browser_claim", on=run.lane, purpose=f"pilot run {run.id}", ttl_s=CLAIM_TTL_S,
                                     wait_s=CLAIM_WAIT_S)
        except ToolFailed as e:
            if e.code == "busy":
                return Outcome("defer", reason=e.message)
            status = self._status_if_stopped(session)
            self.end(run.id)
            code = PilotHeld.code if e.code in LOST_LANE else e.code  # a person has the lane: say so as a run does
            return Outcome("finish", status, error={"code": code, "message": e.message})
        if not session.state.started:  # before the first action: a kill from here on must not start the goal again
            journal.start("0", 0, "started")
            journal.done("0", {"grant": session.child.grant_id})

        def write(row: dict[str, Any]) -> None:
            session.journaled += 1
            path = str(session.journaled)
            journal.start(path, session.journaled, str(row.get("ran") or row.get("op") or "step"))
            journal.done(path, {k: v for k, v in row.items() if k != "element"})  # labels hold page text

        def checkpoint() -> None:
            control.check()
            why = self.worker_stop(run.lane)
            if why:
                raise Stop(Status.REFUSED, PilotLaneOff.code, message=why)

        pilot = Pilot(session.tools, session.decider, rescue=session.rescue, writer=session.writer,
                      checkpoint=checkpoint, on_step=write)
        try:
            result = await pilot.run(session.request, session.state)
        except Exception as e:
            why = getattr(e, "why", None)
            if why == "drain":
                return Outcome("requeue", reason="the node is stopping")
            if why == "cancel":
                self.end(run.id)
                return Outcome("cancel", RunStatus.CANCELLED, error={"code": "cancelled", "message": "cancelled"})
            status = self._status_if_stopped(session)
            self.end(run.id)
            if status is RunStatus.FAILED:
                raise
            log.exception("pilot run %s crashed after it acted", run.id)
            return Outcome("finish", status, error={"code": "internal", "message": "the run crashed after it acted, so "
                                                    "check the lane before anything is tried again"})
        finally:
            with contextlib.suppress(ToolFailed):
                await session.tools.call("browser_release", on=run.lane)
        if result.status is Status.WAITING_APPROVAL:
            session.approval_summary = result.approval_summary
            return Outcome("park", approval_id=result.approval_id, reason=result.approval_summary)
        self.end(run.id)
        return outcome_of(result, Outcome)

    @staticmethod
    def _status_if_stopped(session: Session) -> RunStatus:
        """PARTIAL once a step began, because it may have landed: `failed` invites a retry that posts twice."""
        return RunStatus.PARTIAL if session.state.rows or session.state.effects or session.journaled else RunStatus.FAILED

    def _revoke(self, grant_id: str) -> None:
        policy = self.runtime.get("policy")
        if policy is None:
            return
        try:
            policy.revoke(grant_id, by="pilot", reason="the run ended")
        except Exception:  # noqa: BLE001 - it expires on its own
            log.warning("could not revoke pilot grant %s", grant_id)

    def end(self, run_id: str) -> None:
        session = self.sessions.pop(run_id, None)
        if session is not None:
            self._revoke(session.child.grant_id)

    def _finished(self, run: Any) -> None:
        """A run can end without coming back here: a person denied its commit, or it expired."""
        if run.id in self.sessions:
            self.end(run.id)


def outcome_of(result: Result, outcome: Callable[..., Any]) -> Any:
    """The run's honest status. Held, refused and the like are PARTIAL when an action already
    landed, because then the page is not as it was, and FAILED when nothing did."""
    out = result.public()
    if result.status is Status.DONE_VERIFIED:
        return outcome("finish", RunStatus.SUCCEEDED_VERIFIED, proof={"readbacks": result.readbacks}, output=out)
    if result.status is Status.DONE_UNVERIFIED:
        return outcome("finish", RunStatus.SUCCEEDED_UNVERIFIED, output=out, proof={
            "readbacks": result.readbacks, "note": result.message or result.reason})
    status = RunStatus.PARTIAL if result.effects else RunStatus.FAILED
    message = result.message or result.reason or str(result.status)
    if result.needs:
        message = f"{message}. It needs {result.needs}"
    code = PilotLaneOff.code if result.reason == PilotLaneOff.code else ERROR_CODES[result.status]
    return outcome("finish", status, output=out, proof={"readbacks": result.readbacks},
                   error={"code": code, "message": message})


def build(runtime: Runtime) -> PilotService:
    service = PilotService(runtime)
    runtime.register(SERVICE_NAME, service)
    if runtime.has("runner"):
        service.attach()
    return service
