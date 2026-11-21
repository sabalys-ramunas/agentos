"""A link in a task's files never takes agentosd outside them.

The repo a bundle clones, the archive it unpacks and everything the brain leaves in its work and
home dirs are untrusted, and they can hold symlinks (absolute, relative with `..`, to a file or a
directory) or hard links. agentosd writes, chmods, chowns, reads and deletes files there as its
own user (root when it drops to the brain user), so a write that followed one of them would land
on the host. Each case below plants a link to a "host" file outside the task and checks the host
file is untouched after the daemon's step.
"""

from __future__ import annotations

import base64
import io
import json
import os
import pwd
import subprocess
import tarfile
from pathlib import Path

import pytest

from openajar.brains import bundle as bundle_mod
from openajar.brains.models import BrainKind, ContextBundle, GitRef, InlineFile, Runner
from openajar.core.errors import InvalidRequest

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@x"}
SECRET = "host secret, never to be touched\n"


@pytest.fixture
def host(tmp_path):
    """A stand-in for the host: a secret file and a directory outside every task dir."""
    h = tmp_path / "host"
    (h / "dir").mkdir(parents=True)
    (h / "secret").write_text(SECRET)
    os.chmod(h / "secret", 0o600)
    os.chmod(h / "dir", 0o755)
    return h


def untouched(host: Path) -> None:
    assert (host / "secret").read_text() == SECRET
    assert (host / "secret").stat().st_mode & 0o777 == 0o600
    assert os.listdir(host / "dir") == []
    assert (host / "dir").stat().st_mode & 0o777 == 0o755


def make_repo(root: Path, links: dict[str, str], files: dict[str, str] | None = None) -> Path:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "wip"], cwd=repo, check=True, env=ENV)
    for name, text in {"README.md": "hi\n", **(files or {})}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    for name, target in links.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, repo / name)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=ENV)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=repo, check=True, env=ENV)
    return repo


def git_bundle(repo: Path, **kw) -> ContextBundle:
    return ContextBundle(git=GitRef(remote=str(repo), ref="wip"), **kw)


