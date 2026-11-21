"""Compile a connector into runner flows (docs/design/connectors.md 4.4).

Each operation becomes one FlowDefinition:

    id        connector.<site>.<operation>
    lane      */*            the binding picks the real lane (Brief C)
    domain    the connector's domain
    effect    the operation's declared effect (checked against the steps)
    params    the input schema plus the hidden binding params acts_as_id and account_language
    steps     the operation's steps, assert_identity filled in from acting_as, and a preflight
              step inserted right before the commit step
    connector the operation's contract and what the runner needs beyond the steps (outputs,
              readback, commit, pace, signals, rehearsal, discard); part of the version hash
    expose_as_tool false     connector tools are <site>_<op> (Brief C), never flow_<id>

Two hashes per operation:

    contract        sha256 over operation name, effect, input and output schemas (without
                    descriptions and titles), acting-as kind, commit request shape and readback
                    kinds. A locator-only repair keeps it, so standing approvals survive, and
                    so does marking the operation verified after a canary.
    implementation  over everything: equals the compiled FlowVersion.version.
    proof           over everything a run does (proof_hash): the compiled flow without its
                    title, description, version and verified mark. A canary's mark names it, so
                    any edit to steps, locators, readbacks, preflights, the commit request or
                    pace makes the mark stale; a version bump or a wording fix does not.

A step that needs a runner capability this runner lacks (`supported`, default: what its
models accept) is emitted with PENDING_MARKER listing what it needs. Such an operation
compiles (validated through a shadow flow, with the same step paths) but cannot be installed,
and its implementation hash is provisional. With every capability present the compiled flow,
its discard steps included (FlowDefinition.discard_steps), is validated as the runner will run it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from openajar.core.errors import InvalidRequest
from openajar.core.models import Effect
from openajar.runner.connectors.lint import Issue, lint_connector, template_problems
from openajar.runner.connectors.models import BINDING_PARAMS, Connector, Operation
from openajar.runner.connectors.steps import (
    deep,
    mark_pending,
    runner_capabilities,
    shadow_steps,
    walk,
)
from openajar.runner.flowdoc import FlowDefinition
from openajar.runner.flows import validation_message
from openajar.runner.names import CONNECTOR_PREFIX

CONNECTOR_FILE = "connector.json"
_SCHEMA_PROSE = frozenset({"description", "title", "examples", "$comment"})
_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_ONE = ("items", "additionalProperties", "not", "if", "then", "else", "contains", "propertyNames")


class ConnectorInvalid(InvalidRequest):
    code = "connector_invalid"

    def __init__(self, message: str, issues: list[Issue] | None = None):
        super().__init__(message)
        self.issues = issues or []


def _hash(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def semantic_schema(schema: Any) -> Any:
    """A JSON schema without its prose (description, title, examples, $comment), so a
    wording fix does not change the contract. Property names are kept."""
    if isinstance(schema, list):
        return [semantic_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _SCHEMA_PROSE:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {k: semantic_schema(v) for k, v in value.items()}
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            out[key] = [semantic_schema(v) for v in value]
        elif key in _SCHEMA_ONE:
            out[key] = semantic_schema(value)
        else:
            out[key] = value
    return out


def _reach(op: Operation) -> dict[str, Any]:
    """Whether it reaches people, as the operation declares it. What it declares is its contract."""
    out: dict[str, Any] = {}
    if op.sends is not None:
        out["sends"] = op.sends.model_dump(mode="json")
    if op.not_to_people:
        out["not_to_people"] = True
    return out


def contract_body(connector: str, name: str, op: Operation) -> dict[str, Any]:
    kinds = []
    if op.readback and op.readback.live:
        kinds.append("live")
    if op.readback and op.readback.public:
        kinds.append(f"public:{op.readback.public}")
    request = op.commit.request if op.commit else None
    reach = _reach(op)
    return {
        "connector": connector, "operation": name, "effect": str(op.effect),
        "input": semantic_schema(op.input), "output": semantic_schema(op.output),
        "acting_as": op.acting_as.kind,
        "commit_request": _request_shape(request),
        "readback": sorted(kinds),
        **({"reach": reach} if reach else {}),  # only when declared: one that says nothing keeps its hash
    }


def _request_shape(request: Any) -> dict[str, Any] | None:
    """The commit request in the contract; `op` and `match` only when set, so a request written
    before they existed keeps its contract hash (and the standing approvals bound to it)."""
    if request is None:
        return None
    out = request.model_dump(mode="json")
    for key in ("op", "match"):
        if out.get(key) is None:
            out.pop(key, None)
    return out


def contract_hash(connector: str, name: str, op: Operation) -> str:
    return "sha256:" + _hash(contract_body(connector, name, op))


# what proof_hash leaves out: words a person reads, the version a mark itself bumps, and the mark
_PROOF_SKIP = frozenset({"title", "description"})
_PROOF_SKIP_META = frozenset({"version", "verified"})
MARK_VERIFIED, MARK_STALE = "verified", "stale"


def proof_hash(definition: Mapping[str, Any]) -> str:
    """The hash a canary's verified mark names: the compiled operation (a FlowDefinition document,
    as installed: FlowDefinition.canonical()) without its title, description, version and verified
    text. Steps, locators, readbacks, preflights, the commit request, pace, signals and outputs are
    all in it, so an edit to any of them makes the mark stale."""
    body = {k: v for k, v in definition.items() if k not in _PROOF_SKIP}
    meta = body.get("connector")
    if isinstance(meta, Mapping):
        body["connector"] = {k: v for k, v in meta.items() if k not in _PROOF_SKIP_META}
    return "sha256:" + _hash(body)


def file_mark(op: Operation, proof: str) -> str:
    """What the connector file says of its own mark: verified (its hash is this proof), stale (a
    mark for another implementation, or one with no hash), or "" (none). Information only: the
    node honours its own ledger (openajar.runner.connectors.marks)."""
    if not op.verified:
        return ""
    return MARK_VERIFIED if op.verified_hash == proof else MARK_STALE


@dataclass
class CompiledOperation:
    connector: str
    operation: str
    version: str
    effect: Effect
    contract: str
    implementation: str
    definition: dict[str, Any]  # the FlowDefinition document, as the runner stores it
    pending: tuple[str, ...] = ()  # runner capabilities this runner lacks
    flow: FlowDefinition | None = None  # None while pending
    proof: str = ""  # proof_hash(definition): what a canary's mark names
    mark: str = ""  # the file's own mark: verified, stale or "" (file_mark)

    @property
    def flow_id(self) -> str:
        return f"{CONNECTOR_PREFIX}{self.connector}.{self.operation}"

    @property
    def dotted(self) -> str:
        return f"{self.connector}.{self.operation}"

    @property
    def tool_name(self) -> str:
        return f"{self.connector}_{self.operation}".replace("-", "_")

    @property
    def installable(self) -> bool:
        return self.flow is not None

    def index_entry(self) -> dict[str, Any]:
        return {"effect": str(self.effect), "contract": self.contract, "implementation": self.implementation,
                **({"pending": list(self.pending)} if self.pending else {})}


@dataclass
class CompiledConnector:
    connector: Connector
    operations: dict[str, CompiledOperation]
    issues: list[Issue] = field(default_factory=list)
    root: Path | None = None

    @property
    def key(self) -> str:
        return self.connector.connector

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def pending(self) -> list[str]:
        return sorted({p for op in self.operations.values() for p in op.pending})

    def index(self) -> dict[str, Any]:
        """The bundle index row: {connector, site, version, operations: {name: hashes}}."""
        c = self.connector
        return {"connector": c.connector, "site": c.site, "version": c.version,
                "operations": {name: op.index_entry() for name, op in sorted(self.operations.items())}}


# ---------------------------------------------------------------- compiling


def _identity(op: Operation) -> dict[str, Any]:
    a = op.acting_as
    out: dict[str, Any] = {"acting_as": a.kind, "source": a.source, "expect": "${acts_as_id}"}
    if a.pattern:
        out["pattern"] = a.pattern
    if a.locator:
        out["locator"] = deep(a.locator)
    return out


def _expand_identity(steps: list[Any], op: Operation) -> None:
    for _, step in walk(steps):
        if step.get("type") == "assert_identity":
            step.update(_identity(op))


def _preflight(op: Operation) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for check in op.commit.preflight if op.commit else []:
        if check.kind == "acting_as":
            checks.append({"kind": "acting_as", **_identity(op)})
        elif check.kind == "value_equals":
            checks.append({"kind": "value_equals", "locator": deep(check.locator), "param": check.param})
        elif check.kind == "text_differs":
            checks.append({"kind": "text_differs", "locator": deep(check.locator), "text": check.text,
                           **({"lines": True} if check.lines else {})})
        else:
            checks.append({"kind": "count", "locator": deep(check.locator), "equals": check.equals})
    return {"type": "preflight", "label": "preflight", "checks": checks}


def _params(c: Connector, op: Operation) -> dict[str, Any]:
    params = deep(op.input)
    props = params.setdefault("properties", {})
    props["acts_as_id"] = {"type": "string", "minLength": 1, "maxLength": 80, "show": True,
                           "x-agentos-binding": True, "description": "who the operation acts as, from the binding"}
    props["account_language"] = {"type": "string", "enum": list(c.account_languages), "show": True,
                                 "x-agentos-binding": True, "description": "the account's language, from the binding"}
    params["required"] = list(dict.fromkeys([*params.get("required", []), *BINDING_PARAMS]))
    return params


def _meta(c: Connector, name: str, op: Operation, contract: str, discard: list[Any]) -> dict[str, Any]:
    dump = op.model_dump(mode="json", by_alias=True, exclude_none=True)
    commit = dump.get("commit")
    if commit is not None:
        commit = {k: v for k, v in commit.items() if k != "preflight"}  # compiled into the preflight step
    signed_out = op.readback.signed_out_check() if op.readback else None
    return {
        **({"signed_out": signed_out} if signed_out else {}),  # AUD-22: what the runner checks signed out
        "connector": c.connector, "site": c.site, "operation": name, "version": c.version, "contract": contract,
        "effect": str(op.effect), "acting_as": {"kind": op.acting_as.kind}, "output": deep(op.output),
        "outputs": dict(op.readback.live.outputs) if op.readback and op.readback.live else {},
        "readback": dump.get("readback"), "commit": commit, "pace": dump.get("pace"),
        "signals": dump.get("signals"), "rehearsal": op.rehearsal_mode, "discard": discard,
        "account_languages": list(c.account_languages),
        # the file's canary mark, information only ("" when none or stale): the node honours its
        # own ledger (openajar.runner.connectors.marks), never this
        "verified": op.verified,
        **({"http": dump["http"]} if op.http is not None else {}),  # only a site API: a catalog connector keeps its hash
        **({"surfaces": list(c.surfaces)} if c.surfaces else {}),
    }


def compile_operation(c: Connector, name: str, op: Operation,
                      supported: frozenset[str] | None = None) -> tuple[CompiledOperation, list[str]]:
    """(the compiled operation, errors the runner found validating it)."""
    supported = runner_capabilities() if supported is None else supported
    contract = contract_hash(c.connector, name, op)
    steps = deep(op.steps)
    _expand_identity(steps, op)
    if op.commit is not None:
        at = next((i for i, s in enumerate(steps) if s.get("label") == op.commit.step), None)
        if at is not None:
            steps.insert(at, _preflight(op))
    discard = deep(op.discard)
    _expand_identity(discard, op)
    pending = mark_pending(steps, supported) | mark_pending(discard, supported)
    flow_id = f"{CONNECTOR_PREFIX}{c.connector}.{name}"
    definition: dict[str, Any] = {
        "id": flow_id, "title": op.title,
        "description": (f"{op.description} " if op.description else "")
        + f"Connector operation {c.connector}.{name} {c.version}.",
        "lane": "*/*", "domain": c.domain, "effect": str(op.effect), "params": _params(c, op),
        "steps": steps, "expose_as_tool": False, "tags": ["connector", f"connector:{c.connector}"],
        "connector": _meta(c, name, op, contract, discard),
    }
    reach = _reach(op)
    if reach:  # only when declared, so a connector that says nothing keeps its version
        definition["operation"] = reach
    errors: list[str] = []
    shadow = None
    for what, shadow_of in (("steps", steps), ("discard", discard)):
        if not shadow_of:
            continue
        try:
            sd = FlowDefinition.model_validate({**definition, "steps": shadow_steps(shadow_of)})
        except ValidationError as e:
            errors.append(f"operations.{name}.{what}: {validation_message(e)}")
            continue
        if what == "steps":
            shadow = sd
    if shadow is not None:
        eff = shadow.effective_effect(owner_saved=True)[0]
        if eff != op.effect:
            errors.append(f"operations.{name}.effect: the runner sees {eff}, declared {op.effect}")
    flow = None
    if not pending and not errors:
        try:
            flow = FlowDefinition.model_validate(definition)
        except ValidationError as e:
            errors.append(f"operations.{name}: {validation_message(e)}")
    if flow is not None:
        definition = flow.canonical()
    proof = proof_hash(definition)
    mark = file_mark(op, proof)
    if mark == MARK_STALE:  # a mark for another implementation is no mark (proof_hash skips it)
        definition["connector"]["verified"] = ""
        if flow is not None:
            flow = FlowDefinition.model_validate(definition)
            definition = flow.canonical()
    if flow is not None:
        implementation = flow.content_hash()
    else:  # provisional: the same hash over the document as compiled today
        implementation = "sha256:" + _hash(definition)[:16]
    out = CompiledOperation(connector=c.connector, operation=name, version=c.version, effect=op.effect,
                            contract=contract, implementation=implementation, definition=definition,
                            pending=tuple(sorted(pending)), flow=flow, proof=proof, mark=mark)
    return out, errors


def parse_connector(source: Mapping[str, Any] | Connector, *, local: bool = False,
                    learned: bool = False) -> Connector:
    """`learned`: a connector the node generated (ConnectorService.install_learned), keyed
    <site>-profile; only that caller sets it, never a document."""
    if isinstance(source, Connector):
        return source
    if not isinstance(source, Mapping):
        raise ConnectorInvalid("a connector is a JSON object")
    try:
        return Connector.model_validate(dict(source), context={"local": local, "learned": learned})
    except ValidationError as e:
        message = f"invalid connector: {validation_message(e, limit=10)}"
        raise ConnectorInvalid(message, [Issue("error", "document", validation_message(e, limit=50))]) from e


def compile_connector(source: Mapping[str, Any] | Connector, *, root: Path | None = None,
                      sites: Mapping[str, tuple[str, ...]] | None = None, commit_gate: bool = False,
                      supported: frozenset[str] | None = None, local: bool = False,
                      learned: bool = False) -> CompiledConnector:
    """Validate, lint strictly and compile every operation. Raises ConnectorInvalid with every
    error found; warnings are on the result. `local`: a site API from SiteApiService, the only
    caller that may set it; a connector document cannot say so itself. `learned`: likewise, a
    connector openajar.profiles.learn generated (keyed <site>-profile)."""
    c = parse_connector(source, local=local, learned=learned)
    issues = lint_connector(c, sites=sites, root=root, commit_gate=commit_gate, local=local)
    ops: dict[str, CompiledOperation] = {}
    for name, op in c.operations.items():
        for problem in template_problems(op):
            issues.append(Issue("error", f"operations.{name}", problem))
        compiled, errors = compile_operation(c, name, op, supported)
        issues += [Issue("error", e.split(":", 1)[0], e.split(":", 1)[1].strip()) for e in errors]
        ops[name] = compiled
    errors = [i for i in issues if i.level == "error"]
    if errors:
        shown = "; ".join(f"{i.where}: {i.message}" for i in errors[:8])
        more = f"; and {len(errors) - 8} more" if len(errors) > 8 else ""
        raise ConnectorInvalid(f"connector {c.connector} {c.version} does not compile: {shown}{more}", issues)
    return CompiledConnector(connector=c, operations=ops, issues=issues, root=root)


def load_connector(path: Path | str) -> tuple[dict[str, Any], Path]:
    """(the connector document, its directory) from connectors/<site>/ or a connector.json."""
    p = Path(path)
    file = p / CONNECTOR_FILE if p.is_dir() else p
    try:
        doc = json.loads(file.read_text())
    except FileNotFoundError as e:
        raise ConnectorInvalid(f"{file} not found (a connector directory holds {CONNECTOR_FILE})") from e
    except ValueError as e:
        raise ConnectorInvalid(f"{file} is not valid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise ConnectorInvalid(f"{file} must hold a JSON object")
    return doc, file.parent


def compile_path(path: Path | str, **kw: Any) -> CompiledConnector:
    doc, root = load_connector(path)
    return compile_connector(doc, root=root, **kw)
