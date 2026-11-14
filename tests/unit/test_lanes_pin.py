"""The lane's pinned fingerprint: completion rules, the version 1 migration, and that a launch
gives the browser exactly the pin. Camoufox's launch_options is replaced by a fake that behaves
the way Camoufox 0.5.9 does (it fills whatever a pinned config lacks from a fresh draw, drops
drawn values that are 0, re-derives the location with geoip, and decides canvas noise per launch).
"""

from __future__ import annotations

import json
import random
import stat
from pathlib import Path

import pytest

from agentos.core.errors import EngineError
from agentos.core.models import LaneSpec
from agentos.node.camoufox import engine as ec
from agentos.node.camoufox.engine import (
    CANVAS_PREF,
    CANVAS_TARGETS,
    LAUNCHER_KEYS,
    PIN_META,
    PIN_VERSION,
    CamoufoxEngine,
    Pin,
    backup_pin,
    brief,
    complete_config,
    config_of_env,
    display_size_of,
    distance_km,
    launch_config,
    load_pin,
    location_change,
    parse_camou_config,
    read_pin,
    snap_cores,
    split_pin,
    window_position_of,
    window_size_of,
    with_config,
)

VILNIUS = {"country": "LT", "timezone": "Europe/Vilnius", "latitude": 54.6872, "longitude": 25.2797}
RIGA = {"country": "LV", "timezone": "Europe/Riga", "latitude": 56.9496, "longitude": 24.1052}
EXIT_IP = "203.0.113.9"
WIN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0"
MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) Gecko/20100101 Firefox/158.0"
LINUX_UA = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0"


def v1_pin() -> dict:
    """A pin as the PoC and the first builds made it (the browser's CAMOU_CONFIG on its first
    launch): a Windows draw whose availLeft, availTop, screenX and maxTouchPoints were 0, so
    Camoufox left them out."""
    return {
        "addons": ["/root/agentos/.venv/lib/python3.12/site-packages/camoufox/addons/UBO"],
        "fonts": ["Arial", "Calibri", "Segoe UI", "Tahoma"],
        "geolocation:latitude": 54.6872, "geolocation:longitude": 25.2797,
        "humanize": True, "input:windowsKeyboard": True,
        "locale:language": "en", "locale:region": "US",
        "mediaDevices:enabled": True, "mediaDevices:micros": 1, "mediaDevices:speakers": 1,
        "mediaDevices:webcams": 1,
        "navigator.appVersion": "5.0 (Windows)", "navigator.hardwareConcurrency": 4,
        "navigator.oscpu": "Windows NT 10.0; Win64; x64", "navigator.platform": "Win32",
        "navigator.userAgent": WIN_UA,
        "screen.availHeight": 1032, "screen.availWidth": 1920, "screen.colorDepth": 24,
        "screen.height": 1080, "screen.pixelDepth": 24, "screen.width": 1920,
        "timezone": "Europe/Vilnius",
        "voices": [{"lang": "en-US", "name": "Microsoft David", "voiceUri": "x", "isDefault": True,
                    "isLocalService": True}],
        "voices:blockIfNotDefined": True,
        "webGl2:parameters": {"3379": 16384}, "webGl2:shaderPrecisionFormats": {"35633,36338": {}},
        "webGl2:supportedExtensions": ["EXT_color_buffer_float"],
        "webGl:parameters": {"3379": 16384}, "webGl:renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620)",
        "webGl:shaderPrecisionFormats": {"35633,36338": {}}, "webGl:supportedExtensions": ["ANGLE_instanced_arrays"],
        "webGl:vendor": "Google Inc. (Intel)",
        "webrtc:ipv4": EXIT_IP,
        "window.outerHeight": 1032, "window.outerWidth": 1920, "window.screenY": 0,
    }


