"""Ask me first: routines and accounts run on their own unless a person (or the agent) asked to be
asked first; pay and irreversible always ask; the overlays still force a question (ROUTING.md)."""

from __future__ import annotations

import inspect
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_connector_approvals import LANE as CONNECTOR_LANE
from test_connector_approvals import PARAMS as CONNECTOR_PARAMS
from test_connector_approvals import _harness, _post
from test_connector_approvals import _policy as _connector_policy
from test_policy_support import Clock, make_service

from agentos.core.errors import ApprovalPending, InvalidRequest, PolicyDenied
from agentos.core.models import Effect, Principal, RunStatus
from agentos.core.runtime import Runtime
from agentos.policy.approvals import content_digest
from agentos.policy.ask_first import AskFirst, flow_of_target
from agentos.policy.grants import LaneCap, Specials
from agentos.policy.routing import PolicyConfig, RoutingTable, Rule
from agentos.policy.store import DB
from agentos.runner.connectors.surfaces import PaceLimited
from agentos.runner.tools import ask_first_on

LANE = "biz-a/insta"
POST = {"lane_id": LANE, "domain": "instagram.com", "summary": "Post 1 photo", "content": {"caption": "hi"}}


@pytest.fixture
def svc(tmp_path):
    s, _ = make_service(tmp_path, clock=Clock())
    yield s
    s.close()


def ctx_for(svc, tool: str = "t", **kw):
    kw.setdefault("subject", {"id": "agent-1"})
    kw.setdefault("workspaces", ["biz-a"])
    kw.setdefault("ceiling", Effect.IRREVERSIBLE)
    kw.setdefault("specials", Specials(secret=LaneCap()))
    _, tok = svc.issue(**kw)
    return svc.context_for_token(tok, tool=tool)


def flow_content(flow: str = "biz-a.insta.post", version: str = "v1", lane: str = LANE) -> dict:
    return {"flow": flow, "version": version, "lane": lane, "params": {}}


async def asks(coro) -> ApprovalPending:
    with pytest.raises(ApprovalPending) as e:
        await coro
    return e.value


def reason_of(svc, err: ApprovalPending) -> str:
    return svc.approvals.get(err.approval_id).reason or ""


# ---------------------------------------------------------------- the table, row by row

async def test_read_and_draft_run_on_their_own(svc):
    ctx = ctx_for(svc)
    assert (await ctx.authorize(Effect.READ, lane_id=LANE)).route == "auto"
    assert (await ctx.authorize(Effect.DRAFT, lane_id=LANE)).route == "auto"


async def test_a_commit_on_an_account_runs_on_its_own_by_default(svc):
    d = await ctx_for(svc).authorize(Effect.COMMIT, **POST)
    assert d.route == "auto" and "Ask me first is off" in d.reason


async def test_a_commit_in_a_routine_runs_on_its_own_by_default(svc):
    d = await ctx_for(svc).authorize(Effect.COMMIT, lane_id=LANE, content=flow_content())
    assert d.route == "auto"


async def test_a_routine_with_ask_first_on_asks_with_a_plain_reason(svc):
    svc.ask_first.set("flow", "biz-a.insta.post", True, by="owner", person=True)
    err = await asks(ctx_for(svc).authorize(Effect.COMMIT, lane_id=LANE, content=flow_content()))
    assert reason_of(svc, err) == "Ask me first is on for routine biz-a.insta.post"
    # another routine on the same account still runs on its own
    other = await ctx_for(svc).authorize(Effect.COMMIT, lane_id=LANE, content=flow_content("biz-a.insta.reply"))
    assert other.route == "auto"


async def test_the_switch_follows_the_flow_id_across_versions(svc):
    svc.ask_first.set("flow", "biz-a.insta.post", True, by="owner", person=True)
    for version in ("v1", "v2-an-agent-edit"):
        await asks(ctx_for(svc).authorize(Effect.COMMIT, lane_id=LANE, content=flow_content(version=version)))


