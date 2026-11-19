"""Fix a failing test, push a branch, open the pull request, close: the loop most work is."""

from __future__ import annotations

from env_eval.tasks import Agent, Check, Ctx, Session, Task, done

FIX_OLD = "totalCents * percent / 100"
FIX_NEW = "totalCents * (100 - percent) / 100"


async def check(ctx: Ctx) -> list[Check]:
    branches = ctx.new_branches()
    pulls = ctx.github.opened(ctx.repo.full_name)
    passing = [b for b in branches if ctx.tests_pass(b)[0]]
    main_moved = ctx.branches().get("main") != ctx.notes.get("main")
    return [
        ("a new branch is in the repository", bool(branches)),
        ("the tests pass at its head in a clean checkout", bool(passing)),
        ("a pull request was opened for that branch", any(p["head"]["ref"] in passing for p in pulls)),
        ("main is as it was", not main_moved),
        *await ctx.closed_and_gone(),
    ]


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    env = await a.open(title="Fix the cart discount", repo=ctx.repo.slug, keep="none")
    wd = env["workdir"]
    await a.sh("npm test 2>&1 | tail -15", cwd=wd)
    await a.on_call("file_read", path="src/price.js")
    await a.on_call("file_edit", path="src/price.js", old=FIX_OLD, new=FIX_NEW)
    await a.sh("npm test 2>&1 | tail -8", cwd=wd)
    await a.sh("git checkout -q -b fix-cart-discount && git commit -qam 'Take the discount off the total' && "
               "git push -q origin fix-cart-discount 2>&1", cwd=wd)
    pr = await a.via("environment_pull_request", title="Take the discount off the cart total",
                     body="discounted() returned the discount instead of the price after it. npm test passes.")
    await a.on_call("environment_close")
    return done(f"Fixed discounted(), pushed fix-cart-discount, opened {pr['url']}; the environment is closed.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Reads and edits through the shell, opens the pull request, and leaves the machine running."""
    env = await a.open(title="Fix the cart discount", repo=ctx.repo.slug)
    wd = env["workdir"]
    await a.sh("cat src/price.js test/price.test.js", cwd=wd)
    pattern = FIX_OLD.replace("*", "\\*")
    await a.sh(f"sed -i 's|{pattern}|{FIX_NEW}|' src/price.js && npm test 2>&1 | tail -5", cwd=wd)
    await a.sh("git checkout -q -b fix && git commit -qam fix && git push -q origin fix 2>&1", cwd=wd)
    pr = await a.via("environment_pull_request", title="fix")
    return done(f"Opened {pr['url']}.")


TASK = Task(
    id="t01_fix_push_pr",
    title="fix a failing test, push a branch, open the pull request, close",
    repo="calc",
    sessions=[Session("In agentos, fix the failing tests in {repo}, push the fix on a branch and open the pull "
                      "request.")],
    check=check, perfect=perfect, careless=careless,
)
