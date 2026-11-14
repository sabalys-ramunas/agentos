"""The address rule for webhooks: what may be called, and what is refused before anything leaves.

Nothing here touches the network. Names are answered by a fake resolver, and requests by a fake transport.
"""

from __future__ import annotations

import httpx
import pytest

from agentos.webhooks.outbound import (
    GuardedBackend,
    Outbound,
    OutboundRefused,
    OutboundUnresolved,
    check_url,
    is_public_address,
)

PUBLIC = "93.184.216.34"


async def answer(*addresses: str):
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


async def unresolved(host: str, port: int) -> list[str]:
    raise OSError("no such host")


@pytest.mark.parametrize("url", [
    "http://hooks.example.com/x",                 # not https
    "https://127.0.0.1/x",                        # this computer
    "https://[::1]/x",                            # this computer, IPv6
    "https://10.1.2.3/x",                         # a private network
    "https://192.168.0.9/x",
    "https://169.254.169.254/latest",             # a cloud's metadata address
    "https://localhost/x",
    "https://printer.local/x",
    "https://box.internal/x",
    "https://metadata.google.internal/x",
    "https://user:secret@hooks.example.com/x",    # a password in the address
    "https://hooks.example.com:8080/x",           # not an allowed port
    "https:///x",                                 # no host
    "not a url",
])
def test_an_address_that_is_not_allowed_is_refused_without_a_lookup(url: str) -> None:
    with pytest.raises(OutboundRefused):
        check_url(url)


def test_a_public_https_address_on_443_or_8443_passes_the_check() -> None:
    assert check_url("https://hooks.example.com/agentos").hostname == "hooks.example.com"
    assert check_url("https://hooks.example.com:8443/agentos").port == 8443


@pytest.mark.parametrize("value, public", [
    ("93.184.216.34", True),
    ("2001:4860:4860::8888", True),
    ("127.0.0.1", False),
    ("10.0.0.1", False),
    ("172.16.4.4", False),
    ("100.64.0.1", False),                        # carrier-grade NAT
    ("169.254.169.254", False),
    ("::1", False),
    ("fc00::1", False),
    ("::ffff:10.0.0.1", False),                   # IPv4 wrapped in IPv6
    ("64:ff9b::a00:1", False),                    # NAT64 wrapping a private address
    ("not-an-address", False),
])
def test_only_public_addresses_are_public(value: str, public: bool) -> None:
    assert is_public_address(value) is public


async def test_a_public_answer_is_sent_and_nothing_is_followed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://10.0.0.1/inside", "Retry-After": "90"})

    outbound = Outbound(resolve=await answer(PUBLIC), transport=httpx.MockTransport(handler))
    reply = await outbound.post("https://hooks.example.com/x", content=b"{}", headers={"X-Test": "1"})
    assert reply.status == 302 and reply.retry_after == "90"
    assert len(seen) == 1 and seen[0].headers["X-Test"] == "1"  # the redirect was not followed
    await outbound.aclose()


async def test_a_name_with_a_private_answer_is_never_sent_to() -> None:
    seen: list[httpx.Request] = []
    outbound = Outbound(resolve=await answer(PUBLIC, "10.0.0.7"),
                        transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)))
    with pytest.raises(OutboundRefused):
        await outbound.post("https://hooks.example.com/x", content=b"{}", headers={})
    assert seen == []
    await outbound.aclose()


async def test_a_name_that_does_not_resolve_is_a_retryable_refusal() -> None:
    outbound = Outbound(resolve=unresolved, transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    with pytest.raises(OutboundUnresolved):
        await outbound.check("https://hooks.example.com/x")
    await outbound.aclose()


async def test_the_connection_itself_dials_only_a_checked_public_address() -> None:
    backend = GuardedBackend(await answer("10.0.0.7"))
    with pytest.raises(OutboundRefused):
        await backend.connect_tcp("hooks.example.com", 443)


async def test_the_real_transport_refuses_a_private_name_before_it_connects() -> None:
    """The default client (not a test double) is built and refuses a private answer before any socket opens."""
    outbound = Outbound(resolve=await answer("10.0.0.7"))
    with pytest.raises(OutboundRefused, match="private network"):
        await outbound.post("https://hooks.example.com/x", content=b"{}", headers={})
    await outbound.aclose()
