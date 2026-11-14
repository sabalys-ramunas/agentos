"""What a node reports about itself and its work, shaped for `event` (docs/PROTOCOL.md v1):

- `run`: a run, on every status change (the runner's own feed);
- `node { inventory }`: flows (with their "Ask me first"), schedules, brains, vault names, the
  "Ask me first" switches and the confirmation setting in force;
- `node { system }`, `{ programs }`, `{ services }`, `{ doctor }`: the machine, what is installed,
  what runs, and `agentos doctor`'s checklist.

Everything here is metadata, redacted again before it is queued. A run carries no params, its
proof no file path (`proof.screenshot` names the `file` stream to read it from), the vault only
names, a schedule's params only their names and types, its watch only its page (no query), and the
doctor runs offline, so no proxy is probed and no exit IP is read. Any IP address in free text (a
run's summary and error, a check's words) is masked, before the text is cut.

`Throttle` decides which reports to send: each one once after every welcome, then only what
changed, and never one kind more often than `min_interval` (the latest value waits). `fit()` keeps
each within MAX_REPORT_BYTES (an inventory too large keeps the first items of each section).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos import __version__
from agentos.core.errors import ServiceUnavailable
from agentos.core.logs import url_for_log
from agentos.core.models import LANE_ID_RE
from agentos.node.channel.files import proof_path
from agentos.policy.redact import mask_addresses, redact_text

log = logging.getLogger("agentos.channel")

KINDS = ("inventory", "system", "programs", "services", "doctor", "workflows_catalog")
MAX_ITEMS = 1000
SUMMARY_CHARS = 1000
BRAIN_FIELDS = ("name", "kind", "provider", "state", "seats", "window_used_pct", "model", "locality", "enabled")
VAULT_FIELDS = ("id", "name", "kind", "domain", "lanes", "created_at", "last_used")
SERVICE_STATES = {"running": "running", "starting": "starting", "backoff": "restarting", "failed": "failed",
                  "stopped": "stopped", "stopping": "stopped"}
DOCTOR_STATUS = {"pass": "ok", "warn": "warn", "fail": "fail"}  # a skipped check says nothing about the node
NODE_PROGRAMS = ("camoufox", "playwright")
MAX_REPORT_BYTES = 512 * 1024  # what a controller keeps of one (controller/hub.py), measured as it does
JSON_TYPES = ((bool, "boolean"), (int, "integer"), (float, "number"), (str, "string"), (list, "array"),
              (dict, "object"))


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value if isinstance(value, str) else None


def _text(value: Any, limit: int = SUMMARY_CHARS) -> str | None:
    """Free text as a controller may read it: redacted, addresses masked, and only then cut."""
    return redact_text(mask_addresses(value), max_len=limit) if isinstance(value, str) else None


def _json_type(value: Any) -> str:
    return next((name for kind, name in JSON_TYPES if isinstance(value, kind)), "null")


# ---- runs ----

def run_payload(described: dict, *, effect: str | None, home: Path) -> dict:
    """A runner.describe() as `event { kind: "run" }`. `started_at` is when it started, else when it
    was created (a queued run has started nothing yet; `created_at` says which)."""
    principal = described.get("principal")
    proof = described.get("proof") if isinstance(described.get("proof"), dict) else None
    error = described.get("error") if isinstance(described.get("error"), dict) else None
    run_id = str(described.get("id"))
    out: dict[str, Any] = {
        "id": run_id, "lane": described.get("surface") or described.get("lane"), "flow": described.get("flow"),
        "version": described.get("version"), "status": described.get("status"),
        "summary": _text(described.get("summary")) or "",
        "principal": str(principal.get("id")) if isinstance(principal, dict) and principal.get("id") else "unknown",
        "approval_id": described.get("approval_id"),
        "trigger": described.get("source"), "schedule_id": described.get("schedule_id"),
        "created_at": described.get("created_at"),
        "started_at": described.get("started_at") or described.get("created_at"),
        "finished_at": described.get("finished_at"),
        "cost": _cost(described.get("cost")),
        "proof": None, "steps": [],
    }
    if effect is not None:
        out["effect"] = effect
    if proof is not None or error is not None:
        shot = proof_path(home, proof, error) is not None
        out["proof"] = {"url": _proof_url((proof or {}).get("url")), "id": None,
                        "screenshot": f"runs/{run_id}/proof.png" if shot else None,
                        "readbacks": len((proof or {}).get("readbacks") or [])}
    if error is not None:
        out["error"] = {"code": str(error.get("code") or "error")[:40], "message": _text(error.get("message"), 500)}
    return out


def _cost(cost: Any) -> dict:
    cost = cost if isinstance(cost, dict) else {}
    try:
        return {"tokens": max(0, int(cost.get("tokens") or 0)), "usd": max(0.0, float(cost.get("usd") or 0.0))}
    except (TypeError, ValueError):
        return {"tokens": 0, "usd": 0.0}


def _proof_url(url: Any) -> str | None:
    """Scheme, host and path: a query or fragment may hold a session or a signature."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    return redact_text(url_for_log(url), max_len=2048)


