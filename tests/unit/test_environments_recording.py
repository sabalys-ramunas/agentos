"""Video proofs (agentos.environments.recording, recorder_ctl, media, proofs): browser_record's start,
stop, limits and refusals, the ffmpeg command lines, the proof records, their links and range serving,
and retention with videos counted."""

from __future__ import annotations

import io
import json
import shutil
import struct
import subprocess
import sys
import tarfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from test_environments import node, opened  # noqa: F401  (node is a fixture)
from test_environments_isolation import _record, panel  # noqa: F401  (panel is a fixture)

from agentos.core.errors import InvalidRequest
from agentos.core.models import utcnow
from agentos.env.models import ExecResult
from agentos.environments import media, recorder_ctl, recording
from agentos.environments import proofs as proofs_mod
from agentos.environments.errors import RecordingFailed
from agentos.node.viewer_tokens import ViewTokens
from agentos.policy.wrap import ToolCallError, bind_request_token, wrap_tool

FFMPEG = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else None)
FFPROBE = shutil.which("ffprobe") or ("/opt/homebrew/bin/ffprobe" if Path("/opt/homebrew/bin/ffprobe").exists()
                                      else None)


def _box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(body), kind) + body


def mp4(seconds: float = 12.5, width: int = 1280, height: int = 720, pad: int = 2000) -> bytes:
    """A small MP4 with a movie header and a video track header, as ffmpeg writes them (+faststart)."""
    mvhd = b"\0\0\0\0" + struct.pack(">IIII", 0, 0, 1000, int(seconds * 1000)) + b"\0" * 80
    tkhd = (b"\0\0\0\x03" + struct.pack(">IIIII", 0, 0, 1, 0, int(seconds * 1000)) + b"\0" * 16 + b"\0" * 36
            + struct.pack(">II", width << 16, height << 16))
    return (_box(b"ftyp", b"isom\0\0\x02\0isomiso2avc1mp41") + _box(b"moov", _box(b"mvhd", mvhd)
            + _box(b"trak", _box(b"tkhd", tkhd))) + _box(b"mdat", b"\0" * pad))


def gif(width: int = 640, height: int = 360) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\0" * 64


def token_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["t"][0]


@pytest.fixture
def tokens(node, tmp_path):  # noqa: F811
    t = ViewTokens(tmp_path / "viewer.key")
    node["rt"].register("viewer_links", t)
    return t


class Recorder:
    """recorder_ctl.py as the machine answers it: through the terminal lane's exec, with the request on
    stdin (never on the command line)."""

    def __init__(self, node, *, ffmpeg=True, start_ok=True, files=("rec.mp4", "rec.gif"), seconds=12.5):  # noqa: F811
        self.node, self.ffmpeg, self.start_ok, self.files, self.seconds = node, ffmpeg, start_ok, files, seconds
        self.requests: list[dict] = []
        self.commands: list[str] = []
        self.running = False
        self.installed = 0
        inner = node["machine"].exec

        async def exec(lane_id, cmd, *, timeout_s=None, stdin=None, **kw):
            if isinstance(cmd, str) and cmd.startswith(recording.VM_PY) and "recorder_ctl" in cmd:
                self.commands.append(cmd)
                return self.answer(json.loads(stdin))
            if cmd == recording.INSTALL:
                self.installed += 1
                self.ffmpeg = True
                return ExecResult(exit_code=0, stdout="", stderr="", duration_s=0.01)
            return await inner(lane_id, cmd, timeout_s=timeout_s, stdin=stdin, **kw)

        async def get_archive(lane_id, src, dest_file, *, paths=None, gzip=True, timeout_s=600, max_bytes=None):
            self.fetched = (src, paths, gzip, max_bytes)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                members = [("./rec.mp4", mp4(self.seconds)), ("../evil", b"x"), ("./other.txt", b"y")]
                if "rec.gif" in self.files:
                    members.append(("./rec.gif", gif()))
                for name, data in members:
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    t.addfile(info, io.BytesIO(data))
                link = tarfile.TarInfo("./rec.gif.link")
                link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
                t.addfile(link)
            Path(dest_file).write_bytes(buf.getvalue())
            return {"bytes": len(buf.getvalue()), "files": 2}

        node["envs"].exec = exec
        node["envs"].get_archive = get_archive

    def answer(self, req: dict) -> ExecResult:
        assert req.pop("script") == recording.recorder_script()
        self.requests.append(req)
        action = req["action"]
        if action == "start":
            if not self.ffmpeg:
                out = {"ok": False, "kind": "no_ffmpeg", "error": "ffmpeg is not installed"}
            elif not self.start_ok:
                out = {"ok": False, "kind": "failed", "error": "ffmpeg exited (code 1): cannot open display"}
            else:
                self.running = True
                out = {"ok": True, "pid": 4242}
        elif action == "status":
            out = {"ok": True, "running": self.running, "elapsed_s": 3.0}
        elif action == "stop":
            ended = "stopped" if self.running else "ended"
            self.running = False
            out = {"ok": True, "ended": ended, "duration_s": 999, "width": 1, "height": 1, "bytes": 1,
                   "files": list(self.files)}
        else:
            out = {"ok": True}
        return ExecResult(exit_code=0, stdout="starting\n" + json.dumps(out) + "\n", stderr="", duration_s=0.01)


