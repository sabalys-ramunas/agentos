"""Background jobs and synced working trees in env lanes (agentos.env.jobs, agentos.env.sync), on
the fake host: workloads run on this machine with DirectLauncher, the lane's home is its work dir."""

import asyncio
import base64
import hashlib
import json
import os
import re

import pytest

from agentos.core.errors import InvalidRequest, LaneBusy, NotFound, PolicyDenied
from agentos.core.models import Effect, Principal
from agentos.core.runtime import Runtime
from agentos.env import tool_specs
from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher
from agentos.env.sync import Manifest, empty_dirs_after


class Ctx:
    def __init__(self, runtime):
        self.runtime = runtime
        self.principal = Principal(id="test-agent")
        self.calls: list[dict] = []

    async def authorize(self, effect, *, lane_id=None, domain=None, summary=None, content=None, facts=None):
        self.calls.append({"effect": effect, "lane_id": lane_id, "summary": summary, "content": content})

    def trace(self, kind, **fields):
        pass


@pytest.fixture
async def env(tmp_path):
    mgr = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0, max_timeout_s=30, jobs_keep_per_lane=3))
    mgr.create("dev/run", preset="sandbox", overrides={"filesystem": "overlay"})
    runtime = Runtime(envs=mgr)
    yield runtime
    await mgr.shutdown()


def tools(runtime):
    return {s.name: s.handler for s in tool_specs(runtime)}


async def wait_done(t, ctx, job, timeout=10.0):
    out, err, so, se = b"", b"", 0, 0
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        r = await t["terminal_job_read"](ctx, job=job, stdout_offset=so, stderr_offset=se, wait_ms=500,
                                      base64_output=True)
        out += base64.b64decode(r["stdout"])
        err += base64.b64decode(r["stderr"])
        so, se = r["next_stdout_offset"], r["next_stderr_offset"]
        if r["state"] != "running" and not r["stdout"] and not r["stderr"]:
            return r, out, err
        assert loop.time() < end, "the job did not end"


CHUNK = 1 << 20


def tree_of(root, paths):
    """The manifest a client sends for these paths, and every chunk by name (as agentos run builds them)."""
    files, blobs = [], {}
    for rel in paths:
        p = root / rel
        st = p.lstat()
        if p.is_symlink():
            files.append({"path": rel, "link": os.readlink(p), "mtime": int(st.st_mtime)})
            continue
        data = p.read_bytes()
        parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)]
        shas = [hashlib.sha256(x).hexdigest() for x in parts]
        blobs.update(zip(shas, parts, strict=True))
        files.append({"path": rel, "mode": st.st_mode & 0o777, "mtime": int(st.st_mtime), "size": len(data),
                      "chunks": shas})
    raw = json.dumps({"v": 1, "files": files}).encode()
    msha = hashlib.sha256(raw).hexdigest()
    blobs[msha] = raw
    return [msha], blobs


async def upload_tree(t, ctx, root, paths):
    manifest, raw = tree_of(root, paths)
    blobs = {sha: base64.b64encode(data).decode() for sha, data in raw.items()}
    missing = (await t["terminal_upload_check"](ctx, on="dev/run", chunks=list(blobs)))["missing"]
    if missing:
        await t["terminal_upload"](ctx, on="dev/run", chunks={k: blobs[k] for k in missing})
    return manifest, missing


def store_keep(runtime):
    from agentos.env.sync import SyncState, load_manifest
    store = runtime.envs.chunk_store("dev/run")
    keep = set()
    for entry in SyncState(store.root).load().values():
        keep |= set(entry["manifest"]) | load_manifest(store, entry["manifest"]).chunk_set()
    return keep


def lane_home(runtime):
    return runtime.envs.paths.workdir("dev/run")


# ---------------------------------------------------------------- jobs


