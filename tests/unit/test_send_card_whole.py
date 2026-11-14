"""A send card that is too long for its preview: the person reads every message before saying yes,
and no path approves it from the cut preview alone."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from workflow_support import LIST, MAIL, OWNER, action, make_node, workflow

from agentos.core.errors import InvalidRequest
from agentos.policy.approvals import SEND_PAGE
from agentos.policy.confirm import chat_refusal
from agentos.policy.errors import SeeWholeCard

SEND = f"mail.send@{MAIL}"
TEXT = {"subject": "News", "body": "Hello {{person.name}}, here is a longer note about this week's news."}
MANY = [f"person{i:03d}@example.com" for i in range(60)]


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.wf.sends.timezone = lambda: "UTC"
    n.h.clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    n.wf.sends.save_list(LIST, workspace="biz-a", name="Customers",
                         people=[{"email": a, "name": a.split("@")[0]} for a in MANY], person=True, by="owner")
    return n


async def waiting_card(node):
    wid = node.wf.save(workflow(action("send", SEND, label="send it", inputs=TEXT, send={"list": LIST})), OWNER)["id"]
    node.svc.ask_first.set("lane", MAIL, True, by="owner", person=True)
    view = await node.run(wid)
    return wid, view, node.svc.approvals.get(node.step(view, "send")["approval_id"])


def read_all(store, approval_id):
    page, cursor, seen, seen_to = 0, None, None, []
    while True:
        got = store.send_messages(approval_id, page, cursor)
        seen_to += [m["to"] for m in got["messages"]]
        seen = got["seen"]
        if got["next"] is None:
            return seen, seen_to
        page, cursor = got["next"]["page"], got["next"]["cursor"]


async def test_a_cut_send_card_shows_every_message_a_page_at_a_time(node):
    _, _, ap = await waiting_card(node)
    assert ap.preview_cut() and ap.needs_whole_view()
    info = ap.public()["messages"]
    assert info["count"] == 60 and info["pages"] == -(-60 // SEND_PAGE) and info["read_all_first"]
    seen, to = read_all(node.svc.approvals, ap.id)
    assert sorted(to) == sorted(MANY) and seen
    first = node.svc.approvals.send_messages(ap.id)["messages"][0]
    assert first["inputs"]["body"].startswith("Hello person000,")


async def test_a_cut_send_card_is_not_approved_without_every_page_read(node):
    _, _, ap = await waiting_card(node)
    with pytest.raises(SeeWholeCard):
        node.svc.approvals.decide(ap.id, "approve", by="owner")
    with pytest.raises(SeeWholeCard):
        node.svc.decide(ap.id, "approve")
    out = node.svc.approvals.batch([ap.id], "approve", by="owner")
    assert out[ap.id].startswith("see_whole_card")
    with pytest.raises(SeeWholeCard):  # a token for another card does not count
        node.svc.approvals.decide(ap.id, "approve", by="owner", seen="x" * 43)
    assert node.svc.approvals.get(ap.id).state == "pending"
    assert chat_refusal(ap) is not None  # and chat never answers it


async def test_pages_are_read_in_order(node):
    _, _, ap = await waiting_card(node)
    with pytest.raises(InvalidRequest, match="in order"):
        node.svc.approvals.send_messages(ap.id, 2)
    last = node.svc.approvals.send_messages(ap.id, 0)["next"]
    with pytest.raises(InvalidRequest, match="in order"):  # the cursor of page 1 does not open page 2
        node.svc.approvals.send_messages(ap.id, 2, last["cursor"])


async def test_after_reading_every_message_the_yes_sends_them_all(node):
    _, _, ap = await waiting_card(node)
    seen, _ = read_all(node.svc.approvals, ap.id)
    assert node.svc.approvals.decide(ap.id, "approve", by="owner", seen=seen).state == "approved"
    for i in range(5):  # it sends in batches, a second apart
        node.h.clock.set(datetime(2026, 11, 4, 10, 0, 2 * (i + 1), tzinfo=UTC))
        await node.settle()
    assert sorted(node.mail.to) == sorted(MANY)


async def test_a_message_with_a_secret_shows_it_redacted_and_marked(node):
    _, _, ap = await waiting_card(node)
    store = node.svc.approvals
    content = dict(store.get(ap.id).content)
    content["messages"] = [dict(m) for m in content["messages"]]
    content["messages"][3] = {**content["messages"][3], "inputs": {"body": "key sk-ant-abcdefghijklmnopqrstuvwx"}}
    import json
    with store.db.tx() as conn:
        conn.execute("UPDATE approvals SET content_json = ? WHERE id = ?", (json.dumps(content), ap.id))
    page = store.send_messages(ap.id)
    assert page["withheld"] == 1 and page["messages"][3]["withheld"] is True
    assert "sk-ant-abcdefghijklmnopqrstuvwx" not in json.dumps(page)


async def test_a_long_body_is_shown_whole(tmp_path):
    n = make_node(tmp_path)
    n.wf.sends.timezone = lambda: "UTC"
    n.h.clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    body = "Long news. " * 600  # over 6,000 characters for each person
    wid = n.wf.save(workflow(action("send", SEND, label="send it", inputs={"subject": "Hi", "body": body},
                                    send={"list": LIST})), OWNER)["id"]
    n.svc.ask_first.set("lane", MAIL, True, by="owner", person=True)
    ap = n.svc.approvals.get(n.step(await n.run(wid), "send")["approval_id"])
    assert ap.needs_whole_view()
    seen, _ = read_all(n.svc.approvals, ap.id)
    page = n.svc.approvals.send_messages(ap.id)
    assert page["withheld"] == 0 and page["messages"][0]["inputs"]["body"] == body and seen


async def test_a_standing_approval_for_its_digest_does_not_release_a_cut_send(node):
    _, _, ap = await waiting_card(node)
    node.svc.approvals.add_standing(effects=["commit"], lanes=[MAIL], until=node.svc.approvals.clock() + 3600,
                                    by="owner", targets=["tool:workflow_send"], contents=[ap.content_digest])
    await node.settle()
    assert node.mail.sent == [] and node.svc.approvals.get(ap.id).state == "pending"


async def test_a_short_send_card_needs_no_extra_step(tmp_path):
    n = make_node(tmp_path)
    n.wf.sends.timezone = lambda: "UTC"
    n.h.clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    wid = n.wf.save(workflow(action("send", SEND, label="send it", inputs={"subject": "Hi", "body": "Hi"},
                                    send={"list": LIST})), OWNER)["id"]
    n.svc.ask_first.set("lane", MAIL, True, by="owner", person=True)
    view = await n.run(wid)
    ap = n.svc.approvals.get(n.step(view, "send")["approval_id"])
    assert not ap.needs_whole_view()
    assert n.svc.decide(ap.id, "approve").state == "approved"


# ---- the panel's API: the pages, and the yes that carries the token ----

def test_the_api_serves_the_pages_and_takes_the_yes_only_with_the_token(tmp_path):
    from test_api_members import owner
    from test_node_panel import Node

    from agentos.api.auth import RateLimiter
    from agentos.core.models import Principal
    from agentos.policy.approvals import ActionBinding, ask_for_action

    n = Node(tmp_path)
    try:
        n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)
        messages = [{"to": a, "inputs": {"subject": "News", "body": "Hello " + "x" * 80}} for a in MANY]
        content = {"workflow": "biz.news", "step": "send", "operation": SEND, "list": LIST, "to": None,
                   "people": len(messages), "messages": messages}
        ap = ask_for_action(n.policy.approvals, ActionBinding(effect="commit", tool="workflow_send", lane=MAIL,
                                                              content=content, summary="Send it"),
                            Principal(kind="agent", id="a1", owner="owner"), None, grant_id=None, grant_path=None)
        assert ap.needs_whole_view()
        boss = owner(n)
        r = boss.req("POST", f"/api/approvals/{ap.id}/approve", {})
        assert r.status_code == 409 and r.json()["error"]["code"] == "see_whole_card", r.text
        page, cursor, to = 0, None, []
        while True:
            got = boss.get(f"/api/approvals/{ap.id}/messages?page={page}" + (f"&cursor={cursor}" if cursor else ""))
            assert got.status_code == 200, got.text
            body = got.json()
            to += [m["to"] for m in body["messages"]]
            if body["next"] is None:
                break
            page, cursor = body["next"]["page"], body["next"]["cursor"]
        assert sorted(to) == sorted(MANY)
        r = boss.req("POST", f"/api/approvals/{ap.id}/approve", {"seen": body["seen"]})
        assert r.status_code == 200 and r.json()["state"] == "approved", r.text
    finally:
        n.close()
