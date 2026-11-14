# ruff: noqa: F811  (pytest fixtures imported from test_env_node_support)
import asyncio
import base64

import pytest
from test_env_node_support import mgr, needs_node  # noqa: F401

from agentos.core.config import NodeConfig, save_config, save_proxies
from agentos.core.models import ProxyConfig

pytestmark = [pytest.mark.node, needs_node]

DIRECT_TCP = ("python3 -c 'import socket; s = socket.socket(); s.settimeout(4); "
              "s.connect((\"1.1.1.1\", 443)); print(\"connected\")' 2>&1 | tail -1")
UPSTREAM_PORT = 7451


async def test_network_off_has_no_route_at_all(mgr):
    mgr.create("net/off")
    r = await mgr.exec("net/off", DIRECT_TCP)
    assert "Network is unreachable" in r.stdout
    r = await mgr.exec("net/off", "curl -sS -m 5 https://example.com -o /dev/null 2>&1; echo rc=$?")
    assert "rc=0" not in r.stdout
    r = await mgr.exec("net/off", "ip -br link 2>/dev/null || cat /proc/net/dev")
    assert "eth" not in r.stdout and "ens" not in r.stdout


async def test_isolated_egress_only_through_the_allowlisting_forwarder(mgr):
    mgr.create("net/iso", overrides={"network": "isolated", "egress_allowlist": ["example.com"]})
    r = await mgr.exec("net/iso", "curl -sS -m 20 -o /dev/null -w '%{http_code}' https://example.com", timeout_s=30)
    assert r.stdout == "200", r.stderr
    r = await mgr.exec("net/iso", "curl -sS -m 20 https://www.iana.org -o /dev/null 2>&1; echo rc=$?")
    assert "403" in r.stdout and "rc=0" not in r.stdout
    r = await mgr.exec("net/iso", "curl -sS -m 20 http://www.iana.org/ 2>&1 | head -3")
    assert "not on this lane's allowlist" in r.stdout
    r = await mgr.exec("net/iso", "curl -sS -m 5 --noproxy '*' https://example.com -o /dev/null 2>&1; echo rc=$?")
    assert "rc=0" not in r.stdout                   # direct, not through the proxy: no route
    r = await mgr.exec("net/iso", DIRECT_TCP)
    assert "Network is unreachable" in r.stdout
    r = await mgr.exec("net/iso", "python3 -c 'import socket; print(socket.getaddrinfo(\"example.com\", 443))' "
                                  "2>&1 | tail -1")
    assert "gaierror" in r.stdout or "Errno" in r.stdout     # no DNS inside the lane either
    stats = mgr.info("net/iso").egress
    assert stats.allowed >= 1 and stats.denied >= 2 and stats.last_denied.startswith("www.iana.org")


async def test_private_addresses_are_unreachable_through_the_forwarder(mgr):
    mgr.create("net/open", overrides={"network": "isolated"})
    for target in ("127.0.0.1:22", "127.0.0.1:443", "10.0.0.1:443", "169.254.169.254:80"):
        r = await mgr.exec("net/open", f"curl -sS -m 5 -p -x http://127.0.0.1:3128 https://{target} -o /dev/null "
                                       "2>&1; echo rc=$?")
        assert "rc=0" not in r.stdout, target


async def test_chained_upstream_proxy_keeps_credentials_on_the_host(mgr):
    seen: list[str] = []

    async def upstream(reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        seen.append(head)
        target = head.split()[1]
        host, port = target.rsplit(":", 1)
        try:
            ur, uw = await asyncio.open_connection(host, int(port))
        except OSError:
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
        await writer.drain()

        async def pipe(a, b):
            try:
                while data := await a.read(65536):
                    b.write(data)
                    await b.drain()
            finally:
                b.close()
        await asyncio.gather(pipe(reader, uw), pipe(ur, writer), return_exceptions=True)

    server = await asyncio.start_server(upstream, "127.0.0.1", UPSTREAM_PORT)
    try:
        cfg = NodeConfig(home=mgr.paths.home)
        save_config(cfg)
        save_proxies(cfg, {"up": ProxyConfig(label="up", host="127.0.0.1", port=UPSTREAM_PORT,
                                            username="lane-user", password="sekret-pw-91")})
        mgr.create("net/chain", overrides={"network": "isolated", "egress_allowlist": ["example.com"]},
                   exit_proxy="up")
        r = await mgr.exec("net/chain", "curl -sS -m 20 -o /dev/null -w '%{http_code}' https://example.com",
                           timeout_s=30)
        assert r.stdout == "200", r.stderr
        token = base64.b64encode(b"lane-user:sekret-pw-91").decode()
        assert seen and seen[0].startswith("CONNECT example.com:443") and token in seen[0]
        r = await mgr.exec("net/chain", "env; cat /proc/1/environ 2>/dev/null | tr '\\0' '\\n'")
        assert "sekret" not in r.stdout and token not in r.stdout
    finally:
        server.close()
