"""The agenda (agentos.agenda): planned one-off actions of any kind, approved through the node's
own approvals, fired once through the runner's submit path, kept inside their window by the lane's
pace, and merged into one timeline with the projected fires of recurring schedules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_policy_support import make_service

from agentos.agenda import tools as agenda_tools
from agentos.agenda.models import EntryState
from agentos.agenda.rules import AgendaStateError
from agentos.agenda.service import build
from agentos.core.errors import InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect, RunStatus
from agentos.policy.meters import MeterPolicy
from agentos.policy.routing import PolicyConfig
from agentos.runner.fakes import FakeClock, Harness, post_flow

NY = ZoneInfo("America/New_York")
FLOW = "biz-a.social.post"
LANE = "biz-a/social"
PARAMS = {"caption": "launch day", "image": "x.png"}


def ny(d: int, h: int, mi: int = 0) -> datetime:
    return datetime(2026, 11, d, h, mi, tzinfo=NY).astimezone(UTC)


class Env:
    def __init__(self, tmp_path, clock: FakeClock):
        self.tmp, self.clock = tmp_path, clock
        cfg = PolicyConfig(meters=MeterPolicy(timezone="Europe/Vilnius"))  # the default routing table
        self.policy, _ = make_service(tmp_path / "policy", clock=lambda: clock.now().timestamp(), config=cfg)
        self.start()
        self.h.save(post_flow())
        self.policy.ask_first.set("flow", FLOW, True, by="owner", person=True)  # its commits wait for a person

    def start(self) -> None:
        """A fresh process on the same home: runner, scheduler and agenda built again."""
        services = {"policy": self.policy, "approvals": self.policy.approvals, "traces": self.policy.traces,
                    "notifier": self.policy.notifier}
        site = getattr(getattr(self, "h", None), "site", None)
        self.h = Harness(self.tmp / "runner", clock=self.clock, services=services, site=site)
        self.policy.runtime = self.h.runtime
        self.agenda = build(self.h.runtime, home=self.tmp / "agenda", clock=self.clock)

    def agent(self, workspaces: tuple[str, ...] = ("biz-a",), tool: str = "agenda_draft"):
        _, tok = self.policy.issue(subject={"id": "claude-code"}, workspaces=list(workspaces),
                                   ceiling=Effect.IRREVERSIBLE)
        p, g = self.policy.resolve(tok)
        return self.policy.context(g, principal=p, tool=tool, token=tok)

    async def tick(self) -> None:
        await self.h.scheduler.tick()
        await self.h.runner.run_until_idle()

    def posts(self) -> list:
        return self.h.site.state["posts"]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, FakeClock(ny(5, 9)))
    yield e
    e.agenda.close()
    e.policy.close()


async def _proposed(env: Env, *, when: dict | None = None, ctx=None) -> dict:
    ctx = ctx or env.agent()
    when = when if when is not None else {"at": ny(5, 11).isoformat()}
    out = await agenda_tools.agenda_draft(ctx, title="Launch post", action={"flow": FLOW, "params": PARAMS},
                                          when=when, notes="the photo from Tuesday", propose=True)
    assert out["state"] == "proposed" and out["approval"]["id"]
    return out


def _approve(env: Env, entry: dict) -> None:
    env.policy.approvals.decide(entry["approval"]["id"], "approve", by="owner")  # Needs you, as the owner


# ---------------------------------------------------------------- lifecycle


async def test_a_drafted_post_waits_for_a_person_then_runs_once_at_its_time_with_proof(env):
    out = await _proposed(env)
    ap = env.policy.approvals.get(out["approval"]["id"])
    assert ap.state == "pending" and ap.summary.startswith("Agenda: 'Launch post', at ")
    assert ap.card is not None  # the same flow card a run's approval shows: the real caption
    await env.tick()
    assert env.agenda.get(out["id"]).state == EntryState.PROPOSED and env.posts() == []
    _approve(env, out)
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.SCHEDULED and e.approval["via"] == "person" and e.approval["by"] == "owner"
    assert env.policy.approvals.get(out["approval"]["id"]).state == "consumed"  # used once, by this entry
    env.clock.set(ny(5, 10, 59))
    await env.tick()
    assert env.posts() == []
    env.clock.set(ny(5, 11))
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.DONE and len(env.posts()) == 1
    assert e.proof["status"] == "succeeded_verified" and e.proof["readbacks"][-1]["passed"] is True
    assert e.proof["run_id"] == e.run_id and e.finished_at is not None
    run = env.h.runner.get(e.run_id)
    assert run.idempotency_key == f"agenda:{e.id}:{e.approved_hash[7:23]}" and run.source == f"agenda:{e.id}"
    assert run.grant_id == e.grant_id != "owner"  # the drafter's grant, checked again before it ran
    await env.tick()
    assert len(env.posts()) == 1 and len(env.h.runner.list()) == 1


async def test_an_edit_after_approval_asks_again_and_the_old_approval_releases_nothing(env):
    out = await _proposed(env)
    _approve(env, out)
    await env.tick()
    assert env.agenda.get(out["id"]).state == EntryState.SCHEDULED
    edited = await agenda_tools.agenda_update(env.agent(tool="agenda_update"), id=out["id"],
                                              action={"params": {"caption": "launch day!!"}})
    assert edited["state"] == "proposed" and edited["approval"]["id"] != out["approval"]["id"]
    assert edited["action"]["inputs"]["caption"] == "launch day!!"
    env.clock.set(ny(5, 11))
    await env.tick()
    assert env.posts() == []  # nobody approved the new caption
    _approve(env, edited)
    await env.tick()
    assert [p["caption"] for p in env.posts()] == ["launch day!!"]


async def test_editing_only_the_title_or_notes_keeps_the_approval(env):
    out = await _proposed(env)
    _approve(env, out)
    await env.tick()
    e = await env.agenda.update(out["id"], title="Launch post (final)", notes="checked")
    assert e.state == EntryState.SCHEDULED and e.title == "Launch post (final)"


async def test_a_retimed_waiting_entry_withdraws_its_request(env):
    out = await _proposed(env)
    edited = await env.agenda.update(out["id"], when={"at": ny(5, 12).isoformat()})
    assert edited.state == EntryState.PROPOSED and edited.approval["id"] != out["approval"]["id"]
    assert env.policy.approvals.get(out["approval"]["id"]).state == "expired"


async def test_a_denied_entry_goes_back_to_draft_with_the_reason(env):
    out = await _proposed(env)
    env.policy.approvals.decide(out["approval"]["id"], "deny", by="owner", note="wrong photo")
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.DRAFT and "wrong photo" in e.reason


async def test_cancel_withdraws_the_request_and_a_cancelled_entry_cannot_be_approved(env):
    out = await _proposed(env)
    e = await agenda_tools.agenda_cancel(env.agent(tool="agenda_cancel"), id=out["id"], reason="plans changed")
    assert e["state"] == "cancelled" and e["reason"] == "plans changed"
    assert env.policy.approvals.get(out["approval"]["id"]).state == "expired"
    assert e["approval"]["state"] == "withdrawn"  # QA 2026-11-10 A-12: never "pending" on a cancelled entry
    with pytest.raises(AgendaStateError):
        await env.agenda.approve(out["id"])


async def test_a_routine_that_runs_on_its_own_is_scheduled_without_asking(env):
    env.policy.ask_first.set("flow", FLOW, False, by="owner", person=True)
    out = await agenda_tools.agenda_draft(env.agent(), title="Post", action={"flow": FLOW, "params": PARAMS},
                                          when={"at": ny(5, 11).isoformat()}, propose=True)
    assert out["state"] == "scheduled" and out["approval"]["via"] == "auto"
    env.clock.set(ny(5, 11))
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.DONE and env.h.runner.get(e.run_id).authorized  # authorized again when it fired


async def test_as_soon_as_approved_runs_on_the_next_tick_after_the_decision(env):
    out = await _proposed(env, when={})
    assert out["when"]["kind"] == "asap"
    _approve(env, out)
    await env.tick()
    assert env.agenda.get(out["id"]).state == EntryState.DONE and len(env.posts()) == 1


# ---------------------------------------------------------------- firing once


async def test_a_restart_between_submit_and_record_finds_the_same_run(env):
    out = await _proposed(env)
    _approve(env, out)
    await env.tick()
    env.clock.set(ny(5, 11))
    await env.h.scheduler.tick()  # submitted, not yet run
    e = env.agenda.get(out["id"])
    first = e.run_id
    assert e.state == EntryState.RUNNING and first
    # the crash: the run was queued but the entry's write never happened
    e.state, e.run_id = EntryState.SCHEDULED, None
    env.agenda.store.put(e)
    env.agenda.close()
    env.start()
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.run_id == first and e.state == EntryState.DONE
    assert len(env.h.runner.list()) == 1 and len(env.posts()) == 1


# ---------------------------------------------------------------- windows


async def test_a_scheduled_entry_whose_window_passed_while_the_node_was_down_expires(env):
    out = await _proposed(env)
    _approve(env, out)
    await env.tick()
    env.clock.set(ny(5, 11, 31))  # at 11:00 with 30 minutes of grace; the node was down until now
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.EXPIRED and "window closed before it ran" in e.reason and env.posts() == []


async def test_a_proposal_nobody_decided_expires_with_its_window(env):
    out = await _proposed(env, when={"not_before": ny(5, 10).isoformat(), "not_after": ny(5, 12).isoformat()})
    env.clock.set(ny(5, 12, 1))
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.EXPIRED and e.reason == "nobody approved it before its window closed"


async def test_the_lanes_pace_moves_the_fire_later_inside_the_window(env):
    env.clock.set(ny(5, 5))
    out = await _proposed(env, when={"not_before": ny(5, 6).isoformat(), "not_after": ny(5, 10).isoformat()})
    _approve(env, out)
    await env.tick()
    env.policy.set_lane_pace(LANE, {"active_hours": ["08:00-22:00"], "timezone": "America/New_York"})
    env.clock.set(ny(5, 6))
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.SCHEDULED and e.retry_at == ny(5, 8) and env.posts() == []
    assert e.reason.startswith("waiting for the lane's pace")
    env.clock.set(ny(5, 7, 30))
    await env.tick()
    assert env.agenda.get(out["id"]).state == EntryState.SCHEDULED
    env.clock.set(ny(5, 8))
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.DONE and len(env.posts()) == 1


async def test_a_pace_that_leaves_no_room_in_the_window_skips_with_its_reason(env):
    env.clock.set(ny(5, 4))
    out = await _proposed(env, when={"not_before": ny(5, 5).isoformat(), "not_after": ny(5, 7).isoformat()})
    _approve(env, out)
    await env.tick()
    env.policy.set_lane_pace(LANE, {"active_hours": ["08:00-22:00"], "timezone": "America/New_York"})
    env.clock.set(ny(5, 5))
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.SKIPPED and "pace kept it out of its window" in e.reason
    assert "08:00" in e.reason and env.posts() == []


async def test_proposing_into_a_window_the_pace_leaves_no_room_in_is_refused(env):
    env.policy.set_lane_pace(LANE, {"active_hours": ["08:00-22:00"], "timezone": "America/New_York"})
    env.clock.set(ny(5, 4))
    with pytest.raises(InvalidRequest, match="posting pace leaves no time"):
        await _proposed(env, when={"not_before": ny(5, 5).isoformat(), "not_after": ny(5, 7).isoformat()})


async def test_a_run_held_past_the_window_is_cancelled_and_the_entry_skipped(env):
    out = await _proposed(env)
    _approve(env, out)
    await env.tick()
    env.clock.set(ny(5, 11))
    await env.h.scheduler.tick()
    e = env.agenda.get(out["id"])
    run = env.h.runner.get(e.run_id)
    run.error = {"code": "deferred", "message": "a human has control of biz-a/social"}
    run.not_before = ny(5, 13)
    env.h.runner.store.save_run(run)
    env.clock.set(ny(5, 11, 31))
    await env.h.scheduler.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.SKIPPED and "a human has control" in e.reason
    assert env.h.runner.get(run.id).status == RunStatus.CANCELLED


# ---------------------------------------------------------------- authorization


async def test_a_grant_without_the_lane_cannot_draft_for_it(env):
    other = env.agent(workspaces=("biz-b",))
    with pytest.raises(PolicyDenied):
        await agenda_tools.agenda_draft(other, title="Post", action={"flow": FLOW, "params": PARAMS},
                                        when={"at": ny(5, 11).isoformat()})
    assert env.agenda.list() == []


async def test_a_grant_without_the_lane_does_not_see_or_cancel_the_entry(env):
    out = await _proposed(env)
    other = env.agent(workspaces=("biz-b",), tool="agenda_cancel")
    with pytest.raises(NotFound):
        await agenda_tools.agenda_cancel(other, id=out["id"])
    seen = await agenda_tools.agenda_list(other, start=ny(5, 0).isoformat(), end=ny(6, 0).isoformat())
    assert seen["items"] == []


async def test_an_agent_cannot_approve_its_own_entry(env):
    agent = env.agent()
    out = await _proposed(env, ctx=agent)
    names = {s.name for s in agenda_tools.tool_specs(env.h.runtime)}
    assert "agenda_approve" not in names and {"agenda_draft", "agenda_propose", "agenda_cancel"} <= names
    with pytest.raises(PolicyDenied):  # its token never decides what its own grant family asked for
        await env.agenda.approve(out["id"], by="claude-code", refuse_family=agent.grant.root_id)
    assert env.agenda.get(out["id"]).state == EntryState.PROPOSED


async def test_a_revoked_grant_never_fires(env):
    ctx = env.agent()
    out = await _proposed(env, ctx=ctx)
    _approve(env, out)
    await env.tick()
    env.policy.revoke(ctx.grant.id)
    env.clock.set(ny(5, 11))
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.CANCELLED and env.posts() == [] and "was revoked" in e.reason
    assert env.h.runner.list() == []  # nothing was ever queued


async def test_the_owner_approves_a_draft_in_one_step(env):
    out = await agenda_tools.agenda_draft(env.agent(), title="Post", action={"flow": FLOW, "params": PARAMS},
                                          when={"at": ny(5, 11).isoformat()})
    assert out["state"] == "draft"
    e = await env.agenda.approve(out["id"], by="owner")
    assert e.state == EntryState.SCHEDULED and e.approval["via"] == "person"


# ---------------------------------------------------------------- the timeline


@pytest.fixture
def spring(tmp_path):
    e = Env(tmp_path, FakeClock(datetime(2027, 3, 20, 12, 0, tzinfo=UTC)))
    yield e
    e.agenda.close()
    e.policy.close()


async def test_the_timeline_merges_entries_projected_cron_fires_and_past_runs_across_dst(spring):
    env = spring
    env.h.scheduler.add(flow=FLOW, params=PARAMS, cron="30 3 * * *", timezone="Europe/Vilnius")
    await env.h.run(FLOW, {"caption": "earlier", "image": "x.png"})  # a past run, with proof
    entry = env.agenda.draft(title="Spring post", action={"flow": FLOW, "params": PARAMS},
                             when={"at": "2027-03-28T10:00:00+03:00"})
    out = env.agenda.timeline(start=datetime(2027, 3, 20, 0, 0, tzinfo=UTC),
                              end=datetime(2027, 3, 30, 0, 0, tzinfo=UTC), workspace="biz-a")
    projected = [i["at"] for i in out["items"] if i["type"] == "projected"]
    # 03:30 Vilnius every day; on 28 March 03:30 does not exist (03:00 -> 04:00) and fires at 04:30 once
    assert projected == ["2027-03-21T01:30:00+00:00", "2027-03-22T01:30:00+00:00", "2027-03-23T01:30:00+00:00",
                         "2027-03-24T01:30:00+00:00", "2027-03-25T01:30:00+00:00", "2027-03-26T01:30:00+00:00",
                         "2027-03-27T01:30:00+00:00", "2027-03-28T01:30:00+00:00", "2027-03-29T00:30:00+00:00"]
    entries = [i for i in out["items"] if i["type"] == "entry"]
    assert [i["id"] for i in entries] == [entry.id] and entries[0]["state"] == "draft"
    runs = [i for i in out["items"] if i["type"] == "run"]
    assert len(runs) == 1 and runs[0]["state"] == "done" and runs[0]["proof"]["readbacks"]
    assert out["items"] == sorted(out["items"], key=lambda i: (i["at"], i["id"]))
    only = env.agenda.timeline(start=datetime(2027, 3, 20, tzinfo=UTC), end=datetime(2027, 3, 30, tzinfo=UTC),
                               states=["draft"])
    assert [i["id"] for i in only["items"]] == [entry.id]
    assert env.agenda.timeline(start=datetime(2027, 3, 20, tzinfo=UTC), end=datetime(2027, 3, 30, tzinfo=UTC),
                               workspace="biz-b")["items"] == []


async def test_the_autumn_repeated_hour_projects_one_fire(tmp_path):
    env = Env(tmp_path, FakeClock(datetime(2027, 10, 20, tzinfo=UTC)))
    try:
        env.h.scheduler.add(flow=FLOW, params=PARAMS, cron="30 3 * * *", timezone="Europe/Vilnius")
        out = env.agenda.timeline(start=datetime(2027, 10, 30, tzinfo=UTC), end=datetime(2027, 11, 2, tzinfo=UTC))
        assert [i["at"] for i in out["items"] if i["type"] == "projected"] == [
            "2027-10-30T00:30:00+00:00", "2027-10-31T00:30:00+00:00", "2027-11-01T01:30:00+00:00"]
    finally:
        env.agenda.close()
        env.policy.close()


async def test_an_agents_timeline_shows_its_lanes_only_and_hides_history(env):
    out = await _proposed(env)
    seen = await agenda_tools.agenda_list(env.agent(tool="agenda_list"), start=ny(5, 0).isoformat(),
                                          end=ny(6, 0).isoformat())
    assert [i["id"] for i in seen["items"]] == [out["id"]] and "history" not in seen["items"][0]


async def test_timing_is_checked(env):
    with pytest.raises(InvalidRequest, match="timezone"):
        env.agenda.draft(title="x", action={"flow": FLOW, "params": PARAMS}, when={"at": "2026-11-05T11:00:00"})
    with pytest.raises(InvalidRequest, match="already passed"):
        env.agenda.draft(title="x", action={"flow": FLOW, "params": PARAMS},
                         when={"not_before": ny(4, 1).isoformat(), "not_after": ny(4, 2).isoformat()})
    with pytest.raises(InvalidRequest, match="exactly one of"):
        env.agenda.draft(title="x", action={"params": PARAMS})
    with pytest.raises(InvalidRequest):
        env.agenda.draft(title="x", action={"flow": FLOW, "params": {"nope": 1}})
    assert env.agenda.store.list() == []
    e = env.agenda.draft(title="x", action={"flow": FLOW, "params": PARAMS}, when={"at": ny(5, 11).isoformat()})
    assert e.when.not_after - e.when.not_before == timedelta(minutes=30)


# ---------------------------------------------------------------- the panel API


def test_the_api_drafts_approves_lists_and_cancels(env):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from agentos.agenda.api import routes

    client = TestClient(Starlette(routes=routes(env.h.runtime)))
    r = client.post("/api/agenda", json={"title": "Renew the domain", "action": {"flow": FLOW, "params": PARAMS},
                                         "when": {"at": ny(5, 11).isoformat()}, "propose": True})
    assert r.status_code == 201 and r.json()["state"] == "proposed"
    entry = r.json()["id"]
    assert client.post("/api/agenda", json={"title": "x", "nope": 1}).status_code == 400
    r = client.post(f"/api/agenda/{entry}/approve", json={"note": "fine"})
    assert r.status_code == 200 and r.json()["state"] == "scheduled" and r.json()["approval"]["by"] == "owner"
    r = client.get("/api/agenda", params={"workspace": "biz-a", "start": ny(5, 0).isoformat(),
                                          "end": ny(6, 0).isoformat(), "state": "scheduled"})
    assert [i["id"] for i in r.json()["items"]] == [entry]
    assert client.get("/api/agenda", params={"state": "bogus"}).status_code == 400
    r = client.patch(f"/api/agenda/{entry}", json={"notes": "renew for two years"})
    assert r.json()["notes"] == "renew for two years" and r.json()["state"] == "scheduled"
    r = client.post(f"/api/agenda/{entry}/cancel", json={"reason": "renewed by hand"})
    assert r.json()["state"] == "cancelled"
    r = client.post(f"/api/agenda/{entry}/approve")
    assert r.status_code == 409 and r.json()["error"]["code"] == "agenda_state"
    assert client.get("/api/agenda/ag_000000000000").status_code == 404


# ---------------------------------------------------------------- a trigger (an agent or brain task, a flow)


async def test_a_trigger_entry_emits_one_keyed_event_and_finishes_with_its_delivery(tmp_path):
    from events_support import OWNER, World, check_flow, no_quiet

    w = World(tmp_path, policy=False, settings=no_quiet())
    agenda = build(w.runtime, home=tmp_path / "agenda", clock=w.clock)
    try:
        fv = w.flow(check_flow())
        r = w.events.add_route({"name": "Export the monthly report", "match": {"source": "cron", "workspace": "biz-a"},
                                "target": {"kind": "flow", "flow": fv.id}}, principal=OWNER)
        watch = w.events.add_route({"name": "w", "match": {"source": "watch", "workspace": "biz-a"},
                                    "target": {"kind": "flow", "flow": fv.id}}, principal=OWNER)
        with pytest.raises(InvalidRequest, match="timed"):
            agenda.draft(title="x", action={"trigger": watch.id})
        at = w.clock.now() + timedelta(hours=1)
        e = agenda.draft(title="Export the report", notes="the November numbers", action={"trigger": r.id},
                         when={"at": at.isoformat()})
        assert e.workspace == "biz-a" and e.action.kind.value == "route" and e.action.effect == "read"
        e = await agenda.propose(e.id)
        assert e.state == EntryState.SCHEDULED  # this node has no policy service: everything is the owner's
        w.clock.set(at)
        await w.h.scheduler.tick()
        await w.h.scheduler.tick()
        e = agenda.get(e.id)
        assert e.state == EntryState.RUNNING and e.event_id
        events = w.store.list_events(source="cron")
        assert len(events) == 1 and events[0].dedupe_key == agenda.key(e)
        assert events[0].facts["agenda_entry"] == e.id and "untrusted" in events[0].taint  # its words are content
        await w.tick()
        await w.h.runner.run_until_idle()
        await w.tick()
        await w.h.scheduler.tick()
        e = agenda.get(e.id)
        assert e.state == EntryState.DONE and e.proof["event_id"] == e.event_id
        assert e.proof["deliveries"][0]["state"] == "done" and e.proof["deliveries"][0]["ref"]
        timeline = agenda.timeline(start=at - timedelta(days=1), end=at + timedelta(days=1), workspace="biz-a")
        assert [i["id"] for i in timeline["items"] if i["type"] == "entry"] == [e.id]
    finally:
        agenda.close()


async def test_another_grant_proposing_asks_under_the_entrys_own_grant(env):
    drafter = env.agent()
    out = await agenda_tools.agenda_draft(drafter, title="Post", action={"flow": FLOW, "params": PARAMS},
                                          when={"at": ny(5, 11).isoformat()})
    other = env.agent(tool="agenda_propose")
    proposed = await agenda_tools.agenda_propose(other, id=out["id"])
    ap = env.policy.approvals.get(proposed["approval"]["id"])
    assert ap.grant_id == drafter.grant.id != other.grant.id
