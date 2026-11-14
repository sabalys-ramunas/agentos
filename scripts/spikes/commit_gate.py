#!/usr/bin/env python3
"""SPIKE, NOT PRODUCT CODE. Measurements behind docs/design/egress-guard.md section 8 (the commit gate).

On a throwaway Camoufox profile and loopback pages only (egress_guard.py's servers):

  cost   what the commit gate's route (a regex on the rule hosts, none of which is loopback) does
         to the HTTP cache and to page loads, held for a whole visit and held for a few seconds,
         next to no route and a catch-all route
  feed   a typical feed session (a feed, three posts read from it) with no route, with the drive's
         route on the site's host (the real LaneGuard), and with a route that covers another host
  gate   the real LaneGuard, RouteDispatcher and ActGate on a page that copies the dev site's
         composer (a caption box and a Share button that POSTs a CreatePost JSON body, with a fresh
         client id each time): held until approved, sent once approved, held again when the caption
         changes, held when the publish lags the click by 1.5 s (a captcha first) or leaves from a
         timer 3 s after the act, an unknown operation and a PUT held, a read named like a query let
         go, one click's two posts (the first approved: sent and reported, the second held), and a
         person who takes over during the drive never gated or charged. The server log says what
         left the browser.
  flow   a flow that takes the lane right after the agent's press armed a page timer that publishes
         3 s later: the flow reloads its page first (LaneGuard.flow), so the timer dies; a timer the
         agent armed in another tab is still held; the flow's own post from its page goes
         (not run by default: `... commit_gate.py flow`)
  acting who a publish acts as (docs/design/egress-guard.md 8.10), the loopback site read as one whose
         login can post as a Page (`actor_id`): the lane's own account, a Page whose surface posts
         through the lane (authorized on the surface), a Page no surface names (asked, unknown), a
         Page of a workspace the grant does not cover (refused, nothing sent), and a flow running on
         its own (LaneGuard.flow_identity): the unknown Page aborted, the surface's post sent
         (not run by default: `... commit_gate.py acting`)
  first  the cold first click FIRST_RUNS times (default 10), each on a fresh browser: whether the act
         that made the hold reports it, and when the request reached the driver after the click
         (not run by default: `... commit_gate.py first`)

Run it in the agentos-stealth-node image with this checkout mounted read-only:

  docker run --rm -v "$PWD":/work:ro -e PYTHONPATH=/work/src -e DISPLAY_NUM=99 -e BASE_PORT=6410 \\
    --entrypoint /opt/venv/bin/python agentos-stealth-node /work/scripts/spikes/commit_gate.py

Nothing here contacts any other host. Every process it starts is stopped at the end.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import egress_guard as eg  # the loopback servers and the launch of the egress spike

COMPOSER = """<!doctype html><html><head><title>Create post</title></head><body>
<textarea id="caption" aria-label="Write a caption..."></textarea>
<button id="share">Share</button><button id="read">More posts</button><div id="status"></div>
<script>
const CAPTCHA = '__FOREIGN__/recaptcha/reload';
const q = new URLSearchParams(location.search);
const lag = Number(q.get('lag') || 0), timer = Number(q.get('timer') || 0);
const op = q.get('op') || 'CreatePost', method = q.get('method') || 'POST', path = q.get('path') || '/api/graphql';
const post = (caption, name, how, to) => fetch(to, {method: how, headers: {'content-type': 'application/json'},
  body: JSON.stringify({operation: name, csrf_token: Math.random().toString(16).slice(2),
    variables: {input: {caption: caption, audience: 'public', actor_id: q.get('actor') || undefined,
      client_mutation_id: Math.random().toString(16).slice(2)}}})});
document.getElementById('share').onclick = () => {
  if (lag) fetch(CAPTCHA, {method: 'POST', body: 'x', mode: 'no-cors'});  // a captcha first, on its own host
  setTimeout(() => {   // lag: after a captcha POST; timer: the page's own timer, the press sent nothing
    const caption = document.getElementById('caption').value;
    post(caption, op, method, path)
      .then(r => { document.getElementById('status').textContent = 'shared ' + r.status; })
      .catch(e => { document.getElementById('status').textContent = "Couldn't share"; });
    if (q.get('two')) post(caption + ' (and a second post)', op, method, path);  // one click, two posts
  }, lag || timer);
};
document.getElementById('read').onclick = () => post('', 'FeedPaginationQuery', 'POST', '/api/graphql')
  .then(r => { document.getElementById('status').textContent = 'read ' + r.status; });
