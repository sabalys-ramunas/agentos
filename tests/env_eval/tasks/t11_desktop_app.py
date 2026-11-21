"""A desktop app, not a web page (0.5.0's screen tools): the human asks for a command typed into the desktop's
own Terminal window, where they watch it, not run by the agent's terminal tool. The checker reads the
machine the agent leaves open, and that the words went in through screen_act."""

from __future__ import annotations

import asyncio

from env_eval.tasks import Agent, Check, Ctx, Session, Task, ToolFailed, done

LINE = "echo desktop-ok > /work/desktop.txt"


async def check(ctx: Ctx) -> list[Check]:
    code, out = ctx.on_machine("cat /work/desktop.txt")
    typed = any(LINE in str((r.get("args") or {}).get("text") or "") for r in ctx.calls("screen_act") if r.get("ok"))
    by_terminal = any("echo desktop-ok" in str((r.get("args") or {}).get("cmd") or "") for r in ctx.calls("terminal_exec"))
    return [
        ("the file is on the machine left open", code == 0 and out.strip() == "desktop-ok"),
        ("the line was typed on the desktop with screen_act", typed),
        ("the agent's terminal tool did not write it", not by_terminal),
    ]


async def _screen(a: Agent, tool: str, **args) -> object:
    """A screen tool through tools_call; a desktop that is still starting is waited for (no_desktop says so)."""
    for _ in range(24):
        try:
            return await a.via(tool, **args)
        except ToolFailed as e:
            if e.code != "no_desktop":
                raise
            await asyncio.sleep(5)
    raise TimeoutError("the desktop did not start in two minutes")


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    await a.open(title="Desktop terminal", keep="none")
    await a.call("tools_more", {"toolset": "screen"})
    await _screen(a, "screen_open", app="lxterminal")
    await _screen(a, "screen_act", action="type", text=LINE)
    await _screen(a, "screen_act", action="key", keys="Return")
    for _ in range(10):
        out = await a.sh("cat /work/desktop.txt 2>/dev/null || true")
        if "desktop-ok" in out.get("stdout", ""):
            break
        await asyncio.sleep(1)
    return done("I typed it in the desktop's Terminal; /work/desktop.txt says desktop-ok. The machine is open.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Runs the line with its own terminal tool instead of typing it where the human watches."""
    await a.open(title="Desktop terminal", keep="none")
    await a.sh(LINE)
    return done("Done.")


TASK = Task(
    id="t11_desktop_app",
    title="a desktop app: type a command into the desktop's Terminal window with the screen tools",
    sessions=[Session(f"In openajar, open the Terminal app on the computer's desktop and type `{LINE}` into it, so I "
                      "can watch it in the live view. Leave the machine open.")],
    check=check, perfect=perfect, careless=careless, requires=("screen_act",),
)
