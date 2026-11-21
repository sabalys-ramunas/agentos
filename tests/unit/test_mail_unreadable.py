"""No single message stops a mailbox: what cannot be read is a stub, the cursor moves on, and it counts once."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_mail_tools import Ctx, Transport
from testkit.timing import budget
from workflow_support import MAIL, make_node

from openajar.runner.mail import message as parsing
from openajar.runner.mail import session
from openajar.runner.mail import tools as mail_tools
from openajar.runner.mail.cursor import Cursor, MailCursors, Seen, _sent_on, compose, is_new
from openajar.runner.workflows.mail import Mailbox, Received, TlsTransport, parse_message

BOX = Mailbox(id="biz-a/mail", address="hello@biz-a.example", imap_host="imap.invalid", smtp_host="smtp.invalid",
              username="hello@biz-a.example", secret_ref="mail/biz-a/mail")


def nested(depth: int, *, kind: str = "multipart/mixed") -> bytes:
    out = b"From: Eve <eve@example.org>\r\nSubject: deep\r\nMessage-ID: <deep@example.org>\r\nDate: Mon, 2 Nov 2026 10:00:00 +0000\r\n"
    for i in range(depth):
        out += f'Content-Type: {kind}; boundary="b{i}"\r\n\r\n--b{i}\r\n'.encode()
    return out + b"Content-Type: text/plain\r\n\r\nhi\r\n"


def folded(depth: int) -> bytes:
    """The same, with each header folded, so nothing that reads lines sees `Content-Type: multipart`."""
    return nested(depth).replace(b"Content-Type: multipart/mixed;", b"Content-Type:\r\n multipart/mixed;")


GOOD = b"From: a@example.org\r\nSubject: fine\r\nMessage-ID: <fine@example.org>\r\n\r\nhello\r\n"


# ---- the parse ----

@pytest.mark.parametrize("depth", [sys.getrecursionlimit() + 100, 5000])
@pytest.mark.parametrize("make", [nested, folded, lambda d: nested(d, kind="message/rfc822")])
async def test_nesting_past_the_real_limit_is_a_stub_even_on_a_worker_thread(depth, make):
    p = await asyncio.to_thread(parsing.parse, make(depth))
    assert p.unreadable and p.body == "" and p.attachments == ()


def test_what_the_headers_said_is_kept_on_the_stub():
    p = parsing.parse(nested(5000))
    assert p.unreadable and p.subject == "deep" and "eve@example.org" in p.sender and p.message_id == "<deep@example.org>"


def test_nesting_a_real_message_has_is_read():
    p = parsing.parse(nested(8))
    assert not p.unreadable and p.body.strip() == "hi"


def test_a_parser_that_blows_up_is_a_stub(monkeypatch):
    def boom(*_, **__):
        raise OverflowError("anything at all")

    monkeypatch.setattr(parsing.email, "message_from_bytes", boom)
    assert parsing.parse(GOOD).unreadable


SLOW = {"a parameter list": b"Content-Type: multipart/mixed; " + b"a=b;" * 400_000 + b"\r\n\r\nx",
        "a long To": b"To: " + b"a@b," * 400_000 + b"\r\n\r\nx",
        "quoted words": b"From: " + b'"a" ' * 400_000 + b"\r\n\r\nx",
        "a part's header": b'Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\nContent-Type: text/plain; ' + b"a=b;" * 400_000 + b"\r\n\r\nx\r\n--b--\r\n"}


@pytest.mark.parametrize("name", SLOW)
async def test_a_message_that_is_slow_to_parse_on_purpose_runs_out_of_time(name, monkeypatch):
    monkeypatch.setattr(parsing, "PARSE_SECONDS", 0.2)
    started = time.monotonic()
    p = await asyncio.wait_for(asyncio.to_thread(parsing.parse, SLOW[name]), 30)
    assert p.unreadable and p.body == "" and time.monotonic() - started < 5


def test_a_garbage_collector_callback_past_the_limit_does_not_end_the_limit(monkeypatch):
    """A weakref callback that runs in the middle of a parse (the garbage collector) cannot raise: what it
    raises is printed and dropped, and the profile hook with it. Seen in a full test run: the limit was
    lost there and a parse ran on past 30 s. The limit waits for the parser's next call instead."""
    import email.utils
    import weakref

    class Thing:
        pass

    def slow_parse(_raw, **_kw):
        time.sleep(0.15)  # past the limit
        thing = Thing()
        ref = weakref.ref(thing, lambda _r: None)  # the callback runs when `thing` goes, here
        del thing
        assert ref() is None
        deadline = time.monotonic() + 20  # far past the bound below, even scaled for a slow runner
        while time.monotonic() < deadline:  # the parser's own work: the limit must stop it
            email.utils.parseaddr("a@b.example")
        return email.message_from_string("Subject: read\r\n\r\nall of it")

    monkeypatch.setattr(parsing, "PARSE_SECONDS", 0.1)
    monkeypatch.setattr(parsing.email, "message_from_bytes", slow_parse)
    started = time.monotonic()
    p = parsing.parse(GOOD)
    assert p.unreadable and time.monotonic() - started < budget(2)


def test_the_time_limit_leaves_nothing_behind_on_the_thread():
    before = sys.getprofile()
    parsing.parse(GOOD)
    assert sys.getprofile() is before


@pytest.mark.parametrize("charset", ["x\x00y", "utf-99", "zlib", "base64", "rot13", "unicode_escape", "a" * 5000, "", "idna", "utf-7"])
def test_a_hostile_charset_never_stops_a_message(charset):
    raw = f'Subject: =?{charset}?B?aGk=?=\r\nContent-Type: text/plain; charset="{charset}"\r\n\r\nhello \xff\r\n'.encode("latin-1")
    p = parsing.parse(raw)
    assert isinstance(p.body, str) and isinstance(p.subject, str)


