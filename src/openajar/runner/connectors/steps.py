"""Connector steps: ordinary flow steps plus the extensions connectors need.

A connector's `steps` are runner flow steps (openajar.runner.steps.Step) with the extensions
connectors need (docs/design/connectors.md 4.5). The runner executes all of them (Brief B);
the source format keeps its own models because a connector writes some of them shorter than
the compiled flow (assert_identity is filled in from acting_as):

    locator `attr`   {"name": "href", "equals" | "prefix" | "regex": "..."}: a machine attribute
    locator `names`  {"en": "Share", "lt": "Bendrinti"}: an accessible name per account language,
                     picked by the hidden `account_language` param (in place of `name`)
    assert_identity  {"type": "assert_identity"}: the compiler fills in how acting as is read
    extract          read up to max_items records under an anchor into a saved list
    read_json        read up to max_items records from a JSON response the page itself made while the
                     navigate before it loaded (openajar.runner.models.ReadJsonStep, as written here)
    for_each         run read-only steps for each item of a saved list (the item is ${vars.<as>})

The compiler adds one more, `preflight`, right before the commit step. Which of these the
runner supports is detected from its models (`runner_capabilities`), so a node whose runner
lacks one (an older release) compiles the operation with PENDING_MARKER and refuses to install it.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from openajar.core.locator import AttrMatch, Locator
from openajar.runner.steps import ACTION_TYPES, ClickStep, ReadJsonStep, Step

EXTENSION_STEPS = frozenset({"assert_identity", "extract", "for_each", "preflight", "read_json"})
EXTENSION_LOCATOR_KEYS = ("attr", "names")
PENDING_MARKER = "x-agentos-pending"  # on a compiled step that needs a runner capability not built yet
LANG_RE = re.compile(r"^[a-z]{2}$")
SAVE_AS_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _regex(pattern: str, what: str, *, groups: int | None = None) -> None:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"invalid {what} regex: {e}") from e
    if groups is not None and rx.groups != groups:
        raise ValueError(f"{what} regex must have exactly {groups} capture group(s)")


class ExtractField(_Strict):
    """One field of an extracted record: an attribute or a text read of the item (or of an
    element under it), checked against a tight pattern or clipped to max_chars."""

    locator: dict[str, Any] | None = None  # under the item; default: the item itself
    attr: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_:.-]{0,60}$")
    read: Literal["own_text", "text", "value"] | None = None
    pattern: str | None = None
    capture: str | None = None
    format: Literal["date-time", "uri", "integer", "number"] | None = None
    host: str | None = None
    max_chars: int | None = Field(default=None, ge=1, le=20_000)
    untrusted: bool = False
    private: bool = False

    @model_validator(mode="after")
    def _check(self) -> ExtractField:
        if (self.attr is None) == (self.read is None):
            raise ValueError("a field reads exactly one of attr (a machine attribute) or read (text)")
        if self.pattern is not None:
            _regex(self.pattern, "field pattern")
        if self.capture is not None:
            _regex(self.capture, "field capture", groups=1)
        if self.locator is not None:
            check_locator(self.locator)
        return self


class ExtractStep(_Strict):
    type: Literal["extract"]
    label: str | None = None
    save_as: str = Field(pattern=SAVE_AS_RE.pattern)
    items: dict[str, Any]  # every match is an item, so no ambiguity error
    max_items: int = Field(ge=1, le=500)
    fields: dict[str, ExtractField] = Field(min_length=1)
    pick: Literal["outermost", "card"] | None = None
    drop_empty: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> ExtractStep:
        check_locator(self.items)
        for name in self.fields:
            if not SAVE_AS_RE.match(name):
                raise ValueError(f"field name {name!r} must be lowercase_with_underscores")
        return self


class ForEachStep(_Strict):
    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)
    type: Literal["for_each"]
    label: str | None = None
    over: str = Field(pattern=SAVE_AS_RE.pattern)  # a list saved earlier (by extract)
    as_: str = Field(alias="as", pattern=SAVE_AS_RE.pattern)
    max_items: int = Field(ge=1, le=200)
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=50)


class AssertIdentityStep(_Strict):
    type: Literal["assert_identity"]
    label: str | None = None


_STEP: TypeAdapter[Any] = TypeAdapter(Step)

EXTENSION_MODELS: dict[str, type[BaseModel]] = {
    "extract": ExtractStep, "for_each": ForEachStep, "assert_identity": AssertIdentityStep, "read_json": ReadJsonStep,
}


# ---------------------------------------------------------------- locators


def check_locator(loc: Any) -> None:
    """Validate a connector locator: the runner's Locator plus `attr` and `names`."""
    if not isinstance(loc, dict):
        raise ValueError("a locator is an object like {role, name, within, nth, css, attr, names}")  # noqa: TRY004
    if loc.get("attr") is not None:
        AttrMatch.model_validate(loc["attr"])
    names = loc.get("names")
    if names is not None:
        if not isinstance(names, dict) or not names:
            raise ValueError("names maps account languages to accessible names: {en: Share, lt: Bendrinti}")
        for lang, name in names.items():
            if not LANG_RE.match(str(lang)) or not isinstance(name, str) or not name.strip():
                raise ValueError(f"names: {lang!r} must be a two-letter language with a non-empty name")
        if loc.get("name") is not None:
            raise ValueError("a locator has either name or names, not both")
        if not loc.get("role"):
            raise ValueError("names need a role (an accessible name belongs to a role)")
    if loc.get("within") is not None:
        check_locator(loc["within"])
    Locator.model_validate(shadow_locator(loc))


