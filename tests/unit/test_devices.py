"""Devices (AUD-02): one pinned fingerprint plus one proxy shared by several lanes.

Pin resolution (a lane on a device launches with the device's pin, a lane alone with its own),
the rule that a logged-in lane never changes fingerprint silently (moves warn and need a yes), a
device made from a lane's pin, and that two lanes on one device get the same fingerprint but
their own profile, display and scope."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_lanes import PROXY, FakeEngine, make

from agentos.core.errors import ConfirmRequired, InvalidRequest, NotFound
from agentos.core.models import DeviceSpec, LaneSpec, ProxyConfig

LT2 = ProxyConfig(label="lt2", host="198.51.100.20", port=12323, username="u", password="p")
PROXIES = {"lt1": PROXY, "lt2": LT2}


def device_mgr(tmp_path: Path, **kw):
    return make(tmp_path, proxies=PROXIES, self_check=False, **kw)


class PinningEngine(FakeEngine):
    """Pins a new fingerprint (a counter) on a first launch, as the real engine does."""

    def __init__(self):
        super().__init__()
        self.drawn = 0
        self.pins: list[tuple[str, Path, Path]] = []  # lane, profile dir, pin path

    async def launch(self, spec, *, profile_dir, fingerprint_path, display, xauthority=None, proxy):
        if not fingerprint_path.exists():
            self.drawn += 1
            fingerprint_path.parent.mkdir(parents=True, exist_ok=True)
            fingerprint_path.write_text(json.dumps({"navigator.platform": "Linux x86_64", "draw": self.drawn,
                                                    "screen.width": 1280 + self.drawn * 100,
                                                    "timezone": "Europe/Vilnius"}))
        self.pins.append((spec.id, profile_dir, fingerprint_path))
        return await super().launch(spec, profile_dir=profile_dir, fingerprint_path=fingerprint_path,
                                    display=display, xauthority=xauthority, proxy=proxy)


def pin_of(mgr, lane_id: str) -> dict:
    return json.loads(mgr.config.fingerprint_path(lane_id).read_text())


# ---- create, list, remove ----------------------------------------------------------------


async def test_a_device_names_a_proxy_and_lanes_on_it_take_that_proxy(tmp_path):
    mgr, *_ = device_mgr(tmp_path)
    info = await mgr.create_device("biz-a-pc", proxy_label="lt1")
    assert info["name"] == "biz-a-pc" and info["proxy"] == "lt1" and not info["pinned"] and info["lanes"] == []
    lane = await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    assert lane.spec.device == "biz-a-pc" and lane.spec.proxy_label == "lt1"
    with pytest.raises(InvalidRequest, match="uses the device's proxy"):
        await mgr.create(LaneSpec(id="biz-a/facebook", device="biz-a-pc", proxy_label="lt2"))
    with pytest.raises(NotFound, match="no device named"):
        await mgr.create(LaneSpec(id="biz-a/reddit", device="nope"))
    with pytest.raises(InvalidRequest, match="unknown proxy"):
        await mgr.create_device("biz-b-pc", proxy_label="nope")
    with pytest.raises(InvalidRequest, match="already exists"):
        await mgr.create_device("biz-a-pc", proxy_label="lt1")
    assert [d["name"] for d in mgr.devices()] == ["biz-a-pc"]
    assert mgr.devices()[0]["lanes"][0]["id"] == "biz-a/instagram"


async def test_a_device_in_use_is_not_removed_and_an_unused_one_goes_to_the_trash(tmp_path):
    mgr, *_ = device_mgr(tmp_path)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    with pytest.raises(InvalidRequest, match="used by biz-a/instagram"):
        await mgr.remove_device("biz-a-pc")
    await mgr.delete("biz-a/instagram")
    moved = await mgr.remove_device("biz-a-pc")
    assert moved is not None and moved.parent == mgr.trash_dir
    assert mgr.devices() == []


def test_device_names_are_checked():
    with pytest.raises(ValueError):
        DeviceSpec(name="Biz A")
    with pytest.raises(ValueError):
        LaneSpec(id="a/b", device="../x")


# ---- pin resolution ----------------------------------------------------------------------


async def test_lanes_on_one_device_launch_with_its_pin_and_keep_their_own_profiles(tmp_path):
    engine = PinningEngine()
    mgr, _engine, _displays, _clock = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    for name in ("instagram", "facebook"):
        await mgr.create(LaneSpec(id=f"biz-a/{name}", device="biz-a-pc"))
    await mgr.create(LaneSpec(id="personal/reddit", proxy_label="lt2"))
    for lane_id in ("biz-a/instagram", "biz-a/facebook", "personal/reddit"):
        await mgr.open(lane_id)

    by_lane = {lane: (profile, pin) for lane, profile, pin in engine.pins}
    device_pin = mgr.config.device_pin_path("biz-a-pc")
    assert by_lane["biz-a/instagram"][1] == by_lane["biz-a/facebook"][1] == device_pin
    assert by_lane["personal/reddit"][1] == mgr.config.fingerprint_path("personal/reddit")
    assert by_lane["biz-a/instagram"][0] != by_lane["biz-a/facebook"][0]  # own profiles
    assert engine.drawn == 2  # one for the device, one for the personal lane
    # every lane on the device holds an exact copy, so a reader by lane id sees the device's pin
    assert pin_of(mgr, "biz-a/instagram") == pin_of(mgr, "biz-a/facebook") == json.loads(device_pin.read_text())
    assert pin_of(mgr, "personal/reddit")["draw"] != pin_of(mgr, "biz-a/instagram")["draw"]
    # separate browsers on separate displays, each through the device's proxy
    launches = {lane: (display, proxy) for lane, display, proxy in engine.launches}
    assert launches["biz-a/instagram"][0] != launches["biz-a/facebook"][0]
    assert launches["biz-a/instagram"][1].label == launches["biz-a/facebook"][1].label == "lt1"
    assert len({s.pid for s in engine.sessions}) == 3
    await mgr.shutdown()


async def test_each_lane_on_a_device_gets_its_own_scope(tmp_path):
    applied: list[tuple[str, list[int]]] = []

    class Limiter:
        async def apply(self, lane_id, pids):
            applied.append((lane_id, list(pids)))

    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    mgr.limiter = Limiter()
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    for name in ("instagram", "facebook"):
        await mgr.create(LaneSpec(id=f"biz-a/{name}", device="biz-a-pc"))
        await mgr.open(f"biz-a/{name}")
    assert [lane for lane, _ in applied] == ["biz-a/instagram", "biz-a/facebook"]
    assert applied[0][1] != applied[1][1]
    await mgr.shutdown()


async def test_a_lane_made_after_the_device_was_pinned_gets_the_copy_at_once(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    await mgr.open("biz-a/instagram")
    await mgr.create(LaneSpec(id="biz-a/reddit", device="biz-a-pc"))
    assert pin_of(mgr, "biz-a/reddit") == pin_of(mgr, "biz-a/instagram")  # the timezone reads the same at once
    await mgr.shutdown()


async def test_a_copy_that_drifted_is_repaired_at_start(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    await mgr.open("biz-a/instagram")
    await mgr.shutdown()
    mgr.config.fingerprint_path("biz-a/instagram").write_text("{}")
    again, *_ = device_mgr(tmp_path)
    await again.start()
    assert pin_of(again, "biz-a/instagram") == json.loads(again.config.device_pin_path("biz-a-pc").read_text())
    await again.shutdown()


async def test_a_lane_on_a_device_cannot_change_proxy_on_its_own(tmp_path):
    mgr, *_ = device_mgr(tmp_path)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    with pytest.raises(InvalidRequest, match="uses the device's proxy"):
        await mgr.set_proxy("biz-a/instagram", "lt2")


async def test_import_with_a_fingerprint_onto_a_device_is_refused(tmp_path):
    mgr, *_ = device_mgr(tmp_path)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    src = tmp_path / "src-profile"
    src.mkdir()
    fp = tmp_path / "fp.json"
    fp.write_text(json.dumps({"navigator.platform": "Win32"}))
    with pytest.raises(InvalidRequest, match="--fingerprint and --device"):
        await mgr.import_profile(LaneSpec(id="biz-a/instagram", device="biz-a-pc"), src, fp)


# ---- from a lane, and moves -------------------------------------------------------------


async def test_a_device_made_from_a_lane_keeps_that_lane_exactly_as_its_site_knows_it(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create(LaneSpec(id="biz-a/instagram", proxy_label="lt1"))
    await mgr.open("biz-a/instagram")
    await mgr.close("biz-a/instagram")
    before = mgr.config.fingerprint_path("biz-a/instagram").read_bytes()

    with pytest.raises(InvalidRequest, match="keeps that proxy"):
        await mgr.create_device("biz-a-pc", from_lane="biz-a/instagram", proxy_label="lt2")
    info = await mgr.create_device("biz-a-pc", from_lane="biz-a/instagram")
    assert info["proxy"] == "lt1" and info["pinned"] and info["from_lane"] == "biz-a/instagram"
    assert mgr.get("biz-a/instagram").spec.device == "biz-a-pc"
    assert mgr.config.device_pin_path("biz-a-pc").read_bytes() == before
    assert mgr.config.fingerprint_path("biz-a/instagram").read_bytes() == before  # no change for the lane

    await mgr.create(LaneSpec(id="biz-a/facebook", device="biz-a-pc"))
    await mgr.open("biz-a/facebook")
    await mgr.open("biz-a/instagram")
    assert engine.drawn == 1  # the device the site knows, for both
    assert pin_of(mgr, "biz-a/facebook") == pin_of(mgr, "biz-a/instagram")
    await mgr.shutdown()


async def test_a_lane_never_launched_cannot_lend_a_device(tmp_path):
    mgr, *_ = device_mgr(tmp_path)
    await mgr.create(LaneSpec(id="biz-a/instagram", proxy_label="lt1"))
    with pytest.raises(InvalidRequest, match="no pinned fingerprint yet"):
        await mgr.create_device("biz-a-pc", from_lane="biz-a/instagram")
    assert mgr.devices() == []


async def test_moving_a_logged_in_lane_onto_a_device_warns_and_needs_a_yes(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    await mgr.open("biz-a/instagram")
    await mgr.create(LaneSpec(id="biz-a/facebook", proxy_label="lt2"))
    await mgr.open("biz-a/facebook")
    await mgr.close("biz-a/facebook")
    own = mgr.config.fingerprint_path("biz-a/facebook").read_bytes()

    plan = mgr.plan_device_move("biz-a/facebook", "biz-a-pc")
    assert plan["confirm"] is True
    text = " ".join(plan["warnings"])
    assert "new computer" in text and "checkpoint" in text and "new IP" in text
    with pytest.raises(ConfirmRequired) as caught:
        await mgr.set_device("biz-a/facebook", "biz-a-pc")
    assert caught.value.details["confirm"] is True
    assert mgr.get("biz-a/facebook").spec.device is None  # nothing changed
    assert mgr.config.fingerprint_path("biz-a/facebook").read_bytes() == own

    done = await mgr.set_device("biz-a/facebook", "biz-a-pc", confirm=True)
    assert done["done"]
    spec = mgr.get("biz-a/facebook").spec
    assert spec.device == "biz-a-pc" and spec.proxy_label == "lt1"
    assert pin_of(mgr, "biz-a/facebook") == pin_of(mgr, "biz-a/instagram")
    backup = mgr.config.fingerprint_path("biz-a/facebook").with_name("fingerprint.json.before-device.bak")
    assert backup.read_bytes() == own  # the device it had, kept
    kinds = [e.kind for e in mgr.events("biz-a/facebook")]
    assert "device_changed" in kinds

    # and back off the device: its own earlier pin returns, after another yes
    with pytest.raises(ConfirmRequired, match="new computer"):
        await mgr.set_device("biz-a/facebook", None)
    await mgr.set_device("biz-a/facebook", None, confirm=True)
    assert mgr.get("biz-a/facebook").spec.device is None
    assert mgr.config.fingerprint_path("biz-a/facebook").read_bytes() == own
    await mgr.shutdown()


async def test_a_lane_that_never_launched_moves_without_a_warning(tmp_path):
    mgr, *_ = device_mgr(tmp_path)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/blog", proxy_label="lt2"))
    plan = mgr.plan_device_move("biz-a/blog", "biz-a-pc")
    assert plan["confirm"] is False and plan["warnings"] == [
        "biz-a/blog moves from proxy lt2 to lt1: its site sees a new IP address"]
    await mgr.set_device("biz-a/blog", "biz-a-pc")
    assert mgr.get("biz-a/blog").spec.proxy_label == "lt1"


async def test_a_pinned_lane_moving_onto_an_unpinned_device_on_its_proxy_lends_its_pin(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/instagram", proxy_label="lt1"))
    await mgr.open("biz-a/instagram")
    await mgr.close("biz-a/instagram")
    before = mgr.config.fingerprint_path("biz-a/instagram").read_bytes()
    plan = mgr.plan_device_move("biz-a/instagram", "biz-a-pc")
    assert plan["confirm"] is False and "stays the same computer" in plan["changes"][0]
    await mgr.set_device("biz-a/instagram", "biz-a-pc")
    assert mgr.config.device_pin_path("biz-a-pc").read_bytes() == before
    assert mgr.config.fingerprint_path("biz-a/instagram").read_bytes() == before
    await mgr.shutdown()


async def test_a_warm_lane_is_not_moved(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/blog", proxy_label="lt1"))
    await mgr.open("biz-a/blog")
    with pytest.raises(InvalidRequest, match="close it first"):
        await mgr.set_device("biz-a/blog", "biz-a-pc", confirm=True)
    await mgr.shutdown()


# ---- the engine: one device, two lanes ------------------------------------------------------


def test_two_lanes_on_one_device_launch_with_identical_fingerprints_and_their_own_profiles(tmp_path, monkeypatch):
    """The real engine's launch plan, with Camoufox's launch_options faked as test_lanes_pin does
    (a fresh draw on every call): the second lane on a device gets exactly the first one's
    fingerprint, prefs and canvas decision, with its own profile and display."""
    pytest.importorskip("camoufox.fingerprints")
    from test_lanes_pin import FakeCamoufox, FakeGeo

    from agentos.node import engine_camoufox as ec

    running: dict = {}
    monkeypatch.setattr(ec, "read_browser_config", lambda pid: running["config"])
    monkeypatch.setattr(ec, "_host_cores", lambda: 2)
    engine = ec.CamoufoxEngine(resolve=FakeCamoufox(seed=7), geo=FakeGeo(), identity_os=["linux"])
    device_pin = tmp_path / "devices" / "biz-a-pc" / "fingerprint.json"
    device_pin.parent.mkdir(parents=True)

    def launch(lane_id: str, display: str):
        spec = LaneSpec(id=lane_id, device="biz-a-pc", proxy_label="lt1")
        pin = ec.load_pin(device_pin, lane_id) if device_pin.exists() else None
        profile = tmp_path / "lanes" / lane_id / "profile"
        opts = engine.options(spec, profile_dir=profile, fingerprint_path=device_pin, display=display, proxy=PROXY)
        plan = engine.plan_launch(lane_id, opts, pin)
        running["config"] = ec.config_of_env(plan.options["env"])
        engine.settle_pin(lane_id, device_pin, plan, pid=4242)
        return plan, opts

    first, first_opts = launch("biz-a/instagram", ":150")
    second, second_opts = launch("biz-a/facebook", ":151")
    assert first.first and not second.first
    assert first.config == second.config  # one machine: screen, fonts, GPU, timezone, locale, version
    for key in ("screen.width", "window.screenX", "fonts", "webGl:renderer", "timezone", "locale:language",
                "navigator.userAgent"):
        assert first.config.get(key) == second.config.get(key)
    assert first.options["firefox_user_prefs"] == second.options["firefox_user_prefs"]
    assert ec.window_position_of(first.config) == ec.window_position_of(second.config)
    assert first_opts["user_data_dir"] != second_opts["user_data_dir"]
    assert first_opts["env"]["DISPLAY"] != second_opts["env"]["DISPLAY"]
    assert second_opts["proxy"] == first_opts["proxy"]


# ---- the audit: leaks across devices --------------------------------------------------------


def _levels(findings, check="device"):
    return [(f.level, f.detail) for f in findings if f.check == check]


def test_the_audit_flags_two_companies_on_one_device_and_a_lane_on_another_exit():
    from agentos.stealth.stealth import device_findings

    found = device_findings({"biz-a-pc": "lt1"},
                            {"biz-a/instagram": ("biz-a-pc", "lt1"), "biz-b/instagram": ("biz-a-pc", "lt1"),
                             "biz-a/facebook": ("biz-a-pc", "lt2")}, {})
    details = " ".join(d for level, d in _levels(found) if level == "fail")
    assert "2 workspaces (biz-a, biz-b)" in details
    assert "biz-a/facebook on device biz-a-pc leave through another proxy" in details


def test_the_audit_flags_two_devices_on_one_proxy_or_one_exit_ip():
    from agentos.stealth.stealth import device_findings

    same_label = device_findings({"biz-a-pc": "lt1", "biz-b-pc": "lt1"},
                                 {"biz-a/x": ("biz-a-pc", "lt1"), "biz-b/x": ("biz-b-pc", "lt1")}, {})
    assert any(level == "fail" and "2 computers leave through proxy lt1" in d for level, d in _levels(same_label))
    same_ip = device_findings({"biz-a-pc": "lt1", "biz-b-pc": "lt2"},
                              {"biz-a/x": ("biz-a-pc", "lt1"), "biz-b/x": ("biz-b-pc", "lt2")}, {},
                              exits={"lt1": "203.0.113.5", "lt2": "203.0.113.5"})
    assert any(level == "fail" and "exit from one IP 203.0.113.5" in d for level, d in _levels(same_ip))
    lone_lane = device_findings({"biz-a-pc": "lt1"}, {"biz-a/x": ("biz-a-pc", "lt1"), "me/x": (None, "lt1")}, {})
    assert any(level == "warn" and "me/x" in d for level, d in _levels(lone_lane))
    clean = device_findings({"biz-a-pc": "lt1", "me-pc": "lt2"},
                            {"biz-a/x": ("biz-a-pc", "lt1"), "biz-a/y": ("biz-a-pc", "lt1"),
                             "me/x": ("me-pc", "lt2")}, {}, exits={"lt1": "203.0.113.5", "lt2": "203.0.113.6"})
    assert clean == []


def test_the_audit_flags_a_pin_whose_timezone_or_locale_is_not_where_the_proxy_exits():
    from datetime import UTC, datetime

    from agentos.stealth.stealth import device_findings

    pin = {"timezone": "America/New_York", "locale:language": "en", "locale:region": "US",
           "agentos:pin": {"version": 2, "geo": {"ip": "203.0.113.5", "country": "US"}}}
    found = device_findings({"biz-a-pc": "lt1"}, {"biz-a/x": ("biz-a-pc", "lt1")}, {"device:biz-a-pc": pin},
                            geo={"lt1": {"country": "LT", "timezone": "Europe/Vilnius", "ip": "203.0.113.5"}},
                            at=datetime(2026, 11, 5, tzinfo=UTC))
    [location] = [f for f in found if f.check == "location"]
    assert location.level == "warn" and location.lane == "device:biz-a-pc"
    assert "en-US" in location.detail and "America/New_York" in location.detail


async def test_lane_audit_reads_a_device_once_and_its_lanes_as_one_computer(tmp_path):
    from agentos.stealth.stealth import audit_pins

    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    for name in ("instagram", "facebook"):
        await mgr.create(LaneSpec(id=f"biz-a/{name}", device="biz-a-pc"))
        await mgr.open(f"biz-a/{name}")
    await mgr.create(LaneSpec(id="biz-b/instagram", proxy_label="lt2"))
    await mgr.open("biz-b/instagram")
    await mgr.shutdown()
    findings, count = audit_pins(tmp_path)
    assert count == 2  # the device and the lane of its own
    units = {f.lane for f in findings if f.check == "pin"}
    assert "device:biz-a-pc" in units and "biz-a/instagram" not in units
    assert not [f for f in findings if f.check == "collision" and f.level != "ok"]
    assert not [f for f in findings if f.check == "device"]


async def test_a_lost_device_pin_comes_back_from_a_lane_copy_instead_of_a_new_computer(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    await mgr.open("biz-a/instagram")
    await mgr.close("biz-a/instagram")
    known = mgr.config.fingerprint_path("biz-a/instagram").read_bytes()
    mgr.config.device_pin_path("biz-a-pc").unlink()  # an older backup restored without devices/
    await mgr.open("biz-a/instagram")
    assert engine.drawn == 1  # no new computer
    assert mgr.config.device_pin_path("biz-a-pc").read_bytes() == known
    assert "device_pin_restored" in [e.kind for e in mgr.events("biz-a/instagram")]
    await mgr.shutdown()


# ---- review regressions: a logged-in lane never silently becomes a new computer ---------------


async def _launch_close(mgr, lane):
    await mgr.open(lane)
    await mgr.close(lane)


async def test_a_lost_device_pin_comes_back_from_another_lanes_copy_when_a_lane_without_one_launches(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    await _launch_close(mgr, "biz-a/instagram")
    seen = mgr.config.fingerprint_path("biz-a/instagram").read_bytes()
    mgr.config.device_pin_path("biz-a-pc").unlink()
    await mgr.create(LaneSpec(id="biz-a/facebook", device="biz-a-pc"))
    await _launch_close(mgr, "biz-a/facebook")
    assert engine.drawn == 1  # no new computer
    assert mgr.config.fingerprint_path("biz-a/instagram").read_bytes() == seen
    assert mgr.config.fingerprint_path("biz-a/facebook").read_bytes() == seen
    assert mgr.config.device_pin_path("biz-a-pc").read_bytes() == seen


async def test_a_lost_device_pin_whose_copies_disagree_refuses_the_launch(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    for name in ("instagram", "facebook"):
        await mgr.create(LaneSpec(id=f"biz-a/{name}", device="biz-a-pc"))
    await _launch_close(mgr, "biz-a/instagram")
    mgr.config.device_pin_path("biz-a-pc").unlink()
    other = json.loads(mgr.config.fingerprint_path("biz-a/facebook").read_text())
    other["navigator.platform"] = "Win32"
    mgr.config.fingerprint_path("biz-a/facebook").write_text(json.dumps(other))
    with pytest.raises(InvalidRequest, match="disagree"):
        await mgr.open("biz-a/instagram")
    assert engine.drawn == 1 and not mgr.config.device_pin_path("biz-a-pc").exists()
    await mgr.shutdown()


async def test_a_device_without_its_pin_file_is_pinned_by_its_lanes_copies_and_a_joiner_needs_a_yes(tmp_path):
    """Device D lost its pin file; logged-in lane A still holds its copy. Lane L (own pin, same
    proxy) joining D is a new computer to L's site, never L's pin made everyone's."""
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    await _launch_close(mgr, "biz-a/instagram")
    seen_a = mgr.config.fingerprint_path("biz-a/instagram").read_bytes()
    mgr.config.device_pin_path("biz-a-pc").unlink()
    await mgr.create(LaneSpec(id="biz-a/facebook", proxy_label="lt1"))
    await _launch_close(mgr, "biz-a/facebook")
    own = mgr.config.fingerprint_path("biz-a/facebook").read_bytes()
    assert mgr.device_info("biz-a-pc")["pinned"] is True
    plan = mgr.plan_device_move("biz-a/facebook", "biz-a-pc")
    assert plan["confirm"] is True, plan
    with pytest.raises(ConfirmRequired):
        await mgr.set_device("biz-a/facebook", "biz-a-pc")
    assert mgr.config.fingerprint_path("biz-a/facebook").read_bytes() == own
    await mgr.set_device("biz-a/facebook", "biz-a-pc", confirm=True)
    assert mgr.config.device_pin_path("biz-a-pc").read_bytes() == seen_a  # the device keeps A's computer
    assert mgr.config.fingerprint_path("biz-a/facebook").read_bytes() == seen_a
    await _launch_close(mgr, "biz-a/instagram")
    assert mgr.config.fingerprint_path("biz-a/instagram").read_bytes() == seen_a
    assert engine.drawn == 2


