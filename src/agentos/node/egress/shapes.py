"""Request shapes: the operation names, persisted query ids and body fields a request carries, and the owner's observe window."""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentos.core import globs
from agentos.core.errors import InvalidRequest
from agentos.node.egress.constants import (
    _DOTTED_NAME_RE,
    _MULTIPART_NAME_RE,
    _SHAPE_NAME_RE,
    OBSERVE_MAX_MINUTES,
    SHAPE_DEPTH,
    SHAPE_DOTTED_OP_KEYS,
    SHAPE_HEADERS,
    SHAPE_JSON_FIELDS,
    SHAPE_MAX_FIELDS,
    SHAPE_OP_KEYS,
    SHAPE_PARSE_MAX,
)
from agentos.node.egress.rules import UNKNOWN, EffectRule
from agentos.node.egress.sites import norm_path

# ---- request shapes ----------------------------------------------------------------------

def _shape_paths(value: Any, prefix: str, out: list[str], depth: int) -> None:
    """Field paths of a parsed JSON value ('variables.input.title', 'media[]'), names only."""
    if len(out) >= SHAPE_MAX_FIELDS or depth > SHAPE_DEPTH:
        return
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str) or not _SHAPE_NAME_RE.match(key):
                continue
            path = f"{prefix}.{key}" if prefix else key
            out.append(path)
            _shape_paths(value[key], path, out, depth + 1)
    elif isinstance(value, list) and value:
        _shape_paths(value[0], f"{prefix}[]", out, depth + 1)


def _op_name(value: Any) -> str | None:
    if isinstance(value, list) and value:
        value = value[0]  # a batch: its first operation
    if isinstance(value, dict):
        for key in SHAPE_OP_KEYS:
            op = value.get(key)
            if isinstance(op, str) and _SHAPE_NAME_RE.match(op):
                return op
        for key in SHAPE_DOTTED_OP_KEYS:
            op = value.get(key)
            if isinstance(op, str) and len(op) <= 120 and _DOTTED_NAME_RE.match(op):
                return op
    return None


def request_op(headers: dict[str, str] | None, body: bytes | None) -> str | None:
    """The operation name a request carries: an operation header (SHAPE_HEADERS), else the
    operation key of a JSON body or of a form (SHAPE_OP_KEYS). None when there is none, or the
    body is too large to parse (an upload)."""
    from urllib.parse import parse_qsl

    low = {k.lower(): v for k, v in (headers or {}).items()}
    for name in SHAPE_HEADERS:
        value = low.get(name)
        if value and _SHAPE_NAME_RE.match(value):
            return value
    if not body or len(body) > SHAPE_PARSE_MAX:
        return None
    ctype = (low.get("content-type") or "").split(";")[0].strip().lower()
    text = body.decode("utf-8", "replace")
    if "json" in ctype or (not ctype and text[:1] in "[{"):
        with contextlib.suppress(ValueError):
            return _op_name(json.loads(text))
        return None
    if "x-www-form-urlencoded" in ctype or (not ctype and "=" in text):
        return _op_name(dict(parse_qsl(text, keep_blank_values=True)))
    return None


