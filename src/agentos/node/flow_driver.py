"""FlowDriver on a real lane page (Camoufox, Playwright async), plus the recorder's locator.

- Locators resolve exactly like agentos.runner.fakes.resolve_all / resolve_one: scope (the
  page, or the one element `within` resolves to), candidates by ARIA role and accessible name
  (Playwright's own role engine, so names match what snapshots show), the CSS fallback only
  when no role candidate exists, the `own` filter that drops candidates inside a nested element
  with the anchor's role, then `nth` or an ambiguity error. Never "the first one".
- Elements are compared by their path in the composed DOM tree (child indexes, shadow roots
  included), computed by read-only scripts. Nothing marks the page, so the site cannot see
  us. A readback can never be fooled by our own markers.
- Every input is human input from human.py: wheel scroll, a pause, a random point inside the
  target, a real press, typing at human rhythm. Uploads click the visible control and answer
  the file chooser; a hidden file input with no visible control gets the file set directly.
- `attr` matches a machine attribute: the browser reports each candidate's value with its
  path and the same Python matcher as the fake (AttrMatch.matches) decides, so equals, prefix
  and regex mean exactly the same on both sides. An attr-only locator queries `[name]` by CSS.
  Role and CSS queries pierce open shadow roots, and paths step through them.
- extract_items reads records under the same scope rules (each item is the anchor of its field
  locators) and account() reads the site's identity cookie through agentos.node.sites, without a
  request. Neither writes anything to the page.
- watch_json listens to the responses the page's own code causes. Listening is all it does: a
  `response` event on the page, with no route, so nothing is held up and no header is added. A read
  happens only where a body is due, and only from what the browser kept. On Firefox that is
  `Response.body()`. On Chromium Playwright's `Response.body()` goes back to the network when the
  browser kept an empty body under a Content-Length (Network.loadNetworkResource), a request the page
  never made, so a watch reads with Network.getResponseBody on a CDP session of its own, where no such
  fallback exists, and what it cannot read that way is unreadable.
- read_state returns the target's own state: attributes (aria-pressed, aria-checked,
  aria-expanded, href, data-*), live `checked` and `disabled`, the form value or selected
  option, and its own visible text without text inside nested elements of its role, so a
  readback on a post never passes because of one of its comments (the PoC lesson).
- Take over: a human in control stops a flow at the next step boundary (navigate, find, a
  bare key press) with LaneTakenOver, a HumanInControl that the runner treats like a busy
  lane. TakeOverPause makes a stop in the middle of a run a deferral instead of a failure.
- Shared lanes: a flow or connector run claims its lane for the whole run (using(mode="run"),
  agentos.node.claims): other callers' acts and claims wait in the lane's queue, their reads
  still pass. A lane another caller claims makes the run wait in the queue, then LaneBusy (the
  runner defers it). The run is found from the log context the runner sets (`run`).
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import functools
import logging
import re
from bisect import bisect_right
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentos.core.config import home_from_env
from agentos.core.errors import (
    AgentPaused,
    HumanInControl,
    InvalidRequest,
    LaneBusy,
    PolicyDenied,
    RefNotFound,
    ServiceUnavailable,
)
from agentos.core.models import ControlState, NavigateResult, Signal, parse_lane_id
from agentos.node import egress, human
from agentos.node.human import quietly
from agentos.node.interaction import PlaywrightInteraction
from agentos.node.snapshot import parse_line
from agentos.runner.driver import (
    MAX_JSON_BYTES,
    AmbiguousLocator,
    DriverError,
    DriverFactory,
    DriverTimeout,
    ElementNotFound,
    ElementState,
    JsonResponse,
)
from agentos.runner.models import Locator

log = logging.getLogger("agentos.flow_driver")

Page = Any     # playwright.async_api.Page
PwLocator = Any  # playwright.async_api.Locator
Handle = Any   # playwright.async_api.ElementHandle

POLL_S = 0.25
AMBIGUOUS_GRACE_S = 1.0      # an ambiguity can be a transition (old and new element both shown)
CALL_TIMEOUT_S = 15.0        # one resolution pass; a page that hangs longer is a timeout
EVAL_TIMEOUT_MS = 2_000
CHOOSER_TIMEOUT_MS = 8_000
SNAPSHOT_TIMEOUT_MS = 15_000
LOAD_TIMEOUT_MS = 10_000
MAX_TEXT = 20_000
MAX_ATTRS = 80
DWELL_MS = (600, 1500)       # a look at a freshly loaded page before acting on it
NO_NEST_ROLES = frozenset({"", "generic", "none", "presentation"})
OUTLINE_MAX_CHARS = 400_000  # a site API's discovery reads a whole page; no model reads this
ANCHOR_ROLES = ("article", "dialog", "alertdialog", "form", "region", "listitem", "row", "tabpanel", "group",
                "feed", "navigation", "main", "complementary", "search", "banner", "contentinfo")
MAX_ANCHOR_DEPTH = 3
# what an extract looks at, far above any max_items: past this a page only costs the node time
MAX_EXTRACT_MATCHES = 5000
MAX_EXTRACT_CHARS = 1_000_000  # of paths and attribute values, because a deep page has long paths
BODY_TIMEOUT_S = 20.0        # one response's body; a stream or a long poll never ends
PAGE_REQUESTS = ("fetch", "xhr")  # what a page's own code calls for, with a body the driver may read
NO_BODY = (204, 205)

# ---------------------------------------------------------------- browser scripts (read only)

# Path of an element in the composed tree: child indexes from the document down, with -1 for
# a step into a shadow root. Stable while the DOM does not change; never stored on the page.
_PATH_FN = """
const pathOf = (el) => {
  const p = [];
  let n = el;
  while (n) {
    const parent = n.parentNode;
    if (!parent) break;
    p.push(Array.prototype.indexOf.call(parent.childNodes, n));
    if (parent.nodeType === 11 && parent.host) { p.push(-1); n = parent.host; } else { n = parent; }
  }
  return p.reverse().join('/');
};
"""

PATHS_JS = "(els) => { " + _PATH_FN + " return els.map(pathOf); }"
ATTR_ROWS_JS = "(els, name) => { " + _PATH_FN + " return els.map((e) => [pathOf(e), e.getAttribute(name)]); }"
# The same rows up to a limit, read in the page so that the rest never cross to the node.
LIMITED_ROWS_JS = "(els, {limit, chars, name}) => { " + _PATH_FN + """
  const rows = [];
  let size = 0;
  for (const el of els) {
    if (rows.length >= limit || size >= chars) break;
    const row = [pathOf(el), name ? el.getAttribute(name) : null];
    size += row[0].length + (row[1] || '').length;
    rows.push(row);
  }
  return rows;
}"""
PATH_JS = "(el) => { " + _PATH_FN + " return pathOf(el); }"

# The ARIA role of an element found by CSS: the explicit role, else the implicit one for the tags
# that matter as anchors and targets (role-matched elements take the role from Playwright).
ROLE_JS = """
(el) => {
  const explicit = (el.getAttribute('role') || '').trim().split(/\\s+/)[0];
  if (explicit) return explicit.toLowerCase();
  const tag = el.localName;
  const named = () => el.hasAttribute('aria-label') || el.hasAttribute('aria-labelledby') || el.hasAttribute('title');
  const sectioned = () => !!(el.parentElement && el.parentElement.closest('article, aside, main, nav, section'));
  switch (tag) {
    case 'a': case 'area': return el.hasAttribute('href') ? 'link' : '';
    case 'article': return 'article';
    case 'aside': return 'complementary';
    case 'button': return 'button';
    case 'dialog': return 'dialog';
    case 'details': case 'fieldset': case 'optgroup': return 'group';
    case 'footer': return sectioned() ? '' : 'contentinfo';
    case 'header': return sectioned() ? '' : 'banner';
    case 'form': return named() ? 'form' : '';
    case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6': return 'heading';
    case 'img': return el.getAttribute('alt') === '' ? 'presentation' : 'img';
    case 'input': {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (['button', 'submit', 'reset', 'image'].includes(t)) return 'button';
      if (t === 'checkbox' || t === 'radio') return t;
      if (t === 'range') return 'slider';
      if (t === 'number') return 'spinbutton';
      if (t === 'search') return el.hasAttribute('list') ? 'combobox' : 'searchbox';
      if (['email', 'tel', 'text', 'url', ''].includes(t)) return el.hasAttribute('list') ? 'combobox' : 'textbox';
      return '';
    }
    case 'li': return 'listitem';
    case 'main': return 'main';
    case 'nav': return 'navigation';
    case 'ol': case 'ul': case 'menu': return 'list';
    case 'option': return 'option';
    case 'p': return 'paragraph';
    case 'progress': return 'progressbar';
    case 'search': return 'search';
    case 'section': return named() ? 'region' : '';
    case 'select': return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
    case 'table': return 'table';
    case 'tr': return 'row';
    case 'td': return 'cell';
    case 'th': return 'columnheader';
    case 'textarea': return 'textbox';
    default: return el.isContentEditable ? 'textbox' : 'generic';
  }
}
"""

# The target's own state. `exclude` holds the paths of nested elements with the target's role:
# their text is not the target's. Password values are never read, only their length.
STATE_JS = """
(el, {exclude, maxText, maxAttrs}) => {
  """ + _PATH_FN + """
  const path = pathOf(el);
  const skip = new Set(exclude);
  const parts = [];
  const lineParts = [];   // the same text with its line breaks: br, block edges, pre-* newlines
  let size = 0;
  const push = (t, l) => { if (size < maxText) { parts.push(t); lineParts.push(l); size += t.length; } };
  const keepsBreaks = (st) => st.whiteSpace.startsWith('pre') || st.whiteSpace === 'break-spaces';
  const walk = (node, nodePath, visible, pre) => {
    const kids = node.childNodes;
    for (let i = 0; i < kids.length && size < maxText; i++) {
      const c = kids[i];
      if (c.nodeType === 3) { if (visible) push(c.data, pre ? c.data : c.data.replace(/\\s+/g, ' ')); continue; }
      if (c.nodeType !== 1) continue;
      const tag = c.localName;
      if (['script', 'style', 'template', 'noscript', 'textarea'].includes(tag)) continue;
      const cp = nodePath + '/' + i;
      if (skip.has(cp)) { push(' ', ' '); continue; }
      const st = getComputedStyle(c);
      if (st.display === 'none') continue;
      const block = tag === 'br' || !st.display.startsWith('inline');
      if (block) push(' ', '\\n');
      walk(c, cp, st.visibility === 'visible', keepsBreaks(st));
      if (block && tag !== 'br') push(' ', '\\n');
    }
    if (!kids.length && node.shadowRoot) walk(node.shadowRoot, nodePath + '/-1', visible, pre);
  };
  const rootStyle = getComputedStyle(el);
  walk(el, path, rootStyle.visibility === 'visible', keepsBreaks(rootStyle));
  const text = parts.join('').replace(/\\s+/g, ' ').trim();
  const lines = lineParts.join('');
  const attrs = {};
  for (const a of Array.from(el.attributes).slice(0, maxAttrs)) attrs[a.name] = a.value.slice(0, 500);
  const tag = el.localName;
  const type = (el.getAttribute('type') || '').toLowerCase();
  let value = null;
  let sensitive = false;
  if (tag === 'input') {
    if (type === 'password') { sensitive = true; attrs['value_length'] = String(el.value.length); }
    else if (type === 'file') value = Array.from(el.files || [], (f) => f.name).join(', ');
    else value = el.value;
    if (type === 'checkbox' || type === 'radio') attrs['checked'] = String(el.checked);
  } else if (tag === 'textarea') {
    value = el.value;
  } else if (tag === 'select') {
    value = Array.from(el.selectedOptions, (o) => (o.label || o.textContent || '').trim()).join(', ');
  } else if (el.isContentEditable) {
    value = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
  } else {
    value = el.getAttribute('value');
  }
  if (el.matches(':disabled')) attrs['disabled'] = 'true';
  return {path, text, lines, value, attrs, sensitive};
}
"""

_IS_CONNECTED_JS = "(el) => el.isConnected"

_UPLOAD_KIND_JS = """
(el) => {
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return {
    fileInput: el.localName === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'file',
    visible: r.width >= 2 && r.height >= 2 && st.visibility === 'visible' && st.display !== 'none',
  };
}
"""

# Anchor-capable ancestors of an element, nearest first, with their name from the author
# (aria-labelledby, aria-label, title): the only names these container roles can have.
_ANCESTORS_JS = """
(el, roles) => {
  """ + _PATH_FN + """
  const roleOf = """ + ROLE_JS.strip() + """;
  const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();
  const out = [];
  let n = el.parentElement || (el.parentNode && el.parentNode.host);
  while (n && n.nodeType === 1 && n !== document.documentElement && n !== document.body) {
    const role = roleOf(n);
    if (roles.includes(role)) {
      let name = '';
      const ids = n.getAttribute('aria-labelledby');
      if (ids) {
        const root = n.getRootNode();
        name = clean(ids.split(/\\s+/).map((id) => {
          const ref = root.getElementById ? root.getElementById(id) : document.getElementById(id);
          return ref ? ref.textContent : '';
        }).join(' '));
      }
      if (!name) name = clean(n.getAttribute('aria-label'));
      if (!name) name = clean(n.getAttribute('title'));
      out.push({role, name, path: pathOf(n)});
    }
    n = n.parentElement || (n.parentNode && n.parentNode.host);
  }
  return out;
}
"""

# ---------------------------------------------------------------- errors


class LaneTakenOver(HumanInControl, LaneBusy):
    """A human has control of the lane. Raised before any input of the step, so nothing
    happened; a LaneBusy too, so a runner that has not learned about take over defers the run."""


class LaneAgentPaused(AgentPaused, LaneBusy):
    """A human stopped agents on the lane. Raised before any input of the step."""


class WrongElement(ElementNotFound):
    """The locator found an element that cannot take this action (not a text field, not a
    native select, no such option). Raised before any input."""

    code = "wrong_element"


class TargetUnreachable(ElementNotFound):
    """The element is hidden or covered (a banner, a dialog). Raised before the press."""

    code = "target_unreachable"


class FileNotInInbox(ElementNotFound):
    code = "file_not_found"


class FileOutsideInbox(PolicyDenied):
    """Uploads only take files from the lane's own inbox."""


