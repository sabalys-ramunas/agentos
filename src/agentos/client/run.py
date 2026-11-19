"""`agentos run`: run one command on a node against this working tree, and stream it back.

The motivating case is a 25-minute test suite that should not hold the laptop. The command goes
through the connection's tunnel and the agents' grant, the same MCP tools an agent would call, so
it gets exactly the effects and approvals the terminal tools already have:

1. The lane: `<workspace>/run-<repo>` (default workspace `dev`), a sandbox lane with a kept (overlay)
   filesystem, created with terminal_create on first use. Its network is off unless `--allow-host` names
   hosts (a package index, say), which go through the lane's egress forwarder and nowhere else.
2. The files: `git ls-files --cached --others --exclude-standard` (tracked files, plus untracked ones
   .gitignore does not exclude), cut into 1 MB chunks named by their sha256. terminal_upload_check says
   which chunks the node lacks, terminal_upload sends only those, so the second run sends what changed.
   The manifest (paths, modes, mtimes, chunk lists) is uploaded the same way.
3. The run: terminal_job_start with `sync` makes `src/<repo>` in the lane hold exactly those files
   (deleting what was deleted here, keeping what the commands made there: .venv, node_modules) and
   starts the command in the background, in the same directory relative to the repo as here. Both
   are one approval when the grant's routing asks for one.
4. The output: terminal_job_read long-polls from byte offsets and writes stdout and stderr here as they
   come. Ctrl-C cancels the job (terminal_job_cancel). The exit code is the command's.

Hashes of local files are cached by size, mtime and inode (`~/.cache/agentos/run/`), so an unchanged
tree is not read again.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from agentos.client.errors import ClientError

CHUNK_BYTES = 1 << 20            # a file is cut into chunks of this size
UPLOAD_BATCH_BYTES = 2 << 20     # raw bytes per terminal_upload call (base64 keeps it under the 4 MB request cap)
CHECK_BATCH = 5000               # chunk names per terminal_upload_check call
READ_MAX_BYTES = 1 << 20
READ_WAIT_MS = 15000
APPROVAL_WAIT_S = 120
DEFAULT_WORKSPACE = "dev"
DEFAULT_MEMORY_MB = 4096
DEFAULT_CPUS = 2.0
DEFAULT_DISK_MB = 8192
LANE_PREFIX = "run-"
DEST_ROOT = "src"
WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
# what a command's output reads like through a pipe: Python flushes each line, so it streams live
JOB_ENV = {"PYTHONUNBUFFERED": "1"}


class Session(Protocol):
    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any: ...


# ---------------------------------------------------------------- the working tree


_AGENT_SENTENCE = re.compile(r"(?:^|(?<=[.;]\s))(?:Tell the (?:user|human)|Call |Wait with |Do not retry)[^.]*\.?")


def person_words(why: Any) -> str:
    """An approval's `why` as a person reads it: the sentences written for the agent that waits ("Tell the
    user what waits and wait with approval_wait ...") dropped (QA 2026-11-10, tester B 10)."""
    if not isinstance(why, str):
        return ""
    return re.sub(r"\s{2,}", " ", _AGENT_SENTENCE.sub("", why)).strip()


def repo_root(cwd: Path) -> Path:
    """The git working tree that holds `cwd`."""
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True,
                             check=False, timeout=30)
    except FileNotFoundError:
        raise ClientError("agentos run syncs a git working tree, and git is not installed here") from None
    if out.returncode != 0:
        raise ClientError("agentos run syncs a git working tree, and this is not inside one",
                          "run it from a git checkout (its .gitignore decides what is sent)")
    return Path(out.stdout.strip())


def git_source(root: Path) -> dict[str, Any] | None:
    """The branch and commit of the working tree, and whether files changed since that commit: what the
    job records about the tree it was sent (terminal_job_start's `source`). None when git cannot say."""
    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    if not commit:
        return None
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD")  # None on a detached head
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"branch": branch or None, "commit": commit, "dirty": None if status is None else bool(status)}


