"""The step reader of an assisted login: what one sign-in page asks the owner for, from structure.

An assisted login (openajar.node.login_assist) shows the owner a small form with exactly the fields
the current page needs, types what they enter with the vault's typist, presses the step's continue
button, and reads the next page. This module is the reading part, as a pure function over facts the
node collected (`PageRead`), so every rule is unit tested without a browser.

Facts are structural, as everywhere in the vault (openajar.vault.policy): field roles come from
`classify_fields` (type, autocomplete, form, attribute names), buttons from their tag, type, role,
form and developer-written attributes, and the page's state from the signal rules (openajar.node.sites).
Page text is collected only to show it to the owner (the heading, the message, a field's label, a
button's label, the number of a "tap 97 on your phone" prompt). It never decides anything.

Steps, first match wins:

1. signed_in   the site's session cookie is there and the signal is ok.
2. captcha     a challenge renders (the signal says captcha), or the site blocked or rate limited
   stuck       the lane. The owner takes control in the live view.
3. stuck       the page is not https (http only on loopback): nothing is typed into it.
4. sign_in     username and/or password fields; code: one-time-code fields. The continue button is
   code        the submit button of the fields' own form (same frame chain); without one the node
               presses Enter in the last field, as the vault's `login` does. Login fields inside a
               frame from another origin are never asked for.
5. approve     the site's "approve on your phone" page, or a page that shows a big number and no
               fields: the owner approves on their phone and openajar waits for the page to move.
6. stuck       a form asks for something the vault does not fill (a name, a birthday).
7. choose      no fields and several buttons: accounts to pick (data-identifier, data-email), other
   confirm     ways to sign in (identity-provider links, provider hints), consent and confirm buttons.
               One plain button is a confirm. Nothing to press: stuck.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from openajar.core.models import Signal
from openajar.core.signals import FORM_STUCK
from openajar.vault.policy import (
    TEXTLIKE,
    FieldRole,
    classify_fields,
    interactable,
    otp_group,
    same_origin_chain,
    secure_origin,
)
from openajar.vault.typist import FieldInfo

MAX_CHOICES = 8
LABEL_CHARS = 80
MESSAGE_CHARS = 600

# Hosts a "continue with ..." link goes to (registrable domains and the sign-in hosts on them).
PROVIDER_HOSTS = ("accounts.google.com", "appleid.apple.com", "www.facebook.com", "facebook.com",
                  "login.microsoftonline.com", "login.live.com", "github.com", "api.twitter.com", "x.com")
# Developer-written hints (id, name, class, data-provider, data-testid) of a "continue with ..." button.
_PROVIDER_HINT = re.compile(r"(google|apple|facebook|microsoft|github|twitter|sso|oauth|social|e-?mail)",
                            re.IGNORECASE)
BUTTON_ROLES = frozenset({"button", "link", "option", "menuitem", "radio", "tab"})

DEFAULT_LABELS = {"username": "Email or username", "password": "Password", "code": "Code"}


class StepKind(StrEnum):
    SIGNED_IN = "signed_in"
    SIGN_IN = "sign_in"        # username and/or password
    CODE = "code"              # a one-time code
    APPROVE = "approve"        # approve the sign-in on another device, then the page moves on its own
    CHOOSE = "choose"          # pick one of several buttons (an account, a way to sign in)
    CONFIRM = "confirm"        # press the page's one button (consent, continue)
    CAPTCHA = "captcha"        # a challenge: the owner solves it in the live view
    STUCK = "stuck"            # something openajar cannot read or fill: the owner takes control


STOPS = frozenset({StepKind.CAPTCHA, StepKind.STUCK})


@dataclass(frozen=True)
class Action:
    """One clickable thing on the page, structurally. `label` is what the page shows on it, for the
    owner's eyes only."""

    locator: str
    frame_origins: tuple[str, ...]
    tag: str = "button"
    type: str = ""                 # a button's or input's `type` property (submit, button, reset, image)
    role: str = ""                 # the `role` attribute
    form: str | int | None = None  # form owner, keyed as FieldInfo.form
    label: str = ""
    href_host: str = ""            # a link's target host
    hint: str = ""                 # id, name, class, data-provider, data-testid (developer-written)
    identifier: bool = False       # an account item (data-identifier, data-email)
    visible: bool = True
    disabled: bool = False

    @property
    def submit(self) -> bool:
        if self.form is None:
            return False
        if self.tag == "button":
            return self.type in ("submit", "")
        return self.tag == "input" and self.type in ("submit", "image")

    @property
    def usable(self) -> bool:
        return self.visible and not self.disabled and self.type != "reset"

    @property
    def provider(self) -> bool:
        host = self.href_host.lower()
        return any(host == h or host.endswith("." + h) for h in PROVIDER_HOSTS) or bool(
            self.hint and _PROVIDER_HINT.search(self.hint))

    @property
    def button_like(self) -> bool:
        if self.tag == "button" or (self.tag == "input" and self.type in ("submit", "button", "image")):
            return True
        return self.role.lower() in BUTTON_ROLES or self.identifier or (self.tag == "a" and self.provider)


