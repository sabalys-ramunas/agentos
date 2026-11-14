"""A lane's pinned fingerprint: the pin file, what it holds, and how it is read, written and backed up."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.core.errors import EngineError
from agentos.core.fs import write_private

log = logging.getLogger("agentos.engine.camoufox")


# ---- fingerprint pinning ----------------------------------------------------------

def parse_camou_config(environ: bytes) -> dict[str, Any]:
    """Join the CAMOU_CONFIG_<n> chunks of a NUL-separated environ block."""
    env = environ.decode(errors="replace").split("\0")
    chunks = sorted((kv for kv in env if kv.startswith("CAMOU_CONFIG_")),
                    key=lambda kv: int(kv.split("=", 1)[0].rsplit("_", 1)[1]))
    if not chunks:
        raise EngineError("the browser process carries no CAMOU_CONFIG, so its fingerprint cannot be pinned")
    return json.loads("".join(kv.split("=", 1)[1] for kv in chunks))


def write_private_json(path: Path, data: Any, *, exclusive: bool = False) -> None:
    """Write JSON readable only by the owner (created 600, replaced atomically, fsynced). `exclusive`:
    only when no file is there yet (FileExistsError otherwise, and nothing is written)."""
    write_private_bytes(path, json.dumps(data, indent=1).encode(), exclusive=exclusive)


def write_private_bytes(path: Path, raw: bytes, *, exclusive: bool = False) -> None:
    # One writer for every private file (agentos.core.fs): fsynced, so a pin never comes back empty.
    write_private(path, raw, exclusive=exclusive)


def backup_pin(path: Path, version: int, *, label: str | None = None) -> Path:
    """Keep the pin file as it is now, byte for byte, next to it (600):
    `fingerprint.json.pin-v1.bak` (or `fingerprint.json.<label>.bak`), with the time added when
    that name holds another one."""
    raw = path.read_bytes()
    label = label or f"pin-v{version}"
    dest = path.with_name(f"{path.name}.{label}.bak")
    if dest.exists() and dest.read_bytes() != raw:
        dest = path.with_name(f"{path.name}.{label}.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.bak")
    if not dest.exists():
        write_private_bytes(dest, raw)
    return dest


def os_of(config: dict[str, Any]) -> str:
    platform = str(config.get("navigator.platform") or "")
    return "windows" if platform.startswith("Win") else "macos" if platform.startswith("Mac") else "linux"


def read_pin(fingerprint_path: Path, lane_id: str = "lane") -> dict[str, Any]:
    """The pin file as it is stored: the pinned Camoufox config, plus agentos's record under
    PIN_META in a version 2 pin. Raises EngineError for a pin that is not a non-empty JSON
    object: Camoufox reads `{}` as "no config" and would quietly make a new device."""
    try:
        config = json.loads(fingerprint_path.read_text())
    except ValueError as e:
        raise EngineError(
            f"{lane_id}: the pinned fingerprint {fingerprint_path} is not valid JSON ({e}). Restore it "
            "from a backup; deleting it pins a new one, and sites will see a new device.") from None
    if not isinstance(config, dict) or not config:
        raise EngineError(
            f"{lane_id}: the pinned fingerprint {fingerprint_path} holds no settings (it must be a non-empty JSON "
            "object). Restore it from a backup; deleting it pins a new one, and sites will see a new device.")
    return config


# ---- what a pin holds --------------------------------------------------------------
#
# The pin file stays one flat JSON object of Camoufox config keys, so anything that reads a key
# from it (ops reads the pinned Firefox version, the lane manager the window size) works on every
# version. agentos keeps its own record under one reserved key, PIN_META:
#
#   version         2. A file without PIN_META is version 1: the config the browser carried,
#                   as the PoC and the first builds pinned it.
#   prefs           the Firefox prefs Camoufox works out per launch that a page can read
#   canvas_noise    whether canvas readback is covered with random data
#   geo             the exit IP the location keys came from, and that IP's country
#   filled          the keys agentos set because Camoufox would leave them to chance
#   pinned_at, completed_at, completed_from, camoufox: when, from which version, with what

PIN_META = "agentos:pin"
PIN_VERSION = 2

# Config keys that say how a launch behaves, not what the device is. They follow the installed
# Camoufox and the engine's options on every launch (the addon paths move with the install), and
# a new pin leaves them out.
LAUNCHER_KEYS = frozenset({
    "addons", "allowAddonNewtab", "allowMainWorld", "certificatePaths", "certificates", "debug",
    "disableWorldIsolation", "forceScopeAccess", "humanize", "humanize:maxTime", "humanize:minTime",
    "instantAnimations", "showcursor",
})

# Camoufox fills these from a fresh fpgen draw on every launch when the config lacks them, and it
# leaves out every drawn value that is 0, false or empty (fingerprints._cast_to_properties), so a
# first launch that drew `screen.availLeft: 0` pins no availLeft and the next launch shows a new
# one (1920 reads as a second monitor). complete_config() fixes each of them by rule.
DRAWN_KEYS = (
    "navigator.userAgent", "navigator.appVersion", "navigator.oscpu", "navigator.platform",
    "navigator.hardwareConcurrency", "navigator.maxTouchPoints",
    "screen.availLeft", "screen.availTop", "screen.availWidth", "screen.availHeight",
    "screen.width", "screen.height", "screen.colorDepth", "screen.pixelDepth",
    "window.outerWidth", "window.outerHeight", "window.screenX", "window.screenY",
)

# Firefox prefs Camoufox works out on each launch, that a page can read or measure: from a seeded
# draw (WebGL2), from the host (the storage quota from its disk, WebGPU from its GPU), or from the
# identity (scrollbars, maths, keyboard and mouse behaviour of the claimed OS).
PIN_PREFS = (
    "webgl.enable-webgl2",
    "dom.quotaManager.temporaryStorage.fixedLimit",
    "dom.webgpu.enabled", "gfx.blacklist.webgpu",
    "intl.locale.requested",
    "ui.useOverlayScrollbars", "widget.non-native-theme.scrollbar.style",
    "javascript.options.use_ucrt_math", "layout.css.devPixelsPerPx", "gfx.font_rendering.fallback.async",
    "privacy.donottrackheader.enabled", "privacy.globalprivacycontrol.enabled", "permissions.default.geo",
    # what Windows does on a click, a drag or a middle click (Camoufox's windowsBehaviourPrefs)
    "layout.word_select.eat_space_to_next_word", "general.autoScroll", "middlemouse.paste",
    "middlemouse.scrollbarPosition", "full-screen-api.transition-duration.enter",
    "full-screen-api.transition-duration.leave", "ui.scrollToClick", "slider.snapMultiplier",
    "layout.scrollbars.click_and_hold_track.continue_to_end",
)

# Hardening: where Camoufox's defaults read as a bot to a page that looks (docs/design/redteam.md,
# measured on the node 2026-11-05). Applied when a lane is first pinned and recorded in its pin as
# meta "hardening", so a lane keeps what it was created with; an older lane keeps its device.
#   geolocation  Camoufox allows location for every site (permissions.default.geo = 1) so the
#                Permissions API agrees with the position it hands out; a real user's browser says
#                `prompt` on a site never visited. The pin keeps the position (the exit checks use it)
#                but the browser never gets it, and asks: a page waits, as for a user who ignores the
#                prompt. (Keeping the position with `ask` hands it out silently while the API says
#                prompt, Camoufox #769.)
#   quota        the storage quota follows the host's disk (a 38 GB VM reads 4 GB, the same on every
#                lane); about 92% of Firefox users read 10 GiB (Mozilla bug 2037260).
#   WebGL sizes  the node renders with llvmpipe, which enforces exactly 16384 for MAX_TEXTURE_SIZE,
#                MAX_RENDERBUFFER_SIZE and MAX_CUBE_MAP_TEXTURE_SIZE (measured). A claim of 8192 lets a
#                page allocate twice the claim; a claim of 32768 (an NVIDIA GTX 980) fails at the claim.
#                Patching the numbers trades that for another lie (real Firefox on a Linux Intel GPU
#                reports 8192, bug 1199923), so a new Linux lane on a software-rendering node draws
#                its GPU from SOFTWARE_HOST_GPUS: GPUs whose every recorded Linux Firefox device
#                reports exactly 16384 (Camoufox's fpgen data). `harden` only checks, and says so.
#   cores        a claim above what the host runs in parallel shows in a timing test (4 claimed, 4
#                workers ran like 1.5 to 2 on the 2 vCPU VM).
HARDENING = 1
GEO_PREF = "permissions.default.geo"
QUOTA_PREF = "dom.quotaManager.temporaryStorage.fixedLimit"
QUOTA_10_GIB_KB = 52_428_800  # the fixed limit at which a page's estimate() reads 10 GiB
MESA_MAX_SIZE = 16384
WEBGL_SIZE_KEYS = ("3379", "34024", "34076")  # MAX_TEXTURE_SIZE, MAX_RENDERBUFFER_SIZE, MAX_CUBE_MAP_TEXTURE_SIZE
# Linux GPUs whose every recorded Firefox device reports 16384 for all three sizes (fpgen, Camoufox
# 0.5.9: 10 and 16 recorded devices). Left out: llvmpipe (an honest software renderer reads as a
# bot far more than any mismatch), single-device buckets (every lane would share one WebGL device).
SOFTWARE_HOST_GPUS = (
    ("AMD", "Radeon HD 3200 Graphics, or similar"),
    ("AMD", "Radeon R9 200 Series, or similar"),
)
GEOLOCATION_PREFIX = "geolocation:"


def harden(config: dict[str, Any], prefs: dict[str, Any], *,
           host_cores: int | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """A new lane's identity and prefs with the hardening above, and what it changed. The WebGL sizes
    are only checked: the GPU is chosen at the draw (SOFTWARE_HOST_GPUS)."""
    config, prefs, changed = dict(config), dict(prefs), {}
    for key, value in ((GEO_PREF, 0), (QUOTA_PREF, QUOTA_10_GIB_KB)):
        if prefs.get(key) != value:
            prefs[key] = changed[key] = value
    for section in ("webGl:parameters", "webGl2:parameters"):
        params = config.get(section)
        if isinstance(params, dict):
            off = {k: params[k] for k in WEBGL_SIZE_KEYS
                   if isinstance(params.get(k), int) and params[k] != MESA_MAX_SIZE}
            if off:  # a GPU the node cannot honour; reported, never patched (see above)
                log.warning("the new identity claims WebGL sizes %s the node does not enforce (%d); "
                            "a page can tell", off, MESA_MAX_SIZE)
    cores = _int(config.get("navigator.hardwareConcurrency"))
    if host_cores and cores and cores > host_cores:
        config["navigator.hardwareConcurrency"] = changed["navigator.hardwareConcurrency"] = host_cores
    return config, prefs, changed


def hardened(meta: dict[str, Any]) -> bool:
    return isinstance(meta.get("hardening"), int) and meta["hardening"] >= 1


def browser_view(config: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """The config the browser is handed: a hardened lane's position stays in the pin only."""
    if not hardened(meta):
        return config
    return {k: v for k, v in config.items() if not k.startswith(GEOLOCATION_PREFIX)}


