"""Secrets for the app without the secret on the VM: the node's TLS-terminating swap.

The owner binds a vault API key to environments (openajar.environments.appsecrets). On the VM its
variable holds a placeholder, `agentos-secret-<random>`. A request the app makes to the bound host
reaches this proxy on the node (through the allowlist proxy's CONNECT, or the machine's relay on
127.0.10.1:443 that /etc/hosts sends the bound host to), which:

1. ends the TLS connection with a certificate for the bound host, signed by the environment's own
   CA (`EnvCA`: made on the node, its key never leaves the node and is deleted with the
   environment; the VM trusts its certificate). Only a bound host gets a certificate;
2. reads each request on the connection with h11 (keep-alive, chunked bodies, several requests);
3. when the TLS server name and the request's Host header are both the bound host, puts the real
   value in place of the placeholder in the headers (a Basic credential's user or password too) and
   in the URL's query, never in a body. A placeholder sent anywhere else is left as it is, and
   reported (`on_event`);
4. sends it on to the real host over a TLS connection of its own, with full certificate checks,
   to an address the node resolved itself (public addresses only).

HTTP/1.1 only (ALPN http/1.1). Responses go back as they come: an API that echoes the key back in a
response hands it to the machine. Nothing here logs a header, a value or a URL's query.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import ipaddress
import logging
import os
import socket
import ssl
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import h11

log = logging.getLogger("openajar.environments.swap")

PLACEHOLDER_PREFIX = b"agentos-secret-"
READ_CHUNK = 65536
IDLE_S = 300.0
CONNECT_S = 15.0
CA_DAYS = 30
LEAF_DAYS = 7


# ---------------------------------------------------------------- the environment's CA

class EnvCA:
    """A CA of one environment, and the certificates it signs for the bound hosts (one per name,
    made when first asked for)."""

    def __init__(self, cert_pem: bytes, key_pem: bytes, *, workdir: Path | None = None):
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        self.cert_pem = cert_pem
        self._cert = x509.load_pem_x509_certificate(cert_pem)
        self._key = serialization.load_pem_private_key(key_pem, password=None)
        self._workdir = workdir
        self._leaves: dict[str, ssl.SSLContext] = {}

    @classmethod
    def make(cls, name: str, *, workdir: Path | None = None) -> EnvCA:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"openajar environment {name}"[:64])])
        now = dt.datetime.now(dt.UTC)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=CA_DAYS))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                             content_commitment=False, key_encipherment=False,
                                             data_encipherment=False, key_agreement=False, encipher_only=False,
                                             decipher_only=False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
        key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        return cls(cert.public_bytes(serialization.Encoding.PEM), key_pem, workdir=workdir)

    def key_pem(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self._key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption())

    def leaf(self, host: str) -> ssl.SSLContext:
        """A server context with a certificate for `host`, signed by this CA (made once per host)."""
        ctx = self._leaves.get(host)
        if ctx is None:
            ctx = self._leaves[host] = self._make_leaf(host)
        return ctx

    def _make_leaf(self, host: str) -> ssl.SSLContext:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.UTC)
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])]))
                .issuer_name(self._cert.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._cert.public_key()),
                               critical=False)
                .sign(self._key, hashes.SHA256()))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_alpn_protocols(["http/1.1"])
        # load_cert_chain reads files: a 0600 file in a folder of the node's own, removed at once
        fd, path = tempfile.mkstemp(prefix="leaf-", suffix=".pem", dir=self._workdir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(cert.public_bytes(serialization.Encoding.PEM) + self.cert_pem)
                f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
            ctx.load_cert_chain(path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)
        ctx.openajar_host = host  # type: ignore[attr-defined]  (read back after the handshake)
        return ctx


# ---------------------------------------------------------------- bindings

@dataclass(frozen=True)
class Binding:
    """One secret bound for this environment: its variable's name, the hosts it may go to, and the
    placeholder the VM holds instead of it."""
    name: str
    hosts: tuple[str, ...]
    placeholder: str
    ref: str = ""            # the vault entry


Resolver = Callable[[Binding, str], Awaitable[Any]]   # (binding, host) -> the value (a vault Secret or str)


def _reveal(value: Any) -> bytes:
    raw = value.reveal() if hasattr(value, "reveal") else str(value)
    return raw.encode("utf-8")


def _host_only(value: bytes) -> str:
    text = value.decode("latin-1").strip().lower().rstrip(".")
    if text.startswith("["):
        return text[1:].split("]", 1)[0]
    return text.rsplit(":", 1)[0] if text.count(":") == 1 else text


def _origin_form(target: bytes, sni: str) -> bytes | None:
    """The target as `/path?query` when it names no host or the TLS server's own, else None. A server takes
    an absolute-form target's authority over the Host header (RFC 9112 3.2.2), so a request naming another
    host (a shared front routes it there) never gets a value; one naming this host is sent in origin-form."""
    if target.startswith(b"/"):
        return target
    scheme, sep, rest = target.partition(b"://")
    if not sep or scheme.lower() not in (b"http", b"https"):
        return None  # authority-form or asterisk-form: nothing to swap into
    ends = [i for i in (rest.find(b"/"), rest.find(b"?")) if i >= 0]
    end = min(ends) if ends else len(rest)
    authority, tail = rest[:end], rest[end:]
    if not authority or b"@" in authority or _host_only(authority) != sni:
        return None
    return tail if tail.startswith(b"/") else b"/" + tail


def _basic(value: bytes) -> bytes | None:
    """The decoded credential of `Basic <base64 user:password>`, else None."""
    scheme, _, token = value.partition(b" ")
    if scheme.lower() != b"basic" or not token.strip():
        return None
    try:
        return base64.b64decode(token.strip(), validate=True)
    except ValueError:
        return None


def _swap_header(name: bytes, value: bytes, olds: list[tuple[bytes, bytes]]) -> bytes:
    for old, real in olds:
        value = value.replace(old, real)
    raw = _basic(value) if name.lower() in (b"authorization", b"proxy-authorization") else None
    if raw is not None:
        new = raw
        for old, real in olds:
            new = new.replace(old, real)
        if new != raw:
            value = value.partition(b" ")[0] + b" " + base64.b64encode(new)
    return value


def _visible(target: bytes, headers: list[tuple[bytes, bytes]]) -> bytes:
    """What a placeholder may hide in: the target, the header values, and a Basic credential decoded."""
    parts = [target]
    for k, v in headers:
        parts.append(v)
        if k.lower() in (b"authorization", b"proxy-authorization"):
            parts.append(_basic(v) or b"")
    return b"\n".join(parts)


@dataclass
class Swapped:
    request: h11.Request
    names: list[str] = field(default_factory=list)       # the secrets put in (names only)
    elsewhere: list[str] = field(default_factory=list)   # placeholders seen where they are not swapped


async def swap_request(req: h11.Request, *, sni: str, bindings: list[Binding], resolve: Resolver) -> Swapped:
    """The request with each bound placeholder swapped for its value, when the request's Host (and an
    absolute-form target's host) is the TLS server name and the binding allows that host; else as it came.
    Headers and the URL's query only."""
    out = Swapped(req)
    raw_headers = list(req.headers.raw_items())
    seen = _visible(req.target, raw_headers)
    if PLACEHOLDER_PREFIX not in seen:
        return out
    host_hdr = next((v for k, v in req.headers if k == b"host"), b"")
    origin = _origin_form(req.target, sni)
    same = bool(host_hdr) and _host_only(host_hdr) == sni and origin is not None
    olds: list[tuple[bytes, bytes]] = []
    for b in bindings:
        if same and sni in b.hosts and b.placeholder.encode() in seen:
            olds.append((b.placeholder.encode(), _reveal(await resolve(b, sni))))
            out.names.append(b.name)
    headers = [(k, _swap_header(k, v, olds)) for k, v in raw_headers]
    path, q, query = (origin if same and origin is not None else req.target).partition(b"?")
    for old, real in olds:
        query = query.replace(old, quote(real, safe="-._~").encode())
    target = path + q + query
    left = _visible(target, headers)
    out.elsewhere = [b.name for b in bindings if b.placeholder.encode() in left]
    if PLACEHOLDER_PREFIX in left and not out.elsewhere:
        out.elsewhere.append("an unknown placeholder")
    out.request = h11.Request(method=req.method, target=target, headers=headers, http_version=req.http_version)
    return out


