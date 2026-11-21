"""browser_task end to end on the dev node's fake lanes: the real policy service, approvals, commit gate,
runner and claims, with only the decision brain scripted. An agent hands a goal over, the pilot
types and clicks through the node's tools under a child grant, and a publish waits for a person."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from test_policy_support import Clock
from testkit.pilot import pick, step

from openajar.core.errors import InvalidRequest, PolicyDenied, ServiceUnavailable
from openajar.core.models import AccountRef, Effect, LaneSpec
from openajar.core.runtime import Runtime
from openajar.dev.lanes import DevLaneManager
from openajar.dev.net import DEV_HOST, DEV_RULES
from openajar.dev.web import DevInteraction, new_tab
from openajar.node import commitgate, egress, human
from openajar.node import io_tools as node_io
from openajar.node import tools as node_tools
from openajar.node.flow_driver import DWELL_MS
from openajar.pilot import service as pilot_service
from openajar.pilot.grant import pins_for
from openajar.pilot.modes import GOAL
from openajar.pilot.service import PilotService, Settings, load_settings, parse_values, values_of
from openajar.pilot.tools import lane_do
from openajar.policy.service import PolicyService
from openajar.policy.wrap import bind_request_token, wrap_tool
from openajar.runner import service as runner_service
from openajar.runner import tools as runner_tools
from openajar.runner.fakes import BASE

LANE = "biz-a/social"
CAPTION = "Autumn menu is here."


class FakeBrains:
    """BrainService.decide, scripted: a callable per step, which reads the labels it is offered."""

    def __init__(self, *script: Any):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.picks: list[dict[str, Any]] = []

    async def pick(self, principal: Any, **kw: Any) -> dict[str, Any]:
        self.picks.append(kw)
        return {"brain": kw["brain"], "choice": next(iter(kw["options"])), "confidence": 0.9, "tokens": 40}

    async def decide(self, principal: Any, *, workspace: str, state: Any, questions: dict[str, Any],
                     must_not_send: Any, budget: Any = None, job: str | None = None, taint: Any = (),
                     brain: str | None = None, **_: Any) -> dict[str, Any]:
        self.calls.append({"principal": principal, "workspace": workspace, "state": state, "questions": questions,
                           "must_not_send": must_not_send, "budget": budget, "job": job, "brain": brain, "taint": taint})
        entry = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return {"brain": "jev", "model": "jev-test", "answers": entry(questions) if callable(entry) else entry,
                "usage": {"input_tokens": 500, "output_tokens": 0}, "cost_usd": 0.00002, "latency_ms": 200}


def ref_of(questions: dict[str, Any], head: str, word: str) -> str:
    return next(k for k, label in questions[head]["criteria"].items() if word in label)


def type_caption(q: dict[str, Any]) -> dict[str, Any]:
    return step("TYPE", type_value="v1", field_for_v1=ref_of(q, "field_for_v1", "caption"),
                click_target=ref_of(q, "click_target", "Share"))


def click_share(q: dict[str, Any]) -> dict[str, Any]:
    return step("CLICK", click_target=ref_of(q, "click_target", "Share"))


FINISHED = step("DONE", done=0.95)


class Node:
    def __init__(self, tmp_path: Any, *script: Any, settings: Settings | None = None):
        self.lanes = DevLaneManager(make_page=new_tab)
        self.runtime = Runtime(interaction=DevInteraction())
        self.runtime.register("lanes", self.lanes)
        self.policy = PolicyService(tmp_path / "policy", runtime=self.runtime, clock=Clock())
        self.approvals = self.policy.approvals
        self.runtime.register("policy", self.policy)
        self.runtime.register("approvals", self.approvals)
        self.brains = FakeBrains(*script)
        self.runtime.register("brains", self.brains)
        self.runner = runner_service.build(self.runtime, home=tmp_path / "runner", max_concurrent=2)
        specs = [*node_tools.tool_specs(self.runtime), *node_io.tool_specs(self.runtime)]
        self.specs = {s.name: s for s in specs if hasattr(s, "handler")}
        self.pilot = PilotService(self.runtime, settings or Settings(),
                                  wiring=(self.specs, wrap_tool, bind_request_token))
        self.pilot.rules = egress.EFFECT_RULES + DEV_RULES  # as this lane's guard reads them (openajar.dev.net)
        self.pilot.attach()
        self.runtime.register("pilot", self.pilot)
        _, self.token = self.policy.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)

    def close(self) -> None:
        self.policy.close()
        self.runner.store.close()

    def ctx(self, token: str | None = None) -> Any:
        return self.policy.context_for_token(token or self.token, tool="browser_task", lane_id=LANE)

    @property
    def page(self) -> Any:
        return self.lanes._page_for(LANE)

    @property
    def posts(self) -> list[dict]:
        return self.page.driver.site.state["posts"]

    async def start(self, *, path: str = "/compose", **kw: Any) -> dict:
        await node_tools.browser_navigate(self.ctx(), on=LANE, url=f"{BASE}{path}")
        kw.setdefault("goal", f"Post the caption {CAPTION}")
        kw.setdefault("values", {"caption": CAPTION})
        out = await lane_do(self.ctx(), on=LANE, wait_s=0, **kw)
        await self.runner.run_until_idle()
        return await runner_tools.run_status(self.ctx(), id=out["id"])

NODES: list[Any] = []


@pytest.fixture(autouse=True)
def close_the_nodes() -> Any:
    """A real node closes its stores when it stops. A test node has to be told, or each keeps about 9
    files open and hundreds of them push the suite past select's limit of 1024."""
    yield
    while NODES:
        NODES.pop().close()


