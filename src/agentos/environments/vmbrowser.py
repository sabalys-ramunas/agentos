"""An environment's browser on its own machine (environments.toml browser = "vm").

By default an environment's browser runs on the node (Camoufox on Xvfb) and reaches the machine
through a SOCKS tunnel. A node with no browser stack of its own (agentos on a Mac) runs it on the
machine instead, and drives it from here:

- The machine has the stack already (browser.sh, in the docker image and the `dev` snapshot), or gets
  it when the environment comes up.
- The browser lane `env/<id>-browser` stays a lane of the node's lane manager, so every browser tool,
  the egress guard, take over and proofs work as on any lane. `install()` puts a router in front of
  the lane manager's engine and displays: lanes of a "vm" environment go to `VmBrowsers`, every
  other lane to the node's own engine and displays, unchanged.
- The screen: Xvfb and x11vnc on the machine (vmbrowser_ctl.py), started over the SSH connection the
  node holds. The live view is the node's own relay (agentos.node.vnc_relay) on a Unix socket that
  the environment's tunnel forwards to the machine's x11vnc socket, so the viewer, its signed links
  and take over are the same code as for a display on the node.
- The browser: Camoufox's browser server on the machine, on 127.0.0.1 only, at a random path; the
  node connects Playwright to it through the tunnel (agentos.environments.tunnels). It is a fresh
  browser each time the lane opens (a browser server cannot serve a persistent profile); the sign-ins
  made in it (cookies, local storage) are read when the lane closes and when it is saved, and its
  next context opens with them (agentos.environments.signins). It holds no account of the owner's.
- On the machine, localhost is the machine: no proxy. Closing the lane stops the browser, releasing
  the display stops the screen, and closing the environment does both before its tunnel goes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shlex
from importlib import resources
from pathlib import Path
from typing import Any

from agentos.core.aio import run_to_end
from agentos.core.errors import EngineError
from agentos.core.models import LaneSpec, ProxyConfig, ViewerInfo
from agentos.node.camoufox.pin import CANVAS_PREF
from agentos.node.camoufox.prefs import CACHE_PREFS, LINUX_HOST_PREFS, STEADY_CHROME_PREFS, WHEEL_PREFS
from agentos.node.camoufox.session import CamoufoxSession

log = logging.getLogger("agentos.environments")

BROWSER_DIR = "/opt/agentos-browser"
VENV_PY = f"{BROWSER_DIR}/venv/bin/python"
READY = f"{BROWSER_DIR}/ready"
REQUIREMENTS_ON_VM = "/usr/local/lib/agentos-env/browser-requirements.txt"
SCRIPT_ON_VM = "/usr/local/lib/agentos-env/browser.sh"
# The machine runs the control script it is sent, with the request: nothing of it is on a command line.
BOOT = ('import json,sys; d=json.load(sys.stdin); '
        'exec(compile(d.pop("script"),"vmbrowser_ctl","exec"),{"__name__":"__agentos_vm__","REQ":d})')
CTL_TIMEOUT_S = 150.0
INSTALL_TIMEOUT_S = 1500.0
CONNECT_TIMEOUT_MS = 30_000
# A launch is four steps, each with its own limit, so a step that hangs fails with its name and the time
# each step took, well inside the lane's wall (agentos.node.deadlines.WALL_S, 210 s). Field report
# 2026-11-16: browser_navigate failed twice with only "could not get the browser ... within 210 s" while
# the machine's log showed its browser server up in 1.4 s; the hang was between the node and the machine.
LAUNCH_STEPS = ("start", "connect", "context", "page")
STEP_LIMIT_S = {"start": 90.0, "connect": CONNECT_TIMEOUT_MS / 1000 + 5, "context": 30.0, "page": 30.0}
STEP_DOING = {"start": "starting the browser server on the machine", "connect": "connecting to it through the tunnel",
              "context": "opening its browser context", "page": "opening its first page"}
STEP_DONE = {"start": "browser server started", "connect": "connected through the tunnel",
             "context": "context opened", "page": "first page opened"}
LAUNCH_HINT_KEEP_S = 60.0  # a stopped launch's steps, kept for the stopped call's error
# the processes of the machine's browser (Camoufox is Firefox; its server is Python), for a kill during a launch
BROWSER_PROCS = ("camoufox", "firefox", "web content", "isolated web co", "webextensions", "rdd process",
                 "socket process", "utility process", "privileged cont", "python", "xvfb", "x11vnc")
DESKTOP = (1920, 1080)   # an environment's browser shows pages at a desktop width, as on the node
# Playwright drives the machine's browser from here: the `local` extra of agentos (scripts/install.sh) has it.
NO_PLAYWRIGHT = ("this agentos has no Playwright, which drives the environment's browser on its machine. Install "
                 "agentos again with scripts/install.sh, or add the local extra: uv tool install --force "
                 "'agentos[local] @ git+https://github.com/sabalys-ramunas/agentos.git'")


def _text(name: str) -> str:
    return resources.files("agentos.environments").joinpath(name).read_text()


def ctl_script() -> str:
    return _text("vmbrowser_ctl.py")


def ctl_command() -> str:
    return f"{VENV_PY} -I -c {shlex.quote(BOOT)}"


def install_script() -> str:
    """browser.sh with its requirements, as one script for a shell on the machine: both files are
    written next to setup.sh, then browser.sh runs (it does nothing where the stack is ready)."""
    out = ["set -eu", "mkdir -p /usr/local/lib/agentos-env"]
    for path, body, mark in ((REQUIREMENTS_ON_VM, _text("browser-requirements.txt"), "AGENTOS_BROWSER_REQUIREMENTS"),
                             (SCRIPT_ON_VM, _text("browser.sh"), "AGENTOS_BROWSER_SCRIPT")):
        out.append(f"cat > {path} <<'{mark}'\n{body.rstrip()}\n{mark}")
    out.append(f"sh {SCRIPT_ON_VM}")
    return "\n".join(out) + "\n"


def browser_options(spec: LaneSpec, *, identity_os: list[str] | None = None,
                    size: tuple[int, int] = DESKTOP) -> dict[str, Any]:
    """What the machine's Camoufox starts with: the node's own prefs for a lane (agentos.node.camoufox.
    engine), a desktop window that fills the screen, as an environment's browser has on the node. No
    proxy and no PAC: on the machine, localhost is the machine."""
    width, height = size
    opts: dict[str, Any] = {
        "headless": False, "humanize": True, "locale": spec.locale, "enable_cache": True,
        "i_know_what_im_doing": True, "geoip": False, "screen": [width, height], "window": [width, height],
        "firefox_user_prefs": {**CACHE_PREFS, **STEADY_CHROME_PREFS, **WHEEL_PREFS, **LINUX_HOST_PREFS,
                               CANVAS_PREF: ""},
    }
    if identity_os:
        opts["os"] = list(identity_os)
    return opts


class LaunchSteps:
    """How far a browser launch on the machine got, and how long each step took: for its error, its log
    line, and a stuck call's hint (VmBrowsers.launch_hint)."""

    def __init__(self, lane_id: str, env_id: str, clock: Any = None):
        self.lane_id, self.env_id = lane_id, env_id
        self._clock = clock or asyncio.get_running_loop().time
        self.began = self._clock()
        self.done: list[tuple[str, float]] = []
        self.current: str | None = None
        self._step_began = self.began
        self.stopped: float | None = None  # when the call that opened it was stopped (the lane's wall)

    async def run(self, name: str, coro: Any) -> Any:
        """Run one step within its limit (STEP_LIMIT_S). A step that fails or does not answer raises
        LaunchFailed with its name and how long it ran."""
        self.current, self._step_began = name, self._clock()
        limit = STEP_LIMIT_S[name]
        try:
            out = await asyncio.wait_for(coro, limit)
        except TimeoutError:
            raise LaunchFailed(self, name, self._clock() - self._step_began, f"no answer within {limit:g} s") from None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # every failure of a step is named
            raise LaunchFailed(self, name, self._clock() - self._step_began,
                               str(getattr(e, "message", None) or e).strip() or type(e).__name__) from e
        self.done.append((name, self._clock() - self._step_began))
        self.current = None
        return out

    @property
    def elapsed(self) -> float:
        return self._clock() - self.began

    def done_words(self) -> str:
        return ", ".join(f"{STEP_DONE[n]} in {t:.1f} s" for n, t in self.done)

    def now_words(self) -> str:
        """While it runs (or when it was stopped): the steps done and the one it is at, for a stuck call."""
        end = self.stopped if self.stopped is not None else self._clock()
        at = (f"{STEP_DOING[self.current]} for {end - self._step_began:.0f} s" if self.current
              else "between its steps")
        done = self.done_words()
        what = "was still opening when the call was stopped" if self.stopped is not None else "was opening"
        return f"The browser on the machine {what}: {done + '; then ' if done else ''}{at}."


