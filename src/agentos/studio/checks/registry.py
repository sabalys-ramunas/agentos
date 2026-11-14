"""The measured checks: their ids, what each looks for, and which run in the page script."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CheckInfo:
    id: str
    what: str
    chromium_only: bool = False
    in_page: bool = True      # run by page.js (one pass) rather than from Python


CHECKS: tuple[CheckInfo, ...] = (
    CheckInfo("layout.page_overflow", "the page scrolls sideways, and the element that causes it"),
    CheckInfo("layout.clipped", "a control cut by a container with overflow hidden"),
    CheckInfo("layout.occluded", "text or a control covered by another element"),
    CheckInfo("layout.text_overlap", "text drawn over other text"),
    CheckInfo("text.truncated", "text cut short: ellipsis, line clamp, or cut with no ellipsis"),
    CheckInfo("contrast.text", "text contrast against what is really behind it (WCAG 2.x)"),
    CheckInfo("target.size", "pointer targets under 24 by 24 px without spacing (WCAG 2.5.8)"),
    CheckInfo("name.missing", "controls and images with no accessible name"),
    CheckInfo("health.console", "errors in the console and uncaught exceptions", in_page=False),
    CheckInfo("health.requests", "requests that failed or answered 4xx or 5xx", in_page=False),
    CheckInfo("health.fonts", "web fonts that failed to load"),
    CheckInfo("health.images", "images with no reserved size, far oversized, or lazy on the largest paint"),
    CheckInfo("health.layout_shift", "cumulative layout shift", chromium_only=True, in_page=False),
    CheckInfo("health.long_tasks", "long tasks that block input", chromium_only=True, in_page=False),
    CheckInfo("theme.leak", "colours that stay fixed when the theme changes", in_page=False),
)

IDS: tuple[str, ...] = tuple(c.id for c in CHECKS)
BY_ID = {c.id: c for c in CHECKS}


def selected(check: str, only: list[str] | None) -> bool:
    """`only` holds check ids or prefixes ("layout", "health.", "contrast.text"); None runs all."""
    if not only:
        return True
    for p in only:
        p = p.strip().rstrip(".")
        if p and (check == p or check.startswith(p + ".")):
            return True
    return False


# The number that says how bad one instance is, and whether higher is worse. Used to pick the
# worst instance of a group and to tell `worse` from `still` in a delta.
METRIC: dict[str, tuple[str, bool]] = {
    "layout.page_overflow": ("overflow_px", True),
    "layout.clipped": ("visible_share", False),
    "layout.occluded": ("covered_share", True),
    "layout.text_overlap": ("overlap_px2", True),
    "text.truncated": ("hidden_share", True),
    "contrast.text": ("ratio", False),
    "target.size": ("spacing_short_px", True),
    "health.layout_shift": ("cls", True),
    "health.long_tasks": ("blocking_ms", True),
    "health.images": ("factor", True),
}
