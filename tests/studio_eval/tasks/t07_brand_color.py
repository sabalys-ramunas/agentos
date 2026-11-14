"""Check against a product's brand: a button colour that only looks like the brand blue."""

from studio_eval.tasks import Env, Task, html, still_has
from studio_eval.tasks._profile import approved_product

PAGE = html("Checkout", '<h1>Checkout</h1><p>Two items, 48 EUR.</p><button class="pay">Pay now</button>',
            ".pay { background: #2a5fd8; color: #ffffff; border: 0; }")


async def setup(env: Env):
    await approved_product(env)


async def check(env: Env):
    args = {"profile": env.saved["profile"]}
    return [("used the profile", any(r.args.get("profile") for r in env.called("ui_check"))
             or any(r.tool == "profile_use" for r in env.rows) or bool(env.called("system_check"))),
            ("the off-brand colour is fixed", not await still_has(env, "checkout.html", "token.color", **args)),
            ("the agent said it is done", env.answer.get("status") == "done")]


async def perfect(env: Env):
    await env.agent.call("profile_use", {"profile": env.saved["profile"], "task": "design"})
    first = await env.agent.call("ui_check", {"url": env.url("checkout.html"), "profile": env.saved["profile"]})
    assert "token.color" in first["counts"]["by_check"], first["counts"]
    env.edit("checkout.html", "background: #2a5fd8;", "background: #2563eb;")
    await env.agent.call("ui_check", {"url": env.url("checkout.html"), "profile": env.saved["profile"],
                                      "since": first["run"]})
    return {"status": "done", "summary": "The Pay button used #2a5fd8, a look-alike of color.brand; it now uses "
                                         "#2563eb."}


async def careless(env: Env):
    await env.agent.call("ui_check", {"url": env.url("checkout.html"), "only": ["measured"]})
    return {"status": "done", "summary": "No measured findings, it is on brand."}


TASK = Task(id="t07_brand_color", title="fix an off-brand colour", page="checkout.html", setup=setup,
            prompt="Check the checkout page at {url} against our brand (the product profile {profile}) and fix "
                   "anything off-brand.",
            pages={"checkout.html": PAGE}, check=check, perfect=perfect, careless=careless)
