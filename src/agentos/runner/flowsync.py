"""Flows between a node and a folder of flow files: pull, diff and push, through MCP.

Everything goes through the connection's MCP endpoint (agentos.client.call), with the agents'
grant: flows_list and flow_get to read, flow_save to write. So a push is an agent's save under the
normal policy: the grant must reach the flow's lane, a save that asks a person waits for one, and
an agent's version never lowers a flow's effect ("publishes": false counts only when a person saves
it). Nothing here reads the node's database or bypasses a check.

A file and the node's flow are the same when their content hashes are (the flow's version), so a
file in another layout or with comments is not a change, and a pull right after a push writes
nothing. Pull writes only what differs, in the layout of agentos.runner.flowfiles.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentos.client.call import CallError, Session, StillPending, call_tool
from agentos.core.errors import InvalidRequest
from agentos.runner.flowfiles import (
    FlowFile,
    diff_text,
    file_for,
    find_files,
    in_workspace,
    load_flow,
    render,
    secret_problems,
)
from agentos.runner.flows import parse_definition
from agentos.runner.models import FlowDefinition, is_connector_id

# ---- reading the node --------------------------------------------------------------------


def node_flows(session: Session, workspace: str | None = None) -> list[dict[str, Any]]:
    """Every flow head the grant sees (flows_list, walked namespace by namespace when it is long),
    without installed connector operations, and only `workspace`'s when given."""
    found: dict[str, dict[str, Any]] = {}

    def walk(prefix: str | None) -> None:
        listing = call_tool(session, "flows_list", {"prefix": prefix} if prefix else {})
        for row in listing.get("flows") or []:
            found[row["id"]] = row
        if listing.get("hint"):
            for ns in listing.get("namespaces") or []:
                if ns["prefix"] != prefix:
                    walk(ns["prefix"])

    walk(None)
    return [row for fid, row in sorted(found.items())
            if not is_connector_id(fid) and in_workspace(str(row.get("lane") or ""), workspace)]


def node_flow(session: Session, flow_id: str) -> FlowDefinition | None:
    """The node's current version of a flow, or None when it has none (or the grant does not see it)."""
    try:
        got = call_tool(session, "flow_get", {"name": flow_id})
    except CallError as e:
        if e.message.startswith("not_found"):
            return None
        raise
    try:
        return parse_definition(got["definition"])
    except InvalidRequest as e:
        raise InvalidRequest(f"the node's {flow_id} does not read as a flow here ({e.message}); is this agentos "
                             "older than the node's?") from None


# ---- comparing ------------------------------------------------------------------------------


@dataclass
class Change:
    flow_id: str
    status: str  # new, changed, same, only_on_node
    path: Path | None = None
    local: FlowDefinition | None = None
    node: FlowDefinition | None = None

    def diff(self) -> str:
        old = render(self.node) if self.node is not None else ""
        new = render(self.local) if self.local is not None else ""
        return diff_text(old, new, f"node: {self.flow_id}", str(self.path) if self.path else "(no file)")


@dataclass
class Loaded:
    files: list[FlowFile] = field(default_factory=list)

    @property
    def bad(self) -> list[FlowFile]:
        return [f for f in self.files if not f.ok]


def load_paths(paths: list[Path], workspace: str | None = None) -> Loaded:
    """The flow files under `paths`, checked offline. A folder is a flows root (its layout is checked)."""
    out = Loaded()
    for p in paths:
        root = p if p.is_dir() else None
        for f in find_files([p]):
            ff = load_flow(f, root=root)
            if ff.definition is None or in_workspace(ff.definition.lane, workspace):
                out.files.append(ff)
    seen: dict[str, Path] = {}
    for ff in out.files:
        if ff.definition is None:
            continue
        if ff.definition.id in seen:
            ff.errors.append(f"flow {ff.definition.id} is also in {seen[ff.definition.id]}: keep one file per flow")
        seen.setdefault(ff.definition.id, ff.path)
    return out