async def vm_env(node, monkeypatch, **kw):  # noqa: F811
    env_id = (await opened(node))["id"]
    mgr = node["mgr"]
    mgr.store.update(env_id, lambda e: [setattr(e, k, v) for k, v in {"browser": "vm", **kw}.items()])
    monkeypatch.setattr(mgr, "_screen", lambda env: (recording.VM_DISPLAY, recording.VM_XAUTH))
    return mgr.get(env_id)


def tool(node):  # noqa: F811
    [spec] = recording.tool_specs(node["rt"])
    call = wrap_tool(spec, node["rt"])

    async def run(**kw):
        with bind_request_token(node["token"], session="chat-1"):
            return await call(**kw)

    return run


# ------------------------------------------------------------------ start and stop

async def test_a_recording_starts_stops_and_is_kept_as_a_video_proof(node, tokens, monkeypatch):  # noqa: F811
    env = await vm_env(node, monkeypatch)
    rec = Recorder(node)
    run = tool(node)
    started = await run(action="start", proof="guest checkout, end to end")
    assert started["recording"] is True and started["max_s"] == 180 and "stop" in started["next"]
    assert "installed" not in started
    [req] = rec.requests
    assert req == {"action": "start", "dir": recording.VM_DIR, "display": ":99", "xauth": recording.VM_XAUTH,
                   "max_s": 180, "fps": 10, "width": 1280, "crf": 30}
    assert all("guest" not in c and "{" not in c.split("-c", 1)[0] for c in rec.commands)  # the request is stdin
    seen = await node["call"]("environment")
    assert seen["recording"]["title"] == "guest checkout, end to end" and seen["recording"]["stops_at"]
    with pytest.raises(ToolCallError, match="runs already"):
        await run(action="start")
    kept = (await run(action="stop"))["proof"]
    assert kept["kind"] == "video" and kept["title"] == "guest checkout, end to end"
    assert kept["duration_s"] == 12.5 and (kept["width"], kept["height"]) == (1280, 720)  # read here, not told
    assert kept["ended"] == "stopped" and kept["gif"] == {"width": 640, "height": 360, "bytes": len(gif())}
    assert kept["url"].endswith(f"/proof/{kept['id']}.mp4?t={token_of(kept['url'])}")
    assert kept["gif_url"].split("?")[0].endswith(f"{kept['id']}.gif")
    assert rec.fetched == (recording.VM_DIR, ["rec.mp4", "rec.gif"], False, rec.fetched[3])
    assert [r["action"] for r in rec.requests] == ["start", "stop", "discard"]
    mgr = node["mgr"]
    t = token_of(kept["url"])
    assert mgr.signed_proof_path("env", f"{env.id}-browser", kept["id"], "mp4", t).read_bytes() == mp4()
    assert mgr.signed_proof_path("env", f"{env.id}-browser", kept["id"], "gif", t).read_bytes() == gif()
    assert mgr.signed_proof_path("env", f"{env.id}-browser", kept["id"], "png", t) is None
    folder = mgr.home / "environments" / env.id / "proofs"
    assert sorted(p.name for p in folder.iterdir()) == sorted(f"{kept['id']}.{x}" for x in ("gif", "json", "mp4"))
    assert not (mgr.home / "environments" / env.id / "recording-in").exists()  # nothing else of the tar
    assert "recording" not in await node["call"]("environment")
    closed = await node["call"]("environment_close", discard=True)
    assert [p["kind"] for p in closed["proofs"]] == ["video"]