def list_files(root: Path) -> list[str]:
    """Tracked files and untracked files .gitignore does not exclude, relative to `root`."""
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root,
                         capture_output=True, check=False, timeout=120)
    if out.returncode != 0:
        raise ClientError(f"git ls-files failed: {out.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return sorted({p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p})


def slug(name: str, limit: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:limit].strip("-")
    return s or "repo"


def default_lane(root: Path, workspace: str = DEFAULT_WORKSPACE) -> str:
    return f"{workspace}/{LANE_PREFIX}{slug(root.name, 41 - len(LANE_PREFIX))}"


def dest_for(root: Path) -> str:
    """Where the tree goes in the lane, under its home: src/<repo>."""
    return f"{DEST_ROOT}/{re.sub(r'[^A-Za-z0-9._-]+', '-', root.name).lstrip('.-') or 'repo'}"


def relative_cwd(root: Path, cwd: Path) -> str | None:
    """`cwd` inside the tree, as a path to run the command in (None at the top)."""
    try:
        rel = cwd.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return None if str(rel) in ("", ".") else rel.as_posix()


@dataclass
class Tree:
    """The manifest the node applies, and where each chunk's bytes are here."""
    manifest: dict[str, Any]
    chunks: dict[str, tuple[Path, int, int]]   # sha -> (file, offset, length)
    files: int = 0
    bytes: int = 0
    skipped: list[str] = field(default_factory=list)


class HashCache:
    """Chunk hashes of local files by (size, mtime_ns, inode), so an unchanged file is not read again."""

    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict[str, list] = {}
        if path is not None:
            try:
                loaded = json.loads(path.read_text())
                self.data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                self.data = {}
        self.used: dict[str, list] = {}

    def get(self, rel: str, st: os.stat_result) -> list[str] | None:
        hit = self.data.get(rel)
        if hit and hit[:3] == [st.st_size, st.st_mtime_ns, st.st_ino]:
            self.used[rel] = hit
            return list(hit[3])
        return None

    def put(self, rel: str, st: os.stat_result, chunks: list[str]) -> None:
        self.used[rel] = [st.st_size, st.st_mtime_ns, st.st_ino, chunks]

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.used, separators=(",", ":")))
            os.replace(tmp, self.path)
        except OSError:
            pass


def cache_path(root: Path) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "agentos" / "run" / (hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:20] + ".json")


def build_tree(root: Path, paths: list[str], cache: HashCache | None = None) -> Tree:
    """Hash every listed path into the manifest. A path that is gone (deleted, not yet staged), a
    directory (a submodule) or something other than a file or symlink is skipped and named."""
    cache = cache or HashCache(None)
    entries: list[dict[str, Any]] = []
    chunks: dict[str, tuple[Path, int, int]] = {}
    tree = Tree({"v": 1, "files": entries}, chunks)
    for rel in paths:
        if "\n" in rel or rel.startswith("/") or any(p in ("", ".", "..") for p in rel.split("/")):
            tree.skipped.append(rel)
            continue
        full = root / rel
        try:
            st = full.lstat()
        except OSError:
            continue  # deleted here and not staged yet: it is not part of this tree
        if stat.S_ISLNK(st.st_mode):
            entries.append({"path": rel, "link": os.readlink(full), "mtime": int(st.st_mtime)})
            tree.files += 1
            continue
        if not stat.S_ISREG(st.st_mode):
            tree.skipped.append(rel)
            continue
        shas = cache.get(rel, st)
        if shas is None:
            shas = []
            with open(full, "rb") as f:
                while data := f.read(CHUNK_BYTES):
                    shas.append(hashlib.sha256(data).hexdigest())
            cache.put(rel, st, shas)
        for i, sha in enumerate(shas):
            chunks.setdefault(sha, (full, i * CHUNK_BYTES, min(CHUNK_BYTES, st.st_size - i * CHUNK_BYTES)))
        entries.append({"path": rel, "mode": 0o755 if st.st_mode & 0o111 else 0o644, "mtime": int(st.st_mtime),
                        "size": st.st_size, "chunks": shas})
        tree.files += 1
        tree.bytes += st.st_size
    return tree


def manifest_chunks(manifest: dict[str, Any]) -> list[tuple[str, bytes]]:
    raw = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogateescape")
    parts = [raw[i:i + CHUNK_BYTES] for i in range(0, len(raw), CHUNK_BYTES)] or [b""]
    return [(hashlib.sha256(p).hexdigest(), p) for p in parts]


def read_chunk(where: tuple[Path, int, int]) -> bytes:
    path, offset, length = where
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(length)
    return data


# ---------------------------------------------------------------- talking to the node


@dataclass
class SyncStats:
    files: int = 0
    bytes: int = 0
    sent_chunks: int = 0
    sent_bytes: int = 0
    skipped: list[str] = field(default_factory=list)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_duration(seconds: float) -> str:
    s = round(seconds)
    if s < 60:
        return f"{seconds:.1f}s" if seconds < 10 else f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


SIGNALS = {"SIGHUP": 1, "SIGINT": 2, "SIGQUIT": 3, "SIGKILL": 9, "SIGSEGV": 11, "SIGPIPE": 13, "SIGTERM": 15}