@pytest.fixture
def quick(monkeypatch: Any) -> list[tuple[int, int]]:
    """No real pause: the look at a fresh page is counted, not waited out."""
    async def dwell(page: Any, lo: int, hi: int) -> None:
        looks.append((lo, hi))

    looks: list[tuple[int, int]] = []
    monkeypatch.setattr(human, "dwell", dwell)
    return looks


async def node_with(tmp_path: Any, *script: Any, **kw: Any) -> Node:
    commitgate.ASKED.clear()
    n = Node(tmp_path, *script, **kw)
    NODES.append(n)
    await n.lanes.create(LaneSpec(id=LANE, sites=[DEV_HOST], account=AccountRef(site="social", handle="@biza.coffee")))
    return n


async def test_a_commit_waits_for_a_person_then_the_run_finishes_by_itself(tmp_path, quick):
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    try:
        run = await node.start()
        assert run["status"] == "waiting_approval" and run["kind"] == "pilot" and node.posts == []
        approval = node.approvals.get(run["approval_id"])
        assert approval.effect == "commit" and approval.card["text"]["value"] == CAPTION  # the exact content
        assert node.pilot.sessions[run["id"]].child.grant_id == approval.grant_id
        assert (await node_tools.lane_queue(node.ctx(), on=LANE)).get("holder") is None  # nobody waits on the person

        node.approvals.decide(run["approval_id"], "approve")
        await node.runner.poll_approvals()
        await node.runner.run_until_idle()
        done = await runner_tools.run_status(node.ctx(), id=run["id"])
        assert [p["caption"] for p in node.posts] == [CAPTION]
        assert done["status"] == "succeeded_verified" and done["output"]["pilot_status"] == "done_verified"
        assert done["output"]["effects"] == 2 and node.approvals.get(run["approval_id"]).state == "consumed"
        assert run["id"] not in node.pilot.sessions
    finally:
        await node.lanes.shutdown()


async def test_a_fresh_page_gets_the_look_a_flow_gives_it(tmp_path, quick):
    node = await node_with(tmp_path, FINISHED)
    try:
        await node.start()
        assert quick == [DWELL_MS]
    finally:
        await node.lanes.shutdown()


async def test_the_nodes_own_token_gets_a_narrow_child_too(tmp_path, quick):
    """The token in a client's MCP config is the owner's: its child is still one lane and its tools."""
    node = await node_with(tmp_path, type_caption, click_share)
    node.policy.owner_token = "owner-token-for-the-test"
    node.token = node.policy.owner_token
    try:
        run = await node.start()
        assert run["status"] == "waiting_approval" and node.posts == []
        child = node.pilot.sessions[run["id"]].child
        _, grant = node.policy.resolve(child.token)
        assert grant.ceiling == Effect.COMMIT and not grant.lane_allowed("biz-a/other") and grant.caps("brain") is None
        assert grant.pins_allow("browser_act", pins_for(GOAL, node.specs)["browser_act"])
    finally:
        await node.lanes.shutdown()


async def test_a_rescue_needs_the_callers_own_brain_right(tmp_path, quick):
    """Going through the pilot never gets a grant more than it holds: this caller has no `brain`."""
    def shaky(q: dict[str, Any]) -> dict[str, Any]:
        return step("CLICK", click_target=pick(ref_of(q, "click_target", "Share"), 0.4))

    node = await node_with(tmp_path, shaky)
    node.runtime.register("plugins", SimpleNamespace(lane_stop=lambda *_: None,
                                                     brain_for=lambda _, job: {"rescue": "claude-haiku"}.get(job, "jev")))
    try:
        run = await node.start(values=None)
        assert run["output"]["pilot_status"] == "refused" and run["output"]["reason"] == "decider_denied"
        assert run["output"]["escalations"][0]["outcome"] == "failed" and node.brains.picks == []
    finally:
        await node.lanes.shutdown()