TAKE_OVER_CODES = frozenset({HumanInControl.code, AgentPaused.code})

# ---------------------------------------------------------------- small helpers


def _norm(value: str) -> str:
    return " ".join(value.split())


_REGEX_SPECIALS = set("\\^$.|?*+()[]{}/-")


def regex_escape(text: str) -> str:
    """Escape for a pattern that both Python (the fake, Locator validation) and the browser's
    JavaScript regex engine read the same way."""
    return "".join("\\" + ch if ch in _REGEX_SPECIALS else ch for ch in text)


def name_query(loc: Locator) -> dict[str, Any]:
    """Playwright get_by_role arguments with the fake's name semantics: exact after whitespace
    normalization (case sensitive), case-sensitive substring, or a regex search."""
    if loc.name is None:
        return {}
    if loc.match == "regex":
        return {"name": re.compile(loc.name)}
    name = _norm(loc.name)
    if loc.match == "contains":
        return {"name": re.compile(regex_escape(name))}
    if not name:
        return {"name": re.compile(r"^$")}
    return {"name": name, "exact": True}


def attr_css(name: str) -> str:
    """`[name]` for an attribute name, escaped for CSS (names may hold ':' and '.')."""
    return "[" + re.sub(r"([^A-Za-z0-9_-])", r"\\\1", name) + "]"


def tree_order(path: str) -> tuple[int, ...]:
    """Sort key for element paths: document order, a shadow root (-1) before light children."""
    return tuple(int(x) for x in path.split("/") if x)


