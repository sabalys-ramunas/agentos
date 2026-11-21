"""Which engines this install has, how each is launched, and the command that adds a missing one.

Chromium comes in two flavours: Playwright's own build (`bundled`, the version pinned by the
installed playwright package, so the same on every machine) and the Chrome installed on the
computer (`chrome`, whatever version it updated to). `auto` takes the bundled build when it is
there, else Chrome. Firefox and WebKit are always Playwright's own builds.

Playwright is imported inside the functions that use it: openajar without the `studio` extra
still imports this module.
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openajar.core.errors import EngineError, OpenajarError
from openajar.studio.browser.errors import EngineMissing
from openajar.studio.contracts import EngineName

ChromiumFlavor = Literal["auto", "bundled", "chrome"]
CHROMIUM_FLAVORS: tuple[str, ...] = ("auto", "bundled", "chrome")
LAUNCH_TIMEOUT_S = 45

# Flags that make Chromium render the same on every machine: colours in sRGB whatever the screen,
# and glyph outlines without the system's hinting.
CHROMIUM_ARGS = ("--force-color-profile=srgb", "--font-render-hinting=none")

CHROME_PATHS = {
    "darwin": ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    "linux": ("/opt/google/chrome/chrome", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"),
}


@dataclass(frozen=True)
class Launched:
    browser: Any            # playwright Browser
    engine: EngineName
    flavor: str             # bundled | chrome
    version: str


def python_command() -> str:
    return shlex.quote(sys.executable)


def install_command(engine: EngineName | str) -> str:
    """The command that installs an engine for the playwright this openajar runs on."""
    return f"{python_command()} -m playwright install {EngineName(engine).value}"


def deps_command(engine: EngineName | str) -> str:
    """Linux only: the system libraries an engine needs (asks for root)."""
    return f"{python_command()} -m playwright install-deps {EngineName(engine).value}"


STUDIO_EXTRA = "uv sync --extra studio (or pip install 'openajar[studio]')"


def playwright_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("playwright")
    except Exception:  # noqa: BLE001 - not installed is an answer, not an error
        return None


def _browsers_json() -> dict[str, dict[str, Any]]:
    """Playwright's own list of the builds it expects: name -> {revision, browserVersion}."""
    try:
        import playwright
    except ImportError:
        return {}
    path = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    try:
        items = json.loads(path.read_text("utf-8")).get("browsers", [])
    except (OSError, ValueError):
        return {}
    return {item["name"]: item for item in items if isinstance(item, dict) and "name" in item}


