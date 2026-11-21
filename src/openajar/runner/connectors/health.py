"""Per-operation, per-surface health (docs/design/connectors.md 4.7 and 4.8).

    healthy   the last run on this surface succeeded
    broken    a run failed on the contract: a locator before the commit no longer resolves
              (connector_contract_broken), the preflight did not match (preflight_mismatch), the
              publish request did not match (commit_mismatch), or the result broke the output
              schema (output_contract_violation)
    held      a bad signal or the wrong identity held the lane; not the connector's fault
    degraded  a site API's run succeeded with fewer rows than its `expect.min_items`, so the site
              probably changed, or one param set failed on the contract, which may only be that
              caller's bad id, or a read met a bad moment. Nothing stops it running
    unknown   no run yet, or the breakage was on a version that is no longer installed

`record(run)` runs on every finished connector run (a runner on_finished hook). The first
breakage of an operation sends one notification, whatever the number of surfaces or runs that
hit it; while broken, the operation's tool returns connector_broken at once, its schedules skip
their slots and its queued runs wait (they defer, they never fail one by one). A repaired
version (a new implementation hash) clears it; so does the owner (`openajar connector health
--clear`).

On a site API a caller's params can land on a page the contract never meant (an id that does not
exist), and one caller must not close an endpoint for everyone. So a contract failure there counts
as broken only with no params, or once a second param set has failed the same way. Until then it
is degraded and the call fails on its own. The health row keeps a keyed digest of the params that
failed, never their values; a success forgets it. The catalog breaks on the first failure.

A read_json that met a bad moment says nothing of the contract on its own, and with no params a broken
endpoint does not mend itself, so one 5xx must not close it. A 5xx, a response that did not come and a
slow body make it degraded, and three in a row on one version make it broken, under the params rule
above. A response with no body counts as a wrong shape once it repeats. Any success clears the count.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Callable
from typing import Any

from openajar.core.models import RunStatus
from openajar.core.signals import CONNECTOR_HOLD_CODES
from openajar.runner.connectors.models import BINDING_PARAMS
from openajar.runner.flowdoc import FlowDefinition
from openajar.runner.names import is_connector_id
from openajar.runner.owner import TRY_SOURCE
from openajar.runner.records import SUCCEEDED, Run
from openajar.runner.store import RunnerStore
from openajar.runner.util import Clock, clip_text

log = logging.getLogger("openajar.runner")

BROKEN_CODES = frozenset({"connector_contract_broken", "preflight_mismatch", "commit_mismatch",
                          "output_contract_violation"})
HELD_CODES = CONNECTOR_HOLD_CODES  # the old name
# a locator before any input that can publish no longer resolves, or the JSON a read_json waits for
# came in a shape it should not have: the site changed
CONTRACT_CAUSES = frozenset({"element_not_found", "ambiguous_locator", "json_status", "json_invalid", "json_too_large"})
SHAKY_CODES = frozenset({"json_status", "json_not_seen", "json_slow", "json_empty"})  # a run's own code: not mapped above
SHAKY_LIMIT = 3  # in a row on one version

Notify = Callable[..., None]  # notify(title, body, *, level, data)


def operation_of(run: Run) -> str | None:
    """'instagram.post_photo' for a connector run, else None."""
    if not run.flow_id or not is_connector_id(run.flow_id) or run.source == TRY_SOURCE:
        return None  # a try is of a draft: what it finds says nothing of what is live
    return run.flow_id.split(".", 1)[1]


def given_params(definition: FlowDefinition, params: dict[str, Any]) -> dict[str, Any]:
    """What the caller chose: the run's params less the binding's and what a default filled in."""
    props = definition.params.get("properties") or {}
    return {k: v for k, v in params.items() if k not in BINDING_PARAMS
            and not (isinstance(props.get(k), dict) and "default" in props[k] and props[k]["default"] == v)}


class ConnectorHealth:
    def __init__(self, store: RunnerStore, *, clock: Clock | None = None, notify: Notify | None = None,
                 current: Callable[[str], str | None] | None = None):
        self.store = store
        self.clock = clock or Clock()
        self.notify = notify
        self.current = current  # flow id -> the installed implementation hash (None: not installed)

    def _flow_id(self, operation: str) -> str:
        return f"connector.{operation}"

    def _live(self, operation: str, doc: dict[str, Any] | None) -> dict[str, Any] | None:
        """The record, unless it is a breakage of a version that is no longer installed."""
        if doc is None:
            return None
        stale = (doc.get("state") == "broken" and self.current is not None
                 and self.current(self._flow_id(operation)) != doc.get("implementation"))
        return None if stale else doc

    def get(self, operation: str, surface: str) -> dict[str, Any]:
        doc = self._live(operation, self.store.get_health(operation, surface))
        return {"operation": operation, "surface": surface, **(doc or {"state": "unknown"})}

    def list(self, operation: str | None = None) -> list[dict[str, Any]]:
        out = []
        for row in self.store.list_health(operation):
            live = self._live(row["operation"], {k: v for k, v in row.items() if k not in ("operation", "surface")})
            out.append({"operation": row["operation"], "surface": row["surface"], **(live or {"state": "unknown"})})
        return out

    def broken(self, operation: str, surface: str) -> dict[str, Any] | None:
        h = self.get(operation, surface)
        return h if h["state"] == "broken" else None

    def clear(self, operation: str, surface: str | None = None) -> int:
        return self.store.delete_health(operation, surface)

    # ---- recording ----
    def record(self, run: Run) -> None:
        op = operation_of(run)
        if op is None or not run.surface or not run.terminal:
            return
        now = self.clock.now().isoformat()
        old = self.store.get_health(op, run.surface) or {}
        code = (run.error or {}).get("code")
        base = {"last_run": {"id": run.id, "status": str(run.status), "at": now}}
        if run.status in SUCCEEDED:
            state = "degraded" if self._below_expected(run) else "healthy"
            doc = {**base, "state": state, "since": old.get("since") if old.get("state") == state else now,
                   "last_success_at": now, "implementation": run.flow_version}
        elif code in BROKEN_CODES and run.status in (RunStatus.FAILED, RunStatus.PARTIAL):
            err = run.error or {}
            alone = self._only_these_params(run, old)
            if alone is not None:
                doc = {**base, "state": "degraded", "since": old.get("since") if old.get("state") == "degraded" else now,
                       "code": code, "cause": err.get("cause"), "step": err.get("step"), "run_id": run.id,
                       "failed_params": [alone], "last_success_at": old.get("last_success_at"),
                       "implementation": run.flow_version}
                self.store.put_health(op, run.surface, doc)
                return
            self._break(op, run, old, base, now)
            return
        elif code in SHAKY_CODES and run.status in (RunStatus.FAILED, RunStatus.PARTIAL):
            self._record_shaky(op, run, old, base, now)
            return
        elif code in CONNECTOR_HOLD_CODES or (isinstance(code, str) and code.startswith("signal_")):
            doc = {**old, **base, "state": "held", "since": now, "code": code}
        else:  # a timeout, a denial, a cancel: says nothing about the connector
            doc = {**(old or {"state": "unknown"}), **base}
        self.store.put_health(op, run.surface, doc)

    def _break(self, op: str, run: Run, old: dict[str, Any], base: dict[str, Any], now: str) -> None:
        err = run.error or {}
        first = self._first_breakage(op, run.flow_version)
        doc = {**base, "state": "broken", "since": old.get("since") if old.get("state") == "broken" else now,
               "code": err.get("code"), "cause": err.get("cause"), "step": err.get("step"),
               "message": clip_text(str(err.get("message") or ""), 300), "run_id": run.id,
               "implementation": run.flow_version}
        self.store.put_health(op, run.surface or "", doc)
        if first:
            self._notify_broken(op, run)

    def _record_shaky(self, op: str, run: Run, old: dict[str, Any], base: dict[str, Any], now: str) -> None:
        """A bad moment: degraded, until enough of them in a row say more than that."""
        err = run.error or {}
        same = old.get("implementation") == run.flow_version
        in_a_row = (old.get("in_a_row", 0) if same else 0) + 1
        repeats = err.get("code") == "json_empty" and same and old.get("code") == "json_empty"
        alone = self._only_these_params(run, old)
        if alone is None and (in_a_row >= SHAKY_LIMIT or repeats or (same and old.get("state") == "broken")):
            self._break(op, run, old, base, now)
            return
        params = [alone] if alone else (old.get("failed_params") or []) if same else []
        doc = {**base, "state": "degraded", "since": old.get("since") if old.get("state") == "degraded" else now,
               "code": err.get("code"), "step": err.get("step"), "run_id": run.id, "in_a_row": in_a_row,
               "last_success_at": old.get("last_success_at"), "implementation": run.flow_version,
               **({"failed_params": params} if params else {})}
        self.store.put_health(op, run.surface or "", doc)

    def _only_these_params(self, run: Run, old: dict[str, Any]) -> str | None:
        """The digest of this run's params, if it is a site API's and they are the only ones to have
        failed on this version; None when the failure counts toward broken."""
        fv = self.store.get_flow(run.flow_id or "", run.flow_version)
        if fv is None or (fv.saved_by or {}).get("kind") != "local":
            return None
        given = given_params(fv.definition, run.params)
        same = old.get("implementation") == run.flow_version
        if not given or (same and old.get("state") == "broken"):
            return None
        digest = self._digest(given)
        seen = set(old.get("failed_params") or []) if same else set()
        return None if seen - {digest} else digest

    def _digest(self, params: dict[str, Any]) -> str:
        key = self.store.meta("health_params_key", secrets.token_hex(16))
        blob = json.dumps(params, sort_keys=True, default=str).encode()
        return hmac.new(key.encode(), blob, hashlib.sha256).hexdigest()[:16]

    def _below_expected(self, run: Run) -> bool:
        from openajar.runner.siteapis.cache import below_expected

        fv = self.store.get_flow(run.flow_id or "", run.flow_version)
        return fv is not None and below_expected(fv, (run.output or {}).get("data"))

    def _first_breakage(self, op: str, implementation: str | None) -> bool:
        """No surface of this operation is broken on this implementation yet."""
        return not any(r.get("state") == "broken" and r.get("implementation") == implementation
                       for r in self.store.list_health(op))

    def _notify_broken(self, op: str, run: Run) -> None:
        if self.notify is None:
            return
        err = run.error or {}
        posted = ("Nothing was posted." if run.status == RunStatus.FAILED else
                  f"It may have posted before it stopped: check run {run.id}.")
        body = (f"{op} stopped on {run.surface} at step {err.get('step')} ({err.get('code')}): "
                f"{clip_text(str(err.get('message') or ''), 200)} {posted} Its schedules wait and other operations "
                "keep working; it runs again once a repaired version is installed.")
        try:
            self.notify("A connector operation changed", body, level="warning",
                        data={"run_id": run.id, "operation": op, "surface": run.surface, "kind": "connector_broken"})
        except Exception:
            log.exception("could not send the connector breakage notification")
