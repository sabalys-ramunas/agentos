"""Runs on an environment's machine, never on the node: the agent's eyes and hands on the desktop
(openajar.environments.screen, the screen_* tools). The node sends this file with each call, over the SSH
connection it holds, as the `script` of a JSON request on stdin, as it sends vmbrowser_ctl.py; it runs with
the system's python3 and the python3-xlib and python3-pil that desktop.sh installs, on the screen :99 that
vmbrowser_ctl started (its cookie in /run/agentos-browser/xauth). Nothing listens anywhere. It sits beside
desk.py because it is the desktop's (python-xlib and PIL), and travels with the desktop's files too, but the
node never runs that copy: it sends its own with each call.

Actions, each printing one JSON line:

- look {window, image, max_side, png_max}: the open windows (id, title, app, kind, x, y, w, h, front,
  minimized), the pointer, the screen's size and, with `image`, a PNG of the screen (or of one window's area
  of it) scaled so its longest side is at most `max_side`, with the scale that maps it back to the screen.
- act {action, x, y, to_x, to_y, text, keys, window, direction, amount, deadline_s}: one action as real
  input through XTEST, paced like a person's hand (pauses drawn log-normal, a curved glide of the pointer,
  keys held and flown as a typist's): click, double_click, right_click, move, drag, scroll, type, key, and
  focus (a window to the front, through the window manager). It answers what changed: the window in front,
  windows that opened or closed, the pointer. Every key and button it pressed is released when it ends,
  also when it fails or is stopped, and a key it had to map for a character is unmapped again. The node
  stops it with SIGTERM when a person takes over: it is seen at the next pause between keys or steps
  (at most a few hundred milliseconds), and the answer says kind "stopped" and how many characters of
  the text were typed.
- apps {limit}: the apps the desktop's menu would list (.desktop entries: name, id, what they run).
- open {app, file, wait_s}: start an app (a .desktop entry by its name or id, else a program on the PATH),
  detached in a session of its own, on the screen, in /work; then wait up to wait_s for its window. Its
  output goes to a log beside the screen's, of which only the tail of a failed start is said back.

The text to type travels on stdin only, never on a command line, and is never printed back.
"""

import base64
import contextlib
import io
import json
import math
import os
import random
import shlex
import shutil
import signal
import subprocess
import sys
import time

from PIL import Image
from Xlib import XK, X, Xatom, display, xauth
from Xlib import error as xerror
from Xlib.error import XError
from Xlib.ext import xtest
from Xlib.protocol import event as xevent

DISPLAY = ":99"
XAUTH = "/run/agentos-browser/xauth"
WINDOWS_MAX = 40          # windows listed, from the front
TITLE_MAX = 160
MAX_SIDE = 1280           # the picture's longest side by default: what a model reads without scaling it again
PNG_MAX = 640 << 10
SETTLE_S = 0.35           # after an action: let a window open, close or come to the front
STEP_S = 0.008            # a pointer glide moves about every 8 ms
TEXT_MAX = 4000
COMBOS_MAX = 12
NOTCHES_MAX = 30
AGENT_SUFFIX = " · commands"  # desk.py: the agent's own terminal window
MODIFIERS = {"ctrl": "Control_L", "control": "Control_L", "shift": "Shift_L", "alt": "Alt_L", "super": "Super_L",
             "win": "Super_L", "meta": "Super_L", "cmd": "Super_L", "altgr": "ISO_Level3_Shift"}
KEY_NAMES = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab",
             "backspace": "BackSpace", "delete": "Delete", "del": "Delete", "insert": "Insert", "home": "Home",
             "end": "End", "pageup": "Page_Up", "pgup": "Page_Up", "pagedown": "Page_Down", "pgdn": "Page_Down",
             "up": "Up", "down": "Down", "left": "Left", "right": "Right", "space": "space", "plus": "plus",
             "minus": "minus", "menu": "Menu", "print": "Print", "printscreen": "Print", "capslock": "Caps_Lock"}
