"""Jev for the lab: the real endpoint behind a per-question cache and a hard budget.

Questions are evaluated independently, so a cached answer stands for that question on that state
whatever else was asked with it: a change to one question re-asks only that question. The key comes
from TYPESAFE_API_KEY, is taken out of the environment at once and is never written anywhere. The
spend is the API's own token count at the published price, kept in data/spend.jsonl, and a call past
the budget raises instead of going out.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from lanebench.lab.harvest import DATA
from openajar.pilot.deciders import BLOCKED, UNAVAILABLE, DeciderFailed, Reply, holds_secret

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.14.0"
USD_PER_TOKEN = 0.042 / 1_000_000
BUDGET_USD = 2.85             # the brief's limit is 3: stop short of it
RETRY_STATUS = frozenset({429, 500, 502, 503, 529})
TRIES = 7


class OverBudget(RuntimeError):
    pass


def canon(value: Any) -> str:
    """Key order stays: a Choice's option order is part of the question, and can move its answer."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Ledger:
    """What the lab has spent, across every run."""

    def __init__(self, path: Path):
        self.path = path
        self.usd = sum(json.loads(line)["usd"] for line in path.read_text().splitlines()) if path.exists() else 0.0

    def add(self, *, tokens: int, questions: int, ms: int) -> None:
        usd = tokens * USD_PER_TOKEN
        self.usd += usd
        with self.path.open("a") as f:
            f.write(json.dumps({"t": round(time.time()), "tokens": tokens, "questions": questions, "ms": ms, "usd": usd}) + "\n")


class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.answers: dict[str, Any] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                key, answer = json.loads(line)
                self.answers[key] = answer

    def put(self, key: str, answer: Any) -> None:
        self.answers[key] = answer
        with self.path.open("a") as f:
            f.write(json.dumps([key, answer], ensure_ascii=False) + "\n")


class LabJev:
    """The API with its cache. `sealed` forbids spending: a report from answers already kept."""

    def __init__(self, *, root: Path = DATA, concurrency: int = 4, budget: float = BUDGET_USD, offline: bool = False):
        root.mkdir(parents=True, exist_ok=True)
        self.key = os.environ.pop("TYPESAFE_API_KEY", None)
        self.cache, self.ledger = Cache(root / "cache.jsonl"), Ledger(root / "spend.jsonl")
        self.gate = asyncio.Semaphore(concurrency)
        self.budget, self.offline = budget, offline
        self.client = httpx.AsyncClient(timeout=60, follow_redirects=False)
        self.calls = self.hits = self.tokens = 0
        self.latency: list[int] = []

    async def close(self) -> None:
        await self.client.aclose()

    @staticmethod
    def keyed(state: Any, name: str, question: Any) -> str:
        return hashlib.sha256(f"{MODEL}\0{canon(state)}\0{name}\0{canon(question)}".encode()).hexdigest()

    async def _post(self, state: Any, questions: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.offline:
            raise DeciderFailed(UNAVAILABLE, "offline: this question has no kept answer")
        if self.ledger.usd >= self.budget:
            raise OverBudget(f"the lab has spent ${self.ledger.usd:.3f}, its budget is ${self.budget:.2f}")
        if not self.key:
            raise DeciderFailed(UNAVAILABLE, "TYPESAFE_API_KEY is not set")
        body = json.dumps({"model": MODEL, "state": state, "questions": questions}, ensure_ascii=False).encode()
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        async with self.gate:
            for attempt in range(TRIES):
                t0 = time.monotonic()
                try:
                    response = await self.client.post(API_URL, content=body, headers=headers)
                except httpx.TransportError:
                    await asyncio.sleep(0.5 * 2 ** attempt)
                    continue
                if response.status_code in RETRY_STATUS:
                    await asyncio.sleep(float(response.headers.get("retry-after") or 0.5 * 2 ** attempt))
                    continue
                if response.is_error:
                    raise DeciderFailed(UNAVAILABLE, f"Jev answered HTTP {response.status_code}: {response.text[:300]}")
                data = response.json()
                ms = round((time.monotonic() - t0) * 1000)
                tokens = int((data.get("usage") or {}).get("input_tokens") or 0)
                self.ledger.add(tokens=tokens, questions=len(questions), ms=ms)
                self.calls += 1
                self.tokens += tokens
                self.latency.append(ms)
                return data["answers"], tokens
        raise DeciderFailed(UNAVAILABLE, "Jev stayed unavailable")

    async def ask(self, state: Any, questions: dict[str, Any], *, must_not_send: Sequence[str]) -> dict[str, Any]:
        if holds_secret(json.dumps({"state": state, "questions": questions}, ensure_ascii=False), must_not_send):
            raise DeciderFailed(BLOCKED, "a request held a secret, so it was not sent")
        keys = {name: self.keyed(state, name, q) for name, q in questions.items()}
        missing = {name: questions[name] for name, key in keys.items() if key not in self.cache.answers}
        self.hits += len(questions) - len(missing)
        if missing:
            answers, _ = await self._post(state, missing)
            for name in missing:
                self.cache.put(keys[name], answers[name])
        return {name: self.cache.answers[key] for name, key in keys.items()}


class LabDecider:
    """A pilot decider over LabJev, which remembers what each step asked and heard."""

    name = MODEL

    def __init__(self, jev: LabJev):
        self.jev = jev
        self.requests: list[dict[str, Any]] = []

    async def ask(self, state: Any, questions: dict[str, Any], *, must_not_send: Sequence[str]) -> Reply:
        answers = await self.jev.ask(state, questions, must_not_send=must_not_send)
        size = len(canon(state)) + len(canon(questions))
        self.requests.append({"questions": questions, "answers": answers, "chars": size})
        return Reply(answers, usd=0.0, input_tokens=size // 4, model=MODEL, latency_ms=200)