class LaunchFailed(Exception):
    def __init__(self, steps: LaunchSteps, step: str, took: float, why: str):
        super().__init__(why)
        self.steps, self.step, self.took, self.why = steps, step, took, why

    def words(self, kills: list[dict] | None = None) -> str:
        """The error: which step failed and why, how long each took; a kernel's oom kill during the launch first."""
        from agentos.env import oom

        doing = f"{STEP_DOING[self.step]} failed after {self.took:.1f} s ({self.why[:400]})"
        done = self.steps.done_words()
        tail = f" Before it: {done}." if done else ""
        head = f"{self.steps.lane_id}: the browser on environment {self.steps.env_id}'s machine did not open"
        note = oom.note(kills, running=False, what="browser launch") if kills else None
        if note:
            return f"{head}: {note} ({doing[0].upper() + doing[1:]}.{tail})"
        return f"{head}: {doing}.{tail}"


class RemoteSession(CamoufoxSession):
    """A lane's browser that runs on its environment's machine: the same session as on the node (pages,
    routes, dialogs, the egress guard), with no process here. It is alive while Playwright is connected,
    and closing it also stops the browser on the machine (`on_close`)."""

    def __init__(self, playwright: Any, browser: Any, context: Any, profile_dir: Path, close_timeout: float, *,
                 on_close: Any, before_close: Any = None, **kw: Any):
        super().__init__(playwright, context, profile_dir, None, close_timeout, **kw)
        self._browser = browser
        self._on_close = on_close
        self._before_close = before_close  # reads the sign-ins while the context is still there
        browser.on("disconnected", lambda *_: setattr(self, "_ctx_closed", True))

    @property
    def alive(self) -> bool:
        return not (self._closed or self._ctx_closed) and bool(self._browser.is_connected())

    @property
    def pids(self) -> list[int]:
        return []

    async def close(self) -> None:
        if self._closed:
            return
        if self._before_close is not None and self.alive:
            with contextlib.suppress(Exception):
                await self._before_close(self._ctx)
        try:
            await super().close()
        finally:
            await run_to_end(self._stop_remote())

    async def _stop_remote(self) -> None:
        try:
            await self._on_close()
        except Exception as e:  # noqa: BLE001 - the machine may be gone already; deleting it ends the browser
            log.info("the environment's browser did not stop on its machine: %s", e)


