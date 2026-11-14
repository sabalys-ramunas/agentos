"""Connector operations on real Camoufox (Brief B of docs/design/connectors.md).

The fixture site is served under https://social.example/ (a reserved name, RFC 2606) by route
interception in a fresh browser context: every other request is aborted, nothing leaves the
node. Compiled connectors run through the real runner and PlaywrightFlowDriver: a commit that
checks acting as from the composer's actor link, runs the preflight and reads its post back;
a preflight that stops a rewritten caption with nothing posted; Share named per account
language; comments in shadow roots read by extract and for_each into typed, labelled output;
and acting as read from the site's identity cookie (set on the context, never requested).
"""

from __future__ import annotations

import asyncio
import copy
import html
import importlib.util
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

from agentos.core.models import ControlState, LaneControl, LaneInfo, LaneSpec, LaneState, RunStatus, utcnow
from agentos.core.runtime import Runtime
from agentos.node import flow_driver as fd
from agentos.node.interaction import PlaywrightInteraction
from agentos.runner.connectors.compile import compile_connector
from agentos.runner.connectors.service import ConnectorService
from agentos.runner.service import build
from agentos.runner.util import Clock

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]

_spec = importlib.util.spec_from_file_location(
    "connector_samples", Path(__file__).resolve().parent.parent / "unit" / "connector_samples.py")
samples = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = samples
_spec.loader.exec_module(samples)

BASE = "https://social.example"
LANE = "biz-a/social"
OWNER = {"kind": "human", "id": "owner", "owner": "owner"}
GIF = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
SHARE = {"en": "Share", "lt": "Bendrinti"}

COMMENTS = {
    "1": [("t1_a", "ana", "2026-11-04T08:12:00Z", "/p/1/c/t1_a", "First comment on one",
           [("t1_ar", "bo", "2026-11-04T08:20:00Z", "/p/1/c/t1_ar", "A reply inside it", [])]),
          ("t1_b", "cy", "2026-11-04T09:00:00Z", "https://evil.example/phish", "Click my link", [])],
    "2": [("t1_c", "dee", "2026-11-04T10:00:00Z", "/p/2/c/t1_c", "On the second post", [])],
}

SHADOW_JS = """<script>(function attach(root) {
  for (const t of root.querySelectorAll('template[data-shadow]')) {
    const host = t.parentNode; const sr = host.attachShadow({mode: 'open'});
    sr.append(t.content.cloneNode(true)); t.remove(); attach(sr);
  }
})(document);</script>"""


def _doc(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f"</head><body>{body}</body></html>")


