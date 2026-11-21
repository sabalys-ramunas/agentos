#!/usr/bin/env python3
"""Record a golden trace: a lane session on the dev web (openajar.dev.web), never a real site.

  scripts/record_golden.py                 write tests/golden/dev-social/
  scripts/record_golden.py --out DIR       write somewhere else
  scripts/record_golden.py --check         exit 1 when a new recording differs from the one in the repo

tests/unit/test_golden.py replays every trace under tests/golden/ against the engine abstraction.
Record again (and read the diff: it is what an agent now sees differently) after a change to the
tool layer or the dev web that is meant to change what agents read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from openajar.dev import golden

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "tests" / "golden" / "dev-social"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT, help="the trace directory")
    parser.add_argument("--check", action="store_true", help="compare with the trace on disk instead")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="openajar-golden-") as home:
        trace = asyncio.run(golden.record(Path(home)))
    if args.check:
        new, stored = golden.comparable(trace), golden.comparable(golden.load(args.out))
        same = all(json.dumps(new[k], sort_keys=True) == json.dumps(stored[k], sort_keys=True) for k in new)
        if not same:
            print(f"{args.out} differs from a new recording: scripts/record_golden.py", file=sys.stderr)
            return 1
        return 0
    golden.write(args.out, trace)
    calls = trace["session"]["calls"]
    print(f"wrote {args.out}: {len(calls)} tool calls, {len(trace['engine']['calls'])} engine calls, "
          f"{len(trace['har']['log']['entries'])} pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
