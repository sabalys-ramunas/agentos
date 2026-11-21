"""The config a launch gives the browser: completing a pin, the launcher keys, the CAMOU_CONFIG
environment, and where the exit's location moved."""

from __future__ import annotations

import ipaddress
import json
import math
from typing import Any

from openajar.node.camoufox.pin import (
    AVAIL_TOP,
    CANVAS_PREF,
    CANVAS_TARGET,
    CORE_COUNTS,
    DRAWN_KEYS,
    ENV_CHUNK,
    GEO_KEYS,
    LAUNCHER_KEYS,
    LOCATION_MOVE_KM,
    PIN_META,
    PIN_PREFS,
    TASKBAR_HEIGHT,
    WEBRTC_KEYS,
    Pin,
    _int,
    os_of,
)


def _oscpu(platform: str) -> str | None:
    """The navigator.oscpu Firefox reports beside a navigator.platform (Camoufox's oscpu_for_platform)."""
    if platform == "MacIntel":
        return "Intel Mac OS X 10.15"
    if platform.startswith("Win"):
        return "Windows NT 10.0; Win64; x64"
    return "Linux x86_64" if "Linux" in platform or "linux" in platform else None


def _app_version(user_agent: str) -> str | None:
    """navigator.appVersion as Firefox derives it from the user agent: "5.0 (<OS tokens>)" without
    the architecture, the Gecko revision or the Windows build (Camoufox's _set_app_version)."""
    if not user_agent.startswith("Mozilla/5.0 (") or ")" not in user_agent:
        return None
    kept = []
    for token in (t.strip() for t in user_agent[len("Mozilla/5.0 ("):].split(")", 1)[0].split(";")):
        if (token.startswith(("rv:", "Linux ", "Intel Mac OS X")) or token in ("Win64", "x64", "Mobile", "Tablet")
                or not token):
            continue
        kept.append("Windows" if token.startswith("Windows") else token)
    return f"5.0 ({'; '.join(kept)})" if kept else None


def snap_cores(host_cores: int) -> int:
    """The host's core count snapped down into CORE_COUNTS, as Camoufox reports it."""
    allowed = [c for c in CORE_COUNTS if c <= host_cores]
    return allowed[-1] if allowed else CORE_COUNTS[0]


