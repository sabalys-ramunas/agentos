"""Decision brains, for BrainService: `decide` answers a typed question with one in-process HTTP call
(agentos.brains.decisions), and `pick` asks a task brain to choose between options. No task, no
shim and no transcript, under the same privacy floor as tasks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Iterable
from typing import Any

import httpx

from agentos.brains import decisions
from agentos.brains.credentials import brain_ref
from agentos.brains.models import (
    BrainKind,
    BrainSpec,
    BrainState,
    Budget,
    Candidate,
    GrantView,
    Requirements,
    RouteDecision,
    Runner,
    TaskRecord,
    TaskStatus,
)
from agentos.brains.router import route
from agentos.core.errors import InvalidRequest
from agentos.core.models import Principal


class _NextBrain(Exception):
    """This decision brain cannot answer; the next in the chain may. The last one's reason is what the
    caller gets, as a DecisionUnavailable."""

    def __init__(self, reason: str, *, state: str | None = None, retry_after: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.state = state
        self.retry_after = retry_after


PICK_RUNNERS = (Runner.CLAUDE, Runner.CHAT)


PICK_TIMEOUT_S = 90.0


PICK_MAX_TURNS = 2


PICK_POLL_S = 0.2


class DecidingMixin:
    """BrainService's decision methods."""

    # ------------------------------------------------------------------ decisions
    async def decide(self, principal: Principal, *, workspace: str, state: Any,
                     questions: dict[str, dict[str, Any]], must_not_send: Iterable[str],
                     budget: Budget | None = None, job: str | None = None, taint: Iterable[str] = (),
                     brain: str | None = None, assets: dict[str, Any] | None = None,
                     ctx: Any = None) -> dict[str, Any]:
        """Answer typed questions about `state` on a decision brain, in process, and return
        {"brain", "model", "answers", "usage": {"input_tokens", "output_tokens", "estimated"},
        "cost_usd", "latency_ms", "retries"}. Nothing is queued: it answers now or raises.

        must_not_send: strings that must never leave the node (a password, a code). A body holding
        one is refused before any call. There is no default: pass () only when there are none.
        budget: `max_usd` is required (metered spend) and `max_tokens` optional. Without `job` they
        limit this call. With `job`, an id of your choosing, they limit everything spent under that
        id, here and in earlier calls, even across a restart: that is how one budget covers the
        hundreds of calls of one task. The grant's own remainder applies either way.
        taint: labels of what is in `state`; "private" needs the usual co-sign, on top of the
        grant's own taint. brain: use this one decision brain. assets: images, by placeholder.
        ctx: a tool context, the only way to reach a person for a co-sign. Without one, what needs
        a co-sign is refused, so a hot path wants a grant that already allows metered spend.

        Raises DecisionUnavailable (nothing can answer now; see its `state` and `retry_after`),
        DecisionBlocked, DecisionRejected, DecisionOverBudget, or PolicyDenied."""
        labels = await self.ports.taint(principal) | {t.lower() for t in taint}
        return await self._decide(principal, await self._grant_view(principal), labels, workspace=workspace,
                                  state=state, questions=questions, must_not_send=must_not_send, budget=budget,
                                  job=job, brain=brain, assets=assets, ctx=ctx)

    def _pick_brain(self, name: str | None) -> BrainSpec:
        """The brain a pick runs on: the one asked for, else the first that writes in the owner's
        order, a subscription before a metered key. Codex cannot promise to run without tools."""
        if name:
            spec = self._task_brain(name)
            if spec.runner not in PICK_RUNNERS:
                raise InvalidRequest(f"{name} cannot answer a pick: it would run with tools")
            return spec
        ready = [s for s in self.store.list_brains() if s.enabled and s.runner in PICK_RUNNERS]
        ready.sort(key=lambda s: (s.kind != BrainKind.SUBSCRIPTION, s.order))
        if not ready:
            raise decisions.DecisionUnavailable("no model that writes is signed in: run `agentos brains signin claude`")
        return ready[0]

    async def pick(self, principal: Principal, *, workspace: str, question: str, options: dict[str, str],
                   state: Any, must_not_send: Iterable[str], brain: str | None = None, ctx: Any = None,
                   timeout_s: float = PICK_TIMEOUT_S, budget: Budget | None = None) -> dict[str, Any]:
        """One typed answer from a model that writes, for a worker that is unsure: the same options
        the worker had, so a rescue cannot invent an action, a URL or a selector. Returns {"brain",
        "choice" (None when no option is right), "confidence", "tokens", "cost_usd", "latency_ms",
        "task"}.

        It runs as a brain task, so a Claude subscription works here (its token only runs its own
        CLI) under the same confinement, seats, privacy floor and traces. It runs with no tools at
        all, because `state` is a stranger's text. The caller has already authorized Effect.BRAIN.
        A task takes seconds, not milliseconds: this is for the rare step, never every step.

        budget: `max_usd` and `max_tokens` cap this one pick, so pass what is left, not the whole
        allowance. A metered brain is refused without `max_usd`, as any task is; a subscription
        spends no money, so `cost_usd` is 0 there and `tokens` is what it used. Only those two
        fields are read: a pick's turns and minutes are its own.

        must_not_send has no default, as in `decide`. Raises DecisionBlocked, DecisionOverBudget
        (`max_usd` is 0 or less on a metered brain), DecisionRejected, DecisionUnavailable (nothing
        answered in time; the worker should stop for a person). Once
        a task ran, the last two carry its `tokens` and `cost_usd`, because a bad answer is paid for
        too. After a timeout that is what was known then, and a late attempt can add to it."""
        spec = self._pick_brain(brain)
        prompt = decisions.pick_prompt(question, options, state)
        if decisions.leak({"prompt": prompt}, prompt.encode(), list(must_not_send)) is not None:
            raise decisions.DecisionBlocked("a pick held something that must not leave the node, so it was not sent")
        started = time.monotonic()
        limit = budget or Budget()
        if spec.kind == BrainKind.METERED and limit.max_usd is not None and limit.max_usd <= 0:
            raise decisions.DecisionOverBudget(f"budget: {spec.name} is metered and no money is left for a pick")
        out = await self.submit(principal, prompt=prompt, workspace=workspace, brain=spec.name, ctx=ctx,
                                requires=Requirements(tools=False, no_tools=True),
                                budget=Budget(max_usd=limit.max_usd, max_tokens=limit.max_tokens,
                                              max_turns=PICK_MAX_TURNS, max_minutes=timeout_s / 60,
                                              max_queue_minutes=timeout_s / 60))
        task = out["id"]
        try:
            rec, said = await self._pick_reply(task, spec, principal, started, timeout_s)
            choice, confidence = decisions.read_pick(said, options)
        except (decisions.DecisionRejected, decisions.DecisionUnavailable) as exc:
            spent = self._get(task)
            exc.tokens, exc.cost_usd = spent.tokens, spent.cost_usd
            raise
        return {"brain": spec.name, "choice": choice, "confidence": confidence, "tokens": rec.tokens,
                "cost_usd": rec.cost_usd, "latency_ms": round((time.monotonic() - started) * 1000), "task": task}

    async def _pick_reply(self, task: str, spec: BrainSpec, principal: Principal, started: float,
                          timeout_s: float) -> tuple[TaskRecord, str]:
        """The finished task and what it said, or why it will not say anything."""
        while not (rec := self._get(task)).status.terminal:
            if rec.status == TaskStatus.INTERRUPTED or time.monotonic() - started > timeout_s:
                await self.cancel(task, principal)
                raise decisions.DecisionUnavailable(f"{spec.name} did not answer in {timeout_s:.0f} s")
            if rec.status == TaskStatus.QUEUED:  # a brain that cannot start will not start: say so now
                state, why = await self.effective_state(spec)
                if state != BrainState.READY:
                    await self.cancel(task, principal)
                    raise decisions.DecisionUnavailable(f"{spec.name} did not answer: {why or state.value}",
                                                        state=state.value)
            await asyncio.sleep(PICK_POLL_S)
        if rec.status != TaskStatus.SUCCEEDED_UNVERIFIED:
            raise decisions.DecisionUnavailable(f"{spec.name} did not answer: {rec.status_reason or rec.status.value}")
        events = self.tail(task, principal, limit=20)["events"]
        return rec, next((e.get("text") or "" for e in reversed(events) if e.get("kind") == "result"), "")

    async def probe_decision(self, principal: Principal, name: str) -> dict[str, Any]:
        """One harmless question for `agentos brains test --live`. The owner at the node's own shell
        is the co-sign for this fixed, cent-sized call; no tool reaches it. The privacy floor holds."""
        if not self.get_spec(name).caps.decisions:
            raise InvalidRequest(f"{name} is not a decision brain")
        question = {"type": "noul", "instructions": "Does this sentence mention the sky?"}
        return await self._decide(principal, GrantView(allow_metered=True), set(), workspace="agentos-test",
                                  state="The sky is blue today.", questions={"mentions_sky": question},
                                  must_not_send=(), budget=Budget(max_usd=0.01), brain=name)

    async def _decide(self, principal: Principal, grant: GrantView, labels: set[str], *, workspace: str, state: Any,
                      questions: dict[str, dict[str, Any]], must_not_send: Iterable[str],
                      budget: Budget | None = None, job: str | None = None, brain: str | None = None,
                      assets: dict[str, Any] | None = None, ctx: Any = None) -> dict[str, Any]:
        self._check_workspace(workspace)
        decisions.check_job(job)
        ask = decisions.Ask.build(state, questions, assets, must_not_send)
        budget = budget or Budget()
        if brain and not self.get_spec(brain).caps.decisions:
            raise InvalidRequest(f"{brain} runs tasks and cannot answer typed questions")
        tokens, usd = self._decision_spent(job)
        if budget.max_usd is not None and usd >= budget.max_usd > 0:
            raise decisions.DecisionOverBudget(f"budget: ${usd:.6f} of ${budget.max_usd:.6f} is spent")
        if budget.max_tokens is not None and tokens >= budget.max_tokens:
            raise decisions.DecisionOverBudget(f"budget: {tokens} of {budget.max_tokens} tokens are spent")
        # only decision brains are looked at, so an unrelated CLI's cold probe never holds up a decision
        views = [await self._view(s) for s in self.store.list_brains() if s.caps.decisions]
        if not views:
            raise decisions.DecisionUnavailable("no decision brain is registered on this node")
        privacy = self.privacy_for(workspace)
        req = Requirements(tools=False, decisions=True, min_context=ask.context_tokens)

        async def routed(with_grant: GrantView | None = None) -> RouteDecision:
            g = with_grant or grant
            return route(req, privacy=privacy, taint=labels, grant=g, brains=[v for _, v in views],
                         budget=self._decision_left(job, budget, g), online=await self.online(), pinned=brain)

        decision = await routed()
        if decision.action == "needs_approval":
            digest = hashlib.sha256(decisions.encode({"state": state, "questions": questions})).hexdigest()
            await self._cosign(decision, ctx, workspace=workspace, budget=budget, grant=grant,
                               what="typed decisions", content={"body_sha256": digest}, trial=routed,
                               specs={sp.name: sp for sp, _ in views})
            # read again after the wait for the yes: a brain signed in again meanwhile is another brain
            views = [await self._view(s) for s in self.store.list_brains() if s.caps.decisions]
            decision = await routed()
        if decision.action != "run":
            self.ports.trace("brain.decide", ctx=ctx, outcome="not_routed", workspace=workspace, job=job,
                             action=decision.action, reason=decision.reason)
            raise self._decision_refused(decision)
        specs = {s.name: s for s, _ in views}
        last: _NextBrain | None = None
        for cand in decision.candidates:
            try:
                return await self._decide_on(specs[cand.name], cand, ask, principal, grant, budget, job, workspace, ctx,
                                             labels=labels)
            except _NextBrain as exc:
                last = exc
                self.ports.trace("brain.decide", ctx=ctx, outcome="next_brain", brain=cand.name, workspace=workspace,
                                 job=job, reason=exc.reason)
        assert last is not None  # the router said run, so there was a candidate
        raise decisions.DecisionUnavailable(last.reason, state=last.state, retry_after=last.retry_after)

    def _decision_refused(self, decision: RouteDecision) -> decisions.DecisionUnavailable:
        """What the router said, with the brain state behind it when one state explains it all."""
        rejected = decision.rejected
        if rejected and all(r.reason.startswith("cooling down") for r in rejected):
            until = min(self.seats.override(r.name)[2] or 0.0 for r in rejected)
            return decisions.DecisionUnavailable(decision.reason, state=BrainState.COOLING_DOWN.value,
                                                 retry_after=max(until - self.clock(), 0.0))
        if rejected and all(r.reason.startswith("needs the owner") for r in rejected):
            return decisions.DecisionUnavailable(decision.reason, state=BrainState.NEEDS_AUTH.value)
        return decisions.DecisionUnavailable(decision.reason)

    def _decision_spent(self, job: str | None) -> tuple[int, float]:
        """Tokens and dollars spent under `job`, counting calls still in flight."""
        if not job:
            return 0, 0.0
        tokens, usd = self.store.usage_for_task(job)
        held_tokens, held_usd = self._holds.total(job)
        return tokens + held_tokens, usd + held_usd

    def _decision_left(self, job: str | None, budget: Budget, grant: GrantView) -> Budget:
        """What is left to spend. Never above the grant's remainder: the router treats a budget over
        it as a request for more, and the grant shrinks with every call of a job set to its size."""
        tokens, usd = self._decision_spent(job)
        max_usd = None if budget.max_usd is None else budget.max_usd - usd
        if max_usd is not None and grant.remaining_usd is not None:
            max_usd = min(max_usd, grant.remaining_usd)
        max_tokens = None if budget.max_tokens is None else budget.max_tokens - tokens
        if max_tokens is not None and grant.remaining_tokens is not None:
            max_tokens = min(max_tokens, grant.remaining_tokens)
        return Budget(max_usd=max_usd, max_tokens=max_tokens)

    async def _decide_on(self, spec: BrainSpec, cand: Candidate, ask: decisions.Ask, principal: Principal,
                         grant: GrantView, budget: Budget, job: str | None, workspace: str,
                         ctx: Any, labels: set[str] = frozenset()) -> dict[str, Any]:
        """One brain's try: everything that must hold before the body leaves, the call, the money."""
        signin = self._signin(spec.name)
        if why := self._cosign_problem(grant, labels, spec):
            raise _NextBrain(why)
        key = await self.creds.get(brain_ref(spec.name), purpose=f"typed decision in {workspace}")
        if self._signin(spec.name) != signin:
            raise _NextBrain(f"{spec.name} was signed in again just now")
        if not key:
            await self._decision_auth_failed(spec, "no credential stored")
            raise _NextBrain(f"{spec.name} has no credential; run: agentos brains auth {spec.name}",
                             state=BrainState.NEEDS_AUTH.value)
        payload = ask.payload(spec.model or "", redact=cand.redact)
        body = decisions.encode(payload)
        digest = hashlib.sha256(body).hexdigest()
        hit = decisions.leak(payload, body, (*ask.must_not_send, key))
        if hit is not None:
            what = f"must_not_send[{hit}]" if hit < len(ask.must_not_send) else f"the API key of {spec.name}"
            self.ports.trace("brain.decide", ctx=ctx, outcome="blocked", brain=spec.name, workspace=workspace, job=job,
                             body_sha256=digest, what=what)
            raise decisions.DecisionBlocked(f"the body holds {what}, so nothing was sent")
        guess = ask.budget_tokens
        guess_usd = decisions.cost_usd(spec, guess, 0)
        left = self._decision_left(job, budget, grant)
        if left.max_usd is not None and guess_usd > left.max_usd:
            raise decisions.DecisionOverBudget(f"budget: this call could cost ${guess_usd:.6f} and "
                                               f"${max(left.max_usd, 0.0):.6f} is left")
        if left.max_tokens is not None and guess > left.max_tokens:
            raise decisions.DecisionOverBudget(f"budget: this call could use {guess} tokens and "
                                               f"{max(left.max_tokens, 0)} are left")
        started = time.monotonic()
        with self._holds.hold(job, guess, guess_usd):
            reply, retries = await self._decide_post(spec, body, key, ask)
            data = await self._decide_read(spec, reply, (*ask.must_not_send, key))
            counted = decisions.parse_usage(data)
            input_tokens, output_tokens = counted or (guess, 0)  # the budget must still move
            usd = decisions.cost_usd(spec, input_tokens, output_tokens)
            await self.store.run(self.seats.record_usage, spec.name, job, input_tokens + output_tokens, usd)
            await self.ports.charge(getattr(principal, "grant_id", None), tokens=input_tokens + output_tokens, usd=usd)
        answers = decisions.parse_answers(data, payload["questions"])
        if isinstance(answers, str):
            raise _NextBrain(f"{spec.name} sent a reply that cannot be used: {answers}")
        def succeeded() -> None:  # a call that was in flight must not end a cooldown (checked on the worker)
            if self.seats.override(spec.name)[0] is None:
                self.seats.mark_success(spec.name)

        await self.store.run(succeeded)
        latency_ms = round((time.monotonic() - started) * 1000)
        self.ports.trace("brain.decide", ctx=ctx, outcome="answered", brain=spec.name, workspace=workspace, job=job,
                         questions=len(ask.questions), body_sha256=digest, redacted=cand.redact, retries=retries,
                         input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=usd, latency_ms=latency_ms)
        return {"brain": spec.name, "model": answers.model or spec.model, "answers": answers.answers,
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "estimated": counted is None},
                "cost_usd": usd, "latency_ms": latency_ms, "retries": retries}

    async def _decide_post(self, spec: BrainSpec, body: bytes, key: str,
                           ask: decisions.Ask) -> tuple[decisions.Reply, int]:
        """The call, again on the statuses worth another try. Hands back the last reply, and how many
        tries came after the first. A wait longer than a caller should sit through ends the tries."""
        url = (spec.base_url or "").rstrip("/") + "/systemone"
        s = self.settings
        for n in range(s.decide_retries + 1):
            try:
                reply = await self.wire.post(url, body, key, images=ask.uses_images)
            except (httpx.HTTPError, UnicodeError) as exc:  # a key that is no header value is a UnicodeError
                raise _NextBrain(f"{spec.name} did not answer ({type(exc).__name__})") from None
            if reply.status not in decisions.RETRY_STATUSES or n == s.decide_retries:
                return reply, n
            wait = decisions.retry_after(reply.headers.get("retry-after"), self.clock())
            if wait is None:
                wait = s.decide_backoff_seconds * 2 ** n * random.uniform(1.0, 1.25)  # parallel calls spread out
            if wait > s.decide_max_wait_seconds:
                return reply, n
            await asyncio.sleep(wait)
        raise AssertionError("the last try always returns")

    async def _decide_read(self, spec: BrainSpec, reply: decisions.Reply, secrets: Iterable[str]) -> Any:
        """The reply's JSON, or the failure as the brain state it means: 401 and 403 need the owner,
        429 cools the brain down, and what another brain might answer tries the next one."""
        status = reply.status
        if 200 <= status < 300:
            try:
                return json.loads(reply.raw)
            except ValueError:
                return None  # billed or not, it answered: the caller counts it and tries the next brain
        if status in (401, 403):
            await self._decision_auth_failed(spec, f"the endpoint answered HTTP {status}")
            raise _NextBrain(f"{spec.name} rejected its API key (HTTP {status})", state=BrainState.NEEDS_AUTH.value)
        if status == 429:
            now = self.clock()
            wait = decisions.retry_after(reply.headers.get("retry-after"), now)
            def limited() -> tuple[bool, float | None]:  # parallel calls count as one rate limit, not ten
                state, _, until = self.seats.override(spec.name)
                if state == BrainState.COOLING_DOWN:
                    return False, until
                return True, self.seats.mark_rate_limited(spec, resets_at=now + wait if wait else None,
                                                          detail="HTTP 429")

            first, until = await self.store.run(limited)
            if first:
                self.ports.trace("brain.seat", action="cooling_down", brain=spec.name, until=until, reason="HTTP 429")
            raise _NextBrain(f"{spec.name} hit a rate limit", state=BrainState.COOLING_DOWN.value,
                             retry_after=max((until or now) - now, 0.0))
        if status == 404 or status < 400 or status >= 500:  # a redirect lands here: it is never followed
            raise _NextBrain(f"{spec.name} answered HTTP {status}")
        why = decisions.error_text(reply.raw, secrets)
        raise decisions.DecisionRejected(f"{spec.name} refused the request (HTTP {status})" + (f": {why}" if why else ""))

    async def _decision_auth_failed(self, spec: BrainSpec, reason: str) -> None:
        def mark() -> bool:  # check and mark in one call on the worker: parallel calls tell the owner once
            was = self.seats.override(spec.name)[0]
            self.seats.mark_needs_auth(spec.name, reason)
            return was != BrainState.NEEDS_AUTH

        first = await self.store.run(mark)
        self._cred_cache.pop(spec.name, None)
        if first:
            await self._notify_reauth(spec, reason)
