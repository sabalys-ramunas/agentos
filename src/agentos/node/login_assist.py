"""Assisted login: the owner fills a small form, the node types it into the lane's browser.

A person should almost never log in through VNC. Instead agentos reads the sign-in page, shows the
owner a form with exactly the fields this step needs (agentos.vault.steps), a fresh redacted
screenshot and the page's own message, types what the owner enters with the vault's typist at human
pace, presses the step's continue button and reads the next page. The panel's lane modal, the live
view's side section and `agentos lane login` are three faces of this one service (runtime
"login_assist", made on first use by `service_for(runtime)`).

One session per lane, at most:

    start      take control of the lane (agents stop: human_in_control), warm it, open the site's
               login page from the sites table (or `url`, or stay on the page the lane shows), read
    ask        the form is shown; `submit(step, values | choice)` types, presses, settles, reads
    waiting    "approve on your phone": waits for the page to move (an event, never a poll of the
               site's state), or for the owner's "read again"
    stopped    a captcha or a step it cannot read: the owner takes control in the live view
    paused     the owner took control; a hand back (or `resume`) reads the page again
    signed_in  the site's session cookie and an ok signal; control goes back to how it was. The owner
               may save what they typed as a vault entry bound to this lane and domain, which the
               `login` tool then uses with no form
    cancelled  the owner cancelled, or nobody touched the form for SESSION_IDLE_S

What the owner types:

- arrives over the panel session, the node token (CLI) or a signed live-view control link, as a
  Secret, and goes straight to the typist. It is never logged, traced, published, put in an error
  or returned. Results and events carry the step's kind and the page's words, never a value;
- is typed only into the fields of the step the owner saw: before typing, the page is read again
  and must be the same step (same origin, same fields, same button), and each field is checked with
  the vault's own decision (agentos.vault.policy.decide) for a one-off entry bound to this lane and
  this step's host: https (http on loopback), no frame from another origin, the right field role;
- stays in memory (username and password only, never a code) until the session ends, so the owner
  can pick "Save to the vault" at the end; then it is dropped.

Agents never see or type the values. They can only ask: `lane_login_request(on, why)` pushes the
owner and returns at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agentos.core import clock
from agentos.core.errors import AgentosError, InvalidRequest, PolicyDenied, ServiceUnavailable
from agentos.core.models import ControlState, LaneKind, Signal
from agentos.node import login_page
from agentos.node.claims import Holder
from agentos.node.sites import Site, expectation_for, host_is, site, site_for
from agentos.vault.domains import display_host, parse_origin, registrable_domain
from agentos.vault.errors import LoginClosed, LoginStepChanged, ResolutionDenied, TypistAborted
from agentos.vault.models import EntryKind, EntryMeta, validate_name
from agentos.vault.policy import ROLE_SINK, decide
from agentos.vault.secret import Secret
from agentos.vault.steps import Ask, PageRead, Step, StepKind, origin_text, read_step

log = logging.getLogger("agentos.login")

SETTLE_SECONDS = 15.0
APPROVE_WAIT_S = 300.0          # how long the "approve on your phone" step waits for the page to move
SESSION_IDLE_S = 15 * 60        # no owner action for this long: the session is cancelled
KEEP_DONE_S = 10 * 60           # a finished session (and its save offer) stays this long
OPERATION_WAIT_S = 90.0         # a route waits this long for a step to finish, then answers "working"
REQUEST_COOLDOWN_S = 120.0      # an agent's login request, at most once per lane in this window
REQUEST_OPEN_S = 86_400.0       # how long a request shows in Needs you when nobody starts a login
VALUE_MAX = 1024
WHY_MAX = 300
EVENT_KIND = "login"

ACTIVE = frozenset({"working", "ask", "waiting", "stopped", "paused"})
ENDED = frozenset({"signed_in", "cancelled", "done", "failed"})

Clock = Callable[[], float]


def _iso(ts: float | None) -> str | None:
    return clock.iso(ts, z=True) if ts else None  # 0 is "not yet", as before


def _new_id() -> str:
    return "la_" + secrets.token_hex(8)


@dataclass
class Session:
    id: str
    lane: str
    by: str                                  # who started it (owner, or a member's handle)
    started_at: float
    updated_at: float
    site: Site | None = None
    state: str = "working"
    step: Step | None = None
    step_no: int = 0
    note: str = ""
    error: str | None = None
    origin: str = ""
    version: int = 0
    shot: bytes | None = field(default=None, repr=False)
    shot_no: int = 0
    took_control: bool = False
    prior: ControlState = ControlState.AGENT
    prior_hold: bool = False
    values: dict[str, Secret] = field(default_factory=dict, repr=False)   # username, password: for save
    saved: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    waiter: asyncio.Task | None = field(default=None, repr=False)
    changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def step_id(self) -> str | None:
        return f"s{self.step_no}" if self.step is not None else None

    def forget(self) -> None:
        """Drop everything the owner typed and the picture."""
        self.values.clear()
        self.shot = None

    def public(self) -> dict[str, Any]:
        step = None
        if self.step is not None:
            step = {"id": self.step_id, **self.step.public()}
        can_save = (self.state in ("signed_in", "done") and not self.saved and "password" in self.values)
        return {"id": self.id, "lane": self.lane, "state": self.state,
                "site": self.site.key if self.site else None, "site_name": self.site.name if self.site else None,
                "origin": self.origin or None, "host": _host(self.origin), "step": step, "note": self.note,
                "error": self.error, "version": self.version, "screenshot": self.shot_no if self.shot else None,
                "can_save": can_save, "saved": self.saved, "by": self.by, "started_at": _iso(self.started_at),
                "updated_at": _iso(self.updated_at)}


def _host(origin: str) -> str | None:
    try:
        return display_host(parse_origin(origin).host) if origin else None
    except ValueError:
        return None


@dataclass
class LoginRequest:
    lane: str
    why: str
    by: str
    at: float

    def public(self) -> dict[str, Any]:
        return {"lane": self.lane, "why": self.why, "by": self.by, "at": _iso(self.at)}


def assist_entry(lane_id: str, origin: str) -> EntryMeta:
    """A one-off password entry bound to this lane and this step's host only: what the vault's
    decision checks a field against before an owner's value is typed into it."""
    host = parse_origin(origin).host
    now = datetime.now(UTC)
    return EntryMeta(id="assisted-login", name="assisted-login", kind=EntryKind.PASSWORD,
                     domain=registrable_domain(host), hosts=[host], lanes=[lane_id], has_username=True,
                     has_secret=True, has_totp=True, created_at=now, updated_at=now)