async def test_an_account_with_ask_first_on_asks_for_ad_hoc_commits_and_its_routines(svc):
    svc.ask_first.set("lane", LANE, True, by="owner", person=True)
    err = await asks(ctx_for(svc).authorize(Effect.COMMIT, **POST))
    assert reason_of(svc, err) == f"Ask me first is on for account {LANE}"
    await asks(ctx_for(svc).authorize(Effect.COMMIT, lane_id=LANE, content=flow_content()))
    other = await ctx_for(svc).authorize(Effect.COMMIT, lane_id="biz-a/linkedin", content={"x": 1})
    assert other.route == "auto"


async def test_a_commit_on_no_account_always_asks(svc):
    err = await asks(ctx_for(svc).authorize(Effect.COMMIT, summary="apply a workspace plan", content={"p": 1}))
    assert "does not act on one account" in reason_of(svc, err)
    svc.ask_first.set("lane", LANE, False, by="owner", person=True)  # no switch reaches it
    await asks(ctx_for(svc).authorize(Effect.COMMIT, summary="apply a plan", content={"p": 2}))


@pytest.mark.parametrize("effect", [Effect.PAY, Effect.IRREVERSIBLE])
async def test_pay_and_irreversible_always_ask_inside_an_auto_routine(svc, effect):
    assert not svc.ask_first.is_on("flow", "biz-a.insta.post")
    await asks(ctx_for(svc).authorize(effect, lane_id=LANE, content=flow_content()))
    await asks(ctx_for(svc).authorize(effect, lane_id=LANE, content={"amount": 5}))


@pytest.mark.parametrize("effect", [Effect.PAY, Effect.IRREVERSIBLE])
async def test_no_switch_and_no_standing_approval_covers_pay_or_irreversible(svc, effect):
    svc.ask_first.set("flow", "biz-a.insta.post", False, by="owner", person=True)
    svc.ask_first.set("lane", LANE, False, by="owner", person=True)
    await asks(ctx_for(svc).authorize(effect, lane_id=LANE, content=flow_content()))
    await asks(ctx_for(svc).authorize(effect, lane_id=LANE, content={"amount": 5}))
    with pytest.raises(InvalidRequest):
        svc.approvals.add_standing(effects=[effect.value], lanes=[LANE], until=svc.clock() + 3600, by="owner")
    with pytest.raises(ValueError, match="never"):
        RoutingTable(standing_effects=[effect.value])


@pytest.mark.parametrize("effect", ["pay", "irreversible"])
async def test_no_replaced_table_makes_pay_or_irreversible_auto(tmp_path, effect):
    cfg = PolicyConfig(routing=RoutingTable(rules=[Rule(effect="*", then="auto"), Rule(effect=effect, then="auto")]))
    svc, _ = make_service(tmp_path, clock=Clock(), config=cfg)
    try:
        await asks(ctx_for(svc).authorize(Effect(effect), lane_id=LANE, content={"amount": 5}))
        assert (await ctx_for(svc).authorize(Effect.COMMIT, **POST)).route == "auto"
    finally:
        svc.close()


async def test_a_handler_cannot_claim_the_routine_runs_on_its_own(svc):
    svc.ask_first.set("lane", LANE, True, by="owner", person=True)
    await asks(ctx_for(svc).authorize(Effect.COMMIT, **POST, facts={"runs_on_own": True, "ask_first": False}))


async def test_untrusted_content_still_forces_a_question_in_an_auto_routine(svc):
    ctx = ctx_for(svc)
    ctx.taint("untrusted", source="browser_snapshot", kind="web_page", host="evil.example")
    err = await asks(ctx.authorize(Effect.COMMIT, lane_id=LANE, content=flow_content()))
    assert reason_of(svc, err) == "it follows untrusted text from a web page at evil.example"


async def test_the_overlays_ignore_a_persons_off(svc):
    """A person's explicit off lets a routine run alone; it never silences an overlay."""
    svc.ask_first.set("flow", "biz-a.insta.post", False, by="owner", person=True)
    svc.ask_first.set("lane", LANE, False, by="owner", person=True)
    ctx = ctx_for(svc)
    ctx.taint("untrusted", source="browser_snapshot", kind="web_page", host="evil.example")
    await asks(ctx.authorize(Effect.COMMIT, lane_id=LANE, content=flow_content()))
    leak = ctx_for(svc, domains=["instagram.com", "evil.example"], egress=["instagram.com"])
    leak.taint("untrusted", "private", source="browser_snapshot")
    await asks(leak.authorize(Effect.COMMIT, lane_id=LANE, domain="evil.example", content=flow_content()))


