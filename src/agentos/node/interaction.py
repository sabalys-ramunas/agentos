"""Browser interaction for one page: navigate, snapshot, act, screenshot, signal.

Implements `agentos.node.interfaces.Interaction` on the Playwright async API (Camoufox).

- Refs come from Playwright's AI snapshot (see snapshot.py) and resolve with the `aria-ref`
  selector, which matches nothing once the ref is stale.
- Every action is human input from human.py (wheel scroll, pause, random point, real press,
  typing rhythm). No `locator.click()` / `fill()` shortcuts, no synthetic DOM events.
- Read-back (PoC lesson 1): the target's own state is captured before and after the action
  (aria-pressed, aria-checked, aria-expanded, value, selected option, accessible name...).
  `changed` reflects only that element, never a label somewhere nearby.
- `signal` is deterministic (sites.py has the table and the rules): the URL, the main document's
  HTTP status, challenge frames, scripts and marker elements, and the lane's session cookies, read
  with `context.cookies(url)` (read-only, invisible to the page). It never reads page text. Every
  navigate, act, upload and snapshot result carries the signal, a `signal_detail` naming the rule
  that matched, and `account` (whose session this is) when the site shows a user id.
- A lane manager binds each lane's context to its LaneSpec (`bind_lane`), so `logged_out` and
  `wrong_account` know what the lane expects; the latest report per context is kept for it to read
  when a lane use ends (`take_report`), which is where a hold is decided (holds.py).
- Nothing here mutates the page. Our scripts only read, and Camoufox runs them in an
  isolated world the page cannot see.
- Dialogs never freeze a lane. A `DialogGuard` attached to the lane's context owns every
  alert, confirm, prompt and beforeunload: it queues them per page, the agent answers with
  browser_dialog_answer, and one left unanswered for `timeout` seconds is answered for it
  (alert accepted; confirm, prompt and beforeunload dismissed) and recorded. While a dialog is
  open the page's scripts are blocked (evaluate, snapshot, screenshot and input all hang), so
  every page call here races "a dialog opened": an action that opens one returns at once and
  says so, and snapshot shows the dialog instead of hanging. The guard also intercepts native
  file choosers (they never appear on the display; `upload` answers them).
- `upload` attaches files from the lane inbox to a file input, either the input itself or the
  button that opens its chooser, and reads back what the input holds (and what the page's own
  change event saw, for widgets that clear the input after reading it).
- `wait` holds the lane for a bounded time until conditions hold together: a text appears or
  goes away in the accessibility tree, a ref reaches a state, the network goes quiet, the URL
  matches. It only reads. Text waits re-read the tree the way a snapshot does (refs of
  unchanged elements stay valid); ref, network and URL waits never touch the refs. A dialog
  that opens stops the wait and is reported; a challenge signal, a human taking the lane or the
  tab closing stop it too.
"""

from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import json
import logging
import math
import mimetypes
import socket
import time
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel

from agentos.core.errors import AgentosError, EngineError, InvalidRequest, NotFound, RefNotFound
from agentos.core.logs import url_for_log
from agentos.core.models import (
    ActionKind,
    ActionResult,
    LaneSpec,
    NavigateResult,
    Signal,
    SnapshotResult,
    utcnow,
)
from agentos.core.signals import STOP
from agentos.node import human
from agentos.node.human import quietly
from agentos.node.sites import (
    MARKER_SELECTORS,
    MIN_VISIBLE_PX,
    Frame,
    LaneExpectation,
    PageFacts,
    SignalReport,
    classify_page,
    classify_signal,  # noqa: F401 - re-exported for callers of the older API
    expectation_for,
    is_challenge_frame,
)
from agentos.node.snapshot import (
    MAX_CHARS,
    REF_RE,
    SNAPSHOT_TIMEOUT_MS,
    find_text,
    outline_texts,
    take_snapshot,
)

log = logging.getLogger("agentos.interaction")

Page = Any           # playwright.async_api.Page
ElementHandle = Any  # playwright.async_api.ElementHandle

NAVIGATE_TIMEOUT_MS = 45_000
RESOLVE_TIMEOUT_MS = 2_000
SCREENSHOT_TIMEOUT_MS = 15_000
READBACK_TIMEOUT_MS = 2_500
LINK_START_S = 4.0  # a followed link that starts no navigation by then was taken by a script
LINK_QUIET_S = 0.5   # then no request for this long: the script is done, so a goto cannot race it
LINK_SETTLE_S = 6.0  # at most this long waiting for that quiet
READBACK_POLL_MS = 200
READBACK_SETTLE_MS = 300
CHOOSER_TIMEOUT_MS = 5_000
UPLOAD_READBACK_MS = 2_000
NEW_TAB_SETTLE_MS = 3_000
DIALOG_TIMEOUT = 60.0
DIALOG_HISTORY = 20
WAIT_MAX_S = 60.0          # a wait never holds the lane longer than this
WAIT_POLL_MS = 400         # between checks; a page whose tree takes longer to read is read less often
WAIT_SIGNAL_EVERY_S = 2.0  # how often a wait re-reads the page's signal
LONG_REQUEST_S = 20.0      # a request open longer than this is a stream or a long poll, not page work
NET_TRACKED = 500          # requests remembered per page
SETTLE_QUIET_MS = 500
SETTLE_MAX_S = 2.5
READY_STATE_S = 2.0
WAIT_REPORT_S = 5.0        # the signal read that ends a wait, bounded like the wait itself
WAIT_READ_GRACE_S = 0.5    # a read a wait makes may finish this long after the wait's deadline
_UNANSWERED = object()     # a read the page did not answer in time (not the same as "gone")

ALLOWED_SCHEMES = ("http", "https")
_DIALOG_OPEN = "a dialog is open, so the page cannot be read until it is answered"

# The target's own state. Password values are never read back, only their length.
_STATE_JS = """
(el) => {
  const s = {};
  for (const n of ['aria-pressed', 'aria-checked', 'aria-expanded', 'aria-selected', 'aria-current',
                   'aria-disabled', 'aria-valuenow']) {
    const v = el.getAttribute(n);
    if (v !== null) s[n] = v;
  }
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'input' && (type === 'checkbox' || type === 'radio')) {
    s.checked = String(el.checked);
  } else if (tag === 'input' && type === 'password') {
    s['value length'] = String(el.value.length);
  } else if (tag === 'input' || tag === 'textarea') {
    s.value = el.value;
  } else if (tag === 'select') {
    s.selected = Array.from(el.selectedOptions, (o) => (o.label || o.textContent || '').trim()).join(', ');
  } else if (el.isContentEditable) {
    s.text = (el.innerText || el.textContent || '').trim();
  }
  if ('disabled' in el && el.disabled) s.disabled = 'true';
  const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();
  const byIds = (ids) => clean(ids.split(/\\s+/).map((id) => {
    const node = el.getRootNode().getElementById ? el.getRootNode().getElementById(id) : document.getElementById(id);
    return node ? node.textContent : '';
  }).join(' '));
  let name = '';
  if (el.getAttribute('aria-labelledby')) name = byIds(el.getAttribute('aria-labelledby'));
  if (!name) name = clean(el.getAttribute('aria-label'));
  if (!name && el.labels && el.labels.length) name = clean(Array.from(el.labels, (l) => l.textContent).join(' '));
  if (!name && tag === 'img') name = clean(el.getAttribute('alt'));
  if (!name && !['input', 'textarea', 'select'].includes(tag)) name = clean(el.innerText || el.textContent);
  if (!name) name = clean(el.getAttribute('title') || el.getAttribute('placeholder'));
  s.name = name.slice(0, 200);
  return s;
}
"""

# Page facts for the signal (sites.PageFacts): iframe sources (absolute, via the src property) with
# whether they render at a real size on the page (not hidden, not transparent, not parked off the page
# like reCAPTCHA's idle challenge frame), script sources, and which marker elements (by id, passed in
# from sites.MARKER_SELECTORS) exist and show. Reads only; never the page's text.
_SHOWN_JS = """
(el, minPx) => {
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width >= minPx && r.height >= minPx && st.visibility !== 'hidden' && st.display !== 'none'
    && Number(st.opacity) > 0.05 && r.right + window.scrollX > 0 && r.bottom + window.scrollY > 0;
}
""".strip()
_MARKERS_JS = """
([selectors, minPx]) => {
  const shown = (el) => (SHOWN)(el, minPx);
  const frames = Array.from(document.querySelectorAll('iframe[src], frame[src]')).slice(0, 300)
    .map((f) => ({src: f.src, visible: shown(f)}));
  const scripts = Array.from(document.querySelectorAll('script[src]')).slice(0, 300).map((s) => s.src);
  const elements = {};
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) elements[sel] = shown(el);
  }
  return {frames, scripts, elements};
}
""".replace("SHOWN", _SHOWN_JS)

# Read-back keys in the order they are reported.
_STATE_ORDER = ("aria-pressed", "aria-checked", "checked", "aria-selected", "selected", "aria-expanded",
                "value", "value length", "text", "aria-valuenow", "aria-current", "aria-disabled",
                "disabled", "name")
_QUOTED = {"value", "text", "selected", "name"}


def normalize_ref(ref: str) -> str:
    """Accept `e12`, `ref=e12` or `[ref=e12]`; reject anything else."""
    value = (ref or "").strip().strip("[]").strip().removeprefix("ref=")
    if not REF_RE.match(value):
        raise InvalidRequest(f"not a snapshot ref: {ref!r} (refs look like e12)")
    return value


