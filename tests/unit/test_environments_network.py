"""An environment's network (agentos.environments.network, networking, envproxy) and the walls that hold
it outside the machine (agentos.vms.netwall, the Hetzner firewall, the docker entrypoint): an agent only
narrows it, limited reaches the preset, the proxy refuses in plain words and keeps what it refused, a
closed machine is rented closed, and the owner's secret bindings live in the vault with their hosts."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile

import httpx
import pytest
from test_environments import node  # noqa: F401 - the fixture
from test_vault_support import make_vault, private_home

from agentos.core.errors import AgentosError
from agentos.environments import appsecrets, network
from agentos.environments.envproxy import Blocked, EnvForwarder, EnvPolicy
from agentos.environments.models import Environment, EnvSettings
from agentos.environments.network import NetworkNotAllowed, allowlist, narrowed
from agentos.environments.pulls import _with_blocked
from agentos.core.models import utcnow
from agentos.vms import netwall
from agentos.vms.docker import DockerProvider, entrypoint_text, seed_tar
from agentos.vms.hetzner import HetznerProvider
from agentos.vms.provider import FakeProvider, ServerSpec

# ------------------------------------------------------------------ the levels


def test_an_agent_only_narrows_the_nodes_network():
    assert narrowed("full", None) == "full"
    assert narrowed("full", "limited") == "limited" and narrowed("full", "none") == "none"
    assert narrowed("limited", "none") == "none" and narrowed("limited", "limited") == "limited"
    for default, wider in (("limited", "full"), ("none", "limited"), ("none", "full")):
        with pytest.raises(NetworkNotAllowed) as e:
            narrowed(default, wider)
        assert e.value.code == "network_not_allowed"
        assert f"agentos environments allow --network {wider}" in e.value.message
    with pytest.raises(NetworkNotAllowed):
        narrowed("full", "open")


def test_limited_reaches_the_registries_the_repos_host_and_the_owners_hosts_and_nothing_else():
    allow = allowlist("limited", allow_hosts=[".example.org"], repo_host="gitlab.example.net",
                      secret_hosts=["api.stripe.com"])
    policy = EnvPolicy(allow)
    for host in ("registry.npmjs.org", "pypi.org", "files.pythonhosted.org", "crates.io", "proxy.golang.org",
                 "rubygems.org", "repo1.maven.org", "de.archive.ubuntu.com", "archive.ubuntu.com",
                 "registry-1.docker.io", "ghcr.io", "github.com", "codeload.github.com", "nodejs.org",
                 "gitlab.example.net", "api.stripe.com", "example.org", "cdn.example.org"):
        assert policy.check(host, 443)[0], host
    ok, why = policy.check("example.com", 443)
    assert not ok and why == ("example.com is not on this environment's network list. The owner can allow it: "
                              "agentos environments allow --host example.com")
    assert not policy.check("registry.npmjs.org", 22)[0]  # ports 80 and 443 only
    assert not policy.check("evil-npmjs.org", 443)[0]


def test_none_reaches_only_bound_hosts_and_full_lists_nothing():
    assert allowlist("none", allow_hosts=["x.example.org"], repo_host="github.com") == []
    assert not EnvPolicy([]).check("github.com", 443)[0]
    assert allowlist("none", secret_hosts=["api.stripe.com"]) == ["api.stripe.com"]
    assert allowlist("full", allow_hosts=["x.example.org"]) == []


def test_allow_hosts_are_checked_where_they_are_set():
    s = EnvSettings(network="limited", allow_hosts=["API.Example.com", "https://cdn.example.org/x", ".example.net",
                                                    "h.example.com:8443"])
    assert s.allow_hosts == ["api.example.com", "cdn.example.org", ".example.net", "h.example.com:8443"]
    with pytest.raises(ValueError):
        EnvSettings(allow_hosts=["not a host"])


def test_the_machines_script_sets_the_proxy_apt_docker_the_placeholders_and_the_ca():
    script = network.vm_script("limited", variables={"STRIPE_KEY": "agentos-secret-abc"}, ca_pem="CERT",
                               secret_hosts=["api.stripe.com"])
    assert "HTTPS_PROXY=\"http://127.0.0.1:3130\"" in script and "no_proxy=\"localhost,127.0.0.1,::1\"" in script
    assert "STRIPE_KEY=\"agentos-secret-abc\"" in script and "NODE_EXTRA_CA_CERTS" in script
    assert "Acquire::https::Proxy" in script and "docker.service.d/agentos-proxy.conf" in script
    assert "systemctl is-active --quiet docker" in script  # the daemon restarts only when it runs
    assert "127.0.10.1 api.stripe.com" in script and "update-ca-certificates" in script
    assert "sed -i" not in script  # /etc/hosts on docker is a bind mount: written in place
    full = network.vm_script("full", variables={}, ca_pem=None, secret_hosts=[])
    assert "PROXY" not in full.split("rm -f")[0] and "127.0.10.1" not in full


def test_a_pull_request_says_what_was_blocked():
    class Env:
        blocked = [{"host": "example.com", "count": 3, "last": "2026-11-18T10:00:00Z"}]

    body = _with_blocked(None, Env(), "Fixes the checkout.")
    assert body.startswith("Fixes the checkout.") and "### Blocked during this work" in body
    assert "`example.com` (3x)" in body and "agentos environments allow --host" in body
    Env.blocked = []
    assert _with_blocked(None, Env(), "x") == "x"


# ------------------------------------------------------------------ the proxy


async def _ask(port: int, raw: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(raw)
    await w.drain()
    got = await asyncio.wait_for(r.read(4096), 5)
    w.close()
    return got


async def test_the_proxy_refuses_in_plain_words_and_keeps_count_of_what_it_refused():
    changes: list[list[dict]] = []
    blocked = Blocked(clock=utcnow, on_change=changes.append)
    fwd = EnvForwarder("e1", EnvPolicy(allowlist("limited")), blocked)
    server = await asyncio.start_server(fwd._client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        out = await _ask(port, b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 403") and out.endswith(
            b"example.com is not on this environment's network list. The owner can allow it: "
            b"agentos environments allow --host example.com\n")
        out = await _ask(port, b"GET http://example.com/x HTTP/1.1\r\nHost: example.com\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 403") and b"agentos environments allow --host example.com" in out
        await _ask(port, b"CONNECT smtp.example.org:25 HTTP/1.1\r\n\r\n")
    finally:
        server.close()
    rows = {r["host"]: r for r in blocked.list()}
    assert rows["example.com"]["count"] == 2 and rows["example.com"]["last"].endswith("Z")
    assert "smtp.example.org:25" in rows and changes


async def test_a_connect_to_a_bound_host_goes_to_the_swap_not_a_blind_tunnel():
    handed: list[str] = []

    async def swap(reader, writer, *, expect):
        handed.append(expect)
        writer.write(b"swapped")
        await writer.drain()

    fwd = EnvForwarder("e1", EnvPolicy(["api.stripe.com"]), Blocked(clock=utcnow),
                       intercept=lambda h, p: swap if h == "api.stripe.com" and p == 443 else None)
    server = await asyncio.start_server(fwd._client, "127.0.0.1", 0)
    try:
        out = await _ask(server.sockets[0].getsockname()[1], b"CONNECT api.stripe.com:443 HTTP/1.1\r\n\r\n")
        await asyncio.sleep(0.05)
    finally:
        server.close()
    assert out.startswith(b"HTTP/1.1 200") and handed == ["api.stripe.com"]


# ------------------------------------------------------------------ the walls


def test_which_providers_can_close_a_machine():
    assert netwall.cannot_close(DockerProvider()) is None
    assert netwall.cannot_close(HetznerProvider("t")) is None
    assert "firewall = true" in netwall.cannot_close(HetznerProvider("t"), firewall_on=False)
    why = netwall.cannot_close(FakeProvider())
    assert why and "network full" in why
    assert netwall.labels("limited") == {"agentos-network": "limited"} and netwall.labels("full") == {}


async def test_hetzner_rents_a_closed_machine_behind_a_firewall_with_no_way_out():
    sent: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        sent.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json={"firewalls": []})
        return httpx.Response(201, json={"firewall": {"id": 42}})

    prov = HetznerProvider("t", transport=httpx.MockTransport(handler))

    async def ssh_from():
        return ["198.51.100.7/32"]

    fid = await netwall.firewall_for(prov, "limited", "agentos-n-h", {"agentos-node": "n"}, ssh_from)
    assert fid == "42"
    method, path, body = sent[-1]
    assert (method, path, body["name"]) == ("POST", "/v1/firewalls", "agentos-n-h-closed")
    rules = body["rules"]
    assert [r["direction"] for r in rules] == ["in", "in", "out"]
    (out,) = [r for r in rules if r["direction"] == "out"]
    # the one way out is ping to the node: any outbound rule makes Hetzner drop all other outbound traffic
    assert out["protocol"] == "icmp" and out["destination_ips"] == ["198.51.100.7/32"] and "port" not in out
    assert {r["protocol"] for r in rules if r["direction"] == "in"} == {"tcp", "icmp"}
    sent.clear()
    await netwall.firewall_for(prov, "full", "agentos-n-h", {}, ssh_from)
    assert sent[-1][2]["name"] == "agentos-n-h" and all(r["direction"] == "in" for r in sent[-1][2]["rules"])


async def test_a_provider_that_cannot_close_refuses_instead_of_renting_an_open_machine():
    async def ssh_from():
        return []

    with pytest.raises(AgentosError):
        await netwall.firewall_for(FakeProvider(), "none", "fw", {}, ssh_from)


def test_the_docker_entrypoint_closes_everything_but_loopback_dns_included():
    text = entrypoint_text()
    closed = text.split("closed() {", 1)[1].split("\n}\n", 1)[0]
    assert "iptables -A OUTPUT -o lo -j ACCEPT" in closed and "iptables -A OUTPUT -j REJECT" in closed
    assert "53" not in closed and "ESTABLISHED,RELATED" in closed
    assert 'AGENTOS_ENV_NETWORK:-full}" = "closed"' in text and "--bounding-set=-net_admin" in text


def test_a_closed_docker_machine_runs_the_packages_entrypoint_in_its_closed_mode():
    prov = DockerProvider()
    prov._info = {"NCPU": 4, "MemTotal": 8 * 2**30}
    spec = ServerSpec(name="agentos-vm-1", server_type="docker", image="agentos-env:latest", location="local",
                      user_data="", labels={"agentos-network": "limited"})
    args = prov.run_args(spec)
    assert "AGENTOS_ENV_NETWORK=closed" in args and args[args.index("--entrypoint") + 1] == "/etc/agentos-env/entrypoint.sh"
    assert "--entrypoint" not in prov.run_args(ServerSpec(**{**spec.__dict__, "labels": {}}))
    with tarfile.open(fileobj=io.BytesIO(seed_tar("k", "p", "a", entrypoint_text()))) as tar:
        info = tar.getmember("etc/agentos-env/entrypoint.sh")
        assert info.mode == 0o755


# ------------------------------------------------------------------ an environment


class ClosingProvider(FakeProvider):
    closes_network = True


@pytest.fixture
def closing(node, monkeypatch):  # noqa: F811
    prov = ClosingProvider()
    node["vms"]._provider = prov
    monkeypatch.setattr(node["vms"], "provider", lambda: prov)
    node["prov"] = prov
    return node


async def test_an_open_with_network_limited_rents_a_closed_machine_and_sets_it_up(closing):
    out = await closing["call"]("environment_open", title="closed one", network="limited")
    env = closing["mgr"].get(out["id"])
    assert env.network == "limited"
    rec = closing["vms"].store.get(env.vm_id)
    assert rec.network == "limited"
    (server,) = closing["prov"].servers.values()
    assert server.labels["agentos-network"] == "limited"
    scripts = [stdin for cmd, _g, stdin in closing["machine"].ran_in if cmd == "sh -s" and stdin and "3130" in stdin]
    assert scripts and "HTTPS_PROXY" in scripts[0]
    detail = await closing["call"]("environment", on=out["on"])
    assert detail["network"]["policy"] == "limited" and detail["network"]["blocked"] == []
    assert detail["network"]["allowed"]["preset"] == "package registries and code hosts"


async def test_an_agent_cannot_widen_the_nodes_network(closing):
    closing["mgr"].settings.network = "limited"
    with pytest.raises(Exception) as e:
        await closing["call"]("environment_open", title="wider", network="full")
    assert "network_not_allowed" in str(e.value) or getattr(e.value, "code", "") == "network_not_allowed"
    assert closing["prov"].servers == {}
    out = await closing["call"]("environment_open", title="default")
    assert closing["mgr"].get(out["id"]).network == "limited"  # the node's default holds without asking


async def test_a_provider_that_cannot_close_refuses_limited_and_rents_nothing(node):  # noqa: F811
    with pytest.raises(Exception) as e:
        await node["call"]("environment_open", title="closed", network="none")
    assert "network_not_allowed" in str(e.value) or getattr(e.value, "code", "") == "network_not_allowed"
    assert node["prov"].servers == {}


async def test_the_node_side_records_refusals_with_the_environment(closing):
    out = await closing["call"]("environment_open", title="closed two", network="none")
    mgr = closing["mgr"]
    node_net = mgr._nets[out["id"]]
    node_net.blocked.note("example.com")
    node_net.blocked.note("example.com")
    mgr._save_blocked(out["id"])
    env: Environment = mgr.get(out["id"])
    assert env.blocked[0]["host"] == "example.com" and env.blocked[0]["count"] == 2
    view = mgr.network_view(env)
    assert view["blocked"][0]["count"] == 2 and "--host example.com" in view["fix"]
    await mgr.close(out["id"], discard=True)
    assert out["id"] not in mgr._nets and not (mgr.home / "environments-net" / out["id"]).exists()


# ------------------------------------------------------------------ the owner's bindings


@pytest.fixture
async def vault(tmp_path):
    (tmp_path / "vault").mkdir()
    v = make_vault(private_home(tmp_path / "vault"))
    await v.start(control=False)
    yield v
    await v.stop()


async def test_a_binding_is_an_api_key_in_the_vault_for_its_hosts_and_environments(vault):
    await appsecrets.add(vault, "STRIPE_KEY", "sk_test_123456789", ["api.stripe.com"])
    await appsecrets.add(vault, "STRIPE_KEY", "sk_test_shop_only", ["api.stripe.com"], repo="github.com/me/shop")
    entries = await vault.list_entries()
    assert {e.kind.value for e in entries} == {"api_key"} and all(e.lanes == ["env/*"] for e in entries)
    shop = await appsecrets.bound_for(vault, "github.com/me/shop")
    other = await appsecrets.bound_for(vault, "github.com/me/blog")
    assert [(b.var, b.repo) for b in shop] == [("STRIPE_KEY", "github.com/me/shop")]
    assert [(b.var, b.repo) for b in other] == [("STRIPE_KEY", None)]

    class Ctx:
        def trace(self, *a, **k):
            pass

    value = await vault.resolve_app_secret(Ctx(), ref=shop[0].ref, lane_id="env/e1", url="https://api.stripe.com/")
    assert value.reveal() == "sk_test_shop_only"
    with pytest.raises(AgentosError):  # another host never gets it, whatever asks
        await vault.resolve_app_secret(Ctx(), ref=shop[0].ref, lane_id="env/e1", url="https://evil.example.com/")
    with pytest.raises(AgentosError):  # nor a lane outside environments
        await vault.resolve_app_secret(Ctx(), ref=shop[0].ref, lane_id="biz/insta", url="https://api.stripe.com/")
    await appsecrets.remove(vault, "STRIPE_KEY", repo="github.com/me/shop")
    assert [b.repo for b in await appsecrets.all_bound(vault)] == [None]


def test_no_tool_takes_a_secret_binding():
    from agentos.environments.tools import environment_open

    import inspect

    params = set(inspect.signature(environment_open).parameters)
    assert not params & {"secret", "secrets", "env", "allow_hosts", "host"}


async def test_a_bound_secret_reaches_the_machine_as_a_placeholder_only(node, vault):  # noqa: F811
    await appsecrets.add(vault, "STRIPE_KEY", "sk_live_never_on_the_vm", ["api.stripe.com"])
    node["rt"].register("vault", vault)
    out = await node["call"]("environment_open", title="with a secret")
    mgr = node["mgr"]
    env = mgr.get(out["id"])
    assert env.network == "full" and env.secrets == ["STRIPE_KEY"]
    sent = [stdin or "" for _cmd, _g, stdin in node["machine"].ran_in] + [cmd for cmd, _g in node["machine"].ran]
    assert not any("sk_live_never_on_the_vm" in s for s in sent)
    (script,) = [s for s in sent if "STRIPE_KEY=" in s]
    assert "STRIPE_KEY=\"agentos-secret-" in script and "127.0.10.1 api.stripe.com" in script
    assert "HTTPS_PROXY" not in script  # full: no proxy, the relay alone
    assert "BEGIN CERTIFICATE" in script and "PRIVATE KEY" not in script  # the CA's key stays on the node
    node_net = mgr._nets[env.id]
    assert node_net.swap_port and node_net.forwards() == ((network.SWAP_PORT_ON_VM, node_net.swap_port),)
    assert (await node["call"]("environment", on=out["on"]))["network"]["secrets"] == ["STRIPE_KEY"]
