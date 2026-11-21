"""A hostile server costs one poll: deadlines a trickle cannot push back, and caps on everything it can send."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from datetime import UTC, datetime

import pytest
from workflow_support import MAIL, make_node

from openajar.runner.mail import net, session, wire
from openajar.runner.mail.cursor import MailCursors, compose
from openajar.runner.mail.session import MailFailed
from openajar.runner.workflows.mail import Mailbox, Received, TlsTransport

BOX = Mailbox(id="biz-a/mail", address="hello@biz-a.example", imap_host="127.0.0.1", smtp_host="127.0.0.1",
              username="hello@biz-a.example", secret_ref="r", private_hosts=["127.0.0.1"])
GREETING = b"* OK ready\r\n"


class Server:
    """Speaks whatever `fn` says, on a port of its own."""

    def __init__(self, fn):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.fn = fn
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        with contextlib.suppress(OSError):
            while True:
                client, _ = self.listener.accept()
                threading.Thread(target=self._one, args=(client,), daemon=True).start()

    def _one(self, client):
        with contextlib.suppress(OSError), client:
            self.fn(client)

    def close(self):
        self.listener.close()


@pytest.fixture
def server():
    made = []

    def start(fn):
        made.append(Server(fn))
        return made[-1]

    yield start
    for s in made:
        s.close()


# A bound that tells "the deadline ended it" from "nothing did": the trickles below take 28 s or more
# to finish and the silent server waits 30 s, so 5 s is far from both and from a 0.4 s deadline on a
# loaded CI runner (a bound of 2 s failed there under load).
ENDED_BY_DEADLINE_S = 5


def trickle(text):
    def fn(client):
        for i in range(len(text)):
            client.sendall(text[i:i + 1])
            time.sleep(0.02)
    return fn


def forever(first, line):
    def fn(client):
        client.sendall(first)
        while True:
            client.sendall(line * 200)
    return fn


def imap_says(*answers):
    """A greeting, then for each command what is in `answers`, with the command's own tag put in."""
    def fn(client):
        client.sendall(GREETING)
        file = client.makefile("rb")
        for answer in answers:
            line = file.readline()
            client.sendall(answer.replace(b"TAG", line.split(b" ")[0]))
        time.sleep(5)
    return fn


CAPABILITY = b"* CAPABILITY IMAP4rev1\r\nTAG OK done\r\n"


def timed(fn, *args, **kw):
    started = time.monotonic()
    with pytest.raises(MailFailed) as e:
        fn(*args, **kw)
    return e.value.kind, time.monotonic() - started


# ---- the deadline ----

def test_a_deadline_does_not_move():
    now = [0.0]
    d = wire.Deadline(10, clock=lambda: now[0])
    assert d.left() == 10 and not d.past_half()
    now[0] = 6
    assert d.left() == 4 and d.past_half()
    now[0] = 10
    with pytest.raises(MailFailed) as e:
        d.left()
    assert e.value.kind == "too_slow"


def test_a_trickling_imap_greeting_ends_at_the_deadline(server):
    s = server(trickle(b"* OK this greeting goes on and on, a byte at a time, and never ends " * 20))
    kind, took = timed(session.Imap, "127.0.0.1", s.port, timeout=5, deadline=wire.Deadline(0.4))
    assert kind == "too_slow" and took < ENDED_BY_DEADLINE_S


def test_a_trickling_smtp_greeting_ends_at_the_deadline(server):
    s = server(trickle(b"220-this greeting goes on and on, a byte at a time\r\n" * 40))
    kind, took = timed(session.Smtp, "127.0.0.1", s.port, timeout=5, deadline=wire.Deadline(0.4))
    assert kind == "too_slow" and took < ENDED_BY_DEADLINE_S


def test_a_server_that_says_nothing_ends_at_the_deadline(server):
    s = server(lambda client: time.sleep(30))
    kind, took = timed(session.Imap, "127.0.0.1", s.port, timeout=60, deadline=wire.Deadline(0.3))
    assert kind == "too_slow" and took < ENDED_BY_DEADLINE_S


def test_a_deadline_already_gone_opens_nothing(server):
    now = [0.0]
    d = wire.Deadline(1, clock=lambda: now[0])
    now[0] = 2
    s = server(lambda client: client.sendall(GREETING))
    assert timed(session.Imap, "127.0.0.1", s.port, timeout=5, deadline=d)[0] == "too_slow"


# ---- the caps ----

def test_an_endless_line_fails_this_connection(server, monkeypatch):
    monkeypatch.setattr(session, "MAX_LINE", 4096)
    s = server(forever(b"* OK ", b"x" * 100))
    assert timed(session.Imap, "127.0.0.1", s.port, timeout=5, deadline=wire.Deadline(5))[0] == "too_much"


def test_too_many_untagged_lines_fail_this_connection(server, monkeypatch):
    """Few bytes, so only the count of lines can end it."""
    monkeypatch.setattr(session, "MAX_UNTAGGED", 100)

    def fn(client):
        client.sendall(GREETING + b"* 1 EXISTS\r\n" * 150)
        time.sleep(5)

    s = server(fn)
    assert timed(session.Imap, "127.0.0.1", s.port, timeout=5, deadline=wire.Deadline(2))[0] == "too_much"


