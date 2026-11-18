"""Runs on an environment's machine, never on the node: it starts and stops the environment's screen
and browser there (environments.toml browser = "vm"). The node sends this file with each call, over the
SSH connection it holds, as the `script` of a JSON request on stdin (agentos.environments.vmbrowser), so
the machine needs only what browser.sh installed and never runs an older copy of it.

Actions, each printing one JSON line:

- display {width, height}: Xvfb on :99, with a cookie of its own, no TCP listener; x11vnc on the unix
  socket /run/agentos-browser/vnc.sock (a folder only root can open) and no TCP listener either. The
  node's live view reaches it through a forward of that socket over SSH.
- browser {options, port, ws_path}: Camoufox's browser server (camoufox.server.launch_server) on that
  screen, listening on 127.0.0.1:<port> only, at a path the node made at random.
- clipboard {limit, timeout, nothing}: the text the browser copied, for the live view's "copy" button.
- stop {what: ["browser", "display"]}: ends them (each runs in a process group of its own).
- status: which of them run.

Standard library only: it runs before and outside the browser's own imports.
"""

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time

DIR = "/run/agentos-browser"
DISPLAY_NUM = 99
VNC_SOCK = DIR + "/vnc.sock"
XAUTH = DIR + "/xauth"
VENV_PY = "/opt/agentos-browser/venv/bin/python"
CACHE = "/opt/agentos-browser/cache"
X_SOCKET = f"/tmp/.X11-unix/X{DISPLAY_NUM}"
START_WAIT_S = 20.0
BROWSER_WAIT_S = 90.0
FAMILY_WILD = 65535  # an Xauthority entry for any host: the machine's name does not matter
# A video proof's recorder (recorder_ctl.py, agentos.environments.recording) records this screen: it
# ends with the screen, so it finishes its file first
RECORDER_PID = "/var/lib/agentos-env/recording/ffmpeg.pid"

# The browser server: the options the node sent, with the screen and window as Camoufox wants them.
SERVE = """
import json, os, sys
from camoufox.fingerprints import Screen
from camoufox.server import launch_server
path = sys.argv[1]
with open(path) as f:
    o = json.load(f)
os.unlink(path)
w, h = o.pop("screen")
o["screen"] = Screen(min_width=w, max_width=w, min_height=h, max_height=h)
o["window"] = tuple(o["window"])
o["env"] = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
launch_server(**o)
"""


def _say(**out):
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def _pidfile(name):
    return f"{DIR}/{name}.pid"


def _running(name):
    try:
        with open(_pidfile(name)) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _stop(name):
    pid = _running(name)
    if pid is not None:
        for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(pid, sig)
            except OSError:
                break
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and _running(name):
                time.sleep(0.1)
            if not _running(name):
                break
    try:
        os.unlink(_pidfile(name))
    except OSError:
        pass


def _spawn(name, argv, env):
    with open(f"{DIR}/{name}.log", "ab") as log:  # the child keeps its own copy of the file
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                start_new_session=True, close_fds=True)
    with open(_pidfile(name), "w") as f:
        f.write(str(proc.pid))
    return proc


def _tail(name, n=1500):
    try:
        with open(f"{DIR}/{name}.log", "rb") as f:
            return f.read()[-n:].decode(errors="replace")
    except OSError:
        return ""


def _wait(proc, name, ready, wait_s):
    deadline = time.monotonic() + wait_s
    while not ready():
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited (code {proc.returncode}): {_tail(name)}")
        if time.monotonic() > deadline:
            raise RuntimeError(f"{name} did not start within {wait_s:g} s: {_tail(name)}")
        time.sleep(0.1)


