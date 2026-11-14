"""Structural locators and conditions: how a step finds an element and tests the page.

A locator is role + accessible name + nth + an ancestor anchor + a CSS fallback, never a
snapshot ref (refs change on every page load).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from agentos.core.template import TemplateError, render

REF_MESSAGE = ("snapshot refs (like e12) change on every page load; address the element with a "
               "structural locator instead: role, name, within (an ancestor anchor), nth, css")
_REF_VALUE_RE = re.compile(r"^\[?(?:aria-)?ref=|\[ref=|^e\d+$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- locators


ATTR_NAME_RE = r"^[a-z][a-z0-9_:.-]{0,60}$"
LANG_RE = re.compile(r"^[a-z]{2}$")


def check_regex(pattern: str, what: str, *, groups: int | None = None) -> None:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"invalid {what} regex: {e}") from e
    if groups is not None and rx.groups != groups:
        raise ValueError(f"{what} regex must have exactly {groups} capture group(s)")


class AttrMatch(StrictModel):
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
            check_regex(self.regex, "attr")
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


class Locator(StrictModel):
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
            for pattern in [self.name, *(self.names or {}).values()]:
                if pattern and "${" not in pattern:
                    try:
                        re.compile(pattern)
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


class Condition(StrictModel):
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
