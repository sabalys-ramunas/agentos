"""The red team's detectors: what an anti-bot vendor would conclude from one recorded visit.

Each detector reads a session the detector site recorded (openajar.stealth.redteam.server) and returns
a `Verdict`:

    caught      a vendor that runs this check flags the visit (one is enough to lose an account)
    suspicious  a signal that adds to a risk score; several together are a flag
    clean       checked and passed
    skipped     not enough data in this visit to say

Layers: keyboard, mouse, scroll, focus, navigation, http, environment, timing, and (across lanes)
link. Thresholds are set from what real people produce (sources in docs/design/redteam.md). Any
detector that catches a human baseline session is wrong, and the report says so. Pooled detectors
(`judge_pooled`) read many visits together, as a site does over an account's lifetime, and compare
them with human baselines when there are some.

Pure functions over plain dicts. No browser, no I/O.
"""

from __future__ import annotations

import itertools
import math
import statistics
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from openajar.stealth.redteam.stats import (
    ks_2samp,
    ks_critical,
    ks_uniform,
    looks_uniform,
    median,
    percentile,
    rounded,
    skewness,
)

CAUGHT, SUSPICIOUS, CLEAN, SKIPPED = "caught", "suspicious", "clean", "skipped"
RANK = {SKIPPED: -1, CLEAN: 0, SUSPICIOUS: 1, CAUGHT: 2}

MODIFIERS = {"ShiftLeft", "ShiftRight", "ControlLeft", "ControlRight", "AltLeft", "AltRight", "MetaLeft",
             "MetaRight", "CapsLock", "OSLeft", "OSRight"}
SCROLL_KEYS = {"PageDown", "PageUp", "End", "Home", "ArrowDown", "ArrowUp", "Space", "Tab"}
INPUT_EVENTS = ("mousedown", "keydown", "wheel", "touchstart")
SEGMENT_GAP_MS = 120      # a pause longer than this ends one pointer movement
WHEEL_BURST_GAP_MS = 150  # and one wheel gesture


@dataclass
class Verdict:
    id: str
    layer: str
    level: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "layer": self.layer, "level": self.level, "detail": self.detail}
        if self.evidence:
            out["evidence"] = self.evidence
        if self.fix and self.level in (CAUGHT, SUSPICIOUS):
            out["fix"] = self.fix
        return out


# ---- statistics ---------------------------------------------------------------------------------


# ---- the session --------------------------------------------------------------------------------

class Session:
    """One visit as the detector site recorded it."""

    def __init__(self, data: Mapping[str, Any]):
        self.id: str = str(data.get("id"))
        self.human: bool = bool(data.get("human"))
        self.requests: list[dict[str, Any]] = list(data.get("requests") or [])
        self.pages: list[dict[str, Any]] = list(data.get("pages") or [])

    def events(self, *types: str) -> Iterable[tuple[int, dict[str, Any]]]:
        """(page index, event) for every event of these types (all when none), in page order."""
        for i, page in enumerate(self.pages):
            for e in page.get("events") or []:
                if not types or e.get("t") in types:
                    yield i, e

    def env(self) -> dict[str, Any]:
        """The environment the pages reported, the landing page's active tests included."""
        out: dict[str, Any] = {}
        for page in self.pages:
            out.update(page.get("env") or {})
        return out

    def documents(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if _header(r, "Sec-Fetch-Dest") == "document"]


def _header(request: Mapping[str, Any], name: str) -> str | None:
    for k, v in request.get("headers") or []:
        if k.lower() == name.lower():
            return v
    return None


# ---- keyboard -----------------------------------------------------------------------------------

def keystrokes(events: Sequence[Mapping[str, Any]]) -> list[dict[str, float]]:
    """Non-modifier key presses on one page: down time, up time (None if never released)."""
    open_: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for e in events:
        code = e.get("c")
        if code in MODIFIERS or code is None:
            continue
        if e["t"] == "keydown" and not e.get("rep"):
            stroke = {"down": e["ts"], "up": None, "code": code}
            open_[code] = stroke
            out.append(stroke)
        elif e["t"] == "keyup" and code in open_:
            open_.pop(code)["up"] = e["ts"]
    return out


def _all_strokes(s: Session) -> list[list[dict[str, Any]]]:
    return [keystrokes(p.get("events") or []) for p in s.pages]


def kbd_dwell(s: Session) -> Verdict:
    dwell = [k["up"] - k["down"] for strokes in _all_strokes(s) for k in strokes if k["up"] is not None]
    v = Verdict("kbd.dwell", "keyboard", SKIPPED, "too few key presses", {"n": len(dwell)},
                fix="hold each key for a human length of time: `keyboard.press(ch, delay=...)` per character "
                    "(log-normal around 90 ms) instead of `keyboard.type(ch)` (node/human.py type_text)")
    if len(dwell) < 5:
        return v
    med = median(dwell)
    v.evidence.update(median_ms=rounded(med), p10_ms=rounded(percentile(dwell, 0.1)), p90_ms=rounded(percentile(dwell, 0.9)))
    if med < 20:
        v.level, v.detail = CAUGHT, f"keys are released {med:.0f} ms after they go down; people hold them 50 to 150 ms"
    elif med < 40:
        v.level, v.detail = SUSPICIOUS, f"keys are held {med:.0f} ms (median); people hold them 50 to 150 ms"
    else:
        v.level, v.detail = CLEAN, f"keys are held {med:.0f} ms (median)"
    return v


