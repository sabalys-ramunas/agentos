"""Lane edge cases below the manager: ids, store, engine pinning and close, displays, proxy secrets.

Runs on macOS: displays use small fake Xvfb and x11vnc scripts instead of the real tools, and the
engine tests stop before any browser starts. Tests that pin a defect are `xfail(strict=True)` and
name their BUG (see test_lanes_edge.py for the numbering).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import sqlite3
import stat
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from openajar.core.config import NodeConfig, load_proxies, save_proxies
from openajar.core.errors import EngineError
from openajar.core.models import LaneSpec, ProxyConfig, parse_lane_id
from openajar.mcp.server import Redactor
from openajar.node import display as display_mod
from openajar.node.camoufox.engine import CamoufoxEngine, CamoufoxSession
from openajar.node.display import DisplayManagerImpl, pid_alive
from openajar.node.store import LaneStore

PROXY_ENTRY = {"host": "198.51.100.10", "port": 12323, "username": "user1", "password": "hunter2-secret"}


def _proxies_home(tmp_path: Path, entries: dict) -> NodeConfig:
    cfg = NodeConfig(home=tmp_path)
    cfg.secrets_dir.mkdir(parents=True, exist_ok=True)
    cfg.proxies_path.write_text(json.dumps(entries))
    return cfg


# ---- lane ids --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../x/y", "a/../b", "a/.hidden", "A/b", "a/b/c", "a/", "/b", "a b/c",
                                 "a/" + "x" * 42, "a/b\x00"])
def test_lane_ids_that_could_escape_the_lanes_directory_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_lane_id(bad)
    with pytest.raises(ValueError):
        LaneSpec(id=bad)


def test_lane_id_must_end_at_the_name():
    with pytest.raises(ValueError):
        parse_lane_id("personal/reddit\n")


# ---- store -----------------------------------------------------------------------------


def test_store_keeps_every_write_under_concurrent_writers(tmp_path):
    path = tmp_path / "openajar.db"
    LaneStore(path)
    errors: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            store = LaneStore(path)
            for i in range(30):
                store.add(LaneSpec(id=f"w{n}/l{i}"))
                store.touch(f"w{n}/l{i}")
        except BaseException as exc:  # noqa: BLE001 (report any failure from the thread)
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    rows = LaneStore(path).list()
    assert len(rows) == 120 and all(r.last_used is not None for r in rows)


def test_one_corrupt_lane_row_does_not_hide_the_other_lanes(tmp_path):
    store = LaneStore(tmp_path / "openajar.db")
    store.add(LaneSpec(id="a/good"))
    conn = sqlite3.connect(tmp_path / "openajar.db")
    conn.execute("INSERT INTO lanes (id, spec) VALUES (?, ?)", ("b/bad", '{"id": "b/bad", "kind": "martian"}'))
    conn.commit()
    conn.close()
    assert "a/good" in [r.spec.id for r in store.list()]


# ---- engine: pinning and close ---------------------------------------------------------


@pytest.mark.parametrize("pin", ["{}", "[]", "null", '"Win32"'])
def test_unusable_fingerprint_pin_is_refused(tmp_path, pin):
    pytest.importorskip("camoufox.fingerprints")
    fp = tmp_path / "fingerprint.json"
    fp.write_text(pin)
    with pytest.raises(EngineError):
        CamoufoxEngine().options(LaneSpec(id="a/b"), profile_dir=tmp_path, fingerprint_path=fp,
                                 display=":150", proxy=None)


class _SlowContext:
    def __init__(self):
        self.pages: list = []  # the session's page tracker adopts open pages; there are none
        self.closing = asyncio.Event()

    def on(self, *_args):
        return None

    async def close(self):
        self.closing.set()
        await asyncio.sleep(5)


class _Driver:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


async def test_cancelled_engine_close_still_stops_the_driver(tmp_path):
    ctx, driver = _SlowContext(), _Driver()
    session = CamoufoxSession(driver, ctx, tmp_path / "profile", pid=None, close_timeout=30)
    closing = asyncio.create_task(session.close())
    await ctx.closing.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert driver.stopped


# ---- proxy secrets ---------------------------------------------------------------------


def test_redactor_masks_proxy_credentials_in_tool_text(tmp_path):
    cfg = _proxies_home(tmp_path, {"lt1": PROXY_ENTRY})
    red = Redactor(cfg)
    assert "hunter2-secret" not in red.text("proxy hunter2-secret refused the request")
    assert "hunter2-secret" not in red.text("GET http://user1:hunter2-secret@198.51.100.10:12323 failed")


def test_malformed_proxies_file_does_not_log_the_password(tmp_path, caplog):
    entry = {k: v for k, v in PROXY_ENTRY.items() if k != "host"}  # host missing, as if hand-edited
    cfg = _proxies_home(tmp_path, {"lt1": entry})
    with caplog.at_level(logging.DEBUG):
        Redactor(cfg).text("hello")
    assert "hunter2-secret" not in caplog.text


def test_malformed_proxy_entry_error_does_not_echo_the_password(tmp_path):
    entry = {k: v for k, v in PROXY_ENTRY.items() if k != "host"}
    cfg = _proxies_home(tmp_path, {"lt1": entry})
    with pytest.raises(ValueError) as err:
        load_proxies(cfg)
    assert "hunter2-secret" not in str(err.value)


def test_save_proxies_is_private_before_the_password_is_written(tmp_path, monkeypatch):
    """BUG-15: the password never sits in a file others can read, even for a moment: it is
    written to a 600 temp file that atomically replaces the target."""
    import os as _os
    cfg = NodeConfig(home=tmp_path)
    cfg.secrets_dir.mkdir(parents=True)
    cfg.proxies_path.write_text("{}")
    cfg.proxies_path.chmod(0o644)  # left behind by an older tool or a manual edit
    replaced: list[int] = []
    real_replace = _os.replace

    def spy(src, dst, *args, **kwargs):
        if Path(dst) == cfg.proxies_path:
            replaced.append(stat.S_IMODE(Path(src).stat().st_mode))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(_os, "replace", spy)
    save_proxies(cfg, {"lt1": ProxyConfig(label="lt1", **PROXY_ENTRY)})
    assert replaced and all(mode & 0o077 == 0 for mode in replaced)
    assert stat.S_IMODE(cfg.proxies_path.stat().st_mode) == 0o600
    assert not list(cfg.secrets_dir.glob(".proxies.json.*"))  # no temp file left behind


# ---- displays (fake Xvfb and x11vnc, so this runs without X) -----------------------------


def _tool(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\nimport os, pathlib, socket, sys, time\n{body}")
    path.chmod(0o755)
    return str(path)


XVFB_BODY = """
import json; (pathlib.Path(os.environ["FAKE_X_DIR"]) / "xvfb.argv").write_text(json.dumps(sys.argv[1:]))
num = sys.argv[1][1:]
root = pathlib.Path(os.environ["FAKE_X_DIR"])
(root / "xvfb.pid").write_text(str(os.getpid()))  # published first: the socket below means "ready"
(root / f"X{num}").touch()
time.sleep(60)
"""
VNC_BODY = """
import json; (pathlib.Path(os.environ["FAKE_X_DIR"]) / "x11vnc.argv").write_text(json.dumps(sys.argv[1:]))
(pathlib.Path(os.environ["FAKE_X_DIR"]) / "x11vnc.xauthority").write_text(os.environ.get("XAUTHORITY", ""))
s = socket.socket(socket.AF_UNIX)
s.bind(sys.argv[sys.argv.index("-unixsock") + 1]); s.listen()
time.sleep(60)
"""
VNC_EXITS_BODY = "sys.exit(3)\n"
# x11vnc that stays up but leaves the port to another process (its own session, so not ours)
VNC_FOREIGN_BODY = """
import subprocess
port = int(sys.argv[sys.argv.index("-rfbport") + 1])
code = f"import socket, time; s = socket.socket(); s.bind(('127.0.0.1', {port})); s.listen(); time.sleep(60)"
child = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
(pathlib.Path(os.environ["FAKE_X_DIR"]) / "foreign.pid").write_text(str(child.pid))
time.sleep(60)
"""


@pytest.fixture
def socket_dir():
    """Where the fake x11vnc puts its socket: short, since tmp_path can pass macOS's 104 byte limit."""
    parent = Path(tempfile.mkdtemp(prefix="openajar-t-"))
    yield parent / "vnc"
    shutil.rmtree(parent, ignore_errors=True)


