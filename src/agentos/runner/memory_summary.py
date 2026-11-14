"""The run summary a finished run leaves in its workspace's memory (docs/design/workspace-memory.md
3.2 and 4.6, "Node-internal writers").

Built from the runner's structured record only: flow id and version, lane, who started it, the
honest status, the node-observed proof URLs with each readback's pass or fail (never what the page
said), the cost, the digest of what was authorized and, when the owner approved the run, the
shown params the owner read on the approval card. Never page text. The memory service writes it
through `Caller.node()` with node trust, one entry per run (a later status, such as a re-check
that verifies it, is a new version).

A summary that cannot be written is logged and traced; it never fails the run.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from agentos.core.errors import AgentosError
from agentos.core.models import RunKind, RunStatus
from agentos.runner.records import Run

log = logging.getLogger("agentos.runner")

MAX_PROOF = 20


def _readback(rb: dict[str, Any]) -> str:
    passed = rb.get("passed")
    return "passed" if passed is True else "failed" if passed is False else "not checked"


def _proof(run: Run) -> list[dict[str, Any]]:
    proof = run.proof or {}
    rbs = [rb for rb in proof.get("readbacks") or [] if isinstance(rb, dict)]
    out: list[dict[str, Any]] = []
    if isinstance(proof.get("url"), str) and proof["url"]:
        passed = sum(1 for rb in rbs if rb.get("passed") is True)
        out.append({"url": proof["url"],
                    "readback": f"{passed} of {len(rbs)} readbacks passed" if rbs else "no readback"})
    for rb in rbs:
        url = (rb.get("check") or {}).get("url") if isinstance(rb.get("check"), dict) else None
        out.append({"url": url, "path": rb.get("path"), "readback": _readback(rb)} if url else
                   {"path": rb.get("path"), "readback": _readback(rb)})
    return out[:MAX_PROOF]


def summary_record(run: Run, flows: Any = None) -> dict[str, Any] | None:
    """The record `memory.run_summary` takes, or None for a run that is not in a workspace."""
    if not run.terminal or not run.lane or "/" not in run.lane:
        return None
    status = RunStatus(run.status).value.replace("_", "-")
    caller = f"schedule:{run.schedule_id}" if run.schedule_id else (run.principal.get("id") or run.source)
    record: dict[str, Any] = {
        "lane": run.lane, "run_id": run.id, "flow": run.flow_id or str(run.kind), "flow_version": run.flow_version,
        "caller": str(caller)[:200], "status": status, "proof": _proof(run),
        "cost": {k: float(v) for k, v in (run.cost or {}).items() if isinstance(v, int | float)} or None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.finished_at.isoformat() if run.finished_at else None,
    }
    if run.kind == RunKind.FLOW and flows is not None and run.flow_id:
        try:
            fv = flows.get(run.flow_id, run.flow_version)
        except AgentosError:   # a flow deleted since: the summary still says what ran
            fv = None
        if fv is not None:
            content = flows.approval_content(fv, run.lane, run.params)
            record["content_digest"] = "sha256:" + hashlib.sha256(
                json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
            if run.approval_id and run.authorized:
                shown = flows.shown_params(fv)
                lines = [f"{k}: {v}" for k, v in sorted(run.params.items()) if k in shown and isinstance(v, str | int)]
                if lines:
                    record["approved_text"] = "\n".join(lines)
    return record


def write_summary(runtime: Any, run: Run, flows: Any = None) -> dict[str, Any] | None:
    """Write the summary if this node has memory. Never raises."""
    memory = runtime.get("memory") if runtime is not None else None
    fn = getattr(memory, "run_summary", None)
    if not callable(fn):
        return None
    try:
        record = summary_record(run, flows)
        return fn(record) if record is not None else None
    except Exception as e:
        log.warning("run %s: the memory run summary was not written (%s)", run.id, type(e).__name__, exc_info=True)
        _trace_failure(runtime, run, e)
        return None


def _trace_failure(runtime: Any, run: Run, error: Exception) -> None:
    traces = runtime.get("traces")
    write = getattr(traces, "write", None)
    if not callable(write):
        return
    try:
        from agentos.policy.traces import TraceEvent

        code = getattr(error, "code", type(error).__name__)
        write(TraceEvent(kind="memory_run_summary", status="error", lane=run.lane, run_id=run.id,
                         summary="the run summary was not written to memory", error=str(code)[:100]))
    except Exception:
        log.exception("tracing a run summary failure failed")
