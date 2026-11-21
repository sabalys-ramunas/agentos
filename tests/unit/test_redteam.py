"""The red-team bench: detectors on synthetic visits, the detector site's server, the scenario's
ref lookup and the report. No browser."""

from __future__ import annotations

import json
import math
import random
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

from openajar.stealth.redteam import detectors as d
from openajar.stealth.redteam import engine, runner, scenario, stats
from openajar.stealth.redteam.server import DetectorServer

FIREFOX_DOC = ["Host", "User-Agent", "Accept", "Accept-Language", "Accept-Encoding", "Connection", "Referer",
               "Cookie", "Upgrade-Insecure-Requests", "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site",
               "Sec-Fetch-User", "Priority"]
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0"


# ---- building visits ----------------------------------------------------------------------------

class Visit:
    """Events of one page, written the way the sensor writes them."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.t = 1000.0
        self.x, self.y = 400.0, 300.0
        self.events: list[dict] = []
        self.scroll_y = 0

    def ev(self, t: str, **kw) -> dict:
        e = {"t": t, "ts": round(self.t, 1), "raf": round(self.rng.uniform(0, 16), 1), "tr": True, "f": True, "v": True}
        e.update(kw)
        self.events.append(e)
        return e

    def move_to(self, x: float, y: float, *, human: bool) -> None:
        sx, sy = self.x, self.y
        n = 30
        bend = self.rng.uniform(40, 90) if human else 0.0
        overshoot = [0.97, 1.0, 1.03, 1.05, 1.04, 1.02]  # past the target, then back
        for i in range(1, n + 1):
            f = i / n
            ease = f * f * (3 - 2 * f)
            if human and i > n - len(overshoot):
                ease = overshoot[i - (n - len(overshoot)) - 1]
            nx = sx + (x - sx) * ease - math.sin(math.pi * f) * bend * (y - sy) / max(abs(y - sy) + abs(x - sx), 1)
            ny = sy + (y - sy) * ease + math.sin(math.pi * f) * bend * (x - sx) / max(abs(y - sy) + abs(x - sx), 1)
            self.t += 16
            self.ev("mousemove", x=round(nx), y=round(ny), mx=round(nx - self.x), my=round(ny - self.y))
            self.x, self.y = round(nx), round(ny)
        if human:
            self.t += 16
            self.ev("mousemove", x=round(x), y=round(y), mx=round(x - self.x), my=round(y - self.y))
        self.x, self.y = round(x), round(y)

    def click(self, *, hold: float, pause: float) -> None:
        self.t += pause
        self.ev("mousedown", x=self.x, y=self.y, b=1)
        self.t += hold
        self.ev("mouseup", x=self.x, y=self.y)
        self.ev("click", x=self.x, y=self.y)

    def key(self, ch: str, *, hold: float, keyless: bool = False, shift: bool | None = None) -> None:
        cls = "A" if ch.isalpha() and ch.isupper() else "a" if ch.isalpha() else "c"
        code = "Key" + ch.upper() if ch.isascii() and ch.isalpha() else "Digit1"
        sh = ch.isupper() if shift is None else shift
        if not keyless:
            self.ev("keydown", k=cls, c=code, sh=sh, cl=False, rep=False, ch=ord(ch))
        self.ev("beforeinput", it="insertText", ch=ord(ch), n=1, comp=False)
        self.ev("input", it="insertText", ch=ord(ch), n=1, comp=False)
        if not keyless:
            self.t += hold
            self.ev("keyup", k=cls, c=code, sh=sh, cl=False, rep=False, ch=ord(ch))

    def wheel(self, notches: int, gap) -> None:
        for _ in range(notches):
            self.t += gap()
            self.ev("wheel", x=self.x, y=self.y, dx=0, dy=48, dm=0)
            self.scroll_y += 48
            self.t += 5
            self.ev("scroll", sy2=self.scroll_y, tg="doc")


def page(v: Visit, path: str = "/signup") -> dict:
    frames = [i * 16.67 for i in range(120)]
    env = {"userAgent": UA, "language": "en-US", "languages": ["en-US", "en"], "cores": 4, "timeZone": "Europe/Vilnius",
           "screen": [1920, 1080, 1920, 1080], "dpr": 1,
           "cpu": {"claimed": 4, "oneMs": 180, "manyMs": 200, "effective": 3.6},
           "gpu": {"available": True, "renderer": "AMD Radeon RX 580", "msPerDraw": 1.5}, "timerMs": 1}
    return {"meta": {"path": path}, "env": env, "frames": frames, "events": v.events}


def doc(path: str, site: str = "same-origin", order: list[str] = FIREFOX_DOC, extra=()) -> dict:
    values = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.5", "Sec-Fetch-Dest": "document",
              "Sec-Fetch-Site": site}
    headers = [[k, values.get(k, "x")] for k in order] + [list(e) for e in extra]
    return {"method": "GET", "path": path, "query": "", "headers": headers}


TEXT = "Jonas Petraitis labas mėgstu ilgus žygius prie upės ir kavą su draugais šeštadienį ryte"


def bot_session(seed: int = 1) -> dict:
    rng = random.Random(seed)
    v = Visit(rng)
    for target in [(500, 200), (300, 420), (650, 380), (200, 150), (520, 600), (420, 260), (700, 520), (260, 330)]:
        v.move_to(*target, human=False)
        v.click(hold=rng.uniform(60, 150), pause=rng.uniform(90, 260))
    for ch in TEXT:
        v.t += rng.uniform(70, 190)
        v.key(ch, hold=1, keyless=not ch.isascii())
    for _ in range(4):
        v.t += 800
        v.wheel(rng.randint(3, 6), lambda: rng.uniform(20, 60))
    reqs = [doc("/", site="none"), doc("/signup"), doc("/feed"), doc("/feed", site="none")]
    return {"id": f"bot-{seed}", "requests": reqs, "pages": [page(v)]}


def human_session(seed: int = 2) -> dict:
    rng = random.Random(seed)
    v = Visit(rng)
    for target in [(500, 200), (300, 420), (650, 380), (200, 150), (520, 600), (420, 260)]:
        v.move_to(*target, human=True)
        v.click(hold=rng.lognormvariate(math.log(95), 0.3), pause=rng.lognormvariate(math.log(180), 0.5))
    typed: list[tuple[float, int, str, dict]] = []  # (time, order, type, fields)
    t = v.t
    for i, ch in enumerate(TEXT):
        # about a third of the keys go down before the last one comes up, as fluent typists do
        t += rng.lognormvariate(math.log(60 if rng.random() < 0.35 else 160), 0.4)
        hold = rng.lognormvariate(math.log(90), 0.3)
        cls = "A" if ch.isupper() else "a" if ch.isalpha() else "c"
        key = {"k": cls, "c": f"Key{i}", "sh": ch.isupper(), "cl": False, "rep": False, "ch": ord(ch)}
        typed.append((t, len(typed), "keydown", key))
        typed.append((t + 1, len(typed), "input", {"it": "insertText", "ch": ord(ch), "n": 1, "comp": False}))
        typed.append((t + hold, len(typed), "keyup", key))
        if i % 25 == 24:  # a typo, fixed
            t += 250
            back = {"k": "Backspace", "c": "Backspace", "sh": False, "cl": False, "rep": False, "ch": None}
            typed.append((t, len(typed), "keydown", back))
            typed.append((t + 80, len(typed), "keyup", back))
    for when, _, kind, fields in sorted(typed):
        v.t = when
        v.ev(kind, **fields)
    for _ in range(4):
        v.t += 900
        v.wheel(rng.randint(2, 8), lambda: rng.lognormvariate(math.log(35), 0.6))
    reqs = [doc("/", site="none"), doc("/signup"), doc("/feed"), doc("/feed")]
    return {"id": f"human-{seed}", "human": True, "requests": reqs, "pages": [page(v)]}


def levels(session: dict) -> dict[str, str]:
    return {v.id: v.level for v in d.judge(session)}


# ---- detectors ------------------------------------------------------------------------------------

def test_a_bot_visit_is_caught_on_what_our_input_path_gets_wrong():
    lv = levels(bot_session())
    assert lv["kbd.dwell"] == d.CAUGHT
    assert lv["kbd.keyless_text"] == d.CAUGHT
    assert lv["kbd.rollover"] == d.SUSPICIOUS
    assert lv["kbd.corrections"] == d.SUSPICIOUS
    assert lv["nav.typed_urls"] == d.SUSPICIOUS
    assert d.summary(d.judge(bot_session()))["outcome"] == "detected"


def test_a_human_visit_trips_nothing():
    verdicts = d.judge(human_session())
    assert [v.id for v in verdicts if v.level == d.CAUGHT] == []
    assert [v.id for v in verdicts if v.level == d.SUSPICIOUS] == []
    assert d.summary(verdicts)["outcome"] == "passed"


def test_keyless_emoji_alone_is_only_suspicious():
    v = Visit(random.Random(3))
    for ch in "hello":
        v.t += 150
        v.key(ch, hold=90)
    v.t += 300
    v.key("\U0001F600", hold=0, keyless=True)
    verdict = d.kbd_keyless_text(d.Session({"pages": [page(v)]}))
    assert verdict.level == d.SUSPICIOUS
    assert verdict.evidence["symbols_without_key"] == 1


def test_a_capital_without_shift_is_caught():
    v = Visit(random.Random(4))
    v.key("J", hold=90, shift=False)
    assert d.kbd_shift(d.Session({"pages": [page(v)]})).level == d.CAUGHT


def test_a_press_where_the_pointer_never_went_is_caught():
    v = Visit(random.Random(5))
    v.move_to(100, 100, human=True)
    v.x, v.y = 600, 500  # the press lands somewhere else
    v.click(hold=90, pause=150)
    assert d.mouse_teleport(d.Session({"pages": [page(v)]})).level == d.CAUGHT


def test_wheel_away_from_the_pointer_and_scrolling_with_no_input_are_caught():
    v = Visit(random.Random(6))
    v.move_to(300, 300, human=True)
    v.ev("wheel", x=10, y=10, dx=0, dy=48, dm=0)
    s = d.Session({"pages": [page(v)]})
    assert d.wheel_pointer(s).level == d.CAUGHT
    v2 = Visit(random.Random(7))
    v2.t = 5000
    v2.ev("scroll", sy2=0, tg="doc")
    v2.t += 50
    v2.ev("scroll", sy2=900, tg="doc")  # nothing scrolled it: a script did
    assert d.scroll_unexplained(d.Session({"pages": [page(v2)]})).level == d.CAUGHT


def test_input_into_an_unfocused_window_is_caught():
    v = Visit(random.Random(8))
    v.move_to(300, 300, human=True)
    v.click(hold=90, pause=150)
    v.events[-3]["f"] = False
    assert d.env_focus(d.Session({"pages": [page(v)]})).level == d.CAUGHT


def test_proxy_headers_and_a_wrong_order_are_caught():
    s = d.Session({"requests": [doc("/", extra=[("Via", "1.1 squid")])], "pages": []})
    assert d.http_headers(s).level == d.CAUGHT
    shuffled = ["User-Agent", "Host", *FIREFOX_DOC[2:]]
    assert d.http_headers(d.Session({"requests": [doc("/", order=shuffled)], "pages": []})).level == d.CAUGHT
    assert d.http_headers(d.Session({"requests": [doc("/")], "pages": []})).level == d.CLEAN


def test_hardware_that_does_not_perform_as_claimed_is_caught():
    p = page(Visit(random.Random(9)))
    p["env"]["cpu"] = {"claimed": 8, "oneMs": 180, "manyMs": 720, "effective": 2.0}
    p["env"]["gpu"] = {"available": True, "renderer": "NVIDIA GeForce GTX 1060", "msPerDraw": 85.0}
    s = d.Session({"pages": [p]})
    assert d.env_cores(s).level == d.CAUGHT
    assert d.env_gpu(s).level == d.CAUGHT
    p["env"]["gpu"]["renderer"] = "llvmpipe (LLVM 17.0.6, 256 bits)"
    assert d.env_gpu(d.Session({"pages": [p]})).level == d.SUSPICIOUS


def test_an_odd_refresh_rate_is_suspicious():
    p = page(Visit(random.Random(10)))
    p["frames"] = [i * 33.3 for i in range(100)]  # 30 Hz
    assert d.env_frames(d.Session({"pages": [p]})).level == d.SUSPICIOUS


def test_statistics():
    rng = random.Random(11)
    flat = [rng.uniform(70, 190) for _ in range(300)]
    skewed = [rng.lognormvariate(math.log(150), 0.5) for _ in range(300)]
    assert stats.looks_uniform(flat)
    assert not stats.looks_uniform(skewed)
    assert not stats.looks_uniform(flat[:50])  # too few to say
    assert stats.ks_2samp(flat, flat) == 0
    assert stats.ks_2samp(flat, [x + 1000 for x in flat]) == 1
    assert d.phase_concentration([5.0] * 50) == pytest.approx(1.0)
    assert d.phase_concentration([i * 16.667 / 50 for i in range(50)]) < 0.05


def test_pooled_detectors_compare_with_the_people():
    bots = [bot_session(s) for s in range(3)]
    humans = [human_session(s) for s in range(10, 13)]
    pooled = {v.id: v for v in d.judge_pooled(bots, humans)}
    assert pooled["pooled.kbd.hold"].level == d.CAUGHT
    assert pooled["pooled.kbd.flight"].level in (d.CAUGHT, d.SUSPICIOUS)
    same = {v.id: v.level for v in d.judge_pooled(humans[:2], humans[2:])}
    assert same["pooled.kbd.hold"] == d.CLEAN


def test_pooled_detectors_without_people_look_for_flat_random_ranges():
    pooled = {v.id: v for v in d.judge_pooled([bot_session(s) for s in range(16)])}
    assert pooled["pooled.mouse.arrival_to_press"].level == d.SUSPICIOUS
    assert pooled["pooled.scroll.wheel_gap"].level == d.SUSPICIOUS
    assert "needs a human baseline" in pooled["pooled.kbd.hold"].detail


def test_lanes_that_are_one_device_are_linked():
    env = page(Visit(random.Random(12)))["env"]
    other = {**env, "screen": [2560, 1440, 2560, 1400], "cores": 8, "gpu": {"renderer": "Intel UHD 630"},
             "timeZone": "Europe/Riga", "cpu": {"claimed": 8, "oneMs": 260}}
    links = {v.id: v.level for v in d.link_lanes({"a": env, "b": dict(env), "c": other})}
    assert links["link.a~b"] == d.CAUGHT
    assert links["link.a~c"] == d.CLEAN
    # measured on the node: two Linux lanes behind one exit, the same rare GPU name, different speeds
    node = {"userAgent": UA, "screen": [1920, 1080, 1920, 1080], "cores": 4, "timeZone": "Europe/Berlin",
            "languages": ["en-US", "en"], "gpu": {"renderer": "Intel(R) HD Graphics 400, or similar"}, "dpr": 1}
    one = {**node, "userAgent": UA.replace("x86_64", "i686"), "cpu": {"oneMs": 321}}
    two = {**node, "cpu": {"oneMs": 284}}
    assert d.link_lanes({"l1": one, "l2": two})[0].level == d.SUSPICIOUS


# ---- the server -----------------------------------------------------------------------------------

@pytest.fixture
def server():
    s = DetectorServer().start()
    yield s
    s.stop()


def test_the_server_records_a_visit_by_its_cookie(server):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    html = opener.open(f"{server.base}/?run=lane1-r0").read().decode()
    assert "sensor.js" in html
    assert opener.open(f"{server.base}/sensor.js").status == 200
    batch = {"page": "p1", "seq": 0, "meta": {"path": "/"}, "env": {"cores": 4}, "events": [{"t": "mousemove", "ts": 1}],
             "frames": [1, 2, 3]}
    opener.open(urllib.request.Request(f"{server.base}/collect", data=json.dumps(batch).encode(), method="POST"))
    opener.open(urllib.request.Request(f"{server.base}/collect", data=json.dumps(
        {"page": "p1", "seq": 1, "events": [{"t": "click", "ts": 2}], "final": True}).encode(), method="POST"))
    opener.open(f"{server.base}/fingerprint.html").read()  # the bench's probe, not part of the visit
    s = server.store.session("lane1-r0")
    assert [r["path"] for r in s["requests"]] == ["/", "/sensor.js"]
    (p,) = s["pages"]
    assert [e["t"] for e in p["events"]] == ["mousemove", "click"]
    assert p["env"] == {"cores": 4} and p["final"] and p["frames"] == [1, 2, 3]


def test_the_server_keeps_header_names_and_their_order(server):
    import http.client
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port)
    conn.putrequest("GET", "/?run=order-1", skip_host=True, skip_accept_encoding=True)
    for name in ("Host", "User-Agent", "Zeta", "Accept", "Alpha"):
        conn.putheader(name, "x" if name != "Host" else f"{host}:{port}")
    conn.endheaders()
    conn.getresponse().read()
    conn.close()
    names = [k for k, _ in server.store.session("order-1")["requests"][0]["headers"]]
    assert names == ["Host", "User-Agent", "Zeta", "Accept", "Alpha"]


def test_the_server_ignores_bad_run_names_and_strangers(server):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    opener.open(f"{server.base}/?run=../../etc").read()
    opener.open(urllib.request.Request(f"{server.base}/collect", data=b"{}", method="POST"))
    assert server.store.ids() == []


def test_a_human_visit_is_marked(server):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    opener.open(f"{server.base}/?run=human-1&mode=human").read()
    assert server.store.session("human-1")["human"] is True


def test_the_site_has_no_em_dash():
    site = Path(scenario.__file__).with_name("site")
    for f in site.iterdir():
        if f.is_file():  # vendor/ is third-party code, not our text
            assert "—" not in f.read_text(), f.name


# ---- the scenario and the report ------------------------------------------------------------------

SNAPSHOT = """- generic [active]:
  - link "Home" [ref=f1e4] [cursor=pointer]:
    - /url: /
  - textbox "Full name" [ref=e10]
  - button "Like post 25" [ref=e40] [cursor=pointer]
  - combobox "Country" [ref=e16]:
