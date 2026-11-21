"""Context bundles are untrusted input: nothing escapes the work dir."""

import base64
import io
import os
import tarfile

import pytest

from openajar.brains.bundle import HANDOFF_FILE, materialize, validate
from openajar.brains.models import ContextBundle, GitRef, InlineFile
from openajar.core.errors import InvalidRequest


def tar_b64(members: dict[str, bytes], *, symlink: tuple[str, str] | None = None) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(symlink[0])
            info.type = tarfile.SYMTYPE
            info.linkname = symlink[1]
            tar.addfile(info)
    return base64.b64encode(buf.getvalue()).decode()


def test_archive_and_inline_files(tmp_path):
    work = tmp_path / "work"
    b = ContextBundle(summary="state of play", archive_b64=tar_b64({"src/a.py": b"x = 1\n"}),
                      files=[InlineFile(path="notes.md", content="todo"), "src/a.py"])
    text = materialize(b, work)
    assert (work / "src" / "a.py").read_text() == "x = 1\n" and (work / "notes.md").read_text() == "todo"
    assert "state of play" in text and "notes.md, src/a.py" in text
    assert (work / HANDOFF_FILE).exists()


def test_archive_cannot_escape(tmp_path):
    with pytest.raises(InvalidRequest, match="rejected"):
        materialize(ContextBundle(archive_b64=tar_b64({"../evil.txt": b"x"})), tmp_path / "work")
    assert not (tmp_path / "evil.txt").exists()


def test_absolute_archive_paths_land_inside_the_work_dir(tmp_path):
    materialize(ContextBundle(archive_b64=tar_b64({"/tmp/openajar-evil-test": b"x"})), tmp_path / "work")
    assert (tmp_path / "work" / "tmp" / "openajar-evil-test").exists()
    assert not os.path.exists("/tmp/openajar-evil-test")


def test_archive_symlink_out_is_rejected(tmp_path):
    with pytest.raises(InvalidRequest):
        materialize(ContextBundle(archive_b64=tar_b64({}, symlink=("link", "/etc/passwd"))), tmp_path / "w")


@pytest.mark.parametrize("path", ["../x", "/abs", "~/.ssh/id"])
def test_inline_paths_must_be_relative(tmp_path, path):
    with pytest.raises(InvalidRequest):
        materialize(ContextBundle(files=[InlineFile(path=path, content="x")]), tmp_path / "w")


def test_git_remote_rules():
    validate(ContextBundle(git=GitRef(remote="https://github.com/me/repo.git", ref="wip/x")))
    validate(ContextBundle(git=GitRef(remote="git@github.com:me/repo.git")))
    for bad in ("file:///etc", "/root/repo", "ext::sh -c id", "--upload-pack=x"):
        with pytest.raises(InvalidRequest):
            validate(ContextBundle(git=GitRef(remote=bad)))
    with pytest.raises(InvalidRequest):
        validate(ContextBundle(git=GitRef(remote="https://x/y.git", ref="--force")))


def test_redaction_and_empty(tmp_path):
    key = "sk-ant-api03-" + "Q" * 40
    text = materialize(ContextBundle(summary=f"key is {key}", files=[InlineFile(path="k", content=key)]),
                       tmp_path / "w", redact=True)
    assert key not in text and key not in (tmp_path / "w" / "k").read_text()
    assert materialize(None, tmp_path / "w2") is None
    assert materialize(ContextBundle(), tmp_path / "w3") is None


def test_size_caps():
    with pytest.raises(InvalidRequest, match="summary"):
        validate(ContextBundle(summary="x" * 30_000))
    with pytest.raises(InvalidRequest, match="inline"):
        validate(ContextBundle(files=[InlineFile(path="a", content="x" * 1_100_000)]))
