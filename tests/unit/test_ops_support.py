"""Fakes shared by the ops tests: a Host with canned answers and a small OPENAJAR_HOME.

Imported by the other test_ops_* modules; it holds no tests of its own except a sanity check
of the fakes.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from openajar.core.config import NodeConfig, save_config, save_proxies
from openajar.core.models import LaneSpec, ProxyConfig
from openajar.core.system import Host

TOKEN = "tok-SECRET-mcp-0123456789abcdef"
PROXY_USER = "puser-SECRET"
PROXY_PASS = "ppass-SECRET-xyz"
PASSPHRASE = "correct horse battery staple"
UA = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:{v}.0) Gecko/20100101 Firefox/{v}.0"
GiB = 1024**3


def fingerprint(version: int = 158, **extra: Any) -> dict[str, Any]:
    return {"navigator.userAgent": UA.format(v=version), "navigator.appVersion": "5.0 (X11; Ubuntu)",
            "navigator.platform": "Linux x86_64", "navigator.oscpu": "Linux x86_64",
            "navigator.hardwareConcurrency": 8, "screen.width": 1920, "screen.height": 1080,
            "fonts": ["Arial", "DejaVu Sans"], "webGl:renderer": "ANGLE (Intel)", "timezone": "Europe/Vilnius",
            **extra}


def make_home(root: Path, *, lanes: dict[str, int | None] | None = None, proxies: bool = True,
              name: str = "test-node") -> NodeConfig:
    """A home with node.toml, a proxy, openajar.db (WAL) with lane rows, and lane directories.
    `lanes` maps lane id -> pinned Firefox version (None: not pinned yet)."""
    home = root / "home"
    home.mkdir(mode=0o700, parents=True)
    cfg = NodeConfig(home=home, name=name, mcp_token=TOKEN, mcp_port=7510, display_base=210, vnc_base=7512,
                     viewer_base=7515, max_slots=3, max_warm_lanes=2)
    save_config(cfg)
    if proxies:
        save_proxies(cfg, {"lt1": ProxyConfig(label="lt1", host="203.0.113.7", port=12323, username=PROXY_USER,
                                              password=PROXY_PASS)})
    lanes = {"personal/reddit": 158, "work/site": None} if lanes is None else lanes
    db = sqlite3.connect(home / "openajar.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS lanes (id TEXT PRIMARY KEY, spec TEXT NOT NULL, last_used TEXT, "
               "exit_ip TEXT)")
    for lane_id, version in lanes.items():
        spec = LaneSpec(id=lane_id, proxy_label="lt1" if proxies else None)
        db.execute("INSERT INTO lanes (id, spec) VALUES (?, ?)", (lane_id, spec.model_dump_json()))
        profile = cfg.profile_dir(lane_id)
        profile.mkdir(parents=True, mode=0o700)
        for d in (profile.parent.parent, profile.parent, profile):
            d.chmod(0o700)
        (profile / "cookies.sqlite").write_bytes(b"cookie-db-" + lane_id.encode() + os.urandom(64))
        (profile / "prefs.js").write_text('user_pref("a", 1);\n')
        (profile / "cache2").mkdir()
        (profile / "cache2" / "entry").write_bytes(b"cached")
        (profile / "storage" / "default").mkdir(parents=True)
        (profile / "storage" / "default" / "ls.sqlite").write_bytes(b"local-storage")
        for p in profile.rglob("*"):
            p.chmod(0o700 if p.is_dir() else 0o600)
        if version is not None:
            path = cfg.fingerprint_path(lane_id)
            path.write_text(json.dumps(fingerprint(version), indent=1))
            path.chmod(0o600)
    db.commit()
    db.close()
    (home / "openajar.db").chmod(0o600)
    (home / "lanes").mkdir(mode=0o700, exist_ok=True)
    (home / "lanes").chmod(0o700)
    return cfg


class FakeHost(Host):
    """A machine where everything is healthy unless a test says otherwise."""

    def __init__(self) -> None:
        self.platform_info = {"system": "Linux", "machine": "x86_64", "release": "6.8", "id": "ubuntu",
                              "version_id": "24.04", "pretty_name": "Ubuntu 24.04.4 LTS"}
        self.py = {"version": (3, 12, 3), "prefix": "/opt/agentos/.venv", "base_prefix": "/usr",
                   "executable": "/opt/agentos/.venv/bin/python"}
        self.packages: dict[str, bool] | None = None  # None: everything installed
        self.binaries = {"systemctl", "timedatectl", "dpkg-query", "Xvfb", "x11vnc", "websockify", "bwrap"}
        self.camoufox_info: dict[str, str] = {"path": "/cache/camoufox/158", "verstr": "158.0.1-beta.41"}
        self.fpgen_info: dict[str, Any] = {"ok": True, "dir": "/opt/agentos/.venv/lib/fpgen/data"}
        self.fpgen_users: list[str | None] = []
        self.fstypes: dict[str, str] = {}
        self.commands: dict[str, tuple[int, str]] = {
            "systemctl show": (0, ("LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\n"
                                   "User=agentos\nMainPID=42\nNRestarts=0\nExecMainStartTimestamp=Tue 2026-11-03\n")),
            "systemctl is-active": (0, "active\n"),
            "timedatectl show": (0, "NTP=yes\nNTPSynchronized=yes\nTimezone=Etc/UTC\n"),
        }
        self.mem: dict[str, int] | None = {"MemTotal": 4 * GiB, "MemAvailable": 3 * GiB, "SwapTotal": 0,
                                           "SwapFree": 0}
        self.disk: dict[str, tuple[int, int, int]] = {}
        self.default_disk = (40 * GiB, 10 * GiB, 30 * GiB)
        self.ports: set[int] = {7510}
        self.displays: set[int] = set()
        self.procs: list[tuple[int, list[str]]] = []
        self.browsers: dict[str, list[int]] = {}  # profile path -> pids
        self.exit_ips: dict[str, str | Exception] = {}  # proxy host -> exit IP or error
        self.exit_geo: dict[str, dict[str, str] | Exception] = {}  # proxy host -> ipinfo's JSON answer
        self.exit_info_calls: list[str] = []
        self.node_ip: str | Exception = "192.0.2.10"
        self.environs: dict[int, bytes] = {}
        self.cgroups: dict[int, str] = {}  # pid -> its cgroup v2 path
        self.tcp_errors: dict[str, Exception] = {}
        self.dns: dict[str, set[str]] = {}
        self.http_calls: list[tuple[str, str, dict[str, str]]] = []
        self.http_answers = {"anon": 401, "authed": 200}
        self.sockets: dict[str, dict[str, Any] | None] = {}
        self.euid_value = os.geteuid()
        self.now_value: float | None = None
        self.same_fs = True

    def platform(self) -> dict[str, str]:
        return dict(self.platform_info)

    def python(self) -> dict[str, Any]:
        return dict(self.py)

    def euid(self) -> int:
        return self.euid_value

    def hostname(self) -> str:
        return "fake-host"

    def now(self) -> float:
        return self.now_value if self.now_value is not None else super().now()

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.binaries else None

    def run(self, argv: list[str], timeout: float = 10.0, env: dict[str, str] | None = None) -> tuple[int, str]:
        return self.commands.get(" ".join(argv[:2]), (127, ""))

    def dpkg_installed(self, packages: list[str]) -> dict[str, bool] | None:
        if self.packages is None:
            return {p: True for p in packages}
        return {p: self.packages.get(p, True) for p in packages}

    def camoufox(self, cache_home: Path | None = None) -> dict[str, str]:
        return dict(self.camoufox_info)

    def fpgen_model(self, user: str | None = None) -> dict[str, Any]:
        self.fpgen_users.append(user)
        return dict(self.fpgen_info)

    def fstype(self, path: Path) -> str | None:
        return self.fstypes.get(str(path))

    def meminfo(self) -> dict[str, int] | None:
        return self.mem

    def disk_usage(self, path: Path) -> tuple[int, int, int] | None:
        return self.disk.get(str(path), self.default_disk)

    def same_filesystem(self, a: Path, b: Path) -> bool:
        return self.same_fs

    def processes(self) -> list[tuple[int, list[str]]]:
        return list(self.procs)

    def browser_pids(self, profile: Path) -> list[int]:
        return list(self.browsers.get(str(profile), []))

    def cgroup(self, pid: int) -> str | None:
        return self.cgroups.get(pid)

    def port_in_use(self, port: int) -> bool:
        return port in self.ports

    def display_in_use(self, num: int) -> bool:
        return num in self.displays

    def resolve(self, host: str) -> set[str]:
        return self.dns.get(host, {host})

    def tcp_connect(self, host: str, port: int, timeout: float = 5.0) -> float:
        if host in self.tcp_errors:
            raise self.tcp_errors[host]
        return 0.02

    def proxy_exit_ip(self, proxy: ProxyConfig, *, timeout: float = 15.0) -> str:
        answer = self.exit_ips.get(proxy.host, proxy.host)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def proxy_exit_info(self, proxy: ProxyConfig, *, timeout: float = 15.0) -> dict[str, str]:
        self.exit_info_calls.append(proxy.label)
        ip = self.proxy_exit_ip(proxy, timeout=timeout)
        answer = self.exit_geo.get(proxy.host, {"country": "LT", "city": "Vilnius", "timezone": "Europe/Vilnius"})
        if isinstance(answer, Exception):
            raise answer
        return {"ip": ip, **answer}

    def direct_exit_ip(self, *, timeout: float = 10.0) -> str:
        if isinstance(self.node_ip, Exception):
            raise self.node_ip
        return self.node_ip

    def environ(self, pid: int) -> bytes | None:
        return self.environs.get(pid)

    def http_request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                     body: bytes | None = None, timeout: float = 10.0) -> tuple[int, dict[str, str], bytes]:
        headers = headers or {}
        self.http_calls.append((method, url, dict(headers)))
        if method == "DELETE":
            return 200, {}, b""
        if "Authorization" in headers:
            return self.http_answers["authed"], {"mcp-session-id": "s-1"}, b"{}"
        return self.http_answers["anon"], {}, b""

    def unix_request(self, path: Path, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any] | None:
        return self.sockets.get(Path(path).name)


def test_fakes_are_sane(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    assert (cfg.home / "node.toml").stat().st_mode & 0o777 == 0o600
    assert json.loads(cfg.fingerprint_path("personal/reddit").read_text())["navigator.userAgent"].endswith("158.0")
    assert not cfg.fingerprint_path("work/site").exists()
    host = FakeHost()
    assert host.run(["systemctl", "show", "agentosd"])[0] == 0
    assert host.which("nope") is None
