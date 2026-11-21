# ruff: noqa: F811  (pytest fixtures imported from test_env_node_support)
import asyncio
import re
import shutil
import socket
import subprocess
import sys
import textwrap
from datetime import timedelta
from pathlib import Path

import pytest
from test_env_node_support import leftovers, mgr, needs_node, new_home, until  # noqa: F401

from openajar.core.models import utcnow
from openajar.env.manager import EnvManager

pytestmark = [pytest.mark.node, needs_node]

PROBE_PORT = 7455


async def test_orphans_are_cleaned_after_a_crash():
    home = new_home("crash")
    crash = textwrap.dedent(f"""
        import asyncio, os, subprocess
        from pathlib import Path
        from openajar.env.manager import EnvManager
        async def main():
            m = EnvManager(Path({str(home)!r}))
            await m.start()
            m.create("c/eph")
            m.create("c/disk", overrides={{"filesystem": "overlay", "disk_mb": 64}})
            m.create("c/net", overrides={{"network": "isolated"}})
            for lane in ("c/eph", "c/disk", "c/net"):
                await m.warm(lane)
            await m.shell_open("c/eph", term="dumb")
            asyncio.create_task(m.exec("c/disk", "sleep 300"))
            # a process that escapes PDEATHSIG, still inside the lane cgroup
            rt = m._rts["c/net"]
            cg = m.host.cgroup_workload(rt.lane.key, "stray")
            subprocess.Popen(["sh", "-c", f"echo $$ > {{cg}}/cgroup.procs; exec sleep 300"], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.sleep(1.0)
            print("READY", flush=True)
            os._exit(9)
        asyncio.run(main())
    """)
    p = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", crash], capture_output=True, text=True,
                                timeout=120, check=False)
    assert "READY" in p.stdout, p.stderr[-2000:]
    m = EnvManager(home)
    assert m.host.mounts_under(home / "run"), "the crash should have left mounts behind"
    await m.start()
    assert m.last_sweep["mounts"] >= 5 and m.last_sweep["cgroups"] >= 1
    assert not m.host.mounts_under(home)
    assert not list((home / "run" / "env").iterdir())
    await asyncio.sleep(0.5)
    ps = await asyncio.to_thread(subprocess.run, ["pgrep", "-f", "sleep 300"], capture_output=True, text=True,
                                 check=False)
    for pid in ps.stdout.split():
        assert str(home) not in Path(f"/proc/{pid}/cgroup").read_text(errors="replace")
    r = await m.exec("c/disk", "echo back")
    assert r.stdout == "back\n"
    await m.shutdown()
    left = leftovers(home, m)
    shutil.rmtree(home, ignore_errors=True)
    assert not left, left


async def test_idle_lanes_go_cold_and_unmount(mgr):
    mgr.create("lc/idle")
    await mgr.exec("lc/idle", "true")
    assert mgr.host.mounts_under(mgr.paths.run)
    res = await mgr.reap_once(now=utcnow() + timedelta(seconds=mgr.settings.lane_idle_s + 1))
    assert res["lanes_cooled"] == ["lc/idle"]
    assert not mgr.host.mounts_under(mgr.paths.run) and mgr.state("lc/idle").value == "cold"


async def test_service_crash_loop_backs_off(mgr):
    mgr.create("lc/crashy", kind="service", preset="service",
               service={"command": ["/bin/sh", "-c", "echo crashing; exit 3"], "backoff_initial_s": 0.2,
                        "backoff_max_s": 0.8, "stable_after_s": 60})
    await mgr.service_start("lc/crashy")
    await until(lambda: mgr.services["lc/crashy"].status.restarts >= 5, timeout=20)
    await mgr.service_stop("lc/crashy")
    delays = [float(x) for x in re.findall(r"restarting in ([0-9.]+)s", mgr.service_logs("lc/crashy", 500))]
    assert delays[:5] == [0.2, 0.4, 0.8, 0.8, 0.8]


async def test_service_listens_only_inside_its_lane_and_passes_http_health(mgr):
    mgr.create("lc/web", kind="service", preset="service",
               service={"command": ["python3", "-m", "http.server", str(PROBE_PORT), "--bind", "127.0.0.1",
                                    "--directory", "/usr/share"],
                        "health": {"http": f"http://127.0.0.1:{PROBE_PORT}/", "interval_s": 0.5,
                                   "start_period_s": 0.5}})
    await mgr.service_start("lc/web")
    await until(lambda: mgr.services["lc/web"].status.health == "healthy", timeout=20)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", PROBE_PORT), timeout=2).close()
    r = await mgr.exec("lc/web", f"curl -sS -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{PROBE_PORT}/")
    assert r.stdout == "200"
    st = await mgr.service_stop("lc/web")
    assert st.state == "stopped"
    assert "started" in mgr.service_logs("lc/web")


async def test_failing_health_check_restarts_a_real_service(mgr):
    mgr.create("lc/sick", kind="service", preset="service",
               service={"command": ["sleep", "60"], "backoff_initial_s": 0.2,
                        "health": {"cmd": ["/bin/sh", "-c", "test -e /root/ok"], "interval_s": 0.3,
                                   "start_period_s": 0.3, "retries": 2}})
    await mgr.service_start("lc/sick")
    await until(lambda: mgr.services["lc/sick"].status.restarts >= 1, timeout=20)
    await mgr.exec("lc/sick", "touch /root/ok")
    await until(lambda: mgr.services["lc/sick"].status.health == "healthy", timeout=20)
    await mgr.service_stop("lc/sick")


async def test_concurrent_lanes_and_shells_then_clean_shutdown(mgr):
    for i in range(3):
        mgr.create(f"lc/m{i}")
    shells = [await mgr.shell_open(f"lc/m{i}", term="dumb") for i in range(3)]
    rs = await asyncio.gather(*(mgr.exec(f"lc/m{i}", "hostname") for i in range(3)))
    assert [r.stdout for r in rs] == [f"m{i}\n" for i in range(3)]
    assert all(s.running for s in shells)
