"""Stealth and isolation findings: is each lane one consistent, ordinary device, and can lanes be
told apart and kept apart? (docs/design/stealth.md)

Two sources, one kind of answer (a list of `Finding`s, each ok, warn or fail):

    pins    the pinned fingerprints on disk (`openajar lane audit`): each pin's own consistency
            (user agent, platform, oscpu, fonts, GPU, locale, screen, the Firefox version against
            the installed one, Camoufox's own coherence rules when it is installed), and lanes
            that pin the same device. Read only, never starts a browser.
    dumps   what a page's own script read in a live lane (stealth_probe.html, served by the
            stealth check): automation traces, a worker or a frame that disagrees with the window,
            CSS that disagrees with the screen, the real window position, raw WebRTC addresses,
            fonts and GPU of another OS; a lane that looks different on a relaunch (drift); and
            values two lanes share (collisions).

Nothing here imports camoufox or playwright at module import time.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openajar.node.fingerprint import platform_os, ua_os

OK, WARN, FAIL = "ok", "warn", "fail"
RANK = {OK: 0, WARN: 1, FAIL: 2}

PIN_META = "agentos:pin"         # openajar.node.camoufox.engine.PIN_META
PIN_VERSION = 2
PROBE_PAGE = Path(__file__).with_name("stealth_probe.html")

# Keys Camoufox would draw afresh on each launch when a pin lacks them (engine_camoufox.DRAWN_KEYS).
DRAWN_KEYS = (
    "navigator.userAgent", "navigator.appVersion", "navigator.oscpu", "navigator.platform",
    "navigator.hardwareConcurrency", "navigator.maxTouchPoints",
    "screen.availLeft", "screen.availTop", "screen.availWidth", "screen.availHeight",
    "screen.width", "screen.height", "screen.colorDepth", "screen.pixelDepth",
    "window.outerWidth", "window.outerHeight", "window.screenX", "window.screenY",
)

SOFTWARE_GL = ("llvmpipe", "softpipe", "swiftshader", "software", "mesa offscreen", "lavapipe")

# Fonts only one family of systems ships, as the probe page looks for them. A Windows identity that
# shows a Linux font is a Linux host showing through (and the other way round). Left out: fonts a
# Linux desktop answers through a metric-compatible alias (Calibri and Carlito, Cambria and Caladea),
# and the DejaVu and Liberation families, which LibreOffice installs on Windows and macOS (Camoufox
# draws them for Windows identities, from real machines).
OS_ONLY_FONTS = {
    "linux": ("Cantarell", "FreeSans", "Ubuntu", "Ubuntu Mono"),
    "windows": ("Segoe UI", "Segoe UI Emoji", "Segoe UI Symbol", "Consolas", "Candara",
                "Corbel", "Constantia", "Ebrima", "Gadugi", "Leelawadee UI", "Microsoft YaHei", "Nirmala UI",
                "MS Gothic", "Sitka Text", "Bahnschrift", "Ink Free"),
    "macos": ("Apple Chancery", "Avenir", "Avenir Next", "Big Caslon", "Chalkboard", "Cochin", "Didot",
              "Geneva", "Hoefler Text", "Lucida Grande", "Marker Felt", "Menlo", "Monaco", "PingFang SC",
              "Skia", "Snell Roundhand", "Zapfino", "Helvetica Neue"),
}
# A font every stock install of the OS has; an identity that shows none of them is not that OS.
OS_CORE_FONTS = {"windows": ("Segoe UI", "Arial", "Calibri"), "macos": ("Helvetica", "Helvetica Neue", "Menlo"),
                 "linux": ()}

# What a page reads in a dump, per surface, for collisions between lanes. "device" surfaces make two
# lanes the same machine to a site (canvas readback is new noise each session, so two lanes that share
# it share a session's key); "shared" ones every lane on a node shares with every user of the same
# build, OS and language on the same kind of host (the voices of Windows in English, the text metrics
# and audio of a Linux host, media devices, whose ids a page sees only after a camera or microphone
# grant): they link lanes only together with the rest.
DEVICE_SURFACES = ("device", "fonts", "webgl", "canvas")
HOST_SURFACES = ("voices", "mediaDevices", "textWidth", "audio", "math", "webglReadback", "codecs")


@dataclass
class Finding:
    check: str            # automation, consistency, leak, drift, collision, freshness, pin, resources, soak
    level: str
    detail: str
    lane: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"check": self.check, "level": self.level, "detail": self.detail}
        if self.lane:
            out["lane"] = self.lane
        if self.data:
            out["data"] = self.data
        return out


# How a group of checks adds a finding: add(check, level, detail, **data).
_Add = Callable[..., None]


def worst(findings: Iterable[Finding]) -> str:
    return max((f.level for f in findings), key=RANK.__getitem__, default=OK)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---- the OS an identity claims ------------------------------------------------------------------


def oscpu_os(oscpu: Any) -> str | None:
    o = str(oscpu or "")
    return "windows" if "Windows" in o else "macos" if "Mac OS X" in o else "linux" if "Linux" in o else None


def renderer_problem(renderer: Any, claimed: str | None) -> str | None:
    """Why a WebGL renderer does not fit the claimed OS, or None."""
    r = str(renderer or "").lower()
    if not r:
        return None
    if any(s in r for s in SOFTWARE_GL):
        return f"a software renderer ({renderer}), what a machine without a working GPU driver shows"
    if claimed == "windows" and "angle" not in r and "direct3d" not in r:
        return f"{renderer} without ANGLE, which every Windows Firefox reports"
    if claimed == "macos" and ("direct3d" in r or "angle" in r and "metal" not in r and "apple" not in r):
        return f"{renderer} is a Windows renderer string"
    if claimed == "linux" and ("direct3d" in r or "angle (" in r):
        return f"{renderer} is a Windows renderer string"
    return None


# ---- pins ---------------------------------------------------------------------------------------

def split_pin(data: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = data.get(PIN_META)
    return ({k: v for k, v in data.items() if k != PIN_META}, dict(meta) if isinstance(meta, dict) else {})


def ua_major(user_agent: Any) -> int | None:
    from openajar.node.fingerprint import ua_major as major
    return major(user_agent)


def coherence_violations(config: Mapping[str, Any], claimed: str | None) -> list[str] | None:
    """Camoufox's own whole-identity rules (camoufox.coherence) on a pinned config, or None when
    Camoufox is not installed here."""
    try:
        from camoufox import coherence
    except Exception:  # noqa: BLE001 - not a node, or another Camoufox
        return None
    key = {"windows": "win", "macos": "mac", "linux": "lin"}.get(claimed or "")
    if key is None:
        return []
    try:
        return [f"{v.rule}: {v.detail}" for v in coherence.validate(dict(config), key)]
    except Exception as e:  # noqa: BLE001 - a rule that cannot read this pin is not a finding
        return [f"could not run Camoufox's coherence rules: {type(e).__name__}"]


def pin_findings(lane: str, data: Mapping[str, Any], *, installed_major: int | None = None,
                 coherence: bool = True) -> list[Finding]:
    """One pin on its own: does it describe one ordinary machine, and is it current?"""
    config, meta = split_pin(data)
    out: list[Finding] = []

    def add(check: str, level: str, detail: str, **data: Any) -> None:
        out.append(Finding(check, level, detail, lane, data))

    ua = config.get("navigator.userAgent")
    claimed = ua_os(ua)
    _pin_os_findings(config, ua, claimed, add)
    _pin_shape_findings(config, claimed, add)
    version = meta.get("version") if isinstance(meta.get("version"), int) else 1
    if version < PIN_VERSION:
        add("pin", WARN, f"a version {version} pin: it is completed on the lane's next launch")
    if claimed and claimed != "linux":
        add("pin", WARN, f"a {claimed} identity on a Linux node: text metrics, codecs, form widgets and system "
                         "colours still come from Linux (Camoufox issue 895), so a page that measures them sees "
                         "another OS", claimed=claimed)
    _pin_freshness_findings(ua_major(ua), installed_major, add)
    if coherence:
        for violation in coherence_violations(config, claimed) or []:
            add("pin", WARN, f"Camoufox coherence rule {violation}")
    if not out:
        add("pin", OK, f"one consistent {claimed or 'unknown OS'} device")
    return out


def _pin_os_findings(config: Mapping[str, Any], ua: Any, claimed: str | None, add: _Add) -> None:
    """Everything that names an OS must name the one the user agent claims."""
    others = {"navigator.platform": platform_os(config.get("navigator.platform")),
              "navigator.oscpu": oscpu_os(config.get("navigator.oscpu"))}
    for key, seen in others.items():
        if claimed and seen and seen != claimed:
            add("pin", FAIL, f"the user agent says {claimed} and {key} says {seen}", key=key)
    header_ua = config.get("headers.User-Agent")
    if header_ua and ua and header_ua != ua:
        add("pin", FAIL, "the User-Agent header and navigator.userAgent differ")
    problem = renderer_problem(config.get("webGl:renderer"), claimed)
    if problem:
        add("pin", FAIL if "software" in problem else WARN, f"WebGL: {problem}")
    fonts = config.get("fonts") if isinstance(config.get("fonts"), list) else []
    for other, names in OS_ONLY_FONTS.items():
        if claimed and other != claimed:
            stray = sorted(set(fonts) & set(names))
            if stray:
                add("pin", FAIL, f"a {claimed} identity lists {other} fonts: {', '.join(stray[:5])}", fonts=stray)


def _pin_shape_findings(config: Mapping[str, Any], claimed: str | None, add: _Add) -> None:
    """The language, the window against the screen, touch, and the keys Camoufox would draw again."""
    language = config.get("locale:language")
    accept = str(config.get("headers.Accept-Language") or "")
    if language and accept and not accept.lower().startswith(str(language).lower()):
        add("pin", WARN, f"Accept-Language {accept.split(',')[0]} does not start with the locale's language "
                         f"{language}")
    width, height = config.get("screen.width"), config.get("screen.height")
    for axis, outer, size in (("width", config.get("window.outerWidth"), width),
                              ("height", config.get("window.outerHeight"), height)):
        if isinstance(outer, int) and isinstance(size, int) and outer > size:
            add("pin", FAIL, f"the window is {outer} px in {axis}, larger than the {size} px screen")
    touch = config.get("navigator.maxTouchPoints")
    if isinstance(touch, int) and touch > 0 and claimed in ("macos", "linux"):
        add("pin", WARN, f"a {claimed} desktop with {touch} touch points")
    missing = [k for k in DRAWN_KEYS if k not in config]
    if missing:
        add("pin", WARN, f"the pin lacks {', '.join(missing)}, which Camoufox draws again on every launch",
            missing=missing)


def _pin_freshness_findings(pinned: int | None, installed_major: int | None, add: _Add) -> None:
    """The pinned Firefox version against the installed one."""
    if not (installed_major and pinned):
        return
    if pinned < installed_major:
        add("freshness", WARN, f"the pin says Firefox {pinned} and the browser is {installed_major}; a user "
                               "agent that lags its engine is a signal (openajar lane refresh-fingerprint "
                               "--keep-device)", pinned=pinned, installed=installed_major)
    elif pinned > installed_major:
        add("freshness", FAIL, f"the pin says Firefox {pinned}, newer than the installed {installed_major}",
            pinned=pinned, installed=installed_major)


DEVICE_KEYS = ("navigator.userAgent", "navigator.platform", "navigator.hardwareConcurrency", "screen.width",
               "screen.height", "window.outerWidth", "window.outerHeight", "webGl:vendor", "webGl:renderer",
               "timezone")


def device_of(config: Mapping[str, Any]) -> dict[str, Any]:
    """What makes a pin one machine to a site: the keys above, the font list and the voices."""
    out = {k: config.get(k) for k in DEVICE_KEYS}
    out["fonts"] = sorted(config.get("fonts") or []) if isinstance(config.get("fonts"), list) else None
    out["voices"] = digest(config.get("voices")) if config.get("voices") is not None else None
    return out


def cross_pin_findings(pins: Mapping[str, tuple[Mapping[str, Any], str | None]]) -> list[Finding]:
    """Pins of several lanes: {lane: (pin data, proxy label)}. Two lanes on one device are linked by
    any site that sees both; that is the intent only for lanes of one company that also share one
    proxy (AUD-02). The same font list or exit IP behind different proxies links too."""
    out: list[Finding] = []
    devices: dict[str, list[str]] = {}
    fonts: dict[str, list[str]] = {}
    exits: dict[str, set[str]] = {}
    proxies: dict[str, list[str]] = {}
    for lane, (data, proxy) in sorted(pins.items()):
        config, meta = split_pin(data)
        devices.setdefault(digest(device_of(config)), []).append(lane)
        if isinstance(config.get("fonts"), list) and config["fonts"]:
            fonts.setdefault(digest(sorted(config["fonts"])), []).append(lane)
        geo = meta.get("geo") if isinstance(meta.get("geo"), dict) else {}
        if geo.get("ip"):
            exits.setdefault(str(geo["ip"]), set()).add(proxy or "(no proxy)")
        proxies.setdefault(proxy or "(no proxy)", []).append(lane)
    for lanes in devices.values():
        if len(lanes) > 1:
            behind = {pins[lane][1] or "(no proxy)" for lane in lanes}
            if len(behind) > 1:
                out.append(Finding("collision", FAIL, f"{', '.join(lanes)} pin one device but leave through "
                                   f"different proxies ({', '.join(sorted(behind))}): one machine seen from "
                                   "several places", data={"lanes": lanes}))
            else:
                out.append(Finding("collision", WARN, f"{', '.join(lanes)} pin one device behind one proxy: right "
                                   "only when they are meant to be one company's computer (AUD-02)",
                                   data={"lanes": lanes}))
    for lanes in fonts.values():
        if len(lanes) > 1 and not any(set(lanes) <= set(d) for d in devices.values() if len(d) > 1):
            out.append(Finding("collision", WARN, f"{', '.join(lanes)} pin the same font list",
                               data={"lanes": lanes}))
    for ip, labels in sorted(exits.items()):
        if len(labels) > 1:
            out.append(Finding("collision", FAIL, f"proxies {', '.join(sorted(labels))} were pinned from one exit "
                               f"IP {ip}", data={"ip": ip}))
    for label, lanes in sorted(proxies.items()):
        if label != "(no proxy)" and len(lanes) > 1:
            out.append(Finding("collision", OK, f"{len(lanes)} lanes leave through proxy {label}: {', '.join(lanes)}",
                               data={"proxy": label, "lanes": lanes}))
    return out


# ---- dumps --------------------------------------------------------------------------------------

def _get(dump: Mapping[str, Any], section: str) -> dict[str, Any]:
    value = dump.get(section)
    return value if isinstance(value, dict) else {}


def dump_findings(lane: str, dump: Mapping[str, Any], *, config: Mapping[str, Any] | None = None,
                  proxied: bool = False) -> list[Finding]:
    """What one page saw in a live lane. `config` is the lane's pinned config, when known."""
    out: list[Finding] = []

    def add(check: str, level: str, detail: str, **data: Any) -> None:
        out.append(Finding(check, level, detail, lane, data))

    seen = _Dump(dump)
    _automation_findings(seen, add)
    _side_probe_findings(seen, add)
    _geometry_findings(seen, add)
    _input_findings(seen, add)
    _locale_findings(seen, add, config=config, proxied=proxied)
    _engine_findings(seen, add)
    for section, value in dump.items():
        if isinstance(value, str) and value.startswith("error"):
            add("consistency", WARN, f"the {section} probe failed: {value}")
    if not out:
        add("consistency", OK, f"no automation trace and no inconsistency ({seen.claimed or 'unknown OS'} identity)")
    return out


