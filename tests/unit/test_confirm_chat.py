"""approval_confirm through real MCP clients over this node's streamable HTTP: one that handles
elicitation, one that does not, and one on the 2026-07-28 protocol. The caller is the agent, so these
are mostly what it must not be able to do (docs/CONFIRMATIONS.md, Chat)."""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx2
import pytest
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ElicitResult, Implementation
from test_node_panel import TOKEN, Node, bearer
from test_policy_support import Clock, make_service

from agentos.core.config import NodeConfig
from agentos.core.errors import ApprovalPending, NotFound
from agentos.core.models import Effect
from agentos.core.tools import ToolSpec
from agentos.node import daemon
from agentos.policy import confirm_chat, wrap_tool
from agentos.policy.approval_models import ApprovalPolicy, FourEyesRule
from agentos.policy.confirm import ConfirmLevel, Proof
from agentos.policy.confirm_chat import prompt_text, quoted, yes_value
from agentos.policy.errors import Denied, MethodUnavailable
from agentos.policy.routing import PolicyConfig
from agentos.policy.tools import approval_confirm
from agentos.policy.wrap import bind_request_token
from agentos.runner.models import answered_in_person

LANE = "biz-a/instagram"
CONTENT = "Spring menu is out"


@pytest.fixture(autouse=True)
def a_person_reads_at_any_speed(monkeypatch):
    monkeypatch.setattr(confirm_chat, "MIN_ANSWER_S", 0.0)
    monkeypatch.setattr(confirm_chat, "COOLDOWN_S", 0.0)


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.policy.ask_first.set("lane", LANE, True, by="owner", person=True)
    yield n
    n.close()


@pytest.fixture
async def url(node):
    config = NodeConfig(home=node.config.home, mcp_token=TOKEN, mcp_port=0)
    server = daemon._Uvicorn.server(node.app, config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    yield f"http://localhost:{daemon.bound_port(server)}/mcp"
    server.should_exit = True
    await task


@contextlib.asynccontextmanager
async def agent(url, token, *, on_prompt=None, mode="legacy", name="test-chat"):
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http,
        Client(streamable_http_client(url, http_client=http), mode=mode, elicitation_callback=on_prompt,
               client_info=Implementation(name=name, version="1.2")) as client,
    ):
        yield client


