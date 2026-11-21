"""The profile sync runner: learned operations bring every account in line, with no model and no card.

    sync = ProfileSync(profiles_service)
    await sync.run_once()          # every due (account, field), once
    sync.wake()                    # after a profile is saved: run soon instead of at the next tick

For every approved profile, every attached account and every field the account's site has, it
looks for the site's learned operations (openajar.profiles.learner.ProfileLearner.state). A
freshly learned save is run first only on the account it was learned on; when that run finishes
with its own readback, the learner proves it (`prove`), and only then does it run on the site's
other accounts. A field with no learned operations stays `manual` (an agent can teach it:
profile_learn_start). A field already synced for the value it should show is skipped.

Each run goes through the runner like any connector run: principal `flow` with the owner's grant
and source `profiles`, the only caller a learned operation runs for (RunnerService.learned_problem;
no agent tool serves one). Policy routes it as the owner's commit: it goes ahead on its own, with
no approval card, because the owner's consent is the login they saved and the profile they saved.
The one exception is the owner's own: on a lane with "Ask me first" on, the run waits for the
owner's approval like every publish there. The lane's holds, the connector's pace, the lane's
posting pace and meters, and the egress guard's strict commit scope all apply as for every run.
A lesson (openajar.profiles.learner) never asks: on a lane with "Ask me first" on, its save is
refused (the learn window's ask_first), and it is charged to the lane's meters like any publish.

Outcomes, per (account, field), in the profiles store's `sync` table: `in_sync` (done, read back),
`held` (a challenge, a sign-out or a rate limit on the lane: the lane is held and the field waits;
never a reason to learn again), `relearn` (the site changed: a preflight or the contract broke;
an agent learns it again, at most RELEARN_PER_WEEK times a week), `failed` (anything else; tried
again at the next tick).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from openajar.core.signals import PACE_HOLD_CODES, SIGNAL_OF_RUN_CODE, STOP_OR_WRONG_ACCOUNT, signal_of
from openajar.profiles import mapping

log = logging.getLogger("openajar.profiles")

TICK_S = 1800.0
RUN_WAIT_S = 600.0
RELEARN_PER_WEEK = 2
BROKEN = ("connector_broken", "preflight_mismatch", "connector_contract_broken", "element_not_found",
          "readback_failed")
PRINCIPAL = {"kind": "flow", "id": "", "owner": "owner"}
SOURCE = "profiles"
# A run that ends on one of these waits for the lane (`held`) instead of failing (openajar.core.signals).
SIGNALS = STOP_OR_WRONG_ACCOUNT
SIGNAL_CODES = SIGNAL_OF_RUN_CODE
HELD_CODES = PACE_HOLD_CODES


class ProfileSync:
    def __init__(self, service: Any, learner: Any):
        self.svc = service
        self.learner = learner
        self.runtime = service.runtime
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="openajar-profile-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("profiles: sync tick failed")
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), TICK_S)

    # ---------------------------------------------------------------- one pass
    def due(self) -> list[dict[str, Any]]:
        """Every (account, field, value) a verified learned operation should bring in line now."""
        from openajar.profiles.service import value_hash

        out = []
        for pv in self.svc.store.approved():
            for a in pv.definition.accounts:
                r = mapping.resolve(pv.definition, a)
                if r.problems:
                    continue
                rows = self.svc.store.sync(a.surface)
                for f in r.manual:
                    value = r.values[f]
                    if not isinstance(value, str):
                        continue
                    st = self.learner.state(r.site, f)
                    if st is None:
                        continue
                    proving = not st["verified"]
                    if proving and not (st["read_verified"] and st["learned_on"] == a.surface):
                        continue          # a save not proved yet runs only where it was learned
                    vh = value_hash(value)
                    row = rows.get(f) or {}
                    if row.get("value_hash") == vh and row.get("status") in ("in_sync", "relearn"):
                        continue
                    out.append({"surface": a.surface, "site": r.site, "field": f, "value": value, "hash": vh,
                                "flow_id": st["commit"], "proving": proving})
        return out

    async def run_once(self) -> list[dict[str, Any]]:
        if self.runtime is None or self.runtime.get("runner") is None:
            return []
        async with self._lock:
            done = []
            for item in self.due():
                done.append(await self._sync(item))
            return done

    async def _sync(self, item: dict[str, Any]) -> dict[str, Any]:
        from openajar.core.errors import OpenajarError

        runner = self.runtime.require("runner")
        surface, field, vh = item["surface"], item["field"], item["hash"]
        key = f"profile-sync:{surface}:{field}:{vh}"
        try:
            run = runner.existing_run(key, flow_id=item["flow_id"], lane=surface)
            if run is None:
                run, _ = runner.submit(flow_id=item["flow_id"], params={"value": item["value"]}, surface=surface,
                                       principal=dict(PRINCIPAL), source=SOURCE, idempotency_key=key,
                                       authorized=False)
            if str(run.status) in ("queued", "running", "waiting"):
                run = await runner.wait(run.id, RUN_WAIT_S)
        except OpenajarError as e:
            code = getattr(e, "code", "error")
            status = "held" if code in HELD_CODES or signal_of(code) else "failed"
            self.svc.store.set_sync(surface, field, status, value_hash=vh, error=f"{code}: {e.message}"[:300])
            return {**item, "status": status, "error": code}
        state = str(run.status)
        if state in ("done", "succeeded"):
            self.svc.store.set_sync(surface, field, "in_sync", value_hash=vh, run_id=run.id)
            proved = item.get("proving") and self.learner.prove(surface, item["site"], field, run)
            return {**item, "status": "in_sync", "run": run.id, **({"proved": True} if proved else {})}
        code = str((run.error or {}).get("code") or state)
        if state in ("queued", "running", "waiting"):
            return {**item, "status": "waiting", "run": run.id}     # still going: the next tick looks again
        if code in HELD_CODES or signal_of(code):
            status = "held"
        elif code in BROKEN:
            status = "relearn"
        else:
            status = "failed"
        relearn = status == "relearn" and self._may_relearn(surface, field)
        self.svc.store.set_sync(surface, field, status, value_hash=vh, run_id=run.id, error=code, relearn=relearn)
        return {**item, "status": status, "run": run.id, "error": code}

    def _may_relearn(self, surface: str, field: str) -> bool:
        row = self.svc.store.sync(surface).get(field) or {}
        week_ago = datetime.now(UTC) - timedelta(days=7)
        recent = [t for t in row.get("relearns", []) if datetime.fromisoformat(t) > week_ago]
        return len(recent) < RELEARN_PER_WEEK