class FakeCamoufox:
    """camoufox.utils.launch_options, as far as a pin can tell: a fresh draw every call."""

    def __init__(self, seed: int = 0, *, override_prefs: bool = False):
        self.rng = random.Random(seed)
        self.calls: list[dict] = []
        self.override_prefs = override_prefs

    def draw(self, os_name: str) -> dict:
        ua = {"windows": WIN_UA, "macos": MAC_UA, "linux": LINUX_UA}[os_name]
        platform = {"windows": "Win32", "macos": "MacIntel", "linux": "Linux x86_64"}[os_name]
        raw = {
            "navigator.userAgent": ua, "navigator.platform": platform, "navigator.hardwareConcurrency": 4,
            "navigator.maxTouchPoints": self.rng.choice([0, 0, 1, 5, 10]),
            "screen.width": 1920, "screen.height": 1080, "screen.colorDepth": 24, "screen.pixelDepth": 24,
            "screen.availWidth": 1920, "screen.availHeight": 1032,
            "screen.availLeft": self.rng.choice([0, 0, 66, 1366, 1920]), "screen.availTop": self.rng.choice([0, 32]),
            "window.outerWidth": 1920, "window.outerHeight": 1032,
            "window.screenX": self.rng.choice([0, 5, 25, 614]), "window.screenY": self.rng.choice([0, 0, 7]),
        }
        return {k: v for k, v in raw.items() if v}  # Camoufox drops every falsy drawn value

    def __call__(self, **kw) -> dict:
        self.calls.append(kw)
        given = kw.get("config") or {}
        config = dict(given)
        prefs = dict(kw.get("firefox_user_prefs") or {})
        os_name = kw.get("os") or ["windows", "macos", "linux"]
        if isinstance(os_name, list):  # Camoufox picks one of a list
            os_name = self.rng.choice(os_name)
        for key, value in self.draw(os_name).items():
            config.setdefault(key, value)
        if "navigator.oscpu" not in given:
            config["navigator.oscpu"] = ec._oscpu(config["navigator.platform"])
            config["navigator.appVersion"] = ec._app_version(config["navigator.userAgent"])
        seed = random.Random(json.dumps(given, sort_keys=True))  # seeded by the identity, as Camoufox is
        config.setdefault("fonts", seed.sample(["Arial", "Calibri", "Cambria", "Segoe UI", "Tahoma"], 3))
        config.setdefault("webGl:vendor", "Google Inc. (Intel)")
        config.setdefault("webGl:renderer", "ANGLE (Intel, Intel(R) UHD Graphics 620)")
        config["addons"] = ["/opt/agentos/.venv/camoufox/addons/UBO"]
        config.setdefault("humanize", True)
        if kw.get("geoip"):
            config.setdefault("webrtc:ipv4", EXIT_IP)
            config["geolocation:latitude"] = 54.6872 + self.rng.random() / 100  # re-derived on every call
            config["geolocation:longitude"] = 25.2797
            config.setdefault("timezone", "Europe/Vilnius")
        if kw.get("locale"):
            config["locale:language"], config["locale:region"] = kw["locale"].split("-")
        named_gpu = "webGl:renderer" in given
        canvas = kw.get("canvas_noise")
        covered = canvas if canvas is not None else (named_gpu or os_name != "linux")
        if covered:
            overrides = [o for o in str(prefs.get(CANVAS_PREF) or "").split(",") if o]
            prefs[CANVAS_PREF] = ",".join(overrides + list(CANVAS_TARGETS))
        webgl2 = self.rng.choice([True, False])  # a seeded draw that differs between launches
        if self.override_prefs:
            prefs["webgl.enable-webgl2"] = webgl2
        else:
            prefs.setdefault("webgl.enable-webgl2", webgl2)
        prefs.setdefault("dom.quotaManager.temporaryStorage.fixedLimit", 19510009)
        prefs.setdefault("intl.locale.requested", "en-US")
        env = with_config(dict(kw["env"]), config)
        env["CAMOU_PREFS_1"] = json.dumps(prefs)
        out = {"executable_path": "/x/camoufox-bin", "args": [], "env": env, "firefox_user_prefs": prefs,
               "headless": False, "user_data_dir": kw["user_data_dir"]}
        if kw.get("proxy"):
            out["proxy"] = kw["proxy"]
        return out


class FakeGeo:
    def __init__(self, ip: str = EXIT_IP, where: dict | None = None, fail: Exception | None = None):
        self.ip, self.where, self.fail = ip, where or VILNIUS, fail
        self.lookups = 0

    def exit_ip(self, proxy):
        if self.fail:
            raise self.fail
        self.lookups += 1
        return self.ip

    def locate(self, ip):
        if self.fail:
            raise self.fail
        return {"ip": ip, **self.where}


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """One lane's paths, an engine with the fake Camoufox, and a launcher that plays one launch:
    plan, start a "browser" that carries the planned config, settle the pin."""
    pytest.importorskip("camoufox.fingerprints")
    fp = tmp_path / "lane" / "fingerprint.json"
    fp.parent.mkdir()
    running: dict = {}
    monkeypatch.setattr(ec, "read_browser_config", lambda pid: running["config"])
    monkeypatch.setattr(ec, "_host_cores", lambda: 2)
    engine = CamoufoxEngine(resolve=FakeCamoufox(), geo=FakeGeo())

    def launch(*, browser_config=None):
        pin = load_pin(fp, "a/b") if fp.exists() else None
        opts = engine.options(LaneSpec(id="a/b"), profile_dir=tmp_path / "lane" / "profile", fingerprint_path=fp,
                              display=":150", proxy=None)
        plan = engine.plan_launch("a/b", opts, pin)
        running["config"] = browser_config if browser_config is not None else config_of_env(plan.options["env"])
        engine.settle_pin("a/b", fp, plan, pid=4242)
        return plan

    return engine, fp, launch


def identity(config: dict) -> dict:
    """The device as the browser gets it: no launcher keys, and a hardened lane's position stays in
    the pin (ec.browser_view)."""
    return {k: v for k, v in config.items()
            if k not in LAUNCHER_KEYS and k != PIN_META and not k.startswith(ec.GEOLOCATION_PREFIX)}


# ---- completion rules ------------------------------------------------------------------


def test_completion_fills_a_single_monitor_desktop():
    pin = v1_pin()
    done, filled = complete_config(pin)
    assert filled == {"screen.availLeft": 0, "screen.availTop": 0, "window.screenX": 0,
                      "navigator.maxTouchPoints": 0}
    assert {k: done[k] for k in pin} == pin


def test_completion_never_changes_a_pinned_key():
    rng = random.Random(7)
    full, _ = complete_config(v1_pin(), host_cores=8)
    for _ in range(300):
        keys = rng.sample(sorted(full), rng.randint(1, len(full)))
        pin = {k: full[k] for k in keys}
        for k in rng.sample(keys, min(3, len(keys))):  # odd pinned values stay odd
            pin[k] = rng.choice([1920, 66, -5, "x", None, True])
        done, filled = complete_config(pin, host_cores=rng.choice([None, 2, 16]))
        assert {k: done[k] for k in pin} == pin
        assert set(done) == set(pin) | set(filled) and not set(filled) & set(pin)


