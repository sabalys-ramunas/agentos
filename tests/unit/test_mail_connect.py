"""Connecting a mailbox: sign in to both, then save. The password stays out of everything else."""

from __future__ import annotations

import inspect
import logging
from dataclasses import replace

import pytest
from test_vault_support import make_vault, private_home
from workflow_support import make_node

from agentos.core.errors import InvalidRequest
from agentos.runner.mail import connect as connecting
from agentos.runner.mail import tools as mail_tools
from agentos.runner.mail.connect import (
    MailboxTaken,
    MailConfirmHosts,
    MailConnectFailed,
    MailJobEnded,
    connect,
)
from agentos.runner.mail.discovery import Found, Server, discover, typed
from agentos.runner.workflows.mail import SendRefused, SignInFailed
from agentos.vault.secret import Secret

PASSWORD = "the-very-secret-password"
BOX = "biz-a/inbox"


class Verifier:
    """What a transport does at connect, with a choice of how it fails."""

    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    async def verify(self, box, password):
        self.calls.append((box, password))
        if self.fail:
            raise SignInFailed(*self.fail)


def server(host="imap.example.net", port=993, security="tls", typed_=False):
    return Server(host, port, security, "a@example.net", typed=typed_)


def found(trust="https", **kw):
    return Found("a@example.net", server(), server("smtp.example.net", 465), "ispdb", trust, **kw)


@pytest.fixture
async def setup(tmp_path):
    node = make_node(tmp_path)
    vault = make_vault(private_home(tmp_path))
    await vault.start(control=False)
    node.wf.mail.vault = vault
    node.wf.mail.transport = Verifier()
    yield node.wf.mail, vault
    await vault.stop()


async def gmail():
    return await discover("me@gmail.com", None)


async def test_both_logins_come_first_and_then_it_is_saved_bound_to_both_hosts(setup):
    mail, vault = setup
    box = await connect(mail, found(), Secret(PASSWORD), mailbox_id=BOX)
    assert mail.transport.calls[0][1] == PASSWORD and mail.get(BOX).imap_host == "imap.example.net"
    meta = next(s for s in await vault.list_secrets() if s["ref"] == box.secret_ref)
    assert meta["scope"] == {"mail_imap": "imap.example.net:993", "mail_smtp": "smtp.example.net:465"}
    assert meta["kind"] == "mail_password" and PASSWORD not in str(meta) and PASSWORD not in str(box.model_dump())
    assert await mail._password(box) == PASSWORD


@pytest.mark.parametrize("stage", ["imap", "smtp"])
async def test_a_failure_says_which_of_the_two_and_saves_nothing(setup, stage):
    mail, vault = setup
    mail.transport = Verifier((stage, "sign_in"))
    with pytest.raises(MailConnectFailed) as e:
        await connect(mail, found(), Secret(PASSWORD), mailbox_id=BOX)
    assert e.value.stage == stage and ("IMAP" if stage == "imap" else "SMTP") in e.value.message
    assert mail.get(BOX) is None and await vault.list_secrets() == []
    assert PASSWORD not in str(e.value) and e.value.__cause__ is None


async def test_a_wrong_normal_password_on_gmail_says_it_wants_an_app_password(setup):
    mail, _ = setup
    mail.transport = Verifier(("imap", "sign_in"))
    with pytest.raises(MailConnectFailed) as e:
        await connect(mail, await gmail(), Secret("my normal password"), mailbox_id=BOX)
    assert "app password" in e.value.message and "2-Step Verification" in e.value.message
    assert "https://myaccount.google.com/apppasswords" in e.value.message


async def test_an_app_password_pasted_in_its_groups_of_four_signs_in(setup):
    mail, _ = setup
    await connect(mail, await gmail(), Secret("abcd efgh ijkl mnop\n"), mailbox_id=BOX)
    assert mail.transport.calls[0][1] == "abcdefghijklmnop"
    assert mail.get(BOX).files_sent is True and mail.get(BOX).provider == "gmail"


async def test_a_password_elsewhere_keeps_its_spaces(setup):
    mail, _ = setup
    await connect(mail, found(), Secret("two words\r\n"), mailbox_id=BOX)
    assert mail.transport.calls[0][1] == "two words"


