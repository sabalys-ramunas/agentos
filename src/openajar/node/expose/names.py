"""The reserved names an exposed dev server answers at in a lane, and the limits of an expose.

The host is `<name>.expose.openajar.invalid`, always over https:

- `.invalid` is reserved by RFC 2606 and RFC 6761: no public or private resolver may ever answer
  for it. So every path that is not the lane's route fails closed. Another lane, a redirect hop
  (never routed), a WebSocket, a service worker's own fetch, or this lane after the expose ended
  gets a name that does not resolve, never some other machine. `.internal` (ICANN's private-use
  name) can be answered by a private resolver on the node's network, and `localhost` or
  `*.localhost` would fall through to the node's own loopback, which is exactly what a lane must
  never reach.
- https, because the browser never connects anywhere for it (the route answers inside the
  browser's driver, so no certificate is needed). That makes the page a secure context, so
  `Secure` cookies, `SameSite=None` and OAuth providers that insist on https redirect URIs work,
  and on a redirect hop that is not routed, the lane's proxy (or the node's resolver) learns only
  the host name: an https URL's path and query never reach a proxy.
"""

from __future__ import annotations

import re

from openajar.core.errors import InvalidRequest

SUFFIX = "expose.openajar.invalid"
SCHEME = "https"
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
# what the lane's browser routes: any name under the suffix, any scheme a page could use for it,
# any port. The handler answers only an active expose's exact origin and fails everything else.
URL_RE = re.compile(r"^(?:https?|wss?)://[^/?#@]*\.expose\.openajar\.invalid\.?(?::\d*)?(?:[/?#]|$)",
                    re.IGNORECASE)

DEFAULT_TTL_S = 2 * 3600
MAX_TTL_S = 12 * 3600
LEASE_S = 30.0            # the laptop renews every HEARTBEAT_S; a link it stops renewing ends after this
HEARTBEAT_S = 10.0
READY_WITHIN_S = 60.0     # an expose whose SSH forward never came up ends after this
MAX_PER_LANE = 4
MAX_TOTAL = 16


def validate_name(name: str) -> str:
    """A DNS label: lowercase letters, digits and inner hyphens, at most 32 characters."""
    value = (name or "").strip().lower()
    if not NAME_RE.match(value):
        raise InvalidRequest("an expose name is 1 to 32 lowercase letters, digits or hyphens (not at either "
                             "end), for example myapp")
    return value


def host_for(name: str) -> str:
    return f"{validate_name(name)}.{SUFFIX}"


def origin_for(host: str) -> str:
    return f"{SCHEME}://{host}"


def name_of(host: str) -> str | None:
    """The expose name of a host under the suffix, or None."""
    host = host.lower().rstrip(".")
    if not host.endswith("." + SUFFIX):
        return None
    name = host[: -len(SUFFIX) - 1]
    return name if NAME_RE.match(name) else None


def validate_port(port: object) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise InvalidRequest("port must be a number from 1 to 65535")
    return port


def validate_ttl(ttl_s: object) -> float:
    if ttl_s is None:
        return float(DEFAULT_TTL_S)
    if not isinstance(ttl_s, (int, float)) or isinstance(ttl_s, bool) or ttl_s <= 0:
        raise InvalidRequest("ttl_s must be a positive number of seconds")
    if ttl_s > MAX_TTL_S:
        raise InvalidRequest(f"an expose lasts at most {MAX_TTL_S // 3600} hours; start it again after that")
    return float(ttl_s)