async def test_the_lethal_trifecta_still_forces_a_question(svc):
    ctx = ctx_for(svc, domains=["instagram.com", "evil.example"], egress=["instagram.com"])
    ctx.taint("untrusted", "private", source="browser_snapshot")
    err = await asks(ctx.authorize(Effect.COMMIT, lane_id=LANE, domain="evil.example", content={"leak": 1}))
    reason = reason_of(svc, err)
    assert "untrusted content and private data" in reason and "outside its allowed list" in reason
    assert not any(word in reason for word in ("egress", "allowlist", "grant family", "trifecta"))


async def test_a_standing_approval_still_covers_a_commit_that_asks_first(svc):
    svc.ask_first.set("lane", LANE, True, by="owner", person=True)
    content = {"caption": "hi"}
    svc.approvals.add_standing(effects=["commit"], lanes=[LANE], until=svc.clock() + 3600, by="owner",
                               targets=["tool:t"], contents=[content_digest(content)])
    assert (await ctx_for(svc).authorize(Effect.COMMIT, **POST)).route == "standing"


async def test_the_owner_in_person_is_never_asked_by_the_switch(svc):
    svc.ask_first.set("lane", LANE, True, by="owner", person=True)
    ctx = svc.owner_context(tool="panel.post")
    ctx.in_person = True
    # the owner's own panel action co-signs (cosigns), so the switch never stops them there
    assert ctx.cosigns(Effect.COMMIT, lane_id=LANE)


# ---------------------------------------------------------------- who may change it

def test_an_agent_turns_it_on_but_never_off(svc):
    s = svc.ask_first.set("flow", "biz-a.insta.post", True, by="agent:a1", person=False)
    assert s.ask_first and not s.by_person
    with pytest.raises(PolicyDenied):
        svc.ask_first.set("flow", "biz-a.insta.post", False, by="agent:a1", person=False)
    assert svc.ask_first.is_on("flow", "biz-a.insta.post")
    # a person turns it off
    off = svc.ask_first.set("flow", "biz-a.insta.post", False, by="owner", person=True)
    assert not off.ask_first and off.by_person
    # an agent's off on a switch that is already off records nothing
    svc.ask_first.set("lane", LANE, False, by="agent:a1", person=False)
    assert svc.ask_first.get("lane", LANE) is None


def test_ids_are_checked(svc):
    for scope, ident in (("flow", "Bad Id"), ("lane", "no-slash"), ("node", "x"), ("flow", "")):
        with pytest.raises(InvalidRequest):
            svc.ask_first.set(scope, ident, True, by="owner", person=True)


def test_flow_of_target():
    assert flow_of_target("flow:biz-a.post@abc") == "biz-a.post"
    assert flow_of_target("connector:instagram.post@x") == "connector.instagram.post"
    assert flow_of_target("tool:browser_act") is None
    assert flow_of_target(None) is None


def connector_content(surface: str = LANE) -> dict:
    return {"operation": "instagram.post", "contract": "c1", "surface": surface,
            "acts_as": {"kind": "account", "id": "1"}, "params": {}}


async def test_a_connector_operation_reads_its_own_switch_and_its_surfaces(svc):
    assert (await ctx_for(svc).authorize(Effect.COMMIT, lane_id=LANE, content=connector_content())).route == "auto"
    svc.ask_first.set("flow", "connector.instagram.post", True, by="owner", person=True)
    err = await asks(ctx_for(svc).authorize(Effect.COMMIT, lane_id=LANE, content=connector_content()))
    assert reason_of(svc, err) == "Ask me first is on for routine connector.instagram.post"
    svc.ask_first.set("flow", "connector.instagram.post", False, by="owner", person=True)
    svc.ask_first.set("lane", LANE, True, by="owner", person=True)
    await asks(ctx_for(svc).authorize(Effect.COMMIT, lane_id=LANE, content=connector_content()))


