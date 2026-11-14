"""Fakes for tests: a simulated site and FlowDriver, a controllable clock, fake services.

`resolve_all` / `resolve_one` / `own_text` are also the reference semantics for locator
resolution that the real FlowDriver must match (see Locator's docstring).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agentos.core.errors import ApprovalPending, LaneNotFound, NotFound, PolicyDenied
from agentos.core.models import (
    ControlState,
    Effect,
    LaneControl,
    LaneInfo,
    LaneSpec,
    LaneState,
    NavigateResult,
    Principal,
    Signal,
)
from agentos.runner.driver import AmbiguousLocator, ElementNotFound, ElementState, JsonResponse
from agentos.runner.models import Locator
from agentos.runner.util import Clock

# ---------------------------------------------------------------- clock


class FakeClock(Clock):
    """Time moves only when told to (or when a flow sleeps)."""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
        self._mono = 1000.0
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def set(self, when: datetime) -> None:
        self._now = when if when.tzinfo else when.replace(tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(max(0.0, seconds))
        await asyncio.sleep(0)


# ---------------------------------------------------------------- a page as an element tree


@dataclass(eq=False)
class El:
    role: str
    name: str = ""
    text: str = ""
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[El] = field(default_factory=list)
    key: str | None = None  # stable id for form values and click handlers
    css: tuple[str, ...] = ()
    on_click: Callable[[FakeSite, El, FakeDriver], str | None] | None = None
    options: list[str] = field(default_factory=list)
    parent: El | None = field(default=None, repr=False)

    def add(self, *children: El) -> El:
        self.children.extend(children)
        return self

    def describe(self) -> str:
        return f'{self.role} "{self.name}"' + (f" [{self.key}]" if self.key else "")


def _link(root: El) -> El:
    for c in root.children:
        c.parent = root
        _link(c)
    return root


def _descendants(el: El):
    for c in el.children:
        yield c
        yield from _descendants(c)


def _norm(s: str) -> str:
    return " ".join(s.split())


def _name_ok(el: El, loc: Locator) -> bool:
    if loc.name is None:
        return True
    if loc.match == "exact":
        return _norm(el.name) == _norm(loc.name)
    if loc.match == "contains":
        return _norm(loc.name) in _norm(el.name)
    return re.search(loc.name, el.name) is not None


def _nested_same_role(el: El, anchor: El) -> bool:
    p = el.parent
    while p is not None and p is not anchor:
        if p.role == anchor.role:
            return True
        p = p.parent
    return False


def _attr_ok(el: El, loc: Locator) -> bool:
    return loc.attr is None or loc.attr.matches(el.attrs.get(loc.attr.name))


def _ancestors(el: El) -> list[El]:
    out, cur = [], el.parent
    while cur is not None:
        out.append(cur)
        cur = cur.parent
    return out


def _picked(found: list[El], pick: str | None) -> list[El]:
    """ExtractStep.pick over the fake's tree, as the node's driver does it over element paths."""
    if pick == "outermost":
        return [e for e in found if not any(a in found for a in _ancestors(e))]
    if pick == "card":
        out = []
        for e in found:
            others = [o for o in found if o is not e]
            card = e
            for a in _ancestors(e):
                if a.parent is None or any(o is a or a in _ancestors(o) for o in others):
                    break
                card = a
            out.append(card)
        return out
    return found


def resolve_all(root: El, loc: Locator, scope: El | None = None) -> list[El]:
    """Candidates under the scope rules, ignoring nth. Raises for a bad anchor. `scope` is an
    extract item: the field locator resolves inside it, with the item as its anchor."""
    if loc.names is not None and loc.name is None:
        raise ElementNotFound(f"{loc.describe()}: names were not resolved for the account language")
    anchor = resolve_one(root, loc.within, scope) if loc.within else scope
    base = anchor if anchor is not None else root
    found: list[El] = []
    if loc.role:
        found = [e for e in _descendants(base) if e.role == loc.role and _name_ok(e, loc) and _attr_ok(e, loc)
                 and (loc.level is None or e.attrs.get("level") == str(loc.level))]
    elif not loc.css:  # attr alone
        found = [e for e in _descendants(base) if _attr_ok(e, loc)]
    if not found and loc.css:
        sels = {part.strip() for part in loc.css.split(",")}  # a selector list: any of them
        found = [e for e in _descendants(base) if sels & set(e.css) and _attr_ok(e, loc)]
    if anchor is not None and loc.own:
        found = [e for e in found if not _nested_same_role(e, anchor)]
    return found


def resolve_one(root: El, loc: Locator, scope: El | None = None) -> El:
    found = resolve_all(root, loc, scope)
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


def own_text(el: El, role: str | None = None) -> str:
    """Own text: excludes text inside nested elements with the element's own role, at any
    depth (a comment nested in a list inside a post is still not the post's text)."""
    role = el.role if role is None else role
    parts = [el.text] if el.text else []
    for c in el.children:
        if c.role != role:
            t = own_text(c, role)
            if t:
                parts.append(t)
    return _norm(" ".join(parts))


def own_lines(el: El, role: str | None = None) -> str:
    """own_text with its line breaks: each element's text on lines of its own, as a page that
    keeps them (a br, a block) would show it."""
    role = el.role if role is None else role
    parts = [el.text] if el.text else []
    for c in el.children:
        if c.role != role:
            t = own_lines(c, role)
            if t:
                parts.append(t)
    return "\n".join(parts)


# ---------------------------------------------------------------- site and driver


class FakeSite:
    def __init__(self) -> None:
        self.routes: dict[str, Callable[[FakeSite, FakeDriver], El]] = {}
        self.state: dict[str, Any] = {}
        self.signal = Signal.OK
        # who the browser is signed in as, as FlowDriver.account reports it (None: signed out)
        self.identity: dict[str, str] | None = None
        # what a signed-out visitor sees (FlowDriver.read_signed_out): url -> visible; a url not
        # listed is visible. A callable (url, text) -> dict replaces the table.
        self.signed_out: dict[str, bool | None] | Callable[[str, str], dict[str, Any]] = {}
        self.signed_out_reads: list[tuple[str, str]] = []
        self.outline = ""  # what FlowDriver.outline reads: a stored outline, for discovery
        self.json: dict[str, list[JsonResponse]] = {}  # page url -> what its own code fetches as it loads

    def route(self, url: str, builder: Callable[[FakeSite, FakeDriver], El]) -> None:
        self.routes[url] = builder

    def build(self, url: str, driver: FakeDriver) -> El | None:
        builder = self.routes.get(url)
        if builder is None:
            prefixes = [u for u in self.routes if u.endswith("*") and url.startswith(u[:-1])]
            if prefixes:
                builder = self.routes[max(prefixes, key=len)]
        return _link(builder(self, driver)) if builder else None


Hook = Callable[[str, Any, "FakeDriver"], None]


class FakeWatch:
    """FlowDriver.watch_json over a FakeSite: a page's responses arrive as it is navigated to."""

    def __init__(self, driver: FakeDriver, wanted: Callable[[str], bool], keep: int):
        self.driver, self.wanted, self.keep = driver, wanted, keep
        self.responses: list[JsonResponse] = []
        self.closed = False

    def saw(self, seen: list[JsonResponse]) -> None:
        for r in seen:
            if len(self.responses) < self.keep and self.wanted(r.url):
                self.responses.append(r)

    def close(self) -> None:
        self.closed = True
        self.responses = []
        if self in self.driver.watches:
            self.driver.watches.remove(self)


class FakeDriver:
    """FlowDriver over a FakeSite. `hooks` run before every operation and may raise (to
    simulate timeouts, crashes, approvals)."""

    def __init__(self, site: FakeSite, *, url: str = "about:blank"):
        self.site = site
        self.url = url
        self.values: dict[str, str] = {}
        self.root = El("document")
        self.hooks: list[Hook] = []        # run before an operation
        self.after_hooks: list[Hook] = []  # run after an action took effect
        self.log: list[tuple[str, str]] = []
        self.big_text: str | None = None
        self.watches: list[FakeWatch] = []  # open ones

    def _hook(self, op: str, target: Any) -> None:
        for h in list(self.hooks):
            h(op, target, self)

    def _post(self, op: str, target: Any) -> None:
        for h in list(self.after_hooks):
            h(op, target, self)

    def _render(self) -> None:
        self.root = self.site.build(self.url, self) or _link(El("document", name="Not found"))

    async def navigate(self, url: str) -> NavigateResult:
        self._hook("navigate", url)
        if url != self.url:
            self.values = {}
        self.url = url
        self._render()
        self.log.append(("navigate", url))
        self._post("navigate", url)
        for watch in self.watches:
            watch.saw(self.site.json.get(url, []))
        found = self.root.name != "Not found"
        return NavigateResult(url=url, title=self.root.name, status=200 if found else 404, signal=self.site.signal)

    async def current_url(self) -> str:
        return self.url

    async def find(self, locator: Locator, *, timeout_ms: int = 0) -> El:
        self._hook("find", locator)
        return resolve_one(self.root, locator)

    def _after(self, el: El, op: str) -> None:
        self.log.append((op, el.describe()))
        self._post(op, el)

    async def click(self, element: El) -> None:
        self._hook("click", element)
        goto = element.on_click(self.site, element, self) if element.on_click else None
        if goto and goto != self.url:
            self.values = {}
            self.url = goto
        self._render()
        self._after(element, "click")

    async def type(self, element: El, text: str) -> None:
        self._hook("type", element)
        self.values[element.key or element.name] = self.values.get(element.key or element.name, "") + text
        self._after(element, "type")

    async def fill(self, element: El, text: str) -> None:
        self._hook("fill", element)
        self.values[element.key or element.name] = text
        self._after(element, "fill")

    async def select(self, element: El, option: str) -> None:
        self._hook("select", element)
        if element.options and option not in element.options:
            raise ElementNotFound(f"option {option!r} is not in {element.describe()}")
        self.values[element.key or element.name] = option
        self._after(element, "select")

    async def press(self, key: str, element: El | None = None) -> None:
        self._hook("press", element)
        self.log.append(("press", key))
        self._post("press", element)

    async def scroll_to(self, element: El) -> None:
        self._hook("scroll_to", element)
        self._after(element, "scroll_to")

    async def upload(self, element: El, file_ref: str) -> None:
        self._hook("upload", element)
        self.values[element.key or element.name] = file_ref
        self._after(element, "upload")

    async def read_state(self, locator: Locator) -> ElementState:
        self._hook("read_state", locator)
        try:
            found = resolve_all(self.root, locator)
        except (ElementNotFound, AmbiguousLocator) as e:
            return ElementState(count=0, found=False, ambiguous=isinstance(e, AmbiguousLocator), url=self.url)
        el = None
        if locator.nth is not None:
            el = found[locator.nth] if locator.nth < len(found) else None
        elif len(found) == 1:
            el = found[0]
        if el is None:
            return ElementState(count=len(found), found=False, ambiguous=len(found) > 1 and locator.nth is None,
                                url=self.url)
        text = self.big_text if self.big_text is not None else own_text(el)
        lines = self.big_text if self.big_text is not None else own_lines(el)
        return ElementState(count=len(found), found=True, text=text, lines=lines,
                            value=self.values.get(el.key or el.name, el.attrs.get("value")),
                            attributes=dict(el.attrs), url=self.url)

    def watch_json(self, wanted: Callable[[str], bool], *, keep: int = 1, only_json: bool = False) -> FakeWatch:
        self._hook("watch_json", None)
        self.watches.append(FakeWatch(self, wanted, keep))
        return self.watches[-1]

    async def account(self, site: str | None = None, domain: str | None = None) -> dict[str, str] | None:
        self._hook("account", site)
        return dict(self.site.identity) if self.site.identity else None

    async def outline(self) -> str:
        self._hook("outline", None)
        return self.site.outline

    async def extract_items(self, items: Locator, fields: dict[str, Any], max_items: int,
                            pick: str | None = None) -> tuple[list[dict[str, str | None]], int]:
        self._hook("extract", items)
        found = resolve_all(self.root, items)
        if items.nth is not None:
            found = found[items.nth:items.nth + 1]
        found = _picked(found, pick)
        rows = []
        for item in found[:max_items]:
            row: dict[str, str | None] = {}
            for name, f in fields.items():
                target: El | None = item
                if f.locator is not None:
                    try:
                        target = resolve_one(self.root, f.locator, item)
                    except (ElementNotFound, AmbiguousLocator):
                        target = None
                row[name] = None if target is None else self._field(target, f)
            rows.append(row)
        return rows, len(found)

    def _field(self, el: El, f: Any) -> str | None:
        if f.attr is not None:
            return el.attrs.get(f.attr)
        if f.read == "value":
            return self.values.get(el.key or el.name, el.attrs.get("value"))
        return own_text(el) if f.read == "own_text" else own_text(el, role="\0")  # text: nested roles too

    async def screenshot(self) -> bytes:
        self._hook("screenshot", None)
        return b"\x89PNG fake screenshot of " + self.url.encode()

    async def read_signed_out(self, url: str, text: str, *, timeout_ms: int = 20_000) -> dict[str, Any]:
        self._hook("read_signed_out", url)
        self.site.signed_out_reads.append((url, text))
        table = self.site.signed_out
        if callable(table):
            return table(url, text)
        return {"visible": table.get(url, True), "status": 200}

    async def signal(self) -> Signal:
        return self.site.signal


def driver_factory(driver: FakeDriver, *, enter: Callable[[str], None] | None = None):
    """A DriverFactory that always hands out the same FakeDriver."""

    @asynccontextmanager
    async def factory(lane_id: str):
        if enter:
            enter(lane_id)
        driver.lane = lane_id
        yield driver

    return factory


class SimulatedCrash(BaseException):
    """Raised by a hook to model the process dying mid-step (nothing after it runs)."""


# ---------------------------------------------------------------- a social site

BASE = "https://social.example"


def social_site() -> FakeSite:
    """Home, a composer, a profile with a post count, and a post with nested comments that
    each have their own Like (the Facebook lesson from the proof of concept)."""
    site = FakeSite()
    site.state = {"posts": [], "liked": set(), "account": "biz-a"}

    def like_button(item_id: str) -> El:
        pressed = item_id in site.state["liked"]

        def toggle(s: FakeSite, el: El, d: FakeDriver) -> None:
            liked = s.state["liked"]
            liked.symmetric_difference_update({item_id})

        return El("button", "Like", text="Liked" if pressed else "Like", key=f"like-{item_id}",
                  attrs={"aria-pressed": "true" if pressed else "false"}, on_click=toggle)

    def home(s: FakeSite, d: FakeDriver) -> El:
        return El("document", "Home").add(El("main").add(
            El("heading", "Feed"),
            El("button", "New post", on_click=lambda s, el, d: f"{BASE}/compose"),
        ))

    def compose(s: FakeSite, d: FakeDriver) -> El:
        def share(s: FakeSite, el: El, d: FakeDriver) -> str:
            caption = d.values.get("caption", "")
            s.state["posts"].append({"id": str(len(s.state["posts"]) + 1), "caption": caption,
                                     "image": d.values.get("file")})
            return f"{BASE}/u/{s.state['account']}"

        return El("document", "Create post").add(El("dialog", "Create new post").add(
            El("button", "Select from computer", key="file"),
            El("textbox", "Write a caption...", key="caption"),
            El("button", "Share", on_click=share),
        ))

    def profile(s: FakeSite, d: FakeDriver) -> El:
        main = El("main", s.state["account"])
        main.add(El("heading", s.state["account"]), El("status", "Posts", text=f"{len(s.state['posts'])} posts"))
        for p in s.state["posts"]:
            main.add(El("article", f"Post {p['id']}", text=p["caption"],
                        attrs={"data-id": p["id"]}, css=(f"[data-id='{p['id']}']",)))
        return El("document", s.state["account"]).add(main)

    def post(s: FakeSite, d: FakeDriver) -> El:
        comments = [("c1", "Comment by Beata", "So pretty"), ("c2", "Comment by Ieva", "dekui")]
        art = El("article", "Post by Ieva", text="Lovely day at the lake").add(like_button("p1"))
        for cid, name, text in comments:
            art.add(El("article", name, text=text).add(like_button(cid)))
        return El("document", "Post").add(El("main").add(art))

    site.route(f"{BASE}/", home)
    site.route(f"{BASE}/compose", compose)
    site.route(f"{BASE}/u/*", profile)
    site.route(f"{BASE}/p/1", post)
    return site


def post_flow(**overrides: Any) -> dict[str, Any]:
    """A posting flow for social_site with a count-change readback on the profile."""
    flow = {
        "id": "biz-a.social.post", "title": "Post a photo", "lane": "biz-a/social", "effect": "commit",
        "domain": "social.example",
        "params": {"type": "object", "additionalProperties": False, "required": ["caption", "image"],
                   "properties": {"caption": {"type": "string", "maxLength": 2200, "description": "post text",
                                              "show": True},
                                  "image": {"type": "string", "description": "image file ref", "show": True}}},
        "steps": [
            {"type": "navigate", "url": f"{BASE}/u/biz-a"},
            {"type": "wait_for", "state": "any", "locator": {"role": "article", "within": {"role": "main"}},
             "save_as": "posts_before"},
            {"type": "navigate", "url": f"{BASE}/"},
            {"type": "click", "locator": {"role": "button", "name": "New post"}},
            {"type": "upload", "locator": {"role": "button", "name": "Select from computer"}, "param": "image"},
            {"type": "fill", "locator": {"role": "textbox", "name": "Write a caption..."}, "param": "caption",
             "readback": {"condition": {"kind": "value", "equals": "${caption}"}}},
            {"type": "pause", "min_ms": 300, "max_ms": 900},
            {"type": "click", "locator": {"role": "button", "name": "Share"}, "writes": True},
            {"type": "readback", "url": f"{BASE}/u/biz-a",
             "locator": {"role": "article", "within": {"role": "main"}},
             "condition": {"kind": "count", "changed_by": 1, "baseline": "posts_before"}},
        ],
    }
    flow.update(overrides)
    return flow


# ---------------------------------------------------------------- fake runtime services


class FakeLanes:
    def __init__(self, lanes: list[str] | None = None):
        self.info: dict[str, LaneInfo] = {}
        for lane in lanes or []:
            self.add(lane)

    def add(self, lane: str, state: LaneState = LaneState.COLD, reason: str | None = None) -> None:
        self.info[lane] = LaneInfo(spec=LaneSpec(id=lane), state=state, quarantine_reason=reason)

    def get(self, lane_id: str) -> LaneInfo:
        if lane_id not in self.info:
            raise LaneNotFound(f"lane {lane_id} not found")
        return self.info[lane_id]

    async def hold(self, lane_id: str, *, reason: str, until: datetime | None = None, by: str = "agentos") -> LaneInfo:
        """Like LaneManager.hold: agents stay off the lane until a human hands it back."""
        info = self.get(lane_id)
        info.control = LaneControl(state=ControlState.PAUSED, by=by, reason=reason, hold_until=until)
        info.hold = reason
        return info

    async def report_signal(self, lane_id: str, signal: Any, detail: str | None = None) -> LaneInfo:
        """Like LaneManager.report_signal under the default policy: a bad signal holds the lane."""
        return await self.hold(lane_id, reason=f"needs_human:{signal}")


class FakeApprovals:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def create(self, approval_id: str, **fields: Any) -> None:
        """An action card as the policy gate makes one: it binds the content's digest
        (agentos.policy.approvals.ActionBinding), which the runner checks again at dispatch."""
        from agentos.policy.approvals import BINDING_CALL, content_digest

        bound = {"kind": "action", "binding": BINDING_CALL, "content_digest": content_digest(fields.get("content"))}
        self.items[approval_id] = {"id": approval_id, "state": "pending", **bound, **fields}

    def set(self, approval_id: str, state: str) -> None:
        self.items[approval_id]["state"] = state

    async def get(self, approval_id: str) -> dict[str, Any]:
        if approval_id not in self.items:
            raise NotFound(approval_id)
        return self.items[approval_id]

    def consume(self, approval_id: str) -> bool:
        item = self.items.get(approval_id)
        if item is None or item["state"] != "approved":
            return False
        item["state"] = "consumed"
        return True


class FakePolicy:
    """authorize(principal, effect, ...): deny listed effects, ask approval above a ceiling.
    context_for_grant(...) is how the runner reaches it; `revoked` grant ids are refused."""

    def __init__(self, approvals: FakeApprovals | None = None, *, approve_above: Effect | None = Effect.DRAFT,
                 deny: set[Effect] | None = None):
        self.approvals = approvals
        self.approve_above = approve_above
        self.deny = deny or set()
        self.calls: list[dict[str, Any]] = []
        self.revoked: set[str] = set()
        self.grants: list[str | None] = []
        self._n = 0

    def context_for_grant(self, grant_id: str | None, *, principal: Principal | None = None,
                          tool: str | None = None, **kw: Any) -> FakeCtx:
        self.grants.append(grant_id)
        if grant_id in self.revoked:
            raise PolicyDenied(f"grant {grant_id} was revoked")
        return FakeCtx(None, principal=principal or Principal(kind="flow", id="runner"), policy=self)

    async def authorize(self, principal: Principal, effect: Effect, **kw: Any) -> None:
        self.calls.append({"principal": principal, "effect": effect, **kw})
        if effect in self.deny:
            raise PolicyDenied(f"{effect} is not allowed for {principal.id}")
        if self.approve_above is not None and effect.rank is not None and effect.rank > self.approve_above.rank:
            approved = [a for a in (self.approvals.items.values() if self.approvals else [])
                        if a.get("content") == kw.get("content") and a["state"] == "approved"]
            if approved:
                return
            self._n += 1
            aid = f"ap_{self._n}"
            if self.approvals is not None:
                from agentos.policy.approvals import named_target

                self.approvals.create(aid, content=kw.get("content"), summary=kw.get("summary"),
                                      lane=kw.get("lane_id"),
                                      flow_target=named_target("flow_run", kw.get("content"), kw.get("lane_id")))
            raise ApprovalPending(f"{effect} needs the owner's approval", approval_id=aid)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def notify(self, *, title: str, body: str, level: str = "info", data: dict | None = None) -> None:
        self.sent.append({"title": title, "body": body, "level": level, "data": data or {}})


OWNER = Principal(kind="human", id="owner", owner="owner")


class FakeCtx:
    """A ToolContext: records authorize calls; `policy` (if set) decides."""

    def __init__(self, runtime: Any, *, principal: Principal | None = None, policy: FakePolicy | None = None):
        self.runtime = runtime
        self.principal = principal or Principal(kind="agent", id="claude-code", thread="T-1")
        self.policy = policy
        self.authorized: list[dict[str, Any]] = []
        self.traces: list[dict[str, Any]] = []
        from agentos.policy.grants import fake_grant

        self.grant = fake_grant(self.principal.id)  # every lane: `policy` decides what this fake may do

    def can_access_lane(self, lane_id: str) -> bool:
        return self.grant.lane_allowed(lane_id)

    async def authorize(self, effect: Effect, *, lane_id: str | None = None, domain: str | None = None,
                        summary: str | None = None, content: Any = None, at: float | None = None) -> None:
        self.authorized.append({"effect": effect, "lane_id": lane_id, "domain": domain, "summary": summary,
                                "content": content})
        if self.policy is not None:
            await self.policy.authorize(self.principal, effect, lane_id=lane_id, domain=domain, summary=summary,
                                        content=content)

    def trace(self, kind: str, **fields: Any) -> None:
        self.traces.append({"kind": kind, **fields})


# ---------------------------------------------------------------- a test harness


class Harness:
    """Everything a runner test needs, on one home directory. Build a second Harness on the
    same home (and site) to simulate a restart: a fresh process and a fresh browser."""

    def __init__(self, home: Any, *, site: FakeSite | None = None, clock: FakeClock | None = None,
                 services: dict[str, Any] | None = None, driver: FakeDriver | None = None,
                 factory: Any = None, **options: Any):
        import random

        from agentos.core.runtime import Runtime
        from agentos.runner.service import build

        self.home = home
        self.clock = clock or FakeClock()
        self.site = site or social_site()
        self.driver = driver or FakeDriver(self.site)
        self.runtime = Runtime(**(services or {}))
        self.runner = build(self.runtime, home=home, driver_factory=factory or driver_factory(self.driver),
                            clock=self.clock, rng=random.Random(7), **options)
        self.flows = self.runtime.flows
        self.scheduler = self.runner.scheduler
        self.store = self.runner.store

    def save(self, definition: dict[str, Any], *, by: Any = None):
        """Save a flow; `by=OWNER` (or any principal) records who saved it."""
        return self.flows.save(definition, by=by)[0]

    def submit(self, flow_id: str, params: dict[str, Any] | None = None, **kw: Any):
        kw.setdefault("authorized", True)
        return self.runner.submit(flow_id=flow_id, params=params or {}, **kw)[0]

    async def run(self, flow_id: str, params: dict[str, Any] | None = None, **kw: Any):
        run = self.submit(flow_id, params, **kw)
        await self.runner.run_until_idle()
        return self.runner.get(run.id)
