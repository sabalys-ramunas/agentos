"""The lane check (agentos.stealth.lanecheck): its rules, the cookie reader, the checker end to end against a
fake host, the health monitor's use of it, the lane_check tool, the panel routes and the CLI."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_ops_support import PROXY_PASS, PROXY_USER, FakeHost, GiB, fingerprint, make_home
from typer.testing import CliRunner

from agentos.core.errors import LaneNotFound
from agentos.core.models import (
    AccountRef,
    ControlState,
    Effect,
    LaneControl,
    LaneInfo,
    LaneSpec,
    LaneState,
    ProxyConfig,
    Signal,
)
from agentos.node.store import LaneStore, QuarantineRecord, SignalRecord
from agentos.ops.health import CRITICAL, HealthMonitor
from agentos.stealth import lanecheck as lc
from agentos.stealth.lanecheck import FAIL, OK, UNKNOWN, WARN, LaneChecker, LaneFacts

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
CFG = """// camoufox.cfg as shipped (the parts the check reads)
defaultPref("media.peerconnection.ice.no_host", false);
defaultPref("media.peerconnection.ice.proxy_only_if_behind_proxy", true);
defaultPref("media.peerconnection.ice.obfuscate_host_addresses", true);
defaultPref("network.proxy.socks_remote_dns", true);
"""
SESSION = "SESSIONVALUE-SECRET-123"
USER_ID = "4242"
PROXY = ProxyConfig(label="lt1", host="203.0.113.7", port=12323, username=PROXY_USER, password=PROXY_PASS)


# ---- helpers ----------------------------------------------------------------------------------

def cookie_db(profile: Path, cookies: list[tuple[str, str, str, float]], *, wal: bool = False) -> sqlite3.Connection:
    """A cookies.sqlite with Firefox's moz_cookies columns. Returns the open connection (a writer)."""
    profile.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(profile / "cookies.sqlite", isolation_level=None)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE IF NOT EXISTS moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT NOT NULL "
                 "DEFAULT '', name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER, "
                 "creationTime INTEGER, isSecure INTEGER, isHttpOnly INTEGER)")
    for name, value, host, expiry in cookies:
        conn.execute("INSERT INTO moz_cookies (name, value, host, path, expiry) VALUES (?, ?, ?, '/', ?)",
                     (name, value, host, int(expiry)))
    return conn


def signed_in(profile: Path, user_id: str = USER_ID, *, wal: bool = False) -> sqlite3.Connection:
    later = time.time() + 86400 * 30
    return cookie_db(profile, [("sessionid", SESSION, ".instagram.com", later),
                               ("ds_user_id", user_id, ".instagram.com", later),
                               ("other", "x", ".example.com", later)], wal=wal)


class Home:
    """An AGENTOS_HOME with a real lane store and a fake host where everything is healthy."""

    def __init__(self, tmp_path: Path):
        self.cfg = make_home(tmp_path, lanes={})
        self.store = LaneStore(self.cfg.db_path)
        self.host = FakeHost()
        browser = tmp_path / "camoufox"
        browser.mkdir()
        (browser / "camoufox.cfg").write_text(CFG)
        self.host.camoufox_info = {"path": str(browser), "verstr": "158.0.1-beta.41"}
        self.clock = [NOW.timestamp()]

    def lane(self, lane_id: str = "biz/instagram", *, proxy: str | None = "lt1", locale: str = "lt-LT",
             account: AccountRef | None = None, pin: dict | None | bool = True, cookies: bool = True) -> Path:
        self.store.add(LaneSpec(id=lane_id, proxy_label=proxy, locale=locale, account=account))
        lane_dir = self.cfg.lane_dir(lane_id)
        (lane_dir / "profile").mkdir(parents=True, exist_ok=True)
        if pin is True:
            pin = {**fingerprint(158), lc.PIN_META: {"version": 2, "geo": {"ip": "203.0.113.7", "country": "LT"}}}
        if pin:
            (lane_dir / "fingerprint.json").write_text(json.dumps(pin))
        if cookies:
            signed_in(lane_dir / "profile").close()
        return lane_dir / "profile"

    def checker(self, **kw: Any) -> LaneChecker:
        kw.setdefault("host", self.host)
        return LaneChecker(self.cfg, clock=lambda: self.clock[0], **kw)


def leaky(profile) -> None:
    """A profile that turns the engine's WebRTC pref off again: blank frames can leak (a warning)."""
    (profile / "user.js").write_text('user_pref("media.peerconnection.ice.proxy_only", false);\n')


