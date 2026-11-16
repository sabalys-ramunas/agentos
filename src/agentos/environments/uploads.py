"""Uploads: an agent's local checkout (uncommitted changes included) onto its environment's machine,
with no repository connection and without the bytes going through tool arguments. A mixin of
EnvironmentManager, and the environment_upload tool.

environment_upload gives a signed, one-use link on the node's own address, as a proof's link is
(agentos.environments.proofs): `/view/env/<id>/upload?t=...`, signed with the viewer key, so replacing
that key revokes it. The agent packs its checkout and sends it from its own shell:

    A="${TMPDIR:-/tmp}/agentos-work.tgz"; if git rev-parse --is-inside-work-tree ...; then
      git ls-files -co --exclude-standard -z | tar --null -T - -czf "$A"     # a git checkout
    else tar --exclude node_modules --exclude .env ... -czf "$A" .; fi       # a plain folder
    curl --fail-with-body -T "${TMPDIR:-/tmp}/agentos-work.tgz" "<url>"

(PACK is the one line; the archive goes to the temp folder, never into the checkout it packs.)

The viewer serves the link (PUT or POST, agentos.node.viewer_app) and hands the body to
`receive_upload`, which:

1. checks the link: signed for this environment, not expired (UPLOAD_LINK_S), issued by this node
   since it started and not used before (it is spent on first use), and the environment open, ready
   and not frozen. Anything else is a 403;
2. streams the body to a temp file on the node, at most UPLOAD_BYTES_MAX;
3. checks every member before anything reaches the machine (`check_archive`, tarfile's data filter
   and more): no absolute path, no `..`, no link that leaves the folder, no device or fifo, at most
   MEMBERS_MAX members and UNPACKED_BYTES_MAX unpacked;
4. streams the archive to the machine over the environment's SSH (EnvManager.put_archive) and
   extracts it into the folder: what it holds replaces the same paths, and nothing else is deleted;
5. records it in the audit log and in environment()'s last calls, and removes the temp file.

Links live in memory: a node restart ends every one.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import posixpath
import secrets
import shutil
import tarfile
import tempfile
import time
import zlib
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.core import clock
from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Effect
from agentos.core.tools import ToolContext, ToolSpec, refuse_frozen
from agentos.environments.models import WORKSPACE, Environment

log = logging.getLogger("agentos.environments")

UPLOAD_PURPOSE = "upload"
UPLOAD_LINK_S = 900              # a link lives 15 minutes
UPLOAD_BYTES_MAX = 512 << 20     # the body: 30 times a 17 MB monorepo, and at most two at once (below)
UNPACKED_BYTES_MAX = 4 << 30     # what it unpacks to on the machine (a small gzip can unpack to a lot)
MEMBERS_MAX = 200_000            # files, folders and links in one archive
LINKS_OPEN_MAX = 5               # unused links one environment holds at once
UPLOADS_AT_ONCE = 2              # bodies this node receives at once, so they never fill its disk
DISK_RESERVE = 1 << 30           # free space the node keeps for its own work while it receives one
EXTRACT_TIMEOUT_S = 900.0
PATHS_SHOWN = 10
WORK = "/work"
ARCHIVE = '"${TMPDIR:-/tmp}/agentos-work.tgz"'  # in the temp folder: never packed into itself, never left behind
# A git checkout: its tracked and new files, without what git ignores. A plain folder: all of it but the
# usual dependencies and secrets. Either way an empty result is refused by the node (check_archive).
PLAIN_EXCLUDES = ("node_modules", ".env", ".git", ".venv", "__pycache__")
PACK = (f"A={ARCHIVE}; if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then "
        'git ls-files -co --exclude-standard -z | COPYFILE_DISABLE=1 tar --null -T - -czf "$A"; '
        "else COPYFILE_DISABLE=1 tar " + " ".join(f"--exclude {x}" for x in PLAIN_EXCLUDES) + ' -czf "$A" .; fi')
PROJECT_STEPS = (("package.json", "pnpm install"), ("pyproject.toml", "uv sync"),
                 ("requirements.txt", "uv venv -q && uv pip install -r requirements.txt"), ("go.mod", "go build ./..."),
                 ("Cargo.toml", "cargo build"))


class Refused(Exception):
    """An upload the node does not take: the HTTP status, an error code and words for the agent."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class BadArchive(ValueError):
    """The archive is not one the node passes to the machine; the message says which member and why."""