class VmBrowsers:
    """The machine side of "vm" browsers, for the environments service `svc` (its store, terminal
    lanes and tunnels). `engine` is the lane manager's own engine: its file and dialog settings."""

    def __init__(self, svc: Any):
        self.svc = svc
        self.engine: Any = None
        self._viewers: dict[str, ViewerInfo] = {}
        self.launching: dict[str, LaunchSteps] = {}  # lane -> its launch in progress (launch_hint)

    # ---- which lanes
    def env_for(self, lane_id: str) -> Any:
        found = self.svc.env_of_lane(lane_id) if isinstance(lane_id, str) else None
        if found is None or found[1] != "browser" or found[0].browser != "vm":
            return None
        return found[0]

    def handles(self, lane_id: str) -> bool:
        return self.env_for(lane_id) is not None

    def _require(self, lane_id: str) -> tuple[Any, Any]:
        env = self.env_for(lane_id)
        if env is None:
            raise EngineError(f"{lane_id} is not the browser of an environment on its machine")
        tunnel = self.svc.tunnel_of(env.id)
        if tunnel is None or not tunnel.alive or not getattr(tunnel, "vm_browser", False):
            raise EngineError(f"environment {env.id}'s connection to its machine is down; the node starts it "
                              "again within a minute")
        return env, tunnel

    # ---- the machine
    async def ctl(self, env: Any, action: str, *, timeout_s: float = CTL_TIMEOUT_S, check: bool = True,
                  **args: Any) -> dict[str, Any]:
        req = json.dumps({"script": ctl_script(), "action": action, **args})
        res = await self.svc.envs().exec(env.terminal_lane, ctl_command(), stdin=req, timeout_s=timeout_s)
        line = next((ln for ln in reversed((res.stdout or "").splitlines()) if ln.startswith("{")), "")
        try:
            out = json.loads(line) if line else {}
        except ValueError:
            out = {}
        if not check and res.exit_code == 0 and out:
            return out
        if res.exit_code != 0 or not out.get("ok"):
            why = out.get("error") or (res.stderr or res.stdout or "").strip()[-600:] or f"exit {res.exit_code}"
            raise EngineError(f"environment {env.id}: the browser's {action} on its machine failed: {why}")
        return out

    async def installed(self, env: Any) -> bool:
        res = await self.svc.envs().exec(env.terminal_lane, f"test -f {READY}", timeout_s=30)
        return res.exit_code == 0

    async def ensure_installed(self, env: Any) -> None:
        """The browser's stack on the machine: there already on the docker image and the `dev`
        snapshot; installed now on any other (a few minutes)."""
        if await self.installed(env):
            return
        log.info("environment %s: installing the browser on its machine (no image has it)", env.id)
        res = await self.svc.envs().exec(env.terminal_lane, "sh -s", stdin=install_script(),
                                         timeout_s=INSTALL_TIMEOUT_S)
        if res.exit_code != 0:
            tail = (res.stderr or res.stdout or "").strip()[-600:]
            raise EngineError(f"environment {env.id}: installing the browser on its machine failed "
                              f"(exit {res.exit_code}): {tail}")

    # ---- displays
    async def acquire(self, lane_id: str, size: tuple[int, int] | None = None) -> ViewerInfo:
        env, tunnel = self._require(lane_id)
        width, height = size or DESKTOP
        await self.ctl(env, "display", width=width, height=height)
        viewer = ViewerInfo(display=":99", vnc_socket=tunnel.vnc_path, width=width, height=height)
        self._viewers[lane_id] = viewer
        return viewer

    async def release(self, lane_id: str) -> None:
        if self._viewers.pop(lane_id, None) is None:
            return
        env = self.env_for(lane_id)
        if env is not None and env.open:
            with contextlib.suppress(Exception):  # a machine that is gone has no screen to stop
                await self.ctl(env, "stop", what=["browser", "display"], timeout_s=30)

    def get(self, lane_id: str) -> ViewerInfo | None:
        return self._viewers.get(lane_id)

    def healthy(self, lane_id: str) -> bool:
        viewer = self._viewers.get(lane_id)
        env = self.env_for(lane_id)
        tunnel = self.svc.tunnel_of(env.id) if env is not None else None
        return bool(viewer and tunnel is not None and tunnel.alive and viewer.vnc_socket == tunnel.vnc_path
                    and viewer.vnc_socket and os.path.exists(viewer.vnc_socket))

    async def release_all(self) -> None:
        for lane_id in list(self._viewers):
            await self.release(lane_id)

    async def read_clipboard(self, lane_id: str) -> str:
        """What the machine's browser copied, for the live view's copy button (agentos.node.lane_clipboard
        reads a screen on the node the same way, with the same limits and errors)."""
        from agentos.node import lane_clipboard as lc

        env, _tunnel = self._require(lane_id)
        out = await self.ctl(env, "clipboard", check=False, timeout_s=30, limit=lc.MAX_BYTES,
                             timeout=lc.TIMEOUT_SECONDS, nothing=[w.decode() for w in lc.NOTHING_COPIED])
        if out.get("ok"):
            return str(out.get("text") or "")
        if out.get("kind") == "too_large":
            raise lc.ClipboardTooLarge(f"What is copied in the lane is over {lc.MAX_BYTES // (1024 * 1024)} MB, too "
                                       "much to bring over. Copy a smaller part.")
        if out.get("kind") == "timeout":
            raise lc.ClipboardUnreadable("The lane's browser did not hand over what it copied in time. Copy again in "
                                         "the lane, then try once more.")
        raise lc.ClipboardUnreadable(f"The machine could not read the lane's clipboard ({out.get('error') or 'no '
                                     'reason given'}).")

    # ---- the browser
    async def launch(self, spec: LaneSpec, *, profile_dir: Path) -> RemoteSession:
        """Open the lane's browser on its machine, in four timed steps (LAUNCH_STEPS). A step that fails
        or hangs fails the launch with its name, how long each step took, and what the kernel killed for
        memory meanwhile when it did; the log has the same line, and a launch that works logs its times."""
        try:
            import playwright.async_api  # noqa: F401
        except ModuleNotFoundError as e:
            if e.name is None or not e.name.startswith("playwright"):
                raise
            raise EngineError(NO_PLAYWRIGHT) from None

        env, tunnel = self._require(spec.id)
        steps = LaunchSteps(spec.id, env.id)
        self.launching[spec.id] = steps
        try:
            session = await self._launch(spec, env, tunnel, steps, profile_dir=profile_dir)
        except LaunchFailed as e:
            self.launching.pop(spec.id, None)
            kills = await self._launch_kills(env, steps)
            text = e.words(kills)
            log.warning("%s (after %.1f s in all)", text, steps.elapsed)
            raise EngineError(text) from e.__cause__
        except asyncio.CancelledError:  # the lane's wall: its error asks launch_hint how far this got
            steps.stopped = steps._clock()
            log.warning("%s: the launch on environment %s's machine was stopped. %s", spec.id, env.id,
                        steps.now_words())
            raise
        except BaseException:
            self.launching.pop(spec.id, None)
            raise
        self.launching.pop(spec.id, None)
        log.info("%s: the browser runs on environment %s's machine (%s; %.1f s in all)", spec.id, env.id,
                 steps.done_words(), steps.elapsed)
        return session

    def launch_hint(self, lane_id: str) -> str | None:
        """For a call that was stuck on a lane whose browser is opening on its machine, or was stopped
        while it opened in the last LAUNCH_HINT_KEEP_S (agentos.node.lanes slow_hints): the steps done,
        with their times, and the one it was at."""
        steps = self.launching.get(lane_id)
        if steps is None:
            return None
        if steps.stopped is not None and steps._clock() - steps.stopped > LAUNCH_HINT_KEEP_S:
            self.launching.pop(lane_id, None)
            return None
        return steps.now_words()

    async def _launch_kills(self, env: Any, steps: LaunchSteps) -> list[dict] | None:
        """What the machine's kernel killed for memory during the launch (agentos.env.oom), the browser's
        processes first; None when it killed nothing then, or could not be asked."""
        from agentos.env import oom
        from agentos.vms.ssh import PGID_DIR

        try:
            res = await asyncio.wait_for(self.svc.envs().exec(env.terminal_lane, oom.probe_script(
                PGID_DIR, "launch-none", final=False), timeout_s=oom.PROBE_TIMEOUT_S), oom.PROBE_TIMEOUT_S + 2)
            kills = oom.parse(res.stdout or "", elapsed_s=steps.elapsed)
        except Exception:  # noqa: BLE001 - the machine may be the thing that does not answer
            return None
        if not kills:
            return None

        def browsers(k: dict) -> bool:
            return any(p in str(k.get("name", "")).lower() for p in BROWSER_PROCS)

        return sorted(kills, key=browsers)  # oom.note names the newest three: the browser's go last

    async def _launch(self, spec: LaneSpec, env: Any, tunnel: Any, steps: LaunchSteps, *,
                      profile_dir: Path) -> RemoteSession:
        from playwright.async_api import async_playwright

        from agentos.node.egress import TRACE_NAME, EgressTrace, LaneGuard
        from agentos.node.files import FilesConfig, LaneFiles
        from agentos.node.interaction.dialogs import DIALOG_TIMEOUT

        engine = self.engine
        viewer = self._viewers.get(spec.id)
        size = (viewer.width or DESKTOP[0], viewer.height or DESKTOP[1]) if viewer else DESKTOP
        ws_path = "/" + secrets.token_urlsafe(24)
        options = browser_options(spec, identity_os=getattr(engine, "identity_os", None), size=size)
        from agentos.environments.network import vm_browser_prefs

        options["firefox_user_prefs"].update(vm_browser_prefs(getattr(env, "network", "full")))  # a closed network
        await steps.run("start", self.ctl(env, "browser", options=options, port=_vm_port(), ws_path=ws_path,
                                          timeout_s=STEP_LIMIT_S["start"] - 5))
        profile_dir.mkdir(parents=True, exist_ok=True)

        async def stop_remote() -> None:
            if self.svc.store.get(env.id).open:
                await self.ctl(env, "stop", what=["browser"], timeout_s=30)

        pw = await async_playwright().start()
        browser = None
        try:
            browser = await steps.run("connect", pw.firefox.connect(f"ws://127.0.0.1:{tunnel.browser_port}{ws_path}",
                                                                    timeout=CONNECT_TIMEOUT_MS))
            from agentos.environments import signins

            state = signins.for_launch(self.svc, env)  # the sign-ins a save or the lane's last close left
            context = await steps.run("context", browser.new_context(
                no_viewport=True, **({"storage_state": state} if state else {})))

            async def keep_signins(ctx: Any) -> None:
                if signins.enabled(self.svc, self.svc.store.get(env.id)):
                    signins.remember(self.svc, env.id, await signins.read_state(ctx))

            session = RemoteSession(pw, browser, context, profile_dir, float(getattr(engine, "close_timeout", 15.0)),
                                    on_close=stop_remote, before_close=keep_signins,
                                    files=LaneFiles(profile_dir.parent / "files", getattr(engine, "files", None)
                                                    or FilesConfig()),
                                    dialog_timeout=float(getattr(engine, "dialog_timeout", DIALOG_TIMEOUT)))
        except BaseException as e:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pw.stop(), 10)
            await run_to_end(_quietly(stop_remote()))
            if isinstance(e, (asyncio.CancelledError, LaunchFailed)):
                raise
            raise LaunchFailed(steps, steps.current or "connect", 0.0, str(e)) from e
        try:
            session.guard = LaneGuard(spec.id, sites=spec.sites, strict=spec.egress_strict,
                                      trace=EgressTrace(profile_dir.parent / TRACE_NAME)
                                      ).attach(session.context, session.routes)
            # a window on the screen at once, as a browser on the node shows one
            await steps.run("page", session.page())
        except BaseException:
            await run_to_end(session.close())
            raise
        return session


