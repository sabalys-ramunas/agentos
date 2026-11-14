"""Pins one step of the offline replay page (agentos.audit.replay_page._step): its HTML for each kind
of step, with and without the arguments' values."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from pinned import check, outcome

from agentos.audit.replay_page import _step

TZ = ZoneInfo("Europe/Vilnius")
AT = "2026-11-14T10:00:00Z"
BASE = {"id": "tr_1", "seq": 7, "at": AT, "kind": "call", "tool": "browser_act", "status": "ok",
        "chain": {"status": "ok", "rows": []}}
STEPS = {
    "bare": {**BASE},
    "unknown status": {**BASE, "status": "weird", "effect": "mystery"},
    "no status": {**BASE, "status": None},
    "commit with everything": {
        **BASE, "effect": "commit", "summary": "posted https://x.example/p?token=abc", "error": "late?q=1",
        "principal": {"kind": "agent", "id": "claude-code"}, "grant": {"label": "the poster"},
        "after_untrusted": "an email", "run": {"id": "run_1", "flow": "biz.post", "status": "done",
                                               "proof": {"url": "https://x.example/p/1?s=2"}},
        "capture": {"url": "https://x.example/after?x=" + "y" * 400, "title": "T" * 300}, "duration_ms": 1500,
        "approvals": [], "events": [{"kind": "commit_gate", "summary": "asked https://x.example/?a=1", "at": AT},
                                    {"kind": "nothing", "summary": "hidden", "at": AT},
                                    {"kind": "link_click", "summary": "", "at": AT}],
        "args": {"text": "hello", "on": "biz-a/x"}},
    "grant with no label": {**BASE, "grant": {"id": None}},
    "tampered": {**BASE, "chain": {"status": "tampered", "rows": []}},
    "run without proof or flow": {**BASE, "run": {"id": "run_2", "status": "failed"}},
    "capture without url": {**BASE, "capture": {"title": "x"}},
    "capture without title": {**BASE, "capture": {"url": "https://x.example/a"}},
    "fast": {**BASE, "duration_ms": 999},
    "zero duration": {**BASE, "duration_ms": 0},
    "args empty dict": {**BASE, "args": {}},
    "args a list": {**BASE, "args": [1, 2]},
    "many args": {**BASE, "args": {f"k{i}": i for i in range(40)}},
    "long args": {**BASE, "args": {"text": "z" * 20000}},
    "many events": {**BASE, "events": [{"kind": "act_read_back", "summary": f"e{i}", "at": AT} for i in range(30)]},
    "html in text": {**BASE, "summary": "<script>alert(1)</script>", "principal": {"id": "<b>me</b>"}},
}


def test_replay_step_is_pinned():
    cases = {}
    for name, s in STEPS.items():
        for include in (False, True):
            cases[f"{name}, args {'shown' if include else 'hidden'}"] = outcome(
                lambda s=s, include=include: _step(3, dict(s), TZ, None, include_args=include))
    check("replay_step", cases)