async def test_each_job_gets_the_brain_a_person_assigned(tmp_path, quick):
    node = await node_with(tmp_path, FINISHED)

    def brain_for(plugin: str, job: str) -> str:
        assert plugin == "browser-worker"
        if job == "write":
            raise ServiceUnavailable("no brain is assigned to the write job")
        return {"decide": "jev", "rescue": "claude-haiku"}[job]

    node.runtime.register("plugins", SimpleNamespace(lane_stop=lambda *_: None, brain_for=brain_for))
    try:
        await node.start()
        assert node.brains.calls[0]["brain"] == "jev"
        assert node.pilot.brain_for("rescue") == "claude-haiku" and node.pilot.brain_for("write") is None
    finally:
        await node.lanes.shutdown()


async def test_a_denied_commit_ends_the_run_and_revokes_its_grant(tmp_path, quick):
    node = await node_with(tmp_path, type_caption, click_share)
    try:
        run = await node.start()
        child = node.pilot.sessions[run["id"]].child
        node.approvals.decide(run["approval_id"], "deny")
        await node.runner.poll_approvals()
        status = await runner_tools.run_status(node.ctx(), id=run["id"])
        assert status["status"] == "failed" and status["error"]["code"] == "denied" and node.posts == []
        assert run["id"] not in node.pilot.sessions
        assert node.policy.grant_record(child.grant_id)["state"] == "revoked"
    finally:
        await node.lanes.shutdown()


async def test_the_child_grant_is_narrow_and_never_above_its_parent(tmp_path, quick):
    node = await node_with(tmp_path, type_caption, click_share)
    try:
        run = await node.start()
        child = node.pilot.sessions[run["id"]].child
        _, grant = node.policy.resolve(child.token)
        assert grant.ceiling == Effect.COMMIT and grant.lane_allowed(LANE) and not grant.lane_allowed("biz-a/other")
        assert grant.caps("brain") is None and grant.caps("exec") is None and grant.caps("secret") is None
        assert grant.pins_allow("browser_act", pins_for(GOAL, node.specs)["browser_act"])
        assert not grant.pins_allow("browser_navigate", "sha256:0")

        call = wrap_tool(node.specs["browser_navigate"], node.runtime)  # outside the mode's tools: the policy says no
        with bind_request_token(child.token), pytest.raises(Exception, match="pins"):
            await call(on=LANE, url=f"{BASE}/compose")
    finally:
        await node.lanes.shutdown()


