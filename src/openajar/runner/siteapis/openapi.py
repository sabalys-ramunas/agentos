"""The OpenAPI 3.1 document of a site API, and the query params it promises (docs/SITE-APIS.md, OpenAPI).

Generated per key: only the endpoints and surfaces that key can use, above-ceiling endpoints left out.
`input` and `output` go in as they are, less the `x-agentos-*` marks. The document holds no site data,
no key and no sample value: examples and defaults are dropped from every output schema, and examples
come from the input schema only.

`operationId` is `<api>_<endpoint>` on the main operation: GET for a read, POST otherwise. A read's
POST is `<api>_<endpoint>_post`, because an operationId must be unique in a document.

The control names (`on`, `wait`, `max_age`, `stale_ok`) are never params: saving a definition refuses
an input param with one of those names, so a query name never has two meanings.

`coerce_query` is the other half of `query_param`: query strings are text, so the input schema says
what each one is. Arrays repeat the name (`?a=1&a=2`), objects are JSON.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from openajar.core.errors import InvalidRequest
from openajar.core.models import Effect
from openajar.runner.connectors.models import BINDING_PARAMS
from openajar.runner.flowdoc import FlowVersion
from openajar.runner.siteapis.cache import http_of

MAP_KEYWORDS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
ONE_KEYWORDS = frozenset({"items", "additionalProperties", "not", "if", "then", "else", "contains", "propertyNames",
                          "unevaluatedItems", "unevaluatedProperties"})
LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
SAMPLE_KEYWORDS = frozenset({"example", "examples", "default"})
INTEGER_RE = re.compile(r"[+-]?[0-9]+")
TRUE, FALSE = ("true", "1"), ("false", "0")
ERROR_STATUSES = ("400", "401", "403", "404", "409", "422", "429", "502", "503")


class BadParam(InvalidRequest):
    """One query or body param does not fit. `.name` is the param, and the message holds names only."""

    def __init__(self, name: str, why: str):
        super().__init__(f"{name}: {why}")
        self.name = name


def input_schema(fv: FlowVersion) -> dict[str, Any]:
    """The operation's input as a caller sees it: without the params the binding fills in."""
    schema = copy.deepcopy(fv.definition.params or {})
    props = {k: v for k, v in (schema.get("properties") or {}).items() if k not in BINDING_PARAMS}
    required = [r for r in schema.get("required") or [] if r not in BINDING_PARAMS]
    return {**schema, "type": "object", "properties": props, "required": required}


def output_schema(fv: FlowVersion) -> dict[str, Any]:
    return (fv.definition.connector or {}).get("output") or {"type": "object"}


def clean(schema: Any, *, samples: bool) -> Any:
    """A JSON schema without `x-agentos-*` marks (and without sample values unless `samples`), walking
    keywords only: a property may well be called `default`."""
    if not isinstance(schema, Mapping):
        return copy.deepcopy(schema)
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k.startswith("x-agentos-") or (not samples and k in SAMPLE_KEYWORDS):
            continue
        if k in MAP_KEYWORDS and isinstance(v, Mapping):
            out[k] = {name: clean(sub, samples=samples) for name, sub in v.items()}
        elif k in ONE_KEYWORDS:
            out[k] = clean(v, samples=samples)
        elif k in LIST_KEYWORDS and isinstance(v, list):
            out[k] = [clean(sub, samples=samples) for sub in v]
        else:
            out[k] = copy.deepcopy(v)
    return out


def _type(schema: Mapping[str, Any]) -> str | None:
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    return t if isinstance(t, str) else None


def query_param(name: str, prop: dict[str, Any], *, required: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "in": "query", "required": required}
    if prop.get("description"):
        out["description"] = prop["description"]
    kind = _type(prop)
    if kind == "object":
        return out | {"content": {"application/json": {"schema": prop}}}
    if kind == "array":
        out |= {"style": "form", "explode": True}
    return out | {"schema": prop}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/parameters/{name}"}


