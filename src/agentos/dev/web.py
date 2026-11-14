"""The fake web a dev lane browses: one tab per lane, over a site with a feed, a composer dialog, a
profile, a like button that changes state, and a captcha page that reports the captcha signal.

Each tab is a runner.fakes FakeDriver, so a flow replays on the same tab an agent sees. The
interaction reads the tab's element tree for snapshots and acts only through the tab's own
operations (click, type, fill, select, press, scroll), the way a person would at the page.

Each tab also has a network (agentos.dev.net): the composer's Share sends a publish request
through the lane's egress guard, so the commit gate holds an agent's post on the dev node as it
does on a real lane. A held post does not happen, and the composer keeps what was typed. Every
page the tab loads (a navigation, or a click that leads to another page) plays its traffic and
console through that network too, so browser_console and browser_network see a profile page's
failing API call and page error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from agentos.core.errors import InvalidRequest, RefNotFound
from agentos.core.models import ActionKind, ActionResult, NavigateResult, Signal, SnapshotResult
from agentos.dev.image import Color, Raster
from agentos.dev.lanes import FakePage
from agentos.node.interaction import WAIT_MAX_S, UploadResult, WaitFor, WaitResult, _Watch, url_matches
from agentos.node.snapshot import find_text, outline_texts
from agentos.runner.driver import ElementNotFound
from agentos.runner.fakes import BASE, El, FakeDriver, FakeSite, social_site

CHALLENGE_URL = f"{BASE}/challenge"
PUBLISH_KEY = "share"  # the composer's Share: it sends a publish request (agentos.dev.net)
FILE_KEY = "file"  # the composer's "Select from computer": browser_upload answers its chooser
ACTIONABLE = frozenset({"button", "link", "textbox", "checkbox"})
WIDTH, HEIGHT = 960, 600
BAR = 40  # the address bar at the top of the picture

PAPER: Color = (250, 250, 248)
BAR_COLOR: Color = (226, 228, 234)
FIELD: Color = (255, 255, 255)
INK: Color = (60, 64, 72)
MUTED: Color = (160, 166, 176)
LINE: Color = (196, 200, 210)
BUTTON: Color = (228, 233, 244)
BUTTON_ON: Color = (108, 140, 255)
LINK: Color = (50, 110, 220)
CHECK: Color = (60, 170, 110)
EDGE: dict[Signal, Color] = {Signal.OK: (60, 170, 110), Signal.CAPTCHA: (230, 160, 60)}


WAIT_POLL_S = 0.1


def signal_for(url: str) -> Signal:
    return Signal.CAPTCHA if url == CHALLENGE_URL else Signal.OK


def _challenge(site: FakeSite, driver: FakeDriver) -> El:
    def solved(s: FakeSite, el: El, d: FakeDriver) -> str:
        return f"{BASE}/"

    return El("document", "Confirm you are human").add(El("main").add(
        El("heading", "Confirm you are human"),
        El("text", text="Verify you are human to continue. A person has to solve this check."),
        El("checkbox", "I am not a robot", key="human", attrs={"aria-checked": "false"}, on_click=solved),
    ))


def _blank(site: FakeSite, driver: FakeDriver) -> El:
    return El("document", "New tab").add(El("main").add(El("text", text="Start with an address in the bar.")))


def _compose(site: FakeSite, driver: FakeDriver) -> El:
    """social_site's composer, whose Share sends a publish request first (DevDriver.click)."""
    def share(s: FakeSite, el: El, d: FakeDriver) -> str:
        caption = d.values.get("caption", "")
        s.state["posts"].append({"id": str(len(s.state["posts"]) + 1), "caption": caption,
                                 "image": d.values.get("file")})
        return f"{BASE}/u/{s.state['account']}"

    dialog = El("dialog", "Create new post").add(El("button", "Select from computer", key=FILE_KEY))
    if driver.values.get(FILE_KEY):  # the preview a site shows once the photo is read
        dialog.add(El("status", "Photo", text=f"{driver.values[FILE_KEY]} attached"))
    dialog.add(
        El("textbox", "Write a caption...", key="caption"),
        El("button", "Share", key=PUBLISH_KEY, on_click=share),
    )
    if site.state.get("share_failed"):
        dialog.add(El("status", "Not shared", text="Couldn't share your post. Try again."))
    return El("document", "Create post").add(dialog)