def run_effect(runner: Any, described: dict, cache: dict) -> str | None:
    """The effective effect of the run's flow version (what grants and approvals saw)."""
    key = (described.get("flow"), described.get("version"))
    if key in cache:
        return cache[key]
    effect = None
    flows = getattr(runner, "flows", None)
    if key[0] and flows is not None:
        try:
            effect = str(flows.get(key[0], key[1]).effect)
        except Exception:  # noqa: BLE001  (a flow that is gone: the run says nothing of its effect)
            effect = None
    if len(cache) > 512:
        cache.clear()
    cache[key] = effect
    return effect


# ---- inventory ----

# what the inventory last said of a section it could not read: logged when it changes, not each cycle
_section_logged: dict[str, str] = {}


def reset_section_log() -> None:
    _section_logged.clear()


def _section_failed(section: str, exc: BaseException) -> bool:
    """Log a section that could not be read once per cause; True when it is a service that is not set
    up here (its section is sent empty), False for a failure (its section is left out)."""
    not_set_up = isinstance(exc, ServiceUnavailable)
    said = f"{type(exc).__name__}: {exc}"
    if _section_logged.get(section) != said:
        _section_logged[section] = said
        if not_set_up:
            log.info("channel: the inventory sends no %s entries: %s", section, exc)
        else:
            log.warning("channel: the inventory leaves out its %s section: %s", section, said)
    return not_set_up


async def _section(out: dict, section: str, read: Callable[[], Any]) -> None:
    try:
        value = read()
        if asyncio.iscoroutine(value):
            value = await value
    except Exception as exc:  # noqa: BLE001  (one section never drops the rest of the inventory)
        if _section_failed(section, exc):
            out[section] = []
        return
    _section_logged.pop(section, None)
    out[section] = value


async def inventory(runtime: Any) -> dict:
    """`{ flows, schedules, brains, vault, ask_first, confirmation }`. A service that is not set up
    here (a node with no vault) sends its section empty; a section that cannot be read is left out.
    Either is logged once, and the rest of the inventory still goes out."""
    out: dict[str, Any] = {}
    runner = runtime.get("runner")
    flows = runtime.get("flows") or getattr(runner, "flows", None)
    switches = runtime.get("ask_first")
    if flows is not None:
        out["flows"] = [_flow(flows, fv, switches) for fv in flows.list()[:MAX_ITEMS]]
    workflows = runtime.get("workflows")
    if workflows is not None:  # a workflow is a routine: one more row, with its steps
        out["flows"] = [*out.get("flows", []), *workflows.list()[:MAX_ITEMS]]
    if switches is not None and callable(getattr(switches, "list", None)):
        rows = [x.public() for x in switches.list()[:MAX_ITEMS]]
        if rows or "flows" in out:  # a node with no flows and no switches still sends no inventory
            out["ask_first"] = rows
    scheduler = getattr(runner, "scheduler", None)
    if scheduler is not None:
        out["schedules"] = [_schedule(scheduler, s) for s in scheduler.list()[:MAX_ITEMS]]
    brains = runtime.get("brains")
    if brains is not None and callable(getattr(brains, "list_brains", None)):
        async def read_brains() -> list:
            async with asyncio.timeout(10):
                listed = await brains.list_brains()
            return [{k: b.get(k) for k in BRAIN_FIELDS} for b in listed[:MAX_ITEMS]]
        await _section(out, "brains", read_brains)
    vault = runtime.get("vault")
    if vault is not None and callable(getattr(vault, "list_entries", None)):
        async def read_vault() -> list:
            async with asyncio.timeout(10):
                entries = await vault.list_entries()
            return [{k: e.public().get(k) for k in VAULT_FIELDS} for e in entries[:MAX_ITEMS]]
        await _section(out, "vault", read_vault)
    setting = runtime.get("confirmation")
    if setting is not None:  # after both layers: what this node holds an approval to
        out["confirmation"] = setting.current().enforced()
    return out


