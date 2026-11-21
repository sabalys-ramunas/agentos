"""QA bug hunt: the lane check (openajar.stealth.lanecheck).

Every test here runs fully offline: a fake Host answers the geo lookup, the proxy probe and the node's
own IP (the autouse fixture below makes any real socket or subprocess raise, so a test cannot reach the
network by accident), the clock is a list the test moves, and every file lives under tmp_path.

A test marked xfail(strict=True) asserts the CORRECT behaviour and fails today; when the bug is fixed
the strict xfail turns red and the marker comes off. Unmarked tests pin behaviour that held."""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from test_ops_support import FakeHost, fingerprint, make_home

from openajar.core.models import (
    AccountRef,
    ControlState,
    LaneControl,
    LaneInfo,
    LaneSpec,
    LaneState,
    ProxyConfig,
)
from openajar.node.store import LaneStore
from openajar.stealth import lanecheck as lc
from openajar.stealth import lanefacts as lc_facts
from openajar.stealth import lanereport as lc_report
from openajar.stealth import lanerules as lc_rules
from openajar.stealth.lanecheck import FAIL, OK, UNKNOWN, WARN, LaneChecker, LaneFacts

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
CFG = """// camoufox.cfg as shipped (the parts the check reads)
defaultPref("media.peerconnection.ice.no_host", false);
defaultPref("media.peerconnection.ice.proxy_only_if_behind_proxy", true);
defaultPref("media.peerconnection.ice.obfuscate_host_addresses", true);
defaultPref("network.proxy.socks_remote_dns", true);
"""
GATEWAY = ProxyConfig(label="gw", host="gate.example.net", port=12323, username="gw-user-SECRET",
                      password="gw-pass-SECRET")
NODE_IP = "192.0.2.10"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """The lane check does geo lookups over the network in production. In these tests nothing may:
    a real socket connect, DNS lookup or subprocess raises instead of running."""
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the lane check reached the network or started a program in a unit test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


class Rig:
    """An OPENAJAR_HOME with a real lane store, a fake host where everything is healthy, and a clock."""

    def __init__(self, tmp_path: Path):
        self.cfg = make_home(tmp_path, lanes={})
        self.store = LaneStore(self.cfg.db_path)
        self.host = FakeHost()
        browser = tmp_path / "camoufox"
        browser.mkdir()
        (browser / "camoufox.cfg").write_text(CFG)
        self.host.camoufox_info = {"path": str(browser), "verstr": "158.0.1-beta.41"}
        self.clock = [NOW.timestamp()]
        self.proxies: dict[str, ProxyConfig] = {"gw": GATEWAY}

    def lane(self, lane_id: str = "biz/instagram", *, proxy: str | None = "gw", locale: str = "en",
             account: AccountRef | None = None, pin: dict | None = None, cookies: bool = True) -> Path:
        self.store.add(LaneSpec(id=lane_id, proxy_label=proxy, locale=locale, account=account))
        lane_dir = self.cfg.lane_dir(lane_id)
        (lane_dir / "profile").mkdir(parents=True, exist_ok=True)
        if pin is None:
            pin = {**fingerprint(158), lc.PIN_META: {"version": 2}}
        if pin:
            (lane_dir / "fingerprint.json").write_text(json.dumps(pin))
        if cookies:
            cookie_db(lane_dir / "profile", [("sessionid", "SESSION-SECRET-VALUE", ".instagram.com",
                                              time.time() + 86400 * 30),
                                             ("ds_user_id", "4242", ".instagram.com", time.time() + 86400 * 30)])
        return lane_dir / "profile"

    def checker(self, **kw: Any) -> LaneChecker:
        kw.setdefault("host", self.host)
        kw.setdefault("proxies_loader", lambda: dict(self.proxies))
        return LaneChecker(self.cfg, clock=lambda: self.clock[0], **kw)

    def exit_of(self, ip: str, geo: dict[str, str] | None = None) -> None:
        """What the gateway exits from, and what ipinfo says about it."""
        self.host.exit_ips[GATEWAY.host] = ip
        if geo is not None:
            self.host.exit_geo[GATEWAY.host] = geo


