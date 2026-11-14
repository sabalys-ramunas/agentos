"""The site API definition and what it compiles to (docs/SITE-APIS.md, "Definition").

    {"api": "harbor-supplier", "title": ..., "domain": "portal.northsupply.example", "version": "1.0.0",
     "surfaces": ["harbor/supplier"],
     "endpoints": {"open_orders": {"title": ..., "effect": "read", "input": {...}, "output": {...},
                   "steps": [...], "outputs": {"orders": "orders"}, "cache_s": 300,
                   "expect": {"min_items": {"orders": 1}}, "pace": {"per_hour": 60}}}}

`to_connector` turns a definition into a Connector document, so a site API cannot do anything a
connector cannot; compile_connector then lints and hashes it. What it adds on the way:

- `site` and `acting_as`: the site key and `account` when the domain has a row in
  agentos.node.sites (with an assert_identity step, so the wrong account check fires), else `web`
  and `lane`, the lane's own session.
- labels: text read from a page is untrusted unless a person says otherwise. `person=False` (an
  agent wrote it) marks every field but a format the executor checks, and drops every
  `"publishes": false`, so its input steps count as commit until a person vouches.
- `outputs` name the `save_as` of an extract or a read_json (the list) or its `count`, `total`,
  `truncated`: an endpoint returns declared fields only, and only values the labels cover.
- the `http` block the HTTP layer and health read (compiled into definition.connector["http"]).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agentos.core.models import LANE_ID_RE, Effect
from agentos.runner.connectors.compile import CompiledConnector, ConnectorInvalid, compile_connector
from agentos.runner.connectors.lint import Issue, site_hosts
from agentos.runner.connectors.models import (
    CONNECTOR_KEY_RE,
    DOMAIN_RE,
    OPERATION_RE,
    RESERVED_KEYS,
    SEMVER_RE,
    WEB_SITE,
    Commit,
    HttpExpect,
    Pace,
)
from agentos.runner.connectors.steps import deep, is_input, walk
from agentos.runner.flows import FlowService, validation_message
from agentos.runner.models import NodeInstall
from agentos.runner.util import host_in

DEFAULT_CACHE_S = 300
DEFAULT_PACE = {"per_hour": 60}
OUTPUT_FIELDS = ("count", "total", "truncated")  # of an extract's saved value, besides its list
MAX_TEXT_CHARS = 2000  # what a field without its own max_chars may hold
CHECKED_FORMATS = ("date-time", "integer", "number")  # and a uri with a host (_checked)
CHECKED_TYPES = ("integer", "number", "boolean")  # of a read_json field: the executor refuses any other value
READS = ("extract", "read_json")  # the steps whose declared fields an endpoint returns
CONTROLS = ("on", "wait", "max_age", "stale_ok")  # /v1/sites query names: on a GET they always mean this


class State:
    DRAFT, PROPOSED, ACTIVE, PAUSED = "draft", "proposed", "active", "paused"
    ALL = (DRAFT, PROPOSED, ACTIVE, PAUSED)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Endpoint(_Strict):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    effect: str = "read"
    input: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    output: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    outputs: dict[str, str] = Field(default_factory=dict)
    cache_s: int | None = Field(default=None, ge=0, le=86_400)
    expect: HttpExpect = Field(default_factory=HttpExpect)
    pace: Pace | None = None
    commit: Commit | None = None
    discard: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    not_to_people: bool | None = None

    @field_validator("effect")
    @classmethod
    def _effect(cls, v: str) -> str:
        if v in (Effect.PAY, Effect.IRREVERSIBLE):
            raise ValueError(f"{v} is not an endpoint: a site API reads, drafts or commits, and a payment or an "
                             "irreversible step stays with a person")
        if v not in (Effect.READ, Effect.DRAFT, Effect.COMMIT):
            raise ValueError("effect is read, draft or commit")
        return v

    @model_validator(mode="after")
    def _check(self) -> Endpoint:
        if self.effect != Effect.READ and self.cache_s:
            raise ValueError(f"only a read is cached: a {self.effect} endpoint has cache_s 0")
        taken = sorted(set(self.input.get("properties") or {}) & set(CONTROLS))
        if taken:
            raise ValueError(f"{', '.join(taken)} cannot be an input param: /v1/sites uses that name. Rename it")
        unknown = sorted(set(self.expect.min_items) - set(self.outputs))
        if unknown:
            raise ValueError(f"expect.min_items names {', '.join(unknown)}, which is not in outputs")
        return self

    @property
    def http_cache_s(self) -> int:
        if self.effect != Effect.READ:
            return 0
        return DEFAULT_CACHE_S if self.cache_s is None else self.cache_s


class SiteApi(_Strict):
    api: str
    title: str | None = Field(default=None, max_length=120)
    domain: str
    version: str = "1.0.0"
    surfaces: list[str] = Field(default_factory=list, max_length=20)
    endpoints: dict[str, Endpoint] = Field(min_length=1, max_length=50)

    @field_validator("api")
    @classmethod
    def _api(cls, v: str) -> str:
        if not CONNECTOR_KEY_RE.match(v):
            raise ValueError("api must be a lowercase key like 'harbor-supplier' (letters, digits, hyphens)")
        if v in RESERVED_KEYS:
            raise ValueError(f"{v!r} is reserved: its tools would clash with the node's own")
        return v

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        if not DOMAIN_RE.match(v):
            raise ValueError("domain must be a lowercase host name like portal.northsupply.example")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not SEMVER_RE.match(v):
            raise ValueError("version must be semver MAJOR.MINOR.PATCH, like 1.0.0")
        return v

    @field_validator("surfaces")
    @classmethod
    def _surfaces(cls, v: list[str]) -> list[str]:
        for surface in v:
            if not LANE_ID_RE.match(surface):
                raise ValueError(f"surface {surface!r} must look like 'harbor/supplier'")
        if len(set(v)) != len(v):
            raise ValueError("surfaces lists a surface twice")
        return v

    @model_validator(mode="after")
    def _names(self) -> SiteApi:
        for name in self.endpoints:
            if not OPERATION_RE.match(name):
                raise ValueError(f"endpoint name {name!r} must be lowercase_with_underscores")
        return self

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def parse(definition: Mapping[str, Any] | SiteApi) -> SiteApi:
    if isinstance(definition, SiteApi):
        return definition
    if not isinstance(definition, Mapping):
        raise ConnectorInvalid("a site API definition is a JSON object")
    try:
        return SiteApi.model_validate(dict(definition))
    except ValidationError as e:
        raise ConnectorInvalid(f"invalid site API: {validation_message(e, limit=10)}",
                               [Issue("error", "document", validation_message(e, limit=50))]) from e


def site_of(domain: str, sites: Mapping[str, tuple[str, ...]] | None = None) -> str | None:
    """The agentos.node.sites key the domain belongs to, or None."""
    return next((key for key, hosts in site_hosts(sites).items() if any(host_in(domain, h) for h in hosts)), None)


# ---------------------------------------------------------------- what a field is


def _checked(field: Mapping[str, Any]) -> bool:
    """The executor holds this value to a shape no prose fits, whatever a pattern says."""
    return (field.get("type") in CHECKED_TYPES or field.get("format") in CHECKED_FORMATS
            or (field.get("format") == "uri" and bool(field.get("host"))))


def labelled(field: Mapping[str, Any], *, person: bool) -> dict[str, Any]:
    """An extract or read_json field with its labels settled. In what a person wrote, anything
    free-form a page wrote is untrusted: a text read, a JSON string, or an attribute with no format or
    pattern to hold it, and only they unmark one. In what an agent wrote everything is, but a checked
    format or type, and what it says of itself counts for nothing: a pattern can be `.*`."""
    out = dict(field)
    text = out.get("read") is not None or out.get("type") == "string"
    if person:
        free = out.get("read") is not None or not (out.get("format") or out.get("pattern") or out.get("type") in CHECKED_TYPES)
        if free and out.get("untrusted") is None:
            out["untrusted"] = True
    elif not _checked(out):
        out["untrusted"] = True
    if text:
        out.setdefault("max_chars", MAX_TEXT_CHARS)
    return {k: v for k, v in out.items() if v is not None}


def settled(steps: list[dict[str, Any]], *, person: bool) -> list[dict[str, Any]]:
    """The steps as they count for who wrote them: labels settled, and an agent's `publishes: false`
    dropped, so its input steps count as commit until a person vouches."""
    out = deep(steps)
    for _, step in walk(out):
        if step.get("type") in READS:
            step["fields"] = {k: labelled(v, person=person) for k, v in (step.get("fields") or {}).items()}
        if not person and is_input(step):
            step.pop("publishes", None)
    return out


def steps_effect(steps: list[dict[str, Any]], *, person: bool) -> str:
    """What the steps can do: an agent's input step is commit whatever it says."""
    worst = Effect.READ
    for _, step in walk(steps):
        if is_input(step):
            eff = Effect.DRAFT if person and step.get("publishes") is False else Effect.COMMIT
            worst = eff if (eff.rank or 0) > (worst.rank or 0) else worst
    return str(worst)


