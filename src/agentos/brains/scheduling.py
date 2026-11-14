"""Getting a queued brain task ready and onto a seat, for BrainService: prepare its working
directory (context bundle, git checkout), then route it and take a seat when one is free.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from agentos.brains import bundle as bundle_mod
from agentos.brains import confine
from agentos.brains.attempts import read_json
from agentos.brains.models import (
    ContextBundle,
    Privacy,
    TaskRecord,
    TaskStatus,
)
from agentos.core import fs

log = logging.getLogger("agentos.brains")


class SchedulingMixin:
    """BrainService's preparation and scheduling methods."""

    async def _prepare_then_schedule(self, task_id: str) -> None:
        if task_id in self._preparing:
            return
        self._preparing.add(task_id)
        try:
            await self._prepare(task_id)
        finally:
            self._preparing.discard(task_id)
        await self.schedule(task_id)

    async def _prepare(self, task_id: str) -> None:
        rec = self.store.get_task(task_id)
        if rec is None or rec.status != TaskStatus.QUEUED:
            return
        if not rec.prepared:
            raw = read_json(self.task_dir(task_id) / "bundle.json")
            bundle = ContextBundle(**raw) if raw else None
            try:
                work, _ = await asyncio.to_thread(self._brain_dirs, task_id)
                git_env = None
                if bundle is not None and bundle.git is not None:
                    from agentos.git.upstream import config_env

                    pairs = await self._git_pairs(rec)
                    git_env = config_env(pairs) if pairs else None
                handoff = await asyncio.to_thread(self._materialize, task_id, bundle, work,
                                                  redact=rec.privacy == Privacy.REDACTED_CLOUD, git_env=git_env)
            except Exception as exc:  # noqa: BLE001
                rec.status = TaskStatus.FAILED
                rec.status_reason = f"could not prepare the workspace: {getattr(exc, 'message', None) or exc}"[:400]
                rec.ended_at = datetime.now(UTC)
                await self.store.run(self.store.save_task, rec, expect=[TaskStatus.QUEUED])
                await self._cleanup_task(rec)
                return
            rec.handoff = handoff
            rec.prepared = True
            rec.status_reason = "queued"
            await self.store.run(self.store.save_task, rec, expect=[TaskStatus.QUEUED])

    async def _git_pairs(self, rec: TaskRecord) -> list[tuple[str, str]]:
        """Git config that sends the task's git to the node's git gateway (agentos.git) as the task's
        grant, for the remotes declared for its workspace and lanes; [] when there are none. The
        brain gets it in its private ~/.gitconfig, and the bundle's clone uses it too."""
        git = self.runtime.get("git") if self.runtime is not None else None
        if git is None:
            return []
        try:
            return await git.task_pairs(rec.id, grant_id=rec.spend_grant_id, workspace=rec.workspace,
                                        lanes=list(rec.lanes),
                                        ttl_s=(rec.budget.max_minutes or 60) * 60 * 2 + 3600)
        except Exception:  # the task still runs; its git reaches no declared remote
            log.exception("could not set up git for brain task %s", rec.id)
            return []

    def _materialize(self, task_id: str, bundle: ContextBundle | None, work: Path, *, redact: bool,
                     git_env: dict[str, str] | None = None) -> str | None:
        """Write the bundle into the brain's working directory. With a brain user it is written
        into a staging directory of agentosd's own first (no brain can touch it while the clone
        and the archive land), then copied over: as the brain user through the helper, or by
        agentosd without following a link (`fs.copy_tree_beneath`, links copied as links)."""
        user = self._run_as()
        if user is None:  # the work dir is agentosd's own
            return bundle_mod.materialize(bundle, work, redact=redact, allow_file_git=self.allow_file_git,
                                          git_env=git_env)
        stage = self.task_dir(task_id) / "stage"
        shutil.rmtree(stage, ignore_errors=True)
        try:
            handoff = bundle_mod.materialize(bundle, stage, redact=redact, allow_file_git=self.allow_file_git,
                                             git_env=git_env)
            if stage.is_dir():
                if self._via_helper():
                    self._brain_files().unpack(stage, work)
                else:
                    anchor, rel = self._beneath(work)
                    fs.copy_tree_beneath(stage, anchor, rel, owner=(user.pw_uid, user.pw_gid))
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return handoff

    # ------------------------------------------------------------------ scheduling
    async def schedule(self, task_id: str, *, views=None) -> None:
        async with self._sched_lock:
            for _ in range(self.settings.max_attempts + 1):
                if not await self._schedule_once(task_id, views):
                    return
                views = None  # an attempt ended at once (no credential, CLI drift): states changed

    async def _schedule_once(self, task_id: str, views=None) -> bool:
        """One routing pass. True when an attempt started and immediately went back to the queue."""
        rec = self.store.get_task(task_id)
        if rec is None or rec.status != TaskStatus.QUEUED or not rec.prepared:
            return False
        now = self.clock()
        queued_since = rec.queued_since.timestamp() if rec.queued_since else now
        if rec.attempt - rec.resume_from_attempt >= self.settings.max_attempts:
            await self._fail(rec, f"gave up after {self.settings.max_attempts} attempts")
            return False
        if rec.tokens >= (rec.budget.max_tokens or 1 << 62):
            await self._fail(rec, f"token budget of {rec.budget.max_tokens} used up")
            return False
        if rec.budget.max_usd is not None and rec.cost_usd >= rec.budget.max_usd > 0:
            await self._fail(rec, f"dollar budget of ${rec.budget.max_usd:.2f} used up")
            return False
        if self.confinement_required():
            try:
                await asyncio.to_thread(self.check_confinement)
            except confine.BrainUnconfined as exc:
                await self._fail(rec, f"{exc.code}: {exc.message}")
                return False
        views = views if views is not None else await self.brain_views()
        decision = await self.route_for(rec, views=views)
        if decision.action == "run":
            specs = {s.name: s for s, _ in views}
            for cand in decision.candidates:
                if cand.wait:
                    break
                spec = specs[cand.name]
                n = rec.attempt + 1
                adir = self.attempt_dir(rec.id, n)
                if await self.store.run(self.seats.try_acquire, spec, rec, n, adir):
                    self._trace_decision(rec, decision, stage=f"attempt {n}")
                    await self._start_attempt(rec, spec, n, redact=cand.redact)
                    after = self.store.get_task(task_id)
                    return bool(after and after.status == TaskStatus.QUEUED)
            decision.reason = "seats were taken a moment ago; waiting"
        # still queued
        max_q = (rec.budget.max_queue_minutes or self.settings.default_max_queue_minutes) * 60
        if now - queued_since > max_q:
            await self._fail(rec, f"waited {int(max_q // 60)} min in the queue: {decision.reason}")
            return False
        if rec.status_reason != decision.reason:
            rec.status_reason = decision.reason
            await self.store.run(self.store.save_task, rec, expect=[TaskStatus.QUEUED])
            self._trace_decision(rec, decision, stage="queued")
        if decision.action == "needs_approval" and "approval" not in rec.notified:
            rec.notified.append("approval")
            await self.store.run(self.store.save_task, rec, expect=[TaskStatus.QUEUED])
            await self.ports.notify(
                "A brain task is waiting for you",
                f"Task {rec.id} in {rec.workspace} is queued: {decision.reason}.",
                kind="brain.queued", data={"task": rec.id})
        return False
