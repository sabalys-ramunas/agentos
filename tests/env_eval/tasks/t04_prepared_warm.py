"""A repository with .openajar/environment.toml, opened three times: cold, from its prepared machine, warm.

The same small question three times, as three chats would ask it. Between them the harness waits
for what the node does on its own: after the first open it builds the prepared machine in the
background (the harness waits for the build), and before the third it turns the warm pool on
(`warm = 1` in environments.toml, as `openajar environments allow --warm 1` writes it) and waits
until a warm machine is booted. The three open times are the result to watch (open1_s, open2_s and
open3_s in the history); the checker asks that the app listened at each open, that the second
came from the prepared machine and the third from the warm pool.
"""

from __future__ import annotations

from env_eval.tasks import Agent, Check, Ctx, Session, Task, done, until

PRICE = "12.50"
PROMPT = "In openajar, open the storefront ({repo}) and tell me the first price on its home page. Close it when done."
BUILD_S = 420
WARM_S = 300


async def build_done(ctx: Ctx) -> None:
    await until("the prepared machine's build", lambda: any(p.state == "ready" for p in ctx.node.prepared()),
                BUILD_S)


async def pool_on(ctx: Ctx) -> None:
    from openajar.dev.slotnode import mcp_client

    ctx.node.set_settings(warm=1)
    async with mcp_client(ctx.node.mcp_url, ctx.node.token, timeout=60) as c:  # a chat connects: the pool fills
        await c.call_tool("environment", {})
    await until("a warm machine", ctx.node.warm_ready, WARM_S)


async def check(ctx: Ctx) -> list[Check]:
    from env_eval.metrics import open_times

    opens = open_times(ctx.rows)
    ctx.node.set_settings(warm=0)
    return [
        ("three environments were opened", len(opens) == 3),
        ("the app listened at each open", len(opens) == 3 and all(o["app_listening"] for o in opens)),
        ("the second open booted from the prepared machine", len(opens) > 1 and opens[1]["prepared"]),
        ("the third open took the warm machine", len(opens) > 2 and opens[2]["warm"]),
        ("every answer names the first price", len(ctx.answers) == 3 and all(PRICE in a for a in ctx.answers)),
        *await ctx.closed_and_gone(),
    ]


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    env = await a.open(title=f"Storefront price {session + 1}", repo=ctx.repo.slug, keep="none")
    if not (env.get("app") or {}).get("listening"):
        return done(f"the app did not listen: {(env.get('app') or {}).get('why')}", "blocked")
    await a.on_call("browser_navigate", url="http://localhost:3000")
    snap = await a.on_call("browser_snapshot")
    first = PRICE if PRICE in str(snap) else "not shown"
    await a.on_call("environment_close")
    return done(f"The first price on the home page is €{first} (Linen shirt). The environment is closed.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Leaves each machine running ("I may look again"), so no later open is a fresh one."""
    env = await a.open(title="Storefront price", repo=ctx.repo.slug, take_over=True)
    await a.on_call("browser_navigate", url="http://localhost:3000")
    await a.on_call("browser_snapshot")
    return done(f"The first price is €{PRICE}." if env else "")


TASK = Task(
    id="t04_prepared_warm",
    title="open a repository with environment.toml cold, then prepared, then warm",
    repo="storefront",
    sessions=[Session(PROMPT), Session(PROMPT, before=build_done), Session(PROMPT, before=pool_on)],
    check=check, perfect=perfect, careless=careless, new_environments=3,
)