# Operations known to be reads, per site, though their names do not say so (the canary's traffic,
# a canary run of 2026-11-04, and the tests' feed sessions). A name ending in `Query` (Meta's
# convention, `...Query_subscribe` too) or a GraphQL document that is a `query` is a read anyway.
# Added from the real node's gate log of 2026-11-10: Reddit's
# as-you-type validation of a post (it creates nothing), its trophy list, the clearing of the
# account's own achievement notices (nothing another person sees), whether comment guidance exists;
# LinkedIn's counters, tracking pixels, share-box tracking, hover cards, and the fetches that load a
# post's action prompt and its comment box.
READ_OPS: dict[str, re.Pattern[str]] = {
    "reddit": re.compile(r"^(ExposeVariant|CreateCaptchaToken|AchievementUnlockedNotifications|"
                         r"EvaluateCommentAutomationsByPostId|ValidateCreatePostInput|TrophyCategories|"
                         r"DeleteAchievementNotifications|GetIsCommentGuidanceAvailableFromPostId)$"),
    # the typing indicator: others see "typing", nothing is published
    "facebook": re.compile(r"^CometUFILiveTypingBroadcastMutation(_\w+)?$"),
    "instagram": re.compile(r"^$"),
    "linkedin": re.compile(r"^com\.linkedin\.sdui\.(requests\.comments\.courtesyReminder|"
                           r"feed\.update\.comments\.fetchComment|update\.incrementCounterMetric|"
                           r"impl\.homenav\.requests\.getThirdPartyTrackingPixels|"
                           r"requests\.sharing\.LaunchShareboxTracking|requests\.feed\.actorPreviewCard|"
                           r"comments\.fetchFeedUpdateActionPrompt|requests\.comments\.deferredCommentBox)$"),
    "medium": re.compile(r"^$"),
}
# persisted query ids known to be reads, per site (Meta's doc_id): a request that names no operation
# is a read only when its id is here. None are listed yet (Meta rotates them); observe mode shows them.
READ_DOC_IDS: dict[str, frozenset[str]] = {}
_QUERY_NAME = re.compile(r"^[A-Za-z0-9_]*Query(_[A-Za-z0-9]+)?$")
_DOC_KEYWORD = re.compile(r"^\s*(#[^\n]*\n\s*)*(query|mutation|subscription|fragment|\{)")
_MUTATION_WORD = re.compile(r"\bmutation\b")
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,80}$")  # a doc_id a shape names (never a value a person typed)
_DOC_ID_KEYS = ("doc_id", "docId", "queryId", "query_id", "query_hash", "documentId")
_PUT_LIKE = frozenset({"PUT", "PATCH", "DELETE"})


def operations(headers: dict[str, str] | None, body: bytes | None) -> list[dict[str, Any]] | None:
    """Every operation a GraphQL-like request carries, as {name, doc_id, keyword}: the operation
    headers' name, and each element of a batch body (a JSON array, Meta's `queries` form field).
    None when the body cannot be read as one (too large, multipart, another format)."""
    low = {k.lower(): v for k, v in (headers or {}).items()}
    named = [low[h] for h in SHAPE_HEADERS if h != "x-restli-method" and low.get(h)]
    data = body or b""
    if len(data) > SHAPE_PARSE_MAX:
        return None
    ctype = (low.get("content-type") or "").split(";")[0].strip().lower()
    elements = _body_elements(data, ctype)
    if elements is None:
        return None
    out = [_operation(el) for el in elements]
    if named and len(out) == 1 and out[0]["name"] is None:
        out[0]["name"] = named[0]  # the operation only a header names (the body is its variables)
    for name in named:  # a header names the request's operation: it must be a read as well
        out.append({"name": name, "doc_id": None, "keyword": None})
    return out


def _body_elements(data: bytes, ctype: str) -> list[Any] | None:
    """The operations' elements of a body: a JSON object or array, a form (Meta's `queries` batch
    gives one element per query), or one empty element for no body. None for another format."""
    from urllib.parse import parse_qsl

    text = data.decode("utf-8", "replace")
    if not data:
        return [{}]
    if "json" in ctype or (not ctype and text[:1] in "[{"):
        try:
            value = json.loads(text)
        except ValueError:
            return None
        return value if isinstance(value, list) else [value]
    if "x-www-form-urlencoded" in ctype or (not ctype and "=" in text):
        form = dict(parse_qsl(text, keep_blank_values=True))
        if not isinstance(form.get("queries"), str):
            return [form]
        try:  # Meta's batch: {"q0": {"doc_id", "query_params"}, ...}
            batch = json.loads(form["queries"])
        except ValueError:
            return None
        if not isinstance(batch, dict):
            return None
        return [form | q if isinstance(q, dict) else {"_": q} for q in batch.values()] or [form]
    return None