def exit_code_of(job: dict[str, Any]) -> int:
    """This command's exit code for how the job ended: the command's own, 128+N for a signal,
    124 for a timeout (as timeout(1)), 130 for a cancel (as Ctrl-C), 1 when it was lost."""
    state = job.get("state")
    if state == "cancelled":
        return 130
    if state == "timed_out":
        return 124
    if state == "lost":
        return 1
    code = job.get("exit_code")
    if isinstance(code, int):
        return code if 0 <= code <= 255 else 1
    sig = job.get("signal")
    return 128 + SIGNALS.get(sig, 0) if sig in SIGNALS else 1


def ended_words(job: dict[str, Any]) -> str:
    state = job.get("state")
    if state == "exited":
        if job.get("exit_code") is None and job.get("signal"):
            words = f"killed by {job['signal']}"
        else:
            words = f"exit {job.get('exit_code')}"
            if job.get("signal"):
                words += f" ({job['signal']})"
    elif state == "cancelled":
        words = "cancelled"
    elif state == "timed_out":
        words = "timed out"
    elif state == "lost":
        words = "lost (the node stopped while it ran)"
    else:
        words = str(state)
    if job.get("oom_killed"):
        words += ", out of memory"
    return words


class Interrupted(Exception):
    """Ctrl-C while waiting on an approval: nothing was started."""


def tool_error(exc: Exception) -> ClientError:
    """A node error (`code: message`, then `fix: ...`) as one line and a hint."""
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    first = lines[0] if lines else "the node refused"
    fix = next((ln.removeprefix("fix:").strip() for ln in lines[1:] if ln.startswith("fix:")), None)
    return ClientError(first, fix)


