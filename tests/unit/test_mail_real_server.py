"""The mail code against a real Dovecot and Postfix over real TLS (tests/mail_server).

Nothing here reaches a real provider: the server is a container on this machine.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from types import SimpleNamespace

import pytest
from mail_server_support import (
    FRIEND,
    FRIEND_PASSWORD,
    HOST,
    OWNER,
    PASSWORD,
    UTF8,
    UTF8_PASSWORD,
    StrippingProxy,
    start,
)
from test_vault_support import make_vault, private_home
from typer.testing import CliRunner
from workflow_support import make_node

from agentos.runner.mail import cli as mail_cli
from agentos.runner.mail import tools as mail_tools
from agentos.runner.mail.connect import MailConnectFailed, connect
from agentos.runner.mail.cursor import MailCursors, split
from agentos.runner.mail.discovery import Found, Server
from agentos.runner.mail.net import RealNet
from agentos.runner.mail.outbox import UseAList
from agentos.runner.mail.session import MailFailed
from agentos.runner.workflows.mail import (
    Mailbox,
    MessageGone,
    SendRefused,
    SignInFailed,
    TlsTransport,
    build_message,
)
from agentos.runner.workflows.sends import Unsubscribe
from agentos.vault.secret import Secret

pytestmark = pytest.mark.mailserver
UNSUB = Unsubscribe("tok", None, "mailto:owner@example.test?subject=unsubscribe-tok")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    yield from start(tmp_path_factory.mktemp("mailtest"))


def box(server, *, address=OWNER, smtp_port=None, smtp_security="tls", imap_port=None, imap_security="tls",
        host=HOST, mailbox="test/mail") -> Mailbox:
    return Mailbox(id=mailbox, address=address, imap_host=host, imap_port=imap_port or server.ports[993],
                   imap_security=imap_security, smtp_host=host, smtp_port=smtp_port or server.ports[465],
                   smtp_security=smtp_security, username=address, secret_ref="r", private_hosts=[host])


def box_for_owner(server) -> Mailbox:
    return box(server)


def transport(server, **kw) -> TlsTransport:
    return TlsTransport(tls=server.tls, timeout=10, **kw)


def message(to=FRIEND, subject="hello", body="the body", sender=None, **kw) -> EmailMessage:
    return build_message(sender or SimpleNamespace(address=OWNER), to=to, subject=subject, body=body, unsubscribe=UNSUB, **kw)


async def wait_for(fn, seconds=15.0):
    deadline = time.monotonic() + seconds
    while True:
        got = await fn()
        if got or time.monotonic() > deadline:
            return got
        await asyncio.sleep(0.2)


async def deliver(server, to_box, to_password, subject, *, body="the body", raw=None) -> list:
    """Send through Postfix as the owner, and wait for it to arrive."""
    t = transport(server)
    await t.send(box(server), PASSWORD, raw or message(to=to_box.address, subject=subject, body=body), to=to_box.address)

    async def arrived():
        return [m for m in await t.list_messages(to_box, to_password, unread=False, since=None, limit=50)
                if m.subject == subject]

    got = await wait_for(arrived)
    assert got, f"{subject} never arrived"
    return got


# ---- signing in ----

async def test_a_good_password_signs_in_to_both_over_implicit_tls(server):
    await transport(server).verify(box(server), PASSWORD)


async def test_starttls_on_587_and_143_works(server):
    b = box(server, smtp_port=server.ports[587], smtp_security="starttls", imap_port=server.ports[143], imap_security="starttls")
    await transport(server).verify(b, PASSWORD)


async def test_a_password_that_is_not_ascii_signs_in(server):
    await transport(server).verify(box(server, address=UTF8), UTF8_PASSWORD)


async def test_a_bad_password_fails_at_imap_and_says_so(server):
    with pytest.raises(SignInFailed) as e:
        await transport(server).verify(box(server), "wrong-password")
    assert (e.value.stage, e.value.kind) == ("imap", "sign_in")
    assert "wrong-password" not in str(e.value) and e.value.__cause__ is None


async def test_a_bad_smtp_login_is_named_as_smtp(server):
    t = transport(server)
    t._verify_imap = lambda b, p: None
    with pytest.raises(SignInFailed) as e:
        await t.verify(box(server), "wrong-password")
    assert (e.value.stage, e.value.kind) == ("smtp", "sign_in")


# ---- TLS ----

async def test_a_certificate_nobody_vouches_for_is_refused(server):
    t = TlsTransport(timeout=10)  # the system's roots, which do not know this CA
    with pytest.raises(SignInFailed) as e:
        await t.verify(box(server), PASSWORD)
    assert (e.value.stage, e.value.kind) == ("imap", "certificate")


async def test_a_certificate_for_another_name_is_refused(server):
    with pytest.raises(SignInFailed) as e:
        await transport(server).verify(box(server, host="localhost"), PASSWORD)
    assert e.value.kind == "certificate"


async def test_the_same_on_smtp_and_on_starttls(server):
    t = TlsTransport(timeout=10)
    t._verify_imap = lambda b, p: None
    for b in (box(server), box(server, smtp_port=server.ports[587], smtp_security="starttls")):
        with pytest.raises(SignInFailed) as e:
            await t.verify(b, PASSWORD)
        assert (e.value.stage, e.value.kind) == ("smtp", "certificate")


async def test_a_context_that_does_not_verify_is_never_used(server):
    def weak():
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    with pytest.raises(SignInFailed) as e:
        await TlsTransport(tls=weak, timeout=10).verify(box(server), PASSWORD)
    assert e.value.kind == "tls"


async def test_stripping_starttls_from_smtp_stops_before_any_password(server):
    proxy = StrippingProxy(server.ports[587])
    t = transport(server)
    t._verify_imap = lambda b, p: None
    try:
        with pytest.raises(SignInFailed) as e:
            await t.verify(box(server, smtp_port=proxy.port, smtp_security="starttls"), PASSWORD)
        assert (e.value.stage, e.value.kind) == ("smtp", "no_starttls")
        assert b"AUTH" not in bytes(proxy.seen) and PASSWORD.encode() not in bytes(proxy.seen)
    finally:
        proxy.close()


async def test_stripping_starttls_from_imap_stops_before_any_password(server):
    proxy = StrippingProxy(server.ports[143])
    try:
        with pytest.raises(SignInFailed) as e:
            await transport(server).verify(box(server, imap_port=proxy.port, imap_security="starttls"), PASSWORD)
        assert (e.value.stage, e.value.kind) == ("imap", "no_starttls")
        assert b"LOGIN" not in bytes(proxy.seen) and PASSWORD.encode() not in bytes(proxy.seen)
    finally:
        proxy.close()


async def test_a_send_with_a_stripped_starttls_is_refused_and_certainly_did_not_leave(server):
    proxy = StrippingProxy(server.ports[587])
    try:
        with pytest.raises(SendRefused):
            await transport(server).send(box(server, smtp_port=proxy.port, smtp_security="starttls"), PASSWORD, message(), to=FRIEND)
    finally:
        proxy.close()


async def test_a_host_nobody_typed_must_resolve_to_a_public_address(server):
    b = box(server).model_copy(update={"private_hosts": []})
    with pytest.raises(SignInFailed) as e:
        await transport(server).verify(b, PASSWORD)
    assert e.value.kind == "private"


# ---- send, fetch, unseen ----

async def test_a_message_goes_out_and_arrives_with_its_headers(server):
    got = await deliver(server, box(server, address=FRIEND, mailbox="test/friend"), FRIEND_PASSWORD, "first one")
    assert got[0].sender == OWNER and "the body" in got[0].body and got[0].message_id.startswith("<")
    assert not got[0].seen


async def test_words_that_are_not_ascii_survive_the_round_trip(server):
    me = box(server, address=FRIEND, mailbox="test/utf8")
    subject = "Labas, čia ąžuolas"
    got = (await deliver(server, me, FRIEND_PASSWORD, subject, body="Ačiū už laišką"))[0]
    assert got.subject == subject and "Ačiū už laišką" in got.body


async def test_a_recipient_the_server_refuses_certainly_did_not_leave(server):
    with pytest.raises(SendRefused):
        await transport(server).send(box(server), PASSWORD, message(to=FRIEND), to="not an address")


async def test_the_poll_starts_where_the_mailbox_is_and_then_sees_only_new_mail(server):
    me = box(server, address=FRIEND, mailbox="test/poll")
    t = transport(server)
    await deliver(server, me, FRIEND_PASSWORD, "old mail")
    got, cursor = await t.fetch(me, FRIEND_PASSWORD, None)
    assert got == []
    assert (await t.fetch(me, FRIEND_PASSWORD, cursor))[0] == []
    await deliver(server, me, FRIEND_PASSWORD, "new mail")
    got, cursor = await t.fetch(me, FRIEND_PASSWORD, cursor)
    assert [m.subject for m in got] == ["new mail"]
    assert (await t.fetch(me, FRIEND_PASSWORD, cursor))[0] == []


async def test_reading_leaves_unseen_mail_unseen_until_it_is_marked(server):
    me = box(server, address=FRIEND, mailbox="test/unseen")
    t = transport(server)
    mail = (await deliver(server, me, FRIEND_PASSWORD, "stay unseen"))[0]
    await t.read_message(me, FRIEND_PASSWORD, mail.uid)
    await t.fetch(me, FRIEND_PASSWORD, None)
    unread = await t.list_messages(me, FRIEND_PASSWORD, unread=True, since=None, limit=50)
    assert mail.uid in [m.uid for m in unread]
    await t.mark(me, FRIEND_PASSWORD, mail.uid, True)
    unread = await t.list_messages(me, FRIEND_PASSWORD, unread=True, since=None, limit=50)
    assert mail.uid not in [m.uid for m in unread]
    assert (await t.read_message(me, FRIEND_PASSWORD, mail.uid)).seen
    await t.mark(me, FRIEND_PASSWORD, mail.uid, False)
    assert not (await t.read_message(me, FRIEND_PASSWORD, mail.uid)).seen


async def test_the_list_is_newest_first_and_since_cuts_it(server):
    me = box(server, address=FRIEND, mailbox="test/list")
    t = transport(server)
    await deliver(server, me, FRIEND_PASSWORD, "list one")
    await deliver(server, me, FRIEND_PASSWORD, "list two")
    rows = await t.list_messages(me, FRIEND_PASSWORD, unread=False, since=None, limit=2)
    assert [m.subject for m in rows] == ["list two", "list one"]
    future = datetime.now(UTC) + timedelta(days=3)
    assert await t.list_messages(me, FRIEND_PASSWORD, unread=False, since=future, limit=5) == []


async def test_a_reply_keeps_the_thread(server):
    me = box(server, address=FRIEND, mailbox="test/thread")
    first = (await deliver(server, me, FRIEND_PASSWORD, "thread start"))[0]
    reply = build_message(me, to=OWNER, subject="Re: thread start", body="an answer", unsubscribe=UNSUB,
                          in_reply_to=first.message_id, references=first.references)
    t = transport(server)
    await t.send(me, FRIEND_PASSWORD, reply, to=OWNER)
    owner = box(server)

    async def arrived():
        return [m for m in await t.list_messages(owner, PASSWORD, unread=False, since=None, limit=50)
                if m.subject == "Re: thread start"]

    got = (await wait_for(arrived))[0]
    assert got.in_reply_to == first.message_id and first.message_id in got.references


async def test_a_copy_lands_in_sent_once(server):
    me = box(server)
    t = transport(server)
    m = message(subject="filed copy")
    await t.send(me, PASSWORD, m, to=str(m["To"]))
    raw = m.as_bytes()
    assert await t.file_sent(me, PASSWORD, raw, m["Message-ID"])
    assert await t.file_sent(me, PASSWORD, raw, m["Message-ID"])
    count = server.exec("doveadm", "search", "-u", OWNER, "mailbox", "Sent", "HEADER", "Message-ID", m["Message-ID"])
    assert len(count.strip().splitlines()) == 1


async def test_a_large_message_is_capped_and_says_so_and_a_poll_gets_past_it(server):
    me = box(server, address=FRIEND, mailbox="test/large")
    t = transport(server)
    _, cursor = await t.fetch(me, FRIEND_PASSWORD, None)
    big = message(to=FRIEND, subject="a large one", body="start of the text\n")
    big.add_attachment(b"x" * 6_000_000, maintype="application", subtype="octet-stream", filename="big.bin")
    await deliver(server, me, FRIEND_PASSWORD, "a large one", raw=big)
    got, cursor = await t.fetch(me, FRIEND_PASSWORD, cursor)
    assert len(got) == 1 and got[0].truncated and "start of the text" in got[0].body
    assert got[0].attachments[0]["name"] == "big.bin"


# ---- UIDVALIDITY ----

def deeply_nested(depth: int = 5000) -> bytes:
    out = b"From: Eve <eve@example.test>\r\nSubject: deep\r\nMessage-ID: <deep@example.test>\r\n"
    for i in range(depth):
        out += f'Content-Type: multipart/mixed; boundary="b{i}"\r\n\r\n--b{i}\r\n'.encode()
    return out + b"Content-Type: text/plain\r\n\r\nhi\r\n"


async def test_a_message_nested_past_the_recursion_limit_does_not_stop_a_real_poll(server):
    t, me = transport(server), box(server, address=FRIEND)
    _, cursor = await t.fetch(me, FRIEND_PASSWORD, None)

    def append():
        with t._imap(me, FRIEND_PASSWORD) as imap:
            assert imap.append("INBOX", None, None, deeply_nested())[0] == "OK"

    await asyncio.to_thread(append)
    subject = f"after the deep one {time.time()}"
    await deliver(server, me, FRIEND_PASSWORD, subject)
    got, cursor = await t.fetch(me, FRIEND_PASSWORD, cursor)
    assert [m.unreadable for m in got] == [True, False] and got[0].subject == "deep" and got[1].subject == subject
    assert (await t.fetch(me, FRIEND_PASSWORD, cursor))[0] == []


async def test_the_server_is_told_the_address_given_and_no_header_is_read(server):
    subject = f"explicit {time.time()}"
    raw = message(to="nobody-here@example.test", subject=subject)
    got = await deliver(server, box(server, address=FRIEND), FRIEND_PASSWORD, subject, raw=raw)
    assert got[0].to == "nobody-here@example.test"


async def test_the_bounded_clients_read_a_long_answer_over_starttls(server):
    t = transport(server)
    me = box(server, address=FRIEND, imap_port=server.ports[143], imap_security="starttls")
    assert len(await t.list_messages(me, FRIEND_PASSWORD, unread=False, since=None, limit=50)) > 0


async def test_a_changed_uidvalidity_neither_replays_old_mail_nor_loses_new_mail(server):
    me = box(server, address=FRIEND, mailbox="test/validity")
    cursors = MailCursors()
    t = transport(server, cursors=cursors)
    await deliver(server, me, FRIEND_PASSWORD, "before the change")
    _, cursor = await t.fetch(me, FRIEND_PASSWORD, None)
    await deliver(server, me, FRIEND_PASSWORD, "delivered before it too")
    got, cursor = await t.fetch(me, FRIEND_PASSWORD, cursor)
    assert [m.subject for m in got] == ["delivered before it too"]

    server.set_uidvalidity(FRIEND, 987654321)
    await deliver(server, me, FRIEND_PASSWORD, "after the change")
    got, moved = await t.fetch(me, FRIEND_PASSWORD, cursor)
    assert [m.subject for m in got] == ["after the change"]
    assert split(moved)[0] == split(cursor)[0] + 1
    assert split(got[0].uid)[0] == 1 and got[0].uid != split(got[0].uid)[1]

    await deliver(server, me, FRIEND_PASSWORD, "and after that")
    got, moved = await t.fetch(me, FRIEND_PASSWORD, moved)
    assert [m.subject for m in got] == ["and after that"]
    assert (await t.fetch(me, FRIEND_PASSWORD, moved))[0] == []


async def test_an_id_from_before_a_change_does_not_read_another_message(server):
    me = box(server, address=FRIEND, mailbox="test/stale")
    t = transport(server)
    await deliver(server, me, FRIEND_PASSWORD, "stale id")
    mail = (await t.list_messages(me, FRIEND_PASSWORD, unread=False, since=None, limit=1))[0]
    assert (await t.read_message(me, FRIEND_PASSWORD, mail.uid)).subject == "stale id"
    server.set_uidvalidity(FRIEND, 123456789)
    with pytest.raises(MessageGone):
        await t.read_message(me, FRIEND_PASSWORD, mail.uid)


async def test_a_message_that_is_gone_says_so(server):
    me = box(server, address=FRIEND, mailbox="test/gone")
    t = transport(server)
    await t.fetch(me, FRIEND_PASSWORD, None)
    with pytest.raises(MessageGone):
        await t.read_message(me, FRIEND_PASSWORD, 999_999)
    with pytest.raises(MessageGone):
        await t.mark(me, FRIEND_PASSWORD, 999_999, True)


def test_a_failure_says_no_server_text():
    assert str(MailFailed("sign_in")) == "sign_in"


# ---- end to end: the vault, the service and the tools against the real server ----

@pytest.fixture
async def connected(server, tmp_path):
    node = make_node(tmp_path)
    vault = make_vault(private_home(tmp_path))
    await vault.start(control=False)
    mail = node.wf.mail
    mail.vault = vault
    mail.transport = TlsTransport(cursors=mail.cursors, tls=server.tls, timeout=10)
    yield node, mail
    await vault.stop()


def typed_found(server, address=FRIEND) -> Found:
    return Found(address, Server(HOST, server.ports[993], "tls", address, typed=True),
                 Server(HOST, server.ports[587], "starttls", address, typed=True), "typed", "typed")


async def test_connect_signs_in_for_real_and_saves_and_a_bad_password_saves_nothing(server, connected):
    _, mail = connected
    with pytest.raises(MailConnectFailed) as e:
        await connect(mail, typed_found(server), Secret("wrong-password"), mailbox_id="biz-a/inbox")
    assert e.value.stage == "imap" and mail.get("biz-a/inbox") is None and await mail.vault.list_secrets() == []
    box = await connect(mail, typed_found(server), Secret(FRIEND_PASSWORD), mailbox_id="biz-a/inbox")
    assert box.smtp_security == "starttls" and await mail._password(box) == FRIEND_PASSWORD
    await mail.test(box)


async def test_mail_send_goes_out_files_a_copy_and_a_reply_threads(server, connected):
    node, mail = connected
    box = await connect(mail, typed_found(server), Secret(FRIEND_PASSWORD), mailbox_id="biz-a/inbox")
    ctx = SimpleNamespace(runtime=node.h.runtime, principal=SimpleNamespace(id="a"), labels=[],
                          can_access_lane=lambda lane: True)

    async def authorize(*_, **__):
        return None

    ctx.authorize = authorize
    ctx.taint = lambda *labels, **kw: ctx.labels.append(labels)
    subject = f"e2e {time.time()}"
    await deliver(server, box, FRIEND_PASSWORD, subject, body="please answer")
    rows = (await mail_tools.mail_list(ctx, mailbox=box.id, limit=50))["messages"]
    first = next(r for r in rows if r["subject"] == subject)
    read = await mail_tools.mail_read(ctx, mailbox=box.id, id=first["id"])
    assert "please answer" in read["message"]["body"] and ctx.labels
    out = await mail_tools.mail_send(ctx, mailbox=box.id, to=OWNER, subject=f"Re: {subject}", body="my answer",
                                     reply_to_id=first["id"])
    assert out["sent"] and out["in_sent_folder"]
    copies = server.exec("doveadm", "search", "-u", FRIEND, "mailbox", "Sent", "HEADER", "Message-ID", out["message_id"])
    assert len(copies.strip().splitlines()) == 1 and out["reply"]
    await mail_tools.mail_send(ctx, mailbox=box.id, to=OWNER, subject=f"Re: {subject}", body="and one more", reply_to_id=first["id"])
    await mail_tools.mail_send(ctx, mailbox=box.id, to=OWNER, subject="cold", body="x")
    with pytest.raises(UseAList):
        await mail_tools.mail_send(ctx, mailbox=box.id, to=OWNER, subject="again", body="x")
    owner, box_owner = transport(server), box_for_owner(server)

    async def reply():
        return [m for m in await owner.list_messages(box_owner, PASSWORD, unread=False, since=None, limit=50)
                if m.subject == f"Re: {subject}"]

    got = (await wait_for(reply))[0]
    assert got.in_reply_to.startswith("<") and got.references and "unsubscribe-" not in got.body


async def test_an_unsubscribe_by_mail_arrives_through_the_real_poll(server, connected):
    node, mail = connected
    box = await connect(mail, typed_found(server), Secret(FRIEND_PASSWORD), mailbox_id="biz-a/inbox")
    await node.wf.poll_mailbox(box)
    unsubscribe = node.wf.sends.unsubscribe_for("biz-a", OWNER, mailbox=box.address)
    subject = unsubscribe.mailto.split("subject=")[1]
    await deliver(server, box, FRIEND_PASSWORD, subject)
    await node.wf.poll_mailbox(box)
    assert node.wf.sends.store.is_suppressed("biz-a", OWNER)


async def test_the_password_stays_out_of_what_a_real_failure_logs(server, connected, caplog):
    _, mail = connected
    caplog.set_level(logging.DEBUG)
    with pytest.raises(MailConnectFailed):
        await connect(mail, typed_found(server), Secret("wrong-password"), mailbox_id="biz-a/inbox")
    assert "wrong-password" not in caplog.text


async def test_the_probe_of_step_six_sends_no_credential_and_checks_the_certificate(server):
    net = RealNet(context=server.tls)
    assert await net.answers_tls(HOST, server.ports[993], "tls")
    assert await net.answers_tls(HOST, server.ports[587], "starttls")
    assert not await net.answers_tls("localhost", server.ports[993], "tls")
    assert not await RealNet().answers_tls(HOST, server.ports[993], "tls")


def test_the_cli_connects_to_the_real_server_and_a_wrong_password_saves_nothing(server, tmp_path, monkeypatch):
    home = private_home(tmp_path)
    make_vault(home)
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    monkeypatch.setenv("SSL_CERT_FILE", str(server.ca))  # the default context, as the daemon's, trusting this CA
    args = ["connect", FRIEND, "--id", "biz-a/inbox", "--imap-host", HOST, "--imap-port", str(server.ports[993]),
            "--smtp-host", HOST, "--smtp-port", str(server.ports[465])]
    monkeypatch.setattr(mail_cli, "_ask", lambda prompt: "wrong-password")
    r = CliRunner().invoke(mail_cli.app, args)
    assert r.exit_code == 1 and "IMAP" in r.output and "wrong-password" not in r.output
    monkeypatch.setattr(mail_cli, "_ask", lambda prompt: FRIEND_PASSWORD)
    r = CliRunner().invoke(mail_cli.app, args)
    assert r.exit_code == 0, r.output
    assert FRIEND_PASSWORD not in r.output
    r = CliRunner().invoke(mail_cli.app, ["test", "biz-a/inbox"])
    assert r.exit_code == 0 and "both sign-ins work" in r.output, r.output
