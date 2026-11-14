"""One canonical address: what is looked up, counted, compared, stored and sent is the same string."""

from __future__ import annotations

import smtplib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_mail_tools import Ctx, Transport
from workflow_support import LIST, MAIL, OWNER, action, make_node, workflow

from agentos.core.errors import InvalidRequest
from agentos.runner.mail import outbox
from agentos.runner.mail import tools as mail_tools
from agentos.runner.mail.outbox import MailSuppressed, UseAList
from agentos.runner.workflows.mail import SendInDoubt, SendRefused, TlsTransport
from agentos.runner.workflows.replies import Replies
from agentos.runner.workflows.sends import base_address, normal_address

WS = "biz-a"
JANE = "jane@acme.example"


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.wf.mail.transport = Transport()
    n.mail = n.wf.mail.transport
    n.wf.sends.timezone = lambda: "UTC"
    n.h.clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    return n


async def send(node, to):
    return await mail_tools.mail_send(Ctx(node), mailbox=MAIL, to=to, subject="Hi", body="A line")


@pytest.mark.parametrize("bad", [
    "(x)jane@acme.example", "jane(x)@acme.example", "jane@(x)acme.example", "Jane <jane@acme.example>", "<jane@acme.example>",
    '"jane"@acme.example', '"j a"@acme.example', "@a.example,@b.example:jane@acme.example", "@a.example:jane@acme.example",
    "a%b@acme.example", "a!b@acme.example", "jane @acme.example", " jane@acme.example", "jane@acme.example ", "jane@acme.example\n",
    "ja\tne@acme.example", "j\x01ane@acme.example", "jane\x00@acme.example", "a\\b@acme.example", "[x]@acme.example",
    "jane@[1.2.3.4]", "jane@1.2.3.4", "jane@localhost", ".jane@acme.example", "jane.@acme.example", "ja..ne@acme.example",
    "jané@acme.example", "Kate@acme.example", "a,b@acme.example", "a;b@acme.example", "jane@acme.example.", "jane@-acme.example",
    "a@b@acme.example", f"{'a' * 65}@acme.example", f"a@{'b' * 64}.example", "", "@acme.example", "jane@", None, 5, ["jane@acme.example"],
])
def test_anything_but_a_plain_address_is_refused(bad):
    assert normal_address(bad) is None


@pytest.mark.parametrize("given, canonical", [
    ("jane@acme.example", "jane@acme.example"),
    ("Jane@ACME.Example", "jane@acme.example"),
    ("jane+news@acme.example", "jane+news@acme.example"),
    ("jane@bücher.example", "jane@xn--bcher-kva.example"),
    ("jane@BÜCHER.example", "jane@xn--bcher-kva.example"),
    ("jane@пример.рф", "jane@xn--e1afmkfd.xn--p1ai"),
    ("jane.doe_1-x@sub.acme.example", "jane.doe_1-x@sub.acme.example"),
])
def test_one_form_for_one_address(given, canonical):
    assert normal_address(given) == canonical


@pytest.mark.parametrize("address, base", [
    ("jane+news@acme.example", JANE), ("jane+a+b@acme.example", JANE), (JANE, JANE), ("+x@acme.example", "+x@acme.example"),
])
def test_a_tag_is_left_off_when_two_addresses_are_compared(address, base):
    assert base_address(address) == base


# ---- suppression ----

@pytest.mark.parametrize("asked, sent", [(JANE, "jane+x@acme.example"), ("jane+x@acme.example", JANE),
                                         ("jane+x@acme.example", "jane+y@acme.example")])
async def test_a_tag_does_not_get_past_a_suppression(node, asked, sent):
    node.wf.sends.suppress(WS, asked, by="unsubscribe")
    with pytest.raises(MailSuppressed):
        await send(node, sent)
    assert node.mail.sent == []


async def test_a_suppression_kept_with_a_tag_before_this_still_holds(node):
    node.wf.sends.store._exec("INSERT INTO send_suppressed (workspace, address, by, why, at) VALUES (?, ?, 'x', NULL, '2026-01-01')",
                              (WS, "jane+old@acme.example"))
    from agentos.runner.workflows.sends import Sends

    Sends(node.wf.sends.store, node.wf.sends.clock)
    with pytest.raises(MailSuppressed):
        await send(node, JANE)