async def test_a_torn_device_pin_is_refused_and_never_written_over_a_lanes_pin(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/facebook", proxy_label="lt2"))
    await _launch_close(mgr, "biz-a/facebook")
    own = mgr.config.fingerprint_path("biz-a/facebook").read_bytes()
    mgr.config.device_pin_path("biz-a-pc").write_bytes(b'{"navigator.platform": "Lin')  # torn write
    assert mgr.device_info("biz-a-pc")["pin_status"] == "unreadable"
    with pytest.raises(InvalidRequest, match="no readable pin"):
        mgr.plan_device_move("biz-a/facebook", "biz-a-pc")
    with pytest.raises(InvalidRequest, match="no readable pin"):
        await mgr.set_device("biz-a/facebook", "biz-a-pc", confirm=True)
    assert mgr.config.fingerprint_path("biz-a/facebook").read_bytes() == own
    assert mgr.get("biz-a/facebook").spec.device is None
    mgr.config.device_pin_path("biz-a-pc").write_bytes(b"")  # an empty file is no pin either
    with pytest.raises(InvalidRequest, match="no readable pin"):
        await mgr.set_device("biz-a/facebook", "biz-a-pc", confirm=True)
    assert mgr.config.fingerprint_path("biz-a/facebook").read_bytes() == own


async def test_a_device_is_one_companys_computer(tmp_path):
    mgr, *_ = device_mgr(tmp_path)
    await mgr.create_device("shared-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/instagram", device="shared-pc"))
    assert mgr.store.device("shared-pc").workspace == "biz-a"
    with pytest.raises(InvalidRequest, match="biz-a's computer"):
        await mgr.create(LaneSpec(id="biz-b/instagram", device="shared-pc"))
    await mgr.create(LaneSpec(id="biz-b/facebook", proxy_label="lt2"))
    with pytest.raises(InvalidRequest, match="biz-a's computer"):
        mgr.plan_device_move("biz-b/facebook", "shared-pc")
    with pytest.raises(InvalidRequest, match="biz-a's computer"):
        await mgr.set_device("biz-b/facebook", "shared-pc", confirm=True)
    assert mgr.get("biz-b/facebook").spec.device is None
    await mgr.delete("biz-a/instagram")  # empty, the device stays biz-a's
    with pytest.raises(InvalidRequest, match="biz-a's computer"):
        await mgr.create(LaneSpec(id="biz-b/instagram", device="shared-pc"))
    with pytest.raises(InvalidRequest, match="biz-a's computer"):
        await mgr.create(LaneSpec(id="personal/reddit", device="shared-pc"))


