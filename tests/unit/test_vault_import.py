"""Importers: Apple Passwords, Bitwarden JSON, 1Password, generic CSV; plans and shredding."""

import json
import os

import pytest

from agentos.vault import importers
from agentos.vault.errors import EntryInvalid
from agentos.vault.importers import (
    detect_format,
    parse_bindings,
    parse_bitwarden_json,
    parse_csv,
    parse_file,
    shred,
)
from agentos.vault.models import EntryKind

SEED = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
SECRETS = ("ig-pass-1", "reddit-pass-2", "bank-pass-3", "me@example.org", SEED, "my secret note")

APPLE = f"""Title,URL,Username,Password,Notes,OTPAuth
instagram.com (me@example.org),https://www.instagram.com/,me@example.org,ig-pass-1,,otpauth://totp/IG:me?secret={SEED}&issuer=IG
reddit.com,https://old.reddit.com/login,me,reddit-pass-2,my secret note,
Some App,androidapp://com.bank.app,me,bank-pass-3,,
empty,https://example.com,me,,,
"""

ONEPASSWORD = """Title,Url,Username,Password,OTPAuth,Favorite,Archived,Tags,Notes
Instagram,instagram.com,me@example.org,ig-pass-1,,false,false,,
Reddit,https://www.reddit.com,me,reddit-pass-2,,true,false,social,
"""

CHROME = """name,url,username,password,note
www.instagram.com,https://www.instagram.com/accounts/login/,me@example.org,ig-pass-1,
bücher,https://bücher.de/login,me,reddit-pass-2,
"""

BITWARDEN = {
    "encrypted": False,
    "items": [
        {"type": 1, "name": "Instagram", "notes": None,
         "login": {"username": "me@example.org", "password": "ig-pass-1", "totp": SEED,
                   "uris": [{"uri": "androidapp://com.instagram.android"}, {"uri": "https://www.instagram.com"}]}},
        {"type": 2, "name": "Recovery codes", "notes": "my secret note"},
        {"type": 3, "name": "Visa", "card": {"number": "4111"}},
        {"type": 1, "name": "No password", "login": {"username": "x", "uris": [{"uri": "https://x.com"}]}},
    ],
}


def no_secrets(obj):
    text = json.dumps(obj, default=str) + repr(obj)
    for s in SECRETS:
        assert s not in text, s


def test_apple_csv():
    plan = parse_csv(APPLE)
    assert [(r.name, r.kind, r.domain) for r in plan.records] == [
        ("instagram.com", EntryKind.PASSWORD, "instagram.com"),     # username taken out of the name
        ("reddit.com", EntryKind.PASSWORD, "reddit.com"),
    ]
    assert plan.records[0].totp is not None and plan.records[0].username.reveal() == "me@example.org"
    assert plan.skipped == {"no web domain": 1, "no password or TOTP": 1}
    assert plan.dropped_notes == 1
    no_secrets(plan.records)


@pytest.mark.parametrize("title,user,name", [
    ("instagram.com (me@example.org)", "me@example.org", "instagram.com"),
    ("Me@Example.org - Instagram", "me@example.org", "Instagram"),
    ("me@example.org", "me@example.org", "instagram.com"),
    ("Instagram", "me@example.org", "Instagram"),
])
def test_usernames_are_taken_out_of_public_names(title, user, name):
    plan = parse_csv(f"title,url,username,password\n{title},https://instagram.com,{user},ig-pass-1\n")
    assert plan.records[0].name == name


def test_onepassword_and_chrome_csv():
    one = parse_csv(ONEPASSWORD)
    assert [r.domain for r in one.records] == ["instagram.com", "reddit.com"]
    chrome = parse_csv(CHROME)
    assert [r.domain for r in chrome.records] == ["instagram.com", "xn--bcher-kva.de"]


def test_bitwarden_json():
    plan = parse_bitwarden_json(json.dumps(BITWARDEN))
    kinds = [(r.name, r.kind, r.domain) for r in plan.records]
    assert kinds == [("Instagram", EntryKind.PASSWORD, "instagram.com"), ("Recovery codes", EntryKind.NOTE, None)]
    assert plan.skipped == {"card or identity": 1, "no password or TOTP": 1}
    with pytest.raises(EntryInvalid) as exc:
        parse_bitwarden_json(json.dumps({"encrypted": True, "items": []}))
    assert "unencrypted" in exc.value.message
    with pytest.raises(EntryInvalid):
        parse_bitwarden_json("{not json")


def test_detect_format(tmp_path):
    p = tmp_path / "x"
    assert detect_format(p, APPLE) == "apple"
    assert detect_format(p, ONEPASSWORD) == "1password"
    assert detect_format(p, CHROME) == "csv"
    assert detect_format(p, "﻿" + json.dumps(BITWARDEN)) == "bitwarden"


def test_csv_without_secret_columns_is_refused():
    with pytest.raises(EntryInvalid):
        parse_csv("name,url\nx,https://x.com\n")
    assert parse_csv("").records == []


def test_bindings():
    b = parse_bindings(["www.instagram.com=biz-a/instagram", "instagram.com=personal/*", "reddit.com=me/reddit"])
    assert b == {"instagram.com": ["biz-a/instagram", "personal/*"], "reddit.com": ["me/reddit"]}
    for bad in ["instagram.com", "=biz-a/x", "instagram.com=", "com=biz-a/x", "instagram.com=Bad Lane"]:
        with pytest.raises(EntryInvalid):
            parse_bindings([bad])


def test_plan_summary_and_entries():
    plan = parse_csv(APPLE + "instagram.com (me2),https://instagram.com,me2,ig-pass-1b,,\n")
    bindings = parse_bindings(["instagram.com=biz-a/instagram"])
    summary = plan.summary(bindings, existing={"reddit.com"})
    assert summary["importable"] == 3 and summary["kinds"] == {"password": 3}
    assert summary["with_totp"] == 1 and summary["bound"] == 2 and summary["unbound"] == 1
    assert summary["conflicts"] == 1
    assert summary["domains"]["instagram.com"] == {"count": 2, "lanes": ["biz-a/instagram"]}
    no_secrets(summary)
    entries, _ = plan.entries(bindings)
    assert [e.name for e in entries] == ["instagram.com", "reddit.com", "instagram.com (2)"]
    only, skipped = plan.entries(bindings, only_bound=True)
    assert len(only) == 2 and skipped == {"unbound (--only-bound)": 1}


def test_invalid_rows_are_skipped_with_a_reason_not_a_value():
    plan = parse_csv("name,url,username,password,totp\nbad,https://x.com,me,ig-pass-1,not-a-seed!!\n")
    entries, skipped = plan.entries({})
    assert entries == [] and list(skipped) == ["invalid (TOTP seed is not valid base32)"]
    no_secrets(skipped)


def test_file_size_limit_and_encoding(tmp_path, monkeypatch):
    p = tmp_path / "big.csv"
    p.write_text(APPLE)
    monkeypatch.setattr(importers, "MAX_FILE_BYTES", 10)
    with pytest.raises(EntryInvalid):
        parse_file(p)
    monkeypatch.undo()
    p.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(EntryInvalid):
        parse_file(p)
    with pytest.raises(EntryInvalid):
        parse_file(p, "keepass")


def test_shred_overwrites_then_deletes(tmp_path):
    p = tmp_path / "export.csv"
    p.write_text(APPLE)
    link = tmp_path / "hardlink.csv"
    os.link(p, link)                      # watch the same inode after the name is gone
    shred(p)
    assert not p.exists()
    assert link.read_bytes() == b""       # overwritten, then truncated
