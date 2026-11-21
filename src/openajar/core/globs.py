"""Lane and domain patterns, with matching and (conservative) containment.

Lane patterns are `workspace/name`, where each segment is a literal, `*`, or a literal
prefix followed by `*` (`biz-a/insta*`). `*` never crosses the `/`, so `biz-a/*` does not
match `biz-ab/x`. A bare `*` means every lane.

Domain patterns are an exact host (`instagram.com`), `*.instagram.com` (any subdomain,
not the apex), or `*` (any host). `instagram.com` never matches `badinstagram.com`.

Containment (`child within parent`) is used to reject widening when a grant is narrowed.
It is conservative: when it cannot prove containment it says no.

URLs are read the way WHATWG browsers read them (`parse_web_url`), and any string where a
browser and a simpler parser could see different hosts is refused outright: backslashes,
control characters or spaces, a scheme not followed by exactly `//`, more than one `@`,
percent-encoded or other forbidden bytes in the host, and numeric hosts a browser would
reinterpret as IPv4 (`0x7f.1`, `2130706433`). Unicode hosts are mapped with UTS 46
non-transitional processing, as browsers do (`faß.de` is `xn--fa-hia.de`, never `fass.de`).
The policy's domain checks and the node's URL check (openajar.node.tools.inputs.normalize_url) use
this one parser, so the host that is authorized is the host the browser contacts.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

import idna

from openajar.core.errors import InvalidRequest

_SEG_RE = re.compile(r"^(\*|[a-z0-9][a-z0-9-]*\*?)$")
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
# a scheme as browsers and openajar.node.tools read it; "localhost:8080" is a host and a port
SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(?!\d)")
WEB_SCHEMES = ("http", "https", "ws", "wss")
_SPECIAL_SCHEMES = {"http", "https", "ws", "wss", "ftp", "file"}
# WHATWG forbidden host code points, plus "%" (a browser percent-decodes the host first)
_FORBIDDEN_HOST = frozenset("\x00\t\n\r #/:<>?@[\\]^|%")
_HEX_RE = re.compile(r"^0[xX][0-9a-fA-F]*$")


def validate_segment(seg: str) -> str:
    if not _SEG_RE.match(seg):
        raise InvalidRequest(f"bad pattern segment {seg!r} (lowercase, digits, dashes, optional trailing *)")
    return seg


def validate_lane_pattern(pattern: str) -> str:
    if pattern == "*":
        return pattern
    parts = pattern.split("/")
    if len(parts) != 2:
        raise InvalidRequest(f"lane pattern must look like 'workspace/name' or '*': {pattern!r}")
    for p in parts:
        validate_segment(p)
    return pattern


def _split_lane_pattern(pattern: str) -> tuple[str, str]:
    if pattern == "*":
        return "*", "*"
    ws, name = pattern.split("/", 1)
    return ws, name


def segment_match(pattern: str, value: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return pattern == value


def lane_match(pattern: str, lane_id: str) -> bool:
    if lane_id.count("/") != 1:  # a lane id is exactly workspace/name: `ws/*` never reaches ws/a/b
        return False
    ws, name = lane_id.split("/", 1)
    pws, pname = _split_lane_pattern(pattern)
    return segment_match(pws, ws) and segment_match(pname, name)


def segment_within(child: str, parent: str) -> bool:
    if parent == "*":
        return True
    if child == "*":
        return False
    if parent.endswith("*"):
        prefix = parent[:-1]
        return child.rstrip("*").startswith(prefix) if child.endswith("*") else child.startswith(prefix)
    return child == parent


def lane_pattern_within(child: str, parent: str) -> bool:
    cws, cname = _split_lane_pattern(child)
    pws, pname = _split_lane_pattern(parent)
    return segment_within(cws, pws) and segment_within(cname, pname)


@dataclass(frozen=True)
class WebURL:
    """The parts of an absolute URL that decide where a browser connects."""
    scheme: str             # lowercase
    userinfo: str | None    # None when there is no "@"; "" for "https://@host/"
    host: str               # lowercase ASCII (UTS 46); IPv6 in brackets; may end in "."
    port: int | None


def _ambiguous(what: str) -> InvalidRequest:
    # never quotes the URL, which may carry a token or a password
    return InvalidRequest(f"ambiguous or malformed URL: {what}")


def _ends_in_number(host: str) -> bool:
    labels = host.split(".")
    if labels and labels[-1] == "":
        labels = labels[:-1]
    last = labels[-1] if labels else ""
    return bool(last) and (last.isdigit() and last.isascii() or bool(_HEX_RE.match(last)))


def canonical_host(host: str) -> str:
    """A host as a WHATWG browser serializes it: lowercase, Unicode mapped to ASCII with UTS 46
    (non-transitional), IPv4 only in dotted-quad form. Raises InvalidRequest for a host a
    browser would refuse or read differently."""
    if not host:
        raise _ambiguous("no host")
    if host.startswith("["):
        if not host.endswith("]"):
            raise _ambiguous("malformed IPv6 host")
        try:
            return "[" + ipaddress.IPv6Address(host[1:-1]).compressed + "]"
        except ValueError:
            raise _ambiguous("malformed IPv6 host") from None
    if any(c in _FORBIDDEN_HOST or c < " " or c == "\x7f" for c in host):
        raise _ambiguous("forbidden character in the host")
    if not host.isascii():
        try:
            host = idna.encode(host, uts46=True, transitional=False).decode("ascii")
        except (idna.IDNAError, UnicodeError, ValueError):
            raise _ambiguous("the host is not a valid internationalized name") from None
        if any(c in _FORBIDDEN_HOST for c in host):
            raise _ambiguous("forbidden character in the host")
    host = host.lower()
    if _ends_in_number(host):
        try:
            ip = ipaddress.IPv4Address(host.rstrip("."))
        except ValueError:
            ip = None
        if ip is None or str(ip) != host.rstrip("."):
            raise _ambiguous("a numeric host must be a dotted-quad IPv4 address")
    return host


def parse_web_url(raw: str) -> WebURL:
    """Read an absolute URL with an authority (`scheme://host...`) the way WHATWG browsers do,
    or raise InvalidRequest wherever a browser and a simpler parser could disagree about the
    host. Error messages never quote the URL."""
    if any(c <= " " or c == "\x7f" for c in raw):
        raise _ambiguous("control characters or spaces")
    if "\\" in raw:
        raise _ambiguous("backslash (browsers read it as a slash)")
    m = SCHEME_RE.match(raw)
    if not m:
        raise _ambiguous("no scheme")
    scheme = m.group(1).lower()
    rest = raw[m.end():]
    if not rest.startswith("//") or (scheme in _SPECIAL_SCHEMES and rest.startswith("///")):
        raise _ambiguous("the scheme must be followed by exactly //host")
    rest = rest[2:]
    end = min((i for i in (rest.find(c) for c in "/?#") if i >= 0), default=len(rest))
    authority = rest[:end]
    userinfo: str | None = None
    hostport = authority
    if "@" in authority:
        userinfo, _, hostport = authority.rpartition("@")
        if "@" in userinfo:
            raise _ambiguous("more than one @ in the authority")
    if hostport.startswith("["):
        close = hostport.find("]")
        if close < 0:
            raise _ambiguous("malformed IPv6 host")
        host, after = hostport[:close + 1], hostport[close + 1:]
        if after and not after.startswith(":"):
            raise _ambiguous("malformed IPv6 host")
        port_s = after[1:] if after else ""
    else:
        host, _, port_s = hostport.partition(":")
    port = None
    if port_s:
        if not (port_s.isascii() and port_s.isdigit()) or int(port_s) > 65535:
            raise _ambiguous("bad port")
        port = int(port_s)
    return WebURL(scheme=scheme, userinfo=userinfo, host=canonical_host(host), port=port)


def url_host(url: str) -> str:
    """The host a browser contacts for an http(s) URL, normalized like `normalize_domain`."""
    return normalize_domain(url)


def normalize_domain(value: str) -> str:
    """Host from a domain or a web URL: lowercase ASCII (UTS 46), no port, no trailing dot.
    Raises InvalidRequest for anything a browser could read as another host (see the module
    docstring), including userinfo in a string without a scheme."""
    raw = value.strip()
    if SCHEME_RE.match(raw):
        url = parse_web_url(raw)
        if url.scheme not in WEB_SCHEMES:
            raise InvalidRequest("a domain must be a host name or an http(s) URL")
        host = url.host
    else:
        if "@" in raw:
            # "instagram.com:@evil.example": a tool that adds https:// reaches evil.example
            raise _ambiguous("userinfo in a domain without a scheme")
        host = parse_web_url("https://" + raw).host
    host = host.removesuffix(".")
    labels = host.split(".")
    if not host or not all(_LABEL_RE.match(lbl) for lbl in labels):
        raise InvalidRequest(f"bad domain {host[:80]!r}")
    return host


def validate_domain_pattern(pattern: str) -> str:
    p = pattern.strip().lower()
    if p == "*":
        return p
    if p.startswith("*."):
        return "*." + normalize_domain(p[2:])
    return normalize_domain(p)


def domain_match(pattern: str, host: str) -> bool:
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        base = pattern[2:]
        return host.endswith("." + base)
    return host == pattern


def domain_pattern_within(child: str, parent: str) -> bool:
    if parent == "*":
        return True
    if child == "*":
        return False
    if parent.startswith("*."):
        base = parent[2:]
        if child.startswith("*."):
            c = child[2:]
            return c == base or c.endswith("." + base)
        return child.endswith("." + base)
    return child == parent


# ---- git repos (the vm cap's `repos`) ------------------------------------------------
_REPO_PATTERN_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+"
    r"(/[A-Za-z0-9_.-]{1,100})*(/\*|/[A-Za-z0-9_.-]{1,100})$")


def validate_repo_pattern(pattern: str) -> str:
    """host/org/repo, or host/org/* for every repo under a path (lowercase host, no scheme)."""
    p = (pattern or "").strip()
    if not _REPO_PATTERN_RE.match(p) or any(s in (".", "..") for s in p.split("/")):
        raise InvalidRequest(f"a repo pattern looks like github.com/acme/* or github.com/acme/api: {pattern!r}")
    return p


def repo_match(pattern: str, repo: str) -> bool:
    if pattern.endswith("/*"):
        return repo.startswith(pattern[:-1])
    return pattern == repo


def repo_pattern_within(child: str, parent: str) -> bool:
    return child == parent or (parent.endswith("/*") and child.startswith(parent[:-1]))
