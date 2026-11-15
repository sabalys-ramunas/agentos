"""Camoufox engine: one AsyncCamoufox persistent context (one Firefox process) per lane.

The fingerprint is pinned on first launch and every later launch is the same device. Camoufox resolves an identity before each launch
(launch_options), and on a relaunch it still draws a fresh fingerprint to fill whatever the pin
lacks. So the engine resolves the options itself, completes a new identity before the browser
first starts, and hands the browser exactly the pinned config on every launch after that; a pin
made before this (version 1) is completed once on its next launch. The browser's own
CAMOU_CONFIG_* (in /proc/<pid>/environ) is read back to check it got that config.

camoufox and playwright are imported inside the functions that use them, never at module
import time.

A session tracks every page of its context (PageTracker): popups and new tabs become the page
the lane acts in, dialogs are owned by a DialogGuard, and downloads land in the lane outbox
(`<lane dir>/files/outbox`, next to the profile).

Signed agent identity (agentos.webbotauth): with an `identity` provider, a launch installs the
lane's Web Bot Auth signing route, and only when the lane lists hosts to sign for. An empty list
installs no route at all, because any route makes Firefox pause every request of the context and
stop storing responses in its HTTP cache (docs/design/egress-guard.md).

Every session has one RouteDispatcher (agentos.node.routes), the only owner of the context's
routes, and one LaneGuard (agentos.node.egress), the lane's egress guard. The guard listens to
requests and needs no route; on a strict lane it adds a route through the dispatcher, ahead of
the signer, only while an agent acts.

The engine's parts live beside this module: procs (the browser's processes), pin (the pin file and
its model), config (the config a launch gives the browser), launch (LaunchPlan and the Camoufox
lookups), window (the window's size and place), prefs, pages (PageTracker) and session
(CamoufoxSession).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentos.core import clock
from agentos.core.aio import run_to_end
from agentos.core.errors import AgentosError, EngineError
from agentos.core.models import LaneSpec, ProxyConfig
from agentos.node import human, xkeyboard
from agentos.node.browser_output import (
    browser_error,
    capture_driver_stderr,
    forget_browser,
    watch_browser,
    watch_profile,
)
from agentos.node.camoufox.config import (
    _app_version,  # noqa: F401  (served at the old path)
    _ip_family,
    _oscpu,  # noqa: F401  (served at the old path)
    brief,
    canvas_covered,
    complete_config,
    config_of_env,
    distance_km,  # noqa: F401  (served at the old path)
    has_location,
    launch_config,
    location_change,
    pinned_location,
    pinned_prefs,
    snap_cores,  # noqa: F401  (served at the old path)
    with_config,
)
from agentos.node.camoufox.launch import (
    CamoufoxGeo,
    ExitGeo,
    LaunchPlan,
    _camoufox_version,
    _pref_env,
    _resolve_options,
    installed_browser_version,
)
from agentos.node.camoufox.pages import (
    PageTracker,  # noqa: F401  (served at the old path)
    _first_line,
    forget_context,  # noqa: F401  (served at the old path)
    tracker_for,  # noqa: F401  (served at the old path)
)
from agentos.node.camoufox.pin import (
    AVAIL_TOP,  # noqa: F401  (served at the old path)
    CANVAS_PREF,
    CANVAS_TARGETS,
    DRAWN_KEYS,
    GEO_PREF,
    GEOLOCATION_PREFIX,  # noqa: F401  (served at the old path)
    HARDENING,
    LAUNCHER_KEYS,
    MESA_MAX_SIZE,  # noqa: F401  (served at the old path)
    PIN_META,
    PIN_VERSION,
    QUOTA_10_GIB_KB,  # noqa: F401  (served at the old path)
    QUOTA_PREF,  # noqa: F401  (served at the old path)
    SOFTWARE_HOST_GPUS,
    WEBGL_SIZE_KEYS,  # noqa: F401  (served at the old path)
    WEBRTC_KEYS,
    Pin,
    backup_pin,
    browser_view,
    harden,
    hardened,
    load_pin,
    os_of,
    parse_camou_config,
    read_pin,
    split_pin,  # noqa: F401  (served at the old path)
    write_private_bytes,  # noqa: F401  (served at the old path)
    write_private_json,
)
from agentos.node.camoufox.prefs import (
    CACHE_PREFS,
    LINUX_HOST_PREFS,
    PRIVATE_NET_PAC,
    STEADY_CHROME_PREFS,
    WEBRTC_PROXY_PREFS,
    WHEEL_PREFS,
)
from agentos.node.camoufox.procs import (
    BROWSER_BIN,  # noqa: F401  (served at the old path)
    _is_browser,  # noqa: F401  (served at the old path)
    browser_pids,
    browser_pids_under,
)
from agentos.node.camoufox.session import CamoufoxSession
from agentos.node.camoufox.window import display_size_of, place_window, window_position_of, window_size_of
from agentos.node.display import kill_processes
from agentos.node.egress import TRACE_NAME as EGRESS_TRACE_NAME
from agentos.node.egress import EgressTrace, LaneGuard
from agentos.node.files import FilesConfig, LaneFiles
from agentos.node.interaction.dialogs import DIALOG_TIMEOUT

log = logging.getLogger("agentos.engine.camoufox")


def read_browser_config(pid: int) -> dict[str, Any]:
    return parse_camou_config(Path(f"/proc/{pid}/environ").read_bytes())


def _host_cores() -> int | None:
    try:
        return len(os.sched_getaffinity(0)) or None
    except (AttributeError, OSError):
        return os.cpu_count()


# ---- engine -----------------------------------------------------------------------

# Lanes whose browser is a desktop window filling the screen when its identity is drawn (the first
# launch): an agent environment's own browser (agentos.environments, workspace "env"). Its pages are
# the agent's work, checked at a desktop width, so a drawn half-screen window (676 px wide, which
# shows every page's phone layout) would make each check and screenshot wrong. An account lane keeps
# the window its identity draws, as a real person's browser has.
DESKTOP_WORKSPACES = frozenset({"env"})


class CamoufoxEngine:
    name = "camoufox"

    def __init__(
        self,
        *,
        screen: tuple[int, int] = (1920, 1080),
        launch_timeout: float = 120.0,
        close_timeout: float = 15.0,
        extra_options: dict[str, Any] | None = None,
        files: FilesConfig | None = None,
        dialog_timeout: float = DIALOG_TIMEOUT,
        geo: ExitGeo | None = None,
        resolve: Callable[..., dict[str, Any]] | None = None,
        identity: Any = None,
        identity_os: list[str] | None = None,
        browser_version: Callable[[], str | None] | None = None,
    ):
        self.screen = screen
        # The OS a new lane's identity claims (Camoufox's `os`: windows, macos, linux), drawn on its
        # first launch only; every later launch replays the pin. None: Camoufox's own mix.
        self.identity_os = list(identity_os) if identity_os else None
        # Xvfb has no GPU path, so a Linux node renders WebGL with llvmpipe unless Camoufox's
        # virtual-gpu display is in use (extra_options headless="virtual-gpu").
        self.software_rendering = sys.platform.startswith("linux") and \
            (extra_options or {}).get("headless") != "virtual-gpu"
        self.launch_timeout = launch_timeout
        self.close_timeout = close_timeout
        self.identity = identity  # webbotauth.route.IdentityProvider, or None: no lane signs
        self.extra_options = dict(extra_options or {})
        self.files = files or FilesConfig()
        self.dialog_timeout = dialog_timeout
        self.geo: ExitGeo = geo or CamoufoxGeo()
        self.resolve = resolve or _resolve_options  # Camoufox's launch_options (tests pass a fake)
        # The installed browser's version, for the Firefox version bump (bump_firefox). A fake
        # resolve has no real browser behind it, so its version is unknown unless a test gives one.
        self.browser_version = browser_version or (
            (lambda: installed_browser_version(self.extra_options.get("executable_path")))
            if resolve is None else (lambda: None))

    def options(self, spec: LaneSpec, *, profile_dir: Path, fingerprint_path: Path, display: str,
                proxy: ProxyConfig | None, xauthority: str | None = None) -> dict[str, Any]:
        """AsyncCamoufox launch options for one lane (separate so tests can check them)."""
        pin = load_pin(fingerprint_path, spec.id) if fingerprint_path.exists() else None
        return self._options(spec, profile_dir=profile_dir, display=display, proxy=proxy, pin=pin,
                             xauthority=xauthority)

    def _options(self, spec: LaneSpec, *, profile_dir: Path, display: str, proxy: ProxyConfig | None,
                 pin: Pin | None, xauthority: str | None = None) -> dict[str, Any]:
        from camoufox.fingerprints import Screen

        # launch_options() uses `env` instead of os.environ (it does not merge), so pass a
        # full copy pointed at the lane's display. X11 only: Xvfb has no Wayland.
        env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
        env.update(DISPLAY=display, GDK_BACKEND="x11", MOZ_ENABLE_WAYLAND="0")
        if xauthority:  # the lane's Xvfb takes only clients that hold its cookie
            env["XAUTHORITY"] = xauthority
        opts: dict[str, Any] = {
            "persistent_context": True,
            "user_data_dir": str(profile_dir),
            "headless": False,
            "geoip": True,
            "humanize": True,
            "locale": spec.locale,
            "enable_cache": True,
            "i_know_what_im_doing": True,
            "env": env,
            # Bound any generated geometry to the Xvfb screen. With a pinned config the
            # screen comes from the pin and this bound has nothing left to fill.
            "screen": Screen(max_width=self.screen[0], max_height=self.screen[1]),
        }
        # Firefox sends localhost straight past a proxy, so a page in one lane could reach the
        # node's own services (live viewers, the MCP port). Proxied lanes send localhost through
        # the proxy too (it cannot reach the node's loopback); lanes without a proxy get a PAC
        # that blackholes loopback, private and link-local ranges.
        opts["firefox_user_prefs"] = {**CACHE_PREFS, **STEADY_CHROME_PREFS, **WHEEL_PREFS,
                                      **(LINUX_HOST_PREFS if sys.platform.startswith("linux") else {}),
                                      **self._network_guard_prefs(proxied=bool(proxy), pac_dir=profile_dir.parent),
                                      CANVAS_PREF: ""}
        if proxy:
            p = {"server": proxy.server}
            if proxy.username:
                p["username"] = proxy.username
            if proxy.password:
                p["password"] = proxy.password
            opts["proxy"] = p
            if getattr(proxy, "scheme", "http") == "socks5":
                # an environment's tunnel (agentos.environments): names are looked up on its VM too, so
                # localhost and the VM's own names are the VM's
                opts["firefox_user_prefs"]["network.proxy.socks_remote_dns"] = True
        if pin is None and spec.workspace in DESKTOP_WORKSPACES:
            width, height = self.screen
            opts["screen"] = Screen(min_width=width, max_width=width, min_height=height, max_height=height)
            opts["window"] = (width, height)  # Camoufox fits it to the screen's available area
        if pin is None and self.identity_os:
            opts["os"] = list(self.identity_os)
            if self.identity_os == ["linux"] and self.software_rendering:
                opts["webgl_config"] = random.choice(SOFTWARE_HOST_GPUS)
        if pin is not None:
            # A version 1 pin goes in exactly as stored: Camoufox seeds fonts, voices, media devices
            # and WebGL from a hash of the config it is given, so this is what reproduces what every
            # relaunch has shown, and its completion keeps those values.
            opts.update(config=dict(pin.config), os=os_of(pin.config))
            if has_location(pin.config):
                opts["geoip"] = False  # the pinned location stands; plan_launch checks the exit
            if isinstance(pin.meta.get("canvas_noise"), bool):
                opts["canvas_noise"] = pin.meta["canvas_noise"]
            prefs = pin.meta.get("prefs")
            if isinstance(prefs, dict):
                opts["firefox_user_prefs"].update(pinned_prefs(prefs))
        opts.update(self.extra_options)
        return opts

    def window_size(self, fingerprint_path: Path) -> tuple[int, int] | None:
        """The lane's browser window size from its pin, or None before the first launch or for a
        pin without one."""
        try:
            return window_size_of(read_pin(fingerprint_path)) if fingerprint_path.exists() else None
        except (OSError, EngineError):
            return None

    def display_size(self, fingerprint_path: Path) -> tuple[int, int] | None:
        """The size the lane's display is made: the pinned screen (display_size_of), or None before
        the first launch."""
        try:
            return display_size_of(read_pin(fingerprint_path)) if fingerprint_path.exists() else None
        except (OSError, EngineError):
            return None


    def _network_guard_prefs(self, *, proxied: bool, pac_dir: Path) -> dict[str, Any]:
        webrtc = dict(WEBRTC_PROXY_PREFS) if proxied else {}
        if getattr(self, "allow_private_network", False):
            return webrtc
        prefs: dict[str, Any] = {
            "network.proxy.allow_hijacking_localhost": True,
            "network.proxy.no_proxies_on": "",
            **webrtc,
        }
        if not proxied:
            # Firefox does not load a PAC from a data: URL (it then fails closed and blocks
            # everything), so the PAC is a file next to the profile.
            pac = pac_dir / "network-guard.pac"
            if not pac.exists() or pac.read_text() != PRIVATE_NET_PAC:
                pac.write_text(PRIVATE_NET_PAC)
                pac.chmod(0o644)
            prefs["network.proxy.type"] = 2
            prefs["network.proxy.autoconfig_url"] = pac.resolve().as_uri()
        return prefs

    # ---- one launch -----------------------------------------------------------------------

    def plan_launch(self, lane_id: str, opts: dict[str, Any], pin: Pin | None) -> LaunchPlan:
        """Resolve one launch with Camoufox and settle what it would leave to chance. Blocking (it
        can look up the exit IP and read the GeoIP database), so launch() runs it in a thread.

        First launch: the identity Camoufox draws is completed before the browser ever shows it,
        and that is the pin. Every later launch: the browser gets exactly the pinned config, with
        the pinned prefs; a pin older than version 2, or one that lacks a key, is completed on this
        launch (once) and rewritten after the browser has started with it."""
        kwargs = {k: v for k, v in opts.items() if k != "persistent_context"}
        kwargs.setdefault("pin_cpu_cores", False)  # AsyncNewBrowser's own default
        bumped = self.bump_firefox(lane_id, pin)
        if bumped is not None:  # Camoufox is given the new version, so what it resolves agrees
            pin, update = bumped
            kwargs["config"] = dict(pin.config)
        resolved = dict(self.resolve(**kwargs))
        drawn = config_of_env(resolved.get("env") or {})
        prefs = dict(resolved.get("firefox_user_prefs") or {})
        identity = {k: v for k, v in drawn.items() if k not in LAUNCHER_KEYS}
        notes: list[tuple[str, dict[str, Any]]] = []
        new_pin: dict[str, Any] | None = None
        if pin is None:
            config, filled = complete_config(identity, host_cores=_host_cores())
            config, prefs, hardening = harden(config, prefs, host_cores=_host_cores())
            meta: dict[str, Any] = {
                "version": PIN_VERSION, "pinned_at": clock.iso(z=True, timespec="seconds"), "camoufox": _camoufox_version(),
                "prefs": pinned_prefs(prefs), "canvas_noise": canvas_covered(prefs),
                "geo": self._exit_record(config), "filled": sorted(filled), "hardening": HARDENING}
            new_pin = {**config, PIN_META: meta}
            self._set_prefs(resolved, prefs)
            notes.append(("fingerprint_pinned", {"os": os_of(config), "keys": len(config), "filled": brief(filled),
                                                 "hardened": brief(hardening)}))
        elif pin.version > PIN_VERSION:
            log.warning("%s: the pin is version %d, newer than this agentos understands (%d); it is used as it is",
                        lane_id, pin.version, PIN_VERSION)
            config, meta = pin.config, pin.meta
        else:
            legacy = pin.version < PIN_VERSION
            config, filled = complete_config(pin.config, derived=identity if legacy else None,
                                             host_cores=_host_cores())
            meta = dict(pin.meta)
            if filled or legacy or any(k not in meta for k in ("prefs", "canvas_noise", "geo")):
                earlier = meta.get("filled") if isinstance(meta.get("filled"), list) else []
                meta.update(version=PIN_VERSION, completed_at=clock.iso(z=True, timespec="seconds"), completed_from=pin.version,
                            camoufox=meta.get("camoufox") or _camoufox_version(),
                            filled=sorted({*map(str, earlier), *filled}))
                meta.setdefault("prefs", pinned_prefs(prefs))
                meta.setdefault("canvas_noise", canvas_covered(prefs))
                meta.setdefault("geo", self._exit_record(config))
                new_pin = {**config, PIN_META: meta}
                notes.append(("fingerprint_completed", {"from_version": pin.version, "filled": brief(filled)}))
        backup_label = None
        if bumped is not None:
            if new_pin is None:
                backup_label = f"firefox-{update['from']}"
            new_pin = {**config, PIN_META: meta}
            notes.append(("fingerprint_version_bumped", dict(update)))
        missing = [k for k in DRAWN_KEYS if k not in config]
        if missing:
            log.warning("%s: the pin has no %s and no rule can set it without inventing a device; the browser "
                        "shows its own value", lane_id, ", ".join(missing))

        final, changed, dropped = launch_config(config, drawn)
        if pin is not None:
            if changed:
                log.warning("%s: Camoufox changed the pinned %s on this launch; the pinned values are used",
                            lane_id, ", ".join(changed))
            if dropped:
                log.info("%s: left out %s, which Camoufox drew on this launch and the pin does not hold",
                         lane_id, ", ".join(dropped))
            if opts.get("geoip") is False:
                self._check_exit(lane_id, Pin(config, meta), final, opts.get("proxy"), notes)
            self._hold_prefs(lane_id, meta, prefs, resolved)
        final = browser_view(final, meta)
        resolved["env"] = with_config(resolved.get("env") or {}, final)
        return LaunchPlan(resolved, final, first=pin is None, pin=new_pin,
                          old_version=pin.version if pin is not None else None, notes=notes,
                          backup_label=backup_label)

    def bump_firefox(self, lane_id: str, pin: Pin | None) -> tuple[Pin, dict[str, Any]] | None:
        """The pin with its Firefox version moved to the installed browser's, as a real Firefox
        updates itself, or None when there is nothing to move: no pin, a version 1 pin (it is
        completed from the draws it has always shown first, and moved on its next launch), a pin
        newer than this agentos, an installed version that is unknown, the same or older (never
        moved back: `agentos lane audit` fails that). Only node.fingerprint.VERSION_KEYS change (the
        rv: and Firefox/ tokens of the user agent and its header); oscpu, platform and the whole
        device stay. The move is recorded in the pin's firefox_updates."""
        from agentos.node.fingerprint import browser_major, bump, pinned_major

        if pin is None or pin.version != PIN_VERSION:
            return None
        verstr = self.browser_version()
        installed, pinned = browser_major(verstr), pinned_major(pin.config)
        if installed is None or pinned is None or installed <= pinned:
            return None
        config, changed = bump(pin.config, installed)
        if not changed:
            return None
        update = {"from": pinned, "to": installed, "browser": verstr, "keys": changed, "at": clock.iso(z=True, timespec="seconds")}
        earlier = pin.meta.get("firefox_updates") if isinstance(pin.meta.get("firefox_updates"), list) else []
        log.info("%s: Camoufox is now Firefox %d; the pin's Firefox %d moves with it (%s), as a real Firefox "
                 "updates itself; the device stays", lane_id, installed, pinned, ", ".join(changed))
        return Pin(config, {**pin.meta, "firefox_updates": [*earlier, update]}), update

    def _exit_record(self, config: dict[str, Any]) -> dict[str, Any]:
        """The exit IP a pin's location came from (the WebRTC address Camoufox set from it), and
        that IP's country from the GeoIP database."""
        ip = next((config[k] for k in WEBRTC_KEYS if config.get(k)), None)
        if not ip:
            return {}
        try:
            return {"ip": ip, "country": self.geo.locate(str(ip)).get("country")}
        except Exception as e:  # noqa: BLE001 - the country is only for a later comparison
            log.info("could not look up the country of %s: %s", ip, _first_line(e))
            return {"ip": ip, "country": None}

    def _check_exit(self, lane_id: str, pin: Pin, final: dict[str, Any], proxy: dict[str, str] | None,
                    notes: list[tuple[str, dict[str, Any]]]) -> None:
        """The pinned location is never derived again. Warn when the exit has moved away from it,
        and let WebRTC report a changed exit IP (it is the network address, not the device)."""
        pinned = pinned_location(pin)
        try:
            ip = self.geo.exit_ip(proxy)
            here = self.geo.locate(ip)
        except Exception as e:  # noqa: BLE001 - never stop a launch over the comparison
            log.warning("%s: could not look up where the exit is (%s); the pinned location is used", lane_id,
                        _first_line(e))
            return
        moved = location_change(pinned, here)
        if moved:
            log.warning("%s: the exit is no longer where the pin says (%s). The lane keeps its pinned timezone and "
                        "location; check the proxy, or give the account a lane made in the new place",
                        lane_id, moved)
            notes.append(("fingerprint_location_changed", {"pinned": pinned, "exit": here, "change": moved}))
        if pinned["ip"] and ip != pinned["ip"]:
            key = {4: "webrtc:ipv4", 6: "webrtc:ipv6"}.get(_ip_family(ip) or 0)
            if key and key in final:
                final[key] = ip
                log.warning("%s: the exit IP is %s, not the pinned %s; WebRTC reports the new exit (what sites see), "
                            "every other pinned value stays", lane_id, ip, pinned["ip"])
            else:
                log.warning("%s: the exit IP is %s, not the pinned %s; WebRTC keeps the pinned address", lane_id,
                            ip, pinned["ip"])
            notes.append(("fingerprint_exit_ip_changed", {"pinned": pinned["ip"], "exit": ip}))

    def _hold_prefs(self, lane_id: str, meta: dict[str, Any], prefs: dict[str, Any],
                    resolved: dict[str, Any]) -> None:
        """Make the launch carry the pinned prefs and canvas decision (they went in as options, so
        this only acts if Camoufox overrode one)."""
        want = pinned_prefs(meta.get("prefs") if isinstance(meta.get("prefs"), dict) else {})
        canvas = meta.get("canvas_noise")
        if isinstance(canvas, bool) and canvas_covered(prefs) != canvas:
            want[CANVAS_PREF] = ",".join(CANVAS_TARGETS) if canvas else ""
        fixed = {k: v for k, v in want.items() if prefs.get(k) != v}
        if not fixed:
            return
        # Camoufox allows location whenever the config it is given holds a position, which a hardened
        # pin does (the browser never gets it): putting the pinned "ask" back is the plan, not news.
        news = sorted(k for k in fixed if not (k == GEO_PREF and hardened(meta)))
        if news:
            log.warning("%s: Camoufox changed the pinned prefs %s on this launch; the pinned values are used",
                        lane_id, ", ".join(news))
        prefs.update(fixed)
        self._set_prefs(resolved, prefs)

    @staticmethod
    def _set_prefs(resolved: dict[str, Any], prefs: dict[str, Any]) -> None:
        """Make this launch carry `prefs`: in the options and in Camoufox's environment."""
        resolved["firefox_user_prefs"] = prefs
        env = {k: v for k, v in (resolved.get("env") or {}).items() if not k.startswith("CAMOU_PREFS_")}
        resolved["env"] = {**env, **_pref_env(prefs)}

    def settle_pin(self, lane_id: str, fingerprint_path: Path, plan: LaunchPlan, pid: int | None) -> None:
        """After the browser started: check that it carries the planned config, then write the pin
        (a first launch, or a completion, which keeps the old pin beside it). A first launch the
        browser cannot be checked for fails; a completion that cannot be checked is left for the
        next launch, and the pin stays as it was."""
        running: dict[str, Any] | None = None
        if pid is not None:
            try:
                running = read_browser_config(pid)
            except (OSError, EngineError, ValueError) as e:
                log.warning("%s: could not read the browser's config back: %s", lane_id, _first_line(e))
        expected = json.loads(json.dumps(plan.config))
        differs = [] if running is None else sorted(k for k in expected.keys() | running.keys()
                                                    if expected.get(k) != running.get(k))
        if plan.pin is None:
            if differs:
                log.warning("%s: the browser runs with a config other than the pinned one (%s)", lane_id,
                            ", ".join(differs[:10]))
            else:
                log.info("%s: launched with the pinned fingerprint", lane_id)
            return
        if running is None or differs:
            why = (f"it differs in {', '.join(differs[:10])}" if differs
                   else "the browser process was not found in /proc")
            if plan.first:
                raise EngineError(f"the browser's fingerprint could not be checked: {why}")
            log.warning("%s: the pin was not updated on this launch (%s); it stays as it was", lane_id, why)
            plan.notes[:] = [n for n in plan.notes if n[0] not in ("fingerprint_completed", "fingerprint_version_bumped")]
            return
        meta = plan.pin.get(PIN_META, {})
        if plan.first:
            try:
                write_private_json(fingerprint_path, plan.pin, exclusive=True)
            except FileExistsError:
                # Another writer pinned this computer while the browser started (a device's pin
                # taken from a lane). That pin wins; this browser shows a draw nothing pinned.
                raise EngineError("a pin was written while this first launch started, so this browser shows a "
                                  "fingerprint nothing pinned; it was closed. Open the lane again to launch with "
                                  "the pin") from None
            log.info("%s: fingerprint pinned on first launch (%d keys; set %s, which Camoufox would draw again on "
                     "each launch)", lane_id, len(plan.pin) - 1, ", ".join(meta.get("filled") or []) or "nothing")
            return
        backup = backup_pin(fingerprint_path, plan.old_version or 1, label=plan.backup_label)
        write_private_json(fingerprint_path, plan.pin)
        for kind, detail in plan.notes:
            if kind in ("fingerprint_completed", "fingerprint_version_bumped"):
                detail["backup"] = str(backup)
        completed = next((d for kind, d in plan.notes if kind == "fingerprint_completed"), None)
        if completed is not None:
            log.info("%s: completed the pinned fingerprint (version %d to %d): filled %s; the old pin is kept at %s",
                     lane_id, plan.old_version or 1, PIN_VERSION,
                     ", ".join(f"{k}={json.dumps(v)}" for k, v in completed["filled"].items()) or "nothing", backup)
        bumped = next((d for kind, d in plan.notes if kind == "fingerprint_version_bumped"), None)
        if bumped is not None:
            log.info("%s: the pin now says Firefox %d (was %d); the old pin is kept at %s", lane_id, bumped["to"],
                     bumped["from"], backup)

    async def launch(
        self,
        spec: LaneSpec,
        *,
        profile_dir: Path,
        fingerprint_path: Path,
        display: str,
        xauthority: str | None = None,
        proxy: ProxyConfig | None,
    ) -> CamoufoxSession:
        from camoufox.async_api import AsyncNewBrowser
        from playwright.async_api import async_playwright

        profile_dir.mkdir(parents=True, exist_ok=True)
        stale = browser_pids(profile_dir, main_only=False)
        if stale:
            log.warning("%s: %d camoufox-bin processes already hold this profile; stopping them", spec.id,
                        len(stale))
            await kill_processes(stale)
        pin = load_pin(fingerprint_path, spec.id) if fingerprint_path.exists() else None
        opts = self._options(spec, profile_dir=profile_dir, display=display, proxy=proxy, pin=pin,
                             xauthority=xauthority)

        async def start() -> tuple[LaunchPlan, Any]:
            plan = await asyncio.to_thread(self.plan_launch, spec.id, opts, pin)
            return plan, await AsyncNewBrowser(pw, from_options=plan.options, persistent_context=True)

        capture_driver_stderr()  # the driver's stderr through the log scrubbers, not straight to the journal
        watch_profile(profile_dir, spec.id)  # and the browser's own output, with the lane's id
        pw = await async_playwright().start()
        try:
            plan, ctx = await asyncio.wait_for(start(), self.launch_timeout)
        except BaseException as e:
            await _stop_quietly(pw)
            await kill_processes(browser_pids(profile_dir, main_only=False), grace=1.0)
            forget_browser(None, profile_dir)
            if isinstance(e, (AgentosError, asyncio.CancelledError)):
                raise
            if isinstance(e, TimeoutError):
                raise EngineError(f"{spec.id}: the browser did not start within {self.launch_timeout:.0f}s") from e
            raise EngineError(f"{spec.id}: the browser failed to start: {browser_error(e, spec.id)}") from e

        # The browser runs from here on: whatever happens next (a step fails, the open is cancelled),
        # it is closed before the error goes up. The lane has no session yet, so nobody else would.
        session: CamoufoxSession | None = None
        try:
            session = CamoufoxSession(pw, ctx, profile_dir, None, self.close_timeout,
                                      files=LaneFiles(profile_dir.parent / "files", self.files),
                                      dialog_timeout=self.dialog_timeout)
            await self._finish_launch(spec, session, plan, profile_dir=profile_dir,
                                      fingerprint_path=fingerprint_path, display=display,
                                      xauthority=xauthority, proxied=proxy is not None)
        except BaseException:
            if session is not None:
                await run_to_end(session.close())
            else:  # the session itself failed to build: stop what it would have closed
                await run_to_end(self._abandon_start(pw, profile_dir))
            raise
        return session

    @staticmethod
    async def _abandon_start(pw: Any, profile_dir: Path) -> None:
        await _stop_quietly(pw)
        await kill_processes(browser_pids(profile_dir, main_only=False), grace=1.0)
        forget_browser(None, profile_dir)

    async def _finish_launch(self, spec: LaneSpec, session: CamoufoxSession, plan: LaunchPlan, *,
                             profile_dir: Path, fingerprint_path: Path, display: str,
                             xauthority: str | None, proxied: bool) -> None:
        pid = await _wait_for_pid(profile_dir)
        session._pid = pid
        watch_browser(pid, spec.id)  # the browser's own output carries the lane's id in the log
        try:
            await asyncio.to_thread(self.settle_pin, spec.id, fingerprint_path, plan, pid)
        except (AgentosError, asyncio.CancelledError):
            raise
        except Exception as e:
            raise EngineError(f"{spec.id}: could not pin the fingerprint: {browser_error(e, spec.id)}") from e
        session.pin_notes = plan.notes
        await self._place_window(spec.id, display, plan.config, xauthority=xauthority)
        keyboard = await xkeyboard.prepare(display, xauthority)
        if keyboard is not None:
            human.X_KEYBOARDS[session.context] = keyboard
        session.launch_options = plan.options
        session.guard = LaneGuard(spec.id, sites=spec.sites, strict=spec.egress_strict,
                                  trace=EgressTrace(profile_dir.parent / EGRESS_TRACE_NAME)
                                  ).attach(session.context, session.routes)
        await self._install_identity(spec.id, session, profile_dir=profile_dir, proxied=proxied)

    async def _place_window(self, lane_id: str, display: str, config: dict[str, Any], *,
                            xauthority: str | None = None) -> None:
        """Put the real window where the pin says (place_window). Never stops a launch."""
        position = window_position_of(config)
        if position is None or not sys.platform.startswith("linux"):
            return
        try:
            await asyncio.to_thread(place_window, display, position, xauthority=xauthority)  # logs a miss itself
        except Exception as e:  # noqa: BLE001 - a misplaced window is a finding, not a dead lane
            log.warning("%s: could not move the window to its pinned position %s: %s", lane_id, position,
                        _first_line(e))

    async def _install_identity(self, lane_id: str, session: CamoufoxSession, *, profile_dir: Path,
                                proxied: bool) -> None:
        """The Web Bot Auth route, when the lane has hosts to sign for. Signing is additive: a lane
        whose identity cannot load runs unsigned, and the reason goes in its events."""
        session.signer = None
        if self.identity is None:
            return
        try:
            signer = await asyncio.to_thread(self.identity.signer_for, lane_id, profile_dir=profile_dir,
                                             proxied=proxied,
                                             allow_private=getattr(self, "allow_private_network", False))
            if signer is not None:
                await signer.install(getattr(session, "routes", None) or session.context)
        except AgentosError as e:
            log.error("%s: signing is off for this launch: %s", lane_id, e.message)
            session.pin_notes.append(("identity_error", {"error": e.message}))
            return
        except Exception as e:  # noqa: BLE001 - a broken identity must not stop the lane
            log.error("%s: signing is off for this launch: %s", lane_id, _first_line(e))
            session.pin_notes.append(("identity_error", {"error": _first_line(e)}))
            return
        if signer is not None:
            session.signer = signer
            session.pin_notes.append(("identity_signing", signer.summary()))

    async def sweep_orphans(self, root: Path) -> list[int]:
        """Kill camoufox-bin processes whose profile lives under root (left by a previous run)."""
        pids = browser_pids_under(root)
        if pids:
            log.warning("stopping %d orphan camoufox-bin processes under %s", len(pids), root)
            await kill_processes(pids)
        return pids


async def _wait_for_pid(profile_dir: Path, timeout: float = 5.0) -> int | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        pids = browser_pids(profile_dir)
        if pids:
            return min(pids)
        if loop.time() > deadline or not Path("/proc").is_dir():
            return None
        await asyncio.sleep(0.1)


async def _stop_quietly(pw: Any) -> None:
    try:
        await asyncio.wait_for(pw.stop(), 10.0)
    except Exception as e:  # noqa: BLE001
        log.debug("playwright driver stop after a failed launch: %s", browser_error(e, "driver stop"))

