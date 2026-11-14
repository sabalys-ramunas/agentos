"""Git credentials (agentos.git.keys, agentos.git.agent): a deploy key is made in memory and kept only
in the vault, sealed; its public half is all that is ever shown; the node's SSH agent signs an SSH
login with it and nothing else; the vault scrub hides it, whole or a line of it."""

from __future__ import annotations

import asyncio
import os
import shutil
import struct
import subprocess

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from test_vault_support import make_vault, private_home

from agentos.core.errors import InvalidRequest
from agentos.git import keys
from agentos.git.agent import FAILURE, MAX_SIGNATURES, SIGN_REQUEST, SIGN_RESPONSE, SingleKeyAgent, login_user
from agentos.git.wire import Remote


@pytest.fixture
async def vault(tmp_path):
    v = make_vault(private_home(tmp_path))
    await v.start(control=False)
    yield v
    await v.stop()


def _s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def userauth(blob: bytes, user: bytes = b"git", service: bytes = b"ssh-connection", hostkey: bytes | None = None) -> bytes:
    method = b"publickey" if hostkey is None else b"publickey-hostbound-v00@openssh.com"
    return (_s(b"session-id-32-bytes-of-something") + bytes([50]) + _s(user) + _s(service) + _s(method)
            + b"\x01" + _s(b"ssh-ed25519") + _s(blob) + (_s(hostkey) if hostkey is not None else b""))


def test_a_new_key_is_openssh_ed25519_and_its_fingerprint_is_ssh_keygens(tmp_path):
    private, public, fp = keys.generate_ed25519("agentos-test")
    assert private.reveal().startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert public.startswith("ssh-ed25519 AAAA") and public.endswith(" agentos-test")
    assert "BEGIN" not in repr(private) and private.reveal() not in str(private)
    if shutil.which("ssh-keygen"):
        pub = tmp_path / "k.pub"
        pub.write_text(public + "\n")
        out = subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True, check=True).stdout
        assert fp in out
    assert keys.load_private(private.reveal()) is not None


def test_pinned_host_keys_are_the_published_ones():
    assert keys.fingerprint(keys.KNOWN_HOSTS["github.com"][0]) == "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
    assert keys.fingerprint(keys.KNOWN_HOSTS["gitlab.com"][0]) == "SHA256:eUXGGm1YGsMAS7vkcx6JOJdOGHPem5gQp4taiCfCLB8"
    assert keys.fingerprint(keys.KNOWN_HOSTS["bitbucket.org"][0]) == "SHA256:ybgmFkzwOSotHTHLJgHO0QN8L0xErw6vd0VhFA9m3SM"


def test_known_hosts_lines_and_ports(tmp_path):
    assert keys.known_hosts_lines(tmp_path, "github.com") == [f"github.com {keys.KNOWN_HOSTS['github.com'][0]}"]
    assert keys.known_hosts_lines(tmp_path, "git.example.org") == []
    keys.add_known_hosts(tmp_path, ["[git.example.org]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGx"])
    assert keys.known_hosts_lines(tmp_path, "git.example.org") == []  # port 22: not that line
    assert keys.known_hosts_lines(tmp_path, "git.example.org", 2222) == [
        "[git.example.org]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGx"]
    assert oct(keys.known_hosts_path(tmp_path).stat().st_mode & 0o777) == "0o600"


async def test_stored_sealed_listed_without_the_value(vault):
    private, public, fp = keys.generate_ed25519("agentos-app")
    await vault.put_secret("git:gh-app", private.reveal(), kind=keys.SSH_KIND, label="git deploy key",
                           scope=keys.ssh_scope("GitHub.com", ["Owner/App"], public))
    raw = vault.store.path.read_bytes() + b"".join(p.read_bytes() for p in vault.store.path.parent.glob("vault.db-wal"))
    body = private.reveal().splitlines()[1]
    assert body.encode() not in raw  # sealed at rest
    [meta] = await vault.list_secrets()
    assert private.reveal() not in str(meta) and body not in str(meta)
    cred = keys.from_meta(meta)
    assert cred is not None and cred.host == "github.com" and cred.repos == ("owner/app",)
    assert cred.public_key == public and cred.fingerprint == fp and cred.type == "ssh"
    assert "BEGIN" not in str(cred.public())


async def test_the_vault_scrub_hides_the_key_whole_and_by_line(vault):
    private, public, _ = keys.generate_ed25519("x")
    await vault.put_secret("git:k", private.reveal(), kind=keys.SSH_KIND, label="k",
                           scope=keys.ssh_scope("github.com", ["o/r"], public))
    await vault.put_secret("git:t", "github_pat_" + "Q" * 50, kind=keys.TOKEN_KIND, label="t",
                           scope=keys.token_scope("github.com", ["o/*"], None))
    line = private.reveal().splitlines()[2]
    text = f"oops: {private.reveal()} and a line {line} and the token github_pat_{'Q' * 50}"
    out = vault.scrub(text)
    assert line not in out and "Q" * 50 not in out and "[redacted]" in out