async def test_a_job_streams_its_output_and_keeps_the_exit_code(env):
    t, ctx = tools(env), Ctx(env)
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="echo one; echo two >&2; sleep 0.3; echo three; exit 7")
    assert job["state"] == "running" and job["id"].startswith("job-")
    call = ctx.calls[0]
    assert call["effect"] == Effect.EXEC and call["lane_id"] == "dev/run"
    assert "echo one" in call["summary"] and "in the background" in call["summary"]
    first = await t["terminal_job_read"](ctx, job=job["id"], wait_ms=2000)
    assert first["stdout"].startswith("one") and ctx.calls[-1]["effect"] == Effect.READ
    r, out, err = await wait_done(t, ctx, job["id"])
    assert out == b"one\nthree\n" and err == b"two\n"
    assert r["state"] == "exited" and r["exit_code"] == 7 and r["stdout_bytes"] == 10 and r["duration_s"] > 0.2


async def test_reads_resume_from_an_offset_and_text_never_splits_a_character(env):
    t, ctx = tools(env), Ctx(env)
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="printf 'héllo wörld'")
    _, out, _ = await wait_done(t, ctx, job["id"])
    assert out.decode() == "héllo wörld"
    part = await t["terminal_job_read"](ctx, job=job["id"], stdout_offset=3, max_bytes=4)
    assert part["stdout"] == "llo " and part["next_stdout_offset"] == 7  # é is two bytes: offset 3 is after it


async def test_waiting_for_the_end_of_a_long_job_gives_its_start_and_its_end(env):
    """until_exit is how an agent waits for a job to end, and what it needs then is the end: the error,
    the summary. A job that printed far more than max_bytes gives the start and the end with a marker
    that names the bytes it skipped, which a plain read at that offset still pages through."""
    t, ctx = tools(env), Ctx(env)
    job = await t["terminal_job_start"](ctx, on="dev/run",
                                    cmd="echo START; seq 1 100000; echo boom >&2; echo DONE; exit 2")
    r = await t["terminal_job_read"](ctx, job=job["id"], until_exit=True, wait_ms=30000)
    out = r["stdout"]
    assert r["state"] == "exited" and r["exit_code"] == 2 and "boom" in r["stderr"]
    assert out.startswith("START\n1\n") and out.endswith("100000\nDONE\n") and len(out) < 16_400
    assert r["next_stdout_offset"] == r["stdout_bytes"] > 500_000
    skipped = re.search(r"\[agentos: (\d+) bytes skipped here, of (\d+) in all; read them with stdout_offset="
                        r"(\d+)\]\n", out)
    assert skipped and int(skipped[2]) == r["stdout_bytes"], out[3900:4300]
    head, tail = out[:skipped.start()], out[skipped.end():]
    assert head.endswith("\n") and tail.startswith(tuple("0123456789"))  # whole lines on both sides
    assert len(head) + int(skipped[1]) + len(tail) == r["stdout_bytes"] and "skipped" in r["note"]
    middle = await t["terminal_job_read"](ctx, job=job["id"], stdout_offset=int(skipped[3]), max_bytes=1 << 20)
    assert (head + middle["stdout"][:int(skipped[1])] + tail).splitlines() == \
        ["START", *map(str, range(1, 100001)), "DONE"]

    small = await t["terminal_job_start"](ctx, on="dev/run", cmd="seq 1 50; exit 1")
    r = await t["terminal_job_read"](ctx, job=small["id"], until_exit=True, wait_ms=30000)
    assert r["stdout"] == "".join(f"{i}\n" for i in range(1, 51)) and "note" not in r or not r["note"]


async def test_cancel_stops_the_job_and_says_so(env):
    t, ctx = tools(env), Ctx(env)
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="echo started; sleep 30")
    await t["terminal_job_read"](ctx, job=job["id"], wait_ms=2000)
    done = await t["terminal_job_cancel"](ctx, job=job["id"])
    assert done["state"] == "cancelled" and done["exit_code"] is None or done["signal"]
    assert ctx.calls[-1]["effect"] == Effect.EXEC and "Cancel job" in ctx.calls[-1]["summary"]
    assert env.envs.state("dev/run").value in ("warm", "cold")


