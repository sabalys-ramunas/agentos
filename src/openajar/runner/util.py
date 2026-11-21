"""Small helpers shared by the runner package: clock, ids, templates, clipping."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import quote, quote_plus

from openajar.core import clock
from openajar.core.template import TEMPLATE_RE

Clock = clock.Clock  # wall time for records and schedules, monotonic time for waits


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def iso(dt: datetime | None) -> str | None:
    """Fixed-width UTC ISO 8601, so SQLite string comparison orders correctly."""
    return clock.iso_or_none(dt, timespec="microseconds")


parse_dt = clock.parse  # ISO text (Z, an offset, or none: UTC) as an aware datetime; empty is None


_LITERAL_HOST_RE = re.compile(r"^https?://([^/?#]*)", re.IGNORECASE)
_DOT_SEGMENTS = (".", "..")


def _segments(url: str) -> list[str]:
    return url.split("#", 1)[0].split("?", 1)[0].split("/")[3:]


def _dot_segment(template: str, url: str) -> str | None:
    """A value is percent-encoded, but `.` and `..` have nothing to encode and a browser walks them:
    `/orders/${id}/edit` with id `..` would load `/edit`. A param never moves the path it sits in."""
    if any(s in _DOT_SEGMENTS for s in _segments(url)) and not any(s in _DOT_SEGMENTS for s in _segments(template)):
        return "a param filled in a path segment with a dot segment (. or ..), which would leave the path the URL names"
    return None


def url_problem(template: str, url: str, domain: str | None) -> str | None:
    """Why a URL filled in from params may not be loaded, or None if it may. A param may fill
    in the path or query under a literal host (values are percent-encoded there). When a param
    decides the host (a whole-URL param, a templated host), the URL must be plain http(s) and
    inside the registrable domain of the flow's `domain`; a flow without a domain is refused."""
    if not TEMPLATE_RE.search(template):
        return None  # the flow's own literal URL
    m = _LITERAL_HOST_RE.match(template.strip())
    if m and "${" not in m.group(1):
        return _dot_segment(template, url)
    from openajar.vault.domains import normalize_host, parse_origin, registrable_domain

    try:
        origin = parse_origin(url)
    except ValueError:
        return "the URL filled in from params is not a plain http(s) address"
    if not domain:
        return "a param decides this URL's host, so the flow must declare its domain"
    try:
        base = registrable_domain(domain) or normalize_host(domain)
    except ValueError:
        return f"the flow's domain {domain!r} is not a host name"
    if origin.host == base or origin.host.endswith("." + base):
        return None
    return f"the URL filled in from params goes to {origin.host}, outside the flow's domain {base}"


# ---- clipping, so huge page text never bloats a run record or a tool result ----

def host_in(host: str, domain: str) -> bool:
    """`host` is `domain` or one of its subdomains."""
    host, domain = host.lower().rstrip("."), domain.lower()
    return host == domain or host.endswith("." + domain)


_LINE_SPACE = re.compile(r"[^\S\n]+")


def norm_lines(text: str) -> str:
    """Whitespace normalized with its line breaks kept (a profile's About): runs of spaces and
    tabs become one space, each line is trimmed, three or more breaks in a row become two (a
    blank line), and leading and trailing blank lines go."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_LINE_SPACE.sub(" ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def clip_text(value: str, limit: int = 500) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}... ({len(value)} chars, truncated)"


def clip(value: Any, *, max_str: int = 500, max_items: int = 50, depth: int = 0) -> Any:
    if isinstance(value, str):
        return clip_text(value, max_str)
    if depth > 8:
        return "(nested too deep)"
    if isinstance(value, dict):
        items = list(value.items())
        out = {str(k): clip(v, max_str=max_str, max_items=max_items, depth=depth + 1)
               for k, v in items[:max_items]}
        if len(items) > max_items:
            out["_truncated"] = f"{len(items) - max_items} more keys"
        return out
    if isinstance(value, list | tuple):
        out_list = [clip(v, max_str=max_str, max_items=max_items, depth=depth + 1) for v in value[:max_items]]
        if len(value) > max_items:
            out_list.append(f"({len(value) - max_items} more items)")
        return out_list
    return value


def json_size(value: Any) -> int:
    return len(json.dumps(value, default=str))


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)


# ---- param values out of what is shown ----

MIN_SCRUB_LEN = 3  # shorter values are hidden only where they are the whole string


def _scalars(value: Any) -> list[str]:
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, str | int | float):
        return [str(value)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _scalars(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _scalars(v)]
    return []


class Scrubber:
    """Replaces the values of hidden params with `${name}` in anything shown (proof, the step
    journal, errors, logs). A value shows up as typed, or percent-encoded inside a URL.
    Idempotent: text already scrubbed stays the same (placeholders are never rewritten).
    `extra` runs after on every string: on the node, the vault's values, since a run's proof,
    readbacks and errors quote the page (FlowService.vault_scrub)."""

    def __init__(self, hidden: dict[str, Any] | None = None, *, extra: Callable[[str], str] | None = None):
        self._extra = extra
        pairs: dict[str, str] = {}
        for name, value in sorted((hidden or {}).items()):
            for text in _scalars(value):
                for form in (text, quote(text, safe=""), quote_plus(text)):
                    if form:
                        pairs.setdefault(form, "${" + name + "}")
        self._long = sorted(((f, r) for f, r in pairs.items() if len(f) >= MIN_SCRUB_LEN),
                            key=lambda fr: -len(fr[0]))
        self._exact = {f: r for f, r in pairs.items() if len(f) < MIN_SCRUB_LEN}
        names = sorted({r for r in pairs.values()}, key=len, reverse=True)
        self._placeholder = re.compile("(" + "|".join(re.escape(n) for n in names) + ")") if names else None

    def __bool__(self) -> bool:
        return bool(self._long or self._exact or self._extra)

    def _replace(self, value: str) -> str:
        if value in self._exact:
            return self._exact[value]
        for form, repl in self._long:
            if form in value:
                value = value.replace(form, repl)
        return value

    def text(self, value: str) -> str:
        if self._placeholder is not None:
            parts = self._placeholder.split(value)  # odd items are placeholders: kept as they are
            value = self._replace(value) if len(parts) == 1 else "".join(
                part if i % 2 else (self._replace(part) if part else part) for i, part in enumerate(parts))
        return self._extra(value) if self._extra is not None and value else value

    def __call__(self, value: Any) -> Any:
        if not self:
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self(v) for v in value]
        return value  # numbers in proof are counts, never params
