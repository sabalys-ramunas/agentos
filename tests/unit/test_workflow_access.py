"""How people, agents and controllers reach workflows: who is a person, what a tool may do, and what
crosses the channel. A relayed webhook is checked on the node, so a controller can forge nothing."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import secrets
import time

import httpx
import pytest
import yaml
from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop
from channel_support import start_controller, wait_for
from test_node_panel import Node, bearer
from typer.testing import CliRunner
from workflow_support import LIST, MAIL, OWNER, action, commit_flow, make_node, read_flow, workflow

from openajar.core.errors import InvalidRequest, NotFound, PolicyDenied
from openajar.core.models import Effect, LaneSpec
from openajar.events.service import build as build_events
from openajar.mcp import providers as mcp_providers
from openajar.node.channel import reports, wire
from openajar.node.channel.workflows import WorkflowSync, relay_hook, relay_unsubscribe
from openajar.runner.schedules import sign_webhook
from openajar.runner.workflows import tools as wf_tools
from openajar.runner.workflows.templates import TEMPLATES

OWNER_P = {"kind": "human", "id": "owner", "owner": "owner"}
READ = "biz-a.social.read"
POST = "biz-a.social.post"


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.h.save(read_flow(), by=OWNER_P)
    n.h.save(commit_flow(), by=OWNER_P)
    return n


class ToolCtx:
    """A tool call as an agent makes it, even one holding a token that reaches everything."""

    def __init__(self, node, ceiling=Effect.COMMIT, workspaces=("biz-a",)):
        actor = node.agent(ceiling, workspaces)
        self.runtime = node.h.runtime
        self.grant = node.svc.grant_by_id(actor.grant_id)
        self.principal = self.grant.principal()
        self.workspaces = workspaces

    def can_access_lane(self, lane):
        return lane.split("/")[0] in self.workspaces

    async def authorize(self, effect, **kwargs):
        return None  # routing is the policy's own tests; these check what the tools do


# ---- tools: always an agent ----

async def test_a_tool_is_never_a_person_whatever_token_it_holds(node):
    ctx = ToolCtx(node)
    saved = await wf_tools.workflow_save(ctx, definition=workflow(action("look", READ), trigger={"kind": "webhook"}))
    with pytest.raises(PolicyDenied):
        node.wf.enable(saved["id"], wf_tools._actor(ctx))
    assert wf_tools._actor(ctx).person is False
    assert (await wf_tools.workflow_get(ctx, id=saved["id"]))["enabled"] is False


async def test_a_tool_can_only_turn_ask_first_on(node):
    ctx = ToolCtx(node)
    saved = await wf_tools.workflow_save(ctx, definition=workflow(action("look", READ)), ask_first=True)
    assert saved["ask_first"] is True and saved["ask_first_by"] == "agent"
    with pytest.raises(PolicyDenied):
        await wf_tools.workflow_save(ctx, definition=workflow(action("look", READ)), ask_first=False)


async def test_an_agent_sees_only_its_workspaces_and_never_a_lists_addresses(node):
    wid = node.wf.save(workflow(action("look", READ)), OWNER)["id"]
    stranger = ToolCtx(node, workspaces=("other",))
    assert (await wf_tools.workflow_list(stranger))["workflows"] == []
    with pytest.raises(NotFound):
        await wf_tools.workflow_get(stranger, id=wid)
    with pytest.raises(NotFound):
        await wf_tools.workflow_run(stranger, id=wid)
    catalog = await wf_tools.workflow_catalog(ToolCtx(node))
    assert {x["size"] for x in catalog["lists"]} == {3} and "example.com" not in json.dumps(catalog["lists"])


async def test_an_agent_proposes_and_a_person_decides(node):
    ctx = ToolCtx(node)
    saved = await wf_tools.workflow_save(ctx, definition=workflow(action("look", READ), trigger={"kind": "webhook"}))
    card = await wf_tools.workflow_propose(ctx, id=saved["id"])
    ap = node.svc.approvals.get(card["approval_id"])
    assert saved["id"] in ap.summary and ap.options == ["Turn it on", "Leave it off"]


def proposed(node, trigger):
    ctx = ToolCtx(node)
    made = node.wf.save(workflow(action("look", READ), trigger=trigger), wf_tools._actor(ctx))
    return ctx, made["id"], node.wf.propose(made["id"], wf_tools._actor(ctx))["approval_id"]


SCHEDULE = {"kind": "schedule", "cron": "0 9 * * *"}


async def test_a_persons_yes_turns_on_exactly_the_version_on_the_card(node):
    ctx, wid, card = proposed(node, SCHEDULE)
    version = node.wf.public(wid)["version"]
    node.wf.save(workflow(action("post", POST, inputs={"caption": "x", "image": "a.jpg"}), trigger=SCHEDULE),
                 wf_tools._actor(ctx), flow_id=wid)
    await node.settle(1)
    assert node.wf.public(wid)["enabled"] is False  # nobody has answered
    node.svc.decide(card, "approve", answer="Turn it on")
    await node.settle(1)
    row = node.wf.public(wid)
    assert row["enabled"] is True and row["live_version"] == version != row["version"] and row["needs_review"] is True


@pytest.mark.parametrize(("by", "via", "on"), [
    ("human:owner", "session", True), ("owner:cli", None, True), ("controller:owner", None, True),
    ("human:owner", "owner_token", False), ("agent:helper", "token", False)])
async def test_only_a_person_answering_the_card_turns_it_on(node, by, via, on):
    _, wid, card = proposed(node, SCHEDULE)
    node.svc.approvals.decide(card, "approve", by=by, via=via, answer="Turn it on")
    await node.settle(1)
    assert node.wf.public(wid)["enabled"] is on  # a bearer token is not a person (docs/WORKFLOWS.md)


async def test_a_family_with_too_many_cards_waiting_gets_no_more(node, monkeypatch):
    ctx, wid, _ = proposed(node, SCHEDULE)
    monkeypatch.setattr(node.svc.cfg, "max_pending_per_grant", 1)
    with pytest.raises(PolicyDenied, match="too_many_pending"):
        node.wf.propose(wid, wf_tools._actor(ctx))


async def test_a_card_asks_under_the_grant_that_proposed_it(node):
    _, _, card = proposed(node, SCHEDULE)
    assert node.svc.approvals.get(card).grant_path


async def test_a_persons_no_leaves_it_off(node):
    _, wid, card = proposed(node, SCHEDULE)
    node.svc.decide(card, "approve", answer="Leave it off")
    await node.settle(1)
    assert node.wf.public(wid)["enabled"] is False and node.wf.store.state(wid).proposal is None


async def test_a_webhook_workflow_is_turned_on_by_the_person_who_gets_its_secret(node):
    _, wid, card = proposed(node, {"kind": "webhook"})
    node.svc.decide(card, "approve", answer="Turn it on")
    await node.settle(1)
    assert node.wf.public(wid)["enabled"] is False
    assert node.wf.enable(wid, OWNER)["secret"]


async def test_anyone_can_stop_a_message_and_only_a_person_can_lift_that(node):
    ctx = ToolCtx(node)
    await wf_tools.send_suppress(ctx, workspace="biz-a", email="ana@example.com")
    assert node.wf.sends.store.is_suppressed("biz-a", "ana@example.com")
    with pytest.raises(PolicyDenied):
        node.wf.sends.unsuppress("biz-a", "ana@example.com", person=False)
    with pytest.raises(NotFound):
        await wf_tools.send_suppress(ToolCtx(node, workspaces=("other",)), workspace="biz-a", email="x@example.com")


async def test_an_agent_takes_back_only_its_own_draft_that_never_ran_or_was_proposed(node):
    """QA 2026-11-15 P1-1: an agent's saved routine could not be removed by anyone."""
    ctx = ToolCtx(node)
    draft = await wf_tools.workflow_save(ctx, definition=workflow(action("look", READ), name="Draft"))
    out = await wf_tools.workflow_discard(ctx, id=draft["id"])
    assert out["discarded"] is True
    assert draft["id"] not in {w["id"] for w in (await wf_tools.workflow_list(ctx))["workflows"]}
    with pytest.raises(NotFound):
        await wf_tools.workflow_get(ctx, id=draft["id"])
    # a person's workflow, a proposed one and one that ran are a person's to delete
    persons = node.wf.save(workflow(action("look", READ), name="Persons"), OWNER)["id"]
    with pytest.raises(PolicyDenied, match="only a person deletes it now: openajar routines workflows remove"):
        await wf_tools.workflow_discard(ctx, id=persons)
    _, asked, _ = proposed(node, SCHEDULE)
    with pytest.raises(PolicyDenied, match="proposed"):
        await wf_tools.workflow_discard(ctx, id=asked)
    ran = await wf_tools.workflow_save(ctx, definition=workflow(action("look", READ), name="Ran"))
    await wf_tools.workflow_run(ctx, id=ran["id"])
    await node.settle(1)
    with pytest.raises(PolicyDenied, match="has runs"):
        await wf_tools.workflow_discard(ctx, id=ran["id"])
    with pytest.raises(NotFound):  # another workspace's: not there, as for workflow_get
        await wf_tools.workflow_discard(ToolCtx(node, workspaces=("other",)), id=ran["id"])


