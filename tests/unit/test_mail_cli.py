"""`agentos mail ...` with CliRunner: the prompt is faked, and the output never holds the password."""

from __future__ import annotations

import pytest
from test_mail_api import Net, Verifier
from test_vault_support import make_vault, private_home
from typer.testing import CliRunner

from agentos.runner.mail import cli
from agentos.runner.mail.discovery import Srv
from agentos.runner.workflows.mail import SignInFailed
from agentos.vault.service import VaultService

PASSWORD = "the-very-secret-password"
runner = CliRunner()


class LowNet(Net):
    async def srv(self, name):
        return [Srv("mail.other.example", 993)] if "imaps" in name or "submissions" in name else []


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = private_home(tmp_path)
    make_vault(h)
    monkeypatch.setenv("AGENTOS_HOME", str(h))
    monkeypatch.setattr(cli, "NET", Net)
    monkeypatch.setattr(cli, "_ask", lambda prompt: PASSWORD)
    verifier = Verifier()
    real = cli._svc

    def svc():
        wf = real()
        wf.mail.transport = verifier
        return wf

    monkeypatch.setattr(cli, "_svc", svc)
    return h, verifier


def run(*args, **kw):
    return runner.invoke(cli.app, list(args), **kw)


def secrets(home) -> list:
    import asyncio

    return asyncio.run(VaultService(home).list_secrets())


def test_a_gmail_address_connects_from_one_line(home, monkeypatch):
    h, verifier = home
    asked = []
    monkeypatch.setattr(cli, "_ask", lambda prompt: asked.append(prompt) or PASSWORD)
    r = run("connect", "me@gmail.com", "--id", "biz-a/inbox")
    assert r.exit_code == 0, r.output
    assert "imap.gmail.com:993" in r.output and "smtp.gmail.com:465" in r.output and "apppasswords" in r.output
    assert asked == ["App password for me@gmail.com: "] and verifier.calls[0][1] == PASSWORD
    assert PASSWORD not in r.output and "connected as biz-a/inbox" in r.output
    assert [s["ref"] for s in secrets(h)] == ["mail/biz-a/inbox"]


def test_the_password_is_not_an_option():
    r = run("connect", "--help")
    assert "--password" not in r.output and "--imap-host" in r.output


def test_a_failure_says_which_and_saves_nothing(home):
    h, verifier = home
    verifier.fail = ("smtp", "sign_in")
    r = run("connect", "me@gmail.com", "--id", "biz-a/inbox")
    assert r.exit_code == 1 and "SMTP" in r.output and "app password" in r.output and PASSWORD not in r.output
    assert secrets(h) == [] and "biz-a/inbox" not in run("list").output


def test_a_low_trust_result_waits_for_a_yes_before_the_password_is_even_asked(home, monkeypatch):
    h, verifier = home
    monkeypatch.setattr(cli, "NET", LowNet)
    monkeypatch.setattr(cli, "_ask", lambda prompt: pytest.fail("the password was asked for"))
    r = run("connect", "a@example.net", "--id", "biz-a/inbox", input="n\n")
    assert r.exit_code == 1 and "mail.other.example" in r.output and "Only DNS" in r.output
    assert verifier.calls == [] and secrets(h) == []


def test_a_yes_lets_it_go_on(home, monkeypatch):
    _, verifier = home
    monkeypatch.setattr(cli, "NET", LowNet)
    r = run("connect", "a@example.net", "--id", "biz-a/inbox", input="y\n")
    assert r.exit_code == 0, r.output
    assert verifier.calls[0][0] == "mail.other.example"


def test_servers_can_be_typed_and_nothing_is_looked_up(home, monkeypatch):
    _, verifier = home

    class NoNet:
        def __init__(self):
            pytest.fail("nothing should be looked up")

    monkeypatch.setattr(cli, "NET", NoNet)
    r = run("connect", "a@example.net", "--id", "biz-a/inbox", "--imap-host", "10.0.0.2", "--imap-port", "993",
            "--smtp-host", "10.0.0.2", "--smtp-port", "587", "--smtp-security", "starttls")
    assert r.exit_code == 0, r.output
    assert verifier.calls[0][0] == "10.0.0.2"


def test_list_test_and_remove(home):
    h, verifier = home
    run("connect", "me@gmail.com", "--id", "biz-a/inbox")
    listed = run("list")
    assert "biz-a/inbox" in listed.output and "me@gmail.com" in listed.output and "ok" in listed.output
    verifier.calls.clear()
    r = run("test", "biz-a/inbox")
    assert r.exit_code == 0 and "both sign-ins work" in r.output and verifier.calls[0][1] == PASSWORD
    verifier.fail = ("imap", "sign_in")
    r = run("test", "biz-a/inbox")
    assert r.exit_code == 1 and "IMAP" in r.output and PASSWORD not in r.output
    assert "did not accept" in run("list").output
    r = run("remove", "biz-a/inbox", "--yes")
    assert r.exit_code == 0 and secrets(h) == []
    assert "biz-a/inbox" not in run("list").output


def test_remove_asks_first(home):
    run("connect", "me@gmail.com", "--id", "biz-a/inbox")
    assert run("remove", "biz-a/inbox", input="n\n").exit_code == 1
    assert "biz-a/inbox" in run("list").output


def test_a_mailbox_that_is_not_there_says_so(home):
    r = run("test", "biz-a/none")
    assert r.exit_code == 1 and "no mailbox" in r.output


def test_a_sign_in_failure_keeps_its_stage():
    assert SignInFailed("smtp", "sign_in").stage == "smtp"


def test_the_only_way_in_is_connect(home):
    assert run("add", "biz-a/inbox", "--address", "a@example.net").exit_code != 0
    from agentos.runner.workflows import cli as workflows_cli

    assert not hasattr(workflows_cli, "mail_app") and not hasattr(workflows_cli, "mail_add")


def test_a_person_at_the_cli_can_connect_a_mailbox_again(home):
    assert run("connect", "me@gmail.com", "--id", "biz-a/inbox").exit_code == 0
    r = run("connect", "me@gmail.com", "--id", "biz-a/inbox")
    assert r.exit_code == 0, r.output
