"""A limited network: the package comes from npm (allowed), the script reaches a host that is not.

The node's environments.toml says `network = "limited"`, as an owner sets it. npm's registry is on
the list, rates.example.com is not: the node's proxy refuses it with a 403. Over HTTPS curl shows
only "CONNECT tunnel failed, response 403" (the refusal's words are in the proxy's answer, which
curl does not print), so the command's result names it: `network.blocked_now` and the owner's
command (since 2026-11-18; before, the agent had to call environment()). Finishing is installing,
running the script, and telling the human which host was refused and the command that allows it,
instead of retrying.
"""

from __future__ import annotations

from env_eval.tasks import Agent, Check, Ctx, Session, Task, done

HOST = "rates.example.com"
FIX = f"agentos environments allow --host {HOST}"
MAX_RUNS = 2
STARTED = "Fetching today's rates"


def _cmds(ctx: Ctx) -> list[str]:
    return [str((r.get("args") or {}).get("cmd") or "") for r in ctx.calls("terminal_exec") + ctx.calls("terminal_job_start")]


async def check(ctx: Ctx) -> list[Check]:
    outputs = [str(r.get("text") or "") for r in ctx.calls("terminal_exec") + ctx.calls("terminal_job_read") if r.get("ok")]
    installed = any(STARTED in o for o in outputs)  # the script prints it once its dependency loaded
    refused = any(b.get("host") == HOST for e in ctx.environments() for b in (e.blocked or []))
    runs = [c for c in _cmds(ctx) if "rates" in c and "install" not in c]
    answer = ctx.answer()
    return [
        ("npm installed the dependency (the registry is allowed)", installed),
        (f"the node refused {HOST}", refused),
        (f"the script ran at most {MAX_RUNS} times (no retry loop)", 0 < len(runs) <= MAX_RUNS),
        ("the answer names the refused host", HOST in answer),
        ("the answer gives the owner's command", FIX in answer),
        *await ctx.closed_and_gone(),
    ]


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    env = await a.open(title="Rates script", repo=ctx.repo.slug, keep="none")
    await a.sh("npm install 2>&1 | tail -3", cwd=env["workdir"])
    out = await a.sh("npm run rates 2>&1", cwd=env["workdir"])
    if "403" not in str(out):
        return done(f"npm run rates did not stop at a 403: {str(out)[:200]}", "blocked")
    net = out.get("network") or {}  # the command's own result names what was refused
    hosts = ", ".join(net.get("blocked_now") or [])
    if not hosts:  # an agentos before network.blocked_now: environment() lists it
        net = (await a.on_call("environment")).get("network") or {}
        hosts = ", ".join(b["host"] for b in net.get("blocked") or [])
    await a.on_call("environment_close")
    return done(f"npm install worked; `npm run rates` stopped at a 403: this environment's network refused {hosts}. "
                f"To let it through, {net.get('fix')}.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Takes the 403 for a flaky service and tries again, then blames the service."""
    env = await a.open(title="Rates script", repo=ctx.repo.slug)
    await a.sh("npm install 2>&1 | tail -3", cwd=env["workdir"])
    for _ in range(3):
        await a.sh("npm run rates 2>&1 || sleep 5", cwd=env["workdir"])
    await a.on_call("environment_close")
    return done("The rates service is down right now; try again later.")


TASK = Task(
    id="t05_blocked_host",
    title="a limited network: npm allowed, a blocked host reported with its command",
    repo="rates",
    settings={"network": "limited"},
    internet=True,
    sessions=[Session("In agentos, install the dependencies of {repo} and run `npm run rates`. Tell me what it "
                      "prints.")],
    check=check, perfect=perfect, careless=careless,
)
