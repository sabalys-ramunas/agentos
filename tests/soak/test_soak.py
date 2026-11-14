"""Lanes cycling for an hour on a real node: open, act, close, again and again (docs/BENCH.md).

    AGENTOS_TEST_SLOT=53 scripts/node-test.sh soak -m soak tests/soak/test_soak.py -q
    AGENTOS_SOAK_SECONDS=600 ...                 # shorter (default 3600)
    AGENTOS_SOAK_REPORT=/tmp/soak.json ...       # where the samples go (default: next to the homes)

Two lanes take turns; every cycle opens each, navigates, reads, clicks, types, takes a screenshot
and closes it (one warm lane at a time: the node is shared, and a refusal at the memory floor is
waited out and counted, not failed). After every cycle nothing of theirs may be left: no browser, no Xvfb
or x11vnc on the slot's displays, no display files, no listener on the slot's lane ports. Over the
run agentosd's memory and file descriptors must level off instead of growing with the cycles.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import statistics
import time
from pathlib import Path

import pytest
from soak_support import FEED, FORM, POST, ok, one_ref

from agentos.dev.slotnode import footprint, leftovers, mcp_client, text_of

pytestmark = [pytest.mark.soak]

SECONDS = int(os.environ.get("AGENTOS_SOAK_SECONDS", "3600"))
LANES = ("soak/a", "soak/b")
WARMUP_CYCLES = 3
RSS_GROWTH_MB = 40   # agentosd's own memory, last quarter against the first after warm-up
FD_GROWTH = 8
MAX_MEMORY_WAITS = 60  # 30 s each
TRACE = os.environ.get("AGENTOS_SOAK_TRACEMALLOC") == "1"  # snapshots to find what agentosd keeps
TRACE_AT = int(os.environ.get("AGENTOS_SOAK_TRACE_AT", "25"))


async def one_cycle(client, lane: str) -> int:
    calls = 0
    await ok(client, "browser_navigate", {"on": lane, "url": FEED})
    outline = text_of(await ok(client, "browser_snapshot", {"on": lane}))
    toggle = one_ref(outline, "button", "Comments", within=POST)
    await ok(client, "browser_act", {"on": lane, "ref": toggle, "action": "click", "snapshot": "delta"})
    await ok(client, "browser_screenshot", {"on": lane})
    await ok(client, "browser_navigate", {"on": lane, "url": FORM})
    field = one_ref(text_of(await ok(client, "browser_snapshot", {"on": lane})), "textbox", "Name")
    await ok(client, "browser_act", {"on": lane, "ref": field, "action": "fill", "text": "soak"})
    calls += 7
    return calls


async def test_lanes_cycle_without_leaks(node):
    daemon = node(LANES)
    report_path = Path(os.environ.get("AGENTOS_SOAK_REPORT") or daemon.home.parent / "soak-report.json")
    samples: list[dict] = []
    leaks: list[str] = []
    cycles = calls = memory_waits = 0
    started = time.monotonic()
    async with mcp_client(daemon.url, daemon.agent_token) as client:
        while time.monotonic() - started < SECONDS or cycles < WARMUP_CYCLES + 4:
            for lane in LANES:
                while True:
                    try:
                        opened = await ok(client, "lane_open", {"on": lane})
                        assert '"warm"' in text_of(opened), text_of(opened)[:400]
                        calls += await one_cycle(client, lane) + 2
                        break
                    except AssertionError as e:
                        # The node is shared: the owner's lanes can take it below the memory floor,
                        # and refusing to warm then is right. Wait it out; it is not a leak.
                        if "resources_low" not in str(e) or memory_waits >= MAX_MEMORY_WAITS:
                            raise
                        memory_waits += 1
                        await asyncio.sleep(30)
                await ok(client, "lane_close", {"on": lane})
            cycles += 1
            left = leftovers(daemon)
            if left:
                leaks.append(f"cycle {cycles}: {left}")
            if TRACE and cycles in (WARMUP_CYCLES, TRACE_AT):
                os.kill(daemon.pid, signal.SIGUSR1)  # agentosd writes a tracemalloc snapshot
            sample = footprint(daemon).as_dict() | {"cycle": cycles, "t": round(time.monotonic() - started, 1)}
            samples.append(sample)
            assert len(leaks) < 3, "\n".join(leaks)

    steady = samples[WARMUP_CYCLES:]
    quarter = max(len(steady) // 4, 1)
    first, last = steady[:quarter], steady[-quarter:]

    def med(rows, key):
        return statistics.median(r[key] for r in rows)

    rss_growth_mb = (med(last, "daemon_rss_kb") - med(first, "daemon_rss_kb")) / 1024
    fd_growth = med(last, "daemon_fds") - med(first, "daemon_fds")
    report = {"seconds": round(time.monotonic() - started), "cycles": cycles, "calls": calls,
              "lanes": len(LANES), "memory_waits": memory_waits, "leaks": leaks, "rss_growth_mb": round(rss_growth_mb, 1),
              "fd_growth": fd_growth, "first": first[0], "last": last[-1], "samples": samples}
    report_path.write_text(json.dumps(report, indent=1))
    print(f"soak: {cycles} cycles, {calls} calls in {report['seconds']} s; agentosd RSS "
          f"{first[0]['daemon_rss_kb'] // 1024} -> {last[-1]['daemon_rss_kb'] // 1024} MB "
          f"({rss_growth_mb:+.1f} MB), fds {first[0]['daemon_fds']} -> {last[-1]['daemon_fds']}; report {report_path}")
    assert not leaks, "\n".join(leaks)
    assert rss_growth_mb < RSS_GROWTH_MB, f"agentosd grew {rss_growth_mb:.1f} MB over the run"
    assert fd_growth <= FD_GROWTH, f"agentosd holds {fd_growth} more file descriptors than early on"
