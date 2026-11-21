"""Golden launches: what the engine hands the browser for three lanes, byte for byte, so splitting
node/engine_camoufox.py cannot change a launch unnoticed.

Each case plans one launch with the fake Camoufox and exit lookup of test_lanes_pin and compares
what the engine asks Camoufox for and the plan (the Playwright options, the config the browser gets, the pin it writes) with a JSON file
in tests/unit/golden/engine_launch. Values that differ per run (times, temporary paths, the
machine's environment, the installed Camoufox version) are replaced by a marker first.

Record them again (only after a deliberate change): OPENAJAR_RECORD_GOLDEN=1 pytest this file.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any

import pytest
from test_lanes_pin import FakeCamoufox, FakeGeo, v1_pin

from openajar.core.models import LaneSpec, ProxyConfig
from openajar.node.camoufox import engine as ec
from openajar.node.camoufox.engine import PIN_META, CamoufoxEngine, load_pin

GOLDEN = Path(__file__).parent / "golden" / "engine_launch"
TIME_KEYS = {"pinned_at", "completed_at", "at"}


def _normal(value: Any, tmp: str) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in TIME_KEYS and isinstance(v, str):
                out[k] = "<time>"
            elif k == "camoufox" and isinstance(v, str | None):
                out[k] = "<camoufox version>"
            elif k == "env" and isinstance(v, dict):
                # the node's own environment rides along; only what the engine sets is pinned
                out[k] = {ek: _normal(ev, tmp) for ek, ev in sorted(v.items())
                          if ek.startswith(("CAMOU_", "DISPLAY", "GDK_", "MOZ_", "XAUTHORITY"))}
            else:
                out[k] = _normal(v, tmp)
        return out
    if isinstance(value, list | tuple):
        return [_normal(v, tmp) for v in value]
    if isinstance(value, str):
        return value.replace(tmp, "<tmp>")
    if isinstance(value, int | float | bool) or value is None:
        return value
    return repr(value)  # Camoufox's Screen bound


def _plan(tmp_path: Path, monkeypatch, *, platform: str, identity_os: list[str] | None,
          stored: dict | None, proxy: ProxyConfig | None) -> dict[str, Any]:
    pytest.importorskip("camoufox.fingerprints")
    monkeypatch.setattr(ec.sys, "platform", platform)
    monkeypatch.setattr(ec, "_host_cores", lambda: 8)
    random.seed(7)  # the software-GPU pick of a new Linux lane
    fp = tmp_path / "lane" / "fingerprint.json"
    fp.parent.mkdir()
    if stored is not None:
        fp.write_text(json.dumps(stored))
    engine = CamoufoxEngine(resolve=FakeCamoufox(seed=3), geo=FakeGeo(), identity_os=identity_os)
    pin = load_pin(fp, "a/b") if fp.exists() else None
    opts = engine.options(LaneSpec(id="a/b"), profile_dir=tmp_path / "lane" / "profile", fingerprint_path=fp,
                          display=":150", proxy=proxy)
    plan = engine.plan_launch("a/b", opts, pin)
    raw = {"asked": opts, "options": plan.options, "config": plan.config, "first": plan.first, "pin": plan.pin,
           "old_version": plan.old_version, "notes": plan.notes, "backup_label": plan.backup_label}
    return _normal(json.loads(json.dumps(raw, default=repr)), str(tmp_path))


CASES = {
    # a new lane claimed on a Linux node: the Linux identity, a software GPU, the private-network PAC
    "linux_claim": {"platform": "linux", "identity_os": ["linux"], "stored": None, "proxy": None},
    # a new lane on a Mac host: no Linux-only prefs, a macOS identity
    "macos_host": {"platform": "darwin", "identity_os": ["macos"], "stored": None, "proxy": None},
    # a pinned (version 1) lane behind a proxy with credentials: completed once, WebRTC through the proxy
    "pin_with_proxy": {"platform": "linux", "identity_os": None, "stored": v1_pin(),
                       "proxy": ProxyConfig(label="exit", host="198.51.100.4", port=3128,
                                            username="u", password="p")},
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_launch_plan_matches_its_golden_file(case, tmp_path, monkeypatch):
    got = _plan(tmp_path, monkeypatch, **CASES[case])
    path = GOLDEN / f"{case}.json"
    if os.environ.get("OPENAJAR_RECORD_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    assert got == json.loads(path.read_text())
    assert got["first"] is (CASES[case]["stored"] is None) and got["pin"] is not None  # every case writes a pin
    assert PIN_META not in got["config"]