class _Dump:
    """One probe dump, with the sections every group of checks reads."""

    def __init__(self, dump: Mapping[str, Any]):
        self.raw = dump
        self.nav, self.scr, self.win = _get(dump, "navigator"), _get(dump, "screen"), _get(dump, "window")
        self.claimed = ua_os(self.nav.get("userAgent"))

    def get(self, section: str) -> dict[str, Any]:
        return _get(self.raw, section)



def _automation_findings(d: _Dump, add: _Add) -> None:
    """Traces a driver leaves: webdriver, its globals and attributes, patched functions."""
    nav, auto = d.nav, d.get("automation")
    if nav.get("webdriver") or auto.get("webdriver"):
        add("automation", FAIL, "navigator.webdriver is true")
    if auto.get("webdriverOwnProperty"):
        add("automation", FAIL, "navigator has its own webdriver property (stock Firefox has it on the prototype)")
    if auto and not auto.get("webdriverOnPrototype", True):
        add("automation", WARN, "Navigator.prototype has no webdriver getter, which stock Firefox has")
    for key, what in (("globals", "driver globals on window"), ("attributes", "driver attributes on <html>"),
                      ("notNative", "functions that are not native code")):
        if auto.get(key):
            add("automation", FAIL, f"{what}: {', '.join(map(str, auto[key]))}", values=auto[key])
    if auto.get("chrome") not in (None, "undefined"):
        add("automation", FAIL, f"window.chrome is {auto['chrome']} on a Firefox")
    notification, permission = auto.get("notification"), d.raw.get("permissionsNotifications")
    if notification and isinstance(permission, str) and \
            {"default": "prompt", "granted": "granted", "denied": "denied"}.get(notification) != permission:
        add("consistency", WARN, f"Notification.permission is {notification} but the Permissions API says {permission}")


