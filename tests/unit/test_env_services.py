import asyncio
import re
import shlex
import socket
import sys
from datetime import timedelta

import pytest

from agentos.core.errors import LaneBusy
from agentos.core.models import utcnow
from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher
from agentos.env.services import RotatingLog


def make(tmp_path, **settings):
    return EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                      settings=EnvSettings(proxy_port=0, **settings))


@pytest.fixture
async def mgr(tmp_path):
    m = make(tmp_path)
    yield m
    await m.shutdown()


def svc(mgr, lane_id, script, **cfg):
    return mgr.create(lane_id, kind="service", preset="service",
                      service={"command": ["/bin/sh", "-c", script], **cfg})


async def until(pred, timeout=8.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.05)


def test_rotating_log(tmp_path):
    log = RotatingLog(tmp_path / "service.log", max_bytes=1024, backups=2)
    for i in range(400):
        log.write(f"line {i}\n".encode())
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["service.log", "service.log.1", "service.log.2"]
    assert all(p.stat().st_size <= 1024 for p in tmp_path.iterdir())
    tail = log.tail(3).splitlines()
    assert tail == ["line 397", "line 398", "line 399"]


async def test_crash_loop_backs_off_exponentially(mgr):
    svc(mgr, "ws/crashy", "echo boom; exit 1", backoff_initial_s=0.1, backoff_max_s=0.4, stable_after_s=60)
    await mgr.service_start("ws/crashy")
    await until(lambda: mgr.services["ws/crashy"].status.restarts >= 5)
    await mgr.service_stop("ws/crashy")
    log = mgr.service_logs("ws/crashy", 200)
    delays = [float(x) for x in re.findall(r"restarting in ([0-9.]+)s", log)]
    assert delays[:4] == [0.1, 0.2, 0.4, 0.4]
    assert "boom" in log and "exited with 1" in log
    st = mgr.service_status("ws/crashy")
    assert st.desired == "stopped" and st.state == "stopped"


async def test_gives_up_after_max_restarts(mgr):
    svc(mgr, "ws/flaky", "exit 2", backoff_initial_s=0.05, max_restarts=2)
    await mgr.service_start("ws/flaky")
    await until(lambda: mgr.services["ws/flaky"].status.state == "failed")
    assert "gave up" in mgr.service_status("ws/flaky").detail


async def test_failing_health_check_restarts_the_service(mgr):
    svc(mgr, "ws/sick", "echo up; sleep 30", backoff_initial_s=0.05,
        health={"cmd": ["/bin/sh", "-c", "exit 1"], "interval_s": 0.1, "start_period_s": 0.1, "retries": 2})
    await mgr.service_start("ws/sick")
    await until(lambda: mgr.services["ws/sick"].status.restarts >= 1)
    assert "health check failed" in mgr.service_logs("ws/sick")
    await mgr.service_stop("ws/sick")


async def test_http_health_probe_inside_the_lane(mgr):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    # The test's own Python and a plain TCPServer: `python3 -m http.server` looks up the name of
    # 127.0.0.1 (socket.getfqdn) before it listens, and on GitHub's macOS runners that lookup takes
    # about 35 s, so the probe failed three times and the service restarted, again and again.
    serve = ("import http.server, socketserver, sys; socketserver.TCPServer(('127.0.0.1', int(sys.argv[1])), "
             "http.server.SimpleHTTPRequestHandler).serve_forever()")
    svc(mgr, "ws/web", f"exec {shlex.quote(sys.executable)} -c {shlex.quote(serve)} {port}",
        health={"http": f"http://127.0.0.1:{port}/", "interval_s": 0.2, "start_period_s": 0.3})
    st = await mgr.service_start("ws/web")
    assert st.desired == "running"
    try:  # a loaded CI runner can take a while to start Python
        await until(lambda: mgr.services["ws/web"].status.health == "healthy", timeout=30.0)
    except AssertionError:
        status = mgr.services["ws/web"].status
        raise AssertionError(f"not healthy: {status!r}\nservice log:\n{mgr.service_logs('ws/web', 40)}") from None
    assert mgr.state("ws/web").value == "active"
    st = await mgr.service_stop("ws/web")
    assert st.state == "stopped" and st.pid is None


async def test_ttl_expiry_and_restart_on_start(tmp_path):
    m = make(tmp_path)
    svc(m, "ws/keep", "sleep 30")
    await m.service_start("ws/keep", ttl_s=3600)
    await until(lambda: m.services["ws/keep"].status.state == "running")
    await m.shutdown()                          # node restart: desired state survives
    m2 = make(tmp_path)
    await m2.start()
    await until(lambda: "ws/keep" in m2.services and m2.services["ws/keep"].status.state == "running")
    later = utcnow() + timedelta(hours=2)
    m2.services["ws/keep"].status.expires_at = utcnow() - timedelta(seconds=1)
    res = await m2.reap_once(now=later)
    assert res["services_expired"] == ["ws/keep"]
    assert m2.store.service_desired("ws/keep")[0] == "stopped"
    await m2.shutdown()


async def test_service_budget_is_separate_and_enforced(tmp_path):
    m = make(tmp_path, service_budget_mb=600)
    svc(m, "ws/a", "sleep 30")
    svc(m, "ws/b", "sleep 30")
    await m.service_start("ws/a")
    await until(lambda: m.services["ws/a"].running)
    with pytest.raises(LaneBusy, match="service memory budget"):
        await m.service_start("ws/b")
    await m.shutdown()