class Person:
    """A person at a chat client: answers each prompt as told, and remembers what they saw."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen: list = []

    async def __call__(self, context, params):
        self.seen.append(params)
        props = params.requested_schema["properties"]["answer"]
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer(props)
        return answer if isinstance(answer, ElicitResult) else ElicitResult(action="accept", content={"answer": answer})


def yes(props):
    return props["enum"][0]


NO = "no"
DISMISS = ElicitResult(action="cancel")
DECLINE = ElicitResult(action="decline")


def issue(node, *, subject="writer", **kw):
    kw.setdefault("workspaces", ["biz-a"])
    kw.setdefault("ceiling", "pay")
    return node.policy.issue(subject={"id": subject, **kw.pop("sub", {})}, **kw)


async def asked(node, grant, *, effect=Effect.COMMIT, content=CONTENT, summary="Post a thing"):
    ctx = node.policy.context(grant, tool="lane_post")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(effect, lane_id=LANE, content=content, summary=summary)
    return e.value.approval_id


def body(result) -> dict:
    return json.loads(result.content[0].text)


async def confirm(client, ap):
    return body(await client.call_tool("approval_confirm", {"approval_id": ap}))


# ---- a person says yes or no ----

async def test_a_yes_from_the_client_approves_and_is_recorded_as_chat_not_as_the_agent(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    person = Person(yes)
    async with agent(url, token, on_prompt=person) as client:
        out = await confirm(client, ap)
    assert out["status"] == "approved" and "again" in out["next"]
    a = node.policy.approvals.get(ap)
    assert (a.state, a.decided_method, a.decided_level, a.decided_via) == ("approved", "chat", "chat", "chat")
    assert a.decided_by == "chat:owner" and a.decided_detail == {"client": "test-chat 1.2"}
    assert not answered_in_person(a)  # a person's answer through a client, never "in person"
    assert not answered_in_person(a, relayed=True)  # nor a controller relaying one


async def test_activity_shows_a_clients_name_as_what_it_said_about_itself(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    async with agent(url, token, on_prompt=Person(yes)) as client:
        await confirm(client, ap)
    node.policy.traces.flush()
    events, _ = node.policy.traces.query(kind="approval_confirm", limit=5)
    assert any("calling itself" in (e.summary or "") for e in events), [e.summary for e in events]


async def test_the_retried_call_runs_after_a_chat_yes(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    async with agent(url, token, on_prompt=Person(yes)) as client:
        await confirm(client, ap)
    decision = await node.policy.context(g, tool="lane_post").authorize(Effect.COMMIT, lane_id=LANE, content=CONTENT,
                                                                        summary="Post a thing")
    assert decision.route == "approved"


@pytest.mark.parametrize("answer", [NO, DECLINE])
async def test_a_no_denies_and_denying_from_chat_is_always_allowed(node, url, answer):
    g, token = issue(node)
    ap = await asked(node, g)
    async with agent(url, token, on_prompt=Person(answer)) as client:
        out = await confirm(client, ap)
    a = node.policy.approvals.get(ap)
    assert out["status"] == "denied" and (a.state, a.decided_method) == ("denied", "chat")


async def test_a_dismissed_prompt_decides_nothing(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    async with agent(url, token, on_prompt=Person(DISMISS)) as client:
        out = await confirm(client, ap)
    assert out["status"] == "no_answer" and node.policy.approvals.get(ap).state == "pending"


async def test_a_client_that_said_it_could_and_then_fails_gets_the_link(node, url):
    from mcp.shared.exceptions import MCPError
    from mcp.types import ErrorData

    async def broken(context, params):
        raise MCPError(ErrorData(code=-1, message="no screen"))

    g, token = issue(node)
    ap = await asked(node, g)
    async with agent(url, token, on_prompt=broken) as client:
        out = await confirm(client, ap)
    assert out["status"] == "review" and node.policy.approvals.get(ap).state == "pending"


async def test_a_prompt_nobody_answers_ends_with_the_link(node, url, monkeypatch):
    monkeypatch.setattr(confirm_chat, "PROMPT_TTL_S", 0.2)
    g, token = issue(node)
    ap = await asked(node, g)

    async def away(context, params):
        await asyncio.sleep(5)
        return ElicitResult(action="cancel")

    async with agent(url, token, on_prompt=away) as client:
        out = await confirm(client, ap)
    assert out["status"] == "review" and node.policy.approvals.get(ap).state == "pending"


async def test_a_client_on_the_current_protocol_answers_in_a_second_round(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    person = Person(yes)
    async with agent(url, token, on_prompt=person, mode="2026-07-28") as client:
        out = await confirm(client, ap)
    assert out["status"] == "approved" and len(person.seen) == 1
    assert node.policy.approvals.get(ap).decided_method == "chat"


async def test_the_owner_token_as_a_client_still_needs_the_person(node, url):
    """An agent on the node token: the family is the owner's, and the person still answers."""
    g = node.policy.owner_grant()
    ap = await asked(node, g)
    async with agent(url, TOKEN, on_prompt=Person(yes)) as client:
        assert (await confirm(client, ap))["status"] == "approved"
    assert node.policy.approvals.get(ap).decided_by == "chat:owner"


# ---- a client that cannot show a prompt, or an approval that needs more than chat ----

async def test_a_client_without_elicitation_gets_a_review_link_and_no_decision(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    async with agent(url, token) as client:
        out = await confirm(client, ap)
    assert out["status"] == "review" and out["requires"] == "chat" and ap in out["review_url"]
    assert "approves nothing" in out["next"]
    assert node.policy.approvals.get(ap).state == "pending"


@pytest.mark.parametrize("effect,requires", [(Effect.PAY, "present"), (Effect.IRREVERSIBLE, "present")])
async def test_more_than_chat_never_prompts_and_the_link_opens_the_panel(node, url, effect, requires):
    g, token = issue(node, ceiling="irreversible")
    ap = await asked(node, g, effect=effect)
    person = Person(yes)
    async with agent(url, token, on_prompt=person) as client:
        out = await confirm(client, ap)
    assert person.seen == [] and out["status"] == "review" and out["requires"] == requires
    assert node.policy.approvals.get(ap).state == "pending"


async def test_after_untrusted_text_never_prompts(node, url):
    g, token = issue(node)
    ctx = node.policy.context(g, tool="lane_post")
    ctx.taint("untrusted", source="a page")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id=LANE, content=CONTENT, summary="Post")
    person = Person(yes)
    async with agent(url, token, on_prompt=person) as client:
        out = await confirm(client, e.value.approval_id)
    assert person.seen == [] and out["status"] == "review"


