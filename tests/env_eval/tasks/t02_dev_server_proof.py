"""Start the dev server, look at the page in the environment's browser, keep a proof for the human."""

from __future__ import annotations

from env_eval.tasks import Agent, Check, Ctx, Session, Task, done

APP_WORDS = "Autumn sale"
BROWSER_TOOLS = ("browser_navigate", "browser_snapshot", "browser_wait")


async def check(ctx: Ctx) -> list[Check]:
    envs = ctx.environments()
    proofs = [p for e in envs for p in ctx.node.proofs(e.id)]
    of_app = [p for p in proofs if str(p.get("page_url") or "").startswith(("http://localhost:3000",
                                                                            "http://127.0.0.1:3000"))]
    seen = any(APP_WORDS in str(r.get("text") or "") for t in BROWSER_TOOLS for r in ctx.calls(t) if r.get("ok"))
    return [
        ("a proof with a title was kept", any(str(p.get("title") or "").strip() and p.get("png_bytes", 0) > 1000
                                              for p in proofs)),
        ("the proof is of the app on port 3000", bool(of_app)),
        ("the app answered in the environment's browser", seen),
        *await ctx.closed_and_gone(),
    ]


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    env = await a.open(title="Shop home page", repo=ctx.repo.slug, keep="none")
    job = await a.on_call("terminal_job_start", cmd="npm run dev", cwd=env["workdir"])
    await a.on_call("terminal_job_read", job=job["id"], until_port=3000, wait_ms=60000)
    await a.on_call("browser_navigate", url="http://localhost:3000")
    await a.on_call("browser_snapshot")
    shot = await a.on_call("browser_screenshot", proof="shop home page with the autumn sale")
    url = (shot.get("proof") or {}).get("url") if isinstance(shot, dict) else None
    await a.on_call("terminal_job_cancel", job=job["id"])
    await a.on_call("environment_close")
    return done(f"The home page shows the autumn sale with three prices; proof: {url}. The environment is closed.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Starts the server inside terminal_exec and sleeps for it, and takes a screenshot without keeping it."""
    env = await a.open(title="Shop home page", repo=ctx.repo.slug)
    await a.sh("(npm run dev > /tmp/dev.log 2>&1 &) ; sleep 3; curl -s localhost:3000 | head -5", cwd=env["workdir"])
    await a.on_call("browser_navigate", url="http://localhost:3000")
    await a.on_call("browser_screenshot")
    await a.on_call("environment_close")
    return done("The page looks fine.")


TASK = Task(
    id="t02_dev_server_proof",
    title="start the dev server, check the page in the browser, keep a proof",
    repo="shop",
    sessions=[Session("In agentos, start the dev server of {repo}, check its home page in the browser and keep a "
                      "screenshot as proof for me.")],
    check=check, perfect=perfect, careless=careless,
)
