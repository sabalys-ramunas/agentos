"""Where a webhook may send: a public https address, and nothing inside this computer's network.

A page can ask for a webhook, so a webhook must never be a way in to the machine or its network.
Three layers, because each one alone has a known way around it. The URL is checked before anything
leaves. The name is resolved and every address it returns must be public. The socket then connects
to the address that was checked and no other, so a name that changes its answer between the check and
the connection reaches nothing. Redirects are never followed, so a public URL cannot hand the request
on to a private one. Proxies from the environment are ignored, because a proxy would make the address
check meaningless.

This is the hosted cloud's rule, ported so the node enforces it too.
The node's own notifier posts to whatever URL it is given; webhooks are the one place a page's input
reaches the network, so they take the stricter rule.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit

import httpcore
import httpx

ALLOWED_PORTS = frozenset({443, 8443})
TIMEOUT = httpx.Timeout(10, connect=5)
TOTAL_SECONDS = 20

# Names that only ever mean something inside a network, refused before any lookup.
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa")
_INTERNAL_NAMES = frozenset({"localhost", "metadata", "metadata.google.internal", "instance-data"})
# Ranges `is_global` does not catch: a cloud's platform address is public on paper, and these IPv6
# transition prefixes wrap an IPv4 address, which may be a private one.
_REFUSED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("168.63.129.16/32", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
)

Resolver = Callable[[str, int], Awaitable[list[str]]]


class OutboundRefused(Exception):
    """The destination is not allowed. The message is safe to show the person who entered it."""


class OutboundUnresolved(OutboundRefused):
    """The name did not resolve. Refused for a person entering a URL; at send time it is a hiccup to
    retry, never a reason to give up on a webhook."""


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if any(address in network for network in _REFUSED_NETWORKS):
        return False
    return address.is_global and not address.is_multicast and not address.is_reserved


async def system_resolver(host: str, port: int) -> list[str]:
    results = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(item[4][0]) for item in results))


def _literal(host: str) -> bool:
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host)
        return True
    return False


def check_url(url: str) -> SplitResult:
    """Everything that can be refused without touching the network."""
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError as error:
        raise OutboundRefused("That URL is not valid.") from error
    if parts.scheme != "https":
        raise OutboundRefused("Use an https URL.")
    if parts.username or parts.password:
        raise OutboundRefused("Leave the user name and password out of the URL.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise OutboundRefused("That URL has no host.")
    if port not in ALLOWED_PORTS:
        raise OutboundRefused("Use port 443 or 8443.")
    if host in _INTERNAL_NAMES or host.endswith(_INTERNAL_SUFFIXES):
        raise OutboundRefused("That address is inside a private network.")
    if _literal(host) and not is_public_address(host):
        raise OutboundRefused("That address is inside a private network.")
    return parts


async def public_addresses(host: str, port: int, resolve: Resolver) -> list[str]:
    """Every answer must be public. A name with one public and one private address is refused,
    because which of them gets used is not ours to choose."""
    try:
        addresses = await resolve(host, port)
    except OSError as error:
        raise OutboundUnresolved("That host could not be found.") from error
    if not addresses:
        raise OutboundUnresolved("That host could not be found.")
    if not all(is_public_address(address) for address in addresses):
        raise OutboundRefused("That address is inside a private network.")
    return addresses


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Resolves and checks at the moment of connecting, then dials the checked address itself.
    TLS still verifies the name in the URL, because httpcore passes it separately."""

    def __init__(self, resolve: Resolver, inner: httpcore.AsyncNetworkBackend | None = None):
        self._resolve = resolve
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await public_addresses(host, port, self._resolve)
        return await self._inner.connect_tcp(
            addresses[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise OutboundRefused("That address is inside a private network.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, backend: httpcore.AsyncNetworkBackend):
        super().__init__(trust_env=False)
        verify = self._pool._ssl_context
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=verify, network_backend=backend, max_connections=20, keepalive_expiry=0
        )


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    retry_after: str | None = None


class Outbound:
    """The one client for webhook requests. Replies are read for their status only: a body is never
    kept, so an endpoint cannot fill memory or the delivery log."""

    def __init__(self, resolve: Resolver = system_resolver, transport: httpx.AsyncBaseTransport | None = None):
        self._resolve = resolve
        self._client = httpx.AsyncClient(
            transport=transport or GuardedTransport(GuardedBackend(resolve)),
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=False,
        )

    async def check(self, url: str) -> None:
        """For a person entering a URL: the same answer delivery would give, right away."""
        parts = check_url(url)
        await public_addresses(parts.hostname or "", parts.port or 443, self._resolve)

    async def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> Reply:
        await self.check(url)
        async with (
            asyncio.timeout(TOTAL_SECONDS),
            self._client.stream("POST", url, content=content, headers=headers) as response,
        ):
            return Reply(response.status_code, response.headers.get("retry-after"))

    async def aclose(self) -> None:
        await self._client.aclose()