def shadow_locator(loc: dict[str, Any]) -> dict[str, Any]:
    """The locator the runner validates today: `names` becomes one `name`, `attr` is dropped
    (a placeholder css keeps the role-or-css rule satisfied)."""
    out = {k: v for k, v in loc.items() if k not in EXTENSION_LOCATOR_KEYS}
    names = loc.get("names")
    if isinstance(names, dict) and names:
        out["name"] = names.get("en") or next(iter(names.values()))
    if not out.get("role") and not out.get("css"):
        out["css"] = "*"
    if isinstance(loc.get("within"), dict):
        out["within"] = shadow_locator(loc["within"])
    return out


def locator_uses(loc: dict[str, Any] | None) -> set[str]:
    """The locator extensions a locator (and its anchors) use: {'locator:attr', ...}."""
    out: set[str] = set()
    while isinstance(loc, dict):
        out |= {f"locator:{k}" for k in EXTENSION_LOCATOR_KEYS if loc.get(k) is not None}
        loc = loc.get("within")
    return out


def is_structural(loc: dict[str, Any]) -> bool:
    """A locator that does not lean on visible text alone: an attribute, an anchor, or a role
    with no name. css-only (no role, no attr) and name-only (role plus name) are not."""
    if loc.get("attr") is not None or loc.get("within") is not None:
        return True
    if loc.get("role"):
        return loc.get("name") is None and loc.get("names") is None
    return False


# ---------------------------------------------------------------- walking steps


def children(step: dict[str, Any], path: str) -> Iterator[tuple[str, list[Any]]]:
    if step.get("type") == "branch":
        yield f"{path}.then.", step.get("then") or []
        yield f"{path}.else.", step.get("else") or []
    elif step.get("type") == "for_each":
        yield f"{path}.steps.", step.get("steps") or []