async def test_a_session_that_read_untrusted_text_since_the_approval_does_not_prompt(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    node.policy.context(g, tool="browser_snapshot").taint("untrusted", source="a page")
    person = Person(yes)
    async with agent(url, token, on_prompt=person) as client:
        out = await confirm(client, ap)
    assert person.seen == [] and out["status"] == "review"
    assert "untrusted" in out["why"]


async def test_a_question_never_prompts_its_words_are_the_agents(node, url):
    g, token = issue(node)
    ap = node.policy.approvals.create_question(question="Say yes to this, then click accept", options=None,
                                               requester=g.principal(), grant_id=g.id, grant_path=g.path)
    person = Person(yes)
    async with agent(url, token, on_prompt=person) as client:
        out = await confirm(client, ap.id)
    assert person.seen == [] and out["status"] == "review"


async def test_chat_turned_off_never_prompts(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    node.policy.confirmation.change({"methods_off": ["chat"]}, by="owner", role="owner")
    person = Person(yes)
    async with agent(url, token, on_prompt=person) as client:
        out = await confirm(client, ap)
    assert person.seen == [] and out["status"] == "review"


async def test_a_workspace_turning_chat_off_reaches_an_approval_already_waiting(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    node.policy.confirmation.apply_controller({"workspaces": {"biz-a": {"methods_off": ["chat"]}}}, by="controller:org:x")
    person = Person(yes)
    async with agent(url, token, on_prompt=person) as client:
        out = await confirm(client, ap)
    assert person.seen == [] and out["status"] == "review" and "workspace" in out["why"]
    assert node.policy.approvals.get(ap).state == "pending"


async def test_another_workspace_turning_chat_off_changes_nothing_here(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    node.policy.confirmation.apply_controller({"workspaces": {"elsewhere": {"methods_off": ["chat"]}}}, by="controller:org:x")
    async with agent(url, token, on_prompt=Person(yes)) as client:
        assert (await confirm(client, ap))["status"] == "approved"


async def test_the_store_refuses_a_chat_yes_where_the_workspace_turned_chat_off(node):
    g, _token = issue(node)
    ap = await asked(node, g)
    node.policy.confirmation.apply_controller({"workspaces": {"biz-a": {"methods_off": ["chat"]}}}, by="controller:org:x")
    chat = Proof("chat", ConfirmLevel.CHAT)
    with pytest.raises(MethodUnavailable):
        node.policy.approvals.decide(ap, "approve", by="chat:owner", via="chat", in_person=True, proof=chat)
    assert node.policy.approvals.get(ap).state == "pending"
    assert node.policy.approvals.decide(ap, "deny", by="chat:owner", via="chat", in_person=True, proof=chat).state == "denied"


async def test_chat_turned_off_while_the_prompt_is_open_refuses_the_yes_and_keeps_a_no(node, url):
    g, token = issue(node)
    first, second = await asked(node, g, content="one"), await asked(node, g, content="two")

    def off(props):
        node.policy.confirmation.change({"methods_off": ["chat"]}, by="owner", role="owner")
        return yes(props)

    async with agent(url, token, on_prompt=Person(off)) as client:
        assert (await confirm(client, first))["status"] == "review"
    assert node.policy.approvals.get(first).state == "pending"
    node.policy.confirmation.change({"methods_off": []}, by="owner", role="owner")

    def off_no(props):
        node.policy.confirmation.change({"methods_off": ["chat"]}, by="owner", role="owner")
        return NO

    async with agent(url, token, on_prompt=Person(off_no)) as client:
        assert (await confirm(client, second))["status"] == "denied"


async def test_a_long_content_is_not_said_yes_to_unseen(node, url):
    g, token = issue(node)
    ap = await asked(node, g, content="x" * 3000)
    person = Person(yes)
    async with agent(url, token, on_prompt=person) as client:
        assert (await confirm(client, ap))["status"] == "review"
    assert person.seen == []


# ---- another family ----

async def test_another_familys_approval_is_not_found(node, url):
    theirs, _ = issue(node, subject="theirs")
    _, token = issue(node, subject="mine")
    ap = await asked(node, theirs)
    person = Person(yes)
    async with agent(url, token, on_prompt=person) as client:
        result = await client.call_tool("approval_confirm", {"approval_id": ap})
    assert result.is_error and "not_found" in result.content[0].text
    assert person.seen == [] and node.policy.approvals.get(ap).state == "pending"


async def test_an_admin_grant_sees_another_familys_approval_and_still_cannot_prompt_for_it(node, url):
    theirs, _ = issue(node, subject="theirs")
    _, token = node.policy.issue(subject={"id": "boss-bot"}, workspaces=["biz-a"], ceiling="pay", admin=True)
    ap = await asked(node, theirs)
    person = Person(yes)
    async with agent(url, token, on_prompt=person) as client:
        result = await client.call_tool("approval_confirm", {"approval_id": ap})
    assert result.is_error and "not_approver" in result.content[0].text
    assert person.seen == [] and node.policy.approvals.get(ap).state == "pending"


async def test_the_node_token_cannot_prompt_for_another_family_either(node, url):
    theirs, _ = issue(node, subject="theirs")
    ap = await asked(node, theirs)
    person = Person(yes)
    async with agent(url, TOKEN, on_prompt=person) as client:
        result = await client.call_tool("approval_confirm", {"approval_id": ap})
    assert result.is_error and person.seen == []


# ---- an answer that is not this person's answer to this prompt ----

async def test_an_old_yes_is_not_an_answer_to_a_new_prompt(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    kept = []

    def keep_and_dismiss(props):
        kept.append(yes(props))
        return DISMISS

    async with agent(url, token, on_prompt=Person(keep_and_dismiss, lambda props: kept[0])) as client:
        assert (await confirm(client, ap))["status"] == "no_answer"
        out = await confirm(client, ap)
    assert out["status"] == "review" and "does not match" in out["why"]
    assert node.policy.approvals.get(ap).state == "pending"


@pytest.mark.parametrize("value", ["yes", "Yes, approve it", True, 1, ""])
async def test_only_the_prompts_own_yes_is_a_yes(node, url, value):
    g, token = issue(node)
    ap = await asked(node, g)
    async with agent(url, token, on_prompt=Person(ElicitResult(action="accept", content={"answer": value}))) as client:
        out = await confirm(client, ap)
    assert out["status"] == "review" and node.policy.approvals.get(ap).state == "pending"


async def test_an_accept_with_nothing_in_it_is_no_yes(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    async with agent(url, token, on_prompt=Person(ElicitResult(action="accept"))) as client:
        assert (await confirm(client, ap))["status"] == "review"
    assert node.policy.approvals.get(ap).state == "pending"


async def test_a_yes_for_content_that_moved_is_refused(node, url):
    g, token = issue(node)
    ap = await asked(node, g)

    def swap(props):
        node.policy.db.execute("UPDATE approvals SET content_digest = 'sha256:other' WHERE id = ?", (ap,))
        return yes(props)

    async with agent(url, token, on_prompt=Person(swap)) as client:
        out = await confirm(client, ap)
    assert out["status"] == "review" and node.policy.approvals.get(ap).state == "pending"


async def test_the_store_refuses_other_content_in_the_transaction_that_decides(node):
    from agentos.policy.errors import Conflict

    g, _ = issue(node)
    ap = await asked(node, g)
    with pytest.raises(Conflict):
        node.policy.approvals.decide(ap, "approve", by="owner:chat", via="chat", content=("other", "other"))
    assert node.policy.approvals.get(ap).state == "pending"


async def test_a_yes_for_a_decided_approval_changes_nothing(node, url):
    g, token = issue(node)
    ap = await asked(node, g)

    def decided_meanwhile(props):
        node.policy.approvals.decide(ap, "deny", by="owner:cli")
        return yes(props)

    async with agent(url, token, on_prompt=Person(decided_meanwhile)) as client:
        assert (await confirm(client, ap))["status"] == "denied"
    assert node.policy.approvals.get(ap).decided_method == "cli"


async def test_a_grant_revoked_while_the_prompt_is_open_counts_for_nothing(node):
    """Over HTTP the client's answer then gets a 401 and never arrives. Here the answer does arrive."""
    g, _ = issue(node)
    ap = await asked(node, g)
    a = node.policy.approvals.get(ap)
    ctx = node.policy.context(g, tool="approval_confirm")
    answer = confirm_chat.ClientAnswer(json.dumps({"approval": ap, "prompt": "p1", "at": 0.0}), "accept",
                                       {"answer": yes_value(node.policy.approvals.link_key_for("chat"), a, "p1")})
    node.policy.revoke(g.id, by="owner")
    with pytest.raises(Denied) as e:
        node.policy.chat._settle(ctx, ap, node.policy.owner_grant(), answer, "c")
    assert e.value.reason == "revoked" and node.policy.approvals.get(ap).state == "pending"


async def test_a_yes_faster_than_a_person_reads_is_not_counted(node, url, monkeypatch):
    monkeypatch.setattr(confirm_chat, "MIN_ANSWER_S", 60.0)
    g, token = issue(node)
    ap = await asked(node, g)
    async with agent(url, token, on_prompt=Person(yes)) as client:
        out = await confirm(client, ap)
    assert out["status"] == "review" and "faster" in out["why"]
    assert node.policy.approvals.get(ap).state == "pending"


async def test_the_same_answer_sent_again_after_it_was_refused_is_not_counted(node, url, monkeypatch):
    """On a second-round client the state comes back as it went out, so a prompt is spent once."""
    monkeypatch.setattr(confirm_chat, "MIN_ANSWER_S", 60.0)
    g, _ = issue(node)
    ap = await asked(node, g)
    chat = node.policy.chat
    ctx = node.policy.context(g, tool="approval_confirm")
    a = node.policy.approvals.get(ap)
    answer = confirm_chat.ClientAnswer(
        json.dumps({"approval": ap, "prompt": "p1", "at": node.policy.approvals.clock()}), "accept",
        {"answer": yes_value(node.policy.approvals.link_key_for("chat"), a, "p1")})
    assert "faster" in chat._settle(ctx, ap, node.policy.owner_grant(), answer, "c")["why"]
    monkeypatch.setattr(confirm_chat, "MIN_ANSWER_S", 0.0)
    assert "already answered" in chat._settle(ctx, ap, node.policy.owner_grant(), answer, "c")["why"]
    assert node.policy.approvals.get(ap).state == "pending"


async def test_an_old_prompt_has_gone_stale(node):
    g, _ = issue(node)
    ap = await asked(node, g)
    a = node.policy.approvals.get(ap)
    stale = node.policy.approvals.clock() - confirm_chat.PROMPT_TTL_S - 1
    answer = confirm_chat.ClientAnswer(json.dumps({"approval": ap, "prompt": "p1", "at": stale}), "accept",
                                       {"answer": yes_value(node.policy.approvals.link_key_for("chat"), a, "p1")})
    out = node.policy.chat._settle(node.policy.context(g, tool="x"), ap, node.policy.owner_grant(), answer, "c")
    assert out["status"] == "review"


def test_a_yes_is_bound_to_the_prompt_the_content_and_the_key():
    from types import SimpleNamespace

    ap = SimpleNamespace(id="ap_1", content_hash="h", content_digest="d")
    base = yes_value(b"k", ap, "p")
    assert base != yes_value(b"k", ap, "q")
    assert base != yes_value(b"other", ap, "p")
    assert base != yes_value(b"k", SimpleNamespace(id="ap_1", content_hash="h", content_digest="e"), "p")
    assert base != yes_value(b"k", SimpleNamespace(id="ap_1", content_hash="i", content_digest="d"), "p")
    assert base != yes_value(b"k", SimpleNamespace(id="ap_2", content_hash="h", content_digest="d"), "p")


# ---- it cannot nag a person into a yes ----

async def test_a_dismissed_prompt_is_not_asked_again_at_once(node, url, monkeypatch):
    monkeypatch.setattr(confirm_chat, "COOLDOWN_S", 600.0)
    g, token = issue(node)
    ap = await asked(node, g)
    person = Person(DISMISS, yes)
    async with agent(url, token, on_prompt=person) as client:
        assert (await confirm(client, ap))["status"] == "no_answer"
        out = await confirm(client, ap)
    assert out["status"] == "review" and len(person.seen) == 1


async def test_after_enough_prompts_it_goes_to_the_panel(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    person = Person(*[DISMISS] * 10)
    async with agent(url, token, on_prompt=person) as client:
        for _ in range(confirm_chat.MAX_PROMPTS):
            assert (await confirm(client, ap))["status"] == "no_answer"
        assert (await confirm(client, ap))["status"] == "review"
    assert len(person.seen) == confirm_chat.MAX_PROMPTS


async def test_varied_content_does_not_get_past_the_limit_for_a_family(node, url):
    """Three prompts per approval would be three for every new wording, up to max_pending_per_grant."""
    limit = 10
    g, token = issue(node)
    aps = [await asked(node, g, content=f"post number {i}") for i in range(limit + 1)]
    person = Person(*[DISMISS] * 20)
    async with agent(url, token, on_prompt=person) as client:
        for ap in aps[:limit]:
            assert (await confirm(client, ap))["status"] == "no_answer"
        out = await confirm(client, aps[limit])
    assert out["status"] == "review" and out["review_url"]
    assert len(person.seen) == limit


async def test_a_familys_limit_ends_with_the_hour_and_binds_no_other_family(node, url):
    limit = 10
    g, token = issue(node)
    other, other_token = issue(node, subject="another")
    aps = [await asked(node, g, content=f"post number {i}") for i in range(limit + 1)]
    theirs = await asked(node, other, content="their post")
    person = Person(*[DISMISS] * 20)
    async with agent(url, token, on_prompt=person) as client:
        for ap in aps[:limit]:
            await confirm(client, ap)
        assert (await confirm(client, aps[limit]))["status"] == "review"
    async with agent(url, other_token, on_prompt=person) as client:
        assert (await confirm(client, theirs))["status"] == "no_answer"
    now = node.policy.approvals.clock()
    node.policy.approvals.clock = lambda: now + 3601
    async with agent(url, token, on_prompt=person) as client:
        assert (await confirm(client, aps[limit]))["status"] == "no_answer"


async def test_one_prompt_open_at_a_time(node, url):
    g, token = issue(node)
    ap = await asked(node, g)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def slow(context, params):
        entered.set()
        await release.wait()
        return ElicitResult(action="cancel")

    async with agent(url, token, on_prompt=slow) as first, agent(url, token, on_prompt=Person(yes)) as second:
        waiting = asyncio.create_task(confirm(first, ap))
        await entered.wait()
        out = await confirm(second, ap)
        release.set()
        await waiting
    assert out["status"] == "review" and "already open" in out["why"]


async def test_a_workflow_or_a_site_api_is_not_turned_on_by_a_chat_yes(node):
    """What only a person turns on reads `answered_in_person`, and a chat answer is not that."""
    g, _ = issue(node)
    ap = await asked(node, g)
    a = node.policy.approvals.get(ap)
    decider = node.policy.chat.person(a)
    done = node.policy.chat._decide(node.policy.context(g, tool="approval_confirm"), a, decider, "approve", "c")
    assert done["status"] == "approved"
    assert not answered_in_person(node.policy.approvals.get(ap), relayed=True)


# ---- four-eyes, and whose yes this is ----

async def test_four_eyes_keeps_the_person_behind_the_agent_from_a_chat_yes(tmp_path):
    cfg = PolicyConfig(approvals=ApprovalPolicy(four_eyes=[FourEyesRule(workspace="biz-*", effects=["commit"])]))
    svc, _ = make_service(tmp_path, clock=Clock(), config=cfg)
    svc.ask_first.set("lane", LANE, True, by="owner", person=True)
    g, _ = svc.issue(subject={"id": "writer"}, workspaces=["biz-a"], ceiling="commit")
    with pytest.raises(ApprovalPending) as e:
        await svc.context(g, tool="lane_post").authorize(Effect.COMMIT, lane_id=LANE, content=CONTENT, summary="Post")
    assert svc.chat.person(svc.approvals.get(e.value.approval_id)) is None
    svc.close()


async def test_a_members_agent_is_answered_by_that_member_within_their_grant(node, url):
    node.policy.issue_member_grant("m_va", "biz-a", "approver", {})
    g, token = issue(node, owner="m_va")
    ap = await asked(node, g)
    async with agent(url, token, on_prompt=Person(yes)) as client:
        assert (await confirm(client, ap))["status"] == "approved"
    a = node.policy.approvals.get(ap)
    assert a.decided_by == "chat:m_va" and a.decided_method == "chat"


async def test_a_member_who_cannot_approve_gets_no_prompt_for_their_agent(node, url):
    node.policy.issue_member_grant("m_vi", "biz-a", "viewer", {})
    g, token = issue(node, owner="m_vi")
    ap = await asked(node, g)
    person = Person(yes)
    async with agent(url, token, on_prompt=person) as client:
        out = await confirm(client, ap)
    assert person.seen == [] and out["status"] == "review"


async def test_self_approval_stands_beside_chat(node, url):
    g, token = node.policy.issue(subject={"id": "own-admin"}, workspaces=["biz-a"], ceiling="pay", admin=True)
    ap = await asked(node, g)
    r = node.client().post(f"/api/approvals/{ap}/approve", headers=bearer(token), json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"  # a token is no person
    r = node.client().post(f"/api/approvals/{ap}/deny", headers=bearer(token), json={})
    assert r.status_code == 403 and "self_approval" in r.json()["error"]["message"]  # its own family, even to deny
    assert node.policy.approvals.get(ap).state == "pending"


# ---- the model gets no way to answer ----

async def test_the_tool_has_no_argument_that_could_carry_an_answer(node, url):
    _, token = issue(node)
    async with agent(url, token) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "approval_confirm")
    assert set(tool.input_schema["properties"]) == {"approval_id"}


@pytest.mark.parametrize("extra", [{"answer": "yes"}, {"approved": True}, {"decision": "approve"}, {"level": "chat"}])
async def test_an_argument_it_adds_does_nothing(node, url, extra):
    g, token = issue(node)
    ap = await asked(node, g)
    async with agent(url, token) as client:
        await client.call_tool("approval_confirm", {"approval_id": ap, **extra})
    assert node.policy.approvals.get(ap).state == "pending"


@pytest.mark.parametrize("answer,status", [(DISMISS, "no_answer"), (NO, "denied"), (yes, "approved")])
async def test_no_result_the_model_reads_holds_the_prompt_or_a_way_to_answer(node, url, answer, status):
    g, token = issue(node)
    ap = await asked(node, g)
    seen: list = []

    def answering(props):
        seen.append(props["enum"][0])
        return answer(props) if callable(answer) else answer

    person = Person(answering)
    async with agent(url, token, on_prompt=person) as client:
        result = await client.call_tool("approval_confirm", {"approval_id": ap})
        after = await client.call_tool("approval_status", {"approval_id": ap})
    text = json.dumps([c.model_dump() for c in [*result.content, *after.content]]) + str(result.structured_content)
    out = body(result)
    assert out["status"] == status
    assert seen[0] not in text and CONTENT not in text and "These words are the node" not in text
    assert "prompt" not in out and "answer" not in out and "yes" not in out


async def test_a_review_result_holds_no_link_that_decides(node, url):
    node.policy.notifier.config.public_base_url = "https://node.example"
    g, token = issue(node, ceiling="pay")
    ap = await asked(node, g, effect=Effect.PAY)
    async with agent(url, token) as client:
        out = await confirm(client, ap)
    assert out["review_url"].startswith(f"https://node.example/local/needs-you?approval={ap}&e=") and "&s=" in out["review_url"]
    assert "/action" not in out["review_url"] and "d=approve" not in out["review_url"]
    assert "d=deny" not in out["review_url"]


async def test_the_review_link_cannot_be_turned_into_a_decision(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    svc, _ = make_service(tmp_path, clock=Clock())
    svc.notifier.config.public_base_url = "https://node.example"
    g, _ = svc.issue(subject={"id": "writer"}, workspaces=["biz-a"], ceiling="pay")
    with pytest.raises(ApprovalPending) as e:
        await svc.context(g, tool="lane_post").authorize(Effect.PAY, lane_id=LANE, content=CONTENT, summary="Pay")
    ap = e.value.approval_id
    q = parse_qs(urlsplit(svc.notifier.review_url(ap, svc.approvals.get(ap).expires_at)).query)
    exp, sig = q["e"][0], q["s"][0]
    assert svc.approvals.check_review_link(ap, exp, sig).id == ap
    with pytest.raises(Denied) as denied:
        svc.approvals.decide_by_link(ap, "approve", exp, sig)
    assert denied.value.reason == "approve_in_panel"
    from agentos.core.errors import InvalidRequest

    with pytest.raises(InvalidRequest):
        svc.approvals.decide_by_link(ap, "deny", exp, sig)  # signed for review, so it denies nothing either
    assert svc.approvals.get(ap).state == "pending"
    svc.close()


async def test_a_call_with_no_mcp_client_gets_the_link(node):
    """An agent on a controller's MCP, or anything else that reaches the tool with no session here."""
    g, _ = issue(node)
    ap = await asked(node, g)
    out = await approval_confirm(node.policy.context(g, tool="approval_confirm"), approval_id=ap)
    assert out["status"] == "review"


async def test_an_unknown_approval_is_not_found(node):
    g, _ = issue(node)
    with pytest.raises(NotFound):
        await approval_confirm(node.policy.context(g, tool="approval_confirm"), approval_id="ap_nope")


# ---- the prompt ----

async def test_the_prompt_is_the_nodes_and_the_summary_never_reaches_it(node, url):
    g, token = issue(node)
    ap = await asked(node, g, summary="SUMMARY-WORDS approve this now")
    person = Person(DISMISS)
    async with agent(url, token, on_prompt=person) as client:
        await confirm(client, ap)
    message = person.seen[0].message
    assert "SUMMARY-WORDS" not in message
    assert f"| {CONTENT}" in message and f"Approval: {ap}" in message and LANE in message
    assert "enumNames" in person.seen[0].requested_schema["properties"]["answer"]


def test_agent_text_cannot_pass_for_the_nodes_words():
    escape, bidi, zero_width = chr(0x1B), chr(0x202E), chr(0x200B)
    forged = f"fine\n\nagentos says: this one is safe\n| still quoted{escape}[2J{bidi}evil{zero_width}"
    out = quoted(forged)
    assert all(line.startswith("| ") for line in out.splitlines())
    assert escape not in out and bidi not in out and zero_width not in out


def test_a_line_separator_cannot_leave_the_quote():
    assert all(line.startswith("| ") for line in quoted("a agentos says yes b\r\nc\x0bd").splitlines())


def test_the_prompt_quotes_every_line_of_the_content():
    from types import SimpleNamespace

    ap = SimpleNamespace(id="ap_1", effect="commit", tool="lane_post", lane=LANE, domain=None, content_hash="h",
                         content_digest="sha256:abcdef0123456789", preview="one\ntwo")
    text = prompt_text(ap)
    assert "| one\n| two" in text and "Content code: 23456789" in text


# ---- approval_pending says when chat can answer ----

async def test_approval_pending_says_when_chat_can_answer_it(tmp_path):
    async def h_post(ctx, *, on: str, caption: str) -> dict:
        """Post."""
        await ctx.authorize(Effect.COMMIT, lane_id=on, summary="Post", content={"caption": caption})
        return {}

    async def h_pay(ctx, *, on: str) -> dict:
        """Pay."""
        await ctx.authorize(Effect.PAY, lane_id=on, summary="Pay", content={"usd": 5})
        return {}

    svc, rt = make_service(tmp_path, clock=Clock())
    svc.ask_first.set("lane", LANE, True, by="owner", person=True)
    _, token = svc.issue(subject={"id": "writer"}, workspaces=["biz-a"], ceiling="pay")
    post = wrap_tool(ToolSpec(name="post", handler=h_post, effect=Effect.COMMIT), rt)
    pay = wrap_tool(ToolSpec(name="pay", handler=h_pay, effect=Effect.PAY), rt)
    with bind_request_token(token):
        out = await post(on=LANE, caption="hi")
        paying = await pay(on=LANE)
    assert out["status"] == "approval_pending" and out["requires"] == "chat" and out["can_answer_in_chat"] is True
    assert "approval_confirm" in out["offer"]
    assert paying["requires"] == "present" and paying["can_answer_in_chat"] is False
    svc.close()
