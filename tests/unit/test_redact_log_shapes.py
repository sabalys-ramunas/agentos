"""Shapes the shared redaction must catch in free text (log lines, run errors), what it must leave
alone, and the address masker the node uses on everything a controller reads."""

import pytest

from openajar.policy.redact import mask_addresses, redact_text


@pytest.mark.parametrize("text, secret", [
    ("2FA code: 482913", "482913"),
    ("otp 482913", "482913"),
    ("one-time password 482913", "482913"),
    ("verification code is 482913", "482913"),
    ("code=482913", "482913"),
    ("Set cookie connect.sid=s%3Aabc123def456.sig", "abc123def456"),
    ("cookies: xs=12%3AabcDEF%3A2%3A1700000000; c_user=1", "abcDEF"),
    ("GET https://example.com/login?sid=0f9e8d7c6b5a", "0f9e8d7c6b5a"),
    ("PHPSESSID=q1w2e3r4t5y6u7i8", "q1w2e3r4t5y6u7i8"),
    ("wrong password hunter2", "hunter2"),
    ("wrong password hunter", "hunter"),
    ("password is hunter2", "hunter2"),
])
def test_secret_shapes_in_free_text_are_redacted(text, secret):
    assert secret not in redact_text(text)


@pytest.mark.parametrize("text", [
    "exit code 1",
    "error code: 404",
    "HTTP status code 503 after 482913 ms",
    "lane biz-a/social warmed in 1200 ms, 3 tabs",
    "port 8080 open, pid 482913",
    "version 1.2.3 on 2026-11-04T10:00:00.123Z",
    "wrong password for biz-a-login",
    "password field not found on the page",
    "the password reset mail was sent",
    "session 3 of 5",
])
def test_ordinary_log_lines_keep_their_numbers_and_words(text):
    assert redact_text(text) == text


@pytest.mark.parametrize("text, kept", [
    ("exit IP 203.0.113.7 via 198.51.100.2:8080", ["exit IP [address] via [address]:8080"]),
    ("exit 2001:db8::1 and 2a02:6b8::2:242 seen", ["exit [address] and [address] seen"]),
    ("full 2001:0db8:85a3:0000:0000:8a2e:0370:7334 here", ["full [address] here"]),
    ("bracketed [2001:db8::7]:443", ["bracketed [[address]]:443"]),
    ("loopback ::1 and ::ffff:192.0.2.1", ["[address]"]),
])
def test_addresses_are_masked(text, kept):
    masked = mask_addresses(text)
    for part in kept:
        assert part in masked, masked
    for ip in ("203.0.113.7", "198.51.100.2", "2001:db8", "2a02:6b8", "::1 ", "192.0.2.1"):
        assert ip not in masked


@pytest.mark.parametrize("text", [
    "openajar 1.2.3 (camoufox 135.0.1)",
    "at 2026-11-04T10:12:34.567Z, 10:12:34 local",
    "took 12:34:56 to finish",
    "the class Foo::Bar and std::vector",
    "values 300.1.2.3 and 1.2.3.4.5 are not addresses",
])
def test_version_strings_times_and_paths_are_not_addresses(text):
    assert mask_addresses(text) == text