def _operation(el: Any) -> dict[str, Any]:
    """{name, doc_id, keyword} of one element (all None for one that is not an object)."""
    if not isinstance(el, dict):
        return {"name": None, "doc_id": None, "keyword": None}
    doc = el.get("query")
    keyword = None
    if isinstance(doc, str) and doc.strip():
        m = _DOC_KEYWORD.match(doc)
        keyword = "mutation" if _MUTATION_WORD.search(doc) else (m.group(2) if m else "unreadable")
    doc_id = next((str(el[k]) for k in _DOC_ID_KEYS if isinstance(el.get(k), (str, int))), None)
    return {"name": _op_name(el) or (el.get("query_name") if isinstance(el.get("query_name"), str) else None),
            "doc_id": doc_id, "keyword": keyword}


def gql_verdict(site: str, headers: dict[str, str] | None, body: bytes | None, *,
                extra: Mapping[str, frozenset[str]] | None = None) -> tuple[bool, str]:
    """(read, why) for a request a strict_ops read rule matched: a read only when every operation
    in it is a known read (READ_OPS, READ_DOC_IDS, and `extra`: the reads the node's owner
    promoted from the gate log, each name with the doc_ids it was seen with, "" for none: an
    operation of that name is one only with one of those, or with none when "" is), is named
    like a query (`...Query`) or is a GraphQL `query` document. A mutation, an unknown or missing name, an unknown doc_id, a batch with any of
    those, or a body that cannot be read is not (why says which, names only)."""
    ops = operations(headers, body)
    if ops is None:
        return False, "a body the guard cannot read as GraphQL"
    known = READ_OPS.get(site)
    by_convention = False
    for op in ops:
        name, doc_id, keyword = op["name"], op["doc_id"], op["keyword"]
        if keyword == "mutation":
            return False, f"a mutation ({name or 'unnamed'})"
        if name and known is not None and known.match(name):
            continue
        if name and extra and name in extra and (doc_id or "") in extra[name]:
            continue
        if name and "Mutation" in name:
            return False, f"an unknown mutation {name}"
        if keyword == "query" or keyword == "{":
            by_convention = True
            continue
        if name and _QUERY_NAME.match(name):
            by_convention = True
            continue
        if name:
            return False, f"an unknown operation {name}" + (" in a batch" if len(ops) > 1 else "")
        if doc_id is not None and doc_id in READ_DOC_IDS.get(site, frozenset()):
            continue
        if doc_id is not None:
            return False, f"an unknown doc_id {doc_id[:40]}"
        return False, "no operation name"
    return True, "named like a query" if by_convention else "a known read"


def unknown_rule(rules: tuple[EffectRule, ...], host: str, url: str, method: str) -> EffectRule | None:
    """The rule the commit gate holds an unrecognised request on a rule host by: `<site>.unknown`, a
    commit, for this host and path. None when no rule names the host (not a site the gate knows)."""
    site = next((r.site for r in rules if globs.domain_match(r.host, host) or r.host == f"*.{host}"), None)
    if site is None:
        return None
    return EffectRule(f"{site}.{UNKNOWN}", site, host, re.escape(norm_path(url)) + "$", method=method.upper(),
                      note="a request no rule reads: it may publish")


TYPED_MIN = 4          # a typed piece shorter than this is matched only as a whole value
TYPED_SCAN_MAX = 2 * 1024 * 1024  # how much of a body is searched for what the agent typed
TYPED_RUN = 10         # normalised letters and digits a body must share with the typed stream, in a row
TYPED_STREAM_MAX = 16_384  # normalised characters of the stream kept (the newest)
TYPED_KEPT = 20        # whole pieces (one per type or fill) kept, for short text and the card
TYPED_TTL_S = 1800.0   # a short piece is also found inside a value for this long (a whole value: always)
B64_RUN = re.compile(rb"[A-Za-z0-9+/_-]{16,}={0,2}")
B64_MAX_RUNS = 256     # base64-looking runs of one body decoded and checked, at most