def compare(session: Session, files: list[FlowFile], *, workspace: str | None = None,
            with_node_only: bool = False) -> list[Change]:
    """Each file against the node's flow; with `with_node_only`, also the node's flows no file has."""
    heads = {row["id"]: row for row in node_flows(session, workspace)}
    changes: list[Change] = []
    for ff in files:
        defn = ff.definition
        if defn is None:
            continue
        head = heads.get(defn.id)
        if head is not None and head.get("version") == defn.content_hash():
            changes.append(Change(defn.id, "same", ff.path, defn))
            continue
        node = node_flow(session, defn.id) if head is not None else None
        changes.append(Change(defn.id, "changed" if node is not None else "new", ff.path, defn, node))
    if with_node_only:
        mine = {c.flow_id for c in changes}
        changes += [Change(fid, "only_on_node") for fid in heads if fid not in mine]
    return changes


# ---- pull ---------------------------------------------------------------------------------


@dataclass
class PullReport:
    written: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)  # (flow id, why)
    only_here: list[Path] = field(default_factory=list)


def pull(session: Session, root: Path, *, workspace: str | None = None) -> PullReport:
    """Write each of the node's flows (the ones the grant sees, in `workspace` when given) under
    `root`. A file whose flow is the same is left as it is (comments and all); a flow whose lane moved
    to another workspace moves to that folder; a flow that holds what looks like a secret is not
    written. Files of flows the node does not have are left alone and listed in `only_here`.

    A file whose flow differs is overwritten: the node wins, as a checkout does, and `git diff` shows
    what it changed (diff first to see edits not pushed yet). A file that does not read as a flow is
    never overwritten, so half an edit is not lost."""
    report = PullReport()
    files = load_paths([root], workspace).files if root.is_dir() else []
    local = {ff.definition.id: ff for ff in files if ff.definition is not None}
    broken = {ff.path.resolve() for ff in files if ff.definition is None}
    for row in node_flows(session, workspace):
        fid = row["id"]
        try:
            defn = node_flow(session, fid)
        except InvalidRequest as e:
            report.refused.append((fid, e.message))
            continue
        if defn is None:
            continue
        problems = secret_problems(defn)
        if problems:
            report.refused.append((fid, " ".join(problems)))
            continue
        path = file_for(root, defn)
        if path.resolve() in broken:
            why = f"{path} does not read as a flow (agentos flows validate says why); fix it or move it, then pull again"
            report.refused.append((fid, why))
            continue
        mine = local.pop(fid, None)
        if mine is not None and mine.path != path:
            mine.path.unlink()  # its lane moved to another workspace: one file per flow
        elif mine is not None and mine.definition is not None and mine.definition.content_hash() == defn.content_hash():
            report.unchanged.append(mine.path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(defn), encoding="utf-8")
        report.written.append(path)
    report.only_here = sorted(ff.path for ff in local.values())
    return report


# ---- push -----------------------------------------------------------------------------------


@dataclass
class Pushed:
    flow_id: str
    path: Path
    version: str | None = None
    created: bool = False
    warnings: list[str] = field(default_factory=list)
    pending: dict[str, Any] | None = None  # the approval it waits for
    error: str | None = None


def push(session: Session, changes: list[Change], *, wait_s: float = 0.0,
         say: Callable[[str], None] = lambda _: None) -> list[Pushed]:
    """Save each new or changed flow with flow_save, one after the other, as an agent would."""
    out: list[Pushed] = []
    for change in changes:
        if change.status not in ("new", "changed") or change.local is None or change.path is None:
            continue
        done = Pushed(change.flow_id, change.path)
        try:
            result = call_tool(session, "flow_save", {"definition": change.local.canonical()}, wait_s=wait_s, say=say)
        except StillPending as e:
            done.pending = e.result
        except CallError as e:
            done.error = e.message
        else:
            done.version, done.created = result.get("version"), bool(result.get("created"))
            done.warnings = list(result.get("warnings") or [])
        out.append(done)
    return out
