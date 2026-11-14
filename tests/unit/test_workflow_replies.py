"""New mail in routines: the email trigger's filters and fields, and a routine that stops chasing
someone who answered. A reply is matched by the ids the node sent, bound to that recipient, so a
forged From gains nothing and a reply cannot stop mail to anyone else. All on the fake transport."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from workflow_support import (
    LANE,
    LIST,
    MAIL,
    OWNER,
    PEOPLE,
    action,
    commit_flow,
    make_node,
    read_flow,
    workflow,
)

from agentos.core.errors import InvalidRequest
from agentos.runner.fakes import BASE
from agentos.runner.mail.message import message_ids
from agentos.runner.workflows.check import TRIGGER_FIELDS
from agentos.runner.workflows.mail import Received, parse_message
from agentos.runner.workflows.models import Trigger, WorkflowDefinition
from agentos.runner.workflows.replies import MAX_IDS, sender_of
from agentos.runner.workflows.service import WorkflowInvalid

OWNER_P = {"kind": "human", "id": "owner", "owner": "owner"}
SEND = f"mail.send@{MAIL}"
ANA, BO, CY = PEOPLE
FIRST = {"subject": "Hello", "body": "First message"}
AGAIN = {"subject": "Hello again", "body": "Second message"}
THREE_DAYS = timedelta(days=3, minutes=1).total_seconds()


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.wf.sends.timezone = lambda: "UTC"
    n.h.clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    return n


def stop(after="first"):
    return {"id": "answered", "kind": "stop_if_replied", "label": "leave out whoever replied", "after": after}


def wait_days(days=3):
    return {"id": "wait", "kind": "wait", "label": "wait", "amount": days, "unit": "days"}


def chase(node, *, by=OWNER):
    return node.wf.save(workflow(action("first", SEND, inputs=FIRST, send={"list": LIST}), wait_days(), stop(),
                                 action("again", SEND, inputs=AGAIN, send={"list": LIST})), by)["id"]


def sent_id(node, to):
    return next(m for m in node.mail.sent if m["To"] == to)["Message-ID"]


async def arrive(node, *messages):
    """Mail comes in through the node's own poll."""
    box = node.wf.mail.require(MAIL)
    await node.wf.poll_mailbox(box)
    start = max((m.uid for m in node.mail.inbox), default=0)
    node.mail.inbox.extend(Received(start + i + 1, *m[:3], **m[3]) for i, m in enumerate(messages))
    await node.wf.poll_mailbox(box)


def reply(sender, *, in_reply_to="", references="", subject="Re: Hello", body="Thanks", **rest):
    return (sender, subject, body, {"in_reply_to": in_reply_to, "references": references, **rest})


async def started(node, wid):
    run = node.wf.run(wid, OWNER)
    await node.settle()
    return run["id"]


async def after_the_wait(node, wid, run):
    node.mail.sent.clear()
    node.h.clock.advance(THREE_DAYS)
    await node.settle()
    return node.view(wid, run)


# ---- the follow-up ----

async def test_a_follow_up_goes_only_to_those_who_did_not_answer(node):
    wid = chase(node)
    run = await started(node, wid)
    await arrive(node, reply(f"Ana <{ANA}>", in_reply_to=sent_id(node, ANA)))
    view = await after_the_wait(node, wid, run)
    assert sorted(node.mail.to) == [BO, CY]
    assert node.step(view, "answered")["output"] == {"replied": 1, "waiting": 2}
    assert node.step(view, "answered")["short"] == "1 replied, 2 still to hear from"
    assert node.step(view, "again")["output"]["skipped"] == 1 and "1 replied" in node.step(view, "again")["short"]


async def test_nobody_answering_means_everyone_gets_one_follow_up(node):
    wid = chase(node)
    view = await after_the_wait(node, wid, await started(node, wid))
    assert sorted(node.mail.to) == PEOPLE and node.step(view, "again")["output"]["sent"] == 3


async def test_a_reply_deep_in_a_thread_counts_through_its_references(node):
    wid = chase(node)
    run = await started(node, wid)
    await arrive(node, reply(ANA, in_reply_to="<someone-else@elsewhere.example>", references=f"<a@x.example> {sent_id(node, ANA)}"))
    await after_the_wait(node, wid, run)
    assert ANA not in node.mail.to