def _cache_under(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "ms-playwright"


def _account_home() -> Path | None:
    """The account's own home, whatever HOME says (a scratch HOME hides the browsers there)."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return None


def browsers_dir() -> Path:
    """Where Playwright keeps its builds (PLAYWRIGHT_BROWSERS_PATH wins, as it does for Playwright).
    Under a scratch HOME with no builds of its own, the account's own cache is used: the builds
    installed there are not missing."""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env != "0":
        return Path(env).expanduser()
    here = _cache_under(Path.home())
    if not here.is_dir() and os.environ.get("XDG_CACHE_HOME") is None:
        acct = _account_home()
        if acct is not None and acct != Path.home() and _cache_under(acct).is_dir():
            return _cache_under(acct)
    return here


def point_playwright_at_builds() -> None:
    """Make Playwright look where browsers_dir found the builds (it reads HOME otherwise)."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    found = browsers_dir()
    if found != _cache_under(Path.home()):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(found)


def _bundled_dirs(name: str) -> list[Path]:
    """Installed build folders of a Playwright browser (`chromium_headless_shell-1234`), newest first."""
    root = browsers_dir()
    prefix = name.replace("-", "_") + "-"
    try:
        found = [p for p in root.iterdir() if p.is_dir() and p.name.startswith(prefix)
                 and p.name[len(prefix):].isdigit()]
    except OSError:
        return []
    return sorted(found, key=lambda p: int(p.name[len(prefix):]), reverse=True)


def _bundled(name: str) -> dict[str, Any] | None:
    """The build this playwright expects for `name`, when it is installed: {revision, version}."""
    expected = _browsers_json().get(name)
    for folder in _bundled_dirs(name):
        revision = folder.name.rsplit("-", 1)[1]
        if expected is None or revision == str(expected.get("revision")) or name == "webkit":
            # WebKit's revision differs per OS release (revisionOverrides); any installed build counts
            return {"revision": revision, "version": (expected or {}).get("browserVersion"), "path": str(folder)}
    return None


def chrome_path() -> str | None:
    for raw in CHROME_PATHS.get(sys.platform, ()):
        path = Path(raw).expanduser()
        if path.is_file():
            return str(path)
    return None


def chrome_version(path: str | None) -> str | None:
    """Chrome's version without starting it (macOS reads the app's Info.plist)."""
    if not path or sys.platform != "darwin":
        return None
    plist = Path(path).parents[1] / "Info.plist"
    try:
        return str(plistlib.loads(plist.read_bytes()).get("CFBundleShortVersionString") or "") or None
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def detect(chromium: ChromiumFlavor = "auto") -> dict[str, dict[str, Any]]:
    """What each engine would launch as, read from the disk without starting anything."""
    out: dict[str, dict[str, Any]] = {}
    shell = _bundled("chromium-headless-shell") or _bundled("chromium")
    chrome = chrome_path()
    flavors = {"auto": ("bundled", "chrome"), "bundled": ("bundled",), "chrome": ("chrome",)}[chromium]
    pick: dict[str, Any] | None = None
    for flavor in flavors:
        if flavor == "bundled" and shell:
            pick = {"present": True, "flavor": "bundled", "version": shell["version"]}
            break
        if flavor == "chrome" and chrome:
            pick = {"present": True, "flavor": "chrome", "version": chrome_version(chrome)}
            break
    out["chromium"] = pick or {"present": False, "install": install_command("chromium"),
                               "or": "install Google Chrome"}
    for engine in ("firefox", "webkit"):
        found = _bundled(engine)
        out[engine] = ({"present": True, "flavor": "bundled", "version": found["version"]} if found
                       else {"present": False, "install": install_command(engine)})
    return out


def _missing_text(exc: BaseException) -> str:
    return str(exc).split("\n", 1)[0][:300].rstrip(". ")


def _is_missing(exc: BaseException) -> bool:
    text = str(exc)
    return any(mark in text for mark in ("Executable doesn't exist", "is not found at", "distribution 'chrome' is not found",
                                         "Looks like Playwright", "please run the following command"))


def _needs_deps(exc: BaseException) -> bool:
    return "missing dependencies" in str(exc) or "install-deps" in str(exc)


async def launch(pw: Any, engine: EngineName, chromium: ChromiumFlavor = "auto") -> Launched:
    """Start one headless browser. Raises EngineMissing (with the install command) when the engine
    is not there, EngineError when it is there and would not start."""
    from playwright.async_api import Error as PlaywrightError

    if engine is EngineName.CHROMIUM:
        tries: list[tuple[str, dict[str, Any]]] = []
        order = {"auto": ("bundled", "chrome"), "bundled": ("bundled",), "chrome": ("chrome",)}[chromium]
        for flavor in order:
            if flavor == "bundled":
                tries.append(("bundled", {}))
                if not _bundled("chromium-headless-shell") and _bundled("chromium"):
                    tries.append(("bundled", {"channel": "chromium"}))  # the full build, in its new headless mode
            else:
                tries.append(("chrome", {"channel": "chrome"}))
        last: BaseException | None = None
        for flavor, extra in tries:
            try:
                browser = await pw.chromium.launch(headless=True, args=list(CHROMIUM_ARGS), timeout=LAUNCH_TIMEOUT_S * 1000,
                                                   **extra)
            except PlaywrightError as exc:
                last = exc
                if _is_missing(exc):
                    continue
                raise _failed(engine, exc) from exc
            return Launched(browser, engine, flavor, browser.version)
        wanted = {"auto": "Neither Playwright's Chromium nor Google Chrome is installed.",
                  "bundled": "Playwright's Chromium is not installed.",
                  "chrome": "Google Chrome is not installed."}[chromium]
        if chromium == "chrome":
            raise EngineMissing(f"{wanted} Install Google Chrome, or use Playwright's build.", engine="chromium",
                                install=install_command("chromium")) from last
        raise EngineMissing(wanted, engine="chromium", install=install_command("chromium")) from last
    try:
        browser = await getattr(pw, engine.value).launch(headless=True, timeout=LAUNCH_TIMEOUT_S * 1000)
    except PlaywrightError as exc:
        if _is_missing(exc):
            raise EngineMissing(f"{engine.value.capitalize()} is not installed for this playwright.",
                                engine=engine.value, install=install_command(engine)) from exc
        raise _failed(engine, exc) from exc
    return Launched(browser, engine, "bundled", browser.version)


def _failed(engine: EngineName, exc: BaseException) -> OpenajarError:
    if _needs_deps(exc):
        return EngineMissing(f"{engine.value.capitalize()} is installed but this computer lacks the libraries it needs.",
                             engine=engine.value, install=deps_command(engine))
    return EngineError(f"{engine.value} did not start: {_missing_text(exc)}. Try another engine, or check "
                       "studio_status.")


def require_playwright() -> Any:
    """playwright.async_api, or EngineMissing naming the studio extra."""
    try:
        from playwright import async_api
    except ImportError as exc:
        raise EngineMissing("Playwright is not installed, so studio has no browser.", engine="playwright",
                            install=STUDIO_EXTRA) from exc
    return async_api