@pytest.fixture
async def displays(tmp_path, monkeypatch, socket_dir):
    x_dir = tmp_path / "x11"
    x_dir.mkdir()
    monkeypatch.setattr(display_mod, "X_SOCKET_DIR", x_dir)
    monkeypatch.setattr(display_mod, "X_LOCK_DIR", x_dir)  # never the machine's /tmp/.X150-lock
    monkeypatch.setenv("FAKE_X_DIR", str(x_dir))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    config = NodeConfig(home=tmp_path, display_base=150, viewer_base=6150, max_slots=1, max_warm_lanes=1)
    dm = DisplayManagerImpl(config, xvfb=_tool(bin_dir / "xvfb", XVFB_BODY),
                            x11vnc=_tool(bin_dir / "x11vnc", VNC_EXITS_BODY),
                            start_timeout=5.0, log_dir=tmp_path / "logs", socket_dir=socket_dir)
    yield dm, bin_dir, x_dir / "xvfb.pid"
    await dm.release_all()  # stop every fake server the test left running


async def test_failed_display_start_stops_what_it_started_and_frees_the_slot(displays):
    dm, bin_dir, pid_file = displays
    with pytest.raises(EngineError, match="x11vnc"):
        await dm.acquire("a/one")
    # acquire() awaits every process it started before it raises, so Xvfb is already reaped here
    assert not pid_alive(int(pid_file.read_text())), "Xvfb was left running after x11vnc failed"
    assert dm.get("a/one") is None
    # the slot is free again: a working x11vnc now starts on the same display
    (bin_dir / "x11vnc").write_text((bin_dir / "x11vnc").read_text().replace(VNC_EXITS_BODY, VNC_BODY))
    viewer = await dm.acquire("a/one")
    assert viewer.display == ":150" and viewer.vnc_port is None and viewer.viewer_port is None
    assert viewer.vnc_socket == str(dm.socket_dir / "display-150.sock")