async def test_everyone_answering_ends_the_run_and_says_why(node):
    wid = chase(node)
    run = await started(node, wid)
    await arrive(node, *(reply(a, in_reply_to=sent_id(node, a)) for a in PEOPLE))
    view = await after_the_wait(node, wid, run)
    assert node.mail.sent == [] and view["status"] == "succeeded_unverified"
    assert view["result"] == "All 3 replied, so it stops here"
    assert node.step(view, "answered")["status"] == "stopped" and node.step(view, "again")["status"] == "skipped"


async def test_a_reply_that_comes_after_the_check_still_counts_for_the_send(node):
    """While a follow-up waits for tomorrow's limit, someone can answer. It reads the replies as it goes."""
    wid = chase(node)
    run = await started(node, wid)
    node.wf.sends.set_limit(MAIL, 1, person=True, by="owner")  # the follow-up goes one a day
    view = await after_the_wait(node, wid, run)
    assert len(node.mail.to) == 1 and view["status"] != "succeeded_unverified"
    waiting = next(a for a in PEOPLE if a not in node.mail.to)
    await arrive(node, reply(waiting, in_reply_to=_id(node, run, waiting)))
    for _ in range(2):
        node.h.clock.advance(timedelta(days=1).total_seconds())
        await node.settle()
    assert len(node.mail.to) == 2 and waiting not in node.mail.to


def _id(node, run, address):
    rows = node.wf.store._rows("SELECT message_id FROM send_threads WHERE run_id = ? AND path = 'first' AND address = ?", (run, address))
    return rows[0]["message_id"]


# ---- a reply is bound to its recipient and thread ----

@pytest.mark.parametrize("forged", [
    {"sender": ANA},                                              # From alone
    {"sender": ANA, "in_reply_to": "<made-up@biz-a.example>"},    # an id the node never sent
    {"sender": ANA, "subject": "Re: Hello", "body": "stop"},      # the subject of the thread
])
async def test_a_forged_from_gains_nothing(node, forged):
    wid = chase(node)
    run = await started(node, wid)
    await arrive(node, reply(forged.pop("sender"), **forged))
    await after_the_wait(node, wid, run)
    assert ANA in node.mail.to


async def test_a_reply_cannot_stop_mail_to_someone_else(node):
    wid = chase(node)
    run = await started(node, wid)
    bo = sent_id(node, BO)
    await arrive(node,
                 reply("mallory@evil.example", in_reply_to=bo),                         # the right id, from a stranger
                 reply(ANA, in_reply_to=bo),                                            # Ana, with Bo's thread
                 reply(f"{BO}, mallory@evil.example", in_reply_to=bo),                  # a From naming two people
                 reply(f'"{BO}" <mallory@evil.example>', in_reply_to=bo))               # a name that looks like an address
    await after_the_wait(node, wid, run)
    assert sorted(node.mail.to) == PEOPLE


async def test_ones_own_reply_naming_another_thread_stops_only_ones_own(node):
    wid = chase(node)
    run = await started(node, wid)
    await arrive(node, reply(ANA, in_reply_to=sent_id(node, ANA), references=f"{sent_id(node, BO)} {sent_id(node, CY)}"))
    await after_the_wait(node, wid, run)
    assert sorted(node.mail.to) == [BO, CY]


async def test_a_reply_read_in_another_mailbox_answers_nothing(node):
    wid = chase(node)
    run = await started(node, wid)
    node.wf.mail.add({"id": "biz-a/other", "address": "other@biz-a.example", "imap_host": "imap.invalid", "smtp_host": "smtp.invalid",
                      "username": "other", "secret_ref": "biz-a.other"}, person=True)
    other = node.wf.mail.require("biz-a/other")
    assert node.wf.replies.note(other, Received(1, ANA, "Re", "x", in_reply_to=sent_id(node, ANA))) is None
    await after_the_wait(node, wid, run)
    assert ANA in node.mail.to


