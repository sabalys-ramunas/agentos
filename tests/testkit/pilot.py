"""Stand-ins for the pilot's tests: a scripted decider and scripted tools. No model, no lane."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from openajar.pilot.deciders import Pick, Reply, Text
from openajar.pilot.hands import ToolFailed


def pick(key: str, confidence: float = 0.9) -> dict[str, Any]:
    return {"type": "choice", "choice": key, "confidence": confidence}


def step(op: str, confidence: float = 0.9, *, done: float = 0.1, owner: float = 0.0, present: float = 0.9,
         **heads: Any) -> dict[str, Any]:
    """One step's answers. `heads`: a target or value by question name, as a key or a whole answer."""
    out: dict[str, Any] = {"operation": pick(op, confidence), "done": {"type": "noul", "noul": done},
                           "needs_owner": {"type": "noul", "noul": owner},
                           "target_present": {"type": "noul", "noul": present}}
    for name, value in heads.items():
        out[name] = pick(value) if isinstance(value, str) else value
    return out


class ScriptedDecider:
    """Answers from a list, one reply per call. A callable entry sees the questions and answers
    them; the last entry repeats. It keeps every request, so a test can read what a model was sent."""

    def __init__(self, *script: Any, name: str = "scripted", usd: float = 0.0001):
        self.script, self.name, self.usd = list(script), name, usd
        self.requests: list[dict[str, Any]] = []

    async def ask(self, state: Any, questions: dict[str, Any], *, must_not_send: Sequence[str]) -> Reply:
        self.requests.append({"state": state, "questions": questions, "must_not_send": list(must_not_send)})
        entry = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(entry, BaseException):
            raise entry
        answers = entry(questions) if callable(entry) else entry
        return Reply(dict(answers), usd=self.usd, input_tokens=100, output_tokens=1, latency_ms=200, model=self.name)


class ScriptedRescue:
    """Answers one question at a time, from a list; the last entry repeats. Each entry is a key, a
    (key, confidence), None for "no option is right", or an exception to raise."""

    def __init__(self, *script: Any, name: str = "haiku", usd: float = 0.002):
        self.script, self.name, self.usd = list(script), name, usd
        self.asked: list[dict[str, Any]] = []

    async def pick(self, question: str, options: dict[str, str], state: Any, *, must_not_send: Sequence[str],
                   max_usd: float | None = None) -> Pick:
        self.asked.append({"question": question, "options": options, "state": state,
                           "must_not_send": list(must_not_send)})
        entry = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(entry, BaseException):
            raise entry
        choice, confidence = entry if isinstance(entry, tuple) else (entry, 0.8)
        return Pick(choice, confidence, usd=self.usd, tokens=300, latency_ms=4000, model=self.name)


@dataclass
class ScriptedWriter:
    text: str = "Written words"
    name: str = "scripted-writer"
    asked: list[dict[str, Any]] = field(default_factory=list)

    async def write(self, *, goal: str, field: str, lines: Sequence[str], must_not_send: Sequence[str]) -> Text:
        self.asked.append({"goal": goal, "field": field, "lines": list(lines), "must_not_send": list(must_not_send)})
        return Text(self.text, usd=0.001, tokens=50, latency_ms=800, model=self.name)


class FakeTools:
    """A lane as a list of outlines: each snapshot shows the next one, and the last repeats. A tool
    answers from `results`: a value, a ToolFailed to raise, or a list to take one from per call."""

    def __init__(self, *outlines: str, results: dict[str, Any] | None = None,
                 on_call: Callable[[str, dict[str, Any]], None] | None = None):
        self.outlines, self.results, self.on_call = list(outlines), dict(results or {}), on_call
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.looks = 0

    def named(self, name: str) -> list[dict[str, Any]]:
        return [args for tool, args in self.calls if tool == name]

    @property
    def acts(self) -> list[str]:
        return [tool for tool, _ in self.calls if tool not in ("browser_snapshot", "browser_wait", "browser_files")]

    async def look(self) -> None:
        self.looks += 1

    async def call(self, name: str, **arguments: Any) -> Any:
        self.calls.append((name, arguments))
        if self.on_call is not None:
            self.on_call(name, arguments)
        if name == "browser_snapshot":
            return self.outlines.pop(0) if len(self.outlines) > 1 else self.outlines[0]
        result = self.results.get(name, {"ok": True})
        if isinstance(result, list):
            result = result.pop(0) if len(result) > 1 else result[0]
        if isinstance(result, ToolFailed):
            raise result
        return result
