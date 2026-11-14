"""SQLite lane store, plus the pure helpers of the engine and display modules."""

from __future__ import annotations

import json
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from agentos.core.config import NodeConfig
from agentos.core.errors import LaneExists, LaneNotFound
from agentos.core.models import LaneSpec, ProxyConfig, utcnow
from agentos.node.camoufox.engine import CamoufoxEngine, os_of, parse_camou_config, write_private_json
from agentos.node.display import DisplayManagerImpl
from agentos.node.store import LaneStore


def test_store_roundtrip_and_wal(tmp_path):
    store = LaneStore(tmp_path / "agentos.db")
    spec = LaneSpec(id="personal/reddit", proxy_label="lt1")
    store.add(spec)
    with pytest.raises(LaneExists):
        store.add(spec)
    rec = store.get("personal/reddit")
    assert rec.spec == spec and rec.last_used is None and rec.exit_ip is None
    when = utcnow()
    store.touch("personal/reddit", when)
    store.set_exit_ip("personal/reddit", "198.51.100.10")
    rec = store.get("personal/reddit")
    assert rec.last_used == when and rec.exit_ip == "198.51.100.10"
    assert store.get("nope/nope") is None
    with pytest.raises(LaneNotFound):
        store.require("nope/nope")
    with sqlite3.connect(tmp_path / "agentos.db") as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert stat.S_IMODE((tmp_path / "agentos.db").stat().st_mode) == 0o600


def test_store_sees_writes_from_another_connection(tmp_path):
    daemon = LaneStore(tmp_path / "agentos.db")
    cli = LaneStore(tmp_path / "agentos.db")
    assert daemon.list() == []
    cli.add(LaneSpec(id="b/two"))
    cli.add(LaneSpec(id="a/one"))
    assert [r.spec.id for r in daemon.list()] == ["a/one", "b/two"]
    assert daemon.delete("a/one") and not daemon.delete("a/one")
    assert [r.spec.id for r in cli.list()] == ["b/two"]


def test_parse_camou_config_joins_chunks_in_order():
    blob = json.dumps({"navigator.platform": "Win32", "screen.width": 1920})
    parts = [blob[i:i + 4] for i in range(0, len(blob), 4)]
    env = [f"CAMOU_CONFIG_{i + 1}={p}" for i, p in enumerate(parts)]
    env = env[::-1] + ["HOME=/root", "DISPLAY=:110"]  # order in environ is not guaranteed
    assert len(parts) >= 10  # exercises numeric (not lexical) chunk ordering
    assert parse_camou_config("\0".join(env).encode() + b"\0") == json.loads(blob)


def test_parse_camou_config_without_chunks_fails():
    from agentos.core.errors import EngineError
    with pytest.raises(EngineError):
        parse_camou_config(b"HOME=/root\0")


@pytest.mark.parametrize(("platform", "expected"), [("Win32", "windows"), ("MacIntel", "macos"),
                                                    ("Linux x86_64", "linux"), ("", "linux")])
def test_os_of(platform, expected):
    assert os_of({"navigator.platform": platform}) == expected


