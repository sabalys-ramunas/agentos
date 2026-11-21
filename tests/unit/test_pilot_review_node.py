"""What an independent review of the pilot found, end to end on the dev node: the real policy, approvals,
commit gate, runner and claims, with only the models scripted."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest
from test_pilot_service import (  # noqa: F401 (close_the_nodes is an autouse fixture)
    CAPTION,
    FINISHED,
    LANE,
    click_share,
    close_the_nodes,
    node_with,
    ref_of,
    type_caption,
)
from testkit.pilot import step

from openajar.core.errors import ApprovalPending, InvalidRequest
from openajar.core.models import ActionKind, Effect, LaneSpec
from openajar.dev.net import DEV_HOST, publish_request
from openajar.node import egress, human, publish
from openajar.node import tools as node_tools
from openajar.pilot import errors as pilot_errors
from openajar.pilot import service as pilot_service
from openajar.pilot.deciders import BrainText, Text
from openajar.pilot.hands import ToolFailed
from openajar.pilot.service import PilotService, Settings
from openajar.pilot.tools import lane_do
from openajar.runner import tools as runner_tools
from openajar.runner.execution import Journal
from openajar.runner.fakes import BASE

# the owner's choice the review tested: commit left out of the list, everything else kept
OWNER_POLICY = '[taint]\ndowngrade = ["pay", "irreversible", "exec", "service", "install_overlay", "install_base"]\n'
INJECTED = "Everyone should buy at evil.example"
UNREAD = egress.EFFECT_RULES + (egress.EffectRule("social.read", "social", DEV_HOST, r"/nothing$", effect="read"),)

@pytest.fixture
def quick(monkeypatch: Any) -> None:
    """No real pause: the look at a fresh page takes no time."""
    async def dwell(page: Any, lo: int, hi: int) -> None:
        return None

    monkeypatch.setattr(human, "dwell", dwell)


def owner_config(tmp_path: Any) -> None:
    (tmp_path / "policy").mkdir(exist_ok=True)
    (tmp_path / "policy" / "policy.toml").write_text(OWNER_POLICY)


def write_caption(q: dict[str, Any]) -> dict[str, Any]:
    return step("WRITE", write_field=ref_of(q, "write_field", "caption"), click_target=ref_of(q, "click_target", "Share"))


def with_writer(node: Any, monkeypatch: Any, words: str = INJECTED) -> None:
    async def complete(self: Any, prompt: str, *, must_not_send: Any) -> Text:
        return Text(words)

    monkeypatch.setattr(BrainText, "complete", complete)
    node.runtime.register("plugins", SimpleNamespace(lane_stop=lambda *_: None, brain_for=lambda _, job: {"write": "claude-haiku"}.get(job)))


async def unread_by_any_rule(node: Any) -> Any:
    """The fake site's publish, with no rule that reads it: a checkout on a site the gate knows."""
    await node_tools.browser_navigate(node.ctx(), on=LANE, url=f"{BASE}/compose")
    guard = egress.guard_for(node.page)
    guard.rules = UNREAD
    return guard


async def finish(node: Any, run: dict[str, Any]) -> dict[str, Any]:
    node.approvals.decide(run["approval_id"], "approve")
    await node.runner.poll_approvals()
    await node.runner.run_until_idle()
    return await runner_tools.run_status(node.ctx(), id=run["id"])


# ---- 1: money and the irreversible always need a person ---------------------------------------------

@pytest.mark.parametrize(("sites", "named"), [
    ([], "no site list"),
    (["www.amazon.com"], "www.amazon.com"),
    ([DEV_HOST, "shop.example"], "shop.example"),
    ([DEV_HOST, "frame:pay.example"], "frame:pay.example"),
])
async def test_a_lane_the_gate_cannot_read_is_refused_with_a_plain_reason(tmp_path, quick, sites, named):
    node = await node_with(tmp_path, FINISHED)
    try:
        await node.lanes.create(LaneSpec(id="biz-a/shop", sites=sites))
        with pytest.raises(pilot_errors.PilotLaneUncovered, match=re.escape(named)) as refused:
            await lane_do(node.ctx(), on="biz-a/shop", goal="buy it", wait_s=0)
        assert "person" in refused.value.message and "—" not in refused.value.message
        assert refused.value.code == "pilot_lane_uncovered" and node.brains.calls == []
    finally:
        await node.lanes.shutdown()


