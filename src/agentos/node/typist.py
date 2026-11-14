"""The vault's Typist on Playwright pages (Camoufox lanes).

Implements `agentos.vault.typist.Typist`, so `login` and `vault_fill` can type a stored
secret into a login form without the model seeing it and without a hostile page being
able to redirect the keystrokes.

- Reading: one script per frame walks the document (open shadow roots included) and
  returns structural metadata of every input, textarea and select, plus the iframes in
  between, so fields come back in document order across frames. Camoufox runs our
  scripts in an isolated world: nothing is injected or modified, and page overrides of
  DOM prototypes (a lying `activeElement` getter, say) are invisible to these reads.
- Locators: `pw1:<frame path>:<index>:<signature>`. The frame path is the index of each
  iframe in its parent's walk, from the top document down ("top" for the top document);
  the index is the control's position in its own document's walk; the signature is a
  hash of tag, type, name, id, autocomplete and the frame origin chain. A locator
  resolves only if the element at that place still has that signature.
- Frames are reached with `evaluate_handle` + `content_frame()` only, in the isolated
  world, never by trusting anything the page reports.
- Typing: before every keystroke (clearing included, and for a capital both before Shift
  goes down and again right before the letter's key) one probe per frame on the way to
  the field checks, concurrently: the top-level URL origin is unchanged; each ancestor
  document still has the next iframe focused and its origin unchanged; the field is
  still connected, has the same shape, is the deep active element of a document that
  has focus, and its document origin is unchanged. Any failure stops typing at once with
  TypistAborted; the next character is never sent.
- The secret is never logged, traced, screenshotted or returned. Errors on the typing
  path carry fixed messages and no chained Playwright error, and the frames that held
  the text clear it before an exception leaves them.

Residual race: a page can move focus between our probe and the key's arrival (a few
milliseconds: the probe runs right before each key goes down, after any wait, so Shift's
lead before a capital is not part of it), or inside the keydown handler of the key being
typed. Either way at most
one character lands elsewhere, and the next probe stops the typing.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import random
import re
import time
import unicodedata
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from agentos.core.errors import AgentosError, EngineError, InvalidRequest, RefNotFound, ServiceUnavailable
from agentos.core.models import Signal
from agentos.node import egress, human
from agentos.node.human import quietly
from agentos.node.interaction import PlaywrightInteraction, normalize_ref
from agentos.vault.errors import TypistAborted
from agentos.vault.typist import FieldInfo

log = logging.getLogger(__name__)

Page = Any           # playwright.async_api.Page
Frame = Any          # playwright.async_api.Frame
ElementHandle = Any  # playwright.async_api.ElementHandle

LOCATOR_VERSION = "pw1"
MAX_FRAME_DEPTH = 8
SIGNATURE_HEX = 32
RESOLVE_TIMEOUT_MS = 2_000
READ_RETRY_TIMEOUT_MS = 5_000

SETTLE_START_S = 1.5      # a submit usually starts a request or a navigation within this
SETTLE_QUIET_S = 0.5      # no request started or finished for this long
SETTLE_LONG_LIVED_S = 5.0  # requests open longer than this (long polls, streams) do not count
SETTLE_POLL_S = 0.05

# Fixed reasons for TypistAborted. Never built from page content or the text.
NAVIGATED = "the page navigated"
FRAME_CHANGED = "a frame on the way to the field navigated or left the page"
FIELD_GONE = "the field left the page"
FIELD_CHANGED = "the field changed (type, name, id or autocomplete)"
FOCUS_MOVED = "focus left the field"
UNREADABLE = "the page could not be read"
KEY_FAILED = "a key could not be sent"
NOT_CLEARED = "the field could not be cleared"
STALE = "the field is no longer on the page"

# Playwright debug channels that would write typed text to a log.
_LOGGING_CHANNELS = ("pw:protocol", "pw:channel", "pw:api")

# ---- page scripts (read only; run in Camoufox's isolated world) ----------------------

# Shared helpers. walk() visits elements in document order, an open shadow root right after
# its host, and collects form controls, iframes and forms.
_PRELUDE = """
  const HTML_NS = 'http://www.w3.org/1999/xhtml';
  const walk = (doc) => {
    const controls = [], frames = [], forms = [], order = [];
    const stack = [doc];
    while (stack.length) {
      const node = stack.pop();
      if (node.nodeType === 1 && node.namespaceURI === HTML_NS) {
        const tag = node.localName;
        if (tag === 'input' || tag === 'textarea' || tag === 'select') { order.push(0, controls.length); controls.push(node); }
        else if (tag === 'iframe' || tag === 'frame') { order.push(1, frames.length); frames.push(node); }
        else if (tag === 'form') forms.push(node);
      }
      const kids = node.children;
      if (kids) for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
      if (node.nodeType === 1 && node.shadowRoot) stack.push(node.shadowRoot);
    }
    return {controls, frames, forms, order};
  };
  const deepActive = (doc) => {
    let a = doc.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a;
  };
  const originOf = (doc) => {
    try {
      const view = doc.defaultView;
      if (!view || String(view.origin) === 'null') return 'null';
      return String(view.location.origin);
    } catch (e) { return 'null'; }
  };
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    if (!(r.width >= 2 && r.height >= 2)) return false;
    const view = el.ownerDocument.defaultView;
    if (view && (r.right + view.scrollX <= 0 || r.bottom + view.scrollY <= 0)) return false;
    if (typeof el.checkVisibility === 'function') {
      return el.checkVisibility({opacityProperty: true, visibilityProperty: true, checkOpacity: true,
                                 checkVisibilityCSS: true});
    }
    const s = view.getComputedStyle(el);
    return s.visibility === 'visible' && s.display !== 'none' && Number(s.opacity) > 0;
  };
  const shape = (el) => {
    const tag = el.localName;
    const type = tag === 'input' ? String(el.type || 'text').toLowerCase()
      : (tag === 'textarea' ? 'textarea' : String(el.type || 'select-one').toLowerCase());
    return {tag, type, name: el.getAttribute('name') || '', id: el.getAttribute('id') || '',
            autocomplete: el.getAttribute('autocomplete') || ''};
  };
  const describe = (el, w, index) => {
    const s = shape(el);
    const textual = s.tag === 'input' || s.tag === 'textarea';
    const ml = textual ? el.maxLength : -1;
    const owner = el.form || null;
    s.k = 'f';
    s.index = index;
    s.inputmode = el.getAttribute('inputmode') || '';
    s.maxlength = (typeof ml === 'number' && ml >= 0) ? ml : null;
    s.form = owner ? w.forms.indexOf(owner) : null;
    s.visible = shown(el);
    s.disabled = el.matches(':disabled');
    s.readonly = textual ? !!el.readOnly : false;
    return s;
  };