def _vm_port() -> int:
    from agentos.environments.tunnels import VM_BROWSER_PORT

    return VM_BROWSER_PORT


async def _quietly(coro: Any) -> None:
    with contextlib.suppress(Exception):
        await coro


# ---------------------------------------------------------------- in front of the lane manager

class _Front:
    """Everything not routed goes to the object behind (reads and writes alike)."""

    def __init__(self, inner: Any, vm: VmBrowsers):
        object.__setattr__(self, "inner", inner)
        object.__setattr__(self, "vm", vm)

    def __getattr__(self, name: str) -> Any:
        if name in ("inner", "vm"):  # not set yet (copy, pickle): never recurse
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "inner"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "inner"), name, value)


class RoutedEngine(_Front):
    """The lane manager's engine: a "vm" environment's browser lane launches on its machine."""

    async def launch(self, spec: LaneSpec, *, profile_dir: Path, fingerprint_path: Path, display: str,
                     xauthority: str | None = None, proxy: ProxyConfig | None) -> Any:
        if self.vm.handles(spec.id):
            return await self.vm.launch(spec, profile_dir=profile_dir)
        return await self.inner.launch(spec, profile_dir=profile_dir, fingerprint_path=fingerprint_path,
                                       display=display, xauthority=xauthority, proxy=proxy)


