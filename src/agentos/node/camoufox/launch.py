"""What one launch starts (LaunchPlan) and what it asks Camoufox: its launch options, its prefs
environment, the installed version, and the exit IP and its location (ExitGeo)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentos.node.camoufox.pages import _first_line

log = logging.getLogger("agentos.engine.camoufox")


def _camoufox_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("camoufox")
    except Exception:  # noqa: BLE001 - only a note in the pin
        return None


class ExitGeo(Protocol):
    def exit_ip(self, proxy: dict[str, str] | None) -> str: ...
    def locate(self, ip: str) -> dict[str, Any]: ...


class CamoufoxGeo:
    """The lane's exit IP and where it is, the way Camoufox's geoip finds them: one IP lookup
    through the lane's proxy (Camoufox caches it for the life of the process, so a relaunch makes
    no new request) and the local GeoIP database."""

    def exit_ip(self, proxy: dict[str, str] | None) -> str:
        from camoufox.ip import Proxy, public_ip
        return public_ip(Proxy(**proxy).as_string() if proxy else None)

    def locate(self, ip: str) -> dict[str, Any]:
        from camoufox.geolocation import get_geolocation
        geo = get_geolocation(ip)
        return {"ip": ip, "country": geo.locale.region, "timezone": geo.timezone,
                "latitude": geo.latitude, "longitude": geo.longitude}


def _resolve_options(**kwargs: Any) -> dict[str, Any]:
    from camoufox.utils import launch_options
    return launch_options(**kwargs)


def installed_browser_version(executable_path: str | None = None) -> str | None:
    """The version of the Camoufox build a launch runs ("158.0.1-beta.41"): the named executable's
    application.ini, else the build the camoufox package would launch. None when it cannot be read."""
    try:
        from camoufox.utils import resolve_verstr
        return resolve_verstr(Path(executable_path) if executable_path else None)
    except Exception as e:  # noqa: BLE001 - only the version bump needs it
        log.debug("could not read the installed Camoufox version: %s", _first_line(e))
        return None


def _pref_env(prefs: dict[str, Any]) -> dict[str, str]:
    from camoufox.utils import get_pref_env_vars
    return get_pref_env_vars(prefs)


@dataclass
class LaunchPlan:
    """What one launch starts: Playwright options carrying exactly `config`, and the pin to write
    once the browser runs (None: the pin stays as it is)."""

    options: dict[str, Any]
    config: dict[str, Any]
    first: bool
    pin: dict[str, Any] | None = None
    old_version: int | None = None
    notes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    backup_label: str | None = None  # the old pin's backup name (backup_pin); None: by pin version
