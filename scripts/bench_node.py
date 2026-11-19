"""Lane benchmarks on a real node, tracked over commits (docs/BENCH.md).

    make bench                       # syncs, runs on the node in slot 53, appends bench/results.jsonl
    AGENTOS_TEST_SLOT=53 scripts/node-test.sh bench --python scripts/bench_node.py --runs 5

Runs a throwaway agentosd in the slot (agentos.dev.slotnode: the production daemon, its lanes
behind a loopback stand-in proxy that serves tests/fixtures/site, so nothing leaves the node) with
one lane, and measures over MCP, as an agent sees it:

- first_launch_s: the lane's very first browser_open (the fingerprint is pinned, the display made);
- wake_cold_s: browser_open of a cold lane until it is warm (browser, display, exit check);
- navigate_ms and first_snapshot_ms: the first page after a wake and its first browser_snapshot;
- screenshot_ms: browser_screenshot of a warm lane;
- act_ms: browser_act round trip (a click with its read-back, on the feed's Comments toggle);
- mcp_overhead_ms: whoami, a call that reads no page (transport, auth, policy, trace);
- warm_lane_rss_mb and warm_lane_pss_mb: memory of the warm browser's whole process tree;
- close_ms: browser_close back to cold.

Each metric is the median of every sample over --runs wakes. The last line printed is
`BENCH <json>` with the commit, the Camoufox versions and the host; --out appends the JSON too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import secrets
import shutil
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

from agentos.dev.slotnode import (
    SITE,
    Slot,
    SlotDaemon,
    StandInProxy,
    call_ok,
    clean_slot,
    leftovers,
    main_browsers_under,
    mcp_client,
    text_of,
    timed,
    tree_memory_kb,
)
from agentos.node.snapshot import find_refs

ROOT = Path(__file__).resolve().parents[1]
LANE = "bench/lane"
POST = "Post by Ada Lovelace"


def camoufox_versions() -> dict[str, str | None]:
    from importlib.metadata import PackageNotFoundError, version

    try:
        package = version("camoufox")
    except PackageNotFoundError:
        package = None
    browser = None
    with_cache = Path.home() / ".cache" / "camoufox" / "config.json"
    try:
        browser = Path(json.loads(with_cache.read_text())["active_version"]).name
    except (OSError, ValueError, KeyError):
        pass
    return {"camoufox": package, "browser": browser}


def host_info() -> dict[str, object]:
    mem_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_kb = int(line.split()[1])
    except OSError:
        pass
    return {"host": socket.gethostname(), "cpus": os.cpu_count(), "mem_mb": mem_kb // 1024,
            "kernel": platform.release()}


def git_commit() -> tuple[str | None, bool]:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                                    capture_output=True, text=True, check=True).stdout.strip())
        return sha, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, False


def comments_ref(outline: str) -> str:
    refs = find_refs(outline, role="button", name="Comments", within=POST)
    if len(refs) != 1:
        raise AssertionError(f"expected one Comments button in the post, got {refs}")
    return refs[0]


async def measure(daemon: SlotDaemon, runs: int) -> dict[str, list[float]]:
    samples: dict[str, list[float]] = {k: [] for k in (
        "first_launch_s", "wake_cold_s", "navigate_ms", "first_snapshot_ms", "screenshot_ms", "act_ms",
        "mcp_overhead_ms", "warm_lane_rss_mb", "warm_lane_pss_mb", "close_ms")}
    async with mcp_client(daemon.url, daemon.agent_token) as client:
        await call_ok(client, "whoami", {})  # the session is up before anything is timed
        opened, took = await timed(client, "browser_open", {"on": LANE})
        if opened.is_error or '"warm"' not in text_of(opened):
            raise AssertionError(f"browser_open did not warm the lane: {text_of(opened)[:600]}")
        samples["first_launch_s"].append(took)
        await call_ok(client, "browser_close", {"on": LANE})
        for i in range(runs):
            opened, took = await timed(client, "browser_open", {"on": LANE})
            if opened.is_error or '"warm"' not in text_of(opened):
                raise AssertionError(f"run {i}: browser_open did not warm the lane: {text_of(opened)[:600]}")
            samples["wake_cold_s"].append(took)
            nav, took = await timed(client, "browser_navigate", {"on": LANE, "url": f"{SITE}/feed.html"})
            if nav.is_error:
                raise AssertionError(f"run {i}: navigate failed: {text_of(nav)[:600]}")
            samples["navigate_ms"].append(took * 1000)
            snap, took = await timed(client, "browser_snapshot", {"on": LANE})
            if snap.is_error:
                raise AssertionError(f"run {i}: snapshot failed: {text_of(snap)[:600]}")
            samples["first_snapshot_ms"].append(took * 1000)
            ref = comments_ref(text_of(snap))
            for _ in range(3):
                shot, took = await timed(client, "browser_screenshot", {"on": LANE})
                if shot.is_error or not any(getattr(c, "type", "") == "image" for c in shot.content):
                    raise AssertionError(f"run {i}: screenshot failed: {text_of(shot)[:600]}")
                samples["screenshot_ms"].append(took * 1000)
            for _ in range(3):
                act, took = await timed(client, "browser_act", {"on": LANE, "ref": ref, "action": "click"})
                if act.is_error or '"ok": true' not in text_of(act).replace('"ok":true', '"ok": true'):
                    raise AssertionError(f"run {i}: act failed: {text_of(act)[:600]}")
                samples["act_ms"].append(took * 1000)
            for _ in range(5):
                _, took = await timed(client, "whoami", {})
                samples["mcp_overhead_ms"].append(took * 1000)
            rss, pss, _ = tree_memory_kb(main_browsers_under(daemon.home))
            samples["warm_lane_rss_mb"].append(rss / 1024)
            samples["warm_lane_pss_mb"].append(pss / 1024)
            closed, took = await timed(client, "browser_close", {"on": LANE})
            if closed.is_error:
                raise AssertionError(f"run {i}: close failed: {text_of(closed)[:600]}")
            samples["close_ms"].append(took * 1000)
    return samples


def summarize(samples: dict[str, list[float]]) -> tuple[dict[str, float], dict[str, list[float]]]:
    medians, spread = {}, {}
    for name, values in samples.items():
        if values:
            medians[name] = round(statistics.median(values), 3 if name.endswith("_s") else 1)
            spread[name] = [round(min(values), 3), round(max(values), 3)]
    return medians, spread


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=5, help="cold wakes to measure (default 5)")
    ap.add_argument("--out", type=Path, help="append the result to this JSONL file")
    ap.add_argument("--commit", help="the commit measured (default: git rev-parse here)")
    ap.add_argument("--dirty", action="store_true", help="the checkout had changes not committed")
    ap.add_argument("--note", default="", help="a note stored with the result")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway home")
    a = ap.parse_args()
    if not sys.platform.startswith("linux"):
        print("bench_node.py runs on a Linux node: make bench", file=sys.stderr)
        return 2

    slot = Slot.from_env()
    home = Path(os.environ.get("AGENTOS_TEST_ROOT", ROOT / ".node-home")) / f"bench-{secrets.token_hex(4)}"
    home.mkdir(parents=True)
    proxy = StandInProxy(slot.proxy_port, username="bench", password=secrets.token_urlsafe(16)).start()
    daemon = SlotDaemon(home, slot, token=secrets.token_urlsafe(32), proxy=proxy)
    started = time.time()
    try:
        daemon.prepare([LANE])
        daemon.start()
        samples = asyncio.run(measure(daemon, a.runs))
    finally:
        daemon.stop()
        left = leftovers(daemon)
        if left:
            asyncio.run(clean_slot(daemon))
        proxy.stop()
        if not a.keep:
            shutil.rmtree(home, ignore_errors=True)
    commit, dirty = git_commit()
    medians, spread = summarize(samples)
    record = {
        "commit": a.commit or commit, "dirty": a.dirty or dirty,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        **camoufox_versions(), **host_info(), "runs": a.runs, "metrics": medians, "spread": spread,
        "left_running": left or None, "note": a.note or None,
    }
    line = json.dumps(record, sort_keys=True)
    if a.out:
        with a.out.open("a") as f:
            f.write(line + "\n")
    for name, value in medians.items():
        print(f"{name:20} {value:>10}   (min {spread[name][0]}, max {spread[name][1]})")
    print("BENCH " + line)
    return 1 if left else 0


if __name__ == "__main__":
    raise SystemExit(main())
