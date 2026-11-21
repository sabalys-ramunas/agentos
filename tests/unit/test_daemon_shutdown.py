"""agentosd's shutdown with real signals. The handlers serve() installs stay until the lanes have
closed, so a second signal during the shutdown is logged and ignored. A third signal, or a
shutdown still running past its deadline, exits hard and says so. The signal tests run agentosd
in a child process (shutdown_child.py): a signal that reaches the default handler would kill
the test run itself."""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fakes import FakeInteraction, FakeLaneManager, mcp_port_for_tests

from openajar.core.config import NodeConfig
from openajar.node.daemon import node_runtime, serve

CHILD = Path(__file__).with_name("shutdown_child.py")
HARD_EXIT = 3  # the exit code of a forced stop: not a crash (1), not a usage error (2)


class Daemon:
    """One agentosd child process, and how far its shutdown has got."""

    def __init__(self, home: Path, *, slow: float, deadline: float = 28.0):
        self.home = home
        self.marks = home / "marks"
        self.marks.mkdir(parents=True)
        self.log_path = home / "stderr.log"
        self.stderr = self.log_path.open("w")
        env = {**os.environ, "OPENAJAR_TEST_SLOW_SHUTDOWN": str(slow), "OPENAJAR_TEST_DEADLINE": str(deadline)}
        self.proc = subprocess.Popen([sys.executable, str(CHILD), str(home)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=self.stderr)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.stderr.close()

    def log(self) -> str:
        return self.log_path.read_text()

    def marked(self, name: str) -> bool:
        return (self.marks / name).exists()

    def wait_for(self, what: str, done, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while not done():
            if self.proc.poll() is not None:
                raise AssertionError(f"agentosd exited ({self.proc.returncode}) before {what}:\n{self.log()}")
            if time.monotonic() > deadline:
                raise AssertionError(f"agentosd did not reach {what} within {timeout:g}s:\n{self.log()}")
            time.sleep(0.02)

    def wait_marked(self, name: str) -> None:
        self.wait_for(f"the {name!r} step", lambda: self.marked(name))

    def wait_logged(self, text: str) -> None:
        self.wait_for(f"the log line {text!r}", lambda: text in self.log())

    def send(self, sig: signal.Signals) -> None:
        self.proc.send_signal(sig)

    def finish(self, timeout: float = 20.0) -> int:
        return self.proc.wait(timeout)


@pytest.fixture
def daemon_child(tmp_path):
    children: list[Daemon] = []

    def start(**kwargs) -> Daemon:
        child = Daemon(tmp_path, **kwargs)
        children.append(child)
        return child

    yield start
    for child in children:
        child.close()


def test_a_second_signal_during_shutdown_is_logged_and_the_shutdown_finishes(daemon_child):
    d = daemon_child(slow=2.0)
    d.wait_marked("ready")
    d.send(signal.SIGTERM)
    d.wait_marked("shutdown-started")
    d.send(signal.SIGTERM)  # the lanes are still closing: this must not kill the process
    assert d.finish() == 0
    assert d.marked("lanes-closed") and d.marked("serve-returned")
    lines = d.log().splitlines()
    assert sum("shutdown is already in progress" in line for line in lines) == 1
    assert "INFO stopped" in lines


def test_a_third_signal_exits_at_once(daemon_child):
    d = daemon_child(slow=60)
    d.wait_marked("ready")
    d.send(signal.SIGTERM)
    d.wait_marked("shutdown-started")
    d.send(signal.SIGTERM)
    d.wait_logged("shutdown is already in progress")
    d.send(signal.SIGTERM)
    assert d.finish(timeout=10) == HARD_EXIT
    assert not d.marked("lanes-closed")
    assert "exiting now without a clean shutdown: SIGTERM again during shutdown" in d.log()


def test_a_shutdown_past_its_deadline_exits_hard(daemon_child):
    d = daemon_child(slow=60, deadline=1.0)
    d.wait_marked("ready")
    d.send(signal.SIGTERM)
    assert d.finish(timeout=10) == HARD_EXIT
    assert not d.marked("lanes-closed")
    assert "shutdown did not finish within 1s" in d.log()


async def test_the_handlers_stay_installed_until_the_lanes_have_closed(tmp_path):
    seen: list = []

    class Recording(FakeLaneManager):
        async def shutdown(self):
            seen.append(signal.getsignal(signal.SIGTERM))  # a default handler would kill the process here
            await super().shutdown()

    config = NodeConfig(home=tmp_path, mcp_token="tok", mcp_port=mcp_port_for_tests())
    ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(serve(config, node_runtime(config, Recording(config), FakeInteraction()),
                                     on_started=ready.set_result))
    await asyncio.wait_for(ready, 10)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 15)
    assert seen and seen[0] not in (signal.SIG_DFL, signal.SIG_IGN)
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL  # and removed once serve() is done