class SocialFixture:
    """The fake social site as pages: a composer (actor link, a file input, a thumbnail once a
    file is chosen, a caption, Share in the account's language), the profile, and post pages
    whose comments are custom elements with their content in open shadow roots."""

    def __init__(self, *, lang: str = "en", posts: list[dict[str, Any]] | None = None):
        self.lang = lang
        self.posts: list[dict[str, Any]] = copy.deepcopy(posts or [])
        self.homoglyphs = False  # the page swaps a for a Cyrillic a while it is typed, as a site might
        self.requests: list[str] = []

    async def handle(self, route: Any) -> None:
        req = route.request
        self.requests.append(f"{req.method} {req.url}")
        if not req.url.startswith(BASE + "/"):
            await route.abort()
            return
        path = urlsplit(req.url).path
        if req.method == "POST" and path == "/api/posts":
            body = json.loads(req.post_data or "{}")
            self.posts.append({"id": str(len(self.posts) + 1), "caption": body.get("caption", ""),
                               "files": body.get("files", [])})
            await route.fulfill(status=200, content_type="application/json", body="{}")
            return
        page = self.page(path)
        if page is None:
            await route.fulfill(status=404, content_type="text/html", body=_doc("Not found", "<h1>Not found</h1>"))
            return
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=page)

    def page(self, path: str) -> str | None:
        if path == "/":
            return _doc("Home", "<main><h1>Feed</h1><button onclick=\"location.href='/compose'\">New post</button></main>")
        if path == "/compose":
            swap = "true" if self.homoglyphs else "false"
            return _doc("Create post", f"""
<div role="dialog" aria-label="Create new post">
  <a href="/u/biz-a" aria-label="Posting as biz-a">biz-a</a>
  <input type="file" id="file" aria-label="Select from computer">
  <div id="thumbs"></div>
  <textarea id="caption" aria-label="Write a caption..."></textarea>
  <button id="share">{SHARE[self.lang]}</button>
</div>
<script>
const file = document.getElementById('file'), thumbs = document.getElementById('thumbs');
const caption = document.getElementById('caption');
file.addEventListener('change', () => {{
  thumbs.replaceChildren(...Array.from(file.files, () => {{
    const i = document.createElement('img'); i.alt = 'Selected photo'; i.src = '{GIF}';
    i.width = 40; i.height = 40; return i; }}));
}});
caption.addEventListener('input', () => {{ if ({swap}) caption.value = caption.value.replace(/a/g, '\u0430'); }});
document.getElementById('share').addEventListener('click', () => {{
  const x = new XMLHttpRequest();  // synchronous: the post exists before anything else happens
  x.open('POST', '/api/posts', false);
  x.setRequestHeader('content-type', 'application/json');
  x.send(JSON.stringify({{caption: caption.value, files: Array.from(file.files, (f) => f.name)}}));
  location.href = '/u/biz-a';
}});
</script>""")
        if path == "/u/biz-a":
            arts = "".join(f'<article aria-label="Post {p["id"]}" data-id="{p["id"]}">{html.escape(p["caption"])} '
                           f'<a href="/p/{p["id"]}">Open</a></article>' for p in self.posts)
            return _doc("biz-a", f'<main aria-label="biz-a"><h1>biz-a</h1><p role="status">{len(self.posts)} posts</p>'
                                 f"{arts}</main>")
        if path.startswith("/p/") and path.count("/") == 2:
            pid = path.rsplit("/", 1)[1]
            inner = "".join(self.comment(*c, shadow=True) for c in COMMENTS.get(pid, []))
            return _doc(f"Post {pid}", f"<main><h1>Post {pid}</h1>{inner}</main>{SHADOW_JS}")
        return None

    def comment(self, cid: str, author: str, when: str, link: str, body: str, replies: list[Any], *,
                shadow: bool = False) -> str:
        inner = (f'<time datetime="{when}">{when[11:16]}</time><p>{html.escape(body)}</p>'
                 f'<a href="{html.escape(link)}">Permalink</a>'
                 + "".join(self.comment(*r) for r in replies))
        if shadow:
            inner = f"<template data-shadow>{inner}</template>"
        return (f'<x-comment role="article" aria-label="Comment by {author}" thingid="{cid}" author="{author}" '
                f'style="display:block">{inner}</x-comment>')


class Lanes:
    """A lane manager double around the test's page, with the lane manager's hold. Its signed-out
    read (AUD-22) is a fresh context of the test's browser on the same fixture site: no cookies.
    The real one, a throwaway browser with the lane's launch options, is tested in
    test_signed_out_node.py."""

    def __init__(self, page: Any, site: Any = None):
        self.page = page
        self.site = site
        self.signed_out_reads: list[str] = []
        self._control = LaneControl()
        self._lock = asyncio.Lock()
        self.holds: list[str] = []

    def get(self, lane_id: str) -> LaneInfo:
        return LaneInfo(spec=LaneSpec(id=lane_id), state=LaneState.WARM, control=self._control)

    def control(self, lane_id: str) -> LaneControl:
        return self._control

    async def hold(self, lane_id: str, *, reason: str, until: Any = None, by: str = "agentos") -> LaneInfo:
        self.holds.append(reason)
        self._control = LaneControl(state=ControlState.PAUSED, by=by, since=utcnow(), reason=reason)
        return self.get(lane_id)

    async def read_signed_out(self, lane_id: str, url: str, text: str, *, timeout_ms: int = 20_000) -> dict:
        from agentos.node.signed_out import read_visibility

        self.signed_out_reads.append(url)
        ctx = await self.page.context.browser.new_context()
        try:
            await ctx.route("**/*", self.site.handle)
            return await read_visibility(await ctx.new_page(), url, text, timeout_ms=timeout_ms)
        finally:
            await ctx.close()

    @asynccontextmanager
    async def using(self, lane_id: str, *, principal_kind: str = "agent"):
        fd.control_gate(self, lane_id)()
        async with self._lock:
            yield self.page


class Inbox:
    def __init__(self, root: Path):
        self.root = root

    def inbox_dir(self, lane_id: str) -> Path:
        return self.root


@pytest.fixture
def ia() -> PlaywrightInteraction:
    return PlaywrightInteraction(allow_private_targets=True)


@pytest_asyncio.fixture(loop_scope="session")
async def social(browser: Any) -> Any:
    """(page, fixture): a fresh context where only https://social.example/ answers, from Python."""
    fixture = SocialFixture()
    ctx = await browser.new_context()
    await ctx.route("**/*", fixture.handle)
    page = await ctx.new_page()
    try:
        yield page, fixture
    finally:
        await ctx.close()


