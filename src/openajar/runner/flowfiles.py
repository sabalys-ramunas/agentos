"""Flows as files: the YAML a flow is kept as in git, and the offline checks on it.

A flow file is a FlowDefinition (openajar.runner.models) written as YAML, with the values that are
only defaults left out so it reads like the flow, not like the schema. It is the same document
flow_save takes: `openajar flows push` sends it, `openajar flows pull` writes what the node holds.
The layout:

    flows/
      <workspace>/<flow id>.yaml             a flow (its lane's workspace; `_any` for a lane pattern)
      <workspace>/<flow id>.test.yaml        its tests (openajar.runner.flowtest)
      <workspace>/<name>.recording.yaml      a recording its tests replay

Rendering is byte-stable: the same flow always gives the same bytes, and a file parses back to the
same content hash (the flow's version), so a pull right after a push changes nothing. Validation is
the node's own (flows.parse_definition), never a copy of it.

A secret never sits in a flow file. Flows do not take secrets (a login comes from the vault, through
the lane), so a pulled flow that holds what looks like one (a literal typed into a password field, a
default for a param named like a credential, or a value the node masked as a vault value) is
refused, not written.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from openajar.core.errors import InvalidRequest
from openajar.runner.flowdoc import FlowDefinition
from openajar.runner.flows import lint, parse_definition
from openajar.runner.names import is_lane_pattern
from openajar.runner.steps import IDEMPOTENT_DEFAULT, iter_steps
from openajar.runner.urlpattern import names_secret

SUFFIX = ".yaml"
TEST_SUFFIX = ".test.yaml"
RECORDING_SUFFIX = ".recording.yaml"
PATTERN_DIR = "_any"  # flows bound to a lane pattern ("*/instagram") reach more than one workspace
STEP_TYPES = frozenset(IDEMPOTENT_DEFAULT)
STEP_ORDER = ("type", "label", "url", "locator", "param", "text", "option", "key", "state", "save_as", "writes",
              "publishes", "condition", "readback")
ALWAYS = ("id", "lane", "effect")  # shown even when they hold the default, so a reader sees them first
MASKS = ("[redacted]", "[withheld")  # what the node's vault scrub leaves where a vault value was
_SECRET_FIELD = re.compile(r"\b(pass(word|code|phrase)?|secret|token|otp|one.?time|2fa|pin|cvc|cvv)\b", re.IGNORECASE)


# ---- rendering ------------------------------------------------------------------------


def readable(defn: FlowDefinition) -> dict[str, Any]:
    """The definition without the values that are only defaults. Parsing it gives the same flow
    (the same content hash); when it would not, the full canonical form is returned instead."""
    full = defn.canonical()
    mini = defn.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)
    mini = _keep_types(full, mini)
    out = {k: full[k] for k in full if k in ALWAYS or k in mini}
    out = {k: mini.get(k, full[k]) for k in out}
    try:
        same = parse_definition(out).content_hash() == defn.content_hash()
    except InvalidRequest:
        same = False
    return out if same else full


def _keep_types(full: Any, mini: Any) -> Any:
    """Put back each step's `type` (a default of its model, but the step's whole meaning), and put a
    step's keys in reading order: what it is, where, on what, with what, then how it is checked."""
    if isinstance(full, dict) and isinstance(mini, dict):
        out: dict[str, Any] = {}
        step = full.get("type") in STEP_TYPES
        if step and "type" not in mini:
            out["type"] = full["type"]
        for key, value in mini.items():
            out[key] = _keep_types(full.get(key), value)
        if step:
            rank = {key: i for i, key in enumerate(STEP_ORDER)}
            out = dict(sorted(out.items(), key=lambda kv: rank.get(kv[0], len(rank))))  # stable for the rest
        return out
    if isinstance(full, list) and isinstance(mini, list) and len(full) == len(mini):
        return [_keep_types(f, m) for f, m in zip(full, mini, strict=True)]
    return mini


class _Dumper(yaml.SafeDumper):
    """Block style, keys in the order given, and a text with line breaks as a `|` block."""


def _str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value and not value.endswith((" ", "\t")) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def dump_yaml(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False,
                     width=4096, indent=2)


def render(defn: FlowDefinition) -> str:
    """The flow file's text. The same flow always renders to the same bytes."""
    return dump_yaml(readable(defn))


# ---- reading --------------------------------------------------------------------------


class _Loader(yaml.SafeLoader):
    """safe_load that refuses a key given twice (YAML would keep the last one without a word)."""


def _mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"the key {key!r} is given twice",
                                                    key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_yaml(text: str, where: str) -> Any:
    try:
        return yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        line = f" (line {mark.line + 1})" if mark is not None else ""
        problem = getattr(e, "problem", None) or str(e).splitlines()[0]
        raise InvalidRequest(f"{where} is not valid YAML{line}: {problem}".strip()) from None