def test_completion_is_deterministic_and_order_free():
    pin = v1_pin()
    shuffled = dict(sorted(pin.items(), key=lambda kv: random.Random(3).random()))
    a = complete_config(pin, derived={"zz:new": 1, "aa:new": 2}, host_cores=4)
    b = complete_config(shuffled, derived={"aa:new": 2, "zz:new": 1}, host_cores=4)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert complete_config(a[0]) == (a[0], {})  # a complete pin needs nothing more


def test_the_window_sits_where_the_browser_puts_the_work_area():
    # The browser reports availLeft 0 (and availTop 0 off a Mac) whatever the config says, so a
    # pinned second-monitor availLeft never moves the window off the screen a page sees.
    pin = {"navigator.platform": "Linux x86_64", "navigator.userAgent": LINUX_UA, "screen.width": 1920,
           "screen.height": 1080, "screen.availLeft": 1920, "screen.availTop": 32}
    done, _ = complete_config(pin)
    assert (done["window.screenX"], done["window.screenY"]) == (0, 0)
    assert done["screen.availWidth"] == 1920 and done["screen.availHeight"] == 1080  # Linux: no taskbar
    mac, _ = complete_config({"navigator.platform": "MacIntel", "screen.width": 1440, "screen.height": 900})
    assert (mac["screen.availTop"], mac["window.screenY"], mac["screen.availHeight"]) == (25, 25, 824)


@pytest.mark.parametrize(("platform", "ua", "renderer", "depth", "avail_h"), [
    ("Win32", WIN_UA, "ANGLE (Intel)", 24, 1032),
    ("MacIntel", MAC_UA, "Apple M1, or similar", 30, 1004),
    ("MacIntel", MAC_UA, "Intel(R) Iris(TM) Plus Graphics", 24, 1004),
    ("Linux x86_64", LINUX_UA, "Mesa Intel(R) UHD Graphics", 24, 1080),
])
def test_completion_follows_the_pinned_os(platform, ua, renderer, depth, avail_h):
    done, _ = complete_config({"navigator.platform": platform, "navigator.userAgent": ua, "webGl:renderer": renderer,
                               "screen.width": 1920, "screen.height": 1080})
    assert done["screen.colorDepth"] == done["screen.pixelDepth"] == depth
    assert done["screen.availHeight"] == avail_h and done["navigator.maxTouchPoints"] == 0


def test_completion_matches_camoufox_for_oscpu_and_app_version():
    fingerprints = pytest.importorskip("camoufox.fingerprints")
    for platform, ua in (("Win32", WIN_UA), ("MacIntel", MAC_UA), ("Linux x86_64", LINUX_UA),
                         ("Linux x86_64", "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0")):
        done, _ = complete_config({"navigator.platform": platform, "navigator.userAgent": ua})
        assert done["navigator.oscpu"] == fingerprints.oscpu_for_platform(platform)
        assert done["navigator.appVersion"] == fingerprints._app_version_from_user_agent(ua)


def test_cores_snap_down_like_camoufox():
    assert [snap_cores(n) for n in (1, 2, 4, 5, 12, 13, 27, 64)] == [4, 4, 4, 4, 12, 12, 24, 32]
    _done, filled = complete_config({"navigator.platform": "Win32"}, host_cores=6)
    assert filled["navigator.hardwareConcurrency"] == 6
    assert "navigator.hardwareConcurrency" not in complete_config({"navigator.platform": "Win32"})[0]


def test_completion_never_invents_a_screen_or_takes_a_fresh_draw():
    pin = {"navigator.platform": "Win32", "navigator.userAgent": WIN_UA}
    derived = {"screen.width": 1366, "screen.availLeft": 1920, "addons": ["/x"], "humanize": True,
               "fonts": ["Arial"], PIN_META: {"version": 9}}
    done, filled = complete_config(pin, derived=derived)
    assert "screen.width" not in done and "screen.availWidth" not in done
    assert done["screen.availLeft"] == 0  # the rule, not the draw
    assert "addons" not in done and "humanize" not in done and PIN_META not in done
    assert filled["fonts"] == ["Arial"]  # seeded by the identity: what every relaunch showed


def test_launch_config_gives_the_browser_the_pin_and_the_launch_its_launcher_keys():
    pinned = {"a": 1, "b": 2, "addons": ["/old/path"], "humanize": True}
    resolved = {"a": 1, "b": 3, "c": 4, "addons": ["/new/path"], "humanize": True}
    final, changed, dropped = launch_config(pinned, resolved)
    assert final == {"a": 1, "b": 2, "addons": ["/new/path"], "humanize": True}
    assert changed == ["b"] and dropped == ["c"]


def test_the_config_rides_in_chunks_like_camoufox():
    config = {"voices": [{"name": "Zoë ģ", "lang": "lv-LV"}] * 40, "navigator.platform": "Win32"}
    env = with_config({"PATH": "/bin", "CAMOU_CONFIG_1": "stale", "CAMOU_CONFIG_9": "stale"}, config, chunk=97)
    assert env["PATH"] == "/bin" and "CAMOU_CONFIG_9" in env  # more chunks than before
    assert config_of_env(env) == config
    block = "\0".join(f"{k}={v}" for k, v in env.items()).encode()
    assert parse_camou_config(block) == config


def test_location_change_rules():
    assert location_change(VILNIUS, dict(VILNIUS, latitude=54.70)) is None  # a database refresh, 2 km
    assert "timezone Europe/Vilnius to Europe/Riga" in location_change(VILNIUS, RIGA)
    assert "country LT to LV" in location_change(VILNIUS, RIGA)
    assert "km away" in location_change(VILNIUS, dict(VILNIUS, latitude=56.0))
    assert location_change({"timezone": None}, RIGA) is None  # nothing to compare
    assert 260 < distance_km((54.6872, 25.2797), (56.9496, 24.1052)) < 270


