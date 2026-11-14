"""A page that renders nothing: read the page's own error from the call, fix the code."""

from studio_eval.tasks import Env, Task, ToolFailed, html

BUG = "const user = window.SESSION; document.getElementById('root').textContent = 'Hello ' + user.name;"
FIXED = "const user = window.SESSION || { name: 'there' }; document.getElementById('root').textContent = 'Hello ' + user.name;"
PAGE = html("Dashboard", f'<main id="root"></main>\n<script>{BUG}</script>')


async def check(env: Env):
    try:
        await env.check("app.html", themes=["light"], widths=[1280])
        renders = True
    except ToolFailed:
        renders = False
    return [("the page renders now", renders), ("the agent said it is done", env.answer.get("status") == "done")]


async def perfect(env: Env):
    try:
        await env.agent.call("ui_check", {"url": env.url("app.html")})
        raise AssertionError("the page should be blank")
    except ToolFailed as e:
        assert e.code == "studio_blank_page" and "reading 'name'" in e.text, e.text
    env.edit("app.html", BUG, FIXED)
    await env.agent.call("ui_check", {"url": env.url("app.html")})
    return {"status": "done", "summary": "user was undefined (reading 'name'); it now falls back, and the page renders "
                                         "with no findings."}


async def careless(env: Env):
    try:
        await env.agent.call("ui_check", {"url": env.url("app.html")})
    except ToolFailed:
        pass
    return {"status": "done", "summary": "The page is blank, probably the dev server; nothing to fix in the code."}


TASK = Task(id="t06_blank_page", title="a white screen with a script error", page="app.html",
            prompt="The dashboard at {url} shows a white screen. Find out why with studio, fix it, and check it.",
            pages={"app.html": PAGE}, check=check, perfect=perfect, careless=careless)