def dev_site() -> FakeSite:
    """The site one lane's tab sees: each lane has its own profile, so its own state."""
    site = social_site()
    site.route(f"{BASE}/compose", _compose)
    site.route(CHALLENGE_URL, _challenge)
    site.route("about:blank", _blank)
    return site


class DevDriver(FakeDriver):
    """One lane's fake browser: a FakeDriver over dev_site() that counts every change (`version`)
    and reports the signal of the page it is on."""

    def __init__(self, site: FakeSite, *, url: str = "about:blank"):
        super().__init__(site, url=url)
        self.net: Any = None  # agentos.dev.net.DevContext, set by new_tab
        self.version = 0
        self._loaded: str | None = None  # the page whose traffic the network last played
        self.after_hooks.append(self._changed)
        self._render()

    def _changed(self, op: str, target: Any, driver: FakeDriver) -> None:
        self.version += 1
        if self.net is not None and (op == "navigate" or self.url != self._loaded):
            self._loaded = self.url
            self.net.load(self.url, found=self.root.name != "Not found")

    async def navigate(self, url: str) -> NavigateResult:
        fetch = getattr(self, "fetch", None)  # an environment's browser lane (agentos.dev.proxied)
        if fetch is not None and not url.startswith(BASE) and url != "about:blank":
            status, final, root = await fetch(url)
            self.site.route(final, lambda _s, _d, r=root: r)
            result = await super().navigate(final)
            return result.model_copy(update={"status": status, "signal": Signal.OK})
        result = await super().navigate(url)
        return result.model_copy(update={"signal": signal_for(url)})

    async def click(self, element: El) -> None:
        """The composer's Share (PUBLISH_KEY) sends its publish request first; when a route aborts
        it, nothing is posted and the page stays as it is (its refs too), with what was typed."""
        if element.key == PUBLISH_KEY and self.net is not None:
            from agentos.dev.net import publish_request

            media = [self.values["media_id"]] if self.values.get("media_id") else []
            actor = str(self.site.state.get("actor") or "biz-a")  # who the composer posts as (a test sets a Page)
            if not await self.net.send(publish_request(self.values.get("caption", ""), media, actor=actor)):
                self.site.state["share_failed"] = True
                self.version += 1
                return
            self.site.state["share_failed"] = False
        await super().click(element)

    async def upload(self, element: El, file_ref: str) -> None:
        await super().upload(element, file_ref)
        self._render()  # the composer shows what it holds

    async def signal(self) -> Signal:
        return signal_for(self.url)

    async def screenshot(self) -> bytes:
        return render_page(self).png()


def new_tab(lane_id: str) -> FakePage:
    from agentos.dev.net import attach

    driver = DevDriver(dev_site())
    page = FakePage(lane_id, driver=driver)
    attach(page, driver)
    return page


# ---- the picture of a page ----


class _Cursor:
    def __init__(self, x: int, y: int):
        self.x, self.y = x, y


def render_page(driver: DevDriver) -> Raster:
    """The page as a picture: the address bar (edged by the page's signal), then the element tree
    as boxes. Words are not drawn; the snapshot has them. Layout is top to bottom, clipped at the edge."""
    raster = Raster(WIDTH, HEIGHT, PAPER)
    raster.rect(0, 0, WIDTH, BAR, BAR_COLOR)
    raster.rect(0, BAR - 4, WIDTH, 4, EDGE[signal_for(driver.url)])
    raster.rect(120, 9, WIDTH - 240, BAR - 18, FIELD)
    _draw(raster, driver, driver.root, _Cursor(48, BAR + 28))
    return raster


