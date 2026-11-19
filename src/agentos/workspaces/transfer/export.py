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
import hashlib
import io
import json
import os
import stat
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos import __version__
from agentos.core import age
from agentos.core.config import NodeConfig, load_proxies
from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.layout import PROFILE_LOCKS, LaneDir, fsync_dir
from agentos.core.models import LaneKind
from agentos.ops import backup
from agentos.ops.backup import EXCLUDE_PROFILE, SMALL_FILE
from agentos.workspaces.models import check_workspace_id
from agentos.workspaces.transfer.format import (
    _GLOB,
    AGENDA_MEMBER,
    DATA,
    FORMAT,
    FORMAT_VERSION,
    MEMORY_MEMBER,
    META,
    MIN_PASSPHRASE,
    NOT_EXPORTED,
    PARTIAL_PREFIX,
    SUFFIX,
    ExportError,
    ExportRefused,
    memory_counts,
)
from agentos.workspaces.transfer.sources import Sources

# ---- where the data comes from and goes to -----------------------------------------------


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
                                "it (browser_close), or stop agentosd, then export again")
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