# ---------------------------------------------------------------- the proxy

Opener = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def public_addrs(host: str, port: int) -> list[str]:
    """The host's addresses, resolved on the node, public ones only."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), CONNECT_S)
    addrs = list(dict.fromkeys(str(i[4][0]) for i in infos))
    return [a for a in addrs if ipaddress.ip_address(a).is_global]


class SwapProxy:
    """The swap of one environment. `serve_tls(reader, writer, expect=host)` takes a client connection
    that is about to start TLS (after a CONNECT, or from the machine's relay) and serves it."""

    def __init__(self, env_id: str, ca: EnvCA, bindings: list[Binding], resolve: Resolver, *,
                 on_event: Callable[[str, str, str], None] | None = None,
                 upstream_ssl: ssl.SSLContext | None = None, resolve_addrs: Callable[..., Awaitable[list[str]]] = public_addrs,
                 upstream_port: int = 443):
        self.env_id = env_id
        self.ca = ca
        self.bindings = list(bindings)
        self.resolve = resolve
        self.on_event = on_event
        self.upstream_ssl = upstream_ssl
        self.resolve_addrs = resolve_addrs
        self.upstream_port = upstream_port
        self.swapped = 0
        self._tasks: set[asyncio.Task] = set()

    @property
    def hosts(self) -> set[str]:
        return {h for b in self.bindings for h in b.hosts}

    def _event(self, kind: str, host: str, detail: str) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, host, detail)
            except Exception:
                log.exception("environment %s: the swap's event hook failed", self.env_id)

    def _pick(self, sslobj: Any, name: str | None, _ctx: ssl.SSLContext) -> int | None:
        """The TLS server name picks the certificate, and only a bound host has one. No name: the context
        it started with (a CONNECT's host; from the relay, none, so the handshake fails)."""
        if name is None:
            return None
        name = name.lower().rstrip(".")
        if name not in self.hosts:
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        leaf = self.ca.leaf(name)
        leaf.sni_callback = self._pick
        sslobj.context = leaf
        return None

    def _base_context(self, expect: str | None) -> ssl.SSLContext:
        ctx = self.ca.leaf(expect) if expect else ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.sni_callback = self._pick
        return ctx

    async def handle_relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """A connection from the machine's relay (127.0.10.1:443 there): TLS from its first byte."""
        await self.serve_tls(reader, writer, expect=None)

    async def serve_tls(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                        expect: str | None) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            try:
                await asyncio.wait_for(writer.start_tls(self._base_context(expect)), CONNECT_S)
            except (ssl.SSLError, OSError, TimeoutError) as e:
                self._event("tls_refused", expect or "?", type(e).__name__)
                return
            sslobj = writer.get_extra_info("ssl_object")
            host = getattr(getattr(sslobj, "context", None), "openajar_host", None) or expect
            if not host or host not in self.hosts or (expect and host != expect):
                return  # a CONNECT to one host with the server name of another: neither is served
            await self._serve(reader, writer, host)
        except (ConnectionError, TimeoutError, asyncio.IncompleteReadError, ssl.SSLError, h11.ProtocolError):
            pass
        except asyncio.CancelledError:
            pass
        finally:
            if task is not None:
                self._tasks.discard(task)
            with contextlib.suppress(Exception):
                writer.close()

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()

    async def _open_upstream(self, host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        addrs = await self.resolve_addrs(host, self.upstream_port)
        if not addrs:
            raise PermissionError(f"{host} resolves only to private or local addresses")
        ctx = self.upstream_ssl or ssl.create_default_context()
        ctx.set_alpn_protocols(["http/1.1"])
        last: Exception | None = None
        for addr in addrs:
            try:
                return await asyncio.wait_for(asyncio.open_connection(addr, self.upstream_port, ssl=ctx,
                                                                      server_hostname=host), CONNECT_S)
            except (OSError, TimeoutError, ssl.SSLError) as e:
                last = e
        raise ConnectionError(f"cannot reach {host}: {type(last).__name__ if last else 'no address'}")

    @staticmethod
    async def _next(conn: h11.Connection, reader: asyncio.StreamReader) -> Any:
        while True:
            ev = conn.next_event()
            if ev is not h11.NEED_DATA:
                return ev
            data = await asyncio.wait_for(reader.read(READ_CHUNK), IDLE_S)
            conn.receive_data(data)

    async def _reply_error(self, writer: asyncio.StreamWriter, server: h11.Connection, status: int, text: str) -> None:
        body = f"openajar: {text}\n".encode()
        with contextlib.suppress(h11.LocalProtocolError):
            writer.write(server.send(h11.Response(status_code=status, headers=[
                ("content-type", "text/plain"), ("content-length", str(len(body))), ("connection", "close")])))
            writer.write(server.send(h11.Data(data=body)))
            writer.write(server.send(h11.EndOfMessage()))
            await writer.drain()

    async def _serve(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter, host: str) -> None:
        server = h11.Connection(h11.SERVER)
        client = h11.Connection(h11.CLIENT)
        up: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        try:
            while True:
                ev = await self._next(server, cr)
                if not isinstance(ev, h11.Request):
                    return  # ConnectionClosed, or the client is done
                try:
                    swapped = await swap_request(ev, sni=host, bindings=self.bindings, resolve=self.resolve)
                except Exception as e:  # noqa: BLE001 - the vault did not give it: this request fails, in words
                    why = getattr(e, "message", None) or type(e).__name__
                    await self._reply_error(cw, server, 502, f"the node could not read the secret for {host}: {why}")
                    return
                if swapped.names:
                    self.swapped += 1
                    self._event("swapped", host, ", ".join(swapped.names))
                if swapped.elsewhere:
                    self._event("placeholder_elsewhere", _host_only(next(
                        (v for k, v in ev.headers if k == b"host"), host.encode())), ", ".join(swapped.elsewhere))
                if up is None:
                    try:
                        up = await self._open_upstream(host)
                    except (PermissionError, ConnectionError, OSError) as e:
                        await self._reply_error(cw, server, 502, str(e))
                        return
                if not await self._exchange(server, client, cr, cw, up, swapped.request):
                    return
                if server.our_state is h11.MUST_CLOSE or client.our_state is h11.MUST_CLOSE:
                    return
                try:
                    server.start_next_cycle()
                    client.start_next_cycle()
                except h11.LocalProtocolError:
                    return
        finally:
            if up is not None:
                with contextlib.suppress(Exception):
                    up[1].close()

    async def _exchange(self, server: h11.Connection, client: h11.Connection, cr: asyncio.StreamReader,
                        cw: asyncio.StreamWriter, up: tuple[asyncio.StreamReader, asyncio.StreamWriter],
                        req: h11.Request) -> bool:
        """One request to the upstream and its response back. False when the connection must end."""
        ur, uw = up
        headers = [(k, v) for k, v in req.headers.raw_items() if k.lower() != b"expect"]
        if server.they_are_waiting_for_100_continue:
            cw.write(server.send(h11.InformationalResponse(status_code=100, headers=[])))
            await cw.drain()
        uw.write(client.send(h11.Request(method=req.method, target=req.target, headers=headers,
                                         http_version=req.http_version)))
        while True:  # the body, as it comes (h11 frames it either way: Content-Length or chunked)
            ev = await self._next(server, cr)
            if isinstance(ev, h11.Data):
                uw.write(client.send(h11.Data(data=ev.data)))
                await uw.drain()
            elif isinstance(ev, h11.EndOfMessage):
                uw.write(client.send(h11.EndOfMessage()))
                await uw.drain()
                break
            else:
                return False
        while True:
            ev = await self._next(client, ur)
            if isinstance(ev, h11.InformationalResponse):
                continue  # a 100 from the upstream: the body went already
            if isinstance(ev, h11.Response):
                cw.write(server.send(h11.Response(status_code=ev.status_code, headers=list(ev.headers.raw_items()),
                                                  reason=ev.reason, http_version=ev.http_version)))
            elif isinstance(ev, h11.Data):
                cw.write(server.send(h11.Data(data=ev.data)))
            elif isinstance(ev, h11.EndOfMessage):
                cw.write(server.send(h11.EndOfMessage()))
                await cw.drain()
                return True
            else:
                return False
            await cw.drain()
