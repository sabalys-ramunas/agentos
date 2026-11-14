"""Capture accessibility outlines from fixture pages with real Chrome, into tests/fixtures/outlines.

    uv run python scripts/capture_outlines.py

Runs agentos.node.snapshot.take_snapshot, the code browser_snapshot uses, against local files
only. The tests of agentos.runner.siteapis.discover read what this writes, so a change to the
outline format shows up there once the outlines are captured again.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from agentos.node.snapshot import take_snapshot

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "tests" / "fixtures" / "site"
OUT = ROOT / "tests" / "fixtures" / "outlines"
PAGES = ["siteapi/orders.html", "siteapi/cards.html", "siteapi/listings.html", "siteapi/feed.html", "siteapi/plain.html",
         "siteapi/hostile.html", "flowfeed.html", "nestedfeed.html", "bigfeed.html", "index.html", "form.html"]


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        for name in PAGES:
            await page.goto((SITE / name).as_uri())
            snap = await take_snapshot(page, max_chars=200_000)
            (OUT / (Path(name).stem + ".txt")).write_text(snap.text + "\n")
            print(f"{name}: {len(snap.text)} chars, {snap.ref_count} refs, truncated={snap.truncated}", file=sys.stderr)
        (OUT / "captured.json").write_text(json.dumps({"browser": browser.version, "pages": PAGES}, indent=2) + "\n")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
