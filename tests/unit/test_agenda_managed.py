"""Other planned work on the agenda's timeline (agentos.agenda.service._managed): the next keepalive
visit per lane (agentos.keepalive.service.planned) and the engagement sessions nightly() saved
(agentos.engage.service.planned), projected read only, never stored, never planned again."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import count
from types import SimpleNamespace

import pytest
from test_engage import IG_YAML, OWNER
from test_keepalive import LANE, node  # noqa: F401 - the keepalive node fixture
from typer.testing import CliRunner

from agentos.agenda.service import build
from agentos.core.config import NodeConfig, save_config
from agentos.core.runtime import Runtime
from agentos.dev.lanes import DevLaneManager
from agentos.dev.web import new_tab
from agentos.engage.service import EngageService
from agentos.ops import control

IG = "biz-a/instagram"


class AtClock:
    """The agenda's clock on the same time as a service's float clock."""

    def __init__(self, f):
        self.f = f

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.f(), UTC)


def _agenda(runtime: Runtime, home, clock):
    return build(runtime, home=home / "agenda", clock=AtClock(clock))


# ---------------------------------------------------------------- keepalive


async def test_keepalive_projects_each_lanes_next_visit_and_nothing_after_it(node):  # noqa: F811
    now = node.service.now()
    out = node.service.planned(now - timedelta(hours=1), now + timedelta(days=12))
    assert out["enabled"] and "one visit ahead" in out["note"]
    by_lane = {i["lane"]: i for i in out["items"]}
    assert set(by_lane) == {LANE, "biz-a/other"}  # one each: the visit after depends on how this one goes
    for lane, item in by_lane.items():
        assert item["approx"] is True and item["effect"] == "read" and item["workspace"] == "biz-a"
        assert item["at"] == node.service.status(lane)["next_at"]  # the plan the rounds decide on
        assert item["manage"] == f"agentos lane keepalive {lane}"
    assert node.service.planned(now, now + timedelta(minutes=1))["items"] == []  # not in the range
    assert node.service.planned(now, now + timedelta(days=12), workspace="biz-b")["items"] == []


async def test_a_lane_with_keepalive_off_has_no_visit_and_says_why(node):  # noqa: F811
    now = node.service.now()
    node.service.set_opt_out(LANE, True)
    out = node.service.planned(now, now + timedelta(days=12))
    assert [i["lane"] for i in out["items"]] == ["biz-a/other"]
    assert out["skipped"] == [{"lane": LANE, "reason": f"{LANE} opted out (agentos lane keepalive {LANE} --on)"}]
    save_config(node.cfg.model_copy(update={"keepalive": False}))
    off = node.service.planned(now, now + timedelta(days=12))
    assert off["items"] == [] and off["enabled"] is False and "off on this node" in off["note"]


async def test_a_visit_a_gate_holds_for_now_says_it_waits(node):  # noqa: F811
    now = node.service.now()
    await node.lanes.take_control(LANE, by="owner")
    item = next(i for i in node.service.planned(now, now + timedelta(days=12))["items"] if i["lane"] == LANE)
    assert item["waits"] == f"a human has control of {LANE}"


async def test_the_timeline_shows_keepalive_visits_read_only_and_can_hide_them(node, tmp_path):  # noqa: F811
    runtime = node.service.runtime
    runtime.register("keepalive", node.service)
    agenda = _agenda(runtime, tmp_path, node.clock)
    try:
        now = node.service.now()
        span = {"start": now - timedelta(hours=1), "end": now + timedelta(days=12)}
        out = agenda.timeline(**span)
        managed = [i for i in out["items"] if i["type"] == "managed"]
        assert {i["lane"] for i in managed} == {LANE, "biz-a/other"}
        assert all(i["source"] == i["managed_by"] == i["kind"] == "keepalive" and i["state"] == "planned"
                   and i["approx"] and "approval" not in i for i in managed)
        assert out["managed_by"]["keepalive"]["available"] is True
        assert out["managed_by"]["engage"] == {"available": False,
                                               "note": "engagement is not running here, so its plans are not shown"}
        assert out["counts"] == {"planned": 2}
        assert agenda.timeline(**span, managed=False)["items"] == []
        assert agenda.timeline(**span, kind="flow")["items"] == []
        assert len(agenda.timeline(**span, kind="keepalive", states=["planned"])["items"]) == 2
        node.service.set_opt_out(LANE, True)
        seen = agenda.timeline(**span, visible=lambda target, ws: target == "biz-a/other")
        assert [i["lane"] for i in seen["items"]] == ["biz-a/other"]
        assert seen["managed_by"]["keepalive"]["skipped"] == []  # a lane the caller does not see stays hidden
        assert [x["lane"] for x in agenda.timeline(**span)["managed_by"]["keepalive"]["skipped"]] == [LANE]
    finally:
        agenda.close()


async def test_the_ops_socket_serves_what_a_service_plans(node):  # noqa: F811
    health = SimpleNamespace(runtime=node.service.runtime)
    node.service.runtime.register("keepalive", node.service)
    now = node.service.now()
    req = {"op": "planned", "source": "keepalive", "start": now.isoformat(),
           "end": (now + timedelta(days=12)).isoformat()}
    out = await control.dispatch(health, req)
    assert out["ok"] and len(out["planned"]["items"]) == 2
    assert (await control.dispatch(health, {**req, "source": "engage"}))["error"]["code"] == "unavailable"
    assert (await control.dispatch(health, {**req, "source": "runner"}))["ok"] is False
    assert (await control.dispatch(health, {**req, "start": "soon"}))["ok"] is False


# ---------------------------------------------------------------- engagement


@pytest.fixture
async def engaged(tmp_path, monkeypatch):
    """An approved program on biz-a/instagram, with the nightly plans for 5 and 6 November saved."""
    seeds = count()  # pinned like test_engage: the first day has sessions
    monkeypatch.setattr("agentos.engage.service.secrets.token_hex", lambda n: f"{next(seeds):032x}")
    cfg = NodeConfig(home=tmp_path)
    lanes = DevLaneManager(cfg, make_page=new_tab)
    from test_keepalive import spec

    await lanes.create(spec(IG))
    clock = {"t": datetime(2026, 11, 5, 1, 0, tzinfo=UTC).timestamp()}
    runtime = Runtime(config=cfg, lanes=lanes)
    svc = EngageService(runtime, home=tmp_path, clock=lambda: clock["t"])
    runtime.register("engage", svc)
    svc.draft(IG, IG_YAML, by=OWNER)
    svc.approve("biz-a-ig-coffee", by=OWNER)
    svc.nightly()
    yield SimpleNamespace(svc=svc, lanes=lanes, runtime=runtime, clock=clock, home=tmp_path)
    svc.store.close()


def _saved_starts(svc: EngageService, after: datetime) -> list[str]:
    plans = svc.store.plans(IG, date(2026, 11, 5), date(2026, 11, 12))
    return sorted(s.start.astimezone(UTC).isoformat() for p in plans for s in p.sessions if s.start >= after)


async def test_engagement_projects_only_the_sessions_it_saved(engaged):
    now = engaged.svc.now()
    out = engaged.svc.planned(now - timedelta(days=1), now + timedelta(days=7))
    starts = _saved_starts(engaged.svc, now)
    assert starts and [i["at"] for i in sorted(out["items"], key=lambda i: i["at"])] == starts
    assert {i["day"] for i in out["items"]} <= {"2026-11-05", "2026-11-06"}  # later days are not drawn yet
    assert out["drawn_until"] == {IG: "2026-11-06"} and "not drawn yet" in out["note"] and out["dry_run"]
    item = out["items"][0]
    assert item["lane"] == IG and item["program"] == "biz-a-ig-coffee" and item["approx"] is False
    assert datetime.fromisoformat(item["until"]) > datetime.fromisoformat(item["at"])
    assert item["timezone"] == "Europe/Vilnius" and item["manage"] == f"agentos engage status {IG}"
    assert engaged.svc.planned(now, now + timedelta(days=7), workspace="biz-b")["items"] == []


async def test_a_paused_program_has_no_sessions_and_says_why(engaged):
    now = engaged.svc.now()
    await engaged.lanes.hold(IG, reason="checkpoint on instagram")
    out = engaged.svc.planned(now, now + timedelta(days=7))
    assert out["items"] == []
    assert out["paused"] == [{"lane": IG, "program": "biz-a-ig-coffee",
                              "reason": f"{IG} is held (checkpoint on instagram), so its program is paused until "
                                        "the lane is handed back"}]
    await engaged.lanes.hand_back(IG, by="owner")
    assert engaged.svc.planned(now, now + timedelta(days=7))["items"]


async def test_a_draft_program_plans_nothing(tmp_path):
    svc = EngageService(Runtime(), home=tmp_path)
    try:
        svc.draft(IG, IG_YAML, by=OWNER)
        now = svc.now()
        assert svc.planned(now, now + timedelta(days=7))["items"] == []
    finally:
        svc.store.close()


async def test_the_timeline_and_the_api_show_engagement_sessions(engaged):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from agentos.agenda.api import routes

    agenda = _agenda(engaged.runtime, engaged.home, lambda: engaged.clock["t"])
    try:
        start, end = datetime(2026, 11, 5, tzinfo=UTC), datetime(2026, 11, 12, tzinfo=UTC)
        out = agenda.timeline(start=start, end=end, workspace="biz-a")
        sessions = [i for i in out["items"] if i["source"] == "engage"]
        assert [i["at"] for i in sessions] == _saved_starts(engaged.svc, engaged.svc.now())
        assert all(i["type"] == "managed" and i["kind"] == "engage" and i["state"] == "planned" for i in sessions)
        assert out["managed_by"]["engage"]["drawn_until"] == {IG: "2026-11-06"}
        client = TestClient(Starlette(routes=routes(engaged.runtime)))
        q = {"start": start.isoformat(), "end": end.isoformat()}
        assert len(client.get("/api/agenda", params=q).json()["items"]) == len(sessions)
        assert client.get("/api/agenda", params={**q, "managed": "0"}).json()["items"] == []
        assert len(client.get("/api/agenda", params={**q, "state": "planned"}).json()["items"]) == len(sessions)
    finally:
        agenda.close()


def test_the_cli_says_when_agentosd_cannot_be_asked(tmp_path, monkeypatch):
    from agentos.agenda.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    save_config(NodeConfig(home=tmp_path))
    result = CliRunner().invoke(app, ["list"])
    assert result.exit_code == 0, result.output
    assert "(keepalive: agentosd is not running, so what keepalive plans is not shown)" in result.output
    assert "(engage: agentosd is not running" in result.output
    quiet = CliRunner().invoke(app, ["list", "--no-managed"])
    assert "agentosd is not running" not in quiet.output