_JSON_ESC = re.compile(r"\\u([0-9a-fA-F]{4})|\\([\"\\/bfnrt])")
_JSON_CHARS = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_TAG = re.compile(r"<[^<>]{0,512}>")


def _json_char(m: re.Match[str]) -> str:
    if m.group(1):
        return chr(int(m.group(1), 16))
    return _JSON_CHARS.get(m.group(2), m.group(2))


def normalise_text(text: str) -> str:
    """Text as the typed-text check compares it: JSON and URL escapes and HTML entities decoded (a few
    layers deep: a JSON field that carries JSON, a form field that carries an escaped value), tags
    stripped (rich text that splits a run), case folded, and everything that is not a letter or a
    digit dropped (spaces, punctuation, the markup's own quotes). Both the stream and a body go
    through it, so a change one makes the other makes too."""
    from html import unescape
    from urllib.parse import unquote_plus

    for _ in range(4):
        before = text
        text = _JSON_ESC.sub(_json_char, text)
        if "%" in text or "+" in text:
            text = unquote_plus(text)
        if "&" in text:
            text = unescape(text)
        if text == before:
            break
    text = _TAG.sub("", text)
    return "".join(c for c in text.casefold() if c.isalnum())


def _b64_texts(data: bytes) -> list[str]:
    """The text inside base64-looking runs of a body (standard or URL-safe alphabet), decoded."""
    import base64
    import binascii
    from urllib.parse import unquote_to_bytes

    out: list[str] = []
    # as sent, and with JSON's escaped slash and form encoding undone (a run split by %2B or \/)
    seen = dict.fromkeys(m.group(0) for src in (data, unquote_to_bytes(data.replace(b"\\/", b"/")))
                         for m in B64_RUN.finditer(src))
    for found in list(seen)[:B64_MAX_RUNS]:
        run = found.rstrip(b"=")
        run += b"=" * (-len(run) % 4)
        alt = b"-_" if (b"-" in run or b"_" in run) else b"+/"
        try:
            raw = base64.b64decode(run, altchars=alt, validate=True)
        except (ValueError, binascii.Error):
            continue
        text = raw.decode("utf-8", "ignore")
        if text:
            out.append(text)
    return out


def _typed_needles(piece: str) -> list[bytes]:
    """The ways a page can carry a piece of typed text in a body: as typed (UTF-8), escaped in JSON
    (ASCII or not), escaped twice (JSON a JSON field carries, Reddit's rich text), and form-encoded."""
    from urllib.parse import quote, quote_plus

    once = json.dumps(piece)[1:-1]
    raw = [piece, json.dumps(piece, ensure_ascii=False)[1:-1], once, json.dumps(once)[1:-1],
           quote_plus(piece), quote(piece)]
    return list(dict.fromkeys(n.encode("utf-8", "replace") for n in raw if n))


def _piece_in(piece: str, data: bytes, anywhere: bool) -> bool:
    """Today's check of one whole piece: anywhere in the body for TYPED_MIN characters or more (while
    `anywhere`), else only as a whole JSON string or form value."""
    for needle in _typed_needles(piece):
        if anywhere and len(piece) >= TYPED_MIN:
            if needle in data:
                return True
        elif (b'"' + needle + b'"') in data or re.search(
                rb"(^|[=&])" + re.escape(needle) + rb"($|&)", data) is not None:
            return True
    return False