def _origin_of(url: str) -> str:
    return origin_text(url)


def _check_value(name: str, value: Any) -> Secret:
    text = value.reveal() if isinstance(value, Secret) else value
    if not isinstance(text, str) or not text:
        raise InvalidRequest(f"Fill in the {name} field.")
    if len(text) > VALUE_MAX:
        raise InvalidRequest(f"The {name} is longer than {VALUE_MAX} characters, too long to type.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise InvalidRequest(f"The {name} has a line break or a tab in it, so it cannot be typed into a field.")
    if name == "code":
        text = "".join(text.split()).replace("-", "")
    return Secret(text)


def _site_for_lane(info: Any) -> Site | None:
    keys = sorted(expectation_for(info.spec).sites)
    return site(keys[0]) if keys else None


class LoginAssist:
    """Assisted login sessions on this node (see the module doc)."""

    def __init__(self, runtime: Any, *, clock: Clock = time.time, settle_seconds: float = SETTLE_SECONDS,
                 approve_wait_s: float = APPROVE_WAIT_S):
        self.runtime = runtime
        self.clock = clock
        self.settle_seconds = settle_seconds
        self.approve_wait_s = approve_wait_s
        self._sessions: dict[str, Session] = {}
        self._requests: dict[str, LoginRequest] = {}
        self._reaper: asyncio.Task | None = None
        self._listening = False
        self._background: set[asyncio.Task] = set()  # resumes after a hand back, kept until done

    def __repr__(self) -> str:
        return f"LoginAssist(sessions={len(self._sessions)})"

    # ---- services ----
    @property
    def lanes(self) -> Any:
        return self.runtime.require("lanes")

    def _svc(self, name: str) -> Any:
        get = getattr(self.runtime, "get", None)
        return get(name) if callable(get) else None

    def _typist(self, page: Any) -> Any:
        factory = self._svc("typist")
        if factory is None:
            raise ServiceUnavailable("The typist is not configured on this node, so it cannot type a login.")
        return factory(page)

    def _publish(self, s: Session) -> None:
        bus = self._svc("events")
        if bus is None:
            return
        try:
            bus.publish(EVENT_KIND, {"id": s.id, "lane": s.lane, "state": s.state, "version": s.version,
                                     "step": s.step.kind.value if s.step else None})
        except Exception as e:  # noqa: BLE001 - a live update never fails a login
            log.warning("login: event not published: %s", type(e).__name__)

    def _changed(self, s: Session) -> None:
        s.version += 1
        s.updated_at = self.clock()
        s.changed.set()
        s.changed = asyncio.Event()
        self._publish(s)

    # ---- reading ----
    def session(self, lane_id: str) -> Session | None:
        s = self._sessions.get(lane_id)
        if s is not None and self._expired(s):
            return None
        return s

    def get(self, lane_id: str) -> dict[str, Any] | None:
        s = self.session(lane_id)
        out = s.public() if s is not None else None
        req = self._requests.get(lane_id)
        if req is not None and (s is None or s.state not in ENDED):
            out = {**(out or {"lane": lane_id, "state": "none"}), "requested": req.public()}
        return out

    def require(self, lane_id: str) -> Session:
        s = self.session(lane_id)
        if s is None:
            raise LoginClosed(f"No login is running on {lane_id}. Start one with the Log in button or "
                              f"`agentos lane login {lane_id}`.")
        return s

    def screenshot(self, lane_id: str) -> bytes | None:
        s = self.session(lane_id)
        return s.shot if s is not None else None

    async def wait_change(self, lane_id: str, version: int, timeout: float) -> dict[str, Any] | None:
        """The session once its version is past `version`, or as it is after `timeout` seconds."""
        s = self.session(lane_id)
        if s is not None and s.version <= version and timeout > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(s.changed.wait(), timeout)
        return self.get(lane_id)

    def requests(self) -> dict[str, dict[str, Any]]:
        """Agents' open login requests by lane (`{lane, why, by, at}`), for Needs you: one stays until a
        login starts on the lane, and a day at most. A lane whose login is running asks nothing."""
        now = self.clock()
        out: dict[str, dict[str, Any]] = {}
        for lane_id, req in list(self._requests.items()):
            s = self.session(lane_id)
            if now - req.at > REQUEST_OPEN_S or (s is not None and s.state in ACTIVE):
                continue
            out[lane_id] = req.public()
        return out

    # ---- agents ask ----
    def request(self, lane_id: str, *, why: str, by: str) -> dict[str, Any]:
        """An agent asks the owner to log the lane in. It notifies the owner and returns at once."""
        from agentos.node.lanes import clean_reason

        text = clean_reason(why)[:WHY_MAX]
        if not text:
            raise InvalidRequest('Say in one short sentence why the lane needs a login, for example "Reddit '
                                 'signed this lane out; I need it to post the reply."')
        info = self.lanes.get(lane_id)
        if info.spec.kind is not LaneKind.BROWSER:
            raise InvalidRequest(f"{lane_id} is a {info.spec.kind.value} lane; only browser lanes log in.")
        now = self.clock()
        old = self._requests.get(lane_id)
        s = self.session(lane_id)
        if s is not None and s.state in ACTIVE:
            return {"asked": False, "running": True, "lane": lane_id}
        if old is not None and now - old.at < REQUEST_COOLDOWN_S:
            return {"asked": False, "running": False, "lane": lane_id, "already_asked": True}
        req = LoginRequest(lane=lane_id, why=text, by=by, at=now)
        self._requests[lane_id] = req
        bus = self._svc("events")
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.publish(EVENT_KIND, {"lane": lane_id, "state": "requested", "version": 0, "step": None})
        notifier = self._svc("notifier")
        send = getattr(notifier, "send", None)
        target = _site_for_lane(info)
        name = target.name if target else lane_id
        if callable(send):
            try:
                send({"type": "login.requested", "title": f"Log in to {name} on {lane_id}",
                      "body": f"An agent asks you to log {lane_id} in: {text} Open the lane in the panel and "
                              "press Log in. You type into a form, and agentos types it into the site.",
                      "lane": lane_id, "urgent": False})
            except Exception as e:  # noqa: BLE001
                log.warning("login: request push not queued: %s", type(e).__name__)
        return {"asked": True, "running": False, "lane": lane_id}

    # ---- the session's lifecycle ----
    async def start(self, lane_id: str, *, by: str, url: str | None = None) -> dict[str, Any]:
        """Start a login on the lane (or return the one running) and read its first step."""
        lanes = self.lanes
        info = lanes.get(lane_id)
        if info.spec.kind is not LaneKind.BROWSER:
            raise InvalidRequest(f"{lane_id} is a {info.spec.kind.value} lane; only browser lanes log in.")
        if url is not None:
            from agentos.node.interaction.urls import check_url

            interaction = self._svc("interaction")
            url = check_url(url, allow_private=bool(getattr(interaction, "allow_private_targets", False)))
        current = self.session(lane_id)
        if current is not None and current.state in ACTIVE:
            return current.public()
        self._ensure_background()
        now = self.clock()
        s = Session(id=_new_id(), lane=lane_id, by=by, started_at=now, updated_at=now, site=_site_for_lane(info))
        s.note = "Opening the sign-in page."
        self._sessions[lane_id] = s
        self._requests.pop(lane_id, None)
        await self._take(s, by)
        self._changed(s)
        return await self._run(s, self._open(s, url))

    async def submit(self, lane_id: str, *, step: str, values: Mapping[str, Any] | None = None,
                     choice: str | None = None, by: str) -> dict[str, Any]:
        """Type the owner's values into the step's fields and press its button, or press the button
        the owner chose; then read the next step."""
        s = self.require(lane_id)
        if s.state != "ask" and not (choice and s.state in ("waiting", "stopped")):
            raise LoginStepChanged(self._not_now(s))
        if s.step is None or step != s.step_id:
            raise LoginStepChanged("The page moved on since this form was shown. Here is the step it shows now.")
        values = dict(values or {})
        wanted = {a.name for a in s.step.asks}
        if choice is not None:
            if values:
                raise InvalidRequest("Send either the values or a choice, not both.")
            if s.step.choice(choice) is None:
                raise InvalidRequest("That choice is not on this step.")
        else:
            unknown = set(values) - wanted
            if unknown:
                raise InvalidRequest(f"This step has no {', '.join(sorted(unknown))} field.")
            missing = [a.label for a in s.step.asks if a.name not in values]
            if missing:
                raise InvalidRequest(f"Fill in: {', '.join(missing)}.")
            if s.step.kind not in (StepKind.SIGN_IN, StepKind.CODE, StepKind.CONFIRM):
                raise InvalidRequest("This step has nothing to type. Pick one of its buttons.")
        clean = {name: _check_value(name, v) for name, v in values.items()}
        for ask in s.step.asks:
            if ask.name == "code" and len(ask.locators) > 1 and len(clean["code"]) != len(ask.locators):
                raise InvalidRequest(f"The code has {len(ask.locators)} characters.")
        self._touch(s, by)
        self._stop_waiter(s)
        s.state, s.error, s.note = "working", None, "Typing and continuing." if clean else "Pressing the button."
        self._changed(s)
        return await self._run(s, self._act(s, clean, choice))

    async def vault_sign_in(self, lane_id: str, *, by: str, trace: Any = None) -> dict[str, Any]:
        """Sign the lane in from its saved login, with no form and no question. A password in the vault is the
        owner's permission to use it (docs/PRINCIPLES.md). The vault's own flow decides every field: the
        entry must be bound to this lane and to this site, over https, and the field must be the right
        kind. Without a saved login on this page, nothing is typed and the owner is told why."""
        from agentos.vault.login import login_flow

        current = self.session(lane_id)
        if current is None or current.state not in ACTIVE:
            await self.start(lane_id, by=by)
        s = self.require(lane_id)
        if s.state != "ask" or s.step is None or s.step.kind is not StepKind.SIGN_IN:
            raise LoginStepChanged(self._not_now(s))
        vault = self._svc("vault")
        if vault is None:
            raise ServiceUnavailable("The vault is not set up on this node. Run `agentos vault init`.")
        ctx = OwnerVaultContext(self.runtime, lane_id=lane_id, by=by, sink=trace)
        self._touch(s, by)
        self._stop_waiter(s)
        s.state, s.error, s.note = "working", None, "Signing in from the saved login."
        self._changed(s)

        async def work() -> None:
            async with self._page(s) as page:
                typist = self._typist(page)
                result = await login_flow(ctx, vault, typist, lane_id=lane_id, entry=None)
            if not result.get("ok"):
                s.note = "The saved login did not sign in. Nothing else was typed."
            await self._read_now(s)

        return await self._run(s, work())

    async def refresh(self, lane_id: str, *, by: str) -> dict[str, Any]:
        """Read the page again (after an approval on the phone, or anything the owner did)."""
        s = self.require(lane_id)
        if s.state in ENDED:
            return s.public()
        self._touch(s, by)
        if self._busy(s):
            return s.public()
        self._stop_waiter(s)
        s.state, s.note = "working", "Reading the page again."
        self._changed(s)
        return await self._run(s, self._read_now(s))

    async def take(self, lane_id: str, *, by: str) -> dict[str, Any]:
        """The owner goes to the live view. The lane stays theirs; a hand back resumes the form."""
        s = self.require(lane_id)
        if s.state in ENDED:
            return s.public()
        self._touch(s, by)
        self._stop_waiter(s)
        await self._take(s, by)
        s.state = "paused"
        s.note = "You have the live view. Hand the lane back, or press Back to the form, and agentos reads the page."
        self._changed(s)
        return s.public()

    async def resume(self, lane_id: str, *, by: str) -> dict[str, Any]:
        s = self.require(lane_id)
        if s.state in ENDED:
            return s.public()
        self._touch(s, by)
        if self._busy(s):
            return s.public()
        self._stop_waiter(s)
        await self._take(s, by)
        s.state, s.note, s.error = "working", "Reading the page.", None
        self._changed(s)
        return await self._run(s, self._read_now(s))

    async def cancel(self, lane_id: str, *, by: str, done: bool = False) -> dict[str, Any]:
        """Stop the login (`done`: the owner says it is finished). Values are dropped unless a save
        is still on offer after a finished login."""
        s = self.require(lane_id)
        if s.state in ENDED:
            if not done and (s.values or s.shot):  # "no thanks" to the save offer: drop what was typed
                s.forget()
                self._changed(s)
            return s.public()
        self._stop_waiter(s)
        if s.task is not None and not s.task.done():
            s.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await s.task
        s.state = "done" if done else "cancelled"
        s.note = "You finished this login." if done else "Login cancelled. Nothing you typed was kept."
        if not done:
            s.forget()
            s.step = None
        await self._release(s, by, signed_in=done)
        self._changed(s)
        return s.public()

    async def save(self, lane_id: str, *, by: str, name: str | None = None) -> dict[str, Any]:
        """Save the username and password the owner typed as a vault entry bound to this lane and the
        domain they were typed on. Then they are dropped from the session."""
        s = self.require(lane_id)
        if s.saved:
            raise InvalidRequest(f"Already saved as vault entry '{s.saved}'.")
        if s.state not in ("signed_in", "done") or "password" not in s.values:
            raise InvalidRequest("There is nothing to save yet: the login has not finished with a password.")
        vault = self._svc("vault")
        if vault is None:
            raise ServiceUnavailable("The vault is not set up on this node. Run `agentos vault init`.")
        host = parse_origin(s.origin).host
        domain = registrable_domain(host)
        default = f"{(s.site.key if s.site else domain.split('.')[0])}-{s.lane.replace('/', '-')}"
        entry_name = validate_name((name or default).strip()[:64])
        await vault.ensure_unsealed()
        meta = await vault.add_entry(name=entry_name, kind=EntryKind.PASSWORD, domain=domain, lanes=[s.lane],
                                     username=s.values.get("username"), secret=s.values["password"])
        s.saved = meta.name
        s.forget()
        s.note = f"Saved as vault entry '{meta.name}'. Next time the login tool signs {s.lane} in with no form."
        self._changed(s)
        return {**s.public(), "entry": meta.public()}

    # ---- the work ----
    async def _run(self, s: Session, work: Awaitable[None]) -> dict[str, Any]:
        """Run one step of work as the session's task, so a client that goes away does not cut a
        login in half; wait for it a while and answer with the state."""

        async def guarded() -> None:
            try:
                await work
            except asyncio.CancelledError:
                raise
            except AgentosError as e:
                self._fail(s, e)
            except Exception as e:  # noqa: BLE001 - a broken read must not leave the form spinning
                log.warning("login on %s: step failed: %s", s.lane, type(e).__name__)
                self._fail(s, None)

        s.task = asyncio.create_task(guarded(), name=f"login:{s.lane}")
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(s.task), OPERATION_WAIT_S)
        return s.public()

    def _fail(self, s: Session, e: AgentosError | None) -> None:
        """A step of work failed: say so on the form. A step that was shown stays on offer."""
        if s.state in ENDED:
            return
        s.error = e.message if e is not None else "agentos could not read the page."
        if s.state == "working":
            step = s.step
            if step is None or step.stops or step.kind is StepKind.SIGNED_IN:
                s.state = "stopped"
                s.note = "Take control to look at the page, or read it again."
            elif step.kind is StepKind.APPROVE:
                s.state = "waiting"
                s.note = "Press Read again once you approved the sign-in on your phone."
            else:
                s.state = "ask"
        self._changed(s)

    def _busy(self, s: Session) -> bool:
        return s.task is not None and not s.task.done() and s.task is not asyncio.current_task()

    def _page(self, s: Session) -> Any:
        from agentos.node.tools.session import lane_session

        holder = Holder(key=f"login:{s.id}", label="assisted login", kind="human", identity=f"login:{s.by}")
        return lane_session(self.lanes, s.lane, "human", holder=holder)

    async def _open(self, s: Session, url: str | None) -> None:
        interaction = self._svc("interaction")
        async with self._page(s) as page:
            target = url
            if target is None and s.site is not None and s.site.login:
                here = page.url or ""
                try:
                    host = (parse_origin(here).host if here.startswith("http") else "")
                except ValueError:
                    host = ""
                on_site = bool(host) and any(host_is(host, h) for h in s.site.hosts)
                if not on_site:
                    target = s.site.login
            if target is not None:
                if interaction is not None:
                    await interaction.navigate(page, target)
                else:
                    await page.goto(target, wait_until="domcontentloaded")
                await self._typist(page).settle(self.settle_seconds)
        await self._read_now(s)

    async def _act(self, s: Session, values: dict[str, Secret], choice: str | None) -> None:
        assert s.step is not None
        shown = s.step
        async with self._page(s) as page:
            typist = self._typist(page)
            now = read_step(await self._read(s, page, typist))
            if now.shape != shown.shape:
                await self._show(s, now, page, typist)
                raise LoginStepChanged("The page changed before agentos typed anything. Here is the step it "
                                       "shows now.")
            pressed: str | None
            if choice is not None:
                picked = shown.choice(choice)
                assert picked is not None
                pressed = picked.locator
            else:
                pressed = shown.submit
            typed = await self._type(s, page, typist, shown, values) if values else None
            for name in ("username", "password"):
                if name in values:
                    s.values[name] = values[name]
            if pressed is not None:
                if _origin_of(page.url) != shown.origin:
                    raise LoginStepChanged("The page left the sign-in site before the button was pressed.")
                if not await login_page.press_action(page, pressed):
                    raise LoginStepChanged("The button moved before agentos could press it. Read the page "
                                           "again and try once more.")
            elif typed is not None:
                await typist.press(typed, "Enter")
            self._note_input(s)
            await typist.settle(self.settle_seconds)
        await self._read_now(s)

    async def _type(self, s: Session, page: Any, typist: Any, step: Step, values: dict[str, Secret]) -> Any:
        """Type each value into its field; every field checked by the vault's decision first."""
        entry = assist_entry(s.lane, step.origin)
        last = None
        try:
            for ask in step.asks:
                value = values[ask.name]
                parts = [value.reveal()] if len(ask.locators) == 1 else list(value.reveal())
                for locator, text in zip(ask.locators, parts, strict=True):
                    last = await self._type_one(s, typist, entry, ask, locator, text)
        except TypistAborted:
            raise LoginStepChanged("The page changed while agentos was typing, so it stopped. Nothing more was "
                                   "typed. Check the step and try again.") from None
        except ResolutionDenied as e:
            raise LoginStepChanged(f"agentos did not type into this page: {e.message.removeprefix('vault refused: ')}"
                                   ) from None
        return last

    async def _type_one(self, s: Session, typist: Any, entry: EntryMeta, ask: Ask, locator: str, text: str) -> Any:
        fields = await typist.fields()
        f = next((x for x in fields if x.locator == locator), None)
        if f is None:
            raise LoginStepChanged("A field of this step left the page. Here is what it shows now.")
        fresh = await typist.focus(f)
        url = await typist.page_url()
        if _origin_of(url) != s.step.origin:  # type: ignore[union-attr]
            raise LoginStepChanged("The page left the sign-in site, so agentos stopped typing.")
        pool = [fresh if x.locator == fresh.locator else x for x in await typist.fields()]
        decide(entry, lane_id=s.lane, sink=ROLE_SINK[ask.role], url=url, field=fresh, fields=pool).check()
        await typist.type_secret(fresh, text)
        return fresh

    async def _read(self, s: Session, page: Any, typist: Any) -> PageRead:
        interaction = self._svc("interaction")
        report = await interaction.report(page) if interaction is not None else None
        sig = report.signal if report is not None else Signal.UNKNOWN
        signed = await self._signed_in(s, page)
        here = site_for(page.url)
        approve = bool(here and here.asks_approval(page.url))
        return await login_page.read_page(page, typist, signal=sig, signal_detail=report.detail if report else None,
                                          signed_in=signed, approve=approve)

    async def _signed_in(self, s: Session, page: Any) -> bool:
        """The site's session cookies are in the lane's browser (read from the context, never a request)."""
        target = s.site or site_for(page.url)
        if target is None:
            return False
        url = target.home or target.login or page.url
        try:
            cookies = await page.context.cookies([url])
        except Exception:  # noqa: BLE001
            return False
        return target.missing_session(cookies) is None

    async def _read_now(self, s: Session) -> None:
        async with self._page(s) as page:
            typist = self._typist(page)
            step = read_step(await self._read(s, page, typist))
            await self._show(s, step, page, typist)
        if s.state == "signed_in":
            await self._release(s, s.by, signed_in=True)
            self._changed(s)

    async def _show(self, s: Session, step: Step, page: Any, typist: Any) -> None:
        s.shot = await login_page.screenshot(page, typist, await typist.fields())
        s.shot_no += 1
        s.step = step
        s.step_no += 1
        s.origin = step.origin or s.origin
        s.error = None
        name = s.site.name if s.site else (_host(step.origin) or "the site")
        if step.kind is StepKind.SIGNED_IN:
            s.state = "signed_in"
            s.note = f"Signed in to {name}. The lane keeps the session."
            s.values.pop("code", None)
        elif step.stops:
            s.state = "stopped"
            s.note = step.reason
        elif step.kind is StepKind.APPROVE:
            s.state = "waiting"
            s.note = "Approve the sign-in on your phone. agentos goes on when the page moves."
            self._wait_for_page(s)
        else:
            s.state = "ask"
            s.note = {StepKind.SIGN_IN: f"Sign in to {name}.", StepKind.CODE: "Type the code the site sent you.",
                      StepKind.CHOOSE: "Pick one.", StepKind.CONFIRM: "Press the button to go on."}.get(step.kind, "")
        self._changed(s)

    # ---- waiting for the phone ----
    def _wait_for_page(self, s: Session) -> None:
        self._stop_waiter(s)
        s.waiter = asyncio.create_task(self._page_moves(s), name=f"login-wait:{s.lane}")

    async def _page_moves(self, s: Session) -> None:
        """Wait for the active page to navigate (or for a tab to open or close), then read. The lane is
        not held while waiting, so the owner's own actions never queue behind this."""
        moved = asyncio.Event()

        def on_nav(frame: Any) -> None:
            if getattr(frame, "parent_frame", None) is None:
                moved.set()

        def on_any(*_: Any) -> None:
            moved.set()

        try:
            async with self._page(s) as page:
                context = page.context
                page.on("framenavigated", on_nav)
                page.on("close", on_any)
                context.on("page", on_any)
            try:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(moved.wait(), self.approve_wait_s)
            finally:
                for name, fn in (("framenavigated", on_nav), ("close", on_any)):
                    with contextlib.suppress(Exception):
                        page.remove_listener(name, fn)
                with contextlib.suppress(Exception):
                    context.remove_listener("page", on_any)
            if not moved.is_set():
                s.note = "Still waiting for the approval. Press Read again once you approved it on your phone."
                self._changed(s)
                return
            async with self._page(s) as page:
                await self._typist(page).settle(self.settle_seconds)
        except asyncio.CancelledError:
            return
        except AgentosError as e:
            self._fail(s, e)
            return
        if s.state == "waiting":
            s.state = "working"
            self._changed(s)
            try:
                await self._read_now(s)
            except AgentosError as e:
                self._fail(s, e)

    def _stop_waiter(self, s: Session) -> None:
        if s.waiter is not None and not s.waiter.done() and s.waiter is not asyncio.current_task():
            s.waiter.cancel()
        s.waiter = None

    # ---- control of the lane ----
    async def _take(self, s: Session, by: str) -> None:
        lanes = self.lanes
        info = lanes.get(s.lane)
        control = info.control
        if not s.took_control and control.state != ControlState.HUMAN:
            s.prior = control.state
            s.prior_hold = bool(control.reason)
        if control.state != ControlState.HUMAN:
            await lanes.take_control(s.lane, by=by)
            s.took_control = True
        self._note_input(s)

    def _note_input(self, s: Session) -> None:
        note = getattr(self.lanes, "note_human_input", None)
        if callable(note):
            with contextlib.suppress(Exception):
                note(s.lane)

    async def _release(self, s: Session, by: str, *, signed_in: bool) -> None:
        """Give the lane back as it was: to agents when they had it (or when a hold kept them off and the
        lane is now signed in); a human who had it keeps it."""
        if not s.took_control:
            return
        s.took_control = False
        give_back = s.prior == ControlState.AGENT or (signed_in and s.prior_hold)
        if not give_back:
            return
        try:
            await self.lanes.hand_back(s.lane, by=by)
        except AgentosError as e:
            log.info("login on %s: could not hand back: %s", s.lane, e.code)

    def _touch(self, s: Session, by: str) -> None:
        s.updated_at = self.clock()
        self._note_input(s)

    @staticmethod
    def _not_now(s: Session) -> str:
        return {"working": "agentos is still on the last step. Wait a moment.",
                "waiting": "This step waits for your phone. Press Read again once you approved it.",
                "paused": "You have the live view. Press Back to the form first.",
                "stopped": "This step needs you in the live view. Take control, or read the page again."}.get(
            s.state, "This login has ended.")

    # ---- background: hand back resumes, idle sessions end ----
    def _ensure_background(self) -> None:
        if not self._listening:
            add = getattr(self.lanes, "add_control_listener", None)
            if callable(add):
                add(self._on_control)
            self._listening = True
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap(), name="login-assist-reaper")

    def _on_control(self, lane_id: str, old: Any, new: Any) -> Awaitable[None] | None:
        s = self.session(lane_id)
        if s is None or s.state in ENDED or getattr(new, "state", None) != ControlState.AGENT:
            return None
        s.took_control = False
        if s.state in ("paused", "stopped", "ask", "waiting"):
            # not awaited here: the lane manager waits for its listeners, and a read takes seconds
            task = asyncio.get_running_loop().create_task(self._resume_after_handback(s),
                                                          name=f"login-resume:{lane_id}")
            self._background.add(task)  # a task nobody holds can be collected before it runs
            task.add_done_callback(self._background.discard)
        return None

    async def _resume_after_handback(self, s: Session) -> None:
        try:
            await self.resume(s.lane, by=s.by)
        except AgentosError as e:
            log.info("login on %s: not resumed after hand back: %s", s.lane, e.code)
        except Exception as e:  # noqa: BLE001 - logged by type only, as everywhere here: no secret in a log
            log.error("login on %s: resuming after the hand back failed: %s", s.lane, type(e).__name__)

    def _expired(self, s: Session) -> bool:
        now = self.clock()
        limit = KEEP_DONE_S if s.state in ENDED else SESSION_IDLE_S
        return now - s.updated_at > limit

    async def _reap(self) -> None:
        while self._sessions:
            await asyncio.sleep(30)
            for lane_id, s in list(self._sessions.items()):
                if not self._expired(s):
                    continue
                if s.state not in ENDED:
                    with contextlib.suppress(Exception):
                        await self.cancel(lane_id, by=s.by)
                s.forget()
                self._sessions.pop(lane_id, None)

    async def close(self) -> None:
        for s in list(self._sessions.values()):
            self._stop_waiter(s)
            if s.task is not None and not s.task.done():
                s.task.cancel()
            s.forget()
        self._sessions.clear()
        if self._reaper is not None:
            self._reaper.cancel()


