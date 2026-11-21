"""Shared fixtures for env node tests (imported by the test_env_*_node.py modules).

Needs Linux, root and bubblewrap. Each test gets its own OPENAJAR_HOME under
$OPENAJAR_ENV_TEST_DIR (default: <repo>/.home-test) and must leave no mount, cgroup,
network namespace or process behind.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from openajar.env.manager import EnvManager
from openajar.env.paths import EnvSettings

TEST_DIR = Path(os.environ.get("OPENAJAR_ENV_TEST_DIR", Path(__file__).resolve().parents[2] / ".home-test"))

CAN_RUN = sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0 \
    and shutil.which("bwrap") is not None
needs_node = pytest.mark.skipif(not CAN_RUN, reason="needs a Linux node, root and bubblewrap")


def new_home(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:40]
    return TEST_DIR / f"{safe}-{secrets.token_hex(3)}"


def leftovers(home: Path, mgr: EnvManager) -> list[str]:
    out = []
    out += [f"mount {m}" for m in mgr.host.mounts_under(home)]
    inst = Path(mgr.settings.cgroup_root) / mgr.paths.instance
    if inst.exists():
        out += [f"cgroup {p}" for p in inst.iterdir() if p.is_dir()]
    ps = subprocess.run(["pgrep", "-af", str(home)], capture_output=True, text=True, check=False)
    out += [f"process {line}" for line in ps.stdout.splitlines() if "pgrep" not in line and "pytest" not in line]
    return out


@pytest.fixture
async def mgr(request):
    home = new_home(request.node.name)
    m = EnvManager(home, settings=EnvSettings(lane_idle_s=3600))
    await m.start()
    yield m
    await m.shutdown()
    left = leftovers(home, m)
    shutil.rmtree(home, ignore_errors=True)
    assert not left, left


async def until(pred, timeout: float = 15.0, step: float = 0.1):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(step)


async def shell_until(session, needle: str, timeout: float = 10.0) -> str:
    out = ""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while needle not in out and loop.time() < end:
        data, _ = await session.read(wait_s=0.5)
        out += session.read_text(data)
    assert needle in out, out[-800:]
    return out