def workflows_catalog(runtime: Any) -> dict:
    """What a workflow can use here (docs/WORKFLOWS.md): sizes of lists, never their addresses."""
    service = runtime.get("workflows")
    return service.catalog(person=True) if service is not None else {}  # for a person's panel


def _flow(flows: Any, fv: Any, switches: Any = None) -> dict:
    d = fv.definition
    props = d.params.get("properties", {}) if isinstance(d.params, dict) else {}
    required = set(d.params.get("required", [])) if isinstance(d.params, dict) else set()
    return {
        "id": d.id, "lane": d.lane, "version": fv.version, "title": _text(d.title, 200),
        "effect": str(fv.effect), "declared_effect": str(d.effect),
        # names and types only: a schema's defaults and examples are free text
        "params": {"type": "object", "required": sorted(k for k in required if k in props),
                   "properties": {k: {"type": v.get("type")} if isinstance(v, dict) and isinstance(v.get("type"), str)
                                  else {} for k, v in list(props.items())[:64]}},
        "readbacks": d.readback_count() if callable(getattr(d, "readback_count", None)) else None,
        "created_at": _iso(getattr(fv, "created_at", None)),
        # "Ask me first" follows the flow id (agentos.policy.ask_first); off when never set
        **(switches.flow_view(d.id) if callable(getattr(switches, "flow_view", None))
           else {"ask_first": False, "ask_first_by": None}),
    }


def _schedule(scheduler: Any, s: Any) -> dict:
    described = scheduler.describe(s)
    cron, watch = described.get("cron"), described.get("watch")
    state = s.state if isinstance(getattr(s, "state", None), dict) else {}
    return {
        "id": s.id, "flow": s.flow_id or s.route_id, "kind": str(s.kind), "lane": s.surface or s.lane,
        "cron": cron.get("cron") if isinstance(cron, dict) else None,
        "timezone": cron.get("timezone") if isinstance(cron, dict) else None,
        "watch": url_for_log(watch.get("url")) if isinstance(watch, dict) and watch.get("url") else None,
        # names and types only, as for a flow: a value may be an account, a message or a secret
        "params": {str(k)[:64]: {"type": _json_type(v)} for k, v in list((s.params or {}).items())[:64]}
        if isinstance(s.params, dict) else {},
        "deadman_minutes": s.deadman_minutes, "enabled": s.enabled,
        "next_run": described.get("next_run"),
        "last_run_id": state.get("last_run_id"), "last_error": _text(state.get("last_error"), 300),
        "skipped_total": state.get("skipped_total") if isinstance(state.get("skipped_total"), int) else None,
    }


# ---- the machine ----

def system(config: Any, *, started: float | None = None) -> dict:
    """What the machine is. Nothing here identifies a person or an address."""
    cores = os.cpu_count()
    total = _mem_total_mb()
    try:
        disk = shutil.disk_usage(config.home).total // (1024 * 1024)
    except OSError:
        disk = None
    return {
        "os": platform.system().lower() or None, "arch": platform.machine() or None,
        "kernel": platform.release() or None, "cpu_model": _cpu_model(), "cores": cores,
        "mem_total_mb": total, "disk_total_mb": disk, "gpu": None, "timezone": _timezone(config),
        "uptime_s": _uptime(), "version": __version__, "protocol": 1,
        "python": platform.python_version(), "agentosd_started_at": _iso(datetime.fromtimestamp(started, UTC))
        if started else None,
    }


