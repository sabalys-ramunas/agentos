"""agentosd in a child process, for test_daemon_shutdown.py. It serves until it is signalled, and
its lanes close slowly, so the test can send signals while the shutdown is still running.

Usage: python shutdown_child.py <home>. Each step it reaches leaves an empty file in <home>/marks,
which is how the test follows it. Timing comes from the environment: OPENAJAR_TEST_SLOW_SHUTDOWN
(seconds the lanes take to close) and OPENAJAR_TEST_DEADLINE (the daemon's SHUTDOWN_DEADLINE).
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from fakes import FakeInteraction, FakeLaneManager

from openajar.core.config import NodeConfig
from openajar.node import daemon

HOME = Path(sys.argv[1])
MARKS = HOME / "marks"


def mark(name: str) -> None:
    (MARKS / name).touch()


class SlowLanes(FakeLaneManager):
    async def shutdown(self) -> None:
        mark("shutdown-started")
        await asyncio.sleep(float(os.environ["OPENAJAR_TEST_SLOW_SHUTDOWN"]))
        mark("lanes-closed")
        await super().shutdown()


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
    daemon.SHUTDOWN_DEADLINE = float(os.environ["OPENAJAR_TEST_DEADLINE"])
    config = NodeConfig(home=HOME, mcp_token="test-token", mcp_port=0)  # port 0: any free port
    runtime = daemon.node_runtime(config, SlowLanes(config), FakeInteraction())
    asyncio.run(daemon.serve(config, runtime, on_started=lambda port: mark("ready")))
    mark("serve-returned")


if __name__ == "__main__":
    main()
