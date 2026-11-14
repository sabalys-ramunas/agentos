"""The stealth and isolation check: real lanes, a page's own view of them, and how they behave
together (`agentos lane stealth-check`, docs/design/stealth.md).

It opens N scratch lanes (no account, and no proxy unless one is given) in a throwaway
AGENTOS_HOME on a test slot's own displays and ports, never the node's. Each lane loads
stealth_probe.html from a small local server on the slot's last viewer port, which dumps what a
page's own script can read. Then:

    first launch   every lane wakes in turn (the time is the wake time, pin included) and dumps
    all warm       with every lane warm at once: memory per lane (its browser processes' PSS, and the
                   node's available memory before and after) and the warm lanes this node could hold
    relaunches     each lane sleeps and wakes again R times, and dumps again (drift)
    public         optionally, the first lane visits public fingerprint test pages (screenshots and
                   page text kept in the output folder; nothing is scored here)
    soak           optionally, for M minutes: lanes wake, load the page and sleep at random, and every
                   K seconds one lane's browser is killed while another lane loads a page. The other
                   lane must finish untouched (same browser process), and the killed one must wake
                   again with the same pinned device. Process and descriptor counts are sampled.

The findings come from agentos.stealth.stealth. Before each wake the node's available memory is read,
and the check stops (closing its lanes) when it is under `min_available_mb`.

The real lanes are behind the small `Lab` interface so the unit tests drive the check with a fake.
camoufox and playwright are imported only by NodeLab, through the lane manager.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shutil
import signal
import statistics
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from agentos.core.system import meminfo_mb
from agentos.core.text import first_line
from agentos.stealth import stealth
from agentos.stealth.stealth import FAIL, OK, WARN, Finding

log = logging.getLogger("agentos.ops.stealthcheck")

WORKSPACE = "stealth"
PROBE_TIMEOUT = 40.0
PUBLIC_PAGES = {
    "creepjs": "https://abrahamjuliot.github.io/creepjs/",
    "sannysoft": "https://bot.sannysoft.com/",
    "browserscan": "https://www.browserscan.net/",
    "pixelscan": "https://pixelscan.net/fingerprint-check",
}
PUBLIC_SETTLE = 20.0
RESERVED_PORTS = (5900, 6080, 7400, *range(5910, 5920), *range(6100, 6110))
RESERVED_DISPLAYS = (99, *range(100, 110))


# ---- slots ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Slot:
    """A test slot's ranges, as scripts/node-test.sh hands them out: displays :S0 to :S9, VNC ports
    5000 + 10S, viewer ports 6000 + 10S. The probe page is served on the last viewer port."""

    number: int

    @property
    def display_base(self) -> int:
        return self.number * 10

    @property
    def vnc_base(self) -> int:
        return 5000 + self.number * 10

    @property
    def viewer_base(self) -> int:
        return 6000 + self.number * 10

    @property
    def site_port(self) -> int:
        return self.viewer_base + 9


def slot(number: int, nodes: Iterable[Any] = (), *, home: Path | None = None) -> Slot:
    """The slot, or ValueError when it is out of range or touches the PoC or a node on this machine:
    any display, port or home of the node configurations in `nodes` (node_configs()). `home` is
    the scratch home, which must not be inside a node's home or hold one."""
    if not 11 <= number <= 59:
        raise ValueError(f"the slot must be 11 to 59 (got {number})")
    s = Slot(number)
    ports = set(range(s.vnc_base, s.vnc_base + 10)) | set(range(s.viewer_base, s.viewer_base + 10))
    displays = set(range(s.display_base, s.display_base + 10))
    hit = sorted(ports.intersection(RESERVED_PORTS))
    if hit:
        raise ValueError(f"slot {number} would use reserved port {hit[0]}")
    if displays.intersection(RESERVED_DISPLAYS):
        raise ValueError(f"slot {number} would use a reserved display")
    for node in nodes:
        theirs = {node.mcp_port, *range(node.vnc_base, node.vnc_base + node.max_slots),
                  *range(node.viewer_base, node.viewer_base + node.max_slots)}
        hit = sorted(ports & theirs)
        if hit:
            raise ValueError(f"slot {number} would use port {hit[0]} of the node in {node.home}")
        hit = sorted(displays & set(range(node.display_base, node.display_base + node.max_slots)))
        if hit:
            raise ValueError(f"slot {number} would use display :{hit[0]} of the node in {node.home}")
        if home is not None and (home.resolve().is_relative_to(Path(node.home).resolve())
                                 or Path(node.home).resolve().is_relative_to(home.resolve())):
            raise ValueError(f"the scratch home {home} overlaps the node home {node.home}")
    return s


