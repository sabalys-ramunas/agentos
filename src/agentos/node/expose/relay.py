"""One HTTP request to the owner's dev server, over the unix socket the SSH reverse forward made.

The socket is the only place a request goes: the URL's host and port only pick an expose, and are
never dialled. Bodies are whole (Playwright fulfils a route with a whole body), so a streamed answer
(server-sent events, a long poll) arrives only when it ends, and both directions are capped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

TIMEOUT_S = 60.0
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024

# never forwarded either way: they describe one connection, not the request (RFC 9110 7.6.1), or they
# are the relay's to set
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
                        "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
                        "accept-encoding", "content-encoding"})


class RelayError(Exception):
    """The dev server could not be reached or answered past a limit; the message is for the page."""


@dataclass
class Answer:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    def fulfil_headers(self) -> dict[str, str]:
        """Headers as Playwright's fulfil takes them: one value per name, Set-Cookie values joined by
        a newline (Firefox keeps several Set-Cookie lines that way, and sets each)."""
        out: dict[str, str] = {}
        for name, value in self.headers:
            key = name.lower()
            if key in HOP_BY_HOP:
                continue
            if key in out:
                out[key] = out[key] + ("\n" if key == "set-cookie" else ", ") + value
            else:
                out[key] = value
        return out


def request_headers(headers: dict[str, str], host: str) -> list[tuple[str, str]]:
    """What the dev server gets: the browser's headers less the hop-by-hop ones, the exposed host as
    Host (an app builds its absolute URLs and OAuth redirect URIs from it), X-Forwarded-Proto https
    and X-Forwarded-Host, and identity encoding (the body is fulfilled as it arrives)."""
    out = [(k, v) for k, v in headers.items() if k.lower() not in HOP_BY_HOP and not k.lower().startswith("x-forwarded-")]
    out += [("host", host), ("x-forwarded-proto", "https"), ("x-forwarded-host", host),
            ("accept-encoding", "identity")]
    return out


async def fetch(socket_path: Path, method: str, host: str, target: str, headers: list[tuple[str, str]],
                body: bytes | None, *, timeout_s: float = TIMEOUT_S, transport: Any = None) -> Answer:
    """Send one request through the socket and read the whole answer (RelayError when it cannot)."""
    if body is not None and len(body) > MAX_REQUEST_BYTES:
        raise RelayError(f"the request body is over {MAX_REQUEST_BYTES // (1024 * 1024)} MB")
    transport = transport or httpx.AsyncHTTPTransport(uds=str(socket_path), retries=0)
    async with httpx.AsyncClient(transport=transport, timeout=timeout_s, follow_redirects=False,
                                 trust_env=False) as client:
        try:
            req = client.build_request(method, f"http://{host}{target}", headers=headers, content=body)
            resp = await client.send(req, stream=True)
        except httpx.TimeoutException:
            raise RelayError(f"the dev server did not answer within {int(timeout_s)} seconds") from None
        except (httpx.HTTPError, OSError):
            raise RelayError("the dev server on your computer did not answer (is it running, and is "
                             "`agentos expose` still running?)") from None
        try:
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise RelayError(f"the answer is over {MAX_RESPONSE_BYTES // (1024 * 1024)} MB")
                chunks.append(chunk)
        except httpx.HTTPError:
            raise RelayError("the dev server closed the connection before its answer ended") from None
        finally:
            await resp.aclose()
        return Answer(status=resp.status_code, headers=list(resp.headers.multi_items()), body=b"".join(chunks))