def cookie_db(profile: Path, cookies: list[tuple[str, str, str, float]]) -> None:
    """A cookies.sqlite with Firefox's moz_cookies columns."""
    profile.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(profile / "cookies.sqlite", isolation_level=None)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT NOT "
                     "NULL DEFAULT '', name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, "
                     "lastAccessed INTEGER, creationTime INTEGER, isSecure INTEGER, isHttpOnly INTEGER)")
        for name, value, host, expiry in cookies:
            conn.execute("INSERT INTO moz_cookies (name, value, host, path, expiry) VALUES (?, ?, ?, '/', ?)",
                         (name, value, host, int(expiry)))
    finally:
        conn.close()


def by_id(report: lc_report.LaneReport) -> dict[str, lc_report.Check]:
    return {c.id: c for c in report.checks}


class StateManager:
    """The parts of the lane manager the checker reads, with a lane state the test picks."""

    def __init__(self, rig: Rig, state: LaneState):
        self.store = rig.store
        self.state = state
        self.engine = None
        self._lanes: dict[str, Any] = {}

    def list(self) -> list[LaneInfo]:
        return [self.get(rec.spec.id) for rec in self.store.list()]

    def get(self, lane_id: str) -> LaneInfo:
        rec = self.store.require(lane_id)
        return LaneInfo(spec=rec.spec, state=self.state, control=rec.control or LaneControl())


def test_the_offline_guard_stops_the_real_host():
    """Held: if a check ever used the real Host, the first socket would raise instead of connecting."""
    from openajar.core.system import Host

    with pytest.raises(AssertionError, match="reached the network"):
        Host().tcp_connect("203.0.113.7", 80, timeout=0.1)
    with pytest.raises(AssertionError, match="reached the network"):
        Host().proxy_exit_info(GATEWAY, timeout=0.1)
    with pytest.raises(AssertionError, match="reached the network"):
        Host().camoufox()


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)


# ---- the location verdict -----------------------------------------------------------------------

@pytest.mark.parametrize(("exit_tz", "pinned_tz"), [(None, "America/New_York"), ("Europe/Vilnius", "Mars/Olympus")])
def test_location_does_not_claim_a_match_it_never_compared(exit_tz, pinned_tz):
    geo = {"ip": "203.0.113.7", "country": "LT" if exit_tz else "US", **({"timezone": exit_tz} if exit_tz else {})}
    check = lc_rules.judge_location(geo, pinned_tz=pinned_tz, pinned_country=None, locale="en", at=NOW)
    assert "same offset" not in check.detail and "same timezone" not in check.detail, check.detail
    assert check.level in (UNKNOWN, WARN)


def test_location_flags_a_timezone_one_hour_off_and_a_country_one_letter_off():
    """Held: a pin in Vilnius and an exit in Berlin differ by an hour and are caught; so is a country that
    differs from the pinned one by one letter (LT against LV)."""
    far = lc_rules.judge_location({"ip": "x", "country": "DE", "timezone": "Europe/Berlin"}, pinned_tz="Europe/Vilnius",
                            pinned_country=None, locale="en", at=NOW)
    assert far.level == WARN and "UTC+02:00" in far.detail
    one_letter = lc_rules.judge_location({"ip": "x", "country": "LV", "timezone": "Europe/Riga"},
                                   pinned_tz="Europe/Vilnius", pinned_country="LT", locale="lt-LT", at=NOW)
    assert one_letter.level == WARN and "the pin was made in LT" in one_letter.detail
    gb = lc_rules.judge_location({"ip": "x", "country": "GB", "timezone": "Europe/London"}, pinned_tz="Europe/London",
                           pinned_country="GB", locale="en-GB", at=NOW)
    assert gb.level == OK
    assert lc_rules.judge_location({"ip": "x", "country": "IE", "timezone": "Europe/Dublin"}, pinned_tz="Europe/London",
                             pinned_country="GB", locale="en-GB", at=NOW).level == WARN


def test_offset_is_right_on_the_days_the_clocks_change():
    """Held: the offset is read at the instant, so the 23 and 25 hour days give the right pair."""
    before = datetime(2026, 3, 29, 0, 59, tzinfo=UTC)   # Vilnius: 02:59 EET
    after = datetime(2026, 3, 29, 1, 1, tzinfo=UTC)     # Vilnius: 04:01 EEST
    assert lc_rules.utc_offset("Europe/Vilnius", before) == "UTC+02:00"
    assert lc_rules.utc_offset("Europe/Vilnius", after) == "UTC+03:00"
    assert lc_rules.utc_offset("America/St_Johns", NOW) == "UTC-03:30"
    assert lc_rules.utc_offset("Australia/Lord_Howe", datetime(2026, 1, 1, tzinfo=UTC)) == "UTC+11:00"