async def test_a_lane_whose_list_grew_after_the_call_is_refused_when_it_runs(tmp_path, quick):
    node = await node_with(tmp_path, FINISHED)
    try:
        await node_tools.browser_navigate(node.ctx(), on=LANE, url=f"{BASE}/compose")
        out = await lane_do(node.ctx(), on=LANE, goal="g", wait_s=0)
        node.pilot.rules = egress.EFFECT_RULES  # the gate no longer reads this lane's site
        await node.runner.run_until_idle()
        status = await runner_tools.run_status(node.ctx(), id=out["id"])
        assert status["status"] == "failed" and status["error"]["code"] == "pilot_lane_uncovered"
    finally:
        await node.lanes.shutdown()


async def test_a_lane_the_gate_reads_runs(tmp_path, quick):
    node = await node_with(tmp_path, FINISHED)
    try:
        assert (await node.start())["status"] != "failed" and node.brains.calls
    finally:
        await node.lanes.shutdown()


def test_every_entry_of_a_list_is_read_as_widely_as_it_admits() -> None:
    gaps = egress.ungated_sites
    assert gaps(["www.facebook.com", "*.facebook.com", "facebook.com", "www.linkedin.com", "medium.com"]) == []
    assert gaps(["*.linkedin.com", "*.medium.com", "linkedin.com", "www.amazon.com", "accounts.google.@cc", "*"]) == [
        "*.linkedin.com", "*.medium.com", "linkedin.com", "www.amazon.com", "accounts.google.@cc", "*"]
    assert gaps(["frame:www.facebook.com", "frame:hcaptcha.com"]) == ["frame:hcaptcha.com"]


def test_the_bare_domain_of_a_rule_host_is_held_like_the_rest() -> None:
    """`*.facebook.com` does not match facebook.com, and the gate's route does: the two now agree."""
    found = egress.unknown_rule(egress.EFFECT_RULES, "facebook.com", "https://facebook.com/checkout", "POST")
    assert found is not None and found.id == "facebook.unknown"
    assert egress.unknown_rule(egress.EFFECT_RULES, "notfacebook.com", "https://notfacebook.com/x", "POST") is None


async def test_a_request_no_rule_reads_asks_a_person_with_commit_out_of_the_list(tmp_path, quick, monkeypatch):
    owner_config(tmp_path)
    monkeypatch.setitem(publish.RULE_VOLATILE, "social.unknown", (*publish.RULE_VOLATILE["social.post"],
                                                                  "variables.input.client_mutation_id"))
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    try:
        await unread_by_any_rule(node)
        run = await node.start()
        assert run["status"] == "waiting_approval" and node.posts == []
        approval = node.approvals.get(run["approval_id"])
        assert "does not recognise" in approval.summary and "carries text the agent typed" in approval.summary
        assert "may spend money" in approval.reason
        done = await finish(node, run)
        assert [p["caption"] for p in node.posts] == [CAPTION] and done["output"]["effects"] == 2
    finally:
        await node.lanes.shutdown()


async def test_an_approved_request_that_changes_on_its_retry_does_not_go(tmp_path, quick):
    """No rule says which of its fields are volatile, so a random one reads as other content: the
    approval cannot land, and the run says so instead of claiming the publish."""
    owner_config(tmp_path)
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    try:
        await unread_by_any_rule(node)
        done = await finish(node, await node.start())
        assert node.posts == [] and done["output"]["pilot_status"] == "done_unverified"
        assert done["output"]["reason"] == "last_action_refused" and "publish_changed" in done["output"]["message"]
    finally:
        await node.lanes.shutdown()


async def test_a_login_window_still_lets_that_request_go(tmp_path, quick, monkeypatch):
    """Section 8.11 of the egress design: a password in the vault is the owner's permission. The
    fake site's `.example` has no registrable domain, so it is given one."""
    monkeypatch.setattr(egress.sites, "registrable_domain", lambda host: host)
    monkeypatch.setattr(egress.sites, "same_site", lambda a, b: a == b)

    def after_the_fill(q: dict[str, Any]) -> dict[str, Any]:
        guard.open_login(DEV_HOST)  # what the vault's typist does (the drive's end closes a window, so inside the run)
        return click_share(q)

    node = await node_with(tmp_path, type_caption, after_the_fill, FINISHED)
    try:
        guard = await unread_by_any_rule(node)
        run = await node.start()
        assert run["status"] != "waiting_approval" and [p["caption"] for p in node.posts] == [CAPTION]
    finally:
        await node.lanes.shutdown()


