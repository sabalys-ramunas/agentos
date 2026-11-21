"""Stealth findings (openajar.stealth.stealth) and the stealth check's flow (openajar.stealth.stealthcheck),
against two real probe dumps (a Windows and a Linux identity on Camoufox 158.0.1 beta.41 in the
Docker node, tests/fixtures/stealth) and a fake lab."""

from __future__ import annotations

import asyncio
import copy
import json
import random
from pathlib import Path
from typing import Any, ClassVar

import pytest

from openajar.stealth import stealth, stealthcheck
from openajar.stealth.stealth import FAIL, OK, WARN

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stealth"
WIN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0"
LINUX_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0"


def dump(name: str = "windows") -> dict[str, Any]:
    return json.loads((FIXTURES / f"probe-{name}.json").read_text())


def levels(findings, check=None) -> list[tuple[str, str]]:
    return [(f.level, f.detail) for f in findings if check is None or f.check == check]


def bad(findings) -> list[str]:
    return [f.detail for f in findings if f.level != OK]


def pin(**over: Any) -> dict[str, Any]:
    base = {
        "navigator.userAgent": WIN_UA, "headers.User-Agent": WIN_UA, "navigator.platform": "Win32",
        "navigator.oscpu": "Windows NT 10.0; Win64; x64", "navigator.appVersion": "5.0 (Windows)",
        "navigator.hardwareConcurrency": 8, "navigator.maxTouchPoints": 0,
        "screen.width": 1536, "screen.height": 864, "screen.availWidth": 1536, "screen.availHeight": 816,
        "screen.availLeft": 0, "screen.availTop": 0, "screen.colorDepth": 24, "screen.pixelDepth": 24,
        "window.outerWidth": 1536, "window.outerHeight": 816, "window.screenX": 0, "window.screenY": 0,
        "webGl:vendor": "Google Inc. (Intel)", "webGl:renderer": "ANGLE (Intel, Intel(R) HD Graphics Direct3D11 "
        "vs_5_0 ps_5_0), or similar", "fonts": ["Arial", "Segoe UI", "Calibri"], "timezone": "Europe/Vilnius",
        "locale:language": "en", "locale:region": "US", "headers.Accept-Language": "en-US,en;q=0.9",
        "agentos:pin": {"version": 2, "geo": {"ip": "203.0.113.9", "country": "LT"}},
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


# ---- pins ---------------------------------------------------------------------------------------

def test_a_consistent_linux_pin_on_the_installed_firefox_is_ok():
    linux = pin(**{"navigator.userAgent": LINUX_UA, "headers.User-Agent": LINUX_UA,
                   "navigator.platform": "Linux x86_64", "navigator.oscpu": "Linux x86_64",
                   "webGl:renderer": "Radeon R9 200 Series, or similar", "fonts": ["Arial", "DejaVu Sans"]})
    assert bad(stealth.pin_findings("a/b", linux, installed_major=158, coherence=False)) == []


def test_a_windows_pin_on_a_linux_node_is_flagged_once():
    found = stealth.pin_findings("a/b", pin(), installed_major=158, coherence=False)
    assert levels(found) == [(WARN, found[0].detail)] and "issue 895" in found[0].detail


@pytest.mark.parametrize("over, words", [
    ({"navigator.platform": "MacIntel"}, "navigator.platform says macos"),
    ({"navigator.oscpu": "Linux x86_64"}, "navigator.oscpu says linux"),
    ({"headers.User-Agent": LINUX_UA}, "User-Agent header and navigator.userAgent differ"),
    ({"webGl:renderer": "llvmpipe (LLVM 15.0.7, 256 bits)"}, "software renderer"),
    ({"fonts": ["Segoe UI", "Ubuntu", "Cantarell"]}, "lists linux fonts: Cantarell, Ubuntu"),
    ({"window.outerWidth": 1920}, "larger than the 1536 px screen"),
])
def test_an_inconsistent_pin_fails(over, words):
    found = stealth.pin_findings("a/b", pin(**over), installed_major=158, coherence=False)
    assert any(f.level == FAIL and words in f.detail for f in found), levels(found)


def test_pin_warnings_for_locale_touch_missing_keys_and_an_old_pin():
    found = stealth.pin_findings("a/b", pin(**{"headers.Accept-Language": "de-DE,de", "screen.availLeft": None,
                                               "agentos:pin": None}), coherence=False)
    text = " | ".join(bad(found))
    assert "Accept-Language de-DE" in text and "lacks screen.availLeft" in text and "version 1 pin" in text
    mac = pin(**{"navigator.userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) Gecko/20100101 "
                 "Firefox/158.0", "headers.User-Agent": None, "navigator.platform": "MacIntel",
                 "navigator.oscpu": "Intel Mac OS X 10.15", "navigator.maxTouchPoints": 5,
                 "webGl:renderer": "Apple M1, or similar", "fonts": ["Helvetica"]})
    assert any("macos desktop with 5 touch points" in d for d in bad(stealth.pin_findings("a/b", mac,
                                                                                          coherence=False)))


def test_firefox_version_against_the_installed_browser():
    lags = stealth.pin_findings("a/b", pin(), installed_major=160, coherence=False)
    assert [(f.level, f.data) for f in lags if f.check == "freshness"] == [(WARN, {"pinned": 158, "installed": 160})]
    ahead = stealth.pin_findings("a/b", pin(), installed_major=150, coherence=False)
    assert [f.level for f in ahead if f.check == "freshness"] == [FAIL]
    assert not [f for f in stealth.pin_findings("a/b", pin(), coherence=False) if f.check == "freshness"]


def test_camoufox_coherence_rules_run_when_camoufox_is_installed():
    pytest.importorskip("camoufox.coherence")
    m1 = pin(**{"navigator.userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) Gecko/20100101 "
                "Firefox/158.0", "headers.User-Agent": None, "navigator.platform": "MacIntel",
                "navigator.oscpu": "Intel Mac OS X 10.15", "navigator.hardwareConcurrency": 2,
                "webGl:vendor": "Apple", "webGl:renderer": "Apple M1, or similar", "fonts": ["Helvetica"]})
    assert any("coherence rule" in d for d in bad(stealth.pin_findings("a/b", m1)))


def test_cross_pin_collisions_follow_the_proxies():
    a, b = pin(), pin()
    other = pin(**{"screen.width": 1920, "screen.height": 1080, "screen.availWidth": 1920,
                   "screen.availHeight": 1032, "window.outerWidth": 1920, "window.outerHeight": 1032,
                   "fonts": ["Arial", "Segoe UI"]})
    apart = stealth.cross_pin_findings({"x/a": (a, "p1"), "x/b": (b, "p2"), "x/c": (other, "p3")})
    fails = [f.detail for f in apart if f.level == FAIL]
    assert any("x/a, x/b pin one device but leave through different proxies (p1, p2)" in d for d in fails)
    assert any("proxies p1, p2, p3 were pinned from one exit IP 203.0.113.9" in d for d in fails)
    together = stealth.cross_pin_findings({"x/a": (a, "p1"), "x/b": (b, "p1")})
    assert [f.level for f in together if "pin one device" in f.detail] == [WARN]
    assert any(f.level == OK and "2 lanes leave through proxy p1" in f.detail for f in together)
    fonts = stealth.cross_pin_findings({"x/a": (a, None), "x/c": (pin(**{"screen.width": 1600}), None)})
    assert [f.detail for f in fonts if "font list" in f.detail] == ["x/a, x/c pin the same font list"]


# ---- dumps --------------------------------------------------------------------------------------

def test_the_real_dumps_show_no_automation_and_no_inconsistency():
    assert bad(stealth.dump_findings("w", dump("windows"))) == []
    assert bad(stealth.dump_findings("l", dump("linux"))) == []


@pytest.mark.parametrize("change, check, words", [
    (lambda d: d["navigator"].update(webdriver=True), "automation", "navigator.webdriver is true"),
    (lambda d: d["automation"].update(globals=["__pwInitScripts"]), "automation", "driver globals"),
    (lambda d: d["automation"].update(notNative=["Permissions.query"]), "automation", "not native code"),
    (lambda d: d["automation"].update(chrome="object"), "automation", "window.chrome is object"),
    (lambda d: d["automation"].update(webdriverOwnProperty=True), "automation", "its own webdriver"),
    (lambda d: d["worker"].update(platform="Linux x86_64"), "consistency", "a worker sees another browser"),
    (lambda d: d["worker"].update(webgl=["Mesa", "llvmpipe"]), "consistency", "WebGL in a worker names llvmpipe"),
    (lambda d: d["frame"].update(screenWidth=1920), "consistency", "a frame sees another browser"),
    (lambda d: d["css"].update(deviceWidth=False), "consistency", "CSS device-width"),
    (lambda d: d["window"].update(screenX=206, screenY=2), "consistency", "issue 974"),
    (lambda d: d["window"].update(outerWidth=1700), "consistency", "wider than the screen"),
    (lambda d: d["headers"].update({"accept-language": "de-DE,de;q=0.9"}), "consistency", "Accept-Language"),
    (lambda d: d["webgl"].update(renderer="llvmpipe (LLVM 15)"), "consistency", "software renderer"),
    (lambda d: d["fonts"].update(found=["Segoe UI", "Ubuntu Mono"]), "consistency", "shows linux fonts"),
    (lambda d: d["webrtc"].update(rawAddresses=["10.0.0.5"]), "leak", "raw addresses: 10.0.0.5"),
])
def test_a_dump_with_a_tell_fails(change, check, words):
    seen = dump("windows")
    change(seen)
    found = stealth.dump_findings("w", seen)
    assert any(f.check == check and f.level == FAIL and words in f.detail for f in found), levels(found)


def test_libreoffice_fonts_on_windows_are_ordinary():
    seen = dump("windows")
    seen["fonts"]["found"] += ["DejaVu Sans", "Liberation Serif"]
    assert bad(stealth.dump_findings("w", seen)) == []
    assert bad(stealth.pin_findings("a/b", pin(fonts=["Segoe UI", "DejaVu Sans"]), coherence=False,
                                    installed_major=158)) == [stealth.pin_findings(
                                        "a/b", pin(), coherence=False)[0].detail]


def test_two_macs_or_two_linux_desktops_may_share_fonts_and_gpu():
    linux = dump("linux")
    other = copy.deepcopy(linux)
    other["navigator"]["hardwareConcurrency"] = 4
    other["canvas"]["digest"] = "own"
    found = stealth.collision_findings({"x/1": linux, "x/2": other})
    fonts = next(f for f in found if f.data.get("surface") == "fonts")
    assert fonts.level == OK and "every stock Linux desktop shows" in fonts.detail
    assert stealth.worst(found) == OK


def test_dump_warnings():
    seen = dump("windows")
    seen["codecs"]["h264"] = ""
    seen["navigator"]["maxTouchPoints"] = 5
    seen["voices"] = "error: timeout"
    text = " | ".join(f.detail for f in stealth.dump_findings("w", seen) if f.level == WARN)
    assert "cannot play H.264" in text and "maxTouchPoints 5" in text and "voices probe failed" in text
    utc = stealth.dump_findings("w", dump("windows"), proxied=True)
    assert any("proxied lane in UTC" in f.detail for f in utc)
    moved = stealth.dump_findings("w", dump("windows"), config={"timezone": "Europe/Vilnius"})
    assert any(f.level == FAIL and "timezone is UTC, the pin's Europe/Vilnius" in f.detail for f in moved)


def test_drift_ignores_canvas_noise_and_finds_everything_else():
    a, b = dump("windows"), dump("windows")
    b["canvas"]["digest"] = "other"
    assert levels(stealth.drift_findings("w", [a, b], canvas_noise=True)) == [(OK, "2 launches show one device")]
    b["navigator"]["maxTouchPoints"] = 1
    found = stealth.drift_findings("w", [a, b], canvas_noise=True)
    assert found[0].level == FAIL and ".navigator.maxTouchPoints: 0 != 1" in found[0].detail
    linux = dump("linux")  # canvas cover off: readback repeats within a page
    assert stealth.drift_findings("l", [linux, linux], canvas_noise=True)[-1].level == WARN
    assert stealth.drift_findings("w", [a], canvas_noise=True) == []


def test_collisions_between_lanes():
    w1, w2, linux = dump("windows"), dump("windows"), dump("linux")
    w2["canvas"]["digest"] = "another session"
    w2["mediaDevices"]["ids"] = "another profile"
    found = stealth.collision_findings({"x/1": w1, "x/2": w2, "x/3": linux})
    by_surface = {f.data["surface"]: f for f in found if f.data.get("surface")}
    assert by_surface["device"].level == FAIL and by_surface["device"].data["groups"] == [["x/1", "x/2"]]
    assert by_surface["fonts"].level == WARN
    assert by_surface["audio"].level == OK and "every lane of this node" in by_surface["audio"].detail
    assert "canvas" not in by_surface and by_surface["mediaDevices"].level == OK
    apart = dump("windows")
    apart["navigator"]["hardwareConcurrency"] = 4
    for section, key in (("fonts", "digest"), ("mediaDevices", "ids"), ("canvas", "digest"), ("webgl", "params")):
        apart[section][key] = "own"
    clean = stealth.collision_findings({"x/1": w1, "x/2": apart})
    assert [f.level for f in clean if f.level != OK] == []
    assert stealth.collision_findings({"x/1": w1}) == []


def test_the_probe_page_ships_with_the_package_and_the_tests_share_it():
    assert stealth.PROBE_PAGE.exists()
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "site" / "fingerprint.html"
    assert fixture.resolve() == stealth.PROBE_PAGE.resolve()
    page = stealth.PROBE_PAGE.read_text()
    for section in ("automation", "worker", "frame", "css", "webrtc", "codecs", "innerScreenX"):
        assert section in page
    assert "—" not in page


# ---- the check, on a fake lab ----------------------------------------------------------------------

class FakeLab:
    """Lanes that dump the Windows fixture with their own canvas, media devices and core count."""

    proxied = False
    installed_major = 158
    versions: ClassVar[dict[str, str]] = {"camoufox": "0.5.9", "camoufox_browser": "158.0.1-beta.41"}

    def __init__(self, *, available: list[int] | None = None, disturb: bool = False, unrecoverable: bool = False):
        self.lanes: dict[str, dict[str, Any]] = {}
        self.warm: set[str] = set()
        self.dead: set[str] = set()
        self.available = list(available or [6000])
        self.disturb = disturb
        self.unrecoverable = unrecoverable
        self.pids = 100
        self.calls: list[str] = []
        self.stopped = False

    async def start(self):
        self.calls.append("start")

    async def stop(self):
        self.stopped = True
        self.warm.clear()

    async def create(self, lane, *, os_name=None):
        n = len(self.lanes) + 1
        self.lanes[lane] = {"n": n, "os": os_name, "pid": None,
                            "pin": pin(**{"navigator.hardwareConcurrency": 4 + n, "fonts": [f"Font {n}"],
                                          "timezone": "UTC"})}

    async def wake(self, lane):
        if lane in self.dead and self.unrecoverable:
            raise RuntimeError("the browser failed to start")
        self.dead.discard(lane)
        if lane not in self.warm:
            self.pids += 1
            self.lanes[lane]["pid"] = self.pids
            self.warm.add(lane)
        self.calls.append(f"wake {lane}")

    async def sleep(self, lane):
        self.warm.discard(lane)

    async def dump(self, lane):
        if lane not in self.warm:
            await self.wake(lane)
        n = self.lanes[lane]["n"]
        d = dump("windows")
        d["navigator"]["hardwareConcurrency"] = 4 + n
        d["worker"]["hardwareConcurrency"] = d["frame"]["hardwareConcurrency"] = 4 + n
        d["fonts"]["digest"] = d["mediaDevices"]["ids"] = d["canvas"]["digest"] = f"lane {n}"
        d["webgl"]["params"] = f"gpu {n}"
        return d

    async def visit(self, lane, url, out):
        return {"url": url}

    async def kill(self, lane):
        self.warm.discard(lane)
        self.dead.add(lane)
        if self.disturb:
            for other in self.lanes:
                if other != lane and other in self.warm:
                    self.pids += 1
                    self.lanes[other]["pid"] = self.pids
        return [self.lanes[lane]["pid"]]

    def main_pid(self, lane):
        return self.lanes[lane]["pid"] if lane in self.warm else None

    def pin(self, lane):
        return copy.deepcopy(self.lanes[lane]["pin"])

    def lane_memory_mb(self, lane):
        return 600.0 if lane in self.warm else None

    def available_mb(self):
        return self.available.pop(0) if len(self.available) > 1 else self.available[0]

    def total_mb(self):
        return 8000

    def census(self):
        return {"browsers": len(self.warm), "xvfb": len(self.warm), "x11vnc": len(self.warm), "driver": 0, "fds": 20}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 7.0
        return self.now


async def test_the_check_measures_relaunches_memory_and_finds_no_problem(tmp_path):
    lab = FakeLab(available=[6000, 6000, 6000, 4200])
    opts = stealthcheck.Options(lanes=3, relaunches=2, out=tmp_path)
    report = await stealthcheck.run(lab, opts)
    assert [f.detail for f in report.findings if f.level == FAIL] == []
    assert {lane: len(w) for lane, w in report.wakes.items()} == {"stealth/l1": 3, "stealth/l2": 3, "stealth/l3": 3}
    assert report.memory["per_lane_delta_mb"] == 600 and report.memory["browser_pss_mean_mb"] == 600
    assert report.memory["capacity"]["max_warm"] == {"this node": 8, "4 GB": 2, "8 GB": 8, "16 GB": 22}
    assert lab.stopped and not lab.warm
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["level"] == report.level and len(list((tmp_path / "dumps").iterdir())) == 9
    assert len(list((tmp_path / "pins").iterdir())) == 3
    text = (tmp_path / "report.txt").read_text()
    assert "max warm lanes (600 MB each, 800 MB floor): this node 8" in text and "—" not in text


async def test_the_check_stops_before_memory_runs_low(tmp_path):
    lab = FakeLab(available=[6000, 2500, 1900])
    report = await stealthcheck.run(lab, stealthcheck.Options(lanes=3, relaunches=1, min_available_mb=2048))
    assert report.stopped and "1900 MB" in report.stopped and "did not wake stealth/l2" in report.stopped
    assert lab.stopped and not lab.warm


async def test_the_soak_kills_a_lane_and_checks_the_others():
    lab = FakeLab()
    report = stealthcheck.Report()
    opts = stealthcheck.Options(lanes=3, soak_minutes=1, kill_every=20)
    for lane in ("x/1", "x/2", "x/3"):
        await lab.create(lane)
    await stealthcheck.soak(lab, opts, report, ["x/1", "x/2", "x/3"], rng=random.Random(3), clock=Clock())
    kills = report.soak["kills"]
    assert kills and all(k["bystander_ok"] and k["bystander_untouched"] and k["recovered"] and k["same_device"]
                         for k in kills)
    assert [f.level for f in report.findings] == [OK] and not lab.warm


async def test_the_soak_reports_a_disturbed_bystander_and_a_lane_that_does_not_come_back():
    lab = FakeLab(disturb=True, unrecoverable=True)
    report = stealthcheck.Report()
    for lane in ("x/1", "x/2"):
        await lab.create(lane)
    await stealthcheck.soak(lab, stealthcheck.Options(soak_minutes=0.5, kill_every=10), report, ["x/1", "x/2"],
                            rng=random.Random(1), clock=Clock())
    text = " | ".join(f.detail for f in report.findings if f.level == FAIL)
    assert "its browser process changed" in text and "did not wake again" in text


def test_soak_findings_see_descriptors_grow():
    samples = [{"fds": 20, "driver": 1}] * 4 + [{"fds": 90, "driver": 1}] * 4
    found = stealthcheck.soak_findings({"cycles": 8, "loads": 8, "kills": [], "samples": samples})
    assert [f.detail for f in found] == ["fds grew during the soak (20 early, 90 late)"]


def test_slots_never_touch_the_poc_or_the_production_node():
    assert stealthcheck.slot(33).display_base == 330 and stealthcheck.slot(33).site_port == 6339
    for number in (10, 60):
        with pytest.raises(ValueError, match="11 to 59"):
            stealthcheck.slot(number)
    assert stealthcheck.slot(11).vnc_base == 5110


def test_a_slot_that_overlaps_a_node_on_this_machine_is_refused(tmp_path):
    from openajar.core.config import NodeConfig

    # a production node.toml with more slots: displays :110 to :129, viewer ports 6100 to 6119
    big = NodeConfig(home=tmp_path / "prod", max_slots=20, display_base=110)
    for number in (11, 12):
        with pytest.raises(ValueError, match=r"of the node in .*prod"):
            stealthcheck.slot(number, [big])
    assert stealthcheck.slot(13, [big]).display_base == 130
    moved = NodeConfig(home=tmp_path / "dev", display_base=400, viewer_base=6330, vnc_base=7000)
    with pytest.raises(ValueError, match="port 6330 of the node"):
        stealthcheck.slot(33, [moved])
    with pytest.raises(ValueError, match="display :400 of the node"):
        stealthcheck.slot(40, [moved])
    with pytest.raises(ValueError, match="overlaps the node home"):
        stealthcheck.slot(33, [NodeConfig(home=tmp_path)], home=tmp_path / "scratch")


def test_node_configs_reads_every_home_and_refuses_one_it_cannot_read(tmp_path):
    prod, dev, gone = tmp_path / "prod", tmp_path / "dev", tmp_path / "gone"
    prod.mkdir()
    (prod / "node.toml").write_text("max_slots = 20\ndisplay_base = 110\n")
    dev.mkdir()  # no node.toml: a fresh node's defaults
    configs = stealthcheck.node_configs([prod, dev, gone, prod])
    assert [(c.home, c.max_slots, c.display_base) for c in configs] == [(prod, 20, 110), (dev, 10, 100)]
    (dev / "node.toml").write_text("max_slots = [")
    with pytest.raises(ValueError, match="cannot read"):
        stealthcheck.node_configs([dev])


async def test_the_checks_display_sweep_kills_only_its_own_homes_processes(tmp_path, monkeypatch):
    from openajar.core.config import NodeConfig
    from openajar.node import display

    config = NodeConfig(home=tmp_path / "scratch", display_base=110, max_slots=9)
    own = str(config.home / "vnc" / "display-111.xauth")
    procs = [(501, ["Xvfb", ":110", "-screen", "0", "1920x1080x24", "-auth", "/var/lib/agentos/vnc/display-110.xauth"]),
             (502, ["x11vnc", "-display", ":110", "-auth", "/var/lib/agentos/vnc/display-110.xauth"]),
             (503, ["Xvfb", ":111", "-auth", own]), (504, ["x11vnc", "-display", ":111", "-auth", own])]
    killed: list[int] = []

    async def kill(pids, **_):
        killed.extend(pids)

    monkeypatch.setattr(display, "iter_processes", lambda: iter(procs))
    monkeypatch.setattr(display, "kill_processes", kill)
    monkeypatch.setattr(display.DisplayManagerImpl, "_clear_stale_lock", lambda self, num: True)
    assert await display.DisplayManagerImpl(config, sweep_own_only=True).sweep() == [503, 504]
    assert killed == [503, 504]
    assert await display.DisplayManagerImpl(config).sweep() == [501, 502, 503, 504]  # agentosd's own sweep


async def test_the_next_check_stops_what_a_killed_one_left(tmp_path, monkeypatch):
    from openajar.node import display
    from openajar.node.camoufox import engine as engine_camoufox

    dead, live, mine = (tmp_path / f"openajar-stealth-{n}" for n in ("dead0001", "live0001", "mine0001"))
    for home in (dead, live, mine):
        (home / "vnc").mkdir(parents=True)
    (dead / stealthcheck.OWNER_FILE).write_text("999999999 1\n")  # no such process
    stealthcheck.claim_home(live)  # this test's own pid stands in for a check that still runs
    gone = tmp_path / "openajar-stealth-gone0001"  # removed, but its browser was left running
    unrelated = tmp_path / "openajar-stealth-notes"  # not a scratch home's name
    unrelated.mkdir()
    procs = [(601, ["Xvfb", ":330", "-auth", f"{dead}/vnc/display-330.xauth"]),
             (602, [engine_camoufox.BROWSER_BIN, "-profile", f"{dead}/lanes/stealth/l1/profile"]),
             (603, [engine_camoufox.BROWSER_BIN, "-profile", f"{gone}/lanes/stealth/l1/profile"]),
             (604, ["Xvfb", ":331", "-auth", f"{live}/vnc/display-331.xauth"]),
             (605, ["Xvfb", ":332", "-auth", f"{mine}/vnc/display-332.xauth"]),
             (606, ["python3", f"{dead}/notes.py"])]  # not a display or a browser: never touched
    killed: list[int] = []

    async def kill(pids, **_):
        killed.extend(pids)

    monkeypatch.setattr(display, "iter_processes", lambda: iter(procs))
    monkeypatch.setattr(display, "kill_processes", kill)
    stopped = await stealthcheck.sweep_leftovers(tmp_path, keep=mine)
    assert sorted(stopped) == sorted(killed) == [601, 602, 603]
    assert not dead.exists() and live.exists() and mine.exists() and unrelated.exists()


async def test_sigterm_and_sighup_close_the_lanes_like_ctrl_c(tmp_path):
    import os
    import signal

    for signum in (signal.SIGTERM, signal.SIGHUP):
        lab = FakeLab()
        real_wake = lab.wake

        async def wake(lane, lab=lab, real_wake=real_wake, signum=signum):
            await real_wake(lane)
            if lane == "stealth/l2":
                os.kill(os.getpid(), signum)
                await asyncio.sleep(5)

        lab.wake = wake
        with pytest.raises(stealthcheck.Stopped) as stopped:
            await stealthcheck.run_until_stopped(lab, stealthcheck.Options(lanes=3, relaunches=1))
        assert stopped.value.signum == signum
        assert lab.stopped and not lab.warm and "wake stealth/l3" not in lab.calls


def test_wake_summary_and_capacity():
    summary = stealthcheck.wake_summary({"a": [8.0, 2.0, 3.0], "b": [9.0, 2.5]})
    assert summary["first_launch"] == {"n": 2, "median_s": 8.5, "p90_s": 9.0, "max_s": 9.0}
    assert summary["relaunch"]["n"] == 3 and summary["relaunch"]["median_s"] == 2.5
    assert stealthcheck.warm_capacity(3800, 1600, 640, 800)["max_warm"]["this node"] == 2
    assert stealthcheck.warm_capacity(3800, 1600, None, 800) == {}


def test_audit_reads_every_pin_on_a_node(tmp_path):
    def put(lane: str, data: Any) -> None:
        path = tmp_path / "lanes" / lane / "fingerprint.json"
        path.parent.mkdir(parents=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))

    put("biz/ig", pin())
    put("biz/fb", pin())
    put("me/reddit", pin(**{"navigator.platform": "MacIntel"}))
    put("me/broken", "{")
    (tmp_path / "lanes" / "me" / "cold").mkdir()  # not pinned yet: nothing to read
    findings, count = stealth.audit_pins(tmp_path, installed_major=158)
    assert count == 3
    text = stealth.format_findings(findings)
    assert any("fail" in line and "me/broken" in line and "cannot be read" in line for line in text)
    assert any("me/reddit" in line and "navigator.platform says macos" in line for line in text)
    assert any("biz/fb, biz/ig pin one device behind one proxy" in f.detail for f in findings)
    assert stealth.worst(findings) == FAIL