"""

# Every control and iframe of this frame's document, in document order.
_SCAN_JS = """() => {""" + _PRELUDE + """
  const w = walk(document);
  const items = [];
  for (let p = 0; p < w.order.length; p += 2) {
    const i = w.order[p + 1];
    items.push(w.order[p] === 0 ? describe(w.controls[i], w, i) : {k: 'i', index: i, visible: shown(w.frames[i])});
  }
  return {origin: originOf(document), items};
}"""

_CONTROL_AT_JS = """(i) => {""" + _PRELUDE + """ return walk(document).controls[i] || null; }"""
_FRAME_AT_JS = """(i) => {""" + _PRELUDE + """ return walk(document).frames[i] || null; }"""
_CONTROL_INDEX_JS = """(el) => {""" + _PRELUDE + """ return walk(el.ownerDocument).controls.indexOf(el); }"""
_FRAME_INDEX_JS = """(el) => {""" + _PRELUDE + """ return walk(el.ownerDocument).frames.indexOf(el); }"""
_SAME_JS = "(a, b) => a === b"

# Full metadata of one control, with its current index and its document's origin.
_DESCRIBE_JS = """(el) => {""" + _PRELUDE + """
  const doc = el.ownerDocument, w = walk(doc);
  const s = describe(el, w, w.controls.indexOf(el));
  s.connected = el.isConnected;
  s.origin = originOf(doc);
  return s;
}"""

# Per keystroke, on the iframe element that leads to the next frame down.
_CONTAINER_JS = """(el) => {""" + _PRELUDE + """
  const doc = el.ownerDocument;
  return {connected: el.isConnected, origin: originOf(doc), focused: deepActive(doc) === el && doc.hasFocus(),
          visible: shown(el)};
}"""

# Per keystroke, on the field itself. Only the value's length is read, never the value.
_PROBE_JS = """(el) => {""" + _PRELUDE + """
  const doc = el.ownerDocument, s = shape(el);
  s.connected = el.isConnected;
  s.origin = originOf(doc);
  s.focused = deepActive(doc) === el && doc.hasFocus();
  s.length = typeof el.value === 'string' ? el.value.length : 0;
  return s;
}"""


# ---- locators (pure) ---------------------------------------------------------------

_INDEX = r"(?:0|[1-9]\d{0,5})"
_LOCATOR_RE = re.compile(
    rf"^{LOCATOR_VERSION}:(top|{_INDEX}(?:\.{_INDEX}){{0,{MAX_FRAME_DEPTH - 1}}}):({_INDEX}):([0-9a-f]{{{SIGNATURE_HEX}}})$")

Shape = tuple[str, str, str, str, str]  # tag, type, name, id, autocomplete


def shape_of(desc: Mapping[str, Any]) -> Shape:
    return (str(desc.get("tag") or ""), str(desc.get("type") or ""), str(desc.get("name") or ""),
            str(desc.get("id") or ""), str(desc.get("autocomplete") or ""))


def field_signature(shape: Shape, frame_origins: tuple[str, ...] | list[str]) -> str:
    """Hash of the field's structure and where it lives. Equal only for the same tag, type,
    name, id and autocomplete in a frame chain with the same origins."""
    blob = json.dumps([list(shape), list(frame_origins)], separators=(",", ":"))
    return hashlib.sha256(blob.encode("ascii")).hexdigest()[:SIGNATURE_HEX]


def path_text(frames: tuple[int, ...]) -> str:
    return ".".join(str(i) for i in frames) or "top"


@dataclass(frozen=True, slots=True)
class Locator:
    frames: tuple[int, ...]  # iframe index in each parent's walk, top document first
    index: int               # control index in its own document's walk
    sig: str

    def encode(self) -> str:
        return f"{LOCATOR_VERSION}:{path_text(self.frames)}:{self.index}:{self.sig}"

    @classmethod
    def parse(cls, raw: str) -> Locator:
        m = _LOCATOR_RE.match(raw) if isinstance(raw, str) else None
        if m is None:
            raise ValueError("not a typist locator")
        path = () if m[1] == "top" else tuple(int(p) for p in m[1].split("."))
        return cls(frames=path, index=int(m[2]), sig=m[3])


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def field_info(desc: Mapping[str, Any], frames: tuple[int, ...], frame_origins: tuple[str, ...], *,
               frames_visible: bool = True, ref: str | None = None) -> FieldInfo:
    """A FieldInfo from one control's metadata (as the page scripts return it)."""
    shape = shape_of(desc)
    origins = tuple(str(o) for o in frame_origins)
    locator = Locator(frames=tuple(frames), index=int(desc["index"]), sig=field_signature(shape, origins))
    form = desc.get("form")
    return FieldInfo(
        locator=locator.encode(),
        frame_origins=origins,
        tag=shape[0],
        type=shape[1],
        autocomplete=shape[4],
        name=shape[2],
        id=shape[3],
        inputmode=str(desc.get("inputmode") or ""),
        maxlength=_int_or_none(desc.get("maxlength")),
        form=None if form is None else f"{path_text(tuple(frames))}/form{int(form)}",
        visible=bool(desc.get("visible")) and frames_visible,
        disabled=bool(desc.get("disabled")),
        readonly=bool(desc.get("readonly")),
        ref=ref,
    )


