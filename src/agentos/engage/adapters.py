"""Production adapters of the executor's ports (agentos.engage.ports), on the node's services.

    RunnerOperations(runtime)   connector runs through the runner ("runner"), approvals through
                                the policy's approval store ("approvals")
    NodeLanes(runtime)          the lane manager ("lanes") and the surface bindings ("surfaces")
    ConnectorTargets(...)       targets read by connector read operations (agentos.engage.targets)
    NoTargets()                 always None (`no_target`): previews, which never read a site
    NoContent()                 always empty (`no_content`); production uses the items inbox instead
    ChainedContent([ItemsContent(store)])   the owner's items (agentos.engage.items); B3 adds drafts
    production_executor(service, runtime)   the Executor wired from these, or None without a runner

Writes go through the normal policy path. A run is submitted with `authorized=False`, so the
runner authorizes it under the owner's grant at dispatch: a commit asks the owner unless the
owner's own "Ask me first" switches let it run on its own. An operation the executor marks
`ask=True` (approve mode: a comment or a draft the owner has not approved) never relies on
that: the adapter creates an approval card for exactly that run in the approval store and
submits the run parked on it, the way the runner parks any run that waits for a person. The
executor never passes `authorized=True`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from agentos.core.errors import AgentosError
from agentos.core.signals import ENGAGE_HELD_CODES, ENGAGE_SIGNAL_OF_CODE
from agentos.engage.ports import FOREIGN_RUN, SIGNALS, Content, LaneUnavailable, OpResult, OpStatus

log = logging.getLogger("agentos.engage.adapters")

PRINCIPAL = {"kind": "flow", "id": "", "owner": "owner"}   # no id: the runner gives it the owner's grant
SOURCE = "engage"
WAIT_S = 600.0             # how long a window waits for one connector run to finish
SIGNAL_CODES = ENGAGE_SIGNAL_OF_CODE
HELD_CODES = ENGAGE_HELD_CODES  # the old name


def signal_of(code: str | None) -> str | None:
    """The executor's signal for a runner error code (rate_limited, signal_captcha, ...), or None."""
    if not code:
        return None
    sig = SIGNAL_CODES.get(code) or (code.removeprefix("signal_") if code.startswith("signal_") else None)
    return sig if sig in SIGNALS else None


def _code(run: Any) -> str | None:
    err = getattr(run, "error", None) or {}
    return str(err.get("code") or "") or None


def signalled(run: Any) -> bool:
    """A run the runner re-queued after a signal (rate_limited is transient, so it retries): for
    the executor that is the signal, not a run that has yet to start."""
    return str(getattr(run, "status", "")) == "queued" and signal_of(_code(run)) is not None


def acted_of(run: Any) -> bool | None:
    """False when the run provably published nothing: it never started, or it ended failed or
    cancelled with no decision taken on it and a proof that counts no acted or written step (the
    runner turns any acted commit into partial). None otherwise (a crash leaves no proof)."""
    if getattr(run, "started_at", None) is None:
        return False
    proof = getattr(run, "proof", None)
    if str(getattr(run, "status", "")) in ("failed", "cancelled") and getattr(run, "decision", None) is None \
            and isinstance(proof, dict) and proof.get("acted") == 0 and not proof.get("writes"):
        return False
    return None


def result_of(run: Any) -> OpResult:
    """An OpResult from a runner Run (agentos.runner.models.Run)."""
    status = str(getattr(run, "status", ""))
    code = _code(run)
    signal = signal_of(code)
    out = (getattr(run, "output", None) or {}).get("data") or {}
    rid = getattr(run, "id", None)
    if status == "succeeded_verified":
        return OpResult(OpStatus.VERIFIED, run_id=rid, output=out)
    if status in ("succeeded_unverified", "partial"):
        return OpResult(OpStatus.UNVERIFIED, run_id=rid, output=out, error=code)
    if status == "waiting_approval":
        return OpResult(OpStatus.AWAITING_APPROVAL, run_id=rid)
    if status == "queued" and signal is not None:   # it met a signal, and the runner would retry it
        return OpResult(OpStatus.FAILED, signal=signal, error=code, run_id=rid)
    if status == "queued":       # never started in its time (deferred: paced, broken, the lane busy)
        return OpResult(OpStatus.HELD, run_id=rid, error=code or "not_started")
    if status == "running":      # started and not finished in WAIT_S: it may have acted
        return OpResult(OpStatus.UNVERIFIED, run_id=rid, error="still_running")
    assert signal is None or signal in SIGNALS
    return OpResult(OpStatus.FAILED, signal=signal, error=code or status or "failed", run_id=rid, acted=acted_of(run))