TYPED_KEYS = {"\n": "Return", "\r": "Return", "\t": "Tab"}
NEIGHBOURS = set("qwertyuiopasdfghjklzxcvbnm")  # letter pairs a fluent hand rolls over (openajar.node.human)
ROLLOVER_RATE = 0.27
WHEEL = {"up": 4, "down": 5, "left": 6, "right": 7}
APP_DIRS = ("/usr/share/applications", "/usr/local/share/applications", "/root/.local/share/applications")
FIELD_CODES = ("%f", "%F", "%u", "%U")
OLD_CODES = ("%d", "%D", "%n", "%N", "%i", "%c", "%k", "%v", "%m")
APP_ENV = {"DISPLAY": DISPLAY, "XAUTHORITY": XAUTH, "GTK_THEME": "Adwaita", "NO_AT_BRIDGE": "1",
           "GDK_BACKEND": "x11"}  # as the agent's commands get them (openajar.environments.desklink.SCREEN_ENV)
OPEN_LOG = "/run/agentos-browser/open.log"
NOT_APPS = ("x11vnc",)  # programs an agent is not offered: a VNC server that would listen on TCP


class Stop(Exception):
    """The action cannot go on: its time is up, or what it was given is not right. Said back as `error`."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.typed = None  # an act's characters typed before it ended


# SIGTERM or SIGHUP: asked to stop. While an act sends input the stop waits for its next pause (never in
# the middle of an X request, so every key it holds can still come up); anywhere else it is at once.
STOP = {"asked": False, "at_once": True}


def _say(**out):
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- timing, as openajar.node.human draws it


def draw_ms(lo, hi):
    """A time between lo and hi, log-normal with its peak low in the range and a long tail up."""
    if hi <= lo:
        return lo
    low, ratio = max(lo, 1), hi / max(lo, 1)
    middle, spread = low * ratio ** 0.3, math.log(ratio) / 3
    for _ in range(12):
        value = random.lognormvariate(math.log(middle), spread)
        if lo <= value <= hi:
            return value
    return middle


def hold_ms():
    return min(max(random.lognormvariate(math.log(85), 0.3), 40.0), 220.0)


def flight_ms():
    return min(max(random.lognormvariate(math.log(150), 0.35), 80.0), 600.0)


def glide_ms(dist):
    """How long a hand takes to reach a point `dist` pixels away (Fitts: longer reaches take longer,
    but not in proportion), with a person's spread."""
    base = 150 + 85 * math.log2(1 + dist / 12)
    return min(max(base * random.lognormvariate(0, 0.15), 110.0), 1200.0)


# ---------------------------------------------------------------- X


def _wild_auth(self, family, address, dispno, types=(b"MIT-MAGIC-COOKIE-1",)):
    """The screen's cookie is FamilyWild (any host); python-xlib matches by address only (as desk.py)."""
    num = str(dispno).encode()
    for efam, eaddr, enum, ename, edata in self.entries:
        if ename in types and (enum in (b"", num)) and (efam == 65535 or (efam == family and eaddr == address)):
            return ename, edata
    raise xauth.error.XNoAuthError((family, address, dispno))


