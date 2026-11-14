"""Flag a lane benchmark regression: the newest result in bench/results.jsonl against the one before
it on the same host (docs/BENCH.md).

    uv run python scripts/bench_compare.py [--threshold 0.2] [--file bench/results.jsonl] [--against SHA]

Every metric is lower-is-better. A metric regresses when it is more than --threshold (a fraction,
default 0.2) above the baseline and also more than its noise floor in absolute terms (so 3 ms
against 2 ms is not a 50% regression). Exits 1 when any metric regresses, 0 otherwise (and when
there is nothing to compare yet).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILE = ROOT / "bench" / "results.jsonl"

# Absolute differences below these are noise, whatever the ratio.
NOISE_FLOOR = {"_s": 0.15, "_ms": 15.0, "_mb": 25.0}


def floor_of(metric: str) -> float:
    return next((v for suffix, v in NOISE_FLOOR.items() if metric.endswith(suffix)), 0.0)


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def pick(results: list[dict], against: str | None) -> tuple[dict | None, dict | None]:
    """(baseline, newest): the newest result, and the latest earlier one from the same host
    (or the one of commit `against`)."""
    if not results:
        return None, None
    newest = results[-1]
    earlier = [r for r in results[:-1] if r.get("host") == newest.get("host")]
    if against:
        earlier = [r for r in earlier if str(r.get("commit", "")).startswith(against)]
    return (earlier[-1] if earlier else None), newest


def compare(baseline: dict, newest: dict, threshold: float) -> list[tuple[str, float, float, float, bool]]:
    """(metric, baseline, newest, change as a fraction, regressed) for every metric in both."""
    rows = []
    for metric, new in sorted(newest.get("metrics", {}).items()):
        old = baseline.get("metrics", {}).get(metric)
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            continue
        change = (new - old) / old if old else 0.0
        regressed = change > threshold and new - old > floor_of(metric)
        rows.append((metric, old, new, change, regressed))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE)
    ap.add_argument("--threshold", type=float, default=0.2, help="allowed slowdown as a fraction (default 0.2)")
    ap.add_argument("--against", help="compare with this commit's result instead of the previous one")
    a = ap.parse_args(argv)
    baseline, newest = pick(load(a.file), a.against)
    if newest is None:
        print(f"no results in {a.file}")
        return 0
    if baseline is None:
        print(f"{newest.get('commit')}: the first result on {newest.get('host')}, nothing to compare")
        return 0
    rows = compare(baseline, newest, a.threshold)
    print(f"{newest.get('commit')} against {baseline.get('commit')} on {newest.get('host')} "
          f"(threshold {a.threshold:.0%})")
    for metric, old, new, change, regressed in rows:
        mark = "REGRESSION" if regressed else ""
        print(f"  {metric:20} {old:>10} -> {new:<10} {change:+7.1%}  {mark}")
    bad = [r[0] for r in rows if r[4]]
    if bad:
        print(f"regressed: {', '.join(bad)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
