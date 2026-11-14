"""One keepalive visit: open the site's feed, look and scroll at a person's pace, maybe open one item
and come back, then read the session cookies again. It never presses anything and never types.

How it stays read-only, outermost first:

1. The visit holds the lane as the node itself (principal kind "node", holder `node:keepalive`), so
   every gate an agent passes applies: holds, a human in control, claims, the freeze, quarantine.
2. The lane's egress guard is driven by a read-only grant (`DRIVER`, ceiling read) for the whole visit,
   under the guard's effect route (acting(effects_route=True)) on every lane: a request the page sends
   that the gate would hold is refused, recorded and aborted before it leaves. That is a write or commit
   the grant refuses (a like, a follow, a post, an upload), and a PUT, PATCH, DELETE or POST that no
   rule names, classified as the gate classifies it (egress._gate_rule). Reads a normal browser sends
   go as they would for a person. The driver the lane had before is put back afterwards, and an agent's
   drive that the visit ended is bound again before control returns (egress.LaneGuard.restore_drive).
3. The visit itself can only do what `ReadOnly` offers: load a page on its own site's hosts, scroll
   (the wheel, toward an element further down), and go back. It opens an item by loading the address
   of a link to one item on the same site (agentos.keepalive.sites) as a page, through the guard's
   check_url, never by pressing it: a press lands at a point of the link's box, and a site can nest
   a Like or a Follow inside a post link. A button, a text box, a menu, any other link is refused
   before anything is sent (EgressRefused). Nothing is typed, ever.
4. Before every step the visit asks `may_act`: once control of the lane left the node (a human took
   over, a hold) it stops at once (the service also cancels it on the control change).

The pacing is the node's own (the interaction scrolls with the wheel and pauses like a person; see
agentos.node.human) plus dwell times drawn per visit. A visit stops early, as a success, when another
caller starts waiting for the lane: that use keeps the session alive just as well.

A page that shows a challenge, a checkpoint, a block or a login page ends the visit at once with that
outcome; the service marks the lane and tells the owner. The interaction is the lane manager's watched
one, so the signal also reached the lane's hold policy (a needs-human hold), exactly as for an agent.
A page signed in to another account than the lane's pinned one (the identity check, AUD-12) ends it
as `wrong_account`; the service holds the lane for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from agentos.core.errors import EgressRefused, EngineError
from agentos.core.models import ActionKind, Signal
from agentos.core.signals import CHALLENGE
from agentos.keepalive.sites import Entry
from agentos.node import egress
from agentos.node import sites as node_sites
from agentos.node.claims import ACT, Holder

log = logging.getLogger("agentos.keepalive.visit")

PRINCIPAL_KIND = "node"
HOLDER = Holder(key="node:keepalive", label="keepalive (the node)", kind=PRINCIPAL_KIND, identity="node:keepalive")
DRIVER = egress.Driver(kind=PRINCIPAL_KIND, id="node:keepalive", grant_id="keepalive", ceiling="read")
COOKIE_TIMEOUT = 5.0
CHALLENGES = CHALLENGE
SIGNED_OUT = frozenset({Signal.LOGGED_OUT})

_LINE = re.compile(r'^(?P<pad>\s*)- (?P<role>[\w-]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$')
_REF = re.compile(r"\[ref=((?:f\d+)?e\d+)\]")  # agentos.node.snapshot REF_RE: f<frame>e<n> after a navigation
_URL = re.compile(r"^(?P<pad>\s*)- /url: (?P<url>\S+)\s*$")


@dataclass(frozen=True)
class Pace:
    """Seconds a visit spends; every value is drawn inside its range for each visit."""
    first_look: tuple[float, float] = (4.0, 12.0)
    scrolls: tuple[int, int] = (3, 8)
    between: tuple[float, float] = (2.0, 8.0)
    open_item: float = 0.4             # the chance a visit opens one item
    item_look: tuple[float, float] = (6.0, 20.0)
    item_scrolls: tuple[int, int] = (0, 2)
    last_look: tuple[float, float] = (2.0, 6.0)
    max_seconds: float = 300.0


@dataclass(frozen=True)
class Node:
    ref: str
    role: str
    name: str
    url: str | None = None


def outline_nodes(text: str) -> list[Node]:
    """The actionable nodes of a snapshot outline, in page order, with a link's address (its `/url:`
    child line) when the outline shows one."""
    nodes: list[Node] = []
    stack: list[tuple[int, int]] = []  # (indent, index in nodes) of open ref nodes
    for line in text.splitlines():
        url = _URL.match(line)
        if url is not None:
            pad = len(url["pad"])
            while stack and stack[-1][0] >= pad:
                stack.pop()
            if stack:
                i = stack[-1][1]
                n = nodes[i]
                if n.url is None:
                    nodes[i] = Node(n.ref, n.role, n.name, url["url"])
            continue
        m = _LINE.match(line)
        if m is None:
            continue
        pad = len(m["pad"])
        while stack and stack[-1][0] >= pad:
            stack.pop()
        ref = _REF.search(m["rest"] or "")
        if ref is None:
            continue
        nodes.append(Node(ref.group(1), m["role"], (m["name"] or "").replace('\\"', '"')))
        stack.append((pad, len(nodes) - 1))
    return nodes


class _Stop(Exception):
    def __init__(self, outcome: str, reason: str, signal: Signal | None = None):
        super().__init__(reason)
        self.outcome, self.reason, self.signal = outcome, reason, signal


TAKEN = "control of the lane left the node (a human took over, or it was held), so the visit stopped"


class ReadOnly:
    """What a visit may do in the lane, and nothing else. `may_act` is asked before every step."""

    def __init__(self, interaction: Any, page: Any, entry: Entry, guard: Any = None,
                 may_act: Callable[[], bool] = lambda: True):
        self.interaction = interaction
        self.page = page
        self.entry = entry
        self.guard = guard
        self.may_act = may_act
        self.nodes: dict[str, Node] = {}
        self.url = ""
        self.actions = 0

    def _gate(self) -> None:
        if not self.may_act():
            raise _Stop("stopped", TAKEN)

    def _on_site(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return urlsplit(url).scheme in ("http", "https") and any(node_sites.host_is(host, h) for h in self.entry.hosts)

    async def open(self, url: str) -> Any:
        self._gate()
        if not self._on_site(url):
            raise EgressRefused(f"the keepalive visit stays on {self.entry.name}: {urlsplit(url).hostname} is "
                                "not one of its hosts")
        if self.guard is not None:
            self.guard.check_url(url)
        result = await self.interaction.navigate(self.page, url)
        self.url = getattr(result, "url", url) or url
        self.actions += 1
        return result

    async def look(self) -> list[Node]:
        snap = await self.interaction.snapshot(self.page)
        self.url = snap.url or self.url
        found = outline_nodes(snap.text)
        self.nodes = {n.ref: n for n in found}
        return found

    async def scroll(self, ref: str) -> Any:
        self._gate()
        if ref not in self.nodes:
            raise EgressRefused(f"the keepalive visit scrolls only to what it saw on the page ({ref} is not there)")
        self.actions += 1
        return await self.interaction.act(self.page, ref, ActionKind.SCROLL)

    def item_links(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.role == "link" and n.url
                and self.entry.is_item(urljoin(self.url or self.entry.home, n.url), self.url or None)]

    async def open_item(self, ref: str) -> Any:
        """Open one item: only a link to one post or article on this site, loaded as a page from its
        address (open: the site's hosts, the guard's check_url). Nothing on the page is pressed."""
        node = self.nodes.get(ref)
        if node is None or node.role != "link":
            what = f'the {node.role} "{node.name}"' if node is not None else f"ref {ref}"
            raise EgressRefused(f"the keepalive visit is read-only: it opens links to posts and never presses "
                                f"{what}")
        if node not in self.item_links():
            raise EgressRefused(f'the keepalive visit opens only a link to one item on {self.entry.name}, and '
                                f'"{node.name}" is not one')
        return await self.open(urljoin(self.url or self.entry.home, node.url or ""))

    async def back(self, fallback: str) -> None:
        self._gate()
        go_back = getattr(self.page, "go_back", None)
        if callable(go_back):
            try:
                await go_back(wait_until="domcontentloaded", timeout=30_000)
                self.actions += 1
                return
            except Exception as e:  # noqa: BLE001 - a page with no history: load the feed again
                log.debug("go_back failed: %s", type(e).__name__)
        await self.open(fallback)


@dataclass
class Outcome:
    outcome: str = "ok"               # ok | cut | signed_out | challenged | wrong_account | yielded | stopped | failed
    reason: str | None = None
    signal: str | None = None
    url_host: str | None = None
    scrolls: int = 0
    opened_item: bool = False
    seconds: float = 0.0
    loaded: bool = False              # the feed loaded with no bad signal
    identity: str | None = None       # the identity check (AUD-12) on the pages seen: match | wrong_account
    blocks: list[dict[str, Any]] = field(default_factory=list)
    session_before: dict[str, Any] | None = None
    session_after: dict[str, Any] | None = None

    @property
    def session(self) -> str:
        """refreshed | kept | signed_out | unknown: what the session cookies say after the visit."""
        after, before = self.session_after, self.session_before
        if after is None:
            return "unknown"
        if not after.get("signed_in"):
            return "signed_out"
        if before and after.get("expires_at") and (before.get("expires_at") or "") < after["expires_at"]:
            return "refreshed"
        return "kept"

    def detail(self) -> dict[str, Any]:
        return {"host": self.url_host, "scrolls": self.scrolls, "opened_item": self.opened_item,
                "seconds": round(self.seconds, 1), "session": self.session, "loaded": self.loaded,
                **({"identity": self.identity} if self.identity else {}),
                "expires_before": (self.session_before or {}).get("expires_at"),
                "expires_after": (self.session_after or {}).get("expires_at"),
                **({"signal": self.signal} if self.signal else {}),
                **({"blocked": self.blocks} if self.blocks else {})}


def wrong_account_reason(entry: Entry, account: dict[str, Any]) -> str:
    """The plain reason for a wrong account: the account seen and the one the lane expects."""
    expected = account.get("expected") or {}
    handle, uid = expected.get("handle"), expected.get("user_id")
    want = f"{handle} ({uid})" if handle and uid else handle or uid or "its pinned account"
    return f"{entry.name} is signed in as user {account.get('user_id') or 'unknown'}, and the lane expects {want}"


def _judge(result: Any, entry: Entry, *, loading: bool = False, out: Outcome | None = None) -> None:
    signal = getattr(result, "signal", None)
    detail = getattr(result, "signal_detail", None)
    where = f" ({detail})" if detail else ""
    if signal in SIGNED_OUT:
        raise _Stop("signed_out", f"{entry.name} showed its login page{where}: the lane is signed out", signal)
    if signal in CHALLENGES:
        raise _Stop("challenged", f"{entry.name} showed {signal.value}{where}", signal)
    account = getattr(result, "account", None) or {}
    if account.get("status") == node_sites.WRONG_ACCOUNT:
        if out is not None:
            out.identity = node_sites.WRONG_ACCOUNT
        raise _Stop("wrong_account", wrong_account_reason(entry, account))
    if out is not None and account.get("status") == "match" and out.identity is None:
        out.identity = "match"
    if loading and signal == Signal.UNKNOWN:
        raise _Stop("failed", f"{entry.name} did not load")


async def session_state(page: Any, entry: Entry) -> dict[str, Any] | None:
    """{"signed_in", "expires_at"} from the lane's live cookies for the site (the page cannot see this
    read), or None when they cannot be read (no live context, or a site without session cookie rules)."""
    from agentos.stealth.lanecheck import session_expiry

    site = node_sites.site(entry.key)
    context = getattr(page, "context", None)
    read = getattr(context, "cookies", None)
    if site is None or not callable(read):
        return None
    urls = sorted({f"https://{h}/" for h in site.hosts} | ({site.home} if site.home else set()))
    try:
        found = list(await asyncio.wait_for(read(urls), COOKIE_TIMEOUT))
    except Exception as e:  # noqa: BLE001 - the type only: a browser error can quote a value
        log.info("session cookies not read: %s", type(e).__name__)
        return None
    mine = [c for c in found if any(node_sites.host_is(str(c.get("domain", "")).lstrip("."), h) for h in site.hosts)]
    return {"signed_in": site.missing_session(mine) is None, "expires_at": session_expiry(site, mine)}


Sleep = Callable[[float], Awaitable[Any]]


async def browse(ro: ReadOnly, entry: Entry, pace: Pace, r: random.Random, sleep: Sleep,
                 waiting: Callable[[], bool], out: Outcome) -> None:
    """The visit's steps inside the lane. Raises _Stop to end it early."""

    async def rest(span: tuple[float, float]) -> None:
        ro._gate()
        await sleep(r.uniform(*span))
        ro._gate()
        if waiting():
            raise _Stop("yielded", "another caller wanted the lane; that use keeps the session alive too")

    async def scroll_down(count: int) -> None:
        nodes = await ro.look()
        done = 0
        position = -1
        while done < count and position < len(nodes) - 1:  # further down each time, never back up
            position = min(len(nodes) - 1, position + r.randint(1, max(1, len(nodes) // (count + 1))))
            result = await ro.scroll(nodes[position].ref)
            _judge(result, entry, out=out)
            done += 1
            out.scrolls += 1
            await rest(pace.between)

    result = await ro.open(entry.home)
    out.url_host = urlsplit(getattr(result, "url", "") or entry.home).hostname
    _judge(result, entry, loading=True, out=out)
    out.loaded = True
    await rest(pace.first_look)
    await scroll_down(r.randint(*pace.scrolls))
    if r.random() < pace.open_item:
        await ro.look()
        links = ro.item_links()
        if links:
            result = await ro.open_item(r.choice(links).ref)
            _judge(result, entry, out=out)
            out.opened_item = True
            await rest(pace.item_look)
            extra = r.randint(*pace.item_scrolls)
            if extra:
                await scroll_down(extra)
            await ro.back(entry.home)
    await rest(pace.last_look)


async def run(page: Any, interaction: Any, entry: Entry, *, pace: Pace | None = None,
              r: random.Random | None = None, sleep: Sleep = asyncio.sleep,
              waiting: Callable[[], bool] = lambda: False, may_act: Callable[[], bool] = lambda: True,
              clock: Callable[[], float] = time.monotonic) -> Outcome:
    """The visit on a page the caller already holds as the node (agentos.keepalive.service).
    `may_act`: is control of the lane still the node's (asked before every step)."""
    pace = pace or Pace()
    r = r or random.Random()
    out = Outcome()
    started = clock()
    guard = egress.guard_for(page)
    previous = getattr(guard, "driver", None)
    drive = guard.drive_state() if guard is not None else None  # an agent's drive, if one is on
    ro = ReadOnly(interaction, page, entry, guard, may_act)
    out.session_before = await session_state(page, entry)
    try:
        async with contextlib.AsyncExitStack() as stack:
            if guard is not None:
                guard.drive(DRIVER)
                mark = await stack.enter_async_context(guard.acting(route=True, effects_route=True))
                stack.callback(lambda: out.blocks.extend(b.public() for b in guard.blocks_since(mark)))
            async with asyncio.timeout(pace.max_seconds):
                await browse(ro, entry, pace, r, sleep, waiting, out)
    except _Stop as stop:
        out.outcome, out.reason = stop.outcome, stop.reason
        out.signal = stop.signal.value if stop.signal is not None else None
    except TimeoutError:
        out.outcome, out.reason = "cut", f"the visit was cut at {pace.max_seconds:g} s"
    except (EgressRefused, EngineError) as e:  # refused, or the lane's guard could not take its route
        out.outcome, out.reason = "failed", e.message
    finally:
        if guard is not None:
            guard.drive(previous)
            if drive is not None and may_act():  # the visit ended the agent's drive: it goes back on first
                await guard.restore_drive(drive)
    out.seconds = clock() - started
    if out.outcome in ("ok", "yielded", "cut"):
        out.session_after = await session_state(page, entry)
        if out.session_after is not None and not out.session_after.get("signed_in"):
            out.outcome = "signed_out"
            out.reason = f"the {entry.name} session cookie was gone after the visit"
    return out


def lane_session(lanes: Any, lane_id: str) -> Any:
    """The lane, held as the node for one act-mode use (a FakeLaneManager takes no holder)."""
    from agentos.node.tools import lane_session as session

    return session(lanes, lane_id, PRINCIPAL_KIND, holder=HOLDER, mode=ACT)
