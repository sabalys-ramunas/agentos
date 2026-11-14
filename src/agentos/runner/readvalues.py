"""Turning what a page or a JSON body said into typed, capped output values (extract, read_json),
and the connector's outputs and labels from them.

Nothing here puts a value a site gave into a problem text: problems name the kind of value.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from agentos.core.driver import JsonResponse, ResponseWatch, parse_json
from agentos.core.models import Signal
from agentos.core.template import TEMPLATE_RE
from agentos.runner.execution import (
    MAX_FIELD_CHARS,
    MAX_JSON_INTEGER,
    NEEDS_YOU,
    SIGNAL_CODES,
    ExecState,
    StepFailure,
    norm_text,
)
from agentos.runner.steps import (
    ExtractField,
    JsonField,
    ReadJsonStep,
)
from agentos.runner.urlpattern import JSON_ROOT, url_pattern
from agentos.runner.util import host_in

log = logging.getLogger("agentos.runner")

OUTPUT_CAP = 64 * 1024  # bytes of JSON in Run.output["data"]

def typed_value(raw: str | None, f: ExtractField, base_url: str) -> tuple[Any, bool, str | None]:
    """(value, clipped, problem) for one extracted field. A value that fails its pattern or
    format is a problem (the site changed shape); a URL outside `host` is dropped (None)."""
    if raw is None:
        return None, False, None
    value = raw if f.attr is not None else norm_text(raw)
    if f.capture:
        m = re.search(f.capture, value)
        if m is None or m.group(1) is None:  # an optional group can match nothing
            return None, False, None
        value = m.group(1)
    if f.format == "uri" or f.host:
        value = urljoin(base_url, value.strip())
        try:
            parts = urlsplit(value)
            ok_url = parts.scheme in ("http", "https") and bool(parts.hostname)
        except ValueError:
            ok_url, parts = False, None
        if f.host and not (ok_url and parts and parts.scheme == "https" and host_in(parts.hostname or "", f.host)):
            return None, False, None
        if not ok_url:
            return None, False, "is not a URL"
    if f.pattern and re.search(f.pattern, value) is None:
        return None, False, "does not match its pattern"
    if f.format == "date-time":
        try:
            datetime.fromisoformat(value.strip())
        except ValueError:
            return None, False, "is not a date-time"
    elif f.format == "integer":
        if not re.fullmatch(r"[+-]?\d{1,18}", value.strip()):
            return None, False, "is not an integer"
        return int(value), False, None
    elif f.format == "number":
        try:
            return float(value.strip()), False, None
        except ValueError:
            return None, False, "is not a number"
    limit = min(f.max_chars or MAX_FIELD_CHARS, MAX_FIELD_CHARS)
    if len(value) > limit:
        return value[:limit], True, None
    return value, False, None


def on_site(pattern: str, domain: str | None) -> Callable[[str], bool]:
    """Which response URLs a read_json takes: https under the flow's domain, whatever its pattern says,
    then the pattern. Runs on the event loop for every response, so it stays one pass over the URL."""
    rx = url_pattern(pattern)

    def wanted(url: str) -> bool:
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
        except ValueError:
            return False
        return parts.scheme == "https" and bool(domain) and host_in(host, domain or "") and rx.matches(url)

    return wanted


def is_reading(watch: ResponseWatch) -> bool:
    """A response has come and its body is on its way, on a driver that can say so."""
    return bool(getattr(watch, "reading", 0))


def picked_by_caller(url: str, state: ExecState) -> bool:
    """Does a param a caller gave fill this URL? Then the page is theirs, not the endpoint's."""
    from agentos.runner.connectors.health import given_params

    given = given_params(state.flow, state.run.params)
    return any(m.group(1) in given for m in TEMPLATE_RE.finditer(url))


def json_body(seen: JsonResponse | None, step: ReadJsonStep, *, picked: bool = False) -> Any:
    """The parsed body of the response a read_json waited for, or the failure it comes to. `picked`: a
    caller's param chose the page, so a 401 may be theirs alone and must not hold the lane."""
    expected = f"a 2xx JSON response on {step.url}"
    if seen is None:
        raise StepFailure("json_not_seen", f"the page made no request on {step.url} within {step.timeout_ms} ms",
                          acted=False, transient=True, expected=expected, found="no response")
    if seen.status == 304 and not seen.body:  # the page got its data, and the same page would answer the same
        raise StepFailure("json_unreadable", "the site answered 304, so the page used its own copy and no body reached "
                          "the browser", acted=False, expected=expected, found="status 304 with no body")
    if seen.problem == "slow":
        raise StepFailure("json_slow", f"the body of the response on {step.url} did not finish in time", acted=False,
                          transient=True, expected=expected, found="a body that did not finish")
    if seen.problem == "unreadable":
        raise StepFailure("json_unreadable", "the browser no longer held the response's body", acted=False, transient=True)
    if seen.problem == "too_large":
        raise StepFailure("json_too_large", f"the response on {step.url} is too large to read", acted=False,
                          expected=expected, found="over the size cap")
    if seen.status == 429:  # the site's own stop, as a rate limited page is: the lane rests, nothing retries
        raise StepFailure("rate_limited", "the site is rate limiting this lane", acted=False, signal=Signal.RATE_LIMITED,
                          expected=expected, found="status 429")
    if seen.status == 401 and not picked:  # the session ended, not the contract. A 403 can be one caller's params
        raise StepFailure(SIGNAL_CODES[Signal.LOGGED_OUT], f"needs you: {NEEDS_YOU[Signal.LOGGED_OUT]}", acted=False,
                          signal=Signal.LOGGED_OUT, expected=expected, found="status 401")
    if not 200 <= seen.status < 300 and seen.status != 304:  # a 5xx is a bad moment: it says nothing of the contract alone
        raise StepFailure("json_status", f"the response on {step.url} was {seen.status}", acted=False,
                          transient=seen.status >= 500, expected=expected, found=f"status {seen.status}")
    if not seen.body:  # only a repeat says the site changed, and health counts that, so going back says nothing
        raise StepFailure("json_empty", f"the response on {step.url} had no body", acted=False, expected=expected,
                          found=f"status {seen.status} with no body")
    try:
        return parse_json(seen.body)
    except ValueError:  # its text can quote the body
        raise StepFailure("json_invalid", f"the response on {step.url} is not JSON", acted=False,
                          expected=expected, found="not JSON") from None


