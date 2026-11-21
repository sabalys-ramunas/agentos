"""place_window (node/camoufox/window.py) against a fake libX11: the display's own cookie on the connection,
and a window that cannot be placed. The real X server is in the node tests
(test_lanes_hardening_node.py::test_the_browser_window_sits_where_its_pin_says)."""

from __future__ import annotations

import ctypes
import logging
import time

import pytest

from openajar.node.camoufox import window as ec  # _xlib is patched where place_window reads it
from openajar.node.display import _write_xauthority, read_xauthority


class Attributes(ctypes.Structure):  # the fields place_window reads
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("map_state", ctypes.c_int)]


class FakeX11:
    """A screen with top-level windows (x, y, width, height, map_state); records the calls."""

    Attributes = Attributes

    def __init__(self, windows: list[tuple[int, int, int, int, int]], screen: tuple[int, int] = (1920, 1080)):
        self.windows = [list(w) for w in windows]
        self.screen = screen
        self.calls: list[tuple] = []
        self._children = (ctypes.c_ulong * len(windows))(*range(1, len(windows) + 1))

    def XSetAuthorization(self, name, namelen, data, datalen):
        self.calls.append(("auth", name, data))

    def XOpenDisplay(self, name):
        self.calls.append(("open", name))
        return 1

    def XDefaultRootWindow(self, dpy):
        return 0

    def XGetWindowAttributes(self, dpy, window, ref):
        attrs = ref._obj
        if window == 0:
            attrs.width, attrs.height = self.screen
            return 1
        attrs.x, attrs.y, attrs.width, attrs.height, attrs.map_state = self.windows[window - 1]
        return 1

    def XQueryTree(self, dpy, window, top, parent, children, count):
        self.calls.append(("query",))
        ctypes.c_void_p.from_address(ctypes.addressof(children._obj)).value = ctypes.addressof(self._children)
        count._obj.value = len(self.windows)
        return 1

    def XMoveWindow(self, dpy, window, x, y):
        self.calls.append(("move", window, x, y))
        self.windows[window - 1][:2] = [x, y]

    def XSync(self, dpy, discard):
        pass

    def XFree(self, data):
        pass

    def XCloseDisplay(self, dpy):
        self.calls.append(("close",))


def test_the_xauthority_file_reads_back_the_cookie_it_was_written_with(tmp_path):
    path = tmp_path / "display-110.xauth"
    _write_xauthority(path, 110)
    name, cookie = read_xauthority(path)
    assert name == b"MIT-MAGIC-COOKIE-1" and len(cookie) == 16 and cookie in path.read_bytes()
    assert read_xauthority(tmp_path / "missing") is None
    (tmp_path / "empty").write_bytes(b"")
    assert read_xauthority(tmp_path / "empty") is None


def test_place_window_connects_with_the_lanes_cookie_and_restores_the_default(tmp_path, monkeypatch):
    path = tmp_path / "display-110.xauth"
    _write_xauthority(path, 110)
    _, cookie = read_xauthority(path)
    x11 = FakeX11([(0, 0, 1536, 816, 2)])
    monkeypatch.setattr(ec, "_xlib", lambda: x11)
    assert ec.place_window(":110", (0, 25), xauthority=str(path), timeout=1) == 1
    assert x11.calls[:3] == [("auth", b"MIT-MAGIC-COOKIE-1", cookie), ("open", b":110"), ("auth", None, None)]
    assert ("move", 1, 0, 25) in x11.calls


def test_place_window_without_a_readable_cookie_does_not_connect(tmp_path, monkeypatch):
    x11 = FakeX11([(0, 0, 1536, 816, 2)])
    monkeypatch.setattr(ec, "_xlib", lambda: x11)
    with pytest.raises(OSError, match="cookie"):
        ec.place_window(":110", (0, 25), xauthority=str(tmp_path / "gone.xauth"), timeout=1)
    assert x11.calls == []


def test_a_window_that_does_not_fit_returns_at_once_and_is_logged_once(monkeypatch, caplog):
    x11 = FakeX11([(0, 0, 1536, 816, 2)], screen=(1536, 864))
    monkeypatch.setattr(ec, "_xlib", lambda: x11)
    t0 = time.monotonic()
    with caplog.at_level(logging.INFO, logger=ec.log.name):
        assert ec.place_window(":110", (0, 100), timeout=3) == 0
    assert time.monotonic() - t0 < 1, "waited for the deadline although the window was there"
    assert len([r for r in caplog.records if "does not fit" in r.getMessage()]) == 1
    assert not [c for c in x11.calls if c[0] == "move"] and x11.windows[0][:2] == [0, 0]


def test_place_window_waits_for_a_window_that_is_not_mapped_yet(monkeypatch):
    x11 = FakeX11([(0, 0, 1536, 816, 0)])
    monkeypatch.setattr(ec, "_xlib", lambda: x11)
    real_query = x11.XQueryTree

    def query(*args):
        if len([c for c in x11.calls if c[0] == "query"]) == 2:
            x11.windows[0][4] = 2  # mapped on the third look
        return real_query(*args)

    x11.XQueryTree = query
    assert ec.place_window(":110", (10, 20), timeout=3) == 1
    assert x11.windows[0][:2] == [10, 20]