</script></body></html>"""
FEED_IMAGES = 20


def feed_html(name: str) -> bytes:
    """A feed (20 images) and its posts (the feed's first three images and two of their own)."""
    if name == "feed":
        imgs = range(1, FEED_IMAGES + 1)
    else:
        k = int(name.removeprefix("post"))
        imgs = [*range(1, 4), 100 + 2 * k, 101 + 2 * k]
    tags = "".join(f'<img src="/s/img{i}.png" width=10 height=10>' for i in imgs)
    return (f'<!doctype html><html><head><title>{name}</title><link rel="stylesheet" href="/s/a.css">'
            f'<script src="/s/b.js"></script></head><body><h1>{name}</h1>{tags}</body></html>').encode()


def patch_server() -> None:
    """The egress spike's handler, plus the composer page and a feed."""
    old_get = eg.Handler.do_GET

    def do_get(self: Any) -> None:
        if self.path.startswith("/compose"):
            body = COMPOSER.replace("__FOREIGN__", eg.FOREIGN).encode()
            self._send(200, body, {"Content-Type": "text/html", "Cache-Control": "no-store"})
            return
        if self.path.startswith("/feed/"):
            self._send(200, feed_html(self.path.split("?", 1)[0].removeprefix("/feed/")),
                       {"Content-Type": "text/html", "Cache-Control": "no-cache"})
            return
        old_get(self)

    eg.Handler.do_GET = do_get

    def do_put(self: Any) -> None:
        self.do_POST()

    eg.Handler.do_PUT = do_put


def posts_since(i: int) -> list[dict[str, Any]]:
    """What reached the server that could publish: every non-GET to /api (not the captcha's)."""
    return [r for r in eg.since(i) if r["method"] in ("POST", "PUT") and r["path"].startswith("/api/")]


async def cost(work: Path) -> dict[str, Any]:
    from agentos.node.egress import gate_pattern

    regex = gate_pattern()
    out: dict[str, Any] = {"pattern_chars": len(regex.pattern)}

    async def noop(route: Any) -> None:
        await route.fallback()

    for name in ("none", "gate_regex", "catchall"):
        cm, ctx = await eg.launch(work / f"profile-{name}")
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            if name == "gate_regex":
                await ctx.route(regex, noop)
            if name == "catchall":
                await ctx.route("**/*", noop)
            steps = [await eg.goto(page, "p1"), await eg.goto(page, "p2"), await eg.reload(page)]
            times = [(await eg.goto(page, "p2" if k % 2 else "p1"))["ms"] for k in range(12)]
            out[name] = {"p2_after_p1": steps[1]["server"], "reload": steps[2]["server"],
                         "median_ms": statistics.median(times[2:])}
        finally:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
    # the route held only for an act: what loads during it, and is it stored?
    cm, ctx = await eg.launch(work / "profile-window")
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await eg.goto(page, "p1")
        t0 = time.monotonic()
        await ctx.route(regex, noop)
        install_ms = (time.monotonic() - t0) * 1000
        during = await eg.goto(page, "p3")
        t0 = time.monotonic()
        await ctx.unroute(regex, noop)
        remove_ms = (time.monotonic() - t0) * 1000
        after = await eg.goto(page, "p4")
        again = await eg.goto(page, "p4")
        out["window"] = {"install_ms": round(install_ms, 1), "remove_ms": round(remove_ms, 1),
                         "p3_during": during["server"], "p4_after": after["server"], "p4_again": again["server"]}
    finally:
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)
    return out


FEED_SESSION = ("feed", "post1", "feed", "post2", "feed", "post3", "feed", "post1", "feed")


