"""Git credentials: SSH deploy keys made on the node, and tokens, kept as vault machine secrets.

A credential is a machine secret (agentos.vault, sealed with the vault's data key, every read
recorded with its purpose, scrubbed from traces and logs) under the ref `git:<name>`, with a scope
that binds it to one host and a set of repositories:

    {"host": "github.com", "repos": "owner/app,owner/site", "type": "ssh",
     "public_key": "ssh-ed25519 AAAA... agentos@node", "fingerprint": "SHA256:..."}

A deploy key is generated here (Ed25519, in memory) and only its public half is ever printed:
the owner pastes it into the repository's deploy keys. The private half goes straight into the
vault and leaves it only into the node's own SSH agent (agentos.git.agent), never into a file.
A token (a fine-grained personal access token) is typed by the owner and sent only in the HTTP
exchange with its host.

Host keys: the node checks the remote's SSH host key against KNOWN_HOSTS (the published keys of
github.com, gitlab.com and bitbucket.org) and against `AGENTOS_HOME/git/known_hosts`, where
`agentos git key add --host <other>` records the key it saw, with its fingerprint shown to the owner.
"""

from __future__ import annotations

import base64
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentos.core.errors import InvalidRequest
from agentos.core.fs import write_private
from agentos.git.wire import Remote, normalize_host, normalize_repo_pattern, repo_matches
from agentos.vault.secret import Secret

REF_PREFIX = "git:"
SSH_KIND = "git_ssh_key"
TOKEN_KIND = "git_token"
KINDS = (SSH_KIND, TOKEN_KIND)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}$")
REPOS_MAX = 200  # characters of the comma-joined repository list a vault scope can hold
DEFAULT_TOKEN_USER = "x-access-token"  # GitHub's; GitLab and Gitea take any non-empty user with a token

# The SSH host keys the big hosts publish (ed25519). A key the remote shows that is not here, nor in
# AGENTOS_HOME/git/known_hosts, stops the connection: the node never trusts a key on first sight.
KNOWN_HOSTS: dict[str, list[str]] = {
    "github.com": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"],
    "gitlab.com": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAfuCHKVTjquxvt6CM6tdG4SLp1Btn/nOeHHE5UOzRdf"],
    "bitbucket.org": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIazEu89wgQZ4bqs3d63QSMzYVa0MuJ2e2gKTKqu+UUO"],
}


@dataclass(frozen=True)
class Credential:
    """What may be known about a git credential: never its value."""
    ref: str
    kind: str
    host: str
    repos: tuple[str, ...]
    label: str
    public_key: str | None = None
    fingerprint: str | None = None
    username: str | None = None
    last_used_at: str | None = None
    use_count: int = 0
    any_repo: bool = False   # the GitHub login (agentos.git.login): names no repository, serves any for env

    @property
    def name(self) -> str:
        return self.ref[len(REF_PREFIX):]

    @property
    def type(self) -> str:
        return "ssh" if self.kind == SSH_KIND else "token"

    def matches(self, remote: Remote) -> bool:
        return self.host == remote.host and any(repo_matches(p, remote.repo) for p in self.repos)

    def exact(self, remote: Remote) -> bool:
        return remote.repo in self.repos

    def public(self) -> dict[str, Any]:
        """For `agentos git key list` and the gateway's messages."""
        return {"name": self.name, "type": self.type, "host": self.host, "repos": list(self.repos),
                "public_key": self.public_key, "fingerprint": self.fingerprint, "label": self.label,
                "last_used_at": self.last_used_at, "use_count": self.use_count, "any_repo": self.any_repo}


def check_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not _NAME_RE.match(n):
        raise InvalidRequest(f"a key name is lowercase letters, digits, dots, dashes: {name!r}")
    return n


def default_name(host: str, repos: list[str], kind: str) -> str:
    first = repos[0].replace("/*", "").replace("/", "-")
    return check_name(f"{host.split('.')[0]}-{first}-{'ssh' if kind == SSH_KIND else 'token'}")[:60]


def check_repos(repos: list[str]) -> list[str]:
    if not repos:
        raise InvalidRequest("name at least one repository: --repo owner/name (or owner/* for all of owner)")
    out = sorted({normalize_repo_pattern(r) for r in repos})
    if len(",".join(out)) > REPOS_MAX:
        raise InvalidRequest(f"too many repositories for one credential (at most {REPOS_MAX} characters "
                             "together); use owner/* or a second credential")
    return out