def ours(run: Any, flow_id: str, params: dict[str, Any], parent: str | None) -> bool:
    """A run this executor submitted for exactly this operation: the engine's principal and
    source, the window's claim parent (a per-window secret), the same flow and the same params.
    Anything else under the key is somebody else's run, which the executor never adopts."""
    principal = getattr(run, "principal", None) or {}
    got = getattr(run, "params", None) or {}
    return (getattr(run, "source", None) == SOURCE and principal.get("kind") == PRINCIPAL["kind"]
            and not principal.get("id") and getattr(run, "parent", None) == parent and parent is not None
            and getattr(run, "flow_id", None) == flow_id
            and params_hash({k: got.get(k) for k in params}) == params_hash(params))


def params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()


def foreign(run: Any) -> OpResult:
    log.warning("engage: run %s holds this operation's key, and the engine did not submit it", getattr(run, "id", "?"))
    return OpResult(OpStatus.FAILED, error=FOREIGN_RUN, run_id=getattr(run, "id", None))


class RunnerOperations:
    def __init__(self, runtime: Any, *, wait_s: float = WAIT_S):
        self.runtime = runtime
        self.wait_s = wait_s

    def _runner(self) -> Any:
        return self.runtime.require("runner")

    def _ask(self, runner: Any, flow_id: str, surface: str, params: dict[str, Any], key: str) -> str:
        """An approval card for exactly this run (its node-written summary and bound content)."""
        from agentos.core.models import Principal
        from agentos.policy.approvals import ActionBinding, ask_for_action

        approvals = self.runtime.require("approvals")
        fv = runner.flows.get(flow_id)
        target = runner.connector_target(fv, surface, params)
        summary, content = runner.surfaces.approval(fv, target)
        # the card binds the key and the exact params the run is submitted with, besides the
        # content the owner reads; the runner checks the same binding again at dispatch (run_binding)
        binding = ActionBinding(effect=str(getattr(fv, "effect", "commit")), tool=flow_id, lane=surface,
                                params=getattr(target, "params", params), content=content, key=key, summary=summary)
        ap = ask_for_action(
            approvals, binding, Principal(kind="flow", id="engage", owner="owner"),
            "Your engagement program is in approve mode: this waits for you, and nothing is sent until you "
            "approve it.", grant_id="owner", grant_path="owner")
        return ap.id

    async def run(self, surface: str, spec: Any, params: dict[str, Any], *, idempotency_key: str,
                  ask: bool = True, parent: str | None = None) -> OpResult:
        runner = self._runner()
        submitted = False
        try:
            existing = runner.existing_run(idempotency_key, flow_id=spec.flow_id, lane=surface)
            if existing is not None:
                if not ours(existing, spec.flow_id, params, parent):
                    return foreign(existing)
                run = existing
            else:
                approval_id = self._ask(runner, spec.flow_id, surface, params, idempotency_key) if ask else None
                run, created = runner.submit(flow_id=spec.flow_id, params=params, surface=surface,
                                             principal=dict(PRINCIPAL), source=SOURCE, idempotency_key=idempotency_key,
                                             parent=parent, authorized=False, approval_id=approval_id)
                if not created and not ours(run, spec.flow_id, params, parent):
                    return foreign(run)                     # someone took the key between the look and the submit
            submitted = True
            if str(run.status) in ("queued", "running") and not signalled(run):
                run = await runner.wait(run.id, self.wait_s, stop=signalled)
        except AgentosError as e:
            code = getattr(e, "code", "error")
            if code in HELD_CODES:
                return OpResult(OpStatus.HELD, error=code)
            # refused before there was a run: nothing acted
            return OpResult(OpStatus.FAILED, signal=signal_of(code), error=code, acted=None if submitted else False)
        res = result_of(run)
        if str(run.status) == "queued":
            # a held run never acts after its window, and a run that met a signal is not retried
            # behind the executor's back: the executor holds the surface and reschedules instead
            await runner.cancel(run.id, by="engage")
        return res

    def adopt(self, surface: str, *, parent: str, until: Any) -> list[str]:
        """Approved engage runs queued for this session (their slot falls before `until`): they run
        inside its claim, so they pass it instead of waiting until it ends."""
        from agentos.runner.models import RunStatus

        runner = self._runner()
        found = [r.id for r in runner.store.list_runs(status=RunStatus.QUEUED, due_before=until, limit=5000,
                                                       oldest_first=True)
                 if r.source == SOURCE and r.surface == surface and r.approval_id and r.authorized
                 and r.not_before is not None and r.not_before <= until]
        return runner.adopt(found, parent=parent) if found else []

    async def check(self, run_id: str) -> OpResult | None:
        try:
            return result_of(self._runner().get(run_id))
        except AgentosError:
            return None

    async def lookup(self, idempotency_key: str, flow_id: str, surface: str) -> OpResult | None:
        """The outcome of the run an idempotency key started, once it finished; None when there
        is none or it is still queued or running (a profile row agentos.engage.profile settles)."""
        try:
            run = self._runner().existing_run(idempotency_key, flow_id=flow_id, lane=surface)
        except AgentosError:
            return None
        if run is None or str(run.status) in ("queued", "running"):
            return None
        return result_of(run)


