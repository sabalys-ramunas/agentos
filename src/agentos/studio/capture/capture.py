"""Capture: the same screens rendered the same way, kept as a full-page PNG and a layout manifest
per screen. Coded only against the `ReviewBrowser` protocol (contracts.py), never a browser.

A screen that fails is a `Shot` with `ok=False` and the reason; the other screens are kept and
the capture still returns and is saved (AX rule 6: never silently fewer screens).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from agentos.core.errors import InvalidRequest
from agentos.studio.capture.manifest import DEFAULT_CAP, read_manifest
from agentos.studio.capture.screens import screens as screen_set
from agentos.studio.contracts import Capture, ReviewBrowser, Screen, Shot, Target
from agentos.studio.store import StudioStore, iso_now, new_capture_id

log = logging.getLogger("agentos.studio")

MAX_IMAGE_HEIGHT = 16000   # CSS px; a taller page is kept to this height and the manifest says so
ERROR_CAP = 400


def _reason(e: BaseException) -> str:
    code = getattr(e, "code", None) or type(e).__name__
    msg = getattr(e, "message", None) or str(e) or "no detail"
    msg = " ".join(str(msg).split())
    text = f"{code}: {msg}"
    return text if len(text) <= ERROR_CAP else text[:ERROR_CAP - 1] + "…"


def _engine_version(page: Any) -> str | None:
    try:
        browser = page.context.browser
        version = browser.version if browser is not None else None
        name = browser.browser_type.name if browser is not None else None
    except Exception:  # noqa: BLE001 - only a label
        return None
    if not isinstance(version, str):
        return None
    return f"{name} {version}" if isinstance(name, str) else version


async def _one(browser: ReviewBrowser, target: Target, screen: Screen, store: StudioStore,
               manifest_cap: int) -> tuple[Shot, str | None]:
    async with browser.page(target, screen) as page:
        version = _engine_version(page)
        manifest = await read_manifest(page, cap=manifest_cap)
        kwargs: dict[str, Any] = {"full_page": True, "type": "png", "animations": "disabled", "caret": "hide"}
        if manifest.page_height > MAX_IMAGE_HEIGHT:
            kwargs["clip"] = {"x": 0, "y": 0, "width": max(manifest.page_width, 1), "height": MAX_IMAGE_HEIGHT}
            manifest.image_height = MAX_IMAGE_HEIGHT
        png = await page.screenshot(**kwargs)
    if not isinstance(png, (bytes, bytearray)) or not png:
        raise RuntimeError("the browser returned no image")
    image = await asyncio.to_thread(store.put_blob, bytes(png), "image/png")
    blob = await asyncio.to_thread(store.put_blob, manifest.model_dump_json().encode(), "application/json")
    return Shot(screen=screen, ok=True, image=image, manifest=blob), version


async def capture(browser: ReviewBrowser, target: Target, screens: Sequence[Screen] | None = None, *,
                  store: StudioStore, manifest_cap: int = DEFAULT_CAP) -> Capture:
    """One `Shot` per screen, in the order given (default: `screens()`), saved in `store`. A screen
    that fails is recorded with its reason and the others go on."""
    todo = list(screen_set() if screens is None else screens)
    if not todo:
        raise InvalidRequest("name at least one screen to capture")
    keys = [s.key for s in todo]
    if len(set(keys)) != len(keys):
        raise InvalidRequest("two screens have the same state, width and theme: name each once")
    shots: list[Shot] = []
    engine_version: str | None = None
    for screen in todo:
        try:
            shot, version = await _one(browser, target, screen, store, manifest_cap)
            engine_version = engine_version or version
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the reason is the result
            log.info("studio capture: screen %s of %s failed: %s", screen.key, target.url, e)
            shot = Shot(screen=screen, ok=False, error=_reason(e))
        shots.append(shot)
    pinned = getattr(browser, "fonts_pinned", False)
    cap = Capture(id=new_capture_id(), target=target, shots=shots, engine_version=engine_version,
                  fonts_pinned=pinned if isinstance(pinned, bool) else False, created_at=iso_now())
    return await asyncio.to_thread(store.save, cap)
