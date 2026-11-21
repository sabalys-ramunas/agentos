"""Lane health (openajar.stealth.health): one grade from the checks the node already runs.

The grading table, the event window on a fixed clock, pin findings and the session's end, the words
it uses (never "safe", "undetectable", a "human score" or "ban", and no em dash), `openajar lane health
--json`, the API route and its lane scope, and the grades of the dev node's seeded lanes."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_node_panel import TOKEN, bearer
from test_node_panel import Node as PanelNode
from test_ops_support import FakeHost, make_home
from typer.testing import CliRunner

from openajar.core.config import NodeConfig
from openajar.node.store import LaneEvent
from openajar.stealth import health
from openajar.stealth.health import CHECK, NORMAL, STOP, build_health, count_events
from openajar.stealth.lanecheck import CHECK_IDS, FAIL, OK, UNKNOWN, WARN, Check, LaneChecker, LaneReport
from openajar.stealth.stealth import Finding

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
LANE = "biz-a/instagram"


def report(levels: dict[str, str] | None = None, *, state: str = "cold", data: dict[str, dict] | None = None,
           at: datetime = NOW - timedelta(minutes=4)) -> LaneReport:
    """A nine-check report: every check ok unless `levels` says otherwise."""
    levels = levels or {}
    data = data or {}
    checks = [Check(cid, levels.get(cid, OK), f"The {cid} check says {levels.get(cid, OK)}.",
                    f"fix the {cid}" if levels.get(cid, OK) in (WARN, FAIL) else None, data.get(cid, {}))
              for cid in CHECK_IDS]
    return LaneReport(LANE, state, at, checks)


def held(days_ago: float, reason: str, **detail: Any) -> dict[str, Any]:
    return {"lane_id": LANE, "kind": "held", "at": NOW - timedelta(days=days_ago),
            "detail": {"reason": reason, "until": None, "by": "node", **detail}}


def grade(**kw: Any) -> health.LaneHealth:
    kw.setdefault("report", report())
    return build_health(LANE, now=NOW, **kw)


# ---- the grading table ---------------------------------------------------------------------------

@pytest.mark.parametrize(("levels", "want", "facts"), [
    ({}, NORMAL, ["9 of 9 checks pass"]),
    ({"location": UNKNOWN, "session": UNKNOWN}, NORMAL, ["7 of 9 checks pass", "2 could not run"]),
    ({"webrtc": WARN}, CHECK, ["8 of 9 checks pass"]),
    ({"webrtc": WARN, "account": UNKNOWN}, CHECK, ["7 of 9 checks pass", "1 could not run"]),
    ({"session": FAIL}, STOP, ["8 of 9 checks pass"]),
    ({"session": FAIL, "location": UNKNOWN, "dns": WARN}, STOP, ["6 of 9 checks pass", "1 could not run"]),
])
def test_the_grade_follows_the_worst_finding_and_unknown_moves_nothing(levels, want, facts):
    h = grade(report=report(levels))
    assert h.grade == want and h.facts[:len(facts)] == facts
    if want == NORMAL:
        assert h.headline == "No known problems" and h.label == "Looks normal"
    # every check that did not pass is listed, the unknown ones as unknown (never as a pass)
    listed = {f.id: f.level for f in h.findings if f.source == "check"}
    assert listed == {f"check.{cid}": level for cid, level in levels.items()}
    assert h.counts["checks_ok"] == 9 - len(levels)


def test_unknown_alone_neither_raises_nor_lowers_a_grade():
    for base, want in (({}, NORMAL), ({"webrtc": WARN}, CHECK), ({"profile": FAIL}, STOP)):
        with_unknown = {**base, "location": UNKNOWN, "account": UNKNOWN}
        assert grade(report=report(base)).grade == grade(report=report(with_unknown)).grade == want


def test_no_report_and_nothing_verified_are_not_a_good_grade():
    h = grade(report=None)
    assert h.grade == CHECK and [f.id for f in h.findings] == ["not_checked"] and h.facts[0] == "not checked yet"
    h = grade(report=report({cid: UNKNOWN for cid in CHECK_IDS}))
    assert h.grade == CHECK and "nothing_verified" in {f.id for f in h.findings}
    assert h.facts[:2] == ["0 of 9 checks pass", "9 could not run"]


def test_a_quarantine_now_is_a_stop():
    q = report({"hold": FAIL}, state="quarantined", data={"hold": {"kind": "quarantine"}})
    h = grade(report=q)
    assert h.grade == STOP and h.counts["quarantined_now"] and h.headline.startswith("1 problem to fix")
    assert [f.id for f in h.findings if f.level == FAIL] == ["check.hold"]  # said once, by the hold check
    h = grade(quarantined=True)  # what the caller knows wins over a report without it
    assert h.grade == STOP and [f.id for f in h.findings] == ["quarantine"]


def test_the_report_may_come_as_its_json():
    assert grade(report=report({"dns": WARN}).to_dict()).grade == CHECK


# ---- the event window, on a fixed clock ----------------------------------------------------------

@pytest.mark.parametrize(("events", "want", "n", "recent"), [
    ([], NORMAL, 0, 0),
    ([held(13.9, "needs_human:captcha on instagram.com/challenge")], CHECK, 1, 1),
    ([held(14.1, "needs_human:checkpoint")], NORMAL, 1, 0),           # one, older than 14 days: a note
    ([held(20, "backoff:rate_limited"), held(29.9, "needs_human:blocked")], CHECK, 2, 0),
    ([held(30.1, "needs_human:captcha"), held(45, "needs_human:captcha")], NORMAL, 0, 0),  # outside
    ([held(-1, "needs_human:captcha")], NORMAL, 0, 0),                # a time ahead of the clock
])
def test_challenges_in_the_window(events, want, n, recent):
    h = grade(events=events)
    assert (h.grade, h.counts["challenges"], h.counts["challenges_recent"]) == (want, n, recent)
    assert (f"{n} challenge{'s' if n != 1 else ''} in 30 days" if n else "no challenges in 30 days") in h.facts
    if n and want == NORMAL:
        assert [f.level for f in h.findings if f.id == "challenges"] == ["note"]


def test_what_counts_as_a_challenge():
    events = [
        held(1, "needs_human:captcha", after="hand_back"),            # the same captcha, still on screen
        held(2, "quarantined: exit_ip_changed"),                      # the quarantine's own hold
        held(3, "a person paused it"),                                # a hold set by hand
        held(4, "needs_human:logged_out"), held(5, "needs_human:wrong_account"),
        {"lane_id": LANE, "kind": "quarantined", "at": NOW - timedelta(days=6), "detail": {"reason": "exit moved"}},
        {"lane_id": LANE, "kind": "hold_cleared", "at": NOW - timedelta(days=1), "detail": {}},
    ]
    seen = count_events(events, now=NOW)
    assert seen.challenges == [] and [e.what for e in seen.holds] == ["logged_out", "wrong_account"]
    assert [e.what for e in seen.quarantines] == ["exit moved"]
    h = grade(events=events)
    assert h.grade == NORMAL and "1 quarantine in 30 days" in h.facts
    assert {f.id: f.level for f in h.findings} == {"quarantines": "note", "other_holds": "note"}


def test_events_as_the_store_keeps_them_and_a_shorter_window():
    events = [LaneEvent(lane_id=LANE, at=NOW - timedelta(days=d), kind="held",
                        detail={"reason": "needs_human:captcha"}) for d in (2, 8, 12)]
    events.append({"lane_id": LANE, "at": (NOW - timedelta(days=3)).isoformat(), "kind": "held",
                   "detail": {"reason": "backoff:rate_limited"}})
    assert grade(events=events).counts["challenges"] == 4
    h = grade(events=events, days=5)
    assert h.counts["challenges"] == 2 and "2 challenges in 5 days" in h.facts and h.days == 5
    sentence = next(f.sentence for f in h.findings if f.id == "challenges")
    assert sentence == "Sites showed a captcha on 2026-11-09, a rate limit on 2026-11-08 (2 challenges in 5 days)."


# ---- pins, the browser and the session -------------------------------------------------------------

def test_pin_findings_that_name_the_lane_or_its_device():
    found = [
        Finding("pin", FAIL, "the user agent says windows and navigator.platform says linux", LANE),
        Finding("pin", OK, "one consistent linux device", LANE),
        Finding("freshness", WARN, "the pin says Firefox 150 and the browser is 158", LANE),
        Finding("pin", WARN, "a version 1 pin: it is completed on the lane's next launch", LANE),
        Finding("device", WARN, "device office has no proxy: its lanes leave from the node's own IP", "device:office"),
        Finding("collision", WARN, f"{LANE}, biz-a/shop pin the same font list", data={"lanes": [LANE, "biz-a/shop"]}),
        Finding("pin", FAIL, "the pin holds no settings", "biz-b/other"),
    ]
    mine = health.pin_findings_for(found, LANE, device="office")
    assert len(mine) == 6
    h = grade(pin_findings=mine)
    assert h.grade == STOP
    assert [(f.id, f.level) for f in h.findings] == [("pin.pin", FAIL), ("pin.device", WARN), ("pin.collision", WARN)]
    assert h.findings[0].sentence == "The user agent says windows and navigator.platform says linux."
    assert all(f.fix for f in h.findings)


def test_pin_age_browser_lag_and_the_session_end():
    def with_session(days_left: float) -> LaneReport:
        ends = (NOW + timedelta(days=days_left)).isoformat()
        return report(data={"session": {"sites": {"instagram": {"signed_in": True, "expires_at": ends}}},
                            "profile": {"pinned_firefox": 156, "engine_firefox": 158}})

    h = grade(report=with_session(30.5), pinned_at=NOW - timedelta(days=41, hours=3))
    assert h.grade == NORMAL
    assert h.facts == ["9 of 9 checks pass", "no challenges in 30 days", "pin 41 days old",
                       "pinned browser 2 versions behind", "session ends in 30 days"]
    assert (h.counts["pin_age_days"], h.counts["browser_lag"], h.counts["session_days_left"]) == (41, 2, 30)
    assert [f.level for f in grade(report=with_session(10.2)).findings] == ["note"]
    soon = grade(report=with_session(5.5))
    assert soon.grade == CHECK and [(f.id, f.level) for f in soon.findings] == [("session_ends", WARN)]
    assert "openajar lane view biz-a/instagram --control" in soon.findings[0].fix


# ---- the words -------------------------------------------------------------------------------------

FORBIDDEN = re.compile(r"\bsafe(ly|ty)?\b|undetectable|human score|\bban(s|ned)?\b|\u2014", re.IGNORECASE)


def every_text(h: health.LaneHealth) -> list[str]:
    d = h.to_dict()
    out = [d["label"], d["headline"], d["facts_line"], d["note"], *d["checks"], *d["cannot_see"]]
    for f in d["findings"]:
        out += [f["sentence"], f["fix"] or ""]
    return out + [health.format_health(h)]


def test_no_finding_promises_what_it_cannot_know():
    cases = [
        grade(), grade(report=None), grade(report=report({cid: UNKNOWN for cid in CHECK_IDS})),
        grade(quarantined=True),
        grade(events=[held(d, f"needs_human:{s}") for d, s in ((1, "captcha"), (2, "checkpoint"), (3, "blocked"),
                                                                 (4, "logged_out"), (5, "wrong_account"))]
              + [held(6, "backoff:rate_limited"), held(20, "needs_human:captcha"),
                 {"lane_id": LANE, "kind": "quarantined", "at": NOW - timedelta(days=7), "detail": {}}]),
        grade(report=report({"webrtc": WARN, "session": FAIL}), pinned_at=NOW - timedelta(days=3),
              pin_findings=[Finding("collision", FAIL, "a/b, c/d pin one device", data={"lanes": [LANE]}),
                            Finding("location", WARN, "proxy lt1: the exit is in DE", LANE)]),
        # a passed-through sentence loses its em dash
        grade(report=LaneReport(LANE, "cold", NOW, [Check("dns", WARN, "names resolve here \u2014 on the node",
                                                          "set a proxy \u2014 soon")])),
    ]
    for h in cases:
        for text in every_text(h):
            assert not FORBIDDEN.search(text), text
    assert grade().to_dict()["docs"] == "docs/RESPONSIBLE-USE.md"
    assert "terms allow automation" in " ".join(health.CANNOT_SEE)


# ---- openajar lane health --json ------------------------------------------------------------------

def test_the_cli_json_shape_without_a_daemon(tmp_path, monkeypatch):
    from openajar.ops import cli
    from openajar.stealth import lanecheck

    cfg = make_home(tmp_path, lanes={"personal/reddit": 158, "work/site": None})
    monkeypatch.setenv("OPENAJAR_HOME", str(cfg.home))
    real = lanecheck.LaneChecker
    monkeypatch.setattr(lanecheck, "LaneChecker",
                        lambda config, **kw: real(config, **{**kw, "host": FakeHost(), "network": False}))
    r = CliRunner().invoke(cli.app, ["lane-health", "--all", "--days", "14", "--json"])
    data = json.loads(r.output)
    assert set(data) == {"lanes", "days", "cannot_see", "docs", "note"} and data["days"] == 14
    assert [h["lane"] for h in data["lanes"]] == ["personal/reddit", "work/site"]
    one = data["lanes"][0]
    assert {"lane", "grade", "label", "headline", "facts", "facts_line", "findings", "days", "at", "checked_at",
            "state", "counts", "checks", "cannot_see", "note", "docs"} <= set(one)
    assert one["grade"] in (NORMAL, CHECK, STOP) and one["days"] == 14
    assert all(set(f) == {"id", "level", "sentence", "fix", "source"} for f in one["findings"])
    assert r.exit_code == (1 if any(h["grade"] == STOP for h in data["lanes"]) else 0)
    r = CliRunner().invoke(cli.app, ["lane-health", "personal/reddit"])
    assert "personal/reddit:" in r.output and "What this cannot see" in r.output
    r = CliRunner().invoke(cli.app, ["lane-health"])
    assert r.exit_code == 1 and "or pass --all" in r.output


# ---- the API route --------------------------------------------------------------------------------

@pytest.fixture
def panel_node(tmp_path):
    n = PanelNode(tmp_path)
    n.runtime.register("lane_check", LaneChecker(n.config, lanes=n.lanes, host=FakeHost(), network=False,
                                                 write_cache=False))
    yield n
    n.close()


def test_the_health_route_is_scoped_like_the_check(panel_node):
    asyncio.run(panel_node.lanes.hold("biz-a/instagram", reason="needs_human:captcha"))
    grant = panel_node.grant()
    with panel_node.client() as client:
        h = bearer(grant)
        r = client.get("/api/lanes/biz-a/instagram/health?days=14", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["lane"] == "biz-a/instagram" and body["days"] == 14 and body["grade"] == STOP
        assert body["counts"]["challenges"] == 1 and body["checked_at"]
        r = client.get("/api/lanes/personal/reddit/health", headers=h)
        assert r.status_code == 403 and "outside your grant" in r.json()["error"]["message"]
        assert client.get("/api/lanes/biz-a/instagram/health?days=0", headers=h).status_code == 400
        assert client.get("/api/lanes/biz-a/instagram/health?days=x", headers=h).status_code == 400
        assert client.get("/api/lanes/biz-a/nonexistent/health", headers=h).status_code == 404
        listed = client.get("/api/lanes/checks?fresh=1", headers=h).json()["lanes"]
        assert [(x["lane"], x["health"]["grade"]) for x in listed] == [("biz-a/instagram", STOP)]
        owner = client.get("/api/lanes/checks", headers=bearer(TOKEN)).json()["lanes"]
        assert len(owner) == 3 and all("health" in x for x in owner)
    events, _ = panel_node.policy.traces.query(kind="panel", lane="biz-a/instagram")
    assert any(e.tool.endswith("lane.health") for e in events), [e.tool for e in events]


# ---- the dev node's seed ---------------------------------------------------------------------------

async def test_the_dev_seed_shows_each_grade(tmp_path, monkeypatch):
    from openajar.dev import health as dev_health
    from openajar.dev.node import build_runtime, prepare_home, seed

    monkeypatch.setattr(dev_health, "installed_firefox", lambda home: 158)
    home = tmp_path / "node"
    prepare_home(home)
    runtime, lanes = build_runtime(NodeConfig(name="dev", home=home, mcp_token="seed-test-token"))
    try:
        await seed(runtime, lanes)
        checker = runtime.get("lane_check")
        checker.host = FakeHost()  # this machine's disk and Camoufox stay out of it
        graded = {h.lane: h for h in await health.gather_all(checker)}
    finally:
        await lanes.shutdown()
    assert {lane: h.grade for lane, h in graded.items()} == {
        "biz-a/shop": NORMAL, "biz-a/social": CHECK, "biz-b/forum": CHECK, "biz-b/social": STOP}
    assert graded["biz-a/shop"].headline == "No known problems"
    assert graded["biz-a/social"].counts["challenges"] == 2 and "pin 64 days old" in graded["biz-a/social"].facts
    assert [f.id for f in graded["biz-a/social"].findings if f.level == WARN] == ["challenges"]


# ---- what the review found ----

def test_a_lane_where_most_checks_could_not_run_is_not_called_normal():
    mostly_unknown = {cid: UNKNOWN for cid in CHECK_IDS if cid != "profile"}
    h = grade(report=report(mostly_unknown))
    assert h.grade == CHECK and "little_verified" in {f.id for f in h.findings}
    assert h.facts[:2] == ["1 of 9 checks pass", "8 could not run"]
    a_few = {"location": UNKNOWN, "account": UNKNOWN, "dns": UNKNOWN, "webrtc": UNKNOWN}
    assert grade(report=report(a_few)).grade == NORMAL  # under half: listed, and the grade stands


def test_a_member_reads_that_the_audit_found_something_not_which_other_lanes_it_names():
    told = {"lane": "biz-a/x", "grade": CHECK, "findings": [
        {"id": "check.webrtc", "level": WARN, "sentence": "WebRTC can show this computer's address.", "fix": "Close it.",
         "source": "check"},
        {"id": "pin.collision", "level": WARN, "source": "pin", "fix": "Give each its own.",
         "sentence": "biz-a/x, biz-b/secret pin one device but leave through different proxies (pa, pb)."},
        {"id": "pin.exit", "level": FAIL, "source": "pin", "fix": "x",
         "sentence": "Proxies pa, pb exit from one IP 203.0.113.9."}]}
    out = health.for_member(told)
    text = json.dumps(out)
    assert "biz-b" not in text and "203.0.113.9" not in text and "pa, pb" not in text
    assert [f["id"] for f in out["findings"]] == ["check.webrtc", "pin"] and out["findings"][1]["level"] == FAIL
    assert told["findings"][1]["sentence"].startswith("biz-a/x, biz-b/secret")  # the owner's copy is untouched
    assert health.for_member({"findings": []})["findings"] == []
