"""A lane's site list: hosts, paths and the patterns that match them."""

from __future__ import annotations

import contextlib
import re
from urllib.parse import unquote, urlsplit

from openajar.core import globs
from openajar.core.errors import InvalidRequest
from openajar.core.models import (
    COUNTRY_TOKEN,
    FRAME_PREFIX,
    GOOGLE_COUNTRY_SUFFIXES,
    split_site,
)
from openajar.node.egress.constants import WEB_SCHEMES
from openajar.vault.domains import (  # noqa: F401 - the guard reads them here, so a test can patch them
    registrable_domain,
    same_site,
)

# ---- site lists --------------------------------------------------------------------------

def host_of(url: str) -> str | None:
    """The host a browser contacts for a web URL, or None for about:, data:, blob: and the like."""
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme not in WEB_SCHEMES:
        return None
    try:
        return globs.normalize_domain(url)
    except InvalidRequest:
        try:
            host = urlsplit(url).hostname
        except ValueError:
            return ""
        if host and "%" in host:  # a browser decodes the host before it resolves it
            with contextlib.suppress(InvalidRequest):
                return globs.normalize_domain(unquote(host))
        return host or ""


_PCT = re.compile(r"%([0-9A-Fa-f]{2})")


def norm_path(url: str) -> str:
    """A URL's path as a server reads it: percent-encoded unreserved characters decoded and dot
    segments removed, so `/svc/./shreddit/%67raphql` is the same path as `/svc/shreddit/graphql`."""
    try:
        path = urlsplit(url).path or "/"
    except ValueError:
        return "/"

    def unreserved(m: re.Match[str]) -> str:
        c = chr(int(m.group(1), 16))
        return c if (c.isascii() and c.isalnum()) or c in "-._~" else m.group(0).upper()

    out: list[str] = []
    for seg in _PCT.sub(unreserved, path).split("/"):
        if seg == ".":
            continue
        if seg == "..":
            if len(out) > 1:
                out.pop()
            continue
        out.append(seg)
    path = "/".join(out)
    return path if path.startswith("/") else "/" + path


def in_list(patterns: tuple[str, ...] | list[str], host: str) -> bool:
    """A grant's egress list (plain globs patterns)."""
    return any(globs.domain_match(p, host) for p in patterns)


def site_match(pattern: str, host: str) -> bool:
    """One site pattern (no `frame:`): globs syntax, or `<stem>.google.@cc` on a Google country domain."""
    if not pattern.endswith("." + COUNTRY_TOKEN):
        return globs.domain_match(pattern, host)
    i = host.rfind("google.")
    if i < 0 or (i > 0 and host[i - 1] != ".") or host[i + len("google."):] not in GOOGLE_COUNTRY_SUFFIXES:
        return False
    return globs.domain_match(pattern.removesuffix(COUNTRY_TOKEN) + "com", host[:i] + "google.com")


def site_scope(sites: tuple[str, ...] | list[str], host: str) -> str | None:
    """How a lane's site list admits a host: "page" (any document), "frame" (subframes only) or None."""
    scope = None
    for entry in sites:
        frame, pattern = split_site(entry)
        if site_match(pattern, host):
            if not frame:
                return "page"
            scope = "frame"
    return scope


def in_sites(sites: tuple[str, ...] | list[str], host: str, *, frame: bool = False) -> bool:
    """Does the lane's site list admit this host, as a page (or, with `frame`, as a subframe)?"""
    scope = site_scope(sites, host)
    return scope == "page" or (frame and scope == "frame")


def allow_entry(host: str, frame: bool) -> str | None:
    """The site entry that would admit this refused document: the host itself, as `frame:` for a
    subframe, and with `.@cc` for a host on a Google country domain other than google.com."""
    if not host or host == "?":
        return None
    entry = host
    i = host.rfind("google.")
    suffix = host[i + len("google."):] if i >= 0 else ""
    if i >= 0 and (i == 0 or host[i - 1] == ".") and suffix != "com" and suffix in GOOGLE_COUNTRY_SUFFIXES:
        entry = host[:i] + "google." + COUNTRY_TOKEN
    return FRAME_PREFIX + entry if frame else entry
