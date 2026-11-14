"""The declared remotes: which repositories a workspace or a lane may fetch from and push to.

`AGENTOS_HOME/git/remotes.json` (0600), written by the node's CLI (`agentos git remote allow`),
read by the daemon on every request (the file's stamp keeps a parsed copy). A remote nobody
declared is refused by the gateway, whatever credential the vault holds for its host: the
declaration is the owner's yes, as a vault binding is for a login.

A declaration is for a scope: a workspace (`personal`, every lane in it) or one lane
(`personal/code`). On a lane it also names the account a push is a commit on, so that lane's
"Ask me first" switch decides whether a push runs on its own (docs/GIT.md).
"""

from __future__ import annotations

import builtins
import fnmatch
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.fs import write_private_json
from agentos.core.models import LANE_ID_RE
from agentos.git.wire import Remote, normalize_host, normalize_repo_pattern, parse_remote, repo_matches

WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_BRANCH_GLOB_RE = re.compile(r"^[A-Za-z0-9._/*-]{1,200}$")


def check_scope(scope: str) -> str:
    """A workspace (`personal`) or a lane (`personal/code`)."""
    s = (scope or "").strip()
    if WORKSPACE_RE.match(s) or LANE_ID_RE.match(s):
        return s
    raise InvalidRequest(f"a remote is declared for a workspace (personal) or a lane (personal/code), not {scope!r}")


def upstream_host(url: str) -> str:
    """The host of an explicit upstream URL (https://, ssh://, user@host:path). Its path may be
    anything the host serves (a self-hosted server's absolute path), so only the host is checked."""
    u = url.strip()
    if "://" in u:
        parts = urlsplit(u)
        if parts.scheme not in ("https", "ssh") or parts.password or parts.query or parts.fragment:
            raise InvalidRequest("an upstream url is https:// or ssh://, without a password, query or fragment")
        if parts.scheme == "https" and parts.username:
            raise InvalidRequest("an upstream https url carries no user; store a token with `agentos git key add`")
        return normalize_host(parts.hostname or "")
    user, at, rest = u.partition("@")
    host, colon, _path = rest.partition(":")
    if not at or not colon or not user:
        raise InvalidRequest("an upstream url is https://, ssh:// or user@host:path")
    return normalize_host(host)


def scope_workspace(scope: str) -> str:
    return scope.split("/", 1)[0]


class RemoteRule(BaseModel):
    """One declared remote. `protect` adds branch globs to the default branch, which is protected
    unless `allow_default_branch`; a push to a protected branch is irreversible (always a person)."""
    scope: str
    host: str
    repo: str                       # owner/name, or owner/* for every repository of owner
    push: bool = True               # False: fetch only
    protect: list[str] = Field(default_factory=list)
    allow_default_branch: bool = False
    url: str | None = None          # the real remote, when it is not the host's usual address
    added_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    @field_validator("scope")
    @classmethod
    def _scope(cls, v: str) -> str:
        return check_scope(v)

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return normalize_host(v)

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        return normalize_repo_pattern(v)

    @field_validator("protect")
    @classmethod
    def _protect(cls, v: list[str]) -> list[str]:
        for g in v:
            if not _BRANCH_GLOB_RE.match(g):
                raise InvalidRequest(f"not a branch name or glob: {g!r}")
        return sorted(set(v))

    @property
    def slug(self) -> str:
        return f"{self.host}/{self.repo}"

    @property
    def on_lane(self) -> bool:
        return "/" in self.scope

    def matches(self, remote: Remote) -> bool:
        return self.host == remote.host and repo_matches(self.repo, remote.repo)

    def protected(self, branch: str, default_branch: str | None) -> bool:
        """A push to `branch` is irreversible: the default branch (unless allowed) or a glob of
        `protect`."""
        if default_branch and branch == default_branch and not self.allow_default_branch:
            return True
        return any(fnmatch.fnmatchcase(branch, g) for g in self.protect)

    def describe(self) -> str:
        what = "fetch and push" if self.push else "fetch only"
        guard = "default branch asks" if not self.allow_default_branch else "default branch allowed"
        extra = f", also protected: {', '.join(self.protect)}" if self.protect else ""
        return f"{self.slug} for {self.scope}: {what}, {guard}{extra}"


