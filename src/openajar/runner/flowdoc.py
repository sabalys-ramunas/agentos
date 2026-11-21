"""The flow document (FlowDefinition) and its saved, immutable version (FlowVersion).

A FlowVersion's version is the hash of its content.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from openajar.core.locator import Condition, StrictModel
from openajar.core.models import LANE_ID_RE, Effect, utcnow
from openajar.core.template import template_roots
from openajar.runner.names import (
    CONNECTOR_PREFIX,
    CONNECTOR_RESERVED,
    FLOW_ID_RE,
    LANE_PATTERN_RE,
    PARAM_NAME_RE,
    RESERVED_PARAMS,
    SCOPE_ROOTS,
    is_connector_id,
    is_lane_pattern,
)
from openajar.runner.owner import is_owner
from openajar.runner.steps import (
    ACTION_TYPES,
    STEPS_ADAPTER,
    BranchStep,
    ExtractStep,
    ForEachStep,
    PreflightStep,
    ReadbackStep,
    Step,
    iter_steps,
    steps_effect,
    watchers,
)


class ProducedField(StrictModel):
    """One value an operation gives the workflow steps after it (docs/WORKFLOWS.md)."""
    key: str = Field(pattern=r"^[\w-]{1,60}$")
    label: str | None = Field(default=None, max_length=120)
    type: Literal["text", "number", "date", "list", "yes_no"] = "text"
    # False counts only in a version the human owner saved: what a page said is untrusted by default
    untrusted: bool = True


class SendsSpec(StrictModel):
    """Which params of an operation that sends to people hold the address and the message."""
    to: str = "to"
    body: str = "body"


class OperationBlock(StrictModel):
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


class FlowDefinition(StrictModel):
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


    def iter_steps(self) -> Any:
        """(path, step) for every step of the flow, depth first (iter_steps)."""
        return iter_steps(self.steps)
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
            steps = STEPS_ADAPTER.validate_python(raw)
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
