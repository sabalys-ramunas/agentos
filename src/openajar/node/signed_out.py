"""A lane's object as a signed-out visitor sees it (AUD-22; the runner side is
openajar.runner.signed_out).

The check runs in a throwaway browser, never in the lane's own: a second context in the lane's
persistent browser is a Firefox container, and Juggler cannot make it private. Measured on
Camoufox 158 on the node: a page visited there was written to the lane profile's history
(places.sqlite, a real visit), its session store (the closed window, url and title, in the
window list a restore reads) and its cache; only cookies and storage are cleared when the
context closes. So the lane's running browser lends what makes it the same device and the same
network, and nothing else:

- the exact launch options its browser started with (the pinned Camoufox config, the pinned
  prefs, the proxy with its credentials, the network guard prefs and PAC), minus the profile:
  Playwright gives a non-persistent launch a fresh temporary profile and deletes it on close;
- a display of its own, the size of the lane's (its window has the pinned size), so nothing
  shows on the lane's screen or takes its focus;
- the lane's site list, as the egress guard on the throwaway context.

`read_visibility` opens the URL and waits for the text (whitespace folded, case ignored, the first
SNIPPET_CHARS characters, so a "...more" cut of a long caption still counts) in the page's
rendered text: `document.body.innerText`, which leaves out what CSS hides. It reports whether the
text showed, the HTTP status and where the page ended up. Nothing from the page is returned.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from openajar.core.logs import url_for_log

log = logging.getLogger("openajar.signed_out")

SNIPPET_CHARS = 60
MAX_TEXT_CHARS = 400_000  # of the page's text compared per poll
POLL_MS = 250

_SPACE = re.compile(r"\s+")

# Runs in Camoufox's isolated world (lesson 3): reads the rendered text, changes nothing.
_HAS_TEXT = """(needle) => {
  const body = document.body;
  if (!body) return false;
  const text = (body.innerText || "").slice(0, MAX).replace(/\\s+/g, " ").toLowerCase();
  return text.includes(needle);
}""".replace("MAX", str(MAX_TEXT_CHARS))


def snippet(text: str) -> str:
    """What must show: the text with whitespace folded and lower case, its first SNIPPET_CHARS
    characters, cut back to a whole word when the cut falls inside one."""
    folded = _SPACE.sub(" ", text).strip().lower()
    if len(folded) <= SNIPPET_CHARS:
        return folded
    cut = folded[:SNIPPET_CHARS]
    space = cut.rfind(" ")
    return cut[:space] if space >= SNIPPET_CHARS // 2 else cut


async def read_visibility(page: Any, url: str, text: str, *, timeout_ms: int) -> dict[str, Any]:
    """{"visible": True/False/None, "status", "final_url", "note"} for `url` in `page`."""
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    from openajar.node.browser_output import browser_error

    needle = snippet(text)
    if not needle:
        return {"visible": None, "note": "there is no text to look for"}
    started = time.monotonic()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except PlaywrightError as e:
        return {"visible": None, "note": f"the page did not load signed out: {browser_error(e, 'signed-out read')}"}
    status = response.status if response is not None else None
    left = max(1_000, timeout_ms - int((time.monotonic() - started) * 1000))
    try:
        await page.wait_for_function(_HAS_TEXT, arg=needle, polling=POLL_MS, timeout=left)
        visible = True
    except PlaywrightTimeout:
        visible = False
    except PlaywrightError as e:  # the page navigated away mid-check, or the browser went away
        return {"visible": None, "status": status, "final_url": url_for_log(page.url),
                "note": f"the signed-out page could not be read: {browser_error(e, 'signed-out read')}"}
    out: dict[str, Any] = {"visible": visible, "status": status, "final_url": url_for_log(page.url)}
    if not visible:
        out["note"] = (f"the text did not show within {left // 1000} s"
                       + (f" (HTTP {status})" if status and status >= 400 else ""))
    return out


def throwaway_options(launch_options: dict[str, Any], display: str) -> dict[str, Any]:
    """The lane's launch options for a non-persistent launch on `display`: the same config,
    prefs and proxy, no profile."""
    opts = {k: v for k, v in launch_options.items() if k not in ("user_data_dir", "persistent_context")}
    opts["env"] = {**(launch_options.get("env") or {}), "DISPLAY": display}
    return opts
