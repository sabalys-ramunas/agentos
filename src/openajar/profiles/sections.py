"""The sections a profile carries for building and designing (docs/design/studio.md, decisions 6 to 8).

    brand:                              # product and business profiles
      tokens: {...}                     # Design Tokens (DTCG 2025.10) as JSON, at most 64 KB
      fonts: [{family: Inter, role: body, weights: [400, 600], file: sha256:...}]
      logo: sha256:...                  # files kept by the profile store (profile_photo_put)
      assets: {icon: sha256:...}
      components: [{name: Button, attr: data-slot=button}, {name: Card, class_pattern: "^card"}]
      theme: {how: attr, name: data-theme, light: light, dark: dark}
      widths: [360, 768, 1280]
      engine: chromium
    guidelines:                         # any kind but reviewer and audience
      - {id: text-contrast, text: Body text is easy to read, check: contrast.text, threshold: 4.5}
      - {id: calm, text: One accent colour per screen, advisory: true}
    references:
      - {name: Linear, url: https://linear.app, note: the density we want}
    environments:                       # product profiles: where it runs, and the vault entries it needs
      - {name: staging, url: https://staging.acme.example, keys: {stripe: acme/stripe-test}}
    reviewer: {cares_about: [...], rubric: [...], examples: [...]}         # reviewer profiles
    audience: {screen_width: 360, language: lt, network: slow_3g, goals: [...], hard: [...]}

Every string is cleaned with `clean_text` and capped, so page text an agent brought in (a brand
learned from a live site) reaches the owner's approval card as plain, visible text. Nothing here
is a secret: environments name vault entries, and any value that looks like a credential is
refused (`secret_in_profile`), as memory does. Nothing here grants a tool or a permission
either: a reviewer is a judging job, described in words.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

# models.py imports this module after it defines these (its sections come from here)
from openajar.profiles.fields import PHOTO_RE, Strict, clean_text, clean_url

MAX_TOKENS_CHARS = 64_000       # the DTCG JSON, compact
MAX_TOKENS = 2000
MAX_TOKEN_DEPTH = 10
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")
CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){0,3}$")
ATTR_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:.-]{0,60}(=[^\s\"'<>=]{1,100})?$")
LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?$")

# ---- secrets ----------------------------------------------------------------------------

_scanner: Any = None


def _secret_kinds(text: str) -> list[str]:
    global _scanner
    if _scanner is None:
        from openajar.memory.scan import SecretScanner, default_scrubbers

        _scanner = SecretScanner(None, default_scrubbers())
    return _scanner.kinds(text)


def _strings(value: Any, path: str, depth: int = 0) -> list[tuple[str, str]]:
    if depth > 14:
        return []
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            out += [(f"{path}.{k}", str(k))] + _strings(v, f"{path}.{k}", depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [s for i, v in enumerate(value) for s in _strings(v, f"{path}[{i}]", depth + 1)]
    return []


def refuse_secrets(value: Any, where: str) -> None:
    """Raise `secret_in_profile` when any string in `value` looks like a credential. The message
    names the field and the kind, never the value."""
    for path, text in _strings(value, where):
        kinds = _secret_kinds(text)
        if kinds:
            raise PydanticCustomError(
                "secret_in_profile",
                "{where} looks like it holds a secret ({kinds}). A profile never holds secrets: keep it in the "
                "vault and name the vault entry instead (environments[].keys)",
                {"where": path, "kinds": ", ".join(sorted(set(kinds)))})


def _clean_list(values: list[str], what: str, limit: int) -> list[str]:
    return [clean_text(v, what, limit) for v in values]


def _unique(ids: list[str], what: str) -> None:
    seen: set[str] = set()
    for i in ids:
        if i in seen:
            raise ValueError(f"{what} {i!r} is listed twice")
        seen.add(i)


def _slug(v: str, what: str) -> str:
    text = clean_text(v, what, 48)
    if not SLUG_RE.match(text):
        raise ValueError(f"{what} {text[:40]!r} is a short lowercase name like 'text-contrast'")
    return text


def _file_ref(v: str | None, what: str) -> str | None:
    if v is None:
        return None
    text = str(v).strip()
    if not PHOTO_RE.match(text):
        raise ValueError(f"{what} is a file the profile store keeps (sha256: and 64 hex digits, from "
                         f"profile_photo_put), not {text[:40]!r}")
    return text


# ---- design tokens (DTCG 2025.10) -------------------------------------------------------

TOKEN_TYPES = {"color", "dimension", "fontFamily", "fontWeight", "duration", "cubicBezier", "number",
               "strokeStyle", "border", "transition", "shadow", "gradient", "typography"}
COLOR_SPACES = {"srgb", "srgb-linear", "hsl", "hwb", "lab", "lch", "oklab", "oklch", "display-p3", "a98-rgb",
                "prophoto-rgb", "rec2020", "xyz-d65", "xyz-d50"}
_TOKEN_PROPS = {"$value", "$type", "$description", "$extensions", "$deprecated"}
_GROUP_PROPS = {"$type", "$description", "$extensions", "$deprecated", "$extends", "$schema"}
_ALIAS_RE = re.compile(r"^\{([^{}]+)\}$")
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_DIM_RE = re.compile(r"^(-?\d+(?:\.\d+)?)(px|rem)$")
_DUR_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s)$")


def _num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _hex_color(text: str) -> dict[str, Any]:
    h = text[1:]
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [round(int(h[i:i + 2], 16) / 255, 4) for i in (0, 2, 4)]
    out: dict[str, Any] = {"colorSpace": "srgb", "components": rgb, "hex": "#" + h[:6].lower()}
    if len(h) == 8:
        out["alpha"] = round(int(h[6:8], 16) / 255, 4)
    return out


def _value(path: str, kind: str, value: Any) -> Any:
    """A token's $value for its type, normalized to the 2025.10 object forms where a plain string
    is common (a hex colour, "16px", "200ms")."""
    if isinstance(value, dict) and set(value) == {"$ref"}:
        if not (isinstance(value["$ref"], str) and value["$ref"].startswith("#/")):
            raise ValueError(f"brand.tokens.{path}: $ref is a JSON pointer like '#/color/blue/500'")
        return value
    check = _VALUE_CHECKS.get(kind)
    if check is not None:
        return check(path, kind, value)
    # composites (border, shadow, typography, ...): their parts are checked as strings below
    if not isinstance(value, dict | list | str):
        raise ValueError(f"brand.tokens.{path}: a {kind} is an object (or a list of them)")  # noqa: TRY004 - pydantic reads ValueError
    return value


def _color_value(path: str, kind: str, value: Any) -> Any:
    if isinstance(value, str) and _HEX_RE.match(value):
        return _hex_color(value)
    if not isinstance(value, dict) or value.get("colorSpace") not in COLOR_SPACES:
        raise ValueError(f"brand.tokens.{path}: a colour is a hex string like '#3366cc' or "
                         "{colorSpace: srgb, components: [0.2, 0.4, 0.8]}")
    comps = value.get("components")
    if not (isinstance(comps, list) and len(comps) == 3 and all(_num(c) or c == "none" for c in comps)):
        raise ValueError(f"brand.tokens.{path}: components is a list of three numbers")
    if "alpha" in value and not (_num(value["alpha"]) and 0 <= value["alpha"] <= 1):
        raise ValueError(f"brand.tokens.{path}: alpha is a number from 0 to 1")
    if "hex" in value and not (isinstance(value["hex"], str) and _HEX_RE.match(value["hex"])):
        raise ValueError(f"brand.tokens.{path}: hex is like '#3366cc'")
    extra = set(value) - {"colorSpace", "components", "alpha", "hex"}
    if extra:
        raise ValueError(f"brand.tokens.{path}: a colour has no {', '.join(sorted(extra))}")
    return value


def _measure_value(path: str, kind: str, value: Any) -> Any:
    """A dimension (px, rem) or a duration (ms, s), as {value, unit}."""
    pattern, units = (_DIM_RE, {"px", "rem"}) if kind == "dimension" else (_DUR_RE, {"ms", "s"})
    if isinstance(value, str) and pattern.match(value.strip()):
        m = pattern.match(value.strip())
        n = float(m.group(1))
        return {"value": int(n) if n.is_integer() else n, "unit": m.group(2)}
    if not (isinstance(value, dict) and set(value) == {"value", "unit"} and _num(value["value"])
            and value["unit"] in units):
        unit = min(units)
        raise ValueError(f"brand.tokens.{path}: a {kind} is like {{value: 16, unit: {unit}}} or '16{unit}'")
    return value


def _number_value(path: str, kind: str, value: Any) -> Any:
    if not _num(value):
        raise ValueError(f"brand.tokens.{path}: a number token's $value is a number")
    return value


def _weight_value(path: str, kind: str, value: Any) -> Any:
    if not ((_num(value) and 1 <= value <= 1000) or (isinstance(value, str) and len(value) <= 30)):
        raise ValueError(f"brand.tokens.{path}: a font weight is a number from 1 to 1000, or a name like 'bold'")
    return value


def _family_value(path: str, kind: str, value: Any) -> Any:
    if not (isinstance(value, str) or (isinstance(value, list) and value and all(isinstance(x, str) for x in value))):
        raise ValueError(f"brand.tokens.{path}: a font family is a name or a list of names")
    return value


def _bezier_value(path: str, kind: str, value: Any) -> Any:
    if not (isinstance(value, list) and len(value) == 4 and all(_num(x) for x in value)):
        raise ValueError(f"brand.tokens.{path}: a cubic Bezier is four numbers")
    return value


# A token type, as the function that checks (and normalizes) its $value.
_VALUE_CHECKS = {"color": _color_value, "dimension": _measure_value, "duration": _measure_value,
                 "number": _number_value, "fontWeight": _weight_value, "fontFamily": _family_value,
                 "cubicBezier": _bezier_value}


def _text_leaves(value: Any, path: str, depth: int = 0) -> None:
    if depth > MAX_TOKEN_DEPTH + 4:
        raise ValueError(f"brand.tokens.{path} is nested too deep")
    if isinstance(value, str):
        clean_text(value, f"brand.tokens.{path}", 500) if value.strip() else None
    elif isinstance(value, dict):
        for k, v in value.items():
            _text_leaves(v, f"{path}.{k}", depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _text_leaves(v, f"{path}[{i}]", depth + 1)


def check_tokens(data: Any) -> dict[str, Any]:
    """Validate a Design Tokens (DTCG 2025.10) document and return it normalized. Refuses what
    would not import elsewhere: unknown $ properties, names with { } or a dot, a token with no
    type, an alias to a token that does not exist."""
    if not isinstance(data, dict):
        raise ValueError("brand.tokens is a Design Tokens (DTCG) JSON object: groups of tokens, each with $value")  # noqa: TRY004
    size = len(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    if size > MAX_TOKENS_CHARS:
        raise ValueError(f"brand.tokens has {size} characters as JSON; at most {MAX_TOKENS_CHARS}")
    walk = _TokenWalk()
    result = walk.group(data, [], None)
    for name, target in walk.aliases:
        if target not in walk.tokens and f"{target}.$root" not in walk.tokens:
            raise ValueError(f"brand.tokens.{name}: the alias {{{target}}} names no token in this set")
    return result


class _TokenWalk:
    """One walk of a token document: every token's type, and the aliases to check at the end."""

    def __init__(self) -> None:
        self.tokens: dict[str, str | None] = {}       # path -> type (None while only an alias says it)
        self.aliases: list[tuple[str, str]] = []

    def group(self, node: dict[str, Any], path: list[str], inherited: str | None) -> dict[str, Any]:
        """A group or a token, normalized, with what is inside it."""
        if len(path) > MAX_TOKEN_DEPTH:
            raise ValueError(f"brand.tokens.{'.'.join(path)} is nested more than {MAX_TOKEN_DEPTH} groups deep")
        where = ".".join(path) or "(top)"
        is_token = "$value" in node
        out: dict[str, Any] = {}
        kind = node.get("$type", inherited)
        if "$type" in node and node["$type"] not in TOKEN_TYPES:
            raise ValueError(f"brand.tokens.{where}: $type {str(node['$type'])[:30]!r} is not a DTCG type "
                             f"({', '.join(sorted(TOKEN_TYPES))})")
        for key, value in node.items():
            if key.startswith("$") and key != "$root":      # $root is a group's own token
                out[key] = _dollar_property(where, key, value, is_token)
                continue
            if is_token:
                raise ValueError(f"brand.tokens.{where}: a token (it has $value) holds no other tokens; "
                                 f"move {key!r} into a group")
            _check_name(where, key)
            if not isinstance(value, dict):
                raise ValueError(f"brand.tokens.{'.'.join([*path, key])}: a token is an object with $value, "  # noqa: TRY004
                                 f"like {{$value: '#3366cc'}}, not a bare value")
            out[key] = self.group(value, [*path, key], kind)
        if is_token:
            self._token(node, path, kind, out)
        return out

    def _token(self, node: dict[str, Any], path: list[str], kind: str | None, out: dict[str, Any]) -> None:
        name = ".".join(path)
        if not path:
            raise ValueError("brand.tokens: the top level is a group, not a token")
        raw = node["$value"]
        if isinstance(raw, str) and _ALIAS_RE.match(raw):
            self.aliases.append((name, _ALIAS_RE.match(raw).group(1)))
        elif kind is None:
            raise ValueError(f"brand.tokens.{name}: give it a $type (on the token or its group): "
                             f"{', '.join(sorted(TOKEN_TYPES))}")
        else:
            out["$value"] = _value(name, kind, raw)
        _text_leaves(out["$value"], f"{name}.$value")
        self.tokens[name] = kind
        if len(self.tokens) > MAX_TOKENS:
            raise ValueError(f"brand.tokens has more than {MAX_TOKENS} tokens")


