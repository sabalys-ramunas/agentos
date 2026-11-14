"""Private git repositories on VM lanes, without the token ever reaching the VM.

The owner stores a read-only token per repo or per owner (`agentos vm repo add github.com/acme/*`)
in the vault, as a machine secret. `vm_clone` then clones on the node, with the token handed to git
through GIT_ASKPASS and the environment (never the URL, argv or .git/config), and streams the
checkout to the VM over the VM's SSH as a tar. The VM gets the code and a clean `origin` URL; it
never sees the token, so code that runs there (install scripts, a PR's tests) cannot steal it.

Only https remotes on public host names: no file://, ssh, ext:: or submodules, no IP addresses or
localhost (the node must not fetch from its own network on an agent's say-so).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agentos.core.errors import InvalidRequest
from agentos.vms.models import VmError

REF_PREFIX = "vms/git/"
SECRET_KIND = "git_token"
ALLOWED_PROTOCOLS = ("https",)   # tests add "file"
GIT = "git"
_HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
_SEG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_DEST_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,250}$")
DEFAULT_USERS = {"github.com": "x-access-token", "gitlab.com": "oauth2", "bitbucket.org": "x-token-auth"}

ASKPASS = """#!/bin/sh
case "$1" in
  Username*) printf '%s\\n' "$AGENTOS_GIT_USER" ;;
  *) printf '%s\\n' "$AGENTOS_GIT_TOKEN" ;;
esac
"""


@dataclass(frozen=True)
class RepoName:
    host: str
    path: str                       # org/repo, or group/sub/repo

    @property
    def name(self) -> str:
        return f"{self.host}/{self.path}"

    @property
    def short(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.path}.git"


def _host(host: str) -> str:
    host = host.strip().lower()
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(host.strip("[]"))
        raise InvalidRequest("a repo host is a name such as github.com, not an IP address")
    if not _HOST_RE.match(host) or host.endswith((".local", ".internal", ".localhost")):
        raise InvalidRequest(f"not a public git host name: {host!r}")
    return host


def _segments(path: str, *, wildcard: bool = False) -> str:
    path = path.strip("/")
    path = path.removesuffix(".git")
    segs = path.split("/")
    if len(segs) < (1 if wildcard else 2):
        raise InvalidRequest("a repo looks like github.com/org/repo")
    for i, s in enumerate(segs):
        if wildcard and s == "*" and i == len(segs) - 1:
            continue
        if not _SEG_RE.match(s) or s in (".", ".."):
            raise InvalidRequest(f"not a repo path segment: {s!r}")
    return "/".join(segs)


def parse_repo(text: str) -> RepoName:
    """github.com/org/repo, https://github.com/org/repo(.git) or git@github.com:org/repo.git."""
    t = (text or "").strip()
    if m := re.match(r"^git@([^:/]+):(.+)$", t):
        return RepoName(_host(m.group(1)), _segments(m.group(2)))
    if "://" in t:
        u = urlsplit(t)
        if u.scheme not in ("https", "ssh"):
            raise InvalidRequest("only https (or git@) repo addresses")
        if u.password or (u.username and u.scheme == "https"):
            raise InvalidRequest("do not put credentials in the repo address; the node adds the owner's token")
        if u.port or u.query or u.fragment:
            raise InvalidRequest("a repo address has no port, query or fragment")
        return RepoName(_host(u.hostname or ""), _segments(u.path))
    host, _, path = t.partition("/")
    return RepoName(_host(host), _segments(path))


def parse_pattern(text: str) -> str:
    """A repo (github.com/acme/api) or every repo under a path (github.com/acme/*)."""
    t = (text or "").strip()
    if t.endswith("/*"):
        host, _, path = t[:-2].partition("/")
        if "://" in host or "@" in host:
            raise InvalidRequest("a token pattern looks like github.com/acme/* or github.com/acme/api")
        return f"{_host(host)}/{_segments(path + '/*', wildcard=True)}" if path else f"{_host(host)}/*"
    return parse_repo(t).name


def matches(pattern: str, repo: RepoName) -> bool:
    if pattern.endswith("/*"):
        return repo.name.startswith(pattern[:-1])
    return pattern == repo.name


def best_match(patterns: list[str], repo: RepoName) -> str | None:
    """The exact pattern, else the longest wildcard that covers the repo."""
    hits = [p for p in patterns if matches(p, repo)]
    if not hits:
        return None
    return max(hits, key=lambda p: (not p.endswith("/*"), len(p)))


def secret_ref(pattern: str) -> str:
    return REF_PREFIX + hashlib.sha256(pattern.encode()).hexdigest()[:24]


