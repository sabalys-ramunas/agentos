"""Run the environments eval (docs/EVALS.md): does openajar get better for the agents that use it?

    uv run python tests/env_eval/run.py                          # the free suite: perfect and careless
    uv run python tests/env_eval/run.py --agent claude --model haiku --task t01_fix_push_pr --repeat 3
    uv run python tests/env_eval/run.py --ref <sha>              # the same suite on another commit's openajar
    uv run python tests/env_eval/run.py compare [--against <sha>] [--agent perfect]
    uv run python tests/env_eval/run.py list                     # the tasks
    uv run python tests/env_eval/run.py smoke                    # the budgets, with no node and no model
    uv run python tests/env_eval/run.py clean                    # what a killed run left in Docker

Each task runs on a node of its own: openajar's local node with the docker provider in a scratch home
(tests/env_eval/node.py), the task's fixture repository as a local bare repository, a stand-in for
GitHub's pull request API (ghstub.py), and a recording MCP proxy in front (proxy.py) that every agent
goes through. Per task it records whether the checker passed, the wall time, the time to a ready
environment, every tool call, the tokens of tool output read, wrong turns, and for a model agent its
tokens, turns and cost. Results go to <out>/results.json and a table; a summary per agent is
appended to bench/evals.jsonl and compared with the run before it.

The exit code is 1 when the scripts do not hold (perfect must finish every task it ran, careless
none), a model agent errored, or (with --fail-on-regression, and always for `compare`) a metric
regressed past its threshold.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tests/, so env_eval imports

from env_eval import agents as agent_runs
from env_eval import budget, history
from env_eval.ghstub import GitHubStub
from env_eval.metrics import final_json, open_times, time_to_ready, trace_metrics
from env_eval.node import REPO, EvalNode, NodeFailed, docker_ready, sweep_stale
from env_eval.proxy import McpProxy, Recorder
from env_eval.tasks import Agent, Ctx, Task, all_tasks, has_node

SCRIPTED = budget.FREE_AGENTS
MODEL_AGENTS = ("claude", "codex", "cmd")
HISTORY = REPO / "bench" / "evals.jsonl"
DEFAULT_PORT = 7467  # the node; the proxy is port+1 and the GitHub stand-in port+2


def host_id() -> str:
    """This computer, as a short hash: the history may be shared, a machine's name stays out of it."""
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:10]


def git_state(where: Path) -> tuple[str, bool]:
    def out(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=where, capture_output=True, text=True, check=False).stdout.strip()

    return out("rev-parse", "--short=10", "HEAD") or "unknown", bool(out("status", "--porcelain", "--untracked-files=no"))


# ---- one session, one task ----

async def scripted_session(task: Task, kind: str, ctx: Ctx, i: int, url: str, token: str) -> tuple[str, str | None]:
    from openajar.dev.slotnode import mcp_client

    try:
        async with mcp_client(url, token, timeout=400) as client:
            await client.list_tools()   # as a client does when it connects: the surface it starts with
            return await getattr(task, kind)(ctx, Agent(client), i), None
    except Exception as e:  # noqa: BLE001 - a script that stopped is a result (careless often does)
        if kind == "perfect":
            traceback.print_exc()
        while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
            e = e.exceptions[0]  # the MCP client's task group wraps what the script raised
        return "", f"the {kind} script stopped: {type(e).__name__}: {str(e)[:300]}"


async def model_session(args: argparse.Namespace, task: Task, ctx: Ctx, i: int, url: str, token: str,
                        transcript: Path, cap_usd: float) -> tuple[str, dict[str, Any], str | None]:
    prompt = task.prompt(ctx, i)
    skill = agent_runs.skill_path(args.src)
    common = {"url": url, "token": token, "model": args.model, "skill": skill, "transcript": transcript,
              "timeout_s": task.timeout_s, "cwd": ctx.local, "host_tools": task.host_tools}
    if args.agent_kind == "claude":
        return await agent_runs.claude(prompt, cap_usd=cap_usd, max_turns=args.max_turns, **common)
    if args.agent_kind == "codex":
        return await agent_runs.codex(prompt, **common)
    return await agent_runs.command(args.agent_cmd, prompt, **common)


