"""Templates in flow and routine documents: `${name}` or `${vars.before.count}`, filled from a scope."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from agentos.core.errors import InvalidRequest

TEMPLATE_RE = re.compile(r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\}")


class TemplateError(InvalidRequest):
    code = "template_invalid"


def template_roots(text: str) -> set[str]:
    return {m.group(1).split(".", 1)[0] for m in TEMPLATE_RE.finditer(text)}


def _lookup(scope: dict[str, Any], dotted: str) -> Any:
    cur: Any = scope
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise TemplateError(f"template value ${{{dotted}}} is not set")
    return cur


def render(text: str, scope: dict[str, Any], *, url: bool = False) -> str:
    """Fill `${name}` placeholders. In URLs, a value is percent-encoded unless the whole
    template is a single placeholder (so a full URL can be passed as one param)."""
    whole = TEMPLATE_RE.fullmatch(text.strip())
    if whole:
        value = _lookup(scope, whole.group(1))
        return "" if value is None else str(value)

    def sub(m: re.Match[str]) -> str:
        value = _lookup(scope, m.group(1))
        value = "" if value is None else str(value)
        return quote(value, safe="") if url else value

    return TEMPLATE_RE.sub(sub, text)
