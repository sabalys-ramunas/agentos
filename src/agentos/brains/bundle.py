"""Materialize a context bundle into a task's working directory (the laptop-to-node handoff).

A bundle can carry a summary, a file list (paths to look at, or small inline files), a
.tar.gz archive of the working tree, and a git remote and ref. Git is preferred: the laptop
pushes a branch, the node clones it, and the node becomes the single writer.

Everything here treats the bundle as untrusted input: archive members cannot escape the work
dir (tarfile's "data" filter, plus size and count caps), inline paths must be relative, the
repo's and the archive's links are kept as links but never written through (inline files and
AGENTOS_HANDOFF.md go through `core.fs.write_beneath`), and
git remotes are limited to https, ssh and scp-style URLs (file:// only when the owner allows
it, which the tests do).
"""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from agentos.brains.models import ContextBundle, GitRef, InlineFile
from agentos.brains.transcript import redact_patterns
from agentos.core.errors import InvalidRequest
from agentos.core.fs import LinkRefused, write_beneath

MAX_ARCHIVE_BYTES = 25_000_000
MAX_EXTRACTED_BYTES = 250_000_000
MAX_MEMBERS = 20_000
MAX_INLINE_FILE = 1_000_000
MAX_INLINE_TOTAL = 5_000_000
MAX_SUMMARY = 20_000
HANDOFF_FILE = "AGENTOS_HANDOFF.md"
_SCP_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/~-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_FULL_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


def _safe_rel(path: str) -> PurePosixPath:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts or path.startswith("~"):
        raise InvalidRequest(f"bundle file path must be relative and stay inside the work dir: {path!r}")
    return p


def validate(bundle: ContextBundle, *, allow_file_git: bool = False) -> None:
    if bundle.summary and len(bundle.summary) > MAX_SUMMARY:
        raise InvalidRequest(f"bundle summary is over {MAX_SUMMARY} characters")
    total = 0
    for f in bundle.files:
        if isinstance(f, InlineFile):
            _safe_rel(f.path)
            size = len(f.content.encode())
            if size > MAX_INLINE_FILE:
                raise InvalidRequest(f"inline file {f.path} is over {MAX_INLINE_FILE} bytes; use git or an archive")
            total += size
    if total > MAX_INLINE_TOTAL:
        raise InvalidRequest("inline files are over 5 MB together; use git or an archive")
    if bundle.archive_b64 and len(bundle.archive_b64) * 3 // 4 > MAX_ARCHIVE_BYTES:
        raise InvalidRequest("archive is over 25 MB; push a branch and pass git instead")
    if bundle.git:
        remote = bundle.git.remote
        ok = remote.startswith(("https://", "ssh://")) or bool(_SCP_RE.match(remote))
        if allow_file_git and remote.startswith(("file://", "/")):
            ok = True
        if not ok:
            raise InvalidRequest("git remote must be an https, ssh or user@host:path URL")
        if not _REF_RE.match(bundle.git.ref) or bundle.git.ref.startswith("-"):
            raise InvalidRequest("git ref has unsupported characters")
        if bundle.git.commit is not None and not _FULL_SHA_RE.match(bundle.git.commit):
            raise InvalidRequest("git commit is a full commit id (40 or 64 hex characters)")


def _extract_archive(data_b64: str, work: Path) -> int:
    raw = base64.b64decode(data_b64, validate=True)
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise InvalidRequest("archive is over 25 MB")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
        members = tar.getmembers()
        if len(members) > MAX_MEMBERS:
            raise InvalidRequest(f"archive has more than {MAX_MEMBERS} entries")
        if sum(m.size for m in members if m.isfile()) > MAX_EXTRACTED_BYTES:
            raise InvalidRequest("archive expands to more than 250 MB")
        try:
            tar.extractall(work, filter="data")
        except tarfile.FilterError as exc:
            raise InvalidRequest(f"archive entry rejected: {exc}") from exc
    return len(members)


def _git_env(home: Path) -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "GIT_TERMINAL_PROMPT": "0", "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8"}