async def one_session(args: argparse.Namespace, task: Task, kind: str, ctx: Ctx, i: int, url: str, token: str,
                      transcript: Path, cap_usd: float) -> tuple[str, dict[str, Any], str | None]:
    """One session of any agent, with the harness's `during` step beside it (stopped when the session ends)."""
    during = task.sessions[i].during
    step = asyncio.create_task(during(ctx)) if during is not None else None
    try:
        if kind in SCRIPTED:
            answer, err = await scripted_session(task, kind, ctx, i, url, token)
            return answer, {}, err
        return await model_session(args, task, ctx, i, url, token, transcript, cap_usd)
    finally:
        if step is not None:
            step.cancel()
            await asyncio.gather(step, return_exceptions=True)
            if not step.cancelled() and step.exception() is not None:  # the harness's step broke: said, never hidden
                ctx.notes["during_error"] = f"{type(step.exception()).__name__}: {step.exception()}"


def _sum_metrics(parts: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for m in parts:
        for k, v in m.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                out.setdefault(k, v)
            else:
                out[k] = round(out.get(k, 0) + v, 6)
    return out


async def requires_missing(node: EvalNode, task: Task, url: str) -> list[str]:
    if not task.requires:
        return []
    from openajar.dev.slotnode import mcp_client, text_of

    async with mcp_client(url, node.owner_token, timeout=60) as c:
        listed = text_of(await c.call_tool("tools_more", {}))
    return [t for t in task.requires if t not in listed]


async def run_task(args: argparse.Namespace, task: Task, kind: str, rep: int, *, gh: GitHubStub, proxy: McpProxy,
                   out: Path, cap_usd: float) -> dict[str, Any]:
    base = {"task": task.id, "agent": kind, "model": args.model if kind not in SCRIPTED else None, "repeat": rep}
    if task.scripted_only and kind not in SCRIPTED:
        return {**base, "skipped": task.scripted_only}
    if task.host_tools and kind not in SCRIPTED and not args.host_shell:
        return {**base, "skipped": "it needs a shell on this computer to move files (curl, tar): --host-shell "
                                   "allows the commands the task lists"}
    if task.repo and not has_node():
        return {**base, "skipped": "the checker runs the repository's tests with node, which is not installed here"}
    where = out / kind / f"{task.id}-r{rep}"
    started = time.monotonic()
    rec = proxy.recorder
    session_metrics: list[dict[str, Any]] = []
    errors: list[str] = []
    gh.reset()
    with EvalNode(where, port=args.port, github_api=gh.url, settings=task.settings, src=args.src,
                  keep=args.keep) as node:
        ctx = Ctx(task=task, node=node, github=gh)
        if task.repo:
            ctx.repo = node.add_repo(task.repo)
            gh.repos[ctx.repo.full_name] = ctx.repo.bare
            ctx.notes["main"] = ctx.branches().get("main")
        node.serve()
        missing = await requires_missing(node, task, node.mcp_url)
        if missing:
            return {**base, "skipped": f"this openajar has no {', '.join(missing)}"}
        t0s: list[float] = []
        mark0 = rec.mark()
        if task.local is not None:
            ctx.local = where / "local"
            ctx.local.mkdir(parents=True, exist_ok=True)
            task.local(ctx.local)
        for i, session in enumerate(task.sessions):
            if session.before is not None:
                await session.before(ctx)
            mark = rec.mark()
            t0s.append(time.monotonic())
            answer, metrics, err = await one_session(args, task, kind, ctx, i, proxy.url, node.token,
                                                     where / f"session{i + 1}.jsonl", cap_usd)
            calls, _ = rec.since(mark)
            rows = [c.__dict__ for c in calls]
            ctx.session_rows.append(rows)
            ctx.rows += rows
            ctx.answers.append(answer)
            session_metrics.append(metrics)
            if err:
                errors.append(f"session {i + 1}: {err}")
        try:
            checks = await task.check(ctx)
        except Exception as e:  # noqa: BLE001 - a checker that breaks is the eval's fault: said, never hidden
            checks = [(f"the checker failed: {type(e).__name__}: {e}", False)]
        calls, listings = rec.since(mark0)
        with (where / "calls.jsonl").open("w") as f:  # every call with its whole result, to read a run back
            for c in calls:
                f.write(json.dumps({**c.brief(t0s[0] if t0s else 0.0), "args": c.args, "text": c.text},
                                   default=str) + "\n")
        seconds = round(time.monotonic() - started, 1)
        tm = trace_metrics(ctx.rows, new_environments=task.new_environments)
    leftover = node.containers()
    result = {
        **base, "title": task.title, "finished": bool(checks) and all(ok for _, ok in checks) and not errors,
        "checks": [{"check": name, "ok": ok} for name, ok in checks], "errors": errors, "seconds": seconds,
        "ready_s": time_to_ready(ctx.rows, t0s[0]) if t0s else None, "opens": open_times(ctx.rows), **tm,
        "surface_tokens": listings[0].tokens if listings else None,
        "surface_tools": listings[0].tools if listings else None, "instructions_tokens": rec.instructions_tokens,
        "answers": [final_json(a) or a[-600:] for a in ctx.answers], "leftover_containers": len(leftover),
        "trace": [c.brief(t0s[0] if t0s else 0.0) for c in calls],
    }
    if any(session_metrics):
        result["model_metrics"] = _sum_metrics(session_metrics)
    return result


def show(r: dict[str, Any]) -> str:
    if r.get("skipped"):
        return f"    skipped: {r['skipped']}"
    failed = [c["check"] for c in r["checks"] if not c["ok"]]
    m = r.get("model_metrics") or {}
    cost = f", ${m['cost_usd']:.3f}" if "cost_usd" in m else ""
    return (f"    {'finished' if r['finished'] else 'NOT finished'} in {r['seconds']} s (ready {r['ready_s']} s); "
            f"{r['calls']} calls, ~{r['tokens_read']} tokens read, {r['tool_errors']} errors{cost}"
            + (f"\n      failed: {'; '.join(failed)}" if failed else "")
            + (f"\n      wrong turns: {'; '.join(r['wrong_turns'])}" if r["wrong_turns"] else "")
            + (f"\n      errors: {'; '.join(r['errors'])}" if r.get("errors") else ""))


async def run_all(args: argparse.Namespace, tasks: list[Task], out: Path, cap_usd: float) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with GitHubStub(args.port + 2) as gh:
        async with McpProxy(f"http://127.0.0.1:{args.port}", args.port + 1, Recorder()) as proxy:
            for kind in args.agent:
                args.agent_kind = kind
                for task in tasks:
                    for rep in range(1, args.repeat + 1):
                        print(f"  {kind}: {task.id}" + (f" (repeat {rep})" if args.repeat > 1 else "") + " ...",
                              flush=True)
                        try:
                            r = await run_task(args, task, kind, rep, gh=gh, proxy=proxy, out=out, cap_usd=cap_usd)
                        except NodeFailed as e:
                            r = {"task": task.id, "agent": kind, "repeat": rep, "finished": False, "checks": [],
                                 "errors": [f"the node: {e}"], "seconds": 0, "ready_s": None, "calls": 0,
                                 "tokens_read": 0, "tool_errors": 0, "wrong_turns": [], "opens": []}
                        print(show(r), flush=True)
                        results.append(r)
                        (out / "results.json").write_text(json.dumps({"results": results}, indent=1, default=str))
    return results


# ---- the table and the verdict ----

def table(results: list[dict[str, Any]]) -> str:
    head = ("| agent | task | finished | seconds | ready s | opens s | calls | tokens read | errors | wrong turns "
            "| model tokens in/out/cache | cost |")
    lines = [head, "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if r.get("skipped"):
            lines.append(f"| {r['agent']} | {r['task']} | skipped | | | | | | | {r['skipped']} | | |")
            continue
        m = r.get("model_metrics") or {}
        tok = (f"{m.get('input_tokens', 0)}/{m.get('output_tokens', 0)}/{m.get('cache_read_tokens', 0)}"
               if m else "-")
        opens = " ".join(str(o["ready_s"]) for o in r.get("opens") or []) or "-"
        lines.append(f"| {r['agent']} | {r['task']} | {'yes' if r['finished'] else 'no'} | {r['seconds']} | "
                     f"{r['ready_s'] if r['ready_s'] is not None else '-'} | {opens} | {r['calls']} | "
                     f"{r['tokens_read']} | {r['tool_errors']} | {len(r['wrong_turns'])} | {tok} | "
                     f"{f'${m["cost_usd"]:.3f}' if 'cost_usd' in m else '-'} |")
    return "\n".join(lines)


def scripts_hold(results: list[dict[str, Any]]) -> bool:
    ran = [r for r in results if not r.get("skipped")]
    perfect = [r for r in ran if r["agent"] == "perfect"]
    careless = [r for r in ran if r["agent"] == "careless"]
    return all(r["finished"] for r in perfect) and not any(r["finished"] for r in careless)


def write_history(args: argparse.Namespace, results: list[dict[str, Any]], tasks: list[Task]) -> list[dict]:
    commit, dirty = git_state(args.tree)
    at = datetime.now(UTC).isoformat(timespec="seconds")
    recs = []
    for kind in args.agent:
        rec = history.record(results, agent=kind, model=args.model, commit=commit, dirty=dirty, host=host_id(),
                             at=at, repeat=args.repeat, ref=args.ref)
        rec["task_set"] = sorted(t.id for t in tasks)
        if rec["tasks"]:
            history.append(args.history, rec)
            recs.append(rec)
    return recs


def compare_lines(recs: list[dict], hist: list[dict], against: str | None) -> tuple[list[str], bool]:
    lines, regressed = [], False
    for rec in recs:
        base = history.pick([h for h in hist if h.get("task_set") == rec.get("task_set")], rec, against)
        who = rec["agent"] + (f" ({rec['model']})" if rec.get("model") else "")
        if base is None:
            lines.append(f"{who}: the first run of this kind here{' at ' + against if against else ''}, "
                         "nothing to compare")
            continue
        rows = history.compare(base, rec)
        bad = [r["metric"] for r in rows if r["regressed"]]
        regressed |= bool(bad)
        better = sum(r["verdict"] == "better" for r in rows)
        worse = sum(r["verdict"] == "worse" for r in rows)
        lines.append(f"{who}: {rec['commit']}{' (dirty)' if rec['dirty'] else ''} against {base['commit']}"
                     f"{' (dirty)' if base.get('dirty') else ''}: {better} better, {worse} worse"
                     + (f", regressed: {', '.join(bad)}" if bad else ""))
        lines += history.describe(rows)
    return lines, regressed


# ---- the commands ----

def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="The environments eval (docs/EVALS.md).")
    p.add_argument("--agent", nargs="+", default=list(SCRIPTED),
                   help="perfect, careless (free scripts), claude, codex, or cmd with --agent-cmd. Default: the scripts")
    p.add_argument("--model", default="haiku", help="the model agent's model (haiku, sonnet, opus or an id)")
    p.add_argument("--agent-cmd", help="a command for --agent cmd: {prompt} {mcp_config} {url} {token_file} {skill} "
                                       "{model} {transcript}")
    p.add_argument("--task", nargs="*", help="task ids or prefixes (t01): default all")
    p.add_argument("--repeat", type=int, default=1, help="runs of each task (medians go in the history)")
    p.add_argument("--max-usd", type=float, default=budget.DEFAULT_MAX_USD,
                   help=f"refuse a paid run estimated above this; also its hard cap (default {budget.DEFAULT_MAX_USD})")
    p.add_argument("--session-usd", type=float, help="your estimate of one session's cost, for a model the price "
                                                      "table does not know (a Codex model)")
    p.add_argument("--max-turns", type=int, default=agent_runs.MAX_TURNS, help="a claude session's turn limit")
    p.add_argument("--host-shell", action="store_true",
                   help="let a model agent run the shell commands a task lists (t13's curl and tar) on this computer; "
                        "without it such a task is skipped for a model")
    p.add_argument("--ref", help="run the node of this commit (a temporary worktree of it), under this harness")
    p.add_argument("--out", type=Path, help="results and transcripts (default: a new temporary folder)")
    p.add_argument("--keep", action="store_true", help="keep each node's home and repositories under --out")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="the node's port; the next two are used too")
    p.add_argument("--history", type=Path, default=HISTORY)
    p.add_argument("--no-history", action="store_true", help="do not append this run to the history")
    p.add_argument("--fail-on-regression", action="store_true")
    a = p.parse_args(argv)
    if "cmd" in a.agent and not a.agent_cmd:
        p.error("--agent cmd needs --agent-cmd")
    unknown = [x for x in a.agent if x not in (*SCRIPTED, *MODEL_AGENTS)]
    if unknown:
        p.error(f"unknown agent {unknown[0]}: perfect, careless, claude, codex or cmd")
    return a


