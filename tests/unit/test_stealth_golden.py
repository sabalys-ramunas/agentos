"""Characterization of the stealth findings: the two real probe dumps and the test pin, each changed in
one way per branch of dump_findings and pin_findings, and every finding that comes out (check,
level, detail, data), pinned in tests/fixtures/stealth/findings_golden.json. Regenerate the file
only when a change to the findings is meant: AGENTOS_REGEN_GOLDEN=1.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest
from test_stealth import LINUX_UA, dump, pin

from agentos.stealth import stealth

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "stealth" / "findings_golden.json"
MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) Gecko/20100101 Firefox/158.0"


def _set(path: str, value: Any):
    def change(d: dict) -> None:
        *head, last = path.split(".")
        for key in head:
            d = d.setdefault(key, {})
        if value is _DROP:
            d.pop(last, None)
        else:
            d[last] = value
    return change


_DROP = object()

DUMP_CHANGES: dict[str, list] = {
    "as is": [],
    "webdriver": [_set("navigator.webdriver", True)],
    "auto webdriver": [_set("automation.webdriver", True)],
    "own property": [_set("automation.webdriverOwnProperty", True)],
    "no prototype getter": [_set("automation.webdriverOnPrototype", False)],
    "globals": [_set("automation.globals", ["__pw"]), _set("automation.attributes", ["webdriver"]),
                _set("automation.notNative", ["Permissions.query"])],
    "chrome": [_set("automation.chrome", "object")],
    "notification": [_set("automation.notification", "granted"), _set("permissionsNotifications", "prompt")],
    "notification same": [_set("automation.notification", "default"), _set("permissionsNotifications", "prompt")],
    "worker missing": [_set("worker", "error: timeout")],
    "worker gone": [_set("worker", _DROP), _set("frame", _DROP)],
    "worker differs": [_set("worker.platform", "Linux x86_64"), _set("worker.languages", ["de"])],
    "worker webgl": [_set("worker.webgl", ["Mesa", "llvmpipe"])],
    "frame differs": [_set("frame.screenWidth", 1920)],
    "css": [_set("css.deviceWidth", False), _set("css.deviceHeight", False), _set("css.resolution", False)],
    "window wide": [_set("window.outerWidth", 1700)],
    "avail wide": [_set("screen.availWidth", 4000)],
    "toolbars": [_set("window.innerHeight", 10)],
    "toolbars none": [_set("window.outerHeight", 0)],
    "inner screen": [_set("window.screenX", 206), _set("window.screenY", 2)],
    "no inner screen": [_set("window.innerScreenX", _DROP)],
    "touch": [_set("navigator.maxTouchPoints", 5)],
    "touch media": [_set("media.anyCoarse", True)],
    "no media": [_set("media", _DROP), _set("navigator.maxTouchPoints", 5)],
    "pdf": [_set("navigator.pdfViewerEnabled", True), _set("navigator.plugins", [])],
    "ua header": [_set("headers.user-agent", "Other")],
    "accept language": [_set("headers.accept-language", "de-DE,de;q=0.9")],
    "no languages": [_set("navigator.languages", "en"), _set("headers.accept-language", "de-DE")],
    "webgl software": [_set("webgl.renderer", "llvmpipe (LLVM 15)")],
    "webgl other os": [_set("webgl.renderer", "Apple M1, or similar")],
    "fonts linux": [_set("fonts.found", ["Segoe UI", "Ubuntu Mono"])],
    "fonts none core": [_set("fonts.found", ["Comic Neue"])],
    "fonts empty": [_set("fonts.found", [])],
    "codecs": [_set("codecs.h264", "")],
    "webrtc": [_set("webrtc.rawAddresses", ["10.0.0.5"])],
    "probe error": [_set("voices", "error: timeout"), _set("math", "error: x")],
    "no ua": [_set("navigator.userAgent", None)],
    "mac ua": [_set("navigator.userAgent", MAC_UA)],
}
DUMP_OPTIONS = {
    "plain": {},
    "proxied": {"proxied": True},
    "pinned tz": {"config": {"timezone": "Europe/Vilnius"}},
    "same tz": {"config": {"timezone": "UTC"}},
}

PIN_CHANGES: dict[str, dict[str, Any]] = {
    "as is": {},
    "platform": {"navigator.platform": "Linux x86_64"},
    "oscpu": {"navigator.oscpu": "Intel Mac OS X 10.15"},
    "header ua": {"headers.User-Agent": LINUX_UA},
    "software gl": {"webGl:renderer": "llvmpipe"},
    "other os gl": {"webGl:renderer": "Apple M1, or similar"},
    "fonts": {"fonts": ["Arial", "Ubuntu", "Helvetica Neue"]},
    "fonts not a list": {"fonts": "Arial"},
    "language": {"headers.Accept-Language": "de-DE,de"},
    "window": {"window.outerWidth": 4000, "window.outerHeight": 4000},
    "touch linux": {"navigator.userAgent": LINUX_UA, "headers.User-Agent": LINUX_UA,
                    "navigator.platform": "Linux x86_64", "navigator.oscpu": "Linux x86_64",
                    "navigator.maxTouchPoints": 5},
    "mac": {"navigator.userAgent": MAC_UA, "headers.User-Agent": MAC_UA, "navigator.platform": "MacIntel",
            "navigator.oscpu": "Intel Mac OS X 10.15"},
    "old pin": {"agentos:pin": {"version": 1}},
    "no meta": {"agentos:pin": None},
    "no ua": {"navigator.userAgent": None, "headers.User-Agent": None},
}
PIN_OPTIONS = {
    "no install": {},
    "older install": {"installed_major": 150},
    "same install": {"installed_major": 158},
    "newer install": {"installed_major": 170},
}


def _rows(findings) -> list[list]:
    return [[f.check, f.level, f.detail, f.lane, f.data] for f in findings]


def _actual() -> dict:
    out: dict[str, Any] = {}
    for name in ("windows", "linux"):
        for change, steps in DUMP_CHANGES.items():
            for option, kw in DUMP_OPTIONS.items():
                seen = copy.deepcopy(dump(name))
                for step in steps:
                    step(seen)
                out[f"dump {name} / {change} / {option}"] = _rows(stealth.dump_findings("x/y", seen, **kw))
    for change, over in PIN_CHANGES.items():
        for option, kw in PIN_OPTIONS.items():
            data = pin(**over)
            if change == "no meta":
                data.pop("agentos:pin", None)
            out[f"pin {change} / {option}"] = _rows(stealth.pin_findings("x/y", data, coherence=False, **kw))
    return json.loads(json.dumps(out))


def test_the_stealth_findings_are_what_they_were():
    actual = _actual()
    if os.environ.get("AGENTOS_REGEN_GOLDEN"):
        GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
    golden = json.loads(GOLDEN.read_text())
    assert actual.keys() == golden.keys()
    for key in golden:
        assert actual[key] == golden[key], key


@pytest.mark.parametrize("name", ["windows", "linux"])
def test_the_golden_cases_reach_most_branches(name):
    """A guard on the characterization itself: most changes give findings the plain dump does not."""
    golden = json.loads(GOLDEN.read_text())
    plain = golden[f"dump {name} / as is / plain"]
    changed = [k for k in golden if k.startswith(f"dump {name} /") and k.endswith("/ plain") and golden[k] != plain]
    assert len(changed) >= len(DUMP_CHANGES) - 10