# Canvas readback is covered (random data, as with resistFingerprinting) when this pref names the
# canvas targets. Camoufox decides it per launch, and differently on a relaunch: a pinned GPU makes
# it cover a Linux identity that the first launch left uncovered. The pin keeps the decision and
# every launch sets the pref outright, because a persistent profile keeps a pref that was set once.
CANVAS_PREF = "privacy.baselineFingerprintingProtection.overrides"
CANVAS_TARGETS = ("+CanvasImageExtractionPrompt", "+CanvasExtractionBeforeUserInputIsBlocked",
                  "+CanvasExtractionFromThirdPartiesIsBlocked")  # Camoufox's, in its order
CANVAS_TARGET = CANVAS_TARGETS[0]

# Camoufox 0.5.9's own numbers (launcher-constants.json), copied so a completion never depends on
# the installed version: the work area under Firefox's baseline protection is the screen less a
# fixed taskbar, and a core count is snapped down into what real desktops ship with.
TASKBAR_HEIGHT = {"windows": 48, "macos": 76, "linux": 0}
# Where the browser puts the work area whatever the config says (measured on the node, Camoufox
# 0.5.9 on Firefox 158): availLeft 0 always, availTop 25 under a macOS menu bar, else 0.
AVAIL_TOP = {"windows": 0, "macos": 25, "linux": 0}
CORE_COUNTS = (4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 28, 32)