def shown_effect(ep: Endpoint, *, person: bool) -> str:
    """The effect to show and to enforce: never below the steps, whatever the definition declares."""
    steps = steps_effect(ep.steps, person=person)
    return steps if (Effect(steps).rank or 0) > (Effect(ep.effect).rank or 0) else ep.effect


def extracts(steps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["save_as"]: s for _, s in walk(steps) if s.get("type") in READS and s.get("save_as")}


# ---------------------------------------------------------------- to a connector


def _mapping(name: str, ep: Endpoint, found: dict[str, dict[str, Any]]) -> dict[str, str]:
    for save_as, step in found.items():
        for fname, f in step["fields"].items():
            if (f.get("max_chars") or 0) > MAX_TEXT_CHARS:
                raise ConnectorInvalid(f"endpoints.{name}.{save_as}.{fname}: at most {MAX_TEXT_CHARS} characters: an endpoint "
                                       "returns declared fields, never the page's text")
    out: dict[str, str] = {}
    for output, path in ep.outputs.items():
        root, _, rest = path.partition(".")
        if root not in found or (rest and rest not in OUTPUT_FIELDS):
            raise ConnectorInvalid(f"endpoints.{name}.outputs.{output}: {path!r} must name what an extract or read_json step saved "
                                   f"({', '.join(sorted(found)) or 'none here'}) or its {', '.join(OUTPUT_FIELDS)}")
        out[output] = path if rest else f"{root}.items"
    return out


