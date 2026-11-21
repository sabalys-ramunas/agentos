"""The step reader of an assisted login (openajar.vault.steps) on fixture login pages.

Each fixture is what the node reads from a real sign-in page of that shape: the typist's fields,
the page's buttons and links, labels and text. tests/fixtures/site/assist/ holds the same pages as
HTML for the node test on a real Camoufox (tests/node/test_login_assist_node.py).
"""

from __future__ import annotations

import json

import pytest

from openajar.core.models import Signal
from openajar.vault.steps import Action, PageRead, StepKind, clean_text, read_step
from openajar.vault.typist import FieldInfo

G = "https://accounts.google.com"
IG = "https://www.instagram.com"


def fld(n: int, origin: str = G, *, frames: tuple[str, ...] | None = None, **kw) -> FieldInfo:
    return FieldInfo(locator=f"f{n}", frame_origins=frames or (origin,), **kw)


def act(n: int, label: str, origin: str = G, *, frames: tuple[str, ...] | None = None, **kw) -> Action:
    return Action(locator=f"a{n}", frame_origins=frames or (origin,), label=label, **kw)


def google_identifier() -> PageRead:
    return PageRead(
        url=f"{G}/v3/signin/identifier?flowName=GlifWebSignIn",
        fields=(fld(1, type="email", name="identifier", id="identifierId", autocomplete="username webauthn", form=0),
                fld(2, type="hidden", name="hiddenPassword", visible=False, form=0)),
        actions=(act(1, "Forgot email?", type="button", form=0), act(2, "Create account", type="button", form=0),
                 act(3, "Next", type="button", form=0)),
        labels={"f1": "Email or phone"}, heading="Sign in", message="to continue to Gmail",
        signal=Signal.LOGGED_OUT)


def google_password() -> PageRead:
    return PageRead(
        url=f"{G}/v3/signin/challenge/pwd?TL=abc",
        fields=(fld(1, type="email", name="identifier", visible=False, form=0),
                fld(2, type="password", name="Passwd", autocomplete="current-password", form=0),
                fld(3, type="checkbox", name="showPassword", form=0)),
        actions=(act(1, "Forgot password?", type="button", form=0), act(2, "Next", type="button", form=0)),
        labels={"f2": "Enter your password"}, heading="Welcome", message="ada@example.org",
        signal=Signal.LOGGED_OUT)


def instagram() -> PageRead:
    return PageRead(
        url=f"{IG}/accounts/login/",
        fields=(fld(1, IG, type="text", name="username", form=0, maxlength=75),
                fld(2, IG, type="password", name="password", form=0)),
        actions=(act(1, "Show", IG, type="button", form=0), act(2, "Log in", IG, type="submit", form=0),
                 act(3, "Log in with Facebook", IG, type="button", form=0, hint="facebook-login"),
                 act(4, "Forgot password?", IG, tag="a", href_host="www.instagram.com")),
        labels={"f1": "Phone number, username, or email", "f2": "Password"},
        signal=Signal.LOGGED_OUT)


def google_code() -> PageRead:
    return PageRead(
        url=f"{G}/v3/signin/challenge/totp?TL=abc",
        fields=(fld(1, type="tel", name="totpPin", id="totpPin", autocomplete="one-time-code", form=0),),
        actions=(act(1, "Try another way", type="button", form=0), act(2, "Next", type="submit", form=0)),
        labels={"f1": "Enter code"}, heading="2-Step Verification",
        message="Get a verification code from the Google Authenticator app", signal=Signal.CHECKPOINT)


def google_phone(*, approve: bool = True, number: str | None = "97") -> PageRead:
    return PageRead(
        url=f"{G}/v3/signin/challenge/dp?TL=abc",
        actions=(act(1, "Resend it", type="button"), act(2, "Try another way", type="button")),
        heading="2-Step Verification",
        message="Open the Gmail app on iPhone. Google sent a notification to your iPhone. Tap Yes on the "
                "notification, then tap 97 on your phone to verify it's you.",
        number=number, signal=Signal.CHECKPOINT, approve=approve)