@dataclass(frozen=True)
class PageRead:
    """What the node read from the active page of the lane."""

    url: str
    fields: tuple[FieldInfo, ...] = ()
    actions: tuple[Action, ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)   # field locator -> the page's label for it
    heading: str = ""
    message: str = ""
    number: str | None = None      # a big 1 to 3 digit number shown on its own
    signal: Signal = Signal.OK
    signal_detail: str | None = None
    signed_in: bool = False        # the lane's site session cookie is there (the signal decides with it)
    approve: bool = False          # the site table says this URL waits for an approval on a phone

    def fields_visible(self) -> bool:
        return any(interactable(f) and (f.type or "text").lower() in (*TEXTLIKE, "password") for f in self.fields)


@dataclass(frozen=True)
class Ask:
    """One value the owner types: a username, a password or a one-time code."""

    name: str                      # username | password | code
    role: FieldRole
    label: str
    secret: bool
    locators: tuple[str, ...]      # one field, or the boxes of a split code in order
    length: int | None = None      # boxes of a split code, or the field's maxlength

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "secret": self.secret, "length": self.length,
                "autocomplete": {"username": "username", "password": "current-password",
                                 "code": "one-time-code"}[self.name]}


@dataclass(frozen=True)
class Choice:
    id: str
    label: str
    locator: str
    kind: str = "button"           # account | provider | button

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "kind": self.kind}


@dataclass(frozen=True)
class Step:
    kind: StepKind
    origin: str = ""               # the top document's origin: the only origin this step types into
    asks: tuple[Ask, ...] = ()
    submit: str | None = None      # the continue button's locator; None: Enter in the last field
    submit_label: str = ""
    choices: tuple[Choice, ...] = ()
    heading: str = ""
    message: str = ""
    number: str | None = None
    reason: str = ""               # captcha and stuck: why, in plain words

    @property
    def stops(self) -> bool:
        return self.kind in STOPS

    @property
    def shape(self) -> str:
        """What the step would act on: a step read again with the same shape is the same step."""
        parts = [self.kind.value, self.origin, self.submit or "",
                 *("|".join(a.locators) for a in self.asks), *(c.locator for c in self.choices)]
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]

    def choice(self, choice_id: str) -> Choice | None:
        return next((c for c in self.choices if c.id == choice_id), None)

    def public(self) -> dict[str, Any]:
        """What the owner's form shows. No locator, no value."""
        return {"kind": self.kind.value, "heading": self.heading, "message": self.message,
                "number": self.number, "fields": [a.public() for a in self.asks],
                "choices": [c.public() for c in self.choices], "submit": self.submit_label or None,
                "reason": self.reason or None}