def node_configs(homes: Iterable[Path] | None = None) -> list[Any]:
    """The configuration of every node on this machine the check must stay clear of: by default
    $AGENTOS_HOME's, the default home's (/var/lib/agentos) and each running agentosd's (read from
    its environment). A home that exists without node.toml has the defaults; one that does not
    exist is skipped. ValueError when a node.toml cannot be read: then nothing is known to be free."""
    from agentos.core.config import DEFAULT_HOME, config_path, home_from_env, load_config

    if homes is None:
        homes = [home_from_env(), DEFAULT_HOME, *daemon_homes()]
    out, seen = [], set()
    for home in homes:
        home = Path(home)
        if home in seen or not home.is_dir():
            continue
        seen.add(home)
        try:
            out.append(load_config(home))
        except (OSError, ValueError) as e:
            raise ValueError(f"cannot read {config_path(home)} ({first_line(e)}), so the slots it uses are "
                             "unknown") from None
    return out


def daemon_homes() -> list[Path]:
    """AGENTOS_HOME of every running agentosd (`agentos node serve`) whose environment can be read;
    the default home for one that sets none."""
    from agentos.core.config import DEFAULT_HOME
    from agentos.node.display import iter_processes

    homes = []
    for pid, argv in iter_processes():
        words = argv[:4]
        if not (any(os.path.basename(a) == "agentos" for a in words[:2]) and "node" in words and "serve" in words):
            continue
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            log.warning("cannot read the environment of agentosd (pid %d); assuming %s", pid, DEFAULT_HOME)
            homes.append(DEFAULT_HOME)
            continue
        found = [e.split(b"=", 1)[1].decode(errors="replace") for e in env if e.startswith(b"AGENTOS_HOME=")]
        homes.append(Path(found[0]) if found else DEFAULT_HOME)
    return homes


# ---- scratch homes ------------------------------------------------------------------------------
#
# Every check works in a fresh home named SCRATCH_PREFIX..., with an owner file holding the check's
# pid and start time. A check that is killed outright (SIGKILL, a crash) leaves its browsers, Xvfb
# and x11vnc behind; the next check finds them by that prefix and stops them once their owner is
# gone. SIGTERM, SIGHUP and Ctrl-C close the lanes the normal way (run_until_stopped).

SCRATCH_PREFIX = "agentos-stealth-"
SCRATCH_NAME = re.compile(re.escape(SCRATCH_PREFIX) + r"[a-z0-9_]{8}")  # tempfile.mkdtemp's names
SCRATCH_PROCESSES = ("Xvfb", "x11vnc")  # and Camoufox browsers (engine_camoufox._is_browser)
OWNER_FILE = ".stealth-check-owner"
ORPHAN_GRACE = 60.0  # a home without an owner file this young may be a check that is still starting


