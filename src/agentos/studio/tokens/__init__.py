"""Design tokens and "is this on the design system?" (studio part S4).

- `load_tokens(source)`: a TokenSet from a DTCG file (with the resolver or a themes map), a CSS
  custom properties file or a Tailwind v4 theme; `tokens_from_page(page)` from a live page.
- `match(value, category, tokens)`: exact, near or off, the nearest token and how far.
- `system_check(page, tokens, components, ctx)`: findings for one screen, with the provenance
  probe telling token-driven values from hard-coded look-alikes.
- `adherence(page, tokens, components)`: the share of the page on the system.
- `to_dtcg(tokens)`, `to_css(tokens)`: back out.

Nothing here imports playwright: a page is passed in.
"""

from .check import (
    Analysis,
    adherence,
    adherence_from,
    analyse,
    check_and_adherence,
    findings_from,
    system_check,
)
from .export import to_css, to_dtcg
from .load import from_css, from_dtcg, from_tailwind, load_tokens
from .match import match
from .page import PageTokens, read_page_tokens, tokens_from_page

__all__ = [
    "Analysis", "PageTokens", "adherence", "adherence_from", "analyse", "check_and_adherence", "findings_from",
    "from_css", "from_dtcg", "from_tailwind", "load_tokens", "match", "read_page_tokens", "system_check",
    "to_css", "to_dtcg", "tokens_from_page",
]