def clean_text(text: str | None, limit: int) -> str:
    """Page text as one line for the owner's form: whitespace folded, control characters dropped."""
    if not text:
        return ""
    out = " ".join("".join(ch if ch.isprintable() else " " for ch in str(text)).split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def origin_text(url: str) -> str:
    """The URL's origin as text (`https://host[:port]`), or "" when it is not usable for a secret."""
    origin, _ = secure_origin(url)
    if origin is None:
        return ""
    default = {"https": 443, "http": 80}.get(origin.scheme)
    port = "" if origin.port in (None, default) else f":{origin.port}"
    return f"{origin.scheme}://{origin.host}{port}"


def _same_form(a: FieldInfo | Action, b: FieldInfo) -> bool:
    return a.form == b.form and tuple(a.frame_origins) == tuple(b.frame_origins)


def _label(page: PageRead, f: FieldInfo, name: str) -> str:
    return clean_text(page.labels.get(f.locator), LABEL_CHARS) or DEFAULT_LABELS[name]


def _submit_for(page: PageRead, last: FieldInfo, top: Any) -> Action | None:
    """The continue button of the form the last field belongs to, in the same frame chain."""
    if last.form is None:
        return None
    mine = [a for a in page.actions if a.usable and a.submit and _same_form(a, last)
            and same_origin_chain(top, a.frame_origins)]
    return mine[0] if mine else None


def _choices(page: PageRead, top: Any, *, alternatives: bool) -> list[Choice]:
    """Buttons to offer: accounts first, then other ways to sign in, then plain buttons (only when
    `alternatives` is False: a form with fields offers accounts and providers only)."""
    usable = [a for a in page.actions if a.usable and a.button_like and same_origin_chain(top, a.frame_origins)
              and clean_text(a.label, LABEL_CHARS)]
    accounts = [a for a in usable if a.identifier]
    providers = [a for a in usable if not a.identifier and a.provider]
    buttons = [] if alternatives else [a for a in usable if not a.identifier and not a.provider]
    out: list[Choice] = []
    seen: set[str] = set()
    for kind, group in (("account", accounts), ("provider", providers), ("button", buttons)):
        for a in group:
            if a.locator in seen or len(out) >= MAX_CHOICES:
                continue
            seen.add(a.locator)
            out.append(Choice(id=f"c{len(out) + 1}", label=clean_text(a.label, LABEL_CHARS), locator=a.locator,
                              kind=kind))
    return out


def _unfillable(page: PageRead, top: Any, roles: Mapping[str, FieldRole]) -> bool:
    """A form that would submit a field the vault does not fill (a name, a birthday, a new password)."""
    forms_with_submit = {(a.form, a.frame_origins) for a in page.actions if a.usable and a.submit}
    for f in page.fields:
        if f.locator in roles or not interactable(f) or not same_origin_chain(top, f.frame_origins):
            continue
        kind = (f.type or "text").lower()
        if (kind in TEXTLIKE or kind == "password") and (f.form, tuple(f.frame_origins)) in forms_with_submit:
            return True
    return False


def read_step(page: PageRead) -> Step:
    """The step this page is, and what the owner's form asks for. See the module doc."""
    heading = clean_text(page.heading, LABEL_CHARS * 2)
    message = clean_text(page.message, MESSAGE_CHARS)
    origin = origin_text(page.url)
    base = {"origin": origin, "heading": heading, "message": message}

    stop = _signal_step(page, base)
    if stop is not None:
        return stop
    top, _ = secure_origin(page.url)
    if top is None:
        return Step(StepKind.STUCK, reason="This page is not on https, so openajar does not type into it. Take "
                                           "control if you want to go on by hand.", **base)

    roles = classify_fields(page.fields)
    login = [f for f in page.fields if f.locator in roles]
    local = [f for f in login if same_origin_chain(top, f.frame_origins)]
    if login and not local:
        return Step(StepKind.STUCK, reason="The sign-in form is inside a frame from another site, so openajar "
                                           "does not type into it. Take control to sign in by hand.", **base)

    asks, typed = _asks(page, local, roles)
    if asks:
        button = _submit_for(page, typed[-1], top)
        kind = StepKind.CODE if all(a.name == "code" for a in asks) else StepKind.SIGN_IN
        return Step(kind, asks=tuple(asks), submit=button.locator if button else None,
                    submit_label=clean_text(button.label, LABEL_CHARS) if button else "",
                    choices=tuple(_choices(page, top, alternatives=True)), **base)
    return _fieldless_step(page, top, roles, base)


def _signal_step(page: PageRead, base: dict[str, Any]) -> Step | None:
    """1 and 2: signed in, or a page the owner must take over (captcha, blocked, rate limited)."""
    if page.signed_in and page.signal is Signal.OK:
        return Step(StepKind.SIGNED_IN, **base)
    if page.signal is Signal.CAPTCHA:
        return Step(StepKind.CAPTCHA, reason="The page shows a captcha. openajar does not solve captchas: take "
                                             "control, solve it in the live view, then come back to the form.",
                    **base)
    if page.signal in FORM_STUCK:
        what = "blocked this browser" if page.signal is Signal.BLOCKED else "says to slow down"
        return Step(StepKind.STUCK, reason=f"The site {what}. Take control to see the page, or try again later.",
                    **base)
    return None


def _login_fields(local: list[FieldInfo], roles: Mapping[str, FieldRole]
                  ) -> tuple[FieldInfo | None, FieldInfo | None, FieldInfo | None]:
    """(username, password, code) fields of this step: the first password, the username of its form
    (with no password, the first username), and the first code field."""
    pw = next((f for f in local if roles[f.locator] is FieldRole.PASSWORD), None)
    users = [f for f in local if roles[f.locator] is FieldRole.USERNAME]
    user = next((f for f in users if pw is not None and _same_form(f, pw)), None) if pw is not None else None
    if user is None and pw is None and users:
        user = users[0]
    otp = next((f for f in local if roles[f.locator] is FieldRole.OTP), None)
    return user, pw, otp


def _asks(page: PageRead, local: list[FieldInfo], roles: Mapping[str, FieldRole]) -> tuple[list[Ask], list[FieldInfo]]:
    """4: what the owner's form asks for (username, password, or a code), and the fields typed into,
    in order."""
    user, pw, otp = _login_fields(local, roles)
    asks: list[Ask] = []
    typed: list[FieldInfo] = []
    if user is not None:
        asks.append(Ask("username", FieldRole.USERNAME, _label(page, user, "username"), False, (user.locator,),
                        user.maxlength))
        typed.append(user)
    if pw is not None:
        asks.append(Ask("password", FieldRole.PASSWORD, _label(page, pw, "password"), True, (pw.locator,),
                        pw.maxlength))
        typed.append(pw)
    if otp is not None and pw is None:
        group = [f for f in otp_group(local, otp) if f.locator in roles]
        boxes = tuple(f.locator for f in group) or (otp.locator,)
        length = len(boxes) if len(boxes) > 1 else otp.maxlength
        asks.append(Ask("code", FieldRole.OTP, _label(page, otp, "code"), True, boxes, length))
        typed.append(group[-1] if group else otp)
    return asks, typed


def _fieldless_step(page: PageRead, top: Any, roles: Mapping[str, FieldRole], base: dict[str, Any]) -> Step:
    """5 to 7: no login field to ask for: approve on a phone, a form the vault does not fill, or
    buttons to choose from (one plain button is a confirm)."""
    others = _choices(page, top, alternatives=False)
    if page.approve or (page.number and not page.fields_visible()):
        return Step(StepKind.APPROVE, choices=tuple(others), number=page.number, **base)
    if _unfillable(page, top, roles):
        return Step(StepKind.STUCK, reason="This step asks for something openajar does not fill (not an email, a "
                                           "password or a code). Take control to fill it in the live view.",
                    choices=tuple(others), **base)
    if not others:
        return Step(StepKind.STUCK, reason="openajar cannot tell what this page wants. Take control and do this "
                                           "step in the live view.", **base)
    if len(others) == 1 and others[0].kind == "button":
        only = others[0]
        return Step(StepKind.CONFIRM, submit=only.locator, submit_label=only.label, choices=(only,), **base)
    return Step(StepKind.CHOOSE, choices=tuple(others), **base)


def values_for(step: Step, values: Mapping[str, Any]) -> list[tuple[Ask, Any]]:
    """The owner's values in the step's order, each for one of its fields. Unknown or missing names
    are refused by the caller (openajar.node.login_assist), so a value never lands in another field."""
    return [(a, values[a.name]) for a in step.asks if a.name in values]


__all__ = ["Action", "Ask", "Choice", "PageRead", "Step", "StepKind", "clean_text", "origin_text", "read_step",
           "values_for"]