class TypedStream:
    """What the agent entered on one lane's browser, kept in memory for as long as that browser lives
    and never traced or logged (its repr says only how much): the running text of every type, fill
    and printable key sent with press, in order, normalised (normalise_text) and capped at the newest
    TYPED_STREAM_MAX characters, and the last TYPED_KEPT whole type and fill pieces.

    A body carries typed text (found_in) when, normalised the same way, it shares a run of TYPED_RUN
    letters and digits with the stream: text typed in chunks, entered key by key, split by markup,
    escaped, case-changed or spaced differently is still found, however long ago it was typed. Text
    in base64-looking runs of the body is decoded and checked the same way. A short piece (fewer than
    TYPED_RUN normalised characters) is found as a whole JSON string or form value, and, within
    TYPED_TTL_S, anywhere in the body once it has TYPED_MIN characters (the check before the stream).
    What it cannot see: docs/design/egress-guard.md 8.13."""

    def __init__(self, clock: Any = time.time):
        self.clock = clock
        self._pieces: list[tuple[float, str]] = []
        self._norm = ""
        self._grams: frozenset[str] | None = None

    def __repr__(self) -> str:  # never the text
        return f"TypedStream({len(self._pieces)} pieces, {len(self._norm)} characters)"

    def __bool__(self) -> bool:
        return bool(self._norm or self._pieces)

    def note(self, text: str | None, *, piece: bool = True) -> None:
        """The agent entered `text`. `piece`: a whole type or fill (kept whole too); False for a key."""
        if not text:
            return
        if piece and text.strip():
            self._pieces = [*self._pieces[-(TYPED_KEPT - 1):], (self.clock(), text)]
        norm = normalise_text(text)
        if norm:
            self._norm = (self._norm + norm)[-TYPED_STREAM_MAX:]
            self._grams = None

    def pieces(self) -> list[str]:
        """The whole pieces kept, newest first (for a test; the check uses found_in)."""
        return [t for _, t in reversed(self._pieces)]

    def _run_grams(self) -> frozenset[str]:
        if self._grams is None:
            n = self._norm
            self._grams = frozenset(n[i:i + TYPED_RUN] for i in range(len(n) - TYPED_RUN + 1))
        return self._grams

    def _shared_run(self, text: str) -> str | None:
        grams = self._run_grams()
        if not grams:
            return None
        body = normalise_text(text)
        for i in range(len(body) - TYPED_RUN + 1):
            gram = body[i:i + TYPED_RUN]
            if gram in grams:
                return gram
        return None

    def found_in(self, body: bytes | None) -> str | None:
        """What of the stream this body carries: the shared run (normalised), or the short piece it
        holds; None when nothing of it was found. Names nothing about the body: the caller decides."""
        if not body or not self:
            return None
        data = body[:TYPED_SCAN_MAX]
        now = self.clock()
        for at, text in reversed(self._pieces):
            lines = [text.strip(), *(line.strip() for line in text.splitlines())]
            for piece in dict.fromkeys(p for p in lines if p):
                # a long piece found whole names itself (the card quotes it); the stream finds it changed
                anywhere = len(normalise_text(piece)) >= TYPED_RUN or now - at < TYPED_TTL_S
                if _piece_in(piece, data, anywhere):
                    return piece
        text = data.decode("utf-8", "replace")
        run = self._shared_run(text)
        if run is not None:
            return run
        for decoded in _b64_texts(data):
            run = self._shared_run(decoded)
            if run is not None:
                return run
        return None


def typed_in(texts: list[str] | tuple[str, ...], body: bytes | None) -> str | None:
    """found_in for these pieces typed in this order, oldest first (a stream made for one check)."""
    stream = TypedStream()
    for text in texts:
        stream.note(text)
    return stream.found_in(body)


def typed_matches(value: str, found: str) -> bool:
    """Does `value` (a field of a captured record) hold what found_in returned?"""
    norm = normalise_text(found)
    return (norm in normalise_text(value)) if norm else found in value