async def feed_cost(work: Path) -> dict[str, Any]:
    """A typical feed session (the feed, three posts read from it, back each time) with no route,
    with the gate's drive route on the site's own host (every request a round trip to Python, as the
    site's documents and API calls are), and with a route that matches nothing here (what images on
    a CDN host the route does not cover pay: only the cache)."""
    from agentos.node import commitgate
    from agentos.node.egress import Driver, EffectRule, LaneGuard, gate_pattern
    from agentos.node.routes import RouteDispatcher

    async def noop(route: Any) -> None:
        await route.fallback()

    out: dict[str, Any] = {}
    for name in ("none", "drive_on_site", "route_elsewhere"):
        cm, ctx = await eg.launch(work / f"profile-feed-{name}")
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            if name == "drive_on_site":
                guard = LaneGuard("spike/feed").attach(ctx, RouteDispatcher(ctx))
                guard.rules = (EffectRule("loop.post", "loop", "127.0.0.1", r"/api/graphql$", op=r"^CreatePost$"),)
                guard.drive(Driver(kind="agent", id="spike", grant_id="g", ceiling="commit"))
                async with guard.acting(gate=commitgate.ActGate(Ctx(), "spike/feed", call={}, submit_like=False)):
                    pass  # the drive starts; its route stays for the whole session
            if name == "route_elsewhere":
                await ctx.route(gate_pattern(), noop)
            i = eg.mark()
            t0 = time.monotonic()
            for step in FEED_SESSION:
                await page.goto(f"{eg.SITE}/feed/{step}", wait_until="load")
            ms = (time.monotonic() - t0) * 1000
            await asyncio.sleep(0.3)
            rows = eg.since(i)
            images = [r for r in rows if r["path"].startswith("/s/img")]
            out[name] = {"requests": len(rows), "bytes": sum(r["bytes"] for r in rows),
                         "image_fetches": len(images), "distinct_images": len({r["path"] for r in images}),
                         "ms": round(ms, 1)}
        finally:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
    return out


class Ctx:
    """Approves by content digest: what an owner approved goes, anything else waits."""

    def __init__(self) -> None:
        self.approved: set[str] = set()
        self.asked: list[str] = []
        self.runtime = None

    async def authorize(self, effect: Any, *, content: Any = None, **kw: Any) -> None:
        from agentos.core.errors import ApprovalPending
        from agentos.policy.approval_binding import content_digest

        digest = content_digest(content)
        if digest in self.approved:
            return
        self.asked.append(digest)
        raise ApprovalPending("waiting", approval_id=f"ap_{len(self.asked)}")

    def trace(self, kind: str, **fields: Any) -> None:
        pass


class Meter:
    """Counts what the guard charges outside the gate (the gate's authorize meters the rest)."""

    def __init__(self) -> None:
        self.charged = 0

    def charge(self, rule: Any, *, sent: bool) -> str | None:
        self.charged += 1
        return None

    def capped(self) -> bool:
        return False


async def gate(work: Path) -> dict[str, Any]:
    from agentos.node import commitgate
    from agentos.node.egress import Driver, EffectRule, LaneGuard
    from agentos.node.routes import RouteDispatcher

    commitgate.ASKED.clear()
    cm, ctx = await eg.launch(work / "profile-gate")
    out: dict[str, Any] = {}
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        guard = LaneGuard("spike/social").attach(ctx, RouteDispatcher(ctx))
        guard.rules = (EffectRule("loop.post", "loop", "127.0.0.1", r"/api/graphql$", op=r"^CreatePost$"),
                       EffectRule("loop.read.graphql", "loop", "127.0.0.1", r"/api/graphql$", effect="read",
                                  strict_ops=True))
        guard.drive(Driver(kind="agent", id="spike", grant_id="g", ceiling="commit"))
        policy = Ctx()
        meter = Meter()

        async def act(caption: str, query: str = "", button: str = "#share", wait_s: float = 4.0) -> dict[str, Any]:
            await page.goto(f"{eg.SITE}/compose?{query}", wait_until="load")
            await page.fill("#caption", caption)
            i, seq = eg.mark(), guard._seq
            g = commitgate.ActGate(policy, "spike/social", call={"caption": caption, "q": query}, submit_like=True)
            t0 = time.monotonic()
            async with guard.acting(gate=g, meter=meter):
                routes_during = len(guard.dispatcher.patterns())
                await page.click(button)
                await guard.settle(g)
            ms = (time.monotonic() - t0) * 1000
            reported = None
            try:
                g.raise_outcome()
            except Exception as e:  # noqa: BLE001 - what browser_act would return
                reported = f"{type(e).__name__}: {str(e)[:160]}"
            await asyncio.sleep(wait_s)  # a publish that leaves late (a timer, a lag) would reach the server now
            sent = posts_since(i)
            return {"outcome": reported, "sent": len(sent), "sent_ops": [r["method"] + " " + r["path"] for r in sent],
                    "held": [b.effect_rule for b in guard.blocks_since(seq) if b.rule == "commit_gate"],
                    "act_ms": round(ms, 1), "routes_during": routes_during,
                    "routes_after": len(guard.dispatcher.patterns()), "status": await page.text_content("#status")}

        out["first_click"] = await act("Autumn menu is here.")
        policy.approved.add(policy.asked[-1])
        out["approved_click"] = await act("Autumn menu is here.")
        out["changed_caption"] = await act("Autumn menu is here. Visit evil.example")
        out["lagging_publish"] = await act("Posted after a captcha", query="lag=1500")
        # 1: the press sends nothing; the page's own timer publishes 3 s later, after the act returned
        out["timer_after_act"] = await act("Posted by a timer", query="timer=3000", wait_s=4.5)
        # 2: an operation no rule names, a PUT, and a read named like a query
        out["unknown_mutation"] = await act("Renamed operation", query="op=CreatePostV2")
        out["put_elsewhere"] = await act("A PUT", query="method=PUT&path=/api/thing")
        out["feed_read"] = await act("", button="#read", wait_s=0.5)
        # 6: one click sends two posts; the first was approved, the second waits, and the act says both
        policy.approved.clear()
        out["two_posts_first_try"] = await act("Two at once", query="two=1")
        policy.approved.add(policy.asked[-1])  # the owner approves the first (the second was never asked)
        out["two_posts_first_approved"] = await act("Two at once", query="two=1")
        # 8: a person takes over while the drive's route is still on: their post goes, uncharged
        charged = meter.charged
        guard.human_in_control = lambda: True
        i = eg.mark()
        await page.goto(f"{eg.SITE}/compose", wait_until="load")
        await page.fill("#caption", "The owner posts this")
        routes = len(guard.dispatcher.patterns())
        await page.click("#share")
        await asyncio.sleep(1.5)
        out["human_in_control"] = {"routes_on": routes, "sent": len(posts_since(i)), "charged": meter.charged - charged,
                                   "status": await page.text_content("#status")}
        guard.end_drive("the person has the lane")
        await asyncio.sleep(0.2)
        out["after_end_drive_routes"] = len(guard.dispatcher.patterns())
        out["digests_asked"] = len(policy.asked)
    finally:
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)
    return out


