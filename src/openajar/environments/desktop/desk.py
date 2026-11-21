"""The desktop's helper on an environment's machine (openajar.environments.vmbrowser_ctl starts it with the
screen, in a process group of its own). One process, driven by events, so it costs nothing while nothing
happens:

- the wallpaper on the root window (a PNG the control script rendered at the screen's size), drawn once;
- the top bar: the openajar mark and the environment's title, what the agent is doing in a capsule in the
  middle ("Claude Code is using the browser", "You have control"), the date and the time. It reserves its
  height (a strut), as the dock does, and is drawn again only when its words change and once a minute;
- the dock: a glass pill at the bottom, centred, with Browser, Terminal and Files pinned and every other
  app while it has a window, a dot under each one that runs. It reserves its band (a strut), so a maximized
  window sits between the top bar and the dock;
- the agent's terminal: the commands the agent runs (terminal_exec) and their output, which the node sends here
  as it reads them, written to a transcript a read-only terminal window follows (deskterm.py: output cannot
  drive that window or pass for the command and exit lines, which come from the node's record);
- focus: the node asks for the browser or the agent's terminal to come to the front when the agent uses it,
  and a browser window that comes up while an app has the keyboard leaves it there (FocusGuard);
- the browser's window title, without the browser's brand after an em dash;
- where the browser's window is on the screen, sent to the node when it changes, so the live view can draw
  the agent's pointer where the agent acts in the page.

The node reaches it on the unix socket desk.sock in the screen's folder (a folder only root can open), through
the environment's SSH tunnel; it never listens on TCP. Messages are JSON lines:

  {"t": "begin", "id": ..., "cmd": ..., "cwd": ..., "show": bool}   a command starts
  {"t": "out", "id": ..., "s": "o"|"e", "d": text}                   its output as it comes
  {"t": "end", "id": ..., "exit": int|null, "sig": ..., "dur": s, "timeout": bool, "cancelled": bool,
   "dropped": bytes}                                                   it ended
  {"t": "note", "text": ...}                                          a line of its own (a job started)
  {"t": "focus", "what": "browser"|"terminal"}                         bring it to the front
  {"t": "status", "agent": name, "control": "agent"|"human"|"paused", "doing": "browser"|"terminal"|"",
   "tz": minutes}                                                      for the top bar
  {"t": "hello"}                                                       the node is there (nothing to do)

and two go back: {"t": "want", "what": "browser"} when a click on the dock's Browser finds no browser, so the
node opens it (the browser is the agent's: only the node starts it), and {"t": "win", "x", "y", "w", "h",
"shown"}: the browser window's visible area on the screen, in pixels, whenever it changes (moved, resized,
maximized, minimized, gone) and to every node that connects. Until a browser window shows, a note over the
dock says it is on its way.

Runs with the system's python3 and its python3-xlib and python3-pil (desktop.sh installs them), as root.
"""

import json
import os
import select
import shlex
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # deskart, beside it (python3 -I leaves it out)

import deskart as art
from deskterm import Transcript
from PIL import Image
from Xlib import X, Xatom, display, xauth
from Xlib.error import XError
from Xlib.protocol import event as xevent
from Xlib.protocol import request as xrequest

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("AGENTOS_DESK_DIR", HERE)  # wallpaper.png, icons/, commands.log
SOCK = os.environ.get("AGENTOS_DESK_SOCK", os.path.join(os.path.dirname(STATE), "desk.sock"))
LOG = os.path.join(STATE, "commands.log")  # not agent.log: a save from before deskterm may hold one unfiltered
AGENT_SUFFIX = " · commands"     # the agent's terminal window: "<agent> · commands"
WORK = "/work" if os.path.isdir("/work") else os.path.expanduser("~")

PINNED = (("browser", "Browser"), ("terminal", "Terminal"), ("files", "Files"))
SHELL = "env -u XDG_CONFIG_HOME bash -l"
NOTE_S = 25.0        # the dock's note waits this long for the browser, then goes
DOING_S = 12.0       # "is using the browser" stays this long after the agent's last browser call
TZ_MAX = 14 * 60


# ---------------------------------------------------------------- X


def _wild_auth(self, family, address, dispno, types=(b"MIT-MAGIC-COOKIE-1",)):
    """python-xlib matches an Xauthority entry by address only; the screen's cookie is FamilyWild (any host)."""
    num = str(dispno).encode()
    for efam, eaddr, enum, ename, edata in self.entries:
        if ename in types and (enum in (b"", num)) and (efam == 65535 or (efam == family and eaddr == address)):
            return ename, edata
    raise xauth.error.XNoAuthError((family, address, dispno))