async def test_stop_takes_the_title_and_without_a_recording_says_so(node, tokens, monkeypatch):  # noqa: F811
    await vm_env(node, monkeypatch)
    Recorder(node)
    run = tool(node)
    with pytest.raises(ToolCallError, match="no recording runs"):
        await run(action="stop", proof="x")
    await run(action="start")
    kept = (await run(action="stop", proof="  the cart,  after "))["proof"]
    assert kept["title"] == "the cart, after"
    with pytest.raises(ToolCallError, match="action is"):
        await run(action="pause")


async def test_it_records_only_an_open_browser_of_an_environment(node, tokens, monkeypatch):  # noqa: F811
    env = await vm_env(node, monkeypatch)
    Recorder(node)
    mgr = node["mgr"]
    monkeypatch.setattr(mgr, "_screen", lambda env: None)
    with pytest.raises(InvalidRequest, match="browser_navigate"):
        await mgr.record_start(env, title=None)
    run = tool(node)
    with pytest.raises(ToolCallError, match="environment's browser only"):
        await run(on="personal/reddit", action="start")


async def test_an_older_machine_gets_the_recorder_and_the_result_says_so(node, tokens, monkeypatch):  # noqa: F811
    await vm_env(node, monkeypatch)
    rec = Recorder(node, ffmpeg=False)
    started = await tool(node)(action="start")
    assert rec.installed == 1 and "installed first" in started["installed"]
    assert [r["action"] for r in rec.requests] == ["start", "start"]


async def test_a_machine_with_no_network_cannot_get_the_recorder(node, tokens, monkeypatch):  # noqa: F811
    env = await vm_env(node, monkeypatch, network="none")
    rec = Recorder(node, ffmpeg=False)
    with pytest.raises(RecordingFailed, match="network is none") as e:
        await node["mgr"].record_start(env, title=None)
    assert "browser_screenshot(proof=" in e.value.message and rec.installed == 0
    assert node["mgr"].recording_view(env) is None


async def test_a_node_without_ffmpeg_says_how_to_get_it(node, tokens, monkeypatch):  # noqa: F811
    env = node["mgr"].get((await opened(node))["id"])
    mgr = node["mgr"]
    monkeypatch.setattr(mgr, "_screen", lambda env: (":110", "/srv/agentos/display-110.xauth"))
    asked = []

    async def recorder(env, where, action, **kw):
        asked.append((where, action, kw))
        return {"ok": False, "kind": "no_ffmpeg"}

    monkeypatch.setattr(mgr, "_recorder", recorder)
    with pytest.raises(RecordingFailed, match="apt-get install ffmpeg"):
        await mgr.record_start(env, title=None)
    [(where, _, kw)] = asked
    assert where == "node" and kw["display"] == ":110" and kw["dir"] == str(mgr.home / "environments" / env.id
                                                                             / "recording")


async def test_a_failed_start_is_an_error_with_ffmpegs_words(node, tokens, monkeypatch):  # noqa: F811
    env = await vm_env(node, monkeypatch)
    Recorder(node, start_ok=False)
    with pytest.raises(RecordingFailed, match="cannot open display"):
        await node["mgr"].record_start(env, title=None)


