"""Pins the vault's structural field roles (agentos.vault.policy.classify_fields): explicit
autocomplete, the input before a password, a username-first step, one-time codes by structure
and split into single boxes, and the fields that never get a role."""

from __future__ import annotations

from pinned import check, outcome

from agentos.vault.policy import classify_fields
from agentos.vault.typist import FieldInfo

TOP = ("https://login.example.com",)
IFRAME = ("https://login.example.com", "https://auth.example.com")


def f(locator: str, **kw) -> FieldInfo:
    return FieldInfo(locator=locator, frame_origins=kw.pop("frames", TOP), **kw)


CASES = {
    "empty": [],
    "plain login": [f("u", name="user"), f("p", type="password")],
    "autocomplete username and password": [f("u", autocomplete="username"), f("p", type="password",
                                                                              autocomplete="current-password")],
    "new password ignored": [f("u", autocomplete="username"), f("p", type="password", autocomplete="new-password")],
    "otp by autocomplete text": [f("c", autocomplete="one-time-code")],
    "otp by autocomplete password": [f("c", type="password", autocomplete="one-time-code")],
    "otp autocomplete on a checkbox": [f("c", type="checkbox", autocomplete="one-time-code")],
    "username in another form than password": [f("u", form="a"), f("p", type="password", form="b")],
    "two inputs before the password": [f("s", name="search"), f("u", name="x"), f("p", type="password")],
    "username after the password": [f("p", type="password"), f("u", name="user")],
    "explicit username elsewhere in form": [f("x"), f("u", autocomplete="username"), f("p", type="password")],
    "explicit username in other form": [f("u", autocomplete="username", form="b"), f("x", form="a"),
                                        f("p", type="password", form="a")],
    "iframe password": [f("u", frames=IFRAME), f("p", type="password", frames=IFRAME), f("t", name="top")],
    "hidden disabled readonly": [f("h", visible=False), f("d", disabled=True), f("r", readonly=True),
                                 f("p", type="password")],
    "not an input": [f("t", tag="textarea", name="user"), f("p", type="password")],
    "username first by email type": [f("e", type="email")],
    "username first by email autocomplete": [f("e", autocomplete="email")],
    "username first by hint": [f("u", name="login_id")],
    "username first by id hint": [f("u", id="account")],
    "username first hint but code like": [f("u", name="user_code")],
    "username first, two free inputs": [f("a", name="user"), f("b", name="other")],
    "username first, no hint": [f("a", name="q")],
    "username first beside a checkbox": [f("a", name="email_address"), f("c", type="checkbox")],
    "otp numeric with hint": [f("c", inputmode="numeric", name="otp")],
    "otp tel with id hint": [f("c", type="tel", id="verification")],
    "otp number too long": [f("c", type="number", name="code", maxlength=12)],
    "otp number sized": [f("c", type="number", name="code", maxlength=6)],
    "otp numeric no hint": [f("c", inputmode="numeric", name="amount")],
    "otp in a form with a password": [f("p", type="password"), f("c", inputmode="numeric", name="otp")],
    "split otp 6 boxes": [f(f"b{i}", maxlength=1) for i in range(6)],
    "split otp 3 boxes": [f(f"b{i}", maxlength=1) for i in range(3)],
    "split otp 9 boxes": [f(f"b{i}", maxlength=1) for i in range(9)],
    "split otp 4 boxes and a free input": [*[f(f"b{i}", maxlength=1) for i in range(4)], f("x", name="x")],
    "split otp password boxes": [f(f"b{i}", type="password", maxlength=1) for i in range(4)],
    "formless and forms": [f("u1", name="user"), f("p1", type="password"), f("u2", name="user", form="f"),
                           f("p2", type="password", form="f"), f("c", name="otp", type="tel", form="g")],
    "uppercase type": [f("u", name="user"), f("p", type="PASSWORD")],
    "empty type": [f("u", type="", name="user"), f("p", type="password")],
    "inputmode uppercase": [f("c", inputmode="NUMERIC", name="pin")],
}


def test_classify_fields_is_pinned():
    check("classify_fields", {name: outcome(lambda fs=fs: {k: v.value for k, v in classify_fields(fs).items()})
                              for name, fs in CASES.items()})
