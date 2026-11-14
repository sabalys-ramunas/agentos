"""Waits and snapshot deltas on real Camoufox against the local fixture site (run on the node).

Waits resolve on DOM changes the page's own scripts make (an upload's progress, a section that
opens late, a confirm that pops up later, a burst of requests), never mutate the page, and leave
refs alone unless they re-read the tree. Deltas are measured against full snapshots."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from agentos.core.errors import RefNotFound
from agentos.core.models import ActionKind, Signal
from agentos.node.interaction import DialogGuard, PlaywrightInteraction, WaitFor
from agentos.node.snapshot import find_refs, outline_delta
from agentos.node.tools import page_text, render_snapshot

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]

POST = "Post by Ada Lovelace"

# Runs in Camoufox's isolated world: the observer and its log live there, invisible to the page.
WATCH_MUTATIONS_JS = """
() => {
  window.__agentosTestMutations = [];
  new MutationObserver((records) => {
    for (const r of records) window.__agentosTestMutations.push(r.type + ':' + (r.attributeName || ''));
  }).observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
}
"""


@pytest.fixture
def ia() -> PlaywrightInteraction:
    return PlaywrightInteraction(allow_private_targets=True)  # fixtures are served on 127.0.0.1


def one_ref(text: str, role: str, name: str, within: str | None = None) -> str:
    refs = find_refs(text, role=role, name=name, within=within)
    assert len(refs) == 1, f"expected one {role} {name!r} within {within!r}, got {refs}\n{text}"
    return refs[0]


def agent(name: str = "agent-1") -> SimpleNamespace:
    return SimpleNamespace(principal=SimpleNamespace(id=name, kind="agent"))


async def leak(page) -> str:
    return await page.evaluate("document.body.dataset.leak || ''")


async def test_a_wait_resolves_when_the_upload_finishes(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/composer.html")
    snap = await ia.snapshot(page)
    share = one_ref(snap.text, "button", "Share")
    await ia.act(page, share, ActionKind.CLICK)

    result = await ia.wait(page, WaitFor(gone_text=("Uploading",), for_text=("Your post was shared",),
                                         url_matches="posted=1", timeout_s=15))
    assert result.met and result.stopped == "met", result
    assert 0.3 < result.waited_s < 10 and result.reread
    assert result.conditions[0]["seen"] == "status: Your post was shared"  # for_text, then gone_text
    assert result.url.endswith("/composer.html?posted=1") and result.signal is Signal.OK
    assert await page.evaluate("document.getElementById('status').textContent") == "Your post was shared"
    assert await leak(page) == ""


async def test_a_timeout_reports_what_it_saw_last(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/composer.html")
    share = one_ref((await ia.snapshot(page)).text, "button", "Share")
    await ia.act(page, share, ActionKind.CLICK)
    result = await ia.wait(page, WaitFor(for_text=("Your post was shared",), timeout_s=0.4))
    assert not result.met and result.stopped == "timeout"
    assert result.conditions[0]["met"] is False and result.signal is Signal.OK
    assert 0.3 <= result.waited_s < 2


async def test_a_ref_wait_resolves_on_its_own_state_and_keeps_every_ref(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/composer.html")
    snap = await ia.snapshot(page)
    details, share = one_ref(snap.text, "button", "Details"), one_ref(snap.text, "button", "Share")
    clicked = await ia.act(page, share, ActionKind.CLICK)
    assert clicked.changed and "disabled" in clicked.detail

    done = await ia.wait(page, WaitFor(ref=share, ref_state="enabled", timeout_s=10))
    assert done.met and not done.reread and done.waited_s > 0.3  # the upload takes about 1.2 s
    assert done.conditions[0]["seen"].startswith("visible, enabled")
    assert await page.evaluate("document.getElementById('share').disabled") is False

    opened = await ia.act(page, details, ActionKind.CLICK)  # a ref from before the waits still works
    assert opened.changed  # the read-back waited for the page to expand it
    result = await ia.wait(page, WaitFor(ref=details, ref_state="expanded", timeout_s=10))
    assert result.met and "expanded" in result.conditions[0]["seen"]


async def test_a_text_wait_replaces_only_the_refs_of_changed_elements(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/feed.html")
    snap = await ia.snapshot(page)
    like = one_ref(snap.text, "button", "Like 12", within=POST)
    comments = one_ref(snap.text, "button", "Comments", within=POST)
    await ia.act(page, like, ActionKind.CLICK)

    result = await ia.wait(page, WaitFor(for_text=("Like 13",), exact=True, timeout_s=5))
    assert result.met and result.reread
    toggled = await ia.act(page, comments, ActionKind.CLICK)  # unchanged: its ref still works
    assert toggled.ok and toggled.changed
    with pytest.raises(RefNotFound):  # its name changed, so the re-read gave it a new ref
        await ia.act(page, like, ActionKind.CLICK)
    fresh = await ia.snapshot(page)
    assert re.search(r'button "Like 13" \[pressed\] \[ref=e\d+\]', fresh.text)


async def test_a_dialog_that_opens_while_waiting_is_reported_and_left_for_the_agent(page, site_url):
    # A short read-back, so the click returns before the page's confirm (400 ms later) opens.
    ia = PlaywrightInteraction(allow_private_targets=True, readback_timeout_ms=150)
    guard = DialogGuard(timeout=20).attach(page.context)
    try:
        await ia.navigate(page, f"{site_url}/composer.html")
        ask = one_ref((await ia.snapshot(page)).text, "button", "Discard")
        await ia.act(page, ask, ActionKind.CLICK)

        result = await ia.wait(page, WaitFor(for_text=("Discarded", "Kept"), timeout_s=10))
        assert result.stopped == "dialog" and not result.met and result.waited_s < 5
        assert result.dialog["type"] == "confirm" and result.dialog["message"] == "Discard this post?"
        assert guard.pending_on(page) is not None  # reported, not answered

        await guard.answer(page, accept=False)
        after = await ia.wait(page, WaitFor(for_text=("Kept",), timeout_s=5))
        assert after.met and after.conditions[0]["seen"] == "status: Kept"
    finally:
        await guard.close()


async def test_network_quiet_waits_for_the_pages_requests(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/composer.html")
    sync = one_ref((await ia.snapshot(page)).text, "button", "Sync")
    await ia.act(page, sync, ActionKind.CLICK)
    result = await ia.wait(page, WaitFor(network_quiet_ms=600, timeout_s=15))
    assert result.met and result.conditions[0]["seen"].startswith("quiet for")
    assert await page.evaluate("document.getElementById('status').textContent") == "Synced"


async def test_waits_never_mutate_the_page(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/feed.html")
    snap = await ia.snapshot(page)
    like = one_ref(snap.text, "button", "Like 12", within=POST)
    await page.evaluate(WATCH_MUTATIONS_JS)
    html_before = await page.evaluate("document.documentElement.outerHTML")

    result = await ia.wait(page, WaitFor(for_text=("never on this page",), gone_text=("Feed",),
                                         ref=like, ref_state="pressed", network_quiet_ms=100,
                                         url_matches="/feed.html", timeout_s=1.2))
    assert result.stopped == "timeout" and result.reread
    assert {k: c["met"] for c in result.conditions for k in c if k not in ("met", "seen", "matched")} == {
        "for_text": False, "gone_text": False, "for_ref_state": False, "network_quiet_ms": True,
        "url_matches": True}

    await page.wait_for_timeout(400)  # give the page's own leak detector a few ticks
    assert await page.evaluate("document.documentElement.outerHTML") == html_before
    assert await page.evaluate("window.__agentosTestMutations") == []
    assert await leak(page) == ""


async def test_settle_after_a_navigation_is_bounded(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/composer.html")
    loop = asyncio.get_running_loop()
    started = loop.time()
    await ia.settle(page)
    assert loop.time() - started < 3


# ---- deltas, measured -----------------------------------------------------------------------

def tokens(text: str) -> int:
    return round(len(text) / 4)  # the usual estimate for English and markup


def measure(label: str, full: str, other: str) -> None:
    print(f"\n{label}: full {len(full)} chars (~{tokens(full)} tokens), delta {len(other)} chars "
          f"(~{tokens(other)} tokens), {len(other) / len(full):.0%} of full")


async def test_a_like_is_a_one_line_delta_on_the_feed(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/feed.html")
    first = await ia.snapshot(page)
    full = render_snapshot(agent(), "personal/fixture", page, first, "full", labeled=False)
    like = one_ref(first.text, "button", "Like 12", within=POST)
    await ia.act(page, like, ActionKind.CLICK)

    delta = render_snapshot(agent(), "personal/fixture", page, await ia.snapshot(page), "delta")
    measure("feed.html, like a post", full, delta)
    assert delta.startswith("snapshot: delta, 1 changed, 0 new, 0 gone"), delta
    assert re.search(r'~ button "Like 13" \[active\] \[pressed\] \[ref=e\d+\] \[cursor=pointer\] '
                     r'\(was: button "Like 12"', delta)
    assert f'in article "{POST}"' in delta


async def test_a_like_on_a_big_feed_costs_a_few_percent_of_a_snapshot(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/bigfeed.html")
    first = await ia.snapshot(page)
    full = render_snapshot(agent(), "personal/fixture", page, first, "full", labeled=False)
    post = "Post by Grace Hopper"
    like = find_refs(first.text, role="button", within=post)[0]
    await ia.act(page, like, ActionKind.CLICK)

    delta = render_snapshot(agent(), "personal/fixture", page, await ia.snapshot(page), "delta")
    measure("bigfeed.html, like a post", full, delta)
    assert delta.startswith("snapshot: delta, 1 changed, 0 new, 0 gone"), delta[:600]
    assert f'in article "{post}"' in delta and "[pressed]" in delta
    assert len(delta) < len(full) / 10


async def test_load_more_on_a_big_feed_is_a_delta_of_new_posts(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/bigfeed.html")
    first = await ia.snapshot(page)
    full = render_snapshot(agent(), "personal/fixture", page, first, "full", labeled=False)
    await ia.act(page, one_ref(first.text, "button", "Load more"), ActionKind.CLICK)

    delta = render_snapshot(agent(), "personal/fixture", page, await ia.snapshot(page), "delta")
    measure("bigfeed.html, load more", full, delta)
    assert re.search(r"snapshot: delta, 0 changed, \d+ new, 0 gone", delta), delta[:600]
    assert '+ article "Post by Newcomer 1"' in delta and '+   button "Like 0" [ref=e' in delta


async def test_an_upload_flow_is_seen_through_deltas(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/composer.html")
    first = await ia.snapshot(page)
    full = render_snapshot(agent(), "personal/fixture", page, first, "full", labeled=False)
    await ia.act(page, one_ref(first.text, "button", "Share"), ActionKind.CLICK)
    progress = await ia.snapshot(page)
    shown = render_snapshot(agent(), "personal/fixture", page, progress, "delta")
    assert "Uploading" in shown and "[disabled]" in shown
    await ia.wait(page, WaitFor(for_text=("Your post was shared",), timeout_s=10))
    done = await ia.snapshot(page)
    delta = outline_delta(page_text(progress), page_text(done))
    assert delta is not None and "~ status: Your post was shared (was: status: Uploading" in delta.text
    measure("composer.html, upload done (raw delta lines)", full, delta.text)


async def test_an_unchanged_page_is_a_one_line_delta(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/bigfeed.html")
    first = await ia.snapshot(page)
    full = render_snapshot(agent(), "personal/fixture", page, first, "full", labeled=False)
    again = render_snapshot(agent(), "personal/fixture", page, await ia.snapshot(page), "delta")
    measure("bigfeed.html, nothing changed", full, again)
    assert "nothing changed" in again and "your refs still work" in again


# ---- through the MCP tools ------------------------------------------------------------------

class OnePageLanes:
    """A lane manager whose one lane is the test's page."""

    def __init__(self, page):
        from agentos.core.models import LaneInfo, LaneSpec

        self.page, self.info = page, LaneInfo(spec=LaneSpec(id="personal/fixture"))

    def get(self, lane_id: str):
        return self.info

    def list(self):
        return [self.info]

    def using(self, lane_id: str, *, principal_kind: str = "agent"):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def session():
            yield self.page

        return session()