def _draw(raster: Raster, driver: DevDriver, el: El, cur: _Cursor) -> None:
    if cur.y >= HEIGHT - 24:
        return
    role = el.role
    if role == "heading":
        raster.rect(cur.x, cur.y, min(560, 10 * len(el.name) + 60), 24, INK)
        cur.y += 40
    elif role in ("text", "status"):
        label = el.text or el.name
        raster.rect(cur.x, cur.y + 4, min(640, 7 * len(label) + 20), 8, MUTED)
        cur.y += 24
    elif role == "button":
        pressed = el.attrs.get("aria-pressed") == "true"
        width = max(120, 9 * len(el.name or el.text) + 40)
        raster.rect(cur.x, cur.y, width, 32, BUTTON_ON if pressed else BUTTON)
        raster.outline(cur.x, cur.y, width, 32, LINE, 1)
        cur.y += 44
    elif role == "link":
        raster.rect(cur.x, cur.y + 6, 8 * len(el.name) + 20, 2, LINK)
        cur.y += 28
    elif role == "checkbox":
        raster.outline(cur.x, cur.y, 22, 22, INK, 2)
        if el.attrs.get("aria-checked") == "true":
            raster.rect(cur.x + 5, cur.y + 5, 12, 12, CHECK)
        cur.y += 34
    elif role == "textbox":
        raster.rect(cur.x, cur.y, 420, 32, FIELD)
        raster.outline(cur.x, cur.y, 420, 32, LINE, 1)
        value = driver.values.get(el.key or el.name)
        if value:
            raster.rect(cur.x + 10, cur.y + 11, min(400, 7 * len(value)), 10, INK)
        cur.y += 44
    elif role in ("article", "dialog"):
        top, left = cur.y, cur.x
        cur.x, cur.y = left + 16, cur.y + 12
        for child in el.children:
            _draw(raster, driver, child, cur)
        cur.x, cur.y = left, cur.y + 12
        raster.outline(left - 8, top - 4, min(WIDTH - 2 * left, 600), cur.y - top + 8,
                       INK if role == "dialog" else LINE, 2)
        return
    for child in el.children:
        _draw(raster, driver, child, cur)


def frame_of_page(page: FakePage) -> tuple[int, Raster]:
    """(version, picture) of a lane's tab, rendered again only when the tab changed."""
    driver = page.driver
    if driver is None:
        return -1, Raster(WIDTH, HEIGHT, PAPER)
    if page.frame is None or page.frame[0] != driver.version:
        page.frame = (driver.version, render_page(driver))
    return page.frame


# ---- the outline an agent reads ----


def outline(driver: DevDriver) -> tuple[str, dict[str, El]]:
    """An accessibility outline of the tab, one element a line, and the refs of its actionable
    elements (e1, e2, ...). Attributes come out as `pressed=true`, `value="..."` and so on."""
    lines: list[str] = []
    refs: dict[str, El] = {}

    def walk(el: El, depth: int) -> None:
        pad = "  " * depth
        if el.role == "document":
            for child in el.children:
                walk(child, depth)
            return
        if el.role in ACTIONABLE:
            ref = f"e{len(refs) + 1}"
            refs[ref] = el
            lines.append(f'{pad}- {el.role} "{el.name or el.text}" [ref={ref}]{_attrs(driver, el)}')
        elif el.role == "heading":
            lines.append(f'{pad}- heading "{el.name}"')
        elif el.role == "text":
            lines.append(f'{pad}- text: "{el.text or el.name}"')
        elif el.role == "status":
            lines.append(f'{pad}- status "{el.name}": {el.text}')
        else:
            lines.append(f'{pad}- {el.role}' + (f' "{el.name}"' if el.name else ""))
            if el.text:  # a post's caption, a comment's words: what a read-back looks for
                lines.append(f'{pad}  - text: "{el.text}"')
            for child in el.children:
                walk(child, depth + 1)
            return
        for child in el.children:
            walk(child, depth + 1)

    walk(driver.root, 0)
    return "\n".join(lines), refs


def _attrs(driver: DevDriver, el: El) -> str:
    out = "".join(f" {key.removeprefix('aria-')}={value}" for key, value in sorted(el.attrs.items()))
    if el.role == "textbox":
        value = driver.values.get(el.key or el.name)
        if value:
            out += f' value="{value}"'
    return out


# ---- what a wait reads of an element ----