def test_the_tools_are_listed_for_agents(node):
    assert {s.name for s in wf_tools.tool_specs(node.h.runtime)} == {
        "workflow_save", "workflow_list", "workflow_get", "workflow_catalog", "workflow_run", "workflow_runs",
        "workflow_retry", "workflow_propose", "workflow_discard", "send_suppress", "leads_propose"}
    assert all("—" not in s.description for s in wf_tools.tool_specs(node.h.runtime))


# ---- templates ----

def test_the_templates_cover_the_real_jobs(node):
    assert {t["id"] for t in TEMPLATES} == {"order", "invoice", "newsletter", "outreach", "post", "webhook", "new_row", "reply",
                                       "follow_up", "to_list"}
    assert [t["id"] for t in TEMPLATES if t.get("developer")] == ["webhook"]
    assert all(t["sentence"] and t["note"] and "—" not in json.dumps(t) for t in TEMPLATES)


def test_a_template_takes_what_the_workspace_has_and_leaves_the_rest_to_choose(node):
    made = node.wf.from_template("newsletter", "biz-a")
    send = next(s for s in made["steps"] if s["id"] == "send")
    assert send["operation"] == f"mail.send@{MAIL}"
    assert next(s for s in made["steps"] if s["id"] == "write")["operation"] is None
    row = node.wf.save(made, OWNER)
    assert any("Finish" in m["words"] for m in row["missing"]) and row["enabled"] is False


