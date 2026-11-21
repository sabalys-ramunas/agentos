"""Sending to people: the owner's list only, the daily limit, suppression, unsubscribe, and a retry
that never sends twice. Mail arrives by polling, so a node needs no public port."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta

import pytest
from workflow_support import LANE, LIST, MAIL, OWNER, PEOPLE, action, make_node, read_flow, workflow

from openajar.core.errors import InvalidRequest, PolicyDenied
from openajar.runner.connectors.compile import ConnectorInvalid
from openajar.runner.fakes import BASE, SimulatedCrash
from openajar.runner.workflows import mail as mail_module
from openajar.runner.workflows.mail import Received, build_message, parse_message
from openajar.runner.workflows.sends import DEFAULT_DAILY_LIMIT, MAX_DAILY_LIMIT, Unsubscribe
from openajar.runner.workflows.service import WorkflowInvalid

OWNER_P = {"kind": "human", "id": "owner", "owner": "owner"}
SEND = f"mail.send@{MAIL}"
TEXT = {"subject": "News", "body": "Hello {{person.name}}"}


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.wf.sends.timezone = lambda: "UTC"
    n.h.clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    return n


def newsletter(node, *, to=None, list_id=LIST, by=OWNER):
    send = {"list": list_id, **({"to": to} if to else {})}
    return node.wf.save(workflow(action("send", SEND, label="send it", inputs=TEXT, send=send)), by)["id"]


async def test_a_send_step_mails_everyone_on_the_owners_list_one_message_each(node):
    view = await node.run(newsletter(node))
    assert view["status"] == "succeeded_unverified"
    assert sorted(node.mail.to) == PEOPLE
    assert node.step(view, "send")["output"] == {"sent": 3, "skipped": 0, "failed": 0, "in_doubt": 0}
    assert node.step(view, "send")["short"] == "3 sent"
    assert node.mail.sent[0].get_content().startswith("Hello ana")


async def test_every_message_carries_an_unsubscribe_the_workflow_cannot_leave_out(node):
    await node.run(newsletter(node))
    for m in node.mail.sent:
        assert "unsubscribe-" in m["List-Unsubscribe"] and "To stop these messages: mailto:" in m.get_content()


async def test_a_controllers_link_is_used_when_there_is_one_and_it_is_one_click(node):
    node.wf.sends.public_base = lambda: "https://cloud.example/u/node1"
    await node.run(newsletter(node))
    m = node.mail.sent[0]
    assert "<https://cloud.example/u/node1/" in m["List-Unsubscribe"] and m["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


async def test_a_suppressed_address_is_skipped_whatever_the_list_says(node):
    node.wf.sends.suppress("biz-a", "BO@example.com", by="owner")
    view = await node.run(newsletter(node))
    assert sorted(node.mail.to) == ["ana@example.com", "cy@example.com"]
    assert node.step(view, "send")["output"]["skipped"] == 1


async def test_suppression_outlives_the_list_being_replaced(node):
    node.wf.sends.suppress("biz-a", PEOPLE[1], by="owner")
    node.wf.sends.save_list(LIST, workspace="biz-a", name="Customers", people=PEOPLE, person=True, by="owner")
    await node.run(newsletter(node))
    assert PEOPLE[1] not in node.mail.to


async def test_unsubscribing_stops_the_next_send_and_a_bad_token_does_nothing(node):
    wid = newsletter(node)
    await node.run(wid)
    link = next(m for m in node.mail.sent if m["To"] == PEOPLE[0])["List-Unsubscribe"]
    token = link.split("unsubscribe-")[1].rstrip(">")
    assert PEOPLE[0] not in token  # a controller relaying it learns nothing about who it is for
    assert node.wf.sends.unsubscribe("nothing-like-a-real-token-at-all") is False
    assert node.wf.sends.unsubscribe(token) is True
    node.mail.sent.clear()
    await node.run(wid)
    assert sorted(node.mail.to) == PEOPLE[1:]


async def test_an_unsubscribe_arrives_by_the_nodes_own_poll_of_the_mailbox(node):
    wid = newsletter(node)
    await node.run(wid)
    link = next(m for m in node.mail.sent if m["To"] == PEOPLE[0])["List-Unsubscribe"]
    subject = link.split("subject=")[1].rstrip(">")
    box = node.wf.mail.require(MAIL)
    await node.wf.poll_mailbox(box)
    node.mail.inbox.append(Received(1, PEOPLE[0], subject, ""))
    await node.wf.poll_mailbox(box)
    assert node.wf.sends.store.is_suppressed("biz-a", PEOPLE[0])


async def test_the_daily_limit_holds_across_runs_and_the_step_carries_on_tomorrow(node):
    node.wf.sends.set_limit(MAIL, 2, person=True, by="owner")
    wid = newsletter(node)
    view = await node.run(wid)
    assert len(node.mail.sent) == 2
    step = node.step(view, "send")
    assert step["status"] == "waiting" and "tomorrow" in step["note"]
    other = await node.run(wid)
    assert len(node.mail.sent) == 2 and node.step(other, "send")["status"] == "waiting"
    node.h.clock.advance(timedelta(hours=15).total_seconds())
    await node.settle()
    view = node.view(wid, view["id"])
    assert view["status"] == "succeeded_unverified" and node.step(view, "send")["output"]["sent"] == 3


async def test_a_send_has_a_limit_by_default_and_only_a_person_changes_it(node):
    assert node.wf.sends.limit_of(MAIL) == DEFAULT_DAILY_LIMIT == 200
    with pytest.raises(PolicyDenied):
        node.wf.sends.set_limit(MAIL, 1000, person=False, by="agent:a")
    with pytest.raises(InvalidRequest):
        node.wf.sends.set_limit(MAIL, MAX_DAILY_LIMIT + 1, person=True, by="owner")


def test_the_panel_reads_the_limit_from_the_node(node):
    node.wf.sends.suppress("biz-a", PEOPLE[0], by="owner")
    row = node.wf.public(newsletter(node))
    assert row["send_limits"] == [{"step": "send", "list": LIST, "list_size": 3, "daily_limit": 200, "sent_today": 0,
                                   "suppressed": 1, "unsubscribe": True}]


def test_a_list_is_what_a_person_supplied(node):
    with pytest.raises(PolicyDenied):
        node.wf.sends.save_list(None, workspace="biz-a", name="Scraped", people=["a@example.com"], person=False, by="agent:a")
    out = node.wf.sends.save_list(None, workspace="biz-a", name="Mine", people=["a@example.com", "not an address"], person=True, by="owner")
    assert out["size"] == 1 and out["refused"] == 1 and "a@example.com" not in str(node.wf.catalog()["lists"])


def test_a_send_needs_a_list_and_an_address_cannot_come_from_an_input(node):
    with pytest.raises(WorkflowInvalid) as e:
        node.wf.save(workflow(action("send", SEND, inputs={**TEXT, "to": "stranger@example.com"}, send={"list": LIST})), OWNER)
    assert [p["code"] for p in e.value.problems] == ["to_is_in_send"]
    wid = node.wf.save(workflow(action("send", SEND, inputs=TEXT)), OWNER)["id"]
    assert node.wf.public(wid)["missing"] == [{"target": "send", "words": 'Choose who "send" sends to.'}]
    with pytest.raises(WorkflowInvalid):
        node.wf.run(wid, OWNER)


async def test_sending_to_one_address_needs_it_on_the_list(node):
    node.h.save(read_flow(produces=[{"key": "who", "untrusted": False}]), by=OWNER_P)
    wid = node.wf.save(workflow(action("look", "biz-a.social.read"), action("send", SEND, inputs=TEXT, send={"list": LIST, "to": "{{look.who}}"})), OWNER)["id"]
    from openajar.runner.workflows import engine

    engine_output = engine.Walk._output
    try:
        engine.Walk._output = lambda self, child, op: ({"who": "stranger@example.com"}, set())
        view = await node.run(wid)
        assert node.mail.sent == [] and node.step(view, "send")["output"]["skipped"] == 1
        engine.Walk._output = lambda self, child, op: ({"who": "Ana@Example.com"}, set())
        await node.run(wid)
        assert node.mail.to == ["ana@example.com"]
    finally:
        engine.Walk._output = engine_output


def test_an_address_from_a_webhook_cannot_choose_who_gets_a_message(node):
    with pytest.raises(WorkflowInvalid) as e:
        node.wf.save(workflow(action("send", SEND, inputs=TEXT, send={"list": LIST, "to": "{{trigger.email}}"}),
                              trigger={"kind": "webhook"}), OWNER)
    assert [p["code"] for p in e.value.problems] == ["untrusted_decides"]


async def test_a_send_step_asks_once_when_ask_me_first_is_on_and_the_card_says_who(node):
    wid = newsletter(node)
    node.svc.ask_first.set("lane", MAIL, True, by="owner", person=True)
    view = await node.run(wid)
    step = node.step(view, "send")
    assert view["status"] == "waiting_approval" and node.mail.sent == [] and step["approval_id"]
    assert "3" in node.svc.approvals.get(step["approval_id"]).summary
    node.svc.decide(step["approval_id"], "approve")
    await node.settle()
    assert sorted(node.mail.to) == PEOPLE


async def test_a_persons_no_to_a_send_step_sends_nothing(node):
    wid = newsletter(node)
    node.svc.ask_first.set("flow", wid, True, by="owner", person=True)
    view = await node.run(wid)
    node.svc.decide(node.step(view, "send")["approval_id"], "deny")
    await node.settle()
    assert node.mail.sent == [] and node.view(wid, view["id"])["status"] == "failed"


async def test_a_message_with_outside_text_asks_first(node):
    wid = node.wf.save(workflow(action("send", SEND, inputs={"subject": "Hi", "body": "{{trigger.note}}"}, send={"list": LIST}),
                                trigger={"kind": "webhook"}), OWNER)["id"]
    view = await node.run(wid, node.agent(), values={"note": "from outside"})
    assert view["status"] == "waiting_approval" and node.mail.sent == []


# ---- a retried step does not double-send ----

async def test_a_retry_sends_only_to_those_it_never_reached(node):
    node.mail.refuse = {PEOPLE[1]}
    wid = newsletter(node)
    view = await node.run(wid)
    assert view["status"] == "partial" and sorted(node.mail.to) == [PEOPLE[0], PEOPLE[2]]
    assert "1 failed" in node.step(view, "send")["note"]
    node.mail.refuse = set()
    node.wf.retry(wid, view["id"], OWNER)
    await node.settle()
    view = node.view(wid, view["id"])
    assert sorted(node.mail.to) == PEOPLE and len(node.mail.to) == 3
    assert view["status"] == "succeeded_unverified" and node.step(view, "send")["attempts"] == 2


async def test_a_message_that_may_have_gone_is_never_sent_again(node):
    node.mail.lose = {PEOPLE[1]}
    wid = newsletter(node)
    view = await node.run(wid)
    assert view["status"] == "partial" and "may or may not" in node.step(view, "send")["note"]
    node.mail.lose = set()
    node.wf.retry(wid, view["id"], OWNER)
    await node.settle()
    view = node.view(wid, view["id"])
    assert node.mail.to.count(PEOPLE[1]) == 1 and len(node.mail.to) == 3
    assert view["status"] == "succeeded_unverified" and node.step(view, "send")["output"]["in_doubt"] == 1


async def test_a_crash_mid_send_is_in_doubt_not_repeated(node):
    wid = newsletter(node)
    wf = node.wf
    real = node.mail.send
    calls = 0

    async def dies(box, password, message, *, to):
        nonlocal calls
        calls += 1
        await real(box, password, message, to=to)
        if calls == 2:
            raise SimulatedCrash("the process stopped right after the message left")

    node.mail.send = dies
    run = wf.run(wid, OWNER)
    await node.settle()  # the crash leaves the run marked running, as a real one does
    node.mail.send = real
    assert len(node.mail.sent) == 2
    rows = {r["address"]: r["state"] for r in wf.sends.store.ledger_rows(run["id"], "send")}
    assert sorted(rows.values()) == ["sending", "sent"]
    node.runner.recover()
    await node.settle()
    assert len(node.mail.to) == 3 and len(set(node.mail.to)) == 3  # the one in flight was not sent again
    assert sorted(r["state"] for r in wf.sends.store.ledger_rows(run["id"], "send")) == ["sent", "sent", "unknown"]


async def test_two_runs_at_once_cannot_pass_the_limit_between_them(node):
    node.wf.sends.set_limit(MAIL, 3, person=True, by="owner")
    wid = newsletter(node)
    a, b = node.wf.run(wid, OWNER), node.wf.run(wid, OWNER)
    await node.settle()
    assert len(node.mail.sent) == 3
    assert {node.view(wid, r["id"])["status"] for r in (a, b)} == {"succeeded_unverified", "running"}


async def test_a_retried_send_is_authorized_again(node):
    node.mail.refuse = {PEOPLE[1]}
    wid = newsletter(node)
    view = await node.run(wid)
    node.mail.refuse = set()
    node.svc.ask_first.set("flow", wid, True, by="owner", person=True)
    node.wf.retry(wid, view["id"], OWNER)
    await node.settle()
    assert node.view(wid, view["id"])["status"] == "waiting_approval" and len(node.mail.sent) == 2


async def test_a_big_list_goes_out_in_batches_so_it_never_holds_a_slot_for_long(node):
    people = [f"p{i}@example.com" for i in range(60)]
    node.wf.sends.save_list(LIST, workspace="biz-a", name="Customers", people=people, person=True, by="owner")
    wid = newsletter(node)
    view = await node.run(wid)
    assert len(node.mail.sent) == 25 and node.step(view, "send")["status"] == "waiting"
    for _ in range(3):
        node.h.clock.advance(2)
        await node.settle()
    assert sorted(node.mail.to) == sorted(people) and node.view(wid, view["id"])["status"] == "succeeded_unverified"


async def test_a_doubt_early_in_a_big_list_is_still_reported_at_the_end(node):
    people = [f"p{i:02d}@example.com" for i in range(30)]
    node.wf.sends.save_list(LIST, workspace="biz-a", name="Customers", people=people, person=True, by="owner")
    node.mail.lose = {people[0]}
    wid = newsletter(node)
    view = await node.run(wid)
    node.h.clock.advance(2)
    await node.settle()
    view = node.view(wid, view["id"])
    assert view["status"] == "partial" and "may or may not" in node.step(view, "send")["note"]
    assert node.mail.to.count(people[0]) == 1


# ---- a flow that sends runs only as a send step ----

def sending_flow(fid="biz-a.social.email"):
    return {"id": fid, "lane": LANE, "effect": "commit", "operation": {"sends": {"to": "to", "body": "body"}},
            "params": {"type": "object", "required": ["to", "body"],
                       "properties": {"to": {"type": "string"}, "body": {"type": "string"}}},
            "steps": [{"type": "navigate", "url": f"{BASE}/"}]}


async def test_a_flow_that_sends_cannot_be_run_directly(node):
    node.h.save(sending_flow(), by=OWNER_P)
    with pytest.raises(PolicyDenied, match="send step"):
        node.runner.submit(flow_id="biz-a.social.email", params={"to": "x@example.com", "body": "hi"})


def test_an_agent_cannot_drop_the_sends_mark_to_get_round_that(node):
    node.h.save(sending_flow(), by=OWNER_P)
    plain = {k: v for k, v in sending_flow().items() if k != "operation"}
    with pytest.raises(PolicyDenied):
        node.h.save(plain, by={"kind": "agent", "id": "agent-1", "owner": "owner"})


async def test_a_flow_send_gets_the_ledger_the_limit_and_a_footer(node):
    node.wf.sends.public_base = lambda: "https://cloud.example/u/node1"
    node.h.save(sending_flow(), by=OWNER_P)
    node.wf.sends.suppress("biz-a", PEOPLE[0], by="owner")
    wid = node.wf.save(workflow(action("send", "biz-a.social.email", inputs={"body": "Hello {{person.name}}"}, send={"list": LIST})), OWNER)["id"]
    view = await node.run(wid)
    assert node.step(view, "send")["output"] == {"sent": 2, "skipped": 1, "failed": 0, "in_doubt": 0}
    runs = [r for r in node.runner.store.list_runs(flow_id="biz-a.social.email", limit=10)]
    assert sorted(r.params["to"] for r in runs) == PEOPLE[1:]
    assert all("https://cloud.example/u/node1/" in r.params["body"] for r in runs)
    assert len(node.wf.sends.store.ledger_rows(view["id"], "send")) == 3


async def test_a_flow_that_sends_needs_a_way_to_unsubscribe(node):
    node.h.save(sending_flow(), by=OWNER_P)
    wid = node.wf.save(workflow(action("send", "biz-a.social.email", inputs={"body": "x"}, send={"list": LIST})), OWNER)["id"]
    view = await node.run(wid)
    assert view["status"] == "failed" and "unsubscribe" in node.step(view, "send")["note"]
    assert node.runner.store.list_runs(flow_id="biz-a.social.email", limit=10) == []


# ---- mail in: the node dials out ----

def email_workflow(node, **trigger):
    wid = node.wf.save(workflow({"id": "n", "kind": "notify", "message": "From {{trigger.from}}: {{trigger.subject}}"},
                                trigger={"kind": "email", "lane": MAIL, **trigger}), OWNER)["id"]
    node.wf.enable(wid, OWNER)
    return wid


async def test_new_mail_starts_a_workflow_and_old_mail_does_not(node):
    node.mail.inbox.append(Received(5, "old@example.com", "old", "already here"))
    wid = email_workflow(node)
    box = node.wf.mail.require(MAIL)
    await node.wf.poll_mailbox(box)
    assert node.mail.seen_uid == [None]  # the first look only finds out where the mailbox is
    node.mail.inbox.append(Received(6, "new@example.com", "An order", "the text"))
    assert await node.wf.poll_mailbox(box) == 1
    await node.settle()
    [view] = node.wf.runs(wid)
    assert view["cause"] == "An email arrived" and view["steps"][0]["output"]["subject"] == "An order"
    assert view["steps"][0]["untrusted"] == ["body", "from", "id", "subject"]
    assert await node.wf.poll_mailbox(box) == 0 and len(node.wf.runs(wid)) == 1


async def test_the_same_mail_read_twice_starts_one_run(node):
    wid = email_workflow(node)
    box = node.wf.mail.require(MAIL)
    await node.wf.poll_mailbox(box)
    node.mail.inbox.append(Received(1, "a@example.com", "x", "y"))
    await node.wf.poll_mailbox(box)
    node.runner.store._exec("DELETE FROM meta WHERE key = ?", (f"mail_uid:{MAIL}",))
    node.runner.store.meta(f"mail_uid:{MAIL}", "0")
    assert await node.wf.poll_mailbox(box) == 0
    await node.settle()
    assert len(node.wf.runs(wid)) == 1


async def test_a_mail_trigger_filters_on_what_the_owner_wrote(node):
    wid = email_workflow(node, subject="invoice", **{"from": "billing@"})
    box = node.wf.mail.require(MAIL)
    await node.wf.poll_mailbox(box)
    node.mail.inbox += [Received(1, "billing@shop.example", "Your Invoice 12", "x"), Received(2, "billing@shop.example", "hello", "x"),
                        Received(3, "someone@shop.example", "invoice", "x")]
    assert await node.wf.poll_mailbox(box) == 1
    await node.settle()
    assert len(node.wf.runs(wid)) == 1


async def test_a_mail_trigger_is_polled_on_the_schedulers_tick(node):
    email_workflow(node)

    async def tick():  # the tick starts the read and leaves it, so this waits for what it started
        await node.runner.scheduler.tick()
        await asyncio.gather(*node.wf._polls.values())

    await tick()
    assert node.mail.seen_uid == [None]
    await tick()
    assert node.mail.seen_uid == [None]  # not again until the poll interval has passed
    node.h.clock.advance(61)
    await tick()
    assert node.mail.seen_uid == [None, 0]


def test_mail_is_dial_out_only():
    source = inspect.getsource(mail_module)
    assert "SMTP_SSL" in source and "IMAP4_SSL" in source
    for listener in ("listen(", "serve_forever", "start_server", "bind("):
        assert listener not in source


def test_a_subject_from_outside_cannot_carry_a_header_of_its_own():
    box = mail_module.Mailbox(id=MAIL, address="hello@biz-a.example", imap_host="i", smtp_host="s", username="u", secret_ref="r")
    m = build_message(box, to="a@example.com", subject="Hi\r\nBcc: everyone@example.com", body="b",
                      unsubscribe=Unsubscribe("t" * 24, None, "mailto:hello@biz-a.example?subject=unsubscribe-x"))
    assert m["Bcc"] is None and "Bcc: everyone" in m["Subject"]


def test_a_mailbox_password_never_shows(node):
    box = node.wf.mail.require(MAIL)
    assert box.public() == {"id": MAIL, "address": "hello@biz-a.example"}
    with pytest.raises(PolicyDenied):
        node.wf.mail.add({"id": "biz-a/other", "address": "a@b.example", "imap_host": "i", "smtp_host": "s", "username": "u",
                          "secret_ref": "r"}, person=False)


def test_a_message_body_is_read_as_text():
    raw = b"From: a@example.com\r\nSubject: Hi\r\nContent-Type: text/plain\r\n\r\nthe body\r\n"
    got = parse_message(7, raw)
    assert (got.uid, got.sender, got.subject, got.body.strip()) == (7, "a@example.com", "Hi", "the body")


# ---- no declaration needed: a commit in a loop counts against the daily limit ----

def agent_flow(fid="biz-a.social.plain"):
    """A commit that says nothing about who it reaches: it could be mail to people."""
    return {"id": fid, "lane": LANE, "effect": "commit", "params": {"properties": {"note": {"type": "string"}}},
            "steps": [{"type": "navigate", "url": f"{BASE}/"}]}


def people_flow(xs=None, fid="biz-a.social.people"):
    return read_flow(fid, produces=[{"key": "xs", "type": "list", "untrusted": False}])


def looped(node, flow_id="biz-a.social.plain", by=OWNER):
    node.h.save(people_flow(), by=OWNER_P)
    return node.wf.save(workflow(action("look", "biz-a.social.people"), {
        "id": "each", "kind": "loop", "over": "{{look.xs}}",
        "body": [action("act", flow_id, label="act", inputs={"note": "{{each.value}}"})]}), by)["id"]


@pytest.fixture
def items(monkeypatch):
    """What the read gives the loop: `items.value` is the list."""
    from types import SimpleNamespace

    from openajar.runner.workflows import engine

    got = SimpleNamespace(value=[f"p{i}" for i in range(5)])
    monkeypatch.setattr(engine.Walk, "_output", lambda self, child, op: (
        ({"xs": got.value}, set()) if op.flow_id == "biz-a.social.people" else ({}, set())))
    return got


def acted(node, flow_id="biz-a.social.plain"):
    return sorted(r.params["note"] for r in node.runner.store.list_runs(flow_id=flow_id, limit=100))


async def test_a_workflow_looping_an_undeclared_commit_stops_at_the_limit_and_carries_on_tomorrow(node, items):
    node.wf.sends.set_limit(LANE, 3, person=True, by="owner")
    node.h.save(agent_flow(), by=OWNER_P)
    wid = looped(node)
    view = await node.run(wid)
    assert acted(node) == ["p0", "p1", "p2"]
    step = node.step(view, "act")
    assert view["status"] == "running" and step["status"] == "waiting" and "tomorrow" in step["note"]
    assert node.wf.sends.store.sent_on(LANE, node.wf.sends.day()) == 3
    node.h.clock.advance(timedelta(hours=15).total_seconds())
    await node.settle()
    assert acted(node) == ["p0", "p1", "p2", "p3", "p4"]
    assert node.view(wid, view["id"])["status"] == "succeeded_unverified"


async def test_an_agent_cannot_declare_its_way_out_of_the_limit(node, items):
    node.wf.sends.set_limit(LANE, 2, person=True, by="owner")
    agent = node.agent()
    declared = {**agent_flow(), "operation": {"not_to_people": True}}
    node.h.save(declared, by={"kind": "agent", "id": "agent-1", "owner": "owner"})
    assert node.wf.operation("biz-a.social.plain").not_to_people is False
    wid = looped(node, by=agent)
    await node.run(wid, agent)
    assert acted(node) == ["p0", "p1"]


async def test_an_agent_declaring_a_lower_effect_gets_no_way_out_either(node, items):
    node.wf.sends.set_limit(LANE, 2, person=True, by="owner")
    agent = node.agent()
    click = {"type": "click", "locator": {"role": "button", "name": "New post"}, "publishes": False}
    node.h.save({**agent_flow(), "effect": "read", "steps": [{"type": "navigate", "url": f"{BASE}/"}, click]},
                by={"kind": "agent", "id": "agent-1", "owner": "owner"})
    assert node.wf.operation("biz-a.social.plain").effect.value == "commit"
    await node.run(looped(node, by=agent), agent)
    assert acted(node) == ["p0", "p1"]


async def test_an_agents_new_version_loses_a_persons_mark(node, items):
    node.wf.sends.set_limit(LANE, 2, person=True, by="owner")
    node.h.save({**agent_flow(), "operation": {"not_to_people": True}}, by=OWNER_P)
    assert node.wf.operation("biz-a.social.plain").not_to_people is True
    node.h.save({**agent_flow(), "title": "changed", "operation": {"not_to_people": True}},
                by={"kind": "agent", "id": "agent-1", "owner": "owner"})
    assert node.wf.operation("biz-a.social.plain").not_to_people is False
    await node.run(looped(node))
    assert acted(node) == ["p0", "p1"]


async def test_a_persons_mark_means_it_is_not_counted(node, items):
    node.wf.sends.set_limit(LANE, 2, person=True, by="owner")
    node.h.save({**agent_flow(), "operation": {"not_to_people": True}}, by=OWNER_P)
    view = await node.run(looped(node))
    assert view["status"] == "succeeded_unverified" and len(acted(node)) == 5
    assert node.wf.sends.store.sent_on(LANE, node.wf.sends.day()) == 0


async def test_a_person_can_vouch_for_what_an_agent_saved(node, items):
    node.wf.sends.set_limit(LANE, 2, person=True, by="owner")
    declared = {**agent_flow(), "operation": {"not_to_people": True}}
    node.h.save(declared, by={"kind": "agent", "id": "agent-1", "owner": "owner"})
    node.h.save(declared, by=OWNER_P)  # the same version, re-saved by a person
    assert node.wf.operation("biz-a.social.plain").not_to_people is True


async def test_a_step_runs_the_version_whose_mark_was_read(node, items):
    node.h.save({**agent_flow(), "operation": {"not_to_people": True}}, by=OWNER_P)
    marked = node.wf.operation("biz-a.social.plain").version
    await node.run(looped(node))
    assert {r.flow_version for r in node.runner.store.list_runs(flow_id="biz-a.social.plain", limit=10)} == {marked}


def test_a_flow_cannot_both_send_and_reach_nobody(node):
    with pytest.raises((InvalidRequest, ValueError)):
        node.h.save({**agent_flow(), "operation": {"sends": {"to": "to", "body": "body"}, "not_to_people": True}}, by=OWNER_P)


async def test_a_commit_fed_a_whole_list_spends_one_slot_per_item_before_it_acts(node, items):
    node.wf.sends.set_limit(LANE, 7, person=True, by="owner")
    node.h.save({**agent_flow(), "params": {"properties": {"note": {"type": "array"}}}}, by=OWNER_P)
    node.h.save(people_flow(), by=OWNER_P)
    wid = node.wf.save(workflow(action("look", "biz-a.social.people"), action("act", "biz-a.social.plain", inputs={"note": "{{look.xs}}"})), OWNER)["id"]
    await node.run(wid)
    assert node.wf.sends.store.sent_on(LANE, node.wf.sends.day()) == 5
    second = await node.run(wid)  # 5 more will not fit in 2: it waits, whole
    assert node.step(second, "act")["status"] == "waiting"
    assert len(node.runner.store.list_runs(flow_id="biz-a.social.plain", limit=10)) == 1


async def test_a_list_too_big_for_any_day_fails_and_says_to_loop(node, items):
    node.wf.sends.set_limit(LANE, 3, person=True, by="owner")
    node.h.save({**agent_flow(), "params": {"properties": {"note": {"type": "array"}}}}, by=OWNER_P)
    node.h.save(people_flow(), by=OWNER_P)
    wid = node.wf.save(workflow(action("look", "biz-a.social.people"), action("act", "biz-a.social.plain", inputs={"note": "{{look.xs}}"})), OWNER)["id"]
    view = await node.run(wid)
    assert view["status"] == "failed" and "Loop over them" in node.step(view, "act")["note"]
    assert node.runner.store.list_runs(flow_id="biz-a.social.plain", limit=10) == []


async def test_a_list_that_only_shows_up_when_it_runs_is_counted_too(node):
    node.wf.sends.set_limit(LANE, 2, person=True, by="owner")
    node.h.save({**agent_flow(), "params": {"properties": {"note": {"type": "array"}}}}, by=OWNER_P)
    wid = node.wf.save(workflow(action("act", "biz-a.social.plain", inputs={"note": "{{trigger.who}}"})), OWNER)["id"]
    assert node.wf.public(wid)["send_limits"] == []  # nothing a save could see
    view = await node.run(wid, values={"who": ["a", "b", "c"]})
    assert view["status"] == "failed" and node.runner.store.list_runs(flow_id="biz-a.social.plain", limit=10) == []


async def test_a_single_commit_outside_a_loop_is_not_counted(node):
    node.wf.sends.set_limit(LANE, 0, person=True, by="owner")
    node.h.save(agent_flow(), by=OWNER_P)
    wid = node.wf.save(workflow(action("act", "biz-a.social.plain", inputs={"note": "one"})), OWNER)["id"]
    assert (await node.run(wid))["status"] == "succeeded_unverified"


async def test_undeclared_commits_and_real_sends_share_one_limit(node, items):
    node.wf.sends.set_limit(MAIL, 4, person=True, by="owner")
    await node.run(newsletter(node))  # 3 of the 4
    node.h.save(agent_flow("biz-a.social.other"), by=OWNER_P)
    assert node.wf.sends.store.sent_on(MAIL, node.wf.sends.day()) == 3
    node.wf.sends.set_limit(LANE, 1, person=True, by="owner")
    await node.run(looped(node, "biz-a.social.other"))
    assert len(acted(node, "biz-a.social.other")) == 1


async def test_a_failed_counted_step_gives_its_slot_back_and_a_doubtful_one_keeps_it(node, items):
    node.wf.sends.set_limit(LANE, 5, person=True, by="owner")
    from openajar.runner.workflows import engine

    assert engine.settled(type("R", (), {"status": engine.RunStatus.FAILED})) == "failed"
    assert engine.settled(type("R", (), {"status": engine.RunStatus.PARTIAL})) == "unknown"
    assert engine.settled(type("R", (), {"status": engine.RunStatus.SUCCEEDED_VERIFIED})) == "sent"
    store, day = node.wf.sends.store, node.wf.sends.day()
    assert store.ledger_claim_many("r", "a#1", 2, LANE, day, node.h.clock.now(), 5) == "claimed"
    assert store.ledger_claim_many("r", "a#1", 2, LANE, day, node.h.clock.now(), 5) == "claimed"  # a walk coming back
    assert store.sent_on(LANE, day) == 2
    store.ledger_settle("r", "a#1", "failed")
    assert store.sent_on(LANE, day) == 0
    assert store.ledger_claim_many("r", "b#1", 6, LANE, day, node.h.clock.now(), 5) == "limit"
    assert store.sent_on(LANE, day) == 0  # all or none


def test_the_panel_sees_a_counted_step_and_that_it_has_no_unsubscribe(node):
    node.h.save(agent_flow(), by=OWNER_P)
    row = node.wf.public(looped(node))
    assert row["send_limits"] == [{"step": "act", "list": None, "list_size": 0, "daily_limit": 200, "sent_today": 0,
                                   "suppressed": 0, "unsubscribe": False, "counted": True}]


# ---- connectors declare it too ----

def compiled(**declared):
    from connector_samples import SITES, commit_op, connector

    from openajar.runner.connectors.compile import compile_connector

    return compile_connector(connector({"post_photo": {**commit_op(), **declared}}), sites=SITES)


def installed(tmp_path, **declared):
    from connector_samples import SITES, bind

    from openajar.runner.connectors.service import ConnectorService
    from openajar.runner.fakes import OWNER as INSTALLED_BY

    n = make_node(tmp_path)
    ConnectorService(n.h.flows, sites=SITES).install(compiled(**declared), by=INSTALLED_BY)
    bind(n.h)
    return n, n.wf.operation(f"connector.social.post_photo@{LANE}")


SENDS = {"to": "caption", "body": "caption"}


def test_a_connector_that_sends_runs_only_as_a_send_step(tmp_path):
    n, op = installed(tmp_path, sends=SENDS)
    assert op.sends is not None and op.sends.to == "caption"
    with pytest.raises(PolicyDenied, match="send step"):
        n.runner.submit(flow_id=op.flow_id, params={"caption": "x", "image": "a.jpg"}, lane=LANE)


def test_installing_a_connector_is_a_persons_act_so_its_mark_counts(tmp_path):
    assert installed(tmp_path / "a", not_to_people=True)[1].not_to_people is True
    assert installed(tmp_path / "b")[1].not_to_people is False


def test_a_connector_that_says_nothing_is_counted_in_a_loop(tmp_path):
    n, op = installed(tmp_path)
    n.h.save(people_flow(), by=OWNER_P)
    wid = n.wf.save(workflow(action("look", "biz-a.social.people"), {
        "id": "each", "kind": "loop", "over": "{{look.xs}}",
        "body": [action("act", op.id, inputs={"caption": "{{each.value}}", "image": "a.jpg"})]}), OWNER)["id"]
    assert [x["step"] for x in n.wf.public(wid)["send_limits"]] == ["act"]


def test_a_connector_that_says_nothing_keeps_its_contract_and_version():
    def contract(**declared):
        return compiled(**declared).operations["post_photo"]

    plain = contract()
    assert "operation" not in plain.definition
    assert contract(sends=SENDS).contract != plain.contract != contract(not_to_people=True).contract


def test_a_connector_cannot_say_both_or_name_an_input_it_does_not_have():
    with pytest.raises(ConnectorInvalid, match="not both"):
        compiled(sends=SENDS, not_to_people=True)
    with pytest.raises(ConnectorInvalid, match="not one of the operation's inputs"):
        compiled(sends={"to": "nobody", "body": "caption"})


# ---- what the card shows is what is sent ----

async def test_a_send_card_shows_each_message_filled_as_it_will_be_sent(node):
    wid = newsletter(node)
    node.svc.ask_first.set("lane", MAIL, True, by="owner", person=True)
    view = await node.run(wid)
    ap = node.svc.approvals.get(node.step(view, "send")["approval_id"])
    for name in ("ana", "bo", "cy"):
        assert f"Hello {name}" in ap.preview
    assert '"Hello "' not in ap.preview


async def test_a_message_filled_differently_at_send_time_is_not_sent(node, monkeypatch):
    from openajar.runner.workflows import engine

    wid = newsletter(node)
    node.svc.ask_first.set("lane", MAIL, True, by="owner", person=True)
    view = await node.run(wid)
    node.svc.decide(node.step(view, "send")["approval_id"], "approve")
    real_inputs, real_authorize = engine.Walk._person_inputs, engine.Walk._authorize_send

    def other(self, step, op, person):
        values, outside = real_inputs(self, step, op, person)
        return {**values, "body": "Something the owner never saw"}, outside

    async def authorize_then_change(self, *a, **kw):
        await real_authorize(self, *a, **kw)
        monkeypatch.setattr(engine.Walk, "_person_inputs", other)  # only what is sent changes, not the card

    monkeypatch.setattr(engine.Walk, "_authorize_send", authorize_then_change)
    await node.settle()
    assert node.mail.sent == []
    view = node.view(wid, view["id"])
    assert view["status"] == "failed" and "not the one that was approved" in node.step(view, "send")["note"]


async def test_a_flow_send_card_names_the_flow_version_and_a_newer_one_asks_again(node):
    node.h.save(sending_flow(), by=OWNER_P)
    wid = node.wf.save(workflow(action("send", "biz-a.social.email", inputs={"body": "Hello {{person.name}}"},
                                       send={"list": LIST})), OWNER)["id"]
    node.svc.ask_first.set("flow", wid, True, by="owner", person=True)
    view = await node.run(wid)
    first = node.step(view, "send")["approval_id"]
    v1 = node.runner.flows.get("biz-a.social.email").version
    assert v1 in node.svc.approvals.get(first).preview
    newer = sending_flow()
    newer["steps"][0]["url"] += "newer"
    node.h.save(newer, by=OWNER_P)
    node.svc.decide(first, "approve")
    await node.settle()
    assert node.runner.store.list_runs(flow_id="biz-a.social.email", limit=10) == []
    view = node.view(wid, view["id"])
    assert view["status"] == "waiting_approval" and node.step(view, "send")["approval_id"] != first


async def test_a_run_someone_else_queued_under_a_send_key_is_not_taken_for_the_send(node):
    from openajar.runner.workflows.engine import address_key

    node.h.save(sending_flow(), by=OWNER_P)
    node.h.save({"id": "biz-a.social.look", "lane": LANE, "steps": [{"type": "navigate", "url": f"{BASE}/"}]})
    wid = node.wf.save(workflow(action("send", "biz-a.social.email", inputs={"body": "Hi"}, send={"list": LIST})), OWNER)["id"]
    node.svc.ask_first.set("flow", wid, True, by="owner", person=True)
    view = await node.run(wid)
    for a in PEOPLE:  # what an agent could queue while the card waits
        node.runner.submit(flow_id="biz-a.social.look", idempotency_key=f"wf:{view['id']}:send:{address_key(a)}")
    node.svc.decide(node.step(view, "send")["approval_id"], "approve")
    await node.settle()
    view = node.view(wid, view["id"])
    assert view["status"] == "failed" and node.wf.sends.store.ledger_rows(view["id"], "send") == [] or \
        all(r["state"] != "sent" for r in node.wf.sends.store.ledger_rows(view["id"], "send"))


async def test_a_send_that_waits_after_its_yes_never_goes_on_with_a_newer_flow_version(node):
    node.h.save(sending_flow(), by=OWNER_P)
    node.wf.sends.public_base = lambda: "https://cloud.example/u/node1"
    node.wf.sends.limit_of = lambda account: 1  # one a day, so the step waits for tomorrow after the first
    wid = node.wf.save(workflow(action("send", "biz-a.social.email", inputs={"body": "Hello {{person.name}}"},
                                       send={"list": LIST})), OWNER)["id"]
    node.svc.ask_first.set("flow", wid, True, by="owner", person=True)
    view = await node.run(wid)
    node.svc.decide(node.step(view, "send")["approval_id"], "approve")
    await node.settle()
    assert len(node.runner.store.list_runs(flow_id="biz-a.social.email", limit=10)) == 1, node.view(wid, view["id"])
    newer = sending_flow()
    newer["steps"][0]["url"] += "newer"
    node.h.save(newer, by=OWNER_P)  # saved while the step waits
    node.h.clock.advance(86400)
    await node.settle()
    v2 = node.runner.flows.get("biz-a.social.email").version
    assert [r for r in node.runner.store.list_runs(flow_id="biz-a.social.email", limit=10) if r.flow_version == v2] == []