def _working_vnc(bin_dir: Path) -> None:
    (bin_dir / "x11vnc").write_text((bin_dir / "x11vnc").read_text().replace(VNC_EXITS_BODY, VNC_BODY))


async def test_x11vnc_listens_only_on_a_unix_socket_in_a_private_directory(displays, socket_dir):
    """No TCP listener at all: any local user (the brain user included) can open a localhost port."""
    dm, bin_dir, pid_file = displays
    _working_vnc(bin_dir)
    socket_dir.mkdir(mode=0o755)
    os.chmod(socket_dir, 0o755)  # an existing, too open directory is tightened, not trusted
    viewer = await dm.acquire("a/one")
    argv = json.loads((pid_file.parent / "x11vnc.argv").read_text())
    assert argv[argv.index("-rfbport") + 1] == "0"
    assert argv[argv.index("-rfbportv6") + 1] == "-1"  # without it x11vnc 0.9.16 still opens [::]:5900
    assert argv[argv.index("-unixsock") + 1] == viewer.vnc_socket
    assert "-localhost" not in argv  # with -unixsock it refuses every client on the socket (seen on 0.9.16)
    assert stat.S_IMODE(socket_dir.stat().st_mode) == 0o700
    assert stat.S_ISSOCK(Path(viewer.vnc_socket).stat().st_mode)
    await dm.release("a/one")
    assert not Path(viewer.vnc_socket).exists()


def _xauthority_entries(path: Path) -> list[dict]:
    """The entries of an Xauthority file: big-endian shorts and counted strings (libXau's format)."""
    raw, entries, pos = path.read_bytes(), [], 0

    def take(n: int) -> bytes:
        nonlocal pos
        pos += n
        return raw[pos - n:pos]

    def counted() -> bytes:
        return take(int.from_bytes(take(2), "big"))

    while pos < len(raw):
        entries.append({"family": int.from_bytes(take(2), "big"), "address": counted(), "number": counted(),
                        "name": counted(), "data": counted()})
    return entries


async def test_xvfb_x11vnc_and_the_viewer_share_a_cookie_that_only_lasts_while_the_display_runs(displays, socket_dir):
    """Xvfb's abstract socket takes any local client; only one that holds the lane's MIT-MAGIC-COOKIE-1
    gets in, so the brain user cannot screenshot or type into a logged-in browser."""
    dm, bin_dir, pid_file = displays
    _working_vnc(bin_dir)
    viewer = await dm.acquire("a/one")
    auth = Path(viewer.xauthority)
    assert auth == socket_dir / "display-150.xauth"
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    [entry] = _xauthority_entries(auth)
    assert entry["family"] == 256 and entry["address"] == socket.gethostname().encode()
    assert entry["number"] == b"150" and entry["name"] == b"MIT-MAGIC-COOKIE-1" and len(entry["data"]) == 16
    xvfb = json.loads((pid_file.parent / "xvfb.argv").read_text())
    vnc = json.loads((pid_file.parent / "x11vnc.argv").read_text())
    assert xvfb[xvfb.index("-auth") + 1] == str(auth)
    assert vnc[vnc.index("-auth") + 1] == str(auth)
    assert (pid_file.parent / "x11vnc.xauthority").read_text() == str(auth)  # the environment too
    await dm.release("a/one")
    assert not auth.exists(), "the cookie outlived its display"
    await dm.acquire("a/one")
    assert _xauthority_entries(auth)[0]["data"] != entry["data"], "a display started again reused its cookie"


