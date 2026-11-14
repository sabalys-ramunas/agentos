"""A retried flow_run, or a retried connector tool, without an idempotency key (QA-2 #11).

An agent whose call timed out calls again with the same arguments, as it is told to. Before, each call
made a new run and a new approval, so approving both posted twice on a real account. Now an identical
call while the first run is still open (queued, running or waiting for approval) hands back that run;
a new key, other params or another grant is a new run."""

from __future__ import annotations

from test_connector_approvals import LANE, _harness, _no_pace, _policy, _post
from test_policy_support import Clock

from agentos.core.models import Effect, RunStatus
from agentos.policy.service import PolicyService
from agentos.runner.fakes import Harness, post_flow
from agentos.runner.tools import tool_specs

PARAMS = {"caption": "Autumn at the lake", "image": "a.jpg"}


def _tool(h: Harness, name: str):
    return next(s for s in tool_specs(h.runtime) if s.name == name).handler


def _asking(tmp_path):
    h = Harness(tmp_path / "runner")
    svc = PolicyService(tmp_path / "policy", runtime=h.runtime, clock=Clock())
    h.runtime.register("policy", svc)
    h.runtime.register("approvals", svc.approvals)
    svc.ask_first.set("lane", LANE, True, by="owner", person=True)
    h.save(post_flow())
    _, tok = svc.issue(subject={"id": "agent-a"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    return h, svc, tok


async def test_an_identical_flow_run_retry_returns_the_waiting_run_and_asks_once(tmp_path):
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")

    async def call(**kw):
        return await run(svc.context_for_token(tok, tool="flow_run"), name="biz-a.social.post",
                         wait_seconds=0, **{"params": PARAMS, **kw})

    first = await call()
    assert first["status"] == "waiting_approval"
    again = await call()
    assert again["id"] == first["id"] and again["duplicate"] is True, again
    assert again["approval_id"] == first["approval_id"]
    assert len([a for a in svc.approvals.list(state="pending")]) == 1
    # a new key is a new run; so are other params
    keyed = await call(idempotency_key="second-post")
    assert keyed["id"] != first["id"] and not keyed.get("duplicate")
    other = await call(params={**PARAMS, "caption": "Another day at the lake"})
    assert other["id"] not in (first["id"], keyed["id"])
    # another grant asking the same is its own run
    _, tok2 = svc.issue(subject={"id": "agent-b"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    theirs = await run(svc.context_for_token(tok2, tool="flow_run"), name="biz-a.social.post",
                       params=PARAMS, wait_seconds=0)
    assert theirs["id"] != first["id"]
    # once the first run is over, the same call gets the keyed run still open (a keyless retry of a keyed
    # call is that call), and once both are over it is a new run
    await h.runner.cancel(first["id"])
    assert h.runner.get(first["id"]).status == RunStatus.CANCELLED
    assert (await call())["id"] == keyed["id"]
    await h.runner.cancel(keyed["id"])
    later = await call()
    assert later["id"] not in (first["id"], keyed["id"]) and not later.get("duplicate")
    svc.close()


async def test_an_identical_connector_retry_returns_the_waiting_run(tmp_path):
    h, _inbox = _harness(tmp_path, ops={"post_photo": _no_pace()})
    svc, tok = _policy(h, tmp_path)
    svc.ask_first.set("lane", LANE, True, by="owner", person=True)
    post = _post(h)

    async def call(**kw):
        return await post(svc.context_for_token(tok, tool="social_post_photo"), on=LANE,
                          caption="Autumn at the lake", image="photo.jpg", wait_seconds=0, **kw)

    first = await call()
    assert first["status"] == "waiting_approval", first
    again = await call()
    assert again["id"] == first["id"] and again["duplicate"] is True, again
    assert len(svc.approvals.list(state="pending")) == 1
    keyed = await call(idempotency_key="k2")
    assert keyed["id"] != first["id"]
    svc.close()


async def test_the_retry_after_the_approve_releases_the_same_run_and_posts_once(tmp_path):
    """The server tells an agent to call again with the same arguments once a person approved. That
    call used to make a second run that took the approval, and the first run asked again."""
    h, svc, tok = _asking(tmp_path)
    run = _tool(h, "flow_run")
    ctx = lambda: svc.context_for_token(tok, tool="flow_run")
    first = await run(ctx(), name="biz-a.social.post", params=PARAMS, wait_seconds=0)
    svc.approvals.decide(first["approval_id"], "approve", by="owner")
    again = await run(ctx(), name="biz-a.social.post", params=PARAMS, wait_seconds=0)
    assert again["id"] == first["id"] and again["duplicate"] and again["status"] == "queued", again
    await h.runner.run_until_idle()
    assert h.runner.get(first["id"]).status == RunStatus.SUCCEEDED_VERIFIED
    assert len(h.site.state["posts"]) == 1
    assert len(h.runner.store.list_runs(flow_id="biz-a.social.post")) == 1
    assert svc.approvals.list(state="pending") == []
    svc.close()


async def test_run_status_says_why_it_waits_and_reads_queued_as_soon_as_a_person_approves(tmp_path):
    """QA 2026-11-10 tester A 4 and 6: the agent can tell the human why, and a run just approved
    does not read "waiting for the owner" until the next poll."""
    from test_policy_support import PRESENT

    h, svc, tok = _asking(tmp_path)
    first = await _tool(h, "flow_run")(svc.context_for_token(tok, tool="flow_run"), name="biz-a.social.post",
                                       params=PARAMS, wait_seconds=0)
    assert first["status"] == "waiting_approval" and first.get("why"), first
    assert "no need to call flow_run again" in first["message"]
    status = _tool(h, "run_status")
    waiting = await status(svc.context_for_token(tok, tool="run_status"), id=first["id"])
    assert waiting["why"] == first["why"] and "starts on its own" in waiting["summary"]
    svc.approvals.decide(first["approval_id"], "approve", by="owner", proof=PRESENT)
    after = await status(svc.context_for_token(tok, tool="run_status"), id=first["id"])
    assert after["status"] != "waiting_approval" and "why" not in after, after