# ---- the geo cache ------------------------------------------------------------------------------

async def test_one_lookup_per_proxy_even_when_many_checks_race(rig):
    """Held: eight lanes on one proxy, checked at once, ask ipinfo once; a failed lookup is not retried
    for a day; nothing leaves the fake host."""
    for i in range(8):
        rig.lane(f"biz/ig{i}", cookies=False)
    rig.exit_of("198.51.100.1", {"country": "LT", "timezone": "Europe/Vilnius"})
    checker = rig.checker()
    await checker.check_all()
    assert rig.host.exit_info_calls == ["gw"] and rig.host.http_calls == []
    cache_text = checker.geo_path.read_text()          # keyed by label; holds no proxy credential
    assert list(json.loads(cache_text)) == ["gw"] and GATEWAY.username not in cache_text
    assert GATEWAY.password not in cache_text
    rig.clock[0] += 3600
    rig.host.exit_geo[GATEWAY.host] = OSError("HTTP 429 from ipinfo.io")
    await rig.checker().check_all()
    assert rig.host.exit_info_calls == ["gw"]


async def test_a_failed_lookup_is_not_retried_in_a_loop(rig):
    rig.lane(cookies=False)
    rig.exit_of("198.51.100.1")
    rig.host.exit_geo[GATEWAY.host] = OSError("timed out")
    checker = rig.checker()
    for _ in range(5):
        rig.clock[0] += 120   # past the 60 s proxy-probe reuse each time
        report = await checker.check("biz/instagram")
    assert rig.host.exit_info_calls == ["gw"]
    assert by_id(report)["location"].level == UNKNOWN


async def test_a_new_lookup_replaces_the_old_answer_whole(rig):
    rig.lane(pin={**fingerprint(158, timezone="Europe/Berlin"), lc.PIN_META: {"version": 2}})
    rig.exit_of("198.51.100.1", {"country": "DE", "region": "Berlin", "city": "Berlin", "timezone": "Europe/Berlin"})
    checker = rig.checker()
    await checker.check("biz/instagram")
    rig.clock[0] += lc.GEO_TTL + 60
    rig.exit_of("198.51.100.2", {"country": "LT"})   # ipinfo gave only the keys it knew
    loc = by_id(await checker.check("biz/instagram"))["location"]
    cached = json.loads(checker.geo_path.read_text())["gw"]
    assert cached["ip"] == "198.51.100.2" and cached["country"] == "LT"
    assert "timezone" not in cached and "city" not in cached, cached
    assert loc.data["exit_timezone"] != "Europe/Berlin", loc.detail


async def test_a_future_timestamp_in_the_geo_cache_does_not_block_lookups(rig):
    """A VM that booted with a wrong clock wrote `tried` ten days ahead; the clock is right now."""
    rig.lane(cookies=False)
    rig.exit_of("198.51.100.2", {"country": "LT", "timezone": "Europe/Vilnius"})
    stamp = NOW.timestamp() + 10 * 86400
    cache = rig.cfg.home / "cache" / "exit-geo.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"gw": {"ip": "198.51.100.1", "country": "DE", "timezone": "Europe/Berlin",
                                        "tried": stamp, "at": stamp}}))
    loc = by_id(await rig.checker().check("biz/instagram"))["location"]
    assert rig.host.exit_info_calls == ["gw"], loc.detail


async def test_a_clock_stepped_back_does_not_freeze_the_proxy_probe(rig):
    rig.lane(cookies=False)
    rig.exit_of("198.51.100.1", {"country": "LT", "timezone": "Europe/Vilnius"})
    checker = rig.checker()
    first = by_id(await checker.check("biz/instagram"))["exit_ip"]
    assert first.data["exit_ip"] == "198.51.100.1"
    rig.clock[0] -= 3600          # NTP corrects a clock that ran an hour fast
    rig.exit_of("198.51.100.9")   # and the proxy now exits elsewhere
    second = by_id(await checker.check("biz/instagram"))["exit_ip"]
    assert second.data["exit_ip"] == "198.51.100.9", "the probe from an hour 'ahead' is still being served"


# ---- exit IP: what a missing fact must not turn into ---------------------------------------------

async def test_an_unknown_node_ip_is_not_a_pass(rig):
    rig.lane(cookies=False)
    rig.exit_of(NODE_IP)                          # the gateway fails open: traffic leaves from the node
    rig.host.node_ip = OSError("HTTP 429 from ipinfo.io")
    exit_ip = by_id(await rig.checker().check("biz/instagram"))["exit_ip"]
    assert exit_ip.level != OK, exit_ip.detail


