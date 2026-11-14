"""Hosts, registrable domains and origins, normalized the way a browser sees them.

Pure functions. Everything is compared in A-label (punycode) form, so a Cyrillic
lookalike such as `instаgram.com` becomes `xn--instgram-46g.com` and can never equal
`instagram.com`. Anything a browser would not treat as one plain host (userinfo,
backslashes, percent escapes, whitespace, odd numeric IPv4 forms) is rejected rather
than guessed at.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

import idna

from agentos.core.models import GOOGLE_COUNTRY_SUFFIXES

_ASCII_LABEL_RE = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
_FORBIDDEN_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
_FORBIDDEN_HOST_CHARS = set("%@/?#[]\\:*^|<>\"'`{} ")
_NUMERICISH_RE = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")

DEFAULT_PORTS = {"http": 80, "https": 443, "imaps": 993, "imap": 143}


@lru_cache(maxsize=1)
def _psl():
    # Bundled Public Suffix List (ICANN and private sections), no network. Unknown TLDs
    # have no registrable domain, so they can only ever match exactly (see below).
    from publicsuffixlist import PublicSuffixList

    return PublicSuffixList(accept_unknown=False)


def _normalize_ip(host: str) -> str | None:
    """Canonical text of an IP literal, or None if `host` is not one."""
    if ":" in host:
        try:
            return ipaddress.IPv6Address(host).compressed
        except ValueError as e:
            raise ValueError("not a valid IPv6 address") from e
    last = host.rsplit(".", 1)[-1]
    if _NUMERICISH_RE.match(last):
        # A browser would parse this as IPv4 (including forms like 0x7f.1 or 2130706433).
        # Only the canonical dotted-quad is accepted.
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError as e:
            raise ValueError("not a canonical IPv4 address") from e
    return None


def normalize_host(host: str) -> str:
    """Lowercase A-label form of a host, without a trailing dot. Raises ValueError."""
    if not isinstance(host, str) or not host or len(host) > 260:
        raise ValueError("empty or overlong host")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        if ":" not in host:
            raise ValueError("bracketed host must be IPv6")
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in host):
        raise ValueError("control or space characters in host")
    host = host.removesuffix(".")
    if not host:
        raise ValueError("empty host")
    if ":" in host:
        if "%" in host:
            raise ValueError("IPv6 zone ids are not allowed")
        ip = _normalize_ip(host)
        assert ip is not None
        return ip
    if any(c in _FORBIDDEN_HOST_CHARS for c in host):
        raise ValueError("forbidden character in host")
    if host.isascii():
        h = host.lower()
    else:
        try:
            h = idna.encode(host, uts46=True, transitional=False).decode("ascii")
        except (idna.IDNAError, UnicodeError) as e:
            raise ValueError("not a valid internationalized host") from e
        if any(c in _FORBIDDEN_HOST_CHARS for c in h):
            raise ValueError("forbidden character in host")
    h = h.removesuffix(".")
    ip = _normalize_ip(h)
    if ip is not None:
        return ip
    if len(h) > 253:
        raise ValueError("host too long")
    labels = h.split(".")
    for label in labels:
        if not _ASCII_LABEL_RE.match(label):
            raise ValueError("invalid host label")
        if label.startswith("xn--"):
            try:
                idna.decode(label)
            except (idna.IDNAError, UnicodeError) as e:
                raise ValueError("invalid punycode label") from e
    return h


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_loopback(host: str) -> bool:
    """`localhost` or a loopback IP (127.0.0.0/8, ::1). Expects a normalized host."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def registrable_domain(host: str) -> str | None:
    """eTLD+1 of a host (`www.instagram.com` -> `instagram.com`, `a.b.co.uk` -> `b.co.uk`,
    `me.github.io` -> `me.github.io`). IPs and `localhost` are their own registrable
    domain. None for public suffixes and unknown TLDs."""
    h = normalize_host(host)
    if is_ip(h) or h == "localhost":
        return h
    return _psl().privatesuffix(h)


def _on_google(domain: str) -> bool:
    return domain.startswith("google.") and domain[len("google."):] in GOOGLE_COUNTRY_SUFFIXES


def same_site(registrable: str | None, domain: str) -> bool:
    """Is this registrable domain the entry's site? Exact, or both of Google's own country domains:
    one account signs in on accounts.google.com and on the exit country's accounts.google.lt, and
    can hop between them in the middle of a sign-in."""
    if registrable is None:
        return False
    return registrable == domain or (_on_google(registrable) and _on_google(domain))


def display_host(host: str) -> str:
    """Unicode form for humans (`xn--bcher-kva.de` -> `bücher.de`); falls back to input."""
    try:
        return idna.decode(host)
    except (idna.IDNAError, UnicodeError, ValueError):
        return host


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        if DEFAULT_PORTS.get(self.scheme) == self.port:
            return f"{self.scheme}://{host}"
        return f"{self.scheme}://{host}:{self.port}"


def parse_origin(url: str, *, schemes: tuple[str, ...] = ("http", "https")) -> Origin:
    """Origin of an absolute URL (or a serialized origin). Raises ValueError for opaque
    origins (`null`, `about:blank`, `data:`), userinfo, and anything ambiguous."""
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise ValueError("empty or overlong url")
    if _FORBIDDEN_URL_CHARS.search(url):
        raise ValueError("control characters, spaces or backslashes in url")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in schemes:
        raise ValueError("unsupported scheme")
    netloc = parts.netloc
    if not netloc or "@" in netloc:
        raise ValueError("missing host or userinfo present")
    if netloc.startswith("["):
        end = netloc.find("]")
        if end < 0:
            raise ValueError("unterminated IPv6 literal")
        raw_host, rest = netloc[: end + 1], netloc[end + 1 :]
        if rest and not rest.startswith(":"):
            raise ValueError("garbage after IPv6 literal")
        raw_port = rest[1:] if rest else ""
    else:
        raw_host, sep, raw_port = netloc.partition(":")
        if sep and not raw_port:
            raw_port = ""
    if raw_port:
        if not raw_port.isdigit() or len(raw_port) > 5 or not 0 < int(raw_port) < 65536:
            raise ValueError("invalid port")
        port = int(raw_port)
    else:
        port = DEFAULT_PORTS[scheme]
    return Origin(scheme=scheme, host=normalize_host(raw_host), port=port)


def host_of(value: str) -> str:
    """Host of a URL, or of a bare host / `host/path` string (`instagram.com/login`)."""
    value = value.strip()
    if "://" in value:
        return parse_origin(value, schemes=("http", "https")).host
    bare = re.split(r"[/?#]", value, maxsplit=1)[0]
    if ":" in bare and not bare.startswith("["):
        bare = bare.split(":", 1)[0]
    return normalize_host(bare)
