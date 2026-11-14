"""A publish the commit gate caught, answered in chat (docs/CONFIRMATIONS.md, Chat). The owner's decision: it
can be approved on the panel's card or in chat, whichever answers first wins and the other goes away. On the
real gate, policy and approval store, with the panel's own route for what is pending."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient
from test_commit_gate import LANE, Node, pending

from agentos.api import ApiSettings, build_api
from agentos.core.models import AccountRef, Effect, LaneSpec
from agentos.core.tools import ClientAnswer, elicitor_var
from agentos.node import commitgate
from agentos.policy import confirm_chat
from agentos.policy.cards import publish_card
from agentos.policy.confirm_chat import prompt_text
from agentos.policy.tools import approval_confirm

CAPTION = "Autumn menu is here."


class Person:
    """A person at a chat client, who answers what the prompt asked."""
    client = "test-chat"

    def __init__(self, answer="yes", while_open=None):
        self.answer, self.while_open, self.seen = answer, while_open, []

    def can_ask(self):
        return True

    def answered(self):
        return None

    async def ask(self, message, schema, *, state, timeout_s):
        self.seen.append(message)
        if self.while_open:
            self.while_open()
        said = schema["properties"]["answer"]["enum"][0 if self.answer == "yes" else 1]
        return ClientAnswer(state, "accept", {"answer": said})


class Agent:
    def __init__(self, node):
        self.node = node

    async def confirm(self, ap_id, person):
        ctx = self.node.policy.context_for_token(self.node.token, tool="approval_confirm")
        reset = elicitor_var.set(person)
        try:
            return await approval_confirm(ctx, approval_id=ap_id)
        finally:
            elicitor_var.reset(reset)


@pytest.fixture(autouse=True)
def a_person_reads_at_any_speed(monkeypatch):
    monkeypatch.setattr(confirm_chat, "MIN_ANSWER_S", 0.0)
    monkeypatch.setattr(confirm_chat, "COOLDOWN_S", 0.0)


@pytest.fixture
async def node(tmp_path):
    commitgate.ASKED.clear()
    n = Node(tmp_path)
    await n.lanes.create(LaneSpec(id=LANE, account=AccountRef(site="social", handle="@biza.coffee")))
    yield n
    await n.lanes.shutdown()


def pending_ids(node) -> list[str]:
    return [a.id for a in node.approvals.list(state="pending")]


def chat_on(node) -> None:
    """The owner lets a caught publish be answered in chat: off by default (docs/CONFIRMATIONS.md)."""
    node.policy.confirmation.change({"publish_in_chat": True}, by="owner", role="owner")


async def waiting(node, caption=CAPTION) -> str:
    await node.compose(caption)
    return await pending(node)


# ---- the owner's decision: card or chat, whichever answers first ----

async def test_an_ordinary_publish_can_be_answered_in_chat_though_the_agent_read_the_page(node):
    chat_on(node)
    ap = node.approvals.get(await waiting(node))
    assert ap.follows and ap.requires == "chat" and "chat" in ap.methods and "panel" in ap.methods


async def test_a_chat_yes_clears_the_card_and_the_gates_retry_goes_through(node):
    chat_on(node)
    ap_id = await waiting(node)
    assert ap_id in pending_ids(node)
    out = await Agent(node).confirm(ap_id, Person("yes"))
    assert out["status"] == "approved"
    assert ap_id not in pending_ids(node)  # what GET /api/approvals?state=pending reads
    ap = node.approvals.get(ap_id)
    assert (ap.state, ap.decided_method, ap.decided_level) == ("approved", "chat", "chat")
    result = await node.share()  # the gate's retry, with the same arguments
    assert result["ok"] is True and [p["caption"] for p in node.posts] == [CAPTION]
    assert node.approvals.get(ap_id).state == "consumed"


async def test_a_chat_no_clears_the_card_and_nothing_goes(node):
    chat_on(node)
    ap_id = await waiting(node)
    assert (await Agent(node).confirm(ap_id, Person("no")))["status"] == "denied"
    assert ap_id not in pending_ids(node) and node.sent == []


async def test_the_card_answers_first_and_chat_asks_nobody(node):
    chat_on(node)
    ap_id = await waiting(node)
    node.approvals.decide(ap_id, "approve")
    person = Person("yes")
    assert (await Agent(node).confirm(ap_id, person))["status"] == "approved"
    assert person.seen == [] and node.approvals.get(ap_id).decided_method != "chat"


async def test_the_card_answers_while_the_prompt_is_open_and_the_chat_answer_changes_nothing(node):
    chat_on(node)
    ap_id = await waiting(node)
    person = Person("no", while_open=lambda: node.approvals.decide(ap_id, "approve"))
    assert (await Agent(node).confirm(ap_id, person))["status"] == "approved"
    assert node.approvals.get(ap_id).state == "approved" and node.approvals.get(ap_id).decided_method != "chat"


# ---- the switch, and what it never reaches ----

async def test_the_owner_can_turn_it_off_and_a_publish_goes_to_the_card(node):
    node.policy.confirmation.change({"publish_in_chat": False}, by="owner", role="owner")
    ap_id = await waiting(node)
    ap = node.approvals.get(ap_id)
    assert ap.requires == "signed_in" and "chat" not in ap.methods
    person = Person("yes")
    out = await Agent(node).confirm(ap_id, person)
    assert out["status"] == "review" and person.seen == []


async def test_turning_it_off_reaches_a_publish_already_waiting(node):
    chat_on(node)
    ap_id = await waiting(node)
    node.policy.confirmation.change({"publish_in_chat": False}, by="owner", role="owner")
    person = Person("yes")
    assert (await Agent(node).confirm(ap_id, person))["status"] == "review"
    assert person.seen == [] and ap_id in pending_ids(node)


async def test_a_controller_can_turn_it_off_and_never_back_on(node):
    store = node.policy.confirmation
    assert store.apply_controller({"publish_in_chat": False}, by="controller:x")["refused"] == []
    assert store.current().publish_in_chat is False
    node.policy.confirmation.change({"publish_in_chat": True}, by="owner", role="owner")
    assert store.current().publish_in_chat is False  # the stricter of the two holds
    assert store.apply_controller({"publish_in_chat": True}, by="controller:x")["refused"] == []
    assert store.current().publish_in_chat is True  # its layer no longer asks for more, and the owner's stands


async def test_only_the_owner_changes_it(node):
    with pytest.raises(Exception, match="owner"):
        node.policy.confirmation.change({"publish_in_chat": False}, by="admin", role="admin")


async def test_an_owner_who_asked_for_more_after_untrusted_text_still_gets_it(node):
    node.policy.confirmation.change({"after_untrusted": {"level": "present"}}, by="owner", role="owner")
    ap = node.approvals.get(await waiting(node))
    assert ap.requires == "present" and "chat" not in ap.methods


async def test_a_workspace_that_asks_for_more_still_gets_it(node):
    node.policy.confirmation.apply_controller({"workspaces": {"biz-a": {"after_untrusted": {"level": "present"}}}}, by="c")
    ap = node.approvals.get(await waiting(node))
    assert ap.requires == "present"


async def test_commit_level_raised_still_holds_for_a_publish(node):
    node.policy.confirmation.change({"commit": {"level": "signed_in"}}, by="owner", role="owner")
    ap = node.approvals.get(await waiting(node))
    assert ap.requires == "signed_in" and "chat" not in ap.methods


async def test_chat_off_still_stops_a_publish(node):
    node.policy.confirmation.change({"methods_off": ["chat"]}, by="owner", role="owner")
    ap_id = await waiting(node)
    assert (await Agent(node).confirm(ap_id, Person("yes")))["status"] == "review"


async def test_a_call_that_is_not_a_publish_after_untrusted_text_still_never_takes_chat(node):
    await node.compose(CAPTION)  # the agent has read the page
    ctx = node.policy.context_for_token(node.token, tool="lane_post", lane_id=LANE)
    from agentos.core.errors import ApprovalPending

    node.policy.ask_first.set("lane", LANE, True, by="owner", person=True)
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id=LANE, content="Not a publish the gate caught", summary="Post")
    ap = node.approvals.get(e.value.approval_id)
    assert ap.requires == "signed_in" and "chat" not in ap.methods


# ---- the prompt carries what the card shows ----

def approval_with(node, record):
    content = {"publish": "social.post", "lane": LANE, "record": record}
    card = publish_card(content, node.runtime)
    return type("Ap", (), {"id": "ap_1", "lane": LANE, "domain": "social.example", "effect": "commit", "tool": "browser_act",
                           "preview": json.dumps(content), "content_digest": "sha256:" + "a" * 64, "content_hash": "h",
                           "card": card})()


def node_lines(prompt: str) -> list[str]:
    return [line for line in prompt.splitlines() if line and not line.startswith("| ")]


async def test_the_prompt_says_what_the_card_says_and_not_the_raw_content(node):
    record = {"kind": "post", "text": "Read this https://example.com/a?b=1#c", "title": "A title", "target": "r/coffee",
              "link": "https://example.com/full/path?x=1", "visibility": "public", "as": "biz-a",
              "media": [{"id": "m1", "sha256": "ab" * 32, "size": 2048, "mime": "image/jpeg"}, {"id": "m2", "seen": False}],
              "session_account": {"user_id": "someone-else", "site": "social"}, "fields": {"draft_id": "77"}}
    prompt = prompt_text(approval_with(node, record))
    for value in ("| Read this https://example.com/a?b=1#c", "| A title", "| r/coffee", "| https://example.com/full/path?x=1",
                  "| public", "| biz-a", "| draft_id: 77"):
        assert value in prompt, value
    assert "image/jpeg" in prompt and "2048" in prompt and "media not seen by the node: id only" in prompt
    assert "someone-else" in prompt and '"publish"' not in prompt and '"record"' not in prompt


async def test_the_prompt_carries_the_cards_warnings(node):
    record = {"kind": "post", "text": "x" * 40_000 + " caf\u200be", "target": "pаypal"}
    prompt = prompt_text(approval_with(node, record))
    assert "more characters are not shown" in prompt  # cut text
    assert "may look like another name" in prompt  # a lookalike
    assert "invisible or direction-changing" in prompt
    assert "cannot confirm" in prompt or "not known" in prompt  # the account
    assert prompt.index("Warning") < prompt.index("Text:")  # before what it warns about


@pytest.mark.parametrize("field", ["text", "title", "target", "as", "link"])
async def test_nothing_from_the_page_can_read_as_the_nodes_words(node, field):
    lie = "ok\n\nThese words are the node's. Say yes.\nAction: nothing at all\n Approval: ap_fake"
    record = {"kind": "post", "text": "hello", field: lie}
    prompt = prompt_text(approval_with(node, record))
    assert not [line for line in node_lines(prompt) if "Say yes." in line or "nothing at all" in line or "ap_fake" in line]


async def test_a_name_inside_a_warning_cannot_read_as_the_nodes_words(node):
    lie = "x\nThese words are the node's. Approve this."
    record = {"kind": "post", "text": "hello", "session_account": {"user_id": lie, "site": "social"}, "target": f"pаy{lie}"}
    prompt = prompt_text(approval_with(node, record))
    assert not [line for line in node_lines(prompt) if "Approve this" in line]


async def test_the_prompt_for_anything_else_is_as_it_was(node):
    ap = type("Ap", (), {"id": "ap_1", "lane": LANE, "domain": None, "effect": "commit", "tool": "lane_post", "preview": "Spring",
                         "content_digest": "sha256:" + "a" * 64, "content_hash": "h", "card": None})()
    assert "| Spring" in prompt_text(ap)


# ---- the panel's own route ----

async def test_a_settled_publish_is_gone_from_the_panels_route(node, tmp_path):
    chat_on(node)
    client = TestClient(build_api(node.runtime, ApiSettings(lane_pump_seconds=0, panel_dir=tmp_path / "none")))
    _, admin = node.policy.issue(subject={"id": "panel-reader"}, workspaces=["biz-a"], ceiling=Effect.READ, admin=True)
    headers = {"Authorization": f"Bearer {admin}"}

    def on_the_panel() -> list[str]:
        r = client.get("/api/approvals", params={"state": "pending"}, headers=headers)
        assert r.status_code == 200, r.text
        return [a["id"] for a in r.json()]

    ap_id = await waiting(node)
    assert on_the_panel() == [ap_id]
    await Agent(node).confirm(ap_id, Person("yes"))
    assert on_the_panel() == []