async def test_a_timeout_ends_the_job(env):
    t, ctx = tools(env), Ctx(env)
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="sleep 30", timeout_s=0.5)
    r, _, _ = await wait_done(t, ctx, job["id"])
    assert r["state"] == "timed_out" and "timeout" in r["note"]


async def test_env_and_cwd_reach_the_command(env):
    t, ctx = tools(env), Ctx(env)
    (lane_home(env) / "sub").mkdir(parents=True)
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd='echo "$GREETING from ${PWD##*/}"', cwd="sub",
                                    env={"GREETING": "hi"})
    _, out, _ = await wait_done(t, ctx, job["id"])
    assert out == b"hi from sub\n"
    for bad in ({"cwd": "../etc"}, {"cwd": "/etc"}, {"cwd": "-rf"}, {"env": {"BAD NAME": "x"}}):
        with pytest.raises(InvalidRequest):
            await t["terminal_job_start"](ctx, on="dev/run", cmd="true", **bad)


async def test_jobs_are_listed_limited_and_survive_a_restart(env, tmp_path):
    t, ctx = tools(env), Ctx(env)
    env.envs.settings.max_jobs_per_lane = 1
    running = await t["terminal_job_start"](ctx, on="dev/run", cmd="sleep 30")
    with pytest.raises(LaneBusy):
        await t["terminal_job_start"](ctx, on="dev/run", cmd="true")
    listed = await t["terminal_jobs"](ctx)
    assert [j["id"] for j in listed] == [running["id"]] and listed[0]["state"] == "running"
    assert await t["terminal_jobs"](ctx, on="dev/run") == listed
    # the node stops while it runs: a new manager on the same home finds it lost, and its output
    mgr2 = EnvManager(env.envs.paths.home, host=FakeHost(), launcher=DirectLauncher(), use_lock=False,
                      settings=EnvSettings(proxy_port=0))
    await mgr2.start()
    try:
        info, _, _ = await mgr2.job_read(running["id"])
        assert info.state == "lost" and "node stopped" in info.note
    finally:
        await mgr2.shutdown()
    with pytest.raises(NotFound):
        await t["terminal_job_read"](ctx, job="job-0000000000")


async def test_old_finished_jobs_are_pruned(env):
    t, ctx = tools(env), Ctx(env)
    ids = []
    for i in range(5):
        job = await t["terminal_job_start"](ctx, on="dev/run", cmd=f"echo {i}")
        await wait_done(t, ctx, job["id"])
        ids.append(job["id"])
    kept = {j["id"] for j in await t["terminal_jobs"](ctx)}
    assert ids[-1] in kept and ids[0] not in kept and len(kept) <= 4
    jobs_dir = env.envs.paths.jobs("dev/run")
    assert not (jobs_dir / f"{ids[0]}.out").exists()


async def test_a_cold_lane_ends_its_jobs_with_a_note(env):
    t, ctx = tools(env), Ctx(env)
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="sleep 30")
    await env.envs.cool("dev/run")
    info = (await t["terminal_jobs"](ctx))[0]
    assert info["id"] == job["id"] and info["state"] == "exited" and "lane was stopped" in info["note"]


# ---------------------------------------------------------------- sync


def write(root, rel, text, mode=0o644):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    p.chmod(mode)


