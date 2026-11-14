"""Soak and chaos runs on a real node (docs/BENCH.md). Marked `soak`, so neither the normal run
nor `pytest -m node` picks them up:

    AGENTOS_TEST_SLOT=53 scripts/node-test.sh soak -m soak tests/soak -q
    AGENTOS_SOAK_SECONDS=600 ... tests/soak/test_soak.py      # a shorter soak

Each test gets its own throwaway agentosd in the slot (agentos.dev.slotnode) with a home under
.node-home/, its lanes behind the stand-in proxy (the only web they see, and the proxy the chaos
tests stop). After each test the slot must be empty: no browser whose profile is under the home,
no Xvfb or x11vnc on the slot's displays, no display files, no listener on the slot's lane ports.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentos.dev.slotnode import Slot, SlotDaemon, StandInProxy, clean_slot, leftovers

ROOT = Path(os.environ.get("AGENTOS_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
MIN_AVAILABLE_MB = 1800  # two warm lanes and the daemon, above the node's own 800 MB floor


def available_mb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def pytest_collection_modifyitems(config, items):
    if sys.platform.startswith("linux") and shutil.which("Xvfb") and shutil.which("x11vnc"):
        return
    skip = pytest.mark.skip(reason="needs a Linux node with Xvfb, x11vnc and Camoufox")
    for item in items:
        if "soak" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def slot() -> Slot:
    return Slot.from_env()


@pytest.fixture
def proxy(slot) -> Iterator[StandInProxy]:
    stand_in = StandInProxy(slot.proxy_port, username="soak", password="pw-" + secrets.token_urlsafe(12)).start()
    try:
        yield stand_in
    finally:
        stand_in.stop()


@pytest.fixture
async def node(slot, proxy):
    """make(lanes, **node_toml) -> a started SlotDaemon. Stopped, checked and removed afterwards."""
    if available_mb() < MIN_AVAILABLE_MB:
        pytest.skip(f"only {available_mb()} MB available on the node; these runs need {MIN_AVAILABLE_MB} MB")
    made: list[SlotDaemon] = []

    def make(lanes, start: bool = True, **node_toml) -> SlotDaemon:
        home = ROOT / f"soak-{secrets.token_hex(4)}"
        home.mkdir(parents=True)
        daemon = SlotDaemon(home, slot, token=secrets.token_urlsafe(32), proxy=proxy, node=dict(node_toml))
        made.append(daemon)
        daemon.prepare(lanes)
        if start:
            daemon.start()
        return daemon

    yield make
    problems = []
    for daemon in made:
        daemon.stop()
        left = leftovers(daemon)
        if left:
            problems.append(f"{daemon.home.name}: left after a graceful stop: {left}")
            await clean_slot(daemon)
        log = daemon.log_text()
        leaked = [i for i, s in enumerate(daemon.secrets()) if s and s in log]
        if leaked:
            problems.append(f"{daemon.home.name}: secrets {leaked} (0 node token, 1 proxy password, 2 proxy "
                            "basic auth, 3 agent grant) appear in agentosd's logs")
        if os.environ.get("AGENTOS_SOAK_KEEP") != "1":
            shutil.rmtree(daemon.home, ignore_errors=True)
    assert not problems, "\n".join(problems)
