"""Queueing a run (RunnerService.submit): the checks before it is queued, an identical open call's
run handed back (open_twin), and the connector checks (surface, learned operations).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from openajar.core.aio import maybe_await
from openajar.core.errors import (
    InvalidRequest,
    PolicyDenied,
    ServiceUnavailable,
)
from openajar.core.models import LANE_ID_RE, Effect, Principal, RunKind, RunStatus
from openajar.runner.execution import learned_flow
from openajar.runner.names import is_connector_id
from openajar.runner.owner import TRY_SOURCE, is_owner
from openajar.runner.records import Run
from openajar.runner.run_rules import LEARNED_SOURCE, OPEN_STATUSES, OWNER_GRANT, TWIN_WINDOW, is_person
from openajar.runner.util import new_id


class SubmitMixin:
    """submit(), open_twin() and the connector checks of RunnerService (openajar.runner.runner)."""

    def submit(self, *, flow_id: str | None = None, version: str | None = None,
               params: dict[str, Any] | None = None, lane: str | None = None, kind: RunKind | str = RunKind.FLOW,
               idempotency_key: str | None = None, parent: str | None = None,
               principal: Principal | dict[str, Any] | None = None, source: str = "tool",
               schedule_id: str | None = None, trigger: dict[str, Any] | None = None,
               authorized: bool = False, approval_id: str | None = None,
               grant_id: str | None = None, not_before: datetime | None = None,
               surface: str | None = None, auto: bool = False, pace_id: int | None = None,
               target: Any = None) -> tuple[Run, bool]:
        """Queue a run. Returns (run, created); an existing idempotency key returns the
        existing run and creates nothing (a key used for another flow or lane is refused).

        `grant_id` is the grant the run acts under; it defaults to the principal's grant, and
        to the owner's grant for the human owner or an anonymous call from inside the node.
        With a policy service the grant must be live and cover the lane and the flow's
        effective effect, or PolicyDenied is raised and nothing is queued.

        `not_before` (aware) is the earliest start: the run waits for it in the queue, and an
        approval decided earlier keeps it waiting until then.

        A connector operation (`connector.<site>.<op>`) runs on a surface: `surface` (or `lane`,
        from entry points that pass `on`) names it, and its binding decides the lane and fills
        the binding params, which the caller may never pass. The grant must cover the surface,
        the operation must not be broken there, and its pace must allow one more run. `target`: a
        binding already made, by a site API's try on a lane no surface names yet.

        `pace_id`: the lane's pace ledger row the caller's authorization wrote for this publish
        (Decision.pace_ids); a commit run with none is checked and recorded right before it runs."""
        kind = RunKind(kind)
        asked = dict(params or {}) if isinstance(params, dict) else None  # the inputs as the caller passed them
        if not_before is not None and not_before.tzinfo is None:
            raise InvalidRequest("not_before needs a timezone (an ISO 8601 time like 2026-11-08T09:00:00+02:00)")
        if params is not None and not isinstance(params, dict):
            raise InvalidRequest("params must be a JSON object of name: value")
        if idempotency_key:
            existing = self.existing_run(idempotency_key, flow_id=flow_id, lane=surface or lane, kind=kind)
            if existing:
                return existing, False
        principal, grant_id = _principal_and_grant(principal, grant_id)
        fv, effect = None, None
        if kind == RunKind.FLOW:
            fv, lane, params, surface = self._flow_target(flow_id, version, params, lane, surface, trigger, target)
            version, effect = fv.version, fv.effect
        elif lane is not None and not LANE_ID_RE.match(lane):
            raise InvalidRequest(f"invalid lane id {lane!r}")
        self.check_grant(grant_id, principal, effect=effect, lane=surface or lane)
        self._check_binding(grant_id, flow_id=flow_id, version=version, target=surface or lane,
                            key=idempotency_key, params=asked)
        if surface is not None:
            self.check_connector(fv, surface, trial=source == TRY_SOURCE, person=is_person(principal))
            problem = self.learned_problem(fv, surface, principal, source)
            if problem:
                raise PolicyDenied(problem)
        now = self.clock.now()
        start_after = not_before if not_before is not None and not_before > now else None
        run = Run(id=new_id("run"), kind=kind, lane=lane, surface=surface, flow_id=flow_id, flow_version=version,
                  params=params or {}, idempotency_key=idempotency_key, parent=parent,
                  principal=principal, grant_id=grant_id, source=source, schedule_id=schedule_id,
                  trigger=trigger, authorized=authorized, auto=auto, approval_id=approval_id, created_at=now,
                  pace_id=pace_id if authorized else None,
                  updated_at=now,
                  status=RunStatus.WAITING_APPROVAL if approval_id else RunStatus.QUEUED,
                  parked_at=now if approval_id else None, not_before=start_after, start_after=start_after)
        run, created = self.store.insert_run(run)
        if created:
            self._emit(run)
            self._wake.set()
        return run, created

    def _flow_target(self, flow_id: str | None, version: str | None, params: dict[str, Any] | None,
                     lane: str | None, surface: str | None, trigger: dict[str, Any] | None,
                     target: Any) -> tuple[Any, str | None, dict[str, Any] | None, str | None]:
        """The flow version a flow run runs, and its lane, params and surface: a connector
        operation's from its binding (the caller never passes the binding params), a flow's from
        the flow and its params schema."""
        if not flow_id:
            raise InvalidRequest("a flow run needs a flow id")
        fv = self.flows.get(flow_id, version)
        if fv.definition.sends and (trigger or {}).get("kind") != "workflow":
            raise PolicyDenied(f"{fv.id} sends to people, so it runs only as a workflow's send step, which keeps "
                               "the owner's list, the suppression list, the daily limit and an unsubscribe")
        if is_connector_id(fv.id):
            target = target or self.connector_target(fv, surface or lane, params, lane=lane if surface else None)
            return fv, target.lane, target.params, target.surface.id
        if surface is not None:
            raise InvalidRequest(f"{fv.id} is a flow, not a connector operation; it runs on a lane, not a surface")
        return fv, self.flows.resolve_lane(fv, lane), self.flows.validate_params(fv, params), surface

    @contextlib.asynccontextmanager
    async def twin_guard(self, *parts: Any) -> AsyncIterator[None]:
        """Hold identical keyless calls one at a time, from open_twin to submit, so two that arrive
        together cannot both make a run (and an approval) for the same post."""
        key = json.dumps([p.timestamp() if isinstance(p, datetime) else p for p in parts], sort_keys=True,
                         default=str)
        lock = self._twin_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                yield
        finally:
            if not lock.locked() and self._twin_locks.get(key) is lock:
                self._twin_locks.pop(key, None)

    async def open_twin(self, *, flow_id: str, version: str, params: dict[str, Any], grant_id: str | None,
                        lane: str | None = None, surface: str | None = None, not_before: datetime | None = None,
                        content: Any = None) -> Run | None:
        """A run a keyless call would duplicate (QA-2 #11): the same flow version, lane (or surface),
        params, grant and start, made by a tool call, and not finished yet. A retry after a timeout then
        gets the run that already waits, not a second run and a second approval. `content`: what the
        call's approval would bind, which finds a run whose params the owner edited when approving.
        A run that asked for an approval or waits for its slot counts however old it is; any other one
        queued or running counts for TWIN_WINDOW."""
        if not grant_id:
            return None
        now = self.clock.now()
        for run in self.store.list_runs(status=OPEN_STATUSES, flow_id=flow_id,
                                        lane=None if surface else lane, limit=200):
            if (run.flow_version != version or run.grant_id != grant_id
                    or run.surface != surface or (surface is None and run.lane != lane)
                    or run.source != "tool" or run.schedule_id is not None or run.parent is not None):
                continue
            asked = not_before if not_before is not None and not_before > run.created_at else None
            if run.start_after != asked:
                continue
            if not (run.approval_id is not None or run.start_after is not None
                    or now - run.created_at <= TWIN_WINDOW):
                continue
            if run.params == params or (content is not None and await self._edited_from(run, content)):
                return run
        return None

    async def _edited_from(self, run: Run, content: Any) -> bool:
        """Did the owner approve `run` with an edit, from an approval for exactly `content`?"""
        from openajar.policy.approval_binding import content_digest

        get = getattr(self._service("approvals"), "get", None)
        if not run.approval_id or not callable(get):
            return False
        try:
            ap = await maybe_await(get(run.approval_id))
        except Exception:  # noqa: BLE001 - an approval that is gone edited nothing
            return False
        edit = ap.get("edit") if isinstance(ap, dict) else getattr(ap, "edit", None)
        before = edit.get("digest_before") if isinstance(edit, dict) else None
        return isinstance(before, str) and before == content_digest(content)

    def existing_run(self, key: str, *, flow_id: str | None = None, lane: str | None = None,
                     kind: RunKind | str = RunKind.FLOW, params: dict[str, Any] | None = None) -> Run | None:
        """The run an idempotency key already names, or None. The same key for another flow,
        lane or kind is refused: it would hand back a run the caller did not ask for. So is one
        with other `params`, for a caller that passes them."""
        existing = self.store.run_by_key(key)
        if existing is None:
            return None
        if (existing.kind != RunKind(kind) or (flow_id is not None and existing.flow_id != flow_id)
                or (lane is not None and existing.lane is not None and lane not in (existing.lane, existing.surface))):
            # never which run: it may be on a lane the caller's grant does not reach
            raise InvalidRequest(f"idempotency key {key!r} is already used by another run; use a new key")
        if params is not None and existing.params != params:
            raise InvalidRequest(f"idempotency key {key!r} is already used by run {existing.id} for other "
                                 "arguments; use a new key")
        return existing

    # ------------------------------------------------------------ connector runs

    def connector_target(self, fv: Any, on: str | None, params: dict[str, Any] | None, *,
                         lane: str | None = None, validate: bool = True) -> Any:
        """The binding of a run of the connector operation `fv` on surface `on`
        (connectors.surfaces.Target). `lane`, when given, must be the binding's lane."""
        if self.surfaces is None:
            raise ServiceUnavailable("connector operations need the surfaces store, which this node lacks")
        self._check_active(fv)
        target = self.surfaces.target(fv, on, params, validate=validate)
        if lane is not None and lane != target.lane:
            raise InvalidRequest(f"surface {target.surface.id} runs on {target.lane}, not {lane}")
        return target

    def _check_active(self, fv: Any) -> None:
        """A site API runs what the owner activated: a staged version is a try's, and a try comes
        with its own target. Every way to pin a version comes through here, so none of them runs a
        draft nobody activated. The catalog is reviewed, and may pin any version it has."""
        head = self.store.get_flow(fv.id)
        if (fv.saved_by or {}).get("kind") == "local" and (head is None or head.version != fv.version):
            raise PolicyDenied(f"{fv.id} {fv.version} is not active: a site API runs only the version the owner "
                               "activated. Leave the version out, or try it with site_api_try")

    def check_connector(self, fv: Any, surface: str, *, exclude: str | None = None, started_only: bool = False,
                        trial: bool = False, person: bool = False) -> None:
        """Raise ConnectorBroken when the operation is broken on the surface (or a person paused its
        site API), PaceLimited when its pace allows no more runs now. `trial`: a site API's try, which
        is how a repair is checked, so health does not stop it. A pause does, unless `person`: a person
        fixing a paused API tests the fix, and an agent waits for them."""
        from openajar.runner.connectors.pace import pace_problem
        from openajar.runner.connectors.surfaces import ConnectorBroken, PaceLimited

        meta = fv.definition.connector or {}
        op = f"{meta.get('connector')}.{meta.get('operation')}"
        site_apis = self.site_apis
        if not (trial and person) and site_apis is not None and site_apis.state_of(meta.get("connector")) == "paused":
            from openajar.runner.siteapis.service import ApiPaused

            raise ApiPaused(f"a person paused {meta.get('connector')}; nothing runs until they activate it")
        broken = self.health.broken(op, surface) if self.health is not None and not trial else None
        if broken:
            raise ConnectorBroken(f"{op} is broken on {surface} since {broken.get('since')} "
                                  f"({broken.get('code')} at step {broken.get('step')}); nothing runs until a "
                                  "repaired version is installed. Do not post it another way.")
        problem = pace_problem(self.store, fv.id, surface, meta.get("pace"), self.clock.now(), exclude=exclude,
                               started_only=started_only, local=(fv.saved_by or {}).get("kind") == "local")
        if problem:
            raise PaceLimited(problem[0], retry_at=problem[1])

    def learned_problem(self, fv: Any, surface: str, principal: dict[str, Any] | None, source: str | None) -> str | None:
        """Why a run of a learned operation (openajar.profiles.learn) may not start, or None. It runs
        only for the profile sync and learner (source `profiles`, the node's own `flow` principal):
        no agent, tool, schedule or workflow runs one. A learned save (commit) runs only once it is
        verified, or, while it is still being proved, on the account it was learned on."""
        if not learned_flow(fv):
            return None
        p = principal if isinstance(principal, dict) else {}
        if source != LEARNED_SOURCE or p.get("kind") != "flow" or p.get("id") or p.get("grant_id"):
            return (f"{fv.id} was learned on this node and only the profile sync runs it: change the profile "
                    "instead (profile_update), and openajar brings every account in line")
        if fv.effect != Effect.COMMIT:
            return None
        saved = fv.saved_by or {}
        learner = getattr(self._service("profiles"), "learner", None)
        state = learner.state(saved.get("site"), saved.get("field")) if learner is not None and \
            saved.get("site") and saved.get("field") else None
        if state is None or state.get("commit") != fv.id:
            return f"{fv.id} is not a learned save the profile learner knows; it does not run"
        if state.get("verified"):
            return None
        if state.get("read_verified") and state.get("learned_on") == surface:
            return None
        return (f"{fv.id} is not proved yet: its first save runs on {state.get('learned_on') or 'the account it was '
                'learned on'}, and only after that save is read back does it run on {surface}")

    def connector_approval(self, fv: Any, run: Run) -> tuple[str, dict[str, Any]]:
        """(summary, content) of a queued connector run, as its tool would have asked."""
        target = self.connector_target(fv, run.surface, {k: v for k, v in run.params.items()
                                                         if k not in ("acts_as_id", "account_language")})
        if target.lane != run.lane or target.params != run.params:
            raise PolicyDenied(f"the binding of surface {run.surface} changed since this run was queued (lane or "
                               "acting as); queue it again")
        return self.surfaces.approval(fv, target)


def _principal_and_grant(principal: Principal | dict[str, Any] | None,
                         grant_id: str | None) -> tuple[dict[str, Any], str | None]:
    """The principal as a dict, and the grant a run acts under: the one passed, else the principal's,
    else the owner's grant for the human owner or an anonymous call from inside the node."""
    if isinstance(principal, Principal):
        principal = principal.model_dump()
    principal = principal or {}
    if grant_id is None:
        grant_id = principal.get("grant_id") or (
            OWNER_GRANT if not principal.get("id") or is_owner(principal) else None)
    return principal, grant_id
