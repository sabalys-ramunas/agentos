"""An agenda entry on a connector operation, end to end: drafted on a surface, proposed with the
connector's own approval card, approved in Needs you, fired through the surface's binding at its
time, and finished with the proof and typed output the run left (docs/AGENDA.md)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from connector_samples import LANE, SITES, Inbox, bind, commit_op, connector, connector_site, live
from test_policy_support import make_service

from agentos.agenda import tools as agenda_tools
from agentos.agenda.models import ActionKind, EntryState
from agentos.agenda.service import build
from agentos.core.errors import PolicyDenied
from agentos.core.models import Effect, RunStatus
from agentos.policy.meters import MeterPolicy
from agentos.policy.routing import PolicyConfig
from agentos.runner.connectors.compile import compile_connector
from agentos.runner.connectors.service import ConnectorService
from agentos.runner.fakes import OWNER, FakeClock, FakeLanes, Harness

OP = "connector.social.post_photo"
CAPTION = "Autumn at the lake"
PARAMS = {"caption": CAPTION, "image": "photo.jpg"}


@pytest.fixture
def env(tmp_path):
    clock = FakeClock()
    policy, _ = make_service(tmp_path / "policy", clock=lambda: clock.now().timestamp(),
                             config=PolicyConfig(meters=MeterPolicy(timezone="Europe/Vilnius")))
    inbox = Inbox(tmp_path / "inbox")
    inbox.put(LANE, "photo.jpg", b"\xff\xd8\xff\xe0 a small photo")  # files_put put it in the lane's inbox
    h = Harness(tmp_path / "runner", clock=clock, site=connector_site(),
                services={"lanes": FakeLanes([LANE]), "policy": policy, "approvals": policy.approvals,
                          "traces": policy.traces, "notifier": policy.notifier, "files": inbox})
    policy.runtime = h.runtime
    live(h.driver)
    ConnectorService(h.flows, sites=SITES).install(compile_connector(connector({"post_photo": commit_op()}),
                                                                     sites=SITES), by=OWNER)
    bind(h)
    policy.ask_first.set("flow", OP, True, by="owner", person=True)  # every post waits for a person
    agenda = build(h.runtime, home=tmp_path / "agenda", clock=clock)
    yield h, policy, agenda, clock
    agenda.close()
    policy.close()


def agent(policy, workspaces=("biz-a",)):
    _, tok = policy.issue(subject={"id": "claude-code"}, workspaces=list(workspaces), ceiling=Effect.IRREVERSIBLE)
    p, g = policy.resolve(tok)
    return policy.context(g, principal=p, tool="agenda_draft", token=tok)


async def test_a_connector_post_is_approved_fired_through_its_surface_and_proven(env):
    h, policy, agenda, clock = env
    at = clock.now() + timedelta(hours=1)
    out = await agenda_tools.agenda_draft(agent(policy), title="Autumn post",
                                          action={"connector": "social.post_photo", "params": PARAMS, "on": LANE},
                                          when={"at": at.isoformat()}, propose=True)
    assert out["kind"] == "connector" and out["action"]["surface"] == LANE and out["action"]["target"] == OP
    assert out["action"]["inputs"]["caption"] == CAPTION and "acts_as_id" not in out["action"]["inputs"]
    assert out["state"] == "proposed"
    ap = policy.approvals.get(out["approval"]["id"])
    assert ap.lane == LANE and ap.target.startswith("connector:social.post_photo@")  # the connector's own card
    assert CAPTION in (ap.preview or "") and ap.summary.startswith("Agenda: 'Autumn post'")
    policy.approvals.decide(ap.id, "approve", by="owner")
    await h.scheduler.tick()
    e = agenda.get(out["id"])
    assert e.state == EntryState.SCHEDULED and e.action.kind == ActionKind.CONNECTOR
    clock.set(at)
    await h.scheduler.tick()
    await h.runner.run_until_idle()
    e = agenda.get(out["id"])
    assert e.state == EntryState.DONE, e.reason
    run = h.runner.get(e.run_id)
    assert run.surface == LANE and run.lane == LANE and run.source == f"agenda:{e.id}"
    assert run.params == {**PARAMS, "acts_as_id": "biz-a", "account_language": "en"}  # filled by the binding
    assert run.status == RunStatus.SUCCEEDED_VERIFIED
    assert h.site.state["posts"] == [{"id": "1", "caption": CAPTION, "image": "photo.jpg"}]
    assert e.proof["status"] == "succeeded_verified" and e.proof["run_id"] == run.id
    assert e.proof["readbacks"] and all(rb["passed"] for rb in e.proof["readbacks"])
    assert "run_status" in e.proof["output"]  # a typed result exists; the proof points to it, never copies it
    await h.scheduler.tick()
    await h.runner.run_until_idle()
    assert len(h.site.state["posts"]) == 1 and len(h.runner.list()) == 1


async def test_a_connector_entry_needs_a_grant_that_sees_the_surface(env):
    _h, policy, agenda, clock = env
    with pytest.raises(PolicyDenied):
        await agenda_tools.agenda_draft(agent(policy, ("biz-b",)), title="Not mine",
                                        action={"connector": "social.post_photo", "params": PARAMS, "on": LANE},
                                        when={"at": (clock.now() + timedelta(hours=1)).isoformat()})
    with pytest.raises(PolicyDenied, match="binding"):
        agenda.draft(title="Who it acts as", action={"connector": "social.post_photo", "on": LANE,
                                                     "params": {**PARAMS, "acts_as_id": "biz-b"}})
    assert agenda.list() == []