def _mem_total_mb() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        return None


def _cpu_model() -> str | None:
    try:
        with open("/proc/cpuinfo") as handle:
            for line in handle:
                if line.lower().startswith(("model name", "hardware")):
                    return line.split(":", 1)[1].strip()[:128] or None
    except OSError:
        pass
    return (platform.processor() or None) if platform.system() != "Linux" else None


def _uptime() -> int | None:
    try:
        with open("/proc/uptime") as handle:
            return int(float(handle.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def _timezone(config: Any) -> str | None:
    zone = getattr(config, "timezone", None)
    if isinstance(zone, str) and zone:
        return zone
    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        return None
    return link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else None


def programs(runtime: Any) -> list[dict]:
    """agentos and its browser stack (scope node), what env lanes installed (lane), and what the
    env base images bake in (base_image)."""
    from importlib import metadata

    out: dict[tuple, dict] = {}
    out[("agentos", "agentos", "node")] = {"name": "agentos", "version": __version__, "source": "agentos",
                                           "scope": "node", "lanes": []}
    for name in NODE_PROGRAMS:
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
        out[(name, "pip", "node")] = {"name": name, "version": version, "source": "pip", "scope": "node",
                                      "lanes": []}
    envs = runtime.get("envs")
    store = getattr(envs, "store", None)
    for info in _env_lanes(envs):
        lane_id = info.lane.id
        try:
            manifest = store.manifest(lane_id) if store is not None else {}
        except Exception:  # noqa: BLE001, S112  (a lane whose manifest cannot be read is left out)
            continue
        for source, packages in (manifest or {}).items():
            for name, version in (packages or {}).items() if isinstance(packages, dict) else ():
                item = out.setdefault((str(name), str(source), "lane", str(version)),
                                      {"name": str(name)[:128], "version": str(version)[:64], "source": str(source),
                                       "scope": "lane", "lanes": []})
                if len(item["lanes"]) < 200:
                    item["lanes"].append(lane_id)
    images = getattr(envs, "images", None)
    if callable(images):
        try:
            built = list(images())
        except Exception:  # noqa: BLE001
            built = []
        for image in built:
            for source, packages in (getattr(image, "manifest", None) or {}).items():
                for name, version in (packages or {}).items() if isinstance(packages, dict) else ():
                    out.setdefault((str(name), str(source), "base_image", str(version)),
                                   {"name": str(name)[:128], "version": str(version)[:64], "source": str(source),
                                    "scope": "base_image", "lanes": [], "pinned_hash": image.content_hash
                                    if str(getattr(image, "content_hash", "")).startswith("sha256:") else None,
                                    "installed_at": _iso(getattr(image, "built_at", None))})
    return list(out.values())[:MAX_ITEMS]


def services(runtime: Any, *, started: float | None = None) -> list[dict]:
    """agentosd itself, and every service lane's supervised process."""
    out = [{"name": "agentosd", "lane": None, "state": "running", "restarts": 0,
            "started_at": _iso(datetime.fromtimestamp(started, UTC)) if started else None, "ports": []}]
    envs = runtime.get("envs")
    status = getattr(envs, "service_status", None)
    for info in _env_lanes(envs):
        lane = info.lane
        if str(getattr(lane.kind, "value", lane.kind)) != "service" or not callable(status):
            continue
        try:
            st = status(lane.id)
        except Exception:  # noqa: BLE001, S112
            continue
        name = lane.id.split("/", 1)[1]
        out.append({"name": name, "lane": lane.id, "state": SERVICE_STATES.get(str(st.state), "unknown"),
                    "health": str(st.health), "restarts": int(st.restarts or 0), "started_at": _iso(st.started_at),
                    "last_exit": st.last_exit, "ports": []})
    return out[:MAX_ITEMS]


def _env_lanes(envs: Any) -> list[Any]:
    if envs is None:
        return []
    try:
        return [info for info in envs.list() if LANE_ID_RE.fullmatch(getattr(info.lane, "id", ""))]
    except Exception:  # noqa: BLE001
        return []


def doctor(config: Any, scrub: Callable[[str], str] = lambda text: text) -> list[dict]:
    """`agentos doctor` offline (no proxy probed, so no exit IP), as `{ id, name, status, detail, fix, at }`."""
    from agentos.ops import doctor as checks

    report = checks.run(checks.DoctorContext(home=config.home, config=config, network=False, timeout=5.0))
    out = []
    for result in report.results:
        status = DOCTOR_STATUS.get(result.status)
        if status is None:
            continue
        out.append({"id": result.id[:64], "name": result.title[:128], "status": status,
                    "detail": _text(scrub(result.detail or ""), 300),
                    "fix": _text(scrub(result.fix), 300) if result.fix else None,
                    "at": report.generated_at})
    return out[:MAX_ITEMS]


# ---- size ----

def report_size(value: Any) -> int:
    return len(json.dumps(value, default=str))


def fit(kind: str, value: Any) -> tuple[Any, bool]:
    """`(value, cut)` within MAX_REPORT_BYTES. Too large, an inventory keeps the first N items of each
    section, the same N for all, with `counts` (the full length of each) and `truncated: true`; a list
    report keeps its first N. The largest N that fits is found the same way each time, so an unchanged
    report is cut the same. `(None, True)` when not even one item fits."""
    if report_size(value) <= MAX_REPORT_BYTES:
        return value, False
    if isinstance(value, dict):
        sections = {k: v for k, v in value.items() if isinstance(v, list)}
        longest = max((len(v) for v in sections.values()), default=0)

        def cut(n: int) -> Any:
            return {**value, **{k: v[:n] for k, v in sections.items()},
                    "counts": {k: len(v) for k, v in sections.items()}, "truncated": True}
    elif isinstance(value, list):
        longest = len(value)

        def cut(n: int) -> Any:
            return value[:n]
    else:
        return None, True
    low, high = 0, longest  # the largest n whose cut fits
    while low < high:
        mid = (low + high + 1) // 2
        if report_size(cut(mid)) <= MAX_REPORT_BYTES:
            low = mid
        else:
            high = mid - 1
    trimmed = cut(low)
    if low == 0 or report_size(trimmed) > MAX_REPORT_BYTES:
        return None, True
    return trimmed, True


# ---- rate limits ----

class Throttle:
    """Which reports to send now. A kind is sent when its value differs from the last one sent,
    and at most once per `min_interval`; a newer value waits for the window and replaces any older
    one still waiting. `force()` (each welcome) sends every kind once more at its next offer, changed
    or not and inside the window or not: a new controller session may have lost what was sent."""

    def __init__(self, min_interval: float, clock: Callable[[], float] = time.monotonic):
        self.min_interval = min_interval
        self.clock = clock
        self.sent: dict[str, tuple[str, float]] = {}  # kind -> (digest, when)
        self.waiting: dict[str, Any] = {}
        self.forced: set[str] = set()

    def force(self) -> None:
        self.forced = set(KINDS)

    def offer(self, kind: str, value: Any) -> Any | None:
        """`value` if it should go out now, else None (it is kept, and `due()` returns it later)."""
        mark = digest(value)
        last = self.sent.get(kind)
        now = self.clock()
        forced = kind in self.forced
        self.forced.discard(kind)
        if not forced and last is not None and last[0] == mark:
            self.waiting.pop(kind, None)
            return None
        if not forced and last is not None and now - last[1] < self.min_interval:
            self.waiting[kind] = value
            return None
        self.waiting.pop(kind, None)
        self.sent[kind] = (mark, now)
        return value

    def due(self) -> dict[str, Any]:
        """What waited for its window and may go now."""
        now = self.clock()
        out = {}
        for kind, value in list(self.waiting.items()):
            last = self.sent.get(kind)
            if last is None or now - last[1] >= self.min_interval:
                out[kind] = value
                self.sent[kind] = (digest(value), now)
                del self.waiting[kind]
        return out