def by_id(report: lc.LaneReport) -> dict[str, lc.Check]:
    return {c.id: c for c in report.checks}


@pytest.fixture
def home(tmp_path) -> Home:
    return Home(tmp_path)


# ---- rules ----------------------------------------------------------------------------------

def test_exit_rule_matches_the_self_check():
    ok = lc.judge_exit(PROXY, "203.0.113.7", host_ips={"203.0.113.7"}, pinned=None, node_ip="192.0.2.10")
    assert ok.level == OK
    other = lc.judge_exit(PROXY, "198.51.100.1", host_ips={"203.0.113.7"}, pinned=None, node_ip="192.0.2.10")
    assert other.level == FAIL and "quarantine" in other.detail
    node = lc.judge_exit(PROXY, "192.0.2.10", host_ips={"203.0.113.7"}, pinned=None, node_ip="192.0.2.10")
    assert node.level == FAIL and "node's own IP" in node.detail
    gw = ProxyConfig(label="gw", host="gate.example.net", port=1)
    assert lc.judge_exit(gw, "198.51.100.9", host_ips=set(), pinned=None, node_ip="192.0.2.10").level == OK
    # an unpinned gateway is not passed while the node's own IP is unknown (QA-lanecheck-5)
    assert lc.judge_exit(gw, "198.51.100.9", host_ips=set(), pinned=None, node_ip=None).level == UNKNOWN
    assert lc.judge_exit(gw, "198.51.100.9", host_ips=set(), pinned="198.51.100.9", node_ip=None).level == OK
    moved = lc.judge_exit(gw, "198.51.100.10", host_ips=set(), pinned="198.51.100.9", node_ip=None)
    assert moved.level == FAIL and "198.51.100.9" in moved.detail and "{lane}" in moved.fix


def test_location_compares_country_and_offset():
    geo = {"ip": "203.0.113.7", "country": "LT", "timezone": "Europe/Vilnius"}
    same = lc.judge_location(geo, pinned_tz="Europe/Vilnius", pinned_country="LT", locale="lt-LT", at=NOW)
    assert same.level == OK and "the same timezone" in same.detail
    riga = lc.judge_location({**geo, "timezone": "Europe/Riga"}, pinned_tz="Europe/Vilnius", pinned_country="LT",
                             locale="lt-LT", at=NOW)
    assert riga.level == OK and "same offset" in riga.detail
    locale = lc.judge_location(geo, pinned_tz="Europe/Vilnius", pinned_country="LT", locale="en-US", at=NOW)
    assert locale.level == WARN and "en-US says US" in locale.detail and "LT (Europe/Vilnius, UTC+02:00)" in locale.detail
    far = lc.judge_location({"ip": "x", "country": "US", "timezone": "America/New_York"}, pinned_tz="Europe/Vilnius",
                            pinned_country="LT", locale="en", at=NOW)
    assert far.level == WARN
    assert "the pin was made in LT" in far.detail and "Europe/Vilnius (UTC+02:00)" in far.detail
    assert far.data["exit_offset"] == "UTC-05:00" and far.data["pinned_offset"] == "UTC+02:00"
    assert lc.judge_location(None, pinned_tz="Europe/Vilnius", pinned_country=None, locale="en", at=NOW).level == UNKNOWN
    first = lc.judge_location(geo, pinned_tz=None, pinned_country=None, locale="lt", at=NOW)
    assert first.level == OK and "first launch" in first.detail
    assert lc.locale_region("en-US") == "US" and lc.locale_region("en") is None and lc.locale_region("zh-Hant-TW") == "TW"


def test_prefs_follow_firefox_order():
    cfg = lc.parse_prefs(CFG + 'lockPref("network.trr.mode", 5);\npref("a.b", "x");\n')
    prefs = lc.effective_prefs(cfg=cfg, launch={"media.peerconnection.enabled": True, "network.trr.mode": 2},
                               prefs_js={"media.peerconnection.ice.proxy_only": True},
                               user_js={"media.peerconnection.ice.proxy_only": False})
    assert prefs["media.peerconnection.ice.proxy_only"] is False  # user.js beats prefs.js
    assert prefs["network.trr.mode"] == 5                          # a locked pref beats everything
    assert prefs["media.peerconnection.ice.proxy_only_if_behind_proxy"] is True and prefs["a.b"] == "x"
    assert lc.parse_prefs('user_pref("x", nonsense);') == {}