def test_brief_keeps_events_small():
    out = brief({"screen.availLeft": 0, "fonts": ["Arial"] * 50, "webGl:renderer": "x" * 200})
    assert out == {"fonts": "(50 entries)", "screen.availLeft": 0, "webGl:renderer": "\"" + "x" * 79 + "..."}


# ---- the pin file ----------------------------------------------------------------------


def test_a_version_1_pin_reads_as_it_always_did(tmp_path):
    fp = tmp_path / "fingerprint.json"
    fp.write_text(json.dumps(v1_pin()))
    assert read_pin(fp) == v1_pin()
    pin = load_pin(fp)
    assert pin.version == 1 and pin.meta == {} and pin.config == v1_pin()


def test_a_version_2_pin_keeps_its_record_apart(tmp_path):
    fp = tmp_path / "fingerprint.json"
    fp.write_text(json.dumps({**v1_pin(), PIN_META: {"version": 2, "canvas_noise": True}}))
    pin = load_pin(fp)
    assert pin.version == 2 and pin.meta["canvas_noise"] is True and PIN_META not in pin.config
    assert window_size_of(read_pin(fp)) == (1920, 1032)
    assert Pin(pin.config, pin.meta).to_json() == json.loads(fp.read_text())


def test_a_broken_record_or_a_record_alone_is_handled(caplog):
    assert split_pin({"navigator.platform": "Win32", PIN_META: "nonsense"}).version == 1
    assert "not an object" in caplog.text
    with pytest.raises(EngineError, match="only agentos's record"):
        split_pin({PIN_META: {"version": 2}})


def test_ops_tools_read_and_refresh_a_version_2_pin(tmp_path):
    from agentos.node import fingerprint as ops_fp
    fp = tmp_path / "fingerprint.json"
    stored = {**v1_pin(), PIN_META: {"version": 2, "prefs": {"webgl.enable-webgl2": True}}}
    fp.write_text(json.dumps(stored))
    assert ops_fp.pinned_major(json.loads(fp.read_text())) == 158
    ops_fp.refresh(fp, "a/b", 159)
    refreshed = json.loads(fp.read_text())
    assert refreshed[PIN_META] == stored[PIN_META]
    assert "Firefox/159.0" in refreshed["navigator.userAgent"]
    assert {k: v for k, v in refreshed.items() if k != "navigator.userAgent"} == \
        {k: v for k, v in stored.items() if k != "navigator.userAgent"}


def test_backup_keeps_the_exact_bytes_and_never_overwrites(tmp_path):
    fp = tmp_path / "fingerprint.json"
    fp.write_bytes(b'{"navigator.platform":"Win32" }')
    first = backup_pin(fp, 1)
    assert first.name == "fingerprint.json.pin-v1.bak" and first.read_bytes() == fp.read_bytes()
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert backup_pin(fp, 1) == first  # the same pin again: one backup
    fp.write_bytes(b'{"navigator.platform":"MacIntel"}')
    second = backup_pin(fp, 1)
    assert second != first and second.read_bytes() == fp.read_bytes()
    assert first.read_bytes() == b'{"navigator.platform":"Win32" }'


# ---- options ---------------------------------------------------------------------------


def options_for(tmp_path: Path, stored: dict | None):
    pytest.importorskip("camoufox.fingerprints")
    fp = tmp_path / "fingerprint.json"
    if stored is not None:
        fp.write_text(json.dumps(stored))
    return CamoufoxEngine().options(LaneSpec(id="a/b"), profile_dir=tmp_path / "profile", fingerprint_path=fp,
                                    display=":150", proxy=None)


def test_options_hand_a_version_1_pin_over_exactly_as_stored(tmp_path):
    opts = options_for(tmp_path, v1_pin())
    # Camoufox seeds fonts, voices and WebGL from a hash of this very dict: what relaunches showed
    assert opts["config"] == v1_pin() and opts["os"] == "windows"
    assert opts["geoip"] is False  # the pin holds the location
    assert "canvas_noise" not in opts
    assert opts["firefox_user_prefs"][CANVAS_PREF] == ""


def test_options_derive_the_location_once_when_a_pin_lacks_it(tmp_path):
    pin = {k: v for k, v in v1_pin().items() if k != "webrtc:ipv4"}  # WebRTC would show the node's IP
    assert options_for(tmp_path, pin)["geoip"] is True


def test_options_carry_a_version_2_pin_and_its_prefs(tmp_path):
    stored = {**v1_pin(), PIN_META: {"version": 2, "canvas_noise": False,
                                     "prefs": {"webgl.enable-webgl2": False, "not.an.identity.pref": 1}}}
    opts = options_for(tmp_path, stored)
    assert opts["config"] == v1_pin() and PIN_META not in opts["config"]
    assert opts["canvas_noise"] is False and opts["geoip"] is False
    prefs = opts["firefox_user_prefs"]
    assert prefs["webgl.enable-webgl2"] is False and "not.an.identity.pref" not in prefs
    assert prefs[CANVAS_PREF] == "" and prefs["browser.cache.disk.capacity"] == 262144


def test_first_launch_options_leave_the_draw_to_camoufox(tmp_path):
    opts = options_for(tmp_path, None)
    assert "config" not in opts and "os" not in opts and opts["geoip"] is True
    assert opts["firefox_user_prefs"][CANVAS_PREF] == ""  # a profile keeps a pref that was set once
    assert opts["firefox_user_prefs"]["browser.startup.couldRestoreSession.count"] == -1  # no one-time bar
    assert opts["firefox_user_prefs"]["mousewheel.default.delta_multiplier_y"] == 275  # a notch like a mouse's