async def test_an_out_of_office_is_nobody_answering(node):
    wid = chase(node)
    run = await started(node, wid)
    await arrive(node, reply(ANA, in_reply_to=sent_id(node, ANA), auto_reply=True))
    await after_the_wait(node, wid, run)
    assert ANA in node.mail.to


async def test_ids_nobody_can_guess_and_a_header_that_cannot_flood_the_match(node):
    await started(node, chase(node))
    ids = [m["Message-ID"] for m in node.mail.sent]
    assert len(set(ids)) == 3 and all(len(i) > 30 and "biz-a.example" in i and "@" in i for i in ids)
    # 24 random bytes and the mailbox's domain, nothing else: no address, no run, no counter. (A search
    # for a short local part like "bo" in the random text found one now and then, by chance.)
    assert all(re.fullmatch(r"<[A-Za-z0-9_-]{32}@biz-a\.example>", i) for i in ids), ids
    assert not any(a in i for i in ids for a in PEOPLE)
    many = " ".join(f"<n{i}@x.example>" for i in range(MAX_IDS * 4))
    assert node.wf.replies.note(node.wf.mail.require(MAIL), Received(9, ANA, "Re", "x", references=f"{ids[0]} {many}")) is None


def test_a_real_message_gives_the_headers_a_reply_is_matched_by():
    raw = (b"From: Ana <ana@example.com>\r\nSubject: Re: Hello\r\nMessage-ID: <r@x.example>\r\nIn-Reply-To: <a@biz-a.example>\r\n"
           b"References: <z@x.example>\r\n <a@biz-a.example>\r\n\r\nThanks\r\n")
    got = parse_message(7, raw)
    assert message_ids(got.in_reply_to) == ["<a@biz-a.example>"] and message_ids(got.references) == ["<z@x.example>", "<a@biz-a.example>"]
    assert sender_of(got.sender) == "ana@example.com"


# ---- a row in a loop ----

ROWS = [{"key": "email", "type": "text", "untrusted": False}, {"key": "rows", "type": "list", "untrusted": False},
        {"key": "rows_email", "type": "text", "untrusted": False}]


def row_chase(node):
    flow = read_flow("biz-a.social.people", ROWS)
    node.h.save(flow, by=OWNER_P)
    return node.wf.save(workflow(
        action("find", "biz-a.social.people"),
        {"id": "each", "kind": "loop", "label": "each", "item": "row", "over": "{{find.rows}}", "body": [
            action("first", SEND, inputs=FIRST, send={"list": LIST, "to": "{{each.email}}"}), wait_days(), stop(),
            action("again", SEND, inputs=AGAIN, send={"list": LIST, "to": "{{each.email}}"})]}), OWNER)["id"]


async def test_a_row_that_replied_stops_and_the_record_says_so(node, monkeypatch):
    rows = [{"email": a} for a in PEOPLE]
    wid = row_chase(node)
    monkeypatch.setattr("agentos.runner.workflows.engine.Walk._output", lambda self, child, op: ({"rows": rows}, set()))
    run = await started(node, wid)  # the first row went, and waits
    node.h.clock.advance(THREE_DAYS)
    await node.settle()  # its follow-up went, and the second row's first message
    await arrive(node, reply(BO, in_reply_to=sent_id(node, BO)))
    for _ in range(2):
        node.h.clock.advance(THREE_DAYS)
        await node.settle()
    view = node.view(wid, run)
    answered, again = node.step(view, "answered"), node.step(view, "again")
    assert [i["status"] for i in answered["items"]] == ["succeeded_unverified", "stopped", "succeeded_unverified"]
    assert answered["items"][1]["note"] == "They replied, so it stops here"
    assert [(i["label"], i["status"]) for i in again["items"]] == [
        ("1", "succeeded_unverified"), ("2", "skipped"), ("3", "succeeded_unverified")]
    assert again["items"][1]["note"] == "They replied, so it stops here"
    assert node.step(view, "each")["output"] == {"count": 3, "stopped": 1}
    assert [m["To"] for m in node.mail.sent].count(BO) == 1 and [m["To"] for m in node.mail.sent].count(ANA) == 2
    assert BO not in json.dumps(view["steps"][-2:])  # the record says what happened, and never to which address