async def test_a_stale_socket_from_a_dead_x11vnc_does_not_block_the_slot(displays, socket_dir):
    dm, bin_dir, _ = displays
    _working_vnc(bin_dir)
    socket_dir.mkdir(mode=0o700)
    with socket.socket(socket.AF_UNIX) as dead:
        dead.bind(str(socket_dir / "display-150.sock"))  # bound, never listening: what a crash leaves
    viewer = await dm.acquire("a/one")
    assert viewer.vnc_socket == str(socket_dir / "display-150.sock")


async def test_a_socket_directory_owned_by_someone_else_is_refused(displays, socket_dir, monkeypatch):
    dm, bin_dir, _ = displays
    _working_vnc(bin_dir)
    monkeypatch.setattr(display_mod.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(EngineError, match="not a directory owned by this user"):
        await dm.acquire("a/one")
    assert dm.get("a/one") is None


async def test_a_path_in_a_socket_directory_someone_else_owns_is_left_alone(displays, socket_dir, monkeypatch):
    """The directory's owner is checked before anything in it is removed, on start and on release."""
    dm, bin_dir, _ = displays
    _working_vnc(bin_dir)
    socket_dir.mkdir(mode=0o700)
    left = socket_dir / "display-150.sock"
    left.write_bytes(b"not ours")  # nothing listens on it: a stale path would be unlinked
    with monkeypatch.context() as m:
        m.setattr(display_mod.os, "geteuid", lambda: os.getuid() + 1)
        with pytest.raises(EngineError, match="not a directory owned by this user"):
            await dm.acquire("a/one")
    assert left.read_bytes() == b"not ours"
    viewer = await dm.acquire("a/one")  # ours again: the stale path is replaced by the live socket
    assert stat.S_ISSOCK(Path(viewer.vnc_socket).stat().st_mode)
    with monkeypatch.context() as m:  # the directory changes hands while the display runs
        m.setattr(display_mod.os, "geteuid", lambda: os.getuid() + 1)
        await dm.release("a/one")
    assert Path(viewer.vnc_socket).exists() and Path(viewer.xauthority).exists()


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_a_listener_that_is_not_our_x11vnc_does_not_count_as_ready(socket_dir):
    """Ready means the x11vnc we started holds the listening socket, not just that the path answers."""
    socket_dir.mkdir(mode=0o700)
    path = socket_dir / "display-150.sock"
    with socket.socket(socket.AF_UNIX) as foreign:
        foreign.bind(str(path))
        assert not display_mod._socket_listening(path)  # bound but not listening
        foreign.listen()
        assert display_mod._socket_listening(path)
        assert display_mod._socket_listening(path, os.getpid())
        other = os.getppid()
        assert not display_mod._socket_listening(path, other), "a foreign listener passed as our x11vnc"


async def test_acquire_replaces_a_display_whose_xvfb_died(displays):
    dm, bin_dir, pid_file = displays
    _working_vnc(bin_dir)
    await dm.acquire("a/one")
    old = int(pid_file.read_text())
    xvfb = dm._slots["a/one"].procs[0]
    assert xvfb.pid == old
    os.kill(old, signal.SIGKILL)
    await asyncio.wait_for(xvfb.wait(), 10)  # reaped: the display is dead, by event rather than by polling
    await dm.acquire("a/one")
    new = int(pid_file.read_text())
    assert new != old and pid_alive(new), "the lane was given a display whose Xvfb is gone"


@pytest.mark.skipif(not Path("/proc/net/tcp").exists(), reason="who owns a listener is read from /proc")
async def test_a_foreign_listener_on_the_vnc_port_is_not_our_x11vnc(displays):
    dm, bin_dir, _ = displays
    (bin_dir / "x11vnc").write_text((bin_dir / "x11vnc").read_text().replace(VNC_EXITS_BODY, VNC_FOREIGN_BODY))
    dm.start_timeout = 1.0
    foreign = Path(os.environ["FAKE_X_DIR"]) / "foreign.pid"
    try:
        with pytest.raises(EngineError, match="did not become ready"):
            await dm.acquire("a/one")
        assert foreign.exists(), "the fake x11vnc never started its foreign listener"
    finally:
        if foreign.exists():
            os.kill(int(foreign.read_text()), signal.SIGKILL)
    assert dm.get("a/one") is None


def test_a_session_whose_pid_was_never_found_is_looked_up_not_assumed_alive(tmp_path):
    session = CamoufoxSession(_Driver(), _SlowContext(), tmp_path / "profile", pid=None, close_timeout=1)
    if Path("/proc").is_dir():
        assert session.alive is False, "no browser runs with this profile, so the lane is not alive"
    else:
        assert session.alive is True  # nothing to look at here; the context close event still counts
    session._ctx_closed = True
    assert session.alive is False