def _responses(fv: FlowVersion, *, get: bool) -> dict[str, Any]:
    answer = {"allOf": [{"$ref": "#/components/schemas/Answer"},
                        {"type": "object", "properties": {"data": clean(output_schema(fv), samples=False)}}]}
    out: dict[str, Any] = {"200": {"description": "The result.", "headers": _ok_headers(),
                                   "content": {"application/json": {"schema": answer}}},
                           "202": {"$ref": "#/components/responses/Pending"}}
    if get:
        out["304"] = {"description": "Not changed since the ETag in If-None-Match."}
    return out | {s: {"$ref": "#/components/responses/Problem"} for s in ERROR_STATUSES}


def _ok_headers() -> dict[str, Any]:
    names = ("Age", "ETag", "Agentos-Run", "RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset")
    return {n: {"$ref": f"#/components/headers/{n}"} for n in names}


def _operation(api: str, name: str, fv: FlowVersion, *, method: str) -> dict[str, Any]:
    d = fv.definition
    get = method == "get"
    schema = clean(input_schema(fv), samples=True)
    controls = [_ref("On"), _ref("Wait")]
    if fv.effect == Effect.READ:
        controls += [_ref("MaxAge"), _ref("StaleOk")]
    op: dict[str, Any] = {
        "operationId": f"{api}_{name}" if get or fv.effect != Effect.READ else f"{api}_{name}_post",
        "summary": d.title or name, "tags": [api],
        "x-agentos-effect": str(fv.effect), "x-agentos-untrusted-fields": list(http_of(fv).get("untrusted_fields") or []),
        "x-agentos-cache-s": int(http_of(fv).get("cache_s") or 0),
    }
    lead = (d.description or "").split(" Connector operation ")[0].strip()
    if lead:
        op["description"] = lead
    if get:
        required = set(schema.get("required") or [])
        op["parameters"] = [*controls, *(query_param(k, v, required=k in required and "default" not in v)
                                         for k, v in sorted(schema["properties"].items()))]
    else:
        idem = {"name": "Idempotency-Key", "in": "header", "required": fv.effect == Effect.COMMIT,
                "description": "A repeat with the same key returns the first run.",
                "schema": {"type": "string", "minLength": 1, "maxLength": 128}}
        op["parameters"] = controls if fv.effect == Effect.READ else [*controls, idem]
        op["requestBody"] = {"required": bool(schema.get("required")),
                             "content": {"application/json": {"schema": schema}}}
    op["responses"] = _responses(fv, get=get)
    return op