class Screen:
    def __init__(self):
        os.environ["XAUTHORITY"] = os.environ.get("XAUTHORITY") or XAUTH
        xauth.Xauthority.get_best_auth = _wild_auth
        self.d = display.Display(DISPLAY)
        self.root = self.d.screen().root
        geo = self.root.get_geometry()
        self.W, self.H = geo.width, geo.height
        self._atoms = {}
        self.pressed_keys = []
        self.pressed_buttons = []
        self.mapped = {}  # spare keycode -> keysym mapped for a character this action types
        self.deadline = None
        self.typed = 0

    def atom(self, name):
        if name not in self._atoms:
            self._atoms[name] = self.d.intern_atom(name)
        return self._atoms[name]

    def prop(self, win, name, kind=X.AnyPropertyType):
        try:
            p = win.get_full_property(self.atom(name) if isinstance(name, str) else name, kind)
        except XError:
            return None
        return p.value if p is not None else None

    def title(self, win):
        t = self.prop(win, "_NET_WM_NAME", self.atom("UTF8_STRING"))
        if t is None:
            t = self.prop(win, Xatom.WM_NAME)
        if isinstance(t, bytes):
            t = t.decode("utf-8", "replace")
        return (t or "")[:TITLE_MAX]

    # ---- windows
    def client_ids(self):
        ids = self.prop(self.root, "_NET_CLIENT_LIST_STACKING", Xatom.WINDOW)
        if ids is None:
            ids = self.prop(self.root, "_NET_CLIENT_LIST", Xatom.WINDOW) or []
        return [int(i) for i in ids]

    def active_id(self):
        active = self.prop(self.root, "_NET_ACTIVE_WINDOW", Xatom.WINDOW)
        return int(active[0]) if active and active[0] else None

    def describe(self, wid, active):
        """One window as the agent reads it, or None for what is not an app's window (the dock, the top
        bar, a tooltip) or a window that went meanwhile."""
        win = self.d.create_resource_object("window", wid)
        try:
            cls = win.get_wm_class() or ("", "")
            attrs = win.get_attributes()
            geo = win.get_geometry()
            at = self.root.translate_coords(win, 0, 0)
        except XError:
            return None
        types = self.prop(win, "_NET_WM_WINDOW_TYPE") or []
        if types and not {self.atom("_NET_WM_WINDOW_TYPE_NORMAL"), self.atom("_NET_WM_WINDOW_TYPE_DIALOG")} & set(types):
            return None
        state = set(self.prop(win, "_NET_WM_STATE") or [])
        if self.atom("_NET_WM_STATE_SKIP_TASKBAR") in state:
            return None
        title = self.title(win)
        name, klass = (cls[0] or ""), (cls[1] or "")
        # the window's area as it shows: the window manager's frame (its title bar) and not GTK's invisible
        # edge round a window that draws its own title bar
        fl, fr, ft, fb = (list(self.prop(win, "_NET_FRAME_EXTENTS", Xatom.CARDINAL) or []) + [0, 0, 0, 0])[:4]
        gl, gr, gt, gb = (list(self.prop(win, "_GTK_FRAME_EXTENTS", Xatom.CARDINAL) or []) + [0, 0, 0, 0])[:4]
        x, y = at.x + gl - fl, at.y + gt - ft
        w, h = geo.width - gl - gr + fl + fr, geo.height - gt - gb + ft + fb
        hidden = self.atom("_NET_WM_STATE_HIDDEN") in state or attrs.map_state != X.IsViewable
        return {"id": wid, "title": title, "app": (klass or name)[:60], "kind": self.kind(name, klass, title),
                "x": x, "y": y, "w": max(0, w), "h": max(0, h), "front": wid == active, "minimized": hidden}

    @staticmethod
    def kind(name, klass, title):
        both = (name + " " + klass).lower()
        if "camoufox" in both or "firefox" in both or name.lower() == "navigator":
            return "browser"
        if klass.lower() == "lxterminal":
            return "agent_terminal" if title.endswith(AGENT_SUFFIX) else "terminal"
        if klass.lower() == "pcmanfm":
            return "files"
        return "app"

    def windows(self):
        active = self.active_id()
        out = []
        for wid in reversed(self.client_ids()):  # the front first
            w = self.describe(wid, active)
            if w is not None:
                out.append(w)
            if len(out) >= WINDOWS_MAX:
                break
        return out

    def pointer(self):
        p = self.root.query_pointer()
        return [p.root_x, p.root_y]

    # ---- the picture
    def picture(self, area, max_side, png_max):
        """The screen's pixels in `area` (x, y, w, h, inside the screen) as a PNG at most `max_side` on its
        longest side and `png_max` bytes: in full colour when that fits, else in 256 colours (a wallpaper's
        gradients are what make a desktop's PNG large; an app's flat colours survive), else smaller.
        Returns (png, size, scale, colors)."""
        x, y, w, h = area
        raw = self.root.get_image(x, y, w, h, X.ZPixmap, 0xFFFFFFFF)
        im = Image.frombytes("RGB", (w, h), raw.data, "raw", "BGRX")
        side = max_side
        for _ in range(4):
            scale = min(1.0, side / max(w, h))
            shot = im if scale >= 1.0 else im.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                                                     Image.LANCZOS)
            for colors in (0, 256):
                pic = shot if not colors else shot.quantize(colors, method=Image.Quantize.FASTOCTREE,
                                                            dither=Image.Dither.NONE)
                buf = io.BytesIO()
                pic.save(buf, "PNG", compress_level=3 if not colors else 6)  # level 6 on full colour: a tenth
                data = buf.getvalue()                                        # smaller, three times slower
                if len(data) <= png_max:
                    return data, shot.size, scale, colors
            side = int(max(shot.size) * 0.75)
        raise Stop("too_large", "the screen's picture is too large even when small")

    # ---- input
    def check_time(self):
        if STOP["asked"]:
            raise Stop("stopped", "the action was stopped")
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise Stop("time_limit", "the action ran out of time")

    def sleep_ms(self, ms):
        self.check_time()
        STOP["at_once"] = True
        try:
            time.sleep(ms / 1000)
        finally:
            STOP["at_once"] = False
        self.check_time()

    def motion(self, x, y):
        xtest.fake_input(self.d, X.MotionNotify, x=int(x), y=int(y))
        self.d.flush()

    def button(self, n, down):
        xtest.fake_input(self.d, X.ButtonPress if down else X.ButtonRelease, n)
        self.d.sync()
        if down:
            self.pressed_buttons.append(n)
        elif n in self.pressed_buttons:
            self.pressed_buttons.remove(n)

    def key(self, code, down):
        xtest.fake_input(self.d, X.KeyPress if down else X.KeyRelease, code)
        self.d.sync()
        if down:
            self.pressed_keys.append(code)
        elif code in self.pressed_keys:
            self.pressed_keys.remove(code)

    def let_go(self):
        """Every key and button this action holds comes up, and characters it mapped are unmapped."""
        for code in reversed(list(self.pressed_keys)):
            with contextlib.suppress(Exception):  # the next one still comes up
                self.key(code, False)
        for n in reversed(list(self.pressed_buttons)):
            with contextlib.suppress(Exception):
                self.button(n, False)
        if self.mapped:
            with contextlib.suppress(Exception):
                width = self._syms_per_code()
                for code in self.mapped:
                    self.d.change_keyboard_mapping(code, [(X.NoSymbol,) * width])
                self.d.sync()
            self.mapped.clear()

    def glide(self, x, y, slow=1.0):
        """The pointer to (x, y) as a hand moves it: a curve that bows a little to one side, quick in the
        middle and slow at both ends, now and then a little past a far target and back."""
        sx, sy = self.pointer()
        dist = math.hypot(x - sx, y - sy)
        if dist < 1:
            return
        if dist > 240 and random.random() < 0.3:
            past = 1 + random.uniform(0.03, 0.08)
            ox, oy = sx + (x - sx) * past, sy + (y - sy) * past
            if 0 <= ox < self.W and 0 <= oy < self.H:
                self._curve((sx, sy), (ox, oy), glide_ms(dist) * slow)
                self.sleep_ms(draw_ms(60, 190))
                sx, sy = ox, oy
                dist = math.hypot(x - sx, y - sy)
        self._curve((sx, sy), (x, y), glide_ms(dist) * slow)

    def _curve(self, a, b, ms):
        (ax, ay), (bx, by) = a, b
        dx, dy = bx - ax, by - ay
        dist = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / dist, dx / dist  # a bow to one side, up to a sixth of the way
        bow1, bow2 = random.uniform(-0.17, 0.17) * dist, random.uniform(-0.12, 0.12) * dist
        c1 = (ax + dx * 0.3 + nx * bow1, ay + dy * 0.3 + ny * bow1)
        c2 = (ax + dx * 0.7 + nx * bow2, ay + dy * 0.7 + ny * bow2)
        steps = max(4, int(ms / 1000 / STEP_S))
        began, last = time.monotonic(), None
        for i in range(1, steps + 1):
            s = i / steps
            t = s * s * s * (10 - 15 * s + 6 * s * s)  # minimum jerk: slow, quick, slow
            u = 1 - t
            px = u ** 3 * ax + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t ** 3 * bx
            py = u ** 3 * ay + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t ** 3 * by
            point = (min(max(round(px), 0), self.W - 1), min(max(round(py), 0), self.H - 1))
            if i == steps:
                point = (int(bx), int(by))
            if point != last:
                self.motion(*point)
                last = point
            self.check_time()
            wait = began + s * ms / 1000 - time.monotonic()
            if wait > 0:
                time.sleep(wait)

    def press_button(self, n, lo=50, hi=230):
        self.button(n, True)
        try:
            self.sleep_ms(draw_ms(lo, hi))
        finally:
            self.button(n, False)

    # ---- keys
    def _syms_per_code(self):
        first = self.d.display.info.min_keycode
        return len(self.d.get_keyboard_mapping(first, 1)[0])

    def code_for(self, keysym):
        """(keycode, needs shift) for a keysym: a key the keymap has, else a spare keycode mapped to it."""
        if keysym in self.mapped.values():
            return next(c for c, s in self.mapped.items() if s == keysym), False
        for code, index in self.d.keysym_to_keycodes(keysym):
            if index in (0, 1):
                return code, index == 1
        info = self.d.display.info
        first, count = info.min_keycode, info.max_keycode - info.min_keycode + 1
        table = self.d.get_keyboard_mapping(first, count)
        spare = [first + i for i, syms in enumerate(table) if not any(syms) and first + i not in self.mapped]
        if not spare:
            raise Stop("invalid", "the keyboard has no free key for a character in the text")
        code = spare[-1]
        self.d.change_keyboard_mapping(code, [(keysym,) * len(table[0])])
        self.d.sync()
        self.mapped[code] = keysym
        time.sleep(0.05)  # apps read the new mapping (MappingNotify) before the key comes
        return code, False

    def keysym_of_name(self, name):
        if len(name) == 1:
            return self.keysym_of_char(name)
        sym = XK.string_to_keysym(KEY_NAMES.get(name.lower(), name))
        if not sym:
            sym = XK.string_to_keysym(name[:1].upper() + name[1:])  # f5 -> F5
        if not sym:
            raise Stop("invalid", f"no key is named {name!r}: use names like Return, Escape, Tab, BackSpace, Delete, "
                                  "Home, End, Page_Up, Up, F5, a, 1 or slash")
        return sym

    @staticmethod
    def keysym_of_char(ch):
        if ch in TYPED_KEYS:
            return XK.string_to_keysym(TYPED_KEYS[ch])
        cp = ord(ch)
        if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
            return cp
        if cp < 0x20 or 0x7F <= cp < 0xA0:
            raise Stop("invalid", "the text has a control character other than a new line or a tab")
        return 0x01000000 | cp

    def type_text(self, text):
        """Key by key: each held about 85 ms, flights of about 150 ms between key downs, Shift held round
        capitals, and a letter now and then down before the one before it is up."""
        shift = self.code_for(XK.string_to_keysym("Shift_L"))[0]
        shift_down, prev_char, prev_code = False, "", None
        for ch in text:
            code, needs_shift = self.code_for(self.keysym_of_char(ch))
            if needs_shift != shift_down:
                if prev_code is not None:
                    self.key(prev_code, False)
                    prev_code = None
                    self.sleep_ms(draw_ms(20, 70))
                self.key(shift, needs_shift)
                shift_down = needs_shift
                self.sleep_ms(draw_ms(40, 110))
            hold = hold_ms()
            if prev_code is not None:
                rolls = (prev_char.lower() in NEIGHBOURS and ch.lower() in NEIGHBOURS and prev_char != ch
                         and random.random() < ROLLOVER_RATE)
                if not rolls:
                    self.key(prev_code, False)
                    prev_code = None
                    self.sleep_ms(max(10.0, flight_ms() - hold))
                else:
                    self.sleep_ms(draw_ms(15, 60))
            self.key(code, True)
            self.typed += 1
            if prev_code is not None:  # the rolled over key comes up just after the next went down
                self.sleep_ms(draw_ms(10, 45))
                self.key(prev_code, False)
            self.sleep_ms(hold)
            prev_char, prev_code = ch, code
        if prev_code is not None:
            self.key(prev_code, False)
        if shift_down:
            self.sleep_ms(draw_ms(20, 70))
            self.key(shift, False)

    def combo(self, words):
        """One combination like ctrl+shift+t: the modifiers down one after another, the key, then all up."""
        parts = [p for p in words.split("+") if p] if words != "+" else ["plus"]
        if words.endswith("++"):
            parts.append("plus")
        if not parts:
            raise Stop("invalid", "an empty key")
        mods = [MODIFIERS[p.lower()] for p in parts[:-1] if p.lower() in MODIFIERS]
        if len(mods) != len(parts) - 1:
            bad = next(p for p in parts[:-1] if p.lower() not in MODIFIERS)
            raise Stop("invalid", f"{bad!r} is not a modifier: use ctrl, shift, alt or super before the key")
        last = parts[-1]
        if last.lower() in MODIFIERS:  # a modifier alone (Super opens nothing here, but it is a key)
            sym, needs_shift = XK.string_to_keysym(MODIFIERS[last.lower()]), False
            code = self.code_for(sym)[0]
        else:
            if len(last) == 1 and mods and last.isalpha():
                last = last.lower()  # ctrl+shift+T is ctrl+shift+t: the shift is said
            code, needs_shift = self.code_for(self.keysym_of_name(last))
        if needs_shift and "Shift_L" not in mods:
            mods.append("Shift_L")
        held = []
        try:
            for m in mods:
                mc = self.code_for(XK.string_to_keysym(m))[0]
                self.key(mc, True)
                held.append(mc)
                self.sleep_ms(draw_ms(30, 90))
            self.key(code, True)
            self.sleep_ms(draw_ms(60, 140))
            self.key(code, False)
        finally:
            for mc in reversed(held):
                time.sleep(draw_ms(15, 50) / 1000)
                self.key(mc, False)

    def activate(self, wid):
        win = self.d.create_resource_object("window", wid)
        ev = xevent.ClientMessage(window=win, client_type=self.atom("_NET_ACTIVE_WINDOW"),
                                  data=(32, [2, X.CurrentTime, 0, 0, 0]))
        self.root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        self.d.flush()


