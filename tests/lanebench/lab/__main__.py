"""The lab's commands. Run from the repository root with `PYTHONPATH=src:tests`:

    python -m lanebench.lab harvest --sets tuning            # walk the fixtures (no key needed)
    python -m lanebench.lab score --sets tuning              # ask Jev, grade, print a report
    python -m lanebench.lab report <run.json>                # print a saved report again
    python -m lanebench.lab spend                            # what the lab has spent so far
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from lanebench.lab import sets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lanebench.lab")
    sub = parser.add_subparsers(dest="command", required=True)
    h = sub.add_parser("harvest")
    h.add_argument("--sets", default="tuning")
    h.add_argument("--tasks", default="")
    h.add_argument("--workers", type=int, default=4)
    h.add_argument("--force", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("--sets", default="quick")
    s.add_argument("--tasks", default="")
    s.add_argument("--name", default="", help="save the rows under data/runs/<name>.json")
    s.add_argument("--sealed", action="store_true", help="keep the rows, print nothing about them")
    s.add_argument("--offline", action="store_true", help="kept answers only: no call goes out")
    s.add_argument("--brief", action="store_true", help="only the top lines of the report")
    s.add_argument("--concurrency", type=int, default=4)
    c = sub.add_parser("closed")
    c.add_argument("--sets", default="quick")
    c.add_argument("--tasks", default="")
    c.add_argument("--seeds", default="", help="only these seeds of the sets")
    c.add_argument("--name", default="")
    c.add_argument("--sealed", action="store_true")
    c.add_argument("--workers", type=int, default=4)
    r = sub.add_parser("report")
    r.add_argument("run")
    sub.add_parser("spend")
    args = parser.parse_args(argv)
    if args.command == "harvest":
        from lanebench.lab import harvest

        names = args.tasks.split(",") if args.tasks else None
        jobs = sets.jobs(args.sets.split(","), names)
        asyncio.run(harvest.harvest(jobs, workers=args.workers, force=args.force))
    elif args.command == "score":
        return asyncio.run(score(args))
    elif args.command == "closed":
        return asyncio.run(closed_runs(args))
    elif args.command == "report":
        from lanebench.lab import grade

        print(grade.report(json.loads(Path(args.run).read_text())))
    else:
        from lanebench.lab.harvest import DATA
        from lanebench.lab.jev import Ledger

        print(f"${Ledger(DATA / 'spend.jsonl').usd:.4f}")
    return 0


async def closed_runs(args: argparse.Namespace) -> int:
    from lanebench.lab import closed
    from lanebench.lab.harvest import DATA
    from lanebench.lab.jev import LabJev

    jobs = sets.jobs(args.sets.split(","), args.tasks.split(",") if args.tasks else None)
    if args.seeds:
        wanted = {int(s) for s in args.seeds.split(",")}
        jobs = [j for j in jobs if j[1] in wanted]
    jev = LabJev()
    spent = jev.ledger.usd
    try:
        rows = await closed.closed(jobs, jev, workers=args.workers)
    finally:
        await jev.close()
    if args.name:
        closed.save(rows, DATA / "closed" / f"{args.name}.json")
    cost = f"${jev.ledger.usd - spent:.4f} this run, ${jev.ledger.usd:.4f} in all"
    print(f"sealed: {len(rows)} runs kept. {cost}" if args.sealed else f"{closed.table(rows)}\n{cost}")
    return 0


async def score(args: argparse.Namespace) -> int:
    from lanebench.lab import grade, replay
    from lanebench.lab.harvest import DATA
    from lanebench.lab.jev import LabJev

    jobs = sets.jobs(args.sets.split(","), args.tasks.split(",") if args.tasks else None)
    walks = replay.load(jobs)
    jev = LabJev(concurrency=args.concurrency, offline=args.offline)
    spent = jev.ledger.usd
    gate = asyncio.Semaphore(args.concurrency * 2)

    async def one(walk: dict[str, Any]) -> list[dict[str, Any]]:
        async with gate:
            return await replay.decide_walk(walk, jev)

    try:
        rows = [r for part in await asyncio.gather(*[one(w) for w in walks]) for r in part]
    finally:
        await jev.close()
    if args.name:
        out = DATA / "runs" / f"{args.name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, ensure_ascii=False))
    cost = f"{jev.calls} calls, {jev.hits} questions from the cache, ${jev.ledger.usd - spent:.4f} this run, ${jev.ledger.usd:.4f} in all"
    if args.sealed:
        print(f"sealed: {len(rows)} rows kept. {cost}")
        return 0
    text = grade.report(rows)
    print("\n".join(text.splitlines()[:3]) if args.brief else text)
    print(cost)
    return 0


if __name__ == "__main__":
    sys.exit(main())