def _with_every_tag(node):
    produces = {"shop.order": [("email", "text"), ("item", "text")], "shop.stock": [("left", "number")],
                "invoices.overdue": [("invoices", "list"), ("invoices_email", "text"), ("invoices_number", "text"), ("invoices_days", "number")],
                "leads.quiet": [("leads", "list"), ("leads_email", "text")], "mail.draft": [("subject", "text"), ("body", "text")],
                "content.plan": [("idea", "text")], "job.run": []}
    for tag, fields in produces.items():
        flow = read_flow(f"biz-a.tools.{tag.replace('.', '-')}", [{"key": k, "type": t, "untrusted": False} for k, t in fields])
        flow["operation"] = {**flow.get("operation", {}), "tags": [tag]}
        flow["params"] = {"properties": {"order_id": {"type": "string"}, "item": {"type": "string"}}}
        node.h.save(flow, by=OWNER_P)
    node.h.save(commit_flow("biz-a.tools.post-publish") | {"operation": {"tags": ["post.publish"]}}, by=OWNER_P)


@pytest.mark.parametrize("template", [t["id"] for t in TEMPLATES if t["id"] != "new_row"])  # that one waits for a Site API
def test_every_template_passes_the_safety_rules_once_filled_in(node, template):
    _with_every_tag(node)
    made = node.wf.from_template(template, "biz-a")

    def fill_list(steps):
        for s in steps:
            if s.get("send") is not None:
                s["send"] = {**s["send"], "list": LIST}
            for arm in ("then", "otherwise", "body"):
                fill_list(s.get(arm) or [])

    fill_list(made["steps"])
    if template == "post":
        next(s for s in made["steps"] if s["id"] == "post")["inputs"]["image"] = "a.jpg"
    row = node.wf.save(made, OWNER)
    assert row["missing"] == []


async def test_a_newsletter_runs_on_its_own_after_a_persons_yes(node):
    _with_every_tag(node)
    made = node.wf.from_template("newsletter", "biz-a")
    next(s for s in made["steps"] if s["id"] == "send")["send"]["list"] = LIST
    from openajar.runner.workflows import engine

    output = engine.Walk._output
    engine.Walk._output = lambda self, child, op: ({"subject": "News", "body": "This week"}, set())
    try:
        wid = node.wf.save(made, OWNER)["id"]
        view = await node.run(wid)
        node.svc.decide(node.step(view, "read")["approval_id"], "approve", answer="Yes")
        await node.settle()
    finally:
        engine.Walk._output = output
    assert len(node.mail.sent) == 3 and node.view(wid, view["id"])["status"] == "succeeded_unverified"