def _side_probe_findings(d: _Dump, add: _Add) -> None:
    """A worker and a frame must see the browser the page sees."""
    nav, scr, intl = d.nav, d.scr, d.get("intl")
    for name in ("worker", "frame"):
        other = d.get(name)
        if not other:
            if isinstance(d.raw.get(name), str):
                add("consistency", WARN, f"the {name} probe failed: {d.raw[name]}")
            continue
        pairs = {"userAgent": nav.get("userAgent"), "platform": nav.get("platform"),
                 "hardwareConcurrency": nav.get("hardwareConcurrency"), "languages": nav.get("languages"),
                 "webdriver": nav.get("webdriver"), "timeZone": intl.get("timeZone"),
                 "appVersion": nav.get("appVersion"), "language": nav.get("language"),
                 "offsetJanuary": intl.get("offsetJanuary"),
                 "screenWidth": scr.get("width"), "screenHeight": scr.get("height")}
        differ = sorted(k for k, v in pairs.items() if k in other and other[k] != v)
        if differ:
            add("consistency", FAIL, f"a {name} sees another browser than the page: {', '.join(differ)}",
                values={k: [pairs[k], other[k]] for k in differ})
        worker_gl = other.get("webgl")
        page_gl = d.get("webgl")
        if isinstance(worker_gl, list) and page_gl.get("renderer") and worker_gl[1] != page_gl.get("renderer"):
            add("consistency", FAIL, f"WebGL in a {name} names {worker_gl[1]}, the page {page_gl.get('renderer')}")


