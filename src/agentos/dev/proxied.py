"""The dev node's browser for an agent environment: real pages, read through the environment's tunnel.

On `agentos dev up` every lane's tab is the fake social site (agentos.dev.web). An environment's
browser lane (`env/<id>-browser`) is different: its proxy is the environment's SOCKS tunnel to its
machine, so its tab fetches each page through that tunnel (DNS too, localhost included, as the real
browser does) and shows what the HTML says, as an outline of headings, links, buttons and text. No
script runs and nothing is drawn: it is enough to see that a dev server on the machine answers and
what it serves, without a browser on the laptop.
"""

from __future__ import annotations

import asyncio
import ipaddress
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from agentos.runner.fakes import El

FETCH_TIMEOUT_S = 15.0
MAX_BYTES = 2_000_000
TEXT_MAX = 300
BLOCKS = {"p", "li", "td", "th", "pre", "label", "span", "div", "main", "section", "article", "footer", "header"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class FetchError(Exception):
    pass


async def socks_fetch(url: str, host: str, port: int) -> tuple[int, str, str]:
    """GET `url` through a SOCKS5 proxy (no auth, the name resolved by the proxy). Returns
    (status, final url, body text). Follows up to 5 redirects."""
    for _ in range(6):
        status, headers, body = await _get(url, host, port)
        if status in (301, 302, 303, 307, 308) and headers.get("location"):
            url = urljoin(url, headers["location"])
            continue
        return status, url, body
    raise FetchError("too many redirects")


async def _get(url: str, proxy_host: str, proxy_port: int) -> tuple[int, dict[str, str], str]:
    parts = urlsplit(url)
    if parts.scheme != "http":
        raise FetchError(f"the dev browser reads http pages only ({parts.scheme} needs the real browser on a node)")
    host = parts.hostname or ""
    port = parts.port or 80
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(proxy_host, proxy_port), FETCH_TIMEOUT_S)
    except (OSError, TimeoutError) as e:
        raise FetchError(f"the environment's tunnel does not answer ({e})") from None
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        if await reader.readexactly(2) != b"\x05\x00":
            raise FetchError("the tunnel refused the SOCKS greeting")
        try:
            ip = ipaddress.ip_address(host)
            addr = (b"\x01" + ip.packed) if ip.version == 4 else (b"\x04" + ip.packed)
        except ValueError:
            name = host.encode("idna")
            addr = b"\x03" + bytes([len(name)]) + name
        writer.write(b"\x05\x01\x00" + addr + port.to_bytes(2, "big"))
        await writer.drain()
        head = await reader.readexactly(4)
        if head[1] != 0:
            raise FetchError(f"nothing answers at {host}:{port} on the environment's machine (SOCKS reply {head[1]})")
        skip = {1: 4, 4: 16}.get(head[3])
        if skip is None:
            skip = (await reader.readexactly(1))[0]
        await reader.readexactly(skip + 2)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        writer.write(f"GET {path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUser-Agent: agentos-dev\r\n"
                     "Accept: text/html,*/*\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(MAX_BYTES), FETCH_TIMEOUT_S)
        while len(raw) < MAX_BYTES:
            more = await asyncio.wait_for(reader.read(MAX_BYTES - len(raw)), FETCH_TIMEOUT_S)
            if not more:
                break
            raw += more
    except asyncio.IncompleteReadError:
        raise FetchError("the environment's tunnel closed the connection") from None
    finally:
        writer.close()
    head_raw, _, body = raw.partition(b"\r\n\r\n")
    lines = head_raw.decode("latin-1").split("\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError):
        raise FetchError("the page did not answer with HTTP") from None
    headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _unchunk(body)
    return status, headers, body.decode("utf-8", "replace")


def _unchunk(data: bytes) -> bytes:
    out, pos = b"", 0
    while pos < len(data):
        end = data.find(b"\r\n", pos)
        if end < 0:
            break
        try:
            size = int(data[pos:end].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += data[end + 2:end + 2 + size]
        pos = end + 2 + size + 2
    return out


class _Outline(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.items: list[El] = []
        self._tag: list[str] = []
        self._text: list[str] = []
        self._href: str | None = None
        self._skip = 0

    def _flush(self, role: str | None = None) -> None:
        text = " ".join(" ".join(self._text).split())[:TEXT_MAX]
        self._text = []
        if not text:
            return
        if role == "heading":
            self.items.append(El("heading", name=text))
        elif role == "link":
            self.items.append(El("link", name=text, attrs={"href": self._href or ""}))
        elif role == "button":
            self.items.append(El("button", name=text))
        else:
            self.items.append(El("text", text=text))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
            return
        if tag in HEADINGS or tag in ("a", "button") or tag in BLOCKS or tag == "title":
            self._flush()
        if tag == "a":
            self._href = dict(attrs).get("href")
        self._tag.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self.title = " ".join(" ".join(self._text).split())[:TEXT_MAX]
            self._text = []
        elif tag in HEADINGS:
            self._flush("heading")
        elif tag == "a":
            self._flush("link")
            self._href = None
        elif tag == "button":
            self._flush("button")
        elif tag in BLOCKS:
            self._flush()
        if self._tag and self._tag[-1] == tag:
            self._tag.pop()

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._text.append(data)


def page_of(html: str, url: str, status: int) -> El:
    p = _Outline()
    p.feed(html)
    p.close()
    p._flush()
    root = El("document", name=p.title or url)
    root.add(*p.items[:400])
    if status >= 400 and not p.items:
        root.add(El("text", text=f"HTTP {status}"))
    return root


def attach(driver: Any, proxy_host: str, proxy_port: int) -> None:
    """Make a dev tab read pages through an environment's tunnel (agentos.dev.lanes.DevLaneManager)."""

    async def fetch(url: str) -> tuple[int, str, El]:
        try:
            status, final, body = await socks_fetch(url, proxy_host, proxy_port)
        except FetchError as e:
            return 502, url, El("document", name="Not found", children=[El("text", text=str(e))])
        return status, final, page_of(body, final, status)

    driver.fetch = fetch
