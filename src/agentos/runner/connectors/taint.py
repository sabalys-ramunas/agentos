"""Connector outputs into the taint ledger (docs/design/connectors.md 4.10).

A connector run's `Run.output` lists `untrusted_fields` (comment bodies, author names: text a
stranger wrote) and `private_fields` (customer names, order notes). When the run ends, its grant
family is labelled `untrusted` and `private` through the policy's taint port
(`PolicyService.memory_rules.taint_add`, the same ledger the lethal-trifecta rule reads), so what
the agent sends out next is checked. The owner in person (a human principal: the panel, the CLI)
neither adds nor receives labels; an agent that later reads the output through run_status or a
connector tool labels its own family there (`label_context`).
"""

from __future__ import annotations

import logging
from typing import Any

from agentos.runner.records import Run

log = logging.getLogger("agentos.runner")


def output_labels(output: dict[str, Any] | None) -> set[str]:
    out = output or {}
    labels = set()
    if out.get("untrusted_fields"):
        labels.add("untrusted")
    if out.get("private_fields"):
        labels.add("private")
    return labels


def _source(run: Run) -> str:
    src = (run.output or {}).get("source") or {}
    return f"connector:{src.get('connector')}.{src.get('operation')}:{run.id}"


def record_run(runtime: Any, run: Run) -> set[str]:
    """Label the run's grant family with what its output carries. Returns the labels added."""
    labels = output_labels(run.output)
    if not labels or not run.grant_id or (run.principal or {}).get("kind") == "human":
        return set()
    policy = runtime.get("policy") if runtime is not None else None
    context = getattr(policy, "context_for_grant", None)
    if not callable(context):
        return set()
    try:
        ctx = context(run.grant_id, tool="connector_run")
    except Exception as e:  # noqa: BLE001 - a revoked grant cannot act on anything it read
        log.info("run %s: no taint recorded, its grant is gone (%s)", run.id, e)
        return set()
    try:  # through the context, so a run under a turn token taints that turn (ROUTING.md)
        ctx.taint(*sorted(labels), source=_source(run), kind="connector_output")
    except TypeError:  # a context without origin kinds
        port = getattr(policy, "memory_rules", None)
        add = getattr(port, "taint_add", None)
        if callable(add):
            add(ctx.taint_key, labels, source=_source(run))
        else:
            ctx.taint(*labels, source=_source(run))
    return labels


def label_context(ctx: Any, output: dict[str, Any] | None, *, source: str, kind: str = "connector_output") -> None:
    """Label the caller's own family when a tool hands it an output with labelled fields. `kind` is the
    word an approval that follows it uses for where the text came from."""
    labels = output_labels(output)
    taint = getattr(ctx, "taint", None)
    if labels and callable(taint):
        try:
            try:
                taint(*sorted(labels), source=source, kind=kind)
            except TypeError:  # a context without origin kinds
                taint(*sorted(labels), source=source)
        except Exception:  # a context without a ledger (tests, M1) labels nothing
            log.debug("could not label the caller's family", exc_info=True)
