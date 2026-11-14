"""agentos.core.proc: the process helpers, and the old import paths that still reach them."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

from agentos.core import proc


def test_pid_alive_and_kill():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        assert proc.pid_alive(child.pid)
        asyncio.run(proc.kill_processes([child.pid], grace=2.0))
        child.wait(timeout=5)
        assert not proc.pid_alive(child.pid)
    finally:
        if child.poll() is None:
            child.kill()


def test_kill_processes_never_kills_itself():
    asyncio.run(proc.kill_processes([os.getpid()], grace=0))


def test_the_display_module_still_exports_them():
    from agentos.node import display

    for name in ("iter_processes", "pid_alive", "process_group", "kill_processes"):
        assert getattr(display, name) is getattr(proc, name)