def request_shape(headers: dict[str, str] | None, body: bytes | None) -> dict[str, Any]:
    """The shape of a request: its content type, an operation name (from SHAPE_HEADERS or a body
    key in SHAPE_OP_KEYS) and the names of its body's fields (JSON paths, form and multipart
    field names, and the fields inside a form field that holds JSON). Never a value: what a person
    typed never reaches the trace. Names that do not look like identifiers are dropped."""
    low = {k.lower(): v for k, v in (headers or {}).items()}
    ctype = (low.get("content-type") or "").split(";")[0].strip().lower()
    shape: dict[str, Any] = {"type": ctype or None}
    op = _shape_headers(low, shape)
    fields: list[str] = []
    data = body or b""
    if "multipart/" in ctype:
        fields = _multipart_fields(data)
    elif data and len(data) <= SHAPE_PARSE_MAX:
        op = _shape_body(data.decode("utf-8", "replace"), ctype, op, fields)
    if op:
        shape["op"] = op
    if fields:
        shape["fields"] = fields[:SHAPE_MAX_FIELDS]
    exact = body_fields(headers, body)
    if exact is None or exact != set(fields):
        shape["partial"] = True  # the names are not all of the body's: a promotion cannot bind them
    if data and len(data) <= SHAPE_PARSE_MAX:
        # the persisted query ids it names: a promotion binds its name to these (gateshapes)
        doc_ids = sorted({o["doc_id"] for o in operations(headers, body) or () if o["doc_id"] is not None
                          and _DOC_ID_RE.match(o["doc_id"])})
        if doc_ids:
            shape["doc_ids"] = doc_ids[:SHAPE_MAX_FIELDS]
    return shape


def _shape_headers(low: dict[str, str], shape: dict[str, Any]) -> str | None:
    """The operation headers that hold a name, added to `shape`, and the first of them."""
    op: str | None = None
    for name in SHAPE_HEADERS:
        value = low.get(name)
        if value and _SHAPE_NAME_RE.match(value):
            shape.setdefault("headers", {})[name] = value
            op = op or value
    return op


def _multipart_fields(data: bytes) -> list[str]:
    fields = list(dict.fromkeys(m.decode("ascii", "replace") for m in _MULTIPART_NAME_RE.findall(data[:65536])))
    return [f for f in fields if _SHAPE_NAME_RE.match(f)][:SHAPE_MAX_FIELDS]


def _shape_body(text: str, ctype: str, op: str | None, fields: list[str]) -> str | None:
    """The field names of a JSON or form body, added to `fields`, and the operation it names
    (`op` when one is named already)."""
    from urllib.parse import parse_qsl

    if "json" in ctype or (not ctype and text[:1] in "[{"):
        with contextlib.suppress(ValueError):
            parsed = json.loads(text)
            op = op or _op_name(parsed)
            _shape_paths(parsed[0] if isinstance(parsed, list) and parsed else parsed, "", fields, 1)
    elif "x-www-form-urlencoded" in ctype or (not ctype and "=" in text):
        pairs = parse_qsl(text, keep_blank_values=True)
        form = {k: v for k, v in pairs}
        op = op or _op_name(form)
        for key, value in pairs:
            if len(fields) >= SHAPE_MAX_FIELDS:
                break
            if not _SHAPE_NAME_RE.match(key) or key in fields:
                continue
            fields.append(key)
            if key in SHAPE_JSON_FIELDS:
                with contextlib.suppress(ValueError):
                    _shape_paths(json.loads(value), key, fields, 2)
    return op


# top-level body keys that name an operation or a persisted query: a body with one is never covered by
# a read promoted for a request that names none (gateshapes)
OP_FIELDS = frozenset({*SHAPE_OP_KEYS, *SHAPE_DOTTED_OP_KEYS, *_DOC_ID_KEYS, "query", "queries", "query_name"})


