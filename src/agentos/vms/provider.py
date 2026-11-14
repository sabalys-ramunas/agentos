"""What agentos needs from a cloud: create, read, list by label and delete servers, plus the
firewall, a throwaway SSH key resource and snapshots. Hetzner first (hetzner.py); a fake for tests
and `agentos dev up`."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Server:
    id: str
    name: str
    status: str                 # initializing, starting, running, off, deleting, ...
    ipv4: str | None
    labels: dict[str, str]
    server_type: str = ""
    created: str = ""
    ssh_port: int = 22          # the port SSH answers on at ipv4 (the docker provider maps one per machine)


@dataclass
class ServerSpec:
    name: str
    server_type: str
    image: str
    location: str
    user_data: str
    labels: dict[str, str]
    ssh_key_id: str | None = None
    firewall_id: str | None = None


class Provider(Protocol):
    name: str
    firewall: bool              # False: the provider's machines are not on the internet (docker), no firewall

    async def check(self) -> None: ...
    async def price_hourly(self, server_type: str, location: str) -> float | None: ...
    async def server_types(self) -> list[dict]: ...
    async def image_info(self, image: str) -> dict | None: ...
    async def create_server(self, spec: ServerSpec) -> Server: ...
    async def get_server(self, server_id: str) -> Server | None: ...
    async def list_servers(self, labels: dict[str, str]) -> list[Server]: ...
    async def delete_server(self, server_id: str) -> None: ...
    async def ensure_firewall(self, name: str, labels: dict[str, str], ssh_from: list[str]) -> str: ...
    async def create_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> str: ...
    async def delete_ssh_key(self, key_id: str) -> None: ...
    async def list_ssh_keys(self, labels: dict[str, str]) -> list[tuple[str, str]]: ...
    async def resolve_image(self, image: str) -> str: ...
    async def create_snapshot(self, server_id: str, description: str, labels: dict[str, str]) -> str: ...


FAKE_PRICES = {"cx23": 0.006, "cax11": 0.0055, "cpx22": 0.0095, "cpx42": 0.04, "ccx23": 0.05}
# name: (cores, memory GB, disk GB, architecture, cpu type), like Hetzner's 2026 line
FAKE_SPECS = {"cx23": (2, 4.0, 40, "x86", "shared"), "cax11": (2, 4.0, 40, "arm", "shared"),
              "cpx22": (2, 4.0, 80, "x86", "shared"), "cpx42": (8, 16.0, 320, "x86", "shared"),
              "ccx23": (4, 16.0, 160, "x86", "dedicated")}


def _match(labels: dict[str, str], want: dict[str, str]) -> bool:
    return all(labels.get(k) == v for k, v in want.items())


@dataclass
class FakeProvider:
    """An in-memory cloud. Servers are running the moment they exist; `fail_create` and
    `fail_delete` make the next calls raise, for tests."""
    name: str = "fake"
    firewall: bool = True
    servers: dict[str, Server] = field(default_factory=dict)
    keys: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)
    firewalls: dict[str, list[str]] = field(default_factory=dict)
    snapshots: dict[str, dict[str, str]] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=lambda: dict(FAKE_PRICES))
    specs: dict[str, tuple[int, float, int, str, str]] = field(default_factory=lambda: dict(FAKE_SPECS))
    unavailable: set[str] = field(default_factory=set)   # types the "cloud" is out of right now
    fail_create: int = 0
    fail_delete: int = 0
    user_data: dict[str, str] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1000))

    async def check(self) -> None:
        return None

    async def price_hourly(self, server_type: str, location: str) -> float | None:
        return self.prices.get(server_type)

    async def server_types(self) -> list[dict]:
        out = []
        for n, p in self.prices.items():
            cores, mem, disk, arch, cpu = self.specs.get(n, (2, 4.0, 40, "x86", "shared"))
            out.append({"name": n, "cores": cores, "memory_gb": mem, "disk_gb": disk, "architecture": arch,
                         "cpu_type": cpu, "price_hourly_eur": {"nbg1": p, "fsn1": p}, "available": None})
        return out

    async def image_info(self, image: str) -> dict | None:
        if image.startswith("snapshot:"):
            await self.resolve_image(image)
            return {"architecture": "x86", "disk_gb": 10}
        return None

    async def create_server(self, spec: ServerSpec) -> Server:
        from agentos.vms.models import VmError
        if self.fail_create:
            self.fail_create -= 1
            raise VmError("fake: create refused")
        if spec.server_type not in self.prices:
            raise VmError(f"fake: no server type {spec.server_type}", provider_code="invalid_input")
        if spec.server_type in self.unavailable:
            raise VmError(f"fake: {spec.server_type} is sold out", provider_code="resource_unavailable")
        sid = str(next(self._ids))
        s = Server(id=sid, name=spec.name, status="running", ipv4=f"203.0.113.{int(sid) % 250 + 1}",
                   labels=dict(spec.labels), server_type=spec.server_type)
        self.servers[sid] = s
        self.user_data[sid] = spec.user_data
        return s

    async def get_server(self, server_id: str) -> Server | None:
        return self.servers.get(server_id)

    async def list_servers(self, labels: dict[str, str]) -> list[Server]:
        return [s for s in self.servers.values() if _match(s.labels, labels)]

    async def delete_server(self, server_id: str) -> None:
        if self.fail_delete:
            self.fail_delete -= 1
            from agentos.vms.models import VmError
            raise VmError("fake: delete failed")
        if self.servers.pop(server_id, None) is not None:
            self.deleted.append(server_id)

    async def ensure_firewall(self, name: str, labels: dict[str, str], ssh_from: list[str]) -> str:
        self.firewalls[name] = list(ssh_from)
        return f"fw-{name}"

    async def create_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> str:
        kid = f"key-{next(self._ids)}"
        self.keys[kid] = (public_key, dict(labels))
        return kid

    async def delete_ssh_key(self, key_id: str) -> None:
        self.keys.pop(key_id, None)

    async def list_ssh_keys(self, labels: dict[str, str]) -> list[tuple[str, str]]:
        return [(k, v[0]) for k, v in self.keys.items() if _match(v[1], labels)]

    async def resolve_image(self, image: str) -> str:
        if image.startswith("snapshot:"):
            name = image.split(":", 1)[1]
            found = [i for i, lab in self.snapshots.items() if lab.get("agentos-image") == name]
            if not found:
                from agentos.vms.models import VmError
                raise VmError(f"no snapshot named {name}; build it with `agentos vm image build {name}`")
            return found[-1]
        return image

    async def create_snapshot(self, server_id: str, description: str, labels: dict[str, str]) -> str:
        sid = f"snap-{next(self._ids)}"
        self.snapshots[sid] = dict(labels)
        return sid