async def test_a_connector_surface_reads_the_lane_it_posts_through(svc):
    class Surfaces:
        def find(self, surface_id):
            return SimpleNamespace(lane="biz-a/login") if surface_id == "biz-a/page" else None

        def list(self):
            return []  # no surface posts through the lane the connector names

    svc.runtime = Runtime(surfaces=Surfaces())
    ctx = ctx_for(svc, lanes=["biz-a/page"])
    assert (await ctx.authorize(Effect.COMMIT, lane_id="biz-a/page", content=connector_content("biz-a/page"))
            ).route == "auto"
    svc.ask_first.set("lane", "biz-a/login", True, by="owner", person=True)
    err = await asks(ctx.authorize(Effect.COMMIT, lane_id="biz-a/page", content=connector_content("biz-a/page")))
    assert reason_of(svc, err) == "Ask me first is on for account biz-a/login"


async def test_a_commit_on_a_lane_asks_first_when_a_surface_posting_through_it_does(svc):
    """A commit on a lane reads the switch of each surface that posts through it, whatever it sends, as
    the lane's egress guard reads them (node/tools.lane_asks_first); a draft on the lane does not."""
    svc.runtime = Runtime(surfaces=SimpleNamespace(list=lambda: [SimpleNamespace(id="biz-a/page", lane=LANE)]))
    svc.ask_first.set("lane", "biz-a/page", True, by="owner", person=True)
    confirm = {"dialog": "Delete this post?", "accept": True, "text": None}
    err = await asks(ctx_for(svc).authorize(Effect.COMMIT, lane_id=LANE, content=confirm))
    assert reason_of(svc, err) == "Ask me first is on for account biz-a/page"
    assert (await ctx_for(svc).authorize(Effect.DRAFT, lane_id=LANE, content=confirm)).route == "auto"


# ---------------------------------------------------------------- MCP tools and the runner

def _with_policy(tmp_path):
    from agentos.runner.fakes import Harness

    h = Harness(tmp_path / "runner", dispatch_interval_s=0.05)
    svc, _ = make_service(tmp_path / "policy", clock=Clock())
    svc.runtime = h.runtime
    h.runtime.register("policy", svc)
    h.runtime.register("approvals", svc.approvals)
    h.runtime.register("ask_first", svc.ask_first)
    return h, svc


def _tool(h, name: str):
    from agentos.runner.tools import tool_specs

    return next(s for s in tool_specs(h.runtime) if s.name == name).handler


PARAMS = {"caption": "autumn", "image": "a.jpg"}


async def test_an_auto_routine_posts_on_its_own_and_one_that_asks_first_waits(tmp_path):
    from agentos.runner.fakes import post_flow

    h, svc = _with_policy(tmp_path)
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    await h.runner.start()
    try:
        saved = await _tool(h, "flow_save")(svc.context_for_token(tok, tool="flow_save"), definition=post_flow())
        assert saved["ask_first"] is False and saved["ask_first_by"] is None
        out = await _tool(h, "flow_run")(svc.context_for_token(tok, tool="flow_run"), name="biz-a.social.post",
                                         params=PARAMS, wait_seconds=5)
        assert out["status"] in (RunStatus.SUCCEEDED_VERIFIED, RunStatus.SUCCEEDED_UNVERIFIED), out
        assert len(h.site.state["posts"]) == 1

        again = await _tool(h, "flow_save")(svc.context_for_token(tok, tool="flow_save"), definition=post_flow(),
                                            ask_first=True)
        assert again["ask_first"] is True and again["ask_first_by"] == "agent"
        waiting = await _tool(h, "flow_run")(svc.context_for_token(tok, tool="flow_run"), name="biz-a.social.post",
                                             params={**PARAMS, "caption": "winter"}, wait_seconds=0)
        assert waiting["status"] == RunStatus.WAITING_APPROVAL
        assert len(h.site.state["posts"]) == 1
    finally:
        await h.runner.stop()


