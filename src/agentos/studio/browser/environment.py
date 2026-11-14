"""The pinned environment a review page renders in, reported with every page (studio-ax.md, rule 7).

Pinned today: the engine and its version, the viewport and scale, locale and timezone, the clock
(every page starts at settle.PINNED_TIME), random numbers (seeded), motion (reduced, animations
and transitions off) and the caret (hidden).

Not pinned yet: fonts. No font set is bundled, so text renders in the fonts of the machine the
browser runs on, and a page's named fonts that are missing fall back to whatever that machine
has. Every report says so (`fonts_pinned: false`) and names the fonts that fell back.

The seam for a font set: FONT_SET below. When a set is bundled, the pool serves its faces through
`context.route` under a reserved host and adds `@font-face` rules for them in the init script
(settle.pins_script), before the page's own styles; `pinned()` then reports the set's name and
`fonts_pinned: true`, and `font_report` counts a family as pinned when the set has it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentos.studio.browser import settle


@dataclass(frozen=True)
class FontSet:
    """A bundled set of font files that stand in for the system's fonts (none is bundled yet)."""

    name: str
    version: str
    families: frozenset[str] = field(default_factory=frozenset)


FONT_SET: FontSet | None = None

NOT_PINNED = ("no font set is bundled yet: text renders in this machine's fonts, so the same page can render "
              "differently on another machine")


def pinned() -> dict[str, Any]:
    """What every page of the pool shares, engine aside."""
    fonts = FONT_SET
    return {
        "fonts_pinned": fonts is not None,
        "font_set": f"{fonts.name} {fonts.version}" if fonts else None,
        "clock": settle.PINNED_TIME.isoformat().replace("+00:00", "Z"),
        "random": f"seeded ({settle.RANDOM_SEED})",
        "motion": "reduced; animations and transitions off; caret hidden",
    }


def font_report(fell_back: list[dict[str, Any]]) -> dict[str, Any]:
    """The font part of a page's report: pinned or not, and why not."""
    if FONT_SET is None:
        return {"fonts": NOT_PINNED}
    missing = [f["family"] for f in fell_back if f["family"] not in FONT_SET.families]
    return {"fonts": f"pinned to {FONT_SET.name}" + (f"; not in the set: {', '.join(missing)}" if missing else "")}