# ---- the node API: who is a person ----

@pytest.fixture
def panel(tmp_path):
    n = Node(tmp_path)
    build_events(n.runtime)
    yield n
    n.close()


def api_workflow():
    return {"name": "A job", "workspace": "biz-a", "trigger": {"kind": "webhook"},
            "steps": [{"id": "n", "kind": "notify", "message": "It ran."}]}


def test_every_workflow_route_needs_credentials(panel):
    client = panel.client()
    for method, path in (("GET", "/api/workflows"), ("POST", "/api/workflows"), ("GET", "/api/workflows/catalog"),
                         ("POST", "/api/workflows/x.y/enable"), ("POST", "/api/workflows/lists")):
        assert client.request(method, path, json={}).status_code == 401, path


def test_a_bearer_token_is_not_a_person_even_the_owners(panel):
    client = panel.client()
    token = {"Authorization": f"Bearer {panel.config.mcp_token}"}
    wid = client.post("/api/workflows", json=api_workflow(), headers=token).json()["id"]
    r = client.post(f"/api/workflows/{wid}/enable", headers=token)
    assert r.status_code == 403 and "person" in r.text
    assert client.post("/api/workflows/lists", json={"workspace": "biz-a", "name": "x", "people": []}, headers=token).status_code == 403
    assert client.put("/api/workflows/limits/biz-a~mail", json={"daily_limit": 5}, headers=token).status_code == 403
    assert client.get(f"/api/workflows/{wid}", headers=token).json()["enabled"] is False


def test_a_signed_in_person_turns_it_on_and_gets_the_secret_once(panel):
    client = panel.client()
    csrf = panel.sign_in(client)
    wid = client.post("/api/workflows", json=api_workflow(), headers=csrf).json()["id"]
    r = client.post(f"/api/workflows/{wid}/enable", headers=csrf)
    assert r.status_code == 200 and len(r.json()["secret"]) == 64
    row = client.get(f"/api/workflows/{wid}", headers=csrf).json()
    assert row["enabled"] is True and "secret" not in row and row["trigger"]["hook"]["token"]


def test_a_broken_rule_comes_back_as_problems_for_a_person(panel):
    client = panel.client()
    csrf = panel.sign_in(client)
    bad = {**api_workflow(), "steps": [{"id": "b", "kind": "branch", "when": {"left": "{{trigger.vip}}", "op": "is", "right": "yes"}}]}
    r = client.post("/api/workflows", json=bad, headers=csrf)
    assert r.status_code == 400
    assert r.json()["error"]["problems"][0]["code"] == "untrusted_decides" and r.json()["error"]["problems"][0]["target"] == "b"


def test_a_grant_cannot_reach_another_workspaces_workflow(panel):
    client = panel.client()
    csrf = panel.sign_in(client)
    wid = client.post("/api/workflows", json={**api_workflow(), "workspace": "biz-b"}, headers=csrf).json()["id"]
    token = bearer(panel.grant(workspaces=["biz-a"]))
    assert client.get(f"/api/workflows/{wid}", headers=token).status_code == 404
    assert client.get("/api/workflows", headers=token).json() == []


# ---- the channel ----

async def full_node(home, name, ctl, lanes):
    """A joined node with the runner and events, as `_start_node` makes one without them."""
    from channel_fake_controller import ControlLanes
    from channel_support import FAST, RunningNode, extra_tools, join_node
    from fakes import FakeInteraction

    from openajar.node.daemon import build_services, node_runtime, serve

    config = await join_node(home, name, ctl)
    manager = ControlLanes(config)
    for lane in lanes:
        await manager.create(LaneSpec(id=lane))
    interaction = FakeInteraction()
    runtime = node_runtime(config, manager, interaction)
    build_services(runtime, ["openajar.policy.service", "openajar.freeze.service", "openajar.runner.service",
                             "openajar.events.service", "openajar.node.channel.service"])
    channel = runtime.get("channel")
    channel.timings = dataclasses.replace(FAST, doctor_after=600.0)
    channel._backoff.low, channel._backoff.high = FAST.backoff_min, FAST.backoff_max
    stop_it = asyncio.Event()
    task = asyncio.create_task(serve(config, runtime, stop=stop_it, providers=[*mcp_providers.tool_providers(), extra_tools]))
    await asyncio.wait_for(channel.welcomed.wait(), 15)
    return RunningNode(name, config, runtime, manager, interaction, stop_it, task)