class RepoTokens:
    """Read-only git tokens for VM clones, as vault machine secrets (one per pattern)."""

    def __init__(self, vault: Any):
        self.vault = vault

    def _need(self) -> Any:
        if self.vault is None or not callable(getattr(self.vault, "put_secret", None)):
            raise VmError("repo tokens live in the vault and this node has none: run `agentos vault init` first")
        return self.vault

    async def add(self, pattern: str, token: str, *, user: str | None = None) -> dict:
        pattern = parse_pattern(pattern)
        token = (token or "").strip()
        if not token or any(c.isspace() for c in token):
            raise InvalidRequest("a token is one word with no spaces")
        host = pattern.split("/", 1)[0]
        user = user or DEFAULT_USERS.get(host, "git")
        if not _SEG_RE.match(user):
            raise InvalidRequest("a git user name is letters, digits, dots, dashes and underscores")
        await self._need().put_secret(secret_ref(pattern), token, kind=SECRET_KIND,
                                      label=f"git token for {pattern} (VM clones)",
                                      scope={"pattern": pattern, "user": user})
        return {"pattern": pattern, "user": user}

    async def list(self) -> list[dict]:
        vault = self.vault
        if vault is None or not callable(getattr(vault, "list_secrets", None)):
            return []
        out = []
        for m in await vault.list_secrets():
            if m.get("ref", "").startswith(REF_PREFIX) and m.get("kind") == SECRET_KIND:
                scope = m.get("scope") or {}
                out.append({"pattern": scope.get("pattern"), "user": scope.get("user"),
                            "last_used_at": m.get("last_used_at"), "use_count": m.get("use_count", 0)})
        return sorted(out, key=lambda r: r["pattern"] or "")

    async def remove(self, pattern: str) -> bool:
        pattern = parse_pattern(pattern)
        if pattern not in {r["pattern"] for r in await self.list()}:
            return False
        await self._need().delete_secret(secret_ref(pattern))
        return True

    async def pattern_for(self, repo: RepoName) -> str | None:
        """Which stored pattern covers the repo (no unseal, no read recorded)."""
        return best_match([r["pattern"] for r in await self.list() if r["pattern"]], repo)

    async def token_for(self, repo: RepoName, *, purpose: str) -> tuple[str, str, str] | None:
        """(pattern, user, token) for the repo, or None. The read is recorded with its purpose."""
        rows = {r["pattern"]: r for r in await self.list() if r["pattern"]}
        pattern = best_match(list(rows), repo)
        if pattern is None:
            return None
        token = await self._need().get_secret(secret_ref(pattern), purpose=purpose)
        if not token:
            return None
        return pattern, rows[pattern]["user"] or DEFAULT_USERS.get(repo.host, "git"), token


def check_dest(dest: str) -> str:
    dest = dest.rstrip("/")
    if not _DEST_RE.match(dest) or any(s in (".", "..") for s in dest.split("/")[1:]) or dest.count("/") < 2:
        raise InvalidRequest("dest is an absolute directory on the VM such as /root/src/api")
    return dest


def check_ref(ref: str | None) -> str | None:
    if ref is None or ref == "":
        return None
    if not _REF_RE.match(ref) or ".." in ref or ref.endswith((".lock", "/")):
        raise InvalidRequest(f"not a git branch, tag or commit: {ref!r}")
    return ref


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _git_env(home: Path, askpass: Path, user: str, token: str) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(askpass), "SSH_ASKPASS": str(askpass),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_LFS_SKIP_SMUDGE": "1",
            "AGENTOS_GIT_USER": user, "AGENTOS_GIT_TOKEN": token}


async def _git(args: list[str], env: dict[str, str], *, token: str, timeout_s: float, cwd: Path | None = None
               ) -> str:
    allow = ["-c", "protocol.allow=never"] + [x for p in ALLOWED_PROTOCOLS for x in ("-c", f"protocol.{p}.allow=always")]
    argv = [GIT, "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", *allow, *args]
    proc = await asyncio.create_subprocess_exec(*argv, env=env, cwd=str(cwd) if cwd else None,
                                                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise VmError(f"git {args[0]} took longer than {int(timeout_s)} s") from None
    if proc.returncode != 0:
        msg = _scrub((err or b"").decode(errors="replace").strip(), token)[-600:]
        if "Authentication failed" in msg or "could not read Username" in msg or "403" in msg:
            msg += " (the stored token may have expired or lack read access to this repo)"
        raise VmError(f"git {args[0]} failed: {msg or f'exit {proc.returncode}'}")
    return (out or b"").decode(errors="replace").strip()


def _size_mb(root: Path) -> float:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dirpath, f)).st_size
    return total / (1024 * 1024)


async def checkout(repo: RepoName, *, user: str, token: str, workdir: Path, ref: str | None = None,
                   depth: int | None = None, timeout_s: float = 600, max_mb: int = 2048,
                   url: str | None = None) -> tuple[Path, str, float]:
    """Clone `repo` into workdir/repo on the node. Returns (the checkout, its commit, its size
    in MB). The token lives only in git's environment; .git/config keeps the plain https URL."""
    workdir.mkdir(parents=True, exist_ok=True)
    workdir.chmod(0o700)
    home = workdir / "home"
    home.mkdir(exist_ok=True)
    askpass = workdir / "askpass"
    askpass.write_text(ASKPASS)
    askpass.chmod(0o700)
    env = _git_env(home, askpass, user, token)
    dest = workdir / "repo"
    args = ["clone", "--quiet", "--no-recurse-submodules"]
    if depth:
        args += ["--depth", str(int(depth))]
        if ref:
            args += ["--branch", ref]
    args += ["--", url or repo.url, str(dest)]
    await _git(args, env, token=token, timeout_s=timeout_s)
    if ref and not depth:
        for cand in (f"origin/{ref}", ref):  # a branch name means the remote's branch
            try:
                sha = await _git(["rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}"], env, token=token,
                                 timeout_s=30, cwd=dest)
                break
            except VmError:
                continue
        else:
            raise VmError(f"{repo.name} has no branch, tag or commit {ref!r}")
        await _git(["checkout", "--quiet", "--detach", sha], env, token=token, timeout_s=120, cwd=dest)
    commit = await _git(["rev-parse", "HEAD"], env, token=token, timeout_s=30, cwd=dest)
    size = _size_mb(dest)
    if size > max_mb:
        raise VmError(f"{repo.name} is {size:.0f} MB checked out, more than the {max_mb} MB limit "
                      "(vms.toml clone_max_mb); try depth=1")
    return dest, commit, size
