"""The flows service: saved flow versions, params validation, the catalog, lint."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from collections import Counter
from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agentos.core import globs
from agentos.core.errors import InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import LANE_ID_RE, Effect, Principal
from agentos.runner.flowdoc import FlowDefinition, FlowVersion
from agentos.runner.names import CONNECTOR_RESERVED, is_connector_id, lane_matches
from agentos.runner.owner import INSTALL_KINDS, NodeInstall, is_owner
from agentos.runner.steps import ACTION_TYPES, ReadbackStep, iter_steps
from agentos.runner.store import RunnerStore
from agentos.runner.util import Clock, Scrubber, clip_text, json_size

log = logging.getLogger("agentos.runner")

MAX_PARAMS_BYTES = 256 * 1024
TOOL_PREFIX = "flow_"
# the static tools in agentos.runner.tools; a flow id never takes one of these names
STATIC_TOOL_NAMES = frozenset({"flows_list", "flow_get", "flow_run", "run_status", "run_cancel", "flow_save",
                               "flow_record_start", "flow_record_stop", "ask_first_on"})


def validation_message(e: ValidationError, limit: int = 6) -> str:
    parts = []
    for err in e.errors()[:limit]:
        loc = ".".join(str(x) for x in err.get("loc", ()) if x not in ("function-after", "function-before"))
        msg = err.get("msg", "invalid").removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    more = len(e.errors()) - limit
    return "; ".join(parts) + (f"; and {more} more" if more > 0 else "")


def parse_definition(definition: dict[str, Any] | FlowDefinition) -> FlowDefinition:
    """A flow document as the node checks it before saving: a JSON object (its version and
    who saved it are left out), validated as a FlowDefinition. InvalidRequest says what is wrong
    and where. FlowService.parse and the offline `agentos flows validate` both use this."""
    if isinstance(definition, FlowDefinition):
        return definition
    if not isinstance(definition, dict):
        raise InvalidRequest("a flow definition is a JSON object")
    data = {k: v for k, v in definition.items() if k not in ("version", "created_at", "created_by")}
    try:
        return FlowDefinition.model_validate(data)
    except ValidationError as e:
        raise InvalidRequest(f"invalid flow: {validation_message(e)}") from e


def tool_name_for(flow_id: str) -> str:
    """'biz-a.instagram.post' -> 'flow_biz_a_instagram_post' (MCP-safe, at most 64 chars).
    Two ids can map to the same name ('biz-a.x' and 'biz_a.x'); assign_tool_names settles it."""
    base = re.sub(r"[^a-z0-9]+", "_", flow_id.lower()).strip("_")
    name = TOOL_PREFIX + base
    if len(name) > 64:
        name = _hashed_name(name, flow_id)
    return name


def _hashed_name(name: str, flow_id: str) -> str:
    return name[:55].rstrip("_") + "_" + hashlib.sha256(flow_id.encode()).hexdigest()[:8]


def assign_tool_names(flow_ids: list[str], taken: set[str] | None = None) -> dict[str, str]:
    """flow id -> its MCP tool name, unique: in id order, a flow whose plain name is taken
    gets a hashed one. flow_tool_specs, flow_save and flows_list all use this."""
    used = set(taken or ())
    out: dict[str, str] = {}
    for fid in sorted(flow_ids):
        name = tool_name_for(fid)
        if name in used:
            name = _hashed_name(name, fid)
        used.add(name)
        out[fid] = name
    return out


def lint(defn: FlowDefinition, *, effect: Effect | None = None) -> list[str]:
    """Warnings that do not block saving, but limit what a run can prove. `effect` is the
    effective effect when known (FlowVersion.effect), else the declared one."""
    warnings: list[str] = []
    steps = list(iter_steps(defn.steps))
    effect = effect or defn.effect
    if effect.rank and defn.readback_count() == 0:
        warnings.append(f"the flow's effect is {effect} but it declares no readback, so its runs can "
                        "end at best succeeded_unverified")
    top = defn.steps
    last_action = max((i for i, s in enumerate(top) if s.type in ACTION_TYPES), default=None)
    if (last_action is not None and getattr(top[last_action], "readback", None) is None
            and not any(isinstance(s, ReadbackStep) for s in top[last_action + 1:])):
        warnings.append(f"the last action (step {last_action}, {top[last_action].type}) is not "
                        "checked by any readback")
    for path, step in steps:
        loc = getattr(step, "locator", None)
        if loc is not None and loc.role is None and loc.attr is None:
            warnings.append(f"step {path} uses a css-only locator; prefer role and name (or attr), which survive "
                            "redesigns")
        if loc is not None and loc.nth is not None and loc.within is None:
            warnings.append(f"step {path} picks by nth without an anchor (within), so it depends on page order")
    return warnings


def _same_lane(a: str, b: str) -> bool:
    """Two flows' lanes, either of which may be a pattern, reach the same lane."""
    return a == b or globs.lane_match(a, b) or globs.lane_match(b, a)


