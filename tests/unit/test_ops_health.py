"""Health: watermarks, drift, proxies, exit-IP quarantine, notifications, the service and its socket."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from test_ops_support import FakeHost, GiB, fingerprint, make_home

from openajar.core.models import LaneInfo, LaneSpec, LaneState
from openajar.core.runtime import Runtime
from openajar.core.system import Host
from openajar.ops import health
from openajar.ops.backup import DaemonCoordinator
from openajar.ops.health import CRITICAL, OK, WARN, HealthMonitor
from openajar.ops.service import HealthService, build


class Lanes:
    """A lane manager with warm lanes on proxy lt1; quarantine() only when asked for."""

    def __init__(self, warm: dict[str, str | None], *, with_quarantine: bool = True, probe: dict | None = None):
        self.infos = {lid: LaneInfo(spec=LaneSpec(id=lid, proxy_label=proxy), state=LaneState.WARM)
                      for lid, proxy in warm.items()}
        self.calls: list[tuple[str, str]] = []
        if with_quarantine:
            self.quarantine = self._quarantine
        if probe is not None:
            async def probe_exit_ip(lane_id: str) -> str | None:
                return probe.get(lane_id)
            self.probe_exit_ip = probe_exit_ip

    def list(self):
        return list(self.infos.values())

    async def _quarantine(self, lane_id: str, reason: str) -> None:
        self.calls.append(("quarantine", lane_id))
        self.infos[lane_id].state = LaneState.QUARANTINED

    async def close(self, lane_id: str):
        self.calls.append(("close", lane_id))
        self.infos[lane_id].state = LaneState.COLD
        return self.infos[lane_id]

    async def open(self, lane_id: str):
        self.calls.append(("open", lane_id))
        self.infos[lane_id].state = LaneState.WARM
        return self.infos[lane_id]


class Notifier:
    def __init__(self):
        self.events: list[dict] = []

    def send(self, event: dict) -> list[int]:
        self.events.append(event)
        return [1]


def monitor(tmp_path, *, lanes=None, host=None, lanes_spec=None, notifier=None):
    cfg = make_home(tmp_path, lanes=lanes_spec)
    host = host or FakeHost()
    return HealthMonitor(cfg, host=host, lanes=lanes, notifier=notifier or Notifier()), host


def test_levels():
    assert health.disk_level(30 * GiB, 40 * GiB) == OK
    assert health.disk_level(3 * GiB, 40 * GiB) == WARN
    assert health.disk_level(int(0.5 * GiB), 400 * GiB) == CRITICAL
    assert health.disk_level(20 * GiB, 1000 * GiB) == CRITICAL  # 2% free
    assert health.memory_level({"MemTotal": 100, "MemAvailable": 50})[0] == OK
    assert health.memory_level({"MemTotal": 100, "MemAvailable": 10})[0] == WARN
    assert health.memory_level({"MemTotal": 100, "MemAvailable": 5})[0] == CRITICAL
    assert health.memory_level({"MemTotal": 100, "MemAvailable": 50, "SwapTotal": 10, "SwapFree": 2})[0] == WARN


def test_one_meminfo_reader_for_every_caller(tmp_path):
    """openajar.core.system.read_meminfo: bytes for the health service and doctor, MB for the rest,
    None where there is no /proc (a Mac)."""
    from openajar.core.system import meminfo_mb, read_meminfo

    path = tmp_path / "meminfo"
    path.write_text("MemTotal:        3984588 kB\nMemAvailable:    1048576 kB\nHugePages_Total:       0\n")
    assert read_meminfo(path) == {"MemTotal": 3984588 * 1024, "MemAvailable": GiB,
                                  "HugePages_Total": 0}
    assert meminfo_mb("MemTotal", path) == 3891 and meminfo_mb("MemAvailable", path) == 1024
    assert meminfo_mb("SwapTotal", path) is None
    assert read_meminfo(tmp_path / "nothing") is None and meminfo_mb("MemTotal", tmp_path / "nothing") is None


async def test_healthy_round_sends_nothing(tmp_path):
    m, _ = monitor(tmp_path, lanes=Lanes({"personal/reddit": "lt1"}))
    report = await m.run_once()
    assert report["level"] == OK
    kinds = {f["kind"] for f in report["findings"]}
    assert {"disk", "memory", "drift", "proxy", "exit_ip"} <= kinds
    assert m.notifier.events == []


async def test_proxy_down_notifies_once_then_on_recovery(tmp_path):
    m, host = monitor(tmp_path)
    await m.run_once()
    host.tcp_errors["203.0.113.7"] = ConnectionRefusedError("refused")
    r1 = await m.run_once()
    r2 = await m.run_once()
    assert r1["level"] == r2["level"] == CRITICAL
    downs = [e for e in m.notifier.events if e["type"] == "health.proxy"]
    assert len(downs) == 1 and "Proxy lt1 is down" in downs[0]["title"] and downs[0]["severity"] == CRITICAL
    del host.tcp_errors["203.0.113.7"]
    await m.run_once()
    assert m.notifier.events[-1]["type"] == "health.recovered"


async def test_exit_ip_mismatch_quarantines_the_warm_lane(tmp_path):
    lanes = Lanes({"personal/reddit": "lt1", "work/site": None})
    m, host = monitor(tmp_path, lanes=lanes)
    host.exit_ips["203.0.113.7"] = "198.51.100.1"
    report = await m.run_once()
    assert lanes.calls == [("quarantine", "personal/reddit")]
    finding = next(f for f in report["findings"] if f["key"] == "exit_ip.personal/reddit")
    assert finding["level"] == CRITICAL and finding["data"]["action"] == "quarantined"
    assert "personal/reddit" in report["quarantined"]
    event = next(e for e in m.notifier.events if e["type"] == "health.exit_ip")
    assert event["lane"] == "personal/reddit" and "198.51.100.1" in event["body"]


async def test_without_a_quarantine_method_the_lane_is_sent_cold(tmp_path):
    lanes = Lanes({"personal/reddit": "lt1"}, with_quarantine=False)
    m, host = monitor(tmp_path, lanes=lanes)
    host.exit_ips["203.0.113.7"] = "198.51.100.1"
    report = await m.run_once()
    assert lanes.calls == [("close", "personal/reddit")]
    finding = next(f for f in report["findings"] if f["kind"] == "exit_ip")
    assert "sent cold" in finding["data"]["action"]


async def test_probe_in_the_lane_wins_over_the_proxy_view(tmp_path):
    lanes = Lanes({"personal/reddit": "lt1"}, probe={"personal/reddit": "198.51.100.77"})
    m, _ = monitor(tmp_path, lanes=lanes)
    report = await m.run_once(proxies=False)
    finding = next(f for f in report["findings"] if f["kind"] == "exit_ip")
    assert finding["level"] == CRITICAL and "via the lane" in finding["detail"]
    assert lanes.calls == [("quarantine", "personal/reddit")]


async def test_a_dead_proxy_never_quarantines(tmp_path):
    lanes = Lanes({"personal/reddit": "lt1"})
    m, host = monitor(tmp_path, lanes=lanes)
    host.exit_ips["203.0.113.7"] = OSError("timed out")
    report = await m.run_once()
    assert lanes.calls == []
    assert any(f["kind"] == "proxy" and f["level"] == CRITICAL for f in report["findings"])


async def test_drift_warning(tmp_path):
    m, _ = monitor(tmp_path, lanes_spec={"a/old": 150, "a/same": 158})
    report = await m.run_once(proxies=False, lanes=False)
    drift = {f["key"]: f for f in report["findings"] if f["kind"] == "drift"}
    assert drift["drift.a/old"]["level"] == WARN and "refresh-fingerprint a/old --keep-device" in \
        drift["drift.a/old"]["detail"]
    assert drift["drift.a/same"]["level"] == OK
    assert [e["key"] for e in m.notifier.events] == ["drift.a/old"]


async def test_installed_version_is_cached(tmp_path, monkeypatch):
    m, host = monitor(tmp_path)
    calls = []
    monkeypatch.setattr(host, "camoufox", lambda cache=None: calls.append(1) or {"verstr": "158.0.1"})
    await m.run_once(proxies=False)
    await m.run_once(proxies=False)
    assert calls == [1]


async def test_disk_and_memory_watermarks(tmp_path):
    host = FakeHost()
    host.default_disk = (40 * GiB, 39 * GiB, int(0.4 * GiB))
    host.mem = {"MemTotal": 4 * GiB, "MemAvailable": int(0.2 * GiB)}
    m, _ = monitor(tmp_path, host=host)
    report = await m.run_once(proxies=False)
    levels = {f["kind"]: f["level"] for f in report["findings"] if f["kind"] in ("disk", "memory")}
    assert levels == {"disk": CRITICAL, "memory": CRITICAL}
    assert {e["type"] for e in m.notifier.events} >= {"health.disk", "health.memory"}


async def test_a_memory_warning_names_the_biggest_lane(tmp_path):
    """The node's one low-memory warning (the memory watchdog has none of its own) says which lane
    to close first, from what the watchdog saw at its last look."""
    lanes = Lanes({"a/one": None, "a/two": None})
    lanes.memory_status = lambda: {"lanes_mb": {"a/one": 900, "a/two": 1200}}
    host = FakeHost()
    host.mem = {"MemTotal": 4 * GiB, "MemAvailable": int(0.4 * GiB)}
    m, _ = monitor(tmp_path, host=host, lanes=lanes)
    [memory] = m.check_memory()
    assert memory.level == WARN and memory.detail.endswith("; the biggest lane is a/two at 1200 MB")
    assert memory.data["biggest_lane"] == "a/two" and "—" not in memory.detail
    host.mem = {"MemTotal": 4 * GiB, "MemAvailable": 2 * GiB}
    assert "biggest lane" not in m.check_memory()[0].detail  # nothing to say while memory is fine


async def test_async_and_broken_notifiers(tmp_path):
    class Async:
        def __init__(self):
            self.events = []

        async def send(self, event):
            self.events.append(event)

    class Broken:
        def send(self, event):
            raise RuntimeError("down")

    host = FakeHost()
    host.tcp_errors["203.0.113.7"] = OSError("x")
    good = Async()
    for notifier in (good, Broken()):
        m, _ = monitor(tmp_path / type(notifier).__name__, host=host, notifier=notifier)
        report = await m.run_once()
        assert report["level"] == CRITICAL  # a broken notifier never breaks the round
    assert [e["type"] for e in good.events] == ["health.proxy"]


# ---- the service and its socket -----------------------------------------------------

@pytest.fixture
def short_sock():
    d = tempfile.mkdtemp(prefix="ops", dir="/tmp")
    yield Path(d) / "ops.sock"
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def test_build_registers_health(tmp_path):
    cfg = make_home(tmp_path)
    rt = Runtime(config=cfg)
    service = build(rt)
    assert rt.get("health") is service and isinstance(service, HealthService)


async def ask(path: Path, payload: dict):
    return await asyncio.to_thread(Host().unix_request, path, payload, 10)


async def test_control_socket_ops(tmp_path, short_sock):
    cfg = make_home(tmp_path)
    lanes = Lanes({"personal/reddit": "lt1", "work/site": "lt1"})
    rt = Runtime(config=cfg, lanes=lanes, notifier=Notifier())
    service = HealthService(rt, socket_path=short_sock, first_delay=3600, host=FakeHost())
    await service.start()
    try:
        assert short_sock.stat().st_mode & 0o777 == 0o600
        assert await ask(short_sock, {"op": "ping"}) == {"ok": True}
        listed = await ask(short_sock, {"op": "lanes"})
        assert {lane["id"]: lane["state"] for lane in listed["lanes"]} == {"personal/reddit": "warm",
                                                                         "work/site": "warm"}
        assert (await ask(short_sock, {"op": "close", "lane": "personal/reddit"}))["state"] == "cold"
        assert (await ask(short_sock, {"op": "open", "lane": "personal/reddit"}))["ok"]
        await asyncio.sleep(0.05)
        assert lanes.infos["personal/reddit"].state == LaneState.WARM
        drained = await ask(short_sock, {"op": "drain", "timeout": 5})
        assert drained["closed"] == ["personal/reddit", "work/site"] and drained["failed"] == {}
        status = await ask(short_sock, {"op": "check"})
        assert status["ok"] and status["status"]["level"] in (OK, WARN, CRITICAL)
        assert (await ask(short_sock, {"op": "status"}))["status"]["at"] is not None
        bad = await ask(short_sock, {"op": "close", "lane": "../etc"})
        assert bad["ok"] is False and bad["error"]["code"] == "invalid"
        assert (await ask(short_sock, {"op": "nope"}))["error"]["code"] == "invalid"
    finally:
        await service.stop()
    assert not short_sock.exists()


async def test_daemon_coordinator_against_the_service(tmp_path, short_sock, monkeypatch):
    cfg = make_home(tmp_path)
    lanes = Lanes({"personal/reddit": "lt1"})
    service = HealthService(Runtime(config=cfg, lanes=lanes), socket_path=short_sock, first_delay=3600,
                            host=FakeHost())
    await service.start()
    try:
        coord = DaemonCoordinator(cfg.home)
        coord.path = short_sock
        assert await asyncio.to_thread(coord.available)
        assert await asyncio.to_thread(coord.warm) == {"personal/reddit"}
        await asyncio.to_thread(coord.close, "personal/reddit")
        assert lanes.infos["personal/reddit"].state == LaneState.COLD
    finally:
        await service.stop()


async def test_service_loop_runs_rounds_and_survives_errors(tmp_path):
    cfg = make_home(tmp_path)
    rt = Runtime(config=cfg)
    service = HealthService(rt, interval=0.01, first_delay=0, control=False, host=FakeHost())
    rounds = []
    original = service.monitor.run_once

    async def counting(**kw):
        rounds.append(kw)
        if len(rounds) == 1:
            raise RuntimeError("first round breaks")
        return await original(**kw)

    service.monitor.run_once = counting
    await service.start()
    await asyncio.sleep(0.2)
    await service.stop()
    assert len(rounds) >= 3
    assert rounds[0]["proxies"] is True and rounds[1]["proxies"] is False  # proxies every 5th round
    assert rounds[0]["lanes"] is False  # no lane manager registered


async def test_drain_without_lanes_is_a_clear_error(tmp_path, short_sock):
    cfg = make_home(tmp_path)
    service = HealthService(Runtime(config=cfg), socket_path=short_sock, first_delay=3600, host=FakeHost())
    await service.start()
    try:
        resp = await ask(short_sock, {"op": "drain"})
        assert resp["ok"] is False and "lane manager" in resp["error"]["message"]
    finally:
        await service.stop()


def test_status_before_any_round(tmp_path):
    service = HealthService(Runtime(config=make_home(tmp_path)), control=False)
    assert service.status()["level"] == "unknown"
    assert json.dumps(service.status())
    assert fingerprint(1)["navigator.userAgent"].endswith("Firefox/1.0")
