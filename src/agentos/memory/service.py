"""MemoryService: stores, labels and serves workspace memory (docs/design/workspace-memory.md).

Runtime service "memory". No MCP or HTTP here: the tools, API routes and CLI (Brief C) build a
`Caller` from the call and use the methods below. Every method takes the caller first and
checks its memory scope before a workspace file is opened.

Writes: `plan_write` (validate, compute trust, say whether the owner must approve) and `write`
(store). Reads: `get`, `search`, `context`, `list_entries`; each returns a `MemoryRead` whose
`taint` the caller's family must receive (the service already adds it through the policy port).
Owner acts: `confirm`, `review`, `forget` (with `plan_forget`). Node: `run_summary`, `expire`,
`verify`, `decisions_digest`, `decisions_for_lane`, `stats`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from agentos.core.config import home_from_env
from agentos.core.errors import InvalidRequest, NotFound, ServiceUnavailable
from agentos.memory.common import (
    labels_of,
    scope_matches,
    trim,
)
from agentos.memory.errors import (
    EntryNotFound,
    OutOfScope,
    PrivacyRefused,
    RateLimited,
)
from agentos.memory.models import (
    DAY,
    LABELS,
    Audience,
    Caller,
    EntryType,
    Limits,
    RunSummaryAttrs,
    State,
    Trust,
    WriteRequest,
    check_entry_id,
    check_workspace,
    load_limits,
)
from agentos.memory.pack import PackMixin
from agentos.memory.portable import PortableMixin
from agentos.memory.ports import (
    MemoryPorts,
    RuntimePolicyPort,
    lane_workspace,
    runtime_cloud_ok,
    runtime_privacy_for,
    runtime_vault_scrub,
)
from agentos.memory.reads import ReadsMixin
from agentos.memory.scan import Scrubber, SecretScanner, default_scrubbers
from agentos.memory.store import (
    MemoryStores,
    WorkspaceDB,
    canonical,
    sha256,
)
from agentos.memory.writes import WritesMixin

log = logging.getLogger("agentos.memory")

SERVICE_NAME = "memory"
EXPIRE_EVERY_S = 3600.0


class _Rates:
    """Writes per grant family in sliding windows (4.11). In memory: a restart resets them."""

    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self._log: dict[str, deque[tuple[float, bool]]] = {}
        self._lock = threading.Lock()

    def check(self, family: str, untrusted: bool, limits: Limits) -> None:
        now = self.clock()
        with self._lock:
            q = self._log.setdefault(family, deque())
            while q and q[0][0] <= now - DAY:
                q.popleft()
            hour = [u for t, u in q if t > now - 3600]
            if len(q) >= limits.writes_per_day:
                raise RateLimited(f"this agent family wrote {len(q)} memory entries today (the cap is "
                                  f"{limits.writes_per_day}); try again later")
            if len(hour) >= limits.writes_per_hour:
                raise RateLimited(f"this agent family wrote {len(hour)} memory entries in the last hour (the cap is "
                                  f"{limits.writes_per_hour}); try again later")
            if untrusted and sum(hour) >= limits.untrusted_writes_per_hour:
                raise RateLimited(f"untrusted writes are capped at {limits.untrusted_writes_per_hour} an hour; "
                                  "try again later")

    def record(self, family: str, untrusted: bool) -> None:
        with self._lock:
            self._log.setdefault(family, deque()).append((self.clock(), untrusted))


class MemoryService(WritesMixin, ReadsMixin, PackMixin, PortableMixin):
    """Runtime service "memory"."""

    def __init__(self, home: Path, ports: MemoryPorts | None = None, *, limits: Limits | None = None,
                 clock: Callable[[], float] = time.time, scrubbers: Sequence[tuple[str, Scrubber]] | None = None):
        self.home = Path(home)
        self.ports = ports or MemoryPorts()
        self.limits = limits or load_limits(self.home)
        self.clock = clock
        self.stores = MemoryStores(self.home / "memory")
        self.scanner = SecretScanner(self.ports.vault_scrub,
                                     default_scrubbers() if scrubbers is None else scrubbers)
        self._rates = _Rates(clock)
        self._task: asyncio.Task[None] | None = None

    # ---- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._expire_loop(), name="memory-expire")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def close(self) -> None:
        self.stores.close()

    async def _expire_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.expire)
            except Exception:
                log.exception("memory retention failed")
            await asyncio.sleep(EXPIRE_EVERY_S)

    # ---- access ----------------------------------------------------------------------

    def _check_scope(self, caller: Caller, workspace: str, *, write: bool = False) -> None:
        check_workspace(workspace)
        if not caller.scope.allows(workspace):
            raise OutOfScope(f"your grant has no memory for {workspace}")
        if write and not caller.scope.write:
            raise OutOfScope(f"your grant can read the memory of {workspace} but not write it")
        exists = self.ports.workspace_exists
        if exists is not None and not exists(workspace):
            raise NotFound(f"no workspace {workspace}")

    def _privacy(self, caller: Caller, workspace: str) -> str:
        """The workspace's privacy level, after refusing a cloud reader a local-only workspace (4.10)."""
        try:
            level = str(self.ports.privacy_for(workspace))
        except Exception:
            log.exception("privacy_for failed; treating %s as local-only", workspace)
            level = "local-only"
        if caller.cloud and level == "local-only" and not self.ports.cloud_ok(workspace):
            raise PrivacyRefused(
                f"{workspace} is local-only: its memory never goes to a cloud model, and an attached agent "
                "counts as one. The owner can allow it by setting memory_cloud_ok on the workspace")
        return level

    def _db(self, workspace: str, *, create: bool = False) -> WorkspaceDB | None:
        return self.stores.get(workspace, create=create)

    def _workspaces_for(self, caller: Caller) -> list[str]:
        ws = caller.scope.workspaces
        return sorted(ws) if ws is not None else self.stores.workspaces()

    def _locate(self, caller: Caller, entry_id: str, workspace: str | None) -> tuple[str, WorkspaceDB, dict]:
        check_entry_id(entry_id)
        if workspace:
            self._check_scope(caller, workspace)
            candidates = [workspace]
        else:
            exists = self.ports.workspace_exists
            candidates = [ws for ws in self._workspaces_for(caller) if exists is None or exists(ws)]
        for ws in candidates:
            db = self._db(ws)
            row = db.entry(entry_id) if db is not None else None
            if row is None:
                continue
            if row["audience"] == Audience.OWNER and not caller.can_read_owner_audience():
                break
            return ws, db, row
        raise EntryNotFound(f"no memory entry {entry_id}")

    def _visible_sql(self, caller: Caller, *, include_expired: bool = False, alias: str = "") -> tuple[str, dict]:
        p = f"{alias}." if alias else ""
        now = self.clock()
        if include_expired:
            where = f"{p}state IN ('live', 'expired')"
        else:
            where = f"{p}state = 'live' AND ({p}expires_at IS NULL OR {p}expires_at > :now)"
        if not caller.can_read_owner_audience():
            where += f" AND {p}audience = 'workspace'"
        return where, {"now": now}

    def _apply_taint(self, caller: Caller, labels: Iterable[str]) -> frozenset[str]:
        if caller.in_person or caller.is_node:
            return frozenset()
        out = frozenset(set(labels) & LABELS)
        if out and caller.family and self.ports.policy is not None:
            self.ports.policy.taint_add(caller.family, sorted(out))
        return out

    def _event(self, kind: str, payload: dict[str, Any]) -> None:
        if self.ports.on_event is None:
            return
        try:
            self.ports.on_event(kind, payload)
        except Exception:
            log.exception("memory event hook failed")

    # ---- content ------------------------------------------------------------------------

    def _content(self, db: WorkspaceDB, version: dict[str, Any]) -> tuple[str | None, str | None, dict | None]:
        title = db.blob(version["title_hash"])
        body = db.blob(version["body_hash"])
        raw = db.blob(version["attrs_hash"]) if version["attrs_hash"] else None
        attrs = json.loads(raw) if raw else ({} if title is not None else None)
        return title, body, attrs

    def _serve(self, db: WorkspaceDB, row: dict[str, Any], text: str | None, *, cloud_redact: bool) -> str | None:
        """Text on its way out: the vault's scrub again (4.14), and for a cloud reader of a
        redacted-cloud workspace the credential patterns too. A vault hit flags the entry."""
        if text is None:
            return None
        out, hit = self.scanner.scrub_out(text)
        if hit:
            self._flag(db, row, "secret_on_read")
        if cloud_redact:
            for _kind, fn in self.scanner.patterns:
                out = fn(out or "")
        return out

    def _serve_attrs(self, db: WorkspaceDB, row: dict[str, Any], attrs: dict | None, *, cloud_redact: bool,
                     etype: str) -> dict | None:
        if attrs is None:
            return None

        def walk(v: Any) -> Any:
            if isinstance(v, str):
                return self._serve(db, row, v, cloud_redact=cloud_redact)
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            return v

        out = walk(attrs)
        if cloud_redact and etype == EntryType.CONTACT and isinstance(out.get("handles"), dict):
            out["handles"] = {k: "[masked]" for k in out["handles"]}
        return out

    def _flag(self, db: WorkspaceDB, row: dict[str, Any], reason: str) -> None:
        flags = set(json.loads(row["flagged"])) if row.get("flagged") else set()
        if reason in flags:
            return
        flags.add(reason)
        row["flagged"] = json.dumps(sorted(flags))
        db.set_fields(row["id"], flagged=row["flagged"])
        self._event("memory_flagged", {"workspace": db.workspace, "id": row["id"], "reason": reason})

    def _withheld(self, caller: Caller, row: dict[str, Any], level: str, *, explicit: bool = False) -> str | None:
        """Why only the title and id may be shown, or None. Private entries need the private
        scope (and never go whole to a cloud reader of a redacted-cloud workspace); suspicious
        notes show their body only when fetched by id."""
        if "private" in self._served_labels(row, level):
            if not caller.can_read_private():
                return "private"
            if caller.cloud and level == "redacted-cloud":
                return "private"
        if not explicit and row.get("suspicious"):
            return "suspicious"
        return None

    @staticmethod
    def _served_labels(row: dict[str, Any], level: str) -> list[str]:
        labels = set(labels_of(row))
        if level == "local-only":
            labels.add("private")
        return sorted(labels)

    # ---- write -----------------------------------------------------------------------------

    # ---- owner acts --------------------------------------------------------------------------

    # ---- node-internal ------------------------------------------------------------------------

    def run_summary(self, record: dict[str, Any]) -> dict[str, Any]:
        """Write the `run_summary` of a finished run or brain task, from the structured record
        only (never page text). `record` keys: workspace (or a lane "ws/name"), run_id or
        brain_task_id, flow, flow_version, lane, caller, status, proof, content_digest,
        approved_text, cost, started_at, ended_at. Idempotent per run: a second call with the
        same status returns the existing entry; a new status (a later re-check verified the run)
        is a new version of it."""
        lane = record.get("lane")
        workspace = record.get("workspace") or lane_workspace(lane)
        if not workspace:
            raise InvalidRequest("a run summary needs a workspace or a lane in a workspace")
        run_id, task_id = record.get("run_id"), record.get("brain_task_id")
        if not (run_id or task_id):
            raise InvalidRequest("a run summary needs run_id or brain_task_id")
        fields = set(RunSummaryAttrs.model_fields)
        attrs = {k: v for k, v in record.items() if k in fields and v is not None}
        if isinstance(attrs.get("approved_text"), str):
            attrs["approved_text"] = trim(attrs["approved_text"], 2000)
        status = attrs.get("status")
        what = record.get("flow") or ("brain task" if task_id else "run")
        title = trim(f"{what} on {lane or workspace}: {status}", self.limits.title_chars)
        lines = _run_summary_lines(record, status, lane)
        key = f"run.{run_id}" if run_id else f"brain.{task_id}"
        caller = Caller.node(run_id=run_id, brain_task_id=task_id, lane=lane)
        db = self._db(workspace)
        existing = db.entry_by_key(key) if db is not None else None
        supersedes = None
        if existing is not None:
            if existing["trust"] != Trust.NODE or _summary_status(db, existing) == status:
                return {"workspace": workspace, "id": existing["id"], "version": existing["current"],
                        "ref": f"{existing['id']}@{existing['current']}", "type": existing["type"], "key": key,
                        "trust": existing["trust"], "labels": labels_of(existing), "existing": True}
            supersedes = f"{existing['id']}@{existing['current']}"
        body = trim("\n".join(lines), self.limits.body_limit(EntryType.RUN_SUMMARY))
        return self.write(caller, workspace, WriteRequest(type=EntryType.RUN_SUMMARY, title=title, body=body,
                                                          key=key, attrs=attrs, supersedes=supersedes))

    def expire(self, now: float | None = None) -> dict[str, dict[str, int]]:
        """The retention job (4.9): live entries past `expires_at` become expired; expired
        entries a further `purge_after_days` later have their content erased like a forget
        (reason "retention"). Returns counts per workspace."""
        now = self.clock() if now is None else now
        cutoff = now - self.limits.purge_after_days * DAY
        out: dict[str, dict[str, int]] = {}
        for ws in self.stores.workspaces():
            db = self._db(ws)
            if db is None:
                continue
            expired = purged = 0
            for row in db.entries("state = 'live' AND expires_at IS NOT NULL AND expires_at <= ?", (now,),
                                  order="expires_at", limit=100000):
                db.set_fields(row["id"], state=State.EXPIRED.value)
                expired += 1
            for row in db.entries("state = 'expired' AND expires_at IS NOT NULL AND expires_at <= ?", (cutoff,),
                                  order="expires_at", limit=100000):
                db.forget(row["id"], at=now, by="node", reason="retention: expired and past the purge window")
                self._unpin(row["id"])
                purged += 1
            if expired or purged:
                out[ws] = {"expired": expired, "purged": purged}
        return out

    def verify(self, workspace: str) -> dict[str, Any]:
        """Check one workspace's chain (4.3): `{ok, versions, forgotten, head, problems}`."""
        check_workspace(workspace)
        db = self._db(workspace)
        if db is None:
            return {"workspace": workspace, "ok": True, "versions": 0, "forgotten": 0, "head": None, "problems": []}
        return db.verify()

    def decisions_digest(self, workspace: str) -> str:
        """The digest of the workspace's live decisions, as served in `context` and recorded
        as the context receipt. Brief B's `decisions_loaded` compares a receipt with this."""
        check_workspace(workspace)
        db = self._db(workspace)
        if db is None:
            return sha256(canonical([]))
        now = self.clock()
        rows = db.entries("type = 'decision' AND state = 'live' AND (expires_at IS NULL OR expires_at > ?)",
                          (now,), order="id", limit=100000)
        refs = []
        for r in rows:
            v = db.version(r["id"], r["current"])
            refs.append(f"{r['id']}@{r['current']}:{v['version_hash'] if v else ''}")
        return sha256(canonical(sorted(refs)))

    def loaded_digest(self, workspace: str) -> str | None:
        """The getter policy's `decisions_loaded` uses on commit traces: the decisions digest,
        or None when the workspace has no live decisions (nothing to load)."""
        db = self._db(workspace)
        if db is None or not db.entries("type = 'decision' AND state = 'live'", limit=1):
            return None
        return self.decisions_digest(workspace)

    def decisions_for_lane(self, workspace: str, lane: str) -> list[dict[str, Any]]:
        """Live decisions whose scope matches `lane`, for approval cards (phase 2). Node and
        owner side only: titles, no taint."""
        check_workspace(workspace)
        db = self._db(workspace)
        if db is None:
            return []
        out = []
        for r in db.entries("type = 'decision' AND state = 'live'", order="created_at", limit=1000):
            v = db.version(r["id"], r["current"])
            raw = db.blob(v["attrs_hash"]) if v and v["attrs_hash"] else None
            attrs = json.loads(raw) if raw else {}
            if scope_matches(attrs.get("scope") or [], [lane]):
                out.append({"id": r["id"], "title": r["title"], "enforced_by": attrs.get("enforced_by"),
                            "enforced": bool(attrs.get("enforced_by"))})
        return out

    def stats(self, workspace: str) -> dict[str, Any]:
        check_workspace(workspace)
        db = self._db(workspace)
        lim = self.limits
        live = db.live_count() if db is not None else 0
        size = db.content_bytes() if db is not None else 0
        return {"workspace": workspace, "live_entries": live, "live_entries_cap": lim.live_entries,
                "content_bytes": size, "content_bytes_cap": lim.workspace_bytes}

    # ---- export and import (4.13) ------------------------------------------------------------

    # ---- reads ---------------------------------------------------------------------------------

    # ---- context pack (4.12) --------------------------------------------------------------------


