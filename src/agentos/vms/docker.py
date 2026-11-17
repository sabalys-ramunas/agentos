"""The docker provider: a "VM" is a local container with sshd (deploy/environments/Dockerfile).

For a person without a cloud account, for CI, and for working on agentos: everything an agent
environment does on a cloud VM works the same way here, because the node reaches the machine only
over SSH (agentos.vms.ssh), with a key and a host key it made itself. Set `provider = "docker"` in
vms.toml and get the image once with `agentos environments setup --pull` (the prebuilt one,
downloaded; --build builds it here).

What differs from a cloud VM, said plainly:

- SSH listens on a port of this computer's loopback (127.0.0.1, one port per machine, picked by
  docker), not on a public address, so there is no firewall to make and nothing to pay.
- The machine shares this computer's kernel. It is a container: fine for your own code and for
  tests, weaker than a VM for code you do not trust. The cloud provider is the isolated one.
- Its walls (`create_server`): a docker network of its own with no traffic between containers, so
  machines never reach each other; CPU, memory and process limits from vms.toml (`docker_cpus`,
  `docker_memory_gb`, `docker_pids`; an environment's size asks for the CPU and memory of its own
  server type, SIZE_TYPES, at most this computer's); every capability dropped but the few a dev box and sshd use;
  and a firewall inside it, set by its entrypoint before sshd starts and then out of reach (no
  NET_ADMIN left), that refuses new connections to private, shared and link-local addresses. That
  is what keeps it off this computer: Docker Desktop for Mac forwards host.docker.internal to the
  Mac's own loopback, where the node's git gateway and the environments' SOCKS tunnels listen; on
  Linux the docker gateway is this computer. DNS (port 53) stays open, and the internet does.
- First-boot cloud-config is not run (there is no cloud-init): the image already has what the
  setup script installs. The keys from the node's cloud-config are copied in before it starts.
- A snapshot is `docker commit` to `<image repository>:snapshot-<name>`.

Every docker call is the docker CLI (`docker_bin` in vms.toml), so a remote docker host works
through DOCKER_HOST like any other docker command.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import tarfile
from typing import Any

from agentos.vms.models import VmError
from agentos.vms.provider import Server, ServerSpec

SERVER_TYPE = "docker"
# The sizes of agent environments (agentos.environments.models.SIZES) as docker machines: their CPUs
# and GB of memory, at most this computer's. Only asked for by name ("pinned"): the cheapest-that-fits
# pick of agentos.vms, where every docker type costs nothing, keeps to SERVER_TYPE.
SIZE_TYPES = {"docker-small": (2, 4), "docker-medium": (4, 8), "docker-large": (8, 16), "docker-xlarge": (16, 32)}
TYPE_LABEL = "agentos-server-type"
SEED_DIR = "etc/agentos-env"   # where the entrypoint reads the keys from (deploy/environments/entrypoint.sh)
CALL_TIMEOUT_S = 120.0
START_CHECKS, START_CHECK_S = 3, 0.3
IMAGE_LABEL = "org.agentos.environment"
IMAGE_VERSION = 2              # the image's entrypoint walls the machine off (deploy/environments/Dockerfile)
# What a dev box and sshd need, and nothing more: files and owners (apt, dpkg), switching users
# (sshd, apt's sandbox), signals, ports under 1024, sshd's chroot and its login records. NET_ADMIN is
# for the entrypoint's firewall only: it drops NET_ADMIN, NET_RAW and SETPCAP before sshd starts.
CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "SETGID", "SETUID", "SETPCAP", "NET_BIND_SERVICE",
        "SYS_CHROOT", "AUDIT_WRITE", "NET_ADMIN")


class _AnyLocation(dict):
    """Prices by location for a machine that runs here: free, wherever vms.toml says it is."""

    def get(self, key: Any, default: Any = None) -> Any:
        return 0.0

    def __bool__(self) -> bool:
        return True


def host_key_of(user_data: str) -> tuple[str, str]:
    """The ed25519 host key pair in the node's cloud-config (agentos.vms.keys.cloud_init)."""
    lines = user_data.splitlines()
    private: list[str] = []
    public = ""
    reading = False
    for line in lines:
        if line.strip() == "ed25519_private: |":
            reading = True
            continue
        if reading:
            if line.startswith("    "):
                private.append(line[4:])
                continue
            reading = False
        if line.strip().startswith("ed25519_public:"):
            public = line.split(":", 1)[1].strip()
    if not private or not public:
        raise VmError("docker: the machine's cloud-config has no host key")
    return "\n".join(private) + "\n", public + "\n"


