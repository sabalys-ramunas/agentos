"""Surfaces and connector bindings (docs/design/connectors.md 4.3, AUD-01).

A lane is a login; a surface (agentos.core.models.Surface) is who you post as. Grants,
approvals, pace and schedules target the surface; its binding decides the lane:

    biz-a/instagram      site instagram, lane biz-a/instagram,    acts as account 51234567
    biz-a/facebook-page  site facebook,  lane personal/facebook,  acts as fb_page 100089000000001

`SurfaceService` (runtime service "surfaces") keeps them in runner.db, owner only, and turns
"run connector.<site>.<op> on <surface> with these params" into what the runner queues:

    target = surfaces.target(fv, "biz-a/facebook-page", {"caption": ...})
    target.lane      personal/facebook  (binding.lane)
    target.params    the caller's params plus Binding.params() (acts_as_id, account_language),
                     which the caller may never pass: the binding decides who a run acts as
    surfaces.approval(fv, target) -> (summary, content)

The approval content is `{operation, contract, surface, acts_as, params}`: shown params
verbatim, other params by a keyed digest, and file params by the sha256 of the bytes in the
lane's inbox. `contract` is the operation's contract hash for a catalog install, so a standing
approval bound to `connector:<site>.<op>@<contract>` survives a repair that only changes
locators, and the implementation hash for anything else (a local override, phase 4).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agentos.core.errors import InvalidRequest, LaneNotFound, NotFound, PolicyDenied
from agentos.core.models import Principal, Surface
from agentos.runner.connectors.lint import site_hosts
from agentos.runner.connectors.models import BINDING_PARAMS, WEB_SITE, Binding
from agentos.runner.flows import FlowService, validation_message
from agentos.runner.models import FlowVersion, is_connector_id, is_owner
from agentos.runner.store import RunnerStore
from agentos.runner.util import Clock, clip_text

log = logging.getLogger("agentos.runner")

FILE_FORMAT = "agentos-file"
FILE_ID_RE = re.compile(r"^in_[0-9a-f]{6,40}$")  # a files_put id (agentos.node.files)
FileDigest = Callable[[str, str], str]  # (lane, file ref) -> "sha256:<hex>" of the bytes on the node
APPROVAL_KEYS = frozenset({"operation", "contract", "surface", "acts_as", "params"})


class ConnectorBroken(PolicyDenied):
    """The operation is broken on this surface (the site changed): it is not run, so an agent
    does not fall back to clicking. It works again once a repaired version is installed."""
    code = "connector_broken"


class PaceLimited(PolicyDenied):
    """The surface's pace for this operation is used up; no approval overrides it."""
    code = "pace_limited"

    def __init__(self, message: str, retry_at: Any = None):
        super().__init__(message)
        self.retry_at = retry_at


@dataclass
class Target:
    """Where and as whom one connector run acts."""
    surface: Surface
    binding: Binding
    lane: str
    params: dict[str, Any]  # validated, with the binding params

    @property
    def caller_params(self) -> dict[str, Any]:
        return {k: v for k, v in self.params.items() if k not in BINDING_PARAMS}


def _owner(by: Principal | Mapping[str, Any] | None, what: str) -> dict[str, Any]:
    p = by.model_dump() if isinstance(by, Principal) else dict(by or {})
    if not is_owner(p):
        raise PolicyDenied(f"only the owner {what} (the panel or the CLI)")
    return p


def _identity(s: Surface) -> tuple[Any, ...]:
    """What a standing approval on a surface was given for: the login it posts through, who it
    posts as, and the account's language."""
    return (s.lane, s.acts_as.kind, s.acts_as.asset_id, s.account_language)


def operation_meta(fv: FlowVersion) -> dict[str, Any]:
    meta = fv.definition.connector
    if not meta or not is_connector_id(fv.id):
        raise InvalidRequest(f"{fv.id} is not a connector operation")
    return meta


def dotted(fv: FlowVersion) -> str:
    meta = operation_meta(fv)
    return f"{meta['connector']}.{meta['operation']}"


def binds_to(fv: FlowVersion) -> str:
    """What approvals of this operation bind to: the contract hash for a catalog install (a
    locator repair keeps it), else the implementation hash (a local edit changes it)."""
    meta = operation_meta(fv)
    catalog = (fv.saved_by or {}).get("kind") == "catalog"
    return str(meta["contract"]) if catalog else fv.version


def file_params(fv: FlowVersion) -> list[str]:
    props = fv.definition.params.get("properties", {})
    return sorted(k for k, v in props.items() if isinstance(v, dict) and v.get("format") == FILE_FORMAT)