# ---------------------------------------------------------------- the actions


def _int(req, name, lo, hi, default=None):
    value = req.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise Stop("invalid", f"{name} is a whole number")
    value = round(value)
    if not lo <= value <= hi:
        raise Stop("invalid", f"{name} is {value}, outside the screen (0 to {hi})")
    return value


def _area(scr, req):
    """The picture's area: the whole screen, or one window's part of it (clipped to the screen)."""
    wid = req.get("window")
    if wid is None:
        return (0, 0, scr.W, scr.H), None
    found = next((w for w in scr.windows() if w["id"] == wid), None)
    if found is None:
        raise Stop("no_window", f"no window {wid} is open on the screen")
    if found["minimized"]:
        raise Stop("no_window", f"window {wid} is minimized: bring it to the front first (focus)")
    x0, y0 = max(0, found["x"]), max(0, found["y"])
    x1, y1 = min(scr.W, found["x"] + found["w"]), min(scr.H, found["y"] + found["h"])
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise Stop("no_window", f"window {wid} is not on the screen")
    return (x0, y0, x1 - x0, y1 - y0), found


def look(req):
    began = time.monotonic()
    scr = Screen()
    area, _win = _area(scr, req)
    out = {"ok": True, "size": [scr.W, scr.H], "windows": scr.windows(), "pointer": scr.pointer()}
    if req.get("image", True):
        max_side = int(req.get("max_side") or MAX_SIDE)
        png, size, scale, colors = scr.picture(area, max(320, min(max_side, 4096)), int(req.get("png_max") or PNG_MAX))
        out.update(png=base64.b64encode(png).decode(), image_size=list(size), area=list(area),
                   scale=round(1 / scale, 6), colors=colors or None)
    out["took_s"] = round(time.monotonic() - began, 3)
    _say(**out)