# ------------------------------------------------------------------ it stops by itself

async def test_a_recording_that_ended_by_itself_is_kept(node, tokens, monkeypatch):  # noqa: F811
    env = await vm_env(node, monkeypatch)
    rec = Recorder(node)
    monkeypatch.setattr(recording, "POLL_S", 0.01)
    mgr = node["mgr"]
    await mgr.record_start(env, title="the flow")
    rec.running = False  # its length passed, or the screen went away
    import asyncio

    for _ in range(200):
        if mgr.recording_view(env) is None and mgr.proofs(env):
            break
        await asyncio.sleep(0.01)
    [kept] = mgr.proofs(env)
    assert kept["kind"] == "video" and kept["ended"] == "ended" and kept["title"] == "the flow"
    with pytest.raises(ToolCallError, match=f"stopped by itself and is kept as proof {kept['id']}"):
        await tool(node)(action="stop")


async def test_closing_or_saving_keeps_the_recording_that_runs(node, tokens, monkeypatch):  # noqa: F811
    env = await vm_env(node, monkeypatch)
    Recorder(node)
    await node["mgr"].record_start(env, title="before the close")
    closed = await node["call"]("environment_close", discard=True)
    [kept] = closed["proofs"]
    assert kept["ended"] == "close" and kept["title"] == "before the close"


# ------------------------------------------------------------------ the recorder's command lines

def test_the_record_command_grabs_the_display_and_holds_no_secret():
    argv = recorder_ctl.record_argv(":99", "/var/lib/agentos-env/recording/rec.mkv", max_s=180, fps=10, width=1280,
                                    crf=30)
    assert argv[:4] == ["nice", "-n", "10", "ffmpeg"] and "-nostdin" in argv
    assert argv[argv.index("-f") + 1] == "x11grab" and argv[argv.index("-i") + 1] == ":99"
    assert argv[argv.index("-t") + 1] == "180" and argv[argv.index("-framerate") + 1] == "10"
    assert argv[argv.index("-c:v") + 1] == "libx264" and argv[argv.index("-pix_fmt") + 1] == "yuv420p"
    assert "min(1280,iw)" in argv[argv.index("-vf") + 1] and argv[-1].endswith("rec.mkv")
    assert not any("xauth" in a.lower() or "cookie" in a.lower() for a in argv)  # the cookie is in XAUTHORITY
    assert "+faststart" in recorder_ctl.mp4_argv("a.mkv", "a.mp4")
    gif_cmd = " ".join(recorder_ctl.gif_argv("a.mp4", "a.gif", speed=4.0, width=640, fps=8, colors=128))
    assert "setpts=PTS/4.000" in gif_cmd and "palettegen" in gif_cmd and "scale=640" in gif_cmd
    assert recorder_ctl.gif_speed(180, 15) == 12 and recorder_ctl.gif_speed(5, 15) == 1


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="no ffmpeg here")
def test_the_recorder_records_stops_and_makes_the_mp4_and_a_small_gif(tmp_path):
    """recorder_ctl.py for real, as the node runs it (sys.executable -I -c BOOT), with an ffmpeg that
    plays a recording instead of grabbing a screen (no X here) and the real one for everything else."""
    sample = tmp_path / "sample.mkv"
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=10", "-t", "25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "matroska", str(sample)], check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ffmpeg"
    fake.write_text(f'#!/bin/sh\ncase "$*" in *x11grab*) for a; do last="$a"; done; cp "{sample}" "$last"; '
                    f'exec sleep 60;; esac\nexec "{FFMPEG}" "$@"\n')
    fake.chmod(0o755)
    (bin_dir / "ffprobe").symlink_to(FFPROBE)
    folder = tmp_path / "rec"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}

    def ctl(action, **kw):
        req = json.dumps({"script": recording.recorder_script(), "action": action, "dir": str(folder), **kw})
        out = subprocess.run([sys.executable, "-I", "-c", recording.BOOT], input=req, capture_output=True, text=True, check=False,
                             env=env, timeout=200)
        return json.loads(out.stdout.strip().splitlines()[-1])

    started = ctl("start", display=":99", xauth=None, max_s=60, fps=10, width=1280, crf=30)
    assert started["ok"], started
    assert ctl("start", display=":99", xauth=None, max_s=60, fps=10, width=1280, crf=30)["kind"] == "running"
    assert ctl("status")["running"] is True
    done = ctl("stop", gif_s=15, gif_max=8 << 20)
    assert done["ok"] and done["ended"] == "stopped" and done["files"] == ["rec.mp4", "rec.gif"], done
    assert 24 <= done["duration_s"] <= 26 and (done["width"], done["height"]) == (1280, 720)
    facts = media.mp4_facts(folder / "rec.mp4")
    assert facts and 24 <= facts["duration_s"] <= 26
    assert 0 < done["gif"]["bytes"] <= 8 << 20 and media.gif_size(folder / "rec.gif")[0] == 640
    assert not (folder / "rec.mkv").exists() and ctl("status")["running"] is False
    assert ctl("discard")["ok"] and not folder.exists()


