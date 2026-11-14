"""Typed outputs from a run's saved values (docs/design/connectors.md 4.10).

`outputs_from_saved` maps readback `save_as` values to the operation's outputs and checks
them against the output schema. Brief B calls it to fill `Run.output` and fails the run with
`output_contract_violation` on any problem; today it is how tests read a connector's result.

A problem goes to errors, notifications and health, so it says which field and what kind of value came,
in the schema's own words. Nothing the site sent reaches it, not a value and not a key of the data.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from agentos.runner.connectors.lint import host_in

_MISSING = object()


def saved_values(store: Any, run_id: str) -> dict[str, Any]:
    """Every value a run saved with save_as: from the step journal, overlaid by the store's
    private step state (kept verbatim when the run has hidden params)."""
    out: dict[str, Any] = {}
    docs = [rec.result for rec in store.steps(run_id)] + list(store.step_states(run_id).values())
    for doc in docs:
        if isinstance(doc, dict) and isinstance(doc.get("vars"), dict):
            out.update(doc["vars"])
    return out


def _lookup(saved: Mapping[str, Any], path: str) -> Any:
    """The value at a dotted path; a number part indexes a list ('current.items.0.value')."""
    cur: Any = saved
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isascii() and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    return {bool: "a boolean", int: "a number", float: "a number", str: "a string", list: "a list"}.get(type(value), "an object")


def _where(err: ValidationError) -> str:
    """The field, from the path through the schema: a path through the data can hold a site's key."""
    parts = list(err.absolute_schema_path)[:-1]
    out: list[str] = []
    while parts:
        part = parts.pop(0)
        if part == "properties" and parts:
            out.append(str(parts.pop(0)))
        elif part == "items" and out:
            out[-1] += "[]"
    return ".".join(out) or "output"


def _schema_problem(err: ValidationError) -> str:
    """jsonschema's own message quotes the value, so it is never used."""
    where, rule = _where(err), err.validator
    if rule == "required" and isinstance(err.instance, dict):
        return f"{where}: {', '.join(k for k in err.validator_value if k not in err.instance)} is missing"
    if rule == "type":
        wanted = err.validator_value if isinstance(err.validator_value, list) else [err.validator_value]
        return f"{where}: is {_kind(err.instance)}, not {' or '.join(str(w) for w in wanted)}"
    return f"{where}: is {_kind(err.instance)} that breaks its {rule} rule"


def _host_problems(schema: dict[str, Any], value: Any, where: str) -> list[str]:
    out: list[str] = []
    host = schema.get("x-agentos-host")
    if host and isinstance(value, str):
        try:
            parts = urlsplit(value)
            ok = parts.scheme == "https" and bool(parts.hostname) and host_in(parts.hostname or "", str(host))
        except ValueError:
            ok = False
        if not ok:
            out.append(f"{where}: is not an https URL under {host}")
    if isinstance(value, dict):
        for k, prop in (schema.get("properties") or {}).items():
            if isinstance(prop, dict) and k in value:
                out += _host_problems(prop, value[k], f"{where}.{k}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            out += _host_problems(schema["items"], item, f"{where}[{i}]")
    return out


def outputs_from_saved(meta: Mapping[str, Any], saved: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """(data, problems) for a compiled operation's `connector` metadata and a run's saved
    values. Any problem is a contract violation (the site changed shape), never passed on."""
    data: dict[str, Any] = {}
    problems: list[str] = []
    for name, path in (meta.get("outputs") or {}).items():
        value = _lookup(saved, path)
        if value is _MISSING:
            problems.append(f"{name}: {path!r} was not saved by the run")
        else:
            data[name] = value
    schema = meta.get("output") or {"type": "object"}
    for err in sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: [str(p) for p in e.path]):
        problems.append(_schema_problem(err))
    problems += [p for k, v in data.items()
                 for p in _host_problems((schema.get("properties") or {}).get(k) or {}, v, k)]
    return data, problems
