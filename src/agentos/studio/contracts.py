"""Shared types for agentos.studio: what the builders of the browser, capture, checks, tokens,
diff and tools agree on. See docs/design/studio.md, part 6, and docs/design/studio-ax.md.

Nothing here imports playwright or PIL. A page is typed `Any`: it is a
`playwright.async_api.Page`, imported only where it is used.
"""

from __future__ import annotations

import hashlib
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- where and how a page is shown -------------------------------------------------------


class EngineName(StrEnum):
    CHROMIUM = "chromium"
    FIREFOX = "firefox"
    WEBKIT = "webkit"


class Theme(StrEnum):
    LIGHT = "light"
    DARK = "dark"


class ThemeSwitch(_Strict):
    """How a site is put into a theme. `media` sets prefers-color-scheme and nothing else; the
    others also set an attribute, a class or a storage key before the page loads. The browser
    reads the theme back and fails the call when it did not apply."""

    how: Literal["media", "attr", "class", "storage"] = "media"
    name: str | None = None      # the attribute, or the storage key
    light: str | None = None     # its value for light (or the class name)
    dark: str | None = None


class Step(_Strict):
    """One action on the way to a state (an open dialog, a filled form)."""

    do: Literal["click", "fill", "press", "hover", "wait_for", "goto"]
    selector: str | None = None
    value: str | None = None


class Screen(_Strict):
    """One rendering: a state at a width in a theme."""

    width: int = Field(ge=240, le=3840)
    height: int = Field(default=900, ge=240, le=4000)
    theme: Theme = Theme.LIGHT
    state: str = "default"
    scale: float = Field(default=1.0, ge=1.0, le=3.0)

    @property
    def key(self) -> str:
        return f"{self.state}@{self.width}-{self.theme.value}"


class Target(_Strict):
    """What to open. `states` maps a state name to the steps that reach it from a fresh load;
    "default" needs no entry."""

    url: str
    engine: EngineName = EngineName.CHROMIUM
    theme_switch: ThemeSwitch = Field(default_factory=ThemeSwitch)
    wait_for: str | None = None                      # a selector that must be visible
    states: dict[str, list[Step]] = Field(default_factory=dict)
    locale: str = "en-US"
    timezone: str = "UTC"


DEFAULT_WIDTHS = (360, 768, 1280)


class ReviewBrowser(Protocol):
    """The browser pool (S1). `page` yields a playwright Page at the target, in the screen's
    size, theme and state, settled: fonts ready, network quiet, motion off, time pinned. It
    raises a studio error when the page did not load, is blank, or the theme did not apply."""

    fonts_pinned: bool               # False until a font set is bundled; capture records it

    def page(self, target: Target, screen: Screen) -> AbstractAsyncContextManager[Any]: ...

    def report(self, page: Any) -> dict[str, Any]:
        """The environment a page from `page()` was rendered in: engine, version, viewport and
        theme read back, and the fonts that fell back. Empty for a page from elsewhere."""
        ...

    async def close(self) -> None: ...


# --- what was captured --------------------------------------------------------------------


class Rect(_Strict):
    x: float
    y: float
    w: float
    h: float


class ComponentRef(_Strict):
    name: str
    source: str | None = None        # "src/components/Card.tsx:42" when known
    via: Literal["attr", "class", "catalogue", "fibre", "none"] = "none"


class Element(_Strict):
    """One visible element in a layout manifest (S2). `key` is stable between two captures of
    the same page: a test id, else role and name path, else a DOM path with text."""

    key: str
    tag: str
    role: str | None = None
    name: str | None = None
    text: str | None = None          # capped; page text is data
    rect: Rect
    styles: dict[str, str] = Field(default_factory=dict)   # the authored properties only
    component: ComponentRef | None = None
    parent: str | None = None        # key of the parent element in the manifest


class Shot(_Strict):
    screen: Screen
    ok: bool = True
    error: str | None = None         # why this screen has no image
    note: str | None = None          # what a reader must know: "image cut at 16000 px of 23000"
    image: str | None = None         # blob id in the store: "sha256:<hex>"
    manifest: str | None = None      # blob id of the JSON manifest


class Capture(_Strict):
    id: str                          # "cap_<...>"
    target: Target
    shots: list[Shot]
    engine_version: str | None = None
    fonts_pinned: bool = False
    created_at: str                  # ISO 8601, UTC


# --- findings -----------------------------------------------------------------------------


class Confidence(StrEnum):
    MEASURED = "measured"      # read from the page; the same every run
    HEURISTIC = "heuristic"    # a rule that can be wrong
    VISION = "vision"          # a model looked at pixels


class Severity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class Where(_Strict):
    selector: str
    key: str | None = None           # the manifest key, when a manifest exists
    role: str | None = None
    name: str | None = None          # capped; page text is data
    rect: Rect | None = None


class SeenAt(_Strict):
    screen: str                      # Screen.key
    count: int = 1


class Finding(_Strict):
    """One cause an agent can fix. Grouped by component and check, never one per DOM node."""

    id: str                          # finding_id(...): the same across runs
    check: str                       # "text.truncated", "contrast.text", "token.color", ...
    severity: Severity
    confidence: Confidence = Confidence.MEASURED
    where: Where
    component: ComponentRef | None = None
    seen_at: list[SeenAt] = Field(default_factory=list)
    measured: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    fix: str | None = None           # one sentence, or a CSS line
    why_heuristic: str | None = None
    evidence: str | None = None      # blob id of a crop