"""


def test_find_ref():
    assert scenario.find_ref(SNAPSHOT, "link", "Home") == "f1e4"
    assert scenario.find_ref(SNAPSHOT, "textbox", "Full name") == "e10"
    assert scenario.find_ref(SNAPSHOT, "button", "Like post 25") == "e40"
    with pytest.raises(scenario.StepFailed):
        scenario.find_ref(SNAPSHOT, "button", "Like post 2")


def test_report_record_and_compare(tmp_path):
    report = runner.Report(backend="local", versions={"camoufox_browser": "158.0.1-beta.41"})
    report.sessions = {"redteam/lane1": [bot_session()]}
    report.verdicts = {"bot-1": d.judge(bot_session())}
    report.pooled = d.judge_pooled([bot_session()])
    text = runner.format_report(report)
    assert text.startswith("red team: DETECTED  (1 of 1 visits caught")
    assert "CAUGHT   kbd.dwell" in text and "fix: hold each key" in text
    assert "—" not in text
    path = tmp_path / "redteam.jsonl"
    assert runner.record(report, path) is None
    previous = runner.record(report, path)
    assert previous["levels"]["kbd.dwell"] == d.CAUGHT
    previous["levels"]["kbd.dwell"] = d.CLEAN
    assert runner.compare(previous, report) == ["worse: kbd.dwell clean -> caught"]


def test_human_baselines_load_and_false_positives_are_reported(tmp_path):
    (tmp_path / "h.json").write_text(json.dumps(bot_session(5)))  # a "person" the detectors catch
    humans = runner.load_humans([tmp_path])
    assert humans[0]["human"] is True
    report = runner.Report(backend="local", versions={})
    report.false_positives = {"bot-5": ["kbd.dwell"]}
    assert "false positives" in runner.format_report(report)


def test_probe_findings_become_verdicts():
    dump = {"automation": {"webdriver": True, "globals": [], "notNative": [], "attributes": []},
            "navigator": {"userAgent": UA, "platform": "Linux x86_64", "oscpu": "Linux x86_64"}}
    verdicts = runner.probe_verdicts("redteam/lane1", dump, None)
    assert any(v.layer == "fingerprint" and v.level == d.CAUGHT for v in verdicts)


# ---- the engine probe and the input path, with values measured on the node VM (2026-11-05) --------

PROD_LANE = {  # a production lane: Camoufox 158 on the 2 vCPU VM, Xvfb, llvmpipe, no sound card, 38 GB disk
    "permissions": {"geolocation": "granted", "notifications": "prompt"},
    "webglTruth": {"renderer": "Intel(R) HD Graphics 400, or similar", "claimedMaxTexture": 4096,
                   "allocatesClaimed": True, "allocatesDouble": True, "listedButUnavailable": []},
    "audio": {"sampleRate": 44100, "baseLatency": 0, "outputLatency": 0, "channels": 0, "state": "suspended"},
    "storage": {"quotaGB": 4, "persisted": False},
    "mediaCapabilities": {"h264_1080": "110", "vp9_1080": "110", "av1_1080": "110", "hevc_1080": "111"},
}
DESKTOP = {  # what a stock Firefox on an ordinary desktop shows
    "permissions": {"geolocation": "prompt"},
    "webglTruth": {"renderer": "Mesa Intel(R) UHD Graphics 620", "claimedMaxTexture": 16384, "allocatesDouble": False,
                   "listedButUnavailable": []},
    "audio": {"channels": 2}, "storage": {"quotaGB": 10.7}, "mediaCapabilities": {"h264_1080": "111"},
}


def test_the_engine_probe_catches_what_a_production_lane_shows():
    lv = {v.id: v.level for v in engine.judge_engine(PROD_LANE)}
    assert lv == {"engine.geo_permission": d.SUSPICIOUS, "engine.webgl_limits": d.CAUGHT, "engine.audio": d.SUSPICIOUS,
                  "engine.quota": d.SUSPICIOUS, "engine.hw_decode": d.SUSPICIOUS}


def test_a_claimed_webgl_size_that_does_not_allocate_is_caught():
    # measured on the node: an NVIDIA GTX 980 identity claims 32768 on llvmpipe, which enforces 16384
    probe = {"webglTruth": {"renderer": "NVIDIA GeForce GTX 980, or similar", "claimedMaxTexture": 32768,
                            "allocatesClaimed": False, "allocatesDouble": False, "listedButUnavailable": []}}
    (v,) = [v for v in engine.judge_engine(probe) if v.id == "engine.webgl_limits"]
    assert v.level == d.CAUGHT and "does not allocate" in v.detail


def test_the_engine_probe_passes_an_ordinary_desktop():
    assert {v.level for v in engine.judge_engine(DESKTOP)} == {d.CLEAN}
    assert {v.level for v in engine.judge_engine({})} == {d.SKIPPED}


def test_unmerged_pointer_moves_and_keys_without_a_physical_key():
    def with_pointer(moves, merged, raw):
        p = page(Visit(random.Random(13)))
        p["env"]["pointer"] = {"moves": moves, "merged": merged, "raw": raw}
        return d.Session({"pages": [p]})
    assert d.mouse_merged(with_pointer(86, 0, 86)).level == d.SKIPPED      # too few to say
    assert d.mouse_merged(with_pointer(300, 0, 300)).level == d.SUSPICIOUS  # Juggler: never merged
    assert d.mouse_merged(with_pointer(300, 5, 310)).level == d.CLEAN       # XTest through the X server
    v = Visit(random.Random(14))
    v.key("a", hold=90)
    v.ev("keydown", k="a", c="", sh=False, cl=False, rep=False, ch=ord("ą"))  # xdotool's keysym remap
    assert d.kbd_physical_key(d.Session({"pages": [page(v)]})).level == d.CAUGHT


# ---- the public gauntlet's verdicts ---------------------------------------------------------------

def test_gauntlet_judges_the_page_that_settled_not_the_first_response():
    from openajar.stealth.redteam import gauntlet as g
    site = "Python is an interpreted language. " * 60  # a real page's worth of text
    # Cloudflare's managed challenge answered 403, Camoufox solved it, Stack Overflow loaded
    assert g.judge(403, "Newest 'python' Questions", "<html>", site)[0] == g.CHALLENGED
    assert g.classify(403, "Newest 'python' Questions", "<html>")[0] == g.BLOCKED  # Paterson's rules
    assert g.judge(403, "Just a moment...", "<html>", "Verifying you are human")[0] in (g.BLOCKED, g.GATED)
    assert g.judge(200, "Indeed", "<html>", "Additional Verification Required. Your Ray ID is x")[0] == g.BLOCKED
    # a protected site carries its vendor's script; that alone is not a gate
    assert g.judge(200, "SEDAR+", "<script src='//perfdrive.com/x.js'>", site)[0] == g.OK
    assert g.judge(200, "", "<script src='//perfdrive.com/x.js'>", "short")[0] == g.GATED  # an untitled shim
    # Medium's home page is short and carries Cloudflare's script: still the site
    assert g.judge(200, "Medium", "<script src='/cdn-cgi/challenge-platform/x'>", "Human stories & ideas")[0] == g.OK
    assert g.judge(None, "", "", "")[0] == g.ERROR


def test_gauntlet_reads_the_panels():
    from openajar.stealth.redteam import gauntlet as g
    assert g.PARSERS["pixelscan-fp"]("Your Browser Fingerprint is inconsistent\nMasking detected\nNo proxy detected\n"
                                     "No automated behavior detected") == {
        "bot": "human", "fingerprint": "inconsistent", "masking": "detected", "proxy": "not detected"}
    assert g.PARSERS["sannysoft"]("WebDriver Advanced\tpassed\nChrome\n(New)\n\tmissing (failed)") == {
        "failed": 0, "chrome_only_failed": 1}
    inc = g.PARSERS["bot-incolumitas"]('"webDriverAdvanced": "FAIL",\n"userAgent": "OK"\nYour Behavioral Score: ...')
    assert inc["failed_tests"] == ["webDriverAdvanced"] and inc["behavioral_score"] == "not computed"
    assert g.tls_summary('{"tls": {"ja4": "t13d1517h2_8daaf6152771_3cbfd9057e0d"}}') == {
        "ja4": "t13d1517h2_8daaf6152771_3cbfd9057e0d"}