def resolve_commit(git: GitRef, home: Path, *, allow_file: bool) -> str:
    """The full commit `git.ref` names on its remote now, for a co-sign to bind. A full commit id is
    itself; a short one is refused (a person approves one commit, not a prefix)."""
    ref = git.ref
    if _FULL_SHA_RE.match(ref):
        return ref
    if _SHA_RE.match(ref):
        raise InvalidRequest("a co-sign binds one commit: pass the full commit id, or a branch, as git.ref")
    proto = ["-c", f"protocol.file.allow={'always' if allow_file else 'never'}"]
    try:
        out = subprocess.run(["git", *proto, "ls-remote", "--", git.remote, ref], env=_git_env(home), check=True,
                             capture_output=True, text=True, timeout=60).stdout
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or "").strip().splitlines()[-1:] or ["git failed"]
        raise InvalidRequest(f"could not read {ref} on the bundle's remote: {redact_patterns(msg[0])[:300]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise InvalidRequest("reading the bundle's git ref timed out") from exc
    found: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        found[name.strip()] = sha.strip()
    # as clone --branch reads it: a branch, else a tag (peeled to its commit), else HEAD itself
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", ref):
        sha = found.get(name)
        if sha and _FULL_SHA_RE.match(sha):
            return sha
    raise InvalidRequest(f"the bundle's remote has no branch or tag {ref}")


def _git_clone(remote: str, ref: str, depth: int | None, work: Path, *, allow_file: bool,
               commit: str | None = None, git_env: dict[str, str] | None = None) -> str:
    env = {**_git_env(work.parent), **(git_env or {})}
    proto = ["-c", f"protocol.file.allow={'always' if allow_file else 'never'}"]
    tmp = work.parent / (work.name + ".clone")
    is_sha = bool(_SHA_RE.match(ref))
    args = ["git", *proto, "clone", "--quiet", "--no-tags"]
    if depth and not is_sha:
        args += ["--depth", str(depth)]
    if ref != "HEAD" and not is_sha:
        args += ["--branch", ref]
    args += ["--", remote, str(tmp)]
    try:
        subprocess.run(args, env=env, check=True, capture_output=True, text=True, timeout=600)
        if is_sha:
            subprocess.run(["git", "-C", str(tmp), "checkout", "--quiet", ref], env=env, check=True,
                           capture_output=True, text=True, timeout=120)
        head = subprocess.run(["git", "-C", str(tmp), "rev-parse", "HEAD"], env=env, check=True,
                              capture_output=True, text=True, timeout=30).stdout.strip()
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or "").strip().splitlines()[-1:] or ["git failed"]
        raise InvalidRequest(f"git clone of the bundle failed: {redact_patterns(msg[0])[:300]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise InvalidRequest("git clone of the bundle timed out") from exc
    if commit is not None and head != commit:
        shutil.rmtree(tmp, ignore_errors=True)
        raise InvalidRequest(f"{ref} moved since the co-sign: it is at {head[:12]}, and the yes was for "
                             f"{commit[:12]}, so nothing was handed to the brain. Submit the task again to "
                             "approve the commit it is at now")
    # move the clone's contents into the (possibly non-empty) work dir
    for child in tmp.iterdir():
        if os.path.lexists(work / child.name):  # lexists: a link there is never followed
            continue
        child.rename(work / child.name)
    shutil.rmtree(tmp, ignore_errors=True)
    return head


def materialize(bundle: ContextBundle | None, work: Path, *, redact: bool = False,
                allow_file_git: bool = False, git_env: dict[str, str] | None = None) -> str | None:
    """Write the bundle into `work`. Returns the handoff text for the prompt (or None). `git_env`
    points the clone at the node's git gateway (agentos.git) when the remote is declared, so a
    private repository clones with the vault's credential, which the clone itself never holds."""
    if bundle is None:
        return None
    validate(bundle, allow_file_git=allow_file_git)
    work.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    if bundle.git:
        head = _git_clone(bundle.git.remote, bundle.git.ref, bundle.git.depth, work,
                          allow_file=allow_file_git, commit=bundle.git.commit, git_env=git_env)
        notes.append(f"The repository {bundle.git.remote} at {bundle.git.ref} (commit {head[:12]}) is checked "
                     "out in your working directory.")
    if bundle.archive_b64:
        n = _extract_archive(bundle.archive_b64, work)
        notes.append(f"An archive of the previous working tree ({n} entries) is unpacked in your working directory.")
    look_at: list[str] = []
    for f in bundle.files:
        if isinstance(f, InlineFile):
            rel = _safe_rel(f.path)
            try:  # the repo or the archive may have put a link on the way: never write through it
                write_beneath(work, rel, redact_patterns(f.content) if redact else f.content, mode=0o644,
                              dir_mode=0o755, refuse_link=True)
            except LinkRefused as exc:
                raise InvalidRequest(f"inline file {f.path} would be written through a link in the "
                                     "repository or archive, so the bundle was refused") from exc
            look_at.append(str(rel))
        else:
            look_at.append(str(_safe_rel(f)))
    summary = bundle.summary or ""
    if redact:
        summary = redact_patterns(summary)
    parts = ["## Handoff from the previous session"]
    if summary:
        parts.append(summary.strip())
    parts += notes
    if look_at:
        parts.append("Files to look at first: " + ", ".join(look_at))
    text = "\n\n".join(parts)
    if len(parts) == 1:
        return None
    # a link of that name from the repo is replaced, never written through (or chmodded)
    write_beneath(work, HANDOFF_FILE, text + "\n", mode=0o644, dir_mode=0o755)
    return text