def tar_b64(entries: list[tarfile.TarInfo | tuple[str, bytes]]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for e in entries:
            if isinstance(e, tuple):
                info = tarfile.TarInfo(e[0])
                info.size = len(e[1])
                tar.addfile(info, io.BytesIO(e[1]))
            else:
                tar.addfile(e)
    return base64.b64encode(buf.getvalue()).decode()


def link_info(name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info


# ---- the bundle: clone, handoff file, inline files, archive ---------------------------------------

def handoff_targets(host: Path) -> list[str]:
    # absolute, relative with `..` (work is tmp/task/work, the host is tmp/host)
    return [str(host / "secret"), "../../host/secret"]


@pytest.mark.parametrize("which", [0, 1], ids=["absolute", "relative"])
def test_a_repo_link_named_like_the_handoff_file_is_replaced_not_written_through(tmp_path, host, which):
    target = handoff_targets(host)[which]
    repo = make_repo(tmp_path, {bundle_mod.HANDOFF_FILE: target})
    work = tmp_path / "task" / "work"
    text = bundle_mod.materialize(git_bundle(repo, summary="carry on"), work, allow_file_git=True)
    untouched(host)
    handoff = work / bundle_mod.HANDOFF_FILE
    assert not handoff.is_symlink() and handoff.read_text() == text + "\n"


def test_an_inline_file_under_a_linked_repo_directory_is_refused(tmp_path, host):
    repo = make_repo(tmp_path, {"sub": str(host / "dir")})
    work = tmp_path / "task" / "work"
    with pytest.raises(InvalidRequest, match="link"):
        bundle_mod.materialize(git_bundle(repo, files=[InlineFile(path="sub/authorized_keys", content="key")]),
                               work, allow_file_git=True)
    untouched(host)


def test_an_inline_file_over_a_linked_repo_file_is_refused(tmp_path, host):
    repo = make_repo(tmp_path, {"notes.md": "../../host/secret"})
    work = tmp_path / "task" / "work"
    with pytest.raises(InvalidRequest, match="link"):
        bundle_mod.materialize(git_bundle(repo, files=[InlineFile(path="notes.md", content="pwned")]),
                               work, allow_file_git=True)
    untouched(host)


def test_an_inline_file_under_a_nested_relative_link_is_refused(tmp_path, host):
    repo = make_repo(tmp_path, {"a/b": "../../../host"}, files={"a/keep": "x"})
    work = tmp_path / "task" / "work"
    with pytest.raises(InvalidRequest, match="link"):
        bundle_mod.materialize(git_bundle(repo, files=[InlineFile(path="a/b/dir/x", content="pwned")]),
                               work, allow_file_git=True)
    untouched(host)


def test_links_inside_the_repo_stay_links(tmp_path, host):
    """Repos have links of their own; they are kept as links (never followed by agentosd)."""
    repo = make_repo(tmp_path, {"docs": "README.md", "out": str(host / "dir")})
    work = tmp_path / "task" / "work"
    bundle_mod.materialize(git_bundle(repo, summary="s"), work, allow_file_git=True)
    assert os.readlink(work / "docs") == "README.md" and os.readlink(work / "out") == str(host / "dir")
    untouched(host)


def test_an_archive_cannot_write_through_a_repo_link(tmp_path, host):
    repo = make_repo(tmp_path, {"sub": str(host / "dir")})
    work = tmp_path / "task" / "work"
    with pytest.raises(InvalidRequest):
        bundle_mod.materialize(git_bundle(repo, archive_b64=tar_b64([("sub/x", b"pwned")])), work,
                               allow_file_git=True)
    untouched(host)


@pytest.mark.parametrize("entries", [
    [link_info("h", "/etc/passwd", tarfile.LNKTYPE)],
    [link_info("h", "../../host/secret", tarfile.LNKTYPE)],
    [("a/../../../host/dir/x", b"pwned")],
    [link_info("l", "../../host/dir"), ("l/x", b"pwned")],
    [link_info("d", "."), link_info("d/e", ".."), link_info("d/e/f", ".."), ("d/e/f/host/dir/x", b"pwned")],
], ids=["hardlink-abs", "hardlink-rel", "dotdot-name", "link-then-file", "chained-links"])
def test_archive_links_and_dotdot_names_are_refused(tmp_path, host, entries):
    work = tmp_path / "task" / "work"
    with pytest.raises(InvalidRequest):
        bundle_mod.materialize(ContextBundle(archive_b64=tar_b64(entries)), work)
    untouched(host)


# ---- the service: brain dirs, files written into the home, read-backs, cleanup -------------------

def brain_user(home: Path) -> pwd.struct_passwd:
    """The brain user, as agentosd sees it: here our own uid (chown to it needs no root)."""
    me = pwd.getpwuid(os.getuid())
    return pwd.struct_passwd((me.pw_name, "x", me.pw_uid, me.pw_gid, "", str(home), "/bin/sh"))


@pytest.fixture
def svc(tmp_path, monkeypatch):
    from testkit.brains import FakeVault, make_service

    from openajar.core.runtime import Runtime

    s = make_service(tmp_path / "node", runtime=Runtime(vault=FakeVault()))
    bhome = tmp_path / "brainhome"
    bhome.mkdir()
    monkeypatch.setattr(s, "_run_as", lambda: brain_user(bhome))
    monkeypatch.setattr(s, "_via_helper", lambda: False)
    s.brain_base = bhome / "tasks"  # type: ignore[attr-defined]  (for the tests' own use)
    s.task_dir("t1").mkdir(parents=True)
    return s


def test_a_home_swapped_for_a_link_is_never_chmodded_or_chowned(svc, host):
    svc._brain_dirs("t1")  # the first attempt makes the dirs
    base = svc.brain_base / "t1"
    (base / "home" / "tmp").rmdir()
    (base / "home").rmdir()
    os.symlink(host / "dir", base / "home")  # the brain, between attempts
    with pytest.raises(OSError):
        svc._brain_dirs("t1")  # the next attempt
    untouched(host)


def test_files_for_the_brain_are_never_written_through_a_link(svc, host):
    _, home = svc._brain_dirs("t1")
    os.symlink(host / "dir", home / ".codex")
    with pytest.raises(OSError):
        svc._write_brain_file(home / ".codex" / "auth.json", "credential", 0o600)
    os.symlink(host / "secret", home / "openajar-mcp.json")
    svc._write_brain_file(home / "openajar-mcp.json", "{}", 0o600)  # the link is replaced, not followed
    assert not (home / "openajar-mcp.json").is_symlink()
    untouched(host)


async def codex(svc):
    from testkit.brains import add

    auth = json.dumps({"tokens": {"access_token": "at-" + "A" * 30, "refresh_token": "rt-a"}})
    await add(svc, "codex-plus", "subscription", "openai", credential=auth)
    spec = svc.get_spec("codex-plus")
    assert spec.runner == Runner.CODEX and spec.kind == BrainKind.SUBSCRIPTION
    svc.store.set_kv("attempt_signin:t1", svc._signin("codex-plus"))
    return spec, auth


@pytest.mark.parametrize("plant", ["file", "dir", "hardlink"])
async def test_a_linked_codex_login_is_never_read_back_into_the_vault(svc, host, plant):
    from openajar.brains.credentials import brain_ref

    spec, before = await codex(svc)
    _, home = svc._brain_dirs("t1")
    stolen = json.dumps({"tokens": {"access_token": "host-" + "S" * 30, "refresh_token": "host"}})
    (host / "secret").write_text(stolen)
    if plant == "dir":
        (host / "dir" / "auth.json").write_text(stolen)
        os.symlink(host / "dir", home / ".codex")
    else:
        (home / ".codex").mkdir()
        if plant == "file":
            os.symlink(host / "secret", home / ".codex" / "auth.json")
        else:
            os.link(host / "secret", home / ".codex" / "auth.json")
    rec = type("Rec", (), {"id": "t1"})()
    await svc._after_attempt_files(rec, spec)
    assert await svc.creds.get(brain_ref("codex-plus")) == before
    assert (host / "secret").read_text() == stolen
    if plant == "dir":
        assert (host / "dir" / "auth.json").exists()


async def test_cleanup_never_deletes_through_a_swapped_home(svc, host):
    svc._brain_dirs("t1")
    base = svc.brain_base / "t1"
    (base / "home" / "tmp").rmdir()
    (base / "home").rmdir()
    os.symlink(host / "dir", base / "home")
    (host / "dir" / "openajar-mcp.json").write_text("{}")
    rec = type("Rec", (), {"id": "t1", "lanes": [], "child_grant_id": None})()
    await svc._cleanup_task(rec)
    assert (host / "dir" / "openajar-mcp.json").exists()


def test_the_bundle_lands_in_the_brain_home_with_its_links_kept_as_links(svc, tmp_path, host):
    repo = make_repo(tmp_path, {"docs": "README.md", "out": str(host / "dir"), bundle_mod.HANDOFF_FILE: "../x"},
                     files={"src/a.py": "x = 1\n"})
    work, _ = svc._brain_dirs("t1")
    b = git_bundle(repo, summary="go on", files=[InlineFile(path="src/b.py", content="y = 2\n")])
    svc.allow_file_git = True
    text = svc._materialize("t1", b, work, redact=False)
    assert (work / "src" / "a.py").read_text() == "x = 1\n" and (work / "src" / "b.py").read_text() == "y = 2\n"
    assert os.readlink(work / "docs") == "README.md" and os.readlink(work / "out") == str(host / "dir")
    assert (work / bundle_mod.HANDOFF_FILE).read_text() == text + "\n"
    assert not (svc.task_dir("t1") / "stage").exists()
    untouched(host)


def test_a_work_dir_swapped_for_a_link_before_the_copy_is_refused(svc, tmp_path, host):
    repo = make_repo(tmp_path, {}, files={"payload": "pwned"})
    work, _ = svc._brain_dirs("t1")
    work.rmdir()
    os.symlink(host / "dir", work)  # another task's brain, racing this one
    svc.allow_file_git = True
    with pytest.raises(OSError):
        svc._materialize("t1", git_bundle(repo, summary="s"), work, redact=False)
    untouched(host)
