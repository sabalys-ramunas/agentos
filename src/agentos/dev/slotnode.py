"""A throwaway agentosd in a dev slot on a node: for the benchmarks (scripts/bench_node.py) and the
soak and chaos tests (tests/soak/). docs/BENCH.md.

The daemon is the real one (`python -m agentos.dev.slotnode serve` runs agentos.node.daemon.serve
with the production composition: real lanes, Camoufox, Xvfb, x11vnc, policy, MCP), in its own home
and on the slot's displays and ports (scripts/node-test.sh exports them). Two seams differ, and
only so that nothing leaves the node:

- Every lane goes through a stand-in proxy on loopback that is also the only web the lanes see:
  it serves tests/fixtures/site under SITE (a name that resolves nowhere), answers the exit IP
  check under IP_URL with an address the test chooses, refuses CONNECT, and never forwards.
  Stopping it makes the proxy unreachable.
- Camoufox's geoip lookup is off (it would ask a real IP service) and the node's own IP is a
  fixed TEST-NET address instead of a lookup.

Linux only for the process accounting (/proc); the rest imports anywhere.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SITE_HOST = "site.bench.example"
IP_HOST = "ip.bench.example"
SITE = f"http://{SITE_HOST}"
IP_URL = f"http://{IP_HOST}/ip"
NODE_IP = "198.51.100.1"   # TEST-NET-2: what the self-check takes as the node's own address
EXIT_IP = "203.0.113.10"   # TEST-NET-3: what the stand-in reports as the lane's exit
PROXY_LABEL = "standin"
OFFLINE_PROXY = "http://127.0.0.1:9"  # nothing listens: library downloads in the daemon go nowhere
SITE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "site"
HARNESS_FILE = "slotnode.json"


# ---- the slot ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """One agent's ranges from scripts/node-test.sh: ten displays, VNC, viewer and MCP ports."""

    display_base: int
    vnc_base: int
    viewer_base: int
    mcp_base: int

    @classmethod
    def from_env(cls, default: int = 53) -> Slot:
        n = int(os.environ.get("AGENTOS_TEST_SLOT") or default)
        return cls(display_base=int(os.environ.get("AGENTOS_TEST_DISPLAY_BASE") or n * 10),
                   vnc_base=int(os.environ.get("AGENTOS_TEST_VNC_BASE") or 5000 + n * 10),
                   viewer_base=int(os.environ.get("AGENTOS_TEST_VIEWER_BASE") or 6000 + n * 10),
                   mcp_base=int(os.environ.get("AGENTOS_TEST_MCP_PORT_BASE") or 7400 + (n - 10) * 10))

    @property
    def displays(self) -> range:
        return range(self.display_base, self.display_base + 10)

    @property
    def lane_ports(self) -> set[int]:
        """The ports lanes may listen on (x11vnc, and websockify in older builds)."""
        return set(range(self.vnc_base, self.vnc_base + 10)) | set(range(self.viewer_base, self.viewer_base + 9))

    @property
    def proxy_port(self) -> int:
        return self.viewer_base + 9  # lanes use at most 9 slots (max_slots below)

    @property
    def mcp_port(self) -> int:
        return self.mcp_base


# ---- the stand-in proxy ----------------------------------------------------------------------


class StandInProxy:
    """The lanes' proxy and the only web they see. Basic auth like a paid proxy (so the password
    is a secret the logs must never show). `exit_ip` is what the exit check reads; change it to
    move the lane's exit. /slow/<ms>/<page> answers after a delay, for calls that must still be
    in flight when something breaks."""

    def __init__(self, port: int, *, username: str, password: str, exit_ip: str = EXIT_IP,
                 site_dir: Path = SITE_DIR):
        self.port, self.username, self.password, self.exit_ip = port, username, password, exit_ip
        self.site_dir = site_dir.resolve()
        self.requests: list[tuple[str, str]] = []  # (host, path) of every request it answered
        self.refused = 0  # CONNECT and unknown hosts
        self._server: ThreadingHTTPServer | None = None
        self._conns: set[socket.socket] = set()
        self._lock = threading.Lock()

    @property
    def auth(self) -> str:
        return "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> StandInProxy:
        if self._server is not None:
            return self
        proxy = self

        class Handler(_ProxyHandler):
            owner = proxy

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, name="standin-proxy", daemon=True).start()
        self._server = server
        return self

    def stop(self) -> None:
        """Stop listening and cut every open connection: the proxy is gone, as when its host dies."""
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        with self._lock:
            conns, self._conns = list(self._conns), set()
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                conn.close()


