import asyncio
import base64
import socket

import pytest

from agentos.env.netproxy import EgressPolicy, Forwarder, Upstream, entry_matches, split_hostport


def test_entry_matching():
    assert entry_matches("example.com", "example.com", 443)
    assert not entry_matches("example.com", "www.example.com", 443)
    assert entry_matches("*.example.com", "www.example.com", 443)
    assert not entry_matches("*.example.com", "example.com", 443)
    assert entry_matches(".example.com", "example.com", 80) and entry_matches(".example.com", "a.example.com", 80)
    assert not entry_matches("example.com", "example.com", 22)
    assert entry_matches("example.com:8443", "example.com", 8443)
    assert not entry_matches("example.com:8443", "example.com", 443)
    assert not entry_matches("example.com", "example.com.evil.net", 443)
    assert split_hostport("[::1]:8080") == ("::1", 8080)
    assert split_hostport("Example.com.") == ("example.com", 443)


def test_policy_decisions():
    p = EgressPolicy(["example.com"], "allow")
    assert p.check("example.com", 443)[0]
    ok, why = p.check("other.org", 443)
    assert not ok and "allowlist" in why
    assert not EgressPolicy([], "deny").check("example.com", 443)[0]
    assert EgressPolicy([], "allow").check("example.com", 443)[0]
    assert not EgressPolicy([], "allow").check("example.com", 25)[0]
    assert not p.check("exa mple.com", 443)[0]


async def _server(handler):
    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def _forwarder(policy, upstream=None):
    fwd = Forwarder("ws/x", policy, upstream=upstream, connect_timeout=3)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    await fwd.serve(s)
    return fwd, port


async def _request(port, data: bytes, read_all=True) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(data)
    await w.drain()
    out = await asyncio.wait_for(r.read() if read_all else r.readuntil(b"\r\n\r\n"), 5)
    w.close()
    return out


@pytest.fixture
async def echo():
    async def handle(r, w):
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
        w.close()
    srv, port = await _server(handle)
    yield port
    srv.close()


async def test_connect_tunnel_to_an_explicitly_allowlisted_address(echo):
    fwd, port = await _forwarder(EgressPolicy([f"127.0.0.1:{echo}"], "deny"))
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"CONNECT 127.0.0.1:{echo} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert b"200" in await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
    w.write(b"ping")
    assert await asyncio.wait_for(r.readexactly(4), 5) == b"ping"
    w.close()
    assert fwd.stats.allowed == 1 and fwd.stats.bytes_out >= 4
    await fwd.close()


async def test_not_allowlisted_is_refused_with_403():
    fwd, port = await _forwarder(EgressPolicy(["example.com"], "deny"))
    out = await _request(port, b"CONNECT other.org:443 HTTP/1.1\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 403") and b"X-Agentos-Egress: denied" in out and b"allowlist" in out
    assert fwd.stats.denied == 1 and fwd.stats.last_denied == "other.org:443"
    await fwd.close()


async def test_private_and_loopback_destinations_are_refused():
    fwd, port = await _forwarder(EgressPolicy([], "allow"))
    out = await _request(port, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 403") and b"private or local" in out
    out = await _request(port, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 403")
    await fwd.close()


async def test_plain_http_is_one_request_per_connection():
    seen = []

    async def origin(r, w):
        data = await r.read(65536)
        seen.append(data)
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await w.drain()
        w.close()
    srv, oport = await _server(origin)
    fwd, port = await _forwarder(EgressPolicy([f"127.0.0.1:{oport}"], "deny"))
    req = (f"GET http://127.0.0.1:{oport}/a?b=1 HTTP/1.1\r\nHost: 127.0.0.1:{oport}\r\nProxy-Connection: keep-alive\r\n\r\n"
           f"GET http://evil.example/x HTTP/1.1\r\nHost: evil.example\r\n\r\n").encode()
    out = await _request(port, req)
    assert out.endswith(b"ok")
    assert len(seen) == 1
    head = seen[0].decode()
    assert head.startswith("GET /a?b=1 HTTP/1.1") and "Connection: close" in head
    assert "Proxy-Connection" not in head and "evil.example" not in head
    out = await _request(port, f"POST http://127.0.0.1:{oport}/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n".encode())
    assert out.startswith(b"HTTP/1.1 411")
    out = await _request(port, b"GET /relative HTTP/1.1\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 400")
    await fwd.close()
    srv.close()


async def test_chains_to_an_upstream_proxy_with_credentials(echo):
    seen = []

    async def upstream(r, w):
        head = (await r.readuntil(b"\r\n\r\n")).decode()
        seen.append(head)
        if head.startswith("CONNECT"):
            er, ew = await asyncio.open_connection("127.0.0.1", echo)
            w.write(b"HTTP/1.1 200 OK\r\n\r\n")
            await w.drain()

            async def pipe(a, b):
                while data := await a.read(65536):
                    b.write(data)
                    await b.drain()
                b.close()
            await asyncio.gather(pipe(r, ew), pipe(er, w))
        else:
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nvia")
            await w.drain()
            w.close()
    srv, uport = await _server(upstream)
    fwd, port = await _forwarder(EgressPolicy(["example.test"], "deny"),
                                 upstream=Upstream("127.0.0.1", uport, "user", "s3cret"))
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(b"CONNECT example.test:443 HTTP/1.1\r\n\r\n")
    assert b"200" in await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
    w.write(b"hello")
    assert await asyncio.wait_for(r.readexactly(5), 5) == b"hello"
    w.close()
    token = base64.b64encode(b"user:s3cret").decode()
    assert seen[0].startswith("CONNECT example.test:443") and f"Proxy-Authorization: Basic {token}" in seen[0]
    out = await _request(port, b"GET http://example.test/p HTTP/1.1\r\nHost: example.test\r\n\r\n")
    assert out.endswith(b"via")
    assert seen[1].startswith("GET http://example.test/p HTTP/1.1") and token in seen[1]
    out = await _request(port, b"CONNECT nope.test:443 HTTP/1.1\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 403") and len(seen) == 2
    await fwd.close()
    srv.close()