def _geometry_findings(d: _Dump, add: _Add) -> None:
    """The screen, the window and CSS media must describe one window on one screen."""
    scr, win, css = d.scr, d.win, d.get("css")
    for key, what in (("deviceWidth", "width"), ("deviceHeight", "height")):
        if css.get(key) is False:
            add("consistency", FAIL, f"CSS device-{what} is not the screen {what} the page reads")
    if css.get("resolution") is False:
        add("consistency", WARN, "CSS resolution does not match devicePixelRatio")
    if isinstance(win.get("outerWidth"), int) and isinstance(scr.get("width"), int) and win["outerWidth"] > scr["width"]:
        add("consistency", FAIL, f"the window ({win['outerWidth']} px) is wider than the screen ({scr['width']} px)")
    if all(isinstance(scr.get(k), int) for k in ("availWidth", "width")) and scr["availWidth"] > scr["width"]:
        add("consistency", FAIL, "the work area is wider than the screen")
    chrome = (win.get("outerHeight") or 0) - (win.get("innerHeight") or 0)
    if win.get("outerHeight") and not 40 <= chrome <= 200:
        add("consistency", WARN, f"the browser's toolbars are {chrome} px tall (outer minus inner height)")
    if isinstance(win.get("innerScreenX"), int) and isinstance(win.get("screenX"), int):
        dx = win["innerScreenX"] - win["screenX"] - ((win.get("outerWidth") or 0) - (win.get("innerWidth") or 0)) // 2
        dy = win["innerScreenY"] - win["screenY"] - chrome
        if abs(dx) > 2 or abs(dy) > 2:
            add("consistency", FAIL, f"the content area is at ({win['innerScreenX']}, {win['innerScreenY']}) on the "
                                     f"screen, which a window at ({win['screenX']}, {win['screenY']}) cannot have "
                                     "(mozInnerScreenX/Y show the real window, Camoufox issue 974)",
                values={"dx": dx, "dy": dy})


