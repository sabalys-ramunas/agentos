"""Periodic node health: exit IPs of warm lanes, proxies, disk and RAM, browser version drift.

`HealthMonitor.run_once()` is the whole logic; the service (agentos.ops.service) only runs it
on a timer. Every finding has a key, a level (ok, warn, critical) and a node-written message.
The monitor notifies on changes of level only (a proxy that stays down is one notification,
and its recovery another), through runtime "notifier" when present: `notifier.send(event)`
with {"type": "health.<kind>", "title", "body", "severity", ...}.

Exit IP rule (the same as the lane self-check): what a warm lane exits from must be one of its
proxy host's addresses. When it is not, the lane is quarantined through the lane manager:
`lanes.quarantine(lane_id, reason)` when the manager has it, else `lanes.close(lane_id)`, which
is also safe because warming it again runs the self-check, which quarantines on a mismatch.
The exit IP is read in the lane's own browser when the manager offers
`probe_exit_ip(lane_id)`; otherwise (and while a human has the lane's live view open, where
the read would cover the screen) it is the proxy's exit seen from the daemon through the
same proxy. A proxy that does not answer never quarantines anything: a browser with a dead
proxy fails its requests rather than leaving from the node's own IP.

Lanes (agentos.stealth.lanecheck): with a `lane_checker`, every round that probes proxies also runs the
lane check of every lane and adds one finding per lane (key `lane.<id>`, kind "lane", the lane's
worst level: warn stays warn, fail is critical), so the owner is notified when a lane's level
changes, never again while it stays the same. The per-lane browser drift check is part of the lane
check then, and the proxy probes of that round are shared with it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.core.config import NodeConfig, load_proxies
from agentos.core.layout import lane_dirs, read_json
from agentos.core.models import LaneState, ProxyConfig
from agentos.core.system import Host, redact
from agentos.node import fingerprint as fpmod

log = logging.getLogger("agentos.ops.health")

OK, WARN, CRITICAL = "ok", "warn", "critical"
ANCHOR_WARN_S = 3600  # heads waiting this long to be anchored get a warning
DISK_WARN = (5 * 1024**3, 10.0)       # (bytes free, percent free): below either is a warning
DISK_CRITICAL = (1 * 1024**3, 5.0)
RAM_WARN, RAM_CRITICAL = 15.0, 7.0    # percent of RAM available
SWAP_WARN = 50.0                      # percent of swap in use
DRIFT_CACHE_SECONDS = 3600.0
_WARM = (LaneState.WARM, LaneState.ACTIVE)


@dataclass
class Finding:
    key: str
    kind: str            # disk, memory, proxy, exit_ip, drift
    level: str
    title: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "level": self.level, "title": self.title,
                "detail": self.detail, "data": self.data}


def _pct(part: float, whole: float) -> float:
    return 100.0 * part / whole if whole else 0.0


def disk_level(free: int, total: int) -> str:
    pct = _pct(free, total)
    if free < DISK_CRITICAL[0] or pct < DISK_CRITICAL[1]:
        return CRITICAL
    if free < DISK_WARN[0] or pct < DISK_WARN[1]:
        return WARN
    return OK


def memory_level(info: dict[str, int]) -> tuple[str, str]:
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    swap, swap_free = info.get("SwapTotal", 0), info.get("SwapFree", 0)
    pct = _pct(avail, total)
    swap_used = _pct(swap - swap_free, swap) if swap else 0.0
    detail = f"{pct:.0f}% of RAM available" + (f", {swap_used:.0f}% of swap in use" if swap else "")
    if pct < RAM_CRITICAL:
        return CRITICAL, detail
    if pct < RAM_WARN or swap_used > SWAP_WARN:
        return WARN, detail
    return OK, detail


def exit_ip_ok(observed: str, expected: set[str]) -> bool:
    return observed in expected


class HealthMonitor:
    def __init__(
        self,
        config: NodeConfig,
        *,
        host: Host | None = None,
        lanes: Any = None,
        notifier: Any = None,
        proxies_loader: Callable[[], dict[str, ProxyConfig]] | None = None,
        clock: Callable[[], float] = time.time,
        proxy_timeout: float = 15.0,
    ):
        self.config = config
        self.host = host or Host()
        self.lanes = lanes
        self.notifier = notifier
        self.proxies_loader = proxies_loader or (lambda: load_proxies(config))
        self.clock = clock
        self.proxy_timeout = proxy_timeout
        self._levels: dict[str, str] = {}
        self._installed: tuple[float, int | None, str | None] | None = None
        self.last: dict[str, Any] | None = None
        self.proxy_results: dict[str, dict[str, Any]] = {}
        self.quarantined: dict[str, str] = {}
        self.lane_checker: Any = None           # agentos.stealth.lanecheck.LaneChecker
        self._lane_findings: list[Finding] = []  # the last lane check round, kept between rounds
        self.policy: Any = None                  # agentos.policy.service, for the action log's write path
        self.anchors: Any = None                 # agentos.node.anchors.AnchorService

    # ---- individual checks ----------------------------------------------------------

    def check_disk(self) -> list[Finding]:
        out = []
        paths = [("home", Path(self.config.home))]
        if not self.host.same_filesystem(Path(self.config.home), Path("/tmp")):
            paths.append(("tmp", Path("/tmp")))
        for name, path in paths:
            usage = self.host.disk_usage(path)
            if usage is None:
                continue
            total, _, free = usage
            level = disk_level(free, total)
            out.append(Finding(f"disk.{name}", "disk", level, f"Disk space under {path}",
                               f"{free / 1024**3:.1f} GiB free ({_pct(free, total):.0f}%)",
                               {"free": free, "total": total}))
        return out

    def check_memory(self) -> list[Finding]:
        info = self.host.meminfo()
        if not info or "MemTotal" not in info:
            return []
        level, detail = memory_level(info)
        data: dict[str, Any] = {k: info.get(k) for k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")}
        biggest = self._biggest_lane() if level != OK else None
        if biggest:
            detail += f"; the biggest lane is {biggest[0]} at {biggest[1]} MB"
            data.update(biggest_lane=biggest[0], biggest_lane_mb=biggest[1])
        return [Finding("memory", "memory", level, "Memory", detail, data)]

    def _biggest_lane(self) -> tuple[str, int] | None:
        """The warm lane that used the most memory at the memory watchdog's last look
        (agentos.node.memwatch, `lanes_mb` in the lane manager's memory_status), so a warning
        about the node says which lane to close first. None without a lane manager that has it."""
        status = getattr(self.lanes, "memory_status", None)
        found = status() if callable(status) else None
        lanes_mb = found.get("lanes_mb") if isinstance(found, dict) else None
        if not lanes_mb:
            return None
        return max(lanes_mb.items(), key=lambda kv: kv[1])

    def check_audit(self) -> list[Finding]:
        """The action log's write path (agentos.policy.audit_health): a warning as soon as it fails,
        critical once calls that change something are refused."""
        traces = getattr(self.policy, "traces", None)
        if traces is None:
            return []
        from agentos.policy import audit_health
        threshold = getattr(getattr(self.policy, "cfg", None), "audit_fail_closed_s", audit_health.DEFAULT_FAIL_CLOSED_S)
        found = audit_health.finding(traces, traces.clock(), threshold)
        out: list[Finding] = []
        if found is None:
            out.append(Finding("audit.write", "audit", OK, "Action log", "every action is being recorded"))
        else:
            level, title, detail = found
            out.append(Finding("audit.write", "audit", level, title, detail,
                               data=audit_health.state(traces, traces.clock(), threshold)))
        torn = getattr(traces, "spill_torn", None)
        spill = getattr(traces, "spill_path", None)
        kept = sorted(str(p) for p in spill.parent.glob(spill.name + ".*.torn")) if spill is not None else []
        if torn:
            where = f"they are kept in {torn['file']}" if torn.get("file") else "they could not be saved"
            out.append(Finding("audit.spill_torn", "audit", WARN, "Action log",
                               f"{torn['lines']} line(s) of the spill file broke off mid-write and were set aside "
                               f"({where}); every complete line was recorded", data={**torn, "files": kept}))
        elif kept:  # set aside before this start: the files stay until the owner looks at them
            out.append(Finding("audit.spill_torn", "audit", WARN, "Action log",
                               f"{len(kept)} file(s) of torn spill lines are kept next to the action log "
                               f"({', '.join(kept[:3])}); look at them, then delete them", data={"files": kept}))
        return out

    def check_anchors(self) -> list[Finding]:
        """Audit anchors (agentos.node.anchors): per target, the newest mark anchored there, how many
        wait, and the last error. A warning when heads wait long or a target holds another head."""
        anchors = self.anchors
        if anchors is None:
            return []
        st = anchors.status()
        if not st.get("on"):
            return [Finding("audit.anchors", "audit", OK, "Audit anchors",
                            "off: checkpoint heads are not kept anywhere off this node", data=st)]
        if st.get("conflicts"):
            return [Finding("audit.anchors", "audit", CRITICAL, "Audit anchors",
                            "the controller holds another head for a checkpoint this node anchored: the action log "
                            "changed after it was anchored", data=st)]
        waiting = sum(int(t.get("pending") or 0) for t in st.get("per_target", {}).values())
        oldest = st.get("oldest_pending_s")
        if waiting and oldest is not None and oldest >= ANCHOR_WARN_S:
            why = f": {st['last_error']}" if st.get("last_error") else ""
            return [Finding("audit.anchors", "audit", WARN, "Audit anchors",
                            f"{waiting} head(s) have waited {oldest / 60:.0f} minutes to be anchored{why}", data=st)]
        return [Finding("audit.anchors", "audit", OK, "Audit anchors",
                        "every checkpoint head is anchored" if not waiting else f"{waiting} head(s) on their way",
                        data=st)]

    def installed_browser(self) -> tuple[int | None, str | None]:
        now = self.clock()
        if self._installed is None or now - self._installed[0] > DRIFT_CACHE_SECONDS:
            cache = Path(self.config.home) / ".cache"
            info = self.host.camoufox(cache if self.host.exists(cache / "camoufox") else None)
            verstr = info.get("verstr")
            self._installed = (now, fpmod.browser_major(verstr), verstr)
        return self._installed[1], self._installed[2]

    def check_drift(self) -> list[Finding]:
        installed, verstr = self.installed_browser()
        if installed is None:
            return []
        out = []
        for lane in lane_dirs(Path(self.config.home)):
            if not lane.fingerprint.exists():
                continue
            try:
                pinned = fpmod.pinned_major(read_json(lane.fingerprint))
            except (OSError, ValueError, TypeError):
                out.append(Finding(f"drift.{lane.id}", "drift", WARN, f"Fingerprint of {lane.id}",
                                   "the pinned fingerprint cannot be read; restore it from a backup"))
                continue
            verdict = fpmod.compare(installed, pinned)
            if verdict == "older":
                out.append(Finding(f"drift.{lane.id}", "drift", WARN, f"Browser version drift on {lane.id}",
                                   f"pinned Firefox {pinned}, engine Firefox {installed}; run: agentos lane "
                                   f"refresh-fingerprint {lane.id} --keep-device",
                                   {"lane": lane.id, "pinned": pinned, "installed": installed}))
            elif verdict == "newer":
                out.append(Finding(f"drift.{lane.id}", "drift", WARN, f"Browser older than {lane.id}'s pin",
                                   f"pinned Firefox {pinned}, engine Firefox {installed} ({verstr}); update Camoufox",
                                   {"lane": lane.id, "pinned": pinned, "installed": installed}))
            else:
                out.append(Finding(f"drift.{lane.id}", "drift", OK, f"Fingerprint of {lane.id}",
                                   f"pinned to Firefox {pinned}", {"lane": lane.id}))
        return out

    def _probe(self, proxy: ProxyConfig) -> dict[str, Any]:
        result: dict[str, Any] = {"label": proxy.label, "host": proxy.host, "port": proxy.port}
        shared = self._shared_probe(proxy)
        if shared is not None:
            return {**result, **shared}
        try:
            self.host.tcp_connect(proxy.host, proxy.port, timeout=min(10.0, self.proxy_timeout))
        except OSError as e:
            return {**result, "ok": False, "error": f"cannot connect ({redact(str(e), proxy.password)})"}
        try:
            ip = self.host.proxy_exit_ip(proxy, timeout=self.proxy_timeout)
        except OSError as e:
            return {**result, "ok": False, "error": redact(str(e), proxy.username, proxy.password)}
        expected = self.host.resolve(proxy.host) | {proxy.host}
        return {**result, "ok": True, "exit_ip": ip, "expected": sorted(expected),
                "matches": exit_ip_ok(ip, expected)}

    def _shared_probe(self, proxy: ProxyConfig) -> dict[str, Any] | None:
        """This round's probe of the proxy by the lane check, when it made one (no second request)."""
        checker = self.lane_checker
        probe = getattr(checker, "_probes", {}).get(proxy.label) if checker is not None else None
        if probe is None or self.clock() - probe.at > 60.0:
            return None
        if not probe.ok:
            return {"ok": False, "error": probe.error or "no answer"}
        expected = self.host.resolve(proxy.host) | {proxy.host}
        return {"ok": True, "exit_ip": probe.exit_ip, "expected": sorted(expected),
                "matches": exit_ip_ok(probe.exit_ip or "", expected)}

    async def check_lane_reports(self) -> list[Finding]:
        """The lane check of every lane, one finding per lane at its worst level."""
        checker = self.lane_checker
        if checker is None:
            return []
        out = []
        for report in await checker.check_all():
            level = {"fail": CRITICAL, "warn": WARN}.get(report.level, OK)
            title = {CRITICAL: f"Lane {report.lane} is not safe to use",
                     WARN: f"Lane {report.lane} needs a look"}.get(level, f"Lane {report.lane}")
            out.append(Finding(f"lane.{report.lane}", "lane", level, title, report.summary(),
                               {"lane": report.lane, "failing": report.failing(),
                                "command": f"agentos lane check {report.lane}"}))
        return out

    async def check_proxies(self) -> list[Finding]:
        try:
            proxies = self.proxies_loader()
        except (OSError, ValueError) as e:
            return [Finding("proxies", "proxy", WARN, "Proxies", f"secrets/proxies.json cannot be read "
                                                               f"({type(e).__name__})")]
        results = await asyncio.gather(*(asyncio.to_thread(self._probe, p) for p in proxies.values()))
        self.proxy_results = {r["label"]: r for r in results}
        out = []
        for r in results:
            label = r["label"]
            if not r["ok"]:
                out.append(Finding(f"proxy.{label}", "proxy", CRITICAL, f"Proxy {label} is down",
                                   f"{r['host']}:{r['port']}: {r['error']}; lanes on {label} cannot load pages",
                                   {k: r[k] for k in ("label", "host", "port")}))
            elif not r["matches"]:
                out.append(Finding(f"proxy.{label}", "proxy", CRITICAL, f"Proxy {label} exits elsewhere",
                                   f"exit IP {r['exit_ip']} is not the proxy host {r['host']}",
                                   {k: r[k] for k in ("label", "host", "port", "exit_ip")}))
            else:
                out.append(Finding(f"proxy.{label}", "proxy", OK, f"Proxy {label}", f"exit IP {r['exit_ip']}",
                                   {k: r[k] for k in ("label", "host", "port", "exit_ip")}))
        return out

    async def check_lanes(self) -> list[Finding]:
        """Exit IP of every warm lane with a proxy; quarantine on a mismatch."""
        if self.lanes is None:
            return []
        try:
            proxies = self.proxies_loader()
        except (OSError, ValueError):
            proxies = {}
        probe = getattr(self.lanes, "probe_exit_ip", None)
        out = []
        for info in self.lanes.list():
            if info.state not in _WARM or not info.spec.proxy_label:
                continue
            lane_id, label = info.spec.id, info.spec.proxy_label
            proxy = proxies.get(label)
            if proxy is not None and not proxy.check_exit:
                continue  # an environment's tunnel: no account to protect, and it exits from the VM
            if proxy is None:
                continue
            observed: str | None = None
            source = "lane"
            if probe is not None:
                try:
                    observed = await probe(lane_id)
                except Exception as e:  # noqa: BLE001
                    log.info("%s: exit IP probe failed: %s", lane_id, type(e).__name__)
                if observed is not None and getattr(self.lanes, "judges_exit_ip", False):
                    # the lane manager judged this exit itself and quarantined if wrong
                    info = self.lanes.get(lane_id)
                    if getattr(info.state, "value", info.state) == "quarantined":
                        why = getattr(info, "quarantine_reason", None) or "exit IP"
                        self.quarantined[lane_id] = why
                        out.append(Finding(f"exit_ip.{lane_id}", "exit_ip", CRITICAL, f"{lane_id} quarantined",
                                           why, {"lane": lane_id, "exit_ip": observed, "proxy": label}))
                    else:
                        out.append(Finding(f"exit_ip.{lane_id}", "exit_ip", OK, f"Exit IP of {lane_id}",
                                           f"{observed} accepted for proxy {label}",
                                           {"lane": lane_id, "exit_ip": observed}))
                    continue
            if observed is None:
                source = "proxy"
                r = self.proxy_results.get(label) or await asyncio.to_thread(self._probe, proxy)
                if not r.get("ok"):
                    continue  # the proxy check reports it; a dead proxy cannot leak
                observed = r["exit_ip"]
            expected = await asyncio.to_thread(self.host.resolve, proxy.host) | {proxy.host}
            if exit_ip_ok(observed, expected):
                out.append(Finding(f"exit_ip.{lane_id}", "exit_ip", OK, f"Exit IP of {lane_id}",
                                   f"{observed} matches proxy {label}", {"lane": lane_id, "exit_ip": observed}))
                continue
            reason = f"exit IP {observed} does not match proxy {label} ({proxy.host}); found by the health check"
            how = await self.quarantine(lane_id, reason)
            out.append(Finding(f"exit_ip.{lane_id}", "exit_ip", CRITICAL, f"{lane_id} quarantined",
                               f"{reason} (seen via the {source}); the lane was {how}",
                               {"lane": lane_id, "exit_ip": observed, "proxy": label, "action": how}))
        return out

    async def quarantine(self, lane_id: str, reason: str) -> str:
        q = getattr(self.lanes, "quarantine", None)
        try:
            if q is not None:
                result = q(lane_id, reason)
                if inspect.isawaitable(result):
                    await result
                how = "quarantined"
            else:
                await self.lanes.close(lane_id)
                how = "sent cold (its self-check runs again before it can warm)"
        except Exception as e:  # noqa: BLE001
            log.error("%s: could not quarantine: %s", lane_id, type(e).__name__)
            how = f"not stopped ({type(e).__name__}); stop it by hand"
        self.quarantined[lane_id] = reason
        log.warning("%s: %s: %s", lane_id, how, reason)
        return how

    # ---- the whole round ----------------------------------------------------------------

    async def run_once(self, *, proxies: bool = True, lanes: bool = True, drift: bool = True,
                       lane_checks: bool | None = None) -> dict[str, Any]:
        findings: list[Finding] = []
        for check in (self.check_disk, self.check_memory, self.check_audit, self.check_anchors):
            try:
                findings += await asyncio.to_thread(check)
            except Exception as e:
                log.exception("health check %s failed", check.__name__)
                findings.append(Finding(f"error.{check.__name__}", "error", WARN, "Health check failed",
                                        f"{check.__name__}: {type(e).__name__}"))
        if drift and self.lane_checker is None:  # with a lane checker, drift is part of each lane's check
            try:
                findings += await asyncio.to_thread(self.check_drift)
            except Exception as e:  # noqa: BLE001
                findings.append(Finding("error.drift", "error", WARN, "Health check failed", type(e).__name__))
        if lane_checks is None:
            lane_checks = proxies and lanes and self.lanes is not None
        if lane_checks and self.lane_checker is not None:
            try:
                self._lane_findings = await self.check_lane_reports()
            except Exception as e:
                log.exception("lane checks failed")
                self._lane_findings = [Finding("error.lanes", "error", WARN, "Lane checks failed", type(e).__name__)]
        findings += self._lane_findings
        if proxies:
            findings += await self.check_proxies()
        if lanes:
            findings += await self.check_lanes()
        await self._notify(findings)
        levels = [f.level for f in findings]
        overall = CRITICAL if CRITICAL in levels else WARN if WARN in levels else OK
        self.last = {"at": datetime.fromtimestamp(self.clock(), UTC).isoformat(timespec="seconds"),
                     "level": overall, "findings": [f.to_dict() for f in findings],
                     "quarantined": dict(self.quarantined)}
        return self.last

    async def _notify(self, findings: list[Finding]) -> None:
        for f in findings:
            before = self._levels.get(f.key)
            self._levels[f.key] = f.level
            if before == f.level or (before is None and f.level == OK):
                continue
            if f.level == OK:
                event = {"type": "health.recovered", "title": f"Recovered: {f.title}", "body": f.detail,
                         "severity": "info", "key": f.key}
            else:
                event = {"type": f"health.{f.kind}", "title": f.title, "body": f.detail, "severity": f.level,
                         "key": f.key, **{k: v for k, v in f.data.items() if k in ("lane", "proxy", "label")}}
            log.log(logging.WARNING if f.level != OK else logging.INFO, "%s: %s", f.title, f.detail)
            await self.send(event)

    async def send(self, event: dict[str, Any]) -> None:
        send = getattr(self.notifier, "send", None) if self.notifier is not None else None
        if send is None:
            return
        try:
            result = send(event)
            if inspect.isawaitable(result):
                await result
        except Exception as e:  # noqa: BLE001 - notifications never break the health loop
            log.warning("notifier failed: %s", type(e).__name__)