async def test_an_agent_cannot_turn_it_off_through_flow_save(tmp_path):
    from agentos.runner.fakes import post_flow

    h, svc = _with_policy(tmp_path)
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    svc.ask_first.set("flow", "biz-a.social.post", True, by="owner", person=True)
    save = _tool(h, "flow_save")
    with pytest.raises(PolicyDenied):
        await save(svc.context_for_token(tok, tool="flow_save"), definition=post_flow(title="edited"),
                   ask_first=False)
    assert h.flows.versions("biz-a.social.post") == []  # refused whole: nothing saved
    # an edit without ask_first keeps the person's setting
    edited = await save(svc.context_for_token(tok, tool="flow_save"), definition=post_flow(title="edited"))
    assert edited["ask_first"] is True and edited["ask_first_by"] == "person"


async def test_no_tool_and_no_definition_field_turns_it_off(tmp_path):
    from agentos.runner.fakes import post_flow
    from agentos.runner.tools import tool_specs

    h, svc = _with_policy(tmp_path)
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    svc.ask_first.set("flow", "biz-a.social.post", True, by="owner", person=True)
    assert [s.name for s in tool_specs(h.runtime) if "ask_first" in s.name] == ["ask_first_on"]
    assert set(inspect.signature(ask_first_on).parameters) == {"ctx", "flow", "lane"}  # nothing says off
    with pytest.raises(InvalidRequest):  # a definition has no such field
        await _tool(h, "flow_save")(svc.context_for_token(tok, tool="flow_save"),
                                    definition={**post_flow(), "ask_first": False})
    assert svc.ask_first.is_on("flow", "biz-a.social.post")


async def test_a_new_version_saved_by_an_agent_still_waits(tmp_path):
    from agentos.runner.fakes import post_flow

    h, svc = _with_policy(tmp_path)
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    first = h.save(post_flow())
    svc.ask_first.set("flow", "biz-a.social.post", True, by="owner", person=True)
    await h.runner.start()
    try:
        edited = await _tool(h, "flow_save")(svc.context_for_token(tok, tool="flow_save"),
                                             definition=post_flow(title="Edited by an agent"))
        assert edited["version"] != first.version and edited["ask_first"] is True
        for version in (edited["version"], first.version):
            out = await _tool(h, "flow_run")(svc.context_for_token(tok, tool="flow_run"), name="biz-a.social.post",
                                             params={**PARAMS, "caption": version}, version=version, wait_seconds=0)
            assert out["status"] == RunStatus.WAITING_APPROVAL
        assert h.site.state["posts"] == []
        h.flows.delete("biz-a.social.post")  # a flow saved again under the id still asks
        assert (await _tool(h, "flow_save")(svc.context_for_token(tok, tool="flow_save"),
                                            definition=post_flow()))["ask_first"] is True
    finally:
        await h.runner.stop()


async def test_a_queued_run_asks_when_the_switch_went_on_while_it_waited(tmp_path):
    from agentos.runner.fakes import post_flow

    h, svc = _with_policy(tmp_path)
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    h.save(post_flow())
    later = (h.clock.now() + timedelta(hours=1)).isoformat()
    queued = await _tool(h, "flow_run")(svc.context_for_token(tok, tool="flow_run"), name="biz-a.social.post",
                                        params=PARAMS, not_before=later)
    run = h.runner.get(queued["id"])
    assert run.authorized and run.approval_id is None
    assert await h.runner._gate(run) is None  # nothing changed: it goes ahead
    svc.ask_first.set("flow", "biz-a.social.post", True, by="owner", person=True)
    outcome = await h.runner._gate(run)
    assert outcome is not None and outcome.action == "park" and outcome.approval_id
    assert not run.authorized
    assert svc.approvals.get(outcome.approval_id).reason == "Ask me first is on for routine biz-a.social.post"
    # a run a person started stays theirs
    mine = run.model_copy(update={"source": "panel", "authorized": True, "approval_id": None})
    assert await h.runner._gate(mine) is None


COPY = "biz-a.social.copy"


def _person_asks_first(h, svc):
    from agentos.runner.fakes import post_flow

    h.save(post_flow())
    svc.ask_first.set("flow", "biz-a.social.post", True, by="owner", person=True)
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    return tok, post_flow