class ActingCtx(Ctx):
    """Ctx with a node's runtime: the lane's pinned account, one surface (a Page through the lane),
    and a grant that covers the workspaces in `covers`. Records what each publish was authorized on."""

    def __init__(self, covers: set[str]) -> None:
        super().__init__()
        from types import SimpleNamespace

        from agentos.core.models import AccountRef, ActsAs, LaneSpec, Surface

        self.covers = covers
        self.on: list[tuple[str, str | None]] = []
        surface = Surface(id="biz-a/loop-page", site="loop", lane="spike/social",
                          acts_as=ActsAs(kind="fb_page", id="100089000000001", name="biz-a"))
        pinned = AccountRef(site="loop", handle="me", user_id="me-1")
        services = {"surfaces": SimpleNamespace(list=lambda: [surface]),
                    "lanes": SimpleNamespace(get=lambda lane: SimpleNamespace(
                        spec=LaneSpec(id="spike/social", account=pinned)))}
        self.runtime = SimpleNamespace(get=services.get)

    def can_access_lane(self, lane: str) -> bool:
        return lane.split("/", 1)[0] in self.covers

    async def authorize(self, effect: Any, *, content: Any = None, lane_id: str | None = None, **kw: Any) -> None:
        acts = (content or {}).get("record", {}).get("acts_as") or {}
        self.on.append((str(lane_id), acts.get("status")))
        if acts.get("status") == "unrecognized":  # what the policy does: always a person
            from agentos.core.errors import ApprovalPending

            raise ApprovalPending("an identity agentos does not know", approval_id="ap_unknown")
        # Ask me first off: a known identity runs on its own