async def test_the_same_request_from_a_plain_agent_follows_the_nodes_policy(tmp_path, quick):
    """Only a pilot grant asks: an agent on its own grant is the owner's to steer."""
    owner_config(tmp_path)
    node = await node_with(tmp_path, FINISHED)
    try:
        await unread_by_any_rule(node)
        snapshot = await node_tools.browser_snapshot(node.ctx(), on=LANE)
        box = re.search(r'textbox "[^"]*[Cc]aption[^"]*".*?\[ref=(\w+)\]', snapshot).group(1)
        share = re.search(r'button "Share".*?\[ref=(\w+)\]', snapshot).group(1)
        await node_tools.browser_act(node.ctx(), on=LANE, ref=box, action=ActionKind.FILL, text=CAPTION)
        await node_tools.browser_act(node.ctx(), on=LANE, ref=share, action=ActionKind.CLICK)
        assert [p["caption"] for p in node.posts] == [CAPTION]
    finally:
        await node.lanes.shutdown()


# ---- 2: words a model wrote always reach a person ---------------------------------------------------

async def test_written_words_ask_with_commit_out_of_the_list_and_show_exactly(tmp_path, quick, monkeypatch):
    owner_config(tmp_path)
    node = await node_with(tmp_path, write_caption, click_share, FINISHED)
    with_writer(node, monkeypatch)
    try:
        run = await node.start(values=None, goal="Reply to the post")
        assert run["status"] == "waiting_approval" and node.posts == []
        approval = node.approvals.get(run["approval_id"])
        assert approval.card["text"]["value"] == INJECTED and "wrote" in approval.reason
        await finish(node, run)
        assert [p["caption"] for p in node.posts] == [INJECTED]
    finally:
        await node.lanes.shutdown()


async def test_text_the_caller_supplied_follows_the_nodes_policy(tmp_path, quick):
    owner_config(tmp_path)
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    try:
        run = await node.start()
        assert run["status"] != "waiting_approval" and [p["caption"] for p in node.posts] == [CAPTION]
    finally:
        await node.lanes.shutdown()


# ---- 5 and 12: a crash never posts twice, and says what may have happened ---------------------------

async def test_a_kill_during_the_first_step_does_not_start_the_goal_again(tmp_path, quick, monkeypatch):
    """kill -9 right after the first click: no `finally` runs, so nothing more reaches the journal.
    Journal writes stop there to stand for that; everything else is real."""
    owner_config(tmp_path)
    node = await node_with(tmp_path, click_share, click_share, FINISHED)
    seen: dict[str, Any] = {"clicks": 0, "dead": False}
    specs, wrap, bind = node.pilot.wiring()
    real_start, real_done = Journal.start, Journal.done

    def alive(real: Any) -> Any:
        def call(self: Any, *a: Any, **k: Any) -> None:
            if not seen["dead"]:
                real(self, *a, **k)
        return call

    monkeypatch.setattr(Journal, "start", alive(real_start))
    monkeypatch.setattr(Journal, "done", alive(real_done))

    def dying(spec: Any, runtime: Any) -> Any:
        fn = wrap(spec, runtime)
        if spec.name != "browser_act":
            return fn

        async def call(**kw: Any) -> Any:
            out = await fn(**kw)
            seen["clicks"] += 1
            if seen["clicks"] == 1:
                seen["dead"] = True
                raise asyncio.CancelledError
            return out
        return call

    async def filled() -> None:
        await node_tools.browser_navigate(node.ctx(), on=LANE, url=f"{BASE}/compose")
        snapshot = await node_tools.browser_snapshot(node.ctx(), on=LANE)
        box = re.search(r'textbox "[^"]*[Cc]aption[^"]*".*?\[ref=(\w+)\]', snapshot).group(1)
        await node_tools.browser_act(node.ctx(), on=LANE, ref=box, action=ActionKind.FILL, text=CAPTION)

    node.pilot._wiring = (specs, dying, bind)
    try:
        await filled()
        out = await lane_do(node.ctx(), on=LANE, goal="press share", wait_s=0, values=None)
        await node.runner.run_until_idle()
        child = next(iter(node.pilot.sessions.values())).child
        assert len(node.posts) == 1

        node.pilot.sessions.clear()  # the restart
        seen["dead"] = False
        fresh = PilotService(node.runtime, Settings(), wiring=(specs, dying, bind))
        fresh.rules = node.pilot.rules
        node.runner.register_executor("pilot", fresh)
        node.runner.recover()
        await filled()
        await node.runner.run_until_idle()

        final = await runner_tools.run_status(node.ctx(), id=out["id"])
        assert seen["clicks"] == 1 and len(node.posts) == 1
        assert final["status"] == "partial" and final["error"]["code"] == "pilot_interrupted"
        assert node.policy.grant_record(child.grant_id)["state"] == "revoked"
    finally:
        await node.lanes.shutdown()


