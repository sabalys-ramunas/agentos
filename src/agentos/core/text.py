"""Small text helpers shared across packages."""

from __future__ import annotations

from collections.abc import Iterable


def first_line(e: BaseException, limit: int = 200) -> str:
    """An error's first line (its type's name when it says nothing), cut to `limit` characters:
    for a log line or a message, never a whole traceback or a page's text."""
    return (str(e).strip() or type(e).__name__).splitlines()[0][:limit]


def close_match(name: str, candidates: Iterable[str], *, cutoff: float = 0.6) -> str | None:
    """The one candidate closest to a misspelt `name` (difflib), or None when nothing is close: what a
    "did you mean" names before any "create it" hint."""
    import difflib

    pool = sorted({c for c in candidates if c and c != name})
    found = difflib.get_close_matches(name, pool, n=1, cutoff=cutoff)
    if found:
        return found[0]
    lowered = name.lower()  # a different case only (Test/Reddit) is still the same thing
    return next((c for c in pool if c.lower() == lowered), None)
