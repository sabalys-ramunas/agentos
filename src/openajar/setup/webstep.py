"""The web app step of `openajar setup` (openajar.setup.cli): offer the prebuilt web app, default yes.

A checkout that built its own (web/dist) keeps it and is not asked. Otherwise the step offers this
release's copy (openajar.setup.webapp), updates one from another release without asking, and records
a no, so the installer does not fetch it on its own later. Without network, or before a release has
the app, it says so and setup goes on: the node works without it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from openajar.core import webapp

if TYPE_CHECKING:
    from openajar.setup.cli import Ctx

WHAT = ("The web app shows every environment in your browser: its live browser, its branch and pushes, "
        "the time left and the cost so far.")


# ------------------------------------------------------------------ the outside world (tests replace these)


def get_web_app(home: Path) -> Any:
    """Download, check and unpack this release's web app (openajar.setup.webapp.install)."""
    from openajar.setup import webapp as installer

    return installer.install(home)


def app_url(home: Path) -> str:
    """Where the local service serves the app: its port, or the default one."""
    try:
        from openajar.local import home as local_home

        known = local_home.read(home)
    except Exception:  # noqa: BLE001 (an unreadable node.toml: the default port, and setup goes on)
        known = None
    return f"http://127.0.0.1:{known.mcp_port if known is not None else 7410}/"


# ------------------------------------------------------------------ the step


def web(ctx: Ctx) -> None:
    ui = ctx.ui
    st = webapp.status(ctx.home)
    url = app_url(ctx.home)
    if st.source in ("env", "checkout"):
        from openajar.setup.web_cli import describe

        ui.say(f"The web app is {describe(st)}: {url}")
        ctx.web = url
        return
    if ctx.opts.no_web:
        ui.say("Skipped the web app (--no-web). `openajar setup web` offers it again.")
        return
    if st.installed is not None:
        _have(ctx, st, url)
        return
    ui.say(WHAT)
    if not ui.confirm("Add the web app (a small download from this release)?", default=st.wanted is not False):
        from openajar.setup import webapp as installer

        installer.decline(ctx.home)
        ui.say("No web app. Your agents and their live view links work without it. "
               "`openajar setup web` adds it later.")
        return
    _install(ctx, url)


def _have(ctx: Ctx, st: webapp.WebStatus, url: str) -> None:
    ui = ctx.ui
    if st.mismatched:
        ui.say(f"The web app is {st.installed}, and this openajar is {st.expected}: updating it.")
        _install(ctx, url)
        return
    if not ctx.quick and not ui.confirm(f"The web app {st.installed} is installed. Keep it?", default=True):
        from openajar.setup import webapp as installer

        installer.remove(ctx.home)
        ui.say("Removed. Updates leave it away. `openajar setup web` adds it again.")
        return
    ui.say(f"The web app {st.installed} is installed: {url}")
    ctx.web = url


def _install(ctx: Ctx, url: str) -> None:
    from openajar.setup import webapp as installer

    ui = ctx.ui
    try:
        got = get_web_app(ctx.home)
    except (installer.WebAppMissing, installer.WebAppRefused) as e:
        ui.say(f"Could not add the web app: {e.message}")
        ui.say("The node works without it. `openajar setup web` tries again.")
        ctx.todo.append("the web app")
        return
    ui.say(f"Added the web app {got.version} (sha256 checked): {url}")
    ctx.web = url


def check_row(ctx: Ctx) -> tuple[bool | None, str]:
    """The health check's line: (ready, to do, or a note)."""
    from openajar.setup.web_cli import describe

    st = webapp.status(ctx.home)
    if st.mismatched:
        return False, describe(st)
    if st.source == "none":
        return None, describe(st)
    return True, describe(st)