def _dollar_property(where: str, key: str, value: Any, is_token: bool) -> Any:
    """A $ property of a token or a group, checked; its value as kept."""
    allowed = _TOKEN_PROPS if is_token else _GROUP_PROPS
    if key not in allowed:
        raise ValueError(f"brand.tokens.{where}: {key} is not a DTCG property of a {'token' if is_token else 'group'}")
    if key == "$description":
        return clean_text(value, f"brand.tokens.{where}.$description", 500, lines=True)
    if key == "$deprecated" and not isinstance(value, bool | str):
        raise ValueError(f"brand.tokens.{where}: $deprecated is true, false or a reason")
    if key == "$extends" and not (isinstance(value, str) and _ALIAS_RE.match(value)):
        raise ValueError(f"brand.tokens.{where}: $extends names a group like '{{base.colors}}'")
    if key == "$extensions":
        if not isinstance(value, dict):
            raise ValueError(f"brand.tokens.{where}: $extensions is an object")
        _text_leaves(value, f"{where}.$extensions")
    return value


def _check_name(where: str, key: str) -> None:
    if key != "$root" and (not key or any(c in key for c in "{}.") or len(key) > 64):
        raise ValueError(f"brand.tokens.{where}: {key[:40]!r} is not a token name (no braces or dots, "
                         "at most 64 characters)")
    clean_text(key, f"brand.tokens.{where} name", 64)


