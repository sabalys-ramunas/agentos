"""Flow documents, runs, steps and schedules.

A flow is an immutable FlowVersion: a JSON document whose version is the hash of its
content. Steps address elements through structural locators (role + accessible name +
nth + an ancestor anchor + a CSS fallback), never through snapshot refs, which change on
every page load. Every step that changes the target's state (`writes: true`) declares a
readback of the target's own state, so a run can prove it did what it claims.

A flow's effective effect (what grants and approvals see) is the stricter of its declared
`effect` and what its steps can do: navigation, waits, scrolls and reads are `read`; any
input step (click, press, type, fill, select, upload) is `commit`, unless the step says
`"publishes": false` AND the version was saved by the human owner, in which case it is
`draft`. So an agent can never lower a flow below its steps (see src/agentos/runner/README.md).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from fnmatch import fnmatchcase
from functools import lru_cache
from itertools import pairwise
from typing import Annotated, Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from agentos.core.models import LANE_ID_RE, Effect, RunStatus, utcnow
from agentos.runner.util import TemplateError, render, template_roots

REF_MESSAGE = ("snapshot refs (like e12) change on every page load; address the element with a "
               "structural locator instead: role, name, within (an ancestor anchor), nth, css")
_REF_VALUE_RE = re.compile(r"^\[?(?:aria-)?ref=|\[ref=|^e\d+$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- locators


ATTR_NAME_RE = r"^[a-z][a-z0-9_:.-]{0,60}$"
LANG_RE = re.compile(r"^[a-z]{2}$")


def _regex_ok(pattern: str, what: str, *, groups: int | None = None) -> None:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"invalid {what} regex: {e}") from e
    if groups is not None and rx.groups != groups:
        raise ValueError(f"{what} regex must have exactly {groups} capture group(s)")


class AttrMatch(_Strict):
    """Match an element by a machine attribute (href, type, data-*, datetime, a custom
    element's attribute): exactly one of equals (the whole value), prefix, regex (a search).
    A missing attribute never matches. Read only: nothing is ever written to the page."""

    name: str = Field(pattern=ATTR_NAME_RE)
    equals: str | None = None
    prefix: str | None = None
    regex: str | None = None

    @model_validator(mode="after")
    def _one(self) -> AttrMatch:
        ops = [k for k in ("equals", "prefix", "regex") if getattr(self, k) is not None]
        if len(ops) != 1:
            raise ValueError("attr needs exactly one of equals, prefix, regex")
        if self.regex is not None and "${" not in self.regex:
            _regex_ok(self.regex, "attr")
        return self

    def matches(self, value: str | None) -> bool:
        """The one matcher both drivers use, so the fake and the browser agree by construction."""
        if value is None:
            return False
        if self.equals is not None:
            return value == self.equals
        if self.prefix is not None:
            return value.startswith(self.prefix)
        return re.search(self.regex or "", value) is not None

    def render(self, scope: dict[str, Any]) -> AttrMatch:
        return self.model_copy(update={k: render(v, scope) for k in ("equals", "prefix", "regex")
                                       if (v := getattr(self, k)) is not None})

    def describe(self) -> str:
        if self.equals is not None:
            return f'[{self.name}="{self.equals}"]'
        if self.prefix is not None:
            return f'[{self.name}^="{self.prefix}"]'
        return f"[{self.name}~/{self.regex}/]"

    def strings(self) -> list[str]:
        return [v for v in (self.equals, self.prefix, self.regex) if v is not None]


class Locator(_Strict):
    """A durable address for one element.

    Resolution (the FlowDriver must implement exactly this):
      1. Scope: the page, or the single element `within` resolves to (the anchor). Inside an
         extract item, the item is the scope (and the anchor, when there is no `within`).
      2. Candidates: elements in scope whose ARIA role equals `role` and whose accessible
         name matches `name` (`match`: exact after whitespace normalization, contains, regex).
         `attr` narrows the candidates to elements whose attribute matches (equals, prefix,
         regex); with no role and no css, every element in scope whose attribute matches is a
         candidate. Open shadow roots are part of the scope.
      3. With an anchor and `own` (the default), drop candidates that sit inside a nested
         element having the anchor's role, so a comment's Like inside a post never counts
         as the post's Like.
      4. If no candidate matched and `css` is set, use the CSS fallback (same scope rules,
         `attr` still applies).
      5. `nth` picks one candidate (0-based); without `nth`, more than one candidate is an
         ambiguity error, never "the first one".

    `names` is a per-language map of accessible names (`{"en": "Share", "lt": "Bendrinti"}`)
    used in place of `name`: `render` picks the entry for the `account_language` param, so a
    driver only ever sees `name`. A language with no entry is an error, never a fallback.
    """

    role: str | None = None
    name: str | None = None
    names: dict[str, str] | None = None
    match: Literal["exact", "contains", "regex"] = "exact"
    level: int | None = Field(default=None, ge=1, le=6)  # a heading's level, so h3 cards skip the page's h1
    attr: AttrMatch | None = None
    nth: int | None = Field(default=None, ge=0, le=500)
    within: Locator | None = None
    own: bool = True
    css: str | None = None
    label: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _no_refs(cls, data: Any) -> Any:
        if isinstance(data, dict) and ("ref" in data or "aria_ref" in data):
            raise ValueError(REF_MESSAGE)
        return data

    @model_validator(mode="after")
    def _check(self) -> Locator:
        if not self.role and not self.css and self.attr is None:
            raise ValueError("a locator needs a role (preferred), an attr or a css fallback")
        if self.role and not re.fullmatch(r"[a-z][a-z-]{1,40}", self.role):
            raise ValueError(f"role must be an ARIA role name like 'button': {self.role!r}")
        if self.css and _REF_VALUE_RE.search(self.css):
            raise ValueError(REF_MESSAGE)
        if (self.name is not None or self.names is not None) and self.role is None:
            raise ValueError("a name needs a role (an accessible name belongs to a role)")
        if self.names is not None:
            if self.name is not None:
                raise ValueError("a locator has either name or names, not both")
            if not self.names:
                raise ValueError("names maps account languages to accessible names: {en: Share, lt: Bendrinti}")
            for lang, name in self.names.items():
                if not LANG_RE.match(lang) or not name.strip():
                    raise ValueError(f"names: {lang!r} must be a two-letter language with a non-empty name")
        if self.match == "regex":
            for name in [self.name, *(self.names or {}).values()]:
                if name and "${" not in name:
                    try:
                        re.compile(name)
                    except re.error as e:
                        raise ValueError(f"invalid name regex: {e}") from e
        return self

    def render(self, scope: dict[str, Any]) -> Locator:
        name, names = self.name, self.names
        if names is not None:
            lang = scope.get("account_language")
            if lang not in names:
                raise TemplateError(f"{self.describe()}: names has no entry for account language {lang!r}"
                                    if lang else f"{self.describe()}: names needs the account_language param")
            name, names = names[lang], None
        return self.model_copy(update={
            "name": render(name, scope) if name else name,
            "names": names,
            "css": render(self.css, scope) if self.css else self.css,
            "attr": self.attr.render(scope) if self.attr else None,
            "within": self.within.render(scope) if self.within else None,
        })

    def describe(self) -> str:
        if self.label:
            return self.label
        parts: list[str] = []
        if self.role:
            parts.append(self.role)
            shown = self.name if self.names is None else "|".join(self.names.values())
            if shown is not None:
                parts.append(f'"{shown}"' if self.match == "exact" else f'{self.match} "{shown}"')
        elif self.css and self.attr is None:
            parts.append(f"css {self.css}")
        if self.attr is not None:
            parts.append(self.attr.describe() if parts else f"element {self.attr.describe()}")
        if self.nth is not None:
            parts.append(f"#{self.nth}")
        text = " ".join(parts)
        if self.within:
            text += f" within {self.within.describe()}" + ("" if self.own else " (including nested)")
        if self.css and (self.role or self.attr is not None):
            text += f" (css fallback {self.css})"
        return text

    def strings(self) -> list[str]:
        out = [s for s in (self.name, self.css) if s]
        if self.names is not None:
            out += [*self.names.values(), "${account_language}"]  # names need that param declared
        if self.attr is not None:
            out += self.attr.strings()
        if self.within:
            out += self.within.strings()
        return out

    def regexes(self) -> list[tuple[str, str]]:
        """(pattern, what) for every regex in this locator and its anchors, to check once
        params are filled in."""
        out: list[tuple[str, str]] = []
        if self.match == "regex" and self.name is not None:
            out.append((self.name, f"the name pattern of {self.describe()}"))
        if self.attr is not None and self.attr.regex is not None:
            out.append((self.attr.regex, f"the attr pattern of {self.describe()}"))
        if self.within:
            out += self.within.regexes()
        return out


# ---------------------------------------------------------------- conditions and readbacks


class Condition(_Strict):
    """A check on the target's own state, read through FlowDriver.read_state.

    kinds: exists (exactly one element at the locator), absent, attribute (`name` plus an
    operator), text (the element's own text), value (form value), count (number of matches,
    with equals / at_least / at_most / changed_by against a `baseline` saved earlier with
    `save_as`), url (the page URL). An attribute condition may instead name `changed_from`, an
    element saved earlier with `save_as`: it holds when the attribute is there and differs from
    that element's, so a readback proves the newest item is a new one, not one that was there
    before. A missing baseline (the saved element was not there, or had no such attribute, or
    was ambiguous) fails it, unless `empty_before_ok` says that nothing there before is a normal
    state (an empty list before the first comment), and then only when the saved read found no
    element at all. `identity` (a regex with one group) compares the part of both values that
    names the thing (a media asset id, without its size and signature) instead of the whole
    value; the new value must match it.

    `lines` (text conditions) compares the text with its line breaks kept (ElementState.lines,
    runner.util.norm_lines) instead of all whitespace collapsed.
    """

    kind: Literal["exists", "absent", "attribute", "text", "value", "count", "url"]
    name: str | None = None
    equals: str | int | None = None
    not_equals: str | None = None
    contains: str | None = None
    matches: str | None = None
    at_least: int | str | None = None
    at_most: int | str | None = None
    changed_by: int | None = None
    baseline: str | None = None
    baseline_count: int | None = None  # filled in at run time from `baseline`
    changed_from: str | None = None
    baseline_value: str | None = None  # filled in at run time from `changed_from`
    identity: str | None = Field(default=None, max_length=300)
    empty_before_ok: bool = False
    lines: bool = False

    @model_validator(mode="after")
    def _check(self) -> Condition:
        ops = {k: getattr(self, k) for k in ("equals", "not_equals", "contains", "matches")
               if getattr(self, k) is not None}
        count_ops = {k: getattr(self, k) for k in ("equals", "at_least", "at_most", "changed_by")
                     if getattr(self, k) is not None}
        k = self.kind
        if self.changed_from is not None:
            if k != "attribute":
                raise ValueError("changed_from belongs to an attribute condition")
            ops["changed_from"] = self.changed_from
        elif self.identity is not None or self.empty_before_ok:
            raise ValueError("identity and empty_before_ok belong to a changed_from condition")
        if self.identity is not None:
            try:
                if re.compile(self.identity).groups != 1:
                    raise ValueError("identity needs exactly one group: the part that names the thing")
            except re.error as e:
                raise ValueError(f"invalid identity regex: {e}") from e
        if self.lines and (k != "text" or self.matches is not None):
            raise ValueError("lines belongs to a text condition with equals, not_equals or contains")
        if k in ("exists", "absent"):
            if ops or count_ops:
                raise ValueError(f"{k} takes no operator")
        elif k in ("attribute", "text", "value", "url"):
            if k == "attribute" and not self.name:
                raise ValueError("an attribute condition needs the attribute name")
            if len(ops) != 1:
                raise ValueError(f"{k} needs exactly one of equals, not_equals, contains, matches"
                                 + (", changed_from" if k == "attribute" else ""))
        elif k == "count":
            if len(count_ops) != 1:
                raise ValueError("count needs exactly one of equals, at_least, at_most, changed_by")
            if self.changed_by is not None and not self.baseline:
                raise ValueError("changed_by needs a baseline: a name saved earlier with save_as")
        if self.matches and "${" not in self.matches:
            try:
                re.compile(self.matches)
            except re.error as e:
                raise ValueError(f"invalid regex: {e}") from e
        return self

    def render(self, scope: dict[str, Any]) -> Condition:
        upd: dict[str, Any] = {}
        for key in ("equals", "not_equals", "contains", "matches", "at_least", "at_most"):
            v = getattr(self, key)
            if isinstance(v, str):
                upd[key] = render(v, scope)
        if self.baseline:
            saved = scope.get("vars", {}).get(self.baseline)
            if not isinstance(saved, dict) or "count" not in saved:
                raise ValueError(
                    f"baseline {self.baseline!r} was not saved before this readback")
            upd["baseline_count"] = int(saved["count"])
        if self.changed_from:
            saved = scope.get("vars", {}).get(self.changed_from)
            if not isinstance(saved, dict):
                raise ValueError(f"changed_from {self.changed_from!r} was not saved before this readback")
            value = (saved.get("attributes") or {}).get(self.name or "") if saved.get("found") else None
            upd["baseline_value"] = value if isinstance(value, str) else None
            upd["baseline_count"] = int(saved.get("count") or 0)    # 0: nothing was there
        return self.model_copy(update=upd)

    def strings(self) -> list[str]:
        return [v for v in (self.equals, self.not_equals, self.contains, self.matches,
                            self.at_least, self.at_most) if isinstance(v, str)]


class Readback(_Strict):
    """A readback attached to an action step. Without a locator it reads the step's own
    target. `url` (optional) is navigated first, which also lets a later re-check run."""

    locator: Locator | None = None
    condition: Condition
    url: str | None = None
    timeout_ms: int = Field(default=8000, ge=0, le=120_000)


# ---------------------------------------------------------------- steps


class _StepBase(_Strict):
    label: str | None = None
    idempotent: bool | None = None  # override the step type's default

    @model_validator(mode="before")
    @classmethod
    def _no_refs(cls, data: Any) -> Any:
        if isinstance(data, dict) and "ref" in data:
            raise ValueError(REF_MESSAGE)
        return data


class NavigateStep(_StepBase):
    type: Literal["navigate"] = "navigate"
    url: str


class WaitForStep(_StepBase):
    """Wait for an element (visible) or for it to go away (gone). `any` reads without
    waiting, which with `save_as` captures a baseline (for example a post count)."""

    type: Literal["wait_for"] = "wait_for"
    locator: Locator
    state: Literal["visible", "gone", "any"] = "visible"
    timeout_ms: int = Field(default=15_000, ge=0, le=300_000)
    save_as: str | None = None


class _ActionStep(_StepBase):
    locator: Locator
    writes: bool = False  # changes the target's state: needs a readback
    publishes: bool | None = None  # false: the owner vouches this input publishes nothing
    readback: Readback | None = None
    timeout_ms: int = Field(default=10_000, ge=0, le=300_000)


def _one_source(step: Any, literal: str) -> Any:
    if (getattr(step, literal) is None) == (step.param is None):
        raise ValueError(f"{step.type} needs exactly one of {literal!r} (a literal or template) or 'param'")
    return step


class ClickStep(_ActionStep):
    type: Literal["click"] = "click"


class TypeStep(_ActionStep):
    type: Literal["type"] = "type"
    text: str | None = None
    param: str | None = None

    @model_validator(mode="after")
    def _src(self) -> TypeStep:
        return _one_source(self, "text")


class FillStep(_ActionStep):
    type: Literal["fill"] = "fill"
    text: str | None = None
    param: str | None = None

    @model_validator(mode="after")
    def _src(self) -> FillStep:
        return _one_source(self, "text")


class SelectStep(_ActionStep):
    type: Literal["select"] = "select"
    option: str | None = None
    param: str | None = None

    @model_validator(mode="after")
    def _src(self) -> SelectStep:
        return _one_source(self, "option")


class UploadStep(_ActionStep):
    type: Literal["upload"] = "upload"
    param: str  # a file param: a path on the node or a files ref


class PressStep(_StepBase):
    type: Literal["press"] = "press"
    key: str
    locator: Locator | None = None
    writes: bool = False
    publishes: bool | None = None
    readback: Readback | None = None
    timeout_ms: int = Field(default=10_000, ge=0, le=300_000)


class ScrollToStep(_StepBase):
    type: Literal["scroll_to"] = "scroll_to"
    locator: Locator
    timeout_ms: int = Field(default=10_000, ge=0, le=300_000)


class ReadbackStep(_StepBase):
    type: Literal["readback"] = "readback"
    locator: Locator | None = None
    condition: Condition
    url: str | None = None
    timeout_ms: int = Field(default=8000, ge=0, le=120_000)
    save_as: str | None = None

    @model_validator(mode="after")
    def _needs_locator(self) -> ReadbackStep:
        if self.locator is None and self.condition.kind != "url":
            raise ValueError("a readback step needs a locator (only url conditions read the page itself)")
        return self


class Check(_Strict):
    """A branch condition: either the page (locator + condition) or a param."""

    locator: Locator | None = None
    condition: Condition | None = None
    param: str | None = None
    equals: Any = None
    is_set: bool | None = None

    @model_validator(mode="after")
    def _one(self) -> Check:
        page = self.condition is not None
        if page == (self.param is not None):
            raise ValueError("a branch checks either the page (condition, with a locator) or a param")
        if page and self.locator is None and self.condition and self.condition.kind != "url":
            raise ValueError("a page check needs a locator")
        return self


class BranchStep(_StepBase):
    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)
    type: Literal["branch"] = "branch"
    if_: Check = Field(alias="if")
    then: list[Step] = Field(default_factory=list)
    else_: list[Step] = Field(default_factory=list, alias="else")


class PauseStep(_StepBase):
    type: Literal["pause"] = "pause"
    min_ms: int = Field(ge=0, le=600_000)
    max_ms: int = Field(ge=0, le=600_000)

    @model_validator(mode="after")
    def _order(self) -> PauseStep:
        if self.max_ms < self.min_ms:
            raise ValueError("max_ms must be at least min_ms")
        return self


ACTING_AS_KINDS = ("account", "fb_page", "li_company", "ig_business")


class IdentityCheck(_Strict):
    """Who the run acts as, read from structure only, compared with `expect` (for connectors
    the hidden `${acts_as_id}` binding param, never the caller's).

    cookie  the site's identity cookie (agentos.node.sites); `site` is a sites.SITES key,
            default: the connector's site, else the site of the flow's domain
    url     the page URL, through `pattern` (one capture group)
    href    the href of the element at `locator`, through `pattern` (one capture group)

    url and href wait up to `timeout_ms` for the pattern to match (a page that is still
    navigating) or the element to appear. A different id fails the run as `wrong_account`
    (kind account) or `wrong_surface` (an asset: a Page, a company page) and holds the lane
    for a human. An id that cannot be read at all fails as `identity_unknown` (the page
    changed shape) without a hold."""

    acting_as: Literal["account", "fb_page", "li_company", "ig_business"] = "account"
    source: Literal["cookie", "url", "href"] = "cookie"
    pattern: str | None = None
    locator: Locator | None = None
    expect: str = Field(min_length=1, max_length=200)
    site: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9]{0,30}$")
    timeout_ms: int = Field(default=10_000, ge=0, le=60_000)

    @model_validator(mode="after")
    def _identity(self) -> IdentityCheck:
        if self.source == "cookie":
            if self.pattern is not None or self.locator is not None:
                raise ValueError("an identity read from the cookie takes no pattern or locator")
            return self
        if not self.pattern:
            raise ValueError(f"an identity read from the {self.source} needs a pattern with one capture group")
        _regex_ok(self.pattern, "identity pattern", groups=1)
        if (self.source == "href") != (self.locator is not None):
            raise ValueError("source href needs the locator of the element whose href carries the id "
                             "(and source url takes none)")
        if self.site is not None:
            raise ValueError("site applies to an identity read from the cookie")
        return self

    def identity(self) -> IdentityCheck:
        return IdentityCheck.model_validate(self.model_dump(include=set(IdentityCheck.model_fields)))


class AssertIdentityStep(_StepBase, IdentityCheck):
    """Check acting as (see IdentityCheck). A read: it never touches the page."""

    type: Literal["assert_identity"] = "assert_identity"


class PreflightCheck(_Strict):
    """One check right before a commit step.

    value_equals  the element at `locator` holds exactly the value of `param` (after
                  whitespace normalization): the composer has the approved text
    count         the number of matches for `locator` equals `equals` (one image attached)
    text_differs  exactly one element is at `locator` (even with nth set), and its own text
                  (after whitespace normalization; with `lines`, line breaks kept) is not
                  `text` (a template of params): the value an edit sets is not there already
                  (a profile's headline). When the only failures are text_differs checks that
                  found their one element showing exactly that value, the run ends
                  `already_current` (nothing to do), not `preflight_mismatch`
    acting_as     an identity check (IdentityCheck fields), again, right before the commit
    """

    kind: Literal["value_equals", "count", "text_differs", "acting_as"]
    locator: Locator | None = None
    param: str | None = None
    equals: int | None = Field(default=None, ge=0, le=100)
    text: str | None = Field(default=None, max_length=500)
    lines: bool = False
    acting_as: Literal["account", "fb_page", "li_company", "ig_business"] | None = None
    source: Literal["cookie", "url", "href"] | None = None
    pattern: str | None = None
    expect: str | None = None
    site: str | None = None

    @model_validator(mode="after")
    def _check(self) -> PreflightCheck:
        identity = ("acting_as", "source", "pattern", "expect", "site")
        if self.kind == "acting_as":
            if self.param is not None or self.equals is not None or self.text is not None or self.lines:
                raise ValueError("an acting_as check takes the identity fields, not param, equals or text")
            self.identity()  # validates the identity fields
            return self
        if any(getattr(self, k) is not None for k in identity):
            raise ValueError(f"a {self.kind} check takes no identity fields")
        if self.locator is None:
            raise ValueError(f"a {self.kind} check needs a locator")
        if self.kind == "value_equals" and (self.param is None or self.equals is not None):
            raise ValueError("value_equals compares the element's value with one param: set param")
        if self.kind == "count" and (self.equals is None or self.param is not None):
            raise ValueError("count needs equals (the expected number of matches)")
        if (self.kind == "text_differs") != (self.text is not None):
            raise ValueError("text_differs compares the element's text with `text` (a template of params), "
                             "and only text_differs takes text")
        if self.kind == "text_differs" and (self.param is not None or self.equals is not None):
            raise ValueError("text_differs takes text, not param or equals")
        if self.lines and self.kind != "text_differs":
            raise ValueError("lines belongs to a text_differs check")
        return self

    def identity(self) -> IdentityCheck:
        data = {k: getattr(self, k) for k in ("acting_as", "source", "pattern", "locator", "expect", "site")
                if getattr(self, k) is not None}
        try:
            return IdentityCheck.model_validate(data)
        except ValidationError as e:
            raise ValueError(f"acting_as check: {e.errors()[0]['msg'].removeprefix('Value error, ')}") from e


class PreflightStep(_StepBase):
    """Checks run right before the commit step (the connector compiler inserts it). Value and
    count checks are polled up to `timeout_ms` (a thumbnail can render late), identity checks
    read once. Any mismatch fails the run as `preflight_mismatch` before anything is posted.
    On resume it always runs again unless the step after it has started."""

    type: Literal["preflight"] = "preflight"
    checks: list[PreflightCheck] = Field(min_length=1, max_length=20)
    timeout_ms: int = Field(default=5000, ge=0, le=60_000)


class ExtractField(_Strict):
    """One field of an extracted record: an attribute (`attr`) or a text read (`read`:
    own_text, text, value) of the item, or of the one element `locator` finds inside it.
    Checked by the executor: `pattern` (a search; anchor it), `format` (date-time, uri,
    integer, number), `host` (a URL inside it, else dropped), `max_chars` (clipped, the run
    reports `truncated`). `capture` (a regex with one group) keeps that group of the value, and
    no match means no value. `untrusted` and `private` label the field for the taint rules."""

    locator: Locator | None = None
    attr: str | None = Field(default=None, pattern=ATTR_NAME_RE)
    read: Literal["own_text", "text", "value"] | None = None
    pattern: str | None = None
    capture: str | None = None
    format: Literal["date-time", "uri", "integer", "number"] | None = None
    host: str | None = Field(default=None, max_length=253)
    max_chars: int | None = Field(default=None, ge=1, le=20_000)
    untrusted: bool = False
    private: bool = False

    @model_validator(mode="after")
    def _check(self) -> ExtractField:
        if (self.attr is None) == (self.read is None):
            raise ValueError("a field reads exactly one of attr (a machine attribute) or read (text)")
        if self.pattern is not None:
            _regex_ok(self.pattern, "field pattern")
        if self.capture is not None:
            _regex_ok(self.capture, "field capture")
            if re.compile(self.capture).groups != 1:
                raise ValueError("a field capture has exactly one group")
        return self


class ExtractStep(_StepBase):
    """Read up to `max_items` records into `save_as`: every element `items` matches is an item
    (no ambiguity error), in page order. Saves {items, count, total, truncated}. A read.

    `pick` narrows the matches: `outermost` drops a match inside another (a reply inside a post);
    `card` makes each match stand for the widest element around it holding no other match, for
    cards of plain boxes no role can name. `drop_empty` skips a record with no value at all (a
    table's header row)."""

    type: Literal["extract"] = "extract"
    save_as: str
    items: Locator
    max_items: int = Field(ge=1, le=500)
    fields: dict[str, ExtractField] = Field(min_length=1, max_length=30)
    pick: Literal["outermost", "card"] | None = None
    drop_empty: bool | None = None

    @model_validator(mode="after")
    def _names(self) -> ExtractStep:
        for name in self.fields:
            if not PARAM_NAME_RE.match(name):
                raise ValueError(f"field name {name!r} must be lowercase_with_underscores")
        return self


# ---------------------------------------------------------------- reading the page's own JSON

MAX_URL_PATTERN = 200  # as a code catcher's pattern (agentos.vault.catcher)
MAX_WATCHED_URL = 2048  # a longer URL is never tested: a page chooses how long its URLs are
JSON_ROOT = "."  # read_json `items`: the body itself is the list
_JSON_PART = r"[A-Za-z0-9_-]{1,60}"
JSON_PATH_RE = re.compile(rf"{_JSON_PART}(?:\.{_JSON_PART}){{0,7}}")
# what may repeat in a URL pattern. Each is bounded by the character after it (_url_tokens), so it ends
# in one place only and compiles possessive: no pattern can make the matcher go back over a URL
URL_RUNS = ("[^/]+", "[^&]+", "[0-9]+", r"\d+", "[a-z0-9_-]+", "[A-Za-z0-9_-]+")
_URL_PLAIN = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-_~%=&:,;@!'")
_URL_ESCAPED = frozenset(".?+*()[]{}|$^")
_SECRET_WORDS = frozenset({"auth", "bearer", "cookie", "cookies", "credential", "credentials", "csrf", "jwt", "otp",
                           "passphrase", "passwd", "password", "passwords", "pwd", "secret", "secrets", "session",
                           "sessions", "sid", "token", "tokens", "xsrf"})
_SECRET_GLUED = ("accesskey", "apikey", "authorization", "cookie", "credential", "passwd", "password", "phpsessid",
                 "privatekey", "secret", "sessionid", "sessionkey", "token")
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")


def names_secret(key: str) -> bool:
    """Does this JSON key name a credential, in any of the ways sites write one (`accessToken`,
    `session_id`, `X-Auth`, `apikey`)? A safety net for a mistake, not a boundary: a key can be called
    anything, so what keeps a secret out is that only declared fields leave, and a person activates."""
    glued = re.sub(r"[-_]", "", key).lower()
    return any(w.lower() in _SECRET_WORDS for w in _WORD_RE.findall(key)) or any(s in glued for s in _SECRET_GLUED)


def _url_tokens(pattern: str) -> tuple[list[tuple[bool, str]], bool]:
    """([(is a run, text)], ends in `$`), or a ValueError that says what to write instead."""
    tokens: list[tuple[bool, str]] = []
    at = 1  # past the ^
    while at < len(pattern):
        run = next((r for r in URL_RUNS if pattern.startswith(r, at)), None)
        ch = pattern[at]
        if run is not None:
            tokens.append((True, run))
            at += len(run)
        elif ch == "$" and at == len(pattern) - 1:
            return tokens, True
        elif ch == "\\" and pattern[at + 1:at + 2] in _URL_ESCAPED:
            tokens.append((False, pattern[at + 1]))
            at += 2
        elif ch in _URL_PLAIN:
            tokens.append((False, ch))
            at += 1
        else:
            raise ValueError(f"a read_json url cannot hold {ch!r} there: it is literal URL text, with a dot or a ? "
                             f"written \\. and \\?, and only {', '.join(URL_RUNS)} may repeat. No groups, alternatives "
                             "or params")
    return tokens, False


@dataclass(frozen=True, slots=True)
class UrlPattern:
    """A read_json `url`, parsed. `origin` is its literal `https://host/`, which lint checks as it
    checks any URL. A match never takes longer than one pass over the URL."""
    origin: str
    regex: re.Pattern[str]

    def matches(self, url: str) -> bool:
        return len(url) <= MAX_WATCHED_URL and self.regex.match(url) is not None


@lru_cache(maxsize=256)
def url_pattern(pattern: str) -> UrlPattern:
    """A read_json `url`: `^https://`, a literal host, a path. It matches from the start of a URL and
    to the end only with `$`. Text an owner or an agent wrote is run against every response a page
    makes, on the node's event loop, so it is held to a form that cannot backtrack, not to `re`."""
    if len(pattern) > MAX_URL_PATTERN:
        raise ValueError(f"a read_json url is at most {MAX_URL_PATTERN} characters")
    if not pattern.startswith("^https://"):
        raise ValueError("a read_json url starts ^https:// and a literal host, so it can only match that site")
    tokens, ended = _url_tokens(pattern)
    text = "".join("\0" if run else t for run, t in tokens)
    authority, slash, _ = text.removeprefix("https://").partition("/")
    if not slash or not authority or "\0" in authority:
        raise ValueError("a read_json url names a literal host and then a path: ^https://host\\.example/api")
    for (run, t), (next_run, following) in pairwise(tokens):
        if run and (next_run or re.fullmatch(t, following)):
            raise ValueError(f"{t} must be followed by a character it cannot match, so it has one place to end")
    source = "".join(t + "+" if run else re.escape(t) for run, t in tokens)
    return UrlPattern(origin=f"https://{authority}/", regex=re.compile(source + (r"\Z" if ended else "")))


def _json_path(path: str, what: str) -> None:
    if not JSON_PATH_RE.fullmatch(path):
        raise ValueError(f"{what} is a plain dotted path like data.orders: keys of letters, digits, _ and -, "
                         "with no wildcard, index or expression, and nothing is evaluated")
    if any(names_secret(part) for part in path.split(".")):
        raise ValueError(f"{what} {path!r} names a token, password, secret, session, cookie or authorization: a site's "
                         "credentials must never reach an output")


class JsonField(_Strict):
    """One field of a read_json record: the value at `path` under the item. `type` says what it must
    be, and a value of another type is a contract violation, never coerced. A key that is not there
    or is null reads as null, and the output schema decides whether that fits. A string may set
    `format` and `host`, which mean what they mean on an ExtractField, and `max_chars`."""

    path: str
    type: Literal["string", "number", "integer", "boolean"]
    format: Literal["date-time", "uri"] | None = None
    host: str | None = Field(default=None, max_length=253)
    max_chars: int | None = Field(default=None, ge=1, le=20_000)
    untrusted: bool = False
    private: bool = False

    @model_validator(mode="after")
    def _check(self) -> JsonField:
        _json_path(self.path, "a field path")
        if self.type != "string" and (self.format or self.host or self.max_chars):
            raise ValueError("format, host and max_chars belong to a string field")
        return self


class ReadJsonStep(_StepBase):
    """Read up to `max_items` records from a JSON response the page itself made while the `navigate`
    before this step loaded it. Saves {items, count, total, truncated}, as an extract does. A read.

    Passive: the driver listens from before the page loads and never makes, repeats, alters or
    delays a request. The first response on `url` after the page commits is the one read, and any
    later match is ignored. `items` is the path of the list in the body (`.` when the body is the
    list), and `timeout_ms` how long to wait for that response once the page has loaded."""

    type: Literal["read_json"] = "read_json"
    save_as: str
    url: str
    items: str
    max_items: int = Field(ge=1, le=500)
    fields: dict[str, JsonField] = Field(min_length=1, max_length=30)
    timeout_ms: int = Field(default=10_000, ge=100, le=60_000)

    @model_validator(mode="after")
    def _check(self) -> ReadJsonStep:
        url_pattern(self.url)
        if self.items != JSON_ROOT:
            _json_path(self.items, "items")
        for name in self.fields:
            if not PARAM_NAME_RE.match(name):
                raise ValueError(f"field name {name!r} must be lowercase_with_underscores")
        return self


class ForEachStep(_StepBase):
    """Run read-only steps once per item of a list saved earlier (an extract's items, or any
    saved list), at most `max_items` times; the item is `${vars.<as>}`. No input step may sit
    inside, at any depth. A value saved inside is collected over the iterations: an
    extract's items are appended, any other saved value becomes a list with one entry per
    iteration."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)
    type: Literal["for_each"] = "for_each"
    over: str
    as_: str = Field(alias="as")
    max_items: int = Field(ge=1, le=200)
    steps: list[Step] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _read_only(self) -> ForEachStep:
        for name in (self.over, self.as_):
            if not PARAM_NAME_RE.match(name):
                raise ValueError(f"for_each over/as {name!r} must be lowercase_with_underscores")
        for path, step in iter_steps(self.steps):
            if step.type in ACTION_TYPES:
                raise ValueError(f"no input steps inside for_each (step {path} is {step.type}): a loop only reads")
        return self


Step = Annotated[
    NavigateStep | WaitForStep | ClickStep | TypeStep | FillStep | SelectStep | PressStep
    | ScrollToStep | UploadStep | ReadbackStep | BranchStep | PauseStep
    | AssertIdentityStep | PreflightStep | ExtractStep | ReadJsonStep | ForEachStep,
    Field(discriminator="type"),
]
BranchStep.model_rebuild()
ForEachStep.model_rebuild()
_STEPS: TypeAdapter[list[Any]] = TypeAdapter(list[Step])

ACTION_TYPES = frozenset({"click", "type", "fill", "select", "press", "upload"})
IDEMPOTENT_DEFAULT = {
    "navigate": True, "wait_for": True, "readback": True, "scroll_to": True, "pause": True,
    "branch": True, "fill": True, "select": True,
    "assert_identity": True, "preflight": True, "extract": True, "read_json": True, "for_each": True,
    "click": False, "type": False, "press": False, "upload": False,
}


def watchers(steps: list[Any], at: int) -> list[int]:
    """The read_json steps that watch the page load at `at`: the ones right after it, past the
    assert_identity the compiler may put there. Listening has to start before that load."""
    out: list[int] = []
    for i in range(at + 1, len(steps)):
        if steps[i].type == "read_json":
            out.append(i)
        elif steps[i].type != "assert_identity":
            break
    return out


def is_idempotent(step: Any) -> bool:
    if step.idempotent is not None:
        return step.idempotent
    return IDEMPOTENT_DEFAULT.get(step.type, False)


def may_publish(step: Any, *, owner_saved: bool) -> bool:
    """An input step counts as publishing unless the owner marked it `publishes: false`."""
    if step.type not in ACTION_TYPES:
        return False
    return not (owner_saved and getattr(step, "publishes", None) is False)


def steps_effect(steps: list[Any], *, owner_saved: bool) -> tuple[Effect, str | None]:
    """The effect a flow's steps can have, and the first step that sets it."""
    worst, why = Effect.READ, None
    for path, step in iter_steps(steps):
        if step.type not in ACTION_TYPES:
            continue
        eff = Effect.COMMIT if may_publish(step, owner_saved=owner_saved) else Effect.DRAFT
        if eff.rank > worst.rank:
            worst, why = eff, f"step {path} ({step.type})"
    return worst, why


# who answered a card, as the approvals store wrote it: (decided_by, decided_via). The owner signed in
# to the panel, and the node's own CLI. The node token writes "human:owner" too, but it may be held by
# an agent, so what tells them apart is how the answer came: its via is owner_token.
IN_PERSON = frozenset({("human:owner", "session"), ("owner:cli", None), ("owner", None)})


def answered_in_person(approval: Any, *, relayed: bool = False) -> bool:
    """A person answered this card. `relayed`: a controller's signed message counts as one too."""
    by = str(approval.decided_by or "")
    return (by, approval.decided_via) in IN_PERSON or (relayed and by.split(":")[0] == "controller")


OWNER_INSTALL_KINDS = frozenset({"catalog", "local"})  # installs the owner chose: they count as the owner's vouch
# a connector the node generated from what an agent did once in a lane (agentos.profiles.learn,
# ConnectorService.install_learned): the node's own install, but never the owner's vouch
LEARNED_KIND = "learned"
INSTALL_KINDS = OWNER_INSTALL_KINDS | {LEARNED_KIND}  # saved_by kinds only the node's own installers write
TRY_SOURCE = "site_api_try"  # Run.source of a site API's try: counts against pace, says nothing of its health


@dataclass(frozen=True, slots=True)
class NodeInstall:
    """`by` for FlowService.save when the node itself installs a connector.* flow: ConnectorService
    as `catalog` (or `learned`, from install_learned), SiteApiService as `local`. A plain object on
    purpose: no JSON body, principal or export can be one, so nothing from outside can claim those
    kinds."""
    saved_by: dict[str, Any]


def is_owner(principal: Any) -> bool:
    """The human owner of the node (not an agent, a flow, or another human such as a VA).
    A signed, reviewed catalog connector the owner installed counts as the owner's vouch
    (`{"kind": "catalog", "installed_by": <owner>}`, docs/design/connectors.md 4.4), and so does a
    site API the owner activated (`{"kind": "local", "installed_by": <owner>}`, docs/SITE-APIS.md).
    A learned connector (`{"kind": "learned"}`) never does: the node wrote it from an agent's
    actions, so its `"publishes": false` marks are not the owner's word."""
    if principal is None:
        return False
    p = principal if isinstance(principal, dict) else principal.model_dump()
    if p.get("kind") == LEARNED_KIND:
        return False
    if p.get("kind") in OWNER_INSTALL_KINDS:
        return bool(p.get("installed_by")) and p.get("installed_by") == p.get("owner", "owner")
    return p.get("kind") == "human" and bool(p.get("id")) and p.get("id") == p.get("owner", "owner")


def iter_steps(steps: list[Any], prefix: str = ""):
    """Yield (path, step) for every step, depth first. Paths: '3', '5.then.0', '6.steps.1'
    (a for_each's steps; in the run journal each iteration has its own: '6.each2.1')."""
    for i, step in enumerate(steps):
        path = f"{prefix}{i}"
        yield path, step
        if isinstance(step, BranchStep):
            yield from iter_steps(step.then, f"{path}.then.")
            yield from iter_steps(step.else_, f"{path}.else.")
        elif isinstance(step, ForEachStep):
            yield from iter_steps(step.steps, f"{path}.steps.")


_EACH_RE = re.compile(r"\.each\d+\.")


def static_path(path: str) -> str:
    """A journal path ('6.each2.1') as the definition's path ('6.steps.1')."""
    return _EACH_RE.sub(".steps.", path)


def step_at(steps: list[Any], path: str) -> Any:
    parts = path.split(".")
    cur: list[Any] = steps
    step = None
    i = 0
    while i < len(parts):
        step = cur[int(parts[i])]
        i += 1
        if i < len(parts):
            key = parts[i]
            if key in ("then", "else") and isinstance(step, BranchStep):
                cur = step.then if key == "then" else step.else_
            elif (key == "steps" or re.fullmatch(r"each\d+", key)) and isinstance(step, ForEachStep):
                cur = step.steps
            else:
                raise KeyError(path)
            i += 1
    if step is None:
        raise KeyError(path)
    return step


# ---------------------------------------------------------------- flows

FLOW_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}(\.[a-z0-9][a-z0-9_-]{0,40}){1,5}$")
LANE_PATTERN_RE = re.compile(r"^[a-z0-9*?\[\]-]{1,31}/[a-z0-9*?\[\]-]{1,41}$")
PARAM_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
RESERVED_PARAMS = frozenset({"on", "ctx", "wait_seconds", "idempotency_key", "vars", "trigger", "version"})
CONNECTOR_PREFIX = "connector."  # flow ids compiled from connectors (agentos.runner.connectors)
CONNECTOR_RESERVED = ("flow ids under connector.* are reserved for installed connectors "
                      "(agentos connector install); save the flow under another id")


def is_connector_id(flow_id: str) -> bool:
    return flow_id.startswith(CONNECTOR_PREFIX)
SCOPE_ROOTS = frozenset({"vars", "trigger"})


def is_lane_pattern(lane: str) -> bool:
    return any(c in lane for c in "*?[")


def lane_matches(pattern: str, lane: str) -> bool:
    return fnmatchcase(lane, pattern)


class ProducedField(_Strict):
    """One value an operation gives the workflow steps after it (docs/WORKFLOWS.md)."""
    key: str = Field(pattern=r"^[\w-]{1,60}$")
    label: str | None = Field(default=None, max_length=120)
    type: Literal["text", "number", "date", "list", "yes_no"] = "text"
    # False counts only in a version the human owner saved: what a page said is untrusted by default
    untrusted: bool = True


class SendsSpec(_Strict):
    """Which params of an operation that sends to people hold the address and the message."""
    to: str = "to"
    body: str = "body"


class OperationBlock(_Strict):
    """What a flow says about itself for workflows: a name, what it gives steps after it, the
    params that name a target, and whether it sends to people. Hashed like the rest of the flow."""
    name: str | None = Field(default=None, max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=20)
    produces: list[ProducedField] = Field(default_factory=list, max_length=60)
    targets: list[str] = Field(default_factory=list, max_length=20)  # filled only by trusted data
    sends: SendsSpec | None = None
    # it reaches nobody, so a loop of it does not count against a daily send limit. Like `untrusted`
    # above, it counts only in a version the human owner saved: an agent cannot declare its way out
    not_to_people: bool | None = None

    @model_validator(mode="after")
    def _one_or_the_other(self) -> OperationBlock:
        if self.sends is not None and self.not_to_people:
            raise ValueError("an operation either sends to people or does not: `sends` or `not_to_people`, not both")
        return self


class FlowDefinition(_Strict):
    id: str
    title: str | None = None
    description: str = ""
    lane: str  # a lane id ("biz-a/instagram") or a pattern ("biz-*/instagram")
    domain: str | None = None
    effect: Effect = Effect.READ
    params: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    steps: list[Step] = Field(min_length=1, max_length=500)
    expose_as_tool: bool = True
    tags: list[str] = Field(default_factory=list)
    # set only by the connector compiler: the operation's contract and what the runner needs
    # beyond the steps (outputs, readback, commit, pace, signals); hashed with everything else
    connector: dict[str, Any] | None = None
    operation: OperationBlock | None = None  # what it says about itself for workflows

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not FLOW_ID_RE.match(v):
            raise ValueError("flow id must be dotted lowercase segments like 'biz-a.instagram.post'")
        return v

    @field_validator("lane")
    @classmethod
    def _lane(cls, v: str) -> str:
        if not (LANE_ID_RE.match(v) or (is_lane_pattern(v) and LANE_PATTERN_RE.match(v))):
            raise ValueError("lane must be a lane id like 'biz-a/instagram' or a pattern like '*/instagram'")
        return v

    @field_validator("effect")
    @classmethod
    def _effect(cls, v: Effect) -> Effect:
        if v.rank is None:
            raise ValueError("a flow's effect must be one of read, draft, commit, pay, irreversible")
        return v

    @field_validator("params")
    @classmethod
    def _params(cls, v: dict[str, Any]) -> dict[str, Any]:
        if v.get("type", "object") != "object":
            raise ValueError("params must be a JSON schema of type object")
        v = {"type": "object", **v}
        try:
            Draft202012Validator.check_schema(v)
        except SchemaError as e:
            raise ValueError(f"params is not a valid JSON schema: {e.message}") from e
        props = v.get("properties", {})
        if not isinstance(props, dict):
            raise ValueError("params.properties must be an object")  # noqa: TRY004 - pydantic wants ValueError
        for name in props:
            if not PARAM_NAME_RE.match(name) or name in RESERVED_PARAMS:
                raise ValueError(f"param name {name!r} must be lowercase_with_underscores and not reserved")
        for name in v.get("required", []):
            if name not in props:
                raise ValueError(f"required param {name!r} is not declared in properties")
        return v

    @model_validator(mode="after")
    def _connector(self) -> FlowDefinition:
        if self.connector is None:
            if is_connector_id(self.id):
                raise ValueError(CONNECTOR_RESERVED)
            return self
        c = self.connector
        expected = f"{CONNECTOR_PREFIX}{c.get('connector')}.{c.get('operation')}"
        if self.id != expected or not c.get("version") or not c.get("contract"):
            raise ValueError("connector metadata belongs to a compiled connector operation "
                             f"(id {expected!r} with a version and a contract hash)")
        self.discard_steps()  # the steps that close the composer are real steps too
        return self

    def discard_steps(self) -> list[Any]:
        """The connector's `discard` steps, validated like the flow's own: real step types,
        declared params only, and no input that may publish (each one is "publishes": false)."""
        raw = (self.connector or {}).get("discard") or []
        try:
            steps = _STEPS.validate_python(raw)
        except ValidationError as e:
            err = e.errors()[0]
            where = ".".join(str(x) for x in err.get("loc", ()) if not str(x).startswith("function-"))
            raise ValueError(f"connector discard {where}: {err.get('msg', 'invalid').removeprefix('Value error, ')}"
                             ) from e
        _check_references(steps, set(self.params.get("properties", {})), what="discard step")
        for path, step in iter_steps(steps):
            if step.type in ACTION_TYPES and getattr(step, "publishes", None) is not False:
                raise ValueError(f"discard step {path} ({step.type}) closes the composer without saving: "
                                 'mark it "publishes": false')
            if step.type == "read_json":
                raise ValueError(f"discard step {path} (read_json): closing the composer reads nothing")
        return steps

    @model_validator(mode="after")
    def _references(self) -> FlowDefinition:
        _check_references(self.steps, set(self.params.get("properties", {})))
        _check_writes(self.steps, "")
        _check_watches(self.steps, "")
        if self.connector is None and any(s.type == "read_json" for _, s in iter_steps(self.steps)):
            raise ValueError("read_json belongs to a connector or a site API, which have a domain, an output "
                             "contract and labels: a plain flow has none of these")
        return self

    @property
    def sends(self) -> SendsSpec | None:
        """How it sends to people, when it does: then it runs only as a workflow's send step."""
        return self.operation.sends if self.operation is not None else None

    # ---- identity ----
    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    def content_hash(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def lane_is_pattern(self) -> bool:
        return is_lane_pattern(self.lane)

    def effective_effect(self, *, owner_saved: bool) -> tuple[Effect, str | None]:
        """(effect, why): the stricter of the declared effect and the steps' effect; `why`
        names the step that raised it above the declaration, else None."""
        steps, why = steps_effect(self.steps, owner_saved=owner_saved)
        if steps.rank > self.effect.rank:
            return steps, why
        return self.effect, None

    def readback_count(self) -> int:
        n = 0
        for _, step in iter_steps(self.steps):
            if isinstance(step, ReadbackStep) or getattr(step, "readback", None) is not None:
                n += 1
        return n


def _check_references(steps: list[Any], declared: set[str], *, what: str = "step") -> None:
    """Saved names are unique, and every template, param and baseline a step names exists."""
    saved: set[str] = set()
    all_steps = list(iter_steps(steps))
    for _, step in all_steps:
        name = getattr(step, "save_as", None)
        if name:
            if not PARAM_NAME_RE.match(name):
                raise ValueError(f"save_as {name!r} must be lowercase_with_underscores")
            if name in saved:
                raise ValueError(f"save_as {name!r} is used twice")
            saved.add(name)
    loops = {step.as_ for _, step in all_steps if isinstance(step, ForEachStep)}
    if loops & saved:
        raise ValueError(f"for_each as {min(loops & saved)!r} is also a save_as name")
    for path, step in all_steps:
        where = f"{what} {path} ({step.type})"
        for text in _step_strings(step):
            for root in template_roots(text):
                if root not in declared and root not in SCOPE_ROOTS:
                    raise ValueError(f"{where}: ${{{root}}} is not a declared param")
        params = [getattr(step, "param", None)]
        if isinstance(step, BranchStep):
            params.append(step.if_.param)
        if isinstance(step, PreflightStep):
            params += [c.param for c in step.checks]
        for param in params:
            if param is not None and param not in declared:
                raise ValueError(f"{where}: param {param!r} is not declared")
        for cond in _step_conditions(step):
            if cond.baseline and cond.baseline not in saved:
                raise ValueError(f"{where}: baseline {cond.baseline!r} is never saved with save_as")
            if cond.changed_from and cond.changed_from not in saved:
                raise ValueError(f"{where}: changed_from {cond.changed_from!r} is never saved with save_as")
        if isinstance(step, ForEachStep) and step.over not in saved:
            raise ValueError(f"{where}: over {step.over!r} is never saved with save_as")


def _step_strings(step: Any) -> list[str]:
    out: list[str] = []
    for key in ("url", "text", "option", "key", "expect"):
        v = getattr(step, key, None)
        if isinstance(v, str):
            out.append(v)
    loc = getattr(step, "locator", None)
    if loc is not None:
        out += loc.strings()
    rb = getattr(step, "readback", None)
    if rb is not None:
        out += rb.condition.strings() + (rb.locator.strings() if rb.locator else []) + ([rb.url] if rb.url else [])
    cond = getattr(step, "condition", None)
    if cond is not None:
        out += cond.strings()
    if isinstance(step, BranchStep):
        if step.if_.locator:
            out += step.if_.locator.strings()
        if step.if_.condition:
            out += step.if_.condition.strings()
    if isinstance(step, PreflightStep):
        for c in step.checks:
            out += ((c.locator.strings() if c.locator else []) + ([c.expect] if c.expect else [])
                    + ([c.text] if c.text else []))
    if isinstance(step, ExtractStep):
        out += step.items.strings()
        for f in step.fields.values():
            out += f.locator.strings() if f.locator else []
    return out


def _step_conditions(step: Any) -> list[Condition]:
    out: list[Condition] = []
    if getattr(step, "readback", None) is not None:
        out.append(step.readback.condition)
    if getattr(step, "condition", None) is not None:
        out.append(step.condition)
    if isinstance(step, BranchStep) and step.if_.condition is not None:
        out.append(step.if_.condition)
    return out


def _check_writes(steps: list[Any], prefix: str) -> None:
    for i, step in enumerate(steps):
        if getattr(step, "writes", False) and getattr(step, "readback", None) is None:
            nxt = steps[i + 1] if i + 1 < len(steps) else None
            if not isinstance(nxt, ReadbackStep):
                raise ValueError(
                    f"step {prefix}{i} ({step.type}) writes, so it must declare a readback of the "
                    "target's own state (an inline readback, or a readback step right after it)")
        if isinstance(step, BranchStep):
            _check_writes(step.then, f"{prefix}{i}.then.")
            _check_writes(step.else_, f"{prefix}{i}.else.")
        elif isinstance(step, ForEachStep):
            _check_writes(step.steps, f"{prefix}{i}.steps.")


def _check_watches(steps: list[Any], prefix: str) -> None:
    """Every read_json sits after the navigate it watches, in the same list."""
    for i, step in enumerate(steps):
        if step.type == "read_json" and not any(s.type == "navigate" and i in watchers(steps, at)
                                                for at, s in enumerate(steps[:i])):
            raise ValueError(f"step {prefix}{i} (read_json) comes right after the navigate it watches, because "
                             "listening starts before the page loads")
        if isinstance(step, BranchStep):
            _check_watches(step.then, f"{prefix}{i}.then.")
            _check_watches(step.else_, f"{prefix}{i}.else.")
        elif isinstance(step, ForEachStep):
            _check_watches(step.steps, f"{prefix}{i}.steps.")


class FlowVersion(BaseModel):
    definition: FlowDefinition
    version: str
    created_at: datetime = Field(default_factory=utcnow)
    created_by: str | None = None
    saved_by: dict[str, Any] | None = None  # the principal that saved it: {kind, id, owner}

    @property
    def id(self) -> str:
        return self.definition.id

    @property
    def owner_saved(self) -> bool:
        return is_owner(self.saved_by)

    @property
    def effect(self) -> Effect:
        """The effective effect: what grants, approvals and the tool list use."""
        return self.definition.effective_effect(owner_saved=self.owner_saved)[0]

    @property
    def effect_reason(self) -> str | None:
        """Why the effective effect is above the declared one, for flows_list and the panel."""
        eff, why = self.definition.effective_effect(owner_saved=self.owner_saved)
        if why is None:
            return None
        return (f"raised from {self.definition.effect} to {eff}: {why} is input that can publish "
                "(only the owner can mark a step \"publishes\": false)")


# ---------------------------------------------------------------- runs


class RunKind(StrEnum):
    FLOW = "flow"
    BRAIN = "brain"
    SCRIPT = "script"
    WORKFLOW = "workflow"
    PILOT = "pilot"  # agentos.pilot: lane_do's cheap worker on one lane


TERMINAL = frozenset({RunStatus.SUCCEEDED_VERIFIED, RunStatus.SUCCEEDED_UNVERIFIED, RunStatus.PARTIAL,
                      RunStatus.FAILED, RunStatus.CANCELLED})
ACTIVE = frozenset({RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL})
SUCCEEDED = frozenset({RunStatus.SUCCEEDED_VERIFIED, RunStatus.SUCCEEDED_UNVERIFIED})


class Run(BaseModel):
    id: str
    kind: RunKind = RunKind.FLOW
    lane: str | None = None
    # a connector run: the AUD-01 surface it acts as. Grants, approvals and pace see the surface;
    # the lane is the login it runs on (the surface's binding), held one run at a time
    surface: str | None = None
    flow_id: str | None = None
    flow_version: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus = RunStatus.QUEUED
    attempts: int = 0
    deferrals: int = 0
    held: int = 0  # deferrals because a human held the lane (never count toward giving up)
    idempotency_key: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    not_before: datetime | None = None
    start_after: datetime | None = None  # the earliest start the caller asked for (flow_run not_before)
    parked_at: datetime | None = None
    proof: dict[str, Any] | None = None
    # a connector run's typed result: {data, untrusted_fields, private_fields, truncated, source}
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    cost: dict[str, Any] = Field(default_factory=lambda: {"tokens": 0, "usd": 0.0, "ms": 0})
    parent: str | None = None
    principal: dict[str, Any] = Field(default_factory=dict)
    grant_id: str | None = None  # the grant that created the run, re-checked before it runs
    source: str = "tool"
    schedule_id: str | None = None
    trigger: dict[str, Any] | None = None
    approval_id: str | None = None
    authorized: bool = False
    auto: bool = False  # authorized because nothing asked ("Ask me first" off), not by a person or a standing approval
    pace_id: int | None = None  # its row on the lane's pace ledger (agentos.policy.pace), once it holds one
    decision: dict[str, Any] | None = None  # {"needed": {...}} or {"choice": ..., "path": ...}
    recheck_at: datetime | None = None
    rechecks: int = 0
    cancel_requested: bool = False
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def workspace(self) -> str:
        """The workspace the run works for: its surface's (the brand), else its lane's."""
        target = self.surface or self.lane
        return target.split("/", 1)[0] if target else "_"

    @property
    def target(self) -> str | None:
        """What grants and approvals check: the surface of a connector run, else the lane."""
        return self.surface or self.lane

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def needs_decision(self) -> bool:
        return bool(self.decision and self.decision.get("needed"))


class StepStatus(StrEnum):
    PENDING = "pending"
    STARTED = "started"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepRecord(BaseModel):
    run_id: str
    path: str
    idx: int = 0
    type: str = ""
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


# ---------------------------------------------------------------- schedules and triggers


class ScheduleKind(StrEnum):
    CRON = "cron"
    WATCH = "watch"
    WEBHOOK = "webhook"
    EVERY = "every"


DEFAULT_TIMEZONE = "Europe/Vilnius"


class CronConfig(_Strict):
    cron: str
    timezone: str = DEFAULT_TIMEZONE
    missed: Literal["skip", "catch_up_once"] = "skip"
    jitter_s: int = Field(default=0, ge=0, le=3600)
    grace_s: int = Field(default=300, ge=30, le=86_400)


class WatchConfig(_Strict):
    url: str
    locator: Locator
    read: Literal["text", "count"] = "text"
    every_s: int = Field(default=300, ge=30, le=86_400)
    debounce_s: int = Field(default=0, ge=0, le=86_400)


class EveryConfig(_Strict):
    """A run every so often, whatever the clock says. What is late runs once."""
    every_s: int = Field(ge=60, le=7 * 86_400)


class WebhookConfig(_Strict):
    accept_params: bool = False
    max_skew_s: int = Field(default=300, ge=10, le=3600)
    token: str = ""
    secret: str = Field(default="", repr=False)


class Schedule(BaseModel):
    """A cron slot, page watch, webhook or interval. It fires either a flow (`flow_id`) or an events
    route (`route_id`, agentos.events): exactly one of them. A route schedule emits an event
    keyed exactly as the flow run would be, and the route decides what it wakes."""
    id: str
    kind: ScheduleKind
    flow_id: str | None = None
    route_id: str | None = None
    flow_version: str | None = None  # pin a version; None runs the current one
    lane: str | None = None
    surface: str | None = None  # a connector schedule's surface; each fire resolves its binding again
    params: dict[str, Any] = Field(default_factory=dict)
    cron: CronConfig | None = None
    watch: WatchConfig | None = None
    webhook: WebhookConfig | None = None
    every: EveryConfig | None = None
    deadman_minutes: int | None = Field(default=None, ge=1)
    max_failures: int = Field(default=3, ge=1, le=100)
    enabled: bool = True
    principal: dict[str, Any] = Field(default_factory=dict)
    grant_id: str | None = None  # the creator's grant; every fire runs under it
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    state: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_target(self) -> Schedule:
        if (self.flow_id is None) == (self.route_id is None):
            raise ValueError("a schedule fires exactly one of a flow (flow_id) or a trigger (route_id)")
        return self

    @property
    def target(self) -> str:
        """What it fires, for messages: the flow id or "trigger rt_..."."""
        return self.flow_id or f"trigger {self.route_id}"

    def public(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        if self.webhook:
            data["webhook"] = {"accept_params": self.webhook.accept_params,
                               "max_skew_s": self.webhook.max_skew_s,
                               "path": f"/api/hooks/{self.webhook.token[:6]}...",
                               "secret": "(hidden; shown once at creation)"}
        return data