def inbox_digest(runtime: Any) -> FileDigest:
    """The default FileDigest: the file in the lane's inbox, hashed from its bytes. A file id
    from files_put (`in_...`) is read through agentos.node.files.LaneFiles.path (which refuses
    links and anything outside the inbox); a name or path through the flow driver's inbox lookup."""

    def digest(lane: str, ref: str) -> str:
        from agentos.node.flow_driver import FileNotInInbox, FileOutsideInbox, inbox_dir, resolve_inbox_file

        ref = str(ref)
        files = runtime.get("files") if runtime is not None else None
        config = runtime.get("config") if runtime is not None else None
        try:
            if files is None and config is not None and FILE_ID_RE.match(ref):
                from agentos.node.io_tools import lane_files

                path = lane_files(runtime, lane).path(ref, box="inbox")
            else:
                path = resolve_inbox_file(inbox_dir(runtime, lane), ref, files=files, lane_id=lane)
        except (FileNotInInbox, FileOutsideInbox, NotFound) as e:
            message = getattr(e, "message", str(e))
            raise InvalidRequest(f"{message} (put it in the inbox of {lane} with files_put first)") from None
        return "sha256:" + _sha256(Path(path))

    return digest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SurfaceService:
    """Runtime service "surfaces": the owner's surfaces and the binding of connector runs."""

    def __init__(self, store: RunnerStore, flows: FlowService, *, clock: Clock | None = None, runtime: Any = None,
                 sites: Mapping[str, tuple[str, ...]] | None = None, file_digest: FileDigest | None = None):
        self.store, self.flows, self.runtime = store, flows, runtime
        self.clock = clock or Clock()
        self.sites = sites
        self.file_digest = file_digest or inbox_digest(runtime)
        self._listeners: list[Callable[[str, Surface | None], None]] = []

    def on_change(self, callback: Callable[[str, Surface | None], None]) -> None:
        """callback(surface_id, surface or None when removed): the MCP layer re-projects tools."""
        self._listeners.append(callback)

    def _changed(self, surface_id: str, surface: Surface | None) -> None:
        for cb in list(self._listeners):
            try:
                cb(surface_id, surface)
            except Exception:
                log.exception("surface change listener failed")

    # ---- reading ----
    def list(self, *, site: str | None = None, allowed: Callable[[str], bool] | None = None) -> list[Surface]:
        out = self.store.list_surfaces(site=site)
        return [s for s in out if allowed is None or allowed(s.id)]

    def get(self, surface_id: str) -> Surface:
        s = self.store.get_surface(surface_id)
        if s is None:
            raise NotFound(f"no surface {surface_id!r} on this node (agentos connector bind adds one)")
        return s

    def find(self, surface_id: str) -> Surface | None:
        return self.store.get_surface(surface_id)

    # ---- writing (owner only) ----
    def put(self, data: Mapping[str, Any] | Surface, *, by: Principal | Mapping[str, Any] | None) -> tuple[Surface, bool]:
        """Create or replace a surface. Returns (surface, created)."""
        owner = _owner(by, "adds or changes surfaces")
        raw = data.model_dump() if isinstance(data, Surface) else dict(data)
        for k in ("created_at", "updated_at", "created_by"):
            raw.pop(k, None)
        try:
            surface = Surface.model_validate(raw)
        except ValidationError as e:
            raise InvalidRequest(f"invalid surface: {validation_message(e)}") from e
        known = site_hosts(self.sites)
        if surface.site == WEB_SITE:
            if surface.acts_as.kind != "lane":
                raise InvalidRequest("a web surface acts as the lane's own session (acts_as kind lane)")
        elif surface.site not in known:
            raise InvalidRequest(f"{surface.site!r} is not a site agentos knows (agentos.node.sites): one of "
                                 f"{', '.join(sorted(known))}")
        elif surface.acts_as.kind == "lane":
            raise InvalidRequest("acts_as lane is for a web surface: a site agentos knows pins its account")
        self._check_lane(surface.lane)
        old = self.store.get_surface(surface.id)
        now = self.clock.now()
        surface = surface.model_copy(update={"created_at": old.created_at if old else now, "updated_at": now,
                                             "created_by": old.created_by if old else owner.get("id")})
        self.store.put_surface(surface)
        if old is not None and _identity(old) != _identity(surface):
            self._lapse_standing(surface.id, "the surface now posts through another login or as someone else")
        self._changed(surface.id, surface)
        return surface, old is None

    def _lapse_standing(self, surface_id: str, why: str) -> list[str]:
        """Revoke the standing approvals that cover connector runs on this surface: the owner
        approved posting through one login as one account, not whatever the surface points at
        later. One-off approvals already lapse (their summary names the lane)."""
        approvals = self.runtime.get("approvals") if self.runtime is not None else None
        if approvals is None or not callable(getattr(approvals, "list_standing", None)):
            return []
        from agentos.policy import globs

        ws = surface_id.split("/", 1)[0]
        lapsed = []
        for st in approvals.list_standing():
            if st.targets and not any(t.startswith("connector:") for t in st.targets):
                continue
            covers = any(globs.lane_match(p, surface_id) for p in st.lanes) or \
                any(globs.segment_match(w, ws) for w in st.workspaces)
            if not covers:
                continue
            try:
                approvals.revoke_standing(st.id, by=f"node: {why}")
                lapsed.append(st.id)
            except Exception:  # already revoked or gone
                log.debug("could not revoke standing approval %s", st.id, exc_info=True)
        if lapsed:
            log.info("surface %s changed: revoked standing approvals %s", surface_id, ", ".join(lapsed))
        return lapsed

    def _check_lane(self, lane: str) -> None:
        lanes = self.runtime.get("lanes") if self.runtime is not None else None
        get = getattr(lanes, "get", None)
        if not callable(get):
            return
        try:
            get(lane)
        except LaneNotFound:
            raise InvalidRequest(f"lane {lane} does not exist on this node; create and log it in first") from None
        except Exception:
            log.debug("could not look up lane %s", lane, exc_info=True)

    def remove(self, surface_id: str, *, by: Principal | Mapping[str, Any] | None) -> Surface:
        _owner(by, "removes surfaces")
        s = self.get(surface_id)
        using = [sch.id for sch in self.store.list_schedules() if sch.surface == surface_id]
        if using:
            raise InvalidRequest(f"schedules {', '.join(using)} post to {surface_id}; remove them first")
        self.store.delete_surface(surface_id)
        self.store.delete_health_surface(surface_id)
        self._changed(surface_id, None)
        return s

    # ---- binding a run ----
    def target(self, fv: FlowVersion, on: str | None, params: Mapping[str, Any] | None, *,
               validate: bool = True) -> Target:
        """The lane and full params of a run of the operation `fv` on surface `on`. Raises
        InvalidRequest when the surface cannot run it, PolicyDenied when the caller passes a
        binding param. `validate=False` leaves the params unchecked (a schedule with templates)."""
        meta = operation_meta(fv)
        if not on:
            raise InvalidRequest(f"{dotted(fv)} runs on a surface: pass on, like 'biz-a/{meta['connector']}'")
        surface = self.find(on)
        if surface is None:
            raise InvalidRequest(f"no surface {on!r} on this node for {dotted(fv)} (agentos connector bind adds one)")
        given = sorted(set(params or {}) & set(BINDING_PARAMS))
        if given:
            raise PolicyDenied(f"{', '.join(given)} come from the surface's binding, never from the caller")
        binding = Binding.from_surface(surface)
        site = meta.get("site") or meta["connector"]
        if binding.connector != site:
            raise InvalidRequest(f"surface {on} is a {surface.site} surface, not {site}")
        only = meta.get("surfaces")
        if only and on not in only:
            raise InvalidRequest(f"{dotted(fv)} runs on {', '.join(only)}, not {on}: only a person changes that")
        kind = (meta.get("acting_as") or {}).get("kind")
        if kind != surface.acts_as.kind:
            raise InvalidRequest(f"{dotted(fv)} acts as {kind}; surface {on} acts as {surface.acts_as.kind}")
        langs = meta.get("account_languages") or []
        if surface.account_language not in langs:
            raise InvalidRequest(f"{dotted(fv)} covers the account languages {', '.join(langs)}; surface {on} "
                                 f"is in {surface.account_language}")
        full = {**dict(params or {}), **binding.params()}
        if validate:
            full = self.flows.validate_params(fv, full)
        assert binding.lane is not None  # a browser binding names its lane
        return Target(surface=surface, binding=binding, lane=binding.lane, params=full)

    def digests(self, fv: FlowVersion, target: Target) -> dict[str, str]:
        """param -> "sha256:<hex>" of each file param's bytes in the lane's inbox."""
        out = {}
        for name in file_params(fv):
            value = target.params.get(name)
            if isinstance(value, str) and value:
                out[name] = self.file_digest(target.lane, value)
        return out

    def approval(self, fv: FlowVersion, target: Target,
                 files: Mapping[str, str] | None = None) -> tuple[str, dict[str, Any]]:
        """(the node-written summary, the content an approval binds to) for one run."""
        files = dict(self.digests(fv, target) if files is None else files)
        shown = FlowService.shown_params(fv)
        bound: dict[str, Any] = {}
        for k, v in sorted(target.caller_params.items()):
            if k in files:
                bound[k] = files[k]
            elif k in shown:
                bound[k] = v
            else:
                bound[k] = self.flows._hidden_digest(v)
        s = target.surface
        content = {"operation": dotted(fv), "contract": binds_to(fv), "surface": s.id,
                   "acts_as": {"kind": s.acts_as.kind, "id": s.acts_as.asset_id}, "params": bound}
        return self.summary(fv, target, files, shown), content

    def summary(self, fv: FlowVersion, target: Target, files: Mapping[str, str], shown: set[str]) -> str:
        """'Post a photo with a caption (instagram.post_photo 1.4.0) as @biza.coffee (account) on
        biz-a/instagram, through biz-a/instagram: caption 'Autumn', image 1 file (sha256 1a2b...)'."""
        meta = operation_meta(fv)
        parts = []
        for k, v in sorted(target.caller_params.items()):
            if k in files:
                parts.append(f"{k} 1 file (sha256 {files[k].removeprefix('sha256:')[:12]})")
            elif k in shown:
                parts.append(f"{k} {clip_text(str(v), 120)!r}")
            else:
                parts.append(f"{k} (hidden)")
        s = target.surface
        return (f"{fv.definition.title or dotted(fv)} ({dotted(fv)} {meta.get('version')}) as {s.acts_as.label()} "
                f"on {s.id}, through {target.lane}: " + (", ".join(parts) or "no params"))