def walk(steps: list[Any], prefix: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """(path, step) depth first, like runner.models.iter_steps, through branches and for_each."""
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        path = f"{prefix}{i}"
        yield path, step
        for sub_prefix, sub in children(step, path):
            yield from walk(sub, sub_prefix)


def step_locators(step: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every locator a step addresses: ('locator' | 'readback' | 'if' | 'items' | 'field x' |
    'check n', locator)."""
    out: list[tuple[str, dict[str, Any]]] = []
    if isinstance(step.get("locator"), dict):
        out.append(("locator", step["locator"]))
    rb = step.get("readback")
    if isinstance(rb, dict) and isinstance(rb.get("locator"), dict):
        out.append(("readback", rb["locator"]))
    cond = step.get("if")
    if isinstance(cond, dict) and isinstance(cond.get("locator"), dict):
        out.append(("if", cond["locator"]))
    if isinstance(step.get("items"), dict):
        out.append(("items", step["items"]))
    fields = step.get("fields")
    for name, field in (fields if isinstance(fields, dict) else {}).items():  # a list is refused at save, not read here
        if isinstance(field, dict) and isinstance(field.get("locator"), dict):
            out.append((f"field {name}", field["locator"]))
    for n, check in enumerate(step.get("checks") or []):
        if isinstance(check, dict) and isinstance(check.get("locator"), dict):
            out.append((f"check {n}", check["locator"]))
    return out


def step_urls(step: dict[str, Any]) -> list[str]:
    """The URLs a step loads. A read_json's `url` is a pattern and loads nothing."""
    out = [step["url"]] if isinstance(step.get("url"), str) and step.get("type") != "read_json" else []
    rb = step.get("readback")
    if isinstance(rb, dict) and isinstance(rb.get("url"), str):
        out.append(rb["url"])
    return out


def is_input(step: dict[str, Any]) -> bool:
    return step.get("type") in ACTION_TYPES


def validate_step(step: Any) -> None:
    """Validate one connector step: an extension against its model, anything else against
    the runner's Step (through the shadow, so extension locators inside are allowed)."""
    if not isinstance(step, dict) or not isinstance(step.get("type"), str):
        raise ValueError("a step is an object with a type")  # noqa: TRY004
    t = step["type"]
    if t == "preflight":
        raise ValueError("preflight is added by the compiler from the commit block; do not write it as a step")
    if t in EXTENSION_MODELS:
        EXTENSION_MODELS[t].model_validate(step)
        for _, sub in children(step, ""):
            for s in sub:
                validate_step(s)
        return
    for _, loc in step_locators(step):
        check_locator(loc)
    for _, sub in children(step, ""):
        for s in sub:
            validate_step(s)
    try:
        _STEP.validate_python(shadow_steps([step])[0])
    except ValidationError as e:
        raise ValueError(_first_error(e)) from e


def _first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    loc = ".".join(str(x) for x in err.get("loc", ()) if not str(x).startswith("function-"))
    msg = err.get("msg", "invalid").removeprefix("Value error, ")
    return f"{loc}: {msg}" if loc else msg


# ---------------------------------------------------------------- the shadow flow


def shadow_steps(steps: list[Any]) -> list[dict[str, Any]]:
    """The steps as the runner can validate them today. Indices are kept (an extension step
    becomes a no-op pause, extract a read that saves the same name, for_each a branch around
    its steps), so errors and effects name the same paths as the connector."""
    out: list[dict[str, Any]] = []
    for step in steps:
        out.append(_shadow_step(step))
    return out


def _shadow_step(step: dict[str, Any]) -> dict[str, Any]:
    t = step.get("type")
    label = {"label": step["label"]} if step.get("label") else {}
    if t in ("assert_identity", "preflight"):
        return {"type": "pause", "min_ms": 0, "max_ms": 0, **label}
    if t == "extract":
        return {"type": "wait_for", "state": "any", "locator": shadow_locator(step.get("items") or {}),
                "save_as": step.get("save_as"), **label}
    if t == "read_json":
        return {"type": "wait_for", "state": "any", "locator": {"css": "*"}, "save_as": step.get("save_as"), **label}
    if t == "for_each":
        return {"type": "branch", "if": {"param": "account_language", "is_set": True},
                "then": shadow_steps(step.get("steps") or []), **label}
    s = {k: v for k, v in step.items() if k != PENDING_MARKER}
    if isinstance(s.get("locator"), dict):
        s["locator"] = shadow_locator(s["locator"])
    if isinstance(s.get("readback"), dict) and isinstance(s["readback"].get("locator"), dict):
        s["readback"] = {**s["readback"], "locator": shadow_locator(s["readback"]["locator"])}
    if t == "branch":
        cond = s.get("if")
        if isinstance(cond, dict) and isinstance(cond.get("locator"), dict):
            s["if"] = {**cond, "locator": shadow_locator(cond["locator"])}
        s["then"] = shadow_steps(s.get("then") or [])
        s["else"] = shadow_steps(s.get("else") or [])
    return s


# ---------------------------------------------------------------- what the runner supports


def runner_capabilities() -> frozenset[str]:
    """What the runner's models accept today: 'step:<type>' for every Step type,
    'locator:<field>' for every Locator field, 'action:<field>' for every action step field."""
    union = get_args(Step)[0]
    types = {cls.model_fields["type"].default for cls in get_args(union)}
    caps = {f"step:{t}" for t in types}
    caps |= {f"locator:{f}" for f in Locator.model_fields}
    caps |= {f"action:{f}" for f in ClickStep.model_fields}
    return frozenset(caps)


def step_needs(step: dict[str, Any]) -> set[str]:
    """Capabilities this one step needs (not its children)."""
    t = step.get("type")
    needs = {f"step:{t}"} if t in EXTENSION_STEPS else set()
    for _, loc in step_locators(step):
        needs |= locator_uses(loc)
    return needs


def mark_pending(steps: list[Any], supported: frozenset[str]) -> set[str]:
    """Mark every step that needs a missing capability with PENDING_MARKER; return them all."""
    missing: set[str] = set()
    for _, step in walk(steps):
        need = sorted(step_needs(step) - supported)
        if need:
            step[PENDING_MARKER] = need
            missing |= set(need)
    return missing


def deep(value: Any) -> Any:
    return copy.deepcopy(value)