def test_webrtc_rule(tmp_path):
    user_js = tmp_path / "user.js"
    base = lc.effective_prefs(cfg=lc.parse_prefs(CFG), launch={"media.peerconnection.enabled": True},
                              prefs_js={}, user_js={})
    shipped = lc.judge_webrtc(base, proxied=True, known=True, user_js=user_js)
    assert shipped.level == WARN and "blank frame" in shipped.detail and str(user_js) in shipped.fix
    assert lc.judge_webrtc({**base, "media.peerconnection.ice.proxy_only": True}, proxied=True, known=True,
                           user_js=user_js).level == OK
    assert lc.judge_webrtc({**base, "media.peerconnection.enabled": False}, proxied=True, known=True,
                           user_js=user_js).level == OK
    bare = {"media.peerconnection.enabled": True}
    assert lc.judge_webrtc(bare, proxied=True, known=True, user_js=user_js).level == FAIL
    assert lc.judge_webrtc(bare, proxied=True, known=False, user_js=user_js).level == UNKNOWN
    shown = {**base, "media.peerconnection.ice.obfuscate_host_addresses": False,
             "media.peerconnection.ice.proxy_only": True}
    assert lc.judge_webrtc(shown, proxied=True, known=True, user_js=user_js).level == FAIL
    assert lc.judge_webrtc(bare, proxied=False, known=False, user_js=user_js).level == OK


def test_dns_rule():
    assert lc.judge_dns({}, proxy=PROXY).level == OK  # an HTTP proxy gets the host name in CONNECT
    assert lc.judge_dns({}, proxy=None).level == OK


def test_hold_rule():
    spec = LaneSpec(id="biz/instagram")
    q = lc.judge_hold(LaneFacts(spec, quarantine={"reason": "exit IP changed", "at": NOW, "by": "node",
                                                  "kind": "exit_ip_changed"}), now=NOW)
    assert q.level == FAIL and "2026-11-04 12:00 UTC" in q.detail and "handback biz/instagram" in q.fix
    held = LaneControl(state=ControlState.PAUSED, by="node", since=NOW, reason="needs_human:checkpoint")
    h = lc.judge_hold(LaneFacts(spec, control=held), now=NOW)
    assert h.level == FAIL and h.data["cleared_by"] == "human" and "needs_human:checkpoint" in h.detail
    timed = held.model_copy(update={"reason": "rate_limited", "hold_until": NOW + timedelta(minutes=30)})
    t = lc.judge_hold(LaneFacts(spec, control=timed), now=NOW)
    assert t.level == WARN and t.data["cleared_by"] == "time" and "12:30" in t.detail
    stopped = lc.judge_hold(LaneFacts(spec, control=LaneControl(state=ControlState.PAUSED, by="robin")), now=NOW)
    assert stopped.level == FAIL and "robin" in stopped.detail
    human = lc.judge_hold(LaneFacts(spec, control=LaneControl(state=ControlState.HUMAN, by="robin")), now=NOW)
    assert human.level == WARN
    sig = lc.judge_hold(LaneFacts(spec, signal={"signal": "captcha", "at": NOW, "detail": "instagram.com"}), now=NOW)
    assert sig.level == WARN and "captcha on instagram.com" in sig.detail
    assert lc.judge_hold(LaneFacts(spec, signal={"signal": "ok", "at": NOW}), now=NOW).level == OK


def test_lane_level_ignores_unknown():
    assert lc.worst([OK, UNKNOWN]) == OK and lc.worst([UNKNOWN, WARN]) == WARN and lc.worst([WARN, FAIL]) == FAIL


# ---- the cookie reader ---------------------------------------------------------------------------