def url_origin(url: str) -> str:
    """The serialized origin of an http(s) URL, "null" for anything else."""
    try:
        parts = urlsplit(url or "")
        port = parts.port
    except ValueError:
        return "null"
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in ("http", "https") or not host:
        return "null"
    if ":" in host:
        host = f"[{host}]"
    default = 80 if scheme == "http" else 443
    return f"{scheme}://{host}" + (f":{port}" if port is not None and port != default else "")


def has_control_chars(text: str) -> bool:
    """Tabs, line breaks and other control characters would move focus or submit a form."""
    return any(unicodedata.category(ch) == "Cc" for ch in text)


def protocol_logging(env: Mapping[str, str]) -> bool:
    """True if DEBUG enables a Playwright channel that logs what we send (typed text included)."""
    for token in re.split(r"[\s,]+", env.get("DEBUG", "")):
        if token and not token.startswith("-") and any(fnmatch.fnmatchcase(c, token) for c in _LOGGING_CHANNELS):
            return True
    return False


# ---- network quiet, for settle() ---------------------------------------------------

class _Network:
    """Requests in flight on one page and a counter of activity (requests, main frame
    navigations), so settle() can tell whether a submit started anything."""

    def __init__(self) -> None:
        self.inflight: dict[Any, float] = {}
        self.seq = 0
        self.changed = time.monotonic()

    def _bump(self) -> None:
        self.seq += 1
        self.changed = time.monotonic()

    def started(self, request: Any) -> None:
        self.inflight[request] = time.monotonic()
        self._bump()

    def ended(self, request: Any) -> None:
        if self.inflight.pop(request, None) is not None:
            self._bump()

    def navigated(self, frame: Any) -> None:
        if getattr(frame, "parent_frame", None) is None:
            self._bump()

    def quiet(self) -> bool:
        now = time.monotonic()
        if now - self.changed < SETTLE_QUIET_S:
            return False
        return all(now - since >= SETTLE_LONG_LIVED_S for since in self.inflight.values())