def _input_findings(d: _Dump, add: _Add) -> None:
    """Touch, plugins and the User-Agent header."""
    nav, media = d.nav, d.get("media")
    touch = bool(media.get("anyCoarse") or media.get("touch"))
    if isinstance(nav.get("maxTouchPoints"), int) and media and (nav["maxTouchPoints"] > 0) != touch:
        add("consistency", WARN, f"maxTouchPoints {nav['maxTouchPoints']} but touch media and events say "
                                 f"{'touch' if touch else 'no touch'}")
    if nav.get("pdfViewerEnabled") and not nav.get("plugins"):
        add("consistency", WARN, "pdfViewerEnabled with no plugins (Firefox lists its PDF viewer five times)")
    headers = d.get("headers")
    if headers.get("user-agent") and nav.get("userAgent") and headers["user-agent"] != nav["userAgent"]:
        add("consistency", FAIL, "the User-Agent header differs from navigator.userAgent")


def _locale_findings(d: _Dump, add: _Add, *, config: Mapping[str, Any] | None, proxied: bool) -> None:
    """Languages and the timezone, against the headers and the pin."""
    nav, headers, intl = d.nav, d.get("headers"), d.get("intl")
    accept = str(headers.get("accept-language") or "")
    languages = nav.get("languages") if isinstance(nav.get("languages"), list) else []
    if accept and languages and accept.split(",")[0].split(";")[0].strip().lower() != str(languages[0]).lower():
        add("consistency", FAIL, f"Accept-Language starts with {accept.split(',')[0]}, navigator.languages with "
                                 f"{languages[0]}")
    if config and config.get("timezone") and intl.get("timeZone") and intl["timeZone"] != config["timezone"]:
        add("consistency", FAIL, f"the page's timezone is {intl['timeZone']}, the pin's {config['timezone']}")
    if intl and intl.get("offsetJanuary") is not None and intl.get("timeZone") in ("UTC", "Etc/UTC") and proxied:
        add("consistency", WARN, "a proxied lane in UTC: the timezone was not set from the exit")


def _engine_findings(d: _Dump, add: _Add) -> None:
    """What the machine shows through the browser: the GPU, the fonts, the codecs, WebRTC."""
    claimed = d.claimed
    problem = renderer_problem(d.get("webgl").get("renderer"), claimed)
    if problem:
        add("consistency", FAIL if "software" in problem else WARN, f"WebGL: {problem}")
    found = set(d.get("fonts").get("found") or [])
    if claimed:
        for other, names in OS_ONLY_FONTS.items():
            stray = sorted(found & set(names)) if other != claimed else []
            if stray:
                add("consistency", FAIL, f"a {claimed} identity shows {other} fonts: {', '.join(stray[:6])}",
                    fonts=stray)
        core = OS_CORE_FONTS.get(claimed) or ()
        if core and found and not found & set(core):
            add("consistency", WARN, f"a {claimed} identity shows none of {', '.join(core)}")
    codecs = d.get("codecs")
    if claimed in ("windows", "macos") and codecs and codecs.get("h264") == "":
        add("consistency", WARN, f"a {claimed} identity cannot play H.264, which every {claimed} Firefox can "
                                 "(the host's codecs show, Camoufox issue 895)")
    raw = d.get("webrtc").get("rawAddresses") or []
    if raw:
        add("leak", FAIL, f"WebRTC shows raw addresses: {', '.join(map(str, raw))}", addresses=raw)


def comparable(dump: Mapping[str, Any], canvas_noise: bool) -> dict[str, Any]:
    """A dump without what changes between launches by design. Firefox's own baseline fingerprinting
    protection (EfficientCanvasRandomization) gives 2D canvas readback a new noise key each session,
    the same within one, as stock Firefox does for every user; with Camoufox's canvas cover on,
    every canvas and WebGL readback is fresh random data (as with resistFingerprinting)."""
    out = json.loads(json.dumps(dump))
    canvas = out.get("canvas")
    if isinstance(canvas, dict):
        canvas.pop("digest", None)
        canvas.pop("sameWithinPage", None)
    if canvas_noise:
        for kind in ("webgl", "webgl2"):
            if isinstance(out.get(kind), dict):
                out[kind].pop("readback", None)
    return out


def differences(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(a.keys() | b.keys()) for d in differences(a.get(k), b.get(k), f"{path}.{k}")]
    return [] if a == b else [f"{path}: {json.dumps(a)[:80]} != {json.dumps(b)[:80]}"]


def drift_findings(lane: str, dumps: list[Mapping[str, Any]], *, canvas_noise: bool) -> list[Finding]:
    """The same lane on several launches must show a page one device."""
    if len(dumps) < 2:
        return []
    first = comparable(dumps[0], canvas_noise)
    out = []
    for n, dump in enumerate(dumps[1:], start=2):
        diff = differences(first, comparable(dump, canvas_noise))
        if diff:
            out.append(Finding("drift", FAIL, f"launch {n} differs from launch 1 in {len(diff)} values: "
                               + "; ".join(diff[:4]), lane, {"differences": diff}))
    canvas = [_get(d, "canvas").get("sameWithinPage") for d in dumps]
    if any(v is not None and v is canvas_noise for v in canvas):
        out.append(Finding("drift", WARN, f"canvas readback {'repeats' if canvas_noise else 'changes'} within a "
                           f"page, which a lane with canvas cover {'on' if canvas_noise else 'off'} should not do",
                           lane))
    if not out:
        out.append(Finding("drift", OK, f"{len(dumps)} launches show one device", lane))
    return out


