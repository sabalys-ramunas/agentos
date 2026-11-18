"""An environment with network "limited" and a bound secret, on a real machine (the docker provider's
container, deploy/environments). Skipped unless docker answers and the image is there.

What it proves, for real: the machine is closed from outside it (no connection out but loopback, root
included); through the node's proxy it reaches a registry (registry.npmjs.org: this test reaches the
internet) and is refused example.com in plain words, which environment() then lists; the app's secret
is a placeholder on the machine, and an HTTPS server on this computer (with a CA of its own, trusted
by the node's swap for this test) gets the real value, through the proxy and through the machine's
relay alike."""

from __future__ import annotations

import asyncio
import contextlib
import ssl

import h11
import pytest
from test_environments_docker import _docker_ready, containers, node  # noqa: F401 - the fixture

from agentos.environments import appsecrets
from agentos.environments.swap import EnvCA

pytestmark = [pytest.mark.skipif(not _docker_ready(), reason="needs docker with the agentos-env image"),
              pytest.mark.network]

BOUND = "api.example.com"
VALUE = "sk_test_real-value-only-on-the-node-42"


async def _upstream(tmp_path) -> tuple[asyncio.AbstractServer, int, list[dict], EnvCA]:
    """An HTTPS server on this computer for BOUND, with a CA of its own: it keeps what it was sent."""
    ca = EnvCA.make("upstream", workdir=tmp_path)
    got: list[dict] = []

    async def answer(reader, writer):
        conn = h11.Connection(h11.SERVER)
        try:
            while True:
                ev = conn.next_event()
                if ev is h11.NEED_DATA:
                    conn.receive_data(await reader.read(65536))
                    continue
                if isinstance(ev, h11.Request):
                    got.append({"target": ev.target.decode(), "headers": {k.decode(): v.decode() for k, v in ev.headers}})
                elif isinstance(ev, h11.EndOfMessage):
                    writer.write(conn.send(h11.Response(status_code=200, headers=[("content-length", "3")]))
                                 + conn.send(h11.Data(data=b"ok\n")) + conn.send(h11.EndOfMessage()))
                    await writer.drain()
                    conn.start_next_cycle()
                elif isinstance(ev, h11.ConnectionClosed):
                    return
        except (OSError, h11.ProtocolError):  # a client that went away
            return
        finally:
            writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0, ssl=ca.leaf(BOUND))
    return server, server.sockets[0].getsockname()[1], got, ca


