"""A real Camoufox's own output (openajar.node.browser_output): the browser's stdout and stderr
reach the log through the Playwright driver, scrubbed, masked and with the lane's id, and none
of it is written anywhere else.

Run on the node:  OPENAJAR_TEST_SLOT=51 scripts/node-test.sh devbatch tests/node/test_browser_output_node.py
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys

import pytest
from test_lanes_node import home, managers, node_config  # noqa: F401

from openajar.core import logs
from openajar.core.models import LaneSpec

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb and Camoufox"),
]

LANE = "test/output"


@pytest.fixture
def journal():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    stream = io.StringIO()
    logs.setup(None, stream=stream, journal=False)
    # stands in for the vault's scrub: Camoufox prints this word on every start
    logs.add_scrubber(lambda text: text.replace("Juggler", "[redacted]"))
    yield stream
    logs.clear_scrubbers()
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)


async def test_a_lane_browsers_output_reaches_the_log_scrubbed_with_the_lane_id(managers, home, journal,  # noqa: F811
                                                                             capfd):
    mgr = managers(max_warm_lanes=1)
    await mgr.start()
    await mgr.create(LaneSpec(id=LANE))
    await mgr.open(LANE)
    await mgr.close(LANE)
    await asyncio.sleep(1.0)  # the drain is a thread: give it the driver's last lines
    out = [line for line in journal.getvalue().splitlines() if "browser stderr:" in line]
    ours = [line for line in out if f"[lane={LANE}]" in line]
    assert any("<launching>" in line and "-profile" in line for line in ours), out[:5]
    assert any("[pid=" in line and "[redacted] listening to the pipe" in line for line in ours), out[:20]
    assert any("<process did exit" in line for line in ours)
    assert not any("Juggler" in line for line in out)
    assert not any(line.rstrip().endswith(("[out]", "[err]")) for line in out)  # empty lines are dropped
    captured = capfd.readouterr()
    assert "Juggler" not in captured.err and "Juggler" not in captured.out  # nothing bypassed the log
