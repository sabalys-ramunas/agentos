"""Snapshot hints on real Camoufox against the fixture site (run on the node).

Reading a hint must never change the page: a MutationObserver on the document and on every open
shadow root sees nothing, and the markup is byte-identical afterwards.
"""

from __future__ import annotations

import re
import time

import pytest

from agentos.node.interaction import PlaywrightInteraction
from agentos.node.snapshot import HINT_BUDGET_MS, HINT_LIMIT, collect_facts, find_refs

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]

# Attached to the document and to every open shadow root (the page's own shadow roots are
# invisible to a document observer). Only test harness state lives here.
WATCH_MUTATIONS_JS = """
() => {
  window.__agentosTestMutations = [];
  const log = (records) => {
    for (const r of records) window.__agentosTestMutations.push(r.type + ':' + (r.attributeName || ''));
  };
  const roots = [document];
  for (const el of document.querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
  for (const root of roots) {
    new MutationObserver(log).observe(root, {subtree: true, childList: true, attributes: true,
                                             characterData: true});
  }
}
"""

SHADOW_HTML_JS = """
() => Array.from(document.querySelectorAll('*'), (el) => (el.shadowRoot ? el.shadowRoot.innerHTML : ''))
  .join('|')
"""

HINT_RE = re.compile(r"^\s*- (?P<role>[a-z]+)\b.*\(hint: (?P<hint>[^)]*)\)")


@pytest.fixture
def ia() -> PlaywrightInteraction:
    return PlaywrightInteraction(allow_private_targets=True)  # fixtures are served on 127.0.0.1


def hinted(text: str) -> list[tuple[str, str]]:
    """(role, hint) for every outline line that carries a hint."""
    return [(m["role"], m["hint"]) for m in (HINT_RE.match(line) for line in text.splitlines()) if m]


async def test_hints_tell_the_unnamed_controls_apart(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/hints.html")
    snap = await ia.snapshot(page)

    assert sorted(hinted(snap.text)) == sorted([
        ("button", "upvote"),      # Reddit-style vote group: the boolean attribute
        ("button", "downvote"),
        ("button", "upvote"),      # web component: the label in its open shadow root
        ("button", "reply"),       # data-icon
        ("button", "open menu"),   # class name that names an action
        ("button", "hide post"),   # visually hidden text
        ("link", "home"),          # data-icon on a link
    ]), snap.text

    # Named controls and the password keep their outline, with no hint and no value.
    for named in ('button "Submit"', 'button "Share this post"', 'button "Save post"', 'button "Share"',
                  'button "upvote"', 'textbox "Pin"'):
        assert named in snap.text, named
    assert len(re.findall(r"^\s*- button \[ref=e\d+\]( \[cursor=pointer\])?$", snap.text, re.MULTILINE)) == 1
    assert "hunter2" not in snap.text and "reveal" not in snap.text
    assert "(hint:" not in next(line for line in snap.text.splitlines() if 'textbox "Pin"' in line)


async def test_reading_hints_leaves_the_page_byte_identical(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/hints.html")
    await page.evaluate(WATCH_MUTATIONS_JS)
    html_before = await page.evaluate("document.documentElement.outerHTML")
    shadow_before = await page.evaluate(SHADOW_HTML_JS)

    snap = await ia.snapshot(page)
    assert len(hinted(snap.text)) == 7  # the reads really happened

    await page.wait_for_timeout(400)  # give the page's own leak detector a few ticks
    assert await page.evaluate("document.documentElement.outerHTML") == html_before
    assert await page.evaluate(SHADOW_HTML_JS) == shadow_before
    assert await page.evaluate("window.__agentosTestMutations") == []


async def test_three_hundred_unnamed_buttons_get_eighty_hints_within_the_budget(page, ia):
    buttons = "".join(
        f'<button type="button" data-icon="item-{i}"><svg viewBox="0 0 10 10" aria-hidden="true">'
        f'<rect width="10" height="10"/></svg></button>'
        for i in range(300))
    await page.set_content(f"<!doctype html><html lang='en'><head><title>Many</title></head>"
                           f"<body><main>{buttons}</main></body></html>")

    started = time.perf_counter()
    snap = await ia.snapshot(page)
    snapshot_s = time.perf_counter() - started

    assert len(hinted(snap.text)) == HINT_LIMIT
    assert "(hint: item 79)" in snap.text and "(hint: item 80)" not in snap.text
    assert snap.text.count("- button [ref=") == 300

    refs = find_refs(snap.text, role="button")
    assert len(refs) == 300
    started = time.perf_counter()
    facts = await collect_facts(page, refs)
    reads_s = time.perf_counter() - started
    assert len(facts) == HINT_LIMIT
    assert reads_s < HINT_BUDGET_MS / 1000 + 0.5
    assert snapshot_s < HINT_BUDGET_MS / 1000 + 1.5


async def test_a_tight_budget_skips_the_rest_without_waiting(page, ia):
    buttons = "".join(f'<button type="button" data-icon="item-{i}"><svg aria-hidden="true"></svg></button>'
                      for i in range(120))
    await page.set_content(f"<!doctype html><html lang='en'><body><main>{buttons}</main></body></html>")
    snap = await ia.snapshot(page)
    refs = find_refs(snap.text, role="button")

    started = time.perf_counter()
    facts = await collect_facts(page, refs, budget_ms=1)
    elapsed = time.perf_counter() - started

    assert len(facts) < HINT_LIMIT
    assert elapsed < 0.5