@pytest.mark.parametrize("os_name", ["windows", "macos"])
def test_a_pinned_lane_keeps_its_whole_device_under_the_linux_default(lane, os_name):
    """lane_os = ["linux"] is for new lanes: a lane pinned as Windows or macOS before relaunches as the
    same device, byte for byte."""
    engine, fp, launch = lane
    engine.identity_os = [os_name]
    launch()
    pinned = fp.read_bytes()
    engine.identity_os = ["linux"]  # the new default
    for _ in range(2):
        plan = launch()
        assert plan.pin is None and fp.read_bytes() == pinned
        assert engine.resolve.calls[-1]["os"] == os_name
        assert identity(plan.config) == identity(json.loads(pinned))


def test_a_new_lane_draws_the_identity_os_it_is_given_and_a_pinned_one_keeps_its_own(tmp_path):
    engine = CamoufoxEngine(identity_os=["linux"])
    fp = tmp_path / "fingerprint.json"
    first = engine.options(LaneSpec(id="a/b"), profile_dir=tmp_path / "p", fingerprint_path=fp, display=":150",
                           proxy=None)
    assert first["os"] == ["linux"] and "config" not in first
    fp.write_text(json.dumps(v1_pin()))
    pinned = engine.options(LaneSpec(id="a/b"), profile_dir=tmp_path / "p", fingerprint_path=fp, display=":150",
                            proxy=None)
    assert pinned["os"] == "windows"  # the pin's own OS: a lane never changes device


def test_the_display_is_the_pinned_screen_and_the_window_sits_at_its_pinned_place():
    pin = {"screen.width": 1536, "screen.height": 864, "window.outerWidth": 1536, "window.outerHeight": 816,
           "window.screenX": 0, "window.screenY": 0}
    assert display_size_of(pin) == (1536, 864) and window_position_of(pin) == (0, 0)
    mac = {**pin, "screen.height": 960, "window.outerHeight": 878, "window.screenY": 25}
    assert display_size_of(mac) == (1536, 960) and window_position_of(mac) == (0, 25)
    scaled = {**pin, "window.devicePixelRatio": 1.25, "window.outerWidth": 1200, "window.screenX": 8}
    assert display_size_of(scaled) == (1920, 1080) and window_position_of(scaled) == (10, 0)
    # a window that would not fit on its screen there: the display is the window, as before
    assert display_size_of({**pin, "window.screenX": 206}) == (1536, 816)
    assert display_size_of({"window.outerWidth": 1280, "window.outerHeight": 720}) == (1280, 720)
    assert display_size_of({"screen.width": 1920}) is None
    assert window_position_of({"window.screenX": -8, "window.screenY": 0}) is None


def test_a_screen_larger_than_xvfb_serves_gives_the_window_a_display_of_its_own_size():
    from agentos.node.display import MAX_SIZE, fit_size

    # an 8K screen at ratio 2 is 15360x8640 pixels, past Xvfb's maximum: the display used to fall
    # back to 1920x1080 under a 7680x4200 window. Now the display is the window, which fits.
    pin = {"screen.width": 7680, "screen.height": 4320, "window.devicePixelRatio": 2,
           "window.outerWidth": 3840, "window.outerHeight": 2100, "window.screenX": 0, "window.screenY": 0}
    size = display_size_of(pin)
    assert size == window_size_of(pin) == (7680, 4200) and fit_size(size) == size
    assert size[0] <= MAX_SIZE[0] and size[1] <= MAX_SIZE[1]
    # a window no display can hold: no size from the pin (the display keeps its default)
    assert display_size_of({**pin, "window.outerWidth": 7680, "window.outerHeight": 4320}) is None


def test_engine_display_size_reads_the_pin(tmp_path):
    fp = tmp_path / "fingerprint.json"
    assert CamoufoxEngine().display_size(fp) is None
    fp.write_text(json.dumps({**v1_pin(), "screen.width": 1920, "screen.height": 1080}))
    assert CamoufoxEngine().display_size(fp) == display_size_of(load_pin(fp).config)
    fp.write_text("{")
    assert CamoufoxEngine().display_size(fp) is None


def test_a_linux_node_measures_fallback_text_the_same_on_every_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(ec.sys, "platform", "linux")
    assert options_for(tmp_path, None)["firefox_user_prefs"]["gfx.font_rendering.fallback.async"] is False
    monkeypatch.setattr(ec.sys, "platform", "darwin")  # a Mac falls back through CoreText: left alone
    assert "gfx.font_rendering.fallback.async" not in options_for(tmp_path, None)["firefox_user_prefs"]


# ---- launches ----------------------------------------------------------------------------