GEO_KEYS = ("timezone", "geolocation:latitude", "geolocation:longitude")
WEBRTC_KEYS = ("webrtc:ipv4", "webrtc:ipv6")
LOCATION_MOVE_KM = 100.0
ENV_CHUNK = 32767  # Camoufox splits CAMOU_CONFIG into chunks of this many characters on Linux


@dataclass
class Pin:
    """A lane's pin: the Camoufox config, and agentos's record ({} for a version 1 pin)."""

    config: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def version(self) -> int:
        version = self.meta.get("version")
        return version if isinstance(version, int) and version >= 1 else 1

    def to_json(self) -> dict[str, Any]:
        return {**self.config, PIN_META: self.meta}


def split_pin(data: dict[str, Any], lane_id: str = "lane") -> Pin:
    meta = data.get(PIN_META)
    if meta is not None and not isinstance(meta, dict):
        log.warning("%s: the pin's %s record is not an object; reading the pin as version 1", lane_id, PIN_META)
        meta = None
    config = {k: v for k, v in data.items() if k != PIN_META}
    if not config:
        raise EngineError(f"{lane_id}: the pinned fingerprint holds no browser settings, only agentos's record. "
                          "Restore it from a backup; deleting it pins a new one, and sites will see a new device.")
    return Pin(config, dict(meta or {}))


def load_pin(fingerprint_path: Path, lane_id: str = "lane") -> Pin:
    return split_pin(read_pin(fingerprint_path, lane_id), lane_id)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
