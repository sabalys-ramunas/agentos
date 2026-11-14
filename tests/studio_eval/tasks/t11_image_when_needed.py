"""Fetch an image only when needed: only pixels changed, inside a photo. A crop of the region is
the right look; a whole screen is not."""

from pathlib import Path

from studio_eval.tasks import Env, Task

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "studio" / "diff"
PAGE = (FIX / "base.html").read_text().replace('src="/photo-a.svg"', 'src="photo-a.svg"')


async def setup(env: Env):
    first = await env.check("team.html", themes=["light"])
    await env.mcp.call("ui_baseline", {"url": env.url("team.html"), "run": first["run"]})
    env.write("photo-a.svg", (FIX / "photo-b.svg").read_text())   # the image file changed


async def check(env: Env):
    s = env.summary()
    whole = [r for r in env.called("ui_image") if not r.args.get("crop")]
    return [("used ui_diff", bool(env.called("ui_diff"))),
            ("said the change is inside the team photo", "photo" in s or "image" in s),
            ("no whole-screen image", not whole)]


async def perfect(env: Env):
    out = await env.agent.call("ui_diff", {"url": env.url("team.html")})
    screen = next(s for s in out["screens"] if s["status"] == "changed")
    region = screen["unexplained"][0]["rect"]
    rect = f"{region['x']:.0f},{region['y']:.0f},{region['w']:.0f},{region['h']:.0f}"
    await env.agent.call("ui_image", {"capture": out["after"], "screen": screen["screen"], "crop": rect})
    return {"status": "done", "summary": "Only pixels inside the Team photo image changed; no layout, style or text "
                                         "changed. The crop shows a different picture."}


async def careless(env: Env):
    out = await env.agent.call("ui_diff", {"url": env.url("team.html")})
    await env.agent.call("ui_image", {"capture": out["after"]})
    return {"status": "done", "summary": "Nothing important changed on the page."}


TASK = Task(id="t11_image_when_needed", title="look at pixels only when needed", page="team.html", setup=setup,
            images="crop",
            prompt="Something changed on {url} since the studio baseline, but the code change only touched an image "
                   "file. Tell me what changed on screen, and where.",
            pages={"team.html": PAGE, "photo-a.svg": (FIX / "photo-a.svg").read_text()},
            check=check, perfect=perfect, careless=careless)
