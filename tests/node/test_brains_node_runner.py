"""Node-only isolation for brain tasks, with the fake CLIs: the unprivileged brain user, the
systemd scope (resource limits, survives the daemon, sweeps escaped processes).

Needs root and systemd on the node, plus the brain user (install_cli.sh --with-user).
Run on the node:  uv run pytest -m node tests/node/test_brains_node_runner.py
"""

import asyncio
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from testkit.brains import OAUTH, FakeConfig, add, wait_status

from openajar.brains import cliprobe
from openajar.brains.service import BrainService
from openajar.brains.settings import BrainsSettings
from openajar.core.models import Principal
from openajar.core.runtime import Runtime
from openajar.dev import fakecli

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0 or not Path("/run/systemd/system").exists(),
                       reason="needs root and systemd (the node)"),
]

P = Principal(id="node-test")


def _brain_user():
    try:
        return pwd.getpwnam("agentos-brain")
    except KeyError:
        pytest.skip("brain user missing: run install_cli.sh --with-user")


@pytest.fixture
def env():
    user = _brain_user()
    # world-traversable bin dir with a self-contained fake CLI run by the system python
    bins = Path(tempfile.mkdtemp(prefix="agentos-brains-fake-"))
    os.chmod(bins, 0o755)
    fakecli.install(bins, python="/usr/bin/python3")
    home = Path(tempfile.mkdtemp(prefix="agentos-brains-home-"))
    cliprobe.clear_cache()
    rt = Runtime(config=FakeConfig(home))
    settings = BrainsSettings(bin_dirs=[str(bins)], isolation="scope", run_as=user.pw_name, poll_seconds=0.2,
                              cancel_grace_seconds=0.5, memory_mb=512, tasks_max=64)
    svc = BrainService(rt, home=home, settings=settings, online_check=lambda: True)
    yield svc, user, bins, home
    for rec in svc.store.list_tasks(limit=200):
        subprocess.run(["systemctl", "stop", f"agentos-brain-{rec.id}-01.scope"], capture_output=True, check=False)
        shutil.rmtree(Path(user.pw_dir) / "tasks" / rec.id, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)
    shutil.rmtree(bins, ignore_errors=True)


def _run(argv, check=False):
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=check)


def _dump(svc, tid):
    return json.loads((svc.task_dir(tid) / "work" / "fake-dump.json").read_text())


async def test_brain_runs_as_the_unprivileged_user_in_a_scope(env):
    svc, user, _, home = env
    await add(svc, "claude-max", "subscription", "anthropic")
    secret_file = home / "secrets" / "brains-credentials.DEV-ONLY.json"
    assert secret_file.exists()
    r = await svc.submit(P, prompt=f"FAKE:readfile READ:{secret_file} READ:/etc/shadow READ:{home}/brains/brains.db",
                         workspace="nodetest")
    st = await wait_status(svc, r["id"], timeout=30)
    assert st["status"] == "succeeded_unverified", svc.tail(r["id"])["events"][-5:]
    d = _dump(svc, r["id"])
    assert d["uid"] == user.pw_uid
    assert set(d["read"].values()) == {"PermissionError"}, d["read"]   # crown jewels stay out of reach
    assert d["cwd"].startswith(user.pw_dir)
    assert os.path.islink(svc.task_dir(r["id"]) / "work")
    launch = json.loads((svc.attempt_dir(r["id"], 1) / "launch.json").read_text())
    assert launch["unit"] == f"agentos-brain-{r['id']}-01.scope"
    assert OAUTH not in json.dumps(launch)


async def test_scope_has_resource_limits_and_cancel_sweeps_escaped_processes(env):
    svc, _user, _, _ = env
    await add(svc, "claude-max", "subscription", "anthropic")
    r = await svc.submit(P, prompt="FAKE:spawn_orphan", workspace="nodetest")
    tid = r["id"]
    await wait_status(svc, tid, until=("running",), timeout=20)
    orphan_file = svc.task_dir(tid) / "work" / "orphan.pid"
    for _ in range(50):
        if orphan_file.exists():
            break
        await asyncio.sleep(0.1)
    orphan = int(orphan_file.read_text())
    unit = f"agentos-brain-{tid}-01.scope"
    show = _run(["systemctl", "show", unit, "-p", "MemoryMax", "-p", "TasksMax", "-p", "ActiveState"]).stdout
    assert "MemoryMax=536870912" in show and "TasksMax=64" in show and "ActiveState=active" in show
    st = await svc.cancel(tid, P, wait=True)
    assert st["status"] == "cancelled"
    await asyncio.sleep(0.5)
    with pytest.raises(ProcessLookupError):
        os.kill(orphan, 0)  # escaped its process group, still swept with the scope


async def test_task_survives_the_daemon_process_exiting(env):
    svc, user, bins, home = env
    await add(svc, "claude-max", "subscription", "anthropic")
    # a separate "daemon" process submits the task and exits at once
    script = f"""
import asyncio, sys
from pathlib import Path
from openajar.brains.service import BrainService
from openajar.brains.settings import BrainsSettings
from testkit.brains import FakeConfig
from openajar.core.models import Principal
from openajar.core.runtime import Runtime
async def main():
    rt = Runtime(config=FakeConfig(Path({str(home)!r})))
    s = BrainsSettings(bin_dirs=[{str(bins)!r}], isolation="scope", run_as={user.pw_name!r})
    svc = BrainService(rt, home=Path({str(home)!r}), settings=s, online_check=lambda: True)
    r = await svc.submit(Principal(id="d"), prompt="FAKE:slow:3", workspace="nodetest")
    print(r["id"])
asyncio.run(main())
"""
    out = _run([sys.executable, "-c", script], check=True)
    tid = out.stdout.strip().splitlines()[-1]
    st = await wait_status(svc, tid, timeout=40)
    assert st["status"] == "succeeded_unverified" and st["turns"] >= 5
