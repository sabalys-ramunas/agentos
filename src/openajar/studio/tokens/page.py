"""A token set read from a live page: the custom properties declared on :root, the theme
scopes ([data-theme], .dark, prefers-color-scheme) and open shadow roots' :host, read from
document.styleSheets. Cross-origin sheets cannot be read there; they are listed, and their CSS
can be passed in (`css_texts`) or fetched (`fetch_cross_origin`)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import TokenSet
from . import page_js
from .color import parse_color
from .load import (
    _merge_themes,
    decls_by_theme,
    parse_css_blocks,
    pick_light_dark,
    raws_from_decls,
    resolve_vars,
)
from .units import ROOT_PX


@dataclass
class PageTokens:
    tokens: TokenSet
    unreadable: list[str] = field(default_factory=list)   # sheets that could not be read
    fetched: list[str] = field(default_factory=list)      # of those, read from their URL


async def read_page_tokens(page, *, tailwind: bool | None = None, css_texts: dict[str, str] | None = None,
                           fetch_cross_origin: bool = False) -> PageTokens:
    data = await page.evaluate(page_js.PAGE_DECLS)
    blocks = [(list(chain), dict(decls)) for chain, decls in data["blocks"]]
    unreadable = list(data.get("unreadable") or [])
    fetched = []
    for href in unreadable:
        text = (css_texts or {}).get(href)
        if text is None and fetch_cross_origin and href.startswith("http"):
            try:
                resp = await page.context.request.get(href, timeout=10_000)
                if resp.ok:
                    text = await resp.text()
            except Exception:  # noqa: BLE001 - listed as unreadable
                text = None
        if text is not None:
            blocks += parse_css_blocks(text)
            fetched.append(href)
    names = {n for _, d in blocks for n in d}
    if tailwind is None:
        tw = {"--spacing", "--text-base", "--font-sans", "--radius-md", "--color-white", "--leading-tight"}
        tailwind = "--spacing" in names and len(names & tw) >= 3
    root_px = await page.evaluate("() => parseFloat(getComputedStyle(document.documentElement).fontSize) || 16")
    per = decls_by_theme(blocks)
    # resolved texts the Python parser cannot read as colours: the page computes them
    pending = set()
    base = per.get("base", {})
    for decls in per.values():
        env = {**base, **decls}
        for raw in env.values():
            r = resolve_vars(raw, env)
            # light-dark() is read once per theme (raws_from_decls), so each side is computed too
            for v in (r, pick_light_dark(r, "light"), pick_light_dark(r, "dark")) if r else ():
                if parse_color(v) is None and ("(" in v or v.isalpha()) and not v.startswith("calc("):
                    pending.add(v)
    extra = await page.evaluate(page_js.CANON_COLORS, sorted(pending)) if pending else {}
    uses = {name: sorted(u) for name, u in (data.get("uses") or {}).items()}
    raws, scales = raws_from_decls(per, tailwind=tailwind, root_px=root_px or ROOT_PX, canon_extra=extra, uses=uses)
    ts = TokenSet(tokens=_merge_themes(raws), scales=scales, source="page")
    return PageTokens(ts, [u for u in unreadable if u not in fetched], fetched)


async def tokens_from_page(page, **kw) -> TokenSet:
    """The page's own tokens. See read_page_tokens for what could not be read."""
    return (await read_page_tokens(page, **kw)).tokens
