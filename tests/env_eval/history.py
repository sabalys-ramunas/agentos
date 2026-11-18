"""A run's summary, the history file, and what got better or worse against an earlier run.

Each run appends one line per agent to bench/evals.jsonl: when, the commit and whether the tree was
dirty, the host, the agent and model, the repeats, and per task the medians over its repeats (the
finished rate a mean). `compare` sets a run against the one before it with the same agent, model
and host (or a named commit's), metric by metric.

A metric regresses when it is worse by more than its threshold (a fraction) *and* by more than its
noise floor (an absolute amount), so 3 s against 2 s, or one more call, is not a 50% regression.
`finished` regresses on any drop. Everything else here is lower-is-better.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

FINISHED = "finished"
# metric suffix -> (threshold as a fraction, noise floor in the metric's unit)
RULES: dict[str, tuple[float, float]] = {
    "_s": (0.25, 3.0),
    "tokens_read": (0.10, 300),
    "surface_tokens": (0.03, 60),
    "instructions_tokens": (0.05, 40),
    "input_tokens": (0.15, 2000),
    "output_tokens": (0.20, 500),
    "cache_read_tokens": (0.20, 20000),
    "cache_write_tokens": (0.20, 5000),
    "calls": (0.20, 1.5),
    "tool_errors": (0.0, 0.5),
    "wrong_turns": (0.0, 0.5),
    "turns": (0.20, 2),
    "cost_usd": (0.20, 0.02),
}
# per task, from each result: the numbers kept in the history
TASK_METRICS = ("seconds_s", "ready_s", "first_navigate_s", "calls", "tokens_read", "tool_errors", "wrong_turns", "turns",
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")


def rule_of(metric: str) -> tuple[float, float] | None:
    name = metric.rsplit(".", 1)[-1]
    for suffix, rule in RULES.items():
        if name.endswith(suffix):
            return rule
    return None


def _median(values: list[float]) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(vals), 3) if vals else None


def flat(result: dict[str, Any]) -> dict[str, float]:
    """One task run's numbers, by TASK_METRICS name."""
    m = result.get("model_metrics") or {}
    out = {"seconds_s": result.get("seconds"), "ready_s": result.get("ready_s"),
           "first_navigate_s": result.get("first_navigate_s"), "calls": result.get("calls"),
           "tokens_read": result.get("tokens_read"), "tool_errors": result.get("tool_errors"),
           "wrong_turns": len(result.get("wrong_turns") or []), **{k: m.get(k) for k in TASK_METRICS if k in m}}
    return {k: v for k, v in out.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per task: the finished rate and each metric's median over the repeats. And the totals: tasks
    finished, and the sums of the per-task medians."""
    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        if not r.get("skipped"):
            by_task.setdefault(r["task"], []).append(r)
    tasks: dict[str, dict[str, Any]] = {}
    for task, runs in sorted(by_task.items()):
        row: dict[str, Any] = {FINISHED: round(sum(bool(r.get("finished")) for r in runs) / len(runs), 3),
                               "runs": len(runs)}
        flats = [flat(r) for r in runs]
        for name in TASK_METRICS:
            v = _median([f[name] for f in flats if name in f])
            if v is not None:
                row[name] = v
        opens = [o for r in runs for o in (r.get("opens") or []) if o.get("ready_s") is not None]
        for i in range(max((len(r.get("opens") or []) for r in runs), default=0)):
            v = _median([(r.get("opens") or [])[i].get("ready_s") for r in runs if len(r.get("opens") or []) > i])
            if v is not None and len(opens) > 1:
                row[f"open{i + 1}_s"] = v
        tasks[task] = row
    totals: dict[str, Any] = {FINISHED: round(sum(t[FINISHED] for t in tasks.values()), 3), "tasks": len(tasks)}
    for name in TASK_METRICS:
        vals = [t[name] for t in tasks.values() if name in t]
        if vals:
            totals[name] = round(sum(vals), 3)
    surface = [r.get("surface_tokens") for r in results if r.get("surface_tokens")]
    if surface:
        totals["surface_tokens"] = max(surface)
    instr = [r.get("instructions_tokens") for r in results if r.get("instructions_tokens")]
    if instr:
        totals["instructions_tokens"] = max(instr)
    return {"tasks": tasks, "totals": totals}


def record(results: list[dict[str, Any]], *, agent: str, model: str | None, commit: str, dirty: bool, host: str,
           at: str, repeat: int, ref: str | None = None) -> dict[str, Any]:
    mine = [r for r in results if r["agent"] == agent]
    return {"at": at, "commit": commit, "dirty": dirty, "ref": ref, "host": host, "agent": agent,
            "model": model if agent not in ("perfect", "careless") else None, "repeat": repeat, **summarize(mine)}


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def append(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def same_kind(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a.get("agent") == b.get("agent") and a.get("model") == b.get("model") and a.get("host") == b.get("host")


def pick(history: list[dict[str, Any]], newest: dict[str, Any], against: str | None = None) -> dict[str, Any] | None:
    """The baseline for `newest`: the latest earlier record of the same kind (or of commit `against`)."""
    earlier = [r for r in history if r is not newest and same_kind(r, newest) and r.get("at", "") <= newest.get("at", "")]
    if against:
        earlier = [r for r in earlier if str(r.get("commit", "")).startswith(against) or r.get("ref") == against]
    return earlier[-1] if earlier else None


def metrics_of(rec: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for task, row in (rec.get("tasks") or {}).items():
        for k, v in row.items():
            if k != "runs" and isinstance(v, (int, float)):
                out[f"{task}.{k}"] = v
    for k, v in (rec.get("totals") or {}).items():
        if k != "tasks" and isinstance(v, (int, float)):
            out[f"total.{k}"] = v
    return out


def verdict(metric: str, old: float, new: float) -> tuple[str, bool]:
    """('better' | 'worse' | 'same', regressed)."""
    if metric.rsplit(".", 1)[-1] == FINISHED:
        if new > old:
            return "better", False
        return ("worse", True) if new < old else ("same", False)
    rule = rule_of(metric)
    if rule is None:
        return "same", False
    threshold, floor = rule
    diff = new - old
    if abs(diff) <= floor:
        return "same", False
    frac = diff / old if old else float("inf")
    if diff > 0:
        return "worse", frac > threshold
    return ("better", False) if -frac > threshold or not old else ("same", False)


def compare(base: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """Every metric in both: old, new, the change as a fraction, better/worse/same, regressed."""
    a, b = metrics_of(base), metrics_of(new)
    rows = []
    for metric in sorted(set(a) & set(b)):
        old, now = a[metric], b[metric]
        word, bad = verdict(metric, old, now)
        change = (now - old) / old if old else 0.0
        rows.append({"metric": metric, "old": old, "new": now, "change": round(change, 3), "verdict": word,
                     "regressed": bad})
    return rows


def describe(rows: list[dict[str, Any]], *, only_changes: bool = True) -> list[str]:
    lines = []
    for r in rows:
        if only_changes and r["verdict"] == "same":
            continue
        mark = "REGRESSION" if r["regressed"] else r["verdict"]
        lines.append(f"  {r['metric']:42} {r['old']:>10g} -> {r['new']:<10g} {r['change']:+7.1%}  {mark}")
    return lines
