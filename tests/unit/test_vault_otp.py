"""TOTP seeds and codes, and the email code catcher (with a fake IMAP server)."""

import base64
import imaplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime

import pytest

from openajar.vault.catcher import (
    CatcherRule,
    ImapCodeCatcher,
    extract_code,
    sender_matches,
    validate_catcher_config,
)
from openajar.vault.errors import CatcherError, EntryInvalid
from openajar.vault.secret import Secret
from openajar.vault.totp import canonical_uri, code_at, parse_totp, seconds_left

RFC_SEED = base64.b32encode(b"12345678901234567890").decode()


# ---- TOTP ----------------------------------------------------------------------

@pytest.mark.parametrize("at,code", [(59, "94287082"), (1111111109, "07081804"), (2000000000, "69279037")])
def test_rfc6238_vectors(at, code):
    spec = parse_totp(f"otpauth://totp/x?secret={RFC_SEED}&digits=8")
    assert code_at(spec, at).reveal() == code


def test_bare_seed_is_forgiving_about_spacing_and_case():
    spec = parse_totp("jbsw y3dp ehpk 3pxp jbsw y3dp ehpk 3pxp")
    assert spec.seed.reveal() == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    assert (spec.digits, spec.period, spec.algorithm) == (6, 30, "SHA1")
    assert "JBSW" not in repr(spec)
    assert len(code_at(spec, 0).reveal()) == 6
    assert seconds_left(spec, 1_800_000_028.5) == pytest.approx(1.5)


def test_otpauth_uri_parameters():
    spec = parse_totp(f"otpauth://totp/Insta:me?secret={RFC_SEED}&digits=8&period=60&algorithm=sha256&issuer=I")
    assert (spec.digits, spec.period, spec.algorithm) == (8, 60, "SHA256")
    uri = canonical_uri(f"otpauth://totp/x?secret={RFC_SEED}", label="ig", issuer="instagram.com")
    assert uri.startswith("otpauth://totp/ig?secret=") and "issuer=instagram.com" in uri
    again = parse_totp(uri)
    assert code_at(again, 59) == code_at(parse_totp(RFC_SEED), 59)


@pytest.mark.parametrize("bad", [
    "", "not base32 !!", "JBSWY3DP", "otpauth://hotp/x?secret=" + RFC_SEED + "&counter=1",
    "otpauth://totp/x?secret=" + RFC_SEED + "&digits=4", "otpauth://totp/x?secret=" + RFC_SEED + "&period=1",
    "otpauth://totp/x?secret=" + RFC_SEED + "&algorithm=MD5", "otpauth://totp/x?digits=6",
])
def test_bad_seeds_are_rejected_without_echo(bad):
    with pytest.raises(EntryInvalid) as exc:
        parse_totp(bad)
    if len(bad) > 10:
        assert bad not in exc.value.message and RFC_SEED not in exc.value.message


# ---- catcher config and matching -----------------------------------------------

def test_catcher_config_validation():
    cfg = validate_catcher_config({"account": "mail", "host": "IMAP.Gmail.com", "sender": "@Mail.Instagram.com"})
    assert cfg["host"] == "imap.gmail.com" and cfg["sender"] == "@mail.instagram.com" and cfg["port"] == 993
    for bad in ({}, {"account": "m", "host": "x y", "sender": "@a.com"}, {"account": "m", "host": "h.com"},
                {"account": "m", "host": "h.com", "sender": "@a.com", "pattern": "("},
                {"account": "m", "host": "h.com", "sender": "@a.com", "window_seconds": 10**9},
                {"account": "m", "host": "h.com", "sender": "@a.com", "password": "x"},
                {"account": "m", "host": "h.com", "sender": "@a.com", "kind": "pop3"}):
        with pytest.raises(EntryInvalid):
            validate_catcher_config(bad)


@pytest.mark.parametrize("rule,header,ok", [
    ("security@mail.instagram.com", "Instagram <security@mail.instagram.com>", True),
    ("security@mail.instagram.com", "security@mail.instagram.com.evil.io", False),
    ("@mail.instagram.com", "no-reply@mail.instagram.com", True),
    ("@mail.instagram.com", "x@evilmail.instagram.com", False),
    ("@mail.instagram.com", "x@mail.instagram.com.evil.io", False),
    ("@mail.instagram.com", "a@mail.instagram.com, b@evil.io", False),
    ("@mail.instagram.com", "", False),
])
def test_sender_matching(rule, header, ok):
    assert sender_matches(rule, header) is ok


NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
RULE = CatcherRule(sender="@mail.instagram.com", window_seconds=600)


