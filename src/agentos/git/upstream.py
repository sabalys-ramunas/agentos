"""Git against the real remote, run by agentosd: the mirror's fetch and push, with the credential.

Every git process here is the node's own, outside any lane, with an environment built from an
allowlist (nothing of agentosd's own environment), no system or global config, no prompts, and
only the https and ssh transports (file only where the owner's tests allow it). The credential
reaches it two ways and no other:

- a deploy key: `IdentityAgent` names a SingleKeyAgent socket that lives for this one operation,
  `IdentityFile` the public half (so ssh offers only that key, never root's own), and the host key
  is checked against the pinned and the owner's known hosts, never accepted on first sight;
- a token: an `http.extraHeader` (Basic, with the token as the password) in GIT_CONFIG_* variables
  of this process, so it is in no command line (`ps`) and in no file.

Errors are explained in words (the remote refused the key, the host key is unknown, the branch moved),
scrubbed of the credential; the raw output of the remote is never returned whole.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import shlex
import shutil
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentos.git.agent import SingleKeyAgent
from agentos.git.keys import SSH_KIND, Credential, key_blob, known_hosts_lines, load_private
from agentos.git.remotes import RemoteRule
from agentos.git.wire import Command, Remote

GIT_TIMEOUT = 900.0
log = logging.getLogger("agentos.git")


class UpstreamError(Exception):
    """The real remote refused or failed; `message` says what to do, with no credential in it."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Target:
    """Where the mirror's remote is and how to reach it."""
    remote: Remote
    url: str
    credential: Credential | None
    port: int | None = None
    user: str = "git"


def target_for(rule: RemoteRule, remote: Remote, credential: Credential | None) -> Target:
    """The real remote's URL: the declaration's `url`, else the host's usual address for the
    credential's kind (ssh for a deploy key, https for a token or none)."""
    if rule.url:
        url = rule.url
    elif credential is not None and credential.kind == SSH_KIND:
        url = f"git@{remote.host}:{remote.repo}.git"
    else:
        url = f"https://{remote.host}/{remote.repo}.git"
    port, user = None, "git"
    if url.startswith("ssh://"):
        from urllib.parse import urlsplit
        u = urlsplit(url)
        port, user = u.port, u.username or "git"
    elif "@" in url and "://" not in url:
        user = url.split("@", 1)[0]
    return Target(remote, url, credential, port, user)


def git_bin() -> str:
    return shutil.which("git") or "git"


def base_env(home: Path) -> dict[str, str]:
    path = os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    return {"PATH": path, "HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false", "GCM_INTERACTIVE": "never"}