async def test_a_limited_machine_with_a_bound_secret(node, tmp_path):  # noqa: F811
    mgr, envs, grant = node["mgr"], node["envs"], node["grant"]
    await appsecrets.add(node["rt"].get("vault"), "SHOP_API_KEY", VALUE, [BOUND])
    server, port, got, up_ca = await _upstream(tmp_path)
    eid = mgr.planned_id("session:s1", "Closed shop")
    plan = mgr.check_network_plan(mgr.network_for_open("limited", None), await mgr.plan(eid, size=None, hours=1))
    env = await mgr.create(eid, plan, binding="session:s1", title="Closed shop", grant_id=grant.id,
                           grant_root=grant.root_id, agent="agent-1", client=None, repo=None, ref=None)
    try:
        env = await mgr.wait_ready(env.id, 150)
        assert env.state == "ready", env.error
        assert env.network == "limited" and env.secrets == ["SHOP_API_KEY"]
        swap = mgr._nets[env.id].swap  # for this test the swap trusts the upstream's CA and reaches loopback
        swap.upstream_ssl = ssl.create_default_context(cadata=up_ca.cert_pem.decode())

        async def here(host, p):
            return ["127.0.0.1"]

        swap.resolve_addrs, swap.upstream_port = here, port
        lane = env.terminal_lane

        async def sh(cmd: str) -> str:
            res = await envs.exec(lane, cmd, timeout_s=60)
            return (res.stdout or "") + (res.stderr or "")

        # closed outside the machine: no way out but the node's proxy, root included
        direct = await sh("curl -sS -m 8 --noproxy '*' -o /dev/null -w '%{http_code}' https://registry.npmjs.org/; "
                          "echo exit=$?")
        assert "exit=0" not in direct, direct
        assert "exit=0" not in await sh("iptables -F OUTPUT 2>&1; echo exit=$?")
        # through the proxy: the registry yes, example.com no, in words
        assert (await sh("curl -s -m 30 -o /dev/null -w '%{http_code}' https://registry.npmjs.org/")).strip() == "200"
        refused = await sh("curl -s -m 10 http://example.com/; echo; curl -sS -m 10 https://example.com/ 2>&1; true")
        assert "example.com is not on this environment's network list" in refused and "403" in refused
        # Node's fetch takes the proxy too (NODE_USE_ENV_PROXY): the 403, not ENOTFOUND; Python prints the words
        fetched = await sh("node -e 'fetch(\"https://example.com/\").then(r => console.log(r.status), e => { "
                           "for (let c = e; c; c = c.cause) console.log(c.code, c.message) })' 2>&1")
        assert "Proxy response (403)" in fetched and "ENOTFOUND" not in fetched, fetched
        got_https = await sh("node -e 'require(\"https\").get(\"https://example.com/\").on(\"error\", e => "
                             "console.log(e.message))' 2>&1")
        assert "403 Forbidden: example.com is not on this environment's network list" in got_https, got_https
        py = await sh("python3 -c 'import urllib.request as u; u.urlopen(\"https://example.com/\", timeout=10)' 2>&1")
        assert "403" in py and "agentos environments allow --host example.com" in py, py
        view = (await mgr.detail(mgr.get(env.id)))["network"]
        assert view["policy"] == "limited" and view["blocked"][0]["host"] == "example.com"
        assert view["blocked"][0]["count"] >= 2 and view["secrets"] == ["SHOP_API_KEY"]

        # the browser on the node: the machine's localhost through its tunnel, every other host through the list
        from agentos.dev.proxied import FetchError, socks_fetch

        await envs.job_start(lane, "mkdir -p /srv/t && echo '<h1>on the vm</h1>' > /srv/t/index.html && cd /srv/t "
                                   "&& exec python3 -m http.server 3000 --bind 127.0.0.1")
        proxy = mgr.proxy_for(mgr.get(env.id))
        assert proxy.port == mgr._nets[env.id].socks.port  # the split proxy, not the machine's own SOCKS
        body = ""
        for _ in range(40):
            with contextlib.suppress(FetchError):
                status, _u, body = await socks_fetch("http://localhost:3000/", proxy.host, proxy.port)
                if status == 200:
                    break
            await asyncio.sleep(0.25)
        assert "on the vm" in body
        with pytest.raises(FetchError):
            await socks_fetch("http://example.org/", proxy.host, proxy.port)
        assert "example.org" in {b["host"] for b in mgr.network_view(mgr.get(env.id))["blocked"]}

        # the secret: a placeholder here, the value at the upstream (through the proxy, then the relay)
        seen = await sh("echo $SHOP_API_KEY")
        assert seen.strip().startswith("agentos-secret-") and VALUE not in seen
        placeholder = seen.strip()
        one = await sh(f'curl -sS -m 20 -H "Authorization: Bearer $SHOP_API_KEY" '
                       f'"https://{BOUND}/v1/charges?key=$SHOP_API_KEY" -w " %{{http_code}}"')
        assert one.strip().endswith("200"), one
        two = await sh(f"curl -sS -m 20 --noproxy '*' -u \"$SHOP_API_KEY:\" https://{BOUND}/v1/balance "
                       "-w ' %{http_code}'")
        assert two.strip().endswith("200"), two
        assert len(got) == 2, got
        assert got[0]["headers"]["authorization"] == f"Bearer {VALUE}" and got[0]["target"].endswith(f"key={VALUE}")
        import base64

        assert base64.b64decode(got[1]["headers"]["authorization"].split()[1]).decode() == f"{VALUE}:"
        everywhere = await sh("cat /etc/environment /etc/hosts; env; grep -r . /usr/local/lib/agentos-env 2>/dev/null "
                              "| head -50")
        assert VALUE not in everywhere and placeholder in everywhere
        # a placeholder sent to another host goes out as it is, and is reported
        await sh("curl -s -m 10 -H \"X-Key: $SHOP_API_KEY\" http://registry.npmjs.org/ -o /dev/null; true")
        hosts = {b["host"]: b for b in mgr.network_view(mgr.get(env.id))["blocked"]}
        assert "placeholder" in hosts["registry.npmjs.org"]["why"]
    finally:
        await mgr.close(env.id, discard=True, reason="test end")
        server.close()
    assert not (mgr.home / "environments-net" / env.id).exists()
    assert await asyncio.to_thread(containers, node["node_name"]) == []