async def test_a_draft_only_parent_gives_a_draft_only_child(tmp_path, quick):
    node = await node_with(tmp_path, type_caption, click_share)
    try:
        _, token = node.policy.issue(subject={"id": "drafter"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
        node.token = token
        run = await node.start()
        assert run["status"] != "waiting_approval" and node.posts == []  # a publish the grant cannot make is refused
    finally:
        await node.lanes.shutdown()


async def test_a_vault_value_needs_a_parent_that_holds_secret(tmp_path, quick):
    node = await node_with(tmp_path, FINISHED)
    try:
        run = await node.start(values={"password": {"vault": "site-login"}})
        assert run["status"] == "failed" and run["error"]["code"] in ("denied", "invalid")
    finally:
        await node.lanes.shutdown()


async def test_every_decision_goes_as_the_caller_with_the_run_as_its_budget(tmp_path, quick):
    node = await node_with(tmp_path, type_caption, click_share)
    try:
        run = await node.start()
        call = node.brains.calls[0]
        assert call["principal"].id == "claude-code" and call["job"] == run["id"] and call["workspace"] == "biz-a"
        assert call["budget"].max_usd == 0.25 and call["must_not_send"] == [] and call["taint"] == ("untrusted",)
    finally:
        await node.lanes.shutdown()


async def test_without_a_rescue_the_run_says_what_it_needs_and_no_rescue_cost_appears(tmp_path, quick):
    def shaky(q: dict[str, Any]) -> dict[str, Any]:
        return step("CLICK", click_target=pick(ref_of(q, "click_target", "Share"), 0.4))

    node = await node_with(tmp_path, shaky)
    try:
        run = await node.start(values=None)
        assert run["status"] == "failed" and run["error"]["code"] == "pilot_needs_help"
        assert "rescue model" in run["error"]["message"] and "fix" in run["error"]
        assert run["output"]["escalations"][0]["outcome"] == "no_rescue"
    finally:
        await node.lanes.shutdown()


async def test_a_second_call_with_the_same_key_is_the_same_run(tmp_path, quick):
    node = await node_with(tmp_path, FINISHED)
    try:
        await node_tools.browser_navigate(node.ctx(), on=LANE, url=f"{BASE}/compose")
        first = await lane_do(node.ctx(), on=LANE, goal="g", idempotency_key="k1", wait_s=0)
        await node.runner.run_until_idle()
        again = await lane_do(node.ctx(), on=LANE, goal="g", idempotency_key="k1", wait_s=0)
        assert again["id"] == first["id"] and again["duplicate"] is True
    finally:
        await node.lanes.shutdown()


async def test_cancel_stops_a_run_at_its_next_step(tmp_path, quick):
    node = await node_with(tmp_path, step("SCROLL"))
    try:
        await node_tools.browser_navigate(node.ctx(), on=LANE, url=f"{BASE}/compose")
        out = await lane_do(node.ctx(), on=LANE, goal="g", wait_s=0)
        await runner_tools.run_cancel(node.ctx(), id=out["id"])
        await node.runner.run_until_idle()
        status = await runner_tools.run_status(node.ctx(), id=out["id"])
        assert status["status"] == "cancelled" and node.brains.calls == []
    finally:
        await node.lanes.shutdown()


async def test_a_restart_never_starts_the_goal_again(tmp_path, quick):
    node = await node_with(tmp_path, type_caption, click_share)
    try:
        run = await node.start()
        node.pilot.sessions.clear()  # what a restart does to the loop's state
        node.approvals.decide(run["approval_id"], "approve")
        await node.runner.poll_approvals()
        await node.runner.run_until_idle()
        status = await runner_tools.run_status(node.ctx(), id=run["id"])
        assert status["status"] == "partial" and status["error"]["code"] == "pilot_interrupted" and node.posts == []
    finally:
        await node.lanes.shutdown()


async def test_a_page_is_masked_before_a_model_reads_it(tmp_path, quick):
    class Vault:
        def scrub(self, text: str, *_: Any, **__: Any) -> str:
            return text.replace("Write a caption", "[redacted]")

    node = await node_with(tmp_path, FINISHED)
    node.runtime.register("vault", Vault())
    try:
        await node.start()
        assert "[redacted]" in node.brains.calls[0]["state"]["outline"]
        assert "Write a caption" not in node.brains.calls[0]["state"]["outline"]
    finally:
        await node.lanes.shutdown()


# ---- the arguments ---------------------------------------------------------------------------------

def test_a_plain_string_that_looks_like_a_secret_is_refused() -> None:
    for name in ("password", "Passcode", "api_key", "otp", "secret answer", "session token"):
        with pytest.raises(InvalidRequest, match="vault"):
            parse_values({name: "x"})
    assert parse_values({"comment": "Nice"}) == [{"name": "comment", "text": "Nice"}]


def test_a_vault_value_shows_a_model_only_a_label() -> None:
    [value] = values_of(parse_values({"password": {"vault": "reddit-login"}}))
    assert value.vault == "reddit-login" and value.secret and value.text is None
    assert "reddit-login" not in value.label


async def test_arguments_are_checked_before_anything_runs(tmp_path, quick):
    node = await node_with(tmp_path, FINISHED)
    try:
        for bad in ({"goal": "  "}, {"goal": "x" * 3000}, {"goal": "g", "values": {"a": 1}}, {"goal": "g", "max_steps": 0},
                    {"goal": "g", "read": " "}):
            with pytest.raises(InvalidRequest):
                await lane_do(node.ctx(), on=LANE, wait_s=0, **bad)
        with pytest.raises(PolicyDenied):
            await lane_do(node.ctx(), on="other/lane", goal="g", wait_s=0)
    finally:
        await node.lanes.shutdown()


def test_settings_read_the_file_and_a_bad_file_falls_back(tmp_path) -> None:
    path = pilot_service.settings_path(tmp_path)
    path.parent.mkdir()
    path.write_text("max_steps = 40\n")
    settings = load_settings(tmp_path)
    assert settings.limits().max_steps == 40
    assert settings.limits(100).max_steps == 40 and settings.limits(5).max_steps == 5
    path.write_text("nope = [")
    assert load_settings(tmp_path) == Settings()


def test_error_codes_read_like_the_catalog() -> None:
    from openajar.core import catalog

    for code in pilot_service.ERROR_CODES.values():
        assert catalog.entry(code) is not None and "—" not in catalog.entry(code).meaning
    assert re.fullmatch(r"pilot_\w+", "pilot_held")


async def test_the_plugin_builds_the_service_and_it_takes_pilot_runs(tmp_path) -> None:
    """What openajar.node.daemon.build_plugins does with `openajar.pilot.service`."""
    from openajar.core.models import RunKind

    runtime = Runtime(interaction=DevInteraction())
    runner = runner_service.build(runtime, home=tmp_path / "runner", max_concurrent=1)
    built = pilot_service.build(runtime)
    assert runtime.require("pilot") is built and runner.executors[RunKind.PILOT] is built
    assert not callable(getattr(built, "close", None))  # the daemon calls a service's close() with no arguments