def pick_tasks(asked: list[str] | None) -> list[Task]:
    tasks = all_tasks()
    if not asked:
        return tasks
    chosen = [t for t in tasks if any(t.id == x or t.id.startswith(x) for x in asked)]
    if not chosen:
        raise SystemExit(f"no such task; tasks: {', '.join(t.id for t in tasks)}")
    return chosen


@contextlib.contextmanager
def ref_tree(ref: str | None):
    """A temporary worktree of `ref` (its src/ is the openajar under test), removed on the way out."""
    if not ref:
        yield None
        return
    tmp = Path(tempfile.mkdtemp(prefix="env-eval-ref-"))
    done = subprocess.run(["git", "worktree", "add", "--detach", str(tmp / "tree"), ref], cwd=REPO,
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"git worktree add {ref}: {done.stderr.strip()}")
    try:
        yield tmp / "tree"
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(tmp / "tree")], cwd=REPO, capture_output=True,
                       check=False)


def _interrupt(*_: object) -> None:
    raise KeyboardInterrupt  # a SIGTERM cleans up as Ctrl-C does


def cmd_run(argv: list[str]) -> int:
    args = parse(argv)
    tasks = pick_tasks(args.task)
    problem = docker_ready()
    if problem:
        print(f"env eval: {problem}", file=sys.stderr)
        return 2
    sessions = sum(len(t.sessions) for t in tasks) * args.repeat
    cap = 0.0
    for kind in args.agent:
        try:
            est, each = budget.guard(kind, args.model, sessions, args.max_usd, args.session_usd)
        except budget.OverBudget as e:
            print(f"env eval: refused: {e}", file=sys.stderr)
            return 2
        if kind not in SCRIPTED:
            cap = each
            print(f"{kind} on {args.model}: {sessions} sessions, estimated at most ${est:.2f} "
                  f"(each session capped at ${each:.2f}; --max-usd {args.max_usd:.2f})", flush=True)
    for kind in args.agent:
        missing = agent_runs.signed_in(kind)
        if missing:
            print(f"env eval: the {kind} agent cannot run: {missing}", file=sys.stderr)
            return 2
    out = args.out or Path(tempfile.mkdtemp(prefix="env-eval-"))
    out.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGINT, signal.default_int_handler)  # Ctrl-C, even when started with it ignored
    signal.signal(signal.SIGTERM, _interrupt)
    with ref_tree(args.ref) as tree:
        args.src = tree / "src" if tree else None
        args.tree = tree or REPO
        print(f"env eval: {len(tasks)} tasks, agents {', '.join(args.agent)}"
              + (f", openajar at {args.ref}" if args.ref else "") + f"; results in {out}", flush=True)
        try:
            results = asyncio.run(run_all(args, tasks, out, cap))
        except KeyboardInterrupt:
            print("env eval: stopped; every node of this run was cleaned up", file=sys.stderr)
            return 130
    (out / "results.json").write_text(json.dumps({"model": args.model, "ref": args.ref, "results": results},
                                                 indent=1, default=str))
    print()
    print(table(results))
    ok = True
    for kind in args.agent:
        mine = [r for r in results if r["agent"] == kind and not r.get("skipped")]
        cost = sum((r.get("model_metrics") or {}).get("cost_usd", 0) for r in mine)
        print(f"{kind}: {sum(r['finished'] for r in mine)} of {len(mine)} finished, "
              f"{sum(r['calls'] for r in mine)} calls, ~{sum(r['tokens_read'] for r in mine)} tokens read, "
              f"{sum(len(r['wrong_turns']) for r in mine)} wrong turns, {sum(r['seconds'] for r in mine):.0f} s"
              + (f", ${cost:.3f}" if kind not in SCRIPTED else ""))
        if kind not in SCRIPTED and any(r.get("errors") for r in mine):
            ok = False
    if set(SCRIPTED) & set(args.agent):
        holds = scripts_hold(results)
        ok &= holds
        print("checkers: " + ("verified (perfect finishes every task, careless none)" if holds else "NOT verified"))
    if not args.no_history:
        hist_before = history.load(args.history)
        recs = write_history(args, results, tasks)
        lines, regressed = compare_lines(recs, hist_before + recs, None)
        print("\n".join(["", *lines]) if lines else "")
        if args.fail_on_regression and regressed:
            ok = False
    return 0 if ok else 1


