"""Files into a lane: chunks uploaded once by content, a manifest prepared, then applied in one
step. A mixin of EnvManager."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import posixpath
import secrets
import tarfile
import zlib
from pathlib import Path
from typing import BinaryIO

from agentos.core.errors import (
    InvalidRequest,
)
from agentos.env import execs
from agentos.env import sync as syncs
from agentos.env.lanes import LaneRuntime, SandboxError
from agentos.env.models import ExecResult

log = logging.getLogger("agentos.env")
ARGV_BATCH = 500  # paths per rm or rmdir workload, well under the kernel's argument limit
ARCHIVE_PATHS_MAX = 1000  # paths one get_archive packs (they go on the workload's command line)


class ArchiveTooLarge(InvalidRequest):
    """get_archive stopped: the archive grew past its max_bytes. The partial file is deleted."""


def archive_paths(paths: list[str] | None) -> list[str]:
    """What get_archive passes to tar: every path relative to its folder, as "./<path>" (so a name
    that starts with "-" is never an option). Refuses absolute paths, "..", and control characters."""
    if paths is None:
        return ["."]
    if not isinstance(paths, list) or not paths or len(paths) > ARCHIVE_PATHS_MAX:
        raise InvalidRequest(f"paths is a list of 1 to {ARCHIVE_PATHS_MAX} paths relative to the folder")
    out = []
    for p in paths:
        if not isinstance(p, str) or not p or len(p) > 4096 or any(ord(c) < 32 for c in p):
            raise InvalidRequest(f"{p!r} is not a path; give paths relative to the folder, like \"src/app.ts\"")
        if p.startswith("/"):
            raise InvalidRequest(f"{p!r} is an absolute path; give paths relative to the folder")
        if ".." in p.split("/"):
            raise InvalidRequest(f"{p!r} has a .. in it; give paths inside the folder")
        out.append("./" + posixpath.normpath(p).removeprefix("./"))
    return out


def _count_files(path: Path, gzip: bool) -> int:
    """Regular files in a tar archive (gzip or not)."""
    n = 0
    try:
        with tarfile.open(path, "r:gz" if gzip else "r:") as tf:
            for m in tf:
                n += m.isreg()
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        raise SandboxError(f"the archive from the lane does not read back ({e})") from None
    return n


def _batches(items: list[str], n: int = ARGV_BATCH) -> list[list[str]]:
    return [items[i:i + n] for i in range(0, len(items), n)]


class Sync:
    """EnvManager's file sync (it reads self.paths, self.settings and self.store)."""

    # =============================================================== sync
    def _sync_lock(self, lane_id: str) -> asyncio.Lock:
        return self._sync_locks.setdefault(lane_id, asyncio.Lock())

    def chunk_store(self, lane_id: str) -> syncs.ChunkStore:
        self.store.get_lane(lane_id)
        st = self._stores.get(lane_id)
        if st is None:
            st = self._stores[lane_id] = syncs.ChunkStore(
                self.paths.sync(lane_id), max_bytes=self.settings.upload_max_mb * 1048576,
                chunk_max_bytes=self.settings.upload_chunk_max_bytes)
        return st

    async def upload_check(self, lane_id: str, chunks: list[str]) -> list[str]:
        """Which of these chunks the node does not have yet for this lane."""
        if not isinstance(chunks, list) or len(chunks) > 50_000:
            raise InvalidRequest("chunks is a list of at most 50000 sha256 names")
        store = self.chunk_store(lane_id)
        return await asyncio.to_thread(store.missing, [syncs.check_sha(c) for c in chunks])

    async def upload(self, lane_id: str, chunks: dict[str, bytes]) -> dict:
        store = self.chunk_store(lane_id)
        async with self._sync_lock(lane_id):
            added = 0
            for sha, data in chunks.items():
                added += await asyncio.to_thread(store.put, sha, data)
            return {"stored": len(chunks), "bytes_added": added, "store_bytes": store.size()}

    async def sync_prepare(self, lane_id: str, *, dest: str, manifest: list[str], clean: bool = False) -> dict:
        """Check a sync before it is approved: the manifest and every chunk it names are on the node.
        Returns what sync_apply takes, with the counts an approval summary shows."""
        syncs.check_dir(dest, "sync.dest")
        lane = self.store.get_lane(lane_id)
        if lane.policy.signed_only or not lane.policy.exec:
            raise InvalidRequest(f"{lane.preset.value} lane {lane_id} runs allowlisted programs only, so it takes no "
                                 "synced files; use a sandbox lane")
        store = self.chunk_store(lane_id)
        m = await asyncio.to_thread(syncs.load_manifest, store, manifest)
        state = syncs.SyncState(store.root)
        previous = {} if clean else (await asyncio.to_thread(state.get, dest)).get("files") or {}
        _, changed, deleted = syncs.plan(m, previous)
        return {"dest": dest, "manifest": list(manifest), "clean": bool(clean), "files": len(m.files),
                "bytes": m.total_bytes(), "changed": len(changed), "deleted": len(deleted)}

    async def sync_apply(self, lane_id: str, prepared: dict) -> dict:
        """Make `dest` in the lane hold exactly the manifest's files (what the command made stays,
        unless `clean`). Runs as workloads inside the lane: rm and rmdir for what went away, then
        `tar -x` of an archive the node builds from the chunks. The caller holds the sync lock."""
        dest, shas, clean = prepared["dest"], prepared["manifest"], prepared["clean"]
        store = self.chunk_store(lane_id)
        m = await asyncio.to_thread(syncs.load_manifest, store, shas)
        state = syncs.SyncState(store.root)
        previous = {} if clean else (await asyncio.to_thread(state.get, dest)).get("files") or {}
        sigs, changed, deleted = syncs.plan(m, previous)
        rt = await self.warm(lane_id)
        loop = asyncio.get_running_loop()
        started = loop.time()
        if clean:
            await self._sync_step(rt, ["/bin/sh", "-c", 'rm -rf -- "$1"', "sh", dest], "clean the destination")
            await asyncio.to_thread(state.drop, dest)
        elif deleted:
            for batch in _batches(deleted):
                await self._sync_step(rt, ["/bin/sh", "-c", 'cd "$1" || exit 0; shift; rm -f -- "$@"', "sh", dest,
                                           *batch], "delete files")
            dirs = syncs.empty_dirs_after(deleted, set(sigs))
            for batch in _batches(dirs):
                script = 'cd "$1" || exit 0; shift; rmdir -- "$@" 2>/dev/null; exit 0'
                await self._sync_step(rt, ["/bin/sh", "-c", script, "sh", dest, *batch], "remove empty directories")
        tmp_dir = store.root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        archive = tmp_dir / f"sync-{secrets.token_hex(4)}.tar"
        f = await asyncio.to_thread(open, archive, "w+b")
        try:
            await asyncio.to_thread(syncs.write_tar, store, m, f)
            f.seek(0)
            await self._sync_step(rt, ["/bin/sh", "-c", 'mkdir -p -- "$1" && cd "$1" && exec tar -xf -', "sh", dest],
                                  "extract the files", stdin_file=f)
        finally:
            f.close()
            archive.unlink(missing_ok=True)
        await asyncio.to_thread(state.set, dest, list(shas), sigs)
        await asyncio.to_thread(self._sync_gc, lane_id)
        return {"dest": dest, "files": len(m.files), "bytes": m.total_bytes(), "changed": len(changed),
                "deleted": len(deleted), "clean": clean, "duration_s": round(loop.time() - started, 3)}

    async def put_archive(self, lane_id: str, dest: str, archive: BinaryIO, *, gzip: bool,
                          timeout_s: float) -> ExecResult:
        """Extract a tar archive the caller checked (agentos.environments.uploads) into `dest` in the lane:
        what it holds replaces the same paths, and nothing else is deleted. Streams `archive` as the
        workload's stdin, so a VM lane gets it over its SSH without the node holding it in memory."""
        script = 'mkdir -p -- "$1" && cd -- "$1" && exec tar -x --no-same-owner -f -' + (" -z" if gzip else "")
        rt = await self.warm(lane_id)
        async with rt.exec_sem:
            res = await execs.run(rt, ["/bin/sh", "-c", script, "sh", dest], timeout_s=timeout_s, max_bytes=8192,
                                  stdin_file=archive, kind="sync")
        await self.store.run(self.store.touch, lane_id)
        return res

    async def get_archive(self, lane_id: str, src: str, dest_file: Path, *, paths: list[str] | None = None,
                          gzip: bool = True, timeout_s: float = 600, max_bytes: int | None = None) -> dict:
        """Pack `src` in the lane (or `paths` under it) with `tar -c[z]f -` and stream the archive into
        `dest_file` on the node (made new, mode 600; a VM lane sends it over its SSH, as put_archive
        takes one), so the node never holds it in memory. Symbolic links are packed as links, never
        followed. Past `max_bytes` the workload is ended, the partial file deleted, and ArchiveTooLarge
        raised; a failed tar deletes it too and raises SandboxError. Returns {"bytes", "files"}
        (files: the regular files in it)."""
        if not isinstance(src, str) or not src or any(ord(c) < 32 for c in src):
            raise InvalidRequest("src is the folder to pack, like /work/shop")
        names = archive_paths(paths)
        dest_file = Path(dest_file)
        script = 'cd -- "$1" && shift && exec tar -c' + ("z" if gzip else "") + 'f - "$@"'
        rt = await self.warm(lane_id)
        fd = os.open(dest_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        done = False
        try:
            with os.fdopen(fd, "wb") as f:
                async with rt.exec_sem:
                    res = await execs.run(rt, ["/bin/sh", "-c", script, "sh", src, *names], timeout_s=timeout_s,
                                          max_bytes=8192, env={"COPYFILE_DISABLE": "1"}, kind="sync",
                                          stdout_file=f, stdout_limit=max_bytes)
            if max_bytes is not None and res.stdout_bytes > max_bytes:
                raise ArchiveTooLarge(f"the archive of {src} is over {max_bytes} bytes, the most this takes, so it "
                                      "stopped and kept nothing. Pack less: a folder further down, or leave out "
                                      "build output and dependencies")
            if res.timed_out:
                raise SandboxError(f"packing {src} took over {int(timeout_s)} seconds, so it stopped and kept nothing")
            changed = res.exit_code == 1 and "changed as we read it" in (res.stderr or "")  # GNU tar: still whole
            if res.exit_code != 0 and not changed:
                said = (res.stderr or "").strip()[-600:] or f"exit {res.exit_code} {res.signal or ''}".strip()
                raise SandboxError(f"tar could not pack {src} in {lane_id}: {said}")
            files = await asyncio.to_thread(_count_files, dest_file, gzip)
            done = True
        finally:
            if not done:
                with contextlib.suppress(OSError):
                    dest_file.unlink()
        await self.store.run(self.store.touch, lane_id)
        return {"bytes": res.stdout_bytes, "files": files}

    async def _sync_step(self, rt: LaneRuntime, argv: list[str], what: str, *, stdin_file=None) -> None:
        async with rt.exec_sem:
            res = await execs.run(rt, argv, timeout_s=self.settings.max_timeout_s, max_bytes=8192,
                                  stdin_file=stdin_file, kind="sync")
        if res.exit_code != 0:
            why = (res.stderr or res.stdout).strip()[-1500:] or f"exit {res.exit_code} {res.signal or ''}".strip()
            raise SandboxError(f"the sync could not {what} in {rt.lane.id}: {why}. A clean sync (sync.clean, "
                               "`agentos run --fresh`) starts the destination over")

    def _sync_gc(self, lane_id: str) -> None:
        """Keep the chunks the applied manifests use (and the manifests' own chunks), drop the rest."""
        store = self.chunk_store(lane_id)
        keep: set[str] = set()
        for entry in syncs.SyncState(store.root).load().values():
            shas = entry.get("manifest") or []
            try:
                keep |= set(shas) | syncs.load_manifest(store, shas).chunk_set()
            except Exception as e:  # noqa: BLE001 - a manifest that no longer loads keeps nothing
                log.warning("sync state of %s names a manifest that does not load: %s", lane_id, e)
        store.gc(keep)