def test_pick_prefers_the_repository_then_a_deploy_key():
    def cred(name, kind, repos):
        return keys.Credential(ref=f"git:{name}", kind=kind, host="github.com", repos=tuple(repos), label=name)
    every = cred("all", keys.TOKEN_KIND, ["owner/*"])
    token = cred("tok", keys.TOKEN_KIND, ["owner/app"])
    key = cred("key", keys.SSH_KIND, ["owner/app"])
    app = Remote("github.com", "owner/app")
    assert keys.pick([every, token, key], app) is key
    assert keys.pick([every, token], app) is token
    assert keys.pick([every], app) is every
    assert keys.pick([every], Remote("github.com", "other/app")) is None
    assert keys.pick([key], Remote("gitlab.com", "owner/app")) is None


def test_scope_limits():
    with pytest.raises(InvalidRequest, match="at least one repository"):
        keys.check_repos([])
    with pytest.raises(InvalidRequest, match="too many"):
        keys.check_repos([f"owner/repo-number-{i}" for i in range(20)])
    with pytest.raises(InvalidRequest):
        keys.check_repos(["../etc"])
    with pytest.raises(InvalidRequest):
        keys.check_token("has space in it")
    with pytest.raises(InvalidRequest):
        keys.check_name("Bad Name")


# ------------------------------------------------------------------ the agent


async def test_the_agent_signs_an_ssh_login_for_git_and_nothing_else(tmp_path):
    private, public, _ = keys.generate_ed25519("x")
    agent = SingleKeyAgent(keys.load_private(private.reveal()), public, tmp_path / "run")
    blob = keys.key_blob(public)
    assert login_user(userauth(blob), blob) == "git"
    ok = agent.answer(bytes([SIGN_REQUEST]) + _s(blob) + _s(userauth(blob)) + b"\0\0\0\0")
    assert ok[0] == SIGN_RESPONSE
    sig_blob = ok[5:]
    (n,) = struct.unpack(">I", sig_blob[0:4])
    assert sig_blob[4:4 + n] == b"ssh-ed25519"
    sig = sig_blob[4 + n + 4:]
    Ed25519PublicKey.from_public_bytes(blob[-32:]).verify(sig, userauth(blob))
    for bad in (b"SSHSIG" + b"\0" * 40,  # a file or commit signature
                userauth(blob, user=b"root"),  # another user
                userauth(blob, service=b"other"),
                userauth(b"another-key-blob")):
        assert agent.answer(bytes([SIGN_REQUEST]) + _s(blob) + _s(bad) + b"\0\0\0\0") == bytes([FAILURE])
    assert agent.answer(bytes([SIGN_REQUEST]) + _s(b"other") + _s(userauth(blob)) + b"\0\0\0\0") == bytes([FAILURE])
    assert agent.answer(bytes([27])) == bytes([FAILURE])  # extensions, adding keys, and the rest: refused


async def test_a_host_bound_login_is_signed_only_for_the_declared_hosts_key(tmp_path):
    private, public, _ = keys.generate_ed25519("x")
    github = keys.key_blob(keys.KNOWN_HOSTS["github.com"][0])
    other = keys.key_blob(keys.KNOWN_HOSTS["gitlab.com"][0])
    agent = SingleKeyAgent(keys.load_private(private.reveal()), public, tmp_path / "run", host_keys=[github])
    blob = keys.key_blob(public)

    def ask(data):
        return agent.answer(bytes([SIGN_REQUEST]) + _s(blob) + _s(data) + b"\0\0\0\0")
    assert ask(userauth(blob, hostkey=github))[0] == SIGN_RESPONSE
    assert ask(userauth(blob, hostkey=other)) == bytes([FAILURE])  # a login to another host: refused
    assert ask(userauth(blob, user=b"root", hostkey=github)) == bytes([FAILURE])


async def test_the_agent_signs_a_few_times_at_most(tmp_path):
    private, public, _ = keys.generate_ed25519("x")
    agent = SingleKeyAgent(keys.load_private(private.reveal()), public, tmp_path / "run")
    blob = keys.key_blob(public)
    req = bytes([SIGN_REQUEST]) + _s(blob) + _s(userauth(blob)) + b"\0\0\0\0"
    assert all(agent.answer(req)[0] == SIGN_RESPONSE for _ in range(MAX_SIGNATURES))
    assert agent.answer(req) == bytes([FAILURE])


@pytest.mark.skipif(not shutil.which("ssh-add"), reason="needs ssh-add")
async def test_the_agent_socket_lists_only_its_key_and_is_gone_after(tmp_path):
    import tempfile

    private, public, fp = keys.generate_ed25519("x")
    run = __import__("pathlib").Path(tempfile.mkdtemp(prefix="ag", dir="/tmp"))
    try:
        agent = SingleKeyAgent(keys.load_private(private.reveal()), public, run)
        async with agent as sock:
            assert oct(os.stat(agent.dir).st_mode & 0o777) == "0o700"
            env = {"PATH": os.environ["PATH"], "SSH_AUTH_SOCK": str(sock)}
            out = await asyncio.to_thread(subprocess.run, ["ssh-add", "-l"], env=env, capture_output=True,
                                          text=True, timeout=10)
            assert fp in out.stdout and out.stdout.count("\n") == 1
            # nothing can be added to it, nor removed from it
            rm = await asyncio.to_thread(subprocess.run, ["ssh-add", "-D"], env=env, capture_output=True,
                                         text=True, timeout=10)
            assert rm.returncode != 0
        assert not sock.exists() and not agent.dir.exists()
    finally:
        shutil.rmtree(run, ignore_errors=True)