def test_cookie_reader_reads_a_copy_and_never_the_file(tmp_path):
    profile = tmp_path / "profile"
    now = time.time()
    writer = cookie_db(profile, [("sessionid", SESSION, ".instagram.com", now + 3600),
                                 ("old", "x", ".instagram.com", now - 10),
                                 ("ms", "y", "www.instagram.com", (now + 3600) * 1000),
                                 ("old_ms", "z", ".instagram.com", (now - 10) * 1000),
                                 ("elsewhere", "w", ".evilinstagram.com", now + 3600)], wal=True)
    try:
        before = sorted(p.name for p in profile.iterdir())
        assert "cookies.sqlite-wal" in before  # the rows are only in the WAL: a plain copy of the db misses them
        found = lc.read_cookie_db(profile, ["instagram.com"], now=now, scratch=tmp_path)
        assert sorted(c["name"] for c in found) == ["ms", "sessionid"]
        assert sorted(p.name for p in profile.iterdir()) == before  # nothing created next to the live file
        assert not [p for p in tmp_path.iterdir() if p.name.startswith("agentos-cookies-")]  # the copy is gone
        writer.execute("INSERT INTO moz_cookies (name, value, host, path, expiry) VALUES ('new', 'v', "
                       "'.instagram.com', '/', ?)", (int(now + 60),))  # the browser keeps writing
        assert "new" in {c["name"] for c in lc.read_cookie_db(profile, ["instagram.com"], now=now)}
    finally:
        writer.close()
    assert lc.read_cookie_db(tmp_path / "empty", ["instagram.com"]) == []
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "cookies.sqlite").write_bytes(b"not a database at all" * 10)
    assert lc.read_cookie_db(tmp_path / "broken", ["instagram.com"], tries=2) is None