async def test_one_proxy_is_one_computers_exit(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    with pytest.raises(InvalidRequest, match="already the exit of device biz-a-pc"):
        await mgr.create_device("biz-b-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-b/facebook", proxy_label="lt2"))
    await mgr.create(LaneSpec(id="biz-b/instagram", proxy_label="lt2"))
    await _launch_close(mgr, "biz-b/facebook")
    with pytest.raises(InvalidRequest, match="--from-lane biz-b/facebook"):
        await mgr.create_device("biz-b-pc", proxy_label="lt2")
    with pytest.raises(ConfirmRequired, match="biz-b/instagram also leave through proxy lt2"):
        await mgr.create_device("biz-b-pc", from_lane="biz-b/facebook")
    assert mgr.store.device("biz-b-pc") is None and mgr.get("biz-b/facebook").spec.device is None
    info = await mgr.create_device("biz-b-pc", from_lane="biz-b/facebook", confirm=True)
    assert info["proxy"] == "lt2" and mgr.store.device("biz-b-pc").workspace == "biz-b"


async def test_a_lane_joining_a_device_waits_for_a_first_launch_on_it(tmp_path):
    """Device D unpinned; lane X on D is mid-first-launch when logged-in lane L (own pin, same
    proxy) asks to join D. The move waits for the launch (the device lock), then sees D pinned
    and asks for a yes instead of handing L's pin to D or D's fresh pin to L."""
    import asyncio

    class Racing(PinningEngine):
        hook = None
        task = None

        async def launch(self, spec, *, profile_dir, fingerprint_path, display, xauthority=None, proxy):
            if not fingerprint_path.exists() and self.hook is not None:
                self.task = asyncio.create_task(self.hook())
                await asyncio.sleep(0.05)  # the move runs now, if nothing holds it back
            return await super().launch(spec, profile_dir=profile_dir, fingerprint_path=fingerprint_path,
                                        display=display, xauthority=xauthority, proxy=proxy)

    engine = Racing()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/facebook", proxy_label="lt1"))
    await _launch_close(mgr, "biz-a/facebook")
    seen_l = mgr.config.fingerprint_path("biz-a/facebook").read_bytes()
    await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    engine.hook = lambda: mgr.set_device("biz-a/facebook", "biz-a-pc")
    await _launch_close(mgr, "biz-a/instagram")
    with pytest.raises(ConfirmRequired):
        await engine.task
    assert mgr.config.fingerprint_path("biz-a/facebook").read_bytes() == seen_l
    assert mgr.get("biz-a/facebook").spec.device is None
    assert pin_of(mgr, "biz-a/instagram") != json.loads(seen_l)


