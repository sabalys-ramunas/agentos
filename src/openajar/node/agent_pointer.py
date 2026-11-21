"""Where the agent acts in its browser, for the live view to draw the agent's pointer over the screen.

The node moves an agent's pointer in a page itself (openajar.node.human: Playwright's mouse, which never moves
the screen's own pointer), so the screen shows nothing where the agent clicks. human.py tells this module each
point it aims at, in the page's own coordinates, before it moves there; a lane whose screen can place a page
point on the screen (a placer: an environment's desktop knows where its browser window is) turns it into a
point on the screen, and every live view watching that lane gets it on the stream it already reads.

The screen tools (openajar.environments.screen: screen_act) move the screen's real pointer and know the
point on the screen already: they tell it with on_screen, so a person sees the same labelled pointer when
the agent uses a desktop app.

Telling costs a dictionary look-up and never waits: nothing here adds time to a tool call, and nothing here
reaches the page. What leaves is only positions and what the agent did there (moved, pressed, scrolled,
typed), or that the pointer should hide.
"""

from __future__ import annotations

import logging
import time
import weakref
from collections.abc import Callable
from typing import Any

log = logging.getLogger("openajar.pointer")

KINDS = ("move", "press", "scroll", "type")
REPLACE_S = 8.0  # a window that moves takes the pointer with it when the agent acted this recently

# context -> lane id (openajar.node.lanes.launch names each lane's session), page -> its viewport (CSS px)
_LANES: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
_VIEWPORTS: weakref.WeakKeyDictionary[Any, tuple[float, float]] = weakref.WeakKeyDictionary()
_SPOTS: weakref.WeakKeyDictionary[Any, tuple[float, float]] = weakref.WeakKeyDictionary()  # page -> last point told

# (x, y, viewport width, viewport height, fresh) on the page -> (x, y, screen width, screen height) on the
# screen. `fresh`: the viewport was read with this point; not when the window moved under an older point
Placer = Callable[[float, float, float, float, bool], "tuple[int, int, int, int] | None"]
Watcher = Callable[[dict[str, Any]], None]


class Pointers:
    def __init__(self) -> None:
        self.placers: dict[str, Placer] = {}
        self.watchers: dict[str, list[Watcher]] = {}
        self.last: dict[str, tuple[float, float, float, float, str, float]] = {}  # lane -> page point, when

    def place_with(self, lane_id: str, placer: Placer | None) -> None:
        """Set (or with None, drop) how `lane_id`'s page points land on its screen."""
        if placer is None:
            self.placers.pop(lane_id, None)
            self.last.pop(lane_id, None)
        else:
            self.placers[lane_id] = placer

    def watch(self, lane_id: str, watcher: Watcher) -> Callable[[], None]:
        self.watchers.setdefault(lane_id, []).append(watcher)

        def unwatch() -> None:
            ws = self.watchers.get(lane_id, [])
            if watcher in ws:
                ws.remove(watcher)
            if not ws:
                self.watchers.pop(lane_id, None)

        return unwatch

    def _send(self, lane_id: str, event: dict[str, Any]) -> None:
        for watcher in list(self.watchers.get(lane_id, ())):
            try:
                watcher(event)
            except Exception:
                log.debug("pointer watcher failed", exc_info=True)

    def moved(self, lane_id: str, x: float, y: float, vw: float, vh: float, kind: str) -> None:
        placer = self.placers.get(lane_id)
        if placer is None:
            return
        self.last[lane_id] = (x, y, vw, vh, kind, time.monotonic())
        if lane_id in self.watchers:
            self._emit(lane_id, placer, (x, y, vw, vh, True), kind)

    def _emit(self, lane_id: str, placer: Placer, point: tuple[float, float, float, float, bool], kind: str) -> None:
        try:
            spot = placer(*point)
        except Exception:
            log.debug("pointer placer failed for %s", lane_id, exc_info=True)
            spot = None
        if spot is None:
            self._send(lane_id, {"hide": "away"})
            return
        sx, sy, sw, sh = spot
        self._send(lane_id, {"x": sx, "y": sy, "w": sw, "h": sh, "kind": kind})

    def on_screen(self, lane_id: str, x: int, y: int, w: int, h: int, kind: str) -> None:
        """The agent acts at (x, y) of its w x h screen itself (the screen tools): no page to place."""
        self.last.pop(lane_id, None)  # a window that moves now does not carry this point
        if kind in KINDS and lane_id in self.watchers and 0 <= x < w and 0 <= y < h:
            self._send(lane_id, {"x": int(x), "y": int(y), "w": int(w), "h": int(h), "kind": kind})

    def replace(self, lane_id: str) -> None:
        """The screen moved under the page (the window moved, was maximized or minimized): the pointer goes
        with it, when the agent acted a moment ago."""
        last, placer = self.last.get(lane_id), self.placers.get(lane_id)
        if last is None or placer is None or lane_id not in self.watchers:
            return
        x, y, vw, vh, _kind, at = last
        if time.monotonic() - at <= REPLACE_S:
            self._emit(lane_id, placer, (x, y, vw, vh, False), "move")

    def hide(self, lane_id: str, why: str) -> None:
        """The agent works elsewhere (its terminal): the pointer fades now rather than later."""
        self.last.pop(lane_id, None)
        if lane_id in self.watchers:
            self._send(lane_id, {"hide": why})


POINTERS = Pointers()


def name_lane(session: Any, lane_id: str) -> None:
    """Which lane a browser session (its context) is, so a page's points reach that lane's live views."""
    context = getattr(session, "context", None)
    if context is not None:
        try:
            _LANES[context] = lane_id
        except TypeError:  # a stand-in that cannot be weakly referenced
            pass


def saw_viewport(page: Any, width: float, height: float) -> None:
    """The page's viewport size as human.py just read it, for placing its points (no read of our own)."""
    try:
        _VIEWPORTS[page] = (float(width), float(height))
    except (TypeError, ValueError):
        pass


def told(page: Any, x: float, y: float, kind: str = "move") -> None:
    """human.py aims at (x, y) of `page` (CSS px of its viewport) for `kind`. Never raises, never waits."""
    try:
        _SPOTS[page] = (float(x), float(y))
        lane_id = _LANES.get(page.context)
        size = _VIEWPORTS.get(page)
        if lane_id is None or size is None or kind not in KINDS:
            return
        POINTERS.moved(lane_id, float(x), float(y), size[0], size[1], kind)
    except Exception:  # noqa: BLE001 - a stand-in page, a closed one: nothing to draw
        return


def told_here(page: Any, kind: str) -> None:
    """The agent acts where its pointer already is (it scrolls, presses or types): tell that point as `kind`."""
    try:
        spot = _SPOTS.get(page)
    except TypeError:
        return
    if spot is not None:
        told(page, spot[0], spot[1], kind)