class RoutedDisplays(_Front):
    """The lane manager's displays: a "vm" environment's browser lane gets its machine's screen."""

    async def acquire(self, lane_id: str, *, size: tuple[int, int] | None = None) -> ViewerInfo:
        if self.vm.handles(lane_id):
            return await self.vm.acquire(lane_id, size)
        return await self.inner.acquire(lane_id, size=size)

    async def release(self, lane_id: str) -> None:
        if self.vm.get(lane_id) is not None:
            return await self.vm.release(lane_id)
        return await self.inner.release(lane_id)

    def get(self, lane_id: str) -> ViewerInfo | None:
        return self.vm.get(lane_id) or self.inner.get(lane_id)

    def healthy(self, lane_id: str) -> bool:
        if self.vm.get(lane_id) is not None:
            return self.vm.healthy(lane_id)
        inner = getattr(self.inner, "healthy", None)
        return inner(lane_id) if callable(inner) else True

    async def read_clipboard(self, lane_id: str, viewer: ViewerInfo) -> str:
        """What the lane copied, for the live view: on the machine for a "vm" lane, else on the node's screen."""
        if self.vm.get(lane_id) is not None:
            return await self.vm.read_clipboard(lane_id)
        from agentos.node import lane_clipboard

        return await lane_clipboard.read_clipboard(viewer.display, viewer.xauthority)

    async def release_all(self) -> None:
        await self.vm.release_all()
        inner = getattr(self.inner, "release_all", None)
        if callable(inner):
            await inner()


def install(lanes: Any, vm: VmBrowsers) -> bool:
    """Put the routers in front of the lane manager's engine and displays, once. False when this lane
    manager has neither (the dev node's simulated lanes): there the browser stays on the node."""
    if lanes is None or getattr(lanes, "engine", None) is None or getattr(lanes, "displays", None) is None:
        return False
    if not isinstance(lanes.engine, RoutedEngine):
        vm.engine = lanes.engine
        lanes.engine = RoutedEngine(lanes.engine, vm)
    if not isinstance(lanes.displays, RoutedDisplays):
        lanes.displays = RoutedDisplays(lanes.displays, vm)
    return True