class Remotes:
    """The declarations file. Thread-safe; re-read when the file changes (the CLI writes it)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._rules: list[RemoteRule] = []

    def _load(self) -> builtins.list[RemoteRule]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._stamp, self._rules = None, []
            return []
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            raw = json.loads(self.path.read_text() or "{}")
            self._rules = [RemoteRule(**r) for r in raw.get("remotes", [])]
            self._stamp = stamp
        return list(self._rules)

    def list(self, scope: str | None = None) -> builtins.list[RemoteRule]:
        with self._lock:
            rules = self._load()
        if scope is None:
            return rules
        ws = scope_workspace(scope)
        if "/" in scope:  # a lane: its own declarations and its workspace's
            return [r for r in rules if r.scope in (scope, ws)]
        return [r for r in rules if scope_workspace(r.scope) == ws]  # a workspace: all of it

    def _save(self, rules: builtins.list[RemoteRule]) -> None:
        write_private_json(self.path, {"version": 1, "remotes": [r.model_dump() for r in rules]}, newline=True)
        self._stamp = None

    def allow(self, scope: str, remote: str, *, push: bool = True, protect: builtins.list[str] | None = None,
              allow_default_branch: bool = False, url: str | None = None) -> RemoteRule:
        """Declare (or replace) `remote` (`host/owner/name`, `host/owner/*` or any remote URL) for a
        workspace or a lane."""
        scope = check_scope(scope)
        if remote.rstrip("/").endswith("/*"):
            host, _, pattern = remote.strip().partition("/")
            host, repo = normalize_host(host), normalize_repo_pattern(pattern)
        else:
            r = parse_remote(remote)
            host, repo = r.host, r.repo
        if url is not None:
            if repo.endswith("/*"):
                raise InvalidRequest("an explicit url is for one repository, not owner/*")
            if not url.startswith(("file://", "/")):
                url_host = upstream_host(url)
                if url_host != host:
                    raise InvalidRequest(f"the url's host {url_host} is not {host}")
        rule = RemoteRule(scope=scope, host=host, repo=repo, push=push, protect=list(protect or []),
                          allow_default_branch=allow_default_branch, url=url)
        with self._lock:
            rules = [r for r in self._load() if not (r.scope == scope and r.host == host and r.repo == repo)]
            rules.append(rule)
            self._save(sorted(rules, key=lambda r: (r.scope, r.host, r.repo)))
        return rule

    def deny(self, scope: str, remote: str) -> RemoteRule:
        """Remove a declaration. NotFound when there is none for exactly this scope and remote."""
        scope = check_scope(scope)
        if remote.rstrip("/").endswith("/*"):
            host, _, pattern = remote.strip().partition("/")
            host, repo = normalize_host(host), normalize_repo_pattern(pattern)
        else:
            r = parse_remote(remote)
            host, repo = r.host, r.repo
        with self._lock:
            rules = self._load()
            keep = [r for r in rules if not (r.scope == scope and r.host == host and r.repo == repo)]
            if len(keep) == len(rules):
                raise NotFound(f"{host}/{repo} is not declared for {scope}; `agentos git remote list` shows what is")
            gone = next(r for r in rules if r not in keep)
            self._save(keep)
        return gone

    def find(self, remote: Remote, scopes: builtins.list[str]) -> RemoteRule | None:
        """The declaration that lets these scopes use `remote`: the first scope (in the order given)
        that has one, an exact repository before an `owner/*`."""
        rules = self.list()
        for scope in scopes:
            found = [r for r in rules if r.scope == scope and r.matches(remote)]
            if found:
                return min(found, key=lambda r: r.repo.endswith("/*"))
        return None

    def hosts_for(self, scopes: builtins.list[str]) -> builtins.list[str]:
        """The hosts declared for any of these scopes: the ones a lane's git is pointed at the
        gateway for."""
        want = set(scopes)
        return sorted({r.host for r in self.list() if r.scope in want})