def generate_ed25519(comment: str) -> tuple[Secret, str, str]:
    """A new Ed25519 key: (the private key in OpenSSH format, as a Secret; the public line; its
    SHA256 fingerprint). Made in memory; nothing is written."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                           serialization.PublicFormat.OpenSSH).decode()
    line = f"{public} {comment}".strip()
    return Secret(private), line, fingerprint(line)


def load_private(private: str) -> Ed25519PrivateKey:
    """The key object for a stored private key (the SSH agent signs with it)."""
    key = serialization.load_ssh_private_key(private.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise InvalidRequest("only Ed25519 deploy keys are supported")
    return key


def key_blob(public_line: str) -> bytes:
    """The wire blob of an `ssh-ed25519 AAAA...` line."""
    parts = public_line.split()
    if len(parts) < 2:
        raise InvalidRequest("not an SSH public key")
    try:
        return base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise InvalidRequest("not an SSH public key") from None


def fingerprint(public_line: str) -> str:
    """`SHA256:...` as ssh-keygen -l prints it."""
    digest = hashlib.sha256(key_blob(public_line)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def ssh_scope(host: str, repos: list[str], public_line: str) -> dict[str, Any]:
    return {"host": normalize_host(host), "repos": ",".join(check_repos(repos)), "type": "ssh",
            "public_key": public_line[:200], "fingerprint": fingerprint(public_line)}


def token_scope(host: str, repos: list[str], username: str | None) -> dict[str, Any]:
    user = (username or DEFAULT_TOKEN_USER).strip()
    if not re.match(r"^[A-Za-z0-9._@+-]{1,100}$", user):
        raise InvalidRequest("the token's user name is letters, digits and . _ @ + -")
    return {"host": normalize_host(host), "repos": ",".join(check_repos(repos)), "type": "token", "username": user}


def check_token(token: str) -> Secret:
    t = (token or "").strip()
    if len(t) < 8 or len(t) > 1024 or any(c.isspace() or ord(c) < 0x21 or ord(c) > 0x7e for c in t):
        raise InvalidRequest("a token is one line of printable characters, at least 8 of them")
    return Secret(t)


def from_meta(meta: dict[str, Any]) -> Credential | None:
    """A Credential from a vault machine secret's metadata, or None for any other secret."""
    ref, kind = str(meta.get("ref", "")), meta.get("kind")
    if not ref.startswith(REF_PREFIX) or kind not in KINDS:
        return None
    scope = meta.get("scope") or {}
    try:
        host = normalize_host(str(scope.get("host", "")))
        repos = tuple(normalize_repo_pattern(r) for r in str(scope.get("repos", "")).split(",") if r)
    except InvalidRequest:
        return None
    return Credential(ref=ref, kind=kind, host=host, repos=repos, label=str(meta.get("label", "")),
                      public_key=scope.get("public_key"), fingerprint=scope.get("fingerprint"),
                      username=scope.get("username"), last_used_at=meta.get("last_used_at"),
                      use_count=int(meta.get("use_count") or 0), any_repo=scope.get("any_repo") is True)


def pick(credentials: list[Credential], remote: Remote) -> Credential | None:
    """The credential for `remote`: one naming the repository before one for `owner/*`, a deploy
    key before a token (a deploy key reaches one repository; a token often more)."""
    found = [c for c in credentials if c.matches(remote)]
    found.sort(key=lambda c: (not c.exact(remote), c.kind != SSH_KIND, c.ref))
    return found[0] if found else None


# ------------------------------------------------------------------ host keys


def known_hosts_path(home: Path) -> Path:
    return Path(home) / "git" / "known_hosts"


def host_patterns(host: str, port: int | None) -> tuple[str, ...]:
    return (host,) if port in (None, 22) else (f"[{host}]:{port}",)


def known_hosts_lines(home: Path, host: str, port: int | None = None) -> list[str]:
    """The host keys the node accepts for `host` (and `port`): the pinned ones, then the file's."""
    pats = host_patterns(host, port)
    lines = [f"{pats[0]} {k}" for k in KNOWN_HOSTS.get(host, [])] if port in (None, 22) else []
    path = known_hosts_path(home)
    if path.is_file():
        for raw in path.read_text().splitlines():
            parts = raw.split()
            if len(parts) >= 3 and not raw.startswith("#") and set(parts[0].split(",")) & set(pats):
                lines.append(" ".join(parts[:3]))
    return lines


def scan_host_keys(host: str, port: int | None = None, timeout: int = 10) -> list[str]:
    """`ssh-keyscan` of a host: its key lines, to show the owner before they are kept."""
    argv = ["ssh-keyscan", "-T", str(timeout), "-t", "ed25519,ecdsa,rsa"]
    if port not in (None, 22):
        argv += ["-p", str(port)]
    try:
        out = subprocess.run([*argv, host], capture_output=True, text=True, timeout=timeout + 5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.splitlines() if line.strip() and not line.startswith("#")]


def add_known_hosts(home: Path, lines: list[str]) -> None:
    """Keep host key lines (deduplicated) in AGENTOS_HOME/git/known_hosts."""
    path = known_hosts_path(home)
    have = path.read_text().splitlines() if path.is_file() else []
    keep = list(dict.fromkeys([*(x for x in have if x.strip()), *(" ".join(x.split()[:3]) for x in lines)]))
    write_private(path, "\n".join(keep) + "\n")
