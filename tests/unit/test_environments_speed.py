"""How fast an environment comes up and goes down: the waits on the path from environment_open to `ready`
and from environment_close to a deleted machine, with no real machine (the docker CLI, the cloud and the VM
are fakes here; tests/unit/test_environments_docker.py runs the real story).

Measured on the docker provider (2026-11-16, a small public repository): an open spent 0.9 s of its 4 s in
three fixed checks of the container, 0.25 s polls after the VM answered, and the repository check after
the plan instead of beside it."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from test_environments import node, opened  # noqa: F401  (node is a fixture)

from agentos.vms import docker
from agentos.vms.docker import DockerProvider, sshd_greets
from agentos.vms.models import VmError
from agentos.vms.provider import ServerSpec

TITLE_FAIL = "Broken machine"
USER_DATA = ("#cloud-config\nssh_keys:\n  ed25519_private: |\n    -----BEGIN-----\n    abc\n    -----END-----\n"
             "  ed25519_public: ssh-ed25519 AAAA host\n")


async def _listener(greeting: bytes | None) -> tuple[asyncio.Server, int]:
    """127.0.0.1:<port> that sends `greeting` (an sshd) or closes at once (docker's forward with nothing
    behind it yet)."""

    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if greeting:
            writer.write(greeting)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(client, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


class FakeDocker(DockerProvider):
    """The docker CLI answered from memory: a container that runs (or stops) with its port on `port`."""

    def __init__(self, port: int, *, stops: bool = False):
        super().__init__()
        self.port, self.stops = port, stops
        self.calls: list[tuple[str, ...]] = []
        self._keys["key-1"] = ("ssh-ed25519 AAAA login", {})

    async def _run(self, *args: str, stdin: bytes | None = None, ok_codes: tuple[int, ...] = (0,),
                   timeout_s: float = docker.CALL_TIMEOUT_S) -> str:
        self.calls.append(args)
        if args[0] == "create":
            return "c0ffee\n"
        if args[0] == "inspect":
            running = not self.stops
            return json.dumps([{"Id": "c0ffee", "Name": "/m1", "Config": {"Labels": {}},
                                "State": {"Running": running, "Status": "running" if running else "exited"},
                                "NetworkSettings": {"Ports": {"22/tcp": [{"HostPort": str(self.port)}]}}}])
        if args[0] == "logs":
            return "agentos-env: could not wall the machine off (iptables needs NET_ADMIN); not starting\n"
        return ""


def _spec() -> ServerSpec:
    return ServerSpec(name="agentos-vm-1", server_type="docker", image="agentos-env:latest", location="local",
                      user_data=USER_DATA, labels={}, ssh_key_id="key-1")


async def test_sshd_greets_only_with_its_banner():
    sshd, port = await _listener(b"SSH-2.0-OpenSSH_9.6\r\n")
    empty, empty_port = await _listener(None)
    try:
        assert await sshd_greets("127.0.0.1", port)
        assert not await sshd_greets("127.0.0.1", empty_port)  # docker accepts before sshd listens
    finally:
        sshd.close()
        empty.close()


async def test_a_docker_machine_is_made_as_soon_as_sshd_greets():
    """No fixed checks after `docker start` (they were 0.9 s of every open): sshd's banner ends the wait."""
    sshd, port = await _listener(b"SSH-2.0-OpenSSH_9.6\r\n")
    prov = FakeDocker(port)
    try:
        t = time.monotonic()
        server = await prov.create_server(_spec())
        took = time.monotonic() - t
    finally:
        sshd.close()
    assert server.status == "running" and server.ssh_port == port
    assert took < 0.5, took
    assert [c[0] for c in prov.calls].count("inspect") == 1


async def test_a_docker_machine_waits_while_its_port_has_no_sshd_yet(monkeypatch):
    """Docker's port forward answers before sshd runs: the wait goes on until the banner, briefly."""
    sshd, port = await _listener(None)
    prov = FakeDocker(port)
    monkeypatch.setattr(docker, "START_WAIT_S", 0.3)
    try:
        t = time.monotonic()
        server = await prov.create_server(_spec())
        took = time.monotonic() - t
    finally:
        sshd.close()
    assert server.status == "running"  # past the wait it is the boot probe's to wait on
    assert 0.25 <= took < 1.5, took
    assert [c[0] for c in prov.calls].count("inspect") > 2


async def test_a_docker_machine_whose_entrypoint_refuses_is_removed_with_its_words():
    prov = FakeDocker(1, stops=True)
    with pytest.raises(VmError, match="did not start: agentos-env: could not wall"):
        await prov.create_server(_spec())
    names = [c[0] for c in prov.calls]
    assert "rm" in names and ("network", "rm", "agentos-vm-1") in [c[:3] for c in prov.calls]


# ------------------------------------------------------------------ the VM answers: the bring-up goes on at once