async def test_a_low_trust_result_gets_no_password_until_a_person_says_yes(setup):
    mail, vault = setup
    low = found("low")
    with pytest.raises(MailConfirmHosts):
        await connect(mail, low, Secret(PASSWORD), mailbox_id=BOX)
    assert mail.transport.calls == [] and await vault.list_secrets() == []
    await connect(mail, low, Secret(PASSWORD), mailbox_id=BOX, confirmed=True)
    assert len(mail.transport.calls) == 1


async def test_hosts_a_person_typed_are_a_yes_and_may_be_private(setup):
    mail, _ = setup
    corrected = typed(found("low"), imap={"host": "10.0.0.2"}, smtp={"host": "10.0.0.3", "port": 587, "security": "starttls"})
    box = await connect(mail, corrected, Secret(PASSWORD), mailbox_id=BOX)
    assert box.private_hosts == ["10.0.0.2", "10.0.0.3"]


async def test_a_host_only_one_side_typed_stays_checked(setup):
    mail, _ = setup
    half = typed(found(), imap={"host": "10.0.0.2"})
    box = await connect(mail, half, Secret(PASSWORD), mailbox_id=BOX)
    assert box.private_hosts == ["10.0.0.2"] and "smtp.example.net" not in box.private_hosts


async def test_a_changed_mailbox_host_cannot_get_the_password(setup):
    mail, _ = setup
    box = await connect(mail, found(), Secret(PASSWORD), mailbox_id=BOX)
    for change in ({"imap_host": "evil.example"}, {"smtp_host": "evil.example"}, {"imap_port": 1993}, {"smtp_port": 2525}):
        moved = mail.add({**box.model_dump(), **change}, person=True)
        with pytest.raises(SendRefused, match="other servers") as e:
            await mail._password(moved)
        assert PASSWORD not in str(e.value)
    assert await mail._password(mail.add(box.model_dump(), person=True)) == PASSWORD


async def test_connecting_again_moves_the_binding_with_a_new_password(setup):
    mail, _ = setup
    await connect(mail, found(), Secret(PASSWORD), mailbox_id=BOX)
    moved = typed(found(), imap={"host": "imap2.example.net"})
    box = await connect(mail, moved, Secret("a-new-one"), mailbox_id=BOX, in_person=True)
    assert await mail._password(box) == "a-new-one"


async def test_an_id_that_holds_another_address_is_not_taken_over(setup):
    mail, _ = setup
    await connect(mail, found(), Secret(PASSWORD), mailbox_id=BOX)
    other = Found("b@example.net", server(), server("smtp.example.net"), "ispdb", "https")
    with pytest.raises(MailboxTaken):
        await connect(mail, other, Secret(PASSWORD), mailbox_id=BOX, in_person=True)


async def test_a_failed_save_takes_the_password_back_out(setup):
    mail, vault = setup

    def broken(*_, **__):
        raise RuntimeError("the store is gone")

    mail.add = broken
    with pytest.raises(RuntimeError):
        await connect(mail, found(), Secret(PASSWORD), mailbox_id=BOX)
    assert await vault.list_secrets() == []


async def test_a_sealed_vault_says_so_before_any_login(setup):
    mail, vault = setup
    vault.seal()
    with pytest.raises(Exception, match="sealed"):
        await connect(mail, found(), Secret(PASSWORD), mailbox_id=BOX)
    assert mail.transport.calls == []