async def test_a_run_writes_its_start_before_its_first_action(tmp_path, quick):
    seen: list[list[str]] = []
    node = await node_with(tmp_path, FINISHED)

    def looks(q: dict[str, Any]) -> dict[str, Any]:
        seen.append([s.path for s in node.runner.store.steps(next(iter(node.pilot.sessions)))])
        return FINISHED

    node.brains.script = [looks]
    try:
        await node.start()
        assert seen == [["0"]]
    finally:
        await node.lanes.shutdown()


async def test_a_parked_run_after_a_restart_is_partial_and_its_child_goes(tmp_path, quick):
    node = await node_with(tmp_path, type_caption, click_share)
    try:
        run = await node.start()
        child = node.pilot.sessions[run["id"]].child
        node.pilot.sessions.clear()
        done = await finish(node, run)
        assert done["status"] == "partial" and done["error"]["code"] == "pilot_interrupted" and node.posts == []
        assert "may have" in done["error"]["message"]
        assert node.policy.grant_record(child.grant_id)["state"] == "revoked"
    finally:
        await node.lanes.shutdown()


async def test_a_failed_claim_after_an_action_reads_partial(tmp_path, quick):
    node = await node_with(tmp_path, type_caption, click_share)
    try:
        run = await node.start()
        tools = node.pilot.sessions[run["id"]].tools

        async def refused(name: str, **_: Any) -> Any:
            raise ToolFailed("denied", "the lane went away")

        tools.call = refused
        done = await finish(node, run)
        assert done["status"] == "partial" and done["error"]["code"] == "denied" and node.posts == []
    finally:
        await node.lanes.shutdown()


async def test_a_crash_after_an_action_reads_partial(tmp_path, quick):
    node = await node_with(tmp_path, type_caption, click_share)
    try:
        run = await node.start()
        tools = node.pilot.sessions[run["id"]].tools
        real = tools.call

        async def crashes(name: str, **kw: Any) -> Any:
            if name == "browser_snapshot":
                raise KeyError("a bug")
            return await real(name, **kw)

        tools.call = crashes
        done = await finish(node, run)
        assert done["status"] == "partial" and done["error"]["code"] == "internal" and "check the lane" in done["error"]["message"]
    finally:
        await node.lanes.shutdown()


async def test_a_crash_before_anything_stays_failed(tmp_path, quick, monkeypatch):
    node = await node_with(tmp_path, FINISHED)
    real = pilot_service.NodeTools.call

    async def crashes(self: Any, name: str, **kw: Any) -> Any:
        if name == "browser_snapshot":
            raise KeyError("a bug")
        return await real(self, name, **kw)

    monkeypatch.setattr(pilot_service.NodeTools, "call", crashes)
    try:
        run = await node.start()
        assert run["status"] == "failed" and run["error"]["code"] == "internal"
    finally:
        await node.lanes.shutdown()


# ---- 9: what a model reads of a run's own arguments is masked too -----------------------------------

async def test_the_goal_and_a_values_label_are_masked_before_a_model_reads_them(tmp_path, quick):
    class Vault:
        def scrub(self, text: str, *_: Any, **__: Any) -> str:
            return text.replace("hunter2", "[redacted]")

    node = await node_with(tmp_path, FINISHED)
    node.runtime.register("vault", Vault())
    try:
        await node.start(goal="Sign in with hunter2", values={"note": "hunter2 again"})
        sent = str(node.brains.calls[0]["questions"])
        assert "[redacted]" in sent and "hunter2" not in sent
    finally:
        await node.lanes.shutdown()


