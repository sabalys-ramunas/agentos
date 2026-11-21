"""An error to recover from: a build longer than a command waits by default (60 s). The first try comes back
cut off; the agent reads why and runs it another way: a longer timeout_s, or a job it reads to the end
(merged from the evals/ suite of 2026-11-20, `recover-slow-command`)."""

from __future__ import annotations

import hashlib

from env_eval.tasks import Agent, Check, Ctx, Session, Task, done

N = 11
BUILD_ID = hashlib.sha256(f"release-{N}".encode()).hexdigest()[:12]


async def check(ctx: Ctx) -> list[Check]:
    seen = any(f"BUILD_ID={BUILD_ID}" in str(r.get("text") or "")
               for t in ("terminal_exec", "terminal_job_read") for r in ctx.calls(t) if r.get("ok"))
    return [
        ("the build ran to its end on the machine", seen),
        ("the answer has the build id", BUILD_ID in ctx.answer()),
        *await ctx.closed_and_gone(),
    ]


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    env = await a.open(title="Release build", repo=ctx.repo.slug, keep="none")
    job = await a.on_call("terminal_job_start", cmd=f"sh build.sh {N}", cwd=env["workdir"])
    await a.on_call("terminal_job_read", job=job["id"], until_exit=True, wait_s=120)
    await a.on_call("environment_close")
    return done(f"The release build finished: build id {BUILD_ID}. The environment is closed.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Runs it with the default wait, reads the cut-off result as a failure, and gives up."""
    env = await a.open(title="Release build", repo=ctx.repo.slug, keep="none")
    await a.sh(f"sh build.sh {N}", cwd=env["workdir"], timeout_s=60)
    await a.on_call("environment_close")
    return done("The build hung, so I stopped it.", status="blocked")


TASK = Task(
    id="t09_slow_command",
    title="a build longer than the default wait: run it as a job and read it to the end",
    repo="slowbuild",
    sessions=[Session("In openajar, run the release build of {repo} (`sh build.sh 11`) and tell me the build id "
                      "it prints at the end.")],
    check=check, perfect=perfect, careless=careless,
)