def google_chooser() -> PageRead:
    return PageRead(
        url=f"{G}/v3/signin/accountchooser?flowName=GlifWebSignIn",
        actions=(act(1, "Ada Lovelace ada@example.org", tag="div", role="link", identifier=True),
                 act(2, "Bo bo@example.org", tag="div", role="link", identifier=True),
                 act(3, "Use another account", tag="div", role="link"),
                 act(4, "Help", tag="a", href_host="support.google.com")),
        heading="Choose an account", signal=Signal.LOGGED_OUT)


def test_google_style_email_then_password():
    step = read_step(google_identifier())
    assert step.kind is StepKind.SIGN_IN and step.origin == G
    assert [(a.name, a.label, a.secret, a.locators) for a in step.asks] == [("username", "Email or phone", False,
                                                                              ("f1",))]
    assert step.submit is None and step.submit_label == ""  # Google's Next is type=button: Enter in the field
    assert step.choices == () and (step.heading, step.message) == ("Sign in", "to continue to Gmail")
    step = read_step(google_password())
    assert step.kind is StepKind.SIGN_IN
    assert [(a.name, a.label, a.secret) for a in step.asks] == [("password", "Enter your password", True)]


def test_instagram_style_email_and_password_on_one_page():
    step = read_step(instagram())
    assert step.kind is StepKind.SIGN_IN and step.origin == IG
    assert [a.name for a in step.asks] == ["username", "password"]
    assert [a.label for a in step.asks] == ["Phone number, username, or email", "Password"]
    assert step.asks[0].length == 75
    assert (step.submit, step.submit_label) == ("a2", "Log in")  # the form's own submit button
    assert [(c.label, c.kind) for c in step.choices] == [("Log in with Facebook", "provider")]  # another way in


def test_a_code_step_and_a_split_code():
    step = read_step(google_code())
    assert step.kind is StepKind.CODE and [(a.name, a.secret) for a in step.asks] == [("code", True)]
    assert (step.submit, step.submit_label) == ("a2", "Next")
    boxes = tuple(fld(i, type="text", inputmode="numeric", maxlength=1, form=0) for i in range(1, 7))
    split = read_step(PageRead(url=f"{G}/challenge", fields=boxes, signal=Signal.CHECKPOINT))
    assert split.kind is StepKind.CODE and split.asks[0].locators == tuple(f"f{i}" for i in range(1, 7))
    assert split.asks[0].length == 6 and split.submit is None


def test_tap_the_number_on_your_phone_waits():
    step = read_step(google_phone())
    assert step.kind is StepKind.APPROVE and step.number == "97" and not step.asks
    assert "tap 97 on your phone" in step.message
    assert [c.label for c in step.choices] == ["Resend it", "Try another way"]  # the owner may pick another way
    # a page that shows a big number and no fields waits too, with no site rule
    assert read_step(google_phone(approve=False)).kind is StepKind.APPROVE
    # the site rule alone (Google's prompt without a number) waits as well
    assert read_step(google_phone(number=None)).kind is StepKind.APPROVE


def test_an_account_chooser_and_a_way_to_sign_in():
    step = read_step(google_chooser())
    assert step.kind is StepKind.CHOOSE
    assert [(c.id, c.label, c.kind) for c in step.choices] == [
        ("c1", "Ada Lovelace ada@example.org", "account"), ("c2", "Bo bo@example.org", "account"),
        ("c3", "Use another account", "button")]  # a plain help link is not offered
    medium = PageRead(url="https://medium.com/m/signin", actions=(
        act(1, "Sign in with Google", "https://medium.com", hint="susi-google"),
        act(2, "Sign in with Apple", "https://medium.com", tag="a", href_host="appleid.apple.com"),
        act(3, "Sign in with email", "https://medium.com", hint="susi-email"),
        act(4, "Terms of service", "https://medium.com", tag="a", href_host="policy.medium.com")))
    step = read_step(medium)
    assert step.kind is StepKind.CHOOSE and [c.kind for c in step.choices] == ["provider"] * 3
    consent = PageRead(url=f"{G}/signin/oauth/consent", actions=(act(1, "Continue"),))
    step = read_step(consent)
    assert step.kind is StepKind.CONFIRM and (step.submit, step.submit_label) == ("a1", "Continue")


