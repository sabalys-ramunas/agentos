"""Recover from a skipped theme: dark mode is an attribute, and a fault hides in dark only."""

from studio_eval.tasks import Env, Task, html, still_has

ATTR_BASE = """html { font: 16px/1.5 Arial, Helvetica, sans-serif; color: #111111; background: #ffffff; }
html[data-theme="dark"] { color: #eeeeee; background: #111111; }
body { margin: 0; padding: 16px; }"""
DARK_FAULT = 'html[data-theme="dark"] .muted { color: #444444; }'
PAGE = html("Settings", '<h1>Settings</h1><p class="muted" data-component="Help">Changes save by themselves.</p>',
            ".muted { color: #555555; }\n" + DARK_FAULT, base=ATTR_BASE)
SWITCH = {"how": "attr", "name": "data-theme", "light": "light", "dark": "dark"}


async def check(env: Env):
    switched = any((r.args.get("theme_switch") or {}).get("how") == "attr" for r in env.called("ui_check"))
    return [("checked dark with the attribute switch", switched),
            ("the dark-only faint text is fixed",
             not await still_has(env, "settings.html", "contrast.text", theme_switch=SWITCH)),
            ("the agent said it is done", env.answer.get("status") == "done")]


async def perfect(env: Env):
    first = await env.agent.call("ui_check", {"url": env.url("settings.html"), "theme_switch": SWITCH})
    assert "contrast.text" in first["counts"]["by_check"], first
    env.edit("settings.html", DARK_FAULT, 'html[data-theme="dark"] .muted { color: #bbbbbb; }')
    await env.agent.call("ui_check", {"url": env.url("settings.html"), "theme_switch": SWITCH,
                                      "since": first["run"]})
    return {"status": "done", "summary": "With data-theme=dark the help text was #444 on #111; it is #bbb now."}


async def careless(env: Env):
    await env.agent.call("ui_check", {"url": env.url("settings.html")})
    return {"status": "done", "summary": "Light is clean; the dark screens did not render, so dark is fine."}


TASK = Task(id="t10_theme_switch", title="a dark mode set by an attribute", page="settings.html",
            prompt='Our settings page {url} has a dark mode: the app sets data-theme="dark" on the html element when '
                   "a user turns it on. Check it in both themes and fix what is measured.",
            pages={"settings.html": PAGE}, check=check, perfect=perfect, careless=careless)
