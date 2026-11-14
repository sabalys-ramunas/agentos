"""QA 2026-11-07 #20: a long AGENTOS_HOME (a deep --home, a test's temp dir) keeps working sockets.
The vault and ops sockets move to a short private directory when AGENTOS_HOME/run/NAME would pass
the unix socket limit, the daemon and the CLI agree on where, and a directory that is not this
user's own is refused with words that say what to do."""

from __future__ import annotations

import os
import shutil

import pytest

from agentos.core import layout
from agentos.ops.control import start_server
from agentos.vault.service import VaultService


@pytest.fixture
def long_home(tmp_path):
    home = tmp_path / ("deep-" + "x" * 120)
    home.mkdir()
    yield home
    shutil.rmtree(layout.short_run_dir(home), ignore_errors=True)


def test_a_short_home_keeps_its_sockets_in_run(tmp_path):
    home = tmp_path.parent / "h"
    if len(os.fsencode(str(home / "run" / "vault.sock"))) > layout.SUN_PATH_MAX:
        pytest.skip("this temp dir is already too long")
    assert layout.ops_socket(home) == home / "run" / "ops.sock"
    assert layout.vault_socket(home) == home / "run" / "vault.sock"


def test_a_long_home_puts_its_sockets_in_a_short_dir(long_home):
    ops, vault = layout.ops_socket(long_home), layout.vault_socket(long_home)
    assert ops.parent == vault.parent == layout.short_run_dir(long_home)
    assert len(os.fsencode(str(vault))) <= layout.SUN_PATH_MAX
    assert layout.short_run_dir(long_home) != layout.short_run_dir(long_home.parent / "other")
    assert VaultService(long_home).socket_path == vault


async def test_the_ops_socket_binds_under_a_long_home(long_home):
    class Svc:
        pass

    path = layout.ops_socket(long_home)
    server = await start_server(Svc(), path)
    try:
        assert path.exists()
        assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
        assert layout.trusted_socket(path) is None
    finally:
        server.close()
        await server.wait_closed()


def test_a_symlinked_short_dir_is_refused_with_a_fix(long_home, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    short = layout.short_run_dir(long_home)
    short.symlink_to(target)
    with pytest.raises(layout.SocketDirUnsafe, match="shorter AGENTOS_HOME"):
        layout.prepare_socket(layout.ops_socket(long_home))
    with pytest.raises(layout.SocketDirUnsafe):
        layout.trusted_socket(layout.vault_socket(long_home))
    short.unlink()


def test_a_path_still_too_long_says_so(tmp_path):
    with pytest.raises(OSError, match="shorter AGENTOS_HOME"):
        layout.prepare_socket(tmp_path / ("y" * 200) / "ops.sock")
