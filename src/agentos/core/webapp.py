"""Where the web app the node serves at `/` comes from, and which prebuilt copy is installed.

The order: $AGENTOS_PANEL_DIR when set, then a checkout's own build (web/dist next to the code),
then the prebuilt copy `agentos web install` unpacked under the home (<home>/web/app). A checkout
that builds its own app keeps serving it; an installed agentos (a uv tool, no web/ beside it)
serves the release's copy. Nothing here touches the network; agentos.setup.webapp downloads.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WEB_DIR = "web"          # <home>/web: the installed app and what was installed
APP_DIR = "app"          # <home>/web/app: the unpacked files, served as they are
STATE_FILE = "web.json"  # <home>/web/web.json: {"version", "sha256", "wanted"}


def checkout_dist() -> Path:
    """web/dist of the checkout this code runs from (it need not exist)."""
    return Path(__file__).resolve().parents[3] / "web" / "dist"  # src/agentos/core/webapp.py -> the repo root


def installed_dir(home: Path) -> Path:
    return Path(home) / WEB_DIR / APP_DIR


def state_path(home: Path) -> Path:
    return Path(home) / WEB_DIR / STATE_FILE


def built(path: Path) -> bool:
    """Is there an app to serve here (an index.html)?"""
    return (Path(path) / "index.html").is_file()


def panel_dir(home: Path | None) -> Path:
    """The folder to serve at `/`. With no app anywhere it is the checkout's web/dist, so the page
    that says how to get one (agentos.api.static.placeholder) can tell a checkout from an install."""
    env = os.environ.get("AGENTOS_PANEL_DIR")
    if env:
        return Path(env)
    checkout = checkout_dist()
    if built(checkout):
        return checkout
    if home is not None and built(installed_dir(home)):
        return installed_dir(home)
    return checkout


def read_state(home: Path) -> dict[str, Any] | None:
    """What `agentos web install` or `remove` wrote last, or None (never asked, or unreadable)."""
    try:
        data = json.loads(state_path(home).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class WebStatus:
    source: str               # "env", "checkout", "installed" or "none": what `/` serves
    path: Path                # the folder served (or the checkout's web/dist, for "none")
    installed: str | None     # the version unpacked under the home, if any
    wanted: bool | None       # the person's last answer: True, False, or None (never asked)
    expected: str             # the version this agentos needs (its own)

    @property
    def mismatched(self) -> bool:
        """An installed copy from another release (the installer updated agentos, not the app)."""
        return self.installed is not None and self.installed != self.expected


def status(home: Path, version: str | None = None) -> WebStatus:
    from agentos import __version__

    expected = version or __version__
    saved = read_state(home) or {}
    have = built(installed_dir(home))
    installed = str(saved.get("version")) if have and saved.get("version") else None
    wanted = saved.get("wanted") if isinstance(saved.get("wanted"), bool) else (True if have else None)
    served = panel_dir(home)
    if os.environ.get("AGENTOS_PANEL_DIR"):
        source = "env"
    elif built(served) and served == checkout_dist():
        source = "checkout"
    elif built(served):
        source = "installed"
    else:
        source = "none"
    return WebStatus(source=source, path=served, installed=installed, wanted=wanted, expected=expected)