def _field_schema(field: Mapping[str, Any], domain: str) -> dict[str, Any]:
    fmt = field.get("format")
    if field.get("type") in CHECKED_TYPES:
        return {"type": [field["type"], "null"]}
    if fmt == "integer":
        return {"type": ["integer", "null"]}
    if fmt == "number":
        return {"type": ["number", "null"]}
    out: dict[str, Any] = {"type": ["string", "null"]}
    if fmt:
        out["format"] = fmt
    if fmt == "uri":
        out["x-agentos-host"] = field.get("host") or domain
    else:
        out["maxLength"] = field.get("max_chars") or MAX_TEXT_CHARS
    return out


def _item_schema(step: Mapping[str, Any], given: Mapping[str, Any] | None, domain: str) -> dict[str, Any]:
    """The item schema of a list output: the one given, with what the extract guarantees filled in."""
    given = dict(given or {})
    props = dict(given.get("properties") or {})
    for name, field in (step.get("fields") or {}).items():
        base = _field_schema(field, domain)
        have = props.get(name)
        props[name] = {**base, **have} if isinstance(have, dict) else base
    return {**given, "type": "object", "properties": props, "additionalProperties": False}


def _output_schema(ep: Endpoint, mapping: dict[str, str], found: dict[str, dict[str, Any]],
                   domain: str) -> dict[str, Any]:
    schema = deep(ep.output)
    props = schema.setdefault("properties", {})
    for output, path in mapping.items():
        root, _, rest = path.partition(".")
        given = props.get(output) if isinstance(props.get(output), dict) else {}
        if rest == "items":
            props[output] = {**given, "type": "array",
                             "items": _item_schema(found[root], given.get("items"), domain)}
        else:
            props[output] = {**given, "type": "boolean" if rest == "truncated" else "integer"}
    schema["required"] = sorted({*schema.get("required", []), *mapping})
    return {**schema, "type": "object", "additionalProperties": False}