def record_levels(paths: list[str], pick: str | None) -> list[int | None]:
    """ExtractStep.pick over element paths in tree order: per match, how many levels up its record
    is (0: the match itself, `card`), or None to drop it (`outermost`)."""
    if pick == "outermost":  # in tree order a match's descendants come right after it
        out: list[int | None] = []
        top: str | None = None
        for p in paths:
            if top is not None and inside(top, p):
                out.append(None)
            else:
                top = p
                out.append(0)
        return out
    if pick != "card":
        return [0] * len(paths)
    parts = [p.split("/") for p in paths]

    def shared(a: list[str], b: list[str]) -> int:
        n = 0
        while n < min(len(a), len(b)) and a[n] == b[n]:
            n += 1
        return n

    out = []
    for i, mine in enumerate(parts):
        near = [shared(mine, parts[j]) for j in (i - 1, i + 1) if 0 <= j < len(parts)]
        keep = (max(near) if near else max(len(mine) - 1, 0)) + 1  # the first level no other match shares
        if "-1" in mine[keep:]:  # never up through a shadow root
            keep = len(mine) - mine[::-1].index("-1") + 1  # the first element under that root
        out.append(max(len(mine) - keep, 0))
    return out


def inside(outer: str, inner: str) -> bool:
    """True if the element at path `inner` is a strict descendant of the one at `outer`."""
    return inner.startswith(outer + "/")


def own_filter(paths: list[str], anchor: str, nested: list[str]) -> list[bool]:
    """For each candidate path: kept unless it sits inside a nested element (strictly inside
    the anchor) that has the anchor's role. Mirrors fakes._nested_same_role."""
    inner = sorted((n for n in nested if inside(anchor, n)), key=tree_order)
    walls = [w for w, up in zip(inner, record_levels(inner, "outermost"), strict=True) if up == 0]
    starts = [tree_order(w) for w in walls]

    def kept(path: str) -> bool:  # subtrees of outermost walls do not overlap: only the last one before it can hold it
        at = bisect_right(starts, tree_order(path)) - 1
        return at < 0 or not inside(walls[at], path)

    return [kept(p) for p in paths]


_NUMBER_RE = re.compile(r"\d[\d.,]*[KkMm]?")
_NUMBER_PATTERN = r"\d[\d.,]*[KkMm]?"


def name_variants(name: str | None) -> list[tuple[str | None, str]]:
    """(name, match) choices for a recorded locator, the most durable first. Counts in a name
    ("Like 12") change, so a pattern that accepts any number comes first; very long names
    (a link around a whole post) match on their start."""
    if name is None:
        return [(None, "exact")]
    name = _norm(name)
    if not name:
        return [(None, "exact")]
    out: list[tuple[str | None, str]] = []
    if _NUMBER_RE.search(name):
        pieces = _NUMBER_RE.split(name)
        out.append(("^" + _NUMBER_PATTERN.join(regex_escape(p) for p in pieces) + "$", "regex"))
    if len(name) > 80:
        head = name[:60].rsplit(" ", 1)[0] or name[:60]
        out.append((head, "contains"))
    out.append((name, "exact"))
    return out


def ref_role_and_name(snapshot: str, ref: str) -> tuple[str | None, str | None]:
    """Role and name of the line that carries `[ref=<ref>]` in a snapshot (raw or outline)."""
    tag = f"[ref={ref}]"
    for raw in snapshot.splitlines():
        if tag in raw:
            line = parse_line(raw)
            return line.role, line.name
    return None, None


def _first_line(e: BaseException) -> str:
    text = str(e).strip() or type(e).__name__
    return text.splitlines()[0][:300]


def _browser_error(e: BaseException) -> bool:
    return type(e).__module__.startswith("playwright") or isinstance(e, TimeoutError)


# ---------------------------------------------------------------- matches and elements


@dataclass(slots=True)
class _Match:
    loc: PwLocator      # a Playwright locator for exactly this element (raw query + nth)
    path: str
    role: str | None    # ARIA role when known (role match); computed lazily for CSS matches


@dataclass(eq=False)
class Element:
    """What find() returns: a pinned handle plus how it was found."""

    handle: Handle
    path: str
    role: str | None
    locator: Locator

    def describe(self) -> str:
        return self.locator.describe()


Gate = Callable[[], None]


# ---------------------------------------------------------------- the page's own responses


def _on_chromium(page: Page) -> bool:
    browser = page.context.browser
    return browser is not None and browser.browser_type.name == "chromium"


def _bare(url: str) -> str:
    return url.split("#", 1)[0]  # Playwright's URL keeps a fragment, CDP's does not


class _ChromiumBodies:
    """A page's bodies read with Network.getResponseBody on a CDP session of its own, which only listens:
    Network.enable shows nothing to the page or the site, and the session never sends a request. Network
    events carry an id that Playwright's Response does not, so a response is found by its turn: the nth
    fetch or XHR to answer on that URL since the page committed is the nth here. A response of the page
    being left takes no turn, by its loader: JsonWatch counts from the commit and this from the load, and
    those agree only because of that. Failing to attach means a read fails, never that it goes through
    Response.body()."""

    def __init__(self, page: Page):
        self._page = page
        self._session: Any = None
        self._main: str | None = None  # the main frame
        self._left: set[str] = set()  # loaders of the page being left
        self._loaders: set[str] = set()  # loaders seen since this load began
        self._turns: Counter[str] = Counter()
        self._ids: dict[tuple[str, int], asyncio.Future[str]] = {}
        self._ends: dict[str, asyncio.Future[bool]] = {}
        self.ready = asyncio.get_running_loop().create_task(self._attach())

    async def _attach(self) -> None:
        session = await self._page.context.new_cdp_session(self._page)
        frames = [(await session.send("Page.getFrameTree"))["frameTree"]]
        self._main = frames[0]["frame"]["id"]
        while frames:
            tree = frames.pop()
            self._left.add(tree["frame"]["loaderId"])
            frames += tree.get("childFrames", [])
        session.on("Network.requestWillBeSent", self._on_request)
        session.on("Network.responseReceived", self._on_response)
        session.on("Network.loadingFinished", lambda event: self._ended(event, True))
        session.on("Network.loadingFailed", lambda event: self._ended(event, False))
        await session.send("Network.enable")
        self._session = session

    def _id_at(self, url: str, turn: int) -> asyncio.Future[str]:
        return self._ids.setdefault((url, turn), asyncio.get_running_loop().create_future())

    def _on_request(self, event: dict[str, Any]) -> None:
        if event.get("type") == "Document" and event.get("frameId") == self._main:
            self._left |= self._loaders
            self._loaders = set()
            self._turns.clear()

    def _on_response(self, event: dict[str, Any]) -> None:
        if event.get("type") not in ("Fetch", "XHR") or event.get("loaderId") in self._left:
            return
        self._loaders.add(event.get("loaderId", ""))
        url, request_id = _bare(event["response"]["url"]), event["requestId"]
        self._ends[request_id] = asyncio.get_running_loop().create_future()
        turn = self._turns[url]
        self._turns[url] += 1
        if not (found := self._id_at(url, turn)).done():
            found.set_result(request_id)

    def _ended(self, event: dict[str, Any], whole: bool) -> None:
        end = self._ends.get(event["requestId"])
        if end is not None and not end.done():
            end.set_result(whole)

    async def read(self, url: str, turn: int) -> bytes:
        await self.ready
        request_id = await self._id_at(_bare(url), turn)
        if not await self._ends[request_id]:
            raise ConnectionError("the request failed before its body ended")
        got = await self._session.send("Network.getResponseBody", {"requestId": request_id})
        return base64.b64decode(got["body"]) if got.get("base64Encoded") else got["body"].encode()

    async def _detach(self) -> None:
        with suppress(Exception):  # the page may be gone
            await self.ready
            await self._session.detach()

    def close(self) -> None:
        for waiting in (*self._ids.values(), *self._ends.values()):
            waiting.cancel()
        with suppress(RuntimeError):  # no loop left: the session goes with the browser
            task = asyncio.get_running_loop().create_task(self._detach())
            _DETACHING.add(task)
            task.add_done_callback(_DETACHING.discard)