def build(runtime: Any, *, home: Path | None = None) -> MemoryService:
    """Composition-root hook: open `$AGENTOS_HOME/memory/` with ports over the runtime's
    policy, traces, vault, brains and workspaces services, and register it as "memory"."""
    cfg = runtime.get("config") if runtime is not None else None
    if home is None:
        home = Path(cfg.home) if cfg is not None and getattr(cfg, "home", None) else home_from_env()
    ports = MemoryPorts(
        policy=RuntimePolicyPort(runtime), vault_scrub=runtime_vault_scrub(runtime),
        privacy_for=runtime_privacy_for(runtime), cloud_ok=runtime_cloud_ok(runtime),
        node_id=getattr(cfg, "name", None))
    try:
        service = MemoryService(Path(home), ports)
    except InvalidRequest as exc:
        raise ServiceUnavailable(str(exc)) from None
    if runtime is not None:
        runtime.register(SERVICE_NAME, service)
    rules = ports.policy.rules() if isinstance(ports.policy, RuntimePolicyPort) else None
    setter = getattr(rules, "set_decisions_digest", None)
    if callable(setter):
        setter(service.loaded_digest)   # commit traces carry decisions_loaded: current | stale | never
    return service


def _run_summary_lines(record: dict[str, Any], status: Any, lane: str | None) -> list[str]:
    """A run summary's body, one line per fact the record holds."""
    lines = [f"Status: {status}"]
    if record.get("flow"):
        ver = record.get("flow_version")
        lines.append(f"Flow: {record['flow']}" + (f" (version {ver})" if ver is not None else ""))
    if lane:
        lines.append(f"Lane: {lane}")
    if record.get("caller"):
        lines.append(f"Started by: {record['caller']}")
    for p in record.get("proof") or []:
        url = p.get("url") if isinstance(p, dict) else None
        if url:
            lines.append(f"Proof: {url}" + (f" ({p.get('readback')})" if p.get("readback") else ""))
    cost = record.get("cost")
    if isinstance(cost, dict) and cost:
        lines.append("Cost: " + ", ".join(f"{k} {v}" for k, v in sorted(cost.items())))
    if record.get("approved_text"):
        lines.append("Approved text: " + trim(record["approved_text"], 1500))
    return lines


def _summary_status(db: WorkspaceDB, entry: dict[str, Any]) -> Any:
    """The status an existing run summary's current version records."""
    v = db.version(entry["id"], entry["current"])
    raw = db.blob(v["attrs_hash"]) if v and v["attrs_hash"] else None
    return (json.loads(raw) if raw else {}).get("status")