def test_without_ffmpeg_the_recorder_says_so(tmp_path):
    req = json.dumps({"script": recording.recorder_script(), "action": "start", "dir": str(tmp_path / "r"),
                      "display": ":99", "max_s": 10, "fps": 10, "width": 1280, "crf": 30})
    out = subprocess.run([sys.executable, "-I", "-c", recording.BOOT], input=req, capture_output=True, text=True, check=False,
                         env={"PATH": str(tmp_path)}, timeout=30)
    assert json.loads(out.stdout.strip().splitlines()[-1])["kind"] == "no_ffmpeg"


# ------------------------------------------------------------------ the files

def test_the_node_reads_an_mp4_and_a_gif_itself(tmp_path):
    path = tmp_path / "a.mp4"
    path.write_bytes(mp4(42.25, 1280, 718))
    assert media.mp4_facts(path) == {"duration_s": 42.25, "width": 1280, "height": 718}
    path.write_bytes(b"\0\0\0\x18ftypisom" + b"\0" * 40)  # no moov
    assert media.mp4_facts(path) is None
    path.write_bytes(b"<html>not a video</html>")
    assert media.mp4_facts(path) is None
    path.write_bytes(gif(320, 200))
    assert media.gif_size(path) == (320, 200) and media.gif_size(tmp_path / "none.gif") is None


async def test_keep_video_refuses_what_is_not_a_video_and_drops_a_large_gif(node, tokens, monkeypatch, tmp_path):  # noqa: F811
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"<script>alert(1)</script>")
    with pytest.raises(InvalidRequest, match="not an MP4"):
        mgr.keep_video(env, bad, None, title="x")
    good, big = tmp_path / "rec.mp4", tmp_path / "rec.gif"
    good.write_bytes(mp4())
    big.write_bytes(gif())
    monkeypatch.setattr(proofs_mod, "GIF_BYTES_MAX", 10)
    kept = mgr.keep_video(env, good, big, title="no preview")
    assert kept["gif"] is None and "gif_url" not in kept
    good.write_bytes(mp4(pad=5000))
    monkeypatch.setattr(proofs_mod, "VIDEO_BYTES_MAX", 1000)
    with pytest.raises(InvalidRequest, match="shorter"):
        mgr.keep_video(env, good, None, title="too long")


