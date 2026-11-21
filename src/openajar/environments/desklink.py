"""The node's side of an environment's desktop (environments.toml desktop = true, browser = "vm"): the
desktop's helper on the machine (desktop/desk.py) listens on desk.sock, which the environment's tunnel
forwards to a Unix socket here (Tunnel.desk_path). Through it the node:

- shows the agent's commands: terminal_exec's output, as the node reads it, in the terminal window titled
  "<agent> · commands" (a tap on the exec, openajar.env.execs). The tap only copies what was already read: a
  command's stdout, stderr, exit code, timing, timeout and cancellation are the same with it or without it,
  a write that would wait is dropped instead, and a command shows at most MIRROR_BYTES of its output;
- brings the window the agent uses to the front: the browser when it drives the browser, its terminal
  when it runs a command. Fire and forget, at most one message per window a FOCUS_GAP_S, never while a
  person has the lane (the browser lane's control is not the agent's): a person who took over keeps their
  windows where they put them. The helper raises a window only when it is not in front already;
- gives a command the screen (DISPLAY, XAUTHORITY, and GTK_THEME so its windows are light like the desktop's), so
  a GUI program it starts opens on the desktop, but only while the agent has the screen: a command started
  while a person has control gets neither, so no tool hands the agent the screen during a take over. This
  is not a wall: the agent's commands run as root on its own machine, and root can read the cookie and the
  screen's socket (docs/THREAT-MODEL.md, "An environment's desktop");
- tells the top bar what the agent is doing (a status line: the agent's name, who has control, and whether it
  uses the browser, runs a command, reads or edits files), when that changes;
- opens the browser when the dock asks for it: the dock's Browser with no browser running sends one line back,
  {"t": "want", "what": "browser"}, and the node opens the environment's browser lane (at most once a
  WANT_GAP_S);
- learns where the browser window is on the screen ({"t": "win", ...}), so the live view can draw the agent's
  pointer where the agent acts in the page (openajar.node.agent_pointer). The numbers are the machine's and so
  untrusted: whole numbers inside the screen, or the pointer is not drawn. Nothing else the machine sends is
  acted on, and a line longer than READ_MAX ends the link.

Every send is best effort and never raises into a tool call.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
import os
import secrets
import time
from typing import Any

from openajar.node.agent_pointer import POINTERS
from openajar.node.viewer_state import env_agent_name

log = logging.getLogger("openajar.environments")

MIRROR_BYTES = 256 * 1024     # a command shows this much of its output in the window, the rest is counted
BUFFER_MAX = 1 << 20          # what may wait for the socket; past it a message is dropped
FOCUS_GAP_S = 1.5             # the same window is asked for again at most this often
RETRY_S = 5.0                 # a socket that refused is tried again after this long
SCREEN_ENV = {"DISPLAY": ":99", "XAUTHORITY": "/run/agentos-browser/xauth", "GTK_THEME": "Adwaita"}
WANT_GAP_S = 10.0             # the dock's Browser opens the browser at most this often
READ_MAX = 4096               # the longest line the node reads from the helper
STATUS_AGAIN_S = 4.0          # the same status is said again at most this often (the top bar's "doing" ages out)
SCREEN_MAX = 16384            # a window's numbers past this are not a window
# what the top bar says the agent is doing, by the tool it calls
DOING = {"terminal_exec": "terminal", "terminal_job_start": "terminal", "file_read": "reading",
         "file_write": "editing", "file_edit": "editing", "screen_act": "desktop", "screen_open": "desktop"}
# the browser tools that act in the browser (and so bring it to the front); reads like browser_tabs too
FOCUS_BROWSER_PREFIX = "browser_"


class _Link:
    """One environment's socket: opened on first use, written without waiting, dropped on any error."""

    def __init__(self, path: str, on_want: Any = None, on_win: Any = None):
        self.path = path
        self.on_want = on_want  # called with "browser" when the dock asks for the browser
        self.on_win = on_win  # called with the browser window's area (x, y, w, h), or None when it is not shown
        self.reader: asyncio.Task | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.opening: asyncio.Task | None = None
        self.pending: list[bytes] = []
        self.failed_at = 0.0
        self.dropped = 0

    def send(self, msg: dict[str, Any]) -> None:
        line = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        w = self.writer
        if w is not None and not w.is_closing():
            if w.transport.get_write_buffer_size() > BUFFER_MAX:
                self.dropped += 1
                return
            try:
                w.write(line)
            except (OSError, RuntimeError):
                self.close()
            return
        if sum(len(p) for p in self.pending) + len(line) > BUFFER_MAX:
            self.dropped += 1
            return
        self.pending.append(line)
        self._open()

    def _open(self) -> None:
        if self.opening is not None or time.monotonic() - self.failed_at < RETRY_S:
            return
        if not os.path.exists(self.path):
            self.pending.clear()
            self.failed_at = time.monotonic()
            return
        self.opening = asyncio.get_running_loop().create_task(self._connect(), name="desk-link")

    async def _connect(self) -> None:
        try:
            r, w = await asyncio.wait_for(asyncio.open_unix_connection(self.path, limit=READ_MAX), 3.0)
        except (OSError, TimeoutError):
            self.failed_at = time.monotonic()
            self.pending.clear()
            return
        finally:
            self.opening = None
        self.writer = w
        self.reader = asyncio.get_running_loop().create_task(self._read(r, w), name="desk-link-read")
        lines, self.pending = self.pending, []
        for line in lines:
            w.write(line)

    async def _read(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        """What the helper sends back: only the dock's ask for the browser is acted on."""
        try:
            while True:
                line = await r.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg == {"t": "want", "what": "browser"} and self.on_want is not None:
                    self.on_want("browser")
                elif isinstance(msg, dict) and msg.get("t") == "win" and self.on_win is not None:
                    self.on_win(window_area(msg))
        except (OSError, ValueError, asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            return
        if self.writer is w:  # the helper went (the screen restarted): the next send opens a new link
            self.reader = None
            self.close()

    def close(self) -> None:
        w, self.writer = self.writer, None
        reader, self.reader = self.reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        if w is not None:
            with contextlib.suppress(Exception):
                w.close()


def window_area(msg: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """The helper's {"t": "win", ...} as (x, y, width, height), or None when the window is not shown or the
    numbers are not a window's (not whole numbers, or outside 0..SCREEN_MAX)."""
    if msg.get("shown") is not True:
        return None
    vals = [msg.get(k) for k in ("x", "y", "w", "h")]
    if not all(type(v) is int for v in vals):
        return None
    x, y, w, h = vals
    if not (-SCREEN_MAX <= x <= SCREEN_MAX and -SCREEN_MAX <= y <= SCREEN_MAX and 0 < w <= SCREEN_MAX
            and 0 < h <= SCREEN_MAX):
        return None
    return x, y, w, h


def placer(area: tuple[int, int, int, int], screen: tuple[int, int], bars: dict[str, float]) -> Any:
    """How a point of the browser's page lands on the screen, given the window's visible `area`: the page fills
    the window's width and its bottom (the browser's bars are above it), so with a page size read with the
    point, its top is the window's bottom less the page's height, and a page zoomed in or out scales by the
    window's width over the page's. `bars` keeps that height of the bars and the scale, for a point placed
    again after the window moved or was resized (its page size is from before). The point is kept inside the
    window and the screen; None when the page's size is not a page's."""
    x0, y0, w, h = area
    sw, sh = screen

    def place(x: float, y: float, vw: float, vh: float, fresh: bool = True) -> tuple[int, int, int, int] | None:
        if not (0 < vw <= SCREEN_MAX and 0 < vh <= SCREEN_MAX):
            return None
        if fresh or "top" not in bars:
            bars["scale"] = min(4.0, max(0.25, w / vw))
            bars["top"] = min(max(h - vh * bars["scale"], 0.0), h / 2)
        scale, top = bars["scale"], y0 + bars["top"]
        sx = min(max(x0 + x * scale, x0), x0 + w - 1)
        sy = min(max(top + y * scale, top), y0 + h - 1)
        return round(min(max(sx, 0), sw - 1)), round(min(max(sy, 0), sh - 1)), sw, sh

    return place


class Tap:
    """One terminal_exec's view in the window: begin when made, its output as execs reads it, end with its result."""

    def __init__(self, link: _Link, cmd: str, cwd: str | None, show: bool):
        self.link = link
        self.id = secrets.token_hex(4)
        self.sent = 0
        self.dropped = 0
        self.began = time.monotonic()
        self._dec = {"o": codecs.getincrementaldecoder("utf-8")("replace"),
                     "e": codecs.getincrementaldecoder("utf-8")("replace")}
        link.send({"t": "begin", "id": self.id, "cmd": cmd[:4000], "cwd": cwd or "", "show": show})

    def feed(self, which: str, data: bytes) -> None:
        room = MIRROR_BYTES - self.sent
        if room <= 0:
            self.dropped += len(data)
            return
        part = data[:room]
        self.dropped += len(data) - len(part)
        self.sent += len(part)
        text = self._dec.get(which, self._dec["o"]).decode(part)
        if text:
            self.link.send({"t": "out", "id": self.id, "s": "e" if which == "e" else "o", "d": text})

    def job(self, job_id: str) -> None:
        """A job (terminal_job_start) started with this command: the window says so; its output stays with the job."""
        self.link.send({"t": "end", "id": self.id, "job": job_id})

    def end(self, res: Any, *, cancelled: bool = False) -> None:
        msg: dict[str, Any] = {"t": "end", "id": self.id, "dropped": self.dropped,
                               "dur": round(time.monotonic() - self.began, 3)}
        if res is None:
            msg["cancelled"] = cancelled
            msg["exit"] = None
            msg["sig"] = "stopped" if cancelled else "failed"
        else:
            msg.update(exit=getattr(res, "exit_code", None), sig=getattr(res, "signal", None),
                       timeout=bool(getattr(res, "timed_out", False)),
                       dur=float(getattr(res, "duration_s", msg["dur"]) or msg["dur"]))
        self.link.send(msg)


class DeskLinks:
    """Every environment's desktop link, for the environments service `svc` (its store, tunnels, lanes) and
    its VmBrowsers `vm` (which screens run the desktop)."""

    def __init__(self, svc: Any, vm: Any):
        self.svc = svc
        self.vm = vm
        self._links: dict[str, _Link] = {}
        self._focused: dict[str, tuple[str, float]] = {}  # environment -> (window asked for, when)
        self._wanted: dict[str, float] = {}  # environment -> when the dock last asked for the browser
        self._opening: set[asyncio.Task] = set()
        self._said: dict[str, tuple[tuple[Any, ...], float]] = {}  # environment -> the last status, when
        self._doing: dict[str, str] = {}  # environment -> what its agent did last
        self._bars: dict[str, dict[str, float]] = {}  # environment -> its browser's bars (placer)
        self._listening = False

    # ---- which environment, and may its windows move
    def _env_of(self, lane_id: str) -> Any:
        found = self.svc.env_of_lane(lane_id) if isinstance(lane_id, str) else None
        if found is None:
            return None
        env = found[0]
        if getattr(env, "browser", None) != "vm" or not self.vm.desktops.get(env.browser_lane):
            return None
        return env

    def _link(self, env: Any) -> _Link | None:
        tunnel = self.svc.tunnel_of(env.id)
        path = getattr(tunnel, "desk_path", None) if tunnel is not None and tunnel.alive else None
        if not path:
            return None
        link = self._links.get(env.id)
        if link is None or link.path != path:
            if link is not None:
                link.close()
            link = self._links[env.id] = _Link(path, on_want=lambda what, i=env.id: self.want(i, what),
                                               on_win=lambda area, e=env: self.window(e, area))
        return link

    def _agent_has(self, env: Any) -> bool:
        """Is the browser lane the agent's now? A person who took over or paused it keeps the screen."""
        lanes = self.svc._get("lanes") if hasattr(self.svc, "_get") else None
        control = getattr(lanes, "control", None)
        if control is None:
            return True
        try:
            state = control(env.browser_lane).state
        except Exception:  # noqa: BLE001 - the lane is not there (yet): nobody holds it
            return True
        return str(getattr(state, "value", state)) == "agent"

    # ---- the hooks
    def mirror(self, lane_id: str, cmd: str, cwd: str | None) -> Tap | None:
        """openajar.env.manager's exec_mirror: a Tap for a command on an environment's terminal lane whose
        screen runs the desktop, else None."""
        env = self._env_of(lane_id)
        if env is None or lane_id != env.terminal_lane:
            return None
        link = self._link(env)
        if link is None:
            return None
        show = self._agent_has(env)
        if show:
            self._focused[env.id] = ("terminal", time.monotonic())
            POINTERS.hide(env.browser_lane, "terminal")  # the agent works in its terminal now
        return Tap(link, cmd, cwd, show)

    def screen_env(self, lane_id: str) -> dict[str, str]:
        """openajar.env.manager's screen_env: DISPLAY and XAUTHORITY for a command on an environment's
        terminal lane while its screen runs the desktop and the agent has it (none while a person has
        control or stopped the agent)."""
        env = self._env_of(lane_id)
        if env is None or lane_id != env.terminal_lane or not self._agent_has(env):
            return {}
        return dict(SCREEN_ENV)

    def focus(self, lane_id: str, what: str) -> None:
        """Bring `what` ("browser" or "terminal") to the front for a tool call on `lane_id`, when it is not
        the window last asked for (or was asked for over FOCUS_GAP_S ago) and the agent has the lane."""
        env = self._env_of(lane_id)
        if env is None:
            return
        last = self._focused.get(env.id)
        now = time.monotonic()
        if last is not None and last[0] == what and now - last[1] < FOCUS_GAP_S:
            return
        if not self._agent_has(env):
            return
        link = self._link(env)
        if link is None:
            return
        self._focused[env.id] = (what, now)
        link.send({"t": "focus", "what": what})

    def before_call(self, tool: str, lane_id: Any) -> None:
        """For openajar.environments.routing: a tool call on an environment's lane, before it runs."""
        try:
            if not isinstance(lane_id, str) or not lane_id.startswith("env/"):
                return
            if tool.startswith(FOCUS_BROWSER_PREFIX) and tool not in ("browser_tabs",):
                self.focus(lane_id, "browser")
            doing = "browser" if tool.startswith(FOCUS_BROWSER_PREFIX) else DOING.get(tool)
            env = self._env_of(lane_id)
            if doing and env is not None:
                self._doing[env.id] = doing
                self.status(env)
        except Exception:  # never in a tool call's way
            log.debug("desktop focus failed for %s", lane_id, exc_info=True)

    def hello(self, env: Any) -> None:
        """Open the link to a helper that just started, so the dock can ask for the browser and the top bar knows
        the agent before the agent runs anything (the helper ignores the hello itself)."""
        link = self._link(env)
        if link is not None:
            link.send({"t": "hello"})
            self._said.pop(env.id, None)
            self.status(env)

    def greet(self, env: Any) -> None:
        """hello, when the node has no link to this environment's helper yet (the node restarted under a
        running screen): the dock's Browser and the top bar know the node is there before the agent's next call."""
        if env.id not in self._links:
            self.hello(env)

    # ---- the top bar
    def status(self, env: Any) -> None:
        """Tell the top bar who has control and what the agent does, when that changed (or the same again after
        STATUS_AGAIN_S, so "is using the browser" stays while the agent keeps at it)."""
        self._listen()
        link = self._link(env)
        if link is None:
            return
        control = self._control(env)
        said = (env_agent_name(env) or "", control, self._doing.get(env.id, ""))
        last = self._said.get(env.id)
        now = time.monotonic()
        if last is not None and last[0] == said and now - last[1] < STATUS_AGAIN_S:
            return
        self._said[env.id] = (said, now)
        link.send({"t": "status", "agent": said[0], "control": control, "doing": said[2],
                   "tz": round(time.localtime().tm_gmtoff / 60)})

    def _control(self, env: Any) -> str:
        lanes = self.svc._get("lanes") if hasattr(self.svc, "_get") else None
        control = getattr(lanes, "control", None)
        try:
            state = control(env.browser_lane).state if control is not None else "agent"
        except Exception:  # noqa: BLE001 - the lane is not there (yet): the agent's
            return "agent"
        state = str(getattr(state, "value", state))
        return state if state in ("agent", "human", "paused") else "agent"

    def _listen(self) -> None:
        """Hear the browser lanes' control change once (a take over, a hand back, a stop) for the top bar."""
        if self._listening:
            return
        lanes = self.svc._get("lanes") if hasattr(self.svc, "_get") else None
        add = getattr(lanes, "add_control_listener", None)
        if callable(add):
            add(self._control_changed)
            self._listening = True

    def _control_changed(self, lane_id: str, _old: Any, _new: Any) -> None:
        try:
            env = self._env_of(lane_id)
            if env is not None and lane_id == env.browser_lane:
                self._doing.pop(env.id, None)
                self.status(env)
        except Exception:
            log.debug("desktop status failed for %s", lane_id, exc_info=True)

    # ---- where the browser window is, for the agent's pointer
    def window(self, env: Any, area: tuple[int, int, int, int] | None) -> None:
        size_of = getattr(self.vm, "screen_size", None)
        size = size_of(env.id) if callable(size_of) else None
        if area is None or size is None:
            POINTERS.place_with(env.browser_lane, None)
            POINTERS.hide(env.browser_lane, "away")
            return
        POINTERS.place_with(env.browser_lane, placer(area, size, self._bars.setdefault(env.id, {})))
        POINTERS.replace(env.browser_lane)

    def want(self, env_id: str, what: str) -> None:
        """The dock's Browser, with no browser running: open the environment's browser lane, as whoever has its
        control now (a person who took over, else the agent). The message comes from the machine (the agent's,
        root there could send it), so it never passes the node's gates: nothing opens while the agent is
        stopped or the lane is frozen. Opening the browser is all it does: nothing is typed or read."""
        now = time.monotonic()
        if what != "browser" or now - self._wanted.get(env_id, -WANT_GAP_S) < WANT_GAP_S:
            return
        try:
            env = self.svc.store.get(env_id)
        except Exception:  # noqa: BLE001 - gone meanwhile
            return
        if not env.open or getattr(env, "state", "ready") != "ready":
            return
        self._wanted[env_id] = now
        task = asyncio.get_running_loop().create_task(self._open_browser(env), name=f"dock-browser-{env_id}")
        self._opening.add(task)
        task.add_done_callback(self._opening.discard)

    async def _open_browser(self, env: Any) -> None:
        lanes = self.svc._get("lanes") if hasattr(self.svc, "_get") else None
        if lanes is None:
            return
        # the message comes from the machine, which is the agent's: it is never taken as a person past the gates
        control = self._control(env)
        if control == "paused":
            return  # the agent is stopped there: nothing starts for it, a dock click included
        try:
            if control == "human":
                # a person has the screen, so the click is most likely theirs: opened as theirs, but only where
                # the node's gates (a freeze, a stop) would let the agent's own call through
                gates = getattr(lanes, "_check_gates", None)
                if not callable(gates):
                    return
                gates(env.browser_lane, "agent")
            await lanes.open(env.browser_lane, principal_kind="human" if control == "human" else "agent")
        except Exception as e:  # noqa: BLE001 - the agent's next browser call opens it
            log.info("environment %s: the dock could not open the browser: %s", env.id, getattr(e, "message", e))

    def forget(self, env_id: str) -> None:
        link = self._links.pop(env_id, None)
        self._focused.pop(env_id, None)
        self._said.pop(env_id, None)
        self._doing.pop(env_id, None)
        self._bars.pop(env_id, None)
        with contextlib.suppress(Exception):
            POINTERS.place_with(self.svc.store.get(env_id).browser_lane, None)
        if link is not None:
            link.close()


def install(svc: Any, vm: Any) -> DeskLinks:
    """Make the service's DeskLinks once and set the env manager's hooks to it."""
    links = getattr(svc, "_desk_links", None)
    if links is None:
        links = DeskLinks(svc, vm)
        svc._desk_links = links
    with contextlib.suppress(Exception):
        envs = svc.envs()
        envs.exec_mirror = links.mirror
        envs.screen_env = links.screen_env
    return links