def seed_tar(host_private: str, host_public: str, authorized: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, text, mode in (("ssh_host_ed25519_key", host_private, 0o600),
                                 ("ssh_host_ed25519_key.pub", host_public, 0o644),
                                 ("authorized_keys", authorized, 0o600)):
            data = text.encode()
            info = tarfile.TarInfo(f"{SEED_DIR}/{name}")
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class DockerProvider:
    name = "docker"
    firewall = False

    def __init__(self, *, image: str = "agentos-env:latest", docker: str = "docker", cpus: float = 2.0,
                 memory_gb: float = 4.0, pids: int = 512):
        self.image = image
        self.docker = docker
        self.cpus = cpus
        self.memory_gb = memory_gb
        self.pids = pids
        self._keys: dict[str, tuple[str, dict[str, str]]] = {}
        self._info: dict | None = None

    def __repr__(self) -> str:
        return f"DockerProvider({self.image})"

    # ------------------------------------------------------------------ docker
    async def _run(self, *args: str, stdin: bytes | None = None, ok_codes: tuple[int, ...] = (0,),
                   timeout_s: float = CALL_TIMEOUT_S) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker, *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            raise VmError(f"docker: cannot run {self.docker} ({e.strerror or e}); install Docker or set docker_bin "
                          "in vms.toml") from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise VmError(f"docker {args[0]} took longer than {int(timeout_s)} s") from None
        if proc.returncode not in ok_codes:
            said = (err or out or b"").decode(errors="replace").strip().splitlines()
            raise VmError(f"docker {args[0]}: {said[-1][:300] if said else f'exit {proc.returncode}'}")
        return out.decode(errors="replace")

    async def _image_exists(self, image: str) -> bool:
        out = await self._run("image", "ls", "--format", "{{.Repository}}:{{.Tag}}", image)
        return bool(out.strip())

    async def _check_version(self, image: str) -> None:
        """Refuse an image made before the machines had walls (its entrypoint would not set them)."""
        raw = await self._run("image", "inspect", "--format", f'{{{{ index .Config.Labels "{IMAGE_LABEL}" }}}}', image,
                              ok_codes=(0, 1))
        try:
            version = int(raw.strip() or 0)
        except ValueError:
            version = 0
        if version < IMAGE_VERSION:
            raise VmError(f"docker: the image {image} is older than this agentos (it does not wall its machines off "
                          "from this computer); get it again with `agentos environments setup --pull`")

    # ------------------------------------------------------------------ provider
    async def check(self) -> None:
        raw = await self._run("info", "--format", "{{json .}}")
        try:
            self._info = json.loads(raw)
        except ValueError:
            self._info = {}
        if not await self._image_exists(self.image):
            raise VmError(f"docker: no image {self.image}; get it with `agentos environments setup --pull`")
        await self._check_version(self.image)

    async def price_hourly(self, server_type: str, location: str) -> float | None:
        return 0.0

    def limits(self, server_type: str) -> tuple[float, float]:
        """(CPUs, GB of memory) one machine of `server_type` may use: vms.toml's limits for the
        default type, a size's own for the others, never more than this computer has."""
        info = self._info or {}
        host_cpu = int(info.get("NCPU") or os.cpu_count() or 2)
        host_mem = float(info.get("MemTotal") or 0) / 2**30
        cpu, mem = (self.cpus, self.memory_gb) if server_type == SERVER_TYPE else SIZE_TYPES[server_type]
        cores = min(cpu, host_cpu)
        cores = int(cores) if float(cores).is_integer() else cores
        return cores, round(min(mem, host_mem) if host_mem else mem, 1)

    async def server_types(self) -> list[dict]:
        if self._info is None:
            try:
                self._info = json.loads(await self._run("info", "--format", "{{json .}}"))
            except (VmError, ValueError):
                self._info = {}
        if not self._info.get("MemTotal"):
            self._info["MemTotal"] = 4 * 2**30
        arch = "arm" if str(self._info.get("Architecture") or "").lower() in ("aarch64", "arm64") else "x86"
        out = []
        for name in (SERVER_TYPE, *SIZE_TYPES):
            cores, memory = self.limits(name)
            # architecture None: whatever this computer is, so every profile's arch fits (VmManager._fits)
            out.append({"name": name, "cores": cores, "memory_gb": memory, "disk_gb": 0, "cpu_type": "shared",
                        "architecture": None, "host_architecture": arch, "price_hourly_eur": _AnyLocation(),
                        "available": None, "pinned": name != SERVER_TYPE})
        return out

    async def image_info(self, image: str) -> dict | None:
        return None

    def _image_for(self, image: str) -> str:
        if image.startswith("snapshot:"):
            repo = self.image.rsplit(":", 1)[0]
            return f"{repo}:snapshot-{image.split(':', 1)[1]}"
        if ":" in image and "/" not in image.split(":", 1)[1] and not image.startswith(("ubuntu", "debian")):
            return image  # a docker image named in full
        return self.image  # "ubuntu-24.04" and other cloud names: the environment image

    async def resolve_image(self, image: str) -> str:
        name = self._image_for(image)
        if not await self._image_exists(name):
            if image.startswith("snapshot:"):
                raise VmError(f"no snapshot named {image.split(':', 1)[1]} (docker image {name})")
            raise VmError(f"docker: no image {name}; get it with `agentos environments setup --pull`")
        await self._check_version(name)
        return name

    def run_args(self, spec: ServerSpec) -> list[str]:
        """`docker create` for one machine: its own network, its limits, few capabilities, and the
        entrypoint's wall turned on (see the module's docstring)."""
        cpus, memory_gb = self.limits(spec.server_type)
        memory = str(int(memory_gb * 2**30))
        args = ["create", "--name", spec.name, "--hostname", spec.name[:63], "--init", "-p", "127.0.0.1::22",
                "--network", spec.name, "--cpus", f"{cpus:g}", "--memory", memory, "--memory-swap", memory,
                "--pids-limit", str(self.pids), "--cap-drop", "ALL", "-e", "AGENTOS_ENV_WALL=1"]
        for cap in CAPS:
            args += ["--cap-add", cap]
        for k, v in sorted({**spec.labels, TYPE_LABEL: spec.server_type}.items()):
            args += ["--label", f"{k}={v}"]
        return [*args, spec.image]

    async def _network(self, name: str, labels: dict[str, str]) -> None:
        """A bridge network for one machine, with no traffic between its containers (it has one)."""
        args = ["network", "create", "--driver", "bridge", "-o", "com.docker.network.bridge.enable_icc=false"]
        for k, v in sorted(labels.items()):
            args += ["--label", f"{k}={v}"]
        await self._run(*args, name)

    async def create_server(self, spec: ServerSpec) -> Server:
        if spec.server_type != SERVER_TYPE and spec.server_type not in SIZE_TYPES:
            raise VmError(f"docker: no server type {spec.server_type}", provider_code="invalid_input")
        host_private, host_public = host_key_of(spec.user_data)
        key = self._keys.get(spec.ssh_key_id or "")
        if key is None:
            raise VmError("docker: the machine has no login key")
        await self._network(spec.name, spec.labels)
        cid = ""
        try:
            cid = (await self._run(*self.run_args(spec))).strip()
            await self._run("cp", "-", f"{cid}:/", stdin=seed_tar(host_private, host_public, key[0] + "\n"))
            await self._run("start", cid)
            server = None
            for _ in range(START_CHECKS):  # an entrypoint that refuses (no wall) exits within a moment
                await asyncio.sleep(START_CHECK_S)
                server = await self.get_server(cid)
                if server is None or server.status != "running":
                    break
            if server is None or not server.ssh_port or server.status != "running":
                said = await self._run("logs", "--tail", "5", cid, ok_codes=(0, 1)) if cid else ""
                raise VmError("docker: the container did not start" + (f": {said.strip()[-300:]}" if said.strip()
                                                                        else ""))
        except BaseException:
            if cid:
                await self._run("rm", "-f", "-v", cid, ok_codes=(0, 1))
            await self._run("network", "rm", spec.name, ok_codes=(0, 1))
            raise
        return server

    def _server(self, d: dict) -> Server:
        cfg = d.get("Config") or {}
        state = d.get("State") or {}
        port = 0
        for b in ((d.get("NetworkSettings") or {}).get("Ports") or {}).get("22/tcp") or []:
            if b.get("HostPort"):
                port = int(b["HostPort"])
                break
        status = "running" if state.get("Running") else str(state.get("Status") or "off")
        return Server(id=str(d.get("Id", ""))[:12], name=str(d.get("Name", "")).lstrip("/"), status=status,
                      ipv4="127.0.0.1" if port else None, labels=dict(cfg.get("Labels") or {}),
                      server_type=(cfg.get("Labels") or {}).get(TYPE_LABEL) or SERVER_TYPE,
                      created=str(d.get("Created", "")), ssh_port=port or 22)

    async def _inspect(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        raw = await self._run("inspect", *ids, ok_codes=(0, 1))
        try:
            return [d for d in json.loads(raw or "[]") if isinstance(d, dict)]
        except ValueError:
            return []

    async def get_server(self, server_id: str) -> Server | None:
        if not server_id:
            return None
        found = await self._inspect([server_id])
        return self._server(found[0]) if found else None

    async def list_servers(self, labels: dict[str, str]) -> list[Server]:
        args = ["ps", "-a", "-q", "--no-trunc"]
        for k, v in sorted(labels.items()):
            args += ["--filter", f"label={k}={v}"]
        ids = [i[:12] for i in (await self._run(*args)).split()]
        return [self._server(d) for d in await self._inspect(ids)]

    async def delete_server(self, server_id: str) -> None:
        found = await self._inspect([server_id]) if server_id else []
        network = str(((found[0].get("HostConfig") or {}).get("NetworkMode")) or "") if found else ""
        await self._run("rm", "-f", "-v", server_id, ok_codes=(0, 1))
        if network.startswith("agentos-"):  # its own network (create_server), never bridge or one of yours
            await self._run("network", "rm", network, ok_codes=(0, 1))

    async def sweep(self, labels: dict[str, str]) -> list[str]:
        """Remove this node's networks that no container uses any more (a machine deleted outside
        agentos, or a create that died halfway). A network still in use is left alone by docker."""
        args = ["network", "ls", "-q"]
        for k, v in sorted(labels.items()):
            args += ["--filter", f"label={k}={v}"]
        gone = []
        for nid in (await self._run(*args)).split():
            used = await self._run("network", "inspect", "--format", "{{len .Containers}}", nid, ok_codes=(0, 1))
            if used.strip() == "0":
                await self._run("network", "rm", nid, ok_codes=(0, 1))
                gone.append(nid)
        return gone

    async def ensure_firewall(self, name: str, labels: dict[str, str], ssh_from: list[str]) -> str:
        return ""  # SSH is on this computer's loopback only

    async def create_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> str:
        kid = f"key-{name}"
        self._keys[kid] = (public_key.strip(), dict(labels))
        return kid

    async def delete_ssh_key(self, key_id: str) -> None:
        self._keys.pop(key_id, None)

    async def list_ssh_keys(self, labels: dict[str, str]) -> list[tuple[str, str]]:
        return [(k, v[0]) for k, v in self._keys.items() if all(v[1].get(a) == b for a, b in labels.items())]

    async def create_snapshot(self, server_id: str, description: str, labels: dict[str, str]) -> str:
        """`docker commit`. An image build's is `<repository>:snapshot-<name>`; an environment's save
        (label agentos-save) is `<repository>:save-<id>`, and that name is its id: a machine boots from it."""
        save = labels.get("agentos-save")
        repo = self.image.rsplit(":", 1)[0]
        image = f"{repo}:save-{save}" if save else self._image_for(f"snapshot:{labels.get('agentos-image') or 'snapshot'}")
        changes = [f"LABEL {k}={v}" for k, v in sorted(labels.items())]
        args = ["commit", "-m", description[:200]]
        for c in changes:
            args += ["--change", c]
        out = (await self._run(*args, server_id, image, timeout_s=900)).strip()[:19]
        return image if save else (out or image)

    async def get_image(self, image_id: str) -> dict | None:
        raw = await self._run("image", "inspect", "--format", "{{.Size}}", image_id, ok_codes=(0, 1))
        if not raw.strip().isdigit():
            return None
        return {"id": image_id, "status": "available", "gb": round(int(raw.strip()) / 2**30, 3), "labels": {}}

    async def delete_image(self, image_id: str) -> None:
        await self._run("rmi", "-f", image_id, ok_codes=(0, 1))

    async def list_images(self, labels: dict[str, str]) -> list[dict]:
        """The images `docker commit` made with every label (a save's is named by its id)."""
        args = ["images", "--format", "{{.Repository}}:{{.Tag}}\t{{.CreatedAt}}"]
        for k, v in sorted(labels.items()):
            args += ["--filter", f"label={k}={v}"]
        out = []
        for line in (await self._run(*args, ok_codes=(0, 1))).splitlines():
            name, _, created = line.partition("\t")
            if name and not name.endswith(":<none>"):
                tag = name.rsplit(":", 1)[-1]
                save = tag.removeprefix("save-") if tag.startswith("save-") else None
                out.append({"id": name, "status": "available", "created": created, "gb": None,
                            "labels": {**labels, **({"agentos-save": save} if save else {})}})
        return out
