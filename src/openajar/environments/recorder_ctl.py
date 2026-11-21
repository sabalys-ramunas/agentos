"""Records the screen an environment's browser shows, as a video proof (openajar.environments.recording).

Runs where that screen is: on the environment's machine for browser = "vm" (sent over the node's SSH
connection with each call, as vmbrowser_ctl.py is), or on the node for browser = "node". Never imported
for its actions. The node sends this file as the `script` of a JSON request on stdin.

ffmpeg grabs the X display (x11grab) in a process of its own (its own session, niced), so it never
gets in the way of the browser or the agent's calls: nothing goes through the lane. It writes Matroska
while it records, which stays readable when the recording is cut short (the screen went away, a kill),
and ends by itself after max_s. `stop` makes the MP4 (H.264, yuv420p, the moov first so a browser
plays it before it has it all) and a short animated GIF preview (sped up to at most gif_s seconds).

Actions, each printing one JSON line:

- start {dir, display, xauth, max_s, fps, width, crf}: `kind: "no_ffmpeg"` when ffmpeg is not there.
- stop {dir, gif_s, gif_max}: ends the recording (or finds it ended) and makes rec.mp4 and rec.gif.
- status {dir}: is it running, and for how long.
- discard {dir}: ends it and deletes its files.

Standard library only.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time

REC, MP4, GIF = "rec.mkv", "rec.mp4", "rec.gif"
PID, STATE, LOG = "ffmpeg.pid", "rec.json", "rec.log"
START_CHECK_S = 1.0     # ffmpeg that cannot open the screen exits within this
STOP_WAIT_S = 15.0      # after SIGINT: ffmpeg writes the rest and exits
ENCODE_TIMEOUT_S = 150.0
MIN_BYTES = 1024
# the GIF preview, smaller each try until it fits gif_max: (width, frames a second, colours)
GIF_TRIES = ((640, 8, 128), (480, 6, 64), (360, 5, 48))


def record_argv(display, out, *, max_s, fps, width, crf):
    """ffmpeg grabbing `display` into `out`: at most `width` pixels wide (even), `fps` frames a second,
    H.264 in yuv420p (what every browser plays), ending after max_s by itself. No secret is on it: the
    display's cookie is in the XAUTHORITY file its environment names."""
    scale = f"scale=w='trunc(min({int(width)},iw)/2)*2':h=-2:flags=bicubic"
    return ["nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "x11grab", "-draw_mouse", "1", "-framerate", str(int(fps)), "-i", str(display),
            "-t", str(int(max_s)), "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
            "-pix_fmt", "yuv420p", "-g", str(int(fps) * 5), "-f", "matroska", str(out)]


def mp4_argv(src, dst):
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-c", "copy",
            "-movflags", "+faststart", str(dst)]


def gif_argv(src, dst, *, speed, width, fps, colors):
    """The preview: `speed` times faster, `fps` frames a second, `width` wide, one palette for it all."""
    graph = (f"setpts=PTS/{speed:.3f},fps={int(fps)},scale={int(width)}:-2:flags=lanczos,split[a][b];"
             f"[a]palettegen=max_colors={int(colors)}:stats_mode=diff[p];"
             "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vf", graph,
            "-loop", "0", str(dst)]


def gif_speed(duration_s, gif_s):
    return max(1.0, float(duration_s) / max(1.0, float(gif_s)))


def _say(**out):
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def _path(req, name):
    return os.path.join(req["dir"], name)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # an exited child nobody reaped yet is not running
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return True