_DETACHING: set[asyncio.Task[None]] = set()


class JsonWatch:
    """FlowDriver.watch_json on one page. A response before the main frame commits belongs to the
    page being left, so nothing counts until then: arming before the load must not read the old page."""

    def __init__(self, page: Page, wanted: Callable[[str], bool], keep: int, only_json: bool):
        self.responses: list[JsonResponse] = []
        self._page, self._wanted, self._keep, self._only_json = page, wanted, keep, only_json
        self._loaded = False
        self._taken = 0
        self._reads: set[asyncio.Task[None]] = set()  # responses that came, with their bodies on the way
        self._turns: Counter[str] = Counter()
        self._bodies = _ChromiumBodies(page) if _on_chromium(page) else None

        def on_frame(frame: Any) -> None:
            if frame.parent_frame is None:
                self._loaded = True

        def on_response(response: Any) -> None:
            self._saw(response)

        self._on_frame, self._on_response = on_frame, on_response
        page.on("framenavigated", on_frame)
        page.on("response", on_response)

    @property
    def reading(self) -> int:
        return len(self._reads)

    @property
    def ready(self) -> asyncio.Task[None] | None:
        """Done once a body can be read, which must be before the page loads."""
        return self._bodies.ready if self._bodies else None

    @staticmethod
    def _ends_here(response: Any) -> bool:
        """A fetch or XHR that got its answer: a hop leads to the response, and is not one."""
        hop = 300 <= response.status < 400 and "location" in response.headers
        return response.request.resource_type in PAGE_REQUESTS and not hop

    def _takes(self, response: Any) -> bool:
        if response.from_service_worker:
            return False
        if self._only_json and "json" not in response.headers.get("content-type", ""):
            return False
        return response.frame.page == self._page and self._wanted(response.url)

    def _saw(self, response: Any) -> None:
        if not self._loaded:
            return
        try:
            if not self._ends_here(response):
                return
            url = _bare(response.url)
            turn = self._turns[url]  # every response takes a turn, read or not, to stay in step with the session
            self._turns[url] += 1
            if self._taken >= self._keep or not self._takes(response):
                return
        except Exception:  # noqa: BLE001 - a service worker's response has no frame
            return
        self._taken += 1
        task = asyncio.get_running_loop().create_task(self._read(response, turn))
        self._reads.add(task)
        task.add_done_callback(self._reads.discard)

    async def _read(self, response: Any, turn: int) -> None:
        url, status = response.url, response.status
        length = response.headers.get("content-length", "")
        if status == 304:  # the page's own If-None-Match, as Chrome shows its own revalidation as a 200: use a body if any
            found = await self._body(response, url, status, turn, length)
        elif not 200 <= status < 300:
            found = JsonResponse(url, status)
        elif status in NO_BODY or response.request.method == "HEAD" or length == "0":
            found = JsonResponse(url, status, b"")
        elif length.isdigit() and int(length) > MAX_JSON_BYTES:
            found = JsonResponse(url, status, problem="too_large")
        else:
            found = await self._body(response, url, status, turn, length)
        self.responses.append(found)

    async def _body(self, response: Any, url: str, status: int, turn: int, length: str) -> JsonResponse:
        read = response.body() if self._bodies is None else self._bodies.read(url, turn)
        try:
            body = await asyncio.wait_for(read, BODY_TIMEOUT_S)
        except TimeoutError:  # a stream, a long poll, a site in trouble
            return JsonResponse(url, status, problem="slow")
        except Exception:  # noqa: BLE001 - gone with a navigation; its text is not kept
            return JsonResponse(url, status, problem="unreadable")
        if len(body) > MAX_JSON_BYTES:
            return JsonResponse(url, status, problem="too_large")
        if not body and length.isdigit() and int(length) > 0:  # one was due, and nothing here goes to fetch it
            return JsonResponse(url, status, problem="unreadable")
        return JsonResponse(url, status, body)

    def close(self) -> None:
        with suppress(Exception):  # the page may be gone
            self._page.remove_listener("response", self._on_response)
            self._page.remove_listener("framenavigated", self._on_frame)
        self._taken = self._keep
        for task in list(self._reads):
            task.cancel()
        if self._bodies is not None:
            self._bodies.close()
        self.responses = []


# ---------------------------------------------------------------- the driver