_NETWORKS: weakref.WeakKeyDictionary[Any, _Network] = weakref.WeakKeyDictionary()


def _network(page: Page) -> _Network:
    net = _NETWORKS.get(page)
    if net is None:
        net = _Network()
        page.on("request", net.started)
        page.on("requestfinished", net.ended)
        page.on("requestfailed", net.ended)
        page.on("framenavigated", net.navigated)
        _NETWORKS[page] = net
    return net


# ---- resolved fields ---------------------------------------------------------------

@dataclass
class _Target:
    """A field resolved to live handles: the frames from the top document down, the iframe
    element leading to each next frame, and the field's own handle."""

    frames: list[Frame]
    containers: list[ElementHandle]   # containers[k] lives in frames[k] and holds frames[k + 1]
    handle: ElementHandle
    path: tuple[int, ...]
    desc: dict[str, Any] = field(default_factory=dict)
    origins: tuple[str, ...] = ()
    frames_visible: bool = True

    @property
    def sig(self) -> str:
        return field_signature(shape_of(self.desc), self.origins)

    def info(self, ref: str | None = None) -> FieldInfo:
        return field_info(self.desc, self.path, self.origins, frames_visible=self.frames_visible, ref=ref)

    async def dispose(self) -> None:
        for h in (*self.containers, self.handle):
            await quietly(h.dispose())


async def _element_at(frame: Frame, script: str, index: int) -> ElementHandle | None:
    handle = await frame.evaluate_handle(script, index)
    element = handle.as_element()
    if element is None:
        await quietly(handle.dispose())
    return element


async def _child_frame(frame: Frame, index: int) -> Frame | None:
    element = await _element_at(frame, _FRAME_AT_JS, index)
    if element is None:
        return None
    try:
        return await element.content_frame()
    finally:
        await quietly(element.dispose())


async def _sent(awaitable: Any) -> bool:
    """Await a key press. A failure becomes False: Playwright's message may quote the key. A
    probe's TypistAborted (type_text's before_key) is raised again with its fixed reason from here,
    so its traceback does not reach into type_text's frames, which hold a character of the text."""
    try:
        await awaitable
    except TypistAborted as e:
        reason = str(e)
    except Exception:  # noqa: BLE001 - reported with a fixed reason by the caller
        return False
    else:
        return True
    raise TypistAborted(reason)


async def _pause(page: Page, lo: int, hi: int) -> None:
    await page.wait_for_timeout(random.randint(lo, hi))