async def test_removing_a_mailbox_takes_its_password_and_its_place(setup):
    mail, vault = setup
    box = await connect(mail, found(), Secret(PASSWORD), mailbox_id=BOX)
    mail.cursors.observe(box.id, 7)
    mail.store._exec("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (f"mail_uid:{box.id}", "5"))
    assert await mail.remove(box.id, person=True)
    assert mail.get(box.id) is None and await vault.list_secrets() == []
    assert mail.store.runner_store.meta(f"mail_uid:{box.id}", "") == "" and mail.cursors.observe(box.id, 9).epoch == 0


async def test_nothing_about_the_password_reaches_a_log(setup, caplog):
    mail, _ = setup
    caplog.set_level(logging.DEBUG)
    mail.transport = Verifier(("imap", "sign_in"))
    with pytest.raises(MailConnectFailed):
        await connect(mail, found(), Secret(PASSWORD), mailbox_id=BOX)
    mail.transport = Verifier()
    await connect(mail, found(), Secret(PASSWORD), mailbox_id=BOX)
    assert PASSWORD not in caplog.text


def test_no_tool_has_an_argument_for_a_password():
    for spec in mail_tools.tool_specs(None):
        names = set(inspect.signature(spec.handler).parameters)
        assert not {n for n in names if any(w in n.lower() for w in ("pass", "secret", "cred", "token"))}, spec.name


# ---- a job ----

async def test_a_job_ends_when_it_is_used(setup):
    mail, _ = setup
    jobs = connecting.jobs_for(mail)
    token, _ = jobs.open("a@example.net", BOX, found(), by="agent")
    await jobs.run(token, Secret(PASSWORD), shown=found().digest())
    assert jobs.get(token) is None
    with pytest.raises(MailJobEnded):
        await jobs.run(token, Secret(PASSWORD), shown=found().digest())


async def test_a_job_ends_with_time(setup):
    mail, _ = setup
    now = [1000.0]
    jobs = connecting.ConnectJobs(mail, clock=lambda: now[0])
    token, _ = jobs.open("a@example.net", BOX, found(), by="agent")
    assert jobs.get(token) is not None
    now[0] += connecting.JOB_TTL_S + 1
    assert jobs.get(token) is None


async def test_a_job_ends_after_its_tries(setup):
    mail, _ = setup
    mail.transport = Verifier(("imap", "sign_in"))
    jobs = connecting.jobs_for(mail)
    token, _ = jobs.open("a@example.net", BOX, found(), by="agent")
    for _ in range(connecting.JOB_TRIES):
        with pytest.raises(MailConnectFailed):
            await jobs.run(token, Secret("wrong"), shown=found().digest())
    assert jobs.get(token) is None


async def test_a_job_keeps_only_a_hash_of_its_token(setup):
    mail, _ = setup
    jobs = connecting.jobs_for(mail)
    token, job = jobs.open("a@example.net", BOX, found(), by="agent")
    assert token not in str(job) and token not in jobs._jobs


async def test_a_job_refuses_hosts_it_did_not_show(setup):
    mail, _ = setup
    jobs = connecting.jobs_for(mail)
    token, _ = jobs.open("a@example.net", BOX, found(), by="owner")
    with pytest.raises(MailConfirmHosts):
        await jobs.run(token, Secret(PASSWORD), shown=found(trust="low").digest()[::-1])
    assert mail.transport.calls == []


async def test_a_job_never_changes_a_mailbox_that_exists(setup):
    mail, _ = setup
    await connect(mail, found(), Secret(PASSWORD), mailbox_id=BOX, in_person=True)
    jobs = connecting.jobs_for(mail)
    token, _ = jobs.open("a@example.net", BOX, found(), by="owner")
    with pytest.raises(MailboxTaken):
        await jobs.run(token, Secret("another"), shown=found().digest())
    assert await mail._password(mail.get(BOX)) == PASSWORD


async def test_only_a_call_in_person_changes_a_mailbox_that_exists(setup):
    mail, _ = setup
    await connect(mail, found(), Secret(PASSWORD), mailbox_id=BOX, in_person=True)
    with pytest.raises(MailboxTaken):
        await connect(mail, found(), Secret("another"), mailbox_id=BOX)
    await connect(mail, found(), Secret("another"), mailbox_id=BOX, in_person=True)
    assert await mail._password(mail.get(BOX)) == "another"


async def test_two_digests_differ_by_any_host_port_or_security():
    base = found()
    others = [replace(base, imap=replace(base.imap, host="x.example.net")), replace(base, imap=replace(base.imap, port=143)),
              replace(base, smtp=replace(base.smtp, security="starttls")), replace(base, smtp=replace(base.smtp, username="other"))]
    assert len({base.digest(), *(o.digest() for o in others)}) == 5


async def test_nothing_found_means_a_person_types_both(setup):
    mail, _ = setup
    jobs = connecting.jobs_for(mail)
    token, _ = jobs.open("a@example.net", BOX, None, by="agent")
    with pytest.raises(InvalidRequest):
        await jobs.run(token, Secret(PASSWORD))
    box = await jobs.run(token, Secret(PASSWORD), imap={"host": "10.1.1.1", "port": 993, "security": "tls"},
                         smtp={"host": "10.1.1.1", "port": 587, "security": "starttls"})
    assert box.smtp_security == "starttls" and box.username == "a@example.net"
