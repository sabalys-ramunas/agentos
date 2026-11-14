"""What the node's tools accept: lane ids, URLs and refs, checked before a lane is touched.
"""

from __future__ import annotations

import re

from agentos.core import globs
from agentos.core.errors import InvalidRequest
from agentos.core.models import ActionKind, parse_lane_id

HAS_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)")
NEEDS_TEXT = {ActionKind.TYPE, ActionKind.FILL, ActionKind.SELECT, ActionKind.PRESS}
MAX_URL_CHARS = 16_384     # far above real pages; a 100 kB URL is a mistake or an attack
MAX_TEXT_CHARS = 20_000    # human-paced typing; more would hold the lane for many minutes
CONTROL_CHARS_RE = re.compile(r"[\x00-\x20\x7f]")  # no spaces, tabs, newlines or NULs inside a URL
REF_FORMAT_RE = re.compile(r"(?:f\d{1,6})?e\d{1,9}")  # the snapshot's aria-ref ids, used with fullmatch


# ---- helpers ----

def lane_id(on: str) -> str:
    try:
        parse_lane_id(on)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from None
    return on


def normalize_url(url: str) -> str:
    """Only web pages: a file:// or about: URL could read the node's own disk (profiles, secrets).
    The URL is read with the policy's WHATWG parser (agentos.core.globs.parse_web_url), so the
    host the policy authorizes is the host the browser loads; anything a browser could read
    differently (a backslash, a second @, a numeric host) is refused. Errors never quote the URL,
    which may carry a token or a password."""
    url = url.strip()
    if not url:
        raise InvalidRequest("url is empty")
    if len(url) > MAX_URL_CHARS:
        raise InvalidRequest(f"url is {len(url)} characters; the limit is {MAX_URL_CHARS}")
    if CONTROL_CHARS_RE.search(url):
        raise InvalidRequest("url contains control characters or whitespace")
    m = HAS_SCHEME_RE.match(url)
    if not m:  # "reddit.com/r/x" or "localhost:8080"
        url = "https://" + url
    elif m.group(0)[:-1].lower() not in ("http", "https"):
        raise InvalidRequest(f"only http and https URLs are allowed, got a {m.group(0)[:-1][:20]} URL")
    parts = globs.parse_web_url(url)
    if parts.userinfo is not None:
        raise InvalidRequest("URLs with a user name or password are refused; log in with the vault (login tool)")
    return url


def url_host(url: str) -> str:
    """The host a browser contacts for a URL that passed normalize_url (what navigation authorizes)."""
    return globs.parse_web_url(url).host


def normalize_ref(ref: str) -> str:
    """Accept "e12", "ref=e12" or "[ref=e12]" so a ref copied from the outline just works."""
    ref = ref.strip().strip("[]").strip()
    ref = ref.removeprefix("ref=")
    if not ref:
        raise InvalidRequest("ref is empty; copy a ref like e12 from the latest browser_snapshot")
    if len(ref) > 24 or not REF_FORMAT_RE.fullmatch(ref):
        raise InvalidRequest("ref must look like e12 (or f3e12 inside a frame); copy it from the latest "
                             "browser_snapshot")
    return ref