class _Guard:
    """The per-keystroke check for one resolved field (see the module docs)."""

    def __init__(self, page: Page, target: _Target, frame_origins: tuple[str, ...] | list[str]):
        self.page = page
        self.target = target
        self.origins = tuple(frame_origins)
        self.top = url_origin(page.url)
        self.shape = shape_of(target.desc)

    async def problem(self) -> tuple[str | None, dict[str, Any]]:
        t = self.target
        if not self.origins or len(self.origins) != len(t.containers) + 1:
            return FRAME_CHANGED, {}
        if url_origin(self.page.url) != self.top or self.top != self.origins[0]:
            return NAVIGATED, {}
        probes = [h.evaluate(_CONTAINER_JS) for h in t.containers]
        probes.append(t.handle.evaluate(_PROBE_JS))
        results = await asyncio.gather(*probes, return_exceptions=True)
        if any(isinstance(r, BaseException) or not isinstance(r, dict) for r in results):
            return UNREADABLE, {}
        *outer, leaf = results
        for k, c in enumerate(outer):
            if not c.get("connected") or c.get("origin") != self.origins[k]:
                return (NAVIGATED if k == 0 else FRAME_CHANGED), {}
            if not c.get("focused"):
                return FOCUS_MOVED, {}
        if not leaf.get("connected"):
            return FIELD_GONE, {}
        if leaf.get("origin") != self.origins[-1]:
            return (NAVIGATED if not outer else FRAME_CHANGED), {}
        if shape_of(leaf) != self.shape:
            return FIELD_CHANGED, {}
        if not leaf.get("focused"):
            return FOCUS_MOVED, {}
        return None, leaf

    async def require(self) -> dict[str, Any]:
        """The field's probe, or TypistAborted if any check fails."""
        reason, leaf = await self.problem()
        if reason is not None:
            log.info("typist: stopped: %s", reason)
            raise TypistAborted(f"typing stopped: {reason}")
        return leaf


# ---- the typist --------------------------------------------------------------------

_DEFAULT_INTERACTION: PlaywrightInteraction | None = None


def _default_interaction() -> PlaywrightInteraction:
    global _DEFAULT_INTERACTION
    if _DEFAULT_INTERACTION is None:
        _DEFAULT_INTERACTION = PlaywrightInteraction()
    return _DEFAULT_INTERACTION


def _first_line(e: BaseException) -> str:
    text = str(e).strip() or type(e).__name__
    return text.splitlines()[0][:300]