def json_kind(value: Any) -> str:
    if value is None:
        return "null"
    return {bool: "a boolean", int: "a number", float: "a number", str: "a string", list: "a list"}.get(type(value), "an object")


def _json_at(root: Any, path: str) -> tuple[Any, bool]:
    """(the value at a dotted path, whether the body has that shape). A key that is not there is
    None, and so is what lies under a null; a part that is neither an object nor null is another shape."""
    cur = root
    for part in path.split("."):
        if cur is None:
            return None, True
        if not isinstance(cur, dict):
            return None, False
        cur = cur.get(part)
    return cur, True


def json_rows(body: Any, step: ReadJsonStep) -> list[Any]:
    rows = body if step.items == JSON_ROOT else _json_at(body, step.items)[0]
    if not isinstance(rows, list):
        raise StepFailure("output_contract_violation", f"the JSON at {step.items} is {json_kind(rows)}, not a list (the "
                          "site may have changed shape)", acted=False, expected="a list", found=json_kind(rows))
    return rows


def _finite(raw: float) -> bool:
    try:
        return math.isfinite(raw)
    except OverflowError:  # an integer past what a float holds
        return False


def json_value(row: dict[str, Any], f: JsonField, shape: ExtractField, base_url: str) -> tuple[Any, bool, str | None]:
    """(value, clipped, problem) for one read_json field. Its declared type or a problem, never a
    conversion, and a problem says what kind of value came, not what it said."""
    raw, fits = _json_at(row, f.path)
    if not fits:
        return None, False, f"{f.path} is not there: a part of it is not an object"
    if raw is None:
        return None, False, None
    wrong = f"is {json_kind(raw)}, not {'an' if f.type == 'integer' else 'a'} {f.type}"
    if f.type == "boolean":
        return (raw, False, None) if isinstance(raw, bool) else (None, False, wrong)
    if f.type == "integer":
        fits = type(raw) is int and abs(raw) < MAX_JSON_INTEGER
        return (raw, False, None) if fits else (None, False, wrong if type(raw) is not int else "is out of range")
    if f.type == "number":
        fits = type(raw) in (int, float) and _finite(raw)
        return (raw, False, None) if fits else (None, False, wrong if type(raw) not in (int, float) else "is out of range")
    if not isinstance(raw, str):
        return None, False, wrong
    try:
        raw.encode()
    except UnicodeEncodeError:  # a lone surrogate, which no answer of the node can carry
        return None, False, "is not valid text"
    value, cut, problem = typed_value(raw, shape, base_url)
    return value, cut, f"does not fit its format ({f.format})" if problem else None


def output_labels(outputs: dict[str, str], labels: dict[str, dict[str, list[str]]]) -> tuple[list[str], list[str]]:
    """untrusted_fields and private_fields as output paths ('items[].body') from the extract
    labels of the saved values the outputs map."""
    untrusted: list[str] = []
    private: list[str] = []
    for name, path in sorted(outputs.items()):
        root, _, rest = path.partition(".")
        lab = labels.get(root)
        if not lab:
            continue
        prefix = f"{name}.items[]." if rest == "" else f"{name}[]." if rest == "items" else None
        if prefix is None:
            continue
        untrusted += [prefix + fld for fld in lab.get("untrusted", [])]
        private += [prefix + fld for fld in lab.get("private", [])]
    return untrusted, private


def _size(value: Any) -> int:
    return len(json.dumps(value, default=str, separators=(",", ":")).encode())


def cap_output(data: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    """Trim the longest lists until the data fits OUTPUT_CAP: (data, trimmed), or (None, _)."""
    if _size(data) <= OUTPUT_CAP:
        return data, False
    data = json.loads(json.dumps(data, default=str))
    while _size(data) > OUTPUT_CAP:
        lists = [v for v in data.values() if isinstance(v, list) and v]
        lists += [v["items"] for v in data.values() if isinstance(v, dict) and isinstance(v.get("items"), list)
                  and v["items"]]
        if not lists:
            return None, True
        max(lists, key=len).pop()
    return data, True