async def acting_spike(work: Path) -> dict[str, Any]:
    from agentos.node import acting, commitgate
    from agentos.node.egress import Driver, EffectRule, LaneGuard
    from agentos.node.routes import RouteDispatcher

    acting.ACTING["loop"] = acting.ActingSpec("loop", True, "fb_page",
                                              (acting.ActingField("variables.input.actor_id", "actor"),))
    commitgate.ASKED.clear()
    cm, ctx = await eg.launch(work / "profile-acting")
    out: dict[str, Any] = {}
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        guard = LaneGuard("spike/social").attach(ctx, RouteDispatcher(ctx))
        guard.rules = (EffectRule("loop.post", "loop", "127.0.0.1", r"/api/graphql$", op=r"^CreatePost$"),)
        guard.drive(Driver(kind="agent", id="spike", grant_id="g", ceiling="commit"))

        async def act(policy: ActingCtx, caption: str, actor: str) -> dict[str, Any]:
            await page.goto(f"{eg.SITE}/compose?actor={actor}", wait_until="load")
            await page.fill("#caption", caption)
            i = eg.mark()
            g = commitgate.ActGate(policy, "spike/social", call={"caption": caption, "a": actor}, submit_like=True)
            async with guard.acting(gate=g):
                await page.click("#share")
                await guard.settle(g)
            reported = None
            try:
                g.raise_outcome()
            except Exception as e:  # noqa: BLE001 - what browser_act would return
                reported = f"{type(e).__name__}: {str(e)[:200]}"
            await asyncio.sleep(1.0)
            return {"outcome": reported, "sent": len(posts_since(i)), "authorized_on": policy.on[-1:],
                    "acts_as": g.acts_as}

        both = ActingCtx({"spike", "biz-a"})
        out["own_account"] = await act(both, "Own post.", "me-1")
        out["page_surface"] = await act(both, "As the Page.", "100089000000001")
        out["page_unknown"] = await act(both, "As a stranger Page.", "100089999999999")
        out["page_not_in_grant"] = await act(ActingCtx({"spike"}), "As the Page, personal grant.", "100089000000001")
        guard.end_drive("the agent let go")
        await asyncio.sleep(0.2)
        # a flow running on its own on the lane: who its publishes act as is checked on the route
        check = commitgate.flow_identity_check(both.runtime, "spike/social", guard)
        flow: dict[str, Any] = {"check": check is not None}
        if check is not None:
            async with guard.flow(page), guard.flow_identity(check):
                flow["routes_during"] = len(guard.dispatcher.patterns())
                for name, actor in (("unknown", "100089999999999"), ("surface", "100089000000001")):
                    await page.goto(f"{eg.SITE}/compose?actor={actor}", wait_until="load")
                    await page.fill("#caption", f"From a flow, {name}")
                    i, seq = eg.mark(), guard._seq
                    await page.click("#share")
                    await asyncio.sleep(1.5)
                    flow[name] = {"sent": len(posts_since(i)),
                                  "blocked": [b.rule for b in guard.blocks_since(seq)],
                                  "status": await page.text_content("#status")}
            await asyncio.sleep(0.2)
            flow["routes_after"] = len(guard.dispatcher.patterns())
        out["flow"] = flow
    finally:
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)
    return out


async def flow_window(work: Path) -> dict[str, Any]:
    """An agent's press arms a page timer (publishes 3 s later), then a flow takes the lane."""
    from agentos.node import commitgate
    from agentos.node.egress import Driver, EffectRule, LaneGuard
    from agentos.node.routes import RouteDispatcher

    commitgate.ASKED.clear()
    cm, ctx = await eg.launch(work / "profile-flow")
    out: dict[str, Any] = {}
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        other = await ctx.new_page()
        guard = LaneGuard("spike/social").attach(ctx, RouteDispatcher(ctx))
        guard.rules = (EffectRule("loop.post", "loop", "127.0.0.1", r"/api/graphql$", op=r"^CreatePost$"),)
        guard.drive(Driver(kind="agent", id="spike", grant_id="g", ceiling="commit"))
        policy = Ctx()

        async def press(p: Any, caption: str, query: str) -> None:
            await p.goto(f"{eg.SITE}/compose?{query}", wait_until="load")
            await p.fill("#caption", caption)
            g = commitgate.ActGate(policy, "spike/social", call={"caption": caption, "q": query}, submit_like=True)
            async with guard.acting(gate=g):
                await p.click("#share")
                await guard.settle(g)

        i, seq = eg.mark(), guard._seq
        await press(page, "Armed on the flow's page", "timer=3000")
        await press(other, "Armed in another tab", "timer=2000")
        t0 = time.monotonic()
        async with guard.flow(page):
            out["reload_ms"] = round((time.monotonic() - t0) * 1000, 1)
            await asyncio.sleep(4.0)  # both timers would have fired by now
            out["during_flow_sent"] = [r["method"] + " " + r["path"] for r in posts_since(i)]
            out["during_flow_held"] = [b.effect_rule for b in guard.blocks_since(seq) if b.rule == "commit_gate"]
            j = eg.mark()
            await page.goto(f"{eg.SITE}/compose", wait_until="load")  # the flow's own post, from its page
            await page.fill("#caption", "The flow's own post")
            await page.click("#share")
            await asyncio.sleep(1.5)
            out["flow_post_sent"] = len(posts_since(j))
        out["after_flow_routes"] = len(guard.dispatcher.patterns())
        guard.end_drive("done")
    finally:
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)
    return out


