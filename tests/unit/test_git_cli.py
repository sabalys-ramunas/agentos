"""`openajar git ...`: a deploy key is made on the node and only its public half is printed; a token
is read from stdin or a prompt, never argv; nothing prints or exports a secret."""

from __future__ import annotations

import json

import pytest
from test_vault_support import make_vault, private_home
from typer.testing import CliRunner

from openajar.cli import app

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = private_home(tmp_path)
    make_vault(h)
    monkeypatch.setenv("OPENAJAR_HOME", str(h))
    return h


def vault_bytes(home) -> bytes:
    return b"".join(p.read_bytes() for p in home.glob("vault.db*"))


def test_key_add_prints_the_public_key_and_seals_the_private_one(home):
    r = runner.invoke(app, ["git", "key", "add", "--host", "github.com", "--repo", "owner/app"])
    assert r.exit_code == 0, r.output
    assert "ssh-ed25519 AAAA" in r.output and "https://github.com/owner/app/settings/keys/new" in r.output
    assert "PRIVATE KEY" not in r.output and "BEGIN" not in r.output
    assert "openajar git remote allow" in r.output
    listed = json.loads(runner.invoke(app, ["git", "key", "list", "--json"]).output)
    [cred] = listed
    assert cred["name"] == "github-owner-app-ssh" and cred["host"] == "github.com" and cred["repos"] == ["owner/app"]
    assert cred["public_key"] in r.output
    assert b"OPENSSH PRIVATE KEY" not in vault_bytes(home)
    assert not list(home.rglob("*.pem")) and not list(home.rglob("id_*"))  # no key file was ever written
    show = runner.invoke(app, ["git", "key", "show", "github-owner-app-ssh"])
    assert show.output.strip() == cred["public_key"]
    again = runner.invoke(app, ["git", "key", "add", "--host", "github.com", "--repo", "owner/app"])
    assert again.exit_code == 1 and "--replace" in again.output


def test_key_add_token_from_stdin_never_echoes_it(home):
    token = "github_pat_" + "T" * 60
    r = runner.invoke(app, ["git", "key", "add", "--token", "--stdin", "--host", "github.com", "--repo", "owner/*",
                            "--name", "org-pat"], input=token + "\n")
    assert r.exit_code == 0, r.output
    assert token not in r.output and "T" * 20 not in r.output
    assert token.encode() not in vault_bytes(home)
    out = runner.invoke(app, ["git", "key", "list"]).output
    assert "org-pat: token for github.com (owner/*), never used" in out and token not in out
    assert runner.invoke(app, ["git", "key", "show", "org-pat"]).exit_code == 1  # a token has no public key
    rm = runner.invoke(app, ["git", "key", "remove", "org-pat"])
    assert rm.exit_code == 0 and "Remove it at github.com too" in rm.output
    assert "No git credentials" in runner.invoke(app, ["git", "key", "list"]).output


def test_another_host_records_the_given_host_key_with_its_fingerprint(home):
    line = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
    r = runner.invoke(app, ["git", "key", "add", "--host", "git.example.org", "--port", "2222", "--repo", "a/b",
                            "--host-key", line])
    assert r.exit_code == 0, r.output
    assert "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" in r.output
    assert (home / "git" / "known_hosts").read_text().strip() == f"[git.example.org]:2222 {line}"


def test_remote_allow_list_deny(home):
    r = runner.invoke(app, ["git", "remote", "allow", "dev", "git@github.com:owner/app.git", "--protect", "release/*"])
    assert r.exit_code == 0, r.output
    assert "github.com/owner/app for dev: fetch and push, default branch asks, also protected: release/*" in r.output
    assert "Declare it on a lane" in r.output
    assert runner.invoke(app, ["git", "remote", "allow", "dev/code", "github.com/owner/site", "--read-only"]).exit_code == 0
    listed = json.loads(runner.invoke(app, ["git", "remote", "list", "--json"]).output)
    assert {(x["scope"], x["repo"], x["push"]) for x in listed} == {("dev", "owner/app", True),
                                                                    ("dev/code", "owner/site", False)}
    assert "owner/site" not in runner.invoke(app, ["git", "remote", "list", "dev/other"]).output
    assert runner.invoke(app, ["git", "remote", "deny", "dev", "github.com/owner/app"]).exit_code == 0
    gone = runner.invoke(app, ["git", "remote", "deny", "dev", "github.com/owner/app"])
    assert gone.exit_code == 1 and "not declared" in gone.output
    bad = runner.invoke(app, ["git", "remote", "allow", "dev", "https://u:secret-value@github.com/o/r"])
    assert bad.exit_code == 1 and "secret-value" not in bad.output


def test_the_vault_git_group_is_the_same(home):
    r = runner.invoke(app, ["vault", "git", "remote", "list"])
    assert r.exit_code == 0 and "No remotes declared" in r.output
