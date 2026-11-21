"""merge: one finding per cause across screens. delta: what changed between two runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from openajar.studio.checks.registry import METRIC
from openajar.studio.checks.run import SEV_RANK
from openajar.studio.contracts import CheckOutput, CheckRun, CoverageItem, Delta, Finding, SeenAt


def _metric(f: Finding) -> float | None:
    key, higher = METRIC.get(f.check, ("", True))
    v = f.measured.get(key)
    if not isinstance(v, int | float):
        return None
    return float(v) if higher else -float(v)


def _rank(f: Finding) -> tuple[int, float]:
    return SEV_RANK[f.severity], _metric(f) or 0.0


def merge(outputs_by_screen: Mapping[str, CheckOutput] | Iterable[CheckOutput], *, run_id: str | None = None,
          used: dict[str, Any] | None = None) -> CheckRun:
    """Fold the outputs of several screens into one run. A finding seen on several screens keeps
    its id, lists every screen in `seen_at`, and shows the worst instance."""
    outputs = list(outputs_by_screen.values()) if isinstance(outputs_by_screen, Mapping) else list(outputs_by_screen)
    by_id: dict[str, Finding] = {}
    seen: dict[str, dict[str, int]] = {}
    coverage: list[CoverageItem] = []
    for out in outputs:
        coverage.extend(out.coverage)
        for f in out.findings:
            counts = seen.setdefault(f.id, {})
            for s in f.seen_at:
                counts[s.screen] = counts.get(s.screen, 0) + s.count
            cur = by_id.get(f.id)
            if cur is None or _rank(f) > _rank(cur):
                by_id[f.id] = f
    findings = []
    for fid, f in by_id.items():
        seen_at = [SeenAt(screen=k, count=v) for k, v in seen[fid].items()]
        findings.append(f.model_copy(update={"seen_at": seen_at}))
    findings.sort(key=lambda f: (-SEV_RANK[f.severity], -len(f.seen_at), f.check, f.id))
    if run_id is None:
        basis = json.dumps({"f": sorted(by_id), "c": [c.model_dump(mode="json") for c in coverage]}, sort_keys=True)
        run_id = "run_" + hashlib.sha256(basis.encode()).hexdigest()[:10]
    return CheckRun(id=run_id, findings=findings, coverage=coverage, used=used or {})


def _ran(run: CheckRun) -> set[tuple[str, str]]:
    return {(c.check, c.screen) for c in run.coverage if c.status == "ran"}


def _worse(a: Finding, b: Finding) -> bool:
    if SEV_RANK[b.severity] != SEV_RANK[a.severity]:
        return SEV_RANK[b.severity] > SEV_RANK[a.severity]
    if len(b.seen_at) > len(a.seen_at):
        return True
    if sum(s.count for s in b.seen_at) > sum(s.count for s in a.seen_at):
        return True
    ma, mb = _metric(a), _metric(b)
    if ma is not None and mb is not None:
        return mb > ma + max(0.02 * abs(ma), 1e-6)
    return False


def delta(before: CheckRun, after: CheckRun) -> Delta:
    """fixed, new, still and worse, by finding id. A finding missing from `after` counts as fixed
    only when its check ran on every screen it was seen on; otherwise see `unchecked`."""
    a = {f.id: f for f in before.findings}
    b = {f.id: f for f in after.findings}
    ran = _ran(after)
    out = Delta()
    for fid, f in a.items():
        if fid in b:
            (out.worse if _worse(f, b[fid]) else out.still).append(fid)
        elif all((f.check, s.screen) in ran for s in f.seen_at):
            out.fixed.append(fid)
    out.new = [fid for fid in b if fid not in a]
    return out


def unchecked(before: CheckRun, after: CheckRun) -> list[str]:
    """Ids from `before` that are gone from `after` without their check having run again on every
    screen they were seen on: not fixed, just not looked at."""
    b = {f.id for f in after.findings}
    ran = _ran(after)
    return [f.id for f in before.findings
            if f.id not in b and not all((f.check, s.screen) in ran for s in f.seen_at)]