def _ip_of(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a browser would use for a literal host, including legacy spellings that
    browsers accept for IPv4 (2130706433, 0x7f.1, 0177.0.0.1, 127.1)."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def is_private_target(host: str) -> bool:
    """The node itself or a private network: loopback, private, link-local, unspecified,
    reserved, multicast, or a *.localhost name."""
    host = (host or "").strip().lower().rstrip(".")
    if host in ("localhost", "") or host.endswith(".localhost"):
        return True
    ip = _ip_of(host)
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_reserved \
        or ip.is_multicast


# Browser lanes whose every request leaves from an agent environment's VM (agentos.environments: a
# SOCKS tunnel, DNS included, localhost not bypassed). For them localhost and private addresses are
# the VM's, not the node's, so a page there may open them. Only the environments service adds to it.
VM_PROXIED_LANES: set[str] = set()


def through_vm(page: Any) -> bool:
    """Is this page in a browser lane whose traffic leaves from an environment's VM?"""
    from agentos.node.egress import guard_for

    lane = getattr(guard_for(page), "lane_id", None)
    return isinstance(lane, str) and lane in VM_PROXIED_LANES


def check_url(url: str, *, allow_private: bool = False) -> str:
    """Only web pages: http(s) and about:blank. No file:, data:, javascript: or browser internals,
    and (unless allowed, for local test fixtures) nothing on the node or its private network."""
    value = (url or "").strip()
    if value == "about:blank":
        return value
    parts = urlsplit(value)
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.netloc:
        raise InvalidRequest(f"only http and https URLs can be opened: {url!r}")
    if not allow_private and is_private_target(parts.hostname or ""):
        raise InvalidRequest(
            f"lanes cannot open addresses on the node or its private network: {parts.hostname}")
    return value


def is_challenge_url(url: str) -> bool:
    """Frames that only a challenge page or a solvable captcha widget loads (sites.VENDORS)."""
    return is_challenge_frame(url)


def _show(key: str, value: str | None) -> str:
    if value is None:
        return "(none)"
    if key in _QUOTED:
        if len(value) > 60:
            value = value[:57] + "..."
        return json.dumps(value, ensure_ascii=False)
    return value


def diff_state(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Human-readable changes of the target's own state, e.g. `aria-pressed false -> true`."""
    keys = [k for k in _STATE_ORDER if k in before or k in after]
    keys += sorted(k for k in set(before) | set(after) if k not in _STATE_ORDER)
    return [f"{k} {_show(k, before.get(k))} -> {_show(k, after.get(k))}"
            for k in keys if before.get(k) != after.get(k)]


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or port is None:
        return None
    return parts.scheme, parts.hostname.lower(), port


def same_origin(a: str, b: str) -> bool:
    """Both are web addresses of one origin (scheme, host and port)."""
    origin = _origin(a)
    return origin is not None and origin == _origin(b)


# The link a person would click to reach `target`: an <a href> of the main document whose address,
# without its fragment, is the target's; shown (not hidden, transparent or tiny), inside the
# viewport's width (the pointer can only wheel up and down to it), opening in this tab (its own
# target, or else the document's <base target>), not a download and with no inline onclick (a
# script that takes the click could do something else). An exact match before one that differs in
# its fragment, then the one in view or nearest to it. _link_leads_to checks it again in Python
# right before the press.
_LINK_TO_JS = """
(target) => {
  const parse = (u) => { try { return new URL(u, document.baseURI); } catch (e) { return null; } };
  const want = parse(target);
  if (!want) return null;
  const vw = window.innerWidth, vh = window.innerHeight;
  const base = document.querySelector('base[target]');
  const baseTarget = base ? base.getAttribute('target') : '';
  let best = null, bestScore = Infinity;
  for (const a of document.querySelectorAll('a[href]')) {
    if (typeof a.href !== 'string' || a.hasAttribute('download') || a.hasAttribute('onclick')) continue;
    const t = (a.hasAttribute('target') ? a.getAttribute('target') : baseTarget).trim().toLowerCase();
    if (t && t !== '_self' && t !== '_top') continue;
    const url = parse(a.href);
    if (!url || url.href !== want.href) continue;
    if (a.checkVisibility && !a.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    const r = a.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.right <= 0 || r.left >= vw) continue;
    const off = r.bottom < 0 ? -r.bottom : (r.top > vh ? r.top - vh : 0);
    if (off < bestScore) { best = a; bestScore = off; }
  }
  return best;
}
"""


# Where a link leads, read right before the press (the page may have changed it since it was found):
# what _link_leads_to decides on. `want` is the target as the browser writes it, without fragment.
_LINK_FACTS_JS = """
(a, target) => {
  let want = null;
  try { want = new URL(target, document.baseURI).href; } catch (e) {}
  const base = document.querySelector('base[target]');
  return {
    want,
    connected: a.isConnected,
    href: typeof a.href === 'string' ? a.href : '',
    target: a.hasAttribute('target') ? a.getAttribute('target') : null,
    baseTarget: base ? base.getAttribute('target') : null,
    download: a.hasAttribute('download'),
    onclick: a.hasAttribute('onclick'),
  };
}
"""


def _link_leads_to(facts: Any) -> str | None:
    """The address a link with these facts (_LINK_FACTS_JS) opens in this tab, when that is exactly
    the target, fragment included; None when it leads anywhere else, opens another tab (its own target or
    the document's <base target>), downloads, carries an inline onclick, left the page, or could not
    be read."""
    if not isinstance(facts, dict) or not facts.get("connected") or facts.get("download") \
            or facts.get("onclick"):
        return None
    want = facts.get("want")
    where = facts.get("target")
    if where is None:
        where = facts.get("baseTarget")
    if not isinstance(want, str) or not want or str(where or "").strip().lower() not in ("", "_self", "_top"):
        return None
    return want if str(facts.get("href") or "") == want else None


class _LinkChanged(Exception):  # internal control flow: the link no longer leads to the target
    pass


class _Navigations:
    """The main frame's navigations while a link is followed, told apart by where they lead.
    `began` resolves when a navigation request starts (or a script shows the target in the same
    document); `landed` when the latest one commits, or with an EngineError when it fails. `ours`:
    it started at the target (fragment aside) or committed there. A same-document address change to
    anything else (a script tidying the address bar) is not the link's navigation and is ignored."""

    def __init__(self, page: Page, target: str, want: str):
        loop = asyncio.get_running_loop()
        self.page, self.main, self.target, self.want = page, page.main_frame, target, want
        self.began: asyncio.Future[None] = loop.create_future()
        self.landed: asyncio.Future[None] = loop.create_future()
        self.chain: list[Any] = []  # the latest navigation's requests, redirects included
        self.start = ""
        self.ours = False

    def on_request(self, request: Any) -> None:
        if self.landed.done() or not _is_main_navigation(request, self.main):
            return
        try:
            url, previous = str(request.url), request.redirected_from
        except Exception:  # noqa: BLE001
            return
        if previous is not None and any(previous is r for r in self.chain):
            self.chain.append(request)  # a redirect: the same navigation, now somewhere else
            return
        self.chain, self.start = [request], url  # a new navigation replaces one still in flight
        if not self.began.done():
            self.began.set_result(None)

    def on_navigated(self, frame: Any) -> None:
        if frame != self.main or self.landed.done():
            return
        url = _strip_fragment(_url_of(self.page))
        if self.chain:
            if url != _strip_fragment(str(getattr(self.chain[-1], "url", ""))):
                return  # not the commit of the navigation in flight (a pushState while it loads)
            self.ours = _strip_fragment(self.start) == self.want or url == self.want
        elif url == self.want:
            self.ours = True  # a script's router showed the target in this document, as a person sees it
            if not self.began.done():
                self.began.set_result(None)
        else:
            return
        self.landed.set_result(None)

    def on_failed(self, request: Any) -> None:
        if self.landed.done() or not any(request is r for r in self.chain):
            return
        self.ours = _strip_fragment(self.start) == self.want
        self.landed.set_exception(EngineError(f"navigation to {self.target} failed: "
                                              f"{getattr(request, 'failure', None) or 'the request failed'}"))


@dataclass(slots=True)
class _PageState:
    status: int | None = None
    status_url: str | None = None
    inflight: dict[Any, float] = field(default_factory=dict)  # request -> when it started (monotonic)
    last_request: float = 0.0  # when a request last started or ended

    def request_started(self, request: Any) -> None:
        if _resource_type(request) in _STREAMS:
            return
        now = time.monotonic()
        self.inflight[request] = self.last_request = now
        while len(self.inflight) > NET_TRACKED:
            self.inflight.pop(next(iter(self.inflight)))

    def request_ended(self, request: Any) -> None:
        if self.inflight.pop(request, None) is not None:
            self.last_request = time.monotonic()

    def network(self, now: float | None = None) -> tuple[int, float]:
        """(requests in flight that count, seconds since a request started or ended). Requests
        open longer than LONG_REQUEST_S do not count: streams and long polls never end."""
        now = time.monotonic() if now is None else now
        busy = sum(1 for started in self.inflight.values() if now - started < LONG_REQUEST_S)
        return busy, (now - self.last_request) if self.last_request else math.inf


_STREAMS = frozenset({"eventsource", "websocket", "media"})


def _resource_type(request: Any) -> str:
    try:
        return str(request.resource_type)
    except Exception:  # noqa: BLE001
        return ""


# Per page, not per PlaywrightInteraction, so a page never gets a second response listener.
_PAGES: weakref.WeakKeyDictionary[Any, _PageState] = weakref.WeakKeyDictionary()


# ---- lanes: what a context expects, and its latest signal ---------------------------------------

# Keyed by browser context (one per lane), so popups and new tabs share their lane's expectation.
_EXPECT: weakref.WeakKeyDictionary[Any, LaneExpectation] = weakref.WeakKeyDictionary()
_REPORTS: weakref.WeakKeyDictionary[Any, SignalReport] = weakref.WeakKeyDictionary()


def _context_of(page_or_context: Any) -> Any:
    return getattr(page_or_context, "context", page_or_context)


def bind_lane(page_or_context: Any, spec: LaneSpec | LaneExpectation | None) -> None:
    """Tell the signal which lane a browser context belongs to: the sites it should be signed into
    and the pinned account (sites.expectation_for). Call it again when the spec changes; None unbinds.
    An unbound context still gets every signal except logged_out from a missing session."""
    context = _context_of(page_or_context)
    try:
        if spec is None:
            _EXPECT.pop(context, None)
        else:
            _EXPECT[context] = spec if isinstance(spec, LaneExpectation) else expectation_for(spec)
    except TypeError:  # not weak-referenceable (a test double)
        log.debug("cannot bind %r to a lane", context)


def lane_expectation(page_or_context: Any) -> LaneExpectation | None:
    try:
        return _EXPECT.get(_context_of(page_or_context))
    except TypeError:
        return None


def last_report(page_or_context: Any) -> SignalReport | None:
    """The latest signal computed for this lane's context (by any tool, flow or login step)."""
    try:
        return _REPORTS.get(_context_of(page_or_context))
    except TypeError:
        return None


def take_report(page_or_context: Any) -> SignalReport | None:
    """The latest signal for this context, cleared, so the next lane use starts fresh. The lane
    manager reads it when a use ends: the page state the use left behind decides a hold, so a login
    step that passes through the login page and ends signed in never holds the lane."""
    try:
        return _REPORTS.pop(_context_of(page_or_context), None)
    except TypeError:
        return None


def _remember(page: Any, report: SignalReport) -> SignalReport:
    """Keep the latest report for the lane manager. An unknown read (a dialog froze the page) never
    replaces a signal already seen in this lane use."""
    try:
        context = _context_of(page)
        if report.signal is not Signal.UNKNOWN or context not in _REPORTS:
            _REPORTS[context] = report
    except TypeError:
        pass
    return report


async def _read_cookies(page: Any, url: str) -> list[dict[str, Any]] | None:
    """The cookies the browser would send to `url`, from the context (the page cannot see this)."""
    try:
        return await page.context.cookies([url])
    except Exception:  # noqa: BLE001 - a closed context, or a page double with no cookie jar
        return None


# ---- dialogs and file choosers ------------------------------------------------------------

class DialogPending(AgentosError):
    """A page dialog (alert, confirm, prompt, beforeunload) waits for an answer and the page is
    frozen until it gets one. Answer it with browser_dialog_answer."""
    code = "dialog_pending"


# What a dialog does when nobody answers it in time: alerts are acknowledged, everything that
# asks a question gets the safe answer (do not delete, do not leave the page).
AUTO_ACCEPT = frozenset({"alert"})


@dataclass(eq=False)
class PageDialog:
    id: str
    type: str                      # alert | confirm | prompt | beforeunload
    message: str
    default: str | None
    url: str
    opened_at: datetime
    deadline: float                # event loop time of the automatic answer
    page: Any = field(repr=False)
    dialog: Any = field(repr=False)
    outcome: str | None = None     # accepted | dismissed | gone
    answered_by: str | None = None  # agent | timeout | tab_closed | lane_closed
    answered_at: datetime | None = None
    text: str | None = None        # the text a prompt was answered with
    error: str | None = None

    @property
    def pending(self) -> bool:
        return self.outcome is None

    @property
    def automatic(self) -> str:
        return "accepted" if self.type in AUTO_ACCEPT else "dismissed"

    def seconds_left(self) -> int:
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return 0
        return max(0, round(self.deadline - now))

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "type": self.type, "message": self.message, "url": self.url,
                               "opened_at": self.opened_at.isoformat()}
        if self.type == "prompt":
            out["default"] = self.default or ""
        if self.pending:
            out["auto_answer"] = f"{self.automatic} in {self.seconds_left()} s if nobody answers"
        else:
            out.update(outcome=self.outcome, answered_by=self.answered_by,
                       answered_at=self.answered_at.isoformat() if self.answered_at else None)
            if self.text is not None:
                out["text"] = self.text
            if self.error:
                out["error"] = self.error
        return out


def dialog_notice(d: PageDialog, where: str = "on this tab") -> str:
    """The one line agents see in a snapshot or an error while a dialog is open."""
    what = "asks to confirm leaving the page" if d.type == "beforeunload" else "is open"
    return (f"- {d.type} dialog {what} {where}: {json.dumps(d.message[:300], ensure_ascii=False)} "
            f"(answer with browser_dialog_answer; {d.automatic} automatically in {d.seconds_left()} s)")


def _pending_error(d: PageDialog, doing: str) -> DialogPending:
    if d.type == "beforeunload":
        how = ("accept=true leaves the page (unsaved input is lost; a navigation that was asked for then "
               "continues), accept=false stays")
    elif d.type == "prompt":
        how = "accept=true with text answers it, accept=false cancels"
    elif d.type == "confirm":
        how = "accept=true confirms, accept=false cancels"
    else:
        how = "accept=true acknowledges it"
    return DialogPending(f"cannot {doing}: {_a(d.type)} {d.type} dialog is open on this tab "
                         f"({json.dumps(d.message[:300], ensure_ascii=False)}) and the page is frozen until it is "
                         f"answered. Call browser_dialog_answer: {how}. Unanswered, it is {d.automatic} "
                         f"automatically in {d.seconds_left()} s.")


def _a(word: str) -> str:
    return "an" if word[:1] in "aeiou" else "a"


class _DialogOpened(Exception):  # internal control flow, never leaves this module
    def __init__(self, dialog: PageDialog):
        super().__init__(dialog.id)
        self.dialog = dialog


@dataclass
class _Chooser:
    count: int = 0
    chooser: Any = None


class DialogGuard:
    """Owns every dialog and file chooser of one browser context (one lane)."""

    def __init__(self, *, timeout: float = DIALOG_TIMEOUT, history: int = DIALOG_HISTORY):
        self.timeout = timeout
        self._pending: list[PageDialog] = []
        self._history: deque[PageDialog] = deque(maxlen=history)
        self._waiters: list[tuple[Any, asyncio.Future]] = []
        self._timers: set[asyncio.Task] = set()
        self._choosers: dict[Any, _Chooser] = {}
        self._watched: set[Any] = set()
        self._seq = 0
        self._closed = False

    def attach(self, context: Any) -> DialogGuard:
        """Start owning the context's dialogs. A context gets one guard; attaching again returns it."""
        existing = guard_for_context(context)
        if existing is not None:
            return existing
        context.on("dialog", self._on_dialog)
        context.on("page", self._watch_page)
        for page in list(context.pages):
            self._watch_page(page)
        _GUARDS[context] = self
        return self

    def _watch_page(self, page: Any) -> None:
        if page in self._watched:
            return
        self._watched.add(page)
        page.on("close", lambda *_: self._on_page_closed(page))
        # A filechooser listener makes Playwright intercept the chooser, so the native picker never
        # opens on the lane's display (where it would sit until a human closed it).
        page.on("filechooser", lambda chooser: self._on_chooser(page, chooser))

    # ---- events ------------------------------------------------------------------------

    def _on_dialog(self, dialog: Any) -> None:
        loop = asyncio.get_running_loop()
        page = dialog.page
        self._seq += 1
        dtype = str(dialog.type)
        entry = PageDialog(id=f"d{self._seq}", type=dtype, message=str(dialog.message or "")[:2000],
                           default=(dialog.default_value or "") if dtype == "prompt" else None,
                           url=_url_of(page), opened_at=utcnow(), deadline=loop.time() + self.timeout,
                           page=page, dialog=dialog)
        if self._closed:
            self._spawn(self._answer(entry, accept=dtype in AUTO_ACCEPT, text=None, by="lane_closed"))
            return
        self._pending.append(entry)
        log.info("dialog %s (%s) opened on %s: %r", entry.id, dtype, url_for_log(entry.url), entry.message[:120])
        for owner, fut in self._waiters:
            if (owner is page or page is None) and not fut.done():
                fut.set_result(entry)
        self._spawn(self._expire(entry))

    def _on_page_closed(self, page: Any) -> None:
        self._watched.discard(page)
        self._choosers.pop(page, None)
        for entry in [d for d in self._pending if d.page is page]:
            self._settle(entry, "gone", "tab_closed")

    def _on_chooser(self, page: Any, chooser: Any) -> None:
        seen = self._choosers.setdefault(page, _Chooser())
        seen.count += 1
        seen.chooser = chooser
        log.info("file chooser intercepted on %s", _url_of(page))

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    async def _expire(self, entry: PageDialog) -> None:
        loop = asyncio.get_running_loop()
        while entry.pending and loop.time() < entry.deadline:  # extend() may move the deadline
            await asyncio.sleep(entry.deadline - loop.time())
        if entry.pending:
            log.info("dialog %s unanswered for %g s; %s automatically", entry.id, self.timeout, entry.automatic)
            await self._answer(entry, accept=entry.type in AUTO_ACCEPT, text=None, by="timeout")

    def _settle(self, entry: PageDialog, outcome: str, by: str, text: str | None = None) -> None:
        entry.outcome, entry.answered_by, entry.answered_at, entry.text = outcome, by, utcnow(), text
        if entry in self._pending:
            self._pending.remove(entry)
        self._history.append(entry)

    async def _answer(self, entry: PageDialog, *, accept: bool, text: str | None, by: str) -> PageDialog:
        if not entry.pending and entry in self._history:
            return entry
        reply = None
        if accept and entry.type == "prompt":
            reply = text if text is not None else (entry.default or "")
        # settle first, so a timer and an agent can never both answer
        self._settle(entry, "accepted" if accept else "dismissed", by, reply)
        try:
            if accept:
                await (entry.dialog.accept(reply) if reply is not None else entry.dialog.accept())
            else:
                await entry.dialog.dismiss()
        except Exception as e:  # noqa: BLE001 - the tab closed or navigated away meanwhile
            entry.outcome, entry.error = "gone", _first_line(e)
        log.info("dialog %s %s by %s", entry.id, entry.outcome, by)
        return entry

    # ---- queries -----------------------------------------------------------------------

    def pending(self, page: Any = None) -> list[PageDialog]:
        return [d for d in self._pending if page is None or d.page is page]

    def pending_on(self, page: Any) -> PageDialog | None:
        found = self.pending(page)
        return found[0] if found else None

    def current(self, page: Any = None) -> PageDialog | None:
        """The dialog browser_dialog_answer acts on: the active tab's first, else the oldest."""
        return (self.pending_on(page) if page is not None else None) or (self._pending[0] if self._pending else None)

    def history(self, limit: int = 5) -> list[PageDialog]:
        return list(self._history)[-limit:][::-1]

    async def answer(self, page: Any, *, accept: bool, text: str | None = None, by: str = "agent") -> PageDialog:
        entry = self.current(page)
        if entry is None:
            raise NotFound("no dialog is open in this lane")
        return await self._answer(entry, accept=accept, text=text, by=by)

    def extend(self, entry: PageDialog, seconds: float) -> None:
        """Hold off the automatic answer (a human is deciding on an approval for it)."""
        if entry.pending:
            entry.deadline = max(entry.deadline, asyncio.get_running_loop().time() + seconds)

    def chooser_count(self, page: Any) -> int:
        seen = self._choosers.get(page)
        return seen.count if seen else 0

    # ---- racing page calls against dialogs ---------------------------------------------

    def waiter(self, page: Any) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((page, fut))
        return fut

    def discard_waiter(self, fut: asyncio.Future) -> None:
        self._waiters = [(p, f) for p, f in self._waiters if f is not fut]
        if not fut.done():
            fut.cancel()

    async def close(self) -> None:
        """The lane is closing: settle what is open, stop the timers."""
        self._closed = True
        for entry in list(self._pending):
            self._settle(entry, "gone", "lane_closed")
        for task in list(self._timers):
            task.cancel()


# One guard per browser context, so a lane never gets two sets of dialog listeners.
_GUARDS: weakref.WeakKeyDictionary[Any, DialogGuard] = weakref.WeakKeyDictionary()


def guard_for_context(context: Any) -> DialogGuard | None:
    try:
        return _GUARDS.get(context)
    except TypeError:  # not weak-referenceable (a test double)
        return None


def dialog_guard(page: Any) -> DialogGuard | None:
    """The guard of the page's context, if the lane attached one."""
    context = getattr(page, "context", None)
    return guard_for_context(context) if context is not None else None


def _url_of(page: Any) -> str:
    try:
        return str(page.url) if page is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _open_pages(page: Any) -> list[Any]:
    try:
        return [p for p in page.context.pages if not p.is_closed()]
    except Exception:  # noqa: BLE001
        return []


# ---- uploads ------------------------------------------------------------------------------

_FILE_INPUT_JS = """
(el) => {
  const tag = el.tagName.toLowerCase();
  const isFile = tag === 'input' && (el.type || '').toLowerCase() === 'file';
  return {file: isFile, tag, multiple: isFile && !!el.multiple, accept: isFile ? (el.getAttribute('accept') || '') : '',
          disabled: !!el.disabled, connected: el.isConnected};
}
"""

# Watch the page's own change event (in the isolated world; the page cannot see the listener), so
# a widget that reads the files and then clears the input still reads back as attached.
_WATCH_CHANGE_JS = """
(el) => {
  const store = (window.__agentosUploads = window.__agentosUploads || new WeakMap());
  const box = {seen: null};
  store.set(el, box);
  el.addEventListener('change', () => {
    box.seen = Array.from(el.files || [], (f) => ({name: f.name, size: f.size, type: f.type}));
  }, {capture: true, once: true});
}
"""

_READ_FILES_JS = """
(el) => {
  const box = window.__agentosUploads && window.__agentosUploads.get(el);
  return {connected: el.isConnected, files: Array.from(el.files || [], (f) => ({name: f.name, size: f.size, type: f.type})),
          seen: box ? box.seen : null};
}
"""


class UploadResult(BaseModel):
    ok: bool
    ref: str
    url: str
    via: str                                  # "input" (set on the file input) or "chooser" (clicked, then answered)
    files: list[dict[str, Any]]               # what the input holds now: name, size, type
    seen_by_page: list[dict[str, Any]] | None = None  # what the page's change event saw
    signal: Signal = Signal.UNKNOWN
    detail: str | None = None
    signal_detail: str | None = None
    account: dict[str, Any] | None = None


def accept_matches(accept: str, name: str, mime: str) -> bool:
    """Whether a file fits an <input accept="...">: extensions (.jpg), wildcards (image/*) or exact
    types; an empty accept takes anything."""
    tokens = [t.strip().lower() for t in (accept or "").split(",") if t.strip()]
    if not tokens:
        return True
    name, mime = name.lower(), (mime or "").lower()
    aliases = {mime, "image/jpg" if mime == "image/jpeg" else mime}
    for token in tokens:
        if token.startswith("."):
            if name.endswith(token):
                return True
        elif token.endswith("/*"):
            if mime.startswith(token[:-1]):
                return True
        elif token in aliases:
            return True
    return False


def _expected(paths: Sequence[Path], mimes: Sequence[str] | None) -> list[dict[str, Any]]:
    out = []
    for i, p in enumerate(paths):
        mime = (mimes[i] if mimes and i < len(mimes) else None) or mimetypes.guess_type(p.name)[0] or ""
        out.append({"name": p.name, "size": p.stat().st_size, "type": mime})
    return out


def _same_files(got: list[dict[str, Any]] | None, want: list[dict[str, Any]]) -> bool:
    if not got or len(got) != len(want):
        return False
    return all(g.get("name") == w["name"] and g.get("size") == w["size"] for g, w in zip(got, want, strict=True))


def _check_input(info: dict[str, Any], want: list[dict[str, Any]], *, multiple: bool | None = None) -> None:
    if info.get("disabled"):
        raise InvalidRequest("that file input is disabled")
    takes_many = info.get("multiple") if multiple is None else multiple
    if len(want) > 1 and not takes_many:
        raise InvalidRequest(f"that file input takes one file; {len(want)} were given")
    for w in want:
        if not accept_matches(info.get("accept", ""), w["name"], w["type"]):
            raise InvalidRequest(f"that file input accepts {info.get('accept')!r}; {w['name']} ({w['type'] or 'unknown'}) "
                                 "does not fit")


def _sizes(files: list[dict[str, Any]]) -> str:
    return ", ".join(f"{f.get('name')} ({f.get('size')} bytes)" for f in files)


# ---- waits --------------------------------------------------------------------------------

REF_STATES = ("visible", "hidden", "enabled", "disabled", "checked", "unchecked", "pressed", "unpressed",
              "expanded", "collapsed", "selected", "focused", "changed")
STOP_SIGNALS = STOP

# Whether the target renders and has focus. Reads only, in the isolated world.
_VIEW_JS = """
(el) => {
  if (!el.isConnected) return null;
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return {visible: r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none',
          focused: el.ownerDocument.activeElement === el};
}
"""


@dataclass(frozen=True)
class WaitFor:
    """What a wait waits for. Every condition given must hold at the same time."""
    for_text: tuple[str, ...] = ()   # any one of these appears
    gone_text: tuple[str, ...] = ()  # every one of these is gone
    exact: bool = False              # a text must equal a whole name or text, not just be part of one
    ref: str | None = None
    ref_state: str | None = None     # one of REF_STATES
    network_quiet_ms: int | None = None
    url_matches: str | None = None   # part of the URL, or a glob over the whole URL when it has a *
    timeout_s: float = 10.0

    @property
    def reads_tree(self) -> bool:
        return bool(self.for_text or self.gone_text)

    @property
    def empty(self) -> bool:
        return not (self.reads_tree or self.ref or self.network_quiet_ms is not None or self.url_matches)


class WaitResult(BaseModel):
    met: bool
    stopped: str           # met | timeout | dialog | signal | tab_closed | human_in_control | agent_paused
    waited_s: float
    url: str
    conditions: list[dict[str, Any]]
    reread: bool = False   # the tree was re-read like a snapshot: elements that changed have new refs
    dialog: dict[str, Any] | None = None
    signal: Signal = Signal.UNKNOWN
    detail: str | None = None
    signal_detail: str | None = None
    account: dict[str, Any] | None = None


def url_matches(url: str, pattern: str) -> bool:
    """Part of the URL, or a glob over the whole URL when the pattern has a `*`."""
    return fnmatch.fnmatchcase(url, pattern) if "*" in pattern else pattern in url


def ref_state_met(state: str, before: dict[str, str], now: dict[str, str] | None,
                  view: dict[str, Any] | None) -> tuple[bool, str]:
    """Whether the target is in `state`, and what it is now (for the result)."""
    if now is None or view is None:
        return state == "hidden", "gone from the page"
    if state == "changed":
        changes = diff_state(before, now)
        return bool(changes), "; ".join(changes) or "no change in its own state"
    flags = {
        "visible": bool(view.get("visible")),
        "enabled": not (now.get("disabled") == "true" or now.get("aria-disabled") == "true"),
        "checked": now.get("checked") == "true" or now.get("aria-checked") == "true",
        "pressed": now.get("aria-pressed") == "true",
        "expanded": now.get("aria-expanded") == "true",
        "selected": now.get("aria-selected") == "true",
        "focused": bool(view.get("focused")),
    }
    opposite = {"hidden": "visible", "disabled": "enabled", "unchecked": "checked", "unpressed": "pressed",
                "collapsed": "expanded"}
    met = not flags[opposite[state]] if state in opposite else flags[state]
    seen = ["visible" if flags["visible"] else "hidden", "enabled" if flags["enabled"] else "disabled"]
    if "checked" in now or "aria-checked" in now:
        seen.append("checked" if flags["checked"] else "unchecked")
    for key, yes, no in (("aria-pressed", "pressed", "unpressed"), ("aria-expanded", "expanded", "collapsed"),
                         ("aria-selected", "selected", "not selected")):
        if key in now:
            seen.append(yes if flags[yes] else no)
    if flags["focused"]:
        seen.append("focused")
    return met, ", ".join(seen)


class _Watch:
    """What a wait has seen so far, kept outside the loop so a dialog or a timeout that stops the
    loop still reports it."""

    def __init__(self, spec: WaitFor, ref: str | None, before: dict[str, str] | None):
        self.spec, self.before = spec, before or {}
        self.reread = False
        self.conditions: dict[str, dict[str, Any]] = {}
        if spec.for_text:
            self.conditions["for_text"] = {"for_text": list(spec.for_text), "met": False, "seen": None}
        if spec.gone_text:
            self.conditions["gone_text"] = {"gone_text": list(spec.gone_text), "met": False, "seen": None}
        if ref is not None:
            self.conditions["for_ref_state"] = {"for_ref_state": {"ref": ref, "state": spec.ref_state},
                                                "met": False, "seen": None}
        if spec.network_quiet_ms is not None:
            self.conditions["network_quiet_ms"] = {"network_quiet_ms": spec.network_quiet_ms, "met": False,
                                                   "seen": None}
        if spec.url_matches is not None:
            self.conditions["url_matches"] = {"url_matches": spec.url_matches, "met": False, "seen": None}

    def set(self, key: str, met: bool, seen: str | None, **extra: Any) -> None:
        entry = self.conditions[key]
        entry.update(met=met, seen=seen, **extra)

    @property
    def met(self) -> bool:
        return all(c["met"] for c in self.conditions.values())

    def unmet(self) -> str:
        parts = []
        for key, c in self.conditions.items():
            if not c["met"]:
                parts.append(f"{key} not met" + (f" (now: {c['seen']})" if c.get("seen") else ""))
        return "; ".join(parts)


class PlaywrightInteraction:
    """The Interaction protocol for Playwright pages (Camoufox lanes)."""

    def __init__(self, *, snapshot_max_chars: int = MAX_CHARS, navigate_timeout_ms: int = NAVIGATE_TIMEOUT_MS,
                 readback_timeout_ms: int = READBACK_TIMEOUT_MS, allow_private_targets: bool = False):
        self.allow_private_targets = allow_private_targets
        self.snapshot_max_chars = snapshot_max_chars
        self.navigate_timeout_ms = navigate_timeout_ms
        self.readback_timeout_ms = readback_timeout_ms

    # ---- main document status ----------------------------------------------------------

    def _track(self, page: Page) -> _PageState:
        """Remember the HTTP status of the page's main document, including navigations that an
        action (a click, Enter in a search box) starts."""
        state = _PAGES.get(page)
        if state is not None:
            return state
        state = _PageState()
        page_ref = weakref.ref(page)

        def on_response(response: Any) -> None:
            pg = page_ref()
            try:
                main_document = (pg is not None and response.frame == pg.main_frame
                                 and response.request.is_navigation_request())
            except Exception:  # noqa: BLE001 - e.g. service worker responses have no frame
                return
            if main_document:
                state.status, state.status_url = response.status, response.url

        page.on("response", on_response)
        # Plain functions, not bound methods: Playwright keeps a wrapper on a bound method's owner,
        # and a slots dataclass has nowhere to put it.
        page.on("request", lambda request: state.request_started(request))
        page.on("requestfinished", lambda request: state.request_ended(request))
        page.on("requestfailed", lambda request: state.request_ended(request))
        _PAGES[page] = state
        return state

    def _status_for_current(self, page: Page) -> int | None:
        state = _PAGES.get(page)
        if state is None or state.status_url is None:
            return None
        return state.status if _strip_fragment(state.status_url) == _strip_fragment(page.url) else None

    # ---- dialogs -----------------------------------------------------------------------

    async def _race(self, page: Page, work: Awaitable[Any], guard: DialogGuard | None) -> Any:
        """Await `work`, unless a dialog opens on the page first: then stop waiting (the page is
        frozen, so `work` could only hang) and raise _DialogOpened."""
        if guard is None:
            return await work
        waiter = guard.waiter(page)
        task = asyncio.ensure_future(work)
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            raise _DialogOpened(waiter.result())
        finally:
            guard.discard_waiter(waiter)
            if not task.done():
                task.cancel()
                task.add_done_callback(_consume)

    def _header(self, page: Page, guard: DialogGuard | None) -> list[str]:
        """Lines on top of a snapshot: open dialogs (this tab first) and, with several tabs, which one
        this is."""
        lines: list[str] = []
        pages = _open_pages(page)
        if guard is not None:
            for d in sorted(guard.pending(), key=lambda d: d.page is not page):
                where = "on this tab" if d.page is page else (
                    f"on tab {pages.index(d.page)}" if d.page in pages else "in another tab")
                lines.append(dialog_notice(d, where))
        if len(pages) > 1 and page in pages:
            lines.append(f"- tabs: {len(pages)} open, this is tab {pages.index(page)} (browser_tabs lists them, "
                         "browser_tab switches)")
        return lines

    def _frozen_snapshot(self, page: Page, header: list[str]) -> SnapshotResult:
        frozen = "- (the page is frozen until the dialog is answered; nothing on it can be read or acted on)"
        text = "\n".join([*header, frozen])
        return SnapshotResult(url=_url_of(page), title="", text=text, ref_count=0, signal=Signal.UNKNOWN,
                              signal_detail=_DIALOG_OPEN, notices=header, frozen=True)

    # ---- Interaction -------------------------------------------------------------------

    async def navigate(self, page: Page, url: str, *, follow_link: bool = False,
                       on_click: Callable[[], None] | None = None) -> NavigateResult:
        """Open `url` directly. With `follow_link`, and mid-visit when the page shows a link to
        exactly that address, follow it with a click as a person would (the site then sees a Referer
        and a same-origin fetch, not an address typed into the bar); otherwise, or when the click
        does not take the page there, go there directly once whatever the click started has finished
        (never two navigations at once). Either way the page ends on the checked address, or the
        error says where it is. A click runs the page's own handlers, so it is an act: only a caller
        allowed to act on the lane may ask for one (tools.browser_navigate). `on_click` runs right
        before the mouse goes down, so the caller can record the click whatever comes of it (the page
        may land elsewhere, or the navigation fail); it does not run when no press was made."""
        target = check_url(url, allow_private=self.allow_private_targets or through_vm(page))
        state = self._track(page)
        guard = dialog_guard(page)
        if guard is not None and (open_dialog := guard.pending_on(page)) is not None:
            raise _pending_error(open_dialog, f"navigate to {target}")
        await human.still(page)
        await human.place_pointer(page)  # a fresh tab: the pointer leaves the corner before a site can see it
        link = await self._link_to(page, target) if follow_link else None
        led_to: str | None = None
        try:
            followed = False
            if link is not None:
                followed, led_to = await self._race(page, self._follow(page, target, link, on_click), guard)
            if followed:
                status = self._status_for_current(page)
            else:
                if led_to is not None:
                    log.info("the link to %s led to %s; opening the address directly", url_for_log(target),
                             url_for_log(led_to))
                response = await self._race(
                    page, page.goto(target, wait_until="domcontentloaded", timeout=self.navigate_timeout_ms), guard)
                status = response.status if response is not None else None
                if response is not None:
                    state.status, state.status_url = status, response.url
        except _DialogOpened as opened:
            raise _pending_error(opened.dialog, f"finish the navigation to {target} yet") from None
        except AgentosError:
            raise
        except Exception as e:
            # the click on the page's link took the page elsewhere first: say so, it was never checked
            elsewhere = f" (the page's link had taken it to {url_for_log(led_to)})" if led_to is not None else ""
            raise EngineError(f"navigation to {target} failed: {_first_line(e)}{elsewhere}") from e
        finally:
            if link is not None:
                await quietly(link.dispose(), timeout=2.0)
        title = await _title(page)
        human.begin_idle(page)
        return NavigateResult(url=page.url, title=title, status=status, **(await self.report(page)).public())

    async def _link_to(self, page: Page, target: str) -> ElementHandle | None:
        """The link on the page to `target` (_LINK_TO_JS), when the target is another page of the
        site the tab shows. None otherwise, or when the page cannot be read."""
        current = _url_of(page)
        if not same_origin(current, target) or _strip_fragment(current) == _strip_fragment(target):
            return None
        handle = await quietly(page.evaluate_handle(_LINK_TO_JS, target), None, timeout=3.0)
        if handle is None:
            return None
        element = handle.as_element()
        if element is None:
            await quietly(handle.dispose(), timeout=2.0)
        return element

    async def _follow(self, page: Page, target: str, link: ElementHandle,
                      on_click: Callable[[], None] | None = None) -> tuple[bool, str | None]:
        """Click `link` like a person and wait until the page it leads to has loaded: (True, None).
        (False, None) when the click was not made (the link changed, is covered or out of sight) or
        started nothing; (False, url) when it took the page somewhere else, now loaded at `url`. In
        both the caller then goes to the address directly, after anything the click started has
        finished. A navigation to the target that fails raises, as page.goto would."""
        want = _link_leads_to(await quietly(link.evaluate(_LINK_FACTS_JS, target), None, timeout=3.0))
        if want is None:
            return False, None
        pressed = False

        async def still_leads_there() -> None:
            # right before the mouse down: page scripts had the scroll, the pause and the glide to
            # change the link's href or target, or the document's <base target>
            nonlocal pressed
            now = await quietly(link.evaluate(_LINK_FACTS_JS, target), None, timeout=3.0)
            if _link_leads_to(now) != want:
                raise _LinkChanged
            pressed = True
            if on_click is not None:
                on_click()

        navs = _Navigations(page, target, _strip_fragment(want))
        page.on("request", navs.on_request)
        page.on("framenavigated", navs.on_navigated)
        page.on("requestfailed", navs.on_failed)
        try:
            try:
                with human.before_press(still_leads_there):
                    await human.click(page, link)
            except _LinkChanged:
                log.info("the link to %s changed before the press; opening the address directly",
                         url_for_log(target))
            except AgentosError as e:
                if not isinstance(e, human.TargetNotVisible | human.TargetCovered):
                    raise
                log.info("could not click the link to %s (%s); opening it directly", url_for_log(target), e.code)
            except Exception as e:  # noqa: BLE001 - the link left the page under the pointer
                log.info("could not click the link to %s (%s); opening it directly", url_for_log(target),
                         _first_line(e))
            if not pressed and not navs.began.done():
                return False, None
            if not navs.began.done():
                try:
                    await asyncio.wait_for(asyncio.shield(navs.began), LINK_START_S)
                except TimeoutError:
                    # nothing yet: a script may still be at work on the click (a fetch, then a
                    # navigation). Let the network go quiet first, so a late navigation and the
                    # caller's goto do not race.
                    await self._quiet_unless(page, navs.began)
                    if not navs.began.done():
                        return False, None
            async with asyncio.timeout(self.navigate_timeout_ms / 1000):
                try:
                    await navs.landed
                except EngineError:
                    if navs.ours:
                        raise
                    return False, None  # it went elsewhere and failed there, so the page stayed: the goto follows
                if not navs.ours:
                    return False, _url_of(page)
                await page.wait_for_load_state("domcontentloaded")
            return True, None
        finally:
            page.remove_listener("request", navs.on_request)
            page.remove_listener("framenavigated", navs.on_navigated)
            page.remove_listener("requestfailed", navs.on_failed)
            if navs.landed.done() and not navs.landed.cancelled():
                navs.landed.exception()  # retrieved, so asyncio does not warn about a failure nobody awaited

    async def _quiet_unless(self, page: Page, began: asyncio.Future[None]) -> None:
        """Wait until no request has started or ended for LINK_QUIET_S, or `began` resolves, at most
        LINK_SETTLE_S."""
        state = self._track(page)
        deadline = time.monotonic() + LINK_SETTLE_S
        while not began.done() and time.monotonic() < deadline:
            busy, idle = state.network()
            if busy == 0 and idle >= LINK_QUIET_S:
                return
            await asyncio.wait({began}, timeout=0.1)

    async def snapshot(self, page: Page, *, max_chars: int | None = None) -> SnapshotResult:
        """`max_chars`: above the budget an agent's outline has, for a read that keeps none of it."""
        self._track(page)
        guard = dialog_guard(page)
        header = self._header(page, guard)
        if guard is not None and guard.pending_on(page) is not None:
            return self._frozen_snapshot(page, header)
        try:
            snap = await self._race(page, take_snapshot(page, max_chars=max_chars or self.snapshot_max_chars), guard)
        except _DialogOpened:
            return self._frozen_snapshot(page, self._header(page, guard))
        except AgentosError:
            raise
        except Exception as e:
            raise EngineError(f"snapshot failed: {_first_line(e)}") from e
        update: dict[str, Any] = (await self.report(page)).public()
        if header:
            update["text"] = "\n".join([*header, snap.text])
            update["notices"] = header
        return snap.model_copy(update=update)

    async def screenshot(self, page: Page) -> bytes:
        guard = dialog_guard(page)
        if guard is not None and (open_dialog := guard.pending_on(page)) is not None:
            raise _pending_error(open_dialog, "take a screenshot")
        try:
            # caret="initial": the default hides the caret by injecting a style into the page.
            return await self._race(page, page.screenshot(type="png", full_page=False, caret="initial",
                                                          timeout=SCREENSHOT_TIMEOUT_MS), guard)
        except _DialogOpened as opened:
            raise _pending_error(opened.dialog, "take a screenshot") from None
        except Exception as e:
            raise EngineError(f"screenshot failed: {_first_line(e)}") from e

    async def wait(self, page: Page, spec: WaitFor, *,
                   should_stop: Callable[[], str | None] | None = None) -> WaitResult:
        """Wait until every condition in `spec` holds, for at most `spec.timeout_s` (capped at
        WAIT_MAX_S). Returns what each condition saw last and the page's signal. A dialog that opens
        stops the wait and is reported in `dialog`; a challenge signal, the tab closing or
        `should_stop()` returning a reason (a human took the lane) stop it too."""
        if spec.empty:
            raise InvalidRequest("nothing to wait for: give for_text, gone_text, for_ref_state, network_quiet_ms "
                                 "or url_matches")
        if spec.ref is not None and spec.ref_state not in REF_STATES:
            raise InvalidRequest(f"unknown ref state {spec.ref_state!r}; use one of: {', '.join(REF_STATES)}")
        timeout = float(spec.timeout_s)
        if math.isnan(timeout) or timeout < 0:
            raise InvalidRequest("timeout_s must be 0 or more seconds")
        timeout = min(timeout, WAIT_MAX_S)
        state = self._track(page)
        guard = dialog_guard(page)
        if guard is not None and (open_dialog := guard.pending_on(page)) is not None:
            raise _pending_error(open_dialog, "wait")
        loop = asyncio.get_running_loop()
        started = loop.time()  # the deadline covers reading the ref too: a page that never answers holds the lane
        deadline = started + timeout
        ref = handle = before = None
        unanswered = False
        if spec.ref is not None:
            ref = normalize_ref(spec.ref)
            try:
                async with asyncio.timeout_at(deadline + WAIT_READ_GRACE_S):
                    handle = await self._resolve(page, ref)
                    before = await _state(handle)
            except TimeoutError:
                unanswered = True
            else:
                if before is None:
                    raise RefNotFound(f"ref {ref} is no longer on the page. Take a new snapshot.")

        watch = _Watch(spec, ref, before)
        dialog: PageDialog | None = None
        if unanswered:
            stopped = "timeout"
            watch.set("for_ref_state", False, "the page did not answer when its state was read")
        else:
            try:
                stopped = await asyncio.wait_for(
                    self._race(page, self._wait_loop(page, watch, state, handle, deadline, should_stop), guard),
                    max(0.0, deadline - loop.time()) + RESOLVE_TIMEOUT_MS / 1000)
            except _DialogOpened as opened:
                stopped, dialog = "dialog", opened.dialog
            except TimeoutError:
                stopped = "timeout"
        waited = round(loop.time() - started, 1)
        if page.is_closed():
            stopped = "tab_closed"
            report = SignalReport(Signal.UNKNOWN, "the tab closed", _url_of(page))
        else:
            try:
                report = await asyncio.wait_for(self.report(page), WAIT_REPORT_S)
            except TimeoutError:
                report = SignalReport(Signal.UNKNOWN, "the page did not answer", _url_of(page))
        met = stopped == "met"
        if met:
            detail = f"met after {waited:g} s"
        elif stopped == "timeout":
            detail = f"not met within {timeout:g} s: {watch.unmet()}"
        elif dialog is not None:
            detail = (f"{_a(dialog.type)} {dialog.type} dialog opened after {waited:g} s: "
                      f"{json.dumps(dialog.message[:300], ensure_ascii=False)}. The page is frozen until it is "
                      f"answered: call browser_dialog_answer. Unanswered, it is {dialog.automatic} automatically "
                      f"in {dialog.seconds_left()} s")
        elif stopped == "signal":
            detail = f"stopped after {waited:g} s: the page signal is {report.signal.value}"
        elif stopped == "tab_closed":
            detail = f"the tab closed after {waited:g} s"
        else:
            detail = f"stopped after {waited:g} s: a human took control of the lane"
        return WaitResult(met=met, stopped=stopped, waited_s=waited, url=_url_of(page),
                          conditions=list(watch.conditions.values()), reread=watch.reread,
                          dialog=dialog.public() if dialog is not None else None, detail=detail, **report.public())

    async def _wait_loop(self, page: Page, watch: _Watch, state: _PageState, handle: ElementHandle | None,
                         deadline: float, should_stop: Callable[[], str | None] | None) -> str:
        loop = asyncio.get_running_loop()
        next_signal = loop.time() + WAIT_SIGNAL_EVERY_S
        while True:
            if page.is_closed():
                return "tab_closed"
            reason = should_stop() if should_stop is not None else None
            if reason:
                return reason
            began = loop.time()
            await self._check(page, watch, state, handle, deadline)
            if watch.met:
                return "met"
            took = loop.time() - began
            if loop.time() >= next_signal:
                if (await self.report(page)).signal in STOP_SIGNALS:
                    return "signal"
                next_signal = loop.time() + WAIT_SIGNAL_EVERY_S
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "timeout"
            await asyncio.sleep(min(remaining, max(WAIT_POLL_MS / 1000, took)))

    async def _check(self, page: Page, watch: _Watch, state: _PageState, handle: ElementHandle | None,
                     deadline: float) -> None:
        spec = watch.spec
        if spec.reads_tree:
            left_ms = max(1.0, (deadline - asyncio.get_running_loop().time()) * 1000)
            raw = await quietly(page.aria_snapshot(mode="ai", timeout=min(SNAPSHOT_TIMEOUT_MS, left_ms + 500)))
            unreadable = "the page could not be read (it may be loading)"
            texts = outline_texts(raw) if raw is not None else None
            watch.reread = watch.reread or raw is not None
            if spec.for_text:
                hit = None
                for want in spec.for_text if texts is not None else ():
                    if (line := find_text(texts or [], want, exact=spec.exact)) is not None:
                        hit = (want, line)
                        break
                if hit is not None:
                    watch.set("for_text", True, hit[1], matched=hit[0])
                else:
                    watch.set("for_text", False, None if texts is not None else unreadable)
            if spec.gone_text:
                if texts is None:
                    watch.set("gone_text", False, unreadable)
                else:
                    left = [line for want in spec.gone_text
                            if (line := find_text(texts, want, exact=spec.exact)) is not None]
                    watch.set("gone_text", not left, left[0] if left else None)
        if handle is not None:
            budget = max(0.0, deadline - asyncio.get_running_loop().time()) + WAIT_READ_GRACE_S
            now = await quietly(_state(handle), _UNANSWERED, timeout=budget)
            # the inner quietly turns a failed read into None (gone); only no answer in time is _UNANSWERED
            view = await quietly(quietly(handle.evaluate(_VIEW_JS)), _UNANSWERED, timeout=budget) \
                if now is not None and now is not _UNANSWERED else None
            if now is _UNANSWERED or view is _UNANSWERED:
                watch.set("for_ref_state", False, "the page did not answer when its state was read")
            else:
                met, seen = ref_state_met(spec.ref_state or "", watch.before, now, view)
                watch.set("for_ref_state", met, seen)
        if spec.network_quiet_ms is not None:
            busy, idle = state.network()
            quiet = busy == 0 and idle * 1000 >= spec.network_quiet_ms
            if busy:
                seen = f"{busy} request{'s' if busy != 1 else ''} in flight"
            else:
                seen = "no requests seen yet" if math.isinf(idle) else f"quiet for {idle:.1f} s"
            watch.set("network_quiet_ms", quiet, seen)
        if spec.url_matches is not None:
            url = _url_of(page)
            watch.set("url_matches", url_matches(url, spec.url_matches), url)

    async def settle(self, page: Page, *, quiet_ms: int = SETTLE_QUIET_MS, max_s: float = SETTLE_MAX_S) -> None:
        """Give a page that just loaded a moment to render: until no request has started or ended
        for `quiet_ms`, at most `max_s`. Reads only; a dialog ends it at once."""
        state = self._track(page)
        guard = dialog_guard(page)
        if guard is not None and guard.pending_on(page) is not None:
            return

        async def quiet() -> None:
            while True:
                busy, idle = state.network()
                if busy == 0 and idle * 1000 >= quiet_ms:
                    return
                await asyncio.sleep(0.1)

        try:
            await asyncio.wait_for(self._race(page, quiet(), guard), max_s)
        except (TimeoutError, _DialogOpened):
            pass

    async def loading(self, page: Page) -> bool:
        """Whether the page is still loading: its document is not complete yet, or requests are in flight.
        A page that does not answer counts as loading."""
        busy, _idle = self._track(page).network()
        if busy:
            return True
        state = await quietly(page.evaluate("document.readyState"), None, timeout=READY_STATE_S)
        return state != "complete"

    async def wait_rendered(self, page: Page, max_s: float) -> None:
        """Wait, at most `max_s`, for a page that is still loading: its load event, then a quiet moment."""
        loop = asyncio.get_running_loop()
        end = loop.time() + max_s
        await quietly(page.wait_for_load_state("load", timeout=max_s * 1000), None, timeout=max_s)
        left = end - loop.time()
        if left > 0:
            await self.settle(page, max_s=min(SETTLE_MAX_S, left))

    async def signal(self, page: Page) -> Signal:
        return (await self.report(page)).signal

    async def report(self, page: Page) -> SignalReport:
        """The page's signal with the rule that matched and whose session it is (sites.classify_page).
        Remembered per lane context for the lane manager (`take_report`)."""
        url = _url_of(page)
        if not url or url == "about:blank":
            return _remember(page, SignalReport(Signal.UNKNOWN, "nothing is loaded", url))
        guard = dialog_guard(page)
        if guard is not None and guard.pending_on(page) is not None:
            # The page is frozen; reading it would hang.
            return _remember(page, SignalReport(Signal.UNKNOWN, _DIALOG_OPEN, url))
        try:
            facts = await self._race(page, self._facts(page, url), guard)
        except _DialogOpened:
            return _remember(page, SignalReport(Signal.UNKNOWN, _DIALOG_OPEN, url))
        return _remember(page, classify_page(facts, lane_expectation(page)))

    async def _facts(self, page: Page, url: str) -> PageFacts:
        self._track(page)
        status = self._status_for_current(page)
        markers = await quietly(page.evaluate(_MARKERS_JS, [list(MARKER_SELECTORS), MIN_VISIBLE_PX]))
        cookies = await _read_cookies(page, url)
        if markers is None:
            return PageFacts(url=url, status=status, readable=False, cookies=cookies)
        frames = [Frame(f.get("src") or "", bool(f.get("visible"))) for f in markers.get("frames") or []]
        frames += await self._frames_in_hidden_dom(page, {f.url for f in frames})
        return PageFacts(url=url, status=status, frames=tuple(frames), scripts=tuple(markers.get("scripts") or ()),
                         elements=dict(markers.get("elements") or {}), cookies=cookies)

    async def _frames_in_hidden_dom(self, page: Page, seen: set[str]) -> list[Frame]:
        """Challenge frames that a DOM query cannot see (closed shadow roots), found through the
        frame tree and judged by the same visibility check as the rest."""
        found: list[Frame] = []
        for frame in list(getattr(page, "frames", []))[1:]:
            if frame.url in seen or not is_challenge_frame(frame.url):
                continue
            element = await quietly(frame.frame_element())
            shown = await quietly(element.evaluate(_SHOWN_JS, MIN_VISIBLE_PX)) if element is not None else None
            found.append(Frame(frame.url, bool(shown)))
        return found

    async def act(self, page: Page, ref: str, action: ActionKind | str, text: str | None = None) -> ActionResult:
        try:
            kind = ActionKind(action)
        except ValueError:
            raise InvalidRequest(f"unknown action {action!r}; use one of: "
                                 + ", ".join(a.value for a in ActionKind)) from None
        ref = normalize_ref(ref)
        await human.still(page)
        if kind in (ActionKind.TYPE, ActionKind.FILL, ActionKind.SELECT, ActionKind.PRESS) and not text:
            what = {"select": "the option label", "press": "the key, for example Enter"}.get(kind.value, "the text")
            raise InvalidRequest(f"{kind.value} needs text: {what}")
        self._track(page)
        guard = dialog_guard(page)
        if guard is not None and (open_dialog := guard.pending_on(page)) is not None:
            raise _pending_error(open_dialog, f"{kind.value} on {ref}")
        pages_before = set(_open_pages(page))
        choosers_before = guard.chooser_count(page) if guard is not None else 0
        target = await self._resolve(page, ref)
        url_before = page.url
        before = await _state(target)
        if before is None:
            raise RefNotFound(f"ref {ref} is no longer on the page. Take a new snapshot.")

        in_view = None
        try:
            in_view = await self._race(page, self._perform(page, target, kind, text), guard)
        except _DialogOpened as opened:
            return await self._after(page, kind, ref, pages_before, choosers_before, guard, opened=opened.dialog)
        except AgentosError:
            raise
        except Exception as e:
            if page.is_closed():
                return await self._after(page, kind, ref, pages_before, choosers_before, guard)
            if await _state(target) is None:
                raise RefNotFound(f"ref {ref} left the page during {kind.value}. Take a new snapshot.") from e
            raise EngineError(f"{kind.value} on {ref} failed: {_first_line(e)}") from e
        if page.is_closed():
            return await self._after(page, kind, ref, pages_before, choosers_before, guard)

        ok = True
        try:
            if kind is ActionKind.SCROLL:
                changed = None
                ok = bool(in_view)
                detail = "in view" if in_view else "could not bring it fully into view"
            else:
                changed, detail, after = await self._race(
                    page, self._read_back(page, target, before, kind, url_before), guard)
                if kind is ActionKind.SELECT and after is not None:
                    ok = human.match_option([{"label": after.get("selected", "")}], text or "") == 0
                    if not ok:
                        detail += f"; the selected option is {after.get('selected')!r}, not {text!r}"
            if _strip_fragment(page.url) != _strip_fragment(url_before):
                await self._race(page, quietly(page.wait_for_load_state("domcontentloaded", timeout=10_000)), guard)
        except _DialogOpened as opened:
            return await self._after(page, kind, ref, pages_before, choosers_before, guard, opened=opened.dialog)
        if page.is_closed():
            return await self._after(page, kind, ref, pages_before, choosers_before, guard)
        notes = await self._side_effects(page, pages_before, choosers_before, guard, ref)
        human.begin_idle(page)
        return ActionResult(ok=ok, action=kind, ref=ref, url=page.url, changed=changed,
                            detail="; ".join([detail, *notes]), **(await self.report(page)).public())

    async def _after(self, page: Page, kind: ActionKind, ref: str, pages_before: set[Any], choosers_before: int,
                     guard: DialogGuard | None, *, opened: PageDialog | None = None) -> ActionResult:
        """The result of an action that opened a dialog or closed its own tab (a popup finishing)."""
        if opened is not None:
            detail = (f"the {kind.value} opened {_a(opened.type)} {opened.type} dialog: "
                      f"{json.dumps(opened.message[:300], ensure_ascii=False)}. The page is frozen until it is "
                      f"answered: call browser_dialog_answer. Unanswered, it is {opened.automatic} automatically "
                      f"in {opened.seconds_left()} s")
        else:
            if not _open_pages(page):
                # No tab is left: the browser itself went away (it crashed or was stopped), not a
                # popup finishing. What was typed or clicked may have only partly happened.
                raise EngineError(f"the browser closed during the {kind.value} on {ref}, so it may be half "
                                  "done. Take a snapshot to see what reached the page before you go on")
            detail = f"the tab closed during the {kind.value} (a popup or sign-in window finishing)"
        notes = await self._side_effects(page, pages_before, choosers_before, guard, ref)
        why = _DIALOG_OPEN if opened is not None else "the tab closed; take a snapshot of the tab the lane acts in now"
        return ActionResult(ok=True, action=kind, ref=ref, url=_url_of(page), signal=Signal.UNKNOWN, changed=None,
                            detail="; ".join([detail, *notes]), signal_detail=why)

    async def _side_effects(self, page: Page, pages_before: set[Any], choosers_before: int,
                            guard: DialogGuard | None, ref: str) -> list[str]:
        notes: list[str] = []
        new = [p for p in _open_pages(page) if p not in pages_before]
        if new:
            await quietly(asyncio.wait_for(new[-1].wait_for_load_state("domcontentloaded"),
                                           NEW_TAB_SETTLE_MS / 1000))
            notes.append(f"a new tab opened ({_url_of(new[-1]) or 'loading'}) and the lane now acts in it; "
                         "browser_tabs lists the tabs")
        elif page.is_closed():
            rest = _open_pages(page)
            if rest:
                notes.append("the lane now acts in the tab that opened it (or the last one used); "
                             "take a snapshot to see it")
        if guard is not None and guard.chooser_count(page) > choosers_before:
            notes.append(f"it opened a file chooser (not shown on screen): attach files with "
                         f"browser_upload on ref {ref} and file ids from files_list")
        return notes

    # ---- upload ------------------------------------------------------------------------

    async def upload(self, page: Page, ref: str, paths: Sequence[Path | str], *, mimes: Sequence[str] | None = None,
                     root: Path | None = None, chooser_timeout_ms: int = CHOOSER_TIMEOUT_MS) -> UploadResult:
        """Attach files to the file input `ref` names, or to the one its button (or label) opens a
        chooser for, then read back what the input holds. With `root`, every path must be a regular
        file inside it (the lane inbox)."""
        files = [Path(p) for p in paths]
        if not files:
            raise InvalidRequest("no files to upload")
        base = root.resolve() if root is not None else None
        for f in files:
            if f.is_symlink() or not f.is_file():
                raise InvalidRequest(f"{f.name} is not a regular file")
            if base is not None and not f.resolve().is_relative_to(base):
                raise InvalidRequest(f"{f.name} is outside the lane inbox; only inbox files can be uploaded")
        want = _expected(files, mimes)
        ref = normalize_ref(ref)
        self._track(page)
        guard = dialog_guard(page)
        if guard is not None and (open_dialog := guard.pending_on(page)) is not None:
            raise _pending_error(open_dialog, f"upload to {ref}")
        await human.still(page)
        target = await self._resolve(page, ref)
        info = await quietly(target.evaluate(_FILE_INPUT_JS))
        if info is None or not info.get("connected"):
            raise RefNotFound(f"ref {ref} is no longer on the page. Take a new snapshot.")
        try:
            if info["file"]:
                via, element = "input", target
                _check_input(info, want)
                await element.evaluate(_WATCH_CHANGE_JS)
                await self._race(page, element.set_input_files([str(f) for f in files]), guard)
            else:
                via = "chooser"
                chooser = await self._open_chooser(page, target, ref, guard, chooser_timeout_ms)
                element = chooser.element
                chosen = await element.evaluate(_FILE_INPUT_JS)
                _check_input(chosen, want, multiple=chooser.is_multiple())
                await element.evaluate(_WATCH_CHANGE_JS)
                await self._race(page, chooser.set_files([str(f) for f in files]), guard)
            now, seen = await self._race(page, self._read_files(element, want), guard)
        except _DialogOpened as opened:
            raise _pending_error(opened.dialog, f"finish the upload to {ref}") from None
        except AgentosError:
            raise
        except Exception as e:
            raise EngineError(f"upload to {ref} failed: {_first_line(e)}") from e

        if _same_files(now, want):
            ok, detail = True, f"the input holds {_sizes(now)}"
        elif _same_files(seen, want):
            ok, detail = True, f"the page read {_sizes(seen)} and then cleared the input (normal for upload widgets)"
        else:
            ok = False
            detail = (f"the input holds {_sizes(now) or 'nothing'} after setting {_sizes(want)}; "
                      "take a snapshot to see what the page did")
        return UploadResult(ok=ok, ref=ref, url=page.url, via=via, files=now, seen_by_page=seen, detail=detail,
                            **(await self.report(page)).public())

    async def _open_chooser(self, page: Page, target: ElementHandle, ref: str, guard: DialogGuard | None,
                            timeout_ms: int) -> Any:
        """Click the button the way a person would and answer the file chooser it opens."""
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        try:
            async with page.expect_file_chooser(timeout=timeout_ms) as chooser_info:
                await self._race(page, human.click(page, target), guard)
            return await chooser_info.value
        except PlaywrightTimeout:
            raise InvalidRequest(f"clicking {ref} did not open a file chooser within {timeout_ms / 1000:g} s; pass the "
                                 "ref of a file input or of the button that opens one") from None

    async def _read_files(self, element: ElementHandle, want: list[dict[str, Any]]) -> tuple[list, list | None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + UPLOAD_READBACK_MS / 1000
        while True:
            state = await quietly(element.evaluate(_READ_FILES_JS)) or {}
            now, seen = state.get("files") or [], state.get("seen")
            if _same_files(now, want) or _same_files(seen, want) or loop.time() >= deadline:
                return now, seen
            await asyncio.sleep(READBACK_POLL_MS / 1000)

    # ---- act internals -----------------------------------------------------------------

    async def _resolve(self, page: Page, ref: str) -> ElementHandle:
        stale = RefNotFound(f"ref {ref} is not on the page (stale after a navigation or a newer snapshot). "
                            "Take a new snapshot.")
        locator = page.locator(f"aria-ref={ref}")
        try:
            if await locator.count() == 0:
                raise stale
            handle = await locator.element_handle(timeout=RESOLVE_TIMEOUT_MS)
        except RefNotFound:
            raise
        except Exception as e:
            raise stale from e
        if handle is None:
            raise stale
        return handle

    async def _perform(self, page: Page, target: ElementHandle, kind: ActionKind, text: str | None) -> bool | None:
        if kind is ActionKind.CLICK:
            await human.click(page, target)
        elif kind is ActionKind.HOVER:
            await human.hover(page, target)
        elif kind is ActionKind.SCROLL:
            return await human.scroll_into_view(page, target)
        elif kind is ActionKind.SELECT:
            await human.select_option(page, target, text or "")
        elif kind in (ActionKind.TYPE, ActionKind.FILL):
            if await human.editable_kind(target) is None:
                raise InvalidRequest(f"{kind.value} needs a text field; this ref is not one")
            await human.focus_field(page, target, caret_to_end=kind is ActionKind.TYPE)
            if kind is ActionKind.FILL:
                await human.clear_field(page, target)
            await human.type_text(page, text or "", typos=await human.typos_fit(target))
        elif kind is ActionKind.PRESS:
            if not await human.is_focused(target):
                if await human.editable_kind(target) is not None:
                    await human.focus_field(page, target)
                else:
                    await target.focus()  # like arriving with Tab: focus without a click
            await human.press(page, text or "")
        return None

    async def _read_back(self, page: Page, target: ElementHandle, before: dict[str, str], kind: ActionKind,
                         url_before: str) -> tuple[bool | None, str, dict[str, str] | None]:
        """Poll the target's own state until it changes (then let it settle) or time runs out."""
        loop = asyncio.get_running_loop()
        budget = self.readback_timeout_ms if kind is not ActionKind.HOVER else min(self.readback_timeout_ms, 1_000)
        deadline = loop.time() + budget / 1000
        after: dict[str, str] | None = before
        while True:
            after = await _state(target)
            if after is None or diff_state(before, after):
                break
            if loop.time() >= deadline:
                break
            await asyncio.sleep(READBACK_POLL_MS / 1000)
        if after is not None and diff_state(before, after):
            await asyncio.sleep(READBACK_SETTLE_MS / 1000)
            after = await _state(target) or after
        if after is None:
            if _strip_fragment(page.url) != _strip_fragment(url_before):
                return None, f"the page navigated to {page.url}", None
            return None, "the target left the page, so its state cannot be read back", None
        changes = diff_state(before, after)
        if changes:
            return True, "; ".join(changes), after
        return False, "no change in the target's own state", after


async def _state(target: ElementHandle) -> dict[str, str] | None:
    """The target's own state, or None if it is gone (detached, or its document navigated away)."""
    if not await quietly(target.evaluate("(el) => el.isConnected"), False):
        return None
    return await quietly(target.evaluate(_STATE_JS))


async def _title(page: Page) -> str:
    return await quietly(page.title(), "")


def _is_main_navigation(request: Any, main: Any) -> bool:
    try:
        return bool(request.is_navigation_request()) and request.frame == main
    except Exception:  # noqa: BLE001 - e.g. a service worker request has no frame
        return False


def _consume(task: asyncio.Task) -> None:
    """Retrieve the outcome of a page call we stopped waiting for, so asyncio does not warn."""
    if not task.cancelled():
        task.exception()


def _first_line(e: BaseException) -> str:
    text = str(e).strip() or type(e).__name__
    return text.splitlines()[0][:300]


def create_interaction(**kwargs: Any) -> PlaywrightInteraction:
    return PlaywrightInteraction(**kwargs)