class Run:
    """One `agentos run` against one node session. `say` writes the status lines (stderr)."""

    def __init__(self, session: Session, *, say: Callable[[str], None], needs_you: str | None = None,
                 stdout: BinaryIO | None = None, stderr: BinaryIO | None = None):
        self.session = session
        self.say = say
        self.needs_you = needs_you
        self.stdout = stdout if stdout is not None else sys.stdout.buffer
        self.stderr = stderr if stderr is not None else sys.stderr.buffer
        self.manifest: list[str] = []  # the uploaded manifest's chunk names (upload sets it)
        self.pos: dict[str, tuple[int, int]] = {}  # job -> (stdout, stderr) offsets written here so far

    # ------------------------------------------------------------ calls and approvals
    def raw(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        from agentos.client.mcp_http import ToolError
        try:
            return self.session.call(tool, args or {})
        except ToolError as exc:
            raise tool_error(exc) from None

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        """Call a tool; when it waits for a person, say so and wait, then call it again."""
        while True:
            out = self.raw(tool, args)
            if not (isinstance(out, dict) and out.get("status") == "approval_pending"):
                return out
            self.wait_for_approval(out)

    def wait_for_approval(self, pending: dict[str, Any]) -> None:
        aid = pending.get("approval_id")
        self.say(f"waiting for approval: {pending.get('summary') or 'this step'}")
        why = person_words(pending.get("why"))
        if why:
            self.say(f"  why: {why}")
        where = f"Needs you ({self.needs_you})" if self.needs_you else "Needs you in the panel"
        self.say(f"  approve it in {where} or on your phone; Ctrl-C stops waiting")
        try:
            while True:
                st = self.raw("approval_wait", {"approval_id": aid, "timeout_s": APPROVAL_WAIT_S})
                state = st.get("state") if isinstance(st, dict) else None
                if state == "approved":
                    self.say("  approved")
                    return
                if state in ("denied", "expired", "cancelled"):
                    note = f" ({st['note']})" if isinstance(st, dict) and st.get("note") else ""
                    raise ClientError(f"the approval was {state}{note}, so nothing ran")
        except KeyboardInterrupt:
            raise Interrupted from None

    # ------------------------------------------------------------ the lane
    def ensure_lane(self, lane: str, *, allow_hosts: list[str], image: str | None, memory_mb: int | None,
                    cpus: float | None, disk_mb: int | None, recreate: bool = False) -> tuple[dict, bool]:
        """The lane's brief, and whether it was created now."""
        lanes = self.raw("terminals_list")
        found = next((x for x in lanes if isinstance(x, dict) and x.get("id") == lane), None) \
            if isinstance(lanes, list) else None
        if found is not None and recreate:
            self.say(f"removing lane {lane} to make it again (--recreate)")
            self.call("terminal_remove", {"on": lane})
            found = None
        if found is not None:
            if found.get("kind") != "terminal":
                raise ClientError(f"{lane} is a {found.get('kind')} lane, not a terminal lane",
                                  "choose another with --lane")
            want = sorted(set(allow_hosts))
            if want and sorted(found.get("egress_allowlist") or []) != want:
                have = ", ".join(found.get("egress_allowlist") or []) or "none"
                self.say(f"note: lane {lane} lets the command reach {have}; --allow-host applies when the lane is "
                         "made. Run again with --recreate to make it with the hosts you named")
            return found, False
        args: dict[str, Any] = {"on": lane, "preset": "sandbox", "filesystem": "overlay"}
        if allow_hosts:
            args.update(network="isolated", allow_hosts=sorted(set(allow_hosts)))
        if image:
            args["base_image"] = image
        for key, value in (("memory_mb", memory_mb), ("cpus", cpus), ("disk_mb", disk_mb)):
            if value is not None:
                args[key] = value
        self.say(f"creating lane {lane} (sandbox, files kept between runs, "
                 + (f"network only to {', '.join(args['allow_hosts'])}" if allow_hosts else "network off") + ")")
        return self.call("terminal_create", args), True

    # ------------------------------------------------------------ sync
    def upload(self, lane: str, tree: Tree) -> SyncStats:
        stats = SyncStats(files=tree.files, bytes=tree.bytes, skipped=list(tree.skipped))
        mchunks = manifest_chunks(tree.manifest)
        extra = dict(mchunks)
        names = list(tree.chunks) + [sha for sha, _ in mchunks]
        missing: list[str] = []
        for i in range(0, len(names), CHECK_BATCH):
            got = self.raw("terminal_upload_check", {"on": lane, "chunks": names[i:i + CHECK_BATCH]})
            missing += got.get("missing") or []
        batch: dict[str, str] = {}
        size = 0
        for sha in dict.fromkeys(missing):
            data = extra[sha] if sha in extra else read_chunk(tree.chunks[sha])
            if hashlib.sha256(data).hexdigest() != sha:
                raise ClientError(f"a file changed while it was being sent ({tree.chunks[sha][0]})", "run again")
            if batch and size + len(data) > UPLOAD_BATCH_BYTES:
                self.call("terminal_upload", {"on": lane, "chunks": batch})
                batch, size = {}, 0
            batch[sha] = base64.b64encode(data).decode()
            size += len(data)
            stats.sent_chunks += 1
            stats.sent_bytes += len(data)
        if batch:
            self.call("terminal_upload", {"on": lane, "chunks": batch})
        self.manifest = [sha for sha, _ in mchunks]
        return stats

    # ------------------------------------------------------------ the job
    def start(self, lane: str, cmd: str, *, cwd: str | None, dest: str | None, clean: bool,
              timeout_s: float | None, source: dict[str, Any] | None = None) -> dict:
        args: dict[str, Any] = {"on": lane, "cmd": cmd, "env": dict(JOB_ENV)}
        if cwd:
            args["cwd"] = cwd
        if timeout_s:
            args["timeout_s"] = timeout_s
        if dest is not None:
            args["sync"] = {"dest": dest, "manifest": self.manifest, "clean": clean}
            if source:
                args["source"] = source
        try:
            return self.call("terminal_job_start", args)
        except ClientError as exc:
            if "source" not in args or "source" not in str(exc):
                raise
            # a node from before `source`: the run goes without it, and its page says the branch is not known
            args.pop("source")
            return self.call("terminal_job_start", args)

    def read(self, job: str, out: int, err: int, wait_ms: int) -> dict:
        return self.raw("terminal_job_read", {"job": job, "stdout_offset": out, "stderr_offset": err,
                                          "wait_ms": wait_ms, "max_bytes": READ_MAX_BYTES, "base64_output": True})

    def follow(self, job: str, *, wait_ms: int = READ_WAIT_MS, until_done: bool = True) -> dict:
        """Write the job's output here as it comes, until it ended and everything was read (or, with
        until_done false, until what is there now was read). Returns the last read. Picks up where
        the last follow of the same job stopped, so nothing is written twice."""
        out, err = self.pos.get(job, (0, 0))
        while True:
            r = self.read(job, out, err, wait_ms if until_done else 0)
            so, se = base64.b64decode(r.get("stdout") or ""), base64.b64decode(r.get("stderr") or "")
            if so:
                self.stdout.write(so)
                self.stdout.flush()
            if se:
                self.stderr.write(se)
                self.stderr.flush()
            out, err = r.get("next_stdout_offset", out + len(so)), r.get("next_stderr_offset", err + len(se))
            self.pos[job] = (out, err)
            if not so and not se and (r.get("state") != "running" or not until_done):
                return r

    def cancel(self, job: str) -> dict:
        return self.call("terminal_job_cancel", {"job": job})


def command_line(argv: list[str]) -> str:
    """The command as the lane's shell runs it: one word is taken as written (`"make check"` or a
    pipeline in quotes), several words are quoted one by one, as a shell here would pass them."""
    if len(argv) == 1:
        return argv[0]
    return " ".join(shlex.quote(a) for a in argv)


def elapsed_since(t0: float) -> float:
    return time.monotonic() - t0
