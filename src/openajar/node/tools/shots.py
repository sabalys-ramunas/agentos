"""browser_screenshot's options on an environment's browser: a shot at another width, and keeping it
as proof of the work (stored by the environments service, which signs a link to it).

The node layer cannot import openajar.environments, so the service is found on the runtime by name
("environments") and called by its methods: `browser_environment(lane_id)` (the environment whose
browser this browser is, or None) and `keep_proof(env, png, ...)`.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from typing import Any

from openajar.core.errors import InvalidRequest

WIDTH_MIN, WIDTH_MAX = 320, 3840
TITLE_MAX = 120
REFLOW_S = 0.4  # after a width change: let the page lay itself out again before the shot

_NEXT_FRAMES = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"


def environment_of(ctx: Any, lane_id: str) -> Any:
    """The environment whose browser this browser is, or None (an account browser, or no environments)."""
    get = getattr(ctx.runtime, "get", None)
    svc = get("environments") if callable(get) else None
    find = getattr(svc, "browser_environment", None)
    if not callable(find):
        return None
    try:
        return find(lane_id)
    except Exception:  # noqa: BLE001 - not an environment's browser
        return None


def check_width(width: Any) -> int | None:
    if width is None:
        return None
    if isinstance(width, bool) or not isinstance(width, int | float) or not WIDTH_MIN <= int(width) <= WIDTH_MAX:
        raise InvalidRequest(f"width is the page width in CSS pixels, {WIDTH_MIN} to {WIDTH_MAX}, like 390 for a "
                             "phone or 1280 for a laptop")
    return int(width)


def check_title(proof: Any) -> str | None:
    if proof is None:
        return None
    title = " ".join(str(proof).split()) if isinstance(proof, str) else ""
    if not title:
        raise InvalidRequest('proof is a short title for the human, like "checkout page after the fix"')
    if len(title) > TITLE_MAX:
        raise InvalidRequest(f"proof is a title of at most {TITLE_MAX} characters")
    return title


def png_size(png: bytes) -> tuple[int | None, int | None]:
    """A PNG's width and height in pixels, from its header."""
    if len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", png[16:24])
        return int(w), int(h)
    return None, None


async def shot_at_width(interaction: Any, page: Any, width: int, *, full_page: bool) -> bytes:
    """The page laid out `width` CSS pixels wide (its height as it is), then back to its own size. The
    width only: no phone user agent and no touch."""
    size = await page.evaluate("() => ({w: innerWidth, h: innerHeight})")
    await page.set_viewport_size({"width": width, "height": int(size["h"])})
    try:
        await asyncio.sleep(REFLOW_S)
        with contextlib.suppress(Exception):  # a busy page is shot as it is
            await asyncio.wait_for(page.evaluate(_NEXT_FRAMES), 2.0)
        return await (interaction.screenshot(page, full_page=True) if full_page else interaction.screenshot(page))
    finally:
        await page.set_viewport_size({"width": int(size["w"]), "height": int(size["h"])})


def keep(ctx: Any, env: Any, png: bytes, *, title: str, page_url: str | None, on: str) -> dict:
    """Store the shot as proof (the environments service) and trace it in the audit log."""
    svc = ctx.runtime.get("environments")
    width, height = png_size(png)
    proof = svc.keep_proof(env, png, title=title, page_url=page_url, width=width, height=height)
    trace = getattr(ctx, "trace", None)
    if callable(trace):
        trace("browser_screenshot_proof", lane=on, environment=getattr(env, "id", None), proof=proof["id"],
              title=title, page_url=page_url, bytes=len(png))
    return proof