def sync_for(node):
    meta: dict = {}
    return WorkflowSync(node.wf, get_meta=lambda k, d: meta.get(k, d), set_meta=meta.__setitem__)


def body(action_, **rest):
    return {"ts": wire.iso(), "nonce": secrets.token_hex(8), "action": action_, **rest}


def test_a_controllers_message_is_a_person_and_says_who(node):
    sync = sync_for(node)
    message = body("save", definition=workflow(action("look", READ), trigger={"kind": "webhook"}), by="usr_1")
    out = sync.apply(sync.check_body(message), message)
    assert out["ok"] is True
    wid = out["flow"]["id"]
    assert node.wf.get_version(wid).saved_by["by"] == "controller:usr_1"
    message = body("enable", id=wid)
    out = sync.apply(sync.check_body(message), message)
    assert out["ok"] and out["flow"]["enabled"] is True and len(out["secret"]) == 64


def test_a_seen_nonce_and_a_bad_body_are_refused(node):
    sync = sync_for(node)
    message = body("disable", id="x.y")
    sync.check_body(message)
    with pytest.raises(PolicyDenied):
        sync.check_body(message)
    with pytest.raises(InvalidRequest):
        sync.check_body({"ts": wire.iso(), "nonce": secrets.token_hex(8), "action": "drop_tables"})
    with pytest.raises(InvalidRequest):
        sync.check_body({"nonce": secrets.token_hex(8), "action": "run"})


def test_a_refusal_is_an_answer_with_the_problems(node):
    sync = sync_for(node)
    bad = workflow({"id": "b", "kind": "branch", "when": {"left": "{{trigger.x}}"}}, trigger={"kind": "webhook"})
    message = body("save", definition=bad)
    out = sync.apply(sync.check_body(message), message)
    assert out["ok"] is False and out["error"]["problems"][0]["code"] == "untrusted_decides"


async def test_a_relayed_webhook_is_verified_on_the_node(node):
    wid = node.wf.save(workflow(action("look", READ), trigger={"kind": "webhook"}), OWNER)["id"]
    secret = node.wf.enable(wid, OWNER)["secret"]
    token = node.wf.public(wid)["trigger"]["hook"]["token"]
    ts = str(int(node.h.clock.now().timestamp()))
    raw = b'{"order_id":"A-1"}'

    def relayed(signature, data=raw):
        return {"token": token, "body_b64": base64.b64encode(data).decode(),
                "headers": {"x-agentos-timestamp": ts, "x-agentos-signature": signature}}

    scheduler = node.runner.scheduler
    assert (await relay_hook(scheduler, relayed("sha256=forged-by-the-controller")))["status"] == 401
    assert (await relay_hook(scheduler, relayed(sign_webhook(secret, ts, raw), b"changed on the way")))["status"] == 401
    assert (await relay_hook(scheduler, relayed(sign_webhook(secret, ts, raw))))["status"] == 202
    await node.settle()
    assert len(node.wf.runs(wid)) == 1


async def test_a_relayed_body_that_is_too_big_or_not_base64_is_refused(node):
    scheduler = node.runner.scheduler
    assert (await relay_hook(scheduler, {"token": "x", "body_b64": "!!", "headers": {}}))["status"] == 400
    big = base64.b64encode(b"x" * (512 * 1024 + 1)).decode()
    assert (await relay_hook(scheduler, {"token": "x", "body_b64": big, "headers": {}}))["status"] == 413


def test_a_relayed_unsubscribe_that_means_nothing_does_nothing(node):
    assert relay_unsubscribe(node.wf.sends, {"token": "nothing-like-a-real-token-at-all"}) == {"ok": False}
    assert relay_unsubscribe(node.wf.sends, {"token": 5}) == {"ok": False}


async def test_what_arrived_stays_out_of_the_generic_run_view(node):
    wid = node.wf.save(workflow(action("look", READ), trigger={"kind": "webhook"}), OWNER)["id"]
    view = await node.run(wid, values={"note": "private words from an email"})
    described = node.runner.describe(node.runner.get(view["id"]))
    assert "private words" not in json.dumps(described, default=str)