def complete_config(config: dict[str, Any], *, derived: dict[str, Any] | None = None,
                    host_cores: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The config with a value for every key Camoufox would otherwise fill at random on a launch,
    and the keys it set. Never changes a key that is present; the same input gives the same output.

    The rules describe the single-monitor desktop the rest of the pin already is, as the browser
    shows it to a page: the work area starts at the screen's left edge, under the menu bar on a
    Mac (the browser reports that origin whatever the config says, AVAIL_TOP), the window sits at
    the left edge of the work area and at its top (Camoufox never moves the real window, so x 0 is
    also where the first launch showed it), no touchscreen, and the colour depth, oscpu and
    appVersion that go with the pinned screen, platform and user agent. `derived` (only for a pin
    older than version 2) holds
    what Camoufox worked out from the pin itself on this launch: fonts, voices, media devices and
    WebGL are seeded by the pinned identity, so they are what every relaunch since the first one
    has shown, and the pin keeps them."""
    out = dict(config)
    filled: dict[str, Any] = {}

    def fill(key: str, value: Any) -> None:
        if key not in out and value is not None:
            out[key] = filled[key] = value

    target = os_of(out)
    fill("screen.availLeft", 0)
    fill("screen.availTop", AVAIL_TOP[target])
    width, height = _int(out.get("screen.width")), _int(out.get("screen.height"))
    if width and height:
        fill("screen.availWidth", width)
        fill("screen.availHeight", max(1, height - TASKBAR_HEIGHT[target]))
    fill("window.screenX", 0)
    fill("window.screenY", AVAIL_TOP[target])
    fill("navigator.maxTouchPoints", 0)
    apple_silicon = target == "macos" and "Apple M" in str(out.get("webGl:renderer") or "")
    fill("screen.colorDepth", 30 if apple_silicon else 24)
    fill("screen.pixelDepth", out.get("screen.colorDepth"))
    if isinstance(out.get("navigator.platform"), str):
        fill("navigator.oscpu", _oscpu(out["navigator.platform"]))
    if isinstance(out.get("navigator.userAgent"), str):
        fill("navigator.appVersion", _app_version(out["navigator.userAgent"]))
    if host_cores:
        fill("navigator.hardwareConcurrency", snap_cores(host_cores))
    for key in sorted(derived or {}):
        # a drawn key the rules could not set is a fresh random draw, never what a relaunch showed
        if key not in LAUNCHER_KEYS and key not in DRAWN_KEYS and key != PIN_META:
            fill(key, derived[key])
    return out, filled


def brief(values: dict[str, Any], limit: int = 80) -> dict[str, Any]:
    """Values short enough for a log line or a lane event; a long one (a font list) is summarised."""
    out: dict[str, Any] = {}
    for key, value in sorted(values.items()):
        text = json.dumps(value)
        out[key] = value if len(text) <= limit else (f"({len(value)} entries)" if isinstance(value, (list, dict))
                                                     else text[:limit] + "...")
    return out


def launch_config(pinned: dict[str, Any], resolved: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """The config the browser gets: every pinned key as pinned, the launcher keys as this launch
    resolved them. Also returns the pinned keys Camoufox changed on this launch and the keys it
    added that the pin does not have; both are left out."""
    final = {k: v for k, v in pinned.items() if k not in LAUNCHER_KEYS}
    final.update({k: resolved[k] for k in sorted(LAUNCHER_KEYS) if k in resolved})
    changed = sorted(k for k in final if k not in LAUNCHER_KEYS and k in resolved and resolved[k] != final[k])
    dropped = sorted(k for k in resolved if k not in final)
    return final, changed, dropped


def pinned_prefs(prefs: dict[str, Any]) -> dict[str, Any]:
    return {k: prefs[k] for k in PIN_PREFS if k in prefs}


def canvas_covered(prefs: dict[str, Any]) -> bool:
    return CANVAS_TARGET in str(prefs.get(CANVAS_PREF) or "")


def has_location(config: dict[str, Any]) -> bool:
    """Whether a pin holds the whole location Camoufox's geoip derives (so a launch need not)."""
    return all(k in config for k in GEO_KEYS) and any(k in config for k in WEBRTC_KEYS)


def config_of_env(env: dict[str, Any]) -> dict[str, Any]:
    """The config in a set of launch options' CAMOU_CONFIG_<n> environment variables."""
    blob = "".join(v for _, v in sorted((int(k.rsplit("_", 1)[1]), str(v)) for k, v in env.items()
                                        if k.startswith("CAMOU_CONFIG_")))
    return json.loads(blob) if blob else {}


def with_config(env: dict[str, Any], config: dict[str, Any], chunk: int = ENV_CHUNK) -> dict[str, Any]:
    """`env` with CAMOU_CONFIG_<n> replaced by `config`, chunked the way Camoufox chunks it."""
    out = {k: v for k, v in env.items() if not k.startswith("CAMOU_CONFIG_")}
    blob = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    for i in range(0, len(blob), chunk):
        out[f"CAMOU_CONFIG_{i // chunk + 1}"] = blob[i:i + chunk]
    return out


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (lat1, lon1), (lat2, lon2) = (tuple(map(math.radians, p)) for p in (a, b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def location_change(pinned: dict[str, Any], current: dict[str, Any]) -> str | None:
    """How the exit's location moved from the pinned one, or None. Compares the timezone, the
    country and the distance (over LOCATION_MOVE_KM); a value either side lacks is not compared."""
    moves = []
    if pinned.get("timezone") and current.get("timezone") and pinned["timezone"] != current["timezone"]:
        moves.append(f"timezone {pinned['timezone']} to {current['timezone']}")
    if pinned.get("country") and current.get("country") and pinned["country"] != current["country"]:
        moves.append(f"country {pinned['country']} to {current['country']}")
    try:
        km = distance_km((float(pinned["latitude"]), float(pinned["longitude"])),
                         (float(current["latitude"]), float(current["longitude"])))
    except (KeyError, TypeError, ValueError):
        km = 0.0
    if km > LOCATION_MOVE_KM:
        moves.append(f"{km:.0f} km away")
    return ", ".join(moves) or None


def pinned_location(pin: Pin) -> dict[str, Any]:
    geo = pin.meta.get("geo") if isinstance(pin.meta.get("geo"), dict) else {}
    ip = geo.get("ip") or next((pin.config[k] for k in WEBRTC_KEYS if pin.config.get(k)), None)
    return {"ip": ip, "country": geo.get("country"), "timezone": pin.config.get("timezone"),
            "latitude": pin.config.get("geolocation:latitude"), "longitude": pin.config.get("geolocation:longitude")}


def _ip_family(ip: Any) -> int | None:
    try:
        return ipaddress.ip_address(str(ip)).version
    except ValueError:
        return None