def node_commit_op() -> dict[str, Any]:
    """The sample commit operation, acting as the asset the composer's actor link names."""
    op = samples.commit_op()
    op["acting_as"] = {"kind": "fb_page", "source": "href", "pattern": "^/u/([a-z0-9-]+)$",
                       "locator": {"role": "link", "within": {"role": "dialog"}}}
    steps = op["steps"]
    steps[3]["locator"] = {"attr": {"name": "type", "equals": "file"}, "within": {"role": "dialog"}}
    steps.insert(2, steps.pop(1))  # check acting as inside the composer, where the actor link is
    return op


def node_comments_op() -> dict[str, Any]:
    op = samples.comments_op()
    op["acting_as"] = {"kind": "fb_page", "source": "url", "pattern": r"^https://social\.example/u/([a-z0-9-]+)$"}
    return op


async def _runner(tmp_path: Path, page: Any, ia: PlaywrightInteraction, ops: dict[str, Any], site: Any = None):
    box = tmp_path / "inbox"
    box.mkdir(parents=True, exist_ok=True)
    (box / "photo.png").write_bytes(b"\x89PNG fixture")
    lanes = Lanes(page, site)
    rt = Runtime(lanes=lanes, interaction=ia, files=Inbox(box))
    runner = build(rt, home=tmp_path / "home", driver_factory=fd.driver_factory(rt, dwell_ms=(150, 400)),
                   clock=Clock(), poll_s=0.2, backoff_base_s=5.0, max_concurrent=1)
    fd.install_take_over_pause(runner)
    doc = samples.connector(ops)
    ConnectorService(rt.flows, sites=samples.SITES).install(compile_connector(doc, sites=samples.SITES), by=OWNER)
    return runner, lanes


async def _run(runner: Any, flow_id: str, params: dict[str, Any]) -> Any:
    """Bind the surface the way `agentos connector bind` does (its acts-as id and account language
    are the binding's, never the caller's), then run."""
    params = dict(params)
    if "acts_as_id" in params:  # a connector operation; a plain flow has no surface
        acts_as = {"kind": "fb_page", "id": params.pop("acts_as_id")}
        runner.surfaces.sites = runner.surfaces.sites or samples.SITES
        runner.surfaces.put({**samples.SURFACE, "acts_as": acts_as,
                             "account_language": params.pop("account_language")}, by=OWNER)
    run = runner.submit(flow_id=flow_id, params=params, lane=LANE, authorized=True)[0]
    await asyncio.wait_for(runner.run_until_idle(), 240)
    return runner.get(run.id)


CAPTION = "Autumn at the lake"
PARAMS = {"acts_as_id": "biz-a", "account_language": "en", "caption": CAPTION, "image": "photo.png"}


async def test_a_commit_connector_posts_verified_on_camoufox(social, ia, tmp_path):
    page, site = social
    runner, lanes = await _runner(tmp_path, page, ia, {"post_photo": node_commit_op()}, site)
    run = await _run(runner, "connector.social.post_photo", PARAMS)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, (run.error, run.proof)
    assert run.proof["public"]["visible"] is True and lanes.signed_out_reads == [f"{BASE}/u/biz-a"]
    assert site.posts == [{"id": "1", "caption": CAPTION, "files": ["photo.png"]}]
    assert run.output["data"] == {"post_url": f"{BASE}/u/biz-a"}
    steps = {r.path: r for r in runner.store.steps(run.id)}
    assert steps["2"].result["identity"]["id"] == "biz-a"
    assert all(c["passed"] for c in steps["5"].result["preflight"])
    assert lanes.holds == []


async def test_a_rewritten_caption_stops_at_the_preflight_with_nothing_posted(social, ia, tmp_path):
    page, site = social
    site.homoglyphs = True
    runner, lanes = await _runner(tmp_path, page, ia, {"post_photo": node_commit_op()}, site)
    run = await _run(runner, "connector.social.post_photo", PARAMS)
    assert run.status == RunStatus.FAILED, run.error
    assert run.error["code"] == "preflight_mismatch" and run.error["found"] == [CAPTION.replace("a", "\u0430")]
    assert run.error["discard"] == "done" and site.posts == []
    assert lanes.holds == []