def _point(scr, req, need=True):
    x, y = _int(req, "x", 0, scr.W - 1), _int(req, "y", 0, scr.H - 1)
    if (x is None) != (y is None):
        raise Stop("invalid", "give both x and y")
    if x is None and need:
        raise Stop("invalid", "this action needs x and y")
    return (x, y) if x is not None else None


def _do_pointer(scr, action, req):
    at = _point(scr, req, need=action != "scroll")
    if at is not None:
        scr.sleep_ms(draw_ms(200, 600))  # a look at the target before the hand goes
        scr.glide(*at)
    if action == "move":
        return
    if action == "scroll":
        n = _int(req, "amount", 1, NOTCHES_MAX, 3)
        button = WHEEL.get(req.get("direction") or "down")
        if button is None:
            raise Stop("invalid", "direction is up, down, left or right")
        scr.sleep_ms(draw_ms(90, 260))
        for i in range(n):
            scr.press_button(button, 8, 30)
            if i < n - 1:
                scr.sleep_ms(draw_ms(30, 420) if random.random() < 0.15 else draw_ms(30, 140))
        return
    scr.sleep_ms(draw_ms(90, 260))
    if action == "drag":
        to = (_int(req, "to_x", 0, scr.W - 1), _int(req, "to_y", 0, scr.H - 1))
        if None in to:
            raise Stop("invalid", "drag needs to_x and to_y, where it lets go")
        scr.button(1, True)
        try:
            scr.sleep_ms(draw_ms(120, 300))
            scr.glide(*to, slow=1.35)
            scr.sleep_ms(draw_ms(120, 300))
        finally:
            scr.button(1, False)
        return
    n = {"click": 1, "double_click": 1, "right_click": 3}[action]
    scr.press_button(n)
    if action == "double_click":
        scr.sleep_ms(draw_ms(60, 140))
        scr.press_button(1, 40, 110)


