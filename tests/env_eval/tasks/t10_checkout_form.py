"""Browsing: an order with every kind of form field (a number, text, email, a select, radio buttons, check
boxes) on a shop the agent starts on its machine, and a checker that reads what the shop itself recorded.
The select in a form is the case 0.5.0's form_changed fix is for (merged from the evals/ suite of
2026-11-20, `browse-checkout-form`). The agent leaves the machine open for the checker; the harness removes it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from env_eval.outline import act, goto
from env_eval.tasks import Agent, Check, Ctx, Session, Task, done

SEED = 3
URL = "http://localhost:8800"


def _shop():
    spec = importlib.util.spec_from_file_location("shopsite_data", Path(__file__).parents[1] / "repos" / "shopsite" /
                                                  "data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SHOP = _shop()
ORDER = SHOP.order(SEED)


def _prompt() -> str:
    first, second = ORDER["lines"]
    return (f"In openajar, start the shop in {{repo}} (`python3 serve.py --seed {SEED}`, it listens on port 8800) and "
            f"order with its browser: {first['qty']} of \"{first['name']}\" and 1 of \"{second['name']}\". At the "
            f"checkout: full name {ORDER['name']}, email {ORDER['email']}, country {ORDER['country']}, "
            f"{ORDER['delivery']} delivery, {'with' if ORDER['gift'] else 'no'} gift wrap. Accept the terms and place "
            "the order. Tell me the order number, and leave the machine open: I want to look at it.")


async def check(ctx: Ctx) -> list[Check]:
    code, out = ctx.on_machine(f"curl -s --max-time 10 {URL}/_state")
    try:
        state = json.loads(out) if code == 0 else None
    except ValueError:
        state = None
    orders = (state or {}).get("orders") or []
    got = orders[0] if orders else {}
    lines = sorted((line["id"], line["qty"]) for line in got.get("lines", []))
    return [
        ("the shop runs on the machine left open", state is not None),
        ("exactly one order was placed", len(orders) == 1),
        ("the right products and quantities", lines == sorted((line["id"], line["qty"]) for line in ORDER["lines"])),
        ("the buyer's details", (got.get("name"), got.get("email"), got.get("country")) ==
         (ORDER["name"], ORDER["email"], ORDER["country"])),
        ("delivery and gift wrap", (got.get("delivery"), got.get("gift")) == (ORDER["delivery"], ORDER["gift"])),
        ("the answer has the order number", bool(got) and str(got.get("number")) in ctx.answer()),
    ]


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    env = await a.open(title="Shop order", repo=ctx.repo.slug, keep="none")
    job = await a.on_call("terminal_job_start", cmd=f"python3 serve.py --seed {SEED}", cwd=env["workdir"])
    await a.on_call("terminal_job_read", job=job["id"], until_port=8800, wait_s=60)
    outline = await goto(a, URL + "/")
    if "only necessary" in outline.lower():
        await act(a, outline, "click", "Only necessary", role="button", snapshot="none")
    for line in ORDER["lines"]:
        outline = await goto(a, f"{URL}/product/{line['id']}")
        if line["qty"] != 1:
            await act(a, outline, "fill", "Quantity", text=str(line["qty"]), snapshot="none")
        await act(a, outline, "click", "Add to basket", role="button", snapshot="none")
    outline = await goto(a, URL + "/checkout")
    await act(a, outline, "fill", "Full name", text=ORDER["name"], snapshot="none")
    await act(a, outline, "fill", "Email", text=ORDER["email"], snapshot="none")
    await act(a, outline, "select", "Country", text=ORDER["country"], role="combobox", snapshot="none")
    await act(a, outline, "click", "Express" if ORDER["delivery"] == "express" else "Standard", role="radio",
              snapshot="none")
    if ORDER["gift"]:
        await act(a, outline, "click", "Gift wrap", role="checkbox", snapshot="none")
    await act(a, outline, "click", "I accept the terms", role="checkbox", snapshot="none")
    await act(a, outline, "click", "Place the order", role="button", snapshot="none")
    waited = await a.on_call("browser_wait", for_text="Your order number is", wait_s=20)
    number = str(waited.get("url", "")).rstrip("/").rsplit("/", 1)[-1] if isinstance(waited, dict) else "?"
    return done(f"Order {number} is placed; the machine is open for you to look at.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Fills the form but leaves the country select alone, places the order, and does not read the page after."""
    env = await a.open(title="Shop order", repo=ctx.repo.slug, keep="none")
    job = await a.on_call("terminal_job_start", cmd=f"python3 serve.py --seed {SEED}", cwd=env["workdir"])
    await a.on_call("terminal_job_read", job=job["id"], until_port=8800, wait_s=60)
    outline = await goto(a, URL + "/checkout")
    await act(a, outline, "fill", "Full name", text=ORDER["name"], snapshot="none")
    await act(a, outline, "click", "Place the order", role="button", snapshot="none")
    return done("The order is placed.")


TASK = Task(
    id="t10_checkout_form",
    title="order on a shop the agent starts: every kind of form field, read back by the shop",
    repo="shopsite",
    sessions=[Session(_prompt())],
    check=check, perfect=perfect, careless=careless,
)