async def first_clicks(work: Path, runs: int) -> dict[str, Any]:
    """The cold first click, `runs` times, each on a fresh browser: does the act that made the hold
    report it (approval_pending), and when (after the click returned) did the request reach the
    driver's request event and the route handler."""
    from agentos.node import commitgate
    from agentos.node.egress import Driver, EffectRule, LaneGuard
    from agentos.node.routes import RouteDispatcher

    rows = []
    for n in range(runs):
        commitgate.ASKED.clear()
        cm, ctx = await eg.launch(work / f"profile-first-{n}")
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            guard = LaneGuard("spike/social").attach(ctx, RouteDispatcher(ctx))
            guard.rules = (EffectRule("loop.post", "loop", "127.0.0.1", r"/api/graphql$", op=r"^CreatePost$"),)
            guard.drive(Driver(kind="agent", id="spike", grant_id="g", ceiling="commit"))
            seen: dict[str, float] = {}
            on_request, route_guard = guard._on_request, guard._route_handler

            def heard(request: Any, seen: dict = seen, on_request: Any = on_request) -> None:
                if request.method == "POST":
                    seen.setdefault("event", time.monotonic())
                on_request(request)

            async def routed(route: Any, request: Any, seen: dict = seen, route_guard: Any = route_guard) -> bool:
                if request.method == "POST":
                    seen.setdefault("route", time.monotonic())
                return await route_guard(route, request)

            guard._on_request = heard  # type: ignore[method-assign]
            guard._route_handler = routed  # type: ignore[method-assign]
            await page.goto(f"{eg.SITE}/compose", wait_until="load")
            await page.fill("#caption", "Autumn menu is here.")
            i = eg.mark()
            g = commitgate.ActGate(Ctx(), "spike/social", call={"n": n}, submit_like=True)
            async with guard.acting(gate=g, meter=Meter()):
                await page.click("#share")
                clicked = time.monotonic()
                await guard.settle(g)
            done = time.monotonic()
            try:
                g.raise_outcome()
                outcome = "ok"
            except Exception as e:  # noqa: BLE001
                outcome = type(e).__name__
            await asyncio.sleep(1.5)
            rows.append({"outcome": outcome, "sent": len(posts_since(i)),
                         "event_ms": round((seen["event"] - clicked) * 1000) if "event" in seen else None,
                         "route_ms": round((seen["route"] - clicked) * 1000) if "route" in seen else None,
                         "act_ms": round((done - clicked) * 1000)})
        finally:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            shutil.rmtree(work / f"profile-first-{n}", ignore_errors=True)
    hits = sum(r["outcome"] == "ApprovalPending" for r in rows)
    return {"runs": rows, "reported_by_the_act": f"{hits}/{len(rows)}", "leaked": sum(r["sent"] for r in rows)}


async def main_async() -> None:
    patch_server()
    work = Path(tempfile.mkdtemp(prefix="gate-spike-"))
    xvfb = subprocess.Popen(["Xvfb", f":{eg.DISPLAY_NUM}", "-screen", "0", "1280x900x24", "-nolisten", "tcp"],  # noqa: ASYNC220
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    servers = eg.serve()
    report: dict[str, Any] = {}
    try:
        await asyncio.sleep(1.0)
        from importlib.metadata import version

        report["camoufox"], report["playwright"] = version("camoufox"), version("playwright")
        parts = sys.argv[1:] or ["cost", "feed", "gate"]
        if "cost" in parts:
            report["cost"] = await cost(work)
        if "feed" in parts:
            report["feed"] = await feed_cost(work)
        if "gate" in parts:
            report["gate"] = await gate(work)
        if "flow" in parts:
            report["flow"] = await flow_window(work)
        if "acting" in parts:
            report["acting"] = await acting_spike(work)
        if "first" in parts:
            report["first_click"] = await first_clicks(work, int(os.environ.get("FIRST_RUNS", "10")))
    finally:
        for s in servers:
            s.shutdown()
        xvfb.terminate()
        with contextlib.suppress(Exception):
            xvfb.wait(5)
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    os.environ.setdefault("DISPLAY_NUM", "99")
    asyncio.run(main_async())