class PlaywrightFlowDriver:
    """FlowDriver over one lane page. `gate` raises when a human holds the lane."""

    def __init__(self, page: Page, *, lane_id: str, files_dir: Path | str | None = None,
                 interaction: Any = None, gate: Gate | None = None, files: Any = None,
                 dwell_ms: tuple[int, int] = DWELL_MS,
                 signed_out: Callable[..., Awaitable[dict[str, Any]]] | None = None):
        self.page = page
        self._signed_out = signed_out  # (url, text, timeout_ms=) -> the lane manager's read_signed_out
        self.lane_id = lane_id
        self.files_dir = Path(files_dir) if files_dir else default_inbox(lane_id)
        self.interaction = interaction or PlaywrightInteraction()
        self.gate = gate
        self.files = files
        self.dwell_ms = dwell_ms
        self._handles: list[Handle] = []
        self._arming: list[asyncio.Task[None]] = []  # watches that must be listening before the next load

    # ---- control ----
    def _check_gate(self) -> None:
        if self.gate is not None:
            self.gate()

    async def scope_commit(self, flow_id: str, request: dict[str, Any] | None) -> None:
        """A connector run declares its commit request (None for a read): the lane's egress guard
        then refuses any other commit or write this page sends while the run holds the lane
        (agentos.node.egress.FlowCommit). Raises EngineError when the guard cannot bind its route."""
        guard = egress.guard_for(self.page)
        if guard is not None:
            await guard.scope_flow(egress.FlowCommit.of(flow_id, request))

    # ---- navigation and page reads ----
    async def navigate(self, url: str) -> NavigateResult:
        self._check_gate()
        while self._arming:
            with suppress(Exception):  # a watch that could not attach fails its own read
                await self._arming.pop()
        result = await self.interaction.navigate(self.page, url)
        if self.dwell_ms[1] > 0:
            await human.dwell(self.page, *self.dwell_ms)
        return result

    async def current_url(self) -> str:
        return self.page.url

    async def outline(self) -> str:
        self._check_gate()
        return (await self.interaction.snapshot(self.page, max_chars=OUTLINE_MAX_CHARS)).text

    async def screenshot(self) -> bytes:
        return await self.interaction.screenshot(self.page)

    async def signal(self) -> Signal:
        return await self.interaction.signal(self.page)

    async def read_signed_out(self, url: str, text: str, *, timeout_ms: int = 20_000) -> dict[str, Any]:
        """The AUD-22 check (agentos.node.signed_out): a throwaway browser, not this lane's page."""
        if self._signed_out is None:
            raise ServiceUnavailable("this lane cannot open a page signed out")
        return await self._signed_out(url, text, timeout_ms=timeout_ms)

    # ---- resolution (the rules of Locator, as in runner.fakes) ----
    async def _paths(self, raw: PwLocator, limited: bool = False) -> list[str]:
        if limited:
            return [p for p, _ in await self._limited_rows(raw, None)]
        return list(await raw.evaluate_all(PATHS_JS))

    @staticmethod
    async def _limited_rows(raw: PwLocator, attr: str | None) -> list[tuple[str, str | None]]:
        rows = await raw.evaluate_all(LIMITED_ROWS_JS, {"limit": MAX_EXTRACT_MATCHES, "chars": MAX_EXTRACT_CHARS, "name": attr})
        return [(p, v) for p, v in rows]

    async def _role_of(self, m: _Match) -> str:
        if m.role is None:
            m.role = str(await m.loc.evaluate(ROLE_JS, timeout=EVAL_TIMEOUT_MS) or "")
        return m.role

    async def _candidates(self, raw: PwLocator, loc: Locator, anchor: _Match | None,
                          role: str | None, limited: bool = False) -> list[_Match]:
        """The elements of a raw query inside the anchor, narrowed by `attr` (same matcher as
        the fake). `limited`: an extract, which a page must not be able to make expensive."""
        if limited:
            rows = await self._limited_rows(raw, loc.attr.name if loc.attr else None)
        elif loc.attr is None:
            rows = [(p, None) for p in await self._paths(raw)]
        else:
            rows = [(p, v) for p, v in await raw.evaluate_all(ATTR_ROWS_JS, loc.attr.name)]
        found = [_Match(raw.nth(i), p, role) for i, (p, v) in enumerate(rows)
                 if (anchor is None or inside(anchor.path, p)) and (loc.attr is None or loc.attr.matches(v))]
        # tree order (a shadow root's content right after its host), as the fake walks the page:
        # Playwright lists shadow root matches after the light DOM ones, which would change nth
        return sorted(found, key=lambda m: tree_order(m.path))

    async def _resolve_all(self, loc: Locator, scope: _Match | None = None, limited: bool = False) -> list[_Match]:
        """Candidates under the scope rules, ignoring nth. Raises for a bad anchor. `scope` is
        an extract item: the locator resolves inside it, with the item as its anchor."""
        if loc.names is not None and loc.name is None:
            raise ElementNotFound(f"{loc.describe()}: names were not resolved for the account language")
        anchor = await self._resolve_one(loc.within, scope, limited) if loc.within else scope
        base = anchor.loc if anchor is not None else self.page
        found: list[_Match] = []
        if loc.role:
            level = {"level": loc.level} if loc.level else {}
            found = await self._candidates(base.get_by_role(loc.role, **name_query(loc), **level), loc, anchor, loc.role, limited)
        elif not loc.css and loc.attr is not None:  # attr alone
            found = await self._candidates(base.locator("css=" + attr_css(loc.attr.name)), loc, anchor, None, limited)
        if not found and loc.css:
            raw = base.locator("css=" + loc.css)  # always CSS, never another selector engine
            found = await self._candidates(raw, loc, anchor, None, limited)
        if anchor is not None and loc.own and found:
            role = await self._role_of(anchor)
            if role not in NO_NEST_ROLES:
                nested = await self._paths(anchor.loc.get_by_role(role, include_hidden=True), limited)
                keep = own_filter([m.path for m in found], anchor.path, nested)
                found = [m for m, k in zip(found, keep, strict=True) if k]
        return found

    async def _resolve_one(self, loc: Locator, scope: _Match | None = None, limited: bool = False) -> _Match:
        found = await self._resolve_all(loc, scope, limited)
        if loc.nth is not None:
            if loc.nth < len(found):
                return found[loc.nth]
            raise ElementNotFound(f"no element #{loc.nth} for {loc.describe()} ({len(found)} matches)")
        if not found:
            raise ElementNotFound(f"no element matches {loc.describe()}")
        if len(found) > 1:
            raise AmbiguousLocator(f"{len(found)} elements match {loc.describe()}; anchor it with within or set nth",
                                   count=len(found))
        return found[0]

    async def _pin(self, m: _Match) -> Handle | None:
        """A handle on exactly the matched element (None if the page changed meanwhile)."""
        handle = await m.loc.element_handle(timeout=EVAL_TIMEOUT_MS)
        if handle is None:
            return None
        if await handle.evaluate(PATH_JS) != m.path:
            await quietly(handle.dispose())
            return None
        return handle

    async def _release_handles(self) -> None:
        old, self._handles = self._handles, []
        for h in old:
            await quietly(h.dispose())

    async def find(self, locator: Locator, *, timeout_ms: int = 0) -> Element:
        self._check_gate()
        await self._release_handles()  # elements live for one step
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, timeout_ms) / 1000
        ambiguous_since: float | None = None
        last: Exception | None = None
        while True:
            try:
                m = await asyncio.wait_for(self._resolve_one(locator), CALL_TIMEOUT_S)
                handle = await self._pin(m)
                if handle is not None:
                    self._handles.append(handle)
                    return Element(handle=handle, path=m.path, role=m.role, locator=locator)
                last = ElementNotFound(f"{locator.describe()} changed while it was being found")
                ambiguous_since = None
            except AmbiguousLocator as e:
                last = e
                now = loop.time()
                ambiguous_since = now if ambiguous_since is None else ambiguous_since
                if now - ambiguous_since >= AMBIGUOUS_GRACE_S or now >= deadline:
                    raise
            except ElementNotFound as e:
                last, ambiguous_since = e, None
            except TimeoutError as e:
                raise DriverTimeout(f"the page did not answer while finding {locator.describe()}") from e
            except Exception as e:
                if not _browser_error(e):
                    raise
                last = DriverError(f"finding {locator.describe()} failed: {_first_line(e)}", transient=True)
            if loop.time() >= deadline:
                if isinstance(last, DriverError) and last.transient and timeout_ms:
                    raise DriverTimeout(f"{locator.describe()}: {last.message}") from last
                raise last
            self._check_gate()
            await asyncio.sleep(POLL_S)

    # ---- the target's own state ----
    async def _state(self, m: _Match, *, own: bool = True) -> dict[str, Any] | None:
        role = await self._role_of(m)
        exclude: list[str] = []
        if own and role not in NO_NEST_ROLES:
            exclude = [p for p in await self._paths(m.loc.get_by_role(role, include_hidden=True)) if inside(m.path, p)]
        data = await m.loc.evaluate(STATE_JS, {"exclude": exclude, "maxText": MAX_TEXT, "maxAttrs": MAX_ATTRS},
                                    timeout=EVAL_TIMEOUT_MS)
        if not data or data.get("path") != m.path:
            return None
        return data

    async def _read_once(self, locator: Locator) -> ElementState | None:
        """None when the element changed between the query and the read (try again)."""
        url = self.page.url
        try:
            found = await asyncio.wait_for(self._resolve_all(locator), CALL_TIMEOUT_S)
        except (ElementNotFound, AmbiguousLocator) as e:
            return ElementState(count=0, found=False, ambiguous=isinstance(e, AmbiguousLocator), url=url)
        el = None
        if locator.nth is not None:
            el = found[locator.nth] if locator.nth < len(found) else None
        elif len(found) == 1:
            el = found[0]
        if el is None:
            return ElementState(count=len(found), found=False, ambiguous=len(found) > 1 and locator.nth is None,
                                url=url)
        data = await self._state(el)
        if data is None:
            return None
        return ElementState(count=len(found), found=True, text=data["text"], lines=data.get("lines"),
                            value=data["value"],
                            attributes={str(k): str(v) for k, v in data["attrs"].items()}, url=self.page.url)

    async def read_state(self, locator: Locator) -> ElementState:
        last: Exception | None = None
        for _ in range(3):
            try:
                state = await self._read_once(locator)
            except Exception as e:  # a navigation or a re-render under the read
                if not _browser_error(e):
                    raise
                last, state = e, None
            if state is not None:
                return state
            await asyncio.sleep(POLL_S)
        if last is not None:
            raise DriverError(f"reading {locator.describe()} failed: {_first_line(last)}", transient=True) from last
        return ElementState(count=0, found=False, url=self.page.url)

    # ---- records and identity (read only) ----
    async def _field(self, target: _Match, f: Any) -> str | None:
        data = await self._state(target, own=f.read != "text")
        if data is None:
            raise ElementNotFound("the page changed while a record was being read")
        if f.attr is not None:
            value = data["attrs"].get(f.attr)
            return None if value is None else str(value)
        if f.read == "value":
            return None if data.get("sensitive") or data["value"] is None else str(data["value"])
        return str(data["text"])

    @staticmethod
    def _picked(found: list[_Match], pick: str | None) -> list[_Match]:
        out = []
        for m, up in zip(found, record_levels([m.path for m in found], pick), strict=True):
            if up == 0:
                out.append(m)
            elif up is not None:
                path = "/".join(m.path.split("/")[:-up])
                out.append(_Match(m.loc.locator("xpath=" + "/".join([".."] * up)), path, None))
        return out

    async def _extract_once(self, items: Locator, fields: dict[str, Any], max_items: int,
                            pick: str | None = None) -> tuple[list[dict[str, str | None]], int]:
        found = await self._resolve_all(items, limited=True)
        if items.nth is not None:
            found = found[items.nth:items.nth + 1]
        found = self._picked(found, pick)
        rows = []
        for item in found[:max_items]:
            row: dict[str, str | None] = {}
            for name, f in fields.items():
                target: _Match | None = item
                if f.locator is not None:
                    try:
                        target = await self._resolve_one(f.locator, item, limited=True)
                    except (ElementNotFound, AmbiguousLocator):
                        target = None
                row[name] = None if target is None else await self._field(target, f)
            rows.append(row)
        return rows, len(found)

    async def extract_items(self, items: Locator, fields: dict[str, Any], max_items: int,
                            pick: str | None = None) -> tuple[list[dict[str, str | None]], int]:
        self._check_gate()
        last: Exception | None = None
        for _ in range(3):  # a re-render under the read: read the whole list again
            try:
                return await asyncio.wait_for(self._extract_once(items, fields, max_items, pick),
                                              CALL_TIMEOUT_S * 4)
            except AmbiguousLocator:
                raise
            except ElementNotFound as e:
                if "changed while" not in e.message:
                    raise
                last = e
            except TimeoutError as e:
                raise DriverTimeout(f"the page did not answer while reading {items.describe()}") from e
            except Exception as e:
                if not _browser_error(e):
                    raise
                last = e
            await asyncio.sleep(POLL_S)
        raise DriverError(f"reading {items.describe()} failed: {_first_line(last or Exception())}", transient=True)

    def watch_json(self, wanted: Callable[[str], bool], *, keep: int = 1, only_json: bool = False) -> JsonWatch:
        watch = JsonWatch(self.page, wanted, keep, only_json)
        if watch.ready is not None:
            self._arming.append(watch.ready)
        return watch

    async def account(self, site: str | None = None, domain: str | None = None) -> dict[str, str] | None:
        """The signed-in account's id from the site's identity cookie (agentos.node.sites):
        `context.cookies(url)`, no request and invisible to the page."""
        from agentos.node import sites

        here = sites.site_for(self.page.url)
        if site:
            s = sites.site(site)
        else:
            s = (sites.site_for(f"https://{domain}/") if domain else None) or here
        if s is None:
            return None
        url = self.page.url if here is not None and here.key == s.key else None
        return await sites.account_identity(self.page.context, s, url)

    # ---- actions (human input only) ----
    async def _live(self, element: Element) -> Handle:
        if not isinstance(element, Element):
            raise DriverError("that element did not come from this driver's find()")
        if not await quietly(element.handle.evaluate(_IS_CONNECTED_JS), False):
            raise ElementNotFound(f"{element.describe()} left the page before the action")
        return element.handle

    async def _settle(self, url_before: str) -> None:
        if self.page.url.split("#", 1)[0] != url_before.split("#", 1)[0]:
            await quietly(self.page.wait_for_load_state("domcontentloaded", timeout=LOAD_TIMEOUT_MS))

    async def _human(self, element: Element, what: str, coro: Awaitable[Any]) -> Any:
        try:
            return await coro
        except (human.TargetNotVisible, human.TargetCovered) as e:
            raise TargetUnreachable(f"{element.describe()}: {e.message}") from e
        except (DriverError, InvalidRequest):
            raise
        except Exception as e:
            if _browser_error(e):
                raise DriverError(f"{what} on {element.describe()} failed: {_first_line(e)}") from e
            raise

    async def click(self, element: Element) -> None:
        h = await self._live(element)
        url_before = self.page.url
        await self._human(element, "click", human.click(self.page, h))
        await self._settle(url_before)

    async def _text_field(self, element: Element, what: str) -> Handle:
        h = await self._live(element)
        if await human.editable_kind(h) is None:
            raise WrongElement(f"{what} needs a text field; {element.describe()} is not one")
        return h

    async def type(self, element: Element, text: str) -> None:
        h = await self._text_field(element, "type")
        await self._human(element, "type", human.focus_field(self.page, h, caret_to_end=True))
        await self._human(element, "type", human.type_text(self.page, text, typos=await human.typos_fit(h)))

    async def fill(self, element: Element, text: str) -> None:
        h = await self._text_field(element, "fill")
        if await quietly(h.evaluate("(el) => 'value' in el ? el.value : (el.innerText || '')")) == text:
            return  # already holds exactly this text: a person would not retype it
        await self._human(element, "fill", human.focus_field(self.page, h, caret_to_end=False))
        await self._human(element, "fill", human.clear_field(self.page, h))
        if text:
            await self._human(element, "fill", human.type_text(self.page, text, typos=await human.typos_fit(h)))

    async def select(self, element: Element, option: str) -> None:
        h = await self._live(element)
        info = await h.evaluate(human._SELECT_INFO_JS)
        if info.get("tag") != "select":
            raise WrongElement(f"select works on a native dropdown; {element.describe()} is not one. "
                               "For a custom one, click it and then the option.")
        index = human.match_option(info["options"], option)
        if index is None:
            shown = ", ".join(repr(o["label"]) for o in info["options"][:20])
            raise WrongElement(f"option {option!r} is not in {element.describe()}. Options: {shown}")
        if info["options"][index].get("disabled"):
            raise WrongElement(f"option {option!r} of {element.describe()} is disabled")
        await self._human(element, "select", human.select_option(self.page, h, option))

    async def press(self, key: str, element: Element | None = None) -> None:
        if element is None:
            self._check_gate()  # a bare key press has no find() to stop at
        url_before = self.page.url
        if element is None:
            await human.press(self.page, key)
        else:
            h = await self._live(element)

            async def run() -> None:
                if not await human.is_focused(h):
                    if await human.editable_kind(h) is not None:
                        await human.focus_field(self.page, h)
                    else:
                        await h.focus()  # like arriving with Tab: focus without a click
                await human.press(self.page, key)

            await self._human(element, "press", run())
        await self._settle(url_before)

    async def scroll_to(self, element: Element) -> None:
        h = await self._live(element)
        if await h.bounding_box() is None:
            raise TargetUnreachable(f"{element.describe()} is not visible on the page")
        await self._human(element, "scroll", human.scroll_into_view(self.page, h))

    async def upload(self, element: Element, file_ref: str) -> None:
        path = resolve_inbox_file(self.files_dir, file_ref, files=self.files, lane_id=self.lane_id)
        h = await self._live(element)
        kind = await h.evaluate(_UPLOAD_KIND_JS)
        if kind.get("fileInput") and not kind.get("visible"):
            # No visible control to click: set the file the way the OS dialog would.
            await h.set_input_files(str(path))
            return
        try:
            async with self.page.expect_file_chooser(timeout=CHOOSER_TIMEOUT_MS) as chooser_info:
                await self._human(element, "upload", human.click(self.page, h))
            chooser = await chooser_info.value
        except (ElementNotFound, DriverError):
            raise
        except Exception as e:
            if _browser_error(e):
                raise DriverError(f"clicking {element.describe()} did not open a file chooser: "
                                  f"{_first_line(e)}") from e
            raise
        await chooser.set_files(str(path))
        await human.dwell(self.page, 300, 900)


