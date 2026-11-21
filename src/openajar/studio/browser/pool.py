"""The review browser (S1): a small pool of headless browsers for measuring pages, on plain
Playwright, with no display. It implements openajar.studio.contracts.ReviewBrowser.

It is not a lane (docs/design/studio.md, decision 1) and has no path to one: it never touches the
lane manager, the vault, lane profiles or lane cookies. Each `page()` call gets a fresh browser
context (an empty, in-memory profile) that is closed when the call ends, so nothing a page set
(cookies, storage, a service worker) reaches the next call. Service workers run: a dev app that
mocks its API in one (MSW) renders nothing without it.

What a call does, in order:

1. checks the input (the url is http or https, the state has steps, the theme switch can work);
2. waits for a free slot (at most `Limits.max_pages` pages are open at once);
3. starts the engine if it is not running yet (one browser per engine, reused);
4. opens a context with the screen's viewport, scale, locale, timezone and colour scheme, reduced
   motion, a pinned clock and seeded random numbers, and the theme switch as an init script;
5. loads the url and fails when it did not load or answered with an HTTP error;
6. settles (network quiet with a cap, document.fonts.ready, animations finished, two frames);
7. walks the state's steps, and settles again;
8. fails when the page is blank or the theme did not apply (both read back from the page);
9. yields the page; `report(page)` says how it was rendered (engine, version, fonts, theme).

The whole of 2 to 8 has one time limit (`Limits.call_timeout_s`); a caller may keep the page
for `Limits.hold_timeout_s`, after which its context is closed under it.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from openajar.core.config import local_home
from openajar.core.errors import InvalidRequest
from openajar.studio.browser import devices, engines, settle
from openajar.studio.browser.environment import font_report, pinned
from openajar.studio.browser.errors import (
    BlankPage,
    HttpErrorPage,
    PageNotLoaded,
    StateNotReached,
    StudioTimeout,
    ThemeNotApplied,
)
from openajar.studio.contracts import EngineName, Screen, Step, Target

log = logging.getLogger("openajar.studio.browser")

ContextHook = Callable[[Any], Awaitable[None]]

# The proxy of the calls made in this context (a Playwright proxy dict), or None. An agent
# environment sets it for a studio check called from its chat (openajar.environments.routing), so the
# page is loaded through the environment's tunnel: http://localhost:3000 is port 3000 on its VM.
review_proxy: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("openajar_review_proxy",
                                                                                   default=None)

# Hosts a review page may never be pointed at: the cloud metadata services a node on a VPS can
# reach. A page that shows one of them in a screenshot would leak the machine's credentials.
METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "metadata.azure.com"})


@dataclass(frozen=True)
class Limits:
    max_pages: int = 4              # pages open at once, across engines
    call_timeout_s: float = 60.0    # waiting for a slot, starting, loading, state and settling
    hold_timeout_s: float = 300.0   # how long a caller may keep a page before it is closed
    nav_timeout_s: float = 30.0     # the url's document must arrive within this
    load_cap_s: float = 10.0        # then the load event, at most this long
    step_timeout_s: float = 10.0    # each step of a state
    network_quiet_s: float = 0.5    # no request in flight for this long counts as quiet
    network_cap_s: float = 5.0      # stop waiting for quiet after this (a stream, a poll)
    fonts_cap_s: float = 5.0        # document.fonts.ready, at most this long
    first_paint_s: float = 3.0      # a page that shows nothing yet after settling: wait this long for it
    dev_first_paint_s: float = 15.0  # the same on a dev server (a loopback or private host): unbundled modules
                                     # on a small machine take seconds (a Vite app on 2 vCPU, 2026-11-15)
    close_cap_s: float = 2.0        # closing a context or a browser; Playwright's stop ends what is left


DEFAULT_LIMITS = Limits()


def default_state_dir(where: str, node_home: Path | None = None) -> Path:
    """Where studio keeps its captures and runs (docs/design/studio.md, part 9): `~/.openajar/studio/`
    on a laptop, `<node home>/studio/` on a node (the node's home is $HOME of the agentos user)."""
    if where == "node":
        return (node_home or Path.home()) / "studio"
    return local_home() / "studio"


def check_url(url: str, what: str = "url") -> str:
    """http and https only (localhost included); never a cloud metadata address."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidRequest(f"{what} must be an http or https address with a host, like http://localhost:3000/ "
                             f"(got {url[:120]!r}).")
    host = parts.hostname.lower()
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if host in METADATA_HOSTS or (address is not None and address.is_link_local):
        raise InvalidRequest(f"{what} points at a link-local or cloud metadata address ({host}); the review browser "
                             "does not open those.")
    return url


def check_target(target: Target, screen: Screen) -> list[Step]:
    """Everything that can be said wrong before a browser opens. Returns the state's steps."""
    check_url(target.url)
    settle.check_switch(target.theme_switch)
    if screen.state == "default":
        steps = target.states.get("default", [])
    elif screen.state in target.states:
        steps = target.states[screen.state]
    else:
        known = ", ".join(sorted(target.states)) or "none"
        raise InvalidRequest(f"state {screen.state!r} has no steps in target.states (defined: {known}). Add its "
                             "steps, or use state 'default'.")
    for i, step in enumerate(steps, 1):
        if step.do in ("click", "fill", "hover", "wait_for") and not step.selector:
            raise InvalidRequest(f"state {screen.state!r} step {i} ({step.do}) needs a selector.")
        if step.do in ("press", "goto") and not step.value:
            raise InvalidRequest(f"state {screen.state!r} step {i} ({step.do}) needs a value "
                                 f"({'the key' if step.do == 'press' else 'the url'}).")
        if step.do == "goto":
            check_url(step.value or "", f"state {screen.state!r} step {i} url")
    return steps


async def _page_said(page: Any, limit: int = 3) -> str:
    """The page's own uncaught errors and console errors, newest last, capped: what an agent needs
    when a page rendered nothing, since it cannot open the review browser's console."""
    lines: list[str] = []
    with contextlib.suppress(Exception):
        for e in await page.page_errors():
            lines.append(f"uncaught {getattr(e, 'message', '') or e}")
    with contextlib.suppress(Exception):
        for m in await page.console_messages():
            if m.type in ("error", "warning"):
                lines.append(f"{m.type} {m.text}")
    lines = [" ".join(s.split())[:160] for s in dict.fromkeys(lines)]
    return "; ".join(lines[-limit:])


def _dev_host(url: str) -> bool:
    """A dev server's address: localhost, a loopback or a private address (on this computer, or on an
    environment's VM through its proxy)."""
    import ipaddress

    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_unspecified


def _first_line(exc: BaseException, limit: int = 200) -> str:
    text = str(exc).strip()
    for prefix in ("Page.goto: ", "Page.click: ", "Page.fill: ", "Page.hover: ", "Page.press: ",
                   "Page.wait_for_selector: ", "Keyboard.press: "):
        text = text.removeprefix(prefix)
    return text.split("\n", 1)[0][:limit]


# What a step's selector finds, and what a click there would land on: said when a step fails, so
# an agent knows whether its click landed and what was in the way.
DESCRIBE_TARGET = """
(sel) => {
  const name = (e) => {
    if (!e) return 'nothing';
    let s = e.tagName.toLowerCase();
    if (e.id) s += '#' + e.id;
    const c = [...e.classList].slice(0, 2).join('.');
    if (c) s += '.' + c;
    const t = (e.getAttribute('aria-label') || e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
    return t ? `${s} ("${t}")` : s;
  };
  let all;
  try { all = [...document.querySelectorAll(sel)]; } catch (e) { return { invalid: String(e.message || e).slice(0, 120) }; }
  const shown = all.filter((e) => e.checkVisibility ? e.checkVisibility({ opacityProperty: true, visibilityProperty: true })
    : e.getClientRects().length > 0);
  const out = { count: all.length, visible: shown.length };
  const first = shown[0] || all[0];
  if (first) {
    const r = first.getBoundingClientRect();
    out.first = name(first);
    out.box = [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
    out.in_view = r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth && r.width > 0 && r.height > 0;
    if (out.in_view) {
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      out.hit_self = !!hit && (hit === first || first.contains(hit));
      if (!out.hit_self) out.hit = name(hit);
    }
  }
  return out;
}
"""
PAGE_SIZE = "() => [document.getElementsByTagName('*').length, (document.body ? document.body.innerHTML.length : 0)]"


def _target_words(d: dict[str, Any], selector: str) -> str:
    if d.get("invalid"):
        return f"{selector!r} is not a valid selector ({d['invalid']})"
    if not d.get("count"):
        return f"{selector!r} matches nothing on the page now"
    words = f"{selector!r} matches {d['count']} element{'s' if d['count'] != 1 else ''}, {d.get('visible', 0)} visible"
    if d.get("first"):
        words += f"; the first is {d['first']} at {d.get('box')}"
        if d.get("in_view") is False:
            words += ", outside the viewport"
        elif d.get("hit_self") is False:
            words += f", under {d.get('hit')}, which would take the click"
    return words


# Connections that stay open by design (a live event stream, a websocket): never "in flight".
LONG_LIVED_TYPES = frozenset({"eventsource", "websocket"})


def long_lived(content_type: str | None) -> bool:
    """A response that streams for as long as the page is open (server-sent events)."""
    return (content_type or "").split(";", 1)[0].strip().lower() == "text/event-stream"


class _Network:
    """Requests in flight on one page, to wait for the network to go quiet. Event streams and
    websockets stay open for as long as the page does, so they never count: an EventSource
    request is left out when it starts, a fetch that streams text/event-stream once its headers
    arrive."""

    def __init__(self, page: Any):
        self.inflight: set[Any] = set()
        self.streams = 0
        self.changed = time.monotonic()
        page.on("request", self._start)
        page.on("response", self._response)
        page.on("requestfinished", self._end)
        page.on("requestfailed", self._end)

    def _start(self, request: Any) -> None:
        try:
            kind = request.resource_type
        except Exception:  # noqa: BLE001 - a request we cannot read is counted as in flight
            kind = None
        if kind in LONG_LIVED_TYPES:
            self.streams += 1
            return
        self.inflight.add(request)
        self.changed = time.monotonic()

    def _response(self, response: Any) -> None:
        try:
            streaming = long_lived(response.headers.get("content-type"))
            request = response.request
        except Exception:  # noqa: BLE001 - nothing to read: leave it as it is
            return
        if streaming and request in self.inflight:
            self.inflight.discard(request)
            self.streams += 1
            self.changed = time.monotonic()

    def _end(self, request: Any) -> None:
        self.inflight.discard(request)
        self.changed = time.monotonic()

    async def quiet(self, quiet_s: float, cap_s: float) -> bool:
        end = time.monotonic() + cap_s
        while True:
            now = time.monotonic()
            if not self.inflight and now - self.changed >= quiet_s:
                return True
            if now >= end:
                return False
            await asyncio.sleep(0.05)


class ReviewPool:
    """The review browser. See the module docstring. `chromium` picks Playwright's build, Chrome,
    or the first there (`auto`). `on_context` runs on every new context before the page opens
    (tests serve their pages through it with `context.route`)."""

    def __init__(self, *, chromium: engines.ChromiumFlavor = "auto", limits: Limits = DEFAULT_LIMITS,
                 where: str = "local", state_dir: Path | None = None, on_context: ContextHook | None = None):
        if chromium not in engines.CHROMIUM_FLAVORS:
            raise InvalidRequest(f"chromium must be one of {', '.join(engines.CHROMIUM_FLAVORS)}")
        self.chromium = chromium
        self.limits = limits
        self.where = where
        self.state_dir = state_dir if state_dir is not None else default_state_dir(where)
        self._on_context = on_context
        self._pw: Any = None
        self._pw_lock = asyncio.Lock()
        self._browsers: dict[EngineName, engines.Launched] = {}
        self._launch_locks: dict[EngineName, asyncio.Lock] = {e: asyncio.Lock() for e in EngineName}
        self._slots = asyncio.Semaphore(limits.max_pages)
        self._open = 0
        self._reports: dict[int, dict[str, Any]] = {}

    # ---- the engines ----

    async def _playwright(self) -> Any:
        async with self._pw_lock:
            if self._pw is None:
                api = engines.require_playwright()
                engines.point_playwright_at_builds()
                self._pw = await api.async_playwright().start()
            return self._pw

    async def _browser(self, engine: EngineName) -> engines.Launched:
        async with self._launch_locks[engine]:
            launched = self._browsers.get(engine)
            if launched is not None and launched.browser.is_connected():
                return launched
            if launched is not None:
                log.warning("the %s review browser had gone away; starting it again", engine.value)
            pw = await self._playwright()
            launched = await engines.launch(pw, engine, self.chromium)
            self._browsers[engine] = launched
            log.info("review browser started: %s (%s) %s", engine.value, launched.flavor, launched.version)
            return launched

    @property
    def fonts_pinned(self) -> bool:
        """Whether text renders in a pinned font set (False until one is bundled, environment.py)."""
        return bool(pinned()["fonts_pinned"])

    def running(self) -> dict[str, dict[str, str]]:
        return {engine.value: {"flavor": item.flavor, "version": item.version}
                for engine, item in self._browsers.items() if item.browser.is_connected()}

    @property
    def open_pages(self) -> int:
        return self._open

    async def close(self) -> None:
        """Close every browser and stop Playwright. The pool starts them again on the next call."""
        browsers, self._browsers = list(self._browsers.values()), {}
        cap = self.limits.close_cap_s
        for launched in browsers:
            # Google Chrome can take seconds to shut down on macOS; stopping Playwright below ends
            # a browser still closing, so the wait is capped.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(launched.browser.close(), cap)
        async with self._pw_lock:
            pw, self._pw = self._pw, None
        if pw is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pw.stop(), cap * 2)

    # ---- one page ----

    @asynccontextmanager
    async def page(self, target: Target, screen: Screen, *, device: str | None = None) -> AsyncIterator[Any]:
        """`device` (a name in devices.DEVICES) renders the page as that phone or tablet: its user
        agent, touch and mobile flag, on its own engine and at its own size. A device the pool
        cannot honour (another engine, another screen size, not in this Playwright) is refused."""
        steps = check_target(target, screen)
        found = devices.device(device) if device is not None else None
        if found is not None:
            devices.check_device(found, target.engine, screen)
        limits = self.limits
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limits.call_timeout_s
        try:
            async with asyncio.timeout_at(deadline):
                await self._slots.acquire()
        except TimeoutError:
            raise StudioTimeout(f"all {limits.max_pages} review pages stayed busy for {limits.call_timeout_s:g} s. "
                                "Wait for your other studio calls to finish, then call again.",
                                url=target.url, screen=screen.key) from None
        self._open += 1
        context: Any = None
        watchdog: asyncio.Task[None] | None = None
        page: Any = None
        stage = ["starting the engine"]
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    launched = await self._browser(target.engine)
                    stage[0] = "opening the page"
                    context = await self._new_context(launched, target, screen, found)
                    page = await context.new_page()
                    report = await self._prepare(page, launched, target, screen, steps, stage)
                    if found is not None:
                        report["device"] = found.label
                        note = devices.descriptor_note(found, self._pw.devices)
                        if note:
                            report.setdefault("notes", []).append(note)
            except TimeoutError:
                raise StudioTimeout(f"the page was not ready within {limits.call_timeout_s:g} s (stopped while "
                                    f"{stage[0]}). Check that the dev server answers quickly, or call with fewer "
                                    "screens.", url=target.url, screen=screen.key, stage=stage[0]) from None
            self._reports[id(page)] = report
            watchdog = asyncio.create_task(self._close_later(context, report, limits.hold_timeout_s))
            yield page
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if page is not None:
                self._reports.pop(id(page), None)
            if context is not None:
                await _close_context(context, self.limits.close_cap_s)
            self._open -= 1
            self._slots.release()

    def report(self, page: Any) -> dict[str, Any]:
        """How the page now open was rendered: engine and version, viewport, theme read back,
        fonts (pinned or not, which fell back), clock, network. Empty for a page not from here."""
        return dict(self._reports.get(id(page), {}))

    async def _close_later(self, context: Any, report: dict[str, Any], after: float) -> None:
        await asyncio.sleep(after)
        report.setdefault("notes", []).append(f"closed after {after:g} s: the page was held too long")
        log.warning("a review page was held for %ss; closing it", after)
        await _close_context(context, self.limits.close_cap_s)

    async def _new_context(self, launched: engines.Launched, target: Target, screen: Screen,
                           device: devices.Device | None = None) -> Any:
        extra = devices.context_options(device, self._pw.devices) if device is not None else {}
        proxy = review_proxy.get()
        if proxy:
            extra["proxy"] = dict(proxy)
        context = await launched.browser.new_context(
            **extra, viewport={"width": screen.width, "height": screen.height}, device_scale_factor=screen.scale,
            locale=target.locale, timezone_id=target.timezone, color_scheme=screen.theme.value,
            reduced_motion="reduce", forced_colors="none", service_workers="allow", accept_downloads=False,
            ignore_https_errors=True)  # a dev server's own certificate; nothing secret is ever sent from here
        try:
            context.set_default_timeout(self.limits.step_timeout_s * 1000)
            context.set_default_navigation_timeout(self.limits.nav_timeout_s * 1000)
            await context.clock.install(time=settle.PINNED_TIME)
            await context.add_init_script(settle.pins_script())
            script = settle.theme_script(target.theme_switch, screen.theme)
            if script:
                await context.add_init_script(script)
            if self._on_context is not None:
                await self._on_context(context)
        except BaseException:
            await _close_context(context, self.limits.close_cap_s)
            raise
        return context

    async def _prepare(self, page: Any, launched: engines.Launched, target: Target, screen: Screen,
                       steps: list[Step], stage: list[str]) -> dict[str, Any]:
        from playwright.async_api import Error as PlaywrightError

        limits = self.limits
        network = _Network(page)
        notes: list[str] = []
        stage[0] = "loading the url"
        try:
            response = await page.goto(target.url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            raise PageNotLoaded(f"{target.url} did not load: {_first_line(exc)}. Check that the dev server is running "
                                "and the address and port are right, then call again.", url=target.url) from None
        status = response.status if response is not None else None
        if status is not None and status >= 400:
            raise HttpErrorPage(f"{target.url} answered HTTP {status} ({response.status_text or 'error'}). Open the "
                                "address yourself or read the dev server's output, fix the error, then call again.",
                                url=target.url, status=status)
        try:
            await page.wait_for_load_state("load", timeout=limits.load_cap_s * 1000)
        except PlaywrightError:
            notes.append(f"the load event did not fire within {limits.load_cap_s:g} s; checked as it was")
        if target.wait_for:
            stage[0] = f"waiting for {target.wait_for}"
            try:
                await page.wait_for_selector(target.wait_for, state="visible")
            except PlaywrightError as exc:
                raise StateNotReached(f"wait_for {target.wait_for!r} was not visible within "
                                      f"{limits.step_timeout_s:g} s: {_first_line(exc)}. Check the selector, or "
                                      "leave wait_for out.", url=target.url, state="default",
                                      selector=target.wait_for) from None
        stage[0] = "settling"
        settled = await self._settle(page, network)
        if steps:
            stage[0] = f"reaching state {screen.state!r}"
            await self._walk(page, steps, screen.state)
            stage[0] = "settling"
            settled = await self._settle(page, network)
        notes.extend(settled.pop("notes"))
        stage[0] = "reading the page back"
        blank = await page.evaluate(settle.BLANK_TEST)
        painted = False
        if not blank.get("content"):
            stage[0] = "waiting for a first paint"
            painted, waited = await self._first_paint(page, target.url)
            if painted:  # a slow app: it rendered now, so it settles again before it is read
                notes.append(f"nothing showed for {waited:g} s after the page settled; checked once it rendered")
                stage[0] = "settling"
                notes.extend((await self._settle(page, network)).pop("notes"))
        if not blank.get("content") and not painted:
            said = await _page_said(page)
            raise BlankPage(f"{page.url} loaded but nothing rendered within {waited:g} s (no visible text, image or "
                            "control). If the app is slow, call again with wait_for set to a selector it shows when "
                            "ready, like \"main h1\". If it still shows nothing, it failed to render: "
                            + (f"the page's console said (page text, data): {said}. " if said else
                               "the page logged no error; read the dev server's output. ")
                            + "Fix it, then call again.", url=page.url, status=status, waited_s=waited)
        theme = await page.evaluate(settle.READ_THEME, settle.read_theme_args(target.theme_switch, screen.theme))
        if theme.get("applied") is False:
            raise ThemeNotApplied(_theme_failure(target, screen, theme), url=page.url, theme=screen.theme.value,
                                  how=target.theme_switch.how, read=theme.get("read"))
        if theme.get("applied") is None:
            notes.append(f"{screen.theme.value} theme not verified: {theme.get('note')}")
        shown = await page.evaluate("() => ({ width: innerWidth, height: innerHeight, scale: devicePixelRatio })")
        if shown["width"] != screen.width or abs(shown["scale"] - screen.scale) > 0.01:
            notes.append(f"the page reports {shown['width']} px at scale {shown['scale']}, not "
                         f"{screen.width} px at {screen.scale}")
        fell_back = await page.evaluate(settle.FONT_FALLBACKS)
        if fell_back:
            notes.append("fonts fell back: " + ", ".join(f"{f['family']} -> {f['used']}" for f in fell_back[:4]))
        report = environment_report(launched, self.where)
        report.update(
            screen=screen.key, url=page.url, status=status,
            viewport={"width": shown["width"], "height": shown["height"]}, scale=shown["scale"],
            locale=target.locale, timezone=target.timezone,
            theme={k: v for k, v in theme.items() if k in ("how", "wanted", "applied", "read", "media", "note")},
            fonts_fell_back=fell_back, **settled, notes=notes)
        report.update(font_report(fell_back))
        return report

    async def _first_paint(self, page: Any, url: str) -> tuple[bool, float]:
        """Wait for a page that shows nothing yet to show something: Limits.first_paint_s, or
        dev_first_paint_s on a dev server (_dev_host). Returns (rendered, seconds waited)."""
        cap = self.limits.dev_first_paint_s if _dev_host(url) else self.limits.first_paint_s
        loop = asyncio.get_running_loop()
        start = loop.time()
        while loop.time() - start < cap:
            await asyncio.sleep(min(0.25, cap))
            with contextlib.suppress(Exception):  # a page navigating away answers on the next read
                if (await page.evaluate(settle.BLANK_TEST)).get("content"):
                    return True, round(loop.time() - start, 1)
        return False, round(cap, 1)

    async def _settle(self, page: Any, network: _Network) -> dict[str, Any]:
        limits = self.limits
        notes: list[str] = []
        quiet = await network.quiet(limits.network_quiet_s, limits.network_cap_s)
        if not quiet:
            notes.append(f"the network was still busy after {limits.network_cap_s:g} s "
                         f"({len(network.inflight)} request(s) open); checked as it was")
        try:
            fonts = await asyncio.wait_for(page.evaluate(settle.FONTS_READY), limits.fonts_cap_s)
        except TimeoutError:
            fonts = "not ready"
            notes.append(f"web fonts were not ready after {limits.fonts_cap_s:g} s")
        animations = await page.evaluate(settle.FINISH_ANIMATIONS)
        await page.evaluate(settle.NEXT_FRAMES)
        return {"network": "quiet" if quiet else "busy", "fonts_ready": fonts == "loaded",
                "animations": animations, "notes": notes}

    async def _walk(self, page: Any, steps: list[Step], state: str) -> None:
        from playwright.async_api import Error as PlaywrightError

        done: list[str] = []      # what each step before the failing one did, for the error
        for i, step in enumerate(steps, 1):
            before = None
            if step.do in ("click", "fill", "press", "hover"):
                with contextlib.suppress(Exception):
                    before = await page.evaluate(PAGE_SIZE)
            try:
                if step.do == "goto":
                    response = await page.goto(step.value, wait_until="domcontentloaded")
                    if response is not None and response.status >= 400:
                        raise StateNotReached(f"state {state!r} step {i} of {len(steps)} (goto {step.value}) answered "
                                              f"HTTP {response.status}. Fix the step's url.", state=state, step=i,
                                              do=step.do, status=response.status)
                elif step.do == "click":
                    await page.click(step.selector)
                elif step.do == "fill":
                    await page.fill(step.selector, step.value or "")
                elif step.do == "hover":
                    await page.hover(step.selector)
                elif step.do == "wait_for":
                    await page.wait_for_selector(step.selector, state="visible")
                elif step.selector:
                    await page.press(step.selector, step.value)
                else:
                    await page.keyboard.press(step.value)
            except PlaywrightError as exc:
                what = f"{step.do} {step.selector or step.value}"
                seen = ""
                if step.selector:
                    with contextlib.suppress(Exception):
                        seen = " " + _target_words(await page.evaluate(DESCRIBE_TARGET, step.selector), step.selector) + "."
                landed = (" The click did not land: nothing was clicked." if step.do == "click" else "")
                earlier = (" Before it: " + "; ".join(done) + ".") if done else ""
                raise StateNotReached(f"state {state!r} step {i} of {len(steps)} ({what}) failed: {_first_line(exc)}."
                                      f"{landed}{seen}{earlier} Check the selector against the page at this width, "
                                      "or fix the state's steps.",
                                      state=state, step=i, do=step.do, selector=step.selector) from None
            after = None
            if before is not None:
                with contextlib.suppress(Exception):
                    after = await page.evaluate(PAGE_SIZE)
            changed = "the page changed" if after is not None and after != before else "nothing on the page changed"
            done.append(f"step {i} ({step.do} {step.selector or step.value}) landed and {changed}"
                        if step.do in ("click", "fill", "press", "hover") else f"step {i} ({step.do}) done")


def _theme_failure(target: Target, screen: Screen, theme: dict[str, Any]) -> str:
    switch = target.theme_switch
    wanted = screen.theme.value
    if switch.how == "media":
        if theme.get("media") != wanted:
            return (f"the browser did not take prefers-color-scheme: {wanted}. Try another engine, and say so to "
                    "the user.")
        return (f"the page has no styles for prefers-color-scheme: dark, so the {wanted} theme did not apply. If the "
                "site switches theme another way, pass theme_switch (attr, class or storage); if it has no dark "
                "theme, call again with themes=[\"light\"].")
    read = theme.get("read")
    shown = "nothing" if read in (None, "") else repr(str(read)[:80])
    where = {"attr": f"attribute {switch.name!r} on <html>", "class": "the class of <html>",
             "storage": f"localStorage {switch.name!r}"}[switch.how]
    return (f"the {wanted} theme did not apply: {where} reads {shown} after the page loaded (the page's own script "
            "changed it). Check theme_switch against how the site switches theme.")


def environment_report(launched: engines.Launched, where: str) -> dict[str, Any]:
    """The pinned environment: the same for every page of one engine."""
    return {"engine": launched.engine.value, "flavor": launched.flavor, "engine_version": launched.version,
            "headless": True, "where": where, **pinned()}


async def _close_context(context: Any, cap: float = DEFAULT_LIMITS.close_cap_s) -> None:
    """Close a context even when the caller is being cancelled: the close runs as its own task, and
    the wait for it is capped (a context still closing after that is closed with its browser)."""
    task = asyncio.ensure_future(context.close())
    try:
        await asyncio.wait_for(asyncio.shield(task), cap)
    except TimeoutError:
        task.add_done_callback(_quiet)
        log.warning("a review context took over %ss to close; left to close in the background", cap)
    except asyncio.CancelledError:
        task.add_done_callback(_quiet)  # the close goes on; its own failure is not worth a warning
        raise
    except Exception:
        log.debug("closing a review context failed", exc_info=True)


def _quiet(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()

