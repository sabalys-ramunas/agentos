"""Parsing what a lane sends the git gateway (openajar.git.wire) and the declared remotes
(openajar.git.remotes): refuse what cannot be read whole, never guess."""

from __future__ import annotations

import pytest

from openajar.core.errors import InvalidRequest, NotFound
from openajar.git.remotes import Remotes
from openajar.git.service import gitconfig_text, rewrite_pairs
from openajar.git.wire import (
    Remote,
    build_report,
    check_ref,
    demux,
    parse_gateway_path,
    parse_push,
    parse_remote,
    parse_report,
    pkt,
)

A, B, Z = "a" * 40, "b" * 40, "0" * 40


@pytest.mark.parametrize("url", [
    "https://github.com/Owner/App.git", "https://github.com/owner/app", "git@github.com:owner/app.git",
    "ssh://git@github.com/owner/app.git", "ssh://git@GitHub.com:22/owner/app", "github.com/owner/app",
    "https://github.com/owner/app/",
])
def test_remote_forms(url):
    assert parse_remote(url) == Remote("github.com", "owner/app")


def test_nested_groups_and_limits():
    assert parse_remote("https://gitlab.com/g/sub/proj.git").repo == "g/sub/proj"
    for bad in ("https://github.com/owner", "https://github.com/a/b/c/d/e", "https://github.com/owner/../x",
                "https://github.com/.hidden/x", "https://github.com/-x/y", "ftp://github.com/a/b",
                "file:///etc/passwd", "https://github.com/a/b?x=1", "https://bad_host/a/b", ""):
        with pytest.raises(InvalidRequest):
            parse_remote(bad)


def test_a_url_with_a_credential_is_refused_without_echoing_it():
    with pytest.raises(InvalidRequest) as e:
        parse_remote("https://user:ghp_secretsecret@github.com/o/r.git")
    assert "ghp_secret" not in e.value.message and "openajar git key add" in e.value.message
    with pytest.raises(InvalidRequest):
        parse_remote("https://ghp_secretsecret@github.com/o/r.git")


def test_gateway_paths():
    assert parse_gateway_path("/github.com/owner/app.git/info/refs?service=git-upload-pack") == (
        Remote("github.com", "owner/app"), "info/refs")
    assert parse_gateway_path("/github.com/owner/app/git-receive-pack")[1] == "git-receive-pack"
    for bad in ("/github.com/owner/app.git/HEAD", "/github.com/../x/info/refs", "/info/refs", "/x/git-upload-pack"):
        with pytest.raises(InvalidRequest):
            parse_gateway_path(bad)


def test_refs_a_push_may_update():
    assert check_ref("refs/heads/feature/x") and check_ref("refs/tags/v1.0")
    for bad in ("refs/notes/x", "refs/pull/1/head", "HEAD", "refs/heads/a..b", "refs/heads/x.lock",
                "refs/heads/a b", "refs/heads/.x", "refs/heads/x/", "refs/heads/a@{1}", "refs/heads/a~1",
                "refs/heads/a:b", "refs/heads/a\\b", "refs/heads//a"):
        with pytest.raises(InvalidRequest):
            check_ref(bad)


def test_parse_push_commands_and_capabilities():
    body = (pkt(f"{A} {B} refs/heads/main\0report-status side-band-64k agent=git/2\n")
            + pkt(f"{Z} {B} refs/heads/new\n") + b"0000PACK...")
    push = parse_push(body)
    assert [c.kind for c in push.commands] == ["update", "create"]
    assert push.sideband and "report-status" in push.capabilities
    assert push.commands[0].branch == "main"
    assert parse_push(pkt(f"{A} {Z} refs/tags/v1\n") + b"0000").commands[0].kind == "delete"
    for bad in (b"", b"0000", pkt(f"{A} {B}\n") + b"0000", pkt(f"xyz {B} refs/heads/a\n") + b"0000",
                pkt(f"{A} {B} refs/heads/a\n") + pkt(f"{A} {B} refs/heads/a\n") + b"0000",
                pkt("push-cert\0report-status\n") + b"0000", b"zzzz", b"0010short"):
        with pytest.raises(InvalidRequest):
            parse_push(bad)


@pytest.mark.parametrize("sideband", [True, False])
def test_report_round_trip(sideband):
    out = build_report({"refs/heads/a": None, "refs/heads/b": "waits for a person"}, sideband=sideband,
                       messages=["openajar: hello"])
    report, messages = demux(out, sideband)
    assert parse_report(report) == ("ok", {"refs/heads/a": None, "refs/heads/b": "waits for a person"})
    assert messages == (["openajar: hello\n"] if sideband else [])


def test_lane_git_config_points_only_declared_hosts_at_the_gateway(tmp_path):
    pairs = rewrite_pairs("http://127.0.0.1:3129", ["github.com"], "agtg_x")
    assert ("url.http://127.0.0.1:3129/github.com/.insteadOf", "git@github.com:") in pairs
    assert pairs[-1] == ("http.http://127.0.0.1:3129/.extraHeader", "x-agentos-git: agtg_x")
    import subprocess
    f = tmp_path / "gitconfig"
    f.write_text(gitconfig_text(pairs))
    listed = subprocess.run(["git", "config", "-f", str(f), "--list"], capture_output=True, text=True,
                            check=True).stdout.splitlines()
    assert "url.http://127.0.0.1:3129/github.com/.insteadof=https://github.com/" in listed
    assert "http.http://127.0.0.1:3129/.extraheader=x-agentos-git: agtg_x" in listed
    assert "credential.helper=" in listed


# ------------------------------------------------------------------ declarations


def test_allow_find_deny(tmp_path):
    r = Remotes(tmp_path / "git" / "remotes.json")
    r.allow("dev", "github.com/owner/*", push=False)
    r.allow("dev", "https://github.com/owner/app.git")
    r.allow("dev/code", "git@github.com:owner/app.git", protect=["release/*"], allow_default_branch=True)
    app = Remote("github.com", "owner/app")
    assert r.find(app, ["dev/code", "dev"]).scope == "dev/code"
    assert r.find(app, ["dev/other", "dev"]).repo == "owner/app"  # the exact repository before owner/*
    assert r.find(Remote("github.com", "owner/site"), ["dev"]).push is False
    assert r.find(Remote("github.com", "other/app"), ["dev"]) is None
    assert r.find(app, ["prod"]) is None
    assert r.hosts_for(["dev/x", "dev"]) == ["github.com"] and r.hosts_for(["prod"]) == []
    lane = r.find(app, ["dev/code"])
    assert lane.protected("release/1", "main") and not lane.protected("main", "main")
    assert r.find(app, ["dev"]).protected("main", "main")
    assert [x.scope for x in r.list("dev/code")] == ["dev", "dev", "dev/code"]
    assert len(r.list("dev")) == 3 and r.list("prod") == []
    assert oct(r.path.stat().st_mode & 0o777) == "0o600"
    r.deny("dev", "github.com/owner/app")
    assert r.find(app, ["dev"]).repo == "owner/*"
    with pytest.raises(NotFound):
        r.deny("dev", "github.com/owner/app")


def test_declarations_refuse_bad_input(tmp_path):
    r = Remotes(tmp_path / "remotes.json")
    for scope in ("", "Dev", "a/b/c", "*"):
        with pytest.raises(InvalidRequest):
            r.allow(scope, "github.com/o/r")
    with pytest.raises(InvalidRequest, match="host"):
        r.allow("dev", "github.com/o/r", url="ssh://git@gitlab.com/o/r.git")
    with pytest.raises(InvalidRequest):
        r.allow("dev", "github.com/o/r", protect=["bad glob!"])