# ---------------------------------------------------------------- the inbox


def default_inbox(lane_id: str, home: Path | None = None) -> Path:
    workspace, name = parse_lane_id(lane_id)
    return (home or home_from_env()) / "lanes" / workspace / name / "files" / "inbox"


def inbox_dir(runtime: Any, lane_id: str) -> Path:
    """The lane's inbox: the files service's, else AGENTOS_HOME/lanes/<ws>/<name>/files/inbox."""
    files = runtime.get("files") if runtime is not None else None
    for attr in ("inbox_dir", "inbox"):
        fn = getattr(files, attr, None)
        if callable(fn):
            return Path(fn(lane_id))
    cfg = runtime.get("config") if runtime is not None else None
    if cfg is not None and hasattr(cfg, "lane_dir"):
        return cfg.lane_dir(lane_id) / "files" / "inbox"
    return default_inbox(lane_id)


_REF_PREFIXES = ("files:", "file:", "inbox:", "inbox/")


def resolve_inbox_file(inbox: Path, file_ref: str, *, files: Any = None, lane_id: str | None = None) -> Path:
    """A file id, name or path inside the lane's inbox. Anything outside it (absolute paths,
    `..`, a symlink that leads out) is refused."""
    ref = (file_ref or "").strip()
    if not ref:
        raise FileNotInInbox("the upload needs a file: a name or id from the lane's inbox")
    root = Path(inbox).resolve()
    candidate: Path | None = None
    resolver = getattr(files, "resolve", None)
    if callable(resolver) and lane_id:
        try:
            found = resolver(lane_id, ref)
        except Exception:  # noqa: BLE001 - an unknown id falls through to the name lookup
            found = None
        if found:
            candidate = Path(getattr(found, "path", found))
    if candidate is None:
        for prefix in _REF_PREFIXES:
            if ref.startswith(prefix):
                ref = ref[len(prefix):]
                break
        p = Path(ref)
        candidate = p if p.is_absolute() else root / p
    real = candidate.resolve()
    if real != root and not real.is_relative_to(root):
        raise FileOutsideInbox(f"{file_ref!r} is outside the lane's inbox; flows upload only files put in "
                               "the inbox first")
    if real.is_file():
        return real
    if "/" not in ref and root.is_dir():
        hits = [f for f in sorted(root.iterdir())
                if f.is_file() and (f.stem == ref or any(f.name.startswith(ref + s) for s in ("-", "_", ".")))]
        if len(hits) == 1:
            hit = hits[0].resolve()
            if hit.is_relative_to(root) and hit.is_file():
                return hit
            raise FileOutsideInbox(f"{file_ref!r} links outside the lane's inbox")
        if len(hits) > 1:
            raise FileNotInInbox(f"{len(hits)} inbox files match {file_ref!r}; use the full file name")
    raise FileNotInInbox(f"no file {file_ref!r} in the lane's inbox; put it there first")


