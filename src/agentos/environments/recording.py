"""Video proofs: a recording of an environment's browser, kept as proof like a screenshot. A mixin of
EnvironmentManager, and the browser_record tool.

browser_record(action="start") starts ffmpeg on the X display the environment's browser shows (the
machine's Xvfb for browser = "vm", the node's display of that lane for browser = "node"), through
recorder_ctl.py, which runs where that display is. The recorder is a process of its own: nothing
goes through the lane, so the agent's calls never wait for it, and the frames are the real screen
(clicks, typing, scrolling as they happened). browser_record(action="stop", proof="...") ends it,
makes an MP4 and a GIF preview there, brings both to the node over the environment's SSH connection
(EnvManager.get_archive, as environment_download does) and keeps them as one proof
(Proofs.keep_video): the same folder, links, retention and node-wide cap as a screenshot.

A recording stops by itself and keeps what it has: after environments.toml `record_max_s` (ffmpeg's
own limit, and the watcher here), when the environment is saved or closes (last_looks), and when the
browser goes away (its screen ends, so ffmpeg does; the watcher notices within POLL_S). At most one
recording runs for an environment. Only an environment's own browser is recorded: it holds no account
and no vault entry, and an account lane is never.

ffmpeg is in the docker image and the `dev` image (browser.sh). A machine from an older image gets it on
its first recording (apt, through the network's proxy when it is limited), and the result says so; a
machine whose network is `none` cannot, and is refused with recording_failed and the fix.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import shutil
import sys
import tarfile
import time
from dataclasses import dataclass
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from agentos.core import clock
from agentos.core.errors import InvalidRequest
from agentos.core.models import Effect
from agentos.core.tools import ToolContext, ToolSpec
from agentos.environments.errors import EnvironmentNotReady, RecordingFailed
from agentos.environments.models import Environment
from agentos.environments.proofs import GIF_BYTES_MAX, VIDEO_BYTES_MAX

log = logging.getLogger("agentos.environments")

VM_DIR = "/var/lib/agentos-env/recording"   # on the machine (vmbrowser_ctl.py stops it with the screen)
VM_DISPLAY, VM_XAUTH = ":99", "/run/agentos-browser/xauth"  # the machine's screen (vmbrowser_ctl.py)
VM_PY = "/opt/agentos-browser/venv/bin/python"  # there with the machine's browser (browser.sh)
FPS, WIDTH, CRF = 10, 1280, 30   # smooth enough for clicks and typing; a minute of a page is a few MB
GIF_S = 15                       # the preview lasts at most this long (a longer recording is sped up)
POLL_S = 15.0                    # the watcher asks whether ffmpeg still runs this often
CALL_TIMEOUT_S = 60.0
STOP_TIMEOUT_S = 240.0           # stop makes the MP4 and the GIF
FETCH_TIMEOUT_S = 300.0
INSTALL_TIMEOUT_S = 420.0
DEFAULT_TITLE = "browser recording"
FILES = ("rec.mp4", "rec.gif")
BOOT = ('import json,sys; d=json.load(sys.stdin); '
        'exec(compile(d.pop("script"),"recorder_ctl","exec"),{"__name__":"__agentos_recorder__","REQ":d})')
INSTALL = ("export DEBIAN_FRONTEND=noninteractive; command -v ffmpeg >/dev/null 2>&1 && exit 0; "
           "{ apt-get update -q && apt-get install -y -q --no-install-recommends ffmpeg; } "
           ">/tmp/agentos-recorder-install.log 2>&1 || { tail -c 600 /tmp/agentos-recorder-install.log; exit 1; }")
NO_FFMPEG_ON_NODE = ("this node has no ffmpeg, which records an environment's browser (browser = \"node\" records "
                     "the node's screen). Tell the human: sudo apt-get install ffmpeg on the node (deploy/install-node.sh "
                     "installs it). Meanwhile keep a screenshot: browser_screenshot(proof=\"...\")")
NO_NETWORK = ("this machine has no recorder (ffmpeg, which newer images have) and its network is none, so it cannot "
              "install one. Keep a screenshot instead: browser_screenshot(proof=\"...\"). The owner fixes it for next "
              "time with `agentos vm image build dev`, or by opening with network limited")


@dataclass
class _Rec:
    """One environment's recording while it runs."""
    where: str          # "vm" or "node": where ffmpeg runs
    folder: str         # its folder there
    title: str | None
    started_at: Any     # datetime
    began: float        # the loop's clock
    max_s: int
    task: asyncio.Task | None = None


