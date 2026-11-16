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