def finding_id(check: str, component: str | None, anchor: str) -> str:
    """A stable id: the check, the component and where it is, never the order it was found in.
    `anchor` is the manifest key or, without a manifest, the selector."""
    digest = hashlib.sha256(f"{check}\n{component or ''}\n{anchor}".encode()).hexdigest()
    return f"f_{digest[:10]}"


class CoverageItem(_Strict):
    """What one check did on one screen. A check that could not run is `skipped` with the
    reason: it is never left out and never counted as clean."""

    check: str
    screen: str
    status: Literal["ran", "skipped", "failed"]
    reason: str | None = None


class CheckContext(_Strict):
    screen: Screen
    engine: EngineName = EngineName.CHROMIUM
    only: list[str] | None = None            # check ids or prefixes; None runs all
    rules: dict[str, Any] = Field(default_factory=dict)   # thresholds from the profile


class CheckOutput(_Strict):
    """One screen."""

    findings: list[Finding] = Field(default_factory=list)
    coverage: list[CoverageItem] = Field(default_factory=list)


class CheckRun(_Strict):
    """All screens, merged."""

    id: str                          # "run_<...>"
    findings: list[Finding]
    coverage: list[CoverageItem]
    used: dict[str, Any] = Field(default_factory=dict)    # the defaults that were applied


class Delta(_Strict):
    fixed: list[str] = Field(default_factory=list)     # finding ids
    new: list[str] = Field(default_factory=list)
    still: list[str] = Field(default_factory=list)
    worse: list[str] = Field(default_factory=list)
    unchecked: list[str] = Field(default_factory=list)   # open before; their check did not run


# --- the design system --------------------------------------------------------------------


class TokenCategory(StrEnum):
    COLOR = "color"
    SPACE = "space"
    RADIUS = "radius"
    FONT_SIZE = "font_size"
    FONT_FAMILY = "font_family"
    FONT_WEIGHT = "font_weight"
    LINE_HEIGHT = "line_height"
    SHADOW = "shadow"
    BORDER_WIDTH = "border_width"


class Token(_Strict):
    name: str                        # "color.blue.500"
    category: TokenCategory
    value: str                       # canonical: colours as oklab(...), lengths as "<n>px"
    themes: list[Theme] = Field(default_factory=lambda: [Theme.LIGHT, Theme.DARK])
    css_var: str | None = None       # "--color-blue-500" when the page defines it
    semantic: bool = False           # "color.text.primary" before "color.blue.500"


class Scale(_Strict):
    """A rule instead of a list: any multiple of `step` (Tailwind's spacing)."""

    category: TokenCategory
    step_px: float
    max_px: float = 1024
    css_var: str | None = None       # "--spacing": lets the probe trace calc(var(--spacing) * n)


class TokenSet(_Strict):
    tokens: list[Token] = Field(default_factory=list)
    scales: list[Scale] = Field(default_factory=list)
    source: str | None = None        # "profile:acme", "page", a file name


class ComponentRule(_Strict):
    """How a known component is recognised in a page."""

    name: str
    attr: str | None = None          # "data-slot=button"
    class_pattern: str | None = None  # a regular expression


class Match(_Strict):
    kind: Literal["exact", "near", "off"]
    token: str | None = None             # the nearest token's name
    candidates: list[str] = Field(default_factory=list)   # tokens that share the value
    distance: float | None = None        # delta E (OK) for colours, px for lengths
    method: Literal["value", "probe"] = "value"
    alpha_delta: float | None = None     # colours: how far the alpha is from the token's
    provenance: Literal["token", "derived", "fixed"] | None = None   # from the probe


class Adherence(_Strict):
    """How much of a page is on the design system."""

    by_category: dict[str, float] = Field(default_factory=dict)   # share of values on a token
    area_coverage: float | None = None          # known-component pixels over all pixels
    interactive_coverage: float | None = None   # controls inside a known component
    hand_rolled: int = 0


# --- what changed between two captures ------------------------------------------------------


class Change(_Strict):
    """One element that differs between two captures of the same screen."""

    kind: Literal["added", "removed", "moved", "resized", "restyled", "text"]
    key: str                             # the manifest key
    component: ComponentRef | None = None
    before: dict[str, Any] = Field(default_factory=dict)   # only what differs: rect, styles, text
    after: dict[str, Any] = Field(default_factory=dict)
    tokens: dict[str, list[str | None]] = Field(default_factory=dict)   # property -> [before, after] token names
    caused_by: str | None = None         # key of the change that explains this one (a push down)
    moved_with: int = 0                  # on a cause: how many elements it moved


class Region(_Strict):
    """Pixels that changed and no element explains (a canvas, an image). Never guessed at."""

    rect: Rect
    share: float                         # of the screen's pixels
    evidence: str | None = None          # blob id of a crop, before and after side by side


class ScreenDiff(_Strict):
    screen: str                          # Screen.key
    status: Literal["same", "changed", "skipped"]
    reason: str | None = None            # why it was skipped (missing on one side, sizes differ)
    changes: list[Change] = Field(default_factory=list)      # causes first
    unexplained: list[Region] = Field(default_factory=list)
    pixel_share: float = 0.0             # share of pixels that differ, after noise is masked
    lines: list[str] = Field(default_factory=list)           # one plain sentence per cause


class DiffResult(_Strict):
    id: str                              # "diff_<...>"
    before: str                          # capture ids
    after: str
    screens: list[ScreenDiff]