# ---- 14 and 15: the same key ------------------------------------------------------------------------

async def test_a_key_with_other_arguments_is_refused(tmp_path, quick):
    node = await node_with(tmp_path, FINISHED)
    try:
        await node_tools.browser_navigate(node.ctx(), on=LANE, url=f"{BASE}/compose")
        first = await lane_do(node.ctx(), on=LANE, goal="Post A", wait_s=0, idempotency_key="k1")
        await node.runner.run_until_idle()
        with pytest.raises(InvalidRequest, match="other arguments"):
            await lane_do(node.ctx(), on=LANE, goal="Delete everything", wait_s=0, idempotency_key="k1")
        with pytest.raises(InvalidRequest, match="other arguments"):
            await lane_do(node.ctx(), on=LANE, goal="Post A", values={"comment": "x"}, wait_s=0, idempotency_key="k1")
        again = await lane_do(node.ctx(), on=LANE, goal="Post  A", wait_s=0, idempotency_key="k1")  # the same words
        assert again["id"] == first["id"] and again["duplicate"] is True
    finally:
        await node.lanes.shutdown()


async def test_reading_a_duplicates_output_taints_the_reader(tmp_path, quick):
    node = await node_with(tmp_path, FINISHED)
    _, other = node.policy.issue(subject={"id": "other"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    try:
        await node_tools.browser_navigate(node.ctx(), on=LANE, url=f"{BASE}/compose")
        await lane_do(node.ctx(), on=LANE, goal="g", wait_s=0, idempotency_key="k1")
        await node.runner.run_until_idle()
        again = await lane_do(node.ctx(other), on=LANE, goal="g", wait_s=0, idempotency_key="k1")
        assert again["output"] is not None
        _, grant = node.policy.resolve(other)
        assert node.policy.taint_state(grant)["untrusted"] is True
    finally:
        await node.lanes.shutdown()


# ---- 19: a publish a page sends during a scroll -----------------------------------------------------

def publishes_on_the_wheel(node: Any, monkeypatch: Any) -> None:
    async def wheel(page: Any) -> None:
        await page.driver.net.send(publish_request("from the feed", []))

    monkeypatch.setattr(human, "wheel_down", wheel)


async def test_a_hold_during_a_scroll_is_told_to_the_caller(tmp_path, quick, monkeypatch):
    node = await node_with(tmp_path, FINISHED)
    publishes_on_the_wheel(node, monkeypatch)
    try:
        await node_tools.browser_navigate(node.ctx(), on=LANE, url=f"{BASE}/compose")
        await node_tools.browser_snapshot(node.ctx(), on=LANE)  # reading the page is what makes a publish wait
        with pytest.raises(ApprovalPending):
            await node_tools.browser_scroll(node.ctx(), on=LANE)
        assert node.posts == []
    finally:
        await node.lanes.shutdown()


async def test_a_hold_during_a_scroll_parks_the_run_and_it_carries_on(tmp_path, quick, monkeypatch):
    node = await node_with(tmp_path, step("SCROLL"), FINISHED)
    publishes_on_the_wheel(node, monkeypatch)
    try:
        run = await node.start(values=None, path="/")  # the composer is a dialog, and a dialog offers no scroll
        assert run["status"] == "waiting_approval" and node.posts == []
        done = await finish(node, run)  # the fake site only keeps what its own button sends, so the approval tells
        assert node.approvals.get(run["approval_id"]).state == "consumed"
        assert done["output"]["pilot_status"] == "done_unverified" and done["output"]["actions"][0]["operation"] == "SCROLL"
    finally:
        await node.lanes.shutdown()


@pytest.mark.parametrize("platform", sorted(egress.SUGGESTED_SITES))
def test_a_lane_on_a_platforms_reviewed_list_is_known_ground(platform):
    assert egress.unreviewed_sites(egress.SUGGESTED_SITES[platform]) == []


@pytest.mark.parametrize("extra", ["amazon.com", "*.amazon.com", "frame:shop.example", "stripe.com"])
def test_one_unreviewed_site_on_a_reviewed_list_is_still_named(extra):
    assert egress.unreviewed_sites([*egress.SUGGESTED_SITES["facebook"], extra]) == [extra]
