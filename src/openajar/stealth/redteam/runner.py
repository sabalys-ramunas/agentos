"""The red-team bench: lanes visit the detector site the way agents use them, and every detector
gives its verdict (docs/design/redteam.md).

Backends:

    LocalLab   plain Camoufox on this machine (humanize on, as the engine launches it), one browser
               per lane. Fast, for working on the input path; not the node's pins, Xvfb or proxy.
    NodeBench  scratch lanes on a node test slot through `stealthcheck.NodeLab`: the production lane
               manager, engine, pinned fingerprints and displays.

Each run: every lane makes one visit (openajar.stealth.redteam.scenario), and once per lane the stealth
probe is read and checked (`stealth.dump_findings`). Then the detectors judge each visit, the
pooled detectors judge all visits together (against human baselines when given), and the lanes
are compared with each other. Human baseline sessions are judged too: a detector that catches a
person is wrong, and the report lists it as a false positive.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from openajar.stealth.redteam import detectors as d
from openajar.stealth.redteam import engine, scenario
from openajar.stealth.redteam.server import DetectorServer

PROBE_TIMEOUT_MS = 60_000
LEVEL_OF = {"fail": d.CAUGHT, "warn": d.SUSPICIOUS, "ok": d.CLEAN}
RESULTS = Path("bench/redteam.jsonl")


class Lab(Protocol):
    name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def create(self, lane: str, *, os_name: str | None = None) -> None: ...
    def using(self, lane: str) -> contextlib.AbstractAsyncContextManager[Any]: ...
    def pin(self, lane: str) -> dict[str, Any] | None: ...
    def versions(self) -> dict[str, Any]: ...


@dataclass
class Options:
    lanes: int = 1
    runs: int = 1
    os_names: list[str] = field(default_factory=list)
    humans: list[dict[str, Any]] = field(default_factory=list)
    probe: bool = True
    out: Path | None = None


@dataclass
class Report:
    backend: str
    versions: dict[str, Any]
    sessions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)   # lane -> visits
    steps: dict[str, list[list[dict[str, Any]]]] = field(default_factory=dict)
    verdicts: dict[str, list[d.Verdict]] = field(default_factory=dict)       # session id -> verdicts
    pooled: list[d.Verdict] = field(default_factory=list)
    probe: dict[str, list[d.Verdict]] = field(default_factory=dict)          # lane -> probe verdicts
    engine: dict[str, list[d.Verdict]] = field(default_factory=dict)         # lane -> engine probe verdicts
    deep: dict[str, dict[str, Any]] = field(default_factory=dict)            # lane -> engine probe raw
    links: list[d.Verdict] = field(default_factory=list)
    false_positives: dict[str, list[str]] = field(default_factory=dict)      # human session -> detectors

    def all_verdicts(self) -> list[d.Verdict]:
        out = [v for vs in self.verdicts.values() for v in vs] + self.pooled + self.links
        return out + [v for vs in [*self.probe.values(), *self.engine.values()] for v in vs]

    def worst(self) -> dict[str, str]:
        """Each detector's worst level over every visit and lane."""
        out: dict[str, str] = {}
        for v in self.all_verdicts():
            if d.RANK[v.level] > d.RANK.get(out.get(v.id, d.SKIPPED), -1):
                out[v.id] = v.level
            out.setdefault(v.id, v.level)
        return out

    def summary(self) -> dict[str, Any]:
        worst = self.worst()
        caught = sorted(k for k, lv in worst.items() if lv == d.CAUGHT)
        suspicious = sorted(k for k, lv in worst.items() if lv == d.SUSPICIOUS)
        outcome = "detected" if caught else "flagged" if len(suspicious) >= 3 else "passed"
        visits = sum(len(v) for v in self.sessions.values())
        detected = sum(1 for vs in self.verdicts.values() if any(v.level == d.CAUGHT for v in vs))
        return {"outcome": outcome, "visits": visits, "visits_detected": detected, "caught": caught,
                "suspicious": suspicious, "false_positives": self.false_positives}

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend, "versions": self.versions, "summary": self.summary(),
            "visits": {sid: [v.to_dict() for v in vs] for sid, vs in self.verdicts.items()},
            "pooled": [v.to_dict() for v in self.pooled],
            "probe": {lane: [v.to_dict() for v in vs] for lane, vs in self.probe.items()},
            "links": [v.to_dict() for v in self.links],
            "engine": {lane: [v.to_dict() for v in vs] for lane, vs in self.engine.items()},
            "deep": self.deep,
            "steps": self.steps,
        }