def _running(req):
    try:
        with open(_path(req, PID)) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def _state(req):
    try:
        with open(_path(req, STATE)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _tail(req, n=600):
    try:
        with open(_path(req, LOG), "rb") as f:
            return f.read()[-n:].decode(errors="replace").strip()
    except OSError:
        return ""


def _end(pid):
    """SIGINT first: ffmpeg finishes the file. Then TERM, then KILL."""
    if pid <= 1:  # killpg(1) would signal every process of this user, killpg(0) our own group
        return
    for sig, wait in ((signal.SIGINT, STOP_WAIT_S), (signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pid, sig)
        except OSError:
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not _alive(pid):
                return
            time.sleep(0.1)


def _clear(req, names):
    for name in names:
        try:
            os.unlink(_path(req, name))
        except OSError:
            pass


def start(req):
    os.makedirs(req["dir"], mode=0o700, exist_ok=True)
    if _running(req):
        _say(ok=False, kind="running", error="a recording runs already")
        return
    if shutil.which("ffmpeg") is None:
        _say(ok=False, kind="no_ffmpeg", error="ffmpeg is not installed")
        return
    _clear(req, (REC, MP4, GIF, PID, STATE, LOG))
    env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "XAUTHORITY")}
    env["DISPLAY"] = str(req["display"])
    if req.get("xauth"):
        env["XAUTHORITY"] = str(req["xauth"])
    argv = record_argv(req["display"], _path(req, REC), max_s=req["max_s"], fps=req["fps"], width=req["width"],
                       crf=req["crf"])
    with open(_path(req, LOG), "ab") as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                start_new_session=True, close_fds=True)
    with open(_path(req, PID), "w") as f:
        f.write(str(proc.pid))
    with open(_path(req, STATE), "w") as f:
        json.dump({"started": time.time(), "max_s": int(req["max_s"])}, f)
    time.sleep(START_CHECK_S)
    if proc.poll() is not None:
        _say(ok=False, kind="failed", error=f"ffmpeg exited (code {proc.returncode}): {_tail(req)}")
        return
    _say(ok=True, pid=proc.pid)


def _probe(path):
    """(seconds, width, height) as ffprobe reads the file; zeros when it cannot."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height:format=duration", "-of", "json", str(path)],
                             capture_output=True, check=False, timeout=30)
        data = json.loads(out.stdout or b"{}")
        stream = (data.get("streams") or [{}])[0]
        return (float((data.get("format") or {}).get("duration") or 0), int(stream.get("width") or 0),
                int(stream.get("height") or 0))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0, 0, 0


def _run(argv):
    out = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=ENCODE_TIMEOUT_S)
    return out.returncode, out.stderr.decode(errors="replace").strip()[-400:]


def _gif(req, duration_s):
    """The preview, made smaller until it fits gif_max; None when no try fits."""
    dst = _path(req, GIF)
    speed = gif_speed(duration_s, req.get("gif_s") or 15)
    for width, fps, colors in GIF_TRIES:
        code, _err = _run(gif_argv(_path(req, MP4), dst, speed=speed, width=width, fps=fps, colors=colors))
        if code == 0 and 0 < os.path.getsize(dst) <= int(req.get("gif_max") or 8 << 20):
            return {"bytes": os.path.getsize(dst), "width": width, "speed": round(speed, 2)}
    _clear(req, (GIF,))
    return None


def stop(req):
    pid = _running(req)
    if pid:
        _end(pid)
    _clear(req, (PID,))
    rec = _path(req, REC)
    if not os.path.exists(rec) or os.path.getsize(rec) < MIN_BYTES:
        _say(ok=False, kind="empty", error=f"nothing was recorded: {_tail(req) or 'the screen showed nothing'}")
        return
    code, err = _run(mp4_argv(rec, _path(req, MP4)))
    if code != 0:
        _say(ok=False, kind="failed", error=f"the MP4 was not made: {err}")
        return
    _clear(req, (REC,))
    duration, width, height = _probe(_path(req, MP4))
    # not running when asked: it reached its length, or its screen went away (the browser closed)
    ended = "stopped" if pid else ("limit" if duration >= float(_state(req).get("max_s") or 0) - 1 else "ended")
    gif = _gif(req, duration)
    _say(ok=True, ended=ended, duration_s=round(duration, 2), width=width, height=height,
         bytes=os.path.getsize(_path(req, MP4)), gif=gif, files=[MP4] + ([GIF] if gif else []))


def status(req):
    pid = _running(req)
    state = _state(req)
    started = float(state.get("started") or 0)
    _say(ok=True, running=bool(pid), elapsed_s=round(time.time() - started, 1) if started else None)


def discard(req):
    pid = _running(req)
    if pid:
        _end(pid)
    shutil.rmtree(req["dir"], ignore_errors=True)
    _say(ok=True)


def main(req):
    action = {"start": start, "stop": stop, "status": status, "discard": discard}.get(req.get("action"))
    if action is None or not str(req.get("dir") or "").startswith("/"):
        _say(ok=False, error=f"unknown action {req.get('action')!r} or no dir")
        return
    try:
        action(req)
    except Exception as e:  # noqa: BLE001 - the node reads the error from the JSON line
        _say(ok=False, kind="failed", error=str(e)[-1500:])


if __name__ == "__openajar_recorder__":  # run by openajar.environments.recording.BOOT; never on import
    main(REQ)  # noqa: F821 - REQ is the request the node sent
