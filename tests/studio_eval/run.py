"""Run the studio agent experience eval.

    uv run python tests/studio_eval/run.py                      # the scripted perfect and careless agents
    uv run python tests/studio_eval/run.py --agent claude --model haiku
    uv run python tests/studio_eval/run.py --agent claude --model sonnet --task t01_fix_clipped t04_why_moved

Each agent kind gets its own `openajar studio up` in a scratch HOME (never your ~/.openajar) and its
own copy of the task pages. Per task it records: finished (every check of the task passed), the
studio calls, the tokens read from tool results (characters / 4), the images fetched and the
wrong turns. Results go to <out>/results.json, transcripts to <out>/<agent>/<task>.log.

The scripted agents prove the checkers: `perfect` must finish every task and `careless` none.
The exit code is 1 when that does not hold, or when a model agent errored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tests/, so studio_eval imports

from studio_eval.harness import (
    FOOTER,
    Mcp,
    Site,
    Studio,
    Trace,
    claude_agent,
    tokens_read,
    unwrap,
    wrong_turns,
)
from studio_eval.tasks import Env, Task, all_tasks

SCRIPTED = ("perfect", "careless")


async def run_task(task: Task, agent: str, *, studio: Studio, site: Site, mcp: Mcp, out: Path, model: str,
                   timeout_s: float) -> dict[str, Any]:
    folder = site.lay_out(task.id, task.pages)
    env = Env(task_id=task.id, folder=folder, base=site.base(task.id), studio=studio, site=site, mcp=mcp)
    t0 = time.monotonic()
    error = None
    metrics: dict[str, Any] = {}
    try:
        if task.setup is not None:
            await task.setup(env)
    except Exception as e:  # noqa: BLE001 - a broken setup is the eval's fault: said, never hidden
        return {"task": task.id, "agent": agent, "finished": False, "error": f"setup failed: {e}",
                "checks": [], "calls": 0, "tokens_read": 0, "images": 0, "wrong_turns": []}
    if agent in SCRIPTED:
        env.agent = Trace(mcp=mcp)
        try:
            env.answer = await getattr(task, agent)(env)
        except Exception as e:  # noqa: BLE001
            error = f"the scripted agent stopped: {type(e).__name__}: {e}"
            if agent == "perfect":
                traceback.print_exc()
        env.rows = env.agent.rows
    else:
        prompt = task.render_prompt(env) + FOOTER.format(folder=folder, base=env.base)
        rows, env.answer, metrics, error = await claude_agent(
            prompt, studio=studio, folder=folder, model=model, transcript=out / f"{task.id}.log", timeout_s=timeout_s)
        env.rows = rows
    studio_rows = unwrap([r for r in env.rows if r.tool[:1].islower()])
    urls = (env.base, task.url(env))
    try:
        checks = await task.check(env)
    except Exception as e:  # noqa: BLE001
        checks = [(f"the checker failed: {e}", False)]
    return {
        "task": task.id, "agent": agent, "finished": bool(checks) and all(ok for _, ok in checks) and error is None,
        "checks": [{"check": name, "ok": ok} for name, ok in checks],
        "calls": len(studio_rows), "calls_by_tool": _by_tool(studio_rows), "tokens_read": tokens_read(studio_rows),
        "images": sum(r.images for r in studio_rows), "file_edits": sum(1 for r in env.rows if r.tool in ("Edit", "Write")),
        "wrong_turns": wrong_turns(studio_rows, images=task.images, urls=urls), "answer": env.answer, "error": error,
        "seconds": round(time.monotonic() - t0, 1), **({"metrics": metrics} if metrics else {}),
        "trace": [r.brief() for r in studio_rows],
    }


def _by_tool(rows: list[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r.tool] = out.get(r.tool, 0) + 1
    return out


async def run_agent(agent: str, tasks: list[Task], *, out: Path, model: str, timeout_s: float) -> list[dict]:
    where = out / agent
    where.mkdir(parents=True, exist_ok=True)
    results = []
    with Studio(where / "home") as studio, Site(where / "site") as site:
        async with Mcp(studio) as mcp:
            for task in tasks:
                print(f"  {agent}: {task.id} ...", flush=True)
                r = await run_task(task, agent, studio=studio, site=site, mcp=mcp, out=where, model=model,
                                   timeout_s=timeout_s)
                mark = "finished" if r["finished"] else "NOT finished"
                failed = [c["check"] for c in r["checks"] if not c["ok"]]
                print(f"    {mark}; {r['calls']} calls, ~{r['tokens_read']} tokens read, {r['images']} images"
                      + (f"; failed: {', '.join(failed)}" if failed else "")
                      + (f"; wrong turns: {'; '.join(r['wrong_turns'])}" if r["wrong_turns"] else "")
                      + (f"; error: {r['error']}" if r.get("error") else ""), flush=True)
                results.append(r)
    return results


def table(results: list[dict]) -> str:
    lines = ["| agent | task | finished | calls | tokens read | images | wrong turns |", "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['agent']} | {r['task']} | {'yes' if r['finished'] else 'no'} | {r['calls']} | "
                     f"{r['tokens_read']} | {r['images']} | {'; '.join(r['wrong_turns']) or '-'} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="The studio agent experience eval.")
    p.add_argument("--agent", nargs="+", default=list(SCRIPTED),
                   help="perfect, careless (scripted) and/or claude (Claude Code headless). Default: perfect careless")
    p.add_argument("--model", default="haiku", help="claude's model (haiku, sonnet, opus or a full id). Default haiku")
    p.add_argument("--task", nargs="*", help="task ids (default: all)")
    p.add_argument("--out", type=Path, help="where results and transcripts go (default: a new temporary folder)")
    p.add_argument("--timeout", type=float, default=420, help="seconds per task for a model agent")
    args = p.parse_args(argv)
    tasks = all_tasks()
    if args.task:
        tasks = [t for t in tasks if t.id in set(args.task)]
        if not tasks:
            p.error(f"no such task; tasks: {', '.join(t.id for t in all_tasks())}")
    out = args.out or Path(tempfile.mkdtemp(prefix="studio-eval-"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"studio eval: {len(tasks)} tasks, agents {', '.join(args.agent)}; results in {out}")
    results: list[dict] = []
    for agent in args.agent:
        results += asyncio.run(run_agent(agent, tasks, out=out, model=args.model, timeout_s=args.timeout))
    (out / "results.json").write_text(json.dumps({"model": args.model, "results": results}, indent=2, default=str))
    print()
    print(table(results))
    ok = True
    for agent in args.agent:
        mine = [r for r in results if r["agent"] == agent]
        done = sum(r["finished"] for r in mine)
        print(f"{agent}: {done} of {len(mine)} finished, {sum(r['calls'] for r in mine)} calls, "
              f"~{sum(r['tokens_read'] for r in mine)} tokens read, {sum(len(r['wrong_turns']) for r in mine)} wrong turns")
        if agent == "perfect" and done != len(mine):
            ok = False
        if agent == "careless" and done:
            ok = False
        if agent not in SCRIPTED and any(r.get("error") for r in mine):
            ok = False
    if set(SCRIPTED) <= set(args.agent):
        print("checkers: " + ("verified (perfect finishes every task, careless none)" if ok else "NOT verified"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
