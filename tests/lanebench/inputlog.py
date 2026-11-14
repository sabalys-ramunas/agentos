"""The node's input log: every Playwright mouse and keyboard call an executor makes on its lane's
page, with coordinates, steps, delays and the node's own clock.

The page's own listener (sites/static/recorder.js) sees what the page sees, which is the right
witness for where a click landed and whether a scroll had a wheel before it. It is not a witness
for timing: on the node a lane's page can run late, like a background tab, and then it stamps
events late and folds a glide into one event. Pointer travel and key rhythm are therefore judged
from this log (gates.input_log_conformance), which records what was sent, when it was sent.

The log wraps the lane page's `mouse` and `keyboard` for the executor's turn only: the harness's
own readiness and wake-up glides are outside it. MCP tools, the flow driver, human.py and a
planted raw call all go through these two objects, so all are logged. Playwright's own shortcuts
(`click` and `type` on a locator or element) do not: they never reach these objects, so the gate counts
what the page saw against this log and fails what the log cannot account for.
"""

from __future__ import annotations

import functools
import time
from typing import Any

MOUSE = ("move", "down", "up", "click", "dblclick", "wheel")
KEYBOARD = ("type", "press", "down", "up", "insert_text")
ARGS = {
    "mouse.move": ("x", "y"), "mouse.click": ("x", "y"), "mouse.dblclick": ("x", "y"),
    "mouse.wheel": ("dx", "dy"), "keyboard.type": ("text",), "keyboard.press": ("key",),
    "keyboard.down": ("key",), "keyboard.up": ("key",), "keyboard.insert_text": ("text",),
}


def _now_ms() -> float:
    return time.monotonic() * 1000


class InputLog:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._undo: list[tuple[Any, str]] = []

    def attach(self, page: Any) -> InputLog:
        for owner, prefix, names in ((page.mouse, "mouse", MOUSE), (page.keyboard, "keyboard", KEYBOARD)):
            for name in names:
                original = getattr(owner, name, None)
                if original is None:
                    continue
                setattr(owner, name, self._wrap(f"{prefix}.{name}", original))
                self._undo.append((owner, name))
        return self

    def detach(self) -> None:
        for owner, name in self._undo:
            owner.__dict__.pop(name, None)  # the class's own method shows again
        self._undo.clear()

    def _wrap(self, call: str, original: Any) -> Any:
        names = ARGS.get(call, ())

        @functools.wraps(original)
        async def logged(*args: Any, **kwargs: Any) -> Any:
            row: dict[str, Any] = {"call": call, "t": round(_now_ms(), 1)}
            for i, name in enumerate(names):
                if i < len(args):
                    row[name] = args[i]
            for k in ("x", "y", "steps", "delay", "button", "click_count", "delta_x", "delta_y"):
                if k in kwargs:
                    row[k] = kwargs[k]
            if "text" in row:  # what was typed is never kept, only how much
                row["chars"] = len(str(row.pop("text")))
            if len(str(row.get("key", ""))) == 1:  # a character key is typed text too
                del row["key"]
                row["chars"] = 1
            try:
                return await original(*args, **kwargs)
            finally:
                row["end"] = round(_now_ms(), 1)
                self.rows.append(row)

        return logged