def test_the_engine_never_writes_a_first_pin_over_one_written_meanwhile(tmp_path):
    from agentos.node import engine_camoufox as ec

    path = tmp_path / "fingerprint.json"
    ec.write_private_json(path, {"a": 1}, exclusive=True)
    with pytest.raises(FileExistsError):
        ec.write_private_json(path, {"a": 2}, exclusive=True)
    assert json.loads(path.read_text()) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["fingerprint.json"]  # no temp file left


def test_a_first_launch_whose_pin_appeared_meanwhile_fails_instead_of_overwriting_it(tmp_path, monkeypatch):
    pytest.importorskip("camoufox.fingerprints")
    from test_lanes_pin import FakeCamoufox, FakeGeo

    from agentos.core.errors import EngineError
    from agentos.node import engine_camoufox as ec

    running: dict = {}
    monkeypatch.setattr(ec, "read_browser_config", lambda pid: running["config"])
    monkeypatch.setattr(ec, "_host_cores", lambda: 2)
    engine = ec.CamoufoxEngine(resolve=FakeCamoufox(seed=7), geo=FakeGeo(), identity_os=["linux"])
    pin = tmp_path / "devices" / "biz-a-pc" / "fingerprint.json"
    spec = LaneSpec(id="biz-a/instagram", device="biz-a-pc", proxy_label="lt1")
    opts = engine.options(spec, profile_dir=tmp_path / "profile", fingerprint_path=pin, display=":150", proxy=PROXY)
    plan = engine.plan_launch(spec.id, opts, None)
    running["config"] = ec.config_of_env(plan.options["env"])
    pin.parent.mkdir(parents=True)
    pin.write_text('{"navigator.platform": "Win32"}')  # a lane's pin, taken by the device meanwhile
    with pytest.raises(EngineError, match="a pin was written while this first launch started"):
        engine.settle_pin(spec.id, pin, plan, pid=4242)
    assert json.loads(pin.read_text()) == {"navigator.platform": "Win32"}