def body_fields(headers: dict[str, str] | None, body: bytes | None) -> frozenset[str] | None:
    """Every field name of a body, as request_shape names them (JSON paths, form and multipart
    names, and the fields of a form field that holds JSON), but complete: every element of a
    list, no cap that drops one. None when that cannot be had: a format request_shape does not
    read, a body over SHAPE_PARSE_MAX, a name that is not an identifier, more than
    SHAPE_MAX_FIELDS names or deeper than SHAPE_DEPTH."""
    from urllib.parse import parse_qsl

    low = {k.lower(): v for k, v in (headers or {}).items()}
    ctype = (low.get("content-type") or "").split(";")[0].strip().lower()
    data = body or b""
    if not data:
        return frozenset()
    if len(data) > SHAPE_PARSE_MAX:
        return None
    out: set[str] = set()

    def walk(value: Any, prefix: str, depth: int) -> bool:
        if isinstance(value, dict):
            if value and depth > SHAPE_DEPTH:
                return False
            for key in value:
                if not isinstance(key, str) or not _SHAPE_NAME_RE.match(key):
                    return False
                path = f"{prefix}.{key}" if prefix else key
                out.add(path)
                if not walk(value[key], path, depth + 1):
                    return False
        elif isinstance(value, list):
            return all(walk(v, f"{prefix}[]" if prefix else prefix, depth + (1 if prefix else 0)) for v in value)
        return len(out) <= SHAPE_MAX_FIELDS

    if "multipart/" in ctype:
        names = [m.decode("ascii", "replace") for m in _MULTIPART_NAME_RE.findall(data)]
        if not all(_SHAPE_NAME_RE.match(n) for n in names) or len(set(names)) > SHAPE_MAX_FIELDS:
            return None
        return frozenset(names)
    text = data.decode("utf-8", "replace")
    if "json" in ctype or (not ctype and text[:1] in "[{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return frozenset(out) if walk(parsed, "", 1) else None
    if "x-www-form-urlencoded" in ctype or (not ctype and "=" in text):
        for key, value in parse_qsl(text, keep_blank_values=True):
            if not _SHAPE_NAME_RE.match(key):
                return None
            out.add(key)
            if key in SHAPE_JSON_FIELDS:
                with contextlib.suppress(ValueError):
                    if not walk(json.loads(value), key, 2):
                        return None
        return frozenset(out) if len(out) <= SHAPE_MAX_FIELDS else None
    return None


def body_covered(fields: frozenset[str], headers: dict[str, str] | None, body: bytes | None) -> tuple[bool, str]:
    """(covered, why) for a body a read promoted without an operation matched: covered only when
    every field name it has is one of `fields` (optional ones may be missing), and it names no
    operation (no operation header, no top-level OP_FIELDS, no GraphQL document or doc_id)."""
    low = {k.lower(): v for k, v in (headers or {}).items()}
    if any(low.get(h) for h in SHAPE_HEADERS):
        return False, "a header names an operation"
    got = body_fields(headers, body)
    if got is None:
        return False, "a body the guard cannot list every field of"
    if got & OP_FIELDS:
        return False, f"a body that names an operation ({', '.join(sorted(got & OP_FIELDS))})"
    extra = sorted(got - fields)
    if extra:
        return False, f"fields the promoted read never had ({', '.join(extra[:5])})"
    return True, "every field one the promoted read had"


def read_observe(path: Path | None, now: float | None = None) -> float | None:
    """The expiry (epoch seconds) of an observation that still runs, else None."""
    if path is None:
        return None
    try:
        until = float(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return until if until > (time.time() if now is None else now) else None


def write_observe(path: Path, minutes: int | None) -> float | None:
    """Start (minutes, at most OBSERVE_MAX_MINUTES) or end (None) the observation of a lane."""
    if minutes is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return None
    if not 1 <= minutes <= OBSERVE_MAX_MINUTES:
        raise InvalidRequest(f"observe for 1 to {OBSERVE_MAX_MINUTES} minutes")
    until = time.time() + minutes * 60
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{until:.0f}\n")
    return until