class NodeLanes:
    """The lane manager behind a LanePort. A surface acts through its binding's lane."""

    def __init__(self, runtime: Any):
        self.runtime = runtime

    def lane_for(self, surface: str) -> str:
        surfaces = self.runtime.get("surfaces")
        found = surfaces.find(surface) if surfaces is not None else None
        return found.lane if found is not None else surface

    def _holder(self, holder: str) -> Any:
        from agentos.node.claims import Holder

        return Holder(key=holder, label="engagement session", kind="flow", identity=holder)

    async def claim(self, lane: str, *, holder: str, purpose: str, ttl_s: float) -> None:
        lanes = self.runtime.require("lanes")
        try:
            await lanes.claim(lane, self._holder(holder), purpose=purpose, ttl_s=ttl_s, wait_s=60.0,
                              principal_kind="flow")
        except AgentosError as e:
            raise LaneUnavailable(getattr(e, "code", "unavailable"), str(e)) from None

    async def renew(self, lane: str, *, holder: str, ttl_s: float) -> None:
        """Claiming a lane the holder still holds renews it (agentos.node.lanes claim). A claim that
        ended (expired, a take-over) is not claimed again: someone may have used the lane since."""
        lanes = self.runtime.require("lanes")
        if not lanes.claimed_by(lane, holder):
            raise LaneUnavailable("claim_lost", f"the session's claim on {lane} ended")
        await self.claim(lane, holder=holder, purpose="", ttl_s=ttl_s)

    async def release(self, lane: str, *, holder: str) -> None:
        lanes = self.runtime.get("lanes")
        if lanes is None:
            return
        try:
            await lanes.release(lane, self._holder(holder))
        except AgentosError:
            log.warning("engage: releasing %s failed", lane, exc_info=True)


class NoTargets:
    def next(self, surface: str, kind: str, program: Any) -> str | None:
        return None


class NoContent:
    def take(self, surface: str, kind: str, n: int) -> list[Content]:
        return []

    def settle(self, surface: str, content: Content, *, used: bool, run_id: str | None = None) -> None:
        return None


