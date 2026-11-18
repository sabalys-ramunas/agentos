"""The node's TLS swap for app secrets (agentos.environments.swap), end to end in this process: a test
HTTPS upstream with its own CA, the swap in front of it, and a client that trusts only the
environment's CA. The placeholder becomes the value for the bound host only; keep-alive, chunked
bodies and Basic credentials work; no value reaches a log."""

from __future__ import annotations

import asyncio
import base64
import logging
import ssl

import h11
import pytest

from agentos.environments.swap import Binding, EnvCA, SwapProxy, swap_request

HOST = "api.example.test"
OTHER = "other.example.test"
VALUE = "sk_live_the-real-value-123"
PH = "agentos-secret-0123456789abcdef0123456789abcdef"


class Upstream:
    """An HTTPS server on 127.0.0.1 with a CA of its own: it keeps every request it gets."""

    def __init__(self, tmp_path):
        self.ca = EnvCA.make("upstream", workdir=tmp_path)
        self.got: list[tuple[bytes, list[tuple[bytes, bytes]], bytes]] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self):
        ctx = self.ca.leaf(HOST)
        ctx.sni_callback = lambda sslobj, name, _c: setattr(sslobj, "context", self.ca.leaf(name)) if name else None
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0, ssl=ctx)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        conn = h11.Connection(h11.SERVER)
        try:
            while True:
                ev = await _next(conn, reader)
                if not isinstance(ev, h11.Request):
                    return
                body = b""
                while True:
                    e = await _next(conn, reader)
                    if isinstance(e, h11.Data):
                        body += e.data
                    elif isinstance(e, h11.EndOfMessage):
                        break
                self.got.append((ev.target, list(ev.headers), body))
                out = b"ok " + str(len(self.got)).encode()
                writer.write(conn.send(h11.Response(status_code=200, headers=[("content-length", str(len(out)))])))
                writer.write(conn.send(h11.Data(data=out)) + conn.send(h11.EndOfMessage()))
                await writer.drain()
                conn.start_next_cycle()
        except Exception:  # noqa: BLE001 - the test's server: a dropped client ends it
            pass
        finally:
            writer.close()

    def trust(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cadata=self.ca.cert_pem.decode())
        return ctx


async def _next(conn, reader):
    while True:
        ev = conn.next_event()
        if ev is not h11.NEED_DATA:
            return ev
        conn.receive_data(await reader.read(65536))


@pytest.fixture
async def rig(tmp_path):
    up = Upstream(tmp_path)
    await up.start()
    env_ca = EnvCA.make("e1", workdir=tmp_path)
    events: list[tuple[str, str, str]] = []
    asked: list[tuple[str, str]] = []

    async def resolve(binding, host):
        asked.append((binding.name, host))
        return VALUE

    async def addrs(host, port):
        return ["127.0.0.1"]

    proxy = SwapProxy("e1", env_ca, [Binding("STRIPE_KEY", (HOST,), PH)], resolve,
                      on_event=lambda *e: events.append(e), upstream_ssl=up.trust(), resolve_addrs=addrs,
                      upstream_port=up.port)
    relay = await asyncio.start_server(proxy.handle_relay, "127.0.0.1", 0)
    port = relay.sockets[0].getsockname()[1]
    yield {"up": up, "ca": env_ca, "events": events, "asked": asked, "proxy": proxy, "port": port}
    relay.close()
    up.server.close()
    await proxy.close()


async def _connect(rig, server_name=HOST):
    ctx = ssl.create_default_context(cadata=rig["ca"].cert_pem.decode())
    ctx.set_alpn_protocols(["http/1.1"])
    return await asyncio.open_connection("127.0.0.1", rig["port"], ssl=ctx, server_hostname=server_name)


async def _ask(reader, writer, raw: bytes) -> bytes:
    writer.write(raw)
    await writer.drain()
    conn = h11.Connection(h11.CLIENT)
    conn.send(h11.Request(method="GET", target="/", headers=[("host", "x")]))  # only to set its state
    conn.send(h11.EndOfMessage())
    body = b""
    while True:
        ev = conn.next_event()
        if ev is h11.NEED_DATA:
            conn.receive_data(await asyncio.wait_for(reader.read(65536), 5))
            continue
        if isinstance(ev, h11.Data):
            body += ev.data
        if isinstance(ev, h11.EndOfMessage | h11.ConnectionClosed):
            return body


async def test_the_placeholder_becomes_the_value_for_the_bound_host_on_every_request_of_a_connection(rig, caplog):
    caplog.set_level(logging.DEBUG)
    r, w = await _connect(rig)
    one = await _ask(r, w, f"GET /v1/charges?key={PH}&x=1 HTTP/1.1\r\nHost: {HOST}\r\n"
                           f"Authorization: Bearer {PH}\r\n\r\n".encode())
    basic = base64.b64encode(f"{PH}:".encode()).decode()
    two = await _ask(r, w, f"POST /v1/charges HTTP/1.1\r\nHost: {HOST}\r\nAuthorization: Basic {basic}\r\n"
                           "Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n".encode())
    w.close()
    assert (one, two) == (b"ok 1", b"ok 2")  # keep-alive: both on one connection
    (t1, h1, _), (t2, h2, b2) = rig["up"].got
    assert t1 == f"/v1/charges?key={VALUE}&x=1".encode()
    assert dict(h1)[b"authorization"] == f"Bearer {VALUE}".encode()
    assert base64.b64decode(dict(h2)[b"authorization"].split()[1]) == f"{VALUE}:".encode()
    assert b2 == b"hello world"  # the chunked body arrived whole, and bodies are never touched
    assert [e[0] for e in rig["events"]] == ["swapped", "swapped"]
    assert VALUE not in caplog.text and all(VALUE not in str(e) for e in rig["events"])


async def test_a_host_header_that_is_not_the_server_name_gets_no_swap_and_is_reported(rig):
    r, w = await _connect(rig)
    await _ask(r, w, f"GET / HTTP/1.1\r\nHost: {OTHER}\r\nX-Key: {PH}\r\n\r\n".encode())
    w.close()
    (_t, headers, _b), = rig["up"].got
    assert dict(headers)[b"x-key"] == PH.encode() and rig["asked"] == []
    assert rig["events"] == [("placeholder_elsewhere", OTHER, "STRIPE_KEY")]


async def test_a_server_name_that_is_not_bound_gets_no_certificate(rig):
    with pytest.raises((ssl.SSLError, ConnectionError)):
        r, w = await _connect(rig, server_name=OTHER)
        await _ask(r, w, f"GET / HTTP/1.1\r\nHost: {OTHER}\r\n\r\n".encode())
    assert rig["up"].got == []


async def test_a_request_without_a_placeholder_never_reads_the_vault():
    req = h11.Request(method="GET", target="/", headers=[("host", HOST)])
    out = await swap_request(req, sni=HOST, bindings=[Binding("K", (HOST,), PH)], resolve=None)  # type: ignore[arg-type]
    assert out.names == [] and out.elsewhere == [] and out.request is req


async def test_a_value_in_the_query_is_percent_encoded():
    async def resolve(b, h):
        return "a b&c"

    req = h11.Request(method="GET", target=f"/x?k={PH}", headers=[("host", HOST)])
    out = await swap_request(req, sni=HOST, bindings=[Binding("K", (HOST,), PH)], resolve=resolve)
    assert out.request.target == b"/x?k=a%20b%26c"