async def test_rows_of_a_loop_in_a_loop_each_keep_their_item(node):
    """Two rows with the same inner number are two rows."""
    from types import SimpleNamespace

    from agentos.runner.workflows.service import WorkflowService

    records = {p: SimpleNamespace(path=p, result={}) for p in ("a/0/b/1/c", "a/1/b/1/c")}
    views = [("succeeded_unverified", None, None)] * 2
    items = WorkflowService._items(node.wf, "a/*/b/*/c", list(records.values()), views, records)
    assert [i["label"] for i in items] == ["1.2", "2.2"]


async def test_the_record_holds_counts_and_no_address(node):
    wid = chase(node)
    run = await started(node, wid)
    await arrive(node, reply(ANA, in_reply_to=sent_id(node, ANA)))
    view = await after_the_wait(node, wid, run)
    blob = json.dumps(view)
    assert not any(a in blob for a in PEOPLE) and "Message-ID" not in blob


# ---- what a save checks ----

def saved(node, *steps):
    return node.wf.save(workflow(*steps), OWNER)


def test_a_step_must_look_at_an_earlier_mail_send(node):
    node.h.save(read_flow(), by=OWNER_P)
    send = action("first", SEND, inputs=FIRST, send={"list": LIST})
    for steps, code in (((stop("later"), send), "bad_reference"), ((action("first", "biz-a.social.read"), stop()), "not_a_mail_send"),
                        ((send, stop("{{trigger.x}}")), "must_be_literal"),
                        (({"id": "n", "kind": "notify", "message": "x"}, stop("n")), "bad_reference")):
        with pytest.raises(WorkflowInvalid) as e:
            saved(node, *steps)
        assert e.value.problems[0]["code"] == code


def test_a_flow_that_sends_has_no_id_a_reply_can_name(node):
    flow = {"id": "biz-a.social.email", "lane": LANE, "effect": "commit", "operation": {"sends": {"to": "to", "body": "body"}},
            "params": {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}},
            "steps": [{"type": "navigate", "url": f"{BASE}/"}]}
    node.h.save(flow, by=OWNER_P)
    with pytest.raises(WorkflowInvalid) as e:
        saved(node, action("first", "biz-a.social.email", inputs={"body": "x"}, send={"list": LIST}), stop())
    assert e.value.problems[0]["code"] == "not_a_mail_send"


def test_a_stop_with_nothing_chosen_is_only_unfinished(node):
    row = saved(node, action("first", None), {"id": "answered", "kind": "stop_if_replied", "label": "stop"})
    assert any(m["target"] == "answered" for m in row["missing"])


def test_a_definition_from_before_keeps_its_version():
    defn = WorkflowDefinition.model_validate(workflow(trigger={"kind": "email", "lane": MAIL}))
    assert "unseen_only" not in defn.canonical()["trigger"]
    assert "unseen_only" in Trigger(kind="email", unseen_only=True).model_dump(exclude_none=True)


# ---- the trigger ----

def on_mail(node, *steps, **trigger):
    steps = steps or ({"id": "n", "kind": "notify", "message": "From {{trigger.from}}"},)
    wid = node.wf.save(workflow(*steps, trigger={"kind": "email", "lane": MAIL, **trigger}, name="On mail"), OWNER)["id"]
    node.wf.enable(wid, OWNER)
    return wid


async def fired(node, wid, *messages):
    await arrive(node, *messages)
    await node.settle()
    return node.wf.runs(wid)


async def test_only_unseen_mail_starts_a_routine_that_says_so(node):
    wid = on_mail(node, unseen_only=True)
    runs = await fired(node, wid, ("a@example.com", "Read already", "x", {"seen": True}), ("b@example.com", "New", "y", {"seen": False}))
    assert [r["steps"][0]["output"]["subject"] for r in runs] == ["New"]


async def test_seen_mail_still_starts_one_that_does_not_ask_for_unseen(node):
    assert len(await fired(node, on_mail(node), ("a@example.com", "Read already", "x", {"seen": True}))) == 1