async def test_sync_sends_only_what_changed_and_keeps_what_the_command_made(env, tmp_path):
    t, ctx = tools(env), Ctx(env)
    src = tmp_path / "repo"
    write(src, "a.txt", "alpha\n")
    write(src, "pkg/b.py", "print('b')\n")
    write(src, "run.sh", "#!/bin/sh\necho ran\n", 0o755)
    (src / "link").symlink_to("a.txt")
    paths = ["a.txt", "link", "pkg/b.py", "run.sh"]
    manifest, missing = await upload_tree(t, ctx, src, paths)
    assert len(missing) == 4  # three files and the manifest
    upload_call = next(c for c in ctx.calls if c["effect"] == Effect.DRAFT)
    assert "for a sync into lane dev/run" in upload_call["summary"]
    sync = {"dest": "src/repo", "manifest": manifest}
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="./run.sh; cat link; mkdir -p .venv; echo x > .venv/keep",
                                    cwd="src/repo", sync=sync)
    assert job["sync"]["files"] == 4 and job["sync"]["changed"] == 4 and job["sync"]["deleted"] == 0
    summary = ctx.calls[-1]["summary"]
    assert summary.startswith("Sync 4 files (4 changed, 0 deleted) into src/repo and run in the background")
    assert ctx.calls[-1]["content"]["sync"] == {"dest": "src/repo", "manifest": manifest, "clean": False}
    _, out, _ = await wait_done(t, ctx, job["id"])
    assert out == b"ran\nalpha\n"
    dest = lane_home(env) / "src/repo"
    assert (dest / "run.sh").stat().st_mode & 0o111 and (dest / "link").is_symlink()

    # second run: one file changed, one deleted (with its now empty directory), one added
    write(src, "a.txt", "alpha 2\n")
    (src / "pkg/b.py").unlink()
    write(src, "c.txt", "gamma\n")
    paths = ["a.txt", "c.txt", "link", "run.sh"]
    manifest, missing = await upload_tree(t, ctx, src, paths)
    assert len(missing) == 3  # a.txt, c.txt and the manifest; run.sh is on the node already
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="cat a.txt c.txt; ls .venv", cwd="src/repo",
                                    sync={"dest": "src/repo", "manifest": manifest})
    assert job["sync"]["changed"] == 2 and job["sync"]["deleted"] == 1
    _, out, _ = await wait_done(t, ctx, job["id"])
    assert out == b"alpha 2\ngamma\nkeep\n"
    assert not (dest / "pkg").exists() and (dest / ".venv/keep").exists()

    # a file the command changed in the lane is put back to this tree's version on the next sync
    (dest / "a.txt").write_text("changed in the lane\n")
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="cat a.txt", cwd="src/repo",
                                    sync={"dest": "src/repo", "manifest": manifest})
    _, out, _ = await wait_done(t, ctx, job["id"])
    assert out == b"alpha 2\n"

    # clean wipes what the commands made
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="ls -a", cwd="src/repo",
                                    sync={"dest": "src/repo", "manifest": manifest, "clean": True})
    _, out, _ = await wait_done(t, ctx, job["id"])
    assert ".venv" not in out.decode() and "a.txt" in out.decode()
    assert "the destination wiped first" in [c for c in ctx.calls if c["effect"] == Effect.EXEC][-1]["summary"]


async def test_large_files_go_in_chunks_and_unused_chunks_are_dropped(env, tmp_path):
    t, ctx = tools(env), Ctx(env)
    src = tmp_path / "repo"
    big = os.urandom(2_300_000)  # three chunks of 1 MB
    (src).mkdir()
    (src / "big.bin").write_bytes(big)
    manifest, missing = await upload_tree(t, ctx, src, ["big.bin"])
    assert len(missing) == 4
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="true", sync={"dest": "src/repo", "manifest": manifest})
    await wait_done(t, ctx, job["id"])
    landed = (lane_home(env) / "src/repo/big.bin").read_bytes()
    assert hashlib.sha256(landed).digest() == hashlib.sha256(big).digest()
    (src / "big.bin").write_bytes(b"small now")
    manifest, _ = await upload_tree(t, ctx, src, ["big.bin"])
    job = await t["terminal_job_start"](ctx, on="dev/run", cmd="true", sync={"dest": "src/repo", "manifest": manifest})
    await wait_done(t, ctx, job["id"])
    store = env.envs.chunk_store("dev/run")
    store.gc(set(manifest) | store_keep(env), grace_s=0)  # what the apply's own gc keeps, without the grace
    left = {p.name for p in store.dir.rglob("*") if p.is_file()}
    assert left == set(manifest) | {hashlib.sha256(b"small now").hexdigest()}