def test_first_launch_pins_a_complete_identity_and_every_relaunch_is_identical(lane):
    engine, fp, launch = lane
    first = launch()
    assert first.first and first.notes[0][0] == "fingerprint_pinned"
    stored = json.loads(fp.read_text())
    assert stat.S_IMODE(fp.stat().st_mode) == 0o600
    meta = stored.pop(PIN_META)
    assert meta["version"] == PIN_VERSION and meta["geo"] == {"ip": EXIT_IP, "country": "LT"}
    assert set(meta["filled"]) <= {"screen.availLeft", "screen.availTop", "window.screenX", "window.screenY",
                                   "navigator.maxTouchPoints"}
    assert meta["prefs"]["webgl.enable-webgl2"] in (True, False) and isinstance(meta["canvas_noise"], bool)
    assert not set(stored) & LAUNCHER_KEYS  # the addon paths follow the install, never the pin
    assert identity(first.config) == identity(stored)  # the position stays in the pin (hardening)
    for key in ("screen.availLeft", "screen.availTop", "window.screenX", "window.screenY",
                "navigator.maxTouchPoints"):
        assert key in stored

    pinned_bytes = fp.read_bytes()
    for _ in range(3):  # the fake draws a new fingerprint every time, as Camoufox does
        again = launch()
        assert again.pin is None and not again.first
        assert identity(again.config) == identity(stored)
        prefs = again.options["firefox_user_prefs"]
        assert prefs["webgl.enable-webgl2"] == meta["prefs"]["webgl.enable-webgl2"]
        assert ec.canvas_covered(prefs) == meta["canvas_noise"]
        assert json.loads(again.options["env"]["CAMOU_PREFS_1"]) == prefs
        assert fp.read_bytes() == pinned_bytes  # never rewritten
    assert engine.resolve.calls[-1]["geoip"] is False  # the location is never derived again


def test_a_version_1_pin_is_completed_once_with_a_backup(lane):
    _engine, fp, launch = lane
    fp.write_text(json.dumps(v1_pin()))
    original = fp.read_bytes()
    plan = launch()
    assert plan.notes[0][0] == "fingerprint_completed"
    detail = plan.notes[0][1]
    assert detail["from_version"] == 1 and detail["backup"].endswith("fingerprint.json.pin-v1.bak")
    assert detail["filled"] == {"navigator.maxTouchPoints": 0, "screen.availLeft": 0, "screen.availTop": 0,
                                "window.screenX": 0}
    backup = fp.with_name("fingerprint.json.pin-v1.bak")
    assert backup.read_bytes() == original and stat.S_IMODE(backup.stat().st_mode) == 0o600

    stored = json.loads(fp.read_text())
    meta = stored.pop(PIN_META)
    assert {k: stored[k] for k in v1_pin()} == v1_pin()  # not one pinned key changed (not even addons)
    assert meta["version"] == 2 and meta["completed_from"] == 1
    assert meta["canvas_noise"] is True  # a pinned GPU: what this lane's relaunches have shown
    assert meta["geo"] == {"ip": EXIT_IP, "country": "LT"}
    assert identity(plan.config) == identity(stored)
    assert plan.config["addons"] == ["/opt/agentos/.venv/camoufox/addons/UBO"]  # the install's own

    migrated = fp.read_bytes()
    for _ in range(3):
        again = launch()
        assert again.pin is None and identity(again.config) == identity(stored)
    assert fp.read_bytes() == migrated and backup.read_bytes() == original


def test_the_migration_launch_hands_camoufox_the_old_pin_untouched(lane):
    engine, fp, launch = lane
    fp.write_text(json.dumps(v1_pin()))
    launch()
    assert engine.resolve.calls[0]["config"] == v1_pin()  # so its seeded draws are the ones seen so far


def test_a_completion_the_browser_does_not_confirm_is_not_written(lane):
    _engine, fp, launch = lane
    fp.write_text(json.dumps(v1_pin()))
    original = fp.read_bytes()
    plan = launch(browser_config={"navigator.platform": "Win32"})
    assert fp.read_bytes() == original and not fp.with_name("fingerprint.json.pin-v1.bak").exists()
    assert [kind for kind, _ in plan.notes] == []


def test_a_first_launch_the_browser_does_not_confirm_fails(lane):
    _engine, fp, launch = lane
    with pytest.raises(EngineError, match="could not be checked"):
        launch(browser_config={"navigator.platform": "Win32"})
    assert not fp.exists()


def test_a_first_launch_without_the_browser_process_fails(lane, tmp_path):
    engine, fp, _ = lane
    opts = engine.options(LaneSpec(id="a/b"), profile_dir=tmp_path / "p", fingerprint_path=fp, display=":150",
                          proxy=None)
    plan = engine.plan_launch("a/b", opts, None)
    with pytest.raises(EngineError, match="not found in /proc"):
        engine.settle_pin("a/b", fp, plan, pid=None)
    assert not fp.exists()


def test_a_moved_exit_warns_and_the_pinned_location_stays(lane, caplog):
    engine, fp, launch = lane
    launch()
    stored = fp.read_bytes()
    engine.geo = FakeGeo(where=RIGA)
    plan = launch()
    assert "no longer where the pin says" in caplog.text
    kinds = [kind for kind, _ in plan.notes]
    assert kinds == ["fingerprint_location_changed"]
    assert plan.config["timezone"] == "Europe/Vilnius" and fp.read_bytes() == stored


def test_a_changed_exit_ip_moves_only_webrtc(lane, caplog):
    engine, fp, launch = lane
    launch()
    stored = json.loads(fp.read_text())
    engine.geo = FakeGeo(ip="203.0.113.77")
    plan = launch()
    assert plan.config["webrtc:ipv4"] == "203.0.113.77"
    assert identity({k: v for k, v in plan.config.items() if k != "webrtc:ipv4"}) == \
        identity({k: v for k, v in stored.items() if k != "webrtc:ipv4"})
    assert json.loads(fp.read_text()) == stored  # the pin keeps the first exit
    assert [k for k, _ in plan.notes] == ["fingerprint_exit_ip_changed"]
    assert "WebRTC reports the new exit" in caplog.text


