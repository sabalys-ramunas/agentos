"""A reply in a real thread is a conversation: no `use_a_list`, no unsubscribe footer, and the node decides what a reply is."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest
from test_mail_tools import STRANGER, Ctx, Transport, mail
from test_mail_unreadable import nested
from workflow_support import LIST, MAIL, OWNER, action, make_node, workflow

from agentos.runner.mail import outbox
from agentos.runner.mail import tools as mail_tools
from agentos.runner.mail.outbox import MailDailyLimit, MailInDoubt, MailSuppressed, UseAList
from agentos.runner.workflows.mail import MessageGone, parse_message


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.wf.mail.transport = Transport()
    n.mail = n.wf.mail.transport
    n.wf.sends.timezone = lambda: "UTC"
    n.h.clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    return n


async def send(node, to=STRANGER, body="A line", **kw):
    return await mail_tools.mail_send(Ctx(node), mailbox=MAIL, to=to, subject="Re: hello", body=body, **kw)


def spent(node):
    return node.wf.sends.store.sent_on(MAIL, node.wf.sends.day())


def footer(message):
    return "To stop these messages" in message.get_content() or message["List-Unsubscribe"] is not None


# ---- A: use_a_list ----

async def test_answering_someone_again_and_again_is_a_conversation(node):
    node.mail.inbox.append(mail())
    for i in range(20):
        await send(node, body=f"Answer {i}", reply_to_id="7")
    assert len(node.mail.sent) == 20 and spent(node) == 20


async def test_replies_do_not_use_up_the_ten_addresses_of_the_day(node):
    for i in range(12):
        node.mail.inbox.append(mail(uid=i + 1, sender=f"p{i}@example.org"))
        await send(node, to=f"p{i}@example.org", reply_to_id=str(i + 1))
    await send(node, to="fresh@example.org")
    assert len(node.mail.sent) == 13


async def test_a_reply_after_a_first_message_the_same_day_goes(node):
    await send(node)
    node.mail.inbox.append(mail())
    await send(node, reply_to_id="7")
    assert len(node.mail.sent) == 2


async def test_a_reply_does_not_open_the_way_for_a_cold_message(node):
    node.mail.inbox.append(mail())
    await send(node, reply_to_id="7")
    await send(node)  # the first message outside a thread today
    with pytest.raises(UseAList):
        await send(node)


async def test_a_reply_still_counts_toward_the_daily_limit(node):
    node.wf.sends.set_limit(MAIL, 2, person=True, by="owner")
    node.mail.inbox.append(mail())
    await send(node, body="one", reply_to_id="7")
    await send(node, body="two", reply_to_id="7")
    with pytest.raises(MailDailyLimit):
        await send(node, body="three", reply_to_id="7")
    assert len(node.mail.sent) == 2


async def test_suppression_still_applies_to_a_reply(node):
    node.mail.inbox.append(mail())
    node.wf.sends.suppress("biz-a", STRANGER, by="unsubscribe")
    with pytest.raises(MailSuppressed):
        await send(node, reply_to_id="7")
    assert node.mail.sent == []


async def test_a_reply_that_may_have_left_is_not_repeated(node):
    node.mail.inbox.append(mail())
    node.mail.lose = {STRANGER}
    with pytest.raises(MailInDoubt):
        await send(node, reply_to_id="7")
    node.mail.lose = set()
    with pytest.raises(MailInDoubt):
        await send(node, reply_to_id="7")
    assert len(node.mail.sent) == 1 and spent(node) == 1


async def test_a_reply_that_certainly_did_not_leave_can_go_again(node):
    node.mail.inbox.append(mail())
    node.mail.refuse = {STRANGER}
    with pytest.raises(outbox.MailNotSent):
        await send(node, reply_to_id="7")
    node.mail.refuse = set()
    await send(node, reply_to_id="7")
    assert len(node.mail.sent) == 1 and spent(node) == 1


# ---- what is a reply ----

NOT_A_REPLY = {
    "someone else wrote it": {"sender": "other@example.org"},
    "two senders": {"sender": f"{STRANGER}, other@example.org"},
    "no sender": {"sender": ""},
    "no message id": {"message_id": ""},
    "an id that is no id": {"message_id": "junk"},
    "a machine wrote it": {"auto_reply": True},
}


@pytest.mark.parametrize("why", NOT_A_REPLY)
async def test_what_is_not_a_reply_counts_as_before(node, why):
    node.mail.inbox.append(mail(**NOT_A_REPLY[why]))
    await send(node, reply_to_id="7")
    assert footer(node.mail.sent[0])
    with pytest.raises(UseAList):
        await send(node, body="again", reply_to_id="7")


async def test_a_tagged_recipient_is_not_the_exact_sender(node):
    node.mail.inbox.append(mail())
    await send(node, to="someone+x@example.org", reply_to_id="7")
    assert footer(node.mail.sent[0])


async def test_a_message_that_could_not_be_read_is_not_a_reply(node):
    stub = parse_message(7, nested(5000).replace(b"eve@example.org", STRANGER.encode()))
    node.mail.inbox.append(stub)
    await send(node, reply_to_id="7")
    assert footer(node.mail.sent[0])


async def test_a_message_that_is_not_in_the_mailbox_is_no_reply(node):
    with pytest.raises(MessageGone):
        await send(node, reply_to_id="99")
    assert node.mail.sent == []


async def test_the_name_beside_an_address_changes_nothing(node):
    node.mail.inbox.append(mail(sender=f"Someone Real <{STRANGER}>"))
    await send(node, reply_to_id="7")
    assert not footer(node.mail.sent[0])


def test_an_agent_has_no_flag_to_set():
    assert set(inspect.signature(mail_tools.mail_send).parameters) == {"ctx", "mailbox", "to", "subject", "body", "reply_to_id"}


# ---- B: the footer ----

async def test_a_reply_to_someone_who_wrote_first_carries_no_unsubscribe(node):
    node.mail.inbox.append(mail())
    await send(node, reply_to_id="7")
    m = node.mail.sent[0]
    assert not footer(m) and m["List-Unsubscribe-Post"] is None and m.get_content().rstrip() == "A line"
    assert m["In-Reply-To"] == "<m1@example.org>"


async def test_a_message_to_someone_who_did_not_write_first_keeps_it(node):
    await send(node)
    assert footer(node.mail.sent[0])


async def test_a_list_send_always_keeps_it_even_to_someone_who_wrote(node):
    node.mail.inbox.append(mail())
    wid = node.wf.save(workflow(action("send", f"mail.send@{MAIL}", inputs={"subject": "S", "body": "B"}, send={"list": LIST})), OWNER)["id"]
    await node.run(wid)
    assert node.mail.sent and all(footer(m) for m in node.mail.sent)


async def test_the_answer_says_it_went_as_a_reply(node):
    node.mail.inbox.append(mail())
    assert (await send(node, reply_to_id="7"))["reply"] is True
    assert (await send(node, to="fresh@example.org"))["reply"] is False