# ---------------------------------------------------------------- lanes: the factory and the gate


def control_gate(lanes: Any, lane_id: str) -> Gate:
    """A check that raises LaneTakenOver / LaneAgentPaused when a human holds the lane."""

    def state() -> ControlState | None:
        try:
            fn = getattr(lanes, "control", None)
            control = fn(lane_id) if callable(fn) else getattr(lanes.get(lane_id), "control", None)
        except Exception:  # noqa: BLE001 - no control record: nothing to enforce here
            return None
        st = getattr(control, "state", None)
        return ControlState(st) if st is not None else None

    def check() -> None:
        st = state()
        if st == ControlState.HUMAN:
            raise LaneTakenOver(f"a human has control of {lane_id}; the flow waits and continues after "
                                "they hand it back")
        if st == ControlState.PAUSED:
            raise LaneAgentPaused(f"the human stopped agents on {lane_id}; the flow continues only after "
                                  "they hand it back")

    return check


def _using(lanes: Any, lane_id: str) -> Any:
    """The lane as a flow principal. Inside a run (the runner's log context names it), the run's
    implicit claim for the whole session; outside one (a page watch, a re-check), one act."""
    from agentos.core import logs
    from agentos.node.claims import CLAIM_PARENT, RUN, Holder

    ctx = logs.current()
    run_id = ctx.get("run")
    try:
        if run_id:
            holder = Holder.for_run(run_id)
            parent = str(ctx.get("parent") or "")
            if parent.startswith(CLAIM_PARENT):  # a run inside a claim the node holds (an engagement session)
                key = parent[len(CLAIM_PARENT):]
                # honored only while that holder holds a claim on THIS lane (its key carries a nonce
                # per session, so a guessed key matches nothing); otherwise the run waits like any other
                held = getattr(lanes, "claimed_by", None)
                if callable(held) and held(lane_id, key):
                    holder = Holder(key=key, label=f"flow run {run_id}", kind="flow", identity=key, run_id=run_id)
                else:
                    log.warning("flow run %s names claim parent %r, which holds no claim on %s; ignored",
                                run_id, key, lane_id)
            try:
                return lanes.using(lane_id, principal_kind="flow", holder=holder, mode=RUN,
                                   purpose=f"flow run {run_id}")
            except TypeError:  # a lane manager from before shared lanes
                pass
        return lanes.using(lane_id, principal_kind="flow")
    except TypeError:  # a lane manager from before take over: no principal_kind
        return lanes.using(lane_id)


def driver_factory(runtime: Any, *, interaction: Any = None, dwell_ms: tuple[int, int] = DWELL_MS) -> DriverFactory:
    """lane id -> async context manager: hold the lane as a flow principal (so take over
    applies), yield a PlaywrightFlowDriver on its page. Register it as the runtime service
    "flow_driver", or pass it to runner.set_driver_factory."""

    @asynccontextmanager
    async def open_lane(lane_id: str):
        lanes = runtime.require("lanes")
        gate = control_gate(lanes, lane_id)
        gate()
        async with AsyncExitStack() as stack:
            try:
                page = await stack.enter_async_context(_using(lanes, lane_id))
            except (LaneTakenOver, LaneAgentPaused):
                raise
            except HumanInControl as e:
                raise LaneTakenOver(e.message) from e
            except AgentPaused as e:
                raise LaneAgentPaused(e.message) from e
            guard = egress.guard_for(page)
            if guard is not None:
                await stack.enter_async_context(guard.flow(page))
                from agentos.node.commitgate import flow_identity_check

                check = flow_identity_check(runtime, lane_id, guard)  # who its publishes act as (AUD-01)
                if check is not None:
                    await stack.enter_async_context(guard.flow_identity(check))
            read = getattr(lanes, "read_signed_out", None)
            yield PlaywrightFlowDriver(page, lane_id=lane_id, files_dir=inbox_dir(runtime, lane_id),
                                       interaction=interaction or runtime.get("interaction"), gate=gate,
                                       files=runtime.get("files"), dwell_ms=dwell_ms,
                                       signed_out=functools.partial(read, lane_id) if read else None)

    return open_lane


class TakeOverPause:
    """Wraps the runner's flow executor: a run stopped by take over (or by a pause) in the
    middle defers, to continue after the hand back, instead of failing. The stop is raised
    before the step's input, so the step did not happen and runs again on resume."""

    def __init__(self, inner: Any):
        self.inner = inner

    async def execute(self, run: Any, journal: Any, control: Any) -> Any:
        outcome = await self.inner.execute(run, journal, control)
        err = outcome.error or {}
        if (outcome.action in ("finish", "retry", "decide") and err.get("code") in TAKE_OVER_CODES
                and err.get("acted") is not True):
            reason = err.get("message") or "a human has control of the lane"
            return dataclasses.replace(outcome, action="defer", status=None, error=None, reason=reason, path=None)
        return outcome


def install_take_over_pause(runner: Any) -> None:
    """Make take over a pause for flow runs on this runner (idempotent)."""
    from agentos.runner.models import RunKind

    inner = runner.executors.get(RunKind.FLOW)
    if inner is not None and not isinstance(inner, TakeOverPause):
        runner.register_executor(RunKind.FLOW, TakeOverPause(inner))


# ---------------------------------------------------------------- the recorder's locator


