"""Files into a lane: chunks uploaded once by content, a manifest prepared, then applied in one
step. A mixin of EnvManager."""

from __future__ import annotations

import asyncio
import logging
import secrets

from agentos.core.errors import (
    InvalidRequest,
)
from agentos.env import execs
from agentos.env import sync as syncs
from agentos.env.lanes import LaneRuntime, SandboxError

log = logging.getLogger("agentos.env")
ARGV_BATCH = 500  # paths per rm or rmdir workload, well under the kernel's argument limit


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
