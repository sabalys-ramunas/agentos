"""openajar.core.fs: the one writer for private files."""

from __future__ import annotations

import os
import stat
import time

import pytest

from openajar.core import fs


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_writes_600_and_creates_parent(tmp_path):
    path = tmp_path / "a" / "b" / "pin.json"
    old = os.umask(0)
    try:
        fs.write_private(path, b"x")
    finally:
        os.umask(old)
    assert path.read_bytes() == b"x"
    assert _mode(path) == 0o600


def test_text_and_custom_mode(tmp_path):
    path = tmp_path / "f"
    fs.write_private(path, "héllo", mode=0o640)
    assert path.read_text() == "héllo"
    assert _mode(path) == 0o640


def test_replaces_atomically_and_leaves_no_temp(tmp_path):
    path = tmp_path / "f"
    fs.write_private(path, b"one")
    fs.write_private(path, b"two")
    assert path.read_bytes() == b"two"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]


def test_exclusive_never_overwrites(tmp_path):
    path = tmp_path / "f"
    fs.write_private(path, b"first", exclusive=True)
    with pytest.raises(FileExistsError):
        fs.write_private(path, b"second", exclusive=True)
    assert path.read_bytes() == b"first"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]


def test_symlink_at_path_is_replaced_not_followed(tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    link = tmp_path / "f"
    link.symlink_to(victim)
    fs.write_private(link, b"new")
    assert victim.read_bytes() == b"keep"
    assert not link.is_symlink() and link.read_bytes() == b"new"


def test_fsyncs_file_and_directory(tmp_path, monkeypatch):
    synced: list[str] = []
    real = os.fsync

    def spy(fd):
        synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    fs.write_private(tmp_path / "f", b"x")
    assert synced == ["file", "dir"]


def test_failed_write_leaves_old_file_and_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "f"
    fs.write_private(path, b"old")

    def boom(fd):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        fs.write_private(path, b"new")
    assert path.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]


def test_json(tmp_path):
    path = tmp_path / "f.json"
    fs.write_private_json(path, {"a": 1}, newline=True)
    assert path.read_text() == '{\n "a": 1\n}\n'


def test_a_write_removes_what_a_dead_writer_left_and_keeps_a_live_ones(tmp_path):
    import subprocess
    import sys

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    path = tmp_path / "pin.json"
    stale = tmp_path / f".pin.json.{dead.pid}.0123abcd.tmp"
    live = tmp_path / f".pin.json.{os.getppid()}.0123abcd.tmp"   # a writer still running
    old_style = tmp_path / ".pin.json.tmp"                         # what older versions named it
    other = tmp_path / f".pin.json.bak.{dead.pid}.0123abcd.tmp"    # another file's temp
    young = tmp_path / f".pin.json.{dead.pid}.0123abcf.tmp"         # a dead pid, but just written
    huge = tmp_path / ".pin.json.99999999999.0123abcd.tmp"            # not a pid os.kill takes
    hour_ago = time.time() - 2 * fs.STALE_TEMP_S
    for p in (stale, live, old_style, other, young, huge):
        p.write_bytes(b"secret")
        if p is not young:
            os.utime(p, (hour_ago, hour_ago))
    fs.write_private(path, b"x")
    assert young.exists() and path.read_bytes() == b"x"
    assert not stale.exists() and not old_style.exists()
    assert live.exists() and other.exists()
    assert fs.is_temp(stale) and fs.is_temp(live) and not fs.is_temp(path)


# ---- the *_beneath calls: never follow a link below the anchor ------------------------------------

def _beneath_host(tmp_path):
    root, host = tmp_path / "root", tmp_path / "host"
    root.mkdir()
    host.mkdir()
    (host / "secret").write_text("s")
    return root, host


def test_write_beneath_replaces_a_link_and_refuses_a_linked_directory(tmp_path):
    from openajar.core.fs import LinkRefused, write_beneath

    root, host = _beneath_host(tmp_path)
    os.symlink(host / "secret", root / "f")
    write_beneath(root, "f", "mine")
    assert (root / "f").read_text() == "mine" and not (root / "f").is_symlink()
    assert (host / "secret").read_text() == "s"
    os.symlink(host, root / "d")
    with pytest.raises(LinkRefused):
        write_beneath(root, "d/new", "x")
    with pytest.raises(LinkRefused):
        write_beneath(root, "../host/x", "x")
    os.symlink(host / "secret", root / "g")
    with pytest.raises(LinkRefused):
        write_beneath(root, "g", "x", refuse_link=True)
    assert sorted(os.listdir(host)) == ["secret"]


def test_write_beneath_never_writes_into_a_hard_link(tmp_path):
    from openajar.core.fs import write_beneath

    root, host = _beneath_host(tmp_path)
    os.link(host / "secret", root / "h")
    write_beneath(root, "h", "mine")
    assert (host / "secret").read_text() == "s" and (root / "h").read_text() == "mine"


def test_read_beneath_takes_only_a_plain_file(tmp_path):
    from openajar.core.fs import LinkRefused, read_beneath

    root, host = _beneath_host(tmp_path)
    (root / "ok").write_text("fine")
    assert read_beneath(root, "ok") == b"fine" and read_beneath(root, "missing") is None
    os.symlink(host / "secret", root / "l")
    os.link(host / "secret", root / "h")
    os.mkfifo(root / "p")
    (root / "big").write_bytes(b"x" * 11)
    for name in ("l", "h", "p"):
        with pytest.raises(LinkRefused):
            read_beneath(root, name)
    with pytest.raises(LinkRefused):
        read_beneath(root, "big", limit=10)


def test_unlink_beneath_removes_the_link_not_its_target(tmp_path):
    from openajar.core.fs import LinkRefused, unlink_beneath

    root, host = _beneath_host(tmp_path)
    os.symlink(host / "secret", root / "l")
    unlink_beneath(root, "l")
    assert not os.path.lexists(root / "l") and (host / "secret").exists()
    os.symlink(host, root / "d")
    with pytest.raises(LinkRefused):
        unlink_beneath(root, "d/secret")
    assert (host / "secret").exists()


def test_copy_tree_beneath_keeps_links_as_links(tmp_path):
    from openajar.core.fs import LinkRefused, copy_tree_beneath

    root, host = _beneath_host(tmp_path)
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "a" / "f").write_text("data")
    os.chmod(src / "a" / "f", 0o4755)
    os.symlink(host, src / "out")
    copy_tree_beneath(src, root, "w")
    assert (root / "w" / "a" / "f").read_text() == "data"
    assert (root / "w" / "a" / "f").stat().st_mode & 0o7777 == 0o755
    assert os.readlink(root / "w" / "out") == str(host)
    assert sorted(os.listdir(host)) == ["secret"]
    os.symlink(host, root / "w2")
    with pytest.raises(LinkRefused):
        copy_tree_beneath(src, root, "w2")
    assert sorted(os.listdir(host)) == ["secret"]
