"""Downloads: a file or a folder under an environment's /work, off its machine, for the agent or the
person, without the bytes going through tool calls. The reverse of agentos.environments.uploads. A
mixin of EnvironmentManager, and the environment_download tool.

environment_download(path) packs the path on the machine and gives a signed, one-use link on the node's
own address, as an upload link is: `/view/env/<id>/download?t=...`, signed with the viewer key under its
own purpose (DOWNLOAD_PURPOSE), so an upload link never opens a download nor the other way round. The
agent fetches and unpacks it from its own shell:

    curl --fail-with-body -o out.tgz "<url>"
    tar -xzf out.tgz

`download_link`:

1. resolves the path on the machine (readlink -f) and refuses it unless what it names is a file or a
   folder under /work: a symbolic link that leaves /work is refused, and the packing starts at the
   resolved path, so no link in it is followed;
2. packs it with EnvManager.get_archive (`tar -czf -` streamed over the environment's SSH) into a temp
   file under the environment's folder on the node, at most DOWNLOAD_BYTES_MAX; past that it stops and
   the partial file is deleted. Links inside a folder are packed as links, never followed;
3. signs a link for it, valid DOWNLOAD_LINK_S.

The viewer serves it (GET, agentos.node.viewer_app) through `take_download`: a link that is tampered,
expired, used before, issued before the node started, or for a closed environment is a 403. It is
spent on first use. The response is always the .tar.gz as an attachment (application/gzip, nosniff,
no-store), never the file itself, so nothing from the machine is ever served as a page on the node's
own address. The fetch is recorded in the audit log and in environment()'s last calls.

The temp file is deleted once served, when the link expires (the reaper's pass, and every new link),
and when the environment closes. Links live in memory: a node restart ends every one, and the next pass
deletes their files.

DOWNLOAD_BYTES_MAX is 512 MB, the size of the largest upload: a project that goes up comes back down. The
archive streams to disk and never sits in memory; the node packs at most DOWNLOADS_AT_ONCE at once, one
environment holds at most LINKS_OPEN_MAX unfetched files, and packing needs the cap plus DISK_RESERVE
free on the node's disk. Something larger (a dataset, build caches) belongs in git or object storage.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import posixpath
import re
import secrets
import shlex
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.core import clock
from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Effect
from agentos.core.tools import ToolContext, ToolSpec, refuse_frozen
from agentos.environments.models import WORKSPACE, Environment
from agentos.environments.uploads import WORK, Refused

log = logging.getLogger("agentos.environments")

DOWNLOAD_PURPOSE = "download"    # never "upload": the two links can never be swapped
DOWNLOAD_LINK_S = 900            # a link lives 15 minutes
DOWNLOAD_BYTES_MAX = 512 << 20   # the packed archive (see the module's words)
LINKS_OPEN_MAX = 3               # unfetched downloads one environment holds at once
DOWNLOADS_AT_ONCE = 2            # archives this node packs at once
DISK_RESERVE = 1 << 30           # free space the node keeps for its own work while it packs one
PACK_TIMEOUT_S = 600.0
RESOLVE_TIMEOUT_S = 30.0
ARCHIVE = "out.tgz"
UNPACK = f"tar -xzf {ARCHIVE}"
# what the path names on the machine: "dir <real path>", "file <real path>", "other <real path>" or "missing"
RESOLVE = ('[ -e "$1" ] || { echo missing; exit 0; }; p=$(readlink -f -- "$1") || { echo missing; exit 0; }; '
           'if [ -d "$p" ]; then echo "dir $p"; elif [ -f "$p" ]; then echo "file $p"; else echo "other $p"; fi')


@dataclass
class _Download:
    env_id: str
    path: str                 # what was asked, under /work
    real: str                 # what it resolved to on the machine
    kind: str                 # "file" or "folder"
    file: Path                # the archive on the node
    name: str                 # the attachment's file name
    bytes: int
    files: int
    sha256: str
    grant_id: str | None
    by: str                   # "agent" or "owner"
    expires: float


def check_path(path: Any, workdir: str) -> str:
    """The file or folder a download packs: a relative path starts at the workdir; it is /work or
    under it."""
    if not isinstance(path, str) or not path or len(path) > 1024 or any(ord(c) < 32 for c in path):
        raise InvalidRequest('path is a file or a folder under /work, like "/work/shop/dist" or "report.pdf"')
    full = posixpath.normpath(path if path.startswith("/") else posixpath.join(workdir, path))
    if full != WORK and not full.startswith(WORK + "/"):
        raise InvalidRequest(f"path {path!r} is {full}, outside /work; a download takes a file or a folder under /work")
    return full


def _inside_work(real: str) -> bool:
    return real == WORK or real.startswith(WORK + "/")


def attachment_name(real: str) -> str:
    """The download's file name: the path's last part, kept to safe characters, then .tgz."""
    base = posixpath.basename(real.rstrip("/")) or "work"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "work"
    return f"{safe[:80]}.tgz"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ the mixin
class Downloads:
    """For EnvironmentManager (its home, store, runtime, record_call and the proofs' link signing)."""

    def _download_links(self) -> dict[str, _Download]:
        return self.__dict__.setdefault("_downloads", {})

    def _downloads_running(self) -> set[str]:
        return self.__dict__.setdefault("_downloading", set())

    def _downloads_folder(self, env_id: str) -> Path:
        return self.home / "environments" / env_id / "downloads"

    def _now(self) -> float:
        tokens = self._tokens()
        return getattr(tokens, "clock", time.time)() if tokens is not None else time.time()

    def sweep_downloads(self) -> int:
        """Delete expired links and every archive no live link names (one a restart or a close left).
        Returns how many files went."""
        links, now = self._download_links(), self._now()
        for key in [k for k, d in links.items() if d.expires <= now]:
            links.pop(key, None)
        kept = {d.file for d in links.values()}
        gone = 0
        root = self.home / "environments"
        if not root.is_dir():
            return 0
        for folder in root.glob("*/downloads"):
            if folder.parent.name in self._downloads_running():
                continue  # one is being packed there now
            for f in folder.iterdir():
                if f not in kept:
                    with contextlib.suppress(OSError):
                        f.unlink()
                        gone += 1
        return gone

    def drop_downloads(self, env_id: str) -> None:
        """The environment closed: its links end and their archives go."""
        links = self._download_links()
        for key in [k for k, d in links.items() if d.env_id == env_id]:
            links.pop(key, None)
        shutil.rmtree(self._downloads_folder(env_id), ignore_errors=True)

    async def download_link(self, env: Environment, path: str, *, grant_id: str | None, by: str = "agent") -> dict:
        """Pack `path` (a file or a folder under /work) on the environment's machine into a temp file on
        the node, and give a signed, one-use link to it with the commands that fetch and unpack it."""
        from agentos.environments.errors import EnvironmentNotReady

        if not env.open or env.state != "ready":
            raise EnvironmentNotReady(f"environment {env.id} is {env.state}; a download needs it ready "
                                      "(environment() says when)")
        target = check_path(path, env.workdir)
        tokens, config = self._tokens(), self._get("config")
        if tokens is None or config is None:
            raise InvalidRequest("this node cannot sign links (no viewer key), so it gives no downloads")
        with contextlib.suppress(Exception):
            self.sweep_downloads()
        links = self._download_links()
        if sum(1 for d in links.values() if d.env_id == env.id) >= LINKS_OPEN_MAX:
            raise InvalidRequest(f"environment {env.id} has {LINKS_OPEN_MAX} downloads nobody fetched yet; fetch "
                                 "one, or wait up to 15 minutes for them to expire")
        running = self._downloads_running()
        if env.id in running or len(running) >= DOWNLOADS_AT_ONCE:
            from agentos.core.errors import LaneBusy

            raise LaneBusy("another download is being packed; ask again when it is done")
        running.add(env.id)
        try:
            return await self._pack(env, target, grant_id=grant_id, by=by)
        finally:
            running.discard(env.id)

    async def _pack(self, env: Environment, target: str, *, grant_id: str | None, by: str) -> dict:
        envs = self.envs()
        res = await envs.exec(env.terminal_lane, f"sh -c {shlex.quote(RESOLVE)} sh {shlex.quote(target)}",
                              timeout_s=RESOLVE_TIMEOUT_S)
        said = (res.stdout or "").strip().splitlines()
        kind, _, real = (said[-1] if said else "").partition(" ")
        if res.exit_code != 0 or kind not in ("dir", "file", "other", "missing"):
            raise InvalidRequest(f"the machine did not say what {target} is; check it with lane_exec(cmd=\"ls -la "
                                 f"{shlex.quote(target)}\")")
        if kind == "missing":
            raise InvalidRequest(f"{target} does not exist on the machine; list the folder with "
                                 f"lane_exec(cmd=\"ls -la {shlex.quote(posixpath.dirname(target))}\")")
        if not _inside_work(real):
            raise InvalidRequest(f"{target} is a link to {real}, outside /work; a download takes only what is "
                                 "under /work")
        if kind == "other":
            raise InvalidRequest(f"{target} is not a file or a folder (a device, a socket or a fifo)")
        folder = self._downloads_folder(env.id)
        folder.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(folder).free < DOWNLOAD_BYTES_MAX + DISK_RESERVE:
            raise InvalidRequest("the node has too little free disk to pack a download now; tell the human")
        dl_id = "d" + secrets.token_hex(8)
        file = folder / f"{dl_id}.tgz"
        src, paths = (WORK, None) if real == WORK else (posixpath.dirname(real), [posixpath.basename(real)])
        packed = await envs.get_archive(env.terminal_lane, src, file, paths=paths, gzip=True,
                                        timeout_s=PACK_TIMEOUT_S, max_bytes=DOWNLOAD_BYTES_MAX)
        try:
            sha = await asyncio.to_thread(_sha256, file)
            tokens, config = self._tokens(), self._get("config")
            token, expires = tokens.sign_blob(DOWNLOAD_PURPOSE, f"{env.id}/{dl_id}", DOWNLOAD_LINK_S)
        except BaseException:
            with contextlib.suppress(OSError):
                file.unlink()
            raise
        dl = _Download(env.id, target, real, "folder" if kind == "dir" else "file", file, attachment_name(real),
                       packed["bytes"], packed["files"], f"sha256:{sha}", grant_id, by, expires)
        if not self.store.get(env.id).open:  # it closed while this packed
            with contextlib.suppress(OSError):
                file.unlink()
            raise InvalidRequest(f"environment {env.id} closed while this was packed")
        self._download_links()[dl_id] = dl
        url = f"http://localhost:{config.mcp_port}/view/{WORKSPACE}/{env.id}/download?t={token}"
        fetch = f'curl --fail-with-body -o {ARCHIVE} "{url}"'
        what = (f"the folder {posixpath.basename(real) or 'work'} with its {dl.files} files" if dl.kind == "folder"
                else f"the file {posixpath.basename(real)}")
        return {"url": url, "expires_at": clock.iso_or_none(datetime.fromtimestamp(expires, UTC), z=True),
                "path": target, "kind": dl.kind, "files": dl.files, "bytes": dl.bytes, "sha256": dl.sha256,
                "max_bytes": DOWNLOAD_BYTES_MAX, "fetch": fetch, "unpack": UNPACK,
                "next": (f"In your own shell (not lane_exec), in the folder it goes into: {fetch} && {UNPACK}. "
                         f"It unpacks {what} there. The link works once, for 15 minutes")}

    def take_download(self, workspace: str, name: str, token: str | None) -> _Download:
        """The download a token names, now spent (the viewer's GET /view/env/<id>/download?t=...). The
        caller serves its file, then calls `served`. Raises Refused."""
        denied = Refused(403, "denied", "This download link is not valid, has expired or was used already. Ask for "
                                        "a new one with environment_download.")
        tokens = self._tokens()
        value = tokens.open_blob(DOWNLOAD_PURPOSE, token) if tokens is not None and workspace == WORKSPACE else None
        env_id, _, dl_id = (value or "").partition("/")
        links = self._download_links()
        dl = links.get(dl_id)
        if value is None or env_id != name or dl is None or dl.env_id != env_id:
            raise denied
        try:
            env = self.store.get(env_id)
        except (NotFound, ValueError, KeyError):
            links.pop(dl_id, None)
            raise denied from None
        if not env.open:
            self.drop_downloads(env_id)
            raise Refused(403, "denied", f"environment {env_id} is closed, so its download links ended")
        freeze = self._get("freeze")
        if dl.by != "owner" and freeze is not None and callable(getattr(freeze, "frozen_for", None)) \
                and freeze.frozen_for(env.terminal_lane, WORKSPACE) is not None:
            raise Refused(403, "frozen", f"environment {env_id} is frozen, so it gives no files")
        links.pop(dl_id, None)  # spent: from here on, whatever happens, it is not served again
        if not dl.file.is_file():
            raise denied
        return dl

    def served(self, dl: _Download, *, ok: bool, started: float) -> None:
        """After the response: the archive goes, and the fetch is recorded."""
        with contextlib.suppress(OSError):
            dl.file.unlink()
        self._note_download(dl, ok=ok, started=started)

    def _note_download(self, dl: _Download, *, ok: bool, started: float) -> None:
        """The fetch in environment()'s last calls and in the audit log."""
        summary = f"{dl.kind} {dl.path}: {dl.files} files, {dl.bytes} bytes" + ("" if ok else ", not fetched whole")
        try:
            lane = self.store.get(dl.env_id).terminal_lane
        except Exception:  # noqa: BLE001
            return
        with contextlib.suppress(Exception):
            self.record_call(lane, "download", ok=ok, summary=summary, error=None if ok else "error")
        traces = self._get("traces") or getattr(self._get("policy"), "traces", None)
        write = getattr(traces, "write", None)
        if not callable(write):
            return
        from agentos.policy.trace_models import TraceEvent

        principal = dl.grant_id if dl.by != "owner" else "owner"
        try:
            write(TraceEvent(kind="download", tool="environment.download", lane=lane, workspace=WORKSPACE,
                             principal=principal, principal_kind="link", grant_id=dl.grant_id,
                             effect=Effect.EXEC.value, status="ok" if ok else "error",
                             summary=f"download: {summary}"[:1000], error=None if ok else "error",
                             args={"path": dl.path, "kind": dl.kind, "files": dl.files, "bytes": dl.bytes,
                                   "sha256": dl.sha256, "by": dl.by},
                             duration_ms=int((time.monotonic() - started) * 1000)))
        except Exception:
            log.warning("could not record a download from %s", dl.env_id, exc_info=True)


# ------------------------------------------------------------------ the tool
async def environment_download(ctx: ToolContext, *, on: str, path: str) -> dict:
    """A file or a folder under /work, to your own computer: a one-use .tar.gz link (15 minutes); the
    bytes never go through tool calls. Returns fetch and unpack: run them in your own shell (not
    lane_exec). At most 512 MB packed. Effect: exec.

    Args:
        path: A file or a folder under /work, like "/work/shop/dist" or "report.pdf" (a relative one
            starts at the workdir).
    """
    from agentos.env.tools import _facts, _grant_id, _where
    from agentos.environments.manager import EnvironmentManager

    mgr: EnvironmentManager = ctx.runtime.require("environments")
    env = mgr.lane_environment(on)
    if env is None or on != env.terminal_lane or not EnvironmentManager.reaches(env, getattr(ctx, "grant", None),
                                                                                mcp=True):
        raise InvalidRequest("environment_download takes files off your environment's machine; name it with `on` "
                             "(its title or env/<id>), or leave `on` out when you have one open")
    refuse_frozen(ctx, lanes=env.lanes.values())
    target = check_path(path, env.workdir)
    lane = ctx.runtime.require("envs").get(on)
    await ctx.authorize(Effect.EXEC, lane_id=on, summary=f"Pack {target} {_where(lane)} for a download link",
                        content={"download": target}, facts=_facts(lane))
    out = await mgr.download_link(env, target, grant_id=_grant_id(ctx))
    ctx.trace("environment_download", lane=on, environment=env.id, path=target, bytes=out["bytes"],
              files=out["files"])
    return out


def tool_specs(runtime: Any) -> list[ToolSpec]:
    return [ToolSpec("environment_download", environment_download, Effect.EXEC, tags=("environments", "terminal"),
                     hidden_without="environments")]