def recorder_script() -> str:
    return resources.files("agentos.environments").joinpath("recorder_ctl.py").read_text()


def _unpack(tar: Path, dest: Path, names: list[str], limit: int) -> None:
    """The recording's files out of the machine's tar: regular files of those names only, each within
    `limit` bytes. Nothing else in it is written anywhere."""
    with tarfile.open(tar, "r:") as t:
        for member in t.getmembers():
            name = member.name.removeprefix("./")
            if name not in names or not member.isfile() or member.size > limit:
                continue
            src = t.extractfile(member)
            if src is None:
                continue
            with src, (dest / name).open("wb") as out:
                shutil.copyfileobj(src, out)


def _line(text: str) -> dict:
    line = next((ln for ln in reversed((text or "").splitlines()) if ln.startswith("{")), "")
    try:
        out = json.loads(line) if line else {}
    except ValueError:
        out = {}
    return out if isinstance(out, dict) else {}


class Recording:
    """For EnvironmentManager (its home, store, settings, runtime, locks, proofs and replay)."""

    def _recs(self) -> dict[str, _Rec]:
        return self.__dict__.setdefault("_recordings", {})

    def _recorded(self) -> dict[str, str]:
        """The proof id of each environment's last recording that stopped by itself."""
        return self.__dict__.setdefault("_recorded_by_itself", {})

    def recording_tasks(self) -> list[asyncio.Task]:
        return [r.task for r in self._recs().values() if r.task is not None]

    def recording_view(self, env: Environment) -> dict | None:
        """The recording that runs now, for environment(): its title, when it started and stops."""
        rec = self._recs().get(env.id)
        if rec is None:
            return None
        return {"title": rec.title, "started_at": clock.iso_or_none(rec.started_at, z=True),
                "stops_at": clock.iso_or_none(rec.started_at + timedelta(seconds=rec.max_s), z=True)}

    # ================================================================ where it runs
    def _screen(self, env: Environment) -> tuple[str, str | None] | None:
        """The X display the environment's browser shows and its Xauthority, or None while it has none."""
        displays = getattr(self._get("lanes"), "displays", None)
        viewer = displays.get(env.browser_lane) if displays is not None else None
        if viewer is None:
            return None
        if env.browser == "vm":
            return VM_DISPLAY, VM_XAUTH
        return str(viewer.display), viewer.xauthority

    def _record_dir(self, env_id: str) -> Path:
        return self.home / "environments" / env_id / "recording"

    async def _recorder(self, env: Environment, where: str, action: str, *, timeout_s: float = CALL_TIMEOUT_S,
                        **args: Any) -> dict:
        """One action of recorder_ctl.py where the screen is. Returns its JSON line ({ok: false, error}
        when it gave none)."""
        req = json.dumps({"script": recorder_script(), "action": action, **args})
        if where == "vm":
            res = await self.envs().exec(env.terminal_lane, f"{VM_PY} -I -c {shlex.quote(BOOT)}", stdin=req,
                                         timeout_s=timeout_s)
            out, said = _line(res.stdout or ""), (res.stderr or res.stdout or "")
        else:
            proc = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", BOOT, stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                        start_new_session=True)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(req.encode()), timeout_s)
            except TimeoutError:
                proc.kill()
                return {"ok": False, "kind": "failed", "error": f"the recorder did not answer within {timeout_s:g} s"}
            out, said = _line(stdout.decode(errors="replace")), stderr.decode(errors="replace")
        return out or {"ok": False, "kind": "failed", "error": said.strip()[-600:] or "the recorder said nothing"}

    async def _install_recorder(self, env: Environment, where: str) -> float:
        """ffmpeg on a machine from an older image: apt, through its network's proxy when it is limited.
        Returns how long it took."""
        if where != "vm":
            raise RecordingFailed(NO_FFMPEG_ON_NODE)
        if env.network == "none":
            raise RecordingFailed(NO_NETWORK)
        log.info("environment %s: installing the recorder (ffmpeg) on its machine (its image has none)", env.id)
        began = time.monotonic()
        res = await self.envs().exec(env.terminal_lane, INSTALL, timeout_s=INSTALL_TIMEOUT_S)
        if res.exit_code != 0:
            tail = (res.stdout or res.stderr or "").strip()[-400:]
            raise RecordingFailed(f"installing the recorder (ffmpeg) on the machine failed: {tail or 'no output'}. "
                                  "Keep a screenshot instead: browser_screenshot(proof=\"...\")")
        return time.monotonic() - began

    # ================================================================ start and stop
    async def record_start(self, env: Environment, *, title: str | None) -> dict:
        """Start recording the environment's browser. Refused while one runs or the browser has no screen."""
        if not env.open or env.state != "ready":
            raise EnvironmentNotReady(f"environment {env.id} is {env.state}; a recording needs it ready")
        async with self._lock(f"record:{env.id}"):
            if env.id in self._recs():
                raise InvalidRequest("a recording runs already; end it with browser_record(action=\"stop\", "
                                     "proof=\"...\") first")
            screen = self._screen(env)
            if screen is None:
                raise InvalidRequest("the browser is not open, so there is nothing to record yet: "
                                     "browser_navigate(url=...) first, then browser_record(action=\"start\")")
            where = "vm" if env.browser == "vm" else "node"
            folder = VM_DIR if where == "vm" else str(self._record_dir(env.id))
            max_s = int(self.settings.record_max_s)
            args = {"dir": folder, "display": screen[0], "xauth": screen[1], "max_s": max_s, "fps": FPS,
                    "width": WIDTH, "crf": CRF}
            out = await self._recorder(env, where, "start", **args)
            installed = None
            if out.get("kind") == "no_ffmpeg":
                took = await self._install_recorder(env, where)
                installed = (f"the machine had no recorder (ffmpeg), so it was installed first ({took:.0f} s); the "
                             "next recording on it starts at once")
                out = await self._recorder(env, where, "start", **args)
            if not out.get("ok"):
                raise RecordingFailed(f"the recording did not start: {str(out.get('error') or 'no reason')[:600]}")
            now = self._clock()
            rec = _Rec(where, folder, title, now, asyncio.get_running_loop().time(), max_s)
            self._recs()[env.id] = rec
            self._recorded().pop(env.id, None)
            rec.task = asyncio.create_task(self._watch(env.id), name=f"recording {env.id}")
        view = self.recording_view(env) or {}
        return {"recording": True, **view, "max_s": max_s, **({"installed": installed} if installed else {})}

    async def record_stop(self, env: Environment, *, title: str | None = None, cause: str = "stopped") -> dict | None:
        """End the environment's recording and keep it as proof. Returns the proof as environment()
        lists it, or None when none runs. Raises RecordingFailed when it could not be kept."""
        async with self._lock(f"record:{env.id}"):
            rec = self._recs().pop(env.id, None)
            if rec is None:
                return None
            if rec.task is not None and rec.task is not asyncio.current_task():
                rec.task.cancel()
            try:
                return await self._keep_recording(env, rec, title or rec.title or DEFAULT_TITLE, cause)
            finally:
                with contextlib.suppress(Exception):  # its files there go, kept or not
                    await self._recorder(env, rec.where, "discard", dir=rec.folder)

    async def _keep_recording(self, env: Environment, rec: _Rec, title: str, cause: str) -> dict:
        out = await self._recorder(env, rec.where, "stop", timeout_s=STOP_TIMEOUT_S, dir=rec.folder, gif_s=GIF_S,
                                   gif_max=GIF_BYTES_MAX)
        if not out.get("ok"):
            raise RecordingFailed(f"the recording was not kept: {str(out.get('error') or 'no reason')[:600]}")
        names = [f for f in out.get("files") or [] if f in FILES]
        if "rec.mp4" not in names:
            raise RecordingFailed("the recording was not kept: the recorder made no MP4")
        ended = str(out.get("ended") or "stopped")
        ended = cause if ended == "stopped" else ended
        inbox = self.home / "environments" / env.id / "recording-in"
        shutil.rmtree(inbox, ignore_errors=True)
        inbox.mkdir(parents=True)
        try:
            if rec.where == "vm":
                tar = inbox / "rec.tar"
                await self.envs().get_archive(env.terminal_lane, rec.folder, tar, paths=names, gzip=False,
                                              timeout_s=FETCH_TIMEOUT_S,
                                              max_bytes=VIDEO_BYTES_MAX + GIF_BYTES_MAX + (1 << 20))
                await asyncio.to_thread(_unpack, tar, inbox, names, VIDEO_BYTES_MAX + 1)
                here = inbox
            else:
                here = Path(rec.folder)
            gif = here / "rec.gif"
            proof = self.keep_video(env, here / "rec.mp4", gif if gif.is_file() else None, title=title, ended=ended)
        finally:
            shutil.rmtree(inbox, ignore_errors=True)
        log.info("environment %s: kept a recording of %s s as proof %s (%s)", env.id, proof.get("duration_s"),
                 proof["id"], ended)
        return proof

    async def record_end(self, env: Environment, cause: str) -> None:
        """The recording that runs, stopped and kept: the environment is saved or closes, or it ended by
        itself. Never raises."""
        if env.id not in self._recs():
            return
        try:
            proof = await asyncio.wait_for(self.record_stop(env, cause=cause), STOP_TIMEOUT_S + FETCH_TIMEOUT_S)
            if proof is not None:
                self._recorded()[env.id] = proof["id"]
        except Exception:
            log.warning("environment %s: its recording was not kept (%s)", env.id, cause, exc_info=True)

    async def last_looks(self, env: Environment, cause: str) -> None:
        """Before a save or a close: the recording that runs is kept, and the replay's last frame taken."""
        await self.record_end(env, cause)
        await self.replay_last(env, cause)

    async def _watch(self, env_id: str) -> None:
        """Ends a recording that passed its length, or whose ffmpeg ended (the browser went away)."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(POLL_S)
            rec = self._recs().get(env_id)
            if rec is None:
                return
            env = self.store.get(env_id)
            done = loop.time() - rec.began >= rec.max_s + 2 or not env.open
            if not done:
                with contextlib.suppress(Exception):
                    st = await self._recorder(env, rec.where, "status", dir=rec.folder)
                    done = bool(st.get("ok")) and not st.get("running")
            if done:
                rec.task = None  # record_stop must not cancel this task while it keeps the recording
                await self.record_end(env, "limit")
                return

    def stopped_by_itself(self, env: Environment) -> str | None:
        return self._recorded().get(env.id)


# ------------------------------------------------------------------ the tool
async def browser_record(ctx: ToolContext, *, on: str, action: str, proof: str | None = None) -> dict:
    """A video of your environment's browser as proof: start, do the flow, then stop with a title.
    Kept like a screenshot proof (an MP4 and a GIF preview, with links); environment_pull_request
    shows it. It stops by itself after a few minutes, or when the environment closes. The first one
    on an older machine installs the recorder (about a minute).

    Args:
        action: "start", or "stop" to keep it.
        proof: Its title for the human ("guest checkout, end to end"), at start or stop.
    """
    from agentos.environments.manager import EnvironmentManager
    from agentos.node.tools.shots import check_title

    if action not in ("start", "stop"):
        raise InvalidRequest('action is "start" or "stop"')
    title = check_title(proof)
    mgr: EnvironmentManager = ctx.runtime.require("environments")
    env = mgr.browser_environment(on)
    if env is None:
        raise InvalidRequest("browser_record records an environment's browser only: name it with `on`, or leave "
                             "`on` out in a chat with an environment")
    await ctx.authorize(Effect.READ, lane_id=on, summary=f"{action} a recording of {on}"
                        + (f" as proof: {title}" if title else ""))
    if action == "start":
        out = await mgr.record_start(env, title=title)
        ctx.trace("browser_record", lane=on, environment=env.id, action="start", title=title)
        out["next"] = ("Do the flow now (browser_navigate, browser_act); then browser_record(action=\"stop\", "
                       "proof=\"...\"). It stops by itself at stops_at")
        return out
    kept = await mgr.record_stop(env, title=title)
    if kept is None:
        earlier = mgr.stopped_by_itself(env)
        raise InvalidRequest("no recording runs: browser_record(action=\"start\") first"
                             + (f". The last one stopped by itself and is kept as proof {earlier} (environment() "
                                "lists it)" if earlier else ""))
    ctx.trace("browser_record", lane=on, environment=env.id, action="stop", proof=kept["id"], title=kept["title"],
              duration_s=kept.get("duration_s"), bytes=kept.get("bytes"))
    return {"proof": kept, "next": "Give the human proof.url (the MP4; proof.gif_url is a short preview). They work "
                                   "for a few minutes; environment() gives fresh ones. environment_pull_request puts "
                                   "it in the pull request"}


def tool_specs(runtime: Any) -> list[ToolSpec]:
    return [ToolSpec("browser_record", browser_record, Effect.READ, tags=("environments", "files"),
                     hidden_without="environments")]
