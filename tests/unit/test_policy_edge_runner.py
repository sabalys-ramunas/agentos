"""Edge cases in the runner: flows that run with no call-time authorization, and approvals that release runs.

A saved flow is the path a schedule, the panel API or an agent's flow_run takes to act on a lane. These tests use
the real PolicyService (not the runner's FakePolicy, which has an authorize() method the real service lacks), so
they see what production sees.

Bugs are marked xfail(strict=True) with a BUG number, as in test_policy_edge_authorize.py.
"""

from __future__ import annotations

import pytest
from test_policy_support import Clock, make_service

from openajar.core.models import Effect
from openajar.policy.errors import Denied
from openajar.runner.fakes import Harness, post_flow
from openajar.runner.tools import flow_run

PARAMS = {"caption": "hi", "image": "x.png"}
FLOW = "biz-a.social.post"


@pytest.fixture
def build(tmp_path):
    """A policy service and a runner harness that share one runtime, with the post flow saved at `effect`."""
    made = []

    def factory(effect: str = "commit"):
        svc, _ = make_service(tmp_path / "policy", clock=Clock())
        h = Harness(tmp_path / "runner", services={"policy": svc, "approvals": svc.approvals,
                                                   "traces": svc.traces, "notifier": svc.notifier})
        svc.runtime = h.runtime
        h.save(post_flow(effect=effect))
        made.append(svc)
        return svc, h

    yield factory
    for svc in made:
        svc.close()


def agent_context(svc, ceiling: Effect = Effect.IRREVERSIBLE):
    _, tok = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling=ceiling)
    p, g = svc.resolve(tok)
    return svc.context(g, principal=p, tool="flow_run", token=tok)


def pending_ids(svc) -> list[str]:
    return [a.id for a in svc.approvals.list(state="pending")]


async def test_scheduled_commit_flow_waits_for_approval(build):
    _svc, h = build("commit")
    h.submit(FLOW, PARAMS, authorized=False)  # what a schedule or POST /api/flows/{id}/run submits
    await h.runner.run_until_idle()
    assert h.site.state["posts"] == []


async def test_read_effect_flow_cannot_write_without_approval(build):
    svc, h = build("read")  # the steps click Share with writes: true
    await flow_run(agent_context(svc), name=FLOW, params=PARAMS, wait_seconds=0)
    await h.runner.run_until_idle()
    assert h.site.state["posts"] == []


async def test_one_approval_releases_one_run(build):
    svc, h = build("commit")
    ctx = agent_context(svc)
    await flow_run(ctx, name=FLOW, params=PARAMS, wait_seconds=0)
    await flow_run(ctx, name=FLOW, params=PARAMS, wait_seconds=0)  # an agent retry without an idempotency key
    first = pending_ids(svc)[0]
    svc.approvals.decide(first, "approve", by="owner")
    await h.runner.on_approval(first, "approved")
    await h.runner.run_until_idle()
    assert len(h.site.state["posts"]) == 1


async def test_commit_flow_posts_once_after_one_approval(build):
    svc, h = build("commit")
    await flow_run(agent_context(svc), name=FLOW, params=PARAMS, wait_seconds=0)
    await h.runner.run_until_idle()
    assert h.site.state["posts"] == []  # parked until the owner decides
    approval = pending_ids(svc)
    assert len(approval) == 1
    svc.approvals.decide(approval[0], "approve", by="owner")
    await h.runner.on_approval(approval[0], "approved")
    await h.runner.run_until_idle()
    assert len(h.site.state["posts"]) == 1


async def test_idempotency_key_retry_never_posts_twice(build):
    svc, h = build("commit")
    ctx = agent_context(svc)
    await flow_run(ctx, name=FLOW, params=PARAMS, idempotency_key="invoice-7", wait_seconds=0)
    again = await flow_run(ctx, name=FLOW, params=PARAMS, idempotency_key="invoice-7", wait_seconds=0)
    assert again.get("duplicate") is True
    approval = pending_ids(svc)
    assert len(approval) == 1
    svc.approvals.decide(approval[0], "approve", by="owner")
    await h.runner.on_approval(approval[0], "approved")
    await h.runner.run_until_idle()
    assert len(h.site.state["posts"]) == 1


async def test_flow_run_cannot_exceed_the_grant_ceiling(build):
    svc, h = build("commit")
    with pytest.raises(Denied) as e:
        await flow_run(agent_context(svc, ceiling=Effect.READ), name=FLOW, params=PARAMS, wait_seconds=0)
    assert e.value.reason == "ceiling"
    await h.runner.run_until_idle()
    assert h.site.state["posts"] == [] and pending_ids(svc) == []