def surfaces(dump: Mapping[str, Any]) -> dict[str, str | None]:
    """One value per surface a site could link lanes by (see DEVICE_SURFACES, HOST_SURFACES)."""
    nav, scr, win = _get(dump, "navigator"), _get(dump, "screen"), _get(dump, "window")
    gl, canvas, audio = _get(dump, "webgl"), _get(dump, "canvas"), _get(dump, "audio")
    device = {k: nav.get(k) for k in ("userAgent", "platform", "hardwareConcurrency", "languages")}
    device.update({"screen": [scr.get("width"), scr.get("height")], "window": [win.get("outerWidth"),
                   win.get("outerHeight")], "renderer": gl.get("renderer"), "timeZone": _get(dump, "intl").get(
                   "timeZone"), "fonts": _get(dump, "fonts").get("digest"), "voices": _get(dump, "voices").get(
                   "digest")})

    def maybe(value: Any) -> str | None:
        return None if value in (None, {}, []) else digest(value)

    return {
        "os": ua_os(nav.get("userAgent")),
        "device": digest(device),
        "fonts": maybe(_get(dump, "fonts").get("digest")),
        "voices": maybe(_get(dump, "voices").get("digest")),
        "mediaDevices": maybe(_get(dump, "mediaDevices").get("ids")),
        "webgl": maybe([gl.get("renderer"), gl.get("params"), gl.get("extensionsDigest")] if gl else None),
        "canvas": maybe(canvas.get("digest")),
        "textWidth": maybe(canvas.get("textWidth")),
        "audio": maybe(audio.get("digest")),
        "math": maybe(dump.get("math")),
        "webglReadback": maybe(gl.get("readback")),
        "codecs": maybe(dump.get("codecs")),
    }


def collision_findings(dumps: Mapping[str, Mapping[str, Any]]) -> list[Finding]:
    """First-launch dumps of several lanes: which values they share. Two lanes that share the whole
    device are one machine to any site that sees both; a shared font list, GPU or canvas noise each
    narrows it down. Shared surfaces (voices, media devices, text metrics, audio, maths) are what
    every lane on one node shares with every user of the same build, OS and language on that kind of
    host."""
    out: list[Finding] = []
    by_lane = {lane: surfaces(dump) for lane, dump in sorted(dumps.items())}
    n = len(by_lane)
    if n < 2:
        return out
    for surface in (*DEVICE_SURFACES, *HOST_SURFACES):
        groups: dict[str, list[str]] = {}
        for lane, values in by_lane.items():
            if values.get(surface):
                groups.setdefault(values[surface], []).append(lane)
        shared = [lanes for lanes in groups.values() if len(lanes) > 1]
        if not shared:
            continue
        level = FAIL if surface == "device" else WARN if surface in DEVICE_SURFACES else OK
        note = "" if surface in DEVICE_SURFACES else ": shared by every lane of this node and build"
        flagged, ordinary = [], []
        for lanes in shared:
            systems = {by_lane[lane]["os"] for lane in lanes}
            # every Mac of a model has the same fonts and GPU, and so does a stock Linux desktop
            same_kind = surface in ("fonts", "webgl") and len(systems) == 1 and systems <= {"macos", "linux"}
            (ordinary if same_kind else flagged).append(lanes)
        why = "the same device" if surface == "device" else f"the same {surface}"
        kind = ": as every Mac of a model and every stock Linux desktop shows"
        for lanes_groups, lvl, tail in ((flagged, level, note), (ordinary, OK, kind)):
            if lanes_groups:
                sizes = ", ".join(str(len(lanes)) for lanes in lanes_groups)
                names = "; ".join(", ".join(lanes) for lanes in lanes_groups)
                out.append(Finding("collision", lvl, f"{why} on {sizes} of {n} lanes ({names}){tail}",
                                   data={"surface": surface, "groups": lanes_groups}))
    if not any(f.level != OK for f in out):
        out.append(Finding("collision", OK, f"no two of {n} lanes show the same device, and they share nothing a "
                                            "kind of machine does not share"))
    return out


# ---- a node's pins ------------------------------------------------------------------------------

def device_findings(devices: Mapping[str, str | None], lanes: Mapping[str, tuple[str | None, str | None]],
                    pins: Mapping[str, Mapping[str, Any]], *, exits: Mapping[str, str] | None = None,
                    geo: Mapping[str, Mapping[str, Any]] | None = None, at: Any = None) -> list[Finding]:
    """Leaks across devices (AUD-02). `devices`: {name: proxy label}; `lanes`:
    {lane: (device or None, proxy label)}; `pins`: {unit: pin data}, where a unit is a lane with
    its own pin or `device:<name>`; `exits`: {proxy label: pinned exit IP} (the AUD-14 self-check's
    pin); `geo`: {proxy label: {country, timezone, ip}} from the lane check's daily lookup.

    One device is one computer: its lanes are one company's, and leave through its one proxy. Two
    computers on one exit look like an office the site has never seen together; a device's lanes on
    two exits look like one computer in two places; a pin whose timezone, country or locale is not
    where the proxy exits looks like a traveller on a VPN."""
    out: list[Finding] = []
    for name, proxy in sorted(devices.items()):
        out += _one_device_findings(name, proxy, lanes)
    users = _computers_by_proxy(devices, lanes)
    if devices:  # before any device exists, lanes sharing a proxy are the old plan (cross_pin_findings)
        out += _shared_proxy_findings(users)
    out += _shared_exit_findings(users, exits or {})
    out += _pin_location_findings(devices, lanes, pins, geo or {}, at)
    return out


