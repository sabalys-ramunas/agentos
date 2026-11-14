"""The step types of a flow, and the helpers that walk, address and rate them.

A step's effect: navigation, waits, scrolls and reads are `read`; any input step is `commit`,
unless the owner marked it `"publishes": false` (then `draft`).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from agentos.core.locator import ATTR_NAME_RE, REF_MESSAGE, Condition, Locator, StrictModel, check_regex
from agentos.core.models import Effect
from agentos.runner.names import PARAM_NAME_RE
from agentos.runner.urlpattern import JSON_PATH_RE, JSON_ROOT, names_secret, url_pattern


class Readback(StrictModel):
    """A readback attached to an action step. Without a locator it reads the step's own
    target. `url` (optional) is navigated first, which also lets a later re-check run."""

    locator: Locator | None = None
    condition: Condition
    url: str | None = None
    timeout_ms: int = Field(default=8000, ge=0, le=120_000)


# ---------------------------------------------------------------- steps


class _StepBase(StrictModel):
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


class Check(StrictModel):
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


class IdentityCheck(StrictModel):
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
        check_regex(self.pattern, "identity pattern", groups=1)
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


class PreflightCheck(StrictModel):
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


class ExtractField(StrictModel):
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
            check_regex(self.pattern, "field pattern")
        if self.capture is not None:
            check_regex(self.capture, "field capture")
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


def _json_path(path: str, what: str) -> None:
    if not JSON_PATH_RE.fullmatch(path):
        raise ValueError(f"{what} is a plain dotted path like data.orders: keys of letters, digits, _ and -, "
                         "with no wildcard, index or expression, and nothing is evaluated")
    if any(names_secret(part) for part in path.split(".")):
        raise ValueError(f"{what} {path!r} names a token, password, secret, session, cookie or authorization: a site's "
                         "credentials must never reach an output")


class JsonField(StrictModel):
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
STEPS_ADAPTER: TypeAdapter[list[Any]] = TypeAdapter(list[Step])

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