def test_an_exit_lookup_that_fails_does_not_stop_the_launch(lane, caplog):
    engine, _fp, launch = lane
    launch()
    engine.geo = FakeGeo(fail=OSError("no route"))
    plan = launch()
    assert plan.pin is None and "pinned location is used" in caplog.text


def test_pinned_prefs_win_even_when_camoufox_overrides_them(lane, caplog):
    pytest.importorskip("camoufox.utils")
    engine, fp, launch = lane
    launch()
    meta = json.loads(fp.read_text())[PIN_META]
    engine.resolve = FakeCamoufox(seed=99, override_prefs=True)
    for _ in range(4):
        plan = launch()
        prefs = plan.options["firefox_user_prefs"]
        assert prefs["webgl.enable-webgl2"] == meta["prefs"]["webgl.enable-webgl2"]
        env_prefs = ec.config_of_env({k.replace("PREFS", "CONFIG"): v for k, v in plan.options["env"].items()
                                      if k.startswith("CAMOU_PREFS_")})
        assert env_prefs["webgl.enable-webgl2"] == meta["prefs"]["webgl.enable-webgl2"]


def test_a_pin_from_a_newer_agentos_is_used_as_it_is(lane, caplog):
    _engine, fp, launch = lane
    fp.write_text(json.dumps({**v1_pin(), PIN_META: {"version": 3}}))
    original = fp.read_bytes()
    plan = launch()
    assert plan.pin is None and fp.read_bytes() == original and "newer than this agentos" in caplog.text
    assert identity(plan.config) == identity(v1_pin())


def test_a_version_2_pin_missing_its_record_is_completed(lane):
    _engine, fp, launch = lane
    full, _ = complete_config(v1_pin())
    fp.write_text(json.dumps({**full, PIN_META: {"version": 2}}))
    plan = launch()
    stored = json.loads(fp.read_text())
    assert plan.notes[0][1]["filled"] == {} and plan.notes[0][1]["from_version"] == 2
    assert {"prefs", "canvas_noise", "geo"} <= set(stored[PIN_META])
    assert fp.with_name("fingerprint.json.pin-v2.bak").exists()


# ---- the Firefox version follows the installed browser ---------------------------------------


def test_the_first_launch_after_a_browser_upgrade_moves_only_the_firefox_version(lane):
    from agentos.stealth import stealth

    engine, fp, launch = lane
    engine.browser_version = lambda: "158.0.1-beta.41"
    launch()
    before = json.loads(fp.read_text())
    assert launch().pin is None  # the same browser: nothing to do
    engine.browser_version = lambda: "159.0.1-beta.42"  # Camoufox was upgraded
    plan = launch()
    after = json.loads(fp.read_text())
    assert [kind for kind, _ in plan.notes] == ["fingerprint_version_bumped"]
    detail = plan.notes[0][1]
    assert detail["from"] == 158 and detail["to"] == 159 and detail["browser"] == "159.0.1-beta.42"
    assert detail["keys"] == ["navigator.userAgent"] and detail["backup"].endswith("fingerprint.json.firefox-158.bak")
    assert after["navigator.userAgent"] == before["navigator.userAgent"].replace("158.0", "159.0")
    assert plan.config["navigator.userAgent"] == after["navigator.userAgent"]  # the browser runs with it too
    assert engine.resolve.calls[-1]["config"]["navigator.userAgent"] == after["navigator.userAgent"]
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    assert changed == {"navigator.userAgent", PIN_META}  # oscpu, platform, the whole device: unchanged
    record = after[PIN_META]["firefox_updates"]
    assert [(r["from"], r["to"]) for r in record] == [(158, 159)]
    assert {k: v for k, v in after[PIN_META].items() if k != "firefox_updates"} == before[PIN_META]
    assert json.loads(fp.with_name("fingerprint.json.firefox-158.bak").read_text()) == before
    # the audit is quiet afterwards, and the next launch changes nothing
    findings = stealth.pin_findings("a/b", after, installed_major=159)
    assert not [f for f in findings if f.check == "freshness"]
    bumped = fp.read_bytes()
    assert launch().pin is None and fp.read_bytes() == bumped


def test_no_version_bump_down_unknown_or_in_the_same_launch_as_a_version_1_completion(lane):
    engine, fp, launch = lane
    engine.browser_version = lambda: "157.0-beta.30"  # older than the pin: never moved back
    launch()
    pinned = fp.read_bytes()
    assert launch().pin is None and fp.read_bytes() == pinned
    engine.browser_version = lambda: None  # the installed version is unknown
    assert launch().pin is None and fp.read_bytes() == pinned
    fp.write_text(json.dumps(v1_pin()))
    engine.browser_version = lambda: "159.0.1-beta.42"
    plan = launch()  # a version 1 pin is completed first, from the draws it has always shown
    assert [kind for kind, _ in plan.notes] == ["fingerprint_completed"]
    assert engine.resolve.calls[-1]["config"] == v1_pin()
    assert json.loads(fp.read_text())["navigator.userAgent"] == WIN_UA
    plan = launch()  # and moved on its next launch
    assert [kind for kind, _ in plan.notes] == ["fingerprint_version_bumped"]


def test_a_version_bump_the_browser_does_not_confirm_is_not_written(lane):
    engine, fp, launch = lane
    launch()
    pinned = fp.read_bytes()
    engine.browser_version = lambda: "159.0.1-beta.42"
    plan = launch(browser_config={"navigator.platform": "Win32"})
    assert fp.read_bytes() == pinned and plan.notes == []
    assert not fp.with_name("fingerprint.json.firefox-158.bak").exists()


# ---- the lane manager records what a launch did to the pin ---------------------------------