def _labels(mapping: dict[str, str], found: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """untrusted and private output paths ('orders[].number'), as the executor reports them."""
    untrusted: list[str] = []
    private: list[str] = []
    for output, path in sorted(mapping.items()):
        root, _, rest = path.partition(".")
        if rest != "items":
            continue
        for name, field in sorted(found[root]["fields"].items()):
            if field.get("untrusted"):
                untrusted.append(f"{output}[].{name}")
            if field.get("private"):
                private.append(f"{output}[].{name}")
    return untrusted, private


def _with_identity(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An assert_identity right after the first navigate, unless the steps have one: a site agentos
    knows keeps its wrong account check."""
    if any(s.get("type") == "assert_identity" for _, s in walk(steps)):
        return steps
    at = next((i + 1 for i, s in enumerate(steps) if s.get("type") == "navigate"), 0)
    return [*steps[:at], {"type": "assert_identity"}, *steps[at:]]


def _operation(name: str, ep: Endpoint, api: SiteApi, kind: str, person: bool) -> dict[str, Any]:
    steps = settled(ep.steps, person=person)
    found = extracts(steps)
    mapping = _mapping(name, ep, found)
    if kind == "account":
        steps = _with_identity(steps)
    untrusted, private = _labels(mapping, found)
    op: dict[str, Any] = {
        "title": ep.title, "description": ep.description, "effect": ep.effect,
        "input": {**deep(ep.input), "type": "object", "additionalProperties": False},
        "output": _output_schema(ep, mapping, found, api.domain),
        "acting_as": {"kind": kind}, "steps": steps,
        "pace": ep.pace.model_dump(exclude_none=True) if ep.pace else dict(DEFAULT_PACE),
        "http": {"cache_s": ep.http_cache_s, "expect": ep.expect.model_dump(),
                 "untrusted_fields": untrusted, "private_fields": private},
    }
    if mapping:
        op["readback"] = {"live": {"outputs": mapping}}
    if ep.commit is not None:
        op["commit"] = ep.commit.model_dump(exclude_none=True)
    if ep.discard:
        op["discard"] = deep(ep.discard)
    if ep.not_to_people is not None:
        op["not_to_people"] = ep.not_to_people
    return op


def to_connector(definition: Mapping[str, Any] | SiteApi, *, person: bool,
                 sites: Mapping[str, tuple[str, ...]] | None = None,
                 languages: list[str] | None = None) -> dict[str, Any]:
    """The Connector document of a definition. `person`: a person wrote or vouched for it, so its
    `publishes: false` marks and its unmarked fields count."""
    api = parse(definition)
    known = site_of(api.domain, sites)
    kind = "account" if known else "lane"
    return {
        "connector": api.api, "version": api.version, "site": known or WEB_SITE, "domain": api.domain,
        **({"title": api.title} if api.title else {}), "surfaces": list(api.surfaces),
        "account_languages": languages or ["en"],
        "operations": {name: _operation(name, ep, api, kind, person) for name, ep in api.endpoints.items()},
    }


def compile_api(definition: Mapping[str, Any] | SiteApi, *, person: bool,
                sites: Mapping[str, tuple[str, ...]] | None = None,
                languages: list[str] | None = None) -> CompiledConnector:
    """Compile through compile_connector: the same lint and the same hashes as any connector."""
    return compile_connector(to_connector(definition, person=person, sites=sites, languages=languages), sites=sites,
                             local=True)


def local_saved_by(api: str, owner: Mapping[str, Any]) -> NodeInstall:
    """How SiteApiService saves a compiled flow, `owner` being the person who activates it."""
    return NodeInstall({"kind": "local", "id": api, "installed_by": owner.get("id"), "owner": owner.get("owner", "owner")})


# ---------------------------------------------------------------- for tests

SAMPLE: dict[str, Any] = {
    "api": "harbor-supplier", "title": "Harbor supplier portal", "domain": "portal.northsupply.example",
    "version": "1.0.0", "surfaces": ["harbor/supplier"],
    "endpoints": {"open_orders": {
        "title": "Open orders", "description": "Orders that have not shipped yet.", "effect": "read",
        "input": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["open", "late"], "default": "open"}}},
        "output": {"type": "object", "required": ["orders"], "properties": {
            "orders": {"type": "array", "items": {"type": "object", "properties": {
                "number": {"type": "string"}, "total": {"type": "string"},
                "url": {"type": "string", "format": "uri", "x-agentos-host": "portal.northsupply.example"}}}}}},
        "steps": [
            {"type": "navigate", "url": "https://portal.northsupply.example/orders?status=${status}"},
            {"type": "extract", "save_as": "orders", "max_items": 100,
             "items": {"role": "row", "within": {"role": "table", "name": "Orders"}},
             "fields": {"number": {"locator": {"role": "cell", "nth": 0}, "read": "text", "max_chars": 40},
                        "total": {"locator": {"role": "cell", "nth": 3}, "read": "text", "max_chars": 40},
                        "url": {"locator": {"role": "link"}, "attr": "href", "format": "uri",
                                "host": "portal.northsupply.example"}}}],
        "outputs": {"orders": "orders"}, "cache_s": 300, "expect": {"min_items": {"orders": 1}},
        "pace": {"per_hour": 60}}},
}


def compile_for_test(definition: Mapping[str, Any] | None = None, *, person: bool = True,
                     sites: Mapping[str, tuple[str, ...]] | None = None) -> CompiledConnector:
    """A definition (default SAMPLE) compiled as a site API, for tests of the layers above."""
    return compile_api(definition or SAMPLE, person=person, sites=sites if sites is not None else {})


def install_for_test(flows: FlowService, definition: Mapping[str, Any] | None = None, *,
                     owner: str = "owner", sites: Mapping[str, tuple[str, ...]] | None = None) -> list[Any]:
    """Install a local API straight into a FlowService, as an activation would, and return its
    FlowVersions. Bind a surface with `web_surface`."""
    compiled = compile_for_test(definition, sites=sites)
    by = local_saved_by(compiled.key, {"id": owner, "owner": owner})
    out = []
    for op in compiled.operations.values():
        assert op.flow is not None
        out.append(flows.save(op.flow, by=by)[0])
    return out


def web_surface(surface: str = "harbor/supplier", lane: str | None = None) -> dict[str, Any]:
    """The surface document of a `web` site API: SurfaceService.put(web_surface(), by=owner)."""
    return {"id": surface, "site": WEB_SITE, "lane": lane or surface, "acts_as": {"kind": "lane"}}
