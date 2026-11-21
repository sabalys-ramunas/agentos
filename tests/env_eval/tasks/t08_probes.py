"""Probes: one call each, for behaviour a tester once found wrong (merged from the evals/ suite of 2026-11-20).

The scripts only: `perfect` makes the calls on a machine of its own and the checker reads what came back;
`careless` makes none. A probe pins what must keep working (the review of 2026-11-20 found the first four
wrong: P1-6, P1-7 and P2, all fixed since). Every bug a tester finds that one call can show belongs here.
"""

from __future__ import annotations

from env_eval.tasks import Agent, Check, Ctx, Session, Task, done


async def _probe(a: Agent, cmd: str, **args) -> tuple[bool, str]:
    return await a.raw("terminal_exec", {"on": a.on, "cmd": cmd, **args})


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    await a.open(title="Probes", keep="none")
    got: dict[str, tuple[bool, str]] = {}
    got["timeout"] = await _probe(a, "sleep 8", timeout_s=2)
    got["pwd"] = await _probe(a, "pwd")
    await a.sh("mkdir -p /work/probe-dir")
    got["relative"] = await _probe(a, "pwd", cwd="probe-dir")
    got["missing_cwd"] = await _probe(a, "ls", cwd="/work/no-such-folder")
    got["big"] = await _probe(a, "python3 -c \"print('x' * 2000000)\"")
    got["stdin"] = await _probe(a, "python3 -c \"input()\"", timeout_s=20)
    ctx.notes["probes"] = got
    await a.on_call("environment_close")
    return done("Probes run.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Opens a machine, runs nothing it was asked to, and closes it."""
    await a.open(title="Probes", keep="none")
    await a.on_call("environment_close")
    return done("Looks fine.")


async def check(ctx: Ctx) -> list[Check]:
    import json

    got = ctx.notes.get("probes") or {}

    def result(name: str) -> dict:
        ok, text = got.get(name, (False, ""))
        try:
            data = json.loads(text) if ok else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    timeout = got.get("timeout", (False, ""))[1]
    missing = got.get("missing_cwd", (True, ""))
    big = result("big")
    return [
        ("the probes ran", len(got) == 6),
        ("a command cut off by its time limit says what to do next, without the wrapper's own line",
         "terminal_job_start" in timeout and "setsid:" not in timeout),
        ("a command with no cwd starts in the work folder", result("pwd").get("stdout", "").strip().startswith("/work")),
        ("a relative cwd starts at the work folder", result("relative").get("stdout", "").strip() == "/work/probe-dir"),
        ("a cwd that does not exist is an error with a fix line", not missing[0] and "fix" in missing[1].lower()),
        ("two million characters come back cut, and marked", bool(big.get("truncated")) and len(big.get("stdout", "")) < 140000),
        ("a command that reads input with none given fails at once",
         result("stdin").get("exit_code") not in (0, None) and not result("stdin").get("timed_out")),
        *await ctx.closed_and_gone(),
    ]


TASK = Task(
    id="t08_probes",
    title="probes: one call each, for what testers found wrong",
    sessions=[Session("(the scripts only)")],
    check=check, perfect=perfect, careless=careless,
    scripted_only="the probes are calls, not a request a person would type",
)