def _one_device_findings(name: str, proxy: str | None,
                         lanes: Mapping[str, tuple[str | None, str | None]]) -> list[Finding]:
    """One device's lanes: one company's, leaving through the device's one proxy."""
    out: list[Finding] = []
    on = sorted(lane for lane, (device, _p) in lanes.items() if device == name)
    workspaces = sorted({lane.split("/", 1)[0] for lane in on})
    if len(workspaces) > 1:
        out.append(Finding("device", FAIL, f"device {name} has lanes of {len(workspaces)} workspaces "
                           f"({', '.join(workspaces)}): one computer links those companies' accounts. Give each "
                           "company a device of its own", f"device:{name}", {"workspaces": workspaces}))
    strays = sorted(lane for lane in on if lanes[lane][1] != proxy)
    if strays:
        out.append(Finding("device", FAIL, f"{', '.join(strays)} on device {name} leave through another proxy "
                           f"than the device's {proxy or '(none)'}: one computer seen from several places",
                           f"device:{name}", {"lanes": strays}))
    if proxy is None and on:
        out.append(Finding("device", WARN, f"device {name} has no proxy: its lanes leave from the node's own IP",
                           f"device:{name}"))
    return out


def _computers_by_proxy(devices: Mapping[str, str | None],
                        lanes: Mapping[str, tuple[str | None, str | None]]) -> dict[str, list[str]]:
    """{proxy label: the computers behind it}: devices, and lanes with a pin of their own."""
    users: dict[str, list[str]] = {}
    for name, proxy in devices.items():
        if proxy:
            users.setdefault(proxy, []).append(f"device:{name}")
    for lane, (device, proxy) in lanes.items():
        if device is None and proxy:
            users.setdefault(proxy, []).append(lane)
    return users


def _shared_proxy_findings(users: Mapping[str, list[str]]) -> list[Finding]:
    out: list[Finding] = []
    for proxy, units in sorted(users.items()):
        named = sorted(u for u in units if u.startswith("device:"))
        if len(units) > 1 and named:
            level = FAIL if len(named) > 1 else WARN
            out.append(Finding("device", level, f"{len(units)} computers leave through proxy {proxy} "
                               f"({', '.join(sorted(units))}): one exit IP for several computers. One proxy per "
                               "device", None, {"proxy": proxy, "units": sorted(units)}))
    return out


def _shared_exit_findings(users: Mapping[str, list[str]], exits: Mapping[str, str]) -> list[Finding]:
    """Several proxies that exit from one IP put their computers on one address."""
    by_ip: dict[str, list[str]] = {}
    for proxy, ip in exits.items():
        if proxy in users:
            by_ip.setdefault(ip, []).append(proxy)
    out: list[Finding] = []
    for ip, labels in sorted(by_ip.items()):
        units = sorted(u for label in labels for u in users[label])
        if len(labels) > 1 and any(u.startswith("device:") for u in units):
            out.append(Finding("device", FAIL, f"proxies {', '.join(sorted(labels))} exit from one IP {ip}, so "
                               f"{', '.join(units)} are several computers on one address", None,
                               {"ip": ip, "proxies": sorted(labels)}))
    return out


def _pin_location_findings(devices: Mapping[str, str | None], lanes: Mapping[str, tuple[str | None, str | None]],
                           pins: Mapping[str, Mapping[str, Any]], geo: Mapping[str, Mapping[str, Any]],
                           at: Any) -> list[Finding]:
    """Each pin's timezone, country and locale against where its proxy exits."""
    from datetime import UTC, datetime

    when = at or datetime.now(UTC)
    out: list[Finding] = []
    for unit, data in sorted(pins.items()):
        if unit.startswith("device:"):
            proxy = devices.get(unit.split(":", 1)[1])
        else:
            proxy = lanes.get(unit, (None, None))[1]
        where = geo.get(proxy) if proxy else None
        check = location_check(data, where, at=when)
        if check is not None and check.level == "warn":
            out.append(Finding("location", WARN, f"proxy {proxy}: {check.detail}", unit, check.data))
    return out


def location_check(data: Mapping[str, Any], where: Mapping[str, Any] | None, *, at: Any = None) -> Any:
    """A pin's timezone, country and locale against where its proxy exits (the exit-geo cache):
    the lane check's judge_location, or None when the exit's place is not known."""
    from datetime import UTC, datetime

    from openajar.stealth.lanerules import judge_location

    if not where or not where.get("country"):
        return None
    config, meta = split_pin(data)
    pinned_geo = meta.get("geo") if isinstance(meta.get("geo"), dict) else {}
    language, region = config.get("locale:language"), config.get("locale:region")
    return judge_location(dict(where), pinned_tz=config.get("timezone") if isinstance(config.get("timezone"), str)
                          else None, pinned_country=pinned_geo.get("country"),
                          locale=f"{language}-{region}" if language and region else None, at=at or datetime.now(UTC))