def probe_verdicts(lane: str, dump: dict[str, Any], pin: dict[str, Any] | None) -> list[d.Verdict]:
    """The stealth probe's findings as verdicts of the fingerprint layer."""
    from openajar.stealth import stealth

    config = stealth.split_pin(pin)[0] if pin else None
    out = []
    for f in stealth.dump_findings(lane, dump, config=config):
        out.append(d.Verdict(f"fp.{f.check}", "fingerprint", LEVEL_OF.get(f.level, d.SUSPICIOUS), f.detail,
                             dict(f.data), fix="docs/design/stealth.md"))
    if not out:
        out.append(d.Verdict("fp.probe", "fingerprint", d.CLEAN, "the stealth probe found nothing"))
    return out


async def read_probe(page: Any, base: str) -> dict[str, Any]:
    await page.goto(f"{base}/fingerprint.html", timeout=PROBE_TIMEOUT_MS)
    await page.wait_for_selector("#dump[data-state=done]", state="attached", timeout=PROBE_TIMEOUT_MS)
    return json.loads(await page.text_content("#dump"))


async def read_deep(page: Any, base: str, who: str) -> dict[str, Any]:
    await page.goto(f"{base}/deep?who={who}", timeout=PROBE_TIMEOUT_MS)
    await page.wait_for_selector("#out[data-state=done]", state="attached", timeout=PROBE_TIMEOUT_MS)
    return json.loads(await page.text_content("#out"))


async def read_judges(page: Any, base: str) -> dict[str, Any]:
    await page.goto(f"{base}/judges", timeout=PROBE_TIMEOUT_MS)
    await page.wait_for_selector("#out[data-state=done]", state="attached", timeout=PROBE_TIMEOUT_MS)
    return json.loads(await page.text_content("#out"))


async def run(lab: Lab, opts: Options, *, say: Callable[[str], None] = lambda _: None,
              server: DetectorServer | None = None) -> Report:
    from openajar.node.interaction import PlaywrightInteraction

    own_server = server is None
    server = server or DetectorServer().start()
    ix = PlaywrightInteraction(allow_private_targets=True)
    report = Report(backend=lab.name, versions={})
    lanes = [f"redteam/lane{i + 1}" for i in range(opts.lanes)]
    envs: dict[str, dict[str, Any]] = {}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        await lab.start()
        report.versions = lab.versions()
        for i, lane in enumerate(lanes):
            os_name = opts.os_names[i % len(opts.os_names)] if opts.os_names else None
            await lab.create(lane, os_name=os_name)
        for r in range(opts.runs):
            for lane in lanes:
                sid = f"{lane.split('/')[-1]}-r{r}-{stamp}"
                say(f"{lane}: visit {r + 1} of {opts.runs}")
                async with lab.using(lane) as page:
                    steps = await scenario.visit(ix, page, server.base, sid)
                    if r == 0:
                        try:
                            deep = await read_deep(page, server.base, sid)
                            report.deep[lane] = deep
                            report.engine[lane] = engine.judge_engine(deep)
                            report.engine[lane] += engine.judge_botd(await read_judges(page, server.base))
                        except Exception as e:  # noqa: BLE001
                            report.engine[lane] = [d.Verdict("engine.probe", "engine", d.SKIPPED,
                                                             f"the engine probe did not finish: {type(e).__name__}")]
                    if opts.probe and r == 0:
                        try:
                            dump = await read_probe(page, server.base)
                            report.probe[lane] = probe_verdicts(lane, dump, lab.pin(lane))
                        except Exception as e:  # noqa: BLE001 - a probe that fails is a finding
                            report.probe[lane] = [d.Verdict("fp.probe", "fingerprint", d.SKIPPED,
                                                            f"the probe did not finish: {type(e).__name__}")]
                session = server.store.session(sid) or {"id": sid, "requests": [], "pages": []}
                report.sessions.setdefault(lane, []).append(session)
                report.steps.setdefault(lane, []).append([s.__dict__ for s in steps])
                report.verdicts[sid] = d.judge(session)
                envs.setdefault(lane, d.Session(session).env())
                failed = [s for s in steps if not s.ok]
                if failed:
                    say(f"{lane}: {len(failed)} steps did not take: " + "; ".join(f"{s.name}: {s.detail}" for s in failed))
    finally:
        await lab.stop()
        if own_server:
            server.stop()
    agent = [s for visits in report.sessions.values() for s in visits]
    report.pooled = d.judge_pooled(agent, opts.humans)
    if len(lanes) > 1:
        report.links = d.link_lanes(envs)
    for human in opts.humans:
        fired = [v.id for v in d.judge(human) if v.level == d.CAUGHT]
        if fired:
            report.false_positives[str(human.get("id"))] = fired
    if opts.out:
        write_outputs(opts.out, report)
    return report


