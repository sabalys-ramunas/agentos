"""studio_status(): what this install can do, read once by an agent (studio-ax.md, rule 16).

A plain dict, read from the disk without starting a browser: which engines are there (and which
Chromium), whether Playwright and Pillow are installed, local or node, fonts, the pool's limits,
and for each thing missing the command that adds it. S7 turns it into a tool.
"""

from __future__ import annotations

from typing import Any

from openajar.studio.browser import engines
from openajar.studio.browser.environment import FONT_SET, NOT_PINNED
from openajar.studio.contracts import EngineName


def pillow_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("Pillow")
    except Exception:  # noqa: BLE001 - not installed is an answer
        return None


def studio_status(pool: Any = None, *, where: str | None = None) -> dict[str, Any]:
    """`pool`: the running ReviewPool, when there is one (its engines, limits and open pages)."""
    chromium = getattr(pool, "chromium", "auto")
    playwright = engines.playwright_version()
    found = engines.detect(chromium) if playwright else {
        e.value: {"present": False, "install": engines.STUDIO_EXTRA} for e in EngineName}
    running = pool.running() if pool is not None else {}
    for name, item in running.items():  # a started browser knows its exact version
        found[name] = {**found.get(name, {}), "present": True, **item, "running": True}
    pillow = pillow_version()
    missing = [f"{name}: {item['install']}" for name, item in found.items() if not item.get("present")]
    if pillow is None:
        missing.append(f"Pillow (image diffs and crops): {engines.STUDIO_EXTRA}")
    out: dict[str, Any] = {
        "where": where or getattr(pool, "where", "local"),
        "engines": found,
        "default_engine": EngineName.CHROMIUM.value,
        "chromium_preference": chromium,
        "playwright": playwright,
        "pillow": pillow,
        "fonts": {"pinned": FONT_SET is not None, "set": FONT_SET.name if FONT_SET else None,
                  "note": None if FONT_SET else NOT_PINNED},
        "measures": {"layout_shift": "chromium only", "long_tasks": "chromium only"},
    }
    if pool is not None:
        limits = pool.limits
        out["limits"] = {"max_pages": limits.max_pages, "call_timeout_s": limits.call_timeout_s,
                         "hold_timeout_s": limits.hold_timeout_s}
        out["open_pages"] = pool.open_pages
        out["state_dir"] = str(pool.state_dir)
    if missing:
        out["missing"] = missing
    ready = [name for name, item in found.items() if item.get("present")]
    if not playwright:
        out["next"] = f"Studio has no browser: install the studio extra ({engines.STUDIO_EXTRA})."
    elif not found["chromium"].get("present"):
        out["next"] = (f"Chromium, the default engine, is missing: run `{engines.install_command('chromium')}` "
                       "or install Google Chrome.")
    else:
        out["next"] = f"Ready: pages open in {', '.join(ready)}. Chromium is used unless a call names another engine."
    return out