def test_an_endless_answer_ends_at_what_one_answer_may_hold(server):
    s = server(forever(GREETING, b"* 1 EXISTS\r\n"))
    assert timed(session.Imap, "127.0.0.1", s.port, timeout=5, deadline=wire.Deadline(5))[0] == "too_much"


def test_a_literal_past_the_cap_is_refused_before_it_is_read(server):
    s = server(imap_says(CAPABILITY, b"* 1 FETCH (BODY[] {99999999999}\r\nx"))
    imap = session.Imap("127.0.0.1", s.port, timeout=5, deadline=wire.Deadline(5))
    with pytest.raises(MailFailed) as e:
        imap.noop()
    assert e.value.kind == "too_much"


def test_many_literals_under_the_cap_still_end_at_what_one_answer_may_hold(server, monkeypatch):
    monkeypatch.setattr(session, "MAX_RESPONSE", 50_000)
    one = b"* 1 FETCH (BODY[] {1000}\r\n" + b"x" * 1000 + b")\r\n"
    s = server(imap_says(CAPABILITY, one * 500 + b"TAG OK done\r\n"))
    imap = session.Imap("127.0.0.1", s.port, timeout=5, deadline=wire.Deadline(5))
    with pytest.raises(MailFailed) as e:
        imap.noop()
    assert e.value.kind == "too_much"


def test_the_caps_start_again_for_each_command(server, monkeypatch):
    monkeypatch.setattr(session, "MAX_UNTAGGED", 5)
    s = server(imap_says(CAPABILITY, b"* 1 EXISTS\r\n* 2 EXISTS\r\nTAG OK done\r\n", b"* 3 EXISTS\r\n* 4 EXISTS\r\nTAG OK done\r\n",
                         b"* 5 EXISTS\r\n* 6 EXISTS\r\nTAG OK done\r\n"))
    imap = session.Imap("127.0.0.1", s.port, timeout=5, deadline=wire.Deadline(5))
    for _ in range(3):
        assert imap.noop()[0] == "OK"


def test_an_endless_smtp_reply_fails_this_connection(server):
    s = server(forever(b"", b"250-more\r\n"))
    assert timed(session.Smtp, "127.0.0.1", s.port, timeout=5, deadline=wire.Deadline(5))[0] == "too_much"


def test_a_long_smtp_line_fails_this_connection(server):
    s = server(forever(b"220 ", b"x" * 1000))
    assert timed(session.Smtp, "127.0.0.1", s.port, timeout=5, deadline=wire.Deadline(5))[0] == "too_much"


def test_a_plain_smtp_conversation_is_read_as_before(server):
    def fn(client):
        client.sendall(b"220 hello\r\n")
        file = client.makefile("rb")
        file.readline()
        client.sendall(b"250-mail.example\r\n250-SIZE 100\r\n250 AUTH PLAIN\r\n")
        time.sleep(1)

    s = server(fn)
    smtp = session.Smtp("127.0.0.1", s.port, timeout=5, deadline=wire.Deadline(5), local_hostname="[127.0.0.1]")
    assert smtp.ehlo()[0] == 250 and smtp.has_extn("size") and smtp.has_extn("auth")
    smtp.close()


def test_every_word_for_a_failure_is_our_own():
    assert "took too long" in session.explain("imap", "h", "too_slow")
    assert "more than" in session.explain("imap", "h", "too_much")


# ---- the step 6 probe ----

@pytest.mark.parametrize("port_of", [143, 587])
def test_the_starttls_probe_ends_at_its_deadline(server, monkeypatch, port_of):
    s = server(trickle((b"* OK " if port_of == 143 else b"220-") + b"x" * 2000))
    real = socket.create_connection
    monkeypatch.setattr(net, "TIMEOUT", 0.4)
    monkeypatch.setattr(socket, "create_connection", lambda address, *a, **k: real(("127.0.0.1", s.port), *a, **k))
    started = time.monotonic()
    assert net._answers("127.0.0.1", port_of, "starttls", None) is False
    assert time.monotonic() - started < ENDED_BY_DEADLINE_S


# ---- the https fetch ----

class Plain:
    """No TLS to speak of, so the read itself is what is tested."""

    def wrap_socket(self, sock, server_hostname=None):
        return sock


def fetching(monkeypatch, port, *, deadline=0.5):
    monkeypatch.setattr(net, "DEADLINE", deadline)
    monkeypatch.setattr(net, "HTTPS_PORT", port)
    monkeypatch.setattr(net, "all_public", lambda addresses: True)


