"""Real key presses for letters the US layout has no key for.

Playwright knows only the US layout: `keyboard.press("ą")` is an unknown key, and `keyboard.type`
inserts the letter as text with no keydown at all, which no keyboard does (the red team's
`kbd.keyless_text`, docs/design/redteam.md). So on a lane's own X display the layout is set to
Lithuanian and those letters go through the X server (XTest, by xdotool): the browser sees what a
Lithuanian keyboard sends, key `ą`, code `Digit1`. Every other key stays on Playwright, whose keys do
not read the X layout.

Only Lithuanian today. A letter of another language still goes in as text.

xdotool gets a keysym name, never the text, so nothing typed reaches a command line or a log beyond
which key it was, and a secret's letters are only ever a process argument on the node's own host.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass

log = logging.getLogger("agentos.node.xkeyboard")

LAYOUT = "lt"
KEYSYMS = {
    "ą": "aogonek", "č": "ccaron", "ę": "eogonek", "ė": "eabovedot", "į": "iogonek", "š": "scaron",
    "ų": "uogonek", "ū": "umacron", "ž": "zcaron",
    "Ą": "Aogonek", "Č": "Ccaron", "Ę": "Eogonek", "Ė": "Eabovedot", "Į": "Iogonek", "Š": "Scaron",
    "Ų": "Uogonek", "Ū": "Umacron", "Ž": "Zcaron",
}
TIMEOUT_S = 5.0


def _env(display: str, xauthority: str | None) -> dict[str, str]:
    env = {**os.environ, "DISPLAY": display}
    if xauthority:
        env["XAUTHORITY"] = xauthority
    return env


async def _run(env: dict[str, str], *args: str) -> bool:
    proc = await asyncio.create_subprocess_exec(*args, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(TIMEOUT_S):
            return await proc.wait() == 0
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False


@dataclass
class XKeyboard:
    """One lane's X keyboard. Holds names only, never the context or a page, so a registry entry
    cannot keep a closed lane alive."""

    display: str
    xauthority: str | None = None
    broken: bool = False

    def has(self, ch: str) -> bool:
        return not self.broken and ch in KEYSYMS

    async def press(self, ch: str, hold_ms: float) -> bool:
        """Press and release the key for `ch`, held `hold_ms`. False when it could not be sent; the
        caller then inserts the letter, and this keyboard is not tried again."""
        sym = KEYSYMS[ch]
        sent = await _run(_env(self.display, self.xauthority), "xdotool", "keydown", sym,
                          "sleep", f"{hold_ms / 1000:.3f}", "keyup", sym)
        if not sent:
            self.broken = True
            log.warning("the X keyboard on %s failed; letters go in as text from now on", self.display)
        return sent


async def prepare(display: str, xauthority: str | None = None) -> XKeyboard | None:
    """Set the layout on a lane's display. None when the tools are missing or it failed."""
    if not shutil.which("xdotool") or not shutil.which("setxkbmap"):
        log.warning("xdotool or setxkbmap is missing: Lithuanian letters go in as text, with no key press")
        return None
    if not await _run(_env(display, xauthority), "setxkbmap", "-layout", LAYOUT):
        log.warning("could not set the %s layout on %s", LAYOUT, display)
        return None
    return XKeyboard(display, xauthority)