async def test_a_copy_under_a_new_id_starts_on_says_why_and_still_waits(tmp_path):
    h, svc = _with_policy(tmp_path)
    tok, post_flow = _person_asks_first(h, svc)
    await h.runner.start()
    try:
        saved = await _tool(h, "flow_save")(svc.context_for_token(tok, tool="flow_save"),
                                            definition=post_flow(id=COPY))
        assert saved["ask_first"] is True and saved["ask_first_by"] == "agent"
        assert "biz-a.social.post" in saved["ask_first_why"] and "—" not in saved["ask_first_why"]
        s = svc.ask_first.get("flow", COPY)
        assert s.set_by == "agent:agent-1" and not s.by_person and s.public()["why"] == s.why
        out = await _tool(h, "flow_run")(svc.context_for_token(tok, tool="flow_run"), name=COPY, params=PARAMS,
                                         wait_seconds=0)
        assert out["status"] == RunStatus.WAITING_APPROVAL
        assert h.site.state["posts"] == []
    finally:
        await h.runner.stop()


async def test_a_copy_cannot_be_saved_with_ask_first_false(tmp_path):
    h, svc = _with_policy(tmp_path)
    tok, post_flow = _person_asks_first(h, svc)
    with pytest.raises(PolicyDenied):
        await _tool(h, "flow_save")(svc.context_for_token(tok, tool="flow_save"), definition=post_flow(id=COPY),
                                    ask_first=False)
    assert h.flows.versions(COPY) == [] and svc.ask_first.get("flow", COPY) is None


async def test_asking_for_it_on_a_copy_keeps_the_reason(tmp_path):
    h, svc = _with_policy(tmp_path)
    tok, post_flow = _person_asks_first(h, svc)
    await _tool(h, "flow_save")(svc.context_for_token(tok, tool="flow_save"), definition=post_flow(id=COPY),
                                ask_first=True)
    assert svc.ask_first.get("flow", COPY).why


async def test_every_agent_route_to_a_new_flow_is_covered_and_a_person_turns_it_off(tmp_path):
    h, svc = _with_policy(tmp_path)
    tok, post_flow = _person_asks_first(h, svc)
    by = {"kind": "agent", "id": "agent-1", "owner": "owner", "via": "mcp"}  # what flow_record_stop passes
    h.flows.save(post_flow(id=COPY), by=by)
    assert svc.ask_first.is_on("flow", COPY)
    h.flows.save(post_flow(id="biz-a.social.token"), by=Principal(kind="human", id="owner", owner="owner"), agent=True)
    assert svc.ask_first.is_on("flow", "biz-a.social.token")  # a bearer token, the owner's included
    svc.ask_first.set("flow", COPY, False, by="owner", person=True)
    edited = await _tool(h, "flow_save")(svc.context_for_token(tok, tool="flow_save"),
                                         definition=post_flow(id=COPY, title="Edited"))
    assert edited["ask_first"] is False and edited["ask_first_by"] == "person"  # the person's off holds


async def test_a_persons_own_save_is_not_affected(tmp_path):
    h, svc = _with_policy(tmp_path)
    _, post_flow = _person_asks_first(h, svc)
    h.flows.save(post_flow(id=COPY), by=Principal(kind="human", id="owner", owner="owner"))
    h.flows.save(post_flow(id="biz-a.social.member"), by=Principal(kind="human", id="mem_1", owner="mem_1"))
    assert svc.ask_first.get("flow", COPY) is None and svc.ask_first.get("flow", "biz-a.social.member") is None


async def test_only_a_persons_switch_on_the_same_lane_passes_on(tmp_path):
    h, svc = _with_policy(tmp_path)
    tok, post_flow = _person_asks_first(h, svc)
    save = _tool(h, "flow_save")
    other = await save(svc.context_for_token(tok, tool="flow_save"),
                       definition=post_flow(id="biz-a.other.post", lane="biz-a/other"))
    assert other["ask_first"] is False  # another account
    pattern = await save(svc.context_for_token(tok, tool="flow_save"),
                         definition=post_flow(id="biz-a.any.post", lane="biz-a/*"))
    assert pattern["ask_first"] is True  # reaches the person's lane
    svc.ask_first.set("flow", "biz-a.social.post", False, by="owner", person=True)
    svc.ask_first.set("flow", "biz-a.any.post", False, by="owner", person=True)
    svc.ask_first.set("flow", "biz-a.social.post", True, by="agent:agent-1", person=False)
    agents = await save(svc.context_for_token(tok, tool="flow_save"), definition=post_flow(id=COPY))
    assert agents["ask_first"] is False  # an agent's own on does not spread


