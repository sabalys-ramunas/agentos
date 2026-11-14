"""The real `Net` port for discovery: DNS, one https document, and a TLS answer.

The fetch resolves a name, checks every address, and then connects to the very address it checked,
so a name cannot answer differently the second time. It tries each address until one answers. It
verifies the certificate against the name, stops at a size cap, and follows a redirect only to another
https address that passes the same checks. Nothing but the domain is in any URL it is given. Every
read is under a deadline of its own (agentos.runner.mail.wire), so a thread ends by itself.
"""

from __future__ import annotations

import asyncio
import http.client
import logging
import socket
import ssl
from collections.abc import Callable

from agentos.runner.mail import session
from agentos.runner.mail.discovery import (
    MAX_CONFIG,
    Refused,
    Srv,
    all_public,
    check_https_url,
    redirect_target,
)
from agentos.runner.mail.providers import STARTTLS, TLS
from agentos.runner.mail.wire import Deadline, MailFailed, WireSocket

log = logging.getLogger("agentos.runner")

TIMEOUT = 8.0
DNS_LIFETIME = 5.0
DEADLINE = 20.0
MAX_REDIRECTS = 3
USER_AGENT = "agentos-mail-setup"
HTTPS_PORT = 443
MAX_HEADER_LINE = 65_536
MAX_RESPONSE = 2 * MAX_CONFIG


def resolve(host: str, port: int = 443) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]) for i in infos})


class _Pinned(http.client.HTTPSConnection):
    """https to one checked address, with the certificate verified against the name."""

    def __init__(self, host: str, address: str, *, context: ssl.SSLContext, deadline: Deadline):
        super().__init__(host, HTTPS_PORT, timeout=min(TIMEOUT, deadline.left()), context=context)
        self.address, self.deadline = address, deadline
        self.raw: socket.socket | None = None

    def connect(self) -> None:
        sock = socket.create_connection((self.address, HTTPS_PORT), min(TIMEOUT, self.deadline.left()))
        self.raw = sock
        self.raw = self._context.wrap_socket(sock, server_hostname=self.host)
        self.sock = WireSocket(self.raw, self.deadline, max_line=MAX_HEADER_LINE, max_total=MAX_RESPONSE)  # type: ignore[assignment]

    def end(self) -> None:
        self.close()
        if self.raw is not None:
            self.raw.close()


def _ask(conn: _Pinned, url: str, deadline: Deadline) -> tuple[bytes | None, str | None]:
    """One answer: (the document, None), (None, where it redirects), or (None, None) for no document."""
    parts = url.split("/", 3)
    conn.request("GET", "/" + (parts[3] if len(parts) > 3 else ""),
                 headers={"User-Agent": USER_AGENT, "Accept": "application/xml, text/xml"})
    response = conn.getresponse()
    if response.status in (301, 302, 303, 307, 308):
        return None, redirect_target(url, response.getheader("Location"))
    if response.status != 200:
        return None, None
    data = bytearray()
    while len(data) <= MAX_CONFIG:
        deadline.left()
        chunk = response.read(16_384)
        if not chunk:
            return bytes(data), None
        data += chunk
    raise Refused("that config is too big")


def _read(url: str, context: ssl.SSLContext, resolver: Callable[[str], list[str]]) -> bytes | None:
    deadline = Deadline(DEADLINE)
    for _ in range(MAX_REDIRECTS + 1):
        host = check_https_url(url)
        addresses = resolver(host)
        if not all_public(addresses):
            raise Refused(f"{host} does not resolve to a public address")
        for address in addresses:
            conn = _Pinned(host, address, context=context, deadline=deadline)
            try:
                data, target = _ask(conn, url, deadline)
                break
            except OSError:  # this address did not answer, so the next one may
                continue
            finally:
                conn.end()
        else:
            return None
        if target is None:
            return data
        url = target
    raise Refused("too many redirects")


def _answers(host: str, port: int, security: str, context: ssl.SSLContext) -> bool:
    """One verified TLS handshake, and nothing typed: the probe of step 6."""
    deadline = Deadline(TIMEOUT)
    try:
        if security == TLS:
            with socket.create_connection((host, port), TIMEOUT) as raw, context.wrap_socket(raw, server_hostname=host):
                return True
        if port == 143:
            imap = session.open_imap(host, port, STARTTLS, context, timeout=TIMEOUT, deadline=deadline)
            imap.shutdown()
            return True
        smtp = session.Smtp(host, port, timeout=TIMEOUT, local_hostname=session.LOCAL_NAME, deadline=deadline)
        try:
            smtp.ehlo()
            if not smtp.has_extn("starttls"):
                return False
            smtp.starttls(context=context)
            return True
        finally:
            smtp.close()
    except Exception:  # noqa: BLE001 - any failure is "it does not answer"
        return False


class RealNet:
    def __init__(self, context: Callable[[], ssl.SSLContext] = ssl.create_default_context,
                 resolver: Callable[[str], list[str]] = resolve):
        self.context, self.resolver = context, resolver

    async def mx(self, domain: str) -> list[str]:
        return await asyncio.to_thread(self._mx, domain)

    @staticmethod
    def _mx(domain: str) -> list[str]:
        import dns.exception
        import dns.resolver

        try:
            answer = dns.resolver.resolve(domain, "MX", lifetime=DNS_LIFETIME)
        except dns.exception.DNSException:
            return []
        rows = sorted(answer, key=lambda r: r.preference)
        return [str(r.exchange).rstrip(".").lower() for r in rows]

    async def srv(self, name: str) -> list[Srv]:
        return await asyncio.to_thread(self._srv, name)

    @staticmethod
    def _srv(name: str) -> list[Srv]:
        import dns.exception
        import dns.resolver

        try:
            answer = dns.resolver.resolve(name, "SRV", lifetime=DNS_LIFETIME)
        except dns.exception.DNSException:
            return []
        return [Srv(str(r.target).rstrip("."), r.port, r.priority, r.weight) for r in answer]

    async def addresses(self, host: str) -> list[str]:
        return await asyncio.to_thread(self.resolver, host)

    async def fetch(self, url: str) -> bytes | None:
        try:
            return await asyncio.wait_for(asyncio.to_thread(_read, url, self.context(), self.resolver), DEADLINE + 2)
        except (OSError, http.client.HTTPException, TimeoutError, MailFailed):  # ssl errors are OSErrors: no answer, next step
            return None

    async def answers_tls(self, host: str, port: int, security: str) -> bool:
        return await asyncio.to_thread(_answers, host, port, security, self.context())

