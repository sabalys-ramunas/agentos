"""Check and fix one measured fault: a button cut off by its bar."""

from studio_eval.tasks import Env, Task, html, still_has

PAGE = html("Filters", '<div class="bar" data-component="FilterBar"><span>Filters</span>'
            "<button>Apply filters</button></div>",
            ".bar { height: 40px; overflow: hidden; display: flex; gap: 8px; align-items: flex-start; padding: 4px; "
            "width: 300px; background: #eeeeee; color: #111111; }\n.bar button { height: 64px; }")


async def check(env: Env):
    rechecked = any(r.args.get("since") for r in env.called("ui_check"))
    return [("the clipped button is fixed", not await still_has(env, "filters.html", "layout.clipped")),
            ("the agent said it is done", env.answer.get("status") == "done"),
            ("the fix was confirmed with since", rechecked)]


async def perfect(env: Env):
    first = await env.agent.call("ui_check", {"url": env.url("filters.html")})
    assert "layout.clipped" in first["counts"]["by_check"]
    env.edit("filters.html", ".bar { height: 40px;", ".bar { min-height: 40px;")
    after = await env.agent.call("ui_check", {"url": env.url("filters.html"), "since": first["run"]})
    assert after["delta"]["fixed"]
    return {"status": "done", "summary": "The Apply filters button was clipped by the 40px bar; it now grows."}


async def careless(env: Env):
    await env.agent.call("ui_check", {"url": env.url("filters.html")})
    return {"status": "done", "summary": "Checked it, the filter bar is fine."}


TASK = Task(id="t01_fix_clipped", title="check and fix a clipped button", page="filters.html",
            prompt="The filter bar on {url} looks broken on phones. Check it with studio and fix what it measures.",
            pages={"filters.html": PAGE}, check=check, perfect=perfect, careless=careless)