async def run_gauntlet(lab: Lab, *, kinds: set[str] | None = None, out: Path | None = None,
                       say: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    """One scratch lane (through the lab's proxy) visits the public gauntlet (gauntlet.py)."""
    from openajar.node.interaction import PlaywrightInteraction
    from openajar.stealth.redteam import gauntlet

    ix = PlaywrightInteraction()
    targets = [t for t in gauntlet.TARGETS if not kinds or t.kind in kinds or t.name in kinds]
    lane = "redteam/gauntlet"
    try:
        await lab.start()
        versions = lab.versions()
        await lab.create(lane)
        async with lab.using(lane) as page:
            visits = await gauntlet.run(ix, page, targets, out=out / "pages" if out else None, say=say)
    finally:
        await lab.stop()
    result = {"backend": lab.name, "versions": versions, "summary": gauntlet.summary(visits),
              "visits": [v.to_dict() for v in visits], "text": gauntlet.format_visits(visits)}
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / "gauntlet.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
        (out / "gauntlet.txt").write_text(result["text"] + "\n")
    return result


def write_outputs(out: Path, report: Report) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report.to_dict(), indent=1, ensure_ascii=False))
    (out / "report.txt").write_text(format_report(report) + "\n")
    sessions = out / "sessions"
    sessions.mkdir(exist_ok=True)
    for visits in report.sessions.values():
        for s in visits:
            (sessions / f"{s['id']}.json").write_text(json.dumps(s, ensure_ascii=False))


def load_humans(paths: Sequence[Path]) -> list[dict[str, Any]]:
    out = []
    for p in paths:
        files = sorted(p.glob("*.json")) if p.is_dir() else [p]
        for f in files:
            data = json.loads(f.read_text())
            if isinstance(data, dict) and data.get("pages"):
                data["human"] = True
                out.append(data)
    return out


# ---- the report ---------------------------------------------------------------------------------

MARK = {d.CAUGHT: "CAUGHT", d.SUSPICIOUS: "suspect", d.CLEAN: "ok", d.SKIPPED: "-"}
LAYERS = ["keyboard", "mouse", "scroll", "focus", "timing", "navigation", "http", "environment", "engine", "fingerprint",
          "link"]


def format_report(report: Report) -> str:
    s = report.summary()
    lines = [(f"red team: {s['outcome'].upper()}  ({s['visits_detected']} of {s['visits']} visits caught; "
             f"backend {report.backend}; Camoufox {report.versions.get('camoufox_browser') or '?'})"), ""]
    by_id: dict[str, list[d.Verdict]] = {}
    for v in report.all_verdicts():
        by_id.setdefault(v.id, []).append(v)
    worst = report.worst()
    for layer in LAYERS:
        ids = sorted({v.id for v in report.all_verdicts() if v.layer == layer})
        if not ids:
            continue
        lines.append(layer)
        for vid in ids:
            vs = by_id[vid]
            level = worst[vid]
            shown = max(vs, key=lambda v: d.RANK[v.level])
            count = sum(1 for v in vs if v.level == level)
            times = f" ({count}/{len(vs)})" if len(vs) > 1 and level in (d.CAUGHT, d.SUSPICIOUS) else ""
            lines.append(f"  {MARK[level]:8} {vid:32} {shown.detail}{times}")
            if level in (d.CAUGHT, d.SUSPICIOUS) and shown.fix:
                lines.append(f"  {'':8} {'':32} fix: {shown.fix}")
        lines.append("")
    if s["false_positives"]:
        lines.append("false positives (these detectors caught a person; their thresholds are wrong):")
        for sid, ids in s["false_positives"].items():
            lines.append(f"  {sid}: {', '.join(ids)}")
    return "\n".join(lines).rstrip()


