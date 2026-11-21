# ruff: noqa: F811  (pytest fixtures imported from test_env_node_support)
"""`openajar run`'s node side in a real sandbox: a synced tree lands in an overlay lane through the
lane's own tar, a background job streams from it, deletes and the kept .venv behave, cancel ends it."""

import asyncio
import hashlib
import os

import pytest
from test_env_node_support import mgr, needs_node  # noqa: F401

from openajar.client.run import build_tree, manifest_chunks, read_chunk

pytestmark = [pytest.mark.node, needs_node]


async def upload(mgr, lane, root, paths):
    tree = build_tree(root, paths)
    mchunks = manifest_chunks(tree.manifest)
    blobs = dict(mchunks)
    for sha, where in tree.chunks.items():
        blobs[sha] = read_chunk(where)
    missing = await mgr.upload_check(lane, list(blobs))
    await mgr.upload(lane, {s: blobs[s] for s in missing})
    return [s for s, _ in mchunks], missing


async def run_to_end(mgr, lane, cmd, *, sync=None, cwd=None, timeout=60):
    prepared = await mgr.sync_prepare(lane, **sync) if sync else None
    info = await mgr.job_start(lane, cmd, cwd=cwd, sync=prepared)
    out = err = b""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        info, o, e = await mgr.job_read(info.id, stdout_offset=len(out), stderr_offset=len(err), wait_s=1)
        out, err = out + o, err + e
        if info.state != "running" and not o and not e:
            return info, out, err
        assert loop.time() < end, "the job did not end"


def write(root, rel, data, mode=0o644):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    p.chmod(mode)


async def test_a_synced_tree_runs_in_an_overlay_sandbox(mgr, tmp_path):
    lane = "run/proj"
    mgr.create(lane, overrides={"filesystem": "overlay", "disk_mb": 256})
    src = tmp_path / "proj"
    big = os.urandom(3 << 20)
    write(src, "big.bin", big)
    write(src, "pkg/mod.py", "print('mod')\n")
    write(src, "run.sh", "#!/bin/sh\nset -e\necho start\npython3 pkg/mod.py\nsha256sum big.bin | cut -c1-64\n"
                         "readlink alias\nmkdir -p .venv && date > .venv/made\n", 0o755)
    (src / "alias").symlink_to("pkg/mod.py")
    paths = ["alias", "big.bin", "pkg/mod.py", "run.sh"]
    manifest, missing = await upload(mgr, lane, src, paths)
    assert len(missing) == 6  # three chunks of big.bin, two small files, the manifest
    info, out, err = await run_to_end(mgr, lane, "./run.sh", cwd="src/proj",
                                      sync={"dest": "src/proj", "manifest": manifest})
    assert info.state == "exited" and info.exit_code == 0, err
    lines = out.decode().splitlines()
    assert lines[:2] == ["start", "mod"] and lines[2] == hashlib.sha256(big).hexdigest()
    assert lines[3] == "pkg/mod.py" and info.sync["files"] == 4

    # second run: a delete (and its directory), a change; the .venv the first run made stays
    (src / "pkg/mod.py").unlink()
    (src / "alias").unlink()
    write(src, "run.sh", "#!/bin/sh\nls -A; test -f .venv/made && echo venv kept\n", 0o755)
    manifest, missing = await upload(mgr, lane, src, ["big.bin", "run.sh"])
    assert len(missing) == 2
    info, out, _ = await run_to_end(mgr, lane, "./run.sh", cwd="src/proj",
                                    sync={"dest": "src/proj", "manifest": manifest})
    assert info.exit_code == 0 and info.sync["deleted"] == 2
    assert out.decode().split() == [".venv", "big.bin", "run.sh", "venv", "kept"]

    # the lane goes cold and comes back: the overlay kept the tree and the .venv
    await mgr.cool(lane)
    info, out, _ = await run_to_end(mgr, lane, "ls -A", cwd="src/proj")
    assert out.decode().split() == [".venv", "big.bin", "run.sh"]


async def test_cancel_and_network_off(mgr):
    lane = "run/net"
    mgr.create(lane, overrides={"filesystem": "overlay", "disk_mb": 64})
    info = await mgr.job_start(lane, "echo up; sleep 60")
    _, out, _ = await mgr.job_read(info.id, wait_s=5)
    assert out == b"up\n"
    done = await mgr.job_cancel(info.id)
    assert done.state == "cancelled" and done.duration_s < 15
    info, out, _ = await run_to_end(mgr, lane, "python3 -c \"import urllib.request as u; "
                                               "u.urlopen('https://pypi.org', timeout=5)\" && echo reached")
    assert info.exit_code != 0 and b"reached" not in out
