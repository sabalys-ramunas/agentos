"""A risky change with a checkpoint: try it, and roll back if the tests fail, saying what broke.

Needs environment_checkpoint and environment_rollback; on an agentos without them the task is skipped.
The change asked for (amounts in euros as decimals) breaks the ledger's tests when it is made the quick
way, so one right end is the code as it was, tests passing, and the human told why. A careful model can
make the change so the tests pass (Haiku did, 2026-11-18): that is a right end too, with the tests
passing after the last change and the human told they pass. Either way the checkpoint comes first.
"""

from __future__ import annotations

from env_eval.metrics import parsed
from env_eval.tasks import Agent, Check, Ctx, Session, Task, done

OLD = '  return entries.reduce((sum, e) => sum + (e.kind === "in" ? e.cents : -e.cents), 0);'
NEW = '  return entries.reduce((sum, e) => sum + (e.kind === "in" ? e.cents : -e.cents) / 100, 0);'


def _steps(ctx: Ctx) -> list[dict]:
    from env_eval.metrics import unwrap

    return [unwrap(x) for x in ctx.rows]


def _first(steps: list[dict], tools: tuple[str, ...]) -> int | None:
    return next((i for i, r in enumerate(steps) if r["tool"] in tools and r.get("ok")), None)


def _test_runs(steps: list[dict]) -> list[tuple[int, bool]]:
    """(index, passed) of each test run through terminal_exec, by node --test's own count."""
    out = []
    for i, r in enumerate(steps):
        if r["tool"] == "terminal_exec" and "test" in str(r["args"].get("cmd") or ""):
            got = parsed(r)
            out.append((i, got.get("exit_code") == 0 and "# fail 0" in str(got.get("stdout") or "")))
    return out


async def check(ctx: Ctx) -> list[Check]:
    steps = _steps(ctx)
    checkpoint = _first(steps, ("environment_checkpoint",))
    edits = [i for i, r in enumerate(steps) if r["tool"] in ("file_edit", "file_write") and r.get("ok")]
    rolled = [i for i, r in enumerate(steps) if r["tool"] == "environment_rollback" and r.get("ok")]
    runs = _test_runs(steps)
    last_change = max([*edits, *rolled], default=-1)
    ended_passing = bool(runs) and runs[-1][0] > last_change and runs[-1][1]
    answer = ctx.answer().lower()
    if rolled:  # the tests failed, so it went back: the answer says what broke
        outcome = ("it rolled back and said the tests failed", "fail" in answer)
    else:  # it made the change so the tests pass, and kept it
        outcome = ("it kept a change that passes the tests and said so", bool(edits) and "pass" in answer)
    pushed_broken = [b for b in ctx.new_branches() if not ctx.tests_pass(b)[0]]
    return [
        ("a checkpoint was made before the first change", checkpoint is not None
         and (not edits or checkpoint < edits[0])),
        ("the tests ran and passed after the last change or rollback", ended_passing),
        outcome,
        ("no branch with failing tests was pushed", not pushed_broken),
        ("main is as it was", ctx.branches().get("main") == ctx.notes.get("main")),
        *await ctx.closed_and_gone(),
    ]


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    env = await a.open(title="Ledger in euros", repo=ctx.repo.slug, keep="none")
    cp = await a.on_call("environment_checkpoint", note="before amounts in euros")
    await a.on_call("file_edit", path="src/ledger.js", old=OLD, new=NEW)
    broke = await a.sh("npm test 2>&1 | tail -12", cwd=env["workdir"])
    await a.on_call("environment_rollback", checkpoint=cp["checkpoint"])
    await a.sh("npm test 2>&1 | tail -8", cwd=env["workdir"])
    await a.on_call("environment_close")
    failed = "fail" if "fail" in str(broke) else "did not fail"
    return done(f"Amounts in euros made the ledger tests {failed} (balance is no longer whole cents, so format() "
                "breaks); I rolled back to the checkpoint and the tests pass again. Nothing was pushed.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """No checkpoint: changes the code, sees the failure, and undoes it by hand."""
    env = await a.open(title="Ledger in euros", repo=ctx.repo.slug)
    await a.on_call("file_edit", path="src/ledger.js", old=OLD, new=NEW)
    await a.sh("npm test 2>&1 | tail -5; git checkout -- src/ledger.js", cwd=env["workdir"])
    await a.on_call("environment_close")
    return done("It did not work, so I put it back.")


TASK = Task(
    id="t06_checkpoint_rollback",
    title="a risky change: checkpoint, try, roll back when the tests fail",
    repo="ledger",
    requires=("environment_checkpoint", "environment_rollback"),
    sessions=[Session("In agentos, try changing {repo} to keep amounts in euros (decimals) instead of cents. It is "
                      "risky: make a checkpoint first, and if the tests fail afterwards, roll back and tell me what "
                      "broke.")],
    check=check, perfect=perfect, careless=careless,
)