class OwnerVaultContext:
    """The owner at this lane's sign-in, as the vault's tool context. The route has checked that the owner, or a
    member whose role may use the vault, is in person. A password in the vault is already the owner's
    permission, so nothing asks anyone here; the vault's own checks (agentos.vault.policy.decide) still decide
    every field. What the vault records goes to `sink`, which holds names and steps only, never a value."""

    def __init__(self, runtime: Any, *, lane_id: str, by: str, sink: Any = None):
        self.runtime = runtime
        self.lane_id = lane_id
        self.by = by
        self.principal = None
        self._sink = sink

    async def authorize(self, effect: Any, **kwargs: Any) -> None:
        """The owner's own saved login for this lane, in person: no grant to check, nothing to co-sign."""
        if kwargs.get("lane_id") not in (None, self.lane_id):
            raise PolicyDenied("This saved login is for another account.")

    def trace(self, kind: str, **fields: Any) -> None:
        if self._sink is not None:
            self._sink(kind, fields)

    def taint(self, *labels: str, source: str | None = None) -> None:
        return None

    def can_access_lane(self, lane_id: str) -> bool:
        return lane_id == self.lane_id


def service_for(runtime: Any) -> LoginAssist:
    """The node's assisted login service, made and registered on first use."""
    found = runtime.get("login_assist") if callable(getattr(runtime, "get", None)) else None
    if found is not None:
        return found
    if runtime.get("lanes") is None:
        raise ServiceUnavailable("This node has no lanes, so it cannot log one in.")
    svc = LoginAssist(runtime)
    runtime.register("login_assist", svc)
    return svc


__all__ = ["LoginAssist", "Session", "assist_entry", "service_for"]


VALUE_NAMES = ("username", "password", "code")


MAX_VALUE = 1024


def read_values(raw: Any) -> dict[str, Secret]:
    """{name: Secret} from the body's "values". Messages name the field, never its content."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > len(VALUE_NAMES):
        raise InvalidRequest('values is an object like {"username": "...", "password": "..."}.')
    out: dict[str, Secret] = {}
    for name, value in raw.items():
        if name not in VALUE_NAMES:
            raise InvalidRequest(f"values may name {', '.join(VALUE_NAMES)}.")
        if not isinstance(value, str) or not value or len(value) > MAX_VALUE:
            raise InvalidRequest(f"The {name} must be text of 1 to {MAX_VALUE} characters.")
        out[name] = Secret(value)
    return out