@dataclass
class _Link:
    env_id: str
    dest: str
    grant_id: str | None
    expires: float


# ------------------------------------------------------------------ the archive
def check_dest(dest: Any, workdir: str) -> str:
    """The folder an upload goes into: the workdir when left out; a relative one starts at the workdir;
    it is /work or a folder under it."""
    if dest is None or dest == "":
        return workdir
    if not isinstance(dest, str) or len(dest) > 1024 or any(c in dest for c in "\x00\n\r|"):
        raise InvalidRequest('dest is a folder under /work, like "/work/shop" or "packages/web"')
    path = posixpath.normpath(dest if dest.startswith("/") else posixpath.join(workdir, dest))
    if path != WORK and not path.startswith(WORK + "/"):
        raise InvalidRequest(f"dest {dest!r} is {path}, outside /work; an upload goes into /work or a folder under it")
    return path


def _refuse(name: str, why: str) -> BadArchive:
    return BadArchive(f"{name!r} {why}, so nothing was put on the machine. Pack with: {PACK}")


def _check_member(m: tarfile.TarInfo, root: str) -> None:
    name = m.name
    if not name or "\x00" in name:
        raise _refuse(name, "has no name")
    if name.startswith("/"):
        raise _refuse(name, "is an absolute path")
    if ".." in name.split("/"):
        raise _refuse(name, "has a .. in its path")
    if not (m.isreg() or m.isdir() or m.issym() or m.islnk()):
        raise _refuse(name, "is a device, a fifo or another special file")
    try:
        # the data filter on an empty folder of the node's own: its checks see the archive's paths, never
        # anything on the node
        kept = tarfile.data_filter(m, root)
    except tarfile.FilterError as e:
        raise _refuse(name, f"is refused ({e})") from None
    if kept.name != name:
        raise _refuse(name, f"would be extracted as {kept.name!r}")


def _open_tar(path: Path, gz: bool) -> tarfile.TarFile:
    try:
        return tarfile.open(path, "r:gz" if gz else "r:")
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        raise BadArchive(f"the body is not a .tar.gz or a .tar. Pack with: {PACK}") from None