def mail(code="482913", sender="security@mail.instagram.com", sent=NOW, html=False, subject="Your code"):
    m = EmailMessage()
    m["From"] = f"Instagram <{sender}>"
    m["Subject"] = subject
    m["Date"] = format_datetime(sent)
    body = f"Use {code} to confirm it is you. Reference 1234567890." if code else "hello"
    if html:
        m.set_content(f"<p>Use <b>{code}</b> to log in</p>", subtype="html")
    else:
        m.set_content(body)
    return m.as_bytes()


def test_extract_code_respects_sender_window_and_pattern():
    since = NOW - timedelta(seconds=30)
    assert extract_code(RULE, mail(), since=since, now=NOW) == "482913"
    assert extract_code(RULE, mail(html=True), since=since, now=NOW) == "482913"
    assert extract_code(RULE, mail(sender="security@evil.io"), since=since, now=NOW) is None
    assert extract_code(RULE, mail(sent=NOW - timedelta(minutes=20)), since=since, now=NOW) is None
    assert extract_code(RULE, mail(sent=NOW - timedelta(minutes=5)), since=since, now=NOW) is None  # before login
    assert extract_code(RULE, mail(sent=NOW + timedelta(hours=1)), since=since, now=NOW) is None    # future
    assert extract_code(RULE, mail(code=None), since=since, now=NOW) is None
    custom = CatcherRule(sender="@mail.instagram.com", pattern=r"code:\s*([A-Z0-9]{8})")
    assert extract_code(custom, mail(code="code: AB12CD34"), since=since, now=NOW) == "AB12CD34"
    assert extract_code(RULE, b"x" * 2_000_000, since=since, now=NOW) is None


# ---- IMAP polling --------------------------------------------------------------

class FakeImap:
    def __init__(self, server):
        self.server = server

    def login(self, user, pw):
        if (user, pw) != ("me@gmail.com", "mail-pw-1"):
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        self.server.logins += 1

    def select(self, mailbox, readonly=False):
        assert readonly is True
        return ("OK", [b"1"]) if mailbox == '"INBOX"' else ("NO", [b""])

    def search(self, charset, *criteria):
        self.server.searches.append(criteria)
        ids = b" ".join(str(i + 1).encode() for i in range(len(self.server.messages)))
        return "OK", [ids]

    def fetch(self, mid, what):
        assert what == "(BODY.PEEK[])"
        return "OK", [(b"1 (BODY[] {n}", self.server.messages[int(mid) - 1])]

    def logout(self):
        self.server.logouts += 1


class FakeServer:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.logins = self.logouts = 0
        self.searches = []

    def connect(self, host, port):
        if host == "down.example.com":
            raise OSError("connection refused")
        return FakeImap(self)


def catcher(server, clock, sleep, password="mail-pw-1", host="imap.gmail.com"):
    return ImapCodeCatcher(host, 993, Secret("me@gmail.com"), Secret(password), connect=server.connect,
                           clock=clock, sleep=sleep)


async def test_imap_catcher_polls_until_the_code_arrives():
    t = [NOW.timestamp()]
    server = FakeServer([mail(code="111111", sender="other@evil.io")])

    async def sleep(s):
        t[0] += s
        if len(server.searches) == 2:
            server.messages.append(mail(code="654321", sent=datetime.fromtimestamp(t[0], UTC)))

    c = catcher(server, lambda: t[0], sleep)
    code = await c.wait_for_code(CatcherRule(sender="@mail.instagram.com", poll_seconds=5), since=NOW)
    assert code.reveal() == "654321"
    assert server.logins == server.logouts == 3
    assert server.searches[0][:2] == ("FROM", '"mail.instagram.com"')
    assert "mail-pw-1" not in repr(c)


async def test_imap_catcher_times_out():
    t = [NOW.timestamp()]

    async def sleep(s):
        t[0] += s

    c = catcher(FakeServer(), lambda: t[0], sleep)
    rule = CatcherRule(sender="@mail.instagram.com", timeout_seconds=20, poll_seconds=5)
    assert await c.wait_for_code(rule, since=NOW) is None
    assert t[0] - NOW.timestamp() <= 20


async def test_imap_errors_are_clear_and_never_contain_credentials():
    async def sleep(s):
        pass

    with pytest.raises(CatcherError) as exc:
        await catcher(FakeServer(), lambda: NOW.timestamp(), sleep, password="wrong-pw").wait_for_code(
            RULE, since=NOW)
    assert "refused the login" in exc.value.message and "wrong-pw" not in exc.value.message
    assert "me@gmail.com" not in exc.value.message
    with pytest.raises(CatcherError):
        await catcher(FakeServer(), lambda: NOW.timestamp(), sleep, host="down.example.com").wait_for_code(
            RULE, since=NOW)
    with pytest.raises(CatcherError):
        await catcher(FakeServer(), lambda: NOW.timestamp(), sleep).wait_for_code(
            CatcherRule(sender="@x.com", mailbox="Missing"), since=NOW)