async def test_pin_notes_become_lane_events(tmp_path):
    from test_lanes import FakeEngine, make

    class NotingEngine(FakeEngine):
        async def launch(self, spec, **kw):
            session = await super().launch(spec, **kw)
            session.pin_notes = [("fingerprint_completed", {"from_version": 1, "filled": {"screen.availLeft": 0}})]
            return session

    mgr, *_ = make(tmp_path, engine=NotingEngine(), self_check=False)
    await mgr.create(LaneSpec(id="a/b"))
    await mgr.open("a/b")
    events = [e for e in mgr.events("a/b") if e.kind == "fingerprint_completed"]
    assert events and events[0].detail == {"from_version": 1, "filled": {"screen.availLeft": 0}}
    await mgr.shutdown()


# ---- hardening (docs/design/redteam.md): where Camoufox's defaults read as a bot ------------------

def test_harden_asks_for_location_reads_10_gib_and_claims_only_what_the_host_runs(caplog):
    config = {"webGl:parameters": {"3379": 8192, "34024": 8192, "3386": [16384, 16384]},
              "webGl2:parameters": {"3379": 16384}, "navigator.hardwareConcurrency": 4}
    out, prefs, changed = ec.harden(config, {ec.GEO_PREF: 1, "x": 1}, host_cores=2)
    assert prefs == {ec.GEO_PREF: 0, ec.QUOTA_PREF: ec.QUOTA_10_GIB_KB, "x": 1}
    assert out["webGl:parameters"] == config["webGl:parameters"]  # checked, never patched
    assert "does not enforce" in caplog.text
    assert out["navigator.hardwareConcurrency"] == 2
    assert set(changed) == {ec.GEO_PREF, ec.QUOTA_PREF, "navigator.hardwareConcurrency"}
    assert ec.harden(out, prefs, host_cores=8)[2] == {}  # hardening twice changes nothing


def test_a_new_linux_lane_on_a_software_node_draws_a_gpu_the_node_can_honour(tmp_path):
    pytest.importorskip("camoufox.webgl")
    engine = CamoufoxEngine(resolve=FakeCamoufox(), geo=FakeGeo(), identity_os=["linux"])
    engine.software_rendering = True
    opts = engine.options(LaneSpec(id="a/b"), profile_dir=tmp_path / "p", fingerprint_path=tmp_path / "fp.json",
                          display=":150", proxy=None)
    assert opts["webgl_config"] in ec.SOFTWARE_HOST_GPUS
    engine.software_rendering = False  # a GPU node renders on hardware: Camoufox picks as before
    assert "webgl_config" not in engine.options(LaneSpec(id="a/b"), profile_dir=tmp_path / "p",
                                                fingerprint_path=tmp_path / "fp.json", display=":150", proxy=None)


def fpgen_model_installed() -> bool:
    """Is fpgen's pinned model on this machine? Reading the recorded devices without it downloads it from
    GitHub (camoufox.fpgen_model), and a unit test never reaches the network. `camoufox fetch` installs it."""
    try:
        from camoufox.fpgen_model import fpgen_data_dir, is_pinned

        return bool(is_pinned(fpgen_data_dir()))
    except Exception:  # noqa: BLE001 - no camoufox, no fpgen, or a model this user cannot read
        return False


def test_every_recorded_device_of_a_software_gpu_claims_what_llvmpipe_enforces():
    pytest.importorskip("camoufox.webgl")
    if not fpgen_model_installed():
        pytest.skip("fpgen's model is not installed (camoufox fetch), and reading it would download it")
    from camoufox import webgl
    for vendor, renderer in ec.SOFTWARE_HOST_GPUS:
        devices = webgl.recorded_devices("lin", vendor, renderer)
        assert devices
        for d in devices:
            for section in ("webGl:parameters", "webGl2:parameters"):
                for key in ec.WEBGL_SIZE_KEYS:
                    assert d.get(section, {}).get(key, ec.MESA_MAX_SIZE) == ec.MESA_MAX_SIZE


def test_a_new_lane_is_hardened_and_its_browser_never_gets_the_position(lane):
    _engine, fp, launch = lane
    first = launch()
    stored = json.loads(fp.read_text())
    meta = stored[PIN_META]
    assert meta["hardening"] == ec.HARDENING
    assert meta["prefs"][ec.GEO_PREF] == 0 and meta["prefs"][ec.QUOTA_PREF] == ec.QUOTA_10_GIB_KB
    assert "geolocation:latitude" in stored  # kept for the exit checks
    assert stored["navigator.hardwareConcurrency"] == 2  # the fixture's host has 2 cores; the draw said 4
    for plan in (first, launch()):
        assert not any(k.startswith("geolocation:") for k in plan.config)
        assert not any(k.startswith("geolocation:") for k in config_of_env(plan.options["env"]))
        prefs = plan.options["firefox_user_prefs"]
        assert prefs[ec.GEO_PREF] == 0 and prefs[ec.QUOTA_PREF] == ec.QUOTA_10_GIB_KB
        assert json.loads(plan.options["env"]["CAMOU_PREFS_1"]) == prefs


def test_a_lane_pinned_before_hardening_keeps_its_device(lane):
    _engine, fp, launch = lane
    fp.write_text(json.dumps(v1_pin()))
    launch()  # completed to version 2, not hardened
    for _ in range(2):
        plan = launch()
        assert "hardening" not in json.loads(fp.read_text())[PIN_META]
        assert plan.config["geolocation:latitude"] == 54.6872
        assert plan.options["firefox_user_prefs"].get(ec.QUOTA_PREF) != ec.QUOTA_10_GIB_KB