def test_cookie_reader_retries_while_the_file_changes(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    signed_in(profile).close()
    stats = iter([(1, 1, 1), (2, 2, 2), (3, 3, 3), (3, 3, 3), (3, 3, 3), (3, 3, 3)])
    real = lc._stat
    monkeypatch.setattr(lc, "_stat", lambda p: next(stats) if p.name == "cookies.sqlite" else real(p))
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    found = lc.read_cookie_db(profile, ["instagram.com"])
    assert found is not None and {"sessionid", "ds_user_id"} <= {c["name"] for c in found}


# ---- the checker end to end ------------------------------------------------------------------------

async def test_a_healthy_lane_is_safe(home):
    home.lane(account=AccountRef(site="instagram", handle="@acme", user_id=USER_ID))
    report = await home.checker().check("biz/instagram")
    checks = by_id(report)
    assert list(checks) == list(lc.CHECK_IDS)
    assert {c.id: c.level for c in report.checks} == {
        "proxy": OK, "exit_ip": OK, "location": OK, "webrtc": OK, "dns": OK, "profile": OK, "session": OK,
        "account": OK, "hold": OK}
    assert report.level == OK and report.safe
    assert checks["session"].data["source"] == "file"
    assert checks["account"].detail == "Signed in as the expected Instagram account (@acme)."
    text = json.dumps(report.to_dict())
    for secret in (SESSION, USER_ID, PROXY_PASS, PROXY_USER):
        assert secret not in text, secret
    assert "—" not in text and "—" not in lc.format_report(report)


async def test_signed_out_and_wrong_account_fail(home, tmp_path):
    profile = home.lane(account=AccountRef(site="instagram", handle="@acme", user_id="9999"))
    report = await home.checker().check("biz/instagram")
    account = by_id(report)["account"]
    assert account.level == FAIL and "@acme" in account.detail and "9999" not in json.dumps(account.to_dict())
    (profile / "cookies.sqlite").unlink()
    cookie_db(profile, [("ds_user_id", "9999", ".instagram.com", time.time() + 86400 * 30)]).close()
    report = await home.checker().check("biz/instagram")
    assert by_id(report)["session"].level == FAIL and "no sessionid cookie" in by_id(report)["session"].detail
    assert by_id(report)["account"].level == UNKNOWN and report.level == FAIL and not report.safe


async def test_account_without_a_pinned_id_or_visible_id(home):
    home.lane(account=AccountRef(site="instagram", handle="@acme"))
    assert by_id(await home.checker().check("biz/instagram"))["account"].level == WARN
    home.lane("biz/linkedin", account=AccountRef(site="linkedin", user_id="x"), cookies=False)
    cookie_db(home.cfg.profile_dir("biz/linkedin"), [("li_at", "v", ".linkedin.com", time.time() + 86400 * 30)]).close()
    r = await home.checker().check("biz/linkedin")
    assert by_id(r)["session"].level == OK and by_id(r)["account"].level == UNKNOWN
    assert "no account id in its cookies" in by_id(r)["account"].detail
    home.lane("biz/notes", cookies=False)
    notes = await home.checker().check("biz/notes")
    assert by_id(notes)["session"].level == UNKNOWN and by_id(notes)["account"].level == UNKNOWN


async def test_geo_is_looked_up_once_a_day_per_proxy(home):
    home.lane()
    home.lane("biz/ig", cookies=False)
    checker = home.checker()
    await checker.check_all()
    assert home.host.exit_info_calls == ["lt1"]  # two lanes, one proxy, one lookup
    cache = json.loads(checker.geo_path.read_text())
    assert cache["lt1"]["country"] == "LT" and oct(checker.geo_path.stat().st_mode & 0o777) == "0o600"
    assert PROXY_PASS not in checker.geo_path.read_text() and PROXY_USER not in checker.geo_path.read_text()
    home.clock[0] += 3600
    second = home.checker()  # a new process reads the cache
    report = await second.check("biz/instagram")
    assert home.host.exit_info_calls == ["lt1"] and by_id(report)["location"].level == OK
    home.clock[0] += lc.GEO_TTL
    await home.checker().check("biz/instagram")
    assert home.host.exit_info_calls == ["lt1", "lt1"]


async def test_a_moved_exit_and_a_failed_lookup(home):
    home.lane(locale="en-US")
    home.host.exit_geo["203.0.113.7"] = {"country": "DE", "timezone": "Europe/Berlin"}
    loc = by_id(await home.checker().check("biz/instagram"))["location"]
    assert loc.level == WARN and "DE (Europe/Berlin, UTC+01:00)" in loc.detail and "en-US says US" in loc.detail
    home.lane("biz/ig", cookies=False)
    home.host.exit_geo["203.0.113.7"] = OSError("HTTP 429 from ipinfo.io")
    checker = home.checker(write_cache=False)
    checker.geo_path = home.cfg.home / "cache" / "other.json"
    report = await checker.check("biz/ig")
    assert by_id(report)["proxy"].level == OK and by_id(report)["location"].level == UNKNOWN
    assert not checker.geo_path.exists()  # write_cache=False writes nothing
    assert home.host.exit_info_calls == ["lt1"]  # and, since it could not keep the answer, asks nothing
    failing = home.checker()
    failing.geo_path = home.cfg.home / "cache" / "failing.json"
    loc = by_id(await failing.check("biz/ig"))["location"]
    assert loc.level == UNKNOWN and "failed today" in loc.detail
    assert json.loads(failing.geo_path.read_text())["lt1"]["tried"] == home.clock[0]  # counts as today's lookup


async def test_proxy_down_missing_or_absent(home):
    home.lane()
    home.host.tcp_errors["203.0.113.7"] = ConnectionRefusedError("refused")
    r = by_id(await home.checker().check("biz/instagram"))
    assert r["proxy"].level == FAIL and r["exit_ip"].level == UNKNOWN and r["location"].level == UNKNOWN
    home.lane("biz/gone", proxy="nope", cookies=False)
    gone = by_id(await home.checker().check("biz/gone"))
    assert gone["proxy"].level == FAIL and "no proxy nope" in gone["proxy"].detail
    home.lane("biz/fb", proxy=None, cookies=False)
    fb = by_id(await home.checker().check("biz/fb"))
    assert fb["proxy"].level == WARN and fb["webrtc"].level == OK and fb["dns"].level == OK
    home.lane("biz/scratch", proxy=None, cookies=False)
    assert by_id(await home.checker().check("biz/scratch"))["proxy"].level == OK  # no account on it


async def test_offline_probes_nothing(home):
    home.lane()
    calls = []
    home.host.tcp_connect = lambda *a, **k: calls.append(a)
    report = await home.checker(network=False).check("biz/instagram")
    assert calls == [] and by_id(report)["proxy"].level == UNKNOWN


async def test_profile_problems(home):
    home.lane(pin=False)  # used (it has cookies), but no pin
    assert by_id(await home.checker().check("biz/instagram"))["profile"].level == FAIL
    home.lane("biz/new", cookies=False, pin=False)
    fresh = by_id(await home.checker().check("biz/new"))["profile"]
    assert fresh.level == OK and "pinned on first launch" in fresh.detail
    home.lane("biz/old", pin=fingerprint(150))
    old = by_id(await home.checker().check("biz/old"))["profile"]
    assert old.level == WARN and "refresh-fingerprint biz/old --keep-device" in old.fix
    assert "version 1" in old.detail and "Firefox 150" in old.detail  # every warning of the same level shows
    home.lane("biz/v1", pin=fingerprint(158))
    v1 = by_id(await home.checker().check("biz/v1"))["profile"]
    assert v1.level == WARN and "version 1" in v1.detail
    home.lane("biz/ahead", pin=fingerprint(160))
    assert by_id(await home.checker().check("biz/ahead"))["profile"].level == FAIL
    home.lane("biz/locked")
    locked_profile = home.cfg.profile_dir("biz/locked")
    os.symlink("127.0.1.1:+999999", locked_profile / "lock")
    closed = by_id(await home.checker().check("biz/locked"))["profile"]
    assert "stale_lock" not in closed.data  # every clean exit leaves the lock too
    (locked_profile / "sessionstore-backups").mkdir()
    (locked_profile / "sessionstore-backups" / "recovery.jsonlz4").write_bytes(b"x")
    locked = by_id(await home.checker().check("biz/locked"))["profile"]
    assert locked.level == WARN and locked.data["stale_lock"]
    home.host.default_disk = (40 * GiB, 39 * GiB, 1 * GiB)
    assert by_id(await home.checker().check("biz/instagram"))["profile"].level == FAIL
    home.host.default_disk = (40 * GiB, 10 * GiB, 30 * GiB)
    (home.cfg.lane_dir("biz/broken") / "profile").mkdir(parents=True)
    home.store.add(LaneSpec(id="biz/broken", proxy_label="lt1"))
    (home.cfg.lane_dir("biz/broken") / "fingerprint.json").write_text("{nope")
    assert by_id(await home.checker().check("biz/broken"))["profile"].level == FAIL


async def test_an_orphan_browser_fails_the_profile(home):
    profile = home.lane()
    home.host.browsers[str(profile)] = [4321]
    offline = by_id(await home.checker().check("biz/instagram"))["profile"]
    assert offline.level == WARN and "agentosd is not running" in offline.detail
    lanes = FakeManager(home)
    daemon = by_id(await home.checker(lanes=lanes).check("biz/instagram"))["profile"]
    assert daemon.level == FAIL and "nobody owns" in daemon.detail


async def test_holds_from_the_store(home):
    home.lane()
    home.store.set_quarantine("biz/instagram", QuarantineRecord(reason="exit IP changed", at=NOW, kind="exit_ip_changed"))
    report = await home.checker().check("biz/instagram")
    assert report.state == "quarantined" and by_id(report)["hold"].level == FAIL
    home.lane("biz/ig", cookies=False)
    home.store.set_signal("biz/ig", SignalRecord(signal=Signal.CHECKPOINT, at=NOW, detail="instagram.com"))
    assert by_id(await home.checker().check("biz/ig"))["hold"].level == WARN
    with pytest.raises(LaneNotFound):
        await home.checker().check("biz/none")
    assert [r.lane for r in await home.checker().check_all()] == ["biz/ig", "biz/instagram"]


# ---- inside the daemon: the lane manager and a warm lane ---------------------------------------

class FakeContext:
    def __init__(self, cookies: list[dict]):
        self._cookies = cookies
        self.asked: list[list[str]] = []

    async def cookies(self, urls: list[str]) -> list[dict]:
        self.asked.append(urls)
        return self._cookies


class FakeManager:
    """The parts of LaneManagerImpl the checker reads: list, get, store, and a warm lane's context."""

    def __init__(self, home: Home, warm: dict[str, FakeContext] | None = None):
        self.store = home.store
        self.warm = warm or {}
        self.engine = None
        self._lanes = {lane_id: type("L", (), {"session": type("S", (), {"context": ctx})()})()
                       for lane_id, ctx in self.warm.items()}

    def list(self) -> list[LaneInfo]:
        return [self.get(rec.spec.id) for rec in self.store.list()]

    def get(self, lane_id: str) -> LaneInfo:
        rec = self.store.require(lane_id)
        state = LaneState.QUARANTINED if rec.quarantine else LaneState.WARM if lane_id in self.warm else LaneState.COLD
        return LaneInfo(spec=rec.spec, state=state, control=rec.control or LaneControl(),
                        signal=rec.signal.signal if rec.signal else None, signal_at=rec.signal.at if rec.signal else None,
                        quarantine_reason=rec.quarantine.reason if rec.quarantine else None)


async def test_a_warm_lane_reads_cookies_from_its_browser(home):
    home.lane(account=AccountRef(site="instagram", user_id=USER_ID), cookies=False)
    ctx = FakeContext([{"name": "sessionid", "value": SESSION, "domain": ".instagram.com"},
                       {"name": "ds_user_id", "value": USER_ID, "domain": ".instagram.com"}])
    profile = home.cfg.profile_dir("biz/instagram")
    home.host.browsers[str(profile)] = [77]
    prefs = {"media.peerconnection.ice.proxy_only": True}
    home.host.environs[77] = b"PATH=/bin\0CAMOU_PREFS_1=" + json.dumps(prefs).encode() + b"\0SECRET=x\0"
    report = await home.checker(lanes=FakeManager(home, {"biz/instagram": ctx})).check("biz/instagram")
    checks = by_id(report)
    assert report.state == "warm" and checks["session"].data["source"] == "live"
    assert checks["account"].level == OK and checks["profile"].level == OK  # its own browser, not an orphan
    assert checks["webrtc"].level == OK  # the running browser's launch prefs were read
    assert "https://www.instagram.com/" in ctx.asked[0]


# ---- the health monitor -------------------------------------------------------------------------

class Notifier:
    def __init__(self):
        self.events: list[dict] = []

    def send(self, event: dict) -> None:
        self.events.append(event)


async def test_health_notifies_on_lane_level_changes_only(home):
    leaky(home.lane(locale="lt-LT"))
    lanes = FakeManager(home)
    checker = home.checker(lanes=lanes)
    m = HealthMonitor(home.cfg, host=home.host, lanes=lanes, notifier=Notifier(), clock=lambda: home.clock[0])
    m.lane_checker = checker
    r1 = await m.run_once(lanes=True)
    lane = next(f for f in r1["findings"] if f["key"] == "lane.biz/instagram")
    assert lane["level"] == "warn" and "blank frame" in lane["detail"]
    assert not any(f["kind"] == "drift" for f in r1["findings"])  # the lane check covers drift
    assert [e["key"] for e in m.notifier.events] == ["lane.biz/instagram"]
    home.clock[0] += 120
    await m.run_once(lanes=True)
    assert len(m.notifier.events) == 1  # same level: no new notification
    home.store.set_quarantine("biz/instagram", QuarantineRecord(reason="exit IP changed", at=NOW))
    home.clock[0] += 120
    r3 = await m.run_once(lanes=True)
    assert next(f for f in r3["findings"] if f["key"] == "lane.biz/instagram")["level"] == CRITICAL
    assert m.notifier.events[-1]["title"] == "Lane biz/instagram is not safe to use"
    r4 = await m.run_once(proxies=False, lanes=True)  # between lane rounds the last findings stay
    assert any(f["key"] == "lane.biz/instagram" for f in r4["findings"]) and len(m.notifier.events) == 2
    assert home.host.exit_info_calls == ["lt1"]


# ---- the MCP tool --------------------------------------------------------------------------------

class Ctx:
    def __init__(self, runtime: Any, allowed: set[str] | None = None):
        self.runtime = runtime
        self.authorized: list[tuple] = []
        if allowed is not None:
            self.can_access_lane = lambda lane_id: lane_id in allowed

    async def authorize(self, effect: Effect, **kw: Any) -> None:
        self.authorized.append((effect, kw.get("lane_id")))


async def test_lane_check_tool(home):
    from agentos.core.runtime import Runtime
    from agentos.stealth.tools import NEXT, tool_specs

    leaky(home.lane())
    home.lane("other/ig", cookies=False)
    home.store.set_quarantine("other/ig", QuarantineRecord(reason="exit IP changed", at=NOW))
    rt = Runtime(config=home.cfg, lane_check=home.checker())
    (spec,) = tool_specs(rt)
    assert spec.effect == Effect.READ and spec.hidden_without == "lane_check"
    assert "stop" in spec.description.lower() and "do not retry" in spec.description
    ctx = Ctx(rt)
    one = await spec.handler(ctx, on="biz/instagram")
    assert one["lane"] == "biz/instagram" and one["next"] == NEXT["warn"]
    assert ctx.authorized == [(Effect.READ, "biz/instagram")]
    scoped = await spec.handler(Ctx(rt, allowed={"biz/instagram"}))
    assert [r["lane"] for r in scoped["lanes"]] == ["biz/instagram"] and scoped["failing"] == []
    every = await spec.handler(Ctx(rt))
    assert every["failing"] == ["other/ig"] and every["next"] == NEXT["fail"]
    assert next(r for r in every["lanes"] if r["lane"] == "other/ig")["next"].startswith("Stop.")


# ---- the panel routes --------------------------------------------------------------------------

def test_panel_routes(home):
    from starlette.testclient import TestClient

    from agentos.api import ApiSettings, PanelStore, build_api
    from agentos.core.runtime import Runtime

    leaky(home.lane())
    home.lane("biz/ig", cookies=False)
    checker = home.checker()
    rt = Runtime(config=home.cfg, lane_check=checker)
    client = TestClient(build_api(rt, ApiSettings(store=PanelStore(None), owner_token="owner")))
    h = {"Authorization": "Bearer owner"}
    listed = client.get("/api/lanes/checks", headers=h).json()["lanes"]
    assert [(r["lane"], r["level"]) for r in listed] == [("biz/ig", "unknown"), ("biz/instagram", "unknown")]
    one = client.get("/api/lanes/biz/instagram/check", headers=h)
    assert one.status_code == 200 and one.json()["level"] == "warn" and len(one.json()["checks"]) == 9
    listed = {r["lane"]: r for r in client.get("/api/lanes/checks", headers=h).json()["lanes"]}
    assert listed["biz/instagram"]["level"] == "warn" and listed["biz/instagram"]["warning"] == ["webrtc"]
    fresh = {r["lane"]: r for r in client.get("/api/lanes/checks?fresh=1", headers=h).json()["lanes"]}
    assert fresh["biz/ig"]["level"] == "fail" and fresh["biz/ig"]["failing"] == ["session"]
    assert client.get("/api/lanes/biz/nope/check", headers=h).status_code == 404
    assert client.get("/api/lanes/biz/instagram/check").status_code == 401


# ---- the CLI ---------------------------------------------------------------------------------------

def test_cli_offline_exit_codes(home, monkeypatch):
    from agentos.cli import app

    home.lane()
    monkeypatch.setenv("AGENTOS_HOME", str(home.cfg.home))
    runner = CliRunner()
    ok = runner.invoke(app, ["lane", "check", "biz/instagram", "--offline"])
    assert ok.exit_code == 0, ok.output
    assert "biz/instagram: OK" in ok.output and "Signed in: Signed in to Instagram" in ok.output
    assert "--offline: every lane was read from disk" in ok.output and "\u2014" not in ok.output
    home.lane("biz/ig", cookies=False)
    every = runner.invoke(app, ["lane", "check", "--all", "--offline", "--json"])
    assert every.exit_code == 1
    data = json.loads(every.output)
    assert data["ok"] is False and [r["lane"] for r in data["lanes"]] == ["biz/ig", "biz/instagram"]
    assert SESSION not in every.output and PROXY_PASS not in every.output
    assert not (home.cfg.home / "cache" / "exit-geo.json").exists()  # the CLI writes no cache
    assert runner.invoke(app, ["lane", "check"]).exit_code == 1
    assert "no lane biz/none" in runner.invoke(app, ["lane", "check", "biz/none", "--offline"]).output


def test_report_round_trips_through_json(home):
    home.lane()
    report = asyncio.run(home.checker().check("biz/instagram"))
    again = lc.LaneReport.from_dict(json.loads(json.dumps(report.to_dict())))
    assert again.to_dict() == report.to_dict()


def test_store_facts_reads_an_old_schema(tmp_path):
    cfg = make_home(tmp_path)  # a lanes table without control, quarantine or signal columns
    source = lc.StoreFacts(cfg.db_path)
    assert source.lane_ids() == ["personal/reddit", "work/site"]
    facts = source.facts("personal/reddit")
    assert facts.state == "cold" and facts.quarantine is None and source.pinned_exit("lt1") is None


def test_the_lane_check_reports_when_the_session_cookie_expires():
    from agentos.node import sites

    ig = sites.site("instagram")
    assert lc.session_expiry(ig, [{"name": "sessionid", "value": "x", "expires": 1_800_000_000}]) \
        == "2027-01-15T08:00:00+00:00"
    assert lc.session_expiry(ig, [{"name": "sessionid", "value": "x", "expires": -1}]) is None
    assert lc.session_expiry(ig, []) is None


async def test_the_session_check_says_when_the_cookie_expires(home):
    home.lane(account=AccountRef(site="instagram", handle="@acme", user_id=USER_ID))
    report = await home.checker().check("biz/instagram")
    site = by_id(report)["session"].data["sites"]["instagram"]
    assert site["signed_in"] and site["expires_at"] is not None
    assert datetime.fromisoformat(site["expires_at"]) > datetime.now(UTC)