def _do_keys(scr, action, req):
    if action == "type":
        text = req.get("text")
        if not isinstance(text, str) or not text:
            raise Stop("invalid", "type needs text")
        if len(text) > TEXT_MAX:
            raise Stop("invalid", f"text is at most {TEXT_MAX} characters")
        scr.sleep_ms(draw_ms(120, 380))
        scr.type_text(text)
        return
    keys = req.get("keys")
    if not isinstance(keys, str) or not keys.strip():
        raise Stop("invalid", 'key needs keys, like "ctrl+s" or "Return"')
    combos = keys.split()
    if len(combos) > COMBOS_MAX:
        raise Stop("invalid", f"keys is at most {COMBOS_MAX} combinations")
    scr.sleep_ms(draw_ms(120, 380))
    for i, words in enumerate(combos):
        scr.combo(words)
        if i < len(combos) - 1:
            scr.sleep_ms(draw_ms(150, 420))


def act(req):
    began = time.monotonic()
    action = req.get("action")
    scr = Screen()
    scr.deadline = began + float(req.get("deadline_s") or 30.0)
    before = {w["id"] for w in scr.windows()}
    STOP["at_once"] = False
    try:
        if action in ("click", "double_click", "right_click", "move", "drag", "scroll"):
            _do_pointer(scr, action, req)
        elif action in ("type", "key"):
            _do_keys(scr, action, req)
        elif action == "focus":
            wid = req.get("window")
            if not isinstance(wid, int) or wid not in before:
                raise Stop("no_window", f"no window {wid} is open on the screen")
            scr.activate(wid)
        else:
            raise Stop("invalid", f"unknown action {action!r}")
    except Stop as e:
        e.typed = scr.typed
        raise
    finally:
        STOP["at_once"] = True
        scr.let_go()
    if req.get("settle", True):
        time.sleep(SETTLE_S)
    after = scr.windows()
    front = next((w for w in after if w["front"]), None)
    _say(ok=True, front=front, opened=[w for w in after if w["id"] not in before],
         closed=sorted(before - {w["id"] for w in after}), pointer=scr.pointer(),
         took_s=round(time.monotonic() - began, 3))