def record(report: Report, path: Path = RESULTS) -> dict[str, Any] | None:
    """Append one line for the regression history; return the previous line of the same backend."""
    previous = None
    if path.exists():
        for line in path.read_text().splitlines():
            with contextlib.suppress(ValueError):
                row = json.loads(line)
                if row.get("backend") == report.backend:
                    previous = row
    commit, dirty = _git()
    row = {"date": time.strftime("%Y-%m-%dT%H:%M:%S"), "commit": commit, "dirty": dirty, "backend": report.backend,
           "versions": report.versions, **{k: v for k, v in report.summary().items() if k != "false_positives"},
           "levels": report.worst()}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return previous


def compare(previous: dict[str, Any] | None, report: Report) -> list[str]:
    if not previous:
        return []
    before, now = previous.get("levels") or {}, report.worst()
    out = []
    for k in sorted(set(before) | set(now)):
        a, b = before.get(k, d.SKIPPED), now.get(k, d.SKIPPED)
        if a != b and d.SKIPPED not in (a, b):
            word = "worse" if d.RANK[b] > d.RANK[a] else "better"
            out.append(f"{word}: {k} {a} -> {b}")
    return out


def _git() -> tuple[str | None, bool]:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                timeout=5, check=False).stdout.strip() or None
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True,
                                    timeout=5, check=False).stdout.strip())
        return commit, dirty
    except (OSError, subprocess.SubprocessError):
        return None, False


# ---- backends -----------------------------------------------------------------------------------

class LocalLab:
    """Plain Camoufox on this machine, launched as the engine launches a lane (headful, humanize on)."""

    name = "local"

    def __init__(self, *, headless: bool = False, proxy: Any = None):
        self.headless = headless
        self.proxy = proxy  # a ProxyConfig: the lane's exit, with geoip from it as on the node
        self._os: dict[str, str | None] = {}
        self._browsers: dict[str, Any] = {}
        self._stack = contextlib.AsyncExitStack()

    async def start(self) -> None:
        await self._stack.__aenter__()

    async def stop(self) -> None:
        await self._stack.aclose()

    async def create(self, lane: str, *, os_name: str | None = None) -> None:
        self._os[lane] = os_name

    async def _browser(self, lane: str) -> Any:
        if lane not in self._browsers:
            from camoufox.async_api import AsyncCamoufox

            from openajar.node.camoufox.engine import WHEEL_PREFS

            kwargs: dict[str, Any] = {"headless": self.headless, "humanize": True, "i_know_what_im_doing": True,
                                      "firefox_user_prefs": dict(WHEEL_PREFS)}  # a lane's notch, as a page sees it
            if self._os.get(lane):
                kwargs["os"] = self._os[lane]
            if self.proxy is not None:
                kwargs["proxy"] = {"server": self.proxy.server, "username": self.proxy.username or "",
                                   "password": self.proxy.password or ""}
                kwargs["geoip"] = True
            self._browsers[lane] = await self._stack.enter_async_context(AsyncCamoufox(**kwargs))
        return self._browsers[lane]

    @contextlib.asynccontextmanager
    async def using(self, lane: str) -> AsyncIterator[Any]:
        browser = await self._browser(lane)
        page = await browser.new_page()
        try:
            yield page
        finally:
            with contextlib.suppress(Exception):
                await page.close()

    def pin(self, lane: str) -> dict[str, Any] | None:
        return None

    def versions(self) -> dict[str, Any]:
        from importlib.metadata import version
        out: dict[str, Any] = {}
        for package in ("camoufox", "playwright"):
            try:
                out[package] = version(package)
            except Exception:  # noqa: BLE001
                out[package] = None
        try:
            from camoufox.pkgman import installed_verstr
            out["camoufox_browser"] = installed_verstr()
        except Exception:  # noqa: BLE001
            out["camoufox_browser"] = None
        return out


class NodeBench:
    """Scratch lanes on a node test slot (stealthcheck.NodeLab): the production lane manager, engine,
    pins and displays, in a throwaway home."""

    name = "node"

    def __init__(self, node_lab: Any):
        self.lab = node_lab

    async def start(self) -> None:
        await self.lab.start()

    async def stop(self) -> None:
        await self.lab.stop()

    async def create(self, lane: str, *, os_name: str | None = None) -> None:
        await self.lab.create(lane, os_name=os_name)
        await self.lab.wake(lane)

    def using(self, lane: str) -> contextlib.AbstractAsyncContextManager[Any]:
        return self.lab.using(lane)

    def pin(self, lane: str) -> dict[str, Any] | None:
        return self.lab.pin(lane)

    def versions(self) -> dict[str, Any]:
        return dict(self.lab.versions)