def _components(surfaces: list[str]) -> dict[str, Any]:
    on: dict[str, Any] = {"type": "string"} | ({"enum": surfaces} if surfaces else {})
    number = {"type": "integer", "minimum": 0}
    problem = {"type": "object", "required": ["type", "title", "status", "code", "detail"], "properties": {
        "type": {"type": "string", "format": "uri-reference"}, "title": {"type": "string"},
        "status": {"type": "integer"}, "code": {"type": "string"}, "detail": {"type": "string"},
        "retry_at": {"type": "string", "format": "date-time"}, "run": {"type": "string"}}}
    pending = {"type": "object", "required": ["code", "run", "why"], "properties": {
        "code": {"type": "string", "enum": ["pending", "approval_pending"]}, "run": {"type": "string"},
        "why": {"type": "string"}}}
    answer = {"type": "object", "required": ["data", "status", "stale", "cache", "source"], "properties": {
        "data": {}, "status": {"type": "string"},
        "untrusted_fields": {"type": "array", "items": {"type": "string"}},
        "private_fields": {"type": "array", "items": {"type": "string"}},
        "truncated": {"type": "boolean"}, "stale": {"type": "boolean"},
        "cache": {"type": "object", "properties": {"hit": {"type": "boolean"}, "age_s": number}},
        "source": {"type": "object", "properties": {k: {"type": "string"} for k in (
            "api", "endpoint", "version", "surface", "read_at", "run")}}}}
    return {
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "aos_ key"}},
        "schemas": {"Problem": problem, "Pending": pending, "Answer": answer},
        "parameters": {
            "On": {"name": "on", "in": "query", "description": "The surface. Optional when the key sees one.",
                   "schema": on},
            "Wait": {"name": "wait", "in": "query", "description": "Seconds to hold the call open for a result.",
                     "schema": {"type": "number", "minimum": 0, "maximum": 60, "default": 25}},
            "MaxAge": {"name": "max_age", "in": "query", "description": "Lowers the cache time. 0 forces a fresh read.",
                       "schema": number},
            "StaleOk": {"name": "stale_ok", "in": "query", "description": "1: on needs_human, pace_limited or "
                        "rate_limited, return the last good result with stale true.",
                        "schema": {"type": "integer", "enum": [0, 1]}}},
        "headers": {
            "Age": {"schema": number}, "ETag": {"schema": {"type": "string"}}, "Agentos-Run": {"schema": {"type": "string"}},
            "RateLimit-Limit": {"schema": number}, "RateLimit-Remaining": {"schema": number},
            "RateLimit-Reset": {"schema": number}},
        "responses": {
            "Problem": {"description": "An error.", "content": {"application/problem+json": {
                "schema": {"$ref": "#/components/schemas/Problem"}}}},
            "Pending": {"description": "Still running, or a person must approve: poll the run.",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pending"}}}}},
    }


def document(*, api: str, title: str | None, version: str, endpoints: Mapping[str, FlowVersion],
             surfaces: Iterable[str], server: str | None = None) -> dict[str, Any]:
    """The document for these endpoints."""
    paths: dict[str, Any] = {}
    for name, fv in sorted(endpoints.items()):
        item: dict[str, Any] = {}
        if fv.effect == Effect.READ:
            item["get"] = _operation(api, name, fv, method="get")
        item["post"] = _operation(api, name, fv, method="post")
        paths[f"/v1/sites/{api}/{name}"] = item
    paths[f"/v1/sites/{api}/runs/{{run}}"] = {"get": {
        "operationId": f"{api}_run", "summary": "A call that answered 202", "tags": [api],
        "parameters": [{"name": "run", "in": "path", "required": True, "schema": {"type": "string"}}],
        "responses": {"200": {"description": "The result.", "headers": _ok_headers(), "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Answer"}}}},
                      "202": {"$ref": "#/components/responses/Pending"},
                      **{s: {"$ref": "#/components/responses/Problem"} for s in ERROR_STATUSES}}}}
    doc: dict[str, Any] = {"openapi": "3.1.0", "info": {"title": title or api, "version": version},
                           "security": [{"bearer": []}], "paths": paths, "components": _components(sorted(surfaces))}
    return doc | ({"servers": [{"url": server}]} if server else {})


# ---------------------------------------------------------------- params in a query


def coerce_query(schema: Mapping[str, Any], pairs: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Query text as the values the input schema names. Raises BadParam. A name the schema does not have
    is refused, so nobody spends a run on a cache key of made-up params."""
    props = schema.get("properties") or {}
    grouped: dict[str, list[str]] = {}
    for name, value in pairs:
        grouped.setdefault(name, []).append(value)
    out: dict[str, Any] = {}
    for name, values in grouped.items():
        prop = props.get(name)
        if not isinstance(prop, Mapping):
            raise BadParam(name, "this endpoint has no such param")
        kind = _type(prop)
        if kind == "array":
            items = prop.get("items")
            item_kind = _type(items) if isinstance(items, Mapping) else None
            out[name] = [_scalar(name, item_kind, v) for v in values]
        elif len(values) > 1:
            raise BadParam(name, "given more than once")
        else:
            out[name] = _scalar(name, kind, values[0])
    return out


def _scalar(name: str, kind: str | None, raw: str) -> Any:
    if kind == "integer":
        if not INTEGER_RE.fullmatch(raw):
            raise BadParam(name, "must be a whole number")
        return int(raw)
    if kind == "number":
        try:
            value = float(raw)
        except ValueError:
            raise BadParam(name, "must be a number") from None
        if not math.isfinite(value):
            raise BadParam(name, "must be a number")
        return value
    if kind == "boolean":
        if raw.lower() in TRUE or raw.lower() in FALSE:
            return raw.lower() in TRUE
        raise BadParam(name, "must be true or false")
    if kind == "object":
        try:
            value = json.loads(raw)
        except ValueError:
            raise BadParam(name, "must be a JSON object") from None
        if not isinstance(value, dict):
            raise BadParam(name, "must be a JSON object")
        return value
    return raw