async def test_an_edit_of_a_saved_flow_is_not_a_new_flow(tmp_path):
    h, svc = _with_policy(tmp_path)
    tok, post_flow = _person_asks_first(h, svc)
    h.save(post_flow(id=COPY))  # saved by the harness: no agent, no switch
    edited = await _tool(h, "flow_save")(svc.context_for_token(tok, tool="flow_save"),
                                         definition=post_flow(id=COPY, title="Edited"))
    assert edited["ask_first"] is False and svc.ask_first.get("flow", COPY) is None


def test_a_database_from_before_why_is_upgraded(tmp_path):
    db = DB(tmp_path / "policy.db", """CREATE TABLE ask_first (scope TEXT NOT NULL, id TEXT NOT NULL,
        ask_first INTEGER NOT NULL, set_by TEXT NOT NULL, by_person INTEGER NOT NULL, set_at REAL NOT NULL,
        PRIMARY KEY (scope, id))""")
    db.execute("INSERT INTO ask_first VALUES ('lane', 'biz-a/x', 1, 'owner', 1, 1.0)")
    store = AskFirst(db, clock=lambda: 2.0)
    assert store.get("lane", "biz-a/x").why is None and store.is_on("lane", "biz-a/x")
    assert store.set("flow", "biz-a.x", True, by="agent:a", person=False, why="because").why == "because"


async def test_a_queued_run_a_standing_approval_covered_is_not_asked_again(tmp_path):
    from agentos.runner.fakes import post_flow

    h, svc = _with_policy(tmp_path)
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    fv = h.save(post_flow())
    svc.ask_first.set("lane", "biz-a/social", True, by="owner", person=True)
    content = h.flows.approval_content(fv, "biz-a/social", h.flows.validate_params(fv, PARAMS))
    sa = svc.approvals.add_standing(effects=["commit"], lanes=["biz-a/social"], until=svc.clock() + 3600, by="owner",
                                    count=1, targets=[f"flow:{fv.id}@{fv.version}"], contents=[content_digest(content)])
    later = (h.clock.now() + timedelta(hours=1)).isoformat()
    queued = await _tool(h, "flow_run")(svc.context_for_token(tok, tool="flow_run"), name=fv.id, params=PARAMS,
                                        not_before=later)
    run = h.runner.get(queued["id"])
    assert run.authorized and not run.auto
    assert await h.runner._gate(run) is None  # one use spent, and no second question
    assert svc.approvals.list_standing()[0].id == sa.id and svc.approvals.list_standing()[0].uses == 1


# ---------------------------------------------------------------- what still holds when nothing asks

async def test_a_commit_that_runs_on_its_own_stays_on_the_record(svc):
    ctx = ctx_for(svc)
    await ctx.authorize(Effect.COMMIT, **POST)
    events, _ = svc.traces.query(kind="authorize")
    assert [(e.status, e.effect, e.lane) for e in events] == [("ok", "commit", LANE)]
    assert events[0].summary == "auto"


async def test_a_commit_that_runs_on_its_own_says_whether_the_decisions_were_loaded(svc):
    svc.memory_rules.set_decisions_digest({"biz-a": "sha256:d1"}.get)
    await ctx_for(svc).authorize(Effect.COMMIT, **POST)
    events, _ = svc.traces.query(kind="authorize")
    assert events[0].args["decisions_loaded"] == "never"


async def test_a_commit_that_runs_on_its_own_pays_from_the_budget_and_stops_at_it(svc):
    ctx = ctx_for(svc, budget={"usd": 1.0})
    await ctx.authorize(Effect.COMMIT, **POST, cost={"usd": 0.9})
    assert svc.remaining(ctx.grant)["usd"] == pytest.approx(0.1)
    with pytest.raises(PolicyDenied):
        await ctx.authorize(Effect.COMMIT, lane_id=LANE, content={"n": 2}, cost={"usd": 0.9})


