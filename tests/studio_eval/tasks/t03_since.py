"""Use `since`: the user names the run before their fix and wants only what changed."""

from studio_eval.tasks import Env, Task, html

CUT = ".name { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }"
PAGE = html("Files", '<ul><li data-component="FileRow"><span class="name">Quarterly revenue report for the '
            "northern region 2026.pdf</span></li></ul>", "ul { width: 200px; padding: 0; list-style: none; }\n" + CUT)


async def setup(env: Env):
    first = await env.check("files.html")
    assert "text.truncated" in first["counts"]["by_check"], first["counts"]
    env.saved["run"] = first["run"]
    env.edit("files.html", CUT, ".name { display: block; }")   # the user's own fix


async def check(env: Env):
    since = [r for r in env.called("ui_check") if r.args.get("since") == env.saved["run"]]
    return [("checked with since=<the user's run>", bool(since)),
            ("said the truncation is fixed", "fix" in env.summary() and env.answer.get("status") == "done")]


async def perfect(env: Env):
    out = await env.agent.call("ui_check", {"url": env.url("files.html"), "since": env.saved["run"]})
    assert out["delta"]["fixed"]
    return {"status": "done", "summary": f"Since {env.saved['run']}: the truncated file name is fixed, nothing new."}


async def careless(env: Env):
    await env.agent.call("ui_check", {"url": env.url("files.html")})
    return {"status": "done", "summary": "The page is clean."}


TASK = Task(id="t03_since", title="read only what changed with since", page="files.html", setup=setup,
            prompt="I fixed the long file name in the list on {url}. The studio run from before my fix is {run}. "
                   "Tell me whether my fix worked and whether anything else changed, reading as little as you can.",
            pages={"files.html": PAGE}, check=check, perfect=perfect, careless=careless)
