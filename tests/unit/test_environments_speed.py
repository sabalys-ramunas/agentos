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