def desktop_entries():
    """The apps a desktop menu lists: (name, id, Exec) of each .desktop entry that is an application and
    not hidden, the first of an id."""
    seen = set()
    for folder in APP_DIRS:
        try:
            names = sorted(n for n in os.listdir(folder) if n.endswith(".desktop"))
        except OSError:
            continue
        for fname in names:
            entry = _read_entry(os.path.join(folder, fname))
            app_id = fname[:-len(".desktop")]
            if entry is None or app_id in seen or os.path.basename((exec_argv(entry["Exec"]) or [""])[0]) in NOT_APPS:
                continue
            seen.add(app_id)
            yield entry["Name"], app_id, entry["Exec"]


def _read_entry(path):
    fields, section = {}, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    section = line
                elif section == "[Desktop Entry]" and "=" in line:
                    key, _, value = line.partition("=")
                    fields.setdefault(key.strip(), value.strip())
    except OSError:
        return None
    if fields.get("Type") != "Application" or not fields.get("Exec") or not fields.get("Name"):
        return None
    if fields.get("NoDisplay", "").lower() == "true" or fields.get("Hidden", "").lower() == "true":
        return None
    return fields


def exec_argv(line, file=None):
    """A .desktop Exec line as argv: its field codes out, `file` where it takes one (else after it)."""
    try:
        words = shlex.split(line)
    except ValueError:
        return None
    out, placed = [], False
    for w in words:
        if w in FIELD_CODES:
            if file and not placed:
                out.append(file)
                placed = True
            continue
        if w in OLD_CODES:
            continue
        out.append(w.replace("%%", "%"))
    if file and not placed:
        out.append(file)
    return out or None