def _flow_getter(runtime: Any, home: Path | None = None) -> Any:
    """flow_id -> the installed FlowVersion or None: through the runtime's flows, else the
    runner's database."""
    flows = runtime.get("flows") if runtime is not None else None
    if flows is not None:
        return flows.store.get_flow
    if home is not None and (Path(home) / "runner.db").exists():
        try:
            from agentos.runner.store import RunnerStore

            return RunnerStore(Path(home) / "runner.db").get_flow
        except Exception:  # a preview with nothing installed is still a preview
            log.debug("runner.db not readable", exc_info=True)
    return lambda flow_id: None


def installed_checker(runtime: Any, home: Path | None = None) -> Any:
    """flow_id -> installed on this node."""
    get = _flow_getter(runtime, home)
    return lambda flow_id: get(flow_id) is not None


def verified_checker(runtime: Any, home: Path | None = None) -> Any:
    """flow_id -> the installed connector operation's mark from this node's ledger
    (agentos.runner.connectors.marks: a MarkState, stale when the installed operation is not the
    implementation the canary ran), or "" when it has none or is not installed. The owner sets
    marks after a canary (agentos connector verify); a `verified` text inside the installed
    connector is information only and never counts."""
    from agentos.core.config import home_from_env
    from agentos.runner.connectors.marks import MarkLedger

    get = _flow_getter(runtime, home)
    ledger = MarkLedger(Path(home) if home is not None else home_from_env())

    def verified(flow_id: str) -> Any:
        fv = get(flow_id)
        definition = getattr(fv, "definition", None)
        if definition is None:
            return ""
        return ledger.state(flow_id, definition.canonical()) or ""
    return verified


def kind_checker(runtime: Any, home: Path | None = None) -> Any:
    """flow_id -> the installed flow's saved_by kind (catalog, local, learned), or None."""
    get = _flow_getter(runtime, home)
    return lambda flow_id: ((getattr(get(flow_id), "saved_by", None) or {}).get("kind"))


def node_registry(runtime: Any, home: Path | None = None) -> Any:
    """The operation registry on this node's installed connectors."""
    from agentos.engage.operations import Registry

    return Registry(installed=installed_checker(runtime, home), verified=verified_checker(runtime, home),
                    kind=kind_checker(runtime, home))


def production_executor(service: Any, runtime: Any) -> Any:
    """The service's Executor on the node's runner and lanes, or None when this runtime has no
    runner or lane manager (the CLI, tests). Building it acts on nothing: windows run only from
    the service's loop, and only while the node switch `engage_sessions` is on."""
    from agentos.engage.executor import Executor
    from agentos.engage.items import ChainedContent, ItemsContent
    from agentos.engage.targets import ConnectorTargets, surface_identities

    if runtime is None or runtime.get("runner") is None or runtime.get("lanes") is None:
        return None
    operations = RunnerOperations(runtime)
    targets = ConnectorTargets(service.store, operations, own=surface_identities(runtime), clock=service.clock)
    executor = Executor(service.store, registry=node_registry(runtime), operations=operations, targets=targets,
                        content=ChainedContent([ItemsContent(service.store, clock=service.clock)]),
                        lanes=NodeLanes(runtime), clock=service.clock, plan_for=service.day_plan,
                        tz_for=service.timezone, ledger=runtime.get("ledger"))
    slots = getattr(runtime.get("runner"), "approved_slot", None)
    if isinstance(slots, dict):  # an approved comment waits for the next session, not the hour it was approved
        slots[SOURCE] = lambda run, now: executor.approved_slot(run.surface or run.lane or "", now, key=run.id)
    executor.enabled = service.sessions_enabled     # the gate holds approved runs while sessions are off
    gates = getattr(runner, "approved_gate", None) if (runner := runtime.get("runner")) is not None else None
    if isinstance(gates, dict):  # and it never goes late: a slot it missed moves it to the next session
        gates[SOURCE] = lambda run, now: executor.approved_gate(run.surface or run.lane or "", now, key=run.id,
                                                                run=run)
    return executor