async def test_videos_count_in_the_nodes_cap_and_go_whole(node, tokens, monkeypatch, tmp_path):  # noqa: F811
    mgr = node["mgr"]
    old = mgr.get((await opened(node))["id"])
    (tmp_path / "a.mp4").write_bytes(mp4())
    (tmp_path / "a.gif").write_bytes(gif())
    first = mgr.keep_video(old, tmp_path / "a.mp4", tmp_path / "a.gif", title="old one")
    await node["call"]("environment_close", discard=True)
    size = len(mp4()) + len(gif())
    monkeypatch.setattr(proofs_mod, "PROOF_NODE_BYTES_MAX", size + 100)
    env = mgr.get((await opened(node, title="the next one"))["id"])
    (tmp_path / "b.mp4").write_bytes(mp4())
    mgr.keep_video(env, tmp_path / "b.mp4", None, title="new one")  # the old video goes, MP4, GIF and record
    assert mgr.proofs(old) == [] and not list((mgr.home / "environments" / old.id / "proofs").glob(f"{first['id']}*"))
    (tmp_path / "c.mp4").write_bytes(mp4())
    with pytest.raises(InvalidRequest, match="MB of proofs"):
        mgr.keep_video(env, tmp_path / "c.mp4", None, title="one more")  # only an open one's are left
    await node["call"]("environment_close", discard=True)
    assert mgr.prune_proofs(utcnow() + timedelta(days=proofs_mod.PROOF_DAYS + 1)) >= 1 and mgr.proofs(env) == []


# ------------------------------------------------------------------ serving them

def test_the_app_lists_a_video_and_serves_it_with_ranges(panel, tmp_path):  # noqa: F811
    from test_api_members import owner

    api_node, mgr = panel
    boss = owner(api_node)
    env = _record(mgr, "evideoagent", member=None, root="gr_owner_agent")
    (tmp_path / "rec.mp4").write_bytes(mp4())
    (tmp_path / "rec.gif").write_bytes(gif())
    kept = mgr.keep_video(env, tmp_path / "rec.mp4", tmp_path / "rec.gif", title="the flow")
    base = f"/api/orgs/local/environments/{env.id}"
    [listed] = boss.get(base).json()["proofs"]
    assert listed["kind"] == "video" and listed["src"] == f"{base}/proofs/{kept['id']}.mp4"
    assert listed["gif_src"] == f"{base}/proofs/{kept['id']}.gif" and listed["duration_s"] == 12.5
    part = boss.client.get(listed["src"], headers={**boss.h, "Range": "bytes=4-7"})
    assert part.status_code == 206 and part.content == b"ftyp" and part.headers["content-type"] == "video/mp4"
    assert part.headers["content-range"] == f"bytes 4-7/{len(mp4())}"
    whole = boss.get(listed["gif_src"])
    assert whole.status_code == 200 and whole.content == gif() and whole.headers["content-type"] == "image/gif"
    assert boss.get(f"{base}/proofs/{kept['id']}.png").status_code == 404


async def test_the_viewer_serves_a_video_proof_for_its_link_only(tmp_path):
    import httpx
    from test_lanes import make
    from test_viewer_app import FakeTraces

    from agentos.node.viewer_app import build_viewer_app

    video = tmp_path / "p.mp4"
    video.write_bytes(mp4())

    class Envs:
        def is_browser_lane(self, lane_id):
            return False

        def signed_proof_path(self, workspace, name, proof, ext, token):
            return video if (workspace, name, proof, ext, token) == ("env", "eabc1234-browser", "pabcdefghjk",
                                                                      "mp4", "good") else None

    lanes, *_ = make(tmp_path)
    app = build_viewer_app(lanes, lanes.config, traces=FakeTraces(), environments=lambda: Envs())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as http:
        url = "/view/env/eabc1234-browser/proof/pabcdefghjk.mp4"
        part = await http.get(url + "?t=good", headers={"Range": "bytes=0-3"})
        assert part.status_code == 206 and len(part.content) == 4 and part.headers["accept-ranges"] == "bytes"
        assert part.headers["x-content-type-options"] == "nosniff" and "media-src 'self'" in \
            part.headers["content-security-policy"]
        assert (await http.get(url + "?t=bad")).status_code == 403
        assert (await http.get(url.replace(".mp4", ".gif") + "?t=good")).status_code == 403
    await lanes.shutdown()
