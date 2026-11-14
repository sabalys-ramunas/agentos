"""Find why something moved with ui_diff: a banner was added above everything."""

from pathlib import Path

from studio_eval.tasks import Env, Task

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "studio" / "diff"
PAGE = (FIX / "base.html").read_text().replace('src="/photo-a.svg"', 'src="photo-a.svg"')
BANNER = '<div class="banner">Sale ends Friday</div>'


async def setup(env: Env):
    first = await env.check("pricing.html", themes=["light"])
    await env.mcp.call("ui_baseline", {"url": env.url("pricing.html"), "run": first["run"]})
    env.edit("pricing.html", "<!--banner-->", BANNER)   # the change the user does not know about
    env.saved["before"] = env.read("pricing.html")


async def check(env: Env):
    named = any(w in env.summary() for w in ("banner", "sale ends friday"))
    return [("used ui_diff", bool(env.called("ui_diff"))),
            ("named the banner as the cause", named),
            ("changed no file", env.read("pricing.html") == env.saved["before"])]


async def perfect(env: Env):
    out = await env.agent.call("ui_diff", {"url": env.url("pricing.html")})
    lines = " ".join(line for s in out["screens"] for line in s.get("lines", []))
    assert "Sale ends Friday" in lines, lines
    return {"status": "done", "summary": "A new banner, 'Sale ends Friday', was added at the top; it pushed "
                                         "everything below it down by 44 px."}


async def careless(env: Env):
    await env.agent.call("ui_check", {"url": env.url("pricing.html")})
    return {"status": "done", "summary": "The cards have a layout problem in their container."}


TASK = Task(id="t04_why_moved", title="find why the cards moved", page="pricing.html", setup=setup, images="crop",
            prompt="The plan cards on {url} sit lower than they did at the last studio baseline. Find out why. "
                   "Do not change any files.",
            pages={"pricing.html": PAGE, "photo-a.svg": (FIX / "photo-a.svg").read_text()},
            check=check, perfect=perfect, careless=careless)