def kbd_keyless_text(s: Session) -> Verdict:
    """Text that arrives with no key behind it: `insertText` with no keydown just before it, outside
    a paste or an input method (composition)."""
    letters: list[str] = []
    symbols: list[str] = []
    typed = 0
    for page in s.pages:
        last_key = -1e9
        for e in page.get("events") or []:
            if e["t"] == "keydown" and e.get("c") not in MODIFIERS:
                last_key = e["ts"]
            elif e["t"] == "input" and e.get("it") == "insertText" and not e.get("comp"):
                typed += 1
                if e["ts"] - last_key > 150:
                    ch = chr(e["ch"]) if e.get("ch") else "?"
                    (letters if ch.isalpha() and ord(ch) <= 0xFFFF else symbols).append(ch)
    v = Verdict("kbd.keyless_text", "keyboard", SKIPPED, "no typing", {"typed": typed},
                fix="send letters outside the US layout as key presses a matching keyboard layout would make "
                    "(a keydown with the letter as `key`), and emoji through a composition, as an emoji picker "
                    "does; Playwright's `type` inserts them with no key at all")
    if typed == 0:
        return v
    v.evidence.update(letters_without_key=len(letters), symbols_without_key=len(symbols),
                      examples=sorted({unicodedata.name(c, "?") for c in (letters + symbols)[:12]})[:6])
    if letters:
        v.level = CAUGHT
        v.detail = (f"{len(letters)} letters ({''.join(sorted(set(letters)))}) appeared in a field with no key "
                    "pressed; a keyboard, even a Lithuanian one, sends a keydown for each")
    elif symbols:
        v.level = SUSPICIOUS
        v.detail = f"{len(symbols)} symbols or emoji were inserted with no key and no input method"
    else:
        v.level, v.detail = CLEAN, "every typed character came from a key press"
    return v


def kbd_shift(s: Session) -> Verdict:
    bad = sum(1 for _, e in s.events("keydown") if e.get("k") == "A" and not e.get("sh") and not e.get("cl"))
    n = sum(1 for _, e in s.events("keydown") if e.get("k") == "A")
    v = Verdict("kbd.shift", "keyboard", SKIPPED, "no capital letters typed", {"capitals": n})
    if n == 0:
        return v
    v.evidence["without_shift"] = bad
    if bad:
        v.level, v.detail = CAUGHT, f"{bad} capital letters were typed with neither Shift nor Caps Lock down"
        v.fix = "press Shift around capitals (human.type_text)"
    else:
        v.level, v.detail = CLEAN, "every capital letter came with Shift or Caps Lock"
    return v


def flights(s: Session) -> list[float]:
    """Keydown to next keydown, within a run of typing (gaps over 1.5 s start a new run)."""
    out: list[float] = []
    for strokes in _all_strokes(s):
        for a, b in itertools.pairwise(strokes):
            gap = b["down"] - a["down"]
            if 0 < gap < 1500:
                out.append(gap)
    return out


def kbd_physical_key(s: Session) -> Verdict:
    """A keydown for a letter with no physical key behind it (`code` empty): what an X keysym remap
    (xdotool type) or a driver's synthetic key sends for a letter its layout lacks."""
    letters = [e for _, e in s.events("keydown") if e.get("k") in ("a", "A", "c")]
    v = Verdict("kbd.physical_key", "keyboard", SKIPPED, "no typing", {"keys": len(letters)},
                fix="type with the layout the text needs (setxkbmap on the lane's display) so every letter has "
                    "its key (ą is Digit1 on a Lithuanian layout)")
    if not letters:
        return v
    bare = [e for e in letters if not e.get("c")]
    v.evidence["without_code"] = len(bare)
    if bare:
        v.level, v.detail = CAUGHT, f"{len(bare)} letters came with a keydown that names no physical key"
    else:
        v.level, v.detail = CLEAN, "every letter came from a physical key"
    return v


def kbd_rollover(s: Session) -> Verdict:
    """People who type fluently press the next key before releasing the last one."""
    strokes = [k for page in _all_strokes(s) for k in page]
    v = Verdict("kbd.rollover", "keyboard", SKIPPED, "too little typing", {"keys": len(strokes)},
                fix="let key presses overlap now and then, as fast typists do")
    if len(strokes) < 40:
        return v
    overlaps = sum(1 for strokes_ in _all_strokes(s) for a, b in itertools.pairwise(strokes_)
                   if a["up"] is not None and b["down"] < a["up"])
    v.evidence["overlaps"] = overlaps
    if overlaps == 0:
        v.level, v.detail = SUSPICIOUS, f"{len(strokes)} keys and not one pressed before the last came up"
    else:
        v.level, v.detail = CLEAN, f"{overlaps} of {len(strokes)} keys overlapped the one before"
    return v