class FlowService:
    def __init__(self, store: RunnerStore, clock: Clock | None = None):
        self.store = store
        self.clock = clock or Clock()
        self._listeners: list[Callable[[str, FlowVersion], None]] = []
        self.recorder = None  # set by build(): agentos.runner.recorder.Recorder
        self.vault_scrub: Callable[[str], str] | None = None  # set by build(): the vault's values out of what is shown
        # set by build(): (lane, file ref) -> "sha256:<hex>" of the inbox file an upload step would send, or None
        self.file_digest: Callable[[str, str], str | None] | None = None
        self._shown: dict[tuple[str, str], frozenset[str]] = {}  # versions are immutable
        # set by build(): the policy's "Ask me first" switches (agentos.policy.ask_first), when present
        self.ask_first_source: Callable[[], Any] | None = None

    def _switches(self) -> Any:
        return self.ask_first_source() if self.ask_first_source is not None else None

    def ask_first_view(self, flow_id: str) -> dict[str, Any]:
        """`{ ask_first, ask_first_by }`: the flow id's switch (it follows the id across versions)."""
        store = self._switches()
        if store is None or not callable(getattr(store, "flow_view", None)):
            return {"ask_first": False, "ask_first_by": None}
        return store.flow_view(flow_id)

    # ---- change notifications (the MCP layer re-lists dynamic flow tools) ----
    def on_change(self, callback: Callable[[str, FlowVersion], None]) -> None:
        self._listeners.append(callback)

    def _changed(self, flow_id: str, fv: FlowVersion) -> None:
        for cb in list(self._listeners):
            try:
                cb(flow_id, fv)
            except Exception:  # a listener must not break a save
                log.exception("flow change listener failed")

    # ---- versions ----
    def parse(self, definition: dict[str, Any] | FlowDefinition) -> FlowDefinition:
        return parse_definition(definition)

    def asks_first_by_inheritance(self, defn: FlowDefinition) -> str | None:
        """Why a new routine an agent saves starts with "Ask me first" on, or None: a person set
        another routine on its lane to ask first, and the same steps under a new id must not shed
        that. A flow already saved, or one whose switch anybody set, keeps what it has."""
        store = self._switches()
        if store is None or self.store.get_flow(defn.id) is not None or store.get("flow", defn.id) is not None:
            return None
        for s in store.list("flow"):
            other = self.store.get_flow(s.id) if s.ask_first and s.by_person else None
            if other is not None and _same_lane(other.definition.lane, defn.lane):
                return f"a person set routine {s.id} on {other.definition.lane} to ask first, so a new routine an " \
                       "agent saves there starts the same way"
        return None

    def save(self, definition: dict[str, Any] | FlowDefinition, *,
             by: Principal | NodeInstall | dict[str, Any] | str | None = None,
             agent: bool | None = None) -> tuple[FlowVersion, bool]:
        """Save a new immutable version (or re-point the head at an identical old one). `by`
        is who saves it; only a version saved by the human owner may lower a step's effect
        with `publishes: false` (a bare id string counts as not the owner). `agent`: an agent saves
        it (by default when `by` says so; a bearer token says so whatever its principal)."""
        defn = self.parse(definition)
        version = defn.content_hash()
        saved_by = _saved_by(by)
        installed = isinstance(by, NodeInstall)
        if is_connector_id(defn.id) != installed:  # only ConnectorService and SiteApiService install connector.* ids
            raise PolicyDenied(CONNECTOR_RESERVED if not installed else
                               "a node install may only save connector.* flows")
        by_id = saved_by["id"] if saved_by else (by if isinstance(by, str) else None)
        head = self.store.get_flow(defn.id)
        if not installed:
            self._check_tool_name(defn, head)
        if agent is None:
            agent = saved_by is not None and saved_by.get("kind") == "agent"
        why = self.asks_first_by_inheritance(defn) if agent else None
        if why is not None:  # before it exists, so it never runs on its own
            self._switches().set("flow", defn.id, True, by=f"agent:{by_id or 'unknown'}", person=False, why=why)
        if head is not None and head.definition.sends and not defn.sends and not is_owner(saved_by):
            raise PolicyDenied(f"{defn.id} sends to people: only a person can save a version that says it does not")
        existing = self.store.get_flow(defn.id, version)
        if existing is not None and is_owner(saved_by) and not existing.owner_saved:
            # the owner vouches for a version someone else saved first
            self.store.set_flow_saved_by(defn.id, version, saved_by)
            existing = existing.model_copy(update={"saved_by": saved_by})
        fv = existing or FlowVersion(definition=defn, version=version, created_at=self.clock.now(),
                                     created_by=by_id, saved_by=saved_by)
        created = self.store.put_flow(fv)
        self._changed(defn.id, fv)
        return fv, created

    def _check_tool_name(self, defn: FlowDefinition, head: FlowVersion | None) -> None:
        """A flow that starts serving a tool (a new id, or one that served none) never takes a name in
        use: a static tool's, another flow's plain name, or the name another flow is served as now.
        Otherwise the sort order of ids would hand it a name an agent already calls, and another
        workspace's flow would run under it (a flow keeps its name once it has one)."""
        if not defn.expose_as_tool or (head is not None and head.definition.expose_as_tool):
            return
        name = tool_name_for(defn.id)
        others = [fv.id for fv in self.list() if fv.id != defn.id and fv.definition.expose_as_tool]
        taken = set(STATIC_TOOL_NAMES) | {tool_name_for(fid) for fid in others}
        taken |= set(assign_tool_names(others, STATIC_TOOL_NAMES).values())
        if name in taken:
            raise InvalidRequest(f"flow id {defn.id!r} would be served as tool {name}, a tool name that another "
                                 "flow or tool already has. Save it under another id.")

    def stage(self, definition: FlowDefinition, *, by: NodeInstall) -> FlowVersion:
        """A connector version with no head, for a site API's try: it runs when a run pins it, and no
        tool, schedule or list can reach it. Activating the same content later makes it the head."""
        if not isinstance(by, NodeInstall) or not is_connector_id(definition.id):
            raise PolicyDenied(CONNECTOR_RESERVED)
        version = definition.content_hash()
        existing = self.store.get_flow(definition.id, version)
        if existing is not None:
            return existing
        saved_by = _saved_by(by)
        fv = FlowVersion(definition=definition, version=version, created_at=self.clock.now(),
                         created_by=(saved_by or {}).get("id"), saved_by=saved_by)
        self.store.stage_flow(fv)
        return fv

    def get(self, flow_id: str, version: str | None = None) -> FlowVersion:
        fv = self.store.get_flow(flow_id, version)
        if fv is None:
            what = f"flow {flow_id!r}" + (f" version {version}" if version else "")
            raise NotFound(f"{what} is not saved on this node (see flows_list)")
        return fv

    def list(self, prefix: str | None = None) -> list[FlowVersion]:
        heads = self.store.flow_heads()
        if prefix:
            p = prefix.strip(".")
            heads = [f for f in heads if f.id == p or f.id.startswith(p + ".")]
        return heads

    def versions(self, flow_id: str) -> list[FlowVersion]:
        return self.store.flow_versions(flow_id)

    def delete(self, flow_id: str) -> bool:
        return self.store.delete_flow(flow_id)

    # ---- tool names ----
    def tool_names(self, taken: set[str] | None = None) -> dict[str, str]:
        ids = [fv.id for fv in self.list() if fv.definition.expose_as_tool]
        return assign_tool_names(ids, STATIC_TOOL_NAMES if taken is None else taken)

    # ---- params and lanes ----
    def validate_params(self, fv: FlowVersion, params: dict[str, Any] | None) -> dict[str, Any]:
        if params is not None and not isinstance(params, dict):
            raise InvalidRequest(f"params for {fv.id} must be a JSON object of name: value")
        params = dict(params or {})
        if json_size(params) > MAX_PARAMS_BYTES:
            raise InvalidRequest(f"params are larger than {MAX_PARAMS_BYTES // 1024} KB; pass big content "
                                 "as a file ref instead")
        schema = fv.definition.params
        for key, prop in schema.get("properties", {}).items():
            if key not in params and isinstance(prop, dict) and "default" in prop:
                params[key] = prop["default"]
        errors = sorted(Draft202012Validator(schema).iter_errors(params), key=lambda e: list(e.path))
        if errors:
            parts = []
            for e in errors[:5]:
                where = ".".join(str(p) for p in e.path) or "params"
                parts.append(f"{where}: {clip_text(e.message, 160)}")
            raise InvalidRequest(f"invalid params for {fv.id}: " + "; ".join(parts))
        return params

    def resolve_lane(self, fv: FlowVersion, on: str | None) -> str:
        lane = fv.definition.lane
        if fv.definition.lane_is_pattern:
            if not on:
                raise InvalidRequest(f"flow {fv.id} runs on lanes matching {lane!r}; pass `on` with the lane")
            if not LANE_ID_RE.match(on) or not lane_matches(lane, on):
                raise InvalidRequest(f"lane {on!r} does not match the flow's lane pattern {lane!r}")
            return on
        if on and on != lane:
            raise InvalidRequest(f"flow {fv.id} is bound to lane {lane!r}, not {on!r}")
        return lane

    # ---- what may be shown ----
    @staticmethod
    def shown_params(fv: FlowVersion) -> set[str]:
        """Params the flow marks `"show": true` (a caption the owner reads before approving).
        Every other param value stays out of proof, journals, logs and approval previews."""
        props = fv.definition.params.get("properties", {})
        return {k for k, v in props.items() if isinstance(v, dict) and v.get("show") is True}

    def shown_params_of(self, flow_id: str | None, version: str | None) -> frozenset[str]:
        """shown_params for a run's pinned version (cached); nothing is shown for a flow that
        is gone."""
        key = (flow_id or "", version or "")
        if key not in self._shown:
            try:
                self._shown[key] = frozenset(self.shown_params(self.get(flow_id or "", version)))
            except NotFound:
                return frozenset()
        return self._shown[key]

    def scrubber(self, fv: FlowVersion, params: dict[str, Any]) -> Scrubber:
        shown = self.shown_params(fv)
        return Scrubber({k: v for k, v in params.items() if k not in shown}, extra=self.vault_scrub)

    def _hidden_digest(self, value: Any) -> str:
        salt = self.store.meta("param_salt", secrets.token_hex(16))
        blob = json.dumps(value, sort_keys=True, default=str).encode()
        return "hidden:" + hmac.new(salt.encode(), blob, hashlib.sha256).hexdigest()[:16]

    def run_summary(self, fv: FlowVersion, lane: str, params: dict[str, Any]) -> str:
        """Node-authored description for approvals: what will actually run, never model text."""
        d = fv.definition
        shown = self.shown_params(fv)
        parts = [f"{k}={clip_text(str(v), 80)!r}" if k in shown else f"{k}=(hidden)" for k, v in sorted(params.items())]
        title = f" ({d.title})" if d.title else ""
        return f"Run flow {d.id}{title} version {fv.version} on {lane} with effect {fv.effect}: " + (
            ", ".join(parts) or "no params")

    @staticmethod
    def upload_params(fv: FlowVersion) -> set[str]:
        """Params an upload step sends as a file (a file in the lane's inbox), whatever their schema says."""
        return {step.param for _, step in iter_steps(fv.definition.steps)
                if getattr(step, "type", None) == "upload" and isinstance(getattr(step, "param", None), str)}

    def _file_bound(self, lane: str, value: Any) -> str | None:
        if self.file_digest is None or not isinstance(value, str):
            return None
        try:
            return self.file_digest(lane, value)
        except Exception:  # noqa: BLE001 - a file that is not there binds by its name; the upload step fails
            return None

    def approval_content(self, fv: FlowVersion, lane: str, params: dict[str, Any]) -> dict[str, Any]:
        """What an approval binds to. A hidden param is bound by a keyed digest of its value,
        so the approval covers that exact value while the preview never shows it. A param an upload
        step sends is bound by the sha256 of the file's bytes, so another file under the same name is
        another approval (QA-2 #14)."""
        shown = self.shown_params(fv)
        files = self.upload_params(fv)
        bound = {}
        for k, v in params.items():
            digest = self._file_bound(lane, v) if k in files else None
            bound[k] = digest or (v if k in shown else self._hidden_digest(v))
        return {"flow": fv.id, "version": fv.version, "lane": lane, "params": bound}

    # ---- the catalog ----
    def summary(self, fv: FlowVersion, tools: dict[str, str] | None = None,
                lane_seen: Callable[[str], bool] | None = None) -> dict[str, Any]:
        """The flow's shape for flows_list and the panel. `effect` is the effective effect
        (what grants and approvals see); `declared_effect` is what the definition says. With
        `lane_seen`, a last run on a lane it does not see is left out."""
        d = fv.definition
        props = d.params.get("properties", {})
        required = set(d.params.get("required", []))
        last = self.store.list_runs(flow_id=d.id, limit=1)
        if last and lane_seen is not None and not lane_seen(str(last[0].surface or last[0].lane or "")):
            last = []
        tools = tools if tools is not None else self.tool_names()
        out = {
            "id": d.id, "title": d.title, "lane": d.lane, "effect": str(fv.effect),
            "declared_effect": str(d.effect), "version": fv.version,
            "params": {k: {"type": (v or {}).get("type", "any"), "required": k in required,
                           **({"description": v["description"]} if isinstance(v, dict) and v.get("description") else {})}
                       for k, v in props.items()},
            "tool": (tools.get(d.id) or tool_name_for(d.id)) if d.expose_as_tool else None,
            "readbacks": d.readback_count(),
            **self.ask_first_view(d.id),
            "last_run": ({"id": last[0].id, "status": str(last[0].status),
                          "finished_at": last[0].finished_at.isoformat() if last[0].finished_at else None}
                         if last else None),
        }
        if fv.effect_reason:
            out["effect_note"] = fv.effect_reason
        return out

    def catalog(self, prefix: str | None = None, limit: int = 50, *, seen: Callable[[str], bool] | None = None,
                lane_seen: Callable[[str], bool] | None = None, lane: str | None = None) -> dict[str, Any]:
        """Flows by namespace. `seen(lane or pattern)`: only flows bound where the caller reaches count,
        in the totals and namespaces too; `lane_seen` filters each flow's last run (summary). `lane`: only
        flows that run on that lane, bound to it or to a pattern that covers it, whatever their namespace."""
        from agentos.runner.names import lane_matches

        p = (prefix or "").strip(".")
        matching = self.list(p or None)
        if seen is not None:
            matching = [fv for fv in matching if seen(fv.definition.lane)]
        if lane:
            matching = [fv for fv in matching if fv.definition.lane == lane
                        or (fv.definition.lane_is_pattern and lane_matches(fv.definition.lane, lane))]
        depth = p.count(".") + 1 if p else 0
        children: Counter[str] = Counter()
        for fv in matching:
            segs = fv.id.split(".")
            if len(segs) > depth + 1:
                children[".".join(segs[: depth + 1])] += 1
        out: dict[str, Any] = {
            "prefix": p or None,
            **({"lane": lane} if lane else {}),
            "total": len(matching),
            "namespaces": [{"prefix": ns, "flows": n} for ns, n in sorted(children.items())],
        }
        tools = self.tool_names()
        if len(matching) <= limit:
            out["flows"] = [self.summary(fv, tools, lane_seen) for fv in matching]
        else:
            out["flows"] = [self.summary(fv, tools, lane_seen) for fv in matching if fv.id.count(".") == depth][:limit]
            out["hint"] = "Many flows match. Call flows_list again with one of the namespace prefixes."
        return out


def _saved_by(by: Principal | NodeInstall | dict[str, Any] | str | None) -> dict[str, Any] | None:
    if isinstance(by, NodeInstall):
        if by.saved_by.get("kind") not in INSTALL_KINDS or not by.saved_by.get("id"):
            raise PolicyDenied("a node install is saved as catalog or local, with an id")
        return dict(by.saved_by)
    if str(getattr(by, "kind", None) or (by.get("kind") if isinstance(by, dict) else "")).strip().lower() in INSTALL_KINDS:
        raise PolicyDenied("only the node's own installers save a flow as catalog or local")
    if isinstance(by, Principal):
        return {"kind": by.kind, "id": by.id, "owner": by.owner}
    if isinstance(by, dict) and by.get("id"):
        return {"kind": by.get("kind", "agent"), "id": by["id"], "owner": by.get("owner", "owner"),
                **({"via": by["via"]} if by.get("via") else {})}
    return None