@dataclass
class FlowFile:
    """One flow file and what the offline checks found in it."""

    path: Path
    definition: FlowDefinition | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.definition is not None and not self.errors


def is_flow_file(path: Path) -> bool:
    return path.name.endswith(SUFFIX) and not path.name.endswith((TEST_SUFFIX, RECORDING_SUFFIX))


def find_files(paths: list[Path], suffix_check: Any = is_flow_file) -> list[Path]:
    """Flow files under the given directories (recursively) and the files named, sorted."""
    out: set[Path] = set()
    for p in paths:
        if p.is_dir():
            out.update(f for f in p.rglob("*" + SUFFIX) if f.is_file() and suffix_check(f))
        elif p.exists():
            out.add(p)
        else:
            raise InvalidRequest(f"{p} does not exist")
    return sorted(out)


def load_flow(path: Path, *, root: Path | None = None) -> FlowFile:
    """Read and check one flow file, as the node would check it, plus where it sits and secrets.
    `root`: the flows directory, when the file was found in one (then its folder is checked too)."""
    ff = FlowFile(path)
    try:
        data = load_yaml(path.read_text(encoding="utf-8"), "")
        if not isinstance(data, dict):
            raise InvalidRequest(f"it must hold one flow (a mapping with id, lane, steps), not {type(data).__name__}")
        ff.definition = parse_definition(data)
    except InvalidRequest as e:
        ff.errors.append(e.message)
        return ff
    except (OSError, UnicodeDecodeError) as e:
        ff.errors.append(f"it cannot be read: {e}")
        return ff
    defn = ff.definition
    stem = path.name.removesuffix(SUFFIX)
    if stem != defn.id:
        ff.errors.append(f"the file is named {path.name} but holds flow {defn.id}: name it {defn.id}{SUFFIX}")
    if root is not None:
        want = workspace_dir(defn.lane)
        try:
            rel = path.resolve().relative_to(root.resolve())
        except ValueError:
            rel = None
        if rel is not None and rel.parts[:-1] != (want,):
            ff.errors.append(f"flow {defn.id} runs on {defn.lane}, so it belongs in {root / want}, "
                             f"not {path.parent}")
    ff.errors += secret_problems(defn)
    ff.warnings += lint(defn)
    return ff


def workspace_dir(lane: str) -> str:
    """The folder a flow lives in: its lane's workspace, or `_any` for a lane pattern."""
    workspace = lane.split("/", 1)[0]
    return PATTERN_DIR if is_lane_pattern(workspace) else workspace


def file_for(root: Path, defn: FlowDefinition) -> Path:
    return root / workspace_dir(defn.lane) / f"{defn.id}{SUFFIX}"


def in_workspace(lane: str, workspace: str | None) -> bool:
    """Does a flow on `lane` (an id or a pattern) belong to `workspace` (None: every workspace)?"""
    if workspace is None:
        return True
    from fnmatch import fnmatchcase

    return fnmatchcase(workspace, lane.split("/", 1)[0])


# ---- secrets ----------------------------------------------------------------------------


def secret_problems(defn: FlowDefinition) -> list[str]:
    """What in this flow looks like a secret: a flow file must never hold one."""
    problems: list[str] = []
    props = defn.params.get("properties", {})
    for name, prop in props.items():
        if not isinstance(prop, dict) or "default" not in prop:
            continue
        if names_secret(name) or _SECRET_FIELD.search(name) or prop.get("format") == "password":
            problems.append(f"param {name} looks like a credential and has a default value: a secret never sits in "
                            "a flow. Leave the default out and log in with the vault (openajar vault).")
    for path, step in iter_steps(defn.steps):
        text = getattr(step, "text", None)
        loc = getattr(step, "locator", None)
        if isinstance(text, str) and "${" not in text and loc is not None:
            label = " ".join(s for s in (loc.name, loc.label, loc.css, *(loc.names or {}).values()) if s)
            if _SECRET_FIELD.search(label):
                problems.append(f"step {path} ({step.type}) types a literal into {loc.describe()}, which looks like "
                                "a password field: a secret never sits in a flow. Log in with the vault instead.")
    blob = str(defn.canonical())
    for mask in MASKS:
        if mask in blob:
            problems.append(f"the flow holds {mask}...], where the node masked a value from its vault: a vault value "
                            "was written into this flow. Move it out (log in with the vault) and save it again.")
            break
    return problems


# ---- comparing ----------------------------------------------------------------------------


def diff_text(old: str, new: str, old_name: str, new_name: str) -> str:
    """A unified diff, '' when the two are the same."""
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                 fromfile=old_name, tofile=new_name)
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)
