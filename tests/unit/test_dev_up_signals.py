"""`agentos dev up` as a process: a stop by signal removes the temporary home, and never a home the
owner passed in with --home.

Each node runs in its own subprocess and session. The temporary home is made under a folder the
test owns (TMPDIR points there), so the test can see it and remove what is left. A signal goes out
once the node is running (the banner), and again as soon as its temporary home appears: then
serve() has not installed its signal handlers yet, which is the window that used to leak the home.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import pytest

BANNER = "Press Ctrl-C to stop"
WAIT = 90  # seconds: importing the node and seeding it takes a few seconds on a slow machine


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def temporary_homes(folder: Path) -> list[Path]:
    return sorted(folder.glob("agentos-dev-*"))


def wait_for(done: Callable[[], bool], timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def folder():
    """A temporary folder for one test: the node's TMPDIR, removed afterwards with anything left."""
    path = Path(tempfile.mkdtemp(prefix="agentos-test-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def start_node(folder):
    """Start `agentos dev up` in a subprocess; every node still running is killed at the end."""
    started: list[subprocess.Popen] = []

    def start(*options: str) -> tuple[subprocess.Popen, Path]:
        log = folder / f"node-{len(started)}.log"
        env = {**os.environ, "TMPDIR": str(folder), "PYTHONUNBUFFERED": "1"}
        argv = [sys.executable, "-c", "from agentos.cli import app; app()", "dev", "up",
                "--port", str(free_port()), *options]
        with log.open("wb") as out:
            proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        started.append(proc)
        return proc, log

    yield start
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def banner_shown(log: Path) -> bool:
    return BANNER in log.read_text(errors="replace")


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["sigint", "sigterm"])
def test_a_signal_after_the_banner_removes_the_temporary_home(start_node, folder, sig):
    proc, log = start_node()
    assert wait_for(lambda: banner_shown(log) or proc.poll() is not None), log.read_text(errors="replace")
    assert len(temporary_homes(folder)) == 1, log.read_text(errors="replace")

    proc.send_signal(sig)

    assert proc.wait(WAIT) == 0, log.read_text(errors="replace")
    assert temporary_homes(folder) == []


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["sigint", "sigterm"])
def test_a_signal_while_the_node_starts_removes_the_temporary_home(start_node, folder, sig):
    proc, log = start_node("--seed")
    assert wait_for(lambda: bool(temporary_homes(folder)) or proc.poll() is not None), \
        log.read_text(errors="replace")
    assert temporary_homes(folder), log.read_text(errors="replace")

    proc.send_signal(sig)  # the home exists; serve() has not started listening yet

    assert proc.wait(WAIT) == 0, log.read_text(errors="replace")
    assert temporary_homes(folder) == []


def test_a_signal_keeps_a_home_the_owner_passed_in(start_node, folder):
    home = folder / "my-node"
    proc, log = start_node("--home", str(home))
    assert wait_for(lambda: banner_shown(log) or proc.poll() is not None), log.read_text(errors="replace")

    proc.send_signal(signal.SIGINT)

    assert proc.wait(WAIT) == 0, log.read_text(errors="replace")
    assert (home / "vault.db").is_file()
    assert temporary_homes(folder) == []
