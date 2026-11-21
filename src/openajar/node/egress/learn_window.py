"""The learn window's state and its pure checks (docs/design/learn-a-site.md section 4). The guard
(LaneGuard.open_learn, _learn_release) holds one window per lane; openajar.node.egress.learn reaches
it from the runtime.

What the window releases is one request to the field's reviewed endpoint
(openajar.node.sites.PROFILE_EDIT_ENDPOINTS), never "a request that carries the value somewhere".
That it changes nothing but the field is checked three ways (the strongest the guard can see):

1. Acts: from the window's opening, the agent's acts on the lane may enter exactly one text, equal
   to the value (a type or fill; no key typed one character at a time), and upload nothing. A
   second text, another text, a printable key or an upload closes the window.
2. The body: the value sits at the endpoint's `body_key`, exactly and once.
3. The rest of the body: every other field equals what the page loaded as the form's current values
   (the endpoint's `form_data` GET, whose answer the guard keeps in memory, never traced). With no
   such answer seen, nothing is released; a field that differs closes the window.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from openajar.node.egress.constants import SHAPE_PARSE_MAX

LEARN_TTL_S = 900
LEARN_TTL_MAX_S = 3600
VALUE_MAX = 10_000
FIELD_RE = re.compile(r"^[a-z_]{1,30}$")


@dataclass
class LearnWindow:
    """One open learn window: what it may release, until when, and (once used) nothing more."""
    id: str
    field: str
    value: str
    path_prefixes: tuple[str, ...]
    until: float
    site: str                        # the site the request and its page must be on
    grant_id: str                    # only an act of this grant's driver; another driver's act closes it
    endpoint: Any                    # openajar.node.sites.EditEndpoint: the one request it may release
    # the account pinned for the lane, read when the request comes (None: unpinned, so nothing is released)
    pinned: Callable[[], Any]
    # why the lane may not learn right now (a pilot run on it), read when the request comes; None: it may
    excluded: Callable[[], str | None] | None = None
    # the signed-in account on a site ({site, user_id}); None: read from the browser's cookies
    identity: Callable[[str], Awaitable[dict[str, str] | None]] | None = None
    typed: list[str] = field(default_factory=list)  # the texts the agent entered since it opened
    used: bool = False
    refused: list[str] = field(default_factory=list)  # the latest reasons a request was not released

    def note_refused(self, why: str) -> None:
        self.refused = [*self.refused[-9:], why]


def check_open(field_: str, value: str, path_prefixes: Any, ttl_s: Any) -> tuple[str, ...]:
    """Raise ValueError for arguments a window cannot be opened with; the prefixes as a tuple."""
    if not isinstance(field_, str) or not FIELD_RE.match(field_):
        raise ValueError("the field is 1 to 30 of a-z and _")
    if not isinstance(value, str) or not value.strip() or len(value) > VALUE_MAX:
        raise ValueError(f"the value is a non-empty text of at most {VALUE_MAX} characters")
    if isinstance(path_prefixes, str) or not path_prefixes:
        raise ValueError("name the edit pages' path prefixes (a list, like ['/accounts/edit/'])")
    prefixes = tuple(path_prefixes)
    if not all(isinstance(p, str) and p.startswith("/") and len(p) > 1 for p in prefixes):
        raise ValueError("each path prefix starts with / and names a page (not / alone)")
    if not isinstance(ttl_s, int | float) or isinstance(ttl_s, bool) or not 0 < ttl_s <= LEARN_TTL_MAX_S:
        raise ValueError(f"ttl_s is between 1 and {LEARN_TTL_MAX_S} seconds")
    return prefixes


def path_under(page_url: str, prefixes: tuple[str, ...]) -> bool:
    """Is the page under one of the prefixes? A prefix ending in / is a directory; one that does not
    also matches its own path followed by /; a prefix with `?k=v` also needs the page's query to
    carry exactly that value for each of its parameters (and that parameter only once)."""
    try:
        parts = urlsplit(page_url)
    except ValueError:
        return False
    path = parts.path or "/"
    query = parse_qsl(parts.query, keep_blank_values=True)
    for p in prefixes:
        base, _, want = p.partition("?")
        if base.endswith("/"):
            if not (path.startswith(base) or path == base[:-1]):
                continue
        elif not (path == base or path.startswith(base + "/")):
            continue
        need = parse_qsl(want, keep_blank_values=True)
        if all([v for k, v in query if k == name] == [value] for name, value in need):
            return True
    return False


def endpoint_matches(endpoint: Any, host: str, path: str, method: str, op: str | None) -> bool:
    """Is this request the reviewed endpoint (method, exact host, whole path, operation name)?"""
    return (method.upper() == endpoint.method and host == endpoint.host
            and re.fullmatch(endpoint.path, path) is not None and (op or None) == endpoint.op)


def _form(body: bytes) -> list[tuple[str, str]] | None:
    try:
        return parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    except (UnicodeDecodeError, ValueError):
        return None


def body_values(body: bytes | None, key: str) -> list[Any] | None:
    """Every value at `form:<name>` or `json:<a.b>` of the body; None when it cannot be read so."""
    if not body or len(body) > SHAPE_PARSE_MAX:
        return None
    kind, _, where = key.partition(":")
    if kind == "form":
        pairs = _form(body)
        return None if pairs is None else [v for k, v in pairs if k == where]
    if kind == "json":
        try:
            cur: Any = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        for part in where.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return []
            cur = cur[part]
        return [cur]
    return None


def carries_exactly(body: bytes | None, key: str, value: str) -> bool:
    """The value sits at `key` exactly, and only once."""
    found = body_values(body, key)
    return found == [value]


def _same(old: Any, new: str) -> bool:
    """Does a form value the page sends equal what web_form_data gave for that field?"""
    if isinstance(old, bool):
        return new.lower() in (("on", "true", "1") if old else ("", "off", "false", "0"))
    if old is None:
        return new == ""
    if isinstance(old, int | float):
        return new == str(old)
    return isinstance(old, str) and new == old


def others_changed(body: bytes | None, key: str, baseline: dict[str, Any] | None,
                   fields: tuple[str, ...] = ()) -> list[str] | None:
    """The names of the body's other fields that differ from the form's current values, [] when
    none does, None when it cannot be told (no baseline, or a body that is not a form). With the
    form's reviewed `fields`, a body field outside them counts as changed, and a field the body
    leaves out counts as sent empty (an unchecked checkbox: off). Without them, a field the
    baseline does not have counts as changed. A field sent twice counts as changed."""
    if baseline is None or not key.startswith("form:") or not body:
        return None
    pairs = _form(body)
    if pairs is None:
        return None
    name = key.partition(":")[2]
    sent: dict[str, list[str]] = {}
    for k, v in pairs:
        sent.setdefault(k, []).append(v)
    changed: list[str] = []
    names = list(dict.fromkeys([*fields, *sent])) if fields else list(sent)
    for k in names:
        if k == name:
            continue
        values = sent.get(k, [""])
        if len(values) != 1 or (fields and k not in fields):
            changed.append(k)
        elif k not in baseline:
            if fields and values == [""]:
                continue
            changed.append(k)
        elif not _same(baseline[k], values[0]):
            changed.append(k)
    return changed


def form_data_of(text: str, key: str) -> dict[str, Any] | None:
    """The form's current values from a form_data answer (its JSON object at `key`), or None."""
    with contextlib.suppress(ValueError):
        data = json.loads(text)
        found = data.get(key) if isinstance(data, dict) else None
        if isinstance(found, dict):
            return found
    return None
