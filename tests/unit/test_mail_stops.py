"""What certainly did not leave is never counted as maybe, and a mailbox that cannot send stops the whole step."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_mail_tools import Ctx, Transport
from workflow_support import LIST, MAIL, OWNER, PEOPLE, action, make_node, workflow

from openajar.runner.mail import tools as mail_tools
from openajar.runner.mail.outbox import MailNotSent
from openajar.runner.mail.session import MailFailed
from openajar.runner.workflows.mail import Mailbox, SendRefused, SendStopped, TlsTransport, build_message
from openajar.runner.workflows.sends import Unsubscribe
from openajar.vault.errors import VaultSealed

SEND = f"mail.send@{MAIL}"


class Sealed:
    def __init__(self, error=None):
        self.error, self.reads = error or VaultSealed("sealed"), 0

    async def get_secret(self, ref, *, purpose):
        self.reads += 1
        if self.error is not None:
            raise self.error
        return "the password"


class Rejected(Transport):
    """A server that turns down the password, as `TlsTransport._send` says it."""

    def __init__(self):
        super().__init__()
        self.tries = 0

    async def send(self, box, password, message, *, to):
        self.tries += 1
        raise SendStopped("the server did not accept this password")


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.wf.mail.transport = Transport()
    n.mail = n.wf.mail.transport
    n.wf.sends.timezone = lambda: "UTC"
    n.h.clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    return n


def newsletter(node):
    return node.wf.save(workflow(action("send", SEND, label="send it", inputs={"subject": "S", "body": "B"}, send={"list": LIST})), OWNER)["id"]


def code_of(node, view):
    return node.runner.store.get_run(view["id"]).error["code"]


def spent(node):
    return node.wf.sends.store.sent_on(MAIL, node.wf.sends.day())


def states(node, view):
    rows = node.wf.sends.store.ledger_rows(view["id"], "send")
    return {r["state"] for r in rows}


async def test_a_sealed_vault_stops_a_list_at_once_and_spends_nothing(node):
    vault = Sealed()
    node.wf.mail.vault = vault
    view = await node.run(newsletter(node))
    assert view["status"] == "failed" and code_of(node, view) == "vault_sealed"
    assert "Unlock the vault" in node.step(view, "send")["note"]
    assert len(node.wf.sends.store.ledger_rows(view["id"], "send")) == 1 and node.mail.sent == [] and spent(node) == 0
    assert "unknown" not in states(node, view) and "sending" not in states(node, view)


async def test_a_retry_after_unsealing_reaches_everyone(node):
    vault = Sealed()
    node.wf.mail.vault = vault
    wid = newsletter(node)
    view = await node.run(wid)
    vault.error = None
    node.wf.retry(wid, view["id"], OWNER)
    await node.settle()
    assert sorted(node.mail.to) == PEOPLE and spent(node) == len(PEOPLE)


async def test_a_rejected_password_does_not_walk_the_list(node):
    node.wf.mail.transport = Rejected()
    view = await node.run(newsletter(node))
    assert view["status"] == "failed" and node.wf.mail.transport.tries == 1 and spent(node) == 0
    assert "password" in node.step(view, "send")["note"]
    assert "unknown" not in states(node, view)


async def test_what_left_before_a_stop_stays_sent_and_is_not_repeated(node):
    real, count = node.mail.send, []

    async def second_fails(box, password, message, *, to):
        count.append(to)
        if len(count) == 2:
            raise SendStopped("the server did not accept this password")
        await real(box, password, message, to=to)

    node.mail.send = second_fails
    wid = newsletter(node)
    view = await node.run(wid)
    assert len(node.mail.sent) == 1 and spent(node) == 1 and len(count) == 2
    node.mail.send = real
    node.wf.retry(wid, view["id"], OWNER, force=True)
    await node.settle()
    assert sorted(node.mail.to) == PEOPLE and spent(node) == len(PEOPLE)


async def test_one_refused_recipient_still_walks_the_list(node):
    node.mail.refuse = {PEOPLE[0]}
    view = await node.run(newsletter(node))
    assert len(node.mail.sent) == len(PEOPLE) - 1 and code_of(node, view) == "send_failed"


@pytest.mark.parametrize("error", [VaultSealed("sealed"), RuntimeError("a vault in a bad way"), OSError("disk")])
async def test_a_single_message_that_never_reached_a_server_is_not_in_doubt(node, error):
    node.wf.mail.vault = Sealed(error)
    with pytest.raises(MailNotSent):
        await mail_tools.mail_send(Ctx(node), mailbox=MAIL, to="a@example.org", subject="Hi", body="x")
    assert spent(node) == 0
    node.wf.mail.vault = Sealed()
    node.wf.mail.vault.error = None
    await mail_tools.mail_send(Ctx(node), mailbox=MAIL, to="a@example.org", subject="Hi", body="x")
    assert node.mail.rcpt == ["a@example.org"]


async def test_a_password_that_is_gone_stops_too(node):
    class Empty:
        async def get_secret(self, ref, *, purpose):
            return None

    node.wf.mail.vault = Empty()
    with pytest.raises(SendStopped):
        await node.wf.mail.send(node.wf.mail.require(MAIL), build_message(node.wf.mail.require(MAIL), to="a@example.org", subject="s", body="b",
                                                                          unsubscribe=Unsubscribe("t", None, "mailto:a@example.org")), to="a@example.org")


# ---- the transport ----

BOX = Mailbox(id="biz-a/mail", address="hello@biz-a.example", imap_host="imap.invalid", smtp_host="smtp.invalid",
              username="hello@biz-a.example", secret_ref="r", private_hosts=["smtp.invalid"])
MESSAGE = build_message(SimpleNamespace(address="hello@biz-a.example"), to="a@example.org", subject="s", body="b",
                        unsubscribe=Unsubscribe("t", None, "mailto:hello@biz-a.example"))


class Broken(TlsTransport):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def _open_smtp(self, box, password, *_, **__):
        raise self.error


@pytest.mark.parametrize("kind", ["sign_in", "app_password", "web_login", "unreachable", "certificate", "no_starttls", "private"])
def test_a_server_that_cannot_be_signed_in_to_stops_the_step(kind):
    with pytest.raises(SendStopped):
        Broken(MailFailed(kind))._send(BOX, "pw", MESSAGE, "a@example.org")


@pytest.mark.parametrize("error", [ValueError("x"), UnicodeError("x"), RuntimeError("x"), OSError("x")])
def test_anything_else_before_the_message_certainly_did_not_send_it(error):
    with pytest.raises(SendRefused):
        Broken(error)._send(BOX, "pw", MESSAGE, "a@example.org")


def test_a_stop_is_a_refusal_so_every_rule_for_a_refusal_holds():
    assert issubclass(SendStopped, SendRefused)
