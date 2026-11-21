"""Browsing: a value that is not on the first page (a consent overlay, a catalogue in pages with a search
box). On the shop the agent starts on its machine; the checker wants the price in a page the browser
showed and in the answer (merged from the evals/ suite of 2026-11-20, `browse-find-price`). The machine is
not needed afterwards: the agent closes it."""

from __future__ import annotations

from env_eval.outline import act, goto
from env_eval.tasks import Agent, Check, Ctx, Session, Task, done
from env_eval.tasks.t10_checkout_form import SHOP

SEED = 7
URL = "http://localhost:8800"
TARGET = SHOP.target(SEED)
PRICE = SHOP.money(TARGET["price_cents"])
BROWSER = ("browser_navigate", "browser_act", "browser_snapshot", "browser_wait")


async def check(ctx: Ctx) -> list[Check]:
    shown = any(TARGET["name"] in str(r.get("text") or "") and f"{PRICE} EUR" in str(r.get("text") or "")
                for t in BROWSER for r in ctx.calls(t) if r.get("ok"))
    return [
        ("the browser showed the product with its price", shown),
        ("the answer has the right price", PRICE in ctx.answer()),
        *await ctx.closed_and_gone(),
    ]


async def _catalogue(ctx: Ctx, a: Agent) -> str:
    env = await a.open(title="Shop price", repo=ctx.repo.slug, keep="none")
    job = await a.on_call("terminal_job_start", cmd=f"python3 serve.py --seed {SEED}", cwd=env["workdir"])
    await a.on_call("terminal_job_read", job=job["id"], until_port=8800, wait_s=60)
    outline = await goto(a, URL + "/catalogue")
    if "only necessary" in outline.lower():
        outline = await act(a, outline, "click", "Only necessary", role="button")
    return outline


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    outline = await _catalogue(ctx, a)
    page = 1
    while TARGET["name"] not in outline and page < 5:
        page += 1
        outline = await goto(a, f"{URL}/catalogue?page={page}")
    await a.on_call("environment_close")
    return done(f"The {TARGET['name']} costs {PRICE} EUR in the shop's catalogue. The environment is closed.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Reads the first page only and answers with the nearest name it sees there."""
    outline = await _catalogue(ctx, a)
    noun = TARGET["name"].split()[-1]
    near = next((line for line in outline.splitlines() if noun in line and "EUR" in line), "")
    guess = near.split("EUR")[0].split()[-1] if near else "unknown"
    await a.on_call("environment_close")
    return done(f"A {noun.lower()} costs {guess} EUR in the catalogue. The environment is closed.")


TASK = Task(
    id="t15_find_price",
    title="find a value past the first page: a consent overlay, pages, a search box",
    repo="shopsite",
    sessions=[Session(f"In openajar, start the shop in {{repo}} (`python3 serve.py --seed {SEED}`, it listens on port "
                      f"8800) and use its browser: what does the \"{TARGET['name']}\" cost in its catalogue?")],
    check=check, perfect=perfect, careless=careless,
)