async def test_the_tools_navigate_act_and_wait_with_outlines(page, site_url, ia):
    import json

    from mcp import Client

    from agentos.core.config import NodeConfig
    from agentos.core.runtime import Runtime
    from agentos.mcp.registry import build_mcp
    from agentos.node.tools import INSTRUCTIONS, UNTRUSTED, tool_specs

    runtime = Runtime(config=NodeConfig(), lanes=OnePageLanes(page), interaction=ia)
    on = "personal/fixture"

    def parts(result) -> tuple[dict, str]:
        assert not result.is_error, result.content[0].text
        head, _, outline = result.content[0].text.partition("\n\n")
        return json.loads(head), outline

    async with Client(build_mcp(runtime, [tool_specs], instructions=INSTRUCTIONS)) as client:
        nav, outline = parts(await client.call_tool("browser_navigate", {
            "on": on, "url": f"{site_url}/bigfeed.html", "snapshot": "full"}))
        assert nav["status"] == 200 and nav["signal"] == "ok"
        assert outline.startswith("snapshot: full\nlane: personal/fixture") and UNTRUSTED in outline
        like = find_refs(outline, role="button", within="Post by Alan Turing")[0]

        act, delta = parts(await client.call_tool("browser_act", {
            "on": on, "ref": like, "action": "click", "snapshot": "delta"}))
        assert act["ok"] and act["changed"] is True
        assert delta.startswith("snapshot: delta, 1 changed") and 'in article "Post by Alan Turing"' in delta

        more = find_refs(outline, role="button", name="Load more")[0]
        await client.call_tool("browser_act", {"on": on, "ref": more, "action": "click"})
        waited, seen = parts(await client.call_tool("browser_wait", {
            "on": on, "for_text": "Newcomer 2", "timeout_s": 5, "snapshot": "delta"}))
        assert waited["met"] is True and waited["conditions"][0]["seen"] == 'article "Post by Newcomer 2"'
        assert '+ article "Post by Newcomer 1"' in seen  # since the outline of the like, not before it

        quiet = (await client.call_tool("browser_wait", {"on": on, "network_quiet_ms": 300, "timeout_s": 5}))
        assert json.loads(quiet.content[0].text)["met"] is True