async def test_a_device_is_not_removed_while_a_lane_is_made_on_it(tmp_path):
    import asyncio

    mgr, *_ = device_mgr(tmp_path)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    made, removed = await asyncio.gather(mgr.create(LaneSpec(id="biz-a/x", device="biz-a-pc")),
                                         mgr.remove_device("biz-a-pc"), return_exceptions=True)
    assert not isinstance(made, Exception)
    assert isinstance(removed, InvalidRequest) and mgr.store.device("biz-a-pc") is not None


async def test_leaving_a_device_without_an_earlier_pin_keeps_the_devices_computer(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/facebook", proxy_label="lt1"))
    await _launch_close(mgr, "biz-a/facebook")
    seen = mgr.config.fingerprint_path("biz-a/facebook").read_bytes()
    await mgr.set_device("biz-a/facebook", "biz-a-pc")  # the device had no pin: it takes the lane's
    assert mgr.config.device_pin_path("biz-a-pc").read_bytes() == seen
    plan = mgr.plan_device_move("biz-a/facebook", None)
    assert plan["confirm"] is False and "the same computer to its site, now alone" in plan["changes"][0]
    await mgr.set_device("biz-a/facebook", None)
    await _launch_close(mgr, "biz-a/facebook")
    assert mgr.config.fingerprint_path("biz-a/facebook").read_bytes() == seen and engine.drawn == 1

    # a lane whose copy went missing keeps the device's pin too
    await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    mgr.config.fingerprint_path("biz-a/instagram").unlink(missing_ok=True)
    await mgr.set_device("biz-a/instagram", None)
    assert mgr.config.fingerprint_path("biz-a/instagram").read_bytes() == seen


async def test_the_start_up_repair_keeps_a_newer_copy_and_never_overwrites_silently(tmp_path):
    import os

    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    for name in ("instagram", "facebook"):
        await mgr.create(LaneSpec(id=f"biz-a/{name}", device="biz-a-pc"))
    await _launch_close(mgr, "biz-a/instagram")
    device_pin = mgr.config.device_pin_path("biz-a-pc")
    older = device_pin.read_bytes()
    newer = json.loads(mgr.config.fingerprint_path("biz-a/instagram").read_text())
    newer["navigator.platform"] = "Win32"  # stands in for a copy a later pin version wrote
    copy = mgr.config.fingerprint_path("biz-a/instagram")
    copy.write_text(json.dumps(newer))
    os.utime(copy, (device_pin.stat().st_mtime + 60,) * 2)
    mgr.repair_device_pins()
    assert pin_of(mgr, "biz-a/instagram") == newer == json.loads(device_pin.read_text())
    assert pin_of(mgr, "biz-a/facebook") == newer
    kept = device_pin.with_name("fingerprint.json.replaced.bak")
    assert kept.read_bytes() == older
    kept_copy = mgr.config.fingerprint_path("biz-a/facebook").with_name("fingerprint.json.replaced.bak")
    assert kept_copy.read_bytes() == older
    assert "device_pin_replaced" in [e.kind for e in mgr.events("biz-a/instagram")]
    assert "device_pin_copy_replaced" in [e.kind for e in mgr.events("biz-a/facebook")]

    # two newer copies that disagree: nothing is picked, nothing is overwritten
    a, b = dict(newer, draw=101), dict(newer, draw=102)
    for lane_id, pin in (("biz-a/instagram", a), ("biz-a/facebook", b)):
        path = mgr.config.fingerprint_path(lane_id)
        path.write_text(json.dumps(pin))
        os.utime(path, (device_pin.stat().st_mtime + 120,) * 2)
    mgr.repair_device_pins()
    assert pin_of(mgr, "biz-a/instagram") == a and pin_of(mgr, "biz-a/facebook") == b
    assert json.loads(device_pin.read_text()) == newer
    assert "device_pin_conflict" in [e.kind for e in mgr.events("biz-a/facebook")]


async def test_lane_audit_lists_a_pin_the_repair_replaced(tmp_path):
    from agentos.stealth.stealth import audit_pins

    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    await _launch_close(mgr, "biz-a/instagram")
    stale = dict(pin_of(mgr, "biz-a/instagram"), draw=99)
    mgr.config.fingerprint_path("biz-a/instagram").write_text(json.dumps(stale))
    import os
    os.utime(mgr.config.fingerprint_path("biz-a/instagram"), (1, 1))  # older than the device's
    mgr.repair_device_pins()
    findings, _count = audit_pins(tmp_path)
    [kept] = [f for f in findings if f.check == "device" and "replaced" in f.detail]
    assert kept.lane == "biz-a/instagram" and kept.level == "warn"
    await mgr.shutdown()


async def test_none_is_not_a_device_name(tmp_path):
    mgr, *_ = device_mgr(tmp_path)
    with pytest.raises(InvalidRequest, match="means a lane on no device"):
        await mgr.create_device("none", proxy_label="lt1")
    with pytest.raises(ValueError):
        DeviceSpec(name="none")
    with pytest.raises(ValueError):
        LaneSpec(id="a/b", device="none")


async def test_lane_audit_reads_a_device_without_its_pin_file_from_its_lanes_copies(tmp_path):
    from agentos.stealth.stealth import audit_pins

    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    for name in ("instagram", "facebook"):
        await mgr.create(LaneSpec(id=f"biz-a/{name}", device="biz-a-pc"))
    await _launch_close(mgr, "biz-a/instagram")
    mgr.config.device_pin_path("biz-a-pc").unlink()
    findings, count = audit_pins(tmp_path)
    [lost] = [f for f in findings if f.check == "device" and "no pin file" in f.detail]
    assert lost.level == "warn" and lost.lane == "device:biz-a-pc" and count == 1
    mgr.config.fingerprint_path("biz-a/facebook").write_text(json.dumps(dict(pin_of(mgr, "biz-a/facebook"), draw=7)))
    findings, _count = audit_pins(tmp_path)
    [split] = [f for f in findings if f.check == "device" and "disagree" in f.detail]
    assert split.level == "fail" and "biz-a/facebook" in split.detail and "biz-a/instagram" in split.detail
    await mgr.shutdown()


async def test_a_device_create_that_fails_leaves_no_pin_behind(tmp_path, monkeypatch):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create(LaneSpec(id="biz-a/instagram", proxy_label="lt1"))
    await _launch_close(mgr, "biz-a/instagram")

    def taken(device):
        raise InvalidRequest(f"device {device.name} already exists")

    monkeypatch.setattr(mgr.store, "add_device", taken)
    with pytest.raises(InvalidRequest, match="already exists"):
        await mgr.create_device("biz-a-pc", from_lane="biz-a/instagram")
    assert not mgr.config.device_pin_path("biz-a-pc").exists()
    assert mgr.get("biz-a/instagram").spec.device is None


async def test_getting_back_an_earlier_pin_that_is_not_where_the_proxy_exits_is_warned(tmp_path):
    engine = PinningEngine()
    mgr, *_ = device_mgr(tmp_path, engine=engine)
    await mgr.create_device("biz-a-pc", proxy_label="lt1")
    await mgr.create(LaneSpec(id="biz-a/instagram", device="biz-a-pc"))
    await _launch_close(mgr, "biz-a/instagram")
    await mgr.create(LaneSpec(id="biz-a/facebook", proxy_label="lt2"))
    mgr.config.fingerprint_path("biz-a/facebook").write_text(json.dumps(
        {"navigator.platform": "Win32", "timezone": "America/New_York", "locale:language": "en",
         "locale:region": "US", "agentos:pin": {"version": 2, "geo": {"country": "US"}}}))
    await mgr.set_device("biz-a/facebook", "biz-a-pc", confirm=True)
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    (cache / "exit-geo.json").write_text(json.dumps(
        {"lt1": {"country": "LT", "timezone": "Europe/Vilnius", "ip": "203.0.113.5"}}))
    plan = mgr.plan_device_move("biz-a/facebook", None)
    assert plan["confirm"] is True
    [where] = [w for w in plan["warnings"] if "does not match where proxy lt1 exits" in w]
    assert "America/New_York" in where or "en-US" in where