def test_write_private_json(tmp_path):
    path = tmp_path / "lane" / "fingerprint.json"
    write_private_json(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    write_private_json(path, {"a": 2})
    assert json.loads(path.read_text()) == {"a": 2}
    assert [p.name for p in path.parent.iterdir()] == ["fingerprint.json"]


def test_engine_options_first_launch_and_pinned(tmp_path, monkeypatch):
    pytest.importorskip("camoufox.fingerprints")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    engine = CamoufoxEngine()
    spec = LaneSpec(id="a/b", locale="en-US")
    fp = tmp_path / "fingerprint.json"
    proxy = ProxyConfig(label="lt1", host="198.51.100.10", port=12323, username="u", password="p")
    opts = engine.options(spec, profile_dir=tmp_path / "profile", fingerprint_path=fp, display=":110", proxy=proxy)
    assert opts["persistent_context"] is True and opts["headless"] is False
    assert opts["user_data_dir"] == str(tmp_path / "profile")
    assert opts["geoip"] is True and opts["humanize"] is True and opts["enable_cache"] is True
    assert opts["locale"] == "en-US" and opts["i_know_what_im_doing"] is True
    assert opts["proxy"] == {"server": "http://198.51.100.10:12323", "username": "u", "password": "p"}
    assert opts["env"]["DISPLAY"] == ":110" and "WAYLAND_DISPLAY" not in opts["env"]
    assert opts["env"]["PATH"]  # a full environment, since Camoufox does not merge env
    assert "config" not in opts and "os" not in opts
    assert opts["screen"].max_width == 1920 and opts["screen"].max_height == 1080

    fp.write_text(json.dumps({"navigator.platform": "MacIntel", "navigator.userAgent": "UA"}))
    pinned = engine.options(spec, profile_dir=tmp_path / "profile", fingerprint_path=fp, display=":110",
                            proxy=None)
    assert pinned["config"]["navigator.userAgent"] == "UA" and pinned["os"] == "macos"
    assert "proxy" not in pinned


def test_the_browser_is_given_its_displays_cookie(tmp_path):
    """Xvfb takes only clients that hold the lane's cookie, so the browser's environment names its file."""
    pytest.importorskip("camoufox.fingerprints")
    engine = CamoufoxEngine()
    opts = engine.options(LaneSpec(id="a/b"), profile_dir=tmp_path / "profile", fingerprint_path=tmp_path / "fp.json",
                          display=":110", proxy=None, xauthority="/private/vnc/display-110.xauth")
    assert opts["env"]["XAUTHORITY"] == "/private/vnc/display-110.xauth" and opts["env"]["DISPLAY"] == ":110"


def test_display_process_matching_stays_in_our_range(tmp_path):
    dm = DisplayManagerImpl(NodeConfig(home=tmp_path, display_base=110, vnc_base=5810, viewer_base=6110,
                                       max_slots=10))
    m = dm._match_slot_process
    assert m(["Xvfb", ":110", "-screen", "0", "1920x1080x24"]) == 0
    assert m(["/usr/bin/Xvfb", ":119"]) == 9
    assert m(["Xvfb", ":99"]) is None and m(["Xvfb", ":120"]) is None
    assert m(["x11vnc", "-display", ":113", "-rfbport", "5813"]) == 3
    assert m(["x11vnc", "-display", ":99", "-rfbport", "5900"]) is None
    assert m(["/usr/bin/python3", "/usr/bin/websockify", "--web=/usr/share/novnc",
              "127.0.0.1:6115", "127.0.0.1:5815"]) == 5
    assert m(["/usr/bin/python3", "/usr/bin/websockify", "--web=/usr/share/novnc/", "6080",
              "localhost:5900"]) is None
    assert m(["python3", "-m", "pytest", "127.0.0.1:6110"]) is None


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_browser_pid_helpers_ignore_unrelated_processes(tmp_path):
    from agentos.node.camoufox.engine import browser_pids, browser_pids_under
    assert browser_pids(tmp_path / "profile") == []
    assert browser_pids_under(Path(tmp_path)) == []


def test_engine_options_reject_corrupt_pin(tmp_path):
    pytest.importorskip("camoufox.fingerprints")
    from agentos.core.errors import EngineError
    fp = tmp_path / "fingerprint.json"
    fp.write_text("{truncated")
    with pytest.raises(EngineError, match="not valid JSON"):
        CamoufoxEngine().options(LaneSpec(id="a/b"), profile_dir=tmp_path, fingerprint_path=fp, display=":110",
                                 proxy=None)


def test_store_adds_the_new_columns_to_an_old_database(tmp_path):
    path = tmp_path / "agentos.db"
    with sqlite3.connect(path) as conn:  # the schema before quarantine and signals were stored
        conn.execute("CREATE TABLE lanes (id TEXT PRIMARY KEY, spec TEXT NOT NULL, last_used TEXT, exit_ip TEXT)")
        conn.execute("INSERT INTO lanes (id, spec) VALUES (?, ?)", ("a/one", LaneSpec(id="a/one").model_dump_json()))
    store = LaneStore(path)
    LaneStore(path)  # a second process opening it is fine too
    rec = store.require("a/one")
    assert rec.quarantine is None and rec.signal is None
    from agentos.core.models import Signal
    from agentos.node.store import QuarantineRecord, SignalRecord

    store.set_quarantine("a/one", QuarantineRecord(reason="r", at=utcnow(), kind="exit_ip_changed",
                                                   proxy_label="gw", exit_ip="203.0.113.9"))
    store.set_signal("a/one", SignalRecord(signal=Signal.CAPTCHA, at=utcnow(), detail="x.test"))
    rec = store.require("a/one")
    assert rec.quarantine.exit_ip == "203.0.113.9" and rec.signal.signal == Signal.CAPTCHA
    store.pin_proxy_exit("gw", "203.0.113.9")
    assert store.proxy_exit("gw").exit_ip == "203.0.113.9" and store.forget_proxy_exit("gw")
    store.add_event("a/one", "held", {"reason": "x"})
    store.add_event("a/one", "hold_cleared", {})
    assert [e.kind for e in store.events("a/one")] == ["hold_cleared", "held"]