async def test_the_ceiling_and_the_lanes_still_bound_a_commit_that_runs_on_its_own(svc):
    with pytest.raises(PolicyDenied, match="ceiling"):
        await ctx_for(svc, ceiling=Effect.DRAFT).authorize(Effect.COMMIT, **POST)
    with pytest.raises(PolicyDenied):
        await ctx_for(svc).authorize(Effect.COMMIT, lane_id="biz-b/insta", content={"n": 1})


async def test_a_retry_with_a_key_never_posts_twice_when_nothing_asks(tmp_path):
    from agentos.runner.fakes import post_flow

    h, svc = _with_policy(tmp_path)
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    h.save(post_flow())
    await h.runner.start()
    try:
        run = _tool(h, "flow_run")
        first = await run(svc.context_for_token(tok, tool="flow_run"), name="biz-a.social.post", params=PARAMS,
                          idempotency_key="invoice-7", wait_seconds=5)
        again = await run(svc.context_for_token(tok, tool="flow_run"), name="biz-a.social.post", params=PARAMS,
                          idempotency_key="invoice-7", wait_seconds=5)
        assert again["duplicate"] is True and again["id"] == first["id"]
        assert len(h.site.state["posts"]) == 1
    finally:
        await h.runner.stop()


async def test_pace_stops_a_connector_post_that_runs_on_its_own(tmp_path):
    h, _ = _harness(tmp_path)  # pace: min_gap_minutes 90, per_day 3
    svc, tok = _connector_policy(h, tmp_path)

    async def post():
        out = await _post(h)(svc.context_for_token(tok, tool="social_post_photo"), on=CONNECTOR_LANE,
                             caption=CONNECTOR_PARAMS["caption"], image="photo.jpg", wait_seconds=0)
        await h.runner.run_until_idle()
        return h.runner.get(out["id"])

    assert (await post()).status == RunStatus.SUCCEEDED_VERIFIED
    with pytest.raises(PaceLimited, match="at most once every 90 minutes"):
        await post()
    assert len(h.site.state["posts"]) == 1


async def test_ask_first_on_tool_for_a_routine_and_an_account(tmp_path):
    from agentos.runner.fakes import post_flow

    h, svc = _with_policy(tmp_path)
    _, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    h.save(post_flow())
    tool = _tool(h, "ask_first_on")
    out = await tool(svc.context_for_token(tok, tool="ask_first_on"), flow="biz-a.social.post")
    assert out["ask_first"] is True and out["by_person"] is False and out["set_by"] == "agent:agent-1"
    await tool(svc.context_for_token(tok, tool="ask_first_on"), lane="biz-a/social")
    assert svc.ask_first.is_on("lane", "biz-a/social")
    with pytest.raises(InvalidRequest):
        await tool(svc.context_for_token(tok, tool="ask_first_on"))
    from agentos.policy.errors import Denied

    with pytest.raises(Denied):  # outside the grant's lanes
        await tool(svc.context_for_token(tok, tool="ask_first_on"), lane="biz-b/social")


async def test_a_cautious_owner_can_make_everything_ask_until_a_person_turns_it_off(tmp_path):
    cfg = PolicyConfig(routing=RoutingTable(ask_first_default=True))
    svc, _ = make_service(tmp_path, clock=Clock(), config=cfg)
    try:
        await asks(ctx_for(svc).authorize(Effect.COMMIT, **POST))
        assert svc.ask_first.flow_view("biz-a.insta.post") == {"ask_first": True, "ask_first_by": None}
        with pytest.raises(PolicyDenied):  # an agent cannot turn the default off either
            svc.ask_first.set("lane", LANE, False, by="agent:a1", person=False)
        svc.ask_first.set("lane", LANE, False, by="owner", person=True)
        assert (await ctx_for(svc).authorize(Effect.COMMIT, **POST)).route == "auto"
    finally:
        svc.close()


def test_explain_says_a_post_runs_on_its_own_unless_ask_first_is_on():
    from agentos.policy.explain import _route_phrase

    phrase = _route_phrase("Publishing", "commit", RoutingTable())
    assert phrase.startswith("Publishing runs on its own, unless Ask me first is on")
    assert "Paying waits for your approval, and only a person can approve it." == _route_phrase(
        "Paying", "pay", RoutingTable())