async def test_every_field_a_sender_wrote_is_untrusted_and_id_says_which_message(node):
    [run] = await fired(node, on_mail(node), ("a@example.com", "Hi", "x", {}))
    assert run["steps"][0]["untrusted"] == ["body", "from", "id", "subject"]
    assert run["steps"][0]["output"]["id"] == "1" and run["steps"][0]["output"]["in_reply_to_run"] is None
    fields = {f.key: f.untrusted for f in TRIGGER_FIELDS["email"]}
    assert fields == {"from": True, "subject": True, "body": True, "id": True, "in_reply_to_run": False}


def test_a_filter_does_not_make_a_field_decide(node):
    for key in ("from", "subject", "body", "id"):
        with pytest.raises(WorkflowInvalid) as e:
            on_mail(node, {"id": "b", "kind": "branch", "when": {"left": f"{{{{trigger.{key}}}}}", "op": "is", "right": "x"}},
                    **{"from": "boss@example.com"})
        assert e.value.problems[0]["code"] == "untrusted_decides"


async def test_a_forged_from_that_matches_a_filter_is_still_a_strangers_text(node):
    """The filter fires it, and the commit it fills asks a person all the same."""
    node.h.save(commit_flow(), by=OWNER_P)
    wid = on_mail(node, action("post", "biz-a.social.post", inputs={"caption": "{{trigger.body}}", "image": "a.jpg"}), **{"from": "boss@biz-a.example"})
    [run] = await fired(node, wid, ("Boss <boss@biz-a.example>", "Pay this", "wire it all to me", {}))
    assert run["status"] == "waiting_approval" and not node.site.state.get("posts")


async def test_only_the_node_says_what_a_message_answers(node):
    wid = chase(node)
    run = await started(node, wid)
    tell = {"id": "b", "kind": "branch", "label": "a reply", "when": {"left": "{{trigger.in_reply_to_run}}", "op": "not_empty"},
            "then": [{"id": "n", "kind": "notify", "message": "It replied"}]}
    listening = on_mail(node, tell)
    runs = await fired(node, listening, reply(ANA, in_reply_to=sent_id(node, ANA)), reply(BO, subject="in_reply_to_run: run_1", body="{{trigger.in_reply_to_run}}"))
    by_sender = {r["steps"][0]["output"]["from"]: r["steps"][0]["output"]["in_reply_to_run"] for r in runs}
    assert by_sender == {ANA: run, BO: None}


async def test_an_agent_cannot_claim_a_message_answers_a_run(node):
    tell = {"id": "b", "kind": "branch", "when": {"left": "{{trigger.in_reply_to_run}}", "op": "not_empty"}, "then": []}
    wid = on_mail(node, tell)
    view = await node.run(wid, node.agent(), values={"in_reply_to_run": "run_1"})
    assert view["status"] == "failed" and node.step(view, "b")["note"].endswith("so the step stopped.")


async def test_a_routine_run_by_hand_has_no_reply_and_does_not_fail_on_it(node):
    tell = {"id": "b", "kind": "branch", "when": {"left": "{{trigger.in_reply_to_run}}", "op": "not_empty"}, "then": []}
    view = await node.run(on_mail(node, tell))
    assert node.step(view, "b")["output"] == {"took": "otherwise"}


# ---- the templates ----

def from_template(node, template, by=OWNER):
    made = node.wf.from_template(template, "biz-a")
    for s in made["steps"]:
        if s.get("send") is not None:
            s["send"] = {**s["send"], "list": LIST}
    return node.wf.save(made, by)


async def test_when_someone_replies_tell_me(node):
    wid = chase(node)
    await started(node, wid)
    row = from_template(node, "reply")
    assert row["missing"] == [] and row["trigger"]["lane"] == MAIL
    node.wf.enable(row["id"], OWNER)
    runs = await fired(node, row["id"], reply(f"Ana <{ANA}>", in_reply_to=sent_id(node, ANA)), ("stranger@example.com", "Buy this", "x", {}))
    told = [node.step(r, "tell")["status"] for r in sorted(runs, key=lambda r: r["steps"][0]["output"]["id"])]
    assert told == ["succeeded_unverified", "skipped"]


