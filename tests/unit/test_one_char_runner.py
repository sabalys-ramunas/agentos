"""The approval binds what a queued run will do: one changed character before dispatch fails it.

Each refused run is checked by the runner right before it executes (Runner._approval_mismatch):
it fails with approval_mismatch and the fake site records no post.
"""

from agentos.core.models import RunStatus
from agentos.runner.fakes import FakeApprovals, FakePolicy, Harness, post_flow

LANE = "biz-a/social"

PARAMS = {"caption": "Autumn", "image": "files/a.jpg"}


def one_char(text: str) -> str:
    """The text with its last character replaced by a different one."""
    return text[:-1] + ("0" if text[-1] != "0" else "1")


def assert_refused(h, run_id: str, *, because: str, posts: int = 0):
    """The run failed approval_mismatch for the named reason, and the site holds only `posts` posts."""
    run = h.runner.get(run_id)
    assert run.status == RunStatus.FAILED
    assert run.error["code"] == "approval_mismatch" and because in run.error["message"]
    assert len(h.site.state["posts"]) == posts
    return run


def parked_flow_run(tmp_path):
    """A plain flow run parked on the policy gate, with its fake approvals service."""
    approvals = FakeApprovals()
    h = Harness(tmp_path, services={"policy": FakePolicy(approvals), "approvals": approvals})
    return h, approvals, h.save(post_flow())


async def parked_and_approved(h, fv):
    run = await h.run(fv.id, PARAMS, authorized=False, principal={"kind": "flow", "id": "schedule:x"})
    assert run.status == RunStatus.WAITING_APPROVAL and run.approval_id == "ap_1"
    return run


async def test_a_changed_shown_param_after_approval_fails_the_run_and_posts_nothing(tmp_path):
    """One character of a shown param changed after approval: the run fails approval_mismatch and posts nothing."""
    h, approvals, fv = parked_flow_run(tmp_path)
    run = await parked_and_approved(h, fv)
    approvals.set("ap_1", "approved")
    assert await h.runner.poll_approvals() == 1

    stored = h.store.get_run(run.id)
    stored.params = {**stored.params, "caption": "Autumm"}
    assert h.store.save_run(stored)
    await h.runner.run_until_idle()

    assert_refused(h, run.id, because="is for different content")
    assert h.driver.log == []


async def test_the_same_parked_run_approved_and_unchanged_posts_once(tmp_path):
    """Control: the same approved run, unchanged, posts exactly once, so the rig does post."""
    h, approvals, fv = parked_flow_run(tmp_path)
    run = await parked_and_approved(h, fv)
    approvals.set("ap_1", "approved")
    assert await h.runner.poll_approvals() == 1
    await h.runner.run_until_idle()

    assert h.runner.get(run.id).status == RunStatus.SUCCEEDED_VERIFIED
    assert [p["caption"] for p in h.site.state["posts"]] == ["Autumn"]


async def test_a_changed_approval_digest_fails_the_run_and_posts_nothing(tmp_path):
    """The approval's content_digest changed by one character: the run fails approval_mismatch and posts nothing."""
    h, approvals, fv = parked_flow_run(tmp_path)
    run = await parked_and_approved(h, fv)
    approvals.set("ap_1", "approved")
    assert await h.runner.poll_approvals() == 1

    approvals.items["ap_1"]["content_digest"] = one_char(approvals.items["ap_1"]["content_digest"])
    await h.runner.run_until_idle()

    assert_refused(h, run.id, because="is for different content")
    assert h.driver.log == []


async def test_an_approval_gone_before_dispatch_fails_the_run_and_posts_nothing(tmp_path):
    """The approval is deleted after it was approved and before dispatch: the run fails approval_mismatch and posts nothing."""
    h, approvals, fv = parked_flow_run(tmp_path)
    run = await parked_and_approved(h, fv)
    approvals.set("ap_1", "approved")
    assert await h.runner.poll_approvals() == 1

    del approvals.items["ap_1"]
    await h.runner.run_until_idle()

    assert_refused(h, run.id, because="is gone")
    assert h.driver.log == []


async def test_an_authorized_run_without_an_approval_and_an_auto_run_skip_the_check(tmp_path):
    """A run started authorized with no approval, and an auto run, are not checked and behave as before."""
    approvals = FakeApprovals()
    h = Harness(tmp_path, services={"approvals": approvals})
    fv = h.save(post_flow())
    looked: list[str] = []
    real_get = approvals.get

    async def counting_get(approval_id: str):
        looked.append(approval_id)
        return await real_get(approval_id)

    approvals.get = counting_get

    plain = h.submit(fv.id, PARAMS)  # authorized, no approval_id
    await h.runner.run_until_idle()
    assert h.runner.get(plain.id).status == RunStatus.SUCCEEDED_VERIFIED
    assert looked == [] and len(h.site.state["posts"]) == 1

    # an auto run whose card is one character off still runs: the check is skipped for auto runs
    content = h.flows.approval_content(fv, LANE, PARAMS)
    approvals.create("ap_auto", content=content, summary="auto card")
    approvals.items["ap_auto"]["content_digest"] = one_char(approvals.items["ap_auto"]["content_digest"])
    auto = h.submit(fv.id, PARAMS, auto=True, approval_id="ap_auto")
    assert auto.status == RunStatus.WAITING_APPROVAL
    approvals.set("ap_auto", "approved")
    assert await h.runner.poll_approvals() == 1
    await h.runner.run_until_idle()

    assert h.runner.get(auto.id).status == RunStatus.SUCCEEDED_VERIFIED
    assert len(h.site.state["posts"]) == 2


async def test_a_run_released_by_a_steps_own_card_is_not_held_to_the_runs_content(tmp_path):
    """A card a step asked for (a fill, an upload) names no flow, so releasing the run with it is not refused."""
    from agentos.policy.approval_binding import BINDING_CALL, content_digest

    approvals = FakeApprovals()
    h = Harness(tmp_path, services={"approvals": approvals})
    fv = h.save(post_flow())
    run, _ = h.runner.submit(flow_id=fv.id, params=PARAMS, authorized=False, approval_id="ap_step")
    approvals.items["ap_step"] = {"id": "ap_step", "kind": "action", "state": "pending", "tool": "vault_fill",
                                  "binding": BINDING_CALL, "content_digest": content_digest({"action": "fill"}),
                                  "summary": "Fill a login", "content": {"action": "fill"}}
    approvals.set("ap_step", "approved")
    assert await h.runner.poll_approvals() == 1
    await h.runner.run_until_idle()
    assert h.runner.get(run.id).status == RunStatus.SUCCEEDED_VERIFIED
    assert len(h.site.state["posts"]) == 1
