"""Pins the assisted login's step reader (agentos.vault.steps.read_step): the whole Step for each
kind of sign-in page, from the signal and the fields' structure."""

from __future__ import annotations

from pinned import check, outcome, plain

from agentos.core.models import Signal
from agentos.vault.steps import Action, PageRead, read_step
from agentos.vault.typist import FieldInfo

URL = "https://login.example.com/signin"
TOP = ("https://login.example.com",)
OTHER = ("https://login.example.com", "https://idp.other.com")


def f(locator: str, **kw) -> FieldInfo:
    return FieldInfo(locator=locator, frame_origins=kw.pop("frames", TOP), **kw)


def btn(locator: str, label: str = "Next", **kw) -> Action:
    return Action(locator=locator, frame_origins=kw.pop("frames", TOP), label=label, **kw)


LOGIN = (f("u", name="user", form="f"), f("p", type="password", form="f"))
SUBMIT = btn("go", "Sign in", form="f", type="submit")
ACCOUNTS = (btn("a1", "ann@example.com", identifier=True, tag="div"), btn("a2", "bob@example.com", identifier=True,
                                                                         tag="div"))
GOOGLE = btn("g", "Continue with Google", tag="a", href_host="accounts.google.com")
SSO = btn("sso", "Use SSO", hint="sso-login")
PLAIN = btn("ok", "Continue")

CASES = {
    "signed in": PageRead(URL, signed_in=True),
    "signed in but captcha": PageRead(URL, signed_in=True, signal=Signal.CAPTCHA),
    "captcha": PageRead(URL, fields=LOGIN, signal=Signal.CAPTCHA),
    "blocked": PageRead(URL, signal=Signal.BLOCKED),
    "rate limited": PageRead(URL, signal=Signal.RATE_LIMITED),
    "http page": PageRead("http://login.example.com/", fields=LOGIN),
    "http loopback": PageRead("http://127.0.0.1:8080/login", fields=(f("p", type="password",
                                                                        frames=("http://127.0.0.1:8080",)),)),
    "login in another site's frame": PageRead(URL, fields=(f("u", name="user", frames=OTHER),
                                                           f("p", type="password", frames=OTHER))),
    "username and password with submit": PageRead(URL, fields=LOGIN, actions=(SUBMIT,),
                                                  labels={"u": "  Email\naddress ", "p": "Your password"}),
    "username and password, enter": PageRead(URL, fields=LOGIN),
    "password only": PageRead(URL, fields=(f("p", type="password", form="f"),), actions=(SUBMIT,)),
    "username first": PageRead(URL, fields=(f("e", type="email", form="f"),), actions=(SUBMIT,)),
    "username first, formless": PageRead(URL, fields=(f("e", type="email"),), actions=(SUBMIT,)),
    "username in another form than the password": PageRead(URL, fields=(
        f("u", autocomplete="username", form="a"), f("p", type="password", form="b")), actions=(SUBMIT,)),
    "code field": PageRead(URL, fields=(f("c", autocomplete="one-time-code", form="f", maxlength=6),),
                           actions=(SUBMIT,)),
    "split code": PageRead(URL, fields=tuple(f(f"b{i}", maxlength=1, form="f") for i in range(6)), actions=(SUBMIT,)),
    "code beside a password": PageRead(URL, fields=(f("p", type="password", form="f"),
                                                    f("c", autocomplete="one-time-code", form="f"))),
    "submit in another frame": PageRead(URL, fields=LOGIN, actions=(btn("go", "Sign in", form="f", type="submit",
                                                                        frames=OTHER),)),
    "submit disabled, reset, hidden": PageRead(URL, fields=LOGIN, actions=(
        btn("x", form="f", type="submit", disabled=True), btn("r", form="f", type="reset"),
        btn("h", form="f", type="submit", visible=False), btn("i", "Go", form="f", type="image", tag="input"))),
    "sign in with accounts and providers": PageRead(URL, fields=LOGIN, actions=(SUBMIT, *ACCOUNTS, GOOGLE, SSO,
                                                                                PLAIN)),
    "approve flag": PageRead(URL, approve=True, number="42", actions=(PLAIN,)),
    "number, no fields": PageRead(URL, number="97", heading="Check your phone"),
    "number with a visible field": PageRead(URL, number="97", fields=(f("q", name="q", type="search"),)),
    "unfillable field": PageRead(URL, fields=(f("n", name="first_name", form="f"),), actions=(SUBMIT,)),
    "unfillable field in another frame": PageRead(URL, fields=(f("n", name="first_name", form="f", frames=OTHER),),
                                                  actions=(SUBMIT, PLAIN)),
    "nothing to press": PageRead(URL, heading="Hmm", message="Something\x00 went\twrong"),
    "one plain button": PageRead(URL, actions=(PLAIN,)),
    "one account": PageRead(URL, actions=ACCOUNTS[:1]),
    "choose": PageRead(URL, actions=(*ACCOUNTS, GOOGLE, SSO, PLAIN, btn("no-label", "")), heading="Choose"),
    "many choices": PageRead(URL, actions=tuple(btn(f"b{i}", f"Option {i}") for i in range(12))),
    "same locator twice": PageRead(URL, actions=(btn("same", "A", identifier=True), btn("same", "A again"))),
    "role buttons and links": PageRead(URL, actions=(btn("r1", "Tab", tag="div", role="tab"),
                                                     btn("l1", "Plain link", tag="a"), btn("l2", "GitHub", tag="a",
                                                                                          href_host="github.com"))),
    "long heading and message": PageRead(URL, heading="H" * 300, message="M " * 400, actions=(PLAIN,)),
}


def test_read_step_is_pinned():
    check("read_step", {name: outcome(lambda p=p: plain(read_step(p))) for name, p in CASES.items()})