class _Builder:
    """Builds and verifies structural locators on one page (uses the driver's resolver)."""

    def __init__(self, page: Page):
        self.driver = PlaywrightFlowDriver(page, lane_id="rec/rec", dwell_ms=(0, 0))

    async def candidates(self, loc: Locator) -> list[str] | None:
        try:
            return [m.path for m in await self.driver._resolve_all(loc)]
        except (ElementNotFound, AmbiguousLocator):
            return None

    async def verify(self, loc: Locator, path: str) -> bool:
        try:
            return (await self.driver._resolve_one(loc)).path == path
        except (ElementNotFound, AmbiguousLocator):
            return False

    async def best_in(self, anchor: Locator | None, path: str, role: str,
                      name: str | None) -> tuple[Locator | None, bool]:
        """The most durable name variant that leads back to `path` in this scope, and whether it
        needs no nth. A unique match wins; otherwise the variant with the fewest equal matches."""
        best: Locator | None = None
        best_count = 0
        for value, match in name_variants(name):
            loc = Locator(role=role, name=value, match=match, within=anchor)
            found = await self.candidates(loc)
            if not found or path not in found:
                continue
            if len(found) > 1:
                loc = loc.model_copy(update={"nth": found.index(path)})
            if not await self.verify(loc, path):
                continue
            if len(found) == 1:
                return loc, True
            if best is None or len(found) < best_count:
                best, best_count = loc, len(found)
        return best, False

    async def build(self, path: str, role: str, name: str | None, ancestors: list[dict[str, Any]],
                    depth: int = 0) -> Locator | None:
        """A verified locator for the element at `path`. The target is tried within its nearest
        named anchor first (a post's Like, never a comment's), then unnamed ones (a landmark, a
        list item); an anchor itself is tried on the whole page first and nested in its own
        anchors only when it repeats. nth only as a last resort."""
        chains = [ancestors[i:] for i in range(len(ancestors))] if depth < MAX_ANCHOR_DEPTH else []
        chains.sort(key=lambda c: not c[0]["name"])  # named anchors first, each group nearest first
        scopes = [[], *chains] if depth > 0 else [*chains, []]
        fallback: Locator | None = None
        for chain in scopes:
            anchor: Locator | None = None
            if chain:
                anc = chain[0]
                anchor = await self.build(anc["path"], anc["role"], anc["name"] or None, chain[1:], depth + 1)
                if anchor is None:
                    continue
            loc, unique = await self.best_in(anchor, path, role, name)
            if loc is not None and unique:
                return loc
            if loc is not None and fallback is None:
                fallback = loc
        return fallback

    async def css_fallback(self, target: PwLocator, path: str) -> Locator | None:
        """For an element without a usable role: a stable test id, id or form field name."""
        info = await target.evaluate(_STABLE_ATTRS_JS, timeout=EVAL_TIMEOUT_MS)
        options = []
        if info.get("testid"):
            options.append(f'[data-testid="{_css_string(info["testid"])}"]')
        if info.get("id") and re.fullmatch(r"[A-Za-z][\w-]*", info["id"]) and not re.search(r"\d{3,}", info["id"]):
            options.append(f"#{info['id']}")
        if info.get("name") and info.get("tag") in ("input", "select", "textarea", "button"):
            options.append(f'{info["tag"]}[name="{_css_string(info["name"])}"]')
        for css in options:
            loc = Locator(css=css)
            if await self.verify(loc, path):
                return loc
        return None


_STABLE_ATTRS_JS = """
(el) => ({id: el.id || '', testid: el.getAttribute('data-testid') || '', name: el.getAttribute('name') || '',
          tag: el.localName})
"""


def _css_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def structural_locator_for(page: Page, ref: str, *, snapshot: str | None = None) -> Locator:
    """A durable locator for the element a snapshot ref points at (role, accessible name,
    nth among equal matches, the nearest anchor such as a named article or a landmark),
    verified by resolving it back to the same element. `snapshot` (the outline the ref came
    from) is optional: it saves a read of the element's own role and name."""
    value = (ref or "").strip().strip("[]").strip().removeprefix("ref=")
    target = page.locator(f"aria-ref={value}")
    if await target.count() != 1:
        raise RefNotFound(f"ref {ref} is not on the page (stale after a navigation or a newer snapshot). "
                          "Take a new snapshot.")
    path = await target.evaluate(PATH_JS, timeout=EVAL_TIMEOUT_MS)
    role, name = ref_role_and_name(snapshot or "", value)
    if role is None:
        # Any aria snapshot replaces the page's ref map, and only an AI snapshot keeps the refs of
        # unchanged elements, so this is the one way to read Playwright's role and name without
        # breaking the caller's ref (an element's own aria_snapshot() would).
        role, name = ref_role_and_name(await page.aria_snapshot(mode="ai", timeout=SNAPSHOT_TIMEOUT_MS), value)
        if await target.count() != 1 or await target.evaluate(PATH_JS, timeout=EVAL_TIMEOUT_MS) != path:
            raise RefNotFound(f"ref {ref} changed while its locator was being built. Take a new snapshot.")
    guessed = str(await target.evaluate(ROLE_JS, timeout=EVAL_TIMEOUT_MS) or "")
    ancestors = await target.evaluate(_ANCESTORS_JS, list(ANCHOR_ROLES), timeout=EVAL_TIMEOUT_MS)
    builder = _Builder(page)
    for r in dict.fromkeys(x for x in (role, guessed) if x and x not in NO_NEST_ROLES and x != "text"):
        loc = await builder.build(path, r, name if r == role else None, ancestors)
        if loc is not None:
            return loc
    loc = await builder.css_fallback(target, path)
    if loc is not None:
        return loc
    raise InvalidRequest(f"could not build a durable locator for ref {ref}: it has no role and name that "
                         "lead back to it. Record a step on a nearby control instead.")


# ---------------------------------------------------------------- the recorder hook

_RECORDED = {"click": "click", "type": "type", "fill": "fill", "select": "select", "press": "press",
             "scroll": "scroll"}


def _recorder(runtime: Any) -> Any:
    flows = runtime.get("flows") if runtime is not None else None
    return getattr(flows, "recorder", None)


async def _handle_state(handle: Handle | None) -> dict[str, Any] | None:
    if handle is None or not await quietly(handle.evaluate(_IS_CONNECTED_JS), False):
        return None
    data = await quietly(handle.evaluate(STATE_JS, {"exclude": [], "maxText": 2000, "maxAttrs": MAX_ATTRS}))
    if not data:
        return None
    return {"attributes": data["attrs"], "value": None if data.get("sensitive") else data["value"],
            "text": data["text"][:300], "sensitive": bool(data.get("sensitive"))}


async def record_act(runtime: Any, lane_id: str, page: Page, ref: str, action: str, text: str | None,
                     perform: Callable[[], Awaitable[Any]]) -> Any:
    """Run one browser act; while a recording is active on the lane, also push an action event
    with the target's structural locator (built before acting, while the ref resolves) and its
    own state before and after. Never changes what the act does or returns."""
    recorder = _recorder(runtime)
    kind = str(getattr(action, "value", action))
    if recorder is None or kind not in _RECORDED or not recorder.active(lane_id):
        return await perform()
    locator: Locator | None = None
    handle = None
    before = None
    try:
        locator = await structural_locator_for(page, ref)
        handle = await page.locator(f"aria-ref={ref}").element_handle(timeout=EVAL_TIMEOUT_MS)
        before = await _handle_state(handle)
    except Exception as e:  # noqa: BLE001 - the act still runs; the recorder drops the step
        log.info("recorder: no structural locator for %s: %s", ref, _first_line(e))
    result = await perform()
    after = await _handle_state(handle)
    event: dict[str, Any] = {
        "action": _RECORDED[kind], "url": getattr(result, "url", page.url), "ok": bool(getattr(result, "ok", True)),
        "locator": locator.model_dump(exclude_none=True, exclude_defaults=True) if locator else None,
        "sensitive": bool((before or {}).get("sensitive")),
        "state_before": before, "state_after": after,
    }
    if kind in ("type", "fill"):
        event["text"] = text or ""
    elif kind == "select":
        event["option"] = text or ""
    elif kind == "press":
        event["key"] = text or ""
    try:
        recorder.observe(lane_id, event)
    except Exception:
        log.exception("recorder: could not store the event")
    if handle is not None:
        await quietly(handle.dispose())
    return result


def record_navigate(runtime: Any, lane_id: str, url: str) -> None:
    """Push a navigate event while a recording is active on the lane."""
    recorder = _recorder(runtime)
    if recorder is not None and recorder.active(lane_id):
        recorder.observe(lane_id, {"action": "navigate", "url": url, "ok": True})
