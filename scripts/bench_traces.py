"""Trace write cost before and after the audit chain: 10k events each, per-event times.

    uv run python scripts/bench_traces.py [--base b9b2218] [--events 10000] [--rounds 3]

"Before" is policy/traces.py at the base commit, loaded from git into a temporary module; "after"
is the working tree. Each round writes `--events` events of a realistic shape (a tool_start and
tool_end pair with arguments, a summary and a lane) into a fresh database, and times every
write() call. Prints the median, p95 and mean per event and the added median cost.
"""

from __future__ import annotations

import argparse
import importlib.util
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_base(rev: str):
    src = subprocess.run(["git", "show", f"{rev}:src/agentos/policy/traces.py"], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout
    tmp = Path(tempfile.mkdtemp()) / "traces_base.py"
    tmp.write_text(src)
    spec = importlib.util.spec_from_file_location("traces_base", tmp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["traces_base"] = mod
    spec.loader.exec_module(mod)
    return mod


def run(mod, n: int) -> list[float]:
    d = Path(tempfile.mkdtemp())
    store = mod.TraceStore(d / "traces.db")
    times = []
    for i in range(n):
        ev = mod.TraceEvent(kind="tool_end" if i % 2 else "tool_start", tool="browser_act", lane="biz-a/instagram",
                            effect="commit", status="ok", principal="claude-code", principal_kind="agent",
                            grant_id="gr_abc", grant_path="/gr_root/gr_abc/", run_id=None,
                            summary=f"Clicked the Like button on post {i}",
                            args={"on": "biz-a/instagram", "ref": f"e{i}", "action": "click", "snapshot": "delta"},
                            duration_ms=120)
        t = time.perf_counter_ns()
        store.write(ev)
        times.append((time.perf_counter_ns() - t) / 1000)
    if hasattr(store, "close"):
        store.close()
    else:
        store.db.close()
    return times


def summary(times: list[float]) -> tuple[float, float, float]:
    q = statistics.quantiles(times, n=100)
    return statistics.median(times), q[94], statistics.fmean(times)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="b9b2218")
    p.add_argument("--events", type=int, default=10_000)
    p.add_argument("--rounds", type=int, default=3)
    a = p.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    base = load_base(a.base)
    from agentos.policy import traces as head

    res = {"before": [], "after": []}
    for _ in range(a.rounds):  # alternate, so drift hits both
        res["before"] += run(base, a.events)
        res["after"] += run(head, a.events)
    for name, times in res.items():
        med, p95, mean = summary(times)
        print(f"{name:6}  median {med:7.1f} us  p95 {p95:7.1f} us  mean {mean:7.1f} us  ({len(times)} writes)")
    added = statistics.median(res["after"]) - statistics.median(res["before"])
    print(f"added median cost per event: {added:.1f} us")


if __name__ == "__main__":
    main()
