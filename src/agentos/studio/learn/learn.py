"""`learn_brand(browser, url, pages=...)`: open a site in the review browser and draft its brand.

What it does, in order:

1. opens the url (and up to MAX_PAGES - 1 more pages of the same site) in light at each width,
   reading the authored values, the custom properties the site declares and how often its CSS
   uses them, and component hints;
2. finds how the site turns dark by trying each way (prefers-color-scheme, a data-theme
   attribute, a `dark` class, a data-mode attribute, a stored `theme`) on the first page and
   keeping the first that changes the page's colours, read back from the page;
3. reads every page again in dark that way;
4. builds the draft (draft.build_draft): declared tokens where the site has them, clusters where
   it has none, and how much of the pages each covers.

The first page failing fails the call (a studio error with its fix); another page failing is
listed in `failed`.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

from agentos.core.errors import InvalidRequest

from ..contracts import Screen, Target, Theme, ThemeSwitch
from ..tokens.color import delta_e as _de
from ..tokens.color import parse_color
from ..tokens.page import read_page_tokens
from .draft import BrandDraft, Reads, build_draft
from .read import read_hints, read_screen, read_signature, read_usage

MAX_PAGES = 5
DEFAULT_WIDTHS = (360, 1280)
SIGNATURE_DE = 0.05

DARK_SWITCHES: tuple[ThemeSwitch, ...] = (
    ThemeSwitch(how="media"),
    ThemeSwitch(how="attr", name="data-theme", light="light", dark="dark"),
    ThemeSwitch(how="class", dark="dark"),
    ThemeSwitch(how="attr", name="data-mode", light="light", dark="dark"),
    ThemeSwitch(how="storage", name="theme", light="light", dark="dark"),
)


def site_pages(url: str, pages: list[str] | None) -> list[str]:
    """The url, then each extra page (a full url or a path on the same site), at most MAX_PAGES,
    without repeats. A page on another site is refused."""
    from agentos.studio.browser.pool import check_url

    check_url(url)
    origin = urlsplit(url)
    out = [url]
    for p in pages or []:
        full = urljoin(url, str(p).strip())
        parts = urlsplit(full)
        if (parts.scheme, parts.netloc) != (origin.scheme, origin.netloc):
            raise InvalidRequest(f"pages must be on the same site as url ({origin.scheme}://{origin.netloc}); "
                                 f"{str(p)[:80]!r} is not. Learn another site with its own call.")
        check_url(full, "pages")
        if full not in out:
            out.append(full)
    if len(out) > MAX_PAGES:
        raise InvalidRequest(f"at most {MAX_PAGES} pages in one call (the url and {MAX_PAGES - 1} more); "
                             f"{len(out)} were given. Pick the pages that show the most of the design.")
    return out


def _path(url: str) -> str:
    p = urlsplit(url)
    return p.path or "/"


def _bg(sig: dict):
    for v in sig.get("bg") or []:
        c = parse_color(v or "")
        if c is not None and c.alpha > 0.01:
            return c
    return parse_color("#ffffff")


def signature_changed(light: dict, dark: dict) -> bool:
    """Whether a theme switch changed the page: its background, its text colour or any colour
    custom property, read back from the page."""
    if _de(_bg(light), _bg(dark)) > SIGNATURE_DE:
        return True
    lf, df = parse_color(light.get("fg") or ""), parse_color(dark.get("fg") or "")
    if lf is not None and df is not None and _de(lf, df) > SIGNATURE_DE:
        return True
    lv, dv = light.get("vars") or {}, dark.get("vars") or {}
    return any(lv.get(k) != dv.get(k) for k in lv)


async def _detect_theme(browser, url: str, width: int, light_sig: dict, color_vars: list[str]) -> tuple[
        ThemeSwitch | None, str]:
    from agentos.studio.browser.errors import StudioError

    tried = []
    for switch in DARK_SWITCHES:
        label = switch.how if switch.how in ("media", "class") else f"{switch.how} {switch.name}"
        try:
            async with browser.page(Target(url=url, theme_switch=switch), Screen(width=width, theme=Theme.DARK)) as page:
                sig = await read_signature(page, color_vars)
        except StudioError as e:
            tried.append(f"{label} ({getattr(e, 'code', 'failed')})")
            continue
        if signature_changed(light_sig, sig):
            return switch, f"the page's colours change with {label}"
        tried.append(f"{label} (no change)")
    return None, "no dark theme found; tried " + ", ".join(tried)


async def learn_brand(browser, url: str, *, pages: list[str] | None = None,
                      widths: tuple[int, ...] = DEFAULT_WIDTHS, max_elements: int = 4000) -> BrandDraft:
    """Draft a brand from a live site. Reads only names and values; no page text is kept."""
    from agentos.studio.browser.errors import StudioError

    urls = site_pages(url, pages)
    widths = tuple(sorted(set(widths)))
    wide = widths[-1]
    reads = Reads(url=url, pages=[_path(u) for u in urls])
    declared = []
    scales = []
    light_sig: dict | None = None
    color_vars: list[str] = []
    gone: set[int] = set()                 # pages that failed in light are not tried in dark
    for pi, u in enumerate(urls):
        for w in widths:
            screen = Screen(width=w, theme=Theme.LIGHT)
            try:
                async with browser.page(Target(url=u), screen) as page:
                    sr = await read_screen(page, pi, w, "light", max_elements=max_elements)
                    if w == wide:
                        pt = await read_page_tokens(page)
                        declared.append(pt.tokens)
                        if pt.unreadable:
                            reads.notes.append(f"{len(pt.unreadable)} stylesheet(s) on {_path(u)} could not be read "
                                               "(another origin); tokens declared there are missing")
                        usage = await read_usage(page)
                        for var, n in (usage.get("refs") or {}).items():
                            row = reads.usage.setdefault(var, {"rules": 0, "elements": 0})
                            row["rules"] += int(n)
                            row["elements"] += int((usage.get("els") or {}).get(var, 0))
                        hints = await read_hints(page)
                        for k in ("comp", "slot", "cls", "svg", "util"):
                            for name, n in (hints.get(k) or {}).items():
                                reads.hints[k][name] = reads.hints[k].get(name, 0) + int(n)
                        if pi == 0:
                            color_vars = sorted({t.css_var for t in pt.tokens.tokens
                                                 if t.css_var and t.category.value == "color"})
                            light_sig = await read_signature(page, color_vars)
            except StudioError as e:
                if pi == 0:
                    raise
                reads.failed.append(f"{_path(u)} at {w} px: {getattr(e, 'code', 'failed')}")
                gone.add(pi)
                continue
            reads.light.append(sr)
            reads.screens.append(screen.key)
            if sr.truncated:
                reads.notes.append(f"{_path(u)} at {w} px: only the first {max_elements} elements were read")
    # declared tokens: the first page's, then names only later pages add
    if declared:
        names: set[tuple] = set()
        toks = []
        for ts in declared:
            for t in ts.tokens:
                k = (t.name, tuple(t.themes))
                if k not in names:
                    names.add(k)
                    toks.append(t)
            for s in ts.scales:
                if all(s.category != x.category for x in scales):
                    scales.append(s)
        from ..contracts import TokenSet

        reads.declared = TokenSet(tokens=toks, scales=scales, source="page")
    switch, note = await _detect_theme(browser, urls[0], wide, light_sig or {}, color_vars)
    reads.theme, reads.theme_note = switch, note
    if switch is not None:
        for pi, u in enumerate(urls):
            if pi in gone:
                continue
            for w in widths:
                screen = Screen(width=w, theme=Theme.DARK)
                try:
                    async with browser.page(Target(url=u, theme_switch=switch), screen) as page:
                        sr = await read_screen(page, pi, w, "dark", max_elements=max_elements)
                except StudioError as e:
                    reads.failed.append(f"{_path(u)} at {w} px in dark: {getattr(e, 'code', 'failed')}")
                    continue
                reads.dark.append(sr)
                reads.screens.append(screen.key)
    return build_draft(reads)