def _unix_listening(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex(path) == 0
    finally:
        s.close()


def _tcp_listening(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _write_xauthority(path):
    def counted(data):
        return len(data).to_bytes(2, "big") + data

    entry = (FAMILY_WILD.to_bytes(2, "big") + counted(b"") + counted(str(DISPLAY_NUM).encode())
             + counted(b"MIT-MAGIC-COOKIE-1") + counted(secrets.token_bytes(16)))
    try:
        os.unlink(path)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(entry)


def _base_env():
    env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
    env.update(DISPLAY=f":{DISPLAY_NUM}", XAUTHORITY=XAUTH, GDK_BACKEND="x11", MOZ_ENABLE_WAYLAND="0",
               XDG_CACHE_HOME=CACHE, HOME=env.get("HOME") or "/root")
    return env


def _stop_recorder():
    try:
        with open(RECORDER_PID) as f:
            pid = int(f.read().strip())
        os.killpg(pid, signal.SIGINT)
    except (OSError, ValueError):
        return
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[-1].split()[0] == "Z":
                    return  # exited; nobody reaped it yet
        except OSError:
            return
        time.sleep(0.1)


def display(req):
    _stop_recorder()
    _stop("browser")
    _stop("x11vnc")
    _stop("xvfb")
    width, height = int(req.get("width") or 1920), int(req.get("height") or 1080)
    _write_xauthority(XAUTH)
    for stale in (X_SOCKET, f"/tmp/.X{DISPLAY_NUM}-lock", VNC_SOCK):
        try:
            os.unlink(stale)
        except OSError:
            pass
    env = _base_env()
    xvfb = _spawn("xvfb", ["Xvfb", f":{DISPLAY_NUM}", "-screen", "0", f"{width}x{height}x24",
                           "-auth", XAUTH, "-nolisten", "tcp"], env)
    _wait(xvfb, "xvfb", lambda: os.path.exists(X_SOCKET), START_WAIT_S)
    # The node's own x11vnc flags (agentos.node.display): no TCP listener at all, the socket's folder is the gate.
    vnc = _spawn("x11vnc", ["x11vnc", "-display", f":{DISPLAY_NUM}", "-auth", XAUTH, "-rfbport", "0",
                            "-rfbportv6", "-1", "-unixsock", VNC_SOCK, "-noipv6", "-nopw", "-forever", "-shared",
                            "-norc", "-noremote", "-nocmds"], env)
    _wait(vnc, "x11vnc", lambda: _unix_listening(VNC_SOCK), START_WAIT_S)
    _say(ok=True, display=f":{DISPLAY_NUM}", width=width, height=height)


def browser(req):
    _stop("browser")
    if not _running("xvfb"):
        raise RuntimeError("the screen is not running; start it first")
    port = int(req["port"])
    opts = dict(req["options"], host="127.0.0.1", port=port, ws_path=req["ws_path"])
    path = f"{DIR}/browser-{secrets.token_hex(6)}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(opts, f)
    proc = _spawn("browser", [VENV_PY, "-I", "-c", SERVE, path], _base_env())
    _wait(proc, "browser", lambda: _tcp_listening(port), BROWSER_WAIT_S)
    _say(ok=True, port=port)


def clipboard(req):
    """The text on the screen's CLIPBOARD selection, as the node's live view reads a local one
    (agentos.node.lane_clipboard): "" when nothing is copied, `kind` too_large or unreadable."""
    limit = int(req.get("limit") or 1048576)
    nothing = [w.encode() for w in req.get("nothing") or []]
    try:
        out = subprocess.run(["xclip", "-o", "-selection", "clipboard", "-t", "UTF8_STRING"], env=_base_env(),
                             stdin=subprocess.DEVNULL, capture_output=True, check=False,
                             timeout=float(req.get("timeout") or 2.0))
    except FileNotFoundError:
        _say(ok=False, kind="unreadable", error="xclip is not installed on the machine")
        return
    except subprocess.TimeoutExpired:
        _say(ok=False, kind="timeout", error="the browser did not hand over what it copied in time")
        return
    if out.returncode != 0:
        err = out.stderr.strip().lower()
        if not err or any(w in err for w in nothing):
            _say(ok=True, text="")
            return
        _say(ok=False, kind="unreadable", error=out.stderr.decode(errors="replace").strip().splitlines()[0][:160])
        return
    if len(out.stdout) > limit:
        _say(ok=False, kind="too_large", error="what is copied is too large")
        return
    try:
        _say(ok=True, text=out.stdout.decode("utf-8"))
    except UnicodeDecodeError:
        _say(ok=False, kind="unreadable", error="what is copied is not UTF-8 text")


def stop(req):
    _stop_recorder()
    for name in req.get("what") or ["browser", "display"]:
        if name == "display":
            _stop("x11vnc")
            _stop("xvfb")
        else:
            _stop(name)
    _say(ok=True)


def status(_req):
    _say(ok=True, browser=bool(_running("browser")), display=bool(_running("xvfb") and _running("x11vnc")))


def main(req):
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    os.chmod(DIR, 0o700)
    action = {"display": display, "browser": browser, "clipboard": clipboard, "stop": stop,
              "status": status}.get(req.get("action"))
    if action is None:
        _say(ok=False, error=f"unknown action {req.get('action')!r}")
        return
    try:
        action(req)
    except Exception as e:  # noqa: BLE001 - the node reads the error from the JSON line
        _say(ok=False, error=str(e)[-1500:])


if __name__ == "__agentos_vm__":  # run by agentos.environments.vmbrowser.BOOT on the machine; never on import
    main(REQ)  # noqa: F821 - REQ is the request the node sent