def test_a_captcha_stops_and_offers_take_over():
    page = PageRead(url="https://www.facebook.com/login/", fields=instagram().fields, signal=Signal.CAPTCHA,
                    signal_detail="Arkose (FunCaptcha) frame")
    step = read_step(page)
    assert step.kind is StepKind.CAPTCHA and step.stops and not step.asks
    assert "take control" in step.reason and "—" not in step.reason
    blocked = read_step(PageRead(url=IG + "/", signal=Signal.BLOCKED))
    assert blocked.kind is StepKind.STUCK and "blocked" in blocked.reason


def test_signed_in_needs_the_session_and_an_ok_signal():
    assert read_step(PageRead(url=IG + "/", signed_in=True)).kind is StepKind.SIGNED_IN
    # the cookie with a checkpoint page is not done yet
    assert read_step(PageRead(url=IG + "/challenge/", signed_in=True, signal=Signal.CHECKPOINT)).kind \
        is not StepKind.SIGNED_IN


def test_what_it_refuses_to_ask_for():
    # login fields in a frame from another site
    framed = PageRead(url="https://shop.example/login", fields=(
        fld(1, frames=("https://shop.example", "https://evil.example"), type="email", form=0),
        fld(2, frames=("https://shop.example", "https://evil.example"), type="password", form=0)))
    step = read_step(framed)
    assert step.kind is StepKind.STUCK and "another site" in step.reason and not step.asks
    # plain http off loopback
    assert read_step(PageRead(url="http://shop.example/login", fields=instagram().fields)).kind is StepKind.STUCK
    # loopback http is fine (local fixtures)
    local = PageRead(url="http://127.0.0.1:6120/login.html", fields=(
        fld(1, "http://127.0.0.1:6120", type="email", form=0), fld(2, "http://127.0.0.1:6120", type="password", form=0)))
    assert read_step(local).kind is StepKind.SIGN_IN and read_step(local).origin == "http://127.0.0.1:6120"
    # an onboarding form asks for a name: not the vault's to fill
    onboarding = PageRead(url="https://www.linkedin.com/onboarding/", fields=(
        fld(1, "https://www.linkedin.com", type="text", name="firstName", form=0),),
        actions=(act(1, "Next", "https://www.linkedin.com", type="submit", form=0),))
    assert read_step(onboarding).kind is StepKind.STUCK
    # nothing to fill or press
    assert read_step(PageRead(url=IG + "/x")).kind is StepKind.STUCK


def test_the_shape_names_what_the_step_acts_on_and_public_has_no_locator():
    a, b = read_step(instagram()), read_step(instagram())
    assert a.shape == b.shape
    moved = PageRead(url=instagram().url, fields=(fld(1, IG, type="text", name="username", form=0),
                                                  fld(9, IG, type="password", name="password", form=0)),
                     actions=instagram().actions)
    assert read_step(moved).shape != a.shape
    out = json.dumps(a.public())
    assert "f1" not in out and "a2" not in out and '"submit": "Log in"' in out


@pytest.mark.parametrize("raw, want", [
    ("  Sign\n in\t", "Sign in"), ("a\x00b", "a b"), ("x" * 100, "x" * 79 + "…"), (None, "")])
def test_clean_text(raw, want):
    assert clean_text(raw, 80) == want