async def test_a_vault_value_is_scrubbed_at_any_depth(node):
    node.h.flows.vault_scrub = lambda text: text.replace("hunter2", "[hidden]")
    wid = node.wf.save(workflow({"id": "n", "kind": "notify", "message": "{{trigger.deep}}"}), OWNER)["id"]
    view = await node.run(wid, values={"deep": {"a": ["the password is hunter2"]}})
    assert "hunter2" not in json.dumps(view, default=str)


async def test_the_inventory_lists_workflows_as_flows_and_the_catalog_holds_no_address(node):
    node.wf.save(workflow(action("look", READ)), OWNER)
    rows = (await reports.inventory(node.h.runtime))["flows"]
    assert {r["id"] for r in rows} >= {"biz-a.routine.a-job", READ}
    assert "example.com" not in json.dumps(reports.workflows_catalog(node.h.runtime))


async def test_a_real_controller_relays_a_webhook_and_a_persons_action(tmp_path):
    ctl = await start_controller(tmp_path / "controller")
    n = await full_node(tmp_path / "node", "node-a", ctl, ["biz-a/social"])
    hub = ctl.controller.hub
    node_id = n.config.node_id
    try:
        await wait_for(lambda: hub.sessions)
        assert "workflows" in n.channel._supports()
        made = await hub.push_workflow(ctl.org, node_id, {
            "action": "save", "by": "usr_owner",
            "definition": {"name": "Hook", "workspace": "biz-a", "trigger": {"kind": "webhook"},
                           "steps": [{"id": "n", "kind": "notify", "message": "ran"}]}})
        assert made["ok"], made
        wid = made["flow"]["id"]
        on = await hub.push_workflow(ctl.org, node_id, {"action": "enable", "id": wid, "by": "usr_owner"})
        assert on["ok"] and on["secret"]
        token = on["flow"]["trigger"]["hook"]["token"]
        ts = str(int(time.time()))
        raw = b'{"hello":"there"}'
        headers = {"x-agentos-timestamp": ts, "x-agentos-signature": sign_webhook(on["secret"], ts, raw),
                   "cookie": "kept out", "authorization": "kept out"}
        status, reply = await hub.relay_hook(node_id, token, raw, headers)
        assert status == 202 and reply["duplicate"] is False
        assert (await hub.relay_hook(node_id, token, raw, headers))[1]["duplicate"] is True
        assert (await hub.relay_hook(node_id, token, b"forged", headers))[0] == 401
        assert (await hub.relay_hook("no-such-node", token, raw, headers))[0] == 503
        await hub.relay_unsubscribe(node_id, "nothing-like-a-real-token-at-all")
    finally:
        await n.close()
        await ctl.close()


async def test_the_controllers_public_routes_keep_nothing_and_a_mail_scanner_unsubscribes_nobody(tmp_path):
    ctl = await start_controller(tmp_path / "controller")
    n = await full_node(tmp_path / "node", "node-a", ctl, ["biz-a/social"])
    hub = ctl.controller.hub
    node_id = n.config.node_id
    wf = n.runtime.get("workflows")
    try:
        await wait_for(lambda: hub.sessions)
        assert wf.sends.public_base() == f"{ctl.url}/u/{node_id}"  # what a message's link points at
        link = wf.sends.unsubscribe_for("biz-a", "ana@example.com", mailbox=None)
        wid = wf.save(workflow({"id": "n", "kind": "notify", "message": "ran"}, trigger={"kind": "webhook"}), OWNER)["id"]
        secret = wf.enable(wid, OWNER)["secret"]
        token = wf.public(wid)["trigger"]["hook"]["token"]
        ts = str(int(time.time()))
        raw = b'{"hello":"there"}'
        async with httpx.AsyncClient(base_url=ctl.url) as web:
            scanned = await web.get(f"/u/{node_id}/{link.token}")
            assert scanned.status_code == 200 and "<form" in scanned.text
            assert not wf.sends.store.is_suppressed("biz-a", "ana@example.com")
            clicked = await web.post(f"/u/{node_id}/{link.token}")
            assert "not get these messages" in clicked.text
            await wait_for(lambda: wf.sends.store.is_suppressed("biz-a", "ana@example.com"))
            nothing = await web.post(f"/u/{node_id}/not-a-token-this-node-ever-made")
            assert nothing.text == clicked.text  # the page says nothing about what a token means
            headers = {"X-Agentos-Timestamp": ts, "X-Agentos-Signature": sign_webhook(secret, ts, raw)}
            assert (await web.post(f"/hooks/{node_id}/{token}", content=raw, headers=headers)).status_code == 202
            assert (await web.post(f"/hooks/{node_id}/{token}", content=b"forged", headers=headers)).status_code == 401
            big = await web.post(f"/hooks/{node_id}/{token}", content=b"x" * (512 * 1024 + 1), headers=headers)
            assert big.status_code == 413
    finally:
        await n.close()
        await ctl.close()