def config_env(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """GIT_CONFIG_COUNT and friends: config for this process only, in no file and no argv."""
    env = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = k
        env[f"GIT_CONFIG_VALUE_{i}"] = v
    return env


def protocol_args(allow_file: bool) -> list[str]:
    args = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always"]
    if allow_file:
        args += ["-c", "protocol.file.allow=always"]
    return args


@dataclass
class Auth:
    """The environment one upstream git operation runs with (built by `credential_env`)."""
    env: dict[str, str]
    agent: SingleKeyAgent | None = None
    scrub: list[str] = field(default_factory=list)


@contextlib.asynccontextmanager
async def credential_env(target: Target, *, home: Path, agent_dir: Path,
                         read_secret: Callable[[str, str], Any], purpose: str) -> AsyncIterator[Auth]:
    """The env for git to reach `target` with its credential, for the length of the block.
    `read_secret(ref, purpose)` opens the vault (recorded with the purpose)."""
    env = base_env(home)
    cred = target.credential
    if cred is None:
        env.update(config_env([("credential.helper", "")]))
        yield Auth(env)
        return
    value = await read_secret(cred.ref, purpose)
    if not value:
        raise UpstreamError(f"the credential {cred.name} is no longer in the vault (or the vault is sealed); "
                            "the owner runs `agentos git key list` on the node")
    if cred.kind != SSH_KIND:
        user = cred.username or "x-access-token"
        basic = base64.b64encode(f"{user}:{value}".encode()).decode()
        env.update(config_env([("credential.helper", ""), ("http.extraHeader", f"Authorization: Basic {basic}")]))
        yield Auth(env, scrub=[value, basic])
        return
    key = load_private(value)
    del value
    known = known_hosts_lines(home, target.remote.host, target.port)
    host_keys = [key_blob(" ".join(line.split()[1:3])) for line in known]
    agent = SingleKeyAgent(key, cred.public_key or "", agent_dir, user=target.user, host_keys=host_keys)
    async with agent as sock:
        pub = agent.dir / "id.pub"
        pub.write_text((cred.public_key or "") + "\n")
        hosts = agent.dir / "known_hosts"
        hosts.write_text("\n".join(known) + "\n")
        opts = {
            "IdentityAgent": str(sock), "IdentityFile": str(pub), "IdentitiesOnly": "yes",
            "UserKnownHostsFile": str(hosts), "GlobalKnownHostsFile": os.devnull, "StrictHostKeyChecking": "yes",
            "UpdateHostKeys": "no", "BatchMode": "yes", "PasswordAuthentication": "no",
            "KbdInteractiveAuthentication": "no", "ForwardAgent": "no", "ForwardX11": "no",
            "ClearAllForwardings": "yes", "ControlMaster": "no", "ControlPath": "none", "ProxyCommand": "none",
            "PermitLocalCommand": "no", "ConnectTimeout": "20", "LogLevel": "ERROR",
        }
        ssh = ["ssh", "-F", os.devnull] + [f"-o{k}={v}" for k, v in opts.items()]
        env["GIT_SSH_COMMAND"] = " ".join(shlex.quote(a) for a in ssh)
        env["GIT_SSH_VARIANT"] = "ssh"
        env.update(config_env([("credential.helper", "")]))
        yield Auth(env, agent=agent)


async def run_git(args: list[str], *, cwd: Path | None, env: dict[str, str], stdin: bytes | None = None,
                  timeout: float = GIT_TIMEOUT) -> tuple[int, bytes, bytes]:
    """Run git; (exit code, stdout, stderr). A timeout kills it and reads as exit 124."""
    proc = await asyncio.create_subprocess_exec(
        git_bin(), *args, cwd=str(cwd) if cwd else None, env=env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return 124, b"", b"timed out"
    return proc.returncode or 0, out, err


async def ls_remote(url: str, env: dict[str, str], *, timeout_s: float = 20.0,
                    allow_file: bool = False) -> tuple[bool, bytes]:
    """`git ls-remote <url> HEAD` with `env` (base_env, or credential_env's): can the node read the
    repository? (True, b"") or (False, git's stderr). A timeout reads as "timed out"."""
    rc, _out, err = await run_git([*protocol_args(allow_file), "ls-remote", url, "HEAD"], cwd=None, env=env,
                                  timeout=timeout_s)
    return rc == 0, err


def unreachable(stderr: bytes) -> bool:
    """Did git fail to reach the host at all (no answer, no name), rather than being refused?"""
    low = stderr.decode("utf-8", "replace").lower()
    return any(w in low for w in ("timed out", "could not resolve host", "connection refused",
                                  "network is unreachable", "failed to connect"))


def explain(stderr: bytes, target: Target, *, scrub: Sequence[str] = ()) -> str:
    """What went wrong with the real remote, in words, without the credential."""
    text = stderr.decode("utf-8", "replace")
    for s in scrub:
        if s:
            text = text.replace(s, "[redacted]")
    log.warning("git: %s failed: %s", target.remote.slug, " | ".join(text.strip().splitlines()[-6:])[:800])
    low = text.lower()
    slug, cred = target.remote.slug, target.credential
    if "host key verification failed" in low or "no ed25519 host key is known" in low \
            or "host key for" in low:
        return (f"the SSH host key of {target.remote.host} is not one the node knows; the owner checks it and "
                f"runs `agentos git key add --host {target.remote.host} ...` on the node, which records it")
    if "permission denied (publickey" in low or "could not read from remote repository" in low \
            or "authentication failed" in low or "invalid username or password" in low \
            or "returned error: 403" in low or "could not read username" in low:
        if cred is None:
            return (f"{slug} needs a credential and the vault has none for it; the owner runs "
                    f"`agentos git key add --host {target.remote.host} --repo {target.remote.repo}` on the node")
        if cred.kind == SSH_KIND:
            return (f"{target.remote.host} refused the deploy key {cred.name} ({cred.fingerprint}); add its public "
                    f"key to {slug} as a deploy key (with write access to push): `agentos git key list` prints it")
        return (f"{target.remote.host} refused the token {cred.name}; it may have expired or not cover {slug} "
                "(the owner replaces it with `agentos git key add --token ...`)")
    if "repository not found" in low or "does not appear to be a git repository" in low or "not found" in low:
        return f"{slug} was not found on {target.remote.host}, or the credential cannot see it"
    if "stale info" in low or "fetch first" in low or "non-fast-forward" in low:
        return "the remote branch moved since you fetched; fetch, rebase or merge, and push again"
    if "does not support --atomic" in low:
        return "the remote cannot take several refs in one push; push one branch at a time"
    if "protected branch" in low or "pre-receive hook declined" in low:
        return f"{target.remote.host} itself refused the push (a branch protection or a hook of the repository)"
    if "timed out" in low or "could not resolve host" in low or "connection refused" in low:
        return f"the node could not reach {target.remote.host}; try again later"
    last = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lower().startswith("hint:")]
    return f"{target.remote.host} said: {last[-1][:200]}" if last else f"git could not reach {slug}"


# ------------------------------------------------------------------ the mirror


class Mirror:
    """A bare mirror of one declared repository, under AGENTOS_HOME/git/mirrors. Lanes fetch from
    it and push into it; only agentosd talks to the real remote."""

    def __init__(self, path: Path, *, hooks_dir: Path, allow_file: bool = False):
        self.path = Path(path)
        self.hooks_dir = Path(hooks_dir)
        self.allow_file = allow_file
        self.lock = asyncio.Lock()
        self.default_branch: str | None = None

    def env(self) -> dict[str, str]:
        return base_env(self.path)

    async def git(self, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None,
                  timeout: float = GIT_TIMEOUT) -> tuple[int, bytes, bytes]:
        return await run_git(list(args), cwd=self.path, env=env or self.env(), stdin=stdin, timeout=timeout)

    async def ensure(self) -> None:
        if (self.path / "HEAD").is_file():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.hooks_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        rc, _, err = await run_git(["init", "--quiet", "--bare", str(tmp)], cwd=None, env=base_env(self.path.parent))
        if rc != 0:
            raise UpstreamError(f"could not make the node's mirror: {err.decode(errors='replace')[:200]}")
        for k, v in (("gc.auto", "0"), ("receive.denyNonFastForwards", "false"), ("receive.denyDeletes", "false"),
                     ("receive.fsckObjects", "true"), ("core.hooksPath", str(self.hooks_dir)),
                     ("receive.advertisePushOptions", "false"), ("uploadpack.allowFilter", "true"),
                     ("core.logAllRefUpdates", "false")):
            await run_git(["config", k, v], cwd=tmp, env=base_env(tmp))
        os.replace(tmp, self.path)

    async def fetch(self, target: Target, auth: Auth) -> str | None:
        """Bring the mirror to the remote's branches and tags; returns the remote's default branch."""
        proto = protocol_args(self.allow_file)
        rc, _, err = await self.git(*proto, "fetch", "--quiet", "--prune", "--no-tags", "--", target.url,
                                    "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*", env=auth.env)
        if rc != 0:
            raise UpstreamError(explain(err, target, scrub=auth.scrub))
        rc, out, _ = await self.git(*proto, "ls-remote", "--symref", "--", target.url, "HEAD", env=auth.env)
        default = None
        for line in out.decode(errors="replace").splitlines():
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                default = line[len("ref: refs/heads/"):-len("\tHEAD")]
        if default:
            await self.git("symbolic-ref", "HEAD", f"refs/heads/{default}")
        self.default_branch = default
        return default

    async def refs(self) -> dict[str, str]:
        _rc, out, _ = await self.git("for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags")
        found = {}
        for line in out.decode(errors="replace").splitlines():
            sha, _, ref = line.partition(" ")
            found[ref] = sha
        return found

    async def is_ancestor(self, old: str, new: str) -> bool:
        rc, _, _ = await self.git("merge-base", "--is-ancestor", old, new)
        return rc == 0

    async def commits(self, new: str, exclude: list[str], limit: int = 20) -> tuple[int, list[str]]:
        """How many commits `new` brings that none of `exclude` has, and the first `limit` of them."""
        nots = [f"^{s}" for s in exclude]
        rc, out, _ = await self.git("rev-list", "--count", new, *nots)
        count = int(out.strip() or 0) if rc == 0 else 0
        rc, out, _ = await self.git("log", f"-n{limit}", "--format=%h %s", new, *nots)
        return count, [ln[:120] for ln in out.decode(errors="replace").splitlines()]

    async def restore(self, before: dict[str, str], commands: list[Command]) -> None:
        """Put the mirror's refs of `commands` back to `before` (a refused or failed push)."""
        lines = []
        for c in commands:
            old = before.get(c.ref)
            lines.append(f"update {c.ref} {old}\n" if old else f"delete {c.ref}\n")
        await self.git("update-ref", "--stdin", stdin="".join(lines).encode())

    async def push(self, target: Target, auth: Auth, commands: list[Command], force: set[str]) -> None:
        """Push `commands` from the mirror to the real remote, each guarded by a lease on the value
        the lane saw, so a branch that moved upstream meanwhile is never overwritten."""
        args = [*protocol_args(self.allow_file), "push", "--porcelain", "--quiet"]
        if len(commands) > 1:
            args.append("--atomic")
        for c in commands:
            args.append(f"--force-with-lease={c.ref}:{'' if c.kind == 'create' else c.old}")
        args += ["--", target.url]
        for c in commands:
            if c.kind == "delete":
                args.append(f":{c.ref}")
            else:
                args.append(f"{'+' if c.ref in force else ''}{c.new}:{c.ref}")
        rc, out, err = await self.git(*args, env=auth.env)
        if rc != 0:
            raise UpstreamError(explain(err + b"\n" + out, target, scrub=auth.scrub))