def _start_ticks(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def claim_home(home: Path) -> None:
    """Mark `home` as this process's scratch home."""
    (home / OWNER_FILE).write_text(f"{os.getpid()} {_start_ticks(os.getpid()) or ''}\n")


def _owner_alive(home: Path) -> bool:
    try:
        pid_text, _, ticks = (home / OWNER_FILE).read_text().strip().partition(" ")
        pid = int(pid_text)
    except FileNotFoundError:
        try:
            return time.time() - home.stat().st_mtime < ORPHAN_GRACE
        except OSError:
            return False  # the home is gone and something still names it
    except (OSError, ValueError):
        return False
    from agentos.node.display import pid_alive
    return pid_alive(pid) and (not ticks or _start_ticks(pid) == ticks)


def _scratch_home_of(arg: str) -> Path | None:
    """The scratch home a path inside one belongs to (the path up to the SCRATCH_PREFIX folder)."""
    if not arg.startswith("/"):
        return None
    parts = Path(arg).parts
    for i, part in enumerate(parts):
        if SCRATCH_NAME.fullmatch(part):
            return Path(*parts[:i + 1])
    return None


async def sweep_leftovers(root: Path, *, keep: Path | None = None) -> list[int]:
    """Stop what checks killed outright left: every process that names a scratch home whose owner
    is gone (browsers, Xvfb, x11vnc), then remove those homes under `root`. Never `keep` (this
    check's own home) or a home whose check still runs. Returns the pids stopped."""
    from agentos.node.camoufox.engine import _is_browser
    from agentos.node.display import iter_processes, kill_processes

    homes: dict[Path, list[int]] = {}
    for pid, argv in iter_processes():
        if os.path.basename(argv[0]) not in SCRATCH_PROCESSES and not _is_browser(argv):
            continue
        for arg in argv[1:]:
            found = _scratch_home_of(arg.split("=", 1)[-1])
            if found is not None:
                homes.setdefault(found, []).append(pid)
                break
    try:
        for entry in root.iterdir():
            if SCRATCH_NAME.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink():
                homes.setdefault(entry, [])
    except OSError:
        pass
    stopped: list[int] = []
    for home, pids in homes.items():
        if keep is not None and home == keep or _owner_alive(home):
            continue
        if pids:
            log.warning("stopping %d processes left by an earlier stealth check in %s", len(pids), home)
            await kill_processes(pids)
            stopped += pids
        if home.parent == root and home.is_dir() and not home.is_symlink():
            shutil.rmtree(home, ignore_errors=True)
    return stopped


class Stopped(Exception):
    """The check was stopped by a signal (after closing its lanes)."""

    def __init__(self, signum: int):
        super().__init__(f"stopped by {signal.Signals(signum).name}")
        self.signum = signum


async def run_until_stopped(lab: Lab, opts: Options, *, say: Callable[[str], None] = lambda _: None,
                            signals: tuple[int, ...] = (signal.SIGTERM, signal.SIGHUP)) -> Report:
    """run(), where SIGTERM and SIGHUP (a closed terminal) stop the check as Ctrl-C does: the lanes
    close and the lab stops before Stopped is raised. A second signal while closing is ignored."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    caught: list[int] = []

    def stop(signum: int) -> None:
        if not caught and task is not None:
            caught.append(signum)
            task.cancel()

    for signum in signals:
        loop.add_signal_handler(signum, stop, signum)
    try:
        return await run(lab, opts, say=say)
    except asyncio.CancelledError:
        if caught:
            raise Stopped(caught[0]) from None
        raise
    finally:
        for signum in signals:
            loop.remove_signal_handler(signum)


# ---- what the check drives ----------------------------------------------------------------------

class Lab(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def create(self, lane: str, *, os_name: str | None = None) -> None: ...
    async def wake(self, lane: str) -> None: ...
    async def sleep(self, lane: str) -> None: ...
    async def dump(self, lane: str) -> dict[str, Any]: ...
    async def visit(self, lane: str, url: str, out: Path) -> dict[str, Any]: ...
    async def kill(self, lane: str) -> list[int]: ...
    def main_pid(self, lane: str) -> int | None: ...
    def pin(self, lane: str) -> dict[str, Any] | None: ...
    def lane_memory_mb(self, lane: str) -> float | None: ...
    def available_mb(self) -> int | None: ...
    def total_mb(self) -> int | None: ...
    def census(self) -> dict[str, int]: ...
    @property
    def proxied(self) -> bool: ...
    @property
    def installed_major(self) -> int | None: ...
    @property
    def versions(self) -> dict[str, Any]: ...


@dataclass
class Options:
    lanes: int = 3
    relaunches: int = 2
    os_names: list[str] = field(default_factory=list)  # cycled over the lanes; empty: the node's lane_os
    public: bool = False
    soak_minutes: float = 0.0
    kill_every: float = 60.0
    min_available_mb: int = 2048
    floor_mb: int = 800            # the lane manager's memory floor, for the warm-lane estimate
    out: Path | None = None
    seed: int | None = None


class LowMemory(Exception):
    pass


@dataclass
class Report:
    started: float = field(default_factory=time.time)
    options: dict[str, Any] = field(default_factory=dict)
    versions: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    wakes: dict[str, list[float]] = field(default_factory=dict)       # lane -> wake seconds, first launch first
    memory: dict[str, Any] = field(default_factory=dict)
    soak: dict[str, Any] = field(default_factory=dict)
    public: dict[str, Any] = field(default_factory=dict)
    stopped: str | None = None

    @property
    def level(self) -> str:
        return stealth.worst(self.findings)

    def add(self, findings: list[Finding]) -> None:
        self.findings.extend(findings)

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started)),
                "options": self.options, "versions": self.versions, "stopped": self.stopped,
                "wakes": self.wakes, "wake_summary": wake_summary(self.wakes), "memory": self.memory,
                "soak": self.soak, "public": self.public, "findings": [f.to_dict() for f in self.findings]}


def wake_summary(wakes: dict[str, list[float]]) -> dict[str, Any]:
    first = [w[0] for w in wakes.values() if w]
    later = [x for w in wakes.values() for x in w[1:]]

    def stats(values: list[float]) -> dict[str, float] | None:
        if not values:
            return None
        ordered = sorted(values)
        return {"n": len(values), "median_s": round(statistics.median(values), 2),
                "p90_s": round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 2),
                "max_s": round(max(values), 2)}

    return {"first_launch": stats(first), "relaunch": stats(later)}


def warm_capacity(total_mb: int | None, base_used_mb: float | None, per_lane_mb: float | None,
                  floor_mb: int) -> dict[str, Any]:
    """How many lanes could be warm at once on a node of this size and of a few common sizes: what is
    left after the node's own use (the OS, agentosd) and the memory floor, over the measured cost of
    one warm lane (browser, display and driver)."""
    if not per_lane_mb or per_lane_mb <= 0:
        return {}
    base = base_used_mb if base_used_mb is not None else 700.0
    out: dict[str, Any] = {"per_lane_mb": round(per_lane_mb), "node_base_mb": round(base), "floor_mb": floor_mb}
    sizes = {"this node": total_mb} if total_mb else {}
    sizes.update({"4 GB": 4096, "8 GB": 8192, "16 GB": 16384})
    out["max_warm"] = {name: max(0, int((size - base - floor_mb) // per_lane_mb))
                       for name, size in sizes.items() if size}
    return out


# ---- the check ------------------------------------------------------------------------------------

async def run(lab: Lab, opts: Options, *, say: Callable[[str], None] = lambda _: None) -> Report:
    report = Report(options={"lanes": opts.lanes, "relaunches": opts.relaunches, "os": opts.os_names,
                             "public": opts.public, "soak_minutes": opts.soak_minutes,
                             "kill_every": opts.kill_every, "min_available_mb": opts.min_available_mb})
    rng = random.Random(opts.seed)
    lanes = [f"{WORKSPACE}/l{i + 1}" for i in range(opts.lanes)]
    dumps: dict[str, list[dict[str, Any]]] = {lane: [] for lane in lanes}
    await lab.start()
    report.versions = dict(lab.versions)
    try:
        for i, lane in enumerate(lanes):
            await lab.create(lane, os_name=opts.os_names[i % len(opts.os_names)] if opts.os_names else None)
        say(f"{len(lanes)} scratch lanes made")
        await _first_launches(lab, opts, report, lanes, dumps, say)
        await _relaunches(lab, opts, report, lanes, dumps, say)
        if opts.public:
            await _public(lab, opts, report, lanes[0], say)
        if opts.soak_minutes > 0:
            await soak(lab, opts, report, lanes, rng=rng, say=say)
    except LowMemory as e:
        report.stopped = str(e)
        report.add([Finding("resources", WARN, f"stopped early: {e}")])
        say(f"stopped early: {e}")
    finally:
        for lane in lanes:
            try:
                await lab.sleep(lane)
            except Exception as e:  # noqa: BLE001 - every lane gets its close
                log.warning("closing %s: %s", lane, e)
        census = lab.census()
        await lab.stop()
        left = {k: v for k, v in lab.census().items() if v and k != "fds"}
        if left:
            report.add([Finding("resources", FAIL, f"left running after the check: {left}", data=left)])
        report.memory.setdefault("census_before_stop", census)
    pins = _analyse(lab, report, dumps)
    if opts.out is not None:
        write_outputs(opts.out, report, dumps, pins)
    return report


def _guard(lab: Lab, opts: Options, what: str) -> None:
    available = lab.available_mb()
    if available is not None and available < opts.min_available_mb:
        raise LowMemory(f"only {available} MB of memory is available, under the {opts.min_available_mb} MB this "
                        f"check keeps free; did not {what}")


async def _wake(lab: Lab, opts: Options, report: Report, lane: str) -> float:
    _guard(lab, opts, f"wake {lane}")
    t0 = time.monotonic()
    await lab.wake(lane)
    took = time.monotonic() - t0
    report.wakes.setdefault(lane, []).append(round(took, 2))
    return took


async def _first_launches(lab: Lab, opts: Options, report: Report, lanes: list[str],
                          dumps: dict[str, list[dict[str, Any]]], say: Callable[[str], None]) -> None:
    before = lab.available_mb()
    total = lab.total_mb()
    warm: list[str] = []
    try:
        for lane in lanes:
            took = await _wake(lab, opts, report, lane)
            warm.append(lane)
            dumps[lane].append(await lab.dump(lane))
            say(f"{lane}: first launch in {took:.1f} s")
        await asyncio.sleep(2.0)  # let the content processes settle before measuring
        per_lane = {lane: lab.lane_memory_mb(lane) for lane in warm}
        after = lab.available_mb()
        measured = [v for v in per_lane.values() if v]
        report.memory.update({
            "available_before_mb": before, "available_all_warm_mb": after, "total_mb": total, "warm": len(warm),
            "browser_pss_mb": {k: round(v) for k, v in per_lane.items() if v},
        })
        delta = (before - after) / len(warm) if before is not None and after is not None and warm else None
        if delta is not None:
            report.memory["per_lane_delta_mb"] = round(delta)
        if measured:
            report.memory["browser_pss_mean_mb"] = round(statistics.mean(measured))
        cost = delta if delta and delta > 0 else (statistics.mean(measured) if measured else None)
        base = (total - before) if total is not None and before is not None else None
        report.memory["capacity"] = warm_capacity(total, base, cost, opts.floor_mb)
    finally:
        for lane in warm:
            await lab.sleep(lane)


async def _relaunches(lab: Lab, opts: Options, report: Report, lanes: list[str],
                      dumps: dict[str, list[dict[str, Any]]], say: Callable[[str], None]) -> None:
    for n in range(opts.relaunches):
        for lane in lanes:
            took = await _wake(lab, opts, report, lane)
            try:
                dumps[lane].append(await lab.dump(lane))
            finally:
                await lab.sleep(lane)
            say(f"{lane}: relaunch {n + 1} in {took:.1f} s")


async def _public(lab: Lab, opts: Options, report: Report, lane: str, say: Callable[[str], None]) -> None:
    out = (opts.out or Path(".")) / "public"
    await _wake(lab, opts, report, lane)
    try:
        for name, url in PUBLIC_PAGES.items():
            try:
                report.public[name] = await lab.visit(lane, url, out / name)
            except Exception as e:  # noqa: BLE001 - one page down is not the check's failure
                report.public[name] = {"url": url, "error": str(e).splitlines()[0][:200]}
            say(f"{lane}: visited {name}")
    finally:
        await lab.sleep(lane)


def _analyse(lab: Lab, report: Report, dumps: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Adds the findings to the report; returns each lane's pin."""
    pins: dict[str, tuple[dict[str, Any], str | None]] = {}
    for lane, seen in dumps.items():
        pin = lab.pin(lane)
        config, meta = stealth.split_pin(pin or {})
        if pin:
            pins[lane] = (pin, "test-proxy" if lab.proxied else None)
            report.add(stealth.pin_findings(lane, pin, installed_major=lab.installed_major))
        if seen:
            report.add(stealth.dump_findings(lane, seen[0], config=config, proxied=lab.proxied))
            report.add(stealth.drift_findings(lane, seen, canvas_noise=bool(meta.get("canvas_noise"))))
    if len(pins) > 1:
        # scratch lanes all leave through the one test proxy (or none): only collisions of devices count
        report.add([f for f in stealth.cross_pin_findings(pins) if not f.data.get("proxy")])
    first = {lane: seen[0] for lane, seen in dumps.items() if seen}
    report.add(stealth.collision_findings(first))
    return {lane: data for lane, (data, _) in pins.items()}


# ---- soak ------------------------------------------------------------------------------------------

async def soak(lab: Lab, opts: Options, report: Report, lanes: list[str], *, rng: random.Random,
               say: Callable[[str], None] = lambda _: None, clock: Callable[[], float] = time.monotonic) -> None:
    """Lanes wake, load the page and sleep at random until the time is up; every kill_every seconds
    one lane's browser is killed while another lane loads a page (needs two lanes)."""
    deadline = clock() + opts.soak_minutes * 60
    next_kill = clock() + opts.kill_every
    warm: set[str] = set()
    pins = {lane: json.dumps(lab.pin(lane), sort_keys=True) for lane in lanes}
    stats: dict[str, Any] = {"cycles": 0, "loads": 0, "load_errors": [], "kills": [], "samples": []}
    report.soak = stats
    try:
        while clock() < deadline:
            lane = rng.choice(lanes)
            if lane not in warm:
                await _wake(lab, opts, report, lane)
                warm.add(lane)
            try:
                await lab.dump(lane)
                stats["loads"] += 1
            except Exception as e:  # noqa: BLE001 - recorded, the soak goes on
                stats["load_errors"].append({"lane": lane, "error": str(e).splitlines()[0][:200]})
            if rng.random() < 0.35:
                await lab.sleep(lane)
                warm.discard(lane)
            if len(lanes) > 1 and clock() >= next_kill:
                stats["kills"].append(await _kill_one(lab, opts, report, lanes, warm, pins, rng))
                next_kill = clock() + opts.kill_every
            stats["cycles"] += 1
            stats["samples"].append({"t": round(time.time()), "available_mb": lab.available_mb(),
                                     "warm": len(warm), **lab.census()})
            say(f"soak: {stats['cycles']} cycles, {len(stats['kills'])} kills, {len(warm)} warm")
    finally:
        for lane in list(warm):
            await lab.sleep(lane)
    report.add(soak_findings(stats))


async def _kill_one(lab: Lab, opts: Options, report: Report, lanes: list[str], warm: set[str],
                    pins: dict[str, str], rng: random.Random) -> dict[str, Any]:
    victim, bystander = rng.sample(lanes, 2)
    for lane in (victim, bystander):
        if lane not in warm:
            await _wake(lab, opts, report, lane)
            warm.add(lane)
    before = lab.main_pid(bystander)  # its content processes come and go with every page; the browser must not
    load = asyncio.ensure_future(lab.dump(bystander))
    await asyncio.sleep(0.3)  # the bystander's page is loading
    killed = await lab.kill(victim)
    record: dict[str, Any] = {"victim": victim, "bystander": bystander, "killed": killed}
    try:
        await load
        record["bystander_ok"] = True
    except Exception as e:  # noqa: BLE001
        record["bystander_ok"] = False
        record["bystander_error"] = str(e).splitlines()[0][:200]
    record["bystander_untouched"] = before is not None and lab.main_pid(bystander) == before
    try:
        record["recovered_in_s"] = round(await _wake(lab, opts, report, victim), 2)
        await lab.dump(victim)
        record["recovered"] = True
    except LowMemory:
        raise
    except Exception as e:  # noqa: BLE001
        record["recovered"] = False
        record["error"] = str(e).splitlines()[0][:200]
    record["same_device"] = json.dumps(lab.pin(victim), sort_keys=True) == pins[victim]
    return record


def soak_findings(stats: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    for kill in stats.get("kills", []):
        who = f"killing {kill['victim']}'s browser"
        if not kill.get("bystander_ok") or not kill.get("bystander_untouched"):
            out.append(Finding("soak", FAIL, f"{who} disturbed {kill['bystander']}: "
                               f"{kill.get('bystander_error') or 'its browser process changed'}", data=kill))
        if not kill.get("recovered"):
            out.append(Finding("soak", FAIL, f"{who}: the lane did not wake again ({kill.get('error')})", data=kill))
        if not kill.get("same_device", True):
            out.append(Finding("soak", FAIL, f"{who}: the lane's pin changed", data=kill))
    if stats.get("load_errors"):
        out.append(Finding("soak", WARN, f"{len(stats['load_errors'])} page loads failed during the soak",
                           data={"errors": stats["load_errors"][:10]}))
    samples = stats.get("samples") or []
    if len(samples) >= 6:
        third = len(samples) // 3
        for key in ("fds", "driver"):
            early = [s.get(key) or 0 for s in samples[:third]]
            late = [s.get(key) or 0 for s in samples[-third:]]
            if early and late and max(late) > max(early) * 1.5 + 10:
                out.append(Finding("soak", WARN, f"{key} grew during the soak ({max(early)} early, {max(late)} late)"))
    if not out:
        out.append(Finding("soak", OK, f"{stats.get('cycles', 0)} cycles, {stats.get('loads', 0)} page loads and "
                                       f"{len(stats.get('kills', []))} kills: every other lane was untouched and every "
                                       "killed lane woke again as the same device"))
    return out


# ---- output ----------------------------------------------------------------------------------------

def write_outputs(out: Path, report: Report, dumps: dict[str, list[dict[str, Any]]],
                  pins: dict[str, dict[str, Any]] | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    if pins:  # scratch lanes' pins: no account, no secret
        (out / "pins").mkdir(exist_ok=True)
        for lane, data in pins.items():
            (out / "pins" / f"{lane.replace('/', '-')}.json").write_text(json.dumps(data, indent=1, sort_keys=True))
    (out / "report.json").write_text(json.dumps(report.to_dict(), indent=1, default=str))
    (out / "report.txt").write_text(format_report(report) + "\n")
    folder = out / "dumps"
    folder.mkdir(exist_ok=True)
    for lane, seen in dumps.items():
        for n, dump in enumerate(seen, start=1):
            (folder / f"{lane.replace('/', '-')}-{n}.json").write_text(json.dumps(dump, indent=1, sort_keys=True))


def format_report(report: Report) -> str:
    lines = [f"stealth check: {report.level}"]
    if report.versions:
        lines.append("versions: " + ", ".join(f"{k} {v}" for k, v in report.versions.items()))
    if report.stopped:
        lines.append(f"stopped early: {report.stopped}")
    for name, stats in wake_summary(report.wakes).items():
        if stats:
            lines.append(f"wake ({name.replace('_', ' ')}): median {stats['median_s']} s, p90 {stats['p90_s']} s, "
                         f"max {stats['max_s']} s over {stats['n']}")
    mem = report.memory
    if mem.get("browser_pss_mean_mb") is not None:
        lines.append(f"memory: browser PSS {mem['browser_pss_mean_mb']} MB per warm lane on average; available "
                     f"{mem.get('available_before_mb')} MB before, {mem.get('available_all_warm_mb')} MB with "
                     f"{mem.get('warm')} warm ({mem.get('per_lane_delta_mb')} MB per lane)")
    capacity = mem.get("capacity") or {}
    if capacity.get("max_warm"):
        lines.append(f"max warm lanes ({capacity['per_lane_mb']} MB each, {capacity['floor_mb']} MB floor): "
                     + ", ".join(f"{k} {v}" for k, v in capacity["max_warm"].items()))
    if report.soak:
        lines.append(f"soak: {report.soak.get('cycles')} cycles, {len(report.soak.get('kills', []))} kills")
    for name, seen in report.public.items():
        lines.append(f"public {name}: {seen.get('error') or seen.get('summary') or 'kept'}")
    for f in sorted(report.findings, key=lambda f: (-stealth.RANK[f.level], f.check, f.lane or "")):
        lines.append(f"  {f.level:<4}  {f.check:<11} {(f.lane or '-'):<12} {f.detail}")
    return "\n".join(lines)


# ---- the real lanes -----------------------------------------------------------------------------------

class _ProbeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/headers":
            body = json.dumps({k.lower(): v for k, v in self.headers.items()
                               if k.lower() not in ("host", "connection")}, sort_keys=True).encode()
            kind = "application/json"
        elif path in ("/", "/fingerprint.html"):
            body, kind = stealth.PROBE_PAGE.read_bytes(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def pss_mb(pids: list[int]) -> float | None:
    """The proportional set size of these processes, in MB (shared pages split between sharers)."""
    total, seen = 0, False
    for pid in pids:
        try:
            for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
                if line.startswith("Pss:"):
                    total += int(line.split()[1])
                    seen = True
                    break
        except (OSError, ValueError, IndexError):
            continue
    return total / 1024 if seen else None


class NodeLab:
    """Scratch lanes on this node: the production lane manager, Camoufox engine and displays, in a
    throwaway home on a test slot. The engine may reach private addresses (the probe server on
    localhost), so these lanes skip the network guard's PAC; nothing else differs from a lane."""

    def __init__(self, home: Path, test_slot: Slot, *, proxy: Any = None):
        self.home = home
        self.slot = test_slot
        self.proxy = proxy
        self._server: ThreadingHTTPServer | None = None
        self._mgr: Any = None
        self._os: dict[str, str | None] = {}
        self._versions: dict[str, Any] = {}

    @property
    def proxied(self) -> bool:
        return self.proxy is not None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.slot.site_port}/fingerprint.html"

    @property
    def versions(self) -> dict[str, Any]:
        return self._versions

    @property
    def installed_major(self) -> int | None:
        from agentos.node.fingerprint import browser_major
        return browser_major(self._versions.get("camoufox_browser"))

    async def start(self) -> None:
        from agentos.core.config import NodeConfig, save_proxies
        from agentos.node.display import DisplayManagerImpl
        from agentos.node.lanes import build_lane_manager

        self.home.mkdir(parents=True, exist_ok=True)
        claim_home(self.home)
        await sweep_leftovers(self.home.parent, keep=self.home)
        config = NodeConfig(home=self.home, display_base=self.slot.display_base, vnc_base=self.slot.vnc_base,
                            viewer_base=self.slot.viewer_base, max_slots=9, max_warm_lanes=9,
                            idle_seconds=86400, control_idle_seconds=86400, self_check=self.proxied)
        if self.proxy is not None:
            save_proxies(config, {self.proxy.label: self.proxy})
        self._server = ThreadingHTTPServer(("127.0.0.1", self.slot.site_port), _ProbeHandler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name="stealth-probe", daemon=True).start()
        self._mgr = build_lane_manager(config, displays=DisplayManagerImpl(config, sweep_own_only=True))
        self._mgr.engine.allow_private_network = True  # only so the lanes reach the probe server
        await self._mgr.start()
        self._versions = await asyncio.to_thread(_versions)

    async def stop(self) -> None:
        if self._mgr is not None:
            await self._mgr.shutdown()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    async def create(self, lane: str, *, os_name: str | None = None) -> None:
        from agentos.core.models import LaneSpec
        self._os[lane] = os_name
        await self._mgr.create(LaneSpec(id=lane, proxy_label=self.proxy.label if self.proxy else None))

    async def wake(self, lane: str) -> None:
        # The identity's OS only matters on a lane's first launch; later launches replay the pin.
        # Without --os, the node's default (lane_os) as for a production lane.
        default = list(self._mgr.config.lane_os) or None
        self._mgr.engine.identity_os = [self._os[lane]] if self._os.get(lane) else default
        await self._mgr.open(lane)

    async def sleep(self, lane: str) -> None:
        await self._mgr.close(lane)

    async def dump(self, lane: str) -> dict[str, Any]:
        async with self._mgr.using(lane) as page:
            await page.goto(self.url, timeout=PROBE_TIMEOUT * 1000)
            await page.wait_for_selector("#dump[data-state=done]", state="attached", timeout=PROBE_TIMEOUT * 1000)
            return json.loads(await page.text_content("#dump"))

    def using(self, lane: str) -> Any:
        """The lane's page for a block of work (the lane manager's own lease)."""
        return self._mgr.using(lane)

    async def visit(self, lane: str, url: str, out: Path) -> dict[str, Any]:
        out.parent.mkdir(parents=True, exist_ok=True)
        async with self._mgr.using(lane) as page:
            await page.goto(url, timeout=60_000, wait_until="load")
            await page.wait_for_timeout(PUBLIC_SETTLE * 1000)
            await page.screenshot(path=str(out.with_suffix(".png")), full_page=True)
            text = await page.inner_text("body")
        out.with_suffix(".txt").write_text(text)
        summary = f"{text.count('failed')} 'failed' cells" if "sannysoft" in url else None
        return {"url": url, "screenshot": str(out.with_suffix(".png")), "summary": summary}

    async def kill(self, lane: str) -> list[int]:
        """SIGKILL the lane's browser (its main process), as a crash would, and wait until the lane
        manager can see it died."""
        main = self.main_pid(lane)
        if main is None:
            return []
        os.kill(main, signal.SIGKILL)
        session = self._session(lane)
        for _ in range(50):
            if session is None or not session.alive:
                break
            await asyncio.sleep(0.1)
        return [main]

    def _session(self, lane: str) -> Any:
        state = self._mgr._lanes.get(lane)
        return state.session if state is not None else None

    def main_pid(self, lane: str) -> int | None:
        session = self._session(lane)
        pid = getattr(session, "pid", None)
        return pid if pid and getattr(session, "alive", False) else None

    def pin(self, lane: str) -> dict[str, Any] | None:
        path = self._mgr.config.fingerprint_path(lane)
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def lane_memory_mb(self, lane: str) -> float | None:
        return pss_mb(list(self._mgr.lane_pids().get(lane, [])))

    def available_mb(self) -> int | None:
        return meminfo_mb("MemAvailable")

    def total_mb(self) -> int | None:
        return meminfo_mb("MemTotal")

    def census(self) -> dict[str, int]:
        """This check's own processes and descriptors: browsers under its home, the slot's Xvfb and
        x11vnc, Playwright drivers started by this process, and this process's open descriptors."""
        from agentos.node.camoufox.engine import browser_pids_under
        from agentos.node.display import iter_processes

        displays = {f":{self.slot.display_base + i}" for i in range(10)}
        me = os.getpid()
        counts = {"browsers": len(browser_pids_under(self.home)), "xvfb": 0, "x11vnc": 0, "driver": 0}
        for pid, argv in iter_processes():
            name = os.path.basename(argv[0]) if argv else ""
            if name == "Xvfb" and len(argv) > 1 and argv[1] in displays:
                counts["xvfb"] += 1
            elif name == "x11vnc" and any(a in displays for a in argv):
                counts["x11vnc"] += 1
            elif name == "node" and any("playwright" in a for a in argv) and _parent(pid) == me:
                counts["driver"] += 1
        try:
            counts["fds"] = len(os.listdir("/proc/self/fd"))
        except OSError:
            pass
        return counts


def _parent(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _versions() -> dict[str, Any]:
    from importlib.metadata import version

    out: dict[str, Any] = {}
    for package in ("camoufox", "playwright"):
        try:
            out[package] = version(package)
        except Exception:  # noqa: BLE001
            out[package] = None
    try:
        from agentos.core.system import Host
        out["camoufox_browser"] = Host().camoufox(None).get("verstr")
    except Exception:  # noqa: BLE001
        out["camoufox_browser"] = None
    return out