async def test_a_failed_node_ip_read_is_retried_soon(rig):
    rig.lane(cookies=False)
    rig.exit_of(NODE_IP)
    rig.host.node_ip = OSError("timed out")
    checker = rig.checker()
    await checker.check("biz/instagram")
    rig.clock[0] += 120                           # the proxy probe (60 s) is stale, the node IP is readable again
    rig.host.node_ip = NODE_IP
    exit_ip = by_id(await checker.check("biz/instagram"))["exit_ip"]
    assert exit_ip.level == FAIL, exit_ip.detail


async def test_an_unreadable_exit_pin_is_not_a_pass(rig, monkeypatch):
    """agentosd is down (the CLI reads openajar.db itself). The pin says the gateway exits from .1; it now
    exits from .9. If the pin cannot be read, the check must not say OK."""
    rig.lane(cookies=False)
    rig.store.pin_proxy_exit("gw", "198.51.100.1")
    rig.exit_of("198.51.100.9", {"country": "LT", "timezone": "Europe/Vilnius"})
    calls = []
    real = lc_facts.connect_ro

    def flaky(path, timeout=5.0):
        calls.append(path)
        if len(calls) >= 2:       # the lane's facts were read; the pin read finds the database busy
            raise sqlite3.OperationalError("database is locked")
        return real(path, timeout)

    monkeypatch.setattr(lc_facts, "connect_ro", flaky)
    exit_ip = by_id(await rig.checker().check("biz/instagram"))["exit_ip"]
    assert len(calls) >= 2
    assert exit_ip.level != OK, exit_ip.detail


async def test_a_moved_gateway_exit_fails_against_its_pin(rig):
    """Held: with a readable pin, a different exit is a FAIL that names both addresses."""
    rig.lane(cookies=False)
    rig.store.pin_proxy_exit("gw", "198.51.100.1")
    rig.exit_of("198.51.100.9")
    exit_ip = by_id(await rig.checker().check("biz/instagram"))["exit_ip"]
    assert exit_ip.level == FAIL and "198.51.100.1" in exit_ip.detail and "198.51.100.9" in exit_ip.detail


# ---- messages that say something else than what happened -----------------------------------------------

async def test_offline_mode_does_not_blame_the_proxy(rig):
    rig.lane(cookies=False)
    checks = by_id(await rig.checker(network=False).check("biz/instagram"))
    assert "not probed" in checks["proxy"].detail
    for check_id in ("exit_ip", "location"):
        assert "did not answer" not in checks[check_id].detail, checks[check_id].detail


