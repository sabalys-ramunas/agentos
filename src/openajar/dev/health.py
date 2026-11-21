"""What `openajar dev up --seed` gives lane health (openajar.stealth.health) to show.

The dev lanes live in memory, but the lane check reads their folders: without a profile folder every
lane would fail its profile check. So each seeded lane gets an empty profile folder and a pinned
fingerprint of its own (a Linux computer, a different one per lane, pinned some weeks ago), and the
lane manager gets the holds a real lane would have recorded:

    biz-a/social   a captcha 3 days ago (handed back) and a rate limit 9 days ago   -> Check this
    biz-a/shop     nothing                                                           -> Looks normal
    biz-b/social   the seed's captcha holds it for a person now                     -> Stop
    biz-b/forum    a person has control now                                          -> Check this

Nothing here starts a browser or leaves this machine.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openajar.stealth.lanecheck import PIN_META, PIN_VERSION

FIREFOX = 158                   # what the dev lanes report (openajar.dev.lanes device_info)
PIN_AGE_DAYS = {"biz-a/social": 64, "biz-a/shop": 41, "biz-b/social": 37, "biz-b/forum": 23}
# (cores, screen, window, GPU, extra font): one computer per lane, so the lanes never pin one device
COMPUTERS = (
    (8, (1920, 1080), (1920, 1040), "AMD Radeon RX 580 (radeonsi, polaris10, LLVM 15.0.7, DRM 3.49), or similar",
     "Cantarell"),
    (4, (1366, 768), (1366, 728), "Mesa Intel(R) UHD Graphics 620 (KBL GT2), or similar", "Ubuntu"),
    (12, (2560, 1440), (2560, 1400), "NVIDIA GeForce GTX 1660/PCIe/SSE2, or similar", "FreeSans"),
    (16, (1600, 900), (1600, 860), "Mesa Intel(R) Iris(R) Xe Graphics (TGL GT2), or similar", "Ubuntu Mono"),
)


def pin_for(index: int, *, firefox: int, pinned_at: datetime) -> dict[str, Any]:
    """A consistent Linux computer, the `index`th of COMPUTERS, as openajar would pin it."""
    cores, (width, height), (outer_w, outer_h), gpu, font = COMPUTERS[index % len(COMPUTERS)]
    ua = f"Mozilla/5.0 (X11; Linux x86_64; rv:{firefox}.0) Gecko/20100101 Firefox/{firefox}.0"
    return {
        "navigator.userAgent": ua, "headers.User-Agent": ua, "navigator.platform": "Linux x86_64",
        "navigator.oscpu": "Linux x86_64", "navigator.appVersion": "5.0 (X11)",
        "navigator.hardwareConcurrency": cores, "navigator.maxTouchPoints": 0,
        "screen.width": width, "screen.height": height, "screen.availWidth": width,
        "screen.availHeight": outer_h, "screen.availLeft": 0, "screen.availTop": 0, "screen.colorDepth": 24,
        "screen.pixelDepth": 24, "window.outerWidth": outer_w, "window.outerHeight": outer_h, "window.screenX": 0,
        "window.screenY": 0, "webGl:vendor": "Mesa", "webGl:renderer": gpu,
        "fonts": ["Arial", "DejaVu Sans", "Liberation Serif", font], "timezone": "Europe/Vilnius",
        "locale:language": "en", "locale:region": "US", "headers.Accept-Language": "en-US,en;q=0.5",
        PIN_META: {"version": PIN_VERSION, "pinned_at": pinned_at.isoformat(timespec="seconds"),
                   "camoufox": f"{firefox}.0.1", "geo": {}},
    }


def installed_firefox(home: Path) -> int:
    """The installed Camoufox's Firefox version, as the lane check reads it, so the dev pins match it
    (FIREFOX when none is installed: the check then cannot compare, and says so)."""
    from openajar.core.system import Host
    from openajar.node.fingerprint import browser_major

    cache = Path(home) / ".cache"
    try:
        info = Host().camoufox(cache if (cache / "camoufox").exists() else None)
    except Exception:  # noqa: BLE001 - no Camoufox here is the usual laptop
        return FIREFOX
    return browser_major(info.get("verstr")) or FIREFOX


def write_lane_files(lane_dir: Path, pin: dict[str, Any]) -> None:
    """An empty profile folder and the pin, as a lane has after its first launch. A pin already there
    (a kept --home) is left as it is."""
    profile = lane_dir / "profile"
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = lane_dir / "fingerprint.json"
    if path.exists():
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(pin, f, indent=2, sort_keys=True)


def past_holds(now: datetime) -> dict[str, list[dict[str, Any]]]:
    """The holds the node recorded before this start, per lane, as lane events."""
    captcha = now - timedelta(days=3, hours=4)
    rested = now - timedelta(days=9, hours=2)
    return {"biz-a/social": [
        {"lane_id": "biz-a/social", "kind": "held", "at": rested,
         "detail": {"reason": "backoff:rate_limited", "until": rested + timedelta(hours=6), "by": "node"}},
        {"lane_id": "biz-a/social", "kind": "hold_cleared", "at": rested + timedelta(hours=6),
         "detail": {"reason": "backoff:rate_limited", "by": "hold-expired"}},
        {"lane_id": "biz-a/social", "kind": "held", "at": captcha,
         "detail": {"reason": "needs_human:captcha on social.example/login", "until": None, "by": "node"}},
        {"lane_id": "biz-a/social", "kind": "hold_cleared", "at": captcha + timedelta(minutes=12),
         "detail": {"reason": "needs_human:captcha on social.example/login", "by": "owner"}},
    ]}


def seed_health(lanes: Any, lane_ids: list[str], *, now: datetime | None = None) -> None:
    """Files and past holds for the seeded lanes `lane_ids` (those this start created)."""
    now = now or datetime.now(UTC)
    firefox = installed_firefox(Path(lanes.config.home))
    order = sorted(PIN_AGE_DAYS)
    for lane_id in lane_ids:
        index = order.index(lane_id) if lane_id in order else len(order)
        pinned_at = now - timedelta(days=PIN_AGE_DAYS.get(lane_id, 30), hours=5)
        try:
            write_lane_files(lanes.config.lane_dir(lane_id), pin_for(index, firefox=firefox, pinned_at=pinned_at))
        except OSError:
            continue
    held = past_holds(now)
    for lane_id in lane_ids:
        lanes.events_log.extend(held.get(lane_id, []))
    lanes.events_log.sort(key=lambda e: e["at"])  # oldest first, as they were written