def flat_tokens(tokens: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every token as {name, type, value} (an alias keeps its {reference}), in document order."""
    out: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], path: list[str], inherited: str | None) -> None:
        kind = node.get("$type", inherited)
        if "$value" in node:
            row: dict[str, Any] = {"name": ".".join(path), "type": kind, "value": node["$value"]}
            if node.get("$description"):
                row["description"] = node["$description"]
            out.append(row)
            return
        for key, value in node.items():
            if not key.startswith("$") or key == "$root":
                walk(value, [*path, key], kind)

    walk(tokens or {}, [], None)
    return out


# ---- brand ------------------------------------------------------------------------------


class BrandFont(Strict):
    family: str
    role: Literal["body", "heading", "display", "mono", "ui"] | None = None
    weights: list[int] = Field(default_factory=list, max_length=9)
    file: str | None = None           # sha256:..., a font kept by the profile store
    url: str | None = None            # where it is served from, when not a file

    @field_validator("family")
    @classmethod
    def _family(cls, v: str) -> str:
        return clean_text(v, "brand.fonts.family", 100)

    @field_validator("weights")
    @classmethod
    def _weights(cls, v: list[int]) -> list[int]:
        if any(w < 100 or w > 900 for w in v):
            raise ValueError("brand.fonts.weights are numbers from 100 to 900")
        return sorted(set(v))

    @field_validator("file")
    @classmethod
    def _file(cls, v: str | None) -> str | None:
        return _file_ref(v, "brand.fonts.file")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return None if v is None else clean_url(v, "brand.fonts.url")


class BrandComponent(Strict):
    """How a known component is recognised in a page: an attribute (`data-slot=button`) or a
    class pattern (a regular expression), or both."""
    name: str
    attr: str | None = None
    class_pattern: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return clean_text(v, "brand.components.name", 60)

    @field_validator("attr")
    @classmethod
    def _attr(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = clean_text(v, "brand.components.attr", 160)
        if not ATTR_RE.match(text):
            raise ValueError(f"brand.components.attr {text[:40]!r} is an attribute, or attribute=value, like "
                             "'data-slot=button'")
        return text

    @field_validator("class_pattern")
    @classmethod
    def _pattern(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = clean_text(v, "brand.components.class_pattern", 200)
        try:
            re.compile(text)
        except re.error as e:
            raise ValueError(f"brand.components.class_pattern {text[:40]!r} is not a regular expression ({e})") from None
        return text

    @model_validator(mode="after")
    def _one(self) -> BrandComponent:
        if self.attr is None and self.class_pattern is None:
            raise ValueError(f"brand.components {self.name!r} needs attr or class_pattern to be recognised")
        return self


class BrandTheme(Strict):
    """How the product turns dark (openajar.studio.contracts.ThemeSwitch): `media` sets
    prefers-color-scheme only; `attr`, `class` and `storage` also set an attribute, a class or a
    storage key before the page loads."""
    how: Literal["media", "attr", "class", "storage"] = "media"
    name: str | None = None
    light: str | None = None
    dark: str | None = None

    @field_validator("name", "light", "dark")
    @classmethod
    def _text(cls, v: str | None, info: Any) -> str | None:
        return None if v is None else clean_text(v, f"brand.theme.{info.field_name}", 100)

    @model_validator(mode="after")
    def _complete(self) -> BrandTheme:
        if self.how in ("attr", "storage") and not (self.name and self.dark):
            raise ValueError(f"brand.theme with how={self.how} needs name (the "
                             f"{'attribute' if self.how == 'attr' else 'storage key'}) and dark (its value for dark)")
        if self.how == "class" and not self.dark:
            raise ValueError("brand.theme with how=class needs dark (the class that turns it dark)")
        return self


class Brand(Strict):
    tokens: dict[str, Any] | None = None
    fonts: list[BrandFont] = Field(default_factory=list, max_length=12)
    logo: str | None = None
    assets: dict[str, str] = Field(default_factory=dict)
    components: list[BrandComponent] = Field(default_factory=list, max_length=100)
    theme: BrandTheme | None = None
    widths: list[int] = Field(default_factory=list, max_length=8)
    engine: Literal["chromium", "firefox", "webkit"] | None = None
    # CSS selectors of regions studio leaves out of its checks: another product drawn or embedded in
    # the page, an exhibit shown on purpose. None (not []) keeps older profiles' hashes as they were.
    ignore: list[str] | None = Field(default=None, max_length=20)

    @field_validator("ignore")
    @classmethod
    def _ignore(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [clean_text(s, "brand.ignore", 200) for s in v] or None

    @field_validator("tokens")
    @classmethod
    def _tokens(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if v is None else check_tokens(v)

    @field_validator("logo")
    @classmethod
    def _logo(cls, v: str | None) -> str | None:
        return _file_ref(v, "brand.logo")

    @field_validator("assets")
    @classmethod
    def _assets(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 20:
            raise ValueError("brand.assets holds at most 20 files")
        return {_slug(k, "brand.assets name"): _file_ref(f, f"brand.assets.{k}") or "" for k, f in v.items()}

    @field_validator("widths")
    @classmethod
    def _widths(cls, v: list[int]) -> list[int]:
        if any(w < 240 or w > 3840 for w in v):
            raise ValueError("brand.widths are screen widths from 240 to 3840")
        return sorted(set(v))

    @model_validator(mode="after")
    def _names(self) -> Brand:
        _unique([c.name for c in self.components], "brand.components")
        return self

    def files(self) -> dict[str, str]:
        """Every stored file the brand names: {where: sha256 ref}."""
        out = {f"brand.assets.{k}": v for k, v in self.assets.items()}
        if self.logo:
            out["brand.logo"] = self.logo
        out.update({f"brand.fonts[{i}].file": f.file for i, f in enumerate(self.fonts) if f.file})
        return out


# ---- guidelines, references, environments -----------------------------------------------


class Guideline(Strict):
    """One rule. Either `check` (a studio check id, with an optional threshold: contrast.text at
    4.5) that ui_check measures, or `advisory: true` for a rule a person or a reviewer judges."""
    id: str
    text: str
    check: str | None = None
    threshold: float | None = None
    advisory: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _slug(v, "guidelines.id")

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        return clean_text(v, "guidelines.text", 500, lines=True)

    @field_validator("check")
    @classmethod
    def _check(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = clean_text(v, "guidelines.check", 60)
        if not CHECK_ID_RE.match(text):
            raise ValueError(f"guidelines.check {text[:40]!r} is a check id like 'contrast.text'")
        return text

    @model_validator(mode="after")
    def _kind(self) -> Guideline:
        if (self.check is None) == (not self.advisory):
            raise ValueError(f"guideline {self.id!r} is either checked (check: a check id like contrast.text) or "
                             "advisory (advisory: true), one of the two")
        if self.threshold is not None and self.check is None:
            raise ValueError(f"guideline {self.id!r}: a threshold goes with a check")
        return self


class Reference(Strict):
    name: str
    url: str
    note: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return clean_text(v, "references.name", 60)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return clean_url(v, "references.url")

    @field_validator("note")
    @classmethod
    def _note(cls, v: str | None) -> str | None:
        return None if v is None else clean_text(v, "references.note", 300, lines=True)


class Environment(Strict):
    """Where the product runs, and the vault entries its keys are in (names only)."""
    name: str
    url: str
    keys: dict[str, str] = Field(default_factory=dict)    # what the key is for -> vault entry name

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _slug(v, "environments.name")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        url = clean_url(v, "environments.url")
        if re.match(r"^https?://[^/]*@", url, re.IGNORECASE):
            raise ValueError("environments.url carries a login (user:password@); keep logins in the vault")
        return url

    @field_validator("keys")
    @classmethod
    def _keys(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 30:
            raise ValueError("environments.keys names at most 30 vault entries")
        return {_slug(k, "environments.keys name"): clean_text(e, f"environments.keys.{k}", 128)
                for k, e in v.items()}


# ---- reviewer and audience profiles -----------------------------------------------------


class RubricItem(Strict):
    id: str
    question: str
    weight: int = Field(default=1, ge=1, le=5)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _slug(v, "reviewer.rubric.id")

    @field_validator("question")
    @classmethod
    def _q(cls, v: str) -> str:
        return clean_text(v, "reviewer.rubric.question", 300, lines=True)


class Judgement(Strict):
    """An example of good judgement: what was looked at, the verdict, and why."""
    situation: str
    verdict: str
    why: str | None = None

    @field_validator("situation", "verdict", "why")
    @classmethod
    def _text(cls, v: str | None, info: Any) -> str | None:
        return None if v is None else clean_text(v, f"reviewer.examples.{info.field_name}", 500, lines=True)


class Reviewer(Strict):
    """A judging job an agent does. Words only: it names no tool and grants nothing."""
    cares_about: list[str] = Field(default_factory=list, max_length=20)
    rubric: list[RubricItem] = Field(default_factory=list, max_length=20)
    examples: list[Judgement] = Field(default_factory=list, max_length=10)

    @field_validator("cares_about")
    @classmethod
    def _cares(cls, v: list[str]) -> list[str]:
        return _clean_list(v, "reviewer.cares_about", 200)

    @model_validator(mode="after")
    def _ids(self) -> Reviewer:
        _unique([r.id for r in self.rubric], "reviewer.rubric")
        return self


class Audience(Strict):
    """A kind of user: the screen they use, their language and network, what they want and what
    they find hard."""
    screen_width: int | None = Field(default=None, ge=240, le=3840)
    language: str | None = None
    network: Literal["offline_often", "slow_3g", "fast_3g", "4g", "broadband"] | None = None
    goals: list[str] = Field(default_factory=list, max_length=10)
    hard: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not LANG_RE.match(str(v)):
            raise ValueError(f"audience.language {str(v)[:20]!r} is a language tag like 'lt' or 'en-GB'")
        return str(v)

    @field_validator("goals", "hard")
    @classmethod
    def _items(cls, v: list[str], info: Any) -> list[str]:
        return _clean_list(v, f"audience.{info.field_name}", 200)


def guidelines_ok(items: list[Guideline]) -> list[Guideline]:
    if len(items) > 100:
        raise ValueError("guidelines holds at most 100 rules")
    _unique([g.id for g in items], "guideline")
    return items


def references_ok(items: list[Reference]) -> list[Reference]:
    if len(items) > 30:
        raise ValueError("references holds at most 30 products")
    _unique([r.name for r in items], "reference")
    return items


def environments_ok(items: list[Environment]) -> list[Environment]:
    if len(items) > 10:
        raise ValueError("environments holds at most 10")
    _unique([e.name for e in items], "environment")
    return items