def test_bytes_that_are_no_charset_can_be_stored_and_sent_on():
    p = parsing.parse(b"From: \xff\xfe <a@example.org>\r\nSubject: \xed\xa0\x80 caf\xe9\r\n\r\nx\r\n")
    json.dumps([p.sender, p.subject]).encode("utf-8")
    p.subject.encode("utf-8")
    p.sender.encode("utf-8")


# ---- the dates ----

@pytest.mark.parametrize("date", ["1 Jan 99999999999 00:00:00 +0000", "Fri, 31 Dec 9999 23:59:59 -2359", "Mon, 1 Jan 0001 00:00:00 +2359",
                                  "Thu, 31 Dec 9999 23:59:59 -9999", "99 Foo 1 99:99:99", "\x00", "", "0 0 0 0 0"])
def test_a_hostile_date_is_no_date(date):
    when = _sent_on(date)
    assert when is None or when.tzinfo is not None


@pytest.mark.parametrize("date", ["1 Jan 99999999999 00:00:00 +0000", "Fri, 31 Dec 9999 23:59:59 -2359"])
def test_a_hostile_date_does_not_stop_the_check_after_a_changed_validity(date):
    cursor = Cursor(validity=2, watermark=datetime(2026, 11, 1, tzinfo=UTC).isoformat())
    assert is_new(cursor, Seen("f", None, date))


def test_a_hostile_arrival_time_from_a_server_is_no_time():
    assert session.internal_date(b'INTERNALDATE "31-Dec-9999 23:59:59 -2359"') is None


# ---- the poll ----

class Imap:
    def __init__(self, messages):
        self.messages = messages  # uid -> raw


class Polled(TlsTransport):
    def __init__(self, messages):
        super().__init__(cursors=MailCursors())
        self.imap = Imap(messages)

    @contextlib.contextmanager
    def _imap(self, box, password, *_, **__):
        yield self.imap


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(session, "select", lambda imap, *a, **k: session.Selected(len(imap.messages), 7, max(imap.messages) + 1))
    monkeypatch.setattr(session, "search", lambda imap, *a: sorted(imap.messages))
    monkeypatch.setattr(session, "fetch_one", lambda imap, uid, cap=0: session.Fetched(uid, imap.messages[uid], False, None, len(imap.messages[uid])))
    monkeypatch.setattr(session, "newest", lambda imap, *a, **k: sorted(imap.messages))
    monkeypatch.setattr(session, "fetch_headers", lambda imap, uids: [(u, None, imap.messages[u]) for u in uids])


async def test_the_cursor_moves_past_what_cannot_be_read_and_what_follows_it_arrives(server):
    t = Polled({1: GOOD, 2: nested(5000), 3: GOOD.replace(b"fine", b"after")})
    start = await t.fetch(BOX, "pw", None)
    t.imap.messages = {4: nested(5000), 5: GOOD.replace(b"fine", b"after")}
    got, highest = await t.fetch(BOX, "pw", start[1])
    assert [m.unreadable for m in got] == [True, False] and got[1].subject == "after" and highest == compose(0, 5)


async def test_a_new_poll_can_start_on_a_mailbox_with_such_a_message(server):
    assert (await Polled({1: nested(5000)}).fetch(BOX, "pw", None))[0] == []


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.wf.mail.transport = Transport()
    n.mail = n.wf.mail.transport
    return n


def stub(uid=9):
    return parse_message(uid, nested(5000))


async def test_a_stub_starts_nothing_and_counts_once_in_the_health(node):
    box = node.wf.mail.require(MAIL)
    node.mail.inbox.extend([stub(9), Received(10, "a@example.org", "fine", "x")])
    got, highest = await node.wf.mail.fetch(box, 8)
    assert [m.uid for m in got] == [10] and highest == 10
    assert node.wf.mail.health(box.id)["unreadable"] == 1
    await node.wf.mail.fetch(box, 8)  # the same poll again, as after a crash before the cursor was saved
    assert node.wf.mail.health(box.id)["unreadable"] == 1
    node.mail.inbox.append(stub(11))
    await node.wf.mail.fetch(box, 10)
    assert node.wf.mail.health(box.id)["unreadable"] == 2 and node.wf.mail.view(box)["health"]["ok"]


async def test_a_mailbox_with_no_such_message_says_nothing_about_it(node):
    box = node.wf.mail.require(MAIL)
    await node.wf.mail.fetch(box, 0)
    assert "unreadable" not in node.wf.mail.health(box.id)


async def test_the_count_survives_a_change_in_health_and_goes_with_the_mailbox(node):
    box = node.wf.mail.require(MAIL)
    node.mail.inbox.append(stub(9))
    await node.wf.mail.fetch(box, 8)
    node.wf.mail.note_health(box.id, False, "gone")
    assert node.wf.mail.health(box.id)["unreadable"] == 1
    await node.wf.mail.remove(box.id, person=True)
    assert node.wf.mail.health(box.id) is None
    assert not node.wf.store._rows("SELECT 1 FROM meta WHERE key LIKE '%unreadable%'")


async def test_an_agent_sees_a_stub_marked_with_no_body(node):
    node.mail.inbox.append(stub(9))
    out = await mail_tools.mail_list(Ctx(node), mailbox=MAIL)
    assert out["messages"][0]["unreadable"] is True and out["messages"][0]["clip"] == ""
    assert (await mail_tools.mail_read(Ctx(node), mailbox=MAIL, id="9"))["message"]["unreadable"] is True


def test_a_stub_is_a_plain_received():
    assert SimpleNamespace(unreadable=False).unreadable is False and not Received(1, "", "", "").unreadable