@pytest.mark.parametrize("text", [b"HTTP/1.1 200 OK\r\nX-A: " + b"y" * 2000, b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n" + b"z" * 2000])
def test_a_trickling_response_ends_inside_the_thread(server, monkeypatch, text):
    s = server(trickle(text))
    fetching(monkeypatch, s.port, deadline=0.4)
    started = time.monotonic()
    with pytest.raises(MailFailed):
        net._read("https://a.example/x", Plain(), lambda host: ["127.0.0.1"])
    assert time.monotonic() - started < ENDED_BY_DEADLINE_S


async def test_a_fetch_leaves_no_thread_behind(server, monkeypatch):
    s = server(trickle(b"HTTP/1.1 200 OK\r\nX-A: " + b"y" * 2000))
    fetching(monkeypatch, s.port, deadline=0.3)
    ended, real = [], net._read

    def read(*args):
        try:
            return real(*args)
        finally:
            ended.append(True)

    monkeypatch.setattr(net, "_read", read)
    assert await net.RealNet(context=Plain, resolver=lambda host: ["127.0.0.1"]).fetch("https://a.example/x") is None
    assert ended == [True]  # the read itself ended: nothing is left running after the fetch gave up


def test_every_address_is_tried(server, monkeypatch):
    s = server(lambda client: (client.recv(4096), client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")))
    fetching(monkeypatch, s.port, deadline=5)
    tried, real = [], net.socket.create_connection

    def connect(address, *a, **k):
        tried.append(address[0])
        if address[0] != "10.0.0.3":
            raise OSError("no route")
        return real(("127.0.0.1", s.port), *a, **k)

    monkeypatch.setattr(net.socket, "create_connection", connect)
    assert net._read("https://a.example/x", Plain(), lambda host: ["10.0.0.1", "10.0.0.2", "10.0.0.3"]) == b"ok"
    assert tried == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


def test_an_answer_from_one_address_is_the_answer(server, monkeypatch):
    s = server(lambda client: (client.recv(4096), client.sendall(b"HTTP/1.1 404 Nope\r\nContent-Length: 0\r\n\r\n")))
    fetching(monkeypatch, s.port, deadline=5)
    tried, real = [], net.socket.create_connection
    monkeypatch.setattr(net.socket, "create_connection", lambda address, *a, **k: tried.append(address[0]) or real(("127.0.0.1", s.port), *a, **k))
    assert net._read("https://a.example/x", Plain(), lambda host: ["10.0.0.1", "10.0.0.2"]) is None and tried == ["10.0.0.1"]


# ---- the poll ----

class Polled(TlsTransport):
    @contextlib.contextmanager
    def _imap(self, box, password, *_, **__):
        yield object()


RAW = b"From: a@example.org\r\nSubject: s\r\nMessage-ID: <%d@example.org>\r\n\r\nx\r\n"


@pytest.fixture
def slow(monkeypatch):
    uids = list(range(1, 11))
    monkeypatch.setattr(session, "select", lambda imap, *a, **k: session.Selected(len(uids), 7, 11))
    monkeypatch.setattr(session, "search", lambda imap, *a: uids)
    monkeypatch.setattr(session, "newest", lambda imap, *a, **k: [])
    monkeypatch.setattr(session, "fetch_headers", lambda imap, got: [])

    def one(imap, uid, cap=0):
        time.sleep(0.05)
        return session.Fetched(uid, RAW % uid, False, None, 10)

    monkeypatch.setattr(session, "fetch_one", one)


async def test_a_slow_poll_keeps_what_it_read_and_the_next_one_goes_on(slow):
    t = Polled(cursors=MailCursors(), poll_deadline=0.4)
    _, start = await t.fetch(BOX, "pw", None)
    start = compose(0, 0)
    first, reached = await t.fetch(BOX, "pw", start)
    assert 0 < len(first) < 10 and reached == compose(0, first[-1].uid)
    seen = [m.uid for m in first]
    while reached < compose(0, 10):
        more, reached = await t.fetch(BOX, "pw", reached)
        seen += [m.uid for m in more]
    assert seen == list(range(1, 11))


# ---- poll_due ----

@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.h.clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    return n


async def test_one_poll_at_a_time_for_a_mailbox(node):
    box = node.wf.mail.require(MAIL)
    held, ran = asyncio.Event(), []

    async def handle(b):
        ran.append(b.id)
        await held.wait()

    first = asyncio.create_task(node.wf.mail.poll_due(box, handle))
    await asyncio.sleep(0)
    assert await node.wf.mail.poll_due(box, handle) is False and ran == [MAIL]
    held.set()
    assert await first is True
    assert await node.wf.mail.poll_due(box, handle) is True and len(ran) == 2


async def test_a_poll_past_its_time_stops_and_says_so(node):
    box = node.wf.mail.require(MAIL)

    async def stuck(_):
        await asyncio.sleep(30)

    assert await node.wf.mail.poll_due(box, stuck, wait=0.05) is False
    health = node.wf.mail.health(box.id)
    assert not health["ok"] and "too long" in health["words"]
    node.mail.inbox.append(Received(1, "a@example.org", "s", "b"))

    async def fine(_):
        return 1

    assert await node.wf.mail.poll_due(box, fine) is True


async def test_a_poll_that_fails_raises_nothing_and_the_next_one_runs(node):
    box = node.wf.mail.require(MAIL)

    async def broken(_):
        raise RuntimeError("the server said password=hunter2")

    assert await node.wf.mail.poll_due(box, broken) is False
    assert box.id not in node.wf.mail._polling