def find_app(app, file=None):
    """(argv, the name it was found by) for an app's name or id, else a program on the PATH, else None."""
    want = app.strip().lower()
    for name, app_id, line in desktop_entries():
        if want in (name.lower(), app_id.lower(), app_id.lower().rsplit(".", 1)[-1]):
            argv = exec_argv(line, file)
            if argv:
                return argv, name
    path = shutil.which(app.strip())
    if path:
        return [path] + ([file] if file else []), os.path.basename(path)
    return None


def apps(req):
    limit = int(req.get("limit") or 80)
    out = []
    for name, app_id, line in desktop_entries():
        argv = exec_argv(line) or [""]
        out.append({"name": name[:80], "id": app_id[:80], "runs": os.path.basename(argv[0])[:80]})
        if len(out) >= limit:
            break
    _say(ok=True, apps=out)


def open_app(req):
    app, file = req.get("app"), req.get("file")
    if not isinstance(app, str) or not app.strip():
        raise Stop("invalid", "open needs app")
    found = find_app(app, file if isinstance(file, str) and file else None)
    if found is None:
        raise Stop("no_window", f"no app or program is named {app[:80]!r}: screen_open() lists the apps, and "
                                "terminal_exec installs one (apt-get install -y <package>)")
    argv, name = found
    scr = Screen()
    before = set(scr.client_ids())
    env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY",)}
    env.update(APP_ENV, HOME=env.get("HOME") or "/root")
    cwd = "/work" if os.path.isdir("/work") else env["HOME"]
    with open(OPEN_LOG, "ab") as log:
        start = log.tell()
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, close_fds=True)
    deadline = time.monotonic() + min(float(req.get("wait_s") or 8.0), 20.0)
    opened = []
    while time.monotonic() < deadline:
        time.sleep(0.1)
        new = [i for i in scr.client_ids() if i not in before]
        if new:
            time.sleep(0.3)  # its title and place settle
            active = scr.active_id()
            opened = [w for w in (scr.describe(i, active) for i in new) if w is not None]
            if opened:
                break
        if proc.poll() not in (None, 0):
            break
    code = proc.poll()
    out = {"ok": True, "app": name, "opened": opened, "running": code is None}
    if not opened and code not in (None, 0):
        out.update(exit_code=code, output=_log_tail(start))
    _say(**out)


def _log_tail(start, n=1000):
    try:
        with open(OPEN_LOG, "rb") as f:
            f.seek(start)
            return f.read()[-n:].decode("utf-8", "replace")
    except OSError:
        return ""


def _stopped(*_):
    STOP["asked"] = True
    if STOP["at_once"]:
        raise Stop("stopped", "the action was stopped")


def main(req):
    signal.signal(signal.SIGTERM, _stopped)
    signal.signal(signal.SIGHUP, _stopped)
    run = {"look": look, "act": act, "apps": apps, "open": open_app}.get(req.get("action_kind"))
    if run is None:
        _say(ok=False, kind="invalid", error=f"unknown request {req.get('action_kind')!r}")
        return
    try:
        run(req)
    except Stop as e:
        _say(ok=False, kind=e.kind, error=str(e), **({"typed": e.typed} if e.typed is not None else {}))
    except (OSError, XError, xerror.DisplayError) as e:
        _say(ok=False, kind="no_screen", error=f"the screen cannot be reached: {str(e)[:300]}")
    except Exception as e:  # noqa: BLE001 - the node reads the error from the JSON line
        _say(ok=False, kind="failed", error=f"{type(e).__name__}: {str(e)[-600:]}")


if __name__ == "__openajar_vm__":  # run by openajar.environments.screen on the machine; never on import
    main(REQ)  # noqa: F821 - REQ is the request the node sent
