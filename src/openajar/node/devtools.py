"""What a lane's pages print and fetch, kept so an agent testing a web app can see why a page broke
(the browser_console and browser_network tools, openajar.node.devtools_tools).

One DevtoolsCapture per browser context (one lane), attached when the lane's browser starts (the
engine's PageTracker, and the dev node's tab). It only listens to the context's own events
(console, weberror, request, response, requestfinished, requestfailed): nothing is injected into a
page, no route is added and no request waits for it. Two ring buffers hold the newest entries, each
capped in entries and in bytes, so a chatty page can never grow the daemon; what falls out is
counted (`dropped`), and the counts per level go on counting.

What is kept, and what never is:

- Console: each message's level, its text (cut to TEXT_CHARS), where it was logged, and for an
  uncaught page error its stack (cut to STACK_LINES lines). Page errors are level error.
- Network: method, URL, resource type, status, how long it took, the failure text, the response's
  content type, and both header sets with every sensitive one masked (mask_headers: cookies,
  Set-Cookie, Authorization and any header named like a token, key or session). A request body is
  never kept or returned, only its size. A response body is never buffered: the tool asks the
  browser for it when an agent asks for one request (read_body), and says so plainly when the
  browser no longer has it.
- URLs are kept masked (mask_url): no user:pass@, and the value of every query or fragment
  parameter named like a secret (token, code, password, key, session, signature...) or shaped like
  one (a JWT, a long random string) is replaced. The raw URL is never stored; the host is, for the
  egress check a body read makes.

The tools add the rest: the vault scrub on everything returned (openajar.policy.redactor), the grant and
lane checks, the egress check before a body is read, and the untrusted-content label.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import weakref
from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from openajar.core import clock
from openajar.node import egress
from openajar.node.egress.sites import host_of

log = logging.getLogger(__name__)

MASKED = "[masked]"

# ---- limits -----------------------------------------------------------------------------------

CONSOLE_KEEP = 500             # console entries kept per lane
CONSOLE_BYTES = 256 * 1024     # and their size, roughly
NETWORK_KEEP = 500             # requests kept per lane
NETWORK_BYTES = 512 * 1024
TEXT_CHARS = 4000              # a console message's text as stored
STACK_LINES = 15               # lines of a page error's stack as stored
LINE_CHARS = 300               # a stack line, a header value
URL_CHARS = 1000               # a URL as stored
HEADERS_KEPT = 60              # headers of one request or response
BODY_CHARS = 20_000            # characters of one response body returned
BODY_FETCH_MAX = 2 * 1024 * 1024  # a body announced larger than this is not read at all
BODY_TIMEOUT_S = 5.0
ENTRY_OVERHEAD = 200           # bytes counted for an entry beyond its strings

LEVELS = ("error", "warning", "info", "debug")
_LEVEL_OF = {"error": "error", "assert": "error", "warning": "warning", "warn": "warning",
             "debug": "debug", "trace": "debug", "verbose": "debug"}
RESOURCE_TYPES = ("document", "stylesheet", "image", "media", "font", "script", "texttrack", "xhr", "fetch",
                  "eventsource", "websocket", "manifest", "other")

# ---- masking --------------------------------------------------------------------------------------

# A name is sensitive when it contains one of these anywhere ("accessToken", "x-api-key", "PHPSESSID")...
_SENSITIVE_PARTS = ("token", "secret", "passw", "passwd", "pwd", "apikey", "api_key", "api-key", "session",
                    "sessid", "csrf", "xsrf", "cookie", "signature", "credential", "bearer", "jwt", "private",
                    "authorization", "otp", "saml")
# ...or one of its words is one of these ("code", "key", "sig", "auth" alone, never inside "author" or "monkey").
_SENSITIVE_WORDS = frozenset({"key", "code", "pin", "sig", "sid", "auth", "pass", "nonce", "ticket", "hash",
                              "oauth", "verifier", "assertion", "hmac", "mfa", "2fa", "totp"})
_WORD_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+[a-z]*")
ALWAYS_MASKED_HEADERS = frozenset({"cookie", "set-cookie", "authorization", "proxy-authorization",
                                   "x-forwarded-for", "x-real-ip"})
URL_HEADERS = frozenset({"location", "referer", "content-location", "refresh", "link"})
# a value that looks like a token whatever its name: a JWT, or a long run of URL-safe characters with
# letters and digits mixed (a session id, a signed blob)
_JWT_RE = re.compile(r"eyJ[\w-]{6,}\.[\w-]{6,}\.?[\w-]*")
_OPAQUE_RE = re.compile(r"[A-Za-z0-9_\-.~+/%=]{32,}")
_URL_IN_TEXT_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>`]*[^\s\"'<>`.,;:)\]]", re.IGNORECASE)
# "name": "value" in JSON (also with single quotes, as in JS source)
_JSON_PAIR_RE = re.compile(r"""(["'])([\w.\-]{1,64})\1(\s*:\s*)(["'])((?:\\.|(?!\4).){0,4000}?)\4""")
# name=value in a query string or a form body
_FORM_PAIR_RE = re.compile(r"(?<![\w\-])([\w.\-\[\]]{1,64})=([^&\s\"'<>#;]*)")
# <input ... value="..."> of a hidden or password field, or one named like a secret; <meta name=... content=...>
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""\b(value|content)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_NAME_ATTR_RE = re.compile(r"""\b(?:name|id|property)\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)
_HIDDEN_RE = re.compile(r"""\btype\s*=\s*["']?(hidden|password)\b""", re.IGNORECASE)


def sensitive_name(name: str) -> bool:
    """Is this header, parameter or field named like a secret? Generous on purpose: a debugging
    tool that hides a harmless `country_code` costs a guess; one that shows a session costs the
    account."""
    low = name.lower()
    if any(part in low for part in _SENSITIVE_PARTS):
        return True
    return any(w.lower() in _SENSITIVE_WORDS for w in _WORD_RE.findall(name))


def looks_secret(value: str) -> bool:
    """A value shaped like a token whatever it is called: a JWT, or 32+ URL-safe characters mixing
    letters and digits."""
    if len(value) < 32 and not value.startswith("eyJ"):
        return False
    if _JWT_RE.search(value):
        return True
    m = _OPAQUE_RE.fullmatch(value)
    return bool(m) and any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def _mask_pairs(text: str) -> str:
    """`a=1&token=xyz` -> `a=1&token=[masked]`: every sensitive or token-shaped value."""
    if "=" not in text:
        return text
    out = []
    for part in text.split("&"):
        name, eq, value = part.partition("=")
        if eq and value and (sensitive_name(name) or looks_secret(value)):
            out.append(f"{name}={MASKED}")
        else:
            out.append(part)
    return "&".join(out)


def mask_url(url: Any, *, limit: int = URL_CHARS) -> str:
    """A URL as the devtools tools keep and show it: no user:pass@, and the value of every query and
    fragment parameter that is named like a secret or shaped like a token replaced with [masked].
    A data: URL keeps only its type. Cut to `limit` characters."""
    text = str(url or "")
    if text[:5].lower() == "data:":
        head = text[5:].split(",", 1)[0][:60]
        return f"data:{head},... ({len(text)} characters)"
    try:
        parts = urlsplit(text)
    except ValueError:
        return "[url not shown]"
    if parts.scheme.lower() in ("http", "https", "ws", "wss") and parts.netloc:
        host = parts.netloc.rsplit("@", 1)[-1]
        query = _mask_pairs(parts.query)
        fragment = _mask_pairs(parts.fragment) if "=" in parts.fragment else (
            MASKED if looks_secret(parts.fragment) else parts.fragment)
        path = "/".join(MASKED if looks_secret(seg) else seg for seg in parts.path.split("/"))
        text = urlunsplit((parts.scheme, host, path, query, fragment))
    return text if len(text) <= limit else text[:limit] + "..."


def mask_text(text: str) -> str:
    """Page text that may quote a URL, a JSON object, a form body or HTML (a console message, a stack,
    a response body): URLs masked as mask_url does, and the value of every JSON pair, form pair and
    hidden, password or secret-named input or meta tag named like a secret replaced."""
    if not text:
        return text
    if "://" in text:
        text = _URL_IN_TEXT_RE.sub(lambda m: mask_url(m.group(0), limit=10 * URL_CHARS), text)
    if ":" in text and ('"' in text or "'" in text):
        text = _JSON_PAIR_RE.sub(_json_pair, text)
    if "=" in text:
        text = _FORM_PAIR_RE.sub(_form_pair, text)
    if "<" in text:
        text = _INPUT_RE.sub(lambda m: _tag(m.group(0), hidden=True), text)
        text = _META_RE.sub(lambda m: _tag(m.group(0), hidden=False), text)
    return text


def _json_pair(m: re.Match[str]) -> str:
    q, name, colon, vq, value = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    if value and (sensitive_name(name) or looks_secret(value)):
        return f"{q}{name}{q}{colon}{vq}{MASKED}{vq}"
    return m.group(0)


def _form_pair(m: re.Match[str]) -> str:
    name, value = m.group(1), m.group(2)
    if value and value != MASKED and (sensitive_name(name) or looks_secret(value)):
        return f"{name}={MASKED}"
    return m.group(0)


def _tag(tag: str, *, hidden: bool) -> str:
    named = _NAME_ATTR_RE.search(tag)
    secret = bool(named and sensitive_name(named.group(1))) or (hidden and bool(_HIDDEN_RE.search(tag)))
    if not secret:
        return tag
    return _ATTR_RE.sub(lambda a: f'{a.group(1)}="{MASKED}"', tag)


def mask_headers(headers: Any) -> dict[str, str]:
    """Headers as the tools show them: Cookie, Set-Cookie, Authorization (and the proxy's), the
    client's address headers, and every header named like a secret are [masked]; URL headers
    (Location, Referer) are masked as URLs; other values are cut to LINE_CHARS."""
    out: dict[str, str] = {}
    try:
        items: Iterable[tuple[Any, Any]] = dict(headers or {}).items()
    except Exception:  # noqa: BLE001 - headers of a request that went away
        return out
    for name, value in items:
        if len(out) >= HEADERS_KEPT:
            out["..."] = "more headers not shown"
            break
        key = str(name).lower()[:100]
        text = str(value)
        if key in ALWAYS_MASKED_HEADERS or sensitive_name(key):
            out[key] = MASKED
        elif key in URL_HEADERS:
            out[key] = mask_text(text)[:LINE_CHARS]
        else:
            shown = MASKED if looks_secret(text) else text
            out[key] = shown if len(shown) <= LINE_CHARS else shown[:LINE_CHARS] + "..."
    return out


# ---- entries ---------------------------------------------------------------------------------------


def _ref(obj: Any) -> Callable[[], Any] | None:
    if obj is None:
        return None
    try:
        return weakref.ref(obj)
    except TypeError:
        return lambda: obj


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"... ({len(text) - limit} more characters)"


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """A Playwright property, or a test double's attribute; never raises."""
    try:
        value = getattr(obj, name)
    except Exception:  # noqa: BLE001 - a property of an object that went away
        return default
    return default if value is None else value


@dataclass
class ConsoleEntry:
    seq: int
    at: float
    level: str
    kind: str                      # the console call (log, error, warning, ...) or pageerror
    text: str
    page_url: str
    source: str | None = None      # url:line:column of the call or the error
    stack: str | None = None
    page: Callable[[], Any] | None = None

    @property
    def size(self) -> int:
        return ENTRY_OVERHEAD + len(self.text) + len(self.page_url) + len(self.source or "") + len(self.stack or "")


@dataclass
class NetEntry:
    seq: int
    at: float
    started: float                 # time.monotonic() when it was sent
    method: str
    url: str                       # masked (mask_url)
    host: str                      # the host it went to (the body read's egress check)
    type: str
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body_bytes: int | None = None
    redirected_from: str | None = None
    page: Callable[[], Any] | None = None
    status: int | None = None
    status_text: str = ""
    response_headers: dict[str, str] = field(default_factory=dict)
    mime: str = ""
    failure: str | None = None
    ms: int | None = None
    login: bool = False            # sent inside the vault's login window, or a login-shaped post
    response: Any = None           # the browser's response object, while the entry is kept (read_body)
    request: Any = None            # the browser's request object, while the entry is kept (redirects find it)

    @property
    def id(self) -> str:
        return f"r{self.seq}"

    @property
    def done(self) -> bool:
        return self.ms is not None

    @property
    def failed(self) -> bool:
        return self.failure is not None or (self.status is not None and self.status >= 400)

    @property
    def size(self) -> int:
        headers = sum(len(k) + len(v) for k, v in (*self.request_headers.items(), *self.response_headers.items()))
        return ENTRY_OVERHEAD + len(self.url) + len(self.failure or "") + headers


def level_of(kind: str) -> str:
    return _LEVEL_OF.get(kind.lower(), "info")


def _location(loc: Any) -> str | None:
    """`url:line:column` of a console call or a page error, counted from 1 as an editor and a stack
    do (Playwright counts both from 0, measured on Camoufox)."""
    if not isinstance(loc, dict):
        return None
    url = loc.get("url") or ""
    if not url:
        return None
    line = loc.get("lineNumber", loc.get("line"))
    col = loc.get("columnNumber", loc.get("column"))
    where = mask_url(url, limit=300)
    if isinstance(line, int):
        where += f":{line + 1}"
        if isinstance(col, int):
            where += f":{col + 1}"
    return where


def _stack(text: str, message: str = "") -> str:
    """A page error's stack, its lines cut, without a first line that repeats the message."""
    lines = [ln.rstrip()[:LINE_CHARS] for ln in text.strip().splitlines() if ln.strip()]
    if lines and message and lines[0].strip() == message.strip():
        lines = lines[1:]
    if len(lines) > STACK_LINES:
        lines = [*lines[:STACK_LINES], f"... ({len(lines) - STACK_LINES} more lines)"]
    return mask_text("\n".join(lines))


# Paths of a login, a token or a session endpoint: a non-GET there is a login, so its answer (which
# can carry a session token) is never returned.
_LOGIN_PATH_RE = re.compile(r"(?i)(log-?in|sign-?in|sign-?on|session|token|oauth|auth|password|passwd|2fa|mfa|"
                            r"otp|verify|challenge|account/?recover)")


class DevtoolsCapture:
    """The console and network ring buffers of one browser context. See the module docstring."""

    def __init__(self, *, console_keep: int = CONSOLE_KEEP, console_bytes: int = CONSOLE_BYTES,
                 network_keep: int = NETWORK_KEEP, network_bytes: int = NETWORK_BYTES,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic):
        self.console_keep, self.console_bytes = console_keep, console_bytes
        self.network_keep, self.network_bytes = network_keep, network_bytes
        self.clock, self.monotonic = clock, monotonic
        self.started_at = clock()
        self._console: deque[ConsoleEntry] = deque()
        self._console_size = 0
        self._console_seq = 0
        self.console_dropped = 0
        self.levels: Counter[str] = Counter()
        self._network: deque[NetEntry] = deque()
        self._network_size = 0
        self._network_seq = 0
        self.network_dropped = 0
        self.net_counts: Counter[str] = Counter()
        self._by_request: dict[Any, NetEntry] = {}
        self._by_id: dict[str, NetEntry] = {}
        self._context: Any = None
        self._closed = False

    # ---- attaching -------------------------------------------------------------------------

    def attach(self, context: Any) -> DevtoolsCapture:
        """Start listening to the context. A context gets one capture; attaching again returns it."""
        existing = capture_for(context)
        if existing is not None:
            return existing
        self._context = context
        for event, handler in (("console", self._on_console), ("weberror", self._on_weberror),
                               ("request", self._on_request), ("response", self._on_response),
                               ("requestfinished", self._on_finished), ("requestfailed", self._on_failed)):
            context.on(event, handler)
        with contextlib.suppress(TypeError):  # not weak-referenceable (a test double)
            _CAPTURES[context] = self
        return self

    def close(self) -> None:
        """The lane is closing: let go of every browser object, keep nothing."""
        self._closed = True
        self._console.clear()
        self._network.clear()
        self._by_request.clear()
        self._by_id.clear()
        self._console_size = self._network_size = 0

    # ---- console ---------------------------------------------------------------------------

    def _on_console(self, msg: Any) -> None:
        try:
            kind = str(_attr(msg, "type", "log"))
            text = str(_attr(msg, "text", ""))
            self.add_console(level_of(kind), kind, text, page=_attr(msg, "page"),
                             source=_location(_attr(msg, "location")))
        except Exception:
            log.debug("could not record a console message", exc_info=True)

    def _on_weberror(self, web_error: Any) -> None:
        try:
            error = _attr(web_error, "error", web_error)
            name = str(_attr(error, "name", "") or "")
            message = str(_attr(error, "message", "") or error)
            stack = str(_attr(error, "stack", "") or "")
            text = f"{name}: {message}" if name and not message.startswith(name) else message
            self.add_console("error", "pageerror", text, page=_attr(web_error, "page"), stack=stack or None,
                             source=_location(_attr(web_error, "location")))
        except Exception:
            log.debug("could not record a page error", exc_info=True)

    def add_console(self, level: str, kind: str, text: str, *, page: Any = None, source: str | None = None,
                    stack: str | None = None) -> ConsoleEntry | None:
        """Record one console message or page error (the engine's listener, or a fake site)."""
        if self._closed:
            return None
        self._console_seq += 1
        self.levels[level] += 1
        entry = ConsoleEntry(seq=self._console_seq, at=self.clock(), level=level, kind=kind[:40],
                             text=mask_text(_cut(text, TEXT_CHARS)), page_url=mask_url(_attr(page, "url", "")),
                             source=source, stack=_stack(stack, text) if stack else None, page=_ref(page))
        self._console.append(entry)
        self._console_size += entry.size
        while self._console and (len(self._console) > self.console_keep or self._console_size > self.console_bytes):
            gone = self._console.popleft()
            self._console_size -= gone.size
            self.console_dropped += 1
        return entry

    # ---- network ---------------------------------------------------------------------------

    def _on_request(self, request: Any) -> None:
        if self._closed:
            return
        try:
            url = str(_attr(request, "url", ""))
            method = str(_attr(request, "method", "GET")).upper()
            body = _attr(request, "post_data_buffer")
            previous = _attr(request, "redirected_from")
            prior = self._by_request.get(previous) if previous is not None else None
            host = host_of(url)
            self._network_seq += 1
            entry = NetEntry(seq=self._network_seq, at=self.clock(), started=self.monotonic(), method=method[:16],
                             url=mask_url(url), host=host or "", type=str(_attr(request, "resource_type", "other")),
                             request_headers=mask_headers(_attr(request, "headers", {})),
                             request_body_bytes=len(body) if isinstance(body, bytes | bytearray) and body else None,
                             redirected_from=prior.id if prior is not None else None,
                             page=_ref(_page_of(request)), request=request)
            entry.login = method not in ("GET", "HEAD", "OPTIONS") and (
                self._login_window(host or "") or bool(_LOGIN_PATH_RE.search(_path(url))))
            self.net_counts["requests"] += 1
            self._by_request[request] = entry
            self._by_id[entry.id] = entry
            self._network.append(entry)
            self._network_size += entry.size
            self._trim_network()
        except Exception:
            log.debug("could not record a request", exc_info=True)

    def _login_window(self, host: str) -> bool:
        guard = egress.guard_for(self._context) if self._context is not None else None
        check = getattr(guard, "login_open", None)
        try:
            return bool(callable(check) and check(host))
        except Exception:  # noqa: BLE001 - unknown is treated as a login: its answer is withheld
            return True

    def _on_response(self, response: Any) -> None:
        try:
            entry = self._by_request.get(_attr(response, "request"))
            if entry is None:
                return
            before = entry.size
            status = _attr(response, "status")
            entry.status = int(status) if isinstance(status, int) else None
            entry.status_text = str(_attr(response, "status_text", ""))[:100]
            headers = _attr(response, "headers", {})
            entry.response_headers = mask_headers(headers)
            try:
                low = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
            except Exception:  # noqa: BLE001
                low = {}
            entry.mime = low.get("content-type", "").split(";", 1)[0].strip().lower()[:100]
            if "set-cookie" in low and entry.method not in ("GET", "HEAD", "OPTIONS"):
                entry.login = True  # a post that sets a cookie is how a login answers: its body may hold the session
            entry.response = response
            self._grown(entry, before)
        except Exception:
            log.debug("could not record a response", exc_info=True)

    def _on_finished(self, request: Any) -> None:
        entry = self._by_request.get(request)
        if entry is None or entry.done:
            return
        entry.ms = max(0, round((self.monotonic() - entry.started) * 1000))
        if entry.status is not None and entry.status >= 400:
            self.net_counts["http_errors"] += 1

    def _on_failed(self, request: Any) -> None:
        entry = self._by_request.get(request)
        if entry is None or entry.done:
            return
        before = entry.size
        entry.ms = max(0, round((self.monotonic() - entry.started) * 1000))
        failure = _attr(request, "failure")
        text = failure if isinstance(failure, str) else str(_attr(failure, "error_text", "") or failure or "")
        entry.failure = mask_text(text.strip()[:LINE_CHARS]) or "failed"
        self.net_counts["failed"] += 1
        self._grown(entry, before)

    def _grown(self, entry: NetEntry, before: int) -> None:
        if self._by_id.get(entry.id) is entry:  # still kept
            self._network_size += entry.size - before
            self._trim_network()

    def _trim_network(self) -> None:
        while self._network and (len(self._network) > self.network_keep or self._network_size > self.network_bytes):
            gone = self._network.popleft()
            self._network_size -= gone.size
            self.network_dropped += 1
            self._by_id.pop(gone.id, None)
            if gone.request is not None:
                self._by_request.pop(gone.request, None)
            gone.request = gone.response = None

    # ---- reading ---------------------------------------------------------------------------

    def console_entries(self) -> list[ConsoleEntry]:
        return list(self._console)

    def network_entries(self) -> list[NetEntry]:
        return list(self._network)

    def entry(self, request_id: str) -> NetEntry | None:
        return self._by_id.get(request_id)

    def console_view(self, *, level: str = "all", since: int | None = None, last: int = 50,
                     max_chars: int = 500, tab: Callable[[Any], int | str | None] | None = None) -> dict[str, Any]:
        """browser_console's result: the newest `last` entries at `level` or worse after `since`."""
        wanted = _levels(level)
        matched = [e for e in self._console if e.level in wanted and (since is None or e.seq > since)]
        shown = matched[-last:] if last else []
        rows: list[dict[str, Any]] = []
        page_url = None
        for e in shown:
            row: dict[str, Any] = {"seq": e.seq, "at": clock.iso(e.at, z=True, timespec="milliseconds"), "level": e.level}
            if e.kind != e.level:
                row["kind"] = e.kind
            row["text"] = _cut(e.text, max_chars)
            if e.source:
                row["source"] = e.source
            if e.stack:
                row["stack"] = _cut(e.stack, max(max_chars, 1000))
            if e.page_url != page_url:
                row["page"] = page_url = e.page_url
            where = tab(e.page() if e.page is not None else None) if tab is not None else None
            if where is not None:
                row["tab"] = where
            rows.append(row)
        return {"messages": rows, "matched": len(matched), "returned": len(rows), "cursor": self._console_seq,
                "counts": {lv: self.levels[lv] for lv in LEVELS if self.levels[lv]},
                "kept": len(self._console), "dropped": self.console_dropped,
                "since_browser_start": clock.iso(self.started_at, z=True, timespec="milliseconds")}

    def network_view(self, *, failed_only: bool = False, min_status: int | None = None,
                     url_contains: str | None = None, type: str | None = None, since: int | None = None,
                     last: int = 50, tab: Callable[[Any], int | str | None] | None = None) -> dict[str, Any]:
        """browser_network's list: the newest `last` requests that pass every filter given."""
        needle = (url_contains or "").lower()

        def keep(e: NetEntry) -> bool:
            return ((since is None or e.seq > since) and (not failed_only or e.failed)
                    and (min_status is None or (e.status is not None and e.status >= min_status))
                    and (not needle or needle in e.url.lower()) and (type is None or e.type == type))

        matched = [e for e in self._network if keep(e)]
        shown = matched[-last:] if last else []
        rows = []
        for e in shown:
            row = summary_row(e)
            where = tab(e.page() if e.page is not None else None) if tab is not None else None
            if where is not None:
                row["tab"] = where
            rows.append(row)
        counts = {"requests": self.net_counts["requests"], "failed": self.net_counts["failed"],
                  "http_errors": self.net_counts["http_errors"]}
        return {"requests": rows, "matched": len(matched), "returned": len(rows), "cursor": self._network_seq,
                "counts": counts, "kept": len(self._network), "dropped": self.network_dropped,
                "since_browser_start": clock.iso(self.started_at, z=True, timespec="milliseconds")}


def summary_row(e: NetEntry) -> dict[str, Any]:
    row: dict[str, Any] = {"id": e.id, "method": e.method, "url": e.url, "type": e.type}
    if e.status is not None:
        row["status"] = e.status
    if e.failure:
        row["failure"] = e.failure
    if e.ms is not None:
        row["ms"] = e.ms
    else:
        row["pending"] = True
    if e.mime:
        row["mime"] = e.mime
    return row


def detail_row(e: NetEntry) -> dict[str, Any]:
    """One request in full, without its body (read_body adds that)."""
    row = summary_row(e)
    row["at"] = clock.iso(e.at, z=True, timespec="milliseconds")
    if e.status_text:
        row["status_text"] = e.status_text
    if e.redirected_from:
        row["redirected_from"] = e.redirected_from
    row["request_headers"] = e.request_headers
    if e.request_body_bytes:
        row["request_body"] = f"{e.request_body_bytes} bytes (request bodies are never returned)"
    if e.status is not None:
        row["response_headers"] = e.response_headers
    return row


def _levels(level: str) -> frozenset[str]:
    if level == "error":
        return frozenset({"error"})
    if level == "warning":
        return frozenset({"error", "warning"})
    return frozenset(LEVELS)


def _path(url: str) -> str:
    try:
        return urlsplit(url).path
    except ValueError:
        return ""


def _page_of(request: Any) -> Any:
    """The tab a request belongs to; None for a service worker's (Playwright raises there)."""
    try:
        frame = request.frame
        return frame.page if frame is not None else None
    except Exception:  # noqa: BLE001
        return None


# ---- one response body, on demand -------------------------------------------------------------------

_TEXT_MIME_RE = re.compile(r"^(text/|application/(json|javascript|ecmascript|xml|x-www-form-urlencoded|graphql"
                           r"|x-ndjson|ld\+json|problem\+json|manifest\+json)|image/svg\+xml$)|\+json$|\+xml$")


def is_text(mime: str) -> bool:
    return bool(mime) and bool(_TEXT_MIME_RE.search(mime))


def _charset(headers: dict[str, str]) -> str:
    m = re.search(r"charset=([\w.\-]+)", headers.get("content-type", ""), re.IGNORECASE)
    return m.group(1) if m else "utf-8"


async def read_body(entry: NetEntry, *, refused: Callable[[str], str | None]) -> dict[str, Any]:
    """The response body of one kept request, as browser_network(id=...) returns it: `body` (text,
    masked with mask_text, cut to BODY_CHARS) with `body_bytes` and `body_truncated`, or
    `body_withheld` saying why there is none. `refused(host)` is the egress check: why the lane's
    guard would not let the lane's page load that host, or None."""
    why = _withheld(entry, refused)
    if why is not None:
        return {"body_withheld": why}
    try:
        data = await asyncio.wait_for(entry.response.body(), BODY_TIMEOUT_S)
    except TimeoutError:
        return {"body_withheld": f"the browser did not hand over the body within {BODY_TIMEOUT_S:g} s"}
    except Exception:  # noqa: BLE001 - Playwright says the body is gone in several ways
        return {"body_withheld": "the browser no longer has this body (the page navigated away, or the browser "
                                 "let go of it). Load the page again and ask right after the request"}
    if not isinstance(data, bytes | bytearray):
        data = str(data or "").encode()
    try:
        text = bytes(data).decode(_charset({"content-type": entry.mime}), errors="replace")
    except LookupError:
        text = bytes(data).decode("utf-8", errors="replace")
    masked = mask_text(text)
    out: dict[str, Any] = {"body": masked[:BODY_CHARS], "body_bytes": len(data)}
    if len(masked) > BODY_CHARS:
        out["body_truncated"] = f"the first {BODY_CHARS} of {len(masked)} characters"
    return out


def _withheld(entry: NetEntry, refused: Callable[[str], str | None]) -> str | None:
    if entry.failure:
        return "the request failed, so there is no response body"
    if entry.response is None:
        return ("no response has arrived yet" if not entry.done else
                "the browser no longer holds this response")
    if entry.status is not None and 300 <= entry.status < 400:
        return "a redirect has no body; see the request it led to"
    if entry.login:
        return ("this answers a login (a post inside the vault's login window, to a sign-in or token address, or "
                "one that set a cookie), and such a body can carry the session, so it is never returned")
    if not entry.host:
        return "its address has no host the lane's guard can check"
    reason = refused(entry.host)
    if reason:
        return f"the lane's egress guard would not load this host ({reason}), so its body is not read"
    if not is_text(entry.mime):
        return f"only text bodies are returned, and this one is {entry.mime or 'of no stated type'}"
    length = entry.response_headers.get("content-length", "")
    if length.isdigit() and int(length) > BODY_FETCH_MAX:
        return f"it is {int(length)} bytes, more than the {BODY_FETCH_MAX} this tool reads"
    return None


# ---- one capture per context ------------------------------------------------------------------------

_CAPTURES: weakref.WeakKeyDictionary[Any, DevtoolsCapture] = weakref.WeakKeyDictionary()


def capture_for(page_or_context: Any) -> DevtoolsCapture | None:
    """The capture of a page's (or a context's) lane, if one is attached."""
    context = getattr(page_or_context, "context", page_or_context)
    try:
        return _CAPTURES.get(context)
    except TypeError:
        return None

