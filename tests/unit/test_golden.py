"""Golden traces (openajar.dev.golden): each recorded lane session under tests/golden/ replays
against the engine abstraction and must give the agent exactly what it got when it was recorded."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from openajar.dev import golden

GOLDEN = Path(__file__).resolve().parents[1] / "golden"
TRACES = sorted(p for p in GOLDEN.iterdir() if (p / "session.json").is_file())


def test_there_is_a_trace():
    assert [p.name for p in TRACES] == ["dev-social"]


@pytest.mark.parametrize("trace_dir", TRACES, ids=lambda p: p.name)
async def test_replay_gives_the_recorded_results(trace_dir, tmp_path):
    """Fails when the tool layer turns the same engine results into something else (an outline, a
    ref, a signal, a page result or an error). Meant? Record again: scripts/record_golden.py."""
    trace = golden.load(trace_dir)
    replayed, engine = await golden.replay(tmp_path, trace)
    for want, got in zip(trace["session"]["calls"], replayed, strict=True):
        assert got["result"] == want["result"], f"{want['tool']} {json.dumps(want['args'])}"
    assert engine.done, f"the replay used {engine.index} of {len(engine.calls)} recorded engine calls"


@pytest.mark.parametrize("trace_dir", TRACES, ids=lambda p: p.name)
async def test_the_dev_web_still_records_the_same_trace(trace_dir, tmp_path):
    """Fails when the dev web (openajar.dev.web) or the tool layer changed what a session sees."""
    trace = golden.comparable(await golden.record(tmp_path))
    stored = golden.comparable(golden.load(trace_dir))
    assert trace["engine"] == stored["engine"]
    assert trace["session"] == stored["session"]
    assert trace["har"] == stored["har"]


@pytest.mark.parametrize("trace_dir", TRACES, ids=lambda p: p.name)
def test_traces_hold_only_the_fake_site(trace_dir):
    """Never a real site: every page is on a reserved .example host (or about:blank)."""
    trace = golden.load(trace_dir)
    urls = [e["request"]["url"] for e in trace["har"]["log"]["entries"]]
    urls += [c["args"][0] for c in trace["engine"]["calls"] if c["call"] == "navigate"]
    assert urls
    for url in urls:
        host = urlsplit(url).hostname
        assert url == "about:blank" or (host and host.endswith(".example")), url
    assert trace["har"]["log"]["version"] == "1.2"


async def test_a_changed_engine_result_shows_in_the_replay(tmp_path):
    trace = copy.deepcopy(golden.load(TRACES[0]))
    snap = next(c for c in trace["engine"]["calls"] if c["call"] == "snapshot")
    snap["result"]["text"] = snap["result"]["text"].replace('heading "Feed"', 'heading "Fed"')
    replayed, _ = await golden.replay(tmp_path, trace)
    recorded = trace["session"]["calls"]
    differ = [w["tool"] for w, g in zip(recorded, replayed, strict=True) if w["result"] != g["result"]]
    assert differ == ["browser_snapshot"]


async def test_a_replay_that_asks_for_something_else_is_a_mismatch(tmp_path):
    trace = copy.deepcopy(golden.load(TRACES[0]))
    nav = next(c for c in trace["engine"]["calls"] if c["call"] == "navigate")
    nav["args"] = ["https://social.example/elsewhere"]
    replayed, _ = await golden.replay(tmp_path, trace)
    first = next(c for c in replayed if c["tool"] == "browser_navigate")
    assert first["result"]["is_error"]  # the tool layer reports the engine's GoldenMismatch as an error
    assert "GoldenMismatch: engine call 0" in first["result"]["content"][0]["text"]
