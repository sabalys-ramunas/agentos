"""What a server's answer means, and where a poll stands: pure rules, no network."""

from __future__ import annotations

import imaplib
import inspect
import smtplib
from datetime import UTC, datetime, timedelta

import pytest

from agentos.runner.mail import providers, session
from agentos.runner.mail.cursor import Cursor, MailCursors, Seen, compose, fingerprint, is_new, split
from agentos.runner.mail.session import classify_login, explain, internal_date, records

GMAIL = providers.by_id("gmail")


@pytest.mark.parametrize(("text", "kind"), [
    ("b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'", "sign_in"),
    ("b'[ALERT] Application-specific password required: https://support.google.com/accounts/answer/185833 (Failure)'",
     "app_password"),
    ("534 5.7.9 Application-specific password required. Learn more at https://support.google.com/mail/?p=InvalidSecondFactor",
     "app_password"),
    ("535 5.7.8 Username and Password not accepted. For more information, go to BadCredentials", "sign_in"),
    ("534 5.7.14 Please log in with your web browser and then try again", "web_login"),
    ("b'[ALERT] Please log in via your web browser (Failure)'", "web_login"),
    ("b'[ALERT] Your account is not enabled for IMAP use.'", "imap_off"),
    ("b'[ALERT] IMAP access is disabled for your domain.'", "imap_off"),
    ("b'Authentication failed.'", "sign_in"),
])
def test_what_a_refused_login_means(text, kind):
    assert classify_login(text) == kind


@pytest.mark.parametrize("kind", ["sign_in", "app_password"])
@pytest.mark.parametrize("stage", ["imap", "smtp"])
def test_a_wrong_normal_password_on_gmail_asks_for_an_app_password_with_the_link(stage, kind):
    words = explain(stage, "imap.gmail.com", kind, GMAIL)
    assert "app password" in words and "2-Step Verification" in words
    assert "https://myaccount.google.com/apppasswords" in words
    assert ("IMAP" if stage == "imap" else "SMTP") in words


def test_workspace_gets_the_same_words():
    assert "apppasswords" in explain("imap", "imap.gmail.com", "sign_in", providers.by_id("google-workspace"))


def test_each_failure_says_which_and_why_in_plain_words():
    assert "certificate" in explain("smtp", "h", "certificate") and "SMTP" in explain("smtp", "h", "certificate")
    assert "no password was sent" in explain("imap", "h", "no_starttls")
    assert "did not accept" in explain("imap", "h", "sign_in")
    assert "turned off" in explain("imap", "h", "imap_off")
    assert "h" in explain("imap", "h", "unreachable")


def test_a_word_from_a_server_is_never_in_a_message():
    assert "secret" not in explain("imap", "h", classify_login("NO password secret123 is wrong"))


def test_a_server_that_echoes_the_password_back_does_not_put_it_in_an_error():
    secret = "the-very-secret-password"

    class Imap:
        def login(self, user, password):
            raise imaplib.IMAP4.error(f"NO bad password {password}")

    class Smtp:
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, f"bad password {password}".encode())

    for call in (lambda: session.imap_login(Imap(), "a", secret), lambda: session.smtp_login(Smtp(), "a", secret)):
        with pytest.raises(session.MailFailed) as e:
            call()
        assert secret not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


def test_a_password_cannot_end_a_command():
    for call in (session.imap_login, session.smtp_login):
        with pytest.raises(session.MailFailed) as e:
            call(None, "a", "pass\r\nLOGOUT")
        assert e.value.kind == "bad_password"


def test_the_session_only_ever_dials_out_over_tls():
    source = inspect.getsource(session)
    assert "SMTP_SSL" in source and "IMAP4_SSL" in source and "starttls" in source
    assert "CERT_NONE" not in source and "_create_unverified_context" not in source and "check_hostname = False" not in source
    for listener in ("listen(", "serve_forever", "start_server", "bind("):
        assert listener not in source


def test_a_literal_cannot_read_as_a_flag():
    parts = [(b'1 (UID 9 FLAGS () INTERNALDATE "05-Nov-2026 10:00:00 +0000" BODY[] {30}', b"FLAGS (\\Seen) UID 1 x"), b")"]
    meta, literal = next(iter(records(parts)))
    assert b"Seen" not in meta and literal.startswith(b"FLAGS")


def test_items_after_a_literal_are_kept():
    meta, _ = next(iter(records([(b"1 (FLAGS (\\Seen) BODY[] {3}", b"abc"), b" UID 44)"])))
    assert b"UID 44" in meta and b"Seen" in meta


def test_several_messages_in_one_answer():
    got = list(records([(b"1 (UID 7 BODY {1}", b"a"), b")", (b"2 (UID 8 BODY {1}", b"b"), b")"]))
    assert [x[1] for x in got] == [b"a", b"b"]


def test_an_arrival_time_is_read_with_its_zone():
    got = internal_date(b'INTERNALDATE " 5-Nov-2026 12:00:00 +0200"')
    assert got == datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
    assert internal_date(b"nothing") is None


def test_an_id_holds_the_generation_above_the_uid():
    assert compose(0, 55) == 55 and split(55) == (0, 55)
    assert split(compose(3, 55)) == (3, 55) and compose(1, 55) != compose(2, 55)


MARK = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def cursor(**kw) -> Cursor:
    return Cursor(watermark=MARK.isoformat(), **kw)


def test_a_message_seen_before_is_old_whatever_its_arrival_says():
    print_ = fingerprint("<a@x>", "")
    assert not is_new(cursor(recent=[print_]), Seen(print_, MARK + timedelta(hours=1)))


def test_a_message_from_before_the_watermark_is_old():
    assert not is_new(cursor(), Seen("x", MARK - timedelta(days=1)))


def test_a_message_after_it_is_new():
    assert is_new(cursor(), Seen("x", MARK + timedelta(minutes=1)))


def test_clocks_may_disagree_by_a_little():
    assert is_new(cursor(), Seen("x", MARK - timedelta(minutes=5)))


def test_a_rewritten_arrival_time_does_not_make_old_mail_new():
    assert not is_new(cursor(), Seen("x", MARK + timedelta(hours=1), "Mon, 01 Jan 2024 10:00:00 +0000"))


def test_nothing_is_new_without_a_watermark():
    assert not is_new(Cursor(), Seen("x", MARK))


def test_a_changed_validity_moves_the_generation_once_whoever_sees_it_first():
    c = MailCursors()
    assert c.observe("a/b", 5).epoch == 0
    assert c.observe("a/b", 5).epoch == 0
    assert c.observe("a/b", 6).epoch == 1
    assert c.observe("a/b", 6).epoch == 1


def test_the_ring_is_bounded_and_the_watermark_only_moves_forward():
    c = MailCursors()
    c.observe("a/b", 1)
    c.saw("a/b", [Seen(str(i), MARK) for i in range(1500)])
    got = c.observe("a/b", 1)
    assert len(got.recent) == 1000 and got.recent[-1] == "1499"
    c.saw("a/b", [Seen("late", MARK - timedelta(days=1))])
    assert c.observe("a/b", 1).mark >= MARK


def test_the_watermark_stands_still_while_a_change_is_checked():
    c = MailCursors()
    start = c.observe("a/b", 1).mark
    c.saw("a/b", [Seen("x", start + timedelta(days=1))], settle=False)
    assert c.observe("a/b", 1).mark == start