async def test_share_is_found_by_the_account_language(social, ia, tmp_path):
    page, site = social
    site.lang = "lt"
    op = node_commit_op()
    op["pace"] = {"min_gap_minutes": 0, "per_day": 5}  # two posts in a row: the gap is not what this tests
    runner, lanes = await _runner(tmp_path, page, ia, {"post_photo": op}, site)
    run = await _run(runner, "connector.social.post_photo", {**PARAMS, "account_language": "lt"})
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, (run.error, run.proof)
    assert [p["caption"] for p in site.posts] == [CAPTION]
    # the same page with the binding in English: Share is not found and nothing more is posted. A
    # locator miss before the commit is the site changing shape (the operation is held after it).
    wrong = await _run(runner, "connector.social.post_photo", PARAMS)
    assert wrong.status == RunStatus.FAILED and wrong.error["code"] == "connector_contract_broken", wrong.error
    assert wrong.error["cause"] == "element_not_found"
    assert wrong.error["step"] == "6" and [p["caption"] for p in site.posts] == [CAPTION]
    assert lanes.holds == []  # a name in another language is a locator miss, not a wrong account


async def test_the_wrong_page_identity_fails_and_holds_the_lane(social, ia, tmp_path):
    page, site = social
    runner, lanes = await _runner(tmp_path, page, ia, {"post_photo": node_commit_op()}, site)
    run = await _run(runner, "connector.social.post_photo", {**PARAMS, "acts_as_id": "biz-b"})
    assert run.status == RunStatus.FAILED and run.error["code"] == "wrong_surface", run.error
    assert run.error["found"] == "biz-a" and run.error["expected"] == "biz-b"
    assert lanes.holds == ["needs_human:wrong_surface"] and site.posts == []


async def test_comments_in_shadow_roots_are_extracted_typed_and_labelled(social, ia, tmp_path):
    page, site = social
    site.posts = copy.deepcopy(samples.SAMPLE_POSTS)
    runner, lanes = await _runner(tmp_path, page, ia, {"list_comments": node_comments_op()})
    run = await _run(runner, "connector.social.list_comments", {"acts_as_id": "biz-a", "account_language": "en"})
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    data = run.output["data"]
    assert [c["id"] for c in data["comments"]] == ["t1_a", "t1_ar", "t1_b", "t1_c"]
    assert data["comments"][0] == {"id": "t1_a", "author": "ana", "created_at": "2026-11-04T08:12:00Z",
                                   "permalink": f"{BASE}/p/1/c/t1_a", "body": "First commen"}
    assert data["comments"][1]["body"] == "A reply insi"  # the reply's own paragraph, not its parent's
    assert data["comments"][2]["permalink"] is None and data["posts_seen"] == 2
    assert run.output["untrusted_fields"] == ["comments[].author", "comments[].body"]
    assert run.output["truncated"] is True and lanes.holds == []


async def test_acting_as_from_the_identity_cookie(page, site_url, ia, tmp_path):
    """sites.py reads Instagram's ds_user_id from the context's cookies. The cookies are set on
    the context directly: no request ever goes to instagram.com."""
    await page.context.add_cookies([
        {"name": "sessionid", "value": "fixture", "domain": ".instagram.com", "path": "/"},
        {"name": "ds_user_id", "value": "51234567", "domain": ".instagram.com", "path": "/"}])
    lanes = Lanes(page)
    rt = Runtime(lanes=lanes, interaction=ia)
    runner = build(rt, home=tmp_path, driver_factory=fd.driver_factory(rt, dwell_ms=(0, 0)), clock=Clock(),
                   poll_s=0.2, max_concurrent=1)
    flow = {"id": "fd.identity.check", "lane": LANE, "domain": "127.0.0.1",
            "params": {"type": "object", "properties": {"who": {"type": "string", "show": True}}},
            "steps": [{"type": "navigate", "url": f"{site_url}/flowfeed.html"},
                      {"type": "assert_identity", "site": "instagram", "expect": "${who}"}]}
    rt.flows.save(flow)
    ok = await _run(runner, "fd.identity.check", {"who": "51234567"})
    assert ok.status in (RunStatus.SUCCEEDED_UNVERIFIED, RunStatus.SUCCEEDED_VERIFIED), ok.error
    steps = {r.path: r for r in runner.store.steps(ok.id)}
    assert steps["1"].result["identity"] == {"acting_as": "account", "id": "51234567",
                                             "source": "cookie ds_user_id", "status": "match"}
    bad = await _run(runner, "fd.identity.check", {"who": "99999"})
    assert bad.status == RunStatus.FAILED and bad.error["code"] == "wrong_account", bad.error
    assert lanes.holds == ["needs_human:wrong_account"]