class PlaywrightTypist:
    """The Typist protocol for one Playwright page. Cheap to create; one per tool call."""

    def __init__(self, page: Page, *, interaction: Any = None):
        self.page = page
        self.interaction = interaction
        self._mark = _network(page).seq

    def __repr__(self) -> str:
        return "PlaywrightTypist()"

    # ---- reading -----------------------------------------------------------------

    async def page_url(self) -> str:
        return self.page.url

    async def fields(self) -> list[FieldInfo]:
        for attempt in range(2):
            try:
                return await self._scan(self.page.main_frame, (), (), True)
            except Exception as e:  # the main document navigated or was replaced mid-read
                if attempt:
                    raise EngineError(f"could not read the page's form fields: {_first_line(e)}") from e
                await quietly(self.page.wait_for_load_state("domcontentloaded", timeout=READ_RETRY_TIMEOUT_MS))
        raise AssertionError("unreachable")

    async def _scan(self, frame: Frame, path: tuple[int, ...], chain: tuple[str, ...],
                    frames_visible: bool) -> list[FieldInfo]:
        data = await frame.evaluate(_SCAN_JS)
        origins = (*chain, str(data["origin"]))
        out: list[FieldInfo] = []
        for item in data["items"]:
            if item.get("k") == "f":
                out.append(field_info(item, path, origins, frames_visible=frames_visible))
                continue
            if len(path) >= MAX_FRAME_DEPTH:
                continue
            try:
                child = await _child_frame(frame, int(item["index"]))
                if child is not None:
                    out.extend(await self._scan(child, (*path, int(item["index"])), origins,
                                                frames_visible and bool(item.get("visible"))))
            except Exception as e:  # noqa: BLE001 - a frame that navigates or detaches has nothing to fill
                log.debug("typist: skipped a frame: %s", type(e).__name__)
        return out

    async def field_for_ref(self, ref: str) -> FieldInfo:
        ref = normalize_ref(ref)
        stale = (f"ref {ref} is not on the page (stale after a navigation or a newer snapshot). "
                 "Take a new snapshot.")
        handle = await self._ref_handle(ref)
        if handle is None:
            raise RefNotFound(stale)
        target: _Target | None = None
        try:
            index = await quietly(handle.evaluate(_CONTROL_INDEX_JS), None)
            if index is None:
                raise RefNotFound(stale)
            if not isinstance(index, int) or index < 0:
                raise InvalidRequest(f"ref {ref} is not a form field (an input, textarea or select)")
            frame = await quietly(handle.owner_frame(), None)
            path = await self._path_of(frame) if frame is not None else None
            if path is not None:
                target = await self._locate(path, index)
            if target is None or not await quietly(target.handle.evaluate(_SAME_JS, handle), False):
                raise RefNotFound(stale)
            return target.info(ref=ref)
        finally:
            await quietly(handle.dispose())
            if target is not None:
                await target.dispose()

    async def _ref_handle(self, ref: str) -> ElementHandle | None:
        locator = self.page.locator(f"aria-ref={ref}")
        try:
            if await locator.count() == 0:
                return None
            return await locator.element_handle(timeout=RESOLVE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - a stale ref
            return None

    async def _path_of(self, frame: Frame) -> tuple[int, ...] | None:
        """The frame path of `frame`, checked top-down with content_frame()."""
        path: list[int] = []
        current = frame
        try:
            while current.parent_frame is not None:
                if len(path) >= MAX_FRAME_DEPTH:
                    return None
                parent = current.parent_frame
                element = await current.frame_element()
                try:
                    index = await element.evaluate(_FRAME_INDEX_JS)
                finally:
                    await quietly(element.dispose())
                if not isinstance(index, int) or index < 0 or await _child_frame(parent, index) is not current:
                    return None
                path.append(index)
                current = parent
        except Exception:  # noqa: BLE001 - a frame detached on the way up
            return None
        return tuple(reversed(path))

    # ---- resolving locators ------------------------------------------------------

    async def _locate(self, path: tuple[int, ...], index: int) -> _Target | None:
        """Live handles for the control at `index` in the frame at `path`, with fresh metadata."""
        frames = [self.page.main_frame]
        handles: list[ElementHandle] = []
        found = False
        try:
            for j in path:
                element = await _element_at(frames[-1], _FRAME_AT_JS, j)
                if element is None:
                    return None
                handles.append(element)
                child = await element.content_frame()
                if child is None:
                    return None
                frames.append(child)
            handle = await _element_at(frames[-1], _CONTROL_AT_JS, index)
            if handle is None:
                return None
            handles.append(handle)
            target = _Target(frames=frames, containers=handles[:-1], handle=handle, path=tuple(path))
            if not await self._refresh(target) or target.desc.get("index") != index:
                return None
            found = True
            return target
        except Exception:  # noqa: BLE001 - frames navigating or detaching: not resolvable
            return None
        finally:
            if not found:
                for h in handles:
                    await quietly(h.dispose())

    async def _resolve(self, raw: str) -> _Target | None:
        """The live field for a locator, or None if it is malformed, gone or changed."""
        try:
            loc = Locator.parse(raw)
        except ValueError:
            return None
        target = await self._locate(loc.frames, loc.index)
        if target is not None and target.sig != loc.sig:
            await target.dispose()
            return None
        return target

    async def _refresh(self, target: _Target) -> bool:
        """Re-read the field's metadata and its frames' origins and visibility."""
        probes = [h.evaluate(_CONTAINER_JS) for h in target.containers]
        probes.append(target.handle.evaluate(_DESCRIBE_JS))
        results = await asyncio.gather(*probes, return_exceptions=True)
        if any(isinstance(r, BaseException) or not isinstance(r, dict) for r in results):
            return False
        *outer, desc = results
        if not desc.get("connected") or not isinstance(desc.get("index"), int) or desc["index"] < 0:
            return False
        if any(not c.get("connected") for c in outer):
            return False
        target.desc = desc
        target.origins = (*(str(c.get("origin")) for c in outer), str(desc.get("origin")))
        target.frames_visible = all(bool(c.get("visible")) for c in outer)
        return True

    async def box(self, field: FieldInfo) -> dict[str, float] | None:
        """Where the field is in the viewport, in CSS pixels ({x, y, width, height}), or None when it
        is gone or not laid out. For redacting a screenshot (agentos.node.login_assist)."""
        target = await self._resolve(field.locator)
        if target is None:
            return None
        try:
            return await quietly(target.handle.bounding_box(), None)
        finally:
            await target.dispose()

    # ---- acting ------------------------------------------------------------------

    async def focus(self, field: FieldInfo) -> FieldInfo:
        target = await self._resolve(field.locator)
        if target is None:
            raise RefNotFound(f"{STALE} (it changed or the page navigated); read the page again")
        try:
            try:
                await human.click(self.page, target.handle)
            except AgentosError:
                raise
            except Exception as e:
                raise EngineError(f"could not click the field: {_first_line(e)}") from e
            if not await self._refresh(target):
                raise RefNotFound("the field left the page when it was clicked; read the page again")
            return target.info(ref=field.ref)
        finally:
            await target.dispose()

    async def type_secret(self, field: FieldInfo, text: str) -> None:
        target: _Target | None = None
        try:
            if not isinstance(text, str):
                raise InvalidRequest("the typist types text")
            if has_control_chars(text):
                raise InvalidRequest("the secret contains a control character (such as a tab or a line break), "
                                     "so it cannot be typed into a field")
            if protocol_logging(os.environ):
                raise ServiceUnavailable("Playwright protocol logging is on (DEBUG=pw:...), so typing a secret "
                                         "would write it to a log; unset DEBUG and restart the node")
            target = await self._resolve(field.locator)
            if target is None:
                raise TypistAborted(f"typing stopped: {STALE}")
            guard = _Guard(self.page, target, field.frame_origins)
            await self._clear(guard)
            for i in range(len(text)):
                # the probe runs inside type_text, right before each key goes down (Shift, then the
                # letter after Shift's lead); no typos: a slip would put a wrong letter of the
                # secret into the field, however briefly
                if not await _sent(human.type_text(self.page, text[i], typos=False, before_key=guard.require)):
                    raise TypistAborted(f"typing stopped: {KEY_FAILED}")
        finally:
            text = ""  # a traceback through this frame must not show the secret
            if target is not None:
                await target.dispose()

    async def _clear(self, guard: _Guard) -> None:
        """Select everything and delete it with real keys, checking before each key."""
        await human.still(self.page)
        leaf = await guard.require()
        if not leaf.get("length"):
            return
        for key in ("ControlOrMeta+a", "Backspace"):
            await _pause(self.page, 120, 380)
            await guard.require()
            if not await _sent(self.page.keyboard.press(key, delay=random.randint(50, 130))):
                raise TypistAborted(f"typing stopped: {KEY_FAILED}")
            await _pause(self.page, 80, 220)
        leaf = await guard.require()
        if leaf.get("length"):
            raise TypistAborted(f"typing stopped: {NOT_CLEARED}")

    async def open_login(self, domain: str) -> None:
        guard = egress.guard_for(self.page)
        if guard is not None:
            guard.open_login(domain)

    async def close_login(self, domain: str) -> None:
        guard = egress.guard_for(self.page)
        if guard is not None:
            guard.close_login(domain)

    async def press(self, field: FieldInfo, key: str) -> None:
        target = await self._resolve(field.locator)
        if target is None:
            raise TypistAborted(f"typing stopped: {STALE}")
        try:
            guard = _Guard(self.page, target, field.frame_origins)
            await _pause(self.page, 120, 380)
            await guard.require()
            self._mark = _network(self.page).seq
            if not await _sent(self.page.keyboard.press(key, delay=random.randint(50, 130))):
                raise TypistAborted(f"typing stopped: {KEY_FAILED}")
        finally:
            await target.dispose()

    async def settle(self, timeout: float = 10.0) -> None:
        """Wait (bounded) for what the last press started: a navigation or requests, then
        network quiet and the load event. Never raises on timeout."""
        net = _network(self.page)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        start_by = min(deadline, loop.time() + SETTLE_START_S)
        while net.seq == self._mark and loop.time() < start_by:
            await asyncio.sleep(SETTLE_POLL_S)
        while loop.time() < deadline and not net.quiet():
            await asyncio.sleep(SETTLE_POLL_S)
        left = deadline - loop.time()
        if left > 0:
            await quietly(self.page.wait_for_load_state("load", timeout=left * 1000))
        self._mark = net.seq

    async def signal(self) -> Signal:
        interaction = self.interaction if self.interaction is not None else _default_interaction()
        return await interaction.signal(self.page)


def _interaction_of(runtime: Any) -> Any:
    if runtime is None:
        return None
    get = getattr(runtime, "get", None)
    if callable(get):
        return get("interaction")
    return getattr(runtime, "interaction", None)


def typist_factory(runtime: Any = None) -> Callable[[Page], PlaywrightTypist]:
    """`page -> Typist` for the vault. Register it as the runtime's "typist" service:
    `runtime.register("typist", typist_factory(runtime))`. The interaction (for signal())
    is looked up when a typist is made, so it may be registered later."""

    def make(page: Page) -> PlaywrightTypist:
        return PlaywrightTypist(page, interaction=_interaction_of(runtime))

    return make


__all__ = ["Locator", "PlaywrightTypist", "field_info", "field_signature", "typist_factory", "url_origin"]