def _members(tf: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    count = 0
    while True:
        try:
            m = tf.next()
        except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
            raise BadArchive(f"the archive is damaged or cut short after {count} members ({e}); pack it again "
                             "and send it whole") from None
        if m is None:
            return
        count += 1
        if count > MEMBERS_MAX:
            raise BadArchive(f"the archive has more than {MEMBERS_MAX} members; leave out what git ignores (the "
                             "pack command does)")
        yield m


def check_archive(path: Path) -> dict:
    """Check every member of a .tar.gz or .tar before any of it reaches the machine. Returns gzip (the
    machine's tar needs -z), files (regular files), members, unpacked (their bytes) and paths (the
    first PATHS_SHOWN files). Raises BadArchive."""
    with path.open("rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    root = tempfile.mkdtemp(prefix="agentos-upload-")
    files, members, unpacked, paths, kinds = 0, 0, 0, [], set()
    try:
        with _open_tar(path, gz) as tf:
            for m in _members(tf):
                members += 1
                _check_member(m, root)
                if not m.isreg():
                    continue
                files += 1
                unpacked += m.size
                name = m.name.removeprefix("./")
                if name.count("/") == 0:
                    kinds.add(name)  # a top-level file names the project's kind (PROJECT_STEPS)
                if unpacked > UNPACKED_BYTES_MAX:
                    raise BadArchive(f"the archive unpacks to more than {UNPACKED_BYTES_MAX >> 30} GB; leave out "
                                     "build output and dependencies (the pack command does)")
                if len(paths) < PATHS_SHOWN:
                    paths.append(m.name.removeprefix("./"))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if files == 0:
        raise BadArchive("the archive holds no files" + (f" ({members} folders only)" if members else "") +
                         f", so nothing was put on the machine. Run the pack command in the folder with your code: {PACK}")
    return {"gzip": gz, "files": files, "members": members, "unpacked": unpacked, "paths": paths, "kinds": kinds}


# ------------------------------------------------------------------ the mixin
class Uploads:
    """For EnvironmentManager (its home, store, runtime, record_call and the proofs' link signing)."""

    def _upload_links(self) -> dict[str, _Link]:
        return self.__dict__.setdefault("_uploads", {})

    def _uploads_running(self) -> set[str]:
        return self.__dict__.setdefault("_uploading", set())

    def upload_link(self, env: Environment, dest: str | None, *, grant_id: str | None) -> dict:
        """A signed, one-use link that puts an archive into `dest` on the environment's machine, and the
        commands that pack and send it."""
        if not env.open or env.state != "ready":
            from agentos.environments.errors import EnvironmentNotReady

            raise EnvironmentNotReady(f"environment {env.id} is {env.state}; an upload needs it ready "
                                      "(environment() says when)")
        target = check_dest(dest, env.workdir)
        tokens, config = self._tokens(), self._get("config")
        if tokens is None or config is None:
            raise InvalidRequest("this node cannot sign links (no viewer key), so it takes no uploads")
        links = self._upload_links()
        now = getattr(tokens, "clock", time.time)()
        for key in [k for k, link in links.items() if link.expires <= now]:
            links.pop(key, None)
        if sum(1 for link in links.values() if link.env_id == env.id) >= LINKS_OPEN_MAX:
            raise InvalidRequest(f"environment {env.id} has {LINKS_OPEN_MAX} unused upload links; use one, or wait "
                                 "up to 15 minutes for them to expire")
        upload_id = "u" + secrets.token_hex(8)
        token, expires = tokens.sign_blob(UPLOAD_PURPOSE, f"{env.id}/{upload_id}", UPLOAD_LINK_S)
        links[upload_id] = _Link(env.id, target, grant_id, expires)
        url = f"http://localhost:{config.mcp_port}/view/{WORKSPACE}/{env.id}/upload?t={token}"
        return {"url": url, "expires_at": clock.iso_or_none(datetime.fromtimestamp(expires, UTC), z=True),
                "dest": target, "max_bytes": UPLOAD_BYTES_MAX, "pack": PACK,
                "send": f'curl --fail-with-body -T {ARCHIVE} "{url}"'}

    async def receive_upload(self, workspace: str, name: str, token: str | None, body: AsyncIterator[bytes],
                             declared: str | None = None) -> tuple[int, dict]:
        """The viewer's PUT or POST /view/env/<id>/upload?t=...: (HTTP status, JSON body)."""
        try:
            link, env = self._spend(workspace, name, token)
        except Refused as e:
            return e.status, {"error": {"code": e.code, "message": e.message}}
        running = self._uploads_running()
        running.add(link.env_id)
        started = time.monotonic()
        try:
            out = await self._receive(env, link, body, declared)
        except Refused as e:
            self._note_upload(env, link, ok=False, error=e.code, summary=e.message, started=started)
            return e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception as e:  # the agent gets a reason, the log the rest
            log.exception("upload into %s failed", env.id)
            self._note_upload(env, link, ok=False, error="error", summary=type(e).__name__, started=started)
            return 500, {"error": {"code": "error", "message": "the upload failed on the node; ask for a new link "
                                                               "with environment_upload and send it again"}}
        finally:
            running.discard(link.env_id)
        self._note_upload(env, link, ok=True, started=started, out=out)
        return 200, out

    def _spend(self, workspace: str, name: str, token: str | None) -> tuple[_Link, Environment]:
        """The link a token names, now spent, and its environment. Raises Refused."""
        denied = Refused(403, "denied", "This upload link is not valid, has expired or was used already. Ask for "
                                        "a new one with environment_upload.")
        tokens = self._tokens()
        value = tokens.open_blob(UPLOAD_PURPOSE, token) if tokens is not None and workspace == WORKSPACE else None
        env_id, _, upload_id = (value or "").partition("/")
        links = self._upload_links()
        link = links.get(upload_id)
        if value is None or env_id != name or link is None or link.env_id != env_id:
            raise denied
        try:
            env = self.store.get(env_id)
        except (NotFound, ValueError, KeyError):
            links.pop(upload_id, None)
            raise denied from None
        if not env.open:
            links.pop(upload_id, None)
            raise Refused(403, "denied", f"environment {env_id} is closed, so its upload links ended")
        if env.state != "ready":
            raise Refused(409, "environment_not_ready", f"environment {env_id} is {env.state}; send it again once "
                                                        "environment() says ready")
        freeze = self._get("freeze")
        if freeze is not None and callable(getattr(freeze, "frozen_for", None)) \
                and freeze.frozen_for(env.terminal_lane, WORKSPACE) is not None:
            raise Refused(403, "frozen", f"environment {env_id} is frozen, so it takes no files")
        if env_id in self._uploads_running() or len(self._uploads_running()) >= UPLOADS_AT_ONCE:
            raise Refused(409, "busy", "another upload is running; send it again when it is done (the link "
                                       "still works)")
        links.pop(upload_id, None)  # spent: from here on, whatever happens, it is not taken again
        return link, env

    async def _receive(self, env: Environment, link: _Link, body: AsyncIterator[bytes], declared: str | None) -> dict:
        if declared and declared.isdigit() and int(declared) > UPLOAD_BYTES_MAX:
            raise Refused(413, "too_large", f"the archive is {int(declared)} bytes; an upload is at most "
                                            f"{UPLOAD_BYTES_MAX >> 20} MB. Leave out what git ignores (the pack "
                                            "command does)")
        folder = self.home / "environments" / env.id / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        need = (int(declared) if declared and declared.isdigit() else 0) + DISK_RESERVE
        if shutil.disk_usage(folder).free < need:
            raise Refused(507, "unavailable", "the node has too little free disk to take this upload now; "
                                              "tell the human")
        path = folder / f"up-{secrets.token_hex(6)}.part"
        t0 = time.monotonic()
        try:
            size, sha = await _write_body(body, path)
            t1 = time.monotonic()
            try:
                packed = await asyncio.to_thread(check_archive, path)
            except BadArchive as e:
                raise Refused(400, "invalid", str(e)) from None
            t2 = time.monotonic()
            envs = self.envs()
            with path.open("rb") as f:
                res = await envs.put_archive(env.terminal_lane, link.dest, f, gzip=packed["gzip"],
                                             timeout_s=EXTRACT_TIMEOUT_S)
            # where an upload's time goes: the body from the agent, the node's check, the machine's extract
            log.info("environment %s: upload of %d bytes (%d files): body %.1f s, check %.1f s, onto the machine "
                     "%.1f s", env.id, size, packed["files"], t1 - t0, t2 - t1, time.monotonic() - t2)
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
        if res.exit_code != 0:
            said = (res.stderr or res.stdout or "").strip()[-600:] or f"exit {res.exit_code}"
            raise Refused(502, "error", f"the machine could not extract it into {link.dest}: {said}")
        out = {"dest": link.dest, "files": packed["files"], "bytes": size, "unpacked_bytes": packed["unpacked"],
               "paths": packed["paths"], "sha256": f"sha256:{sha}"}
        if packed["files"] > len(packed["paths"]):
            out["more"] = packed["files"] - len(packed["paths"])
        step = next((cmd for marker, cmd in PROJECT_STEPS if marker in packed["kinds"]), None)
        now = (f'Now lane_exec(cmd="{step}", cwd="{link.dest}") or what else the project needs' if step
               else "Now run what the project needs with lane_exec")
        out["next"] = (f"Files in the archive replaced the same paths in {link.dest}; nothing else was deleted. {now}. "
                       "Files in no git repository count as unsaved work at close: download what you make "
                       "(environment_download)")
        return out

    def _note_upload(self, env: Environment, link: _Link, *, ok: bool, started: float, out: dict | None = None,
                     error: str | None = None, summary: str = "") -> None:
        """The upload in environment()'s last calls and in the audit log."""
        if out is not None:
            summary = f"{out['files']} files, {out['bytes']} bytes into {out['dest']}"
        with contextlib.suppress(Exception):
            self.record_call(env.terminal_lane, "upload", ok=ok, summary=summary, error=error)
        traces = self._get("traces") or getattr(self._get("policy"), "traces", None)
        write = getattr(traces, "write", None)
        if not callable(write):
            return
        from agentos.policy.trace_models import TraceEvent

        args = {"dest": link.dest, **({k: out[k] for k in ("files", "bytes", "unpacked_bytes", "sha256")}
                                      if out is not None else {})}
        try:
            write(TraceEvent(kind="upload", tool="environment.upload", lane=env.terminal_lane,
                             workspace=WORKSPACE, principal=link.grant_id, principal_kind="link",
                             grant_id=link.grant_id, effect=Effect.EXEC.value, status="ok" if ok else "error",
                             summary=f"upload: {summary}"[:1000], error=error, args=args,
                             duration_ms=int((time.monotonic() - started) * 1000)))
        except Exception:
            log.warning("could not record an upload into %s", env.id, exc_info=True)


async def _write_body(body: AsyncIterator[bytes], path: Path) -> tuple[int, str]:
    """Stream the request body to `path` (made new, mode 600), at most UPLOAD_BYTES_MAX. (bytes, sha256)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    size, digest = 0, hashlib.sha256()
    with os.fdopen(fd, "wb") as out:
        async for chunk in body:
            size += len(chunk)
            if size > UPLOAD_BYTES_MAX:
                raise Refused(413, "too_large", f"the archive is over {UPLOAD_BYTES_MAX >> 20} MB, the most one upload "
                                                "takes. Leave out what git ignores (the pack command does)")
            digest.update(chunk)
            out.write(chunk)
    if size == 0:
        raise Refused(400, "invalid", f"send the archive as the body: {PACK}, then curl --fail-with-body -T "
                                      f'{ARCHIVE} "<url>"')
    return size, digest.hexdigest()


# ------------------------------------------------------------------ the tool
async def environment_upload(ctx: ToolContext, *, on: str, dest: str | None = None) -> dict:
    """A one-use link (15 minutes) that puts a .tar.gz of your local checkout (uncommitted changes
    too) on the environment's machine; the bytes never go through tool calls. Returns pack and send:
    run them in your own shell (not lane_exec), in the checkout. Files replace the same paths;
    nothing else is deleted. At most 512 MB. Effect: exec.

    Args:
        dest: A folder under /work (default: the workdir, /work/<repo> or /work); a relative one
            starts at the workdir.
    """
    from agentos.env.tools import _facts, _grant_id, _where
    from agentos.environments.manager import EnvironmentManager

    mgr: EnvironmentManager = ctx.runtime.require("environments")
    env = mgr.lane_environment(on)
    if env is None or on != env.terminal_lane or not EnvironmentManager.reaches(env, getattr(ctx, "grant", None),
                                                                                mcp=True):
        raise InvalidRequest("environment_upload puts files on your environment's machine; name it with `on` "
                             "(its title or env/<id>), or leave `on` out when you have one open")
    refuse_frozen(ctx, lanes=env.lanes.values())
    target = check_dest(dest, env.workdir)
    lane = ctx.runtime.require("envs").get(on)
    await ctx.authorize(Effect.EXEC, lane_id=on, summary=f"Make a link that uploads files into {target} {_where(lane)}",
                        content={"upload_into": target}, facts=_facts(lane))
    out = mgr.upload_link(env, target, grant_id=_grant_id(ctx))
    ctx.trace("environment_upload", lane=on, environment=env.id, dest=target)
    out["next"] = (f"In the folder with your code, in your own shell: {PACK} && curl --fail-with-body -T {ARCHIVE} "
                   "\"<url>\". In a git checkout it packs tracked and new files and leaves out what git ignores; in a "
                   "plain folder all of it but node_modules, .env, .git and .venv. An archive with no files is "
                   "refused. The link works once")
    return out


def tool_specs(runtime: Any) -> list[ToolSpec]:
    return [ToolSpec("environment_upload", environment_upload, Effect.EXEC, tags=("environments", "terminal"),
                     hidden_without="environments")]