def kbd_corrections(s: Session) -> Verdict:
    typed = sum(1 for _, e in s.events("input") if e.get("it") == "insertText")
    backs = sum(1 for _, e in s.events("keydown") if e.get("k") in ("Backspace", "Delete"))
    v = Verdict("kbd.corrections", "keyboard", SKIPPED, "too little typing", {"typed": typed, "backspaces": backs},
                fix="make an occasional typo and fix it with Backspace")
    if typed < 60:
        return v
    if backs == 0:
        v.level, v.detail = SUSPICIOUS, f"{typed} characters typed without a single correction"
    else:
        v.level, v.detail = CLEAN, f"{backs} corrections in {typed} characters"
    return v


# ---- mouse --------------------------------------------------------------------------------------

def segments(events: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    moves = [e for e in events if e["t"] == "mousemove"]
    out: list[list[Mapping[str, Any]]] = []
    for e in moves:
        if out and e["ts"] - out[-1][-1]["ts"] <= SEGMENT_GAP_MS:
            out[-1].append(e)
        else:
            out.append([e])
    return out


def _path(seg: Sequence[Mapping[str, Any]]) -> float:
    return sum(math.hypot(b["x"] - a["x"], b["y"] - a["y"]) for a, b in itertools.pairwise(seg))


def movement_shape(seg: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Straightness, where the speed peaks (share of the duration) and whether the movement ends with
    a correction (it overshoots, or it slows and speeds up again near the end), as aimed human
    movements do."""
    path = _path(seg)
    chord = math.hypot(seg[-1]["x"] - seg[0]["x"], seg[-1]["y"] - seg[0]["y"])
    speeds = []
    for a, b in itertools.pairwise(seg):
        dt = max(b["ts"] - a["ts"], 1.0)
        speeds.append(math.hypot(b["x"] - a["x"], b["y"] - a["y"]) / dt)
    smooth = [statistics.fmean(speeds[max(0, i - 1):i + 2]) for i in range(len(speeds))]
    peak = max(range(len(smooth)), key=smooth.__getitem__) if smooth else 0
    duration = max(seg[-1]["ts"] - seg[0]["ts"], 1.0)
    peak_at = (seg[peak + 1]["ts"] - seg[0]["ts"]) / duration if smooth else None
    end = seg[-1]
    dist = [math.hypot(end["x"] - e["x"], end["y"] - e["y"]) for e in seg]
    overshoot = any(dist[i + 1] > dist[i] + 2 for i in range(int(len(dist) * 0.5), len(dist) - 1))
    resurge = False
    tail = smooth[peak:]
    low = math.inf
    for sp in tail:
        low = min(low, sp)
        if low < math.inf and sp > low * 1.5 and sp - low > 0.08:
            resurge = True
            break
    return {"straightness": chord / path if path else 1.0, "peak_at": peak_at, "correction": overshoot or resurge,
            "length": path}


def _aimed(s: Session) -> list[dict[str, Any]]:
    out = []
    for page in s.pages:
        for seg in segments(page.get("events") or []):
            if len(seg) >= 8 and _path(seg) > 150:
                out.append(movement_shape(seg))
    return out


def mouse_teleport(s: Session) -> Verdict:
    """A press where the pointer never went: no mousemove ending at the press point."""
    far: list[float] = []
    unseen = presses = 0
    for page in s.pages:
        last = None
        for e in page.get("events") or []:
            if e["t"] == "mousemove":
                last = e
            elif e["t"] == "mousedown":
                presses += 1
                if last is None:
                    unseen += 1
                else:
                    d = math.hypot(e["x"] - last["x"], e["y"] - last["y"])
                    if d > 3:
                        far.append(round(d))
    v = Verdict("mouse.teleport", "mouse", SKIPPED, "no clicks", {"presses": presses},
                fix="move the pointer onto the target before every press (human._glide_onto)")
    if presses == 0:
        return v
    v.evidence.update(jumped=len(far), no_move_on_page=unseen, jump_px=far[:5])
    if far:
        v.level, v.detail = CAUGHT, f"{len(far)} of {presses} presses landed where the pointer had not moved to"
    elif unseen:
        v.level, v.detail = SUSPICIOUS, f"{unseen} of {presses} presses came with no pointer movement on that page"
    else:
        v.level, v.detail = CLEAN, "every press followed the pointer to its spot"
    return v


def mouse_path(s: Session) -> Verdict:
    shapes = _aimed(s)
    v = Verdict("mouse.path", "mouse", SKIPPED, "too few pointer movements", {"movements": len(shapes)},
                fix="curve the paths more and end some with a small overshoot or correction (Camoufox humanize, "
                    "or our own path generator in node/human.py)")
    if len(shapes) < 4:
        return v
    straight = median([x["straightness"] for x in shapes])
    corrected = sum(1 for x in shapes if x["correction"])
    peak = median([x["peak_at"] for x in shapes if x["peak_at"] is not None])
    v.evidence.update(straightness_median=rounded(straight, 3), corrections=corrected, peak_at_median=rounded(peak, 2))
    problems = []
    if straight > 0.985:
        problems.append(f"paths are near-straight lines (median straightness {straight:.3f}; people about 0.85 to 0.97)")
    if corrected == 0 and len(shapes) >= 6:
        problems.append(f"none of {len(shapes)} movements ends with an overshoot or a correction")
    if problems:
        v.level, v.detail = SUSPICIOUS, "; ".join(problems)
    else:
        v.level, v.detail = CLEAN, (f"{len(shapes)} movements, straightness {straight:.3f}, "
                                    f"{corrected} end with a correction")
    return v


def mouse_merged(s: Session) -> Verdict:
    """A mouse reports at 125 to 1000 Hz and the screen refreshes at 60 to 144, so Firefox merges
    moves (getCoalescedEvents length over 1) and fires pointerrawupdate more often than pointermove.
    Juggler sends every move as a synthesized event, which Firefox never merges
    (CoalescedMouseData::CanCoalesce)."""
    moves = merged = raw = 0
    for page in s.pages:
        p = (page.get("env") or {}).get("pointer") or {}
        moves += int(p.get("moves") or 0)
        merged += int(p.get("merged") or 0)
        raw += int(p.get("raw") or 0)
    v = Verdict("mouse.merged", "mouse", SKIPPED, "too few pointer moves", {"moves": moves},
                fix="drive the pointer through the X server (XTest on the lane's display) instead of Juggler's "
                    "synthesized events, at a real mouse's report rate")
    if moves < 100:
        return v
    v.evidence.update(merged=merged, rawupdates=raw)
    if merged == 0 and raw == moves:
        v.level, v.detail = SUSPICIOUS, (f"{moves} pointer moves and not one merged by the browser, one raw update "
                                         "each: input that never came from a mouse")
    else:
        v.level, v.detail = CLEAN, f"{merged} of {moves} pointer moves merged, {raw} raw updates"
    return v


def mouse_entry(s: Session) -> Verdict:
    """Where the pointer first shows up. A real pointer comes in from wherever the hand left it; a
    driver's starts at the window's corner (0, 0)."""
    first = next((e for _, e in s.events("mousemove")), None)
    v = Verdict("mouse.entry", "mouse", SKIPPED, "the pointer never moved",
                fix="start each lane's pointer somewhere plausible (inside the viewport, near the last "
                    "page's last position) instead of the corner")
    if first is None:
        return v
    x0, y0 = first["x"] - (first.get("mx") or 0), first["y"] - (first.get("my") or 0)
    v.evidence.update(first=[first["x"], first["y"]], came_from=[x0, y0])
    if y0 <= 1 and x0 <= 50:  # from the URL bar a hand comes in along the top edge, not at the corner
        v.level, v.detail = SUSPICIOUS, f"the pointer's first move starts at the window's top-left corner ({x0}, {y0})"
    else:
        v.level, v.detail = CLEAN, f"the pointer first shows up at ({first['x']}, {first['y']})"
    return v


def press_durations(s: Session) -> list[float]:
    out: list[float] = []
    for page in s.pages:
        down = None
        for e in page.get("events") or []:
            if e["t"] == "mousedown":
                down = e["ts"]
            elif e["t"] == "mouseup" and down is not None:
                out.append(e["ts"] - down)
                down = None
    return out


def mouse_press(s: Session) -> Verdict:
    d = press_durations(s)
    v = Verdict("mouse.press", "mouse", SKIPPED, "no clicks", {"n": len(d)},
                fix="hold the button for 60 to 150 ms (human._press_release)")
    if not d:
        return v
    v.evidence.update(median_ms=rounded(median(d)), min_ms=rounded(min(d)))
    if median(d) < 25:
        v.level, v.detail = CAUGHT, f"the button comes up {median(d):.0f} ms after it goes down"
    else:
        v.level, v.detail = CLEAN, f"presses last {median(d):.0f} ms (median)"
    return v


# ---- scroll -------------------------------------------------------------------------------------

def wheel_pointer(s: Session) -> Verdict:
    """A wheel event reports where the pointer is. If it is not where the last mousemove left it, the
    wheel was synthesised somewhere else."""
    far = n = 0
    for page in s.pages:
        last = None
        for e in page.get("events") or []:
            if e["t"] == "mousemove":
                last = e
            elif e["t"] == "wheel":
                n += 1
                if last is not None and math.hypot(e["x"] - last["x"], e["y"] - last["y"]) > 5:
                    far += 1
    v = Verdict("scroll.wheel_pointer", "scroll", SKIPPED, "no wheel scrolling", {"wheel_events": n},
                fix="move the pointer over the area before turning the wheel (human._rest_pointer)")
    if n == 0:
        return v
    v.evidence["away_from_pointer"] = far
    if far:
        v.level, v.detail = CAUGHT, f"{far} of {n} wheel events came from a spot the pointer never moved to"
    else:
        v.level, v.detail = CLEAN, "every wheel event came from where the pointer was"
    return v


def scroll_unexplained(s: Session) -> Verdict:
    """The page scrolled with no wheel, key, drag or touch to explain it: a script scrolled it
    (Playwright's scrollIntoView before a click does exactly this)."""
    unexplained = 0.0
    scrolled = 0.0
    for page in s.pages:
        last_input = -1e9
        last_y = None
        for e in page.get("events") or []:
            t = e["t"]
            if t in ("wheel", "touchstart", "mousedown") or (t == "keydown" and e.get("k") in SCROLL_KEYS | {" "}):
                last_input = e["ts"]
            elif t == "keydown" and e.get("c") not in MODIFIERS:
                last_input = e["ts"]  # typing can scroll the caret into view
            elif t == "scroll" and e.get("tg") == "doc" and e.get("sy2") is not None:
                y = e["sy2"]
                if last_y is not None:
                    delta = abs(y - last_y)
                    scrolled += delta
                    if e["ts"] - last_input > 1200 and e["ts"] > 1500:
                        unexplained += delta
                last_y = y
    v = Verdict("scroll.unexplained", "scroll", SKIPPED, "the page never scrolled", {"scrolled_px": round(scrolled)},
                fix="scroll only with the wheel or keys (human.scroll_into_view); never let Playwright "
                    "scroll an element into view for a click or a focus")
    if scrolled == 0:
        return v
    v.evidence["unexplained_px"] = round(unexplained)
    if unexplained > 300:
        v.level, v.detail = CAUGHT, f"{unexplained:.0f} px of scrolling had no wheel, key or touch behind it"
    elif unexplained > 0:
        v.level, v.detail = SUSPICIOUS, f"{unexplained:.0f} px of scrolling had no input behind it"
    else:
        v.level, v.detail = CLEAN, f"all {scrolled:.0f} px of scrolling followed input"
    return v


def wheel_bursts(s: Session) -> list[list[Mapping[str, Any]]]:
    out: list[list[Mapping[str, Any]]] = []
    for page in s.pages:
        cur: list[Mapping[str, Any]] = []
        for e in page.get("events") or []:
            if e["t"] != "wheel":
                continue
            if cur and e["ts"] - cur[-1]["ts"] > WHEEL_BURST_GAP_MS:
                out.append(cur)
                cur = []
            cur.append(e)
        if cur:
            out.append(cur)
    return out


def wheel_intervals(s: Session) -> list[float]:
    return [b["ts"] - a["ts"] for burst in wheel_bursts(s) for a, b in itertools.pairwise(burst)]


# ---- focus --------------------------------------------------------------------------------------

def env_focus(s: Session) -> Verdict:
    unfocused = hidden = n = 0
    for _, e in s.events(*INPUT_EVENTS):
        n += 1
        unfocused += not e.get("f", True)
        hidden += not e.get("v", True)
    v = Verdict("focus.input", "focus", SKIPPED, "no input", {"input_events": n},
                fix="give the lane's window focus (a window manager on the display, or XSetInputFocus after "
                    "placing the window)")
    if n == 0:
        return v
    v.evidence.update(while_unfocused=unfocused, while_hidden=hidden)
    if unfocused or hidden:
        v.level = CAUGHT
        v.detail = f"{unfocused} inputs arrived while the window had no focus, {hidden} while the page was hidden"
    else:
        v.level, v.detail = CLEAN, "all input arrived in a focused, visible window"
    return v


# ---- navigation and http --------------------------------------------------------------------------

def nav_typed(s: Session) -> Verdict:
    """Inner pages reached by typing their address mid-visit: no Referer, Sec-Fetch-Site none."""
    docs = [r for r in s.documents() if r["method"] == "GET"]
    typed = [r for r in docs[1:] if _header(r, "Sec-Fetch-Site") == "none"]
    v = Verdict("nav.typed_urls", "navigation", SKIPPED, "one page", {"pages": len(docs)},
                fix="reach pages by clicking the link to them when the current page has one; jump by URL "
                    "only to start a visit")
    if len(docs) < 2:
        return v
    v.evidence.update(typed=len(typed), paths=[r["path"] + ("?" + r["query"] if r.get("query") else "")
                                               for r in typed][:5])
    if typed:
        v.level = SUSPICIOUS
        v.detail = (f"{len(typed)} of {len(docs)} pages were opened by typing an inner address mid-visit "
                    "(no Referer), which people rarely do and agents do all the time")
    else:
        v.level, v.detail = CLEAN, "after the first page, every page came from a link on the site"
    return v


FIREFOX_ORDER = [("Host", "User-Agent", "Accept", "Accept-Language", "Accept-Encoding"),
                 ("Upgrade-Insecure-Requests", "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site",
                  "Sec-Fetch-User", "Priority")]
FOREIGN_HEADERS = ("via", "x-forwarded-for", "forwarded", "x-real-ip", "proxy-connection", "x-requested-with",
                   "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "x-proxy-id", "client-ip")


def http_headers(s: Session) -> Verdict:
    bad_order, foreign = [], set()
    for r in s.requests:
        names = [k for k, _ in r.get("headers") or []]
        for chain in FIREFOX_ORDER:
            seen = [n for n in names if n in chain]
            if seen != [n for n in chain if n in seen]:
                bad_order.append(r["path"])
                break
        foreign |= {n.lower() for n in names if n.lower() in FOREIGN_HEADERS}
    v = Verdict("http.headers", "http", SKIPPED, "no requests", {"requests": len(s.requests)},
                fix="a proxy that adds headers must not (use a CONNECT proxy); Firefox's own order must reach the site")
    if not s.requests:
        return v
    v.evidence.update(out_of_order=bad_order[:5], foreign=sorted(foreign))
    if foreign:
        v.level, v.detail = CAUGHT, f"headers no Firefox sends reached the site: {', '.join(sorted(foreign))}"
    elif bad_order:
        v.level, v.detail = CAUGHT, f"{len(bad_order)} requests had headers out of Firefox's order"
    else:
        v.level, v.detail = CLEAN, f"{len(s.requests)} requests in Firefox's header order"
    return v


def http_consistency(s: Session) -> Verdict:
    env = s.env()
    docs = s.documents()
    v = Verdict("http.consistency", "http", SKIPPED, "no page reported its environment",
                fix="the pinned user agent and languages must be what the network layer sends")
    if not docs or not env.get("userAgent"):
        return v
    problems = []
    uas = {_header(r, "User-Agent") for r in docs}
    if uas != {env["userAgent"]}:
        problems.append("the User-Agent header differs from navigator.userAgent")
    lang = (_header(docs[0], "Accept-Language") or "").split(",")[0].split(";")[0].strip()
    if env.get("language") and lang and lang.lower() != str(env["language"]).lower():
        problems.append(f"Accept-Language starts with {lang}, navigator.language is {env['language']}")
    v.evidence.update(user_agents=len(uas), accept_language=lang, language=env.get("language"))
    if problems:
        v.level, v.detail = CAUGHT, "; ".join(problems)
    else:
        v.level, v.detail = CLEAN, "headers agree with what the page reads"
    return v


# ---- environment (active tests) -------------------------------------------------------------------

SOFTWARE_GL = ("llvmpipe", "softpipe", "swiftshader", "software", "lavapipe", "mesa offscreen")


def env_cores(s: Session) -> Verdict:
    cpu = s.env().get("cpu") or {}
    v = Verdict("env.cores", "environment", SKIPPED, "the parallelism test did not run", dict(cpu),
                fix="pin hardwareConcurrency to what the node can run in parallel (the host's cores), or "
                    "give the lane the cores its fingerprint claims")
    if not cpu.get("effective") or not cpu.get("claimed"):
        return v
    claimed, eff = cpu["claimed"], cpu["effective"]
    if claimed >= 4 and eff < max(1.6, 0.4 * claimed):
        v.level = CAUGHT
        v.detail = f"claims {claimed} cores, but {claimed} workers ran like {eff:.1f} in parallel"
    elif claimed >= 4 and eff < 0.6 * claimed:
        v.level = SUSPICIOUS
        v.detail = f"claims {claimed} cores, {claimed} workers ran like {eff:.1f} (a busy machine can do this)"
    else:
        v.level, v.detail = CLEAN, f"{claimed} cores claimed, {eff:.1f} measured in parallel"
    return v


def env_gpu(s: Session) -> Verdict:
    gpu = s.env().get("gpu") or {}
    v = Verdict("env.gpu", "environment", SKIPPED, "the GPU test did not run", dict(gpu),
                fix="a node with a GPU, or identities whose GPU name says software rendering (honest but rare)")
    if not gpu.get("available") or gpu.get("msPerDraw") is None:
        return v
    renderer = str(gpu.get("renderer") or "")
    ms = float(gpu["msPerDraw"])
    software = any(w in renderer.lower() for w in SOFTWARE_GL)
    if software:
        v.level, v.detail = SUSPICIOUS, f"the GPU says software rendering ({renderer}): a VM, rare among real users"
    elif ms > 12:
        v.level = CAUGHT
        v.detail = f"claims {renderer!r} but draws at software speed ({ms:.1f} ms a frame; a real GPU under 5)"
    elif ms > 6:
        v.level = SUSPICIOUS
        v.detail = f"claims {renderer!r} and draws slowly ({ms:.1f} ms a frame)"
    else:
        v.level, v.detail = CLEAN, f"{renderer} draws at {ms:.1f} ms a frame"
    return v


COMMON_HZ = ((55, 65), (70, 78), (82, 92), (95, 105), (115, 125), (140, 170), (230, 250))


def frame_rate(frames: Sequence[float]) -> tuple[float, float] | None:
    """(frames per second, share of frames late by more than half a frame) from rAF timestamps."""
    gaps = [b - a for a, b in itertools.pairwise(frames) if b > a]
    if len(gaps) < 30:
        return None
    period = median(gaps)
    late = sum(1 for g in gaps if g > period * 1.5) / len(gaps)
    return 1000 / period, late


def env_frames(s: Session) -> Verdict:
    rates = [r for p in s.pages if (r := frame_rate(p.get("frames") or []))]
    v = Verdict("env.frames", "environment", SKIPPED, "too few animation frames",
                fix="a display with a real refresh rate (Xvfb paints on a timer)")
    if not rates:
        return v
    hz = median([r[0] for r in rates])
    late = median([r[1] for r in rates])
    v.evidence.update(hz=rounded(hz), late_share=rounded(late, 3))
    if not any(lo <= hz <= hi for lo, hi in COMMON_HZ):
        v.level, v.detail = SUSPICIOUS, f"the screen refreshes at {hz:.1f} Hz, which no common monitor does"
    elif late > 0.2:
        v.level, v.detail = SUSPICIOUS, f"{late:.0%} of frames came late: an overloaded machine"
    else:
        v.level, v.detail = CLEAN, f"{hz:.0f} Hz, {late:.0%} late frames"
    return v


def env_timer(s: Session) -> Verdict:
    t = s.env().get("timerMs")
    v = Verdict("env.timer", "environment", SKIPPED, "the timer test did not run")
    if t is None:
        return v
    v.evidence["resolution_ms"] = t
    if t > 1.5:
        v.level, v.detail = SUSPICIOUS, (f"performance.now() ticks every {t} ms: resistFingerprinting or a "
                                         "privacy browser, a small minority")
    else:
        v.level, v.detail = CLEAN, f"performance.now() resolution {t} ms, as stock Firefox"
    return v


# ---- timing -------------------------------------------------------------------------------------

def arrival_to_press(s: Session) -> list[float]:
    """From the moment the pointer stops on a target to the press: people take a skewed, variable
    100 to 400 ms; a fixed random range shows as a flat distribution."""
    out: list[float] = []
    for page in s.pages:
        last_move = None
        for e in page.get("events") or []:
            if e["t"] == "mousemove":
                last_move = e["ts"]
            elif e["t"] == "mousedown" and last_move is not None:
                out.append(e["ts"] - last_move)
    return out


def judge(session: Mapping[str, Any] | Session) -> list[Verdict]:
    s = session if isinstance(session, Session) else Session(session)
    return [d(s) for d in DETECTORS]


DETECTORS: list[Callable[[Session], Verdict]] = [
    kbd_dwell, kbd_keyless_text, kbd_physical_key, kbd_shift, kbd_rollover, kbd_corrections,
    mouse_teleport, mouse_press, mouse_path, mouse_merged, mouse_entry,
    wheel_pointer, scroll_unexplained,
    env_focus, nav_typed, http_headers, http_consistency,
    env_cores, env_gpu, env_frames, env_timer,
]


# ---- pooled: many visits, as a site sees an account over time -------------------------------------

def _mouse_speeds(s: Session) -> list[float]:
    out = []
    for page in s.pages:
        for seg in segments(page.get("events") or []):
            if len(seg) >= 8 and _path(seg) > 150:
                out.append(_path(seg) / max(seg[-1]["ts"] - seg[0]["ts"], 1.0))
    return out


def _raf_phases(s: Session) -> list[float]:
    return [float(e["raf"]) for _, e in s.events("mousemove") if e.get("raf") is not None]


def phase_concentration(phases: Sequence[float], period: float = 1000 / 60) -> float | None:
    """How tightly mousemove events sit at one point of the animation frame (0 spread evenly, 1 all at
    one phase). Stock Firefox delivers most pointer moves on the refresh tick (Camoufox #977)."""
    if len(phases) < 30:
        return None
    c = sum(math.cos(2 * math.pi * p / period) for p in phases) / len(phases)
    s = sum(math.sin(2 * math.pi * p / period) for p in phases) / len(phases)
    return math.hypot(c, s)


MEASURES: dict[str, tuple[str, Callable[[Session], list[float]], str]] = {
    "kbd.flight": ("keyboard", flights, "the time from one key to the next"),
    "kbd.hold": ("keyboard", lambda s: [k["up"] - k["down"] for st in _all_strokes(s) for k in st
                                        if k["up"] is not None], "how long keys are held"),
    "mouse.arrival_to_press": ("timing", arrival_to_press, "the pause between the pointer stopping and the press"),
    "mouse.press_hold": ("mouse", press_durations, "how long the button is held"),
    "mouse.speed": ("mouse", _mouse_speeds, "pointer speed over aimed movements"),
    "scroll.wheel_gap": ("scroll", wheel_intervals, "the gap between wheel notches"),
}


UNIFORM_CHECKED = ("kbd.flight", "mouse.arrival_to_press", "scroll.wheel_gap")


def judge_pooled(agent: Sequence[Mapping[str, Any]], humans: Sequence[Mapping[str, Any]] = ()) -> list[Verdict]:
    """Distribution checks over every agent visit together. With human baselines, each measure is
    compared with the people's (two-sample KS); without, timing measures are checked for the flat
    shape of a `random.uniform` range."""
    a_s = [x if isinstance(x, Session) else Session(x) for x in agent]
    h_s = [x if isinstance(x, Session) else Session(x) for x in humans]
    out: list[Verdict] = []
    for mid, (layer, fn, what) in MEASURES.items():
        a = [v for s in a_s for v in fn(s)]
        h = [v for s in h_s for v in fn(s)]
        v = Verdict(f"pooled.{mid}", layer, SKIPPED, f"too few samples of {what}",
                    {"agent_n": len(a), "agent_median": rounded(median(a))},
                    fix=f"draw {what} from a skewed, human-shaped distribution (log-normal) fitted to the baselines")
        if h:
            v.evidence.update(human_n=len(h), human_median=rounded(median(h)))
            d = ks_2samp(a, h)
            if d is None:
                out.append(v)
                continue
            crit = ks_critical(len(a), len(h))
            v.evidence.update(ks=rounded(d, 3), ks_critical=rounded(crit, 3))
            if d > max(crit, 0.5):
                v.level, v.detail = CAUGHT, f"{what} is plainly not the people's (KS {d:.2f})"
            elif d > crit:
                v.level, v.detail = SUSPICIOUS, f"{what} differs from the people's (KS {d:.2f}, p < 0.001)"
            else:
                v.level, v.detail = CLEAN, f"{what} matches the people's (KS {d:.2f})"
        elif len(a) >= 60 and mid in UNIFORM_CHECKED:
            ks = ks_uniform(a)
            sk = skewness(a)
            v.evidence.update(ks_uniform=rounded(ks, 3), skew=rounded(sk, 2))
            if looks_uniform(a):
                v.level = SUSPICIOUS
                v.detail = (f"{what} is flat between {min(a):.0f} and {max(a):.0f} ms, the mark of a uniform random "
                            f"range (KS {ks:.3f}); people's timings are skewed")
            else:
                v.level, v.detail = CLEAN, f"{what} is not flat (KS to uniform {ks:.3f}, skew {sk:.2f})"
        elif mid not in UNIFORM_CHECKED:
            v.detail = f"{what}: needs a human baseline (--humans) to judge"
        out.append(v)
    phases = [p for s in a_s for p in _raf_phases(s)]
    r = phase_concentration(phases)
    v = Verdict("pooled.mouse.frame_phase", "mouse", SKIPPED, "too few pointer moves", {"moves": len(phases)},
                fix="deliver pointer moves on the refresh tick, as stock Firefox does (Camoufox PR #978)")
    if r is not None:
        v.evidence["concentration"] = rounded(r, 3)
        hr = phase_concentration([p for s in h_s for p in _raf_phases(s)])
        if hr is not None:
            v.evidence["human_concentration"] = rounded(hr, 3)
            if r < hr - 0.3:
                v.level, v.detail = SUSPICIOUS, (f"pointer moves are spread over the frame ({r:.2f}); the people's "
                                                 f"sit on the refresh tick ({hr:.2f})")
            else:
                v.level, v.detail = CLEAN, f"pointer moves sit on the frame like the people's ({r:.2f} vs {hr:.2f})"
        else:
            v.level, v.detail = CLEAN, f"frame phase concentration {r:.2f} (needs a human baseline to judge)"
    out.append(v)
    return out


# ---- the engine probe (site/deep.html): what the machine and the browser's settings show ----------


# ---- across lanes -------------------------------------------------------------------------------

# Values so common that sharing them says nothing (StatCounter and Firefox telemetry, 2026).
COMMON = {"screen": {(1920, 1080), (1366, 768), (1536, 864), (2560, 1440), (1440, 900), (1280, 720)},
          "cores": {4, 8, 12, 16}, "dpr": {1, 1.25, 1.5, 2}}
LINK_TRAITS = ("userAgent", "screen", "cores", "timeZone", "languages", "gpu.renderer", "dpr")


def link_lanes(envs: Mapping[str, Mapping[str, Any]]) -> list[Verdict]:
    """Can a site that sees several lanes tell they are one operator? Compares what each lane's pages
    reported, pairwise. Sharing a common value (a 1920x1080 screen) says nothing; sharing rare ones
    (a GPU name, an odd screen) together, or running JavaScript at the same speed, does. Timezone and
    languages follow the exit IP, so lanes behind one proxy share them by design."""
    lanes = sorted(envs)
    out: list[Verdict] = []

    def get(env: Mapping[str, Any], key: str) -> Any:
        if key == "gpu.renderer":
            return (env.get("gpu") or {}).get("renderer")
        value = env.get(key)
        return tuple(value[:2]) if key == "screen" and isinstance(value, list) else value

    for i, a in enumerate(lanes):
        for b in lanes[i + 1:]:
            same = [k for k in LINK_TRAITS if get(envs[a], k) is not None and get(envs[a], k) == get(envs[b], k)]
            rare = [k for k in same if k in ("gpu.renderer", "screen", "cores", "dpr")
                    and get(envs[a], k) not in COMMON.get(k, set())]
            cpu_a, cpu_b = (envs[a].get("cpu") or {}).get("oneMs"), (envs[b].get("cpu") or {}).get("oneMs")
            same_speed = bool(cpu_a and cpu_b and abs(cpu_a - cpu_b) / max(cpu_a, cpu_b) < 0.1)
            v = Verdict(f"link.{a}~{b}", "link", CLEAN, "", {"same": same, "rare_shared": rare,
                                                              "same_cpu_speed": same_speed},
                        fix="give lanes identities that differ in their rare traits (GPU, screen), and put lanes "
                            "that must never be linked on different nodes (docs/design/stealth.md)")
            if len(same) == len(LINK_TRAITS) or (len(rare) >= 2 and same_speed):
                v.level, v.detail = CAUGHT, f"{a} and {b} look like one device ({', '.join(same)})"
            elif rare:
                v.level = SUSPICIOUS
                v.detail = f"{a} and {b} share rare traits ({', '.join(rare)}) besides {len(same) - len(rare)} common ones"
            else:
                v.detail = f"{a} and {b} share only common values ({', '.join(same) or 'none'})"
            out.append(v)
    return out


# ---- the verdict --------------------------------------------------------------------------------

def summary(verdicts: Iterable[Verdict]) -> dict[str, Any]:
    vs = list(verdicts)
    caught = [v.id for v in vs if v.level == CAUGHT]
    suspicious = [v.id for v in vs if v.level == SUSPICIOUS]
    if caught:
        outcome = "detected"
    elif len(suspicious) >= 3:
        outcome = "flagged"
    else:
        outcome = "passed"
    return {"outcome": outcome, "caught": caught, "suspicious": suspicious,
            "clean": sum(1 for v in vs if v.level == CLEAN), "skipped": sum(1 for v in vs if v.level == SKIPPED)}
