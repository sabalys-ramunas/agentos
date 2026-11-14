"""Small text helpers shared across packages."""

from __future__ import annotations


def first_line(e: BaseException, limit: int = 200) -> str:
    """An error's first line (its type's name when it says nothing), cut to `limit` characters:
    for a log line or a message, never a whole traceback or a page's text."""
    return (str(e).strip() or type(e).__name__).splitlines()[0][:limit]