def _proxy_exits(home: Path) -> dict[str, str]:
    from openajar.core.layout import connect_ro

    db = Path(home) / "openajar.db"
    if not db.exists():
        return {}
    try:
        conn = connect_ro(db)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "proxy_exits" not in tables:
                return {}
            return {label: ip for label, ip in conn.execute("SELECT label, exit_ip FROM proxy_exits")}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - the audit reads what it can
        return {}


def _exit_geo(home: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((Path(home) / "cache" / "exit-geo.json").read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def replaced_pin_findings(home: Path, devices: Mapping[str, Any], lanes: Iterable[Any]) -> list[Finding]:
    """Pins the start-up repair replaced (openajar.node.devices.repair_device_pins): a device's or a
    lane's copy that differed and was older, kept beside it as `fingerprint.json.replaced*.bak`.
    Listed until the owner has checked which computer the sites know and removed the file."""
    out: list[Finding] = []
    places = [(f"device:{name}", Path(home) / "devices" / name) for name in sorted(devices)]
    places += [(lane.id, lane.fingerprint.parent) for lane in lanes]
    for unit, folder in places:
        for kept in sorted(folder.glob("fingerprint.json.replaced*.bak")) if folder.is_dir() else ():
            out.append(Finding("device", WARN, f"agentosd replaced an older copy of this device's pin at start; the "
                               f"old one is kept at {kept}. Check which computer the sites know, then remove the "
                               "file", unit, {"kept": str(kept)}))
    return out


def audit_pins(home: Path, *, installed_major: int | None = None) -> tuple[list[Finding], int]:
    """Every pinned lane and device on a node, from its files alone (`openajar lane audit`): each
    pin's own findings and the collisions between them. A device's pin is audited once, as
    `device:<name>`, and the lanes on it count as that one computer (AUD-02). Returns the findings
    and how many pins were read."""
    from openajar.core.layout import device_specs, lane_dirs, lane_specs

    specs = lane_specs(home)
    devices = device_specs(home)
    pins: dict[str, tuple[dict[str, Any], str | None]] = {}
    out: list[Finding] = []

    def read(unit: str, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            out.append(Finding("pin", FAIL, f"the pin {path} cannot be read", unit))
            return None
        if not isinstance(data, dict) or not data:
            out.append(Finding("pin", FAIL, "the pin holds no settings", unit))
            return None
        return data

    for name, device in sorted(devices.items()):
        path = Path(home) / "devices" / name / "fingerprint.json"
        if not path.exists():
            continue
        data = read(f"device:{name}", path)
        if data is not None:
            pins[f"device:{name}"] = (data, device.proxy_label)
            out.extend(pin_findings(f"device:{name}", data, installed_major=installed_major))
    copies: dict[str, dict[bytes, list[str]]] = {}  # device without its pin file -> copy -> lanes
    for lane in lane_dirs(home):
        spec = specs.get(lane.id)
        if spec is not None and spec.device:
            unit = f"device:{spec.device}"
            device_pin = Path(home) / "devices" / spec.device / "fingerprint.json"
            try:
                copy = lane.fingerprint.read_bytes() if lane.fingerprint.exists() else None
            except OSError:
                copy = None
            if unit in pins and copy is not None and copy != device_pin.read_bytes():
                out.append(Finding("device", WARN, f"the lane's copy of device {spec.device}'s pin differs from the "
                                   "device's; on its next start agentosd keeps the newer one and the older beside "
                                   "it", lane.id))
            elif spec.device in devices and not device_pin.exists() and copy is not None:
                copies.setdefault(spec.device, {}).setdefault(copy, []).append(lane.id)
            continue
        if not lane.fingerprint.exists():
            continue
        data = read(lane.id, lane.fingerprint)
        if data is None:
            continue
        pins[lane.id] = (data, spec.proxy_label if spec else None)
        out.extend(pin_findings(lane.id, data, installed_major=installed_major))
    for name, found in sorted(copies.items()):
        unit = f"device:{name}"
        if len(found) > 1:
            groups = "; ".join(", ".join(lanes) for lanes in found.values())
            out.append(Finding("device", FAIL, f"device {name} has no pin file and its lanes hold copies that "
                               f"disagree ({groups}): they are not launched until the right pin is back in "
                               f"devices/{name}/fingerprint.json", unit, {"groups": list(found.values())}))
            continue
        [(raw, lanes)] = found.items()
        out.append(Finding("device", WARN, f"device {name} has no pin file; {', '.join(lanes)} hold a copy of one "
                           "pin, which agentosd puts back on the next launch", unit, {"lanes": lanes}))
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict) and data:
            pins[unit] = (data, devices[name].proxy_label)
            out.extend(pin_findings(unit, data, installed_major=installed_major))
    out.extend(replaced_pin_findings(home, devices, lane_dirs(home)))
    out.extend(cross_pin_findings(pins))
    out.extend(device_findings({n: d.proxy_label for n, d in devices.items()},
                               {i: (s.device, s.proxy_label) for i, s in specs.items()},
                               {unit: data for unit, (data, _p) in pins.items()},
                               exits=_proxy_exits(home), geo=_exit_geo(home)))
    return out, len(pins)


def format_findings(findings: Iterable[Finding]) -> list[str]:
    return [f"  {f.level:<4}  {f.check:<11} {(f.lane or '-'):<20} {f.detail}"
            for f in sorted(findings, key=lambda f: (-RANK[f.level], f.lane or "", f.check))]