async def test_a_node_without_workflows_answers_unsupported(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        await start(runtime)
        peer = await fake.peer()
        try:
            sent = await peer.send("workflow", fake.sign("workflow", body("disable", id="x.y")))
            reply = await peer.reply_to(sent.id)
            assert reply.type == "error" and reply.body["code"] == "unsupported"
        finally:
            await stop(runtime)


async def test_a_workflow_message_with_a_bad_signature_is_refused(tmp_path):
    ctl = await start_controller(tmp_path / "controller")
    n = await full_node(tmp_path / "node", "node-a", ctl, ["biz-a/social"])
    hub = ctl.controller.hub
    try:
        await wait_for(lambda: hub.sessions)
        session = hub.sessions[n.config.node_id]
        forged = {**body("delete", id="biz-a.routine.x"), "sig": "AAAA"}
        reply = await hub._request(session, wire.Envelope("workflow", forged))
        assert reply.type == "error" and reply.body["code"] == "denied"
    finally:
        await n.close()
        await ctl.close()


# ---- files ----

def test_a_workflow_file_applies_and_comes_back_the_same(tmp_path, monkeypatch):
    from openajar.runner.workflows import cli

    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    definition = {"name": "From a file", "workspace": "biz-a", "trigger": {"kind": "webhook"},
                  "steps": [{"id": "n", "kind": "notify", "label": "tell you", "message": "It ran."}]}
    path = tmp_path / "job.yaml"
    path.write_text(yaml.safe_dump(definition))
    runner = CliRunner()
    first = runner.invoke(cli.app, ["apply", str(path)])
    assert first.exit_code == 0, first.output
    again = runner.invoke(cli.app, ["apply", str(path)])
    assert again.output == first.output  # the same file changes nothing
    out = runner.invoke(cli.app, ["export", "biz-a.routine.from-a-file"])
    exported = json.loads(out.output)
    assert exported["steps"] == definition["steps"] and exported["trigger"] == definition["trigger"]
    on = runner.invoke(cli.app, ["enable", "biz-a.routine.from-a-file"])
    assert on.exit_code == 0 and "webhook secret, shown once:" in on.output


def test_a_person_removes_a_workflow_with_either_remove(tmp_path, monkeypatch):
    """QA 2026-11-15 P1-1: `routines workflows remove`, and `flows remove` with a workflow id."""
    from openajar.runner import cli as runner_cli
    from openajar.runner.workflows import cli

    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    runner = CliRunner()
    for name in ("One", "Two"):
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump({"name": name, "workspace": "biz-a", "trigger": {"kind": "webhook"},
                                        "steps": [{"id": "n", "kind": "notify", "label": "tell", "message": "x"}]}))
        assert runner.invoke(cli.app, ["apply", str(path)]).exit_code == 0
    out = runner.invoke(cli.app, ["remove", "biz-a.routine.one"])
    assert out.exit_code == 0 and "removed biz-a.routine.one" in out.output
    out = runner.invoke(runner_cli.flows_app, ["remove", "biz-a.routine.two"])
    assert out.exit_code == 0 and "removed workflow biz-a.routine.two" in out.output
    listed = runner.invoke(cli.app, ["list", "--json"])
    assert json.loads(listed.output) == []
    gone = runner.invoke(runner_cli.flows_app, ["remove", "biz-a.routine.two"])
    assert gone.exit_code == 1 and "no flow or workflow" in gone.output


def test_an_error_in_a_file_says_which_step(tmp_path, monkeypatch):
    from openajar.runner.workflows import cli

    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"name": "Bad", "workspace": "biz-a", "trigger": {"kind": "webhook"},
                                "steps": [{"id": "b", "kind": "branch", "when": {"left": "{{trigger.x}}"}}]}))
    out = CliRunner().invoke(cli.app, ["apply", str(path)])
    assert out.exit_code == 1 and "b: What it checks cannot use" in out.output