async def test_follow_up_once_template_chases_once_and_keeps_every_rule(node):
    row = from_template(node, "follow_up")
    assert row["missing"] == []
    run = node.wf.run(row["id"], OWNER, values={"subject": "Hi", "body": "First", "follow_up": "Second"})
    await node.settle()
    assert len(node.mail.sent) == 3
    await arrive(node, reply(CY, in_reply_to=sent_id(node, CY)))
    view = await after_the_wait(node, row["id"], run["id"])
    assert sorted(node.mail.to) == [ANA, BO] and view["status"] == "succeeded_unverified"
    assert all("unsubscribe-" in m["List-Unsubscribe"] and "To stop these messages" in m.get_content() for m in node.mail.sent)


@pytest.mark.parametrize("template", ["follow_up", "to_list"])
async def test_a_template_asks_where_a_person_said_ask_first(node, template):
    node.svc.ask_first.set("lane", MAIL, True, by="owner", person=True)
    row = from_template(node, template)
    run = node.wf.run(row["id"], OWNER, values={"subject": "Hi", "body": "First", "follow_up": "Second"})
    await node.settle()
    assert node.view(row["id"], run["id"])["status"] == "waiting_approval" and node.mail.sent == []


async def test_an_agents_words_in_a_template_ask_a_person(node):
    row = from_template(node, "to_list")
    agent = node.agent()
    view = await node.run(row["id"], agent, values={"subject": "Hi", "body": "Buy now"})
    assert view["status"] == "waiting_approval" and node.mail.sent == []


# ---- a follow-up keeps every anti-spam rule ----

async def test_a_follow_up_does_not_reach_someone_suppressed_since(node):
    wid = chase(node)
    run = await started(node, wid)
    node.wf.sends.suppress("biz-a", BO, by="unsubscribe")
    await after_the_wait(node, wid, run)
    assert sorted(node.mail.to) == [ANA, CY]


async def test_a_follow_up_carries_an_unsubscribe_on_every_message(node):
    wid = chase(node)
    await after_the_wait(node, wid, await started(node, wid))
    assert len(node.mail.sent) == 3
    assert all("unsubscribe-" in m["List-Unsubscribe"] and "To stop these messages" in m.get_content() for m in node.mail.sent)


async def test_a_follow_up_spends_the_same_daily_limit_and_waits_for_tomorrow(node):
    node.wf.sends.set_limit(MAIL, 3, person=True, by="owner")
    wid = chase(node)
    run = await started(node, wid)
    day = (node.h.clock.now() + timedelta(seconds=THREE_DAYS)).date().isoformat()
    for i in range(3):
        node.wf.sends.store.ledger_mark("other-run", "x", f"{i}@example.com", MAIL, "sent", day, node.h.clock.now())
    view = await after_the_wait(node, wid, run)
    assert node.mail.sent == [] and view["status"] not in ("succeeded_unverified", "failed")
    node.h.clock.advance(timedelta(days=1).total_seconds())
    await node.settle()
    assert sorted(node.mail.to) == PEOPLE


async def test_a_follow_up_goes_only_to_those_the_first_reached_and_still_only_to_the_list(node):
    wid = chase(node)
    run = await started(node, wid)
    node.wf.sends.save_list(LIST, workspace="biz-a", name="Customers", people=[ANA, "new@example.com"], person=True, by="owner")
    await after_the_wait(node, wid, run)
    assert node.mail.to == [ANA]  # Bo left the list, and whoever joined never had a first message


async def test_a_follow_up_is_recorded_in_the_ledger_and_is_never_repeated(node):
    wid = chase(node)
    run = await started(node, wid)
    await after_the_wait(node, wid, run)
    rows = node.wf.sends.store.ledger_rows(run, "again")
    assert sorted(r["address"] for r in rows) == PEOPLE and {r["state"] for r in rows} == {"sent"}
    await node.settle()
    assert len(node.mail.sent) == 3


@pytest.mark.parametrize("extra", [{"daily_limit": 9999}, {"unsubscribe": False}, {"list": LIST}, {"to": ANA}])
def test_a_stop_is_no_way_past_a_rule_because_it_has_no_field_for_one(node, extra):
    with pytest.raises(InvalidRequest):
        node.wf.save(workflow(action("first", SEND, inputs=FIRST, send={"list": LIST}), {**stop(), **extra}), OWNER)
