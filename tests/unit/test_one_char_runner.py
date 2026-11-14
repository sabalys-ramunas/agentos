"""The approval binds what a queued run will do: one changed character before dispatch fails it.

Each refused run is checked by the runner right before it executes (Runner._approval_mismatch):
it fails with approval_mismatch and the fake site records no post.
"""

import test_connector_approvals as tca
from connector_samples import POST_PARAMS

from agentos.core.models import RunStatus
from agentos.engage.adapters import PRINCIPAL, SOURCE, RunnerOperations
from agentos.runner.fakes import FakeApprovals, FakePolicy, Harness, post_flow

PARAMS = {"caption": "Autumn", "image": "files/a.jpg"}
FID = tca.FID
LANE = tca.LANE


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


async def keyed_run_approved(h, svc, ops, key: str):
    """A connector run on LANE whose keyed card (made the way engage makes it) is approved and queued."""
    approval_id = ops._ask(h.runner, FID, LANE, POST_PARAMS, key)
    run, _ = h.runner.submit(flow_id=FID, params=POST_PARAMS, surface=LANE, principal=dict(PRINCIPAL),
                             source=SOURCE, idempotency_key=key, authorized=False, approval_id=approval_id)
    assert run.status == RunStatus.WAITING_APPROVAL
    svc.approvals.decide(approval_id, "approve", by="owner")
    assert await h.runner.poll_approvals() == 1
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


async def test_a_keyed_connector_card_fails_the_run_when_its_key_or_a_param_changes(tmp_path):
    """An engage-style keyed card: one character of the idempotency key or of a param changed after approval fails the run and posts nothing."""
    h, _ = tca._harness(tmp_path, ops={"post_photo": tca._no_pace()})
    svc, _ = tca._policy(h, tmp_path)
    h.runtime.register("approvals", svc.approvals)
    ops = RunnerOperations(h.runtime)

    by_key = await keyed_run_approved(h, svc, ops, "engage-key-1")
    stored = h.store.get_run(by_key.id)
    stored.idempotency_key = one_char("engage-key-1")
    assert h.store.save_run(stored)

    by_param = await keyed_run_approved(h, svc, ops, "engage-key-2")
    stored = h.store.get_run(by_param.id)
    stored.params = {**stored.params, "caption": one_char(POST_PARAMS["caption"])}
    assert h.store.save_run(stored)

    await h.runner.run_until_idle()

    assert_refused(h, by_key.id, because="is for a different action")
    assert_refused(h, by_param.id, because="is for different content")


async def test_the_keyed_connector_card_unchanged_posts_once(tmp_path):
    """Control: the same keyed connector run, approved and unchanged, posts exactly once."""
    h, _ = tca._harness(tmp_path, ops={"post_photo": tca._no_pace()})
    svc, _ = tca._policy(h, tmp_path)
    h.runtime.register("approvals", svc.approvals)
    run = await keyed_run_approved(h, svc, RunnerOperations(h.runtime), "engage-key-0")
    await h.runner.run_until_idle()

    assert h.runner.get(run.id).status == RunStatus.SUCCEEDED_VERIFIED
    assert len(h.site.state["posts"]) == 1


async def test_a_legacy_card_without_binding_runs_unchanged_and_refuses_a_changed_param(tmp_path):
    """An engage card made before ActionBinding (binding None, bare legacy hash) runs when unchanged and is refused after a one-character param change."""
    approvals = FakeApprovals()
    h = Harness(tmp_path, services={"approvals": approvals})
    fv = h.save(post_flow())

    def legacy_run(key: str, approval_id: str):
        run, _ = h.runner.submit(flow_id=fv.id, params=PARAMS, authorized=False, idempotency_key=key,
                                 approval_id=approval_id)
        binding = h.runner.run_binding(fv, run)
        approvals.items[approval_id] = {
            "id": approval_id, "kind": "action", "state": "pending", "binding": None, "tool": fv.id,
            "lane": run.target,
            "content_digest": binding.content_digest, "content_hash": binding.legacy_engage_hash(),
            "summary": binding.summary, "content": binding.content,
        }
        return run

    unchanged = legacy_run("legacy-ok", "ap_ok")
    approvals.set("ap_ok", "approved")
    assert await h.runner.poll_approvals() == 1
    await h.runner.run_until_idle()
    assert h.runner.get(unchanged.id).status == RunStatus.SUCCEEDED_VERIFIED
    assert len(h.site.state["posts"]) == 1

    changed = legacy_run("legacy-bad", "ap_bad")
    approvals.set("ap_bad", "approved")
    assert await h.runner.poll_approvals() == 1
    stored = h.store.get_run(changed.id)
    stored.params = {**stored.params, "caption": "Autumm"}
    assert h.store.save_run(stored)
    await h.runner.run_until_idle()

    assert_refused(h, changed.id, because="is for different content", posts=1)


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
    from agentos.policy.approvals import BINDING_CALL, content_digest

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
