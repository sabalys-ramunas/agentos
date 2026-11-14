"""Fixes from the full test of 2026-11-10: routines and schedules."""

from __future__ import annotations

from types import SimpleNamespace

from agentos.runner.cli import _schedule_line
from agentos.runner.models import Run
from agentos.runner.run_rules import bind_workflow, started_by_schedule


def test_schedule_line_of_a_workflow_schedule_does_not_crash():
    """Finding 32: a workflow's schedule has no flow and no lane."""
    line = _schedule_line({"id": "sch_1", "enabled": True, "flow_id": None, "route_id": "rt_f5", "lane": None,
                           "cron": {"cron": "34 8 * * *"}, "next_run": "2026-11-11T08:34:00+00:00"})
    assert "workflow rt_f5" in line and "34 8 * * *" in line and "2026-11-11 08:34" in line
    flow = _schedule_line({"id": "sch_2", "enabled": False, "flow_id": "a.b", "lane": "a/b", "cron": None})
    assert "a.b" in flow and "a/b" in flow and "off" in flow


def test_which_runs_a_schedule_started():
    """Finding 33: only a schedule's own fire is judged on its own; a retry or a manual run is not."""
    assert started_by_schedule(Run(id="r1", trigger={"kind": "cron", "slot": "x"}))
    assert started_by_schedule(Run(id="r2", trigger={"kind": "every", "slot": "x"}))
    assert not started_by_schedule(Run(id="r3", trigger={"kind": "webhook"}))
    assert not started_by_schedule(Run(id="r4"))
    wf = Run(id="r5", trigger={"kind": "event", "route": "rt", "scheduled": True}, params={"by": "trigger"})
    assert started_by_schedule(wf)
    retried = Run(id="r6", trigger={"kind": "event"}, params={"by": "retry", "scheduled": True})
    assert not started_by_schedule(retried)
    # what a caller puts in params is not the runner's word (review of the fix wave): an agent's flow run
    # with {"by": "trigger", "scheduled": true}, or an event run whose params say so, was not a schedule
    for trigger in ({"kind": "agent"}, {"kind": "event", "route": "rt"}, None):
        assert not started_by_schedule(Run(id="r9", trigger=trigger, params={"by": "trigger", "scheduled": True}))
    step = Run(id="r7", trigger={"kind": "workflow", "workflow": "w", "scheduled": True})
    ctx = SimpleNamespace(scheduled=False, routine=None, origins=[])
    bind_workflow(ctx, step)
    assert ctx.scheduled and ctx.routine == "w"
    ctx = SimpleNamespace(scheduled=False, routine=None, origins=[])
    bind_workflow(ctx, Run(id="r8", trigger={"kind": "workflow", "workflow": "w"}))
    assert not ctx.scheduled


def test_a_cron_event_marks_the_workflow_run_scheduled():
    from agentos.runner.workflows.service import WorkflowService

    ev = SimpleNamespace(facts={"slot": "2026-11-10T08:34:00+00:00"}, content=None, kind="cron.fired", source="cron")
    data = WorkflowService.trigger_data(ev)
    assert data["scheduled"] is True and data["by"] == "trigger"
    hook = SimpleNamespace(facts={}, content=SimpleNamespace(text="{}", fields=None), kind="webhook.received",
                           source="webhook")
    assert "scheduled" not in WorkflowService.trigger_data(hook)


def test_the_agenda_names_a_routine_by_its_name():
    """Tester A 8: a projected fire of a workflow reads "Tester A tick routine", not rt_..."""
    from agentos.agenda.service import AgendaService
    from agentos.core.errors import NotFound

    route = SimpleNamespace(name="Tester A tick routine", target=SimpleNamespace(workflow="scratch.routine.tick"))

    def get_route(rid):
        if rid != "rt_f599109119d5":
            raise NotFound("no route")
        return route

    me = SimpleNamespace(_events=lambda: SimpleNamespace(get_route=get_route))
    assert AgendaService._route_name(me, "rt_f599109119d5") == ("Tester A tick routine", "scratch.routine.tick")
    assert AgendaService._route_name(me, "rt_000000000000") == (None, None)


def test_a_trigger_card_names_its_lane_and_filters():
    """Tester A 9: the one line says every filter the trigger has."""
    from agentos.events.models import Match
    from agentos.events.service import match_words

    m = Match(source="run", kind="run.finished", workspace="scratch", lane="scratch/auto", where={"status": "failed"})
    words = match_words(m, "scratch")
    assert words == "run.finished events arrive in scratch on lane scratch/auto where status is failed"
    assert match_words(Match(source="cron", workspace="scratch"), "scratch") == "cron events arrive in scratch"


def test_an_unknown_template_names_the_ones_there_are(tmp_path):
    """Tester A 10: the error lists the templates, since no agent tool lists them."""
    import pytest

    from agentos.core.errors import NotFound
    from agentos.workspaces.service import WorkspaceService

    me = SimpleNamespace(templates_dir=tmp_path)
    with pytest.raises(NotFound) as e:
        WorkspaceService.get_template(me, "nope")
    assert "social-6" in e.value.message and "agentos workspace templates" in e.value.message


def test_an_approval_tells_the_agent_why_and_a_flow_run_need_not_be_called_again():
    """Tester A 4 and 6."""
    from agentos.policy.approval_models import Approval

    ap = Approval(id="ap_1", kind="action", state="pending", summary="Run flow x", created_at=0.0, expires_at=60.0,
                  reason="it follows untrusted text from a web page at example.com", tool="flow_run", requester={})
    assert ap.status()["why"].startswith("it follows untrusted text")
    done = ap.model_copy(update={"state": "approved"}).status()
    assert "starts on its own" in done["next"] and "run_status" in done["next"]
