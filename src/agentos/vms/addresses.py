"""This computer's public addresses, as the internet sees them: the VM firewall lets SSH in from them."""

from __future__ import annotations

import ipaddress

import httpx

EXIT_IP_URLS = ("https://api.ipify.org", "https://ipinfo.io/ip")
EXIT_IP6_URLS = ("https://api6.ipify.org", "https://v6.ipinfo.io/ip")


async def lookup_public_ips() -> list[str]:
    """This computer's public IPv4 address and, when it has one, its IPv6 address, as the internet
    sees them: each asked over its own address family. [] when nothing answered."""
    out: list[str] = []
    for local, urls in (("0.0.0.0", EXIT_IP_URLS), ("::", EXIT_IP6_URLS)):
        for url in urls:
            try:
                transport = httpx.AsyncHTTPTransport(local_address=local)
                async with httpx.AsyncClient(timeout=10, transport=transport) as c:
                    r = await c.get(url, headers={"User-Agent": "agentos-vms", "Accept": "text/plain"})
                out.append(str(ipaddress.ip_address(r.text.strip())))
                break
            except (httpx.HTTPError, ValueError, OSError):
                continue
    return out