async def test_wait_booted_wakes_when_the_boot_ends(tmp_path):
    from agentos.core.runtime import Runtime
    from agentos.env.host import FakeHost
    from agentos.env.manager import EnvManager
    from agentos.env.paths import EnvSettings as TermSettings
    from agentos.env.sandbox import DirectLauncher
    from agentos.vms.manager import VmManager
    from agentos.vms.models import VmSettings
    from agentos.vms.provider import FakeProvider

    async def slow_probe(remote, *, timeout_s=15):
        await asyncio.sleep(0.3)
        return True, ""

    envs = EnvManager(tmp_path, host=FakeHost(), launcher=DirectLauncher(), settings=TermSettings(proxy_port=0))
    rt = Runtime(envs=envs)
    vms = VmManager(tmp_path, runtime=rt, provider=FakeProvider(), settings=VmSettings(ssh_from=["198.51.100.7/32"]),
                    node_name="n1", probe=slow_probe)
    rt.register("vms", vms)
    try:
        rec = await vms.create("env/eabcdefghjk", ttl_minutes=60, wait_s=0)
        assert rec.state == "booting"
        t = time.monotonic()
        assert await vms.wait_booted(rec.id, 5.0) is True
        assert time.monotonic() - t < 0.6 and vms.store.get(rec.id).state == "ready"
        assert await vms.wait_booted(rec.id, 5.0) is False  # no boot runs: nothing to wait for
    finally:
        await vms.shutdown()
        await envs.shutdown()


async def test_the_bring_up_goes_on_the_moment_the_vm_answers(node, monkeypatch):  # noqa: F811
    """The wait for the VM wakes on its boot's end: with a 10 s poll the open still takes no time."""
    from agentos.environments import bringup

    monkeypatch.setattr(bringup, "VM_POLL_S", 10.0)
    t = time.monotonic()
    out = await opened(node, repo="github.com/me/shop", wait_s=30)
    assert out["state"] == "ready" and time.monotonic() - t < 3.0


# ------------------------------------------------------------------ the repository's check beside the plan


async def test_the_repository_check_runs_while_the_open_is_planned(node, monkeypatch):  # noqa: F811
    """`git ls-remote` to the host (half a second) and the plan (the provider's types and images) at once."""
    mgr = node["mgr"]
    plan = mgr.plan

    async def slow_plan(*a, **kw):
        await asyncio.sleep(0.4)
        return await plan(*a, **kw)

    async def slow_repo(url, env, *, timeout_s=20, allow_file=False):
        await asyncio.sleep(0.4)
        return True, b""

    monkeypatch.setattr(mgr, "plan", slow_plan)
    mgr.repo_probe = slow_repo
    t = time.monotonic()
    out = await opened(node, repo="github.com/me/shop", wait_s=0)
    assert out["state"] in ("starting", "ready") and time.monotonic() - t < 0.75


async def test_a_plan_that_fails_still_says_so_first_and_stops_the_check(node, monkeypatch):  # noqa: F811
    from agentos.environments.errors import EnvironmentLimit

    mgr = node["mgr"]
    probed: list[str] = []
    stopped = asyncio.Event()

    async def refused(*a, **kw):
        await asyncio.sleep(0.05)
        raise EnvironmentLimit("at most 1 open at once")

    async def unreadable(url, env, *, timeout_s=20, allow_file=False):
        probed.append(url)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            stopped.set()
            raise
        return False, b"fatal: repository not found"

    monkeypatch.setattr(mgr, "plan", refused)
    mgr.repo_probe = unreadable
    with pytest.raises(Exception, match="at most 1 open at once"):
        await node["call"]("environment_open", title="Other", repo="github.com/me/gone", wait_s=0)
    await asyncio.wait_for(stopped.wait(), 1.0)
    assert probed and not mgr.store.open()


# ------------------------------------------------------------------ the provider's types, read before an open needs them


async def test_the_reaper_keeps_the_server_types_fresh_so_no_open_waits_for_them(tmp_path):
    from test_vms import Clock

    from agentos.core.runtime import Runtime
    from agentos.vms.manager import OFFERS_TTL, VmManager
    from agentos.vms.models import VmSettings
    from agentos.vms.provider import FakeProvider

    class Counting(FakeProvider):
        reads = 0

        async def server_types(self):
            Counting.reads += 1
            return await super().server_types()

    clock = Clock()
    vms = VmManager(tmp_path, runtime=Runtime(), provider=Counting(), settings=VmSettings(), node_name="n1",
                    clock=clock)
    await vms.fresh_offers()  # the reaper's first round, a second after the node starts
    assert Counting.reads == 1
    await vms.plan("env/eabcdefghjk", ttl_minutes=60)
    await vms.fresh_offers()
    assert Counting.reads == 1  # the open used them; still fresh
    clock.advance(seconds=OFFERS_TTL.total_seconds() - 60)  # stale within two rounds: read again now
    await vms.fresh_offers()
    assert Counting.reads == 2
    clock.advance(seconds=OFFERS_TTL.total_seconds() - 1)
    await vms.plan("env/eabcdefghjk", ttl_minutes=60)
    assert Counting.reads == 2 and vms._offers is not None and clock() - vms._offers[0] < OFFERS_TTL