def _same_element(root: El, el: El) -> El | None:
    """`el` in the page as it is now: a click renders the tree again, so the element is found by
    its key (or role and name), as a real page keeps the same node."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node is el or (node.role == el.role and (node.key or node.name) == (el.key or el.name)):
            return node
        stack.extend(node.children)
    return None


def _ref_state(state: str, before: dict[str, str], now: El | None) -> tuple[bool, str]:
    if now is None:
        return state == "hidden", "gone from the page"
    attrs = dict(now.attrs)
    if state == "changed":
        changes = [f"{k}: {before.get(k)} -> {v}" for k, v in attrs.items() if before.get(k) != v]
        return bool(changes), "; ".join(changes) or "no change in its own state"
    flags = {"visible": True, "hidden": False,
             "enabled": attrs.get("disabled") != "true", "disabled": attrs.get("disabled") == "true",
             "checked": attrs.get("aria-checked") == "true", "unchecked": attrs.get("aria-checked") != "true",
             "pressed": attrs.get("aria-pressed") == "true", "unpressed": attrs.get("aria-pressed") != "true"}
    if state not in flags:
        return False, f"the dev site has no {state} state"
    seen = ", ".join(f"{k.removeprefix('aria-')}={v}" for k, v in sorted(attrs.items())) or "visible"
    return flags[state], seen


# ---- the Interaction the MCP tools use ----


class DevInteraction:
    """The Interaction protocol (agentos.node.interfaces) over the lane's fake tab. Refs belong to
    the last snapshot; a click re-renders the tab, so the refs before it no longer resolve."""

    async def navigate(self, page: FakePage, url: str, *, follow_link: bool = False,
                       on_click: Callable[[], None] | None = None) -> NavigateResult:
        return await page.driver.navigate(url)

    async def snapshot(self, page: FakePage) -> SnapshotResult:
        driver = page.driver
        text, refs = outline(driver)
        page.refs, page.refs_root = refs, driver.root
        signal = signal_for(driver.url)
        return SnapshotResult(url=driver.url, title=driver.root.name, text=text or "(empty page)",
                              ref_count=len(refs), signal=signal,
                              signal_detail="challenge page" if signal is Signal.CAPTCHA else None)

    async def act(self, page: FakePage, ref: str, action: ActionKind, text: str | None = None) -> ActionResult:
        driver = page.driver
        element = page.refs.get(ref) if page.refs_root is driver.root else None
        if element is None:
            raise RefNotFound(f"ref {ref} is gone; take a new snapshot")
        before = driver.version
        try:
            if action is ActionKind.CLICK:
                await driver.click(element)
            elif action is ActionKind.TYPE:
                await driver.type(element, text or "")
            elif action is ActionKind.FILL:
                await driver.fill(element, text or "")
            elif action is ActionKind.SELECT:
                await driver.select(element, text or "")
            elif action is ActionKind.PRESS:
                await driver.press(text or "", element)
            elif action is ActionKind.SCROLL:
                await driver.scroll_to(element)
        except ElementNotFound as exc:
            raise InvalidRequest(str(exc)) from None
        # changed: the tab responded (its own operations count every change); hover changes nothing
        return ActionResult(ok=True, action=action, ref=ref, url=driver.url, signal=signal_for(driver.url),
                            changed=driver.version != before)

    async def screenshot(self, page: FakePage) -> bytes:
        return frame_of_page(page)[1].png()

    async def signal(self, page: FakePage) -> Signal:
        return signal_for(page.driver.url)

    async def upload(self, page: FakePage, ref: str, paths: Sequence[Path | str], *,
                     mimes: Sequence[str] | None = None, root: Path | None = None, **_: Any) -> UploadResult:
        """Answer the chooser the composer's "Select from computer" opens with one inbox file. The
        field holds the file's name (never a path on the node), and the composer shows it."""
        driver = page.driver
        files = [Path(p) for p in paths]
        if not files:
            raise InvalidRequest("no files to upload")
        base = root.resolve() if root is not None else None
        for f in files:
            if f.is_symlink() or not f.is_file():
                raise InvalidRequest(f"{f.name} is not a regular file")
            if base is not None and not f.resolve().is_relative_to(base):
                raise InvalidRequest(f"{f.name} is outside the lane inbox; only inbox files can be uploaded")
        element = self._element(page, ref)
        if element.key != FILE_KEY:
            raise InvalidRequest(f"ref {ref} opened no file chooser: pass the button that opens one "
                                 '(on this site, "Select from computer")')
        if len(files) > 1:
            raise InvalidRequest("this field takes one file only")
        shown, kinds = files[0].name, list(mimes or [])
        mime = kinds[0] if kinds else ""
        driver.values.pop("media_id", None)  # a new choice replaces the photo, also when it does not arrive
        driver.values.pop(FILE_KEY, None)
        if driver.net is not None:  # the site reads the photo now, as a real composer does: the guard sees it
            from agentos.dev.net import upload_request

            request, media_id = upload_request(files[0].read_bytes(), mime)
            if not await driver.net.send(request):
                return UploadResult(ok=False, ref=ref, url=driver.url, via="chooser", files=[],
                                    signal=signal_for(driver.url), detail="the lane's guard stopped the upload")
            driver.values["media_id"] = media_id
        await driver.upload(element, shown)
        held = [{"name": shown, "size": files[0].stat().st_size, "type": mime}]
        return UploadResult(ok=True, ref=ref, url=driver.url, via="chooser", files=held, seen_by_page=held,
                            signal=signal_for(driver.url))

    async def wait(self, page: FakePage, spec: WaitFor, *,
                   should_stop: Callable[[], str | None] | None = None) -> WaitResult:
        """browser_wait over the fake tab: the same conditions and result as on a real lane. The fake
        network sends inside a click, so it is quiet whenever a wait looks."""
        driver = page.driver
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + min(max(float(spec.timeout_s), 0.0), WAIT_MAX_S)
        ref = before = None
        if spec.ref is not None:
            ref = spec.ref
            before = dict(self._element(page, ref).attrs)
        watch = _Watch(spec, ref, before)
        target = self._element(page, ref) if ref is not None else None
        while True:
            reason = should_stop() if should_stop is not None else None
            if reason:
                stopped = reason
                break
            self._check(driver, watch, target)
            if watch.met:
                stopped = "met"
                break
            if signal_for(driver.url) is not Signal.OK:
                stopped = "signal"
                break
            if loop.time() >= deadline:
                stopped = "timeout"
                break
            await asyncio.sleep(min(WAIT_POLL_S, max(0.0, deadline - loop.time())))
        waited = round(loop.time() - started, 1)
        signal = signal_for(driver.url)
        detail = {"met": f"met after {waited:g} s", "timeout": f"not met within {spec.timeout_s:g} s: {watch.unmet()}",
                  "signal": f"stopped after {waited:g} s: the page signal is {signal.value}"}.get(
            stopped, f"stopped after {waited:g} s: a human took control of the lane")
        return WaitResult(met=stopped == "met", stopped=stopped, waited_s=waited, url=driver.url,
                          conditions=list(watch.conditions.values()), reread=spec.reads_tree, signal=signal,
                          detail=detail, signal_detail="challenge page" if signal is Signal.CAPTCHA else None)

    def _element(self, page: FakePage, ref: str) -> El:
        element = page.refs.get(ref) if page.refs_root is page.driver.root else None
        if element is None:
            raise RefNotFound(f"ref {ref} is gone; take a new snapshot")
        return element

    @staticmethod
    def _check(driver: DevDriver, watch: _Watch, target: El | None) -> None:
        spec = watch.spec
        if spec.reads_tree:
            texts = outline_texts(outline(driver)[0])
            if spec.for_text:
                hit = next(((want, line) for want in spec.for_text
                            if (line := find_text(texts, want, exact=spec.exact)) is not None), None)
                if hit is not None:
                    watch.set("for_text", True, hit[1], matched=hit[0])
                else:
                    watch.set("for_text", False, None)
            if spec.gone_text:
                left = [line for want in spec.gone_text if (line := find_text(texts, want, exact=spec.exact))]
                watch.set("gone_text", not left, left[0] if left else None)
        if target is not None:
            now = _same_element(driver.root, target)
            met, seen = _ref_state(spec.ref_state or "", watch.before, now)
            watch.set("for_ref_state", met, seen)
        if spec.network_quiet_ms is not None:
            watch.set("network_quiet_ms", True, "no request open")
        if spec.url_matches is not None:
            watch.set("url_matches", url_matches(driver.url, spec.url_matches), driver.url)
