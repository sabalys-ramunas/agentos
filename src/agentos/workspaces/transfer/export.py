"""Single-file export of one workspace, and reading one back (`agentos workspace export|import`).

One export is one file, `<name>.agentos`: a gzipped tar encrypted with age (a passphrase, or one or
more age public keys; see agentos.core.age), so the stock `age -d` opens it too. Inside, in order:

  agentos-export/format.json                format name and version, the workspace, the source
                                            node and host, created_at, agentos version
  workspace/workspace.json                  the workspace record (null when the workspace only
                                            exists as a lane-id prefix) and, per lane, its spec and
                                            readiness row
  workspace/vault.json                      vault entries bound inside the workspace, values
                                            included: they exist in plaintext only inside the
                                            encrypted stream, never in a file
  workspace/flows.json                      flows on the workspace's lanes, every saved version
  workspace/schedules.json                  their schedules, without webhook tokens and secrets
  workspace/grants.json                     grant definitions (subject, scope, ceiling, expiry),
                                            never tokens
  workspace/proxies.json                    the proxy labels the lanes use, each with a digest of
                                            its endpoint; host, port and credentials only with
                                            with_proxies
  workspace/agenda.json                     format 3: the agenda entries not yet run (draft, proposed,
                                            scheduled), with no approval: approvals stay on this node,
                                            so the importing node brings them in as drafts
  workspace/memory.jsonl                    format 2: the workspace's memory, one record a line
                                            (a header with the node's public key, then entries,
                                            versions with provenance and hashes, the content of
                                            what is not forgotten, tombstones, links)
  workspace/lanes/<name>/profile/...        the lane's browser profile (cookies, logins, local
                                            storage, IndexedDB), without caches and lock files
  workspace/lanes/<name>/fingerprint.json   the pinned fingerprint, when the lane has one
  agentos-export/manifest.json              last: every member with its size and SHA-256, the
                                            counts, each lane and how it was copied, notes

Consistency: a browser writes its profile while it runs, so each lane is copied while its browser
is down. A warm lane is paused through the daemon (the ops socket from the CLI, the lane manager
from the panel), copied straight into the encrypted stream, and warmed again. With nothing to pause
it, a warm lane refuses the export; a browser that starts during the copy fails it. No copy of a
profile or of a secret is ever staged on disk: the only file written is the encrypted export,
under a `.partial-` name until it is complete.

Reading (read_export) streams the archive once into a private staging directory: profile files
go to disk there, the JSON members stay in memory. Nothing is used until every file matched the
manifest; a wrong passphrase, a damaged, truncated or altered file raises BadExport and leaves
nothing behind. Applying an import is agentos.workspaces.transfer.import_.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import secrets as pysecrets
import shutil
import stat
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from agentos import __version__
from agentos.core import age
from agentos.core.config import NodeConfig, load_proxies
from agentos.core.errors import AgentosError, Conflict, InvalidRequest, NotFound
from agentos.core.models import LaneKind, LaneSpec
from agentos.ops import backup
from agentos.ops.backup import EXCLUDE_PROFILE, SMALL_FILE, LaneCoordinator, NoDaemon
from agentos.ops.layout import PROFILE_LOCKS, LaneDir, fsync_dir
from agentos.ops.system import Host
from agentos.workspaces.models import LANE_NAME_RE, check_workspace_id

FORMAT = "agentos-workspace-export"
FORMAT_VERSION = 3
READ_VERSIONS = (1, 2, 3)   # format 1 has no memory, formats 1 and 2 no agenda
SUFFIX = ".agentos"
META = "agentos-export"
DATA = "workspace"
PARTIAL_PREFIX = ".partial-"
MIN_PASSPHRASE = backup.MIN_PASSPHRASE
MAX_JSON = 64 * 1024 * 1024
JSON_MEMBERS = ("workspace.json", "vault.json", "flows.json", "schedules.json", "grants.json", "proxies.json")
MEMORY_MEMBER = "memory.jsonl"
AGENDA_MEMBER = "agenda.json"
COUNTS = ("lanes", "profile_files", "profile_bytes", "fingerprints", "vault_entries", "flows", "flow_versions",
          "schedules", "grants", "proxies", "memory_entries", "memory_versions", "agenda_entries")
NOT_EXPORTED = [
    "proxy hosts and credentials (labels and an endpoint digest only, unless with_proxies)",
    "grant tokens (bearer secrets): grants travel as definitions and are re-issued on import",
    "webhook tokens and secrets of schedules: the importing node makes new ones",
    "machine secrets (brain subscription tokens, child grant tokens): they belong to the node",
    "runs, step journals, proofs, screenshots and recordings (history stays where it happened)",
    "approvals of agenda entries, and entries that ran or finished: an entry comes in as a draft to propose again",
    "proxy-rule override records and the activity log",
    "browser caches, profile lock files, symlinks and each lane's shots/ and files/ directories",
    "terminal and service lanes (only browser lanes move for now)",
    "the traces memory provenance cites: each version keeps a snapshot of its evidence, the traces stay put",
]
_GLOB = re.compile(r"[*?\[]")


class ExportRefused(Conflict):
    """The export or import cannot go ahead as asked; nothing was changed."""
    code = "export_refused"


class ExportError(AgentosError):
    code = "export_failed"


class BadExport(InvalidRequest):
    """The file cannot be used: wrong passphrase or key, damaged, truncated, altered, not an export."""
    code = "bad_export"


# ---- where the data comes from and goes to -----------------------------------------------

@dataclass
class Sources:
    """One node's stores, as an export reads them and an import writes them. Built by
    `node_sources` (the CLI, from the node's files) or by the panel routes (the daemon's runtime).
    Any store may be None on a node that does not have it."""
    config: NodeConfig
    lanes: Any                          # the node's lane manager: list, get, create, import_profile
    workspaces: Any = None              # agentos.workspaces.store.WorkspaceStore
    vault: Any = None                   # agentos.vault.service.VaultService
    flows: Any = None                   # agentos.runner.flows.FlowService
    scheduler: Any = None               # agentos.runner.schedules.Scheduler
    policy: Any = None                  # agentos.policy.service.PolicyService
    memory: Any = None                  # agentos.memory.service.MemoryService
    agenda: Any = None                  # agentos.agenda.store.AgendaStore
    coordinator: LaneCoordinator = field(default_factory=NoDaemon)
    host: Host = field(default_factory=Host)

    @property
    def home(self) -> Path:
        return Path(self.config.home)

    def close(self) -> None:
        for store in (self.workspaces, getattr(self.flows, "store", None), self.memory, self.policy, self.agenda):
            closer = getattr(store, "close", None)
            if callable(closer):
                with contextlib.suppress(Exception):
                    closer()


def node_sources(cfg: NodeConfig, lanes: Any, *, create: bool, policy: bool = False,
                 host: Host | None = None) -> Sources:
    """The node's own stores, opened from its files, the way the other CLI commands do. With
    create=False (an export) a store whose file does not exist is left out instead of created.
    The policy service is opened only when asked (it creates the node's root key)."""
    from agentos.core.runtime import Runtime
    from agentos.ops.backup import DaemonCoordinator
    from agentos.vault.service import VaultService
    from agentos.workspaces.store import WorkspaceStore

    home = Path(cfg.home)
    host = host or Host()
    src = Sources(config=cfg, lanes=lanes, vault=VaultService(home), coordinator=DaemonCoordinator(home, host),
                  host=host)
    if create or (home / "workspaces.db").exists():
        src.workspaces = WorkspaceStore(home / "workspaces.db")
    if create or (home / "runner.db").exists():
        from agentos.runner.service import build as build_runner

        runner = build_runner(Runtime(), home=home, register=False)
        src.flows, src.scheduler = runner.flows, runner.scheduler
    if policy or (not create and (home / "policy.db").exists()):
        from agentos.policy.service import PolicyService

        src.policy = PolicyService(home, node_config=cfg)
    if create or (home / "memory").is_dir():
        src.memory = _memory_service(home, src.policy, cfg)
    if create or (home / "agenda.db").exists():
        from agentos.agenda.store import DB_NAME, AgendaStore

        src.agenda = AgendaStore(home / DB_NAME)
    return src


def _memory_service(home: Path, policy: Any, cfg: NodeConfig) -> Any:
    """The node's memory over its files; with the policy, owner entries pin their evidence."""
    from agentos.core.runtime import Runtime
    from agentos.memory.ports import MemoryPorts, RuntimePolicyPort
    from agentos.memory.service import MemoryService

    runtime = Runtime(config=cfg)
    if policy is not None:
        runtime.register("policy", policy)
        runtime.register("traces", policy.traces)
    return MemoryService(home, MemoryPorts(policy=RuntimePolicyPort(runtime), node_id=cfg.name))


def runtime_sources(runtime: Any) -> Sources:
    """The daemon's own services, for the panel routes (call it on the daemon's event loop). Never
    close these Sources: the stores belong to the daemon."""
    lanes = runtime.require("lanes")
    runner = runtime.get("runner")
    return Sources(config=runtime.require("config"), lanes=lanes,
                   workspaces=getattr(runtime.get("workspaces"), "store", None), vault=runtime.get("vault"),
                   flows=runtime.get("flows"), scheduler=getattr(runner, "scheduler", None),
                   policy=runtime.get("policy"), memory=runtime.get("memory"),
                   agenda=getattr(runtime.get("agenda"), "store", None),
                   coordinator=ManagerCoordinator(lanes, asyncio.get_running_loop()),
                   host=Host())


class ManagerCoordinator:
    """Pauses lanes through a lane manager living on an event loop (the daemon, for the panel
    route), from the thread that writes the export."""

    def __init__(self, lanes: Any, loop: asyncio.AbstractEventLoop, timeout: float = 120.0):
        self.lanes, self.loop, self.timeout = lanes, loop, timeout
        self._background: set[Any] = set()

    def available(self) -> bool:
        return True

    def warm(self) -> set[str]:
        return {i.spec.id for i in self.lanes.list() if i.state.value in ("warming", "warm", "active")}

    def close(self, lane_id: str) -> None:
        future = asyncio.run_coroutine_threadsafe(self.lanes.close(lane_id), self.loop)
        try:
            future.result(self.timeout)
        except AgentosError as e:
            raise ExportRefused(f"{lane_id} could not be paused for the export: {e.message}") from None

    def open(self, lane_id: str) -> None:
        future = asyncio.run_coroutine_threadsafe(self.lanes.open(lane_id), self.loop)
        self._background.add(future)
        future.add_done_callback(self._background.discard)


# ---- small helpers ---------------------------------------------------------------------

def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def workspace_of(pattern: str) -> str | None:
    """The workspace part of a lane id or pattern, or None when that part is itself a pattern."""
    ws = pattern.split("/", 1)[0]
    return None if _GLOB.search(ws) else ws


def endpoint_digest(proxy: Any) -> str:
    """Identifies a proxy line (host, port and user, where residential proxies keep the session)
    without saying what it is, so an importer can tell whether its label points at the same one."""
    return hashlib.sha256(f"{proxy.host}:{proxy.port}:{proxy.username or ''}".encode()).hexdigest()


def export_name(workspace: str, now: datetime) -> str:
    return f"agentos-{workspace}-{now.strftime('%Y%m%dT%H%M%SZ')}{SUFFIX}"


def check_encryption(passphrase: str | None, recipients: list[str] | None) -> list[str] | None:
    """The recipients in canonical form, after checking that exactly one way to encrypt was given."""
    if recipients:
        if passphrase:
            raise InvalidRequest("encrypt with a passphrase or to age recipients, not both")
        for r in recipients:
            try:
                age.parse_recipient(r)
            except age.BadKey as e:
                raise InvalidRequest(f"{r.strip()[:24]!r} is not an age recipient (age1..., from age-keygen): "
                                     f"{e}") from None
        return [r.strip() for r in recipients]
    if not passphrase:
        raise InvalidRequest("an export is always encrypted: give a passphrase or an age recipient (age1...)")
    if len(passphrase) < MIN_PASSPHRASE:
        raise InvalidRequest(f"the export passphrase must be at least {MIN_PASSPHRASE} characters")
    return None


def _secret_text(value: Any) -> str | None:
    return None if value is None else value.reveal()


def _jsonable_dt(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


# ---- collecting what goes into the file ----------------------------------------------------

@dataclass
class Bundle:
    header: dict[str, Any]
    workspace: dict[str, Any]
    lanes: list[tuple[str, LaneDir]]           # (lane name, its directory) for every browser lane
    vault: list[dict[str, Any]]
    flows: list[dict[str, Any]]
    schedules: list[dict[str, Any]]
    grants: list[dict[str, Any]]
    proxies: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)
    not_exported: list[dict[str, str]] = field(default_factory=list)
    memory: list[dict[str, Any]] = field(default_factory=list)
    agenda: list[dict[str, Any]] = field(default_factory=list)


async def collect(src: Sources, workspace: str, *, with_proxies: bool = False, now: datetime | None = None,
                  encryption: str = "passphrase") -> Bundle:
    """Read everything one workspace has on this node. Raises NotFound for an unknown workspace and
    ExportRefused when the vault holding its entries is sealed."""
    check_workspace_id(workspace)
    cfg = src.config
    infos = [i for i in src.lanes.list() if i.spec.workspace == workspace]
    record = src.workspaces.get_workspace(workspace) if src.workspaces is not None else None
    if record is None and not infos:
        raise NotFound(f"no workspace {workspace!r} on this node")
    notes: list[str] = []
    not_exported: list[dict[str, str]] = []
    lanes_json: dict[str, Any] = {}
    lane_dirs: list[tuple[str, LaneDir]] = []
    for info in sorted(infos, key=lambda i: i.spec.id):
        spec = info.spec
        if spec.kind is not LaneKind.BROWSER:
            not_exported.append({"lane": spec.id, "why": f"{spec.kind.value} lanes are not exported yet"})
            continue
        row = src.workspaces.lane_row(spec.id) if src.workspaces is not None else None
        readiness = {k: v for k, v in (row or {}).items() if k not in ("id", "workspace")} or None
        lanes_json[spec.name] = {"spec": spec.model_dump(mode="json"), "readiness": readiness}
        lane_dirs.append((spec.name, LaneDir(spec.id, cfg.lane_dir(spec.id))))

    vault = await _collect_vault(src, workspace, notes)
    flows, flow_ids = _collect_flows(src, workspace)
    schedules = _collect_schedules(src, workspace, flows, flow_ids, notes)
    agenda = _collect_agenda(src, workspace, flows, flow_ids, notes, _now(now))
    grants = _collect_grants(src, workspace, notes)
    labels = {lane["spec"].get("proxy_label") for lane in lanes_json.values()}
    labels.add((record or {}).get("default_proxy"))
    proxies = _collect_proxies(cfg, {label for label in labels if label}, with_proxies)
    memory = _collect_memory(src, workspace, notes)
    header = {"format": FORMAT, "version": FORMAT_VERSION, "agentos_version": __version__,
              "created_at": _now(now).isoformat(timespec="seconds"), "workspace": workspace,
              "source": {"node": cfg.name, "hostname": src.host.hostname()}, "encryption": encryption,
              "with_proxies": with_proxies}
    return Bundle(header=header, workspace={"id": workspace, "record": record, "lanes": lanes_json}, lanes=lane_dirs,
                  vault=vault, flows=flows, schedules=schedules, grants=grants, proxies=proxies, notes=notes,
                  not_exported=not_exported, memory=memory, agenda=agenda)


def node_pubkey(home: Path) -> str | None:
    """The node's policy public key, so signatures made on this node still verify after a move."""
    path = Path(home) / "secrets" / "policy.pub"
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _collect_memory(src: Sources, workspace: str, notes: list[str]) -> list[dict[str, Any]]:
    if src.memory is None:
        return []
    records = src.memory.export_records(workspace, node_pubkey=node_pubkey(src.home))
    if records and not records[0].get("chain_ok"):
        raise ExportRefused(f"the memory of {workspace} does not verify on this node (agentos memory verify "
                            f"{workspace}); fix or restore it before exporting")
    return records


async def _collect_vault(src: Sources, workspace: str, notes: list[str]) -> list[dict[str, Any]]:
    from agentos.vault.errors import VaultError, VaultSealed

    vault = src.vault
    try:
        if vault is None or vault.store.header() is None:
            return []
        rows = await vault.export_entries(workspace)
    except VaultSealed:
        raise ExportRefused("the vault is sealed, so the workspace's vault entries cannot be exported. Unseal it "
                            "(agentos vault unseal) and export again") from None
    except VaultError as e:
        raise ExportError(f"the vault cannot be read ({e.code}): {e.message}") from None
    prefix = f"{workspace}/"
    names = {meta.id: meta.name for meta, _ in rows}
    out = []
    for meta, values in rows:
        inside = [p for p in meta.lanes if p.startswith(prefix)]
        if len(inside) < len(meta.lanes):
            notes.append(f"vault entry {meta.name!r} is also bound outside {workspace}; the export carries only its "
                         "bindings inside it")
        config = json.loads(json.dumps(meta.config))
        catcher = config.get("catcher")
        if isinstance(catcher, dict) and catcher.get("account"):
            account = catcher["account"]
            if account in names:
                catcher["account"] = names[account]  # ids change on import; names travel
            elif account not in names.values():
                notes.append(f"vault entry {meta.name!r} catches codes from mailbox entry {account!r}, which is not "
                             f"bound inside {workspace} and is not exported")
        out.append({"name": meta.name, "kind": meta.kind.value, "domain": meta.domain, "hosts": list(meta.hosts),
                    "lanes": inside, "config": config, "created_at": meta.created_at.isoformat(),
                    "username": _secret_text(values.get("username")), "secret": _secret_text(values.get("secret")),
                    "totp": _secret_text(values.get("totp"))})
    return out


def _flow_record(fv: Any, versions: list[Any], *, shared: bool) -> dict[str, Any]:
    return {"id": fv.id, "head": fv.version, "lane": fv.definition.lane, "shared": shared,
            "versions": [{"version": v.version, "definition": v.definition.canonical(),
                          "created_at": v.created_at.isoformat(), "created_by": v.created_by,
                          "saved_by": v.saved_by} for v in versions]}


def _collect_flows(src: Sources, workspace: str) -> tuple[list[dict[str, Any]], set[str]]:
    if src.flows is None:
        return [], set()
    out, ids = [], set()
    for fv in src.flows.list():
        if workspace_of(fv.definition.lane) == workspace:
            out.append(_flow_record(fv, src.flows.versions(fv.id), shared=False))
            ids.add(fv.id)
    return out, ids


def _collect_schedules(src: Sources, workspace: str, flows: list[dict[str, Any]], flow_ids: set[str],
                       notes: list[str]) -> list[dict[str, Any]]:
    if src.scheduler is None:
        return []
    out = []
    for s in src.scheduler.list():
        if not (s.lane or "").startswith(f"{workspace}/"):
            continue
        if s.every:  # it reads for a workflow's site_change trigger, and alone would read for nothing
            notes.append(f"schedule {s.id} reads for a workflow's watch; workflows are not exported, so it is left out")
            continue
        if s.route_id:  # triggers (agentos.events routes) are not part of an export yet
            notes.append(f"schedule {s.id} fires trigger {s.route_id}; triggers are not exported, so it is left out")
            continue
        if s.flow_id not in flow_ids and src.flows is not None:
            fv = src.flows.store.get_flow(s.flow_id)
            if fv is not None:  # a flow on a lane pattern across workspaces, run here by this schedule
                flows.append(_flow_record(fv, src.flows.versions(fv.id), shared=True))
                flow_ids.add(fv.id)
                notes.append(f"flow {fv.id} runs on lanes matching {fv.definition.lane!r} (not only this "
                             f"workspace); it is exported because schedule {s.id} runs it here")
        data = s.model_dump(mode="json")
        data.pop("state", None)
        if data.get("webhook"):
            data["webhook"] = {**data["webhook"], "token": "", "secret": ""}
        out.append(data)
    return out


# what an agenda entry keeps on its node: its approval, its run and what that run left
AGENDA_LOCAL = ("approval", "approved_hash", "run_id", "event_id", "retry_at", "proof", "history", "reason",
                "finished_at")


def _collect_agenda(src: Sources, workspace: str, flows: list[dict[str, Any]], flow_ids: set[str],
                    notes: list[str], now: datetime) -> list[dict[str, Any]]:
    """The workspace's agenda entries that have not run yet (draft, proposed, scheduled), without
    their approval: an approval binds to this node's grants and is used up here, so the importing
    node brings each one in as a draft. Entries that ran or finished are history and stay here, a
    window that already closed is left out, and an entry that fires a trigger is left out like a
    trigger's schedule (triggers are not exported)."""
    if src.agenda is None:
        return []
    from agentos.agenda.models import ActionKind, EntryState

    out = []
    for e in src.agenda.list(workspace=workspace, states=[EntryState.DRAFT, EntryState.PROPOSED,
                                                          EntryState.SCHEDULED]):
        if e.when.not_after is not None and e.when.not_after <= now:
            notes.append(f"agenda entry {e.id} ({e.title}) is left out: its window has closed")
            continue
        if e.action.kind == ActionKind.ROUTE:
            notes.append(f"agenda entry {e.id} ({e.title}) fires trigger {e.action.target}; triggers are not "
                         "exported, so it is left out")
            continue
        if e.action.kind == ActionKind.FLOW and e.action.target not in flow_ids and src.flows is not None:
            fv = src.flows.store.get_flow(e.action.target)
            if fv is not None:  # a flow on a lane pattern across workspaces, planned here by this entry
                flows.append(_flow_record(fv, src.flows.versions(fv.id), shared=True))
                flow_ids.add(fv.id)
                notes.append(f"flow {fv.id} runs on lanes matching {fv.definition.lane!r} (not only this "
                             f"workspace); it is exported because agenda entry {e.id} plans it here")
        data = e.model_dump(mode="json")
        for key in AGENDA_LOCAL:
            data.pop(key, None)
        out.append(data)
    if out:
        notes.append(f"{len(out)} agenda entr{'y' if len(out) == 1 else 'ies'} travel without their approvals; the "
                     "importing node brings them in as drafts to propose again")
    return out


def _collect_grants(src: Sources, workspace: str, notes: list[str]) -> list[dict[str, Any]]:
    if src.policy is None:
        return []
    out, children = [], 0
    roots: set[str] = set()
    records = src.policy.list_grants()
    for r in records:
        scope = r.get("scope") or {}
        blocks = scope.get("blocks") or []
        if r.get("depth") != 0 or not blocks:
            continue
        block = blocks[0]
        patterns = [f"{w}/*" for w in block.get("workspaces") or []] + list(block.get("lanes") or [])
        if not patterns or any(workspace_of(p) != workspace for p in patterns):
            continue
        roots.add(r["id"])
        out.append({"id": r["id"], "subject": block.get("sub"), "issued_at": r.get("issued_at"),
                    "expires_at": r.get("expires_at"),
                    **{k: block.get(k) for k in ("workspaces", "lanes", "domains", "ceiling", "specials", "budget",
                                                 "nbf", "exp", "depth", "fanout", "pins", "egress", "admin", "note")}})
    for r in records:
        if r.get("depth", 0) > 0 and r.get("root_id") in roots:
            children += 1
    if children:
        notes.append(f"{children} narrowed grant(s) made from these grants are not exported; their holders narrow "
                     "the re-issued grant again")
    return out


def _collect_proxies(cfg: NodeConfig, labels: set[str], with_proxies: bool) -> list[dict[str, Any]]:
    try:
        configured = load_proxies(cfg)
    except ValueError as e:
        raise ExportError(str(e)) from None
    out = []
    for label in sorted(labels):
        p = configured.get(label)
        entry: dict[str, Any] = {"label": label, "endpoint": endpoint_digest(p) if p else None,
                                 "on_source": p is not None}
        if with_proxies and p is not None:
            entry.update(host=p.host, port=p.port, username=p.username, password=p.password)
        out.append(entry)
    return out


# ---- writing --------------------------------------------------------------------------------

class _Writer:
    def __init__(self, tar: tarfile.TarFile, now: float):
        self.tar, self.now = tar, now
        self.files: list[dict[str, Any]] = []
        self.dirs: list[str] = []
        self.total = 0

    def _info(self, name: str, size: int, *, directory: bool = False, mtime: float | None = None) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.size = 0 if directory else size
        info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
        info.mode = 0o700 if directory else 0o600
        info.mtime = int(mtime if mtime is not None else self.now)
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    def add_dir(self, rel: str) -> None:
        self.tar.addfile(self._info(f"{DATA}/{rel}", 0, directory=True))
        self.dirs.append(rel)

    def add_bytes(self, rel: str, data: bytes, kind: str, mtime: float | None = None) -> None:
        self.tar.addfile(self._info(f"{DATA}/{rel}", len(data), mtime=mtime), io.BytesIO(data))
        self.files.append({"path": rel, "kind": kind, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.total += len(data)

    def add_json(self, rel: str, payload: Any) -> None:
        self.add_bytes(rel, json.dumps(payload, indent=1, sort_keys=True, default=_jsonable_dt).encode(), "data")

    def add_file(self, rel: str, path: Path, kind: str) -> None:
        st = os.stat(path, follow_symlinks=False)
        if st.st_size <= SMALL_FILE:
            with open(path, "rb") as f:
                self.add_bytes(rel, f.read(), kind, st.st_mtime)
            return
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            reader = backup._ExactReader(f, st.st_size, digest, rel)
            self.tar.addfile(self._info(f"{DATA}/{rel}", st.st_size, mtime=st.st_mtime), reader)
        self.files.append({"path": rel, "kind": kind, "size": st.st_size, "sha256": digest.hexdigest()})
        self.total += st.st_size

    def add_meta(self, name: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, indent=1, sort_keys=True).encode()
        self.tar.addfile(self._info(f"{META}/{name}", len(data)), io.BytesIO(data))


def _profile_entries(profile: Path, skipped: list[str], rel_base: str) -> Iterator[tuple[str, Path, bool]]:
    """(path relative to the profile, path, is_dir) for what an export keeps of a profile."""
    for dirpath, dirnames, filenames in os.walk(profile, followlinks=False):
        here = Path(dirpath)
        rel = here.relative_to(profile)
        keep = []
        for d in sorted(dirnames):
            p = here / d
            if p.is_symlink():
                skipped.append(f"{rel_base}/{(rel / d).as_posix()} (symlink)")
                continue
            if not rel.parts and d in EXCLUDE_PROFILE:
                continue
            keep.append(d)
            yield (rel / d).as_posix(), p, True
        dirnames[:] = keep
        for name in sorted(filenames):
            p = here / name
            if not rel.parts and name in PROFILE_LOCKS:
                continue
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                skipped.append(f"{rel_base}/{(rel / name).as_posix()} (not a regular file)")
                continue
            yield (rel / name).as_posix(), p, False


def _precheck_lanes(src: Sources, lanes: list[tuple[str, LaneDir]]) -> set[str]:
    """Refuse before writing anything when a warm lane cannot be paused. Returns the warm lanes."""
    available = src.coordinator.available()
    warm = src.coordinator.warm() if available else set()
    for _, lane in lanes:
        if (lane.id in warm or src.host.browser_pids(lane.profile)) and not available:
            raise ExportRefused(f"lane {lane.id} is warm and no daemon answers on the ops socket to pause it. Close "
                                "it (lane_close), or stop agentosd, then export again")
    return warm


def _copy_lane(w: _Writer, name: str, lane: LaneDir, *, src: Sources, warm: set[str], skipped: list[str],
               warnings: list[str], fail_after: Callable[[str], None] | None) -> dict[str, Any]:
    host, coordinator = src.host, src.coordinator
    was_warm = lane.id in warm
    paused = False
    if was_warm or host.browser_pids(lane.profile):
        coordinator.close(lane.id)
        paused = True
        if not backup._wait_no_browser(host, lane.profile):
            raise ExportError(f"{lane.id}: its browser did not stop within {backup.PAUSE_WAIT:.0f}s after the pause")
    base = f"lanes/{name}"
    before = (len(w.files), w.total)
    try:
        w.add_dir(base)
        w.add_dir(f"{base}/profile")
        if lane.profile.is_dir() and not lane.profile.is_symlink():
            for rel, path, is_dir in _profile_entries(lane.profile, skipped, f"lanes/{lane.id}/profile"):
                if is_dir:
                    w.add_dir(f"{base}/profile/{rel}")
                else:
                    w.add_file(f"{base}/profile/{rel}", path, "profile")
                    if fail_after:
                        fail_after(f"{base}/profile/{rel}")
        pinned = lane.fingerprint.is_file() and not lane.fingerprint.is_symlink()
        if pinned:
            w.add_file(f"{base}/fingerprint.json", lane.fingerprint, "fingerprint")
        if host.browser_pids(lane.profile):
            raise ExportRefused(f"lane {lane.id} was opened again while it was being copied (an agent warmed it). "
                                "Export again, or stop agents on it first")
    except backup.BackupError as e:
        raise ExportError(e.message) from None
    finally:
        if paused and was_warm:
            try:
                coordinator.open(lane.id)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{lane.id} was paused for the export and did not warm again: {type(e).__name__}")
    files = [f for f in w.files[before[0]:] if f["kind"] == "profile"]
    return {"id": lane.id, "name": name, "copied": "paused" if paused else "cold", "was_warm": was_warm,
            "fingerprint": pinned, "files": len(files), "bytes": sum(f["size"] for f in files)}


@dataclass
class ExportResult:
    path: Path
    size: int
    workspace: str
    counts: dict[str, int]
    lanes: list[dict[str, Any]]
    notes: list[str]
    warnings: list[str]
    skipped: list[str]
    not_exported: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "size": self.size, "workspace": self.workspace, "counts": self.counts,
                "lanes": self.lanes, "notes": self.notes, "warnings": self.warnings, "skipped": self.skipped,
                "not_exported": self.not_exported, "never_exported": NOT_EXPORTED}


def _counts(bundle: Bundle, w: _Writer) -> dict[str, int]:
    profile = [f for f in w.files if f["kind"] == "profile"]
    return {"lanes": len(bundle.workspace["lanes"]), "profile_files": len(profile),
            "profile_bytes": sum(f["size"] for f in profile),
            "fingerprints": sum(1 for f in w.files if f["kind"] == "fingerprint"),
            "vault_entries": len(bundle.vault), "flows": len(bundle.flows),
            "flow_versions": sum(len(f["versions"]) for f in bundle.flows), "schedules": len(bundle.schedules),
            "grants": len(bundle.grants), "proxies": len(bundle.proxies), **memory_counts(bundle.memory),
            **({"agenda_entries": len(bundle.agenda)} if bundle.header["version"] >= 3 else {})}


def memory_bytes(records: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(r, sort_keys=True, separators=(",", ":"), default=_jsonable_dt) + "\n"
                   for r in records).encode()


def memory_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {"memory_entries": sum(1 for r in records if r.get("record") == "entry"),
            "memory_versions": sum(1 for r in records if r.get("record") == "version")}


def write_export(src: Sources, bundle: Bundle, dest: Path, *, passphrase: str | None = None,
                 recipients: list[str] | None = None, log_n: int = age.DEFAULT_LOG_N, now: datetime | None = None,
                 fail_after: Callable[[str], None] | None = None) -> ExportResult:
    """Write the bundle and the lanes' profiles into one encrypted file at `dest` (sync: lanes are
    paused through src.coordinator). `fail_after` is a test hook called after each profile file."""
    dest = Path(dest)
    if dest.exists() or dest.is_symlink():
        raise ExportRefused(f"{dest} already exists; pick another name (an export never overwrites a file)")
    if not dest.parent.is_dir():
        raise ExportRefused(f"{dest.parent} is not a directory")
    warm = _precheck_lanes(src, bundle.lanes)
    partial = dest.with_name(f"{PARTIAL_PREFIX}{dest.name}")
    partial.unlink(missing_ok=True)
    skipped: list[str] = []
    warnings: list[str] = []
    lanes_out: list[dict[str, Any]] = []
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as raw:
            enc = age.Encryptor(raw, passphrase, recipients=recipients, log_n=log_n)
            with tarfile.open(fileobj=enc, mode="w|gz", compresslevel=6) as tar:
                w = _Writer(tar, _now(now).timestamp())
                w.add_meta("format.json", bundle.header)
                w.add_json("workspace.json", bundle.workspace)
                w.add_json("vault.json", bundle.vault)
                w.add_json("flows.json", bundle.flows)
                w.add_json("schedules.json", bundle.schedules)
                w.add_json("grants.json", bundle.grants)
                w.add_json("proxies.json", bundle.proxies)
                if bundle.header["version"] >= 2:
                    w.add_bytes(MEMORY_MEMBER, memory_bytes(bundle.memory), "data")
                if bundle.header["version"] >= 3:
                    w.add_json(AGENDA_MEMBER, bundle.agenda)
                if bundle.lanes:
                    w.add_dir("lanes")
                for name, lane in bundle.lanes:
                    lanes_out.append(_copy_lane(w, name, lane, src=src, warm=warm, skipped=skipped,
                                                warnings=warnings, fail_after=fail_after))
                counts = _counts(bundle, w)
                w.add_meta("manifest.json", {**bundle.header, "files": w.files, "dirs": w.dirs, "counts": counts,
                                             "lanes": lanes_out, "notes": bundle.notes, "skipped": skipped,
                                             "not_exported": bundle.not_exported, "never_exported": NOT_EXPORTED,
                                             "excluded_profile": sorted(EXCLUDE_PROFILE)})
            enc.close()
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(partial, dest)
        fsync_dir(dest.parent)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return ExportResult(path=dest, size=dest.stat().st_size, workspace=bundle.header["workspace"], counts=counts,
                        lanes=lanes_out, notes=bundle.notes, warnings=warnings, skipped=skipped,
                        not_exported=bundle.not_exported)


async def export_workspace(src: Sources, workspace: str, dest: Path, *, passphrase: str | None = None,
                           recipients: list[str] | None = None, with_proxies: bool = False,
                           log_n: int = age.DEFAULT_LOG_N, now: datetime | None = None,
                           fail_after: Callable[[str], None] | None = None) -> ExportResult:
    """Export one workspace into one encrypted file. Reads the stores here, then writes the file in
    a worker thread (lanes are paused from there, so src.coordinator must be thread-safe)."""
    recipients = check_encryption(passphrase, recipients)
    bundle = await collect(src, workspace, with_proxies=with_proxies, now=now,
                           encryption="recipients" if recipients else "passphrase")
    return await asyncio.to_thread(write_export, src, bundle, dest, passphrase=None if recipients else passphrase,
                                   recipients=recipients, log_n=log_n, now=now, fail_after=fail_after)


# ---- reading --------------------------------------------------------------------------------

@dataclass
class ExportContents:
    """A verified export: its JSON in memory, its profiles in `staging` (lanes/<name>/...)."""
    header: dict[str, Any]
    manifest: dict[str, Any]
    workspace: dict[str, Any]
    vault: list[dict[str, Any]]
    flows: list[dict[str, Any]]
    schedules: list[dict[str, Any]]
    grants: list[dict[str, Any]]
    proxies: list[dict[str, Any]]
    staging: Path
    files: int
    bytes: int
    memory: list[dict[str, Any]] = field(default_factory=list)   # format 2; empty for format 1
    agenda: list[dict[str, Any]] = field(default_factory=list)   # format 3; empty before

    @property
    def workspace_id(self) -> str:
        return self.header["workspace"]

    @property
    def lane_names(self) -> list[str]:
        return sorted(self.workspace.get("lanes", {}))

    def profile_dir(self, name: str) -> Path:
        return self.staging / "lanes" / name / "profile"

    def fingerprint(self, name: str) -> Path | None:
        path = self.staging / "lanes" / name / "fingerprint.json"
        return path if path.is_file() else None

    def summary(self) -> dict[str, Any]:
        return {"workspace": self.workspace_id, "created_at": self.header.get("created_at"),
                "source": self.header.get("source"), "agentos_version": self.header.get("agentos_version"),
                "counts": self.manifest.get("counts", {}), "lanes": self.manifest.get("lanes", []),
                "notes": self.manifest.get("notes", []), "not_exported": self.manifest.get("not_exported", [])}

    def discard(self) -> None:
        shutil.rmtree(self.staging, ignore_errors=True)


def _safe_rel(name: str) -> str:
    if not name.startswith(f"{DATA}/"):
        raise BadExport(f"unexpected member {name[:80]!r} in the export")
    rel = PurePosixPath(name[len(DATA) + 1:])
    if rel.is_absolute() or not rel.parts or any(p in ("", ".", "..") for p in rel.parts) or "\\" in name:
        raise BadExport(f"unsafe path {name[:80]!r} in the export")
    return rel.as_posix()


def _member_kind(rel: str, *, directory: bool) -> str:
    """json, profile or fingerprint (files), dir (directories); anything else is refused."""
    parts = rel.split("/")
    if not directory and len(parts) == 1 and parts[0] in (*JSON_MEMBERS, MEMORY_MEMBER, AGENDA_MEMBER):
        return "json"
    if parts[0] == "lanes":
        if len(parts) == 1 and directory:
            return "dir"
        if not LANE_NAME_RE.fullmatch(parts[1]):
            raise BadExport(f"lane name {parts[1][:60]!r} in the export is not valid")
        if len(parts) == 2 and directory:
            return "dir"
        if len(parts) == 3 and parts[2] == "fingerprint.json" and not directory:
            return "fingerprint"
        if parts[2] == "profile" and (directory or len(parts) > 3):
            return "dir" if directory else "profile"
    raise BadExport(f"unexpected member {rel[:80]!r} in the export")


def _hash_to(f: BinaryIO, target: Path, mtime: int) -> str:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    digest = hashlib.sha256()
    with os.fdopen(fd, "wb") as out:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
            out.write(chunk)
    os.utime(target, (mtime, mtime))
    return digest.hexdigest()


def _read_meta(tar: tarfile.TarFile, member: tarfile.TarInfo) -> dict[str, Any]:
    if not member.isreg() or member.size > MAX_JSON:
        raise BadExport(f"{member.name} is malformed")
    f = tar.extractfile(member)
    assert f is not None
    try:
        data = json.loads(f.read())
    except ValueError:
        raise BadExport(f"{member.name} is not valid JSON") from None
    if not isinstance(data, dict):
        raise BadExport(f"{member.name} is malformed")
    return data


def _walk(path: Path, staging: Path, passphrase: str | None,
          identities: list[bytes] | None) -> tuple[dict, dict, dict[str, bytes], dict[str, tuple[int, str]], set[str]]:
    header: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    blobs: dict[str, bytes] = {}
    seen: dict[str, tuple[int, str]] = {}
    dirs: set[str] = set()
    try:
        raw = path.open("rb")
    except OSError as e:
        raise BadExport(f"cannot open {path}: {e.strerror or e}") from None
    with raw:
        try:
            dec = age.Decryptor(raw, passphrase, identities=identities)
            with tarfile.open(fileobj=dec, mode="r|gz") as tar:
                for member in tar:
                    if manifest is not None:
                        raise BadExport("the export has data after its manifest")
                    if header is None:
                        if member.name != f"{META}/format.json":
                            raise BadExport("this is not an agentos workspace export (no format.json first)")
                        header = _read_meta(tar, member)
                        if header.get("format") != FORMAT:
                            raise BadExport("this is not an agentos workspace export")
                        if header.get("version") not in READ_VERSIONS:
                            raise BadExport(f"export format {header.get('version')} is not supported by agentos "
                                            f"{__version__}; import it with a newer agentos")
                        continue
                    if member.name == f"{META}/manifest.json":
                        manifest = _read_meta(tar, member)
                        continue
                    rel = _safe_rel(member.name)
                    if member.isdir():
                        _member_kind(rel, directory=True)
                        dirs.add(rel)
                        (staging / rel).mkdir(mode=0o700, parents=True, exist_ok=True)
                        continue
                    if not member.isreg():
                        raise BadExport(f"{member.name[:80]} is not a regular file or directory")
                    kind = _member_kind(rel, directory=False)
                    if rel in seen:
                        raise BadExport(f"{rel} appears twice in the export")
                    f = tar.extractfile(member)
                    assert f is not None
                    if kind == "json":
                        if member.size > MAX_JSON:
                            raise BadExport(f"{rel} is too large")
                        data = f.read()  # held in memory only: vault.json carries secrets
                        blobs[rel] = data
                        seen[rel] = (len(data), hashlib.sha256(data).hexdigest())
                        continue
                    target = staging / rel
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    seen[rel] = (member.size, _hash_to(f, target, member.mtime))
        except age.NoIdentity as e:
            raise BadExport(str(e)) from None
        except age.WrongPassphrase:
            raise BadExport("wrong passphrase") from None
        except age.TruncatedArchive:
            raise BadExport(f"{path.name} is truncated (an interrupted export or copy); export it again") from None
        except age.NotAgeFile:
            raise BadExport(f"{path.name} is not an encrypted agentos export") from None
        except age.CorruptArchive as e:
            raise BadExport(f"{path.name}: {e}; it was damaged or altered") from None
        except (tarfile.TarError, EOFError) as e:
            raise BadExport(f"{path.name} is damaged ({type(e).__name__})") from None
        except OSError as e:
            raise ExportError(f"cannot stage the export: {e.strerror or e}") from None
    if header is None or manifest is None:
        raise BadExport(f"{path.name} is incomplete (no manifest); export it again")
    return header, manifest, blobs, seen, dirs


def _load_json(blobs: dict[str, bytes], name: str, kind: type) -> Any:
    try:
        data = json.loads(blobs[name])
    except KeyError:
        raise BadExport(f"the export has no {name}") from None
    except ValueError:
        raise BadExport(f"{name} in the export is not valid JSON") from None
    if not isinstance(data, kind):
        raise BadExport(f"{name} in the export is malformed")
    return data


def _load_memory(blobs: dict[str, bytes], header: dict[str, Any], workspace_id: str) -> list[dict[str, Any]]:
    """memory.jsonl: required in format 2, absent in format 1."""
    if header.get("version", 1) < 2:
        if MEMORY_MEMBER in blobs:
            raise BadExport(f"a format 1 export has no {MEMORY_MEMBER}")
        return []
    raw = blobs.get(MEMORY_MEMBER)
    if raw is None:
        raise BadExport(f"the export has no {MEMORY_MEMBER}")
    out: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            raise BadExport(f"{MEMORY_MEMBER} in the export is not valid JSON lines") from None
        if not isinstance(rec, dict) or not isinstance(rec.get("record"), str):
            raise BadExport(f"{MEMORY_MEMBER} in the export is malformed")
        out.append(rec)
    if out and (out[0].get("record") != "memory" or out[0].get("workspace") != workspace_id):
        raise BadExport(f"{MEMORY_MEMBER} does not match the export's workspace")
    return out


def _load_agenda(blobs: dict[str, bytes], header: dict[str, Any], workspace_id: str) -> list[dict[str, Any]]:
    """agenda.json: required in format 3, absent before."""
    if header.get("version", 1) < 3:
        if AGENDA_MEMBER in blobs:
            raise BadExport(f"a format {header.get('version', 1)} export has no {AGENDA_MEMBER}")
        return []
    entries = _load_json(blobs, AGENDA_MEMBER, list)
    if any(not isinstance(e, dict) or e.get("workspace") != workspace_id for e in entries):
        raise BadExport(f"{AGENDA_MEMBER} does not match the export's workspace")
    return entries


def _check_lanes(workspace_id: str, data: dict[str, Any], dirs: set[str]) -> None:
    lanes = data.get("lanes")
    if data.get("id") != workspace_id or not isinstance(lanes, dict):
        raise BadExport("workspace.json does not match the export's header")
    record = data.get("record")
    if record is not None and (not isinstance(record, dict) or record.get("id") != workspace_id):
        raise BadExport("the workspace record in the export is malformed")
    for name, lane in lanes.items():
        if not isinstance(name, str) or not LANE_NAME_RE.fullmatch(name) or not isinstance(lane, dict):
            raise BadExport("a lane in workspace.json is malformed")
        try:
            spec = LaneSpec.model_validate(lane.get("spec"))
        except ValueError:
            raise BadExport(f"the spec of lane {name} in the export is malformed") from None
        if spec.id != f"{workspace_id}/{name}":
            raise BadExport(f"lane {name} in the export names another lane ({spec.id})")
        if f"lanes/{name}/profile" not in dirs:
            raise BadExport(f"the export has no profile for lane {name}")
        readiness = lane.get("readiness")
        if readiness is not None and not isinstance(readiness, dict):
            raise BadExport(f"the readiness of lane {name} in the export is malformed")


def read_export(path: Path, staging: Path, *, passphrase: str | None = None,
                identities: list[bytes] | None = None) -> ExportContents:
    """Decrypt, unpack and verify an export. `staging` must not exist yet; it is created 700 and
    holds the profiles afterwards. On any problem it is removed again and BadExport is raised."""
    path, staging = Path(path), Path(staging)
    staging.mkdir(mode=0o700, parents=False)
    try:
        header, manifest, blobs, seen, dirs = _walk(path, staging, passphrase, identities)
        listed = {}
        for f in manifest.get("files", []):
            if not isinstance(f, dict) or not isinstance(f.get("path"), str):
                raise BadExport("the manifest is malformed")
            listed[f["path"]] = (f.get("size"), f.get("sha256"))
        if set(listed) != set(seen):
            missing = sorted(set(listed) - set(seen))[:3]
            extra = sorted(set(seen) - set(listed))[:3]
            raise BadExport(f"the manifest does not match the export (missing {missing}, unlisted {extra})")
        bad = [rel for rel, got in seen.items() if got != listed[rel]]
        if bad:
            raise BadExport(f"checksum mismatch for {', '.join(bad[:3])}; the export was altered or damaged")
        if set(manifest.get("dirs", [])) != dirs:
            raise BadExport("the manifest's directories do not match the export")
        if any(manifest.get(k) != header.get(k) for k in ("format", "version", "workspace", "created_at", "source")):
            raise BadExport("the manifest does not match the export's header")
        workspace_id = header.get("workspace")
        try:
            check_workspace_id(workspace_id)
        except InvalidRequest:
            raise BadExport("the export names an invalid workspace") from None
        workspace = _load_json(blobs, "workspace.json", dict)
        _check_lanes(workspace_id, workspace, dirs)
        names = set(workspace["lanes"])
        strays = sorted({rel.split("/")[1] for rel in [*seen, *dirs] if rel.startswith("lanes/")} - names)
        if strays or {lane.get("name") for lane in manifest.get("lanes", [])} != names:
            raise BadExport("the lanes in the export do not match its workspace.json")
        parts = {name.removesuffix(".json"): _load_json(blobs, name, list) for name in JSON_MEMBERS[1:]}
        memory = _load_memory(blobs, header, workspace_id)
        agenda = _load_agenda(blobs, header, workspace_id)
        blobs.clear()
        counts = manifest.get("counts")
        if not isinstance(counts, dict):
            raise BadExport("the manifest has no counts")
        actual = {"lanes": len(workspace["lanes"]), "vault_entries": len(parts["vault"]), "flows": len(parts["flows"]),
                  "schedules": len(parts["schedules"]), "grants": len(parts["grants"]),
                  "proxies": len(parts["proxies"]),
                  "flow_versions": sum(len(f.get("versions") or []) for f in parts["flows"] if isinstance(f, dict)),
                  "profile_bytes": sum(size for rel, (size, _) in seen.items() if rel.split("/")[2:3] == ["profile"]),
                  "profile_files": sum(1 for rel in seen if rel.split("/")[2:3] == ["profile"]),
                  "fingerprints": sum(1 for rel in seen if rel.endswith("/fingerprint.json"))}
        if header.get("version", 1) >= 2:
            actual.update(memory_counts(memory))
        if header.get("version", 1) >= 3:
            actual["agenda_entries"] = len(agenda)
        wrong = [k for k, v in actual.items() if counts.get(k) != v]
        if wrong:
            raise BadExport(f"the manifest's counts do not match the export ({', '.join(wrong)})")
        return ExportContents(header=header, manifest=manifest, workspace=workspace, staging=staging,
                              files=len(seen), bytes=sum(size for size, _ in seen.values()), memory=memory,
                              agenda=agenda, **parts)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def staging_dir(home: Path, what: str) -> Path:
    """A fresh private directory path under AGENTOS_HOME/tmp (left out of backups), not created."""
    root = Path(home) / "tmp"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root / f"{what}-{pysecrets.token_hex(6)}"