# ------------------------------------------------------------------ the tunnels beside the setup check and the clone


def _timeline(n, monkeypatch, *, tunnel_s: float = 0.0, setup_s: float = 0.0, gateway: int | None = None):
    """Record when the tunnel, the setup check, the git config and the clone start and end."""
    from agentos.environments import machine as mach

    mgr, fake = n["mgr"], n["machine"]
    seen: list[tuple[str, float]] = []
    exec_ = fake.exec

    async def exec_timed(lane_id, cmd, **kw):
        if cmd == mach.SETUP_STATE:
            seen.append(("setup start", time.monotonic()))
            await asyncio.sleep(setup_s)
            seen.append(("setup end", time.monotonic()))
        if "git clone" in cmd:
            seen.append(("clone start", time.monotonic()))
        return await exec_(lane_id, cmd, **kw)

    async def open_tunnel(env):
        seen.append(("tunnel start", time.monotonic()))
        await asyncio.sleep(tunnel_s)
        seen.append(("tunnel end", time.monotonic()))
        return gateway

    async def git_setup(env):
        seen.append(("git", time.monotonic()))

    monkeypatch.setattr(n["envs"], "exec", exec_timed)
    monkeypatch.setattr(mgr, "_open_tunnel", open_tunnel)
    monkeypatch.setattr(mgr, "_git_setup", git_setup)
    return seen


def _at(seen, what):
    return next(t for name, t in seen if name == what)


async def test_the_tunnels_connect_while_the_setup_check_runs_and_git_waits_for_it(node, monkeypatch):  # noqa: F811
    seen = _timeline(node, monkeypatch, tunnel_s=0.05, setup_s=0.3, gateway=43129)
    out = await opened(node, repo="github.com/me/shop", wait_s=30)
    assert out["state"] == "ready"
    assert _at(seen, "tunnel start") < _at(seen, "setup end")       # connected beside the check
    assert _at(seen, "tunnel end") < _at(seen, "setup end")
    assert _at(seen, "git") >= _at(seen, "setup end")               # git is there once the setup is done


async def test_a_public_clone_does_not_wait_for_the_tunnels(node, monkeypatch):  # noqa: F811
    seen = _timeline(node, monkeypatch, tunnel_s=0.4)
    t = time.monotonic()
    out = await opened(node, repo="github.com/me/shop", wait_s=30)
    assert out["state"] == "ready" and out["workdir"] == "/work/shop"
    assert _at(seen, "clone start") < _at(seen, "tunnel end")       # straight from its host
    assert time.monotonic() - t >= 0.4                              # ready still means the tunnels are up


async def test_a_clone_through_the_gateway_waits_for_the_tunnels(node, monkeypatch):  # noqa: F811
    seen = _timeline(node, monkeypatch, tunnel_s=0.2, gateway=43129)
    monkeypatch.setattr(node["mgr"], "_through_gateway", lambda env: True)
    out = await opened(node, repo="github.com/me/shop", wait_s=30)
    assert out["state"] == "ready"
    assert _at(seen, "clone start") >= _at(seen, "git") >= _at(seen, "tunnel end")


async def test_a_setup_that_fails_stops_the_tunnels_beside_it(node, monkeypatch):  # noqa: F811
    from agentos.policy.wrap import ToolCallError

    seen = _timeline(node, monkeypatch, tunnel_s=5.0)
    node["machine"].setup = "status: error"
    t = time.monotonic()
    with pytest.raises(ToolCallError, match="setup failed"):
        await node["call"]("environment_open", title=TITLE_FAIL, repo="github.com/me/shop", wait_s=30)
    assert time.monotonic() - t < 2.0                               # the tunnels were not waited for
    assert "tunnel start" in [n for n, _ in seen] and "tunnel end" not in [n for n, _ in seen]


# ------------------------------------------------------------------ Hetzner: one connection to its API


async def test_hetzner_keeps_one_client_for_its_calls_and_closes_it(monkeypatch):
    import httpx

    from agentos.vms import hetzner

    made: list[httpx.AsyncClient] = []

    class Counted(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    monkeypatch.setattr(hetzner.httpx, "AsyncClient", Counted)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer tok-SECRET"
        if req.url.path.endswith("/ssh_keys") and req.method == "POST":
            return httpx.Response(201, json={"ssh_key": {"id": 7}})
        if req.url.path.endswith("/locations"):
            return httpx.Response(200, json={"locations": [{"name": "nbg1", "network_zone": "eu-central"}]})
        return httpx.Response(200, json={"servers": []})

    p = hetzner.HetznerProvider("tok-SECRET", transport=httpx.MockTransport(handler))
    await p.check()
    await asyncio.gather(p.create_ssh_key("k", "ssh-ed25519 AAAA", {}), p.locations())
    assert len(made) == 1 and not made[0].is_closed  # three calls, two at once, one client
    await p.aclose()
    assert made[0].is_closed
    await p.check()
    assert len(made) == 2  # a call after the close opens a new one
    await p.aclose()
