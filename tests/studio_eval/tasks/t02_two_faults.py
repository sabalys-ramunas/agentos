"""Two measured faults on one page: faint text and tap targets too small. Both must go."""

from studio_eval.tasks import Env, Task, html, still_has

PAGE = html("Sign up", '<h1>Create your account</h1>'
            '<p class="hint" data-component="Hint">We never share your email.</p>'
            '<div class="tools" data-component="Toolbar"><button aria-label="Show password"></button>'
            '<button aria-label="Copy"></button></div>',
            ".hint { color: #aaaaaa; }\n"
            ".tools button { width: 16px; height: 16px; min-height: 0; padding: 0; border: 0; background: #dddddd; "
            "margin-right: 2px; }")


async def check(env: Env):
    return [("the faint hint is fixed", not await still_has(env, "signup.html", "contrast.text")),
            ("the small buttons are fixed", not await still_has(env, "signup.html", "target.size")),
            ("the agent said it is done", env.answer.get("status") == "done")]


async def perfect(env: Env):
    first = await env.agent.call("ui_check", {"url": env.url("signup.html")})
    assert {"contrast.text", "target.size"} <= set(first["counts"]["by_check"]), first["counts"]
    env.edit("signup.html", ".hint { color: #aaaaaa; }", ".hint { color: #555555; }")
    env.edit("signup.html", "width: 16px; height: 16px;", "width: 44px; height: 44px;")
    after = await env.agent.call("ui_check", {"url": env.url("signup.html"), "since": first["run"]})
    if after["delta"]["new"] or after["delta"]["still"]:   # #555 is faint on the dark background
        env.edit("signup.html", ".hint { color: #555555; }",
                 ".hint { color: #555555; }\n@media (prefers-color-scheme: dark) { .hint { color: #bbbbbb; } }")
        await env.agent.call("ui_check", {"url": env.url("signup.html"), "since": after["run"]})
    return {"status": "done", "summary": "Darkened the hint to #555 and made the two icon buttons 44 px."}


async def careless(env: Env):
    await env.agent.call("ui_check", {"url": env.url("signup.html")})
    env.edit("signup.html", ".hint { color: #aaaaaa; }", ".hint { color: #555555; }")
    return {"status": "done", "summary": "Fixed the contrast of the hint."}


TASK = Task(id="t02_two_faults", title="fix two measured faults", page="signup.html",
            prompt="Make the sign-up page at {url} pass studio's checks.",
            pages={"signup.html": PAGE}, check=check, perfect=perfect, careless=careless)