xauth.Xauthority.get_best_auth = _wild_auth


class Atoms:
    def __init__(self, d):
        self._d = d
        self._cache = {}

    def get(self, name):
        if name not in self._cache:
            self._cache[name] = self._d.intern_atom(name)
        return self._cache[name]


def _prop(win, atom, kind=X.AnyPropertyType):
    try:
        p = win.get_full_property(atom, kind)
    except XError:
        return None
    return p.value if p is not None else None


def _put(d, drawable, gc, x, y, im):
    """An RGB image onto a drawable, in strips under the server's request limit."""
    w, h = im.size
    data = im.convert("RGB").tobytes("raw", "BGRX")
    stride = w * 4
    rows = max(1, (d.display.info.max_request_length * 4 - 64) // stride)
    for top in range(0, h, rows):
        n = min(rows, h - top)
        drawable.put_image(gc, x, y + top, w, n, X.ZPixmap, 24, 0, data[top * stride:(top + n) * stride])


def _net_icon(win, size):
    """A window's own icon (_NET_WM_ICON), the size nearest `size`, as RGBA; None without one."""
    data = _prop(win, Desk.atoms.get("_NET_WM_ICON"), Xatom.CARDINAL)
    if not data:
        return None
    best, i, data = None, 0, list(data)
    while i + 2 <= len(data):
        w, h = data[i], data[i + 1]
        if w <= 0 or h <= 0 or i + 2 + w * h > len(data):
            break
        if best is None or abs(w - size) < abs(best[0] - size) or (best[0] < size <= w):
            best = (w, h, i + 2)
        i += 2 + w * h
    if best is None:
        return None
    w, h, at = best
    px = bytearray()
    for argb in data[at:at + w * h]:
        px += bytes(((argb >> 16) & 255, (argb >> 8) & 255, argb & 255, (argb >> 24) & 255))
    return Image.frombytes("RGBA", (w, h), bytes(px)).resize((size, size), Image.LANCZOS)


def _icon_data(pictures):
    """RGBA pictures as _NET_WM_ICON's cardinals: width, height, then ARGB pixels, for each size."""
    out = []
    for im in pictures:
        w, h = im.size
        out += [w, h]
        out += [(a << 24) | (r << 16) | (g << 8) | b for r, g, b, a in im.getdata()]
    return out


# ---------------------------------------------------------------- what the agent is doing


VERBS = {"paused": " is stopped", "terminal": " is running a command", "browser": " is using the browser",
         "reading": " is reading files", "editing": " is editing files", "desktop": " is using the desktop"}


class Status:
    """What the top bar says, from the node's status messages and the commands it shows."""

    def __init__(self):
        self.agent = ""
        self.control = "agent"
        self.doing = ""
        self.doing_until = 0.0
        self.running = set()  # commands begun and not ended
        self.tz = 0

    def update(self, m):
        name = m.get("agent")
        if isinstance(name, str):
            self.agent = " ".join("".join(c for c in name if c.isprintable()).split())[:32]
        if m.get("control") in ("agent", "human", "paused"):
            self.control = m["control"]
        if m.get("doing") in VERBS:
            self.doing, self.doing_until = m["doing"], time.monotonic() + DOING_S
        tz = m.get("tz")
        if isinstance(tz, int) and -TZ_MAX <= tz <= TZ_MAX:
            self.tz = tz

    def now_doing(self):
        if self.running:
            return "terminal"
        return self.doing if time.monotonic() < self.doing_until else ""

    def words(self):
        """(the agent's name, the rest, a person has control) for the capsule."""
        if self.control == "human":
            return "", "You have control", True
        name = self.agent
        rest = VERBS.get(self.control if self.control == "paused" else self.now_doing(), " has control")
        return (name, rest, False) if name else ("", "The agent" + rest, False)

    def wake_in(self):
        """Seconds until the words change by themselves (the browser call that ages out), or None."""
        if self.running or not self.doing:
            return None
        left = self.doing_until - time.monotonic()
        return left if left > 0 else None


# ---------------------------------------------------------------- who has the keyboard


class FocusGuard:
    """The browser starts in the background when the environment opens, so its first window can come up after
    an app the agent opened (screen_open) or a person opened has the keyboard. That window never takes the
    keyboard or the front from such an app (t11 of the eval: the agent's typing went into the browser): for
    BORN_S after a browser window first shows, the app that last had focus is put back in front, unless the
    browser was asked for (the node, when the agent uses the browser; the dock's Browser). A browser that comes
    up on a desktop where nothing has focus gets it (openbox's rc.xml gives a new browser window none itself).
    The agent's own terminal ("agent") is a read-only transcript: it neither holds nor loses the keyboard here.
    Pure bookkeeping on window ids and kinds, so it is tested without a screen."""

    BORN_S = 6.0

    def __init__(self):
        self.last_app = None   # the newest app window (not the browser, not the agent's terminal) that had focus
        self.asked_at = None   # when the browser was last asked for
        self.born = {}         # browser window id -> when it first showed

    def asked(self, now):
        self.asked_at = now

    def saw(self, active, kinds, now):
        """After the window list changed: `active` the focused window's id (None: none), `kinds` every app
        window's id and kind. Returns the window to put in front, or None to leave focus as it is."""
        new = [w for w, k in kinds.items() if k == "browser" and w not in self.born]
        for w in new:
            self.born[w] = now
        self.born = {w: t for w, t in self.born.items() if w in kinds}
        kind = kinds.get(active)
        if kind is not None and kind not in ("browser", "agent"):
            self.last_app = active
            return None
        asked = self.asked_at is not None and now - self.asked_at < self.BORN_S
        young = any(now - t < self.BORN_S for t in self.born.values())
        if asked:
            self.last_app = None if kind == "browser" else self.last_app
            return None
        if young and self.last_app in kinds and kind in (None, "browser", "agent"):
            return self.last_app
        if kind == "browser" and not young:
            self.last_app = None  # a person (or the agent) chose the browser: it keeps the keyboard
        if new and kind in (None, "agent"):
            return new[-1]  # no app has focus: the new browser gets it, as openbox would have
        return None


# ---------------------------------------------------------------- the desk


class Item:
    def __init__(self, key, label, icon, pinned):
        self.key, self.label, self.icon, self.pinned = key, label, icon, pinned
        self.windows = []  # client windows, oldest first
        self.x = 0


class Desk:
    atoms = None

    def __init__(self):
        self.d = display.Display()
        self.screen = self.d.screen()
        self.root = self.screen.root
        Desk.atoms = self.a = Atoms(self.d)
        geo = self.root.get_geometry()
        self.W, self.H = geo.width, geo.height
        self.transcript = Transcript(LOG)
        self.status = Status()
        self.title = ""
        self.items = []
        self.hover = None
        self.active = None
        self.clients = {}  # window id -> (Item key, window)
        self.guard = FocusGuard()
        self.agent_pids = set()  # the agent's terminal windows we started
        self.agent_term = None
        self.tip = None
        self.wall = None
        self.band = None
        self.bar_pic = None
        self.bar_key = None
        self.server = None
        self.conns = {}
        self.note = None     # the note over the dock: (window, gc, picture, until)
        self.geo = None      # the browser window's area, as last sent
        self._dirty = True
        self._geo_dirty = True

    # ---- wallpaper
    def set_wallpaper(self):
        path = os.path.join(STATE, "wallpaper.png")
        try:
            im = Image.open(path).convert("RGB")
        except OSError:
            im = Image.new("RGB", (self.W, self.H), (11, 16, 51))
        if im.size != (self.W, self.H):
            sw, sh = im.size
            scale = max(self.W / sw, self.H / sh)
            im = im.resize((round(sw * scale), round(sh * scale)), Image.LANCZOS)
            left, top = (im.size[0] - self.W) // 2, (im.size[1] - self.H) // 2
            im = im.crop((left, top, left + self.W, top + self.H))
        self.wall = im
        pix = self.root.create_pixmap(self.W, self.H, self.screen.root_depth)
        gc = pix.create_gc()
        _put(self.d, pix, gc, 0, 0, im)
        gc.free()
        for name in ("_XROOTPMAP_ID", "ESETROOT_PMAP_ID"):  # an older desk's pixmap goes, as hsetroot does
            old = _prop(self.root, self.a.get(name), Xatom.PIXMAP)
            if old and old[0] != pix.id:
                try:  # Display has no kill_client in Ubuntu's python3-xlib: the request itself
                    xrequest.KillClient(display=self.d.display, resource=old[0])
                except XError:
                    pass
        self.root.change_attributes(background_pixmap=pix)
        self.root.clear_area()
        for name in ("_XROOTPMAP_ID", "ESETROOT_PMAP_ID"):
            self.root.change_property(self.a.get(name), Xatom.PIXMAP, 32, [pix.id])
        self.d.set_close_down_mode(X.RetainPermanent)
        self.d.flush()

    def read_title(self):
        try:
            with open(os.path.join(STATE, "desk.json"), encoding="utf-8") as f:
                self.title = " ".join(str(json.load(f).get("title") or "").split())[:80]
        except (OSError, ValueError, AttributeError):
            self.title = ""

    # ---- the top bar and the dock: windows that reserve their edge of the screen
    def _panel(self, y, height, name, strut, events):
        win = self.root.create_window(0, y, self.W, height, 0, self.screen.root_depth, X.InputOutput,
                                      X.CopyFromParent, background_pixmap=X.NONE, event_mask=events)
        win.set_wm_name(name)
        win.set_wm_class(name.replace(" ", "-"), name.replace(" ", "-"))
        a = self.a
        win.change_property(a.get("_NET_WM_WINDOW_TYPE"), Xatom.ATOM, 32, [a.get("_NET_WM_WINDOW_TYPE_DOCK")])
        win.change_property(a.get("_NET_WM_STATE"), Xatom.ATOM, 32,
                            [a.get("_NET_WM_STATE_ABOVE"), a.get("_NET_WM_STATE_STICKY"),
                             a.get("_NET_WM_STATE_SKIP_TASKBAR"), a.get("_NET_WM_STATE_SKIP_PAGER")])
        win.change_property(a.get("_NET_WM_DESKTOP"), Xatom.CARDINAL, 32, [0xFFFFFFFF])
        top, bottom = strut
        win.change_property(a.get("_NET_WM_STRUT"), Xatom.CARDINAL, 32, [0, 0, top, bottom])
        win.change_property(a.get("_NET_WM_STRUT_PARTIAL"), Xatom.CARDINAL, 32,
                            [0, 0, top, bottom, 0, 0, 0, 0, 0, self.W - 1 if top else 0, 0, self.W - 1 if bottom else 0])
        win.set_wm_hints(flags=1, input=0)  # never takes the keyboard
        win.set_wm_normal_hints(flags=1 | 2 | 16 | 32, x=0, y=y, min_width=self.W, min_height=height,
                                max_width=self.W, max_height=height)
        return win

    def make_bar(self):
        self.bar = self._panel(0, art.TOP_H, "openajar top bar", (art.TOP_H, 0), X.ExposureMask)
        self.bar_gc = self.bar.create_gc()
        self.bar_wall = self.wall.crop((0, 0, self.W, art.TOP_H))
        try:
            self.mark = art.icon(STATE, "mark", art.MARK, color=(255, 255, 255))
        except (OSError, subprocess.SubprocessError):
            self.mark = None
        self.paint_bar()
        self.bar.map()

    def paint_bar(self, force=False):
        now = time.time()
        words = self.status.words()
        key = (words, self.title, int((now + self.status.tz * 60) // 60))
        if key != self.bar_key or self.bar_pic is None:
            self.bar_key = key
            self.bar_pic = art.bar(self.bar_wall, self.title, words, self.mark, now, self.status.tz)
            force = True
        if force:
            _put(self.d, self.bar, self.bar_gc, 0, 0, self.bar_pic)
            self.d.flush()

    def make_dock(self):
        self.dock = self._panel(self.H - art.BAND_H, art.BAND_H, "openajar dock", (0, art.BAND_H),
                                X.ExposureMask | X.ButtonPressMask | X.ButtonReleaseMask | X.PointerMotionMask
                                | X.LeaveWindowMask)
        self.gc = self.dock.create_gc()
        self.icons = {key: art.icon(STATE, key, art.ICON) for key in ("browser", "terminal", "files", "agent", "app")}
        try:
            self.app_tile = art.icon(STATE, "app-tile", art.ICON)
        except (OSError, subprocess.SubprocessError):
            self.app_tile = None
        self.dock.map()

    def render(self):
        pinned = [i for i in self.items if i.pinned]
        rest = [i for i in self.items if not i.pinned]
        left, pw, xs, sep_x = art.layout(self.W, len(pinned), len(rest))
        for item, x in zip(pinned + rest, xs, strict=True):
            item.x = x
        entries = [(i.x, i.icon, bool(i.windows)) for i in pinned + rest]
        self.band = art.band(self.wall, entries, sep_x, left, pw)
        self._dirty = False
        self.paint()

    def paint(self):
        if self.band is not None:
            _put(self.d, self.dock, self.gc, 0, 0, self.band)
            self.d.flush()

    # ---- tooltips and the dock's note
    def _label_window(self, text, item, y, dot=False):
        w, h = art.label_width(text, dot), art.TIP_H
        x = max(4, min(self.W - w - 4, item.x + art.ICON // 2 - w // 2))
        try:
            under = self.root.get_image(x, y, w, h, X.ZPixmap, 0xFFFFFFFF)
            bg = Image.frombytes("RGB", (w, h), under.data, "raw", "BGRX")
        except XError:
            bg = Image.new("RGB", (w, h), (20, 24, 40))
        pic = art.label(bg, text, dot)
        win = self.root.create_window(x, y, w, h, 0, self.screen.root_depth, X.InputOutput, X.CopyFromParent,
                                      override_redirect=1, event_mask=X.ExposureMask)
        win.map()
        gc = win.create_gc()
        _put(self.d, win, gc, 0, 0, pic)
        self.d.flush()
        return win, gc, pic

    def show_tip(self, item):
        self.hide_tip()
        if item is not None:
            self.tip = self._label_window(item.label, item, self.H - art.TIP_ABOVE - art.TIP_H)

    def show_note(self, text, item):
        """A note over the dock's `item` that stays (a hover's tip does not move it) until hide_note or NOTE_S."""
        self.hide_note()
        win, gc, pic = self._label_window(text, item, self.H - art.TIP_ABOVE - 2 * art.TIP_H - 8, dot=True)
        self.note = (win, gc, pic, time.monotonic() + NOTE_S)

    def _drop(self, held):
        win, gc = held[0], held[1]
        try:
            gc.free()
            win.destroy()
        except XError:
            pass

    def hide_note(self):
        if self.note is not None:
            self._drop(self.note)
            self.note = None
            self.d.flush()

    def hide_tip(self):
        if self.tip is not None:
            self._drop(self.tip)
            self.tip = None

    # ---- windows on the screen
    def _kind(self, win):
        try:
            cls = win.get_wm_class() or ("", "")
        except XError:
            return None
        types = _prop(win, self.a.get("_NET_WM_WINDOW_TYPE")) or []
        normal = self.a.get("_NET_WM_WINDOW_TYPE_NORMAL")
        if types and normal not in types and self.a.get("_NET_WM_WINDOW_TYPE_DIALOG") not in types:
            return None
        state = _prop(win, self.a.get("_NET_WM_STATE")) or []
        if self.a.get("_NET_WM_STATE_SKIP_TASKBAR") in state:
            return None
        name, klass = (cls[0] or "").lower(), (cls[1] or "").lower()
        if "camoufox" in (name + klass) or "firefox" in (name + klass) or name == "navigator":
            return "browser"
        if klass == "lxterminal":
            pid = _prop(win, self.a.get("_NET_WM_PID"), Xatom.CARDINAL)
            mine = bool(pid) and pid[0] in self.agent_pids
            return "agent" if mine or self._title(win).endswith(AGENT_SUFFIX) else "terminal"
        if klass == "pcmanfm":
            return "files"
        return "app:" + (klass or name or str(win.id))

    def _title(self, win):
        t = _prop(win, self.a.get("_NET_WM_NAME"), self.a.get("UTF8_STRING"))
        if t is None:
            t = _prop(win, Xatom.WM_NAME)
        if isinstance(t, bytes):
            t = t.decode("utf-8", "replace")
        return t or ""

    def agent_title(self):
        return (self.status.agent or "Agent") + AGENT_SUFFIX

    def dress_agent_window(self, win):
        """The agent's terminal window: titled with the agent's name, and the openajar mark as its icon."""
        title = self.agent_title()
        try:
            if self._title(win) != title:
                win.change_property(self.a.get("_NET_WM_NAME"), self.a.get("UTF8_STRING"), 8, title.encode())
                win.change_property(Xatom.WM_NAME, Xatom.STRING, 8, title.encode("latin-1", "replace"))
            if not _prop(win, self.a.get("_AGENTOS_DRESSED")):
                big = art.icon(STATE, "agent", 48)
                win.change_property(self.a.get("_NET_WM_ICON"), Xatom.CARDINAL, 32,
                                    _icon_data([big.resize((24, 24), Image.LANCZOS), big]))
                win.change_property(self.a.get("_AGENTOS_DRESSED"), Xatom.CARDINAL, 32, [1])
        except (XError, OSError, subprocess.SubprocessError):
            pass

    def refresh(self):
        ids = _prop(self.root, self.a.get("_NET_CLIENT_LIST"), Xatom.WINDOW) or []
        active = _prop(self.root, self.a.get("_NET_ACTIVE_WINDOW"), Xatom.WINDOW)
        self.active = active[0] if active else None
        seen = {}
        for wid in ids:
            win = self.clients.get(wid, (None, None))[1] or self.d.create_resource_object("window", wid)
            kind = self.clients[wid][0] if wid in self.clients else self._kind(win)
            if kind is None:
                continue
            seen[wid] = (kind, win)
            if wid not in self.clients:
                try:
                    win.change_attributes(event_mask=X.PropertyChangeMask | X.StructureNotifyMask)
                except XError:
                    continue
                if kind == "browser":
                    self.fix_title(win)
                elif kind == "agent":
                    self.dress_agent_window(win)
        self.clients = seen
        self._geo_dirty = True
        keep = self.guard.saw(self.active, {wid: k for wid, (k, _w) in seen.items()}, time.monotonic())
        if keep is not None and keep != self.active:
            self.activate(seen[keep][1])  # a browser that just came up does not take the keyboard (FocusGuard)
        agent = [w for k, w in seen.values() if k == "agent"]
        self.agent_term = agent[-1] if agent else None
        items = [Item(k, label, self.icons[k], True) for k, label in PINNED]
        by = {i.key: i for i in items}
        for kind, win in seen.values():
            if kind in by:
                by[kind].windows.append(win)
                continue
            if kind == "agent":
                item = Item("agent", self.agent_title(), self.icons["agent"], False)
            else:
                own = _net_icon(win, art.ICON)
                title = self._title(win) or kind[4:]
                pic = art.tile(own, self.app_tile, art.ICON) if own is not None else self.icons["app"]
                item = Item(kind, title[:60], pic, False)
            items.append(item)
            by[kind] = item
            item.windows.append(win)
        if self.note is not None and by["browser"].windows:
            self.hide_note()  # the browser came
        hover_key = self.hover.key if self.hover else None
        self.items = items
        self.hover = next((i for i in items if i.key == hover_key), None)
        self.render()

    def fix_title(self, win):
        """The browser's title without " \u2014 <its brand>": the page's own title is what a person reads."""
        title = self._title(win)
        if " \u2014 " not in title:
            return
        page = title.rsplit(" \u2014 ", 1)[0]
        try:
            win.change_property(self.a.get("_NET_WM_NAME"), self.a.get("UTF8_STRING"), 8, page.encode())
            win.change_property(Xatom.WM_NAME, Xatom.STRING, 8, page.encode("latin-1", "replace"))
        except XError:
            pass

    # ---- where the browser's window is, for the node
    def browser_area(self):
        """The newest browser window's visible area on the screen: its client window without GTK's invisible
        edge round a browser that draws its own title bar. None while it is gone or minimized."""
        wins = [w for k, w in self.clients.values() if k == "browser"]
        if not wins:
            return None
        win = wins[-1]
        try:
            if win.get_attributes().map_state != X.IsViewable:
                return None
            g = win.get_geometry()
            at = self.root.translate_coords(win, 0, 0)
        except XError:
            return None
        if self.a.get("_NET_WM_STATE_HIDDEN") in (_prop(win, self.a.get("_NET_WM_STATE")) or []):
            return None
        edge = _prop(win, self.a.get("_GTK_FRAME_EXTENTS"), Xatom.CARDINAL) or [0, 0, 0, 0]
        left, right, top, bottom = (list(edge) + [0, 0, 0, 0])[:4]
        w, h = g.width - left - right, g.height - top - bottom
        if w <= 0 or h <= 0:
            return None
        return at.x + left, at.y + top, w, h

    def _geo_line(self):
        area = self.geo
        x, y, w, h = area if area else (0, 0, 0, 0)
        return json.dumps({"t": "win", "x": x, "y": y, "w": w, "h": h, "shown": area is not None}).encode() + b"\n"

    def send_geometry(self):
        self._geo_dirty = False
        area = self.browser_area()
        if area == self.geo:
            return
        self.geo = area
        self._tell(self._geo_line())

    def _tell(self, line, conns=None):
        """A line to every node connected (or to `conns`); True when one took it."""
        sent = False
        for c in list(conns if conns is not None else self.conns):
            try:
                c.send(line)
                sent = True
            except OSError:
                continue
        return sent

    # ---- acting on windows
    def activate(self, win):
        ev = xevent.ClientMessage(window=win, client_type=self.a.get("_NET_ACTIVE_WINDOW"),
                                  data=(32, [2, X.CurrentTime, 0, 0, 0]))
        self.root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        self.d.flush()

    def iconify(self, win):
        ev = xevent.ClientMessage(window=win, client_type=self.a.get("WM_CHANGE_STATE"), data=(32, [3, 0, 0, 0, 0]))
        self.root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        self.d.flush()

    def launch(self, argv):
        try:
            return subprocess.Popen(argv, cwd=WORK, env=dict(os.environ), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        except OSError as e:
            sys.stderr.write(f"could not start {argv[0]}: {e}\n")
            return None

    def open_terminal(self):
        self.launch(["lxterminal", "--no-remote", f"--working-directory={WORK}", "-e", SHELL])

    def open_files(self):
        self.launch(["pcmanfm", WORK])

    def open_agent_terminal(self):
        try:
            self.transcript._file()  # its first line, so the window never opens empty
        except OSError:
            pass
        tail = f"stty -echo -icanon 2>/dev/null; printf '\\033[?25l\\033[?7l'; exec tail -s 0.2 -n 400 -F {shlex.quote(LOG)} 2>/dev/null"
        proc = self.launch(["lxterminal", "--no-remote", f"--title={self.agent_title()}", "--geometry=84x18",
                            "-e", "sh -c " + shlex.quote(tail)])
        if proc is not None:
            self.agent_pids.add(proc.pid)

    def click(self, item, button):
        wins = item.windows
        if item.key == "browser":
            self.guard.asked(time.monotonic())
            if wins:
                self.activate(wins[-1])  # the agent's browser: raised, never a second one started
            elif self._tell(b'{"t": "want", "what": "browser"}\n'):
                self.show_note("Opening the browser", item)
            else:
                self.show_note("The browser opens when the agent uses it", item)
            return
        if button == 2 or not wins:  # a middle click opens another one
            {"terminal": self.open_terminal, "files": self.open_files,
             "agent": self.open_agent_terminal}.get(item.key, lambda: None)()
            return
        front = [w for w in wins if w.id == self.active]
        if front and len(wins) > 1:  # cycle through its windows
            i = [w.id for w in wins].index(front[0].id)
            self.activate(wins[(i + 1) % len(wins)])
        elif front and item.key.startswith("app:"):
            self.iconify(front[0])
        else:
            self.activate(wins[-1])

    def focus(self, what):
        if what == "browser":
            self.guard.asked(time.monotonic())
            wins = self.clients and [w for k, w in self.clients.values() if k == "browser"]
            if wins and wins[-1].id != self.active:
                self.activate(wins[-1])
        elif what == "terminal":
            if self.agent_term is None:
                self.open_agent_terminal()  # a new window comes to the front by itself
            elif self.agent_term.id != self.active:
                self.activate(self.agent_term)

    # ---- the node's socket
    def listen(self):
        try:
            os.unlink(SOCK)
        except OSError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)
        try:
            s.bind(SOCK)
        finally:
            os.umask(old)
        s.listen(4)
        s.setblocking(False)
        self.server = s

    def on_status(self, m):
        before = self.status.agent
        self.status.update(m)
        self.transcript.agent = self.status.agent
        if self.status.agent != before:
            for kind, win in self.clients.values():
                if kind == "agent":
                    self.dress_agent_window(win)
            self._dirty = True  # the dock's tooltip for the agent's window
        self.paint_bar()

    def on_message(self, m):
        t = m.get("t")
        if t == "begin":
            self.status.running.add(str(m.get("id")))
            self.transcript.begin(m)
            if m.get("show"):
                self.focus("terminal")
            self.paint_bar()
        elif t == "out":
            self.transcript.out(m)
        elif t == "end":
            self.status.running.discard(str(m.get("id")))
            self.transcript.end(m)
            self.paint_bar()
        elif t == "note":
            self.transcript.note(m)
        elif t == "focus":
            self.focus(m.get("what"))
        elif t == "status":
            self.on_status(m)

    def read_conn(self, c):
        buf = self.conns[c]
        try:
            data = c.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            del self.conns[c]
            c.close()
            if not self.conns:  # the node went: what it said was running is not any more
                self.status.running.clear()
                self.paint_bar()
            return
        buf += data
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf[:] = rest
            try:
                self.on_message(json.loads(line))
            except (ValueError, TypeError, AttributeError):
                continue
            except OSError as e:
                sys.stderr.write(f"transcript: {e}\n")
        if len(buf) > 1 << 20:  # a line that never ends: drop it
            buf.clear()

    # ---- the loop
    def handle(self, ev):
        if ev.type == X.Expose and ev.count == 0:
            self.expose(ev.window)
        elif ev.type == X.MotionNotify and ev.window == self.dock:
            item = self._at(ev.event_x, ev.event_y)
            if item is not self.hover:
                self.hover = item
                self.show_tip(item)
        elif ev.type == X.LeaveNotify and ev.window == self.dock:
            self.hover = None
            self.hide_tip()
        elif ev.type == X.ButtonRelease and ev.window == self.dock:
            item = self._at(ev.event_x, ev.event_y)
            if item is not None and ev.detail in (1, 2):
                self.click(item, ev.detail)
        elif ev.type == X.ConfigureNotify:
            if self.clients.get(getattr(ev.window, "id", None), (None,))[0] == "browser":
                self._geo_dirty = True
        elif ev.type in (X.MapNotify, X.UnmapNotify):
            self._geo_dirty = True
        elif ev.type == X.PropertyNotify:
            self.property_changed(ev)

    def expose(self, win):
        if win == self.dock:
            self.paint()
        elif win == self.bar:
            self.paint_bar(force=True)
        elif self.note is not None and win == self.note[0]:
            _put(self.d, self.note[0], self.note[1], 0, 0, self.note[2])
        elif self.tip is not None and win == self.tip[0]:
            _put(self.d, self.tip[0], self.tip[1], 0, 0, self.tip[2])

    def property_changed(self, ev):
        if ev.window == self.root and ev.atom in (self.a.get("_NET_CLIENT_LIST"), self.a.get("_NET_ACTIVE_WINDOW")):
            self._dirty = True
        elif ev.atom in (self.a.get("_NET_WM_NAME"), Xatom.WM_NAME):
            kind = self.clients.get(ev.window.id, (None,))[0]
            if kind == "browser":
                self.fix_title(ev.window)
            elif kind == "agent":
                self.dress_agent_window(ev.window)  # its title stays the agent's
            elif kind is not None:  # a terminal's title says whose it is: look again
                self.clients.pop(ev.window.id, None)
                self._dirty = True
        elif ev.atom == self.a.get("_NET_WM_STATE"):
            self._dirty = True
        elif ev.atom == self.a.get("_GTK_FRAME_EXTENTS"):
            self._geo_dirty = True

    def _at(self, x, y):
        top = art.BAND_H - art.BOTTOM - art.PILL_H
        if not top <= y <= top + art.PILL_H:
            return None
        for item in self.items:
            if item.x - art.GAP // 2 <= x < item.x + art.ICON + art.GAP // 2:
                return item
        return None

    def _wait(self):
        """How long select may sleep: until the note goes, the clock's next minute, or the words change."""
        now = time.time()
        waits = [60.05 - (now % 60)]
        if self.note is not None:
            waits.append(self.note[3] - time.monotonic())
        aging = self.status.wake_in()
        if aging is not None:
            waits.append(aging + 0.05)
        return max(0.0, min(waits))

    def accept(self):
        try:
            c, _ = self.server.accept()
        except OSError:
            return
        c.setblocking(False)
        self.conns[c] = bytearray()
        self._tell(self._geo_line(), [c])  # where the browser is, from the start

    def run(self):
        self.listen()
        self.read_title()
        self.set_wallpaper()
        self.make_bar()
        self.make_dock()
        self.root.change_attributes(event_mask=X.PropertyChangeMask)
        # the pointer starts in the bottom corner, on the wallpaper: not over the windows the agent works in
        self.root.warp_pointer(self.W - 56, self.H - 44)
        self.refresh()
        open(os.path.join(STATE, "desk.ready"), "w").close()
        xfd = self.d.fileno()
        while True:
            # python-xlib queues events it reads while it waits for a reply (refresh asks for properties), and
            # those never wake select: drain the queue until nothing is pending and nothing is stale
            while True:
                while self.d.pending_events():
                    self.handle(self.d.next_event())
                if self._dirty:
                    self.refresh()
                    continue
                if self._geo_dirty:
                    self.send_geometry()
                    continue
                break
            if self.note is not None and self.note[3] <= time.monotonic():
                self.hide_note()
            self.paint_bar()  # only when its words or the minute changed
            r, _, _ = select.select([xfd, self.server, *self.conns], [], [], self._wait())
            for f in r:
                if f is self.server:
                    self.accept()
                elif f in self.conns:
                    self.read_conn(f)


def main():
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # the apps it starts are never waited for
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Desk().run()


if __name__ == "__main__":
    main()
