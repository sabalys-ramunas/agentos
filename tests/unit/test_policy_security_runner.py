"""Security review: where the runner meets the policy service.

These run the real PolicyService against the runner's fake site and driver, so a flow
that "posts" really changes the fake site's state. Each test was an open finding (SEC-1,
SEC-2) pinned as a strict xfail; they now pass and pin the fix.
"""

from __future__ import annotations

from test_policy_support import Clock

from openajar.core.errors import InvalidRequest
from openajar.core.models import Effect, RunStatus
from openajar.policy.errors import Denied
from openajar.policy.service import PolicyService
from openajar.runner.fakes import BASE, Harness, post_flow
from openajar.runner.tools import tool_specs

PARAMS = {"caption": "posted without a co-sign", "image": "a.jpg"}


def with_policy(tmp_path, **options) -> tuple[Harness, PolicyService]:
    h = Harness(tmp_path / "runner", **options)
    svc = PolicyService(tmp_path / "policy", runtime=h.runtime, clock=Clock())
    h.runtime.register("policy", svc)
    return h, svc


def tool(h: Harness, name: str):
    return next(s for s in tool_specs(h.runtime) if s.name == name).handler


async def test_draft_grant_cannot_post_through_a_self_declared_read_flow(tmp_path):
    h, svc = with_policy(tmp_path, dispatch_interval_s=0.05)
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    await h.runner.start()
    try:
        await tool(h, "flow_save")(svc.context_for_token(tok, tool="flow_save"),
                                   definition=post_flow(id="biz-a.social.harmless", effect="read"))
        await tool(h, "flow_run")(svc.context_for_token(tok, tool="flow_run"), name="biz-a.social.harmless",
                                  params=PARAMS, wait_seconds=5)
    except (Denied, InvalidRequest):
        pass  # fixed: the save or the run is refused
    finally:
        await h.runner.stop()
    assert h.site.state["posts"] == []


async def test_api_submitted_commit_run_waits_for_a_co_sign(tmp_path):
    h, _ = with_policy(tmp_path)
    fv = h.save(post_flow())
    run = await h.run(fv.id, PARAMS, authorized=False, source="api")
    assert run.status == RunStatus.WAITING_APPROVAL
    assert h.site.state["posts"] == []


async def test_queued_run_of_a_revoked_grant_does_not_execute(tmp_path):
    h, svc = with_policy(tmp_path)
    g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    h.save({"id": "biz-a.social.look", "lane": "biz-a/social", "effect": "draft",
            "steps": [{"type": "navigate", "url": f"{BASE}/"}]})
    out = await tool(h, "flow_run")(svc.context_for_token(tok, tool="flow_run"), name="biz-a.social.look",
                                    wait_seconds=0)
    svc.revoke(g.id, reason="the agent went off the rails")
    await h.runner.run_until_idle()
    assert h.runner.get(out["id"]).status not in (RunStatus.SUCCEEDED_VERIFIED, RunStatus.SUCCEEDED_UNVERIFIED)
