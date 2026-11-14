"""The browser window: its size and place from the pin, the display that holds it, and moving the real
window to where the pin says it is, through libX11."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from agentos.node.display import MAX_SIZE, MIN_SIZE, read_xauthority

log = logging.getLogger("agentos.engine.camoufox")


def window_size_of(config: dict[str, Any]) -> tuple[int, int] | None:
    """The browser window's size in screen pixels, as Camoufox sizes the real window from a pin:
    window.outerWidth x window.outerHeight, times the device pixel ratio when it is not 1."""
    try:
        width, height = int(config["window.outerWidth"]), int(config["window.outerHeight"])
        ratio = float(config.get("window.devicePixelRatio") or 1)
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0 or ratio <= 0:
        return None
    return round(width * ratio), round(height * ratio)


def window_position_of(config: dict[str, Any]) -> tuple[int, int] | None:
    """Where the pin says the window is, in screen pixels: window.screenX, window.screenY."""
    try:
        x, y = int(config["window.screenX"]), int(config["window.screenY"])
        ratio = float(config.get("window.devicePixelRatio") or 1)
    except (KeyError, TypeError, ValueError):
        return None
    if x < 0 or y < 0 or ratio <= 0:
        return None
    return round(x * ratio), round(y * ratio)


def _servable(size: tuple[int, int]) -> bool:
    """Xvfb can be made this size (display.MIN_SIZE to MAX_SIZE, which bound its framebuffer)."""
    return MIN_SIZE[0] <= size[0] <= MAX_SIZE[0] and MIN_SIZE[1] <= size[1] <= MAX_SIZE[1]


def display_size_of(config: dict[str, Any]) -> tuple[int, int] | None:
    """The lane's X screen: the pinned screen in pixels, so the real window can sit where the pin
    says it is (place_window) and a full-screen page fills what screen.width and height claim. The
    window's own size when the pin has no screen, the window would not fit on it there, or the
    screen is larger than Xvfb is allowed to be (the window then sits at +0+0 on a display exactly
    its size, never on a smaller fallback screen). None when no display can hold the window."""
    window = window_size_of(config)
    if window is None or not _servable(window):
        return None
    try:
        ratio = float(config.get("window.devicePixelRatio") or 1)
        width, height = round(int(config["screen.width"]) * ratio), round(int(config["screen.height"]) * ratio)
    except (KeyError, TypeError, ValueError):
        return window
    x, y = window_position_of(config) or (0, 0)
    if x + window[0] > width or y + window[1] > height or not _servable((width, height)):
        return window
    return width, height


# ---- the real window --------------------------------------------------------------
#
# Camoufox sizes the real window from the pin but never moves it, and with no window manager it
# sits at +0+0. window.screenX/Y come from the pin while mozInnerScreenX/Y and every mouse event's
# screenX/Y come from the real window, so a page could see a window at (206, 2) whose content
# starts at (0, 85), or a macOS window above the menu bar (Camoufox issue 974; measured on 158.0.1
# beta.41). Each lane has its own X display, so the browser's windows are the only top-level
# windows on it: place_window moves them to the pinned position, through libX11 (which Firefox
# itself needs), and the page then reads one window.

PLACE_TIMEOUT = 5.0
PLACE_MIN_SIZE = 200  # a top-level window smaller than this is not the browser window (a tooltip)


def _xlib() -> Any:
    import ctypes
    import ctypes.util

    class Attributes(ctypes.Structure):  # XWindowAttributes
        _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int),
                    ("border_width", ctypes.c_int), ("depth", ctypes.c_int), ("visual", ctypes.c_void_p),
                    ("root", ctypes.c_ulong), ("c_class", ctypes.c_int), ("bit_gravity", ctypes.c_int),
                    ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
                    ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
                    ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong), ("map_installed", ctypes.c_int),
                    ("map_state", ctypes.c_int), ("all_event_masks", ctypes.c_long),
                    ("your_event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
                    ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p)]

    x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    window, display = ctypes.c_ulong, ctypes.c_void_p
    x11.XOpenDisplay.restype, x11.XOpenDisplay.argtypes = display, [ctypes.c_char_p]
    x11.XDefaultRootWindow.restype, x11.XDefaultRootWindow.argtypes = window, [display]
    x11.XQueryTree.argtypes = [display, window, ctypes.POINTER(window), ctypes.POINTER(window),
                               ctypes.POINTER(ctypes.POINTER(window)), ctypes.POINTER(ctypes.c_uint)]
    x11.XGetWindowAttributes.argtypes = [display, window, ctypes.POINTER(Attributes)]
    x11.XMoveWindow.argtypes = [display, window, ctypes.c_int, ctypes.c_int]
    x11.XSync.argtypes = [display, ctypes.c_int]
    x11.XSetAuthorization.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    x11.XFree.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [display]
    x11.Attributes = Attributes
    return x11


_X_AUTH_LOCK = threading.Lock()  # XSetAuthorization is process-wide: one XOpenDisplay at a time


def _open_display(x11: Any, display: str, xauthority: str | None) -> Any:
    """XOpenDisplay with the display's own cookie. Xvfb takes only clients that hold it, and XAUTHORITY
    in os.environ is not the lane's (nor may it be set there: other threads read the environment), so
    the cookie is handed to libX11 for this one connection and the default restored after."""
    if not xauthority:
        return x11.XOpenDisplay(display.encode())
    auth = read_xauthority(xauthority)
    if auth is None:
        raise OSError(f"cannot read the X cookie in {xauthority}")
    name, cookie = auth
    with _X_AUTH_LOCK:
        x11.XSetAuthorization(name, len(name), cookie, len(cookie))
        try:
            return x11.XOpenDisplay(display.encode())
        finally:
            x11.XSetAuthorization(None, 0, None, 0)


def place_window(display: str, position: tuple[int, int], *, xauthority: str | None = None,
                 timeout: float = PLACE_TIMEOUT) -> int:
    """Move the browser's top-level windows on `display` (whose cookie is in `xauthority`, the file
    the browser gets) to `position` (screen pixels), once they are mapped. Returns how many windows
    sit there; 0 when none appeared within `timeout`, or at once when the window is there but the
    move would push it off the screen (it is left where it is: waiting would not make it fit).
    Logs why nothing was placed. Blocking."""
    import ctypes

    x11 = _xlib()
    dpy = _open_display(x11, display, xauthority)
    if not dpy:
        raise OSError(f"cannot open X display {display}")
    try:
        root = x11.XDefaultRootWindow(dpy)
        screen = x11.Attributes()
        x11.XGetWindowAttributes(dpy, root, ctypes.byref(screen))
        deadline = time.monotonic() + timeout
        while True:
            placed, misfit = 0, None
            parent, top = ctypes.c_ulong(), ctypes.c_ulong()
            children, count = ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
            if x11.XQueryTree(dpy, root, ctypes.byref(top), ctypes.byref(parent), ctypes.byref(children),
                              ctypes.byref(count)):
                try:
                    for i in range(count.value):
                        attrs = x11.Attributes()
                        if not x11.XGetWindowAttributes(dpy, children[i], ctypes.byref(attrs)):
                            continue
                        if attrs.map_state != 2 or attrs.width < PLACE_MIN_SIZE or attrs.height < PLACE_MIN_SIZE:
                            continue  # not viewable, or not the browser window
                        if position[0] + attrs.width > screen.width or position[1] + attrs.height > screen.height:
                            misfit = (attrs.width, attrs.height, attrs.x, attrs.y)
                            continue
                        if (attrs.x, attrs.y) != position:
                            x11.XMoveWindow(dpy, children[i], *position)
                        placed += 1
                finally:
                    if children:
                        x11.XFree(children)
            x11.XSync(dpy, 0)
            if placed:
                return placed
            if misfit:
                log.warning("the window (%dx%d) does not fit on %s (%dx%d) at %s; it stays at +%d+%d", *misfit[:2],
                            display, screen.width, screen.height, position, *misfit[2:])
                return 0
            if time.monotonic() > deadline:
                log.warning("no browser window appeared on %s within %.0fs to move to %s", display, timeout, position)
                return 0
            time.sleep(0.1)
    finally:
        x11.XCloseDisplay(dpy)