async def test_a_corrupt_cookie_file_is_not_blamed_on_a_busy_browser(rig, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    profile = rig.lane(account=AccountRef(site="instagram", user_id="4242"), cookies=False)
    (profile / "cookies.sqlite").write_bytes(b"this is not a database at all" * 20)
    session = by_id(await rig.checker().check("biz/instagram"))["session"]
    assert session.level == UNKNOWN
    assert "while the browser wrote it" not in session.detail, session.detail


async def test_a_missing_proxy_is_not_described_as_no_proxy(rig):
    rig.lane(proxy="nope", cookies=False)
    checks = by_id(await rig.checker().check("biz/instagram"))
    assert checks["proxy"].level == FAIL and "no proxy nope" in checks["proxy"].detail
    for check_id in ("webrtc", "dns"):
        assert not checks[check_id].detail.startswith("No proxy"), checks[check_id].detail


# ---- holds --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ended_ago", [timedelta(minutes=1), timedelta(0)])
def test_an_ended_timed_hold_is_not_a_hold_for_a_human(ended_ago):
    hold = LaneControl(state=ControlState.PAUSED, by="node", since=NOW - timedelta(hours=1), reason="rate_limited",
                       hold_until=NOW - ended_ago)
    check = lc_rules.judge_hold(LaneFacts(LaneSpec(id="biz/instagram"), control=hold), now=NOW)
    assert check.level != FAIL and check.data.get("cleared_by") != "human", check.detail


def test_a_time_in_another_zone_is_converted_to_utc():
    vilnius = ZoneInfo("Europe/Vilnius")
    at = datetime(2026, 3, 29, 4, 30, tzinfo=vilnius)    # the day the clocks change: 04:30 EEST is 01:30 UTC
    assert at.astimezone(UTC).strftime("%H:%M") == "01:30"
    q = lc_rules.judge_hold(LaneFacts(LaneSpec(id="biz/instagram"), quarantine={"reason": "x", "at": at, "by": "node",
                                                                          "kind": "manual"}), now=NOW)
    assert "2026-03-29 01:30 UTC" in q.detail, q.detail


# ---- the profile check --------------------------------------------------------------------------------

async def test_a_lane_that_is_still_warming_its_first_launch_has_no_pin_yet(rig):
    """The engine writes the pin after the browser started (settle_pin). Until then the profile already
    has cookies.sqlite and times.json, and the state is 'warming'."""
    rig.lane(pin=False)
    manager = StateManager(rig, LaneState.WARMING)
    profile = by_id(await rig.checker(lanes=manager).check("biz/instagram"))["profile"]
    assert profile.level != FAIL, profile.detail


async def test_a_cold_used_lane_without_a_pin_still_fails(rig):
    """Held: the same profile when the lane is cold is a real FAIL."""
    rig.lane(pin=False)
    profile = by_id(await rig.checker().check("biz/instagram"))["profile"]
    assert profile.level == FAIL and "pin file is gone" in profile.detail


# ---- the report as a whole ----------------------------------------------------------------------------

async def test_a_crashed_check_is_not_safe_to_use(rig):
    rig.lane(cookies=False)

    def boom(path: Path) -> Any:
        raise RuntimeError("disk probe exploded")

    rig.host.disk_usage = boom
    (report,) = await rig.checker().check_all()
    assert "check itself failed" in by_id(report)["profile"].detail
    assert report.level != OK and "every check passed" not in report.summary(), report.summary()


def test_the_summary_does_not_say_every_check_passed_when_some_could_not_run():
    report = lc_report.LaneReport("biz/instagram", "cold", NOW, [lc_report.Check("proxy", OK, "Proxy gw answers."),
                                                         lc_report.Check("session", UNKNOWN, "The session cookies could "
                                                                  "not be read.")])
    assert "every check passed" not in report.summary()


async def test_the_registered_checker_knows_the_lane_manager_before_the_first_round(rig):
    """The tool and the panel route call the registered LaneChecker directly. HealthService hands it the
    lane manager only when a round or its own lane_check() starts (first_delay, 30 s by default)."""
    from openajar.core.runtime import Runtime
    from openajar.ops.service import HealthService

    profile = rig.lane(proxy="lt1")
    rig.host.browsers[str(profile)] = [77]          # the lane's own browser
    runtime = Runtime(config=rig.cfg, lanes=StateManager(rig, LaneState.WARM))
    service = HealthService(runtime, control=False, first_delay=3600, host=rig.host)
    runtime.register("lane_check", service.checker)
    report = await runtime.require("lane_check").check("biz/instagram")
    assert report.state == "warm", report.state
    assert "not running" not in by_id(report)["profile"].detail, by_id(report)["profile"].detail


# ---- the account check and the report's data -----------------------------------------------------------

async def test_an_odd_account_cookie_does_not_abort_the_check(rig):
    profile = rig.lane("biz/reddit", account=AccountRef(site="reddit", user_id="t2_abc"), cookies=False)
    cookie_db(profile, [("reddit_session", "²4242,1700000000,hash", ".reddit.com", time.time() + 86400)])
    error = None
    try:
        report = await rig.checker().check("biz/reddit")
    except ValueError as e:
        error = e
    assert error is None, f"check() raised: {error}"
    assert by_id(report)["account"].level == UNKNOWN


async def test_cookie_names_that_carry_the_user_id_stay_out_of_the_report(rig):
    profile = rig.lane("biz/wordpress", account=AccountRef(site="wordpress", user_id="login"), cookies=False)
    later = time.time() + 86400 * 30
    cookie_db(profile, [("wordpress_logged_in_0a1b2c", "login|1999999999|tok|mac", ".wordpress.com", later),
                        ("wp-settings-9917", "editor=html", ".wordpress.com", later),
                        ("wp-settings-time-9917", "1700000000", ".wordpress.com", later)])
    report = await rig.checker().check("biz/wordpress")
    assert by_id(report)["session"].level == OK
    assert "9917" not in json.dumps(report.to_dict())


async def test_no_secret_reaches_the_report_or_the_log(rig, caplog):
    """Held: cookie values, the account id, proxy credentials and the node token stay out of the report,
    its text form and the log lines, also when the proxy fails with an error that echoes them."""
    caplog.set_level("DEBUG")
    rig.lane(account=AccountRef(site="instagram", handle="@acme", user_id="9999"))
    rig.exit_of("198.51.100.1")
    rig.host.exit_geo[GATEWAY.host] = OSError(f"407 for {GATEWAY.username}:{GATEWAY.password}")
    report = await rig.checker().check("biz/instagram")
    rig.host.tcp_errors[GATEWAY.host] = OSError(f"refused for {GATEWAY.username}:{GATEWAY.password}")
    down = await rig.checker().check("biz/instagram")
    assert by_id(down)["proxy"].level == FAIL
    text = "".join(json.dumps(r.to_dict()) + lc_report.format_report(r) + repr(r) for r in (report, down)) + caplog.text
    for secret in ("SESSION-SECRET-VALUE", "4242", "9999", GATEWAY.username, GATEWAY.password):
        assert secret not in text, secret


async def test_the_check_writes_nothing_into_the_profile(rig):
    """Held: file names, sizes and mtimes of the whole lane folder are the same after a check, for a cold
    lane with a WAL cookie database."""
    profile = rig.lane(account=AccountRef(site="instagram", user_id="4242"))
    conn = sqlite3.connect(profile / "cookies.sqlite")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO moz_cookies (name, value, host, path, expiry) VALUES ('x', 'y', '.instagram.com', "
                 "'/', ?)", (int(time.time()) + 600,))
    conn.commit()                              # the writer stays open: the browser holds the file
    (profile / "prefs.js").write_text('user_pref("a.b", 1);\n')
    lane_dir = rig.cfg.lane_dir("biz/instagram")

    def snapshot() -> dict[str, tuple[int, int]]:
        return {str(p.relative_to(lane_dir)): (p.stat().st_size, p.stat().st_mtime_ns)
                for p in sorted(lane_dir.rglob("*")) if p.is_file()}

    before = snapshot()
    report = await rig.checker().check("biz/instagram")
    after = snapshot()                         # while the writer is still open (closing it would checkpoint)
    conn.close()
    assert by_id(report)["session"].level == OK
    assert after == before
    assert not list(rig.cfg.home.rglob("openajar-cookies-*"))


async def test_reading_launch_prefs_from_the_real_engine_writes_no_file(rig):
    """Held: the check asks the real engine for its network prefs; for an unproxied lane the engine would
    write network-guard.pac next to the profile, and the check must never get there."""
    from openajar.node.camoufox.engine import CamoufoxEngine

    rig.lane(cookies=False)
    rig.lane("biz/direct", proxy=None, cookies=False)
    manager = StateManager(rig, LaneState.COLD)
    manager.engine = CamoufoxEngine()
    lanes = rig.cfg.home / "lanes"
    before = sorted(str(p.relative_to(rig.cfg.home)) for p in lanes.rglob("*"))
    checker = rig.checker(lanes=manager)
    for lane_id in ("biz/instagram", "biz/direct"):
        await checker.check(lane_id)
    assert sorted(str(p.relative_to(rig.cfg.home)) for p in lanes.rglob("*")) == before
    assert not list(lanes.rglob("network-guard.pac"))


async def test_proxy_error_text_keeps_no_piece_of_the_password(rig):
    rig.proxies["gw"] = ProxyConfig(label="gw", host=GATEWAY.host, port=1, username="bob", password="bobsecret99")
    rig.lane(cookies=False)
    rig.host.tcp_errors[GATEWAY.host] = OSError("proxy refused bob:bobsecret99")
    proxy = by_id(await rig.checker().check("biz/instagram"))["proxy"]
    assert proxy.level == FAIL
    assert "secret99" not in proxy.detail, proxy.detail


# ---- prefs --------------------------------------------------------------------------------------------

async def test_a_commented_out_pref_is_not_read_as_set(rig):
    profile = rig.lane(cookies=False)
    (profile / "user.js").write_text('/* kept for later\nuser_pref("media.peerconnection.enabled", false);\n*/\n')
    assert lc_rules.parse_prefs((profile / "user.js").read_text()) == {}
    webrtc = by_id(await rig.checker().check("biz/instagram"))["webrtc"]
    # Not "WebRTC is off": the commented-out pref is not set. The engine's proxy_only (set on every
    # proxied lane since the WebRTC leak fix) is what makes it ok.
    assert webrtc.data["media.peerconnection.enabled"] is not False, webrtc.data
    assert webrtc.detail != "WebRTC is off.", webrtc.detail