async def test_a_sync_is_checked_before_anyone_is_asked(env, tmp_path):
    t, ctx = tools(env), Ctx(env)
    with pytest.raises(NotFound):  # the manifest was never uploaded
        await t["terminal_job_start"](ctx, on="dev/run", cmd="true", sync={"dest": "src/r", "manifest": ["a" * 64]})
    bad = json.dumps({"v": 1, "files": [{"path": "../escape", "chunks": []}]}).encode()
    sha = hashlib.sha256(bad).hexdigest()
    await t["terminal_upload"](ctx, on="dev/run", chunks={sha: base64.b64encode(bad).decode()})
    with pytest.raises(InvalidRequest):
        await t["terminal_job_start"](ctx, on="dev/run", cmd="true", sync={"dest": "src/r", "manifest": [sha]})
    for dest in ("/root", "../x", "src/../x", "-x"):
        with pytest.raises(InvalidRequest):
            await t["terminal_job_start"](ctx, on="dev/run", cmd="true", sync={"dest": dest, "manifest": [sha]})
    assert not any(c["effect"] == Effect.EXEC for c in ctx.calls)


async def test_uploads_are_checked_and_capped(env):
    t, ctx = tools(env), Ctx(env)
    with pytest.raises(InvalidRequest):  # the data does not match its name
        await t["terminal_upload"](ctx, on="dev/run", chunks={"b" * 64: base64.b64encode(b"x").decode()})
    with pytest.raises(InvalidRequest):
        await t["terminal_upload"](ctx, on="dev/run", chunks={"not-a-sha": "eA=="})
    env.envs.settings.upload_max_mb = 0
    env.envs._stores.clear()
    data = b"hello"
    with pytest.raises(InvalidRequest, match="store is full"):
        await t["terminal_upload"](ctx, on="dev/run", chunks={hashlib.sha256(data).hexdigest():
                                                          base64.b64encode(data).decode()})


async def test_a_second_sync_waits_for_the_job_in_the_same_directory(env, tmp_path):
    t, ctx = tools(env), Ctx(env)
    src = tmp_path / "repo"
    write(src, "a.txt", "a\n")
    manifest, _ = await upload_tree(t, ctx, src, ["a.txt"])
    sync = {"dest": "src/repo", "manifest": manifest}
    first = await t["terminal_job_start"](ctx, on="dev/run", cmd="sleep 30", sync=sync)
    with pytest.raises(LaneBusy, match=first["id"]):
        await t["terminal_job_start"](ctx, on="dev/run", cmd="true", sync=sync)
    await t["terminal_job_cancel"](ctx, job=first["id"])


def test_manifest_rules():
    with pytest.raises(InvalidRequest):
        Manifest.model_validate({"files": [{"path": "a"}, {"path": "a"}]}).check()
    with pytest.raises(InvalidRequest):
        Manifest.model_validate({"files": [{"path": "l", "link": "/etc"}, {"path": "l/x"}]}).check()
    m = Manifest.model_validate({"files": [{"path": "x", "mode": 0o4777}]})
    assert m.files[0].mode == 0o755
    assert empty_dirs_after(["a/b/c.txt", "a/d.txt", "e/f.txt"], {"a/keep.txt"}) == ["a/b", "e"]


async def test_agents_lanes_stay_private_and_under_the_caps(env):
    t, ctx = tools(env), Ctx(env)
    made = await t["terminal_create"](ctx, on="dev/big", filesystem="overlay", memory_mb=4096, cpus=2, disk_mb=8192)
    assert made["filesystem"] == "overlay"
    assert "filesystem overlay, memory_mb 4096, cpus 2, disk_mb 8192" in ctx.calls[-1]["summary"]
    with pytest.raises(PolicyDenied, match="agent_max_memory_mb"):
        await t["terminal_create"](ctx, on="dev/huge", memory_mb=10**6)
    with pytest.raises(PolicyDenied, match="never shares the node filesystem"):
        await t["terminal_create"](ctx, on="dev/shared", filesystem="shared")