def test_lifting_a_suppression_lifts_it_for_the_tag_too(node):
    node.wf.sends.suppress(WS, "jane+x@acme.example", by="unsubscribe")
    assert node.wf.sends.unsuppress(WS, JANE, person=True)
    assert not node.wf.sends.store.is_suppressed(WS, "jane+x@acme.example")


def test_a_list_counts_a_tagged_person_as_suppressed(node):
    node.wf.sends.save_list("list.biz-a.tags", workspace=WS, name="Tags", people=["jane+x@acme.example", "bob@acme.example"],
                            person=True, by="owner")
    node.wf.sends.suppress(WS, JANE, by="unsubscribe")
    assert node.wf.sends.public_list("list.biz-a.tags")["suppressed"] == 1


async def test_a_list_send_skips_a_tagged_person_who_is_suppressed(node):
    lid = "list.biz-a.tags"
    node.wf.sends.save_list(lid, workspace=WS, name="Tags", people=["jane+x@acme.example"], person=True, by="owner")
    node.wf.sends.suppress(WS, JANE, by="unsubscribe")
    wid = node.wf.save(workflow(action("send", f"mail.send@{MAIL}", inputs={"subject": "S", "body": "B"}, send={"list": lid})), OWNER)["id"]
    await node.run(wid)
    assert node.mail.sent == []


# ---- the ledger and the daily rules ----

async def test_a_tag_does_not_get_past_once_a_day(node):
    await send(node, JANE)
    with pytest.raises(UseAList):
        await send(node, "jane+again@acme.example")
    assert len(node.mail.sent) == 1


async def test_tags_do_not_make_more_addresses_of_the_day(node):
    for i in range(outbox.MAX_DIRECT_A_DAY):
        await send(node, f"p{i}@example.org")
    with pytest.raises(UseAList):
        await send(node, "p0+new@example.org")
    assert len(node.mail.sent) == outbox.MAX_DIRECT_A_DAY


async def test_the_slot_is_claimed_for_the_person_and_the_mail_goes_to_what_was_given(node):
    await send(node, "jane+x@acme.example")
    day = node.wf.sends.day()
    assert node.wf.sends.store.ledger_get(outbox.direct_run(day), MAIL, JANE) == "sent"
    assert node.mail.rcpt == ["jane+x@acme.example"] and node.mail.to == ["jane+x@acme.example"]


def test_a_list_supplied_twice_under_a_tag_holds_the_person_once(node):
    out = node.wf.sends.save_list("list.biz-a.twice", workspace=WS, name="Twice", person=True, by="owner",
                                  people=[JANE, "jane+x@acme.example", "JANE@acme.example"])
    assert out["size"] == 1


def test_a_list_read_from_a_file_may_carry_spaces_round_an_address(node):
    out = node.wf.sends.save_list("list.biz-a.csv", workspace=WS, name="Csv", person=True, by="owner", people=[f" {JANE}\n"])
    assert out["size"] == 1 and out["refused"] == 0


# ---- the unsubscribe token ----

async def test_the_token_in_a_tagged_message_stops_the_person(node):
    await send(node, "jane+x@acme.example")
    token = node.wf.sends.unsubscribe_for(WS, "jane+x@acme.example", mailbox="hello@biz-a.example").token
    assert node.wf.sends.unsubscribe(token)
    for address in (JANE, "jane+x@acme.example", "jane+other@acme.example"):
        assert node.wf.sends.store.is_suppressed(WS, address)


def test_one_token_for_one_person(node):
    a = node.wf.sends.unsubscribe_for(WS, JANE, mailbox="hello@biz-a.example").token
    b = node.wf.sends.unsubscribe_for(WS, "jane+x@acme.example", mailbox="hello@biz-a.example").token
    assert a == b


# ---- what is sent ----

@pytest.mark.parametrize("to", ["(x)jane@acme.example", "jane(x)@acme.example", "jane%x@acme.example"])
async def test_a_comment_never_reaches_a_send(node, to):
    with pytest.raises(InvalidRequest):
        await send(node, to)
    assert node.mail.sent == []