def cmd_compare(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Compare the newest eval run with the one before it (or --against).")
    p.add_argument("--against", help="a commit (or the --ref a run was made with)")
    p.add_argument("--agent", default="perfect")
    p.add_argument("--model", default=None)
    p.add_argument("--history", type=Path, default=HISTORY)
    p.add_argument("--all", action="store_true", help="every metric, not only the ones that changed")
    a = p.parse_args(argv)
    hist = history.load(a.history)
    mine = [h for h in hist if h.get("agent") == a.agent and (a.model is None or h.get("model") == a.model)]
    if not mine:
        print(f"no runs of {a.agent} in {a.history}")
        return 0
    newest = mine[-1]
    base = history.pick([h for h in mine if h.get("task_set") == newest.get("task_set")], newest, a.against)
    if base is None:
        print(f"{newest['commit']}: nothing to compare it with")
        return 0
    rows = history.compare(base, newest)
    print(f"{newest['commit']}{' (dirty)' if newest.get('dirty') else ''} against {base['commit']}"
          f"{' (dirty)' if base.get('dirty') else ''} ({a.agent}{', ' + newest['model'] if newest.get('model') else ''})")
    print("\n".join(history.describe(rows, only_changes=not a.all)) or "  nothing changed past its noise floor")
    bad = [r["metric"] for r in rows if r["regressed"]]
    if bad:
        print(f"regressed: {', '.join(bad)}")
        return 1
    return 0


def cmd_smoke() -> int:
    """The smoke checks (smoke.py): no node, no Docker, no model; exit 1 when one is over its budget."""
    from env_eval import smoke

    checks = smoke.run()
    print(smoke.show(checks))
    return 0 if all(c["ok"] for c in checks.values()) else 1


def cmd_list() -> int:
    for t in all_tasks():
        extra = f" (needs {', '.join(t.requires)})" if t.requires else ""
        print(f"{t.id:28} {len(t.sessions)} session{'s' if len(t.sessions) > 1 else ''}  {t.title}{extra}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["compare"]:
        return cmd_compare(argv[1:])
    if argv[:1] == ["list"]:
        return cmd_list()
    if argv[:1] == ["smoke"]:
        return cmd_smoke()
    if argv[:1] == ["clean"]:
        print(f"removed: {sweep_stale()}")
        return 0
    return cmd_run(argv)


if __name__ == "__main__":
    sys.exit(main())