class _ProxyHandler(BaseHTTPRequestHandler):
    owner: StandInProxy
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        with self.owner._lock:
            self.owner._conns.add(self.connection)

    def finish(self) -> None:
        with contextlib.suppress(OSError):
            super().finish()
        with self.owner._lock:
            self.owner._conns.discard(self.connection)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8",
              headers: Iterable[tuple[str, str]] = ()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_CONNECT(self) -> None:
        self.owner.refused += 1
        self._send(403, b"no tunnels here")
        self.close_connection = True

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.do_GET()

    def do_GET(self) -> None:
        if self.headers.get("Proxy-Authorization") != self.owner.auth:
            self._send(407, b"proxy auth", headers=[("Proxy-Authenticate", 'Basic realm="standin"')])
            return
        parts = urlsplit(self.path)
        host = (parts.hostname or (self.headers.get("Host") or "").split(":")[0]).lower()
        path = parts.path or "/"
        if host == IP_HOST:
            self.owner.requests.append((host, path))
            self._send(200, self.owner.exit_ip.encode(), "text/plain",
                       headers=[("Access-Control-Allow-Origin", "*")])
            return
        if host != SITE_HOST:
            self.owner.refused += 1
            self._send(404, b"not here")
            return
        self.owner.requests.append((host, path))
        if path.startswith("/slow/"):
            _, _, ms, rest = path.split("/", 3)
            time.sleep(min(int(ms) if ms.isdigit() else 0, 60_000) / 1000)
            path = "/" + rest
        if path.endswith("/"):
            path += "index.html"
        target = (self.owner.site_dir / path.lstrip("/")).resolve()
        if not target.is_relative_to(self.owner.site_dir) or not target.is_file():
            self._send(404, b"<!doctype html><title>Not found</title><h1>Not found</h1>")
            return
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript",
                 ".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json"}.get(
            target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


def check_fingerprint_model() -> None:
    """Camoufox downloads its fingerprint model on first use (from GitHub). The slot daemon has no
    way out, so a fresh venv must fetch it once beforehand."""
    try:
        from fpgen import pkgman
    except ImportError:
        return  # not a node install: the daemon fails on its own, with its own message
    if not pkgman.DATA_DIR.is_dir() or not any(pkgman.DATA_DIR.iterdir()):
        raise RuntimeError("Camoufox's fingerprint model is missing from this venv: run "
                           "`uv run python -m fpgen fetch` once on the node (it downloads from GitHub)")


# ---- the daemon ----------------------------------------------------------------------------


@dataclass
class SlotDaemon:
    """A real agentosd in `home` on the slot's ranges, as a child process this harness owns."""

    home: Path
    slot: Slot
    token: str
    proxy: StandInProxy
    node: dict[str, Any] = field(default_factory=dict)  # node.toml overrides
    proc: subprocess.Popen | None = None
    starts: int = 0
    agent_token: str = ""  # an agent's own grant on the lanes' workspaces (what agents hold, not the node token)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.slot.mcp_port}/mcp"

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None and self.proc.poll() is None else None

    def config(self):
        from agentos.core.config import NodeConfig

        values = {"name": "slot-node", "home": self.home, "mcp_host": "127.0.0.1", "mcp_port": self.slot.mcp_port,
                  "mcp_token": self.token, "display_base": self.slot.display_base,
                  "vnc_base": self.slot.vnc_base, "viewer_base": self.slot.viewer_base, "max_slots": 9,
                  "max_warm_lanes": 2, "idle_seconds": 3600, "self_check": True, **self.node}
        return NodeConfig(**values)

    def prepare(self, lanes: Iterable[str] = ()) -> None:
        """Write node.toml, the proxy, a keyfile vault and fake brains, and create the lanes."""
        from agentos.core.config import save_config, save_proxies
        from agentos.core.models import LaneSpec, ProxyConfig
        from agentos.dev.node import prepare_home

        check_fingerprint_model()
        prepare_home(self.home)
        cfg = self.config()
        save_config(cfg)
        save_proxies(cfg, {PROXY_LABEL: ProxyConfig(label=PROXY_LABEL, host="localhost", port=self.proxy.port,
                                                    username=self.proxy.username, password=self.proxy.password)})
        (self.home / HARNESS_FILE).write_text(json.dumps({"node_ip": NODE_IP, "self_check_url": IP_URL}))
        lanes = list(lanes)
        workspaces = sorted({lane.split("/", 1)[0] for lane in lanes})
        if workspaces and not self.agent_token:
            self.agent_token = self.issue_grant(workspaces)
        wanted = [lane for lane in lanes if not cfg.lane_dir(lane).exists()]
        if wanted:
            from agentos.node.lanes import build_lane_manager

            async def create() -> None:
                mgr = build_lane_manager(cfg)
                for lane in wanted:
                    await mgr.create(LaneSpec(id=lane, proxy_label=PROXY_LABEL))

            _run_in_thread(create)

    def issue_grant(self, workspaces: Iterable[str], *, subject: str = "slot-agent", ceiling: str = "draft") -> str:
        """`agentos grants issue` for an agent on these workspaces and the fixture site; the token."""
        args = [sys.executable, "-m", "agentos.cli", "grants", "issue", "--subject", subject, "--kind", "agent",
                "--ceiling", ceiling, "--domain", SITE_HOST]
        for ws in workspaces:
            args += ["--workspace", ws]
        out = subprocess.run(args, env={**os.environ, "AGENTOS_HOME": str(self.home)}, capture_output=True,
                             text=True, timeout=60, check=False)
        token = next((line.strip() for line in out.stdout.splitlines() if line.strip().startswith("agt")), "")
        if out.returncode != 0 or not token:
            raise RuntimeError(f"could not issue an agent grant: {out.stderr[-600:]}")
        return token

    def set_node(self, **values: Any) -> None:
        """Change node.toml (takes effect at the next start)."""
        from agentos.core.config import save_config

        self.node.update(values)
        save_config(self.config())

    def start(self, timeout: float = 90) -> None:
        if self.pid is not None:
            raise RuntimeError("the slot daemon is already running")
        env = {**os.environ, "AGENTOS_HOME": str(self.home), "PYTHONUNBUFFERED": "1",
               "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[var] = OFFLINE_PROXY
        out = open(self.home / "daemon.out", "ab")  # noqa: SIM115 - the child keeps it
        try:
            self.proc = subprocess.Popen([sys.executable, "-m", "agentos.dev.slotnode", "serve"], env=env,
                                         stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         start_new_session=True)
        finally:
            out.close()
        self.starts += 1
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"the slot daemon exited with {self.proc.returncode}: {self.tail()}")
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", self.slot.mcp_port), 0.5):
                return
            time.sleep(0.2)
        raise RuntimeError(f"the slot daemon did not listen on {self.slot.mcp_port}: {self.tail()}")

    def stop(self, timeout: float = 90) -> int | None:
        """SIGTERM and wait: the graceful shutdown systemd does."""
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        code, self.proc = self.proc.returncode, None
        return code

    def kill(self) -> None:
        """SIGKILL agentosd alone, as a crash or the OOM killer would: its children are left."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self.proc = None

    def tail(self, n: int = 4000) -> str:
        path = self.home / "daemon.out"
        return path.read_text(errors="replace")[-n:] if path.exists() else ""

    def log_text(self) -> str:
        """Everything agentosd wrote: its JSON log files and its stdout and stderr."""
        parts = [p.read_text(errors="replace") for p in sorted((self.home / "logs").glob("*"))
                 if p.is_file()] if (self.home / "logs").exists() else []
        out = self.home / "daemon.out"
        if out.exists():
            parts.append(out.read_text(errors="replace"))
        return "\n".join(parts)

    def secrets(self) -> list[str]:
        return [self.token, self.proxy.password, self.proxy.auth.split(" ", 1)[1], self.agent_token]


def _run_in_thread(make_coro) -> None:
    """asyncio.run in a thread of its own, so a caller inside an event loop can use it too."""
    errors: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(make_coro())
        except BaseException as e:  # noqa: BLE001 - raised again in the caller
            errors.append(e)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]


def _dump_on_sigusr1(home: Path) -> None:
    """Trace Python allocations; each SIGUSR1 writes a snapshot to home/tracemalloc-<n>.snap."""
    import gc
    import tracemalloc

    tracemalloc.start(12)
    count = [0]

    def dump(signum: int, frame: Any) -> None:
        gc.collect()
        count[0] += 1
        tracemalloc.take_snapshot().dump(str(home / f"tracemalloc-{count[0]}.snap"))

    signal.signal(signal.SIGUSR1, dump)


def serve_main() -> None:
    """`python -m agentos.dev.slotnode serve`: agentosd for AGENTOS_HOME with the two seams above."""
    from agentos.core import logs
    from agentos.core.config import home_from_env, load_config
    from agentos.node import daemon

    cfg = load_config(home_from_env())
    harness = json.loads((cfg.home / HARNESS_FILE).read_text())
    logs.setup(cfg.home)

    async def node_ip() -> str:
        return harness["node_ip"]

    def lanes_factory(config):
        from agentos.node.lanes import build_lane_manager

        mgr = build_lane_manager(config, node_ip_probe=node_ip, self_check_url=harness["self_check_url"])
        mgr.engine.extra_options = {**mgr.engine.extra_options, "geoip": False}
        return mgr

    daemon._build_lane_manager = lanes_factory
    if os.environ.get("AGENTOS_SOAK_TRACEMALLOC") == "1":
        _dump_on_sigusr1(cfg.home)
    daemon.install_log_scrubbers(cfg)
    asyncio.run(daemon.serve(cfg))


# ---- MCP ----------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def mcp_client(url: str, token: str, *, timeout: float = 180) -> AsyncIterator[Any]:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=timeout) as http,
        Client(streamable_http_client(url, http_client=http), mode="legacy") as client,
    ):
        yield client


def text_of(result: Any) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def json_of(result: Any) -> Any:
    text = text_of(result)
    try:
        return json.loads(text)
    except ValueError:
        first = text.split("\n", 1)[0]
        try:
            return json.loads(first)
        except ValueError:
            return {"text": text}


async def timed(client: Any, tool: str, args: dict[str, Any]) -> tuple[Any, float]:
    t0 = time.perf_counter()
    result = await client.call_tool(tool, args)
    return result, time.perf_counter() - t0


async def call_ok(client: Any, tool: str, args: dict[str, Any]) -> Any:
    result = await client.call_tool(tool, args)
    if result.is_error:
        raise AssertionError(f"{tool} {args} failed: {text_of(result)[:600]}")
    return result


def lanes_by_id(result: Any) -> dict[str, dict[str, Any]]:
    data = json_of(result)
    items = data.get("lanes", data) if isinstance(data, dict) else data
    return {item["id"]: item for item in items} if isinstance(items, list) else {}


# ---- what the slot holds (Linux /proc) -----------------------------------------------------


def _status(pid: int) -> dict[str, str]:
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return {}
    return dict(line.split(":", 1) for line in text.splitlines() if ":" in line)


def rss_kb(pid: int) -> int:
    value = _status(pid).get("VmRSS", "0 kB").split()[0]
    return int(value) if value.isdigit() else 0


def pss_kb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def fd_count(pid: int) -> int:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return 0


def tree_memory_kb(main_pids: Iterable[int]) -> tuple[int, int, int]:
    """(RSS, PSS, processes) of each browser's whole process group (content processes share it)."""
    from agentos.node.display import process_group

    pids: set[int] = set()
    for pid in main_pids:
        try:
            pids.update(process_group(os.getpgid(pid)))
        except OSError:
            continue
    return sum(rss_kb(p) for p in pids), sum(pss_kb(p) for p in pids), len(pids)


def browsers_under(home: Path) -> list[int]:
    from agentos.node.engine_camoufox import browser_pids_under

    return browser_pids_under(home)


def main_browsers_under(home: Path) -> list[int]:
    from agentos.node.display import iter_processes

    pids = set(browsers_under(home))
    return [pid for pid, argv in iter_processes() if pid in pids and "-contentproc" not in argv]


def slot_display_processes(slot: Slot) -> list[tuple[int, str]]:
    """Xvfb and x11vnc on the slot's displays."""
    from agentos.node.display import iter_processes

    names = {f":{n}" for n in slot.displays}
    out = []
    for pid, argv in iter_processes():
        base = os.path.basename(argv[0]) if argv else ""
        if base in ("Xvfb", "x11vnc") and names.intersection(argv):
            out.append((pid, " ".join(argv[:3])))
    return out


def slot_display_files(slot: Slot) -> list[str]:
    return [p for n in slot.displays for p in (f"/tmp/.X11-unix/X{n}", f"/tmp/.X{n}-lock") if os.path.exists(p)]


def slot_listeners(slot: Slot) -> set[int]:
    from agentos.node.display import listening_ports

    return (listening_ports() or set()) & slot.lane_ports


@dataclass
class Footprint:
    """What the slot holds at one moment."""

    at: float
    daemon_rss_kb: int
    daemon_fds: int
    browsers: int
    display_procs: int
    display_files: int
    ports: int
    warm_rss_kb: int

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def footprint(daemon: SlotDaemon) -> Footprint:
    pid = daemon.pid
    browsers = main_browsers_under(daemon.home)
    return Footprint(at=time.time(), daemon_rss_kb=rss_kb(pid) if pid else 0, daemon_fds=fd_count(pid) if pid else 0,
                     browsers=len(browsers_under(daemon.home)), display_procs=len(slot_display_processes(daemon.slot)),
                     display_files=len(slot_display_files(daemon.slot)), ports=len(slot_listeners(daemon.slot)),
                     warm_rss_kb=tree_memory_kb(browsers)[0])


def leftovers(daemon: SlotDaemon) -> dict[str, Any]:
    """Anything of the slot's still running or held: empty when the slot is clean."""
    out: dict[str, Any] = {}
    if pids := browsers_under(daemon.home):
        out["browsers"] = pids
    if procs := slot_display_processes(daemon.slot):
        out["display_processes"] = procs
    if files := slot_display_files(daemon.slot):
        out["display_files"] = files
    if ports := slot_listeners(daemon.slot):
        out["ports"] = sorted(ports)
    return out


async def clean_slot(daemon: SlotDaemon) -> None:
    """Kill what this harness's daemon left (only processes whose profile is under its home, or
    Xvfb/x11vnc on the slot's own displays), then remove stale display files of the slot."""
    from agentos.node.display import kill_processes

    pids = browsers_under(daemon.home) + [pid for pid, _ in slot_display_processes(daemon.slot)]
    if pids:
        await kill_processes(pids)
    for path in slot_display_files(daemon.slot):
        with contextlib.suppress(OSError):
            os.unlink(path)


if __name__ == "__main__":
    if sys.argv[1:] != ["serve"]:
        raise SystemExit("usage: python -m agentos.dev.slotnode serve")
    serve_main()