async def test_a_row_stored_before_this_is_not_handed_to_a_server(node):
    lid = "list.biz-a.old"
    node.wf.sends.save_list(lid, workspace=WS, name="Old", people=[JANE], person=True, by="owner")
    node.wf.sends.store._exec("UPDATE send_people SET address = ? WHERE list_id = ?", ("(x)jane@acme.example", lid))
    wid = node.wf.save(workflow(action("send", f"mail.send@{MAIL}", inputs={"subject": "S", "body": "B"}, send={"list": lid})), OWNER)["id"]
    await node.run(wid)
    assert node.mail.sent == []


async def test_a_list_send_names_each_recipient_outright(node):
    wid = node.wf.save(workflow(action("send", f"mail.send@{MAIL}", inputs={"subject": "S", "body": "B"}, send={"list": LIST})), OWNER)["id"]
    await node.run(wid)
    assert sorted(node.mail.rcpt) == sorted(node.mail.to) and node.mail.rcpt


class Smtp:
    """What a server is told, in order."""

    def __init__(self, replies=()):
        self.said, self.replies = [], dict(replies)

    def docmd(self, cmd, args=""):
        self.said.append(f"{cmd} {args}".strip())
        return self.replies.get(cmd, (354 if cmd == "DATA" else 250, b"ok"))

    def send(self, raw):
        self.said.append(raw)

    def getreply(self):
        return self.replies.get("END", (250, b"queued"))


def hand_over(smtp, to=JANE, raw=b"Subject: x\r\n\r\nhi\r\n"):
    box = SimpleNamespace(address="hello@biz-a.example")
    TlsTransport._hand_over(smtp, box, to, raw)


def test_the_server_is_told_the_canonical_address_and_no_header_is_read():
    smtp = Smtp()
    hand_over(smtp, raw=b"To: (x)someone@else.example\r\nBcc: other@else.example\r\n\r\nhi\r\n")
    assert smtp.said[:3] == ["MAIL FROM:<hello@biz-a.example>", f"RCPT TO:<{JANE}>", "DATA"]
    assert sum(1 for line in smtp.said if isinstance(line, str) and line.startswith("RCPT")) == 1


def test_a_line_that_starts_with_a_dot_is_doubled_and_the_message_ends():
    smtp = Smtp()
    hand_over(smtp, raw=b"Subject: x\r\n\r\n.hidden\r\nline")
    assert smtp.said[-1] == b"Subject: x\r\n\r\n..hidden\r\nline\r\n.\r\n"


@pytest.mark.parametrize("cmd, code", [("MAIL", 451), ("MAIL", 550), ("RCPT", 450), ("RCPT", 550), ("DATA", 451), ("DATA", 554)])
def test_anything_before_the_message_certainly_did_not_send_it(cmd, code):
    with pytest.raises(SendRefused):
        hand_over(Smtp({cmd: (code, b"no")}))


def test_a_connection_lost_before_the_message_certainly_did_not_send_it():
    smtp = Smtp()

    def lost(cmd, args=""):
        raise smtplib.SMTPServerDisconnected("gone")

    smtp.docmd = lost
    with pytest.raises(SendRefused):
        hand_over(smtp)


def test_after_the_message_only_a_refusal_is_certain():
    with pytest.raises(SendRefused):
        hand_over(Smtp({"END": (554, b"no")}))
    with pytest.raises(SendInDoubt):
        hand_over(Smtp({"END": (451, b"later")}))
    smtp = Smtp()

    def lost():
        raise smtplib.SMTPServerDisconnected("gone")

    smtp.getreply = lost
    with pytest.raises(SendInDoubt):
        hand_over(smtp)


# ---- who answered ----

def test_an_answer_from_the_untagged_address_is_the_same_person(node):
    replies = Replies(node.wf.store, node.wf.sends.clock)
    box = node.wf.mail.require(MAIL)
    now = node.wf.sends.clock.now()
    node.wf.store.thread_put("<sent@biz-a.example>", "run-1", "send", "jane+news@acme.example", box.id, now)
    message = type("M", (), {"in_reply_to": "<sent@biz-a.example>", "references": "", "sender": f"Jane <{JANE}>", "auto_reply": False})
    assert replies.note(box, message) == "run-1"
