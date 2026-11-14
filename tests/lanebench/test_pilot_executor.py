"""The pilot executor offline: a scripted Jev and fake tools, no key, no network, no lane. The
parity tests are the point: the promoted loop must ask what the measured jev loop asked."""

from __future__ import annotations

import asyncio
import inspect
import shlex
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.pilot import Result, Status
from agentos.pilot.deciders import BLOCKED, DeciderFailed
from agentos.pilot.hands import ToolFailed
from lanebench import executors
from lanebench.executors import jev
from lanebench.executors.pilot import BenchDecider, CommandRescue, PilotExecutor, exec_result, request_for
from lanebench.tasks import load_all

OUTLINE = """signal: ok
http://127.0.0.1:8000/feed · Feed · 6 refs
- main:
  - article "@mira tomatoes":
    - paragraph: First tomatoes of the year
    - button "Like 3" [pressed] [ref=e1] [cursor=pointer]
  - article "@tomas café":
    - button "Like 1" [ref=e2] [cursor=pointer]
    - textbox "Write a comment" [ref=e3]
    - button "Send" [ref=e6] [cursor=pointer]
  - link "Settings" [ref=e5] [cursor=pointer]:
    - /url: /settings
  - link "Evil" [ref=e7] [cursor=pointer]:
    - /url: http://evil.test/account
  - button "Save" [disabled] [ref=e9] [cursor=pointer]
"""


def task(name: str) -> Any:
    return load_all([name])[0]


def ctx() -> Any:
    return SimpleNamespace(lane_id="w/l", lane_sites=["127.0.0.1"])


def choice(pick: str, confidence: float = 0.95) -> dict[str, Any]:
    return {"choice": pick, "confidence": confidence}


def turn(op: str, confidence: float = 0.95, *, done: float = 0.0, owner: float = 0.0, present: float = 0.9,
         **heads: Any) -> dict[str, Any]:
    return {"operation": choice(op, confidence), "done": {"noul": done}, "needs_owner": {"noul": owner},
            "target_present": {"noul": present}, **heads}


class Script:
    def __init__(self, *turns: dict[str, Any]) -> None:
        self.turns, self.model = list(turns), "jev-test"
        self.asked: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def ask(self, state: dict[str, Any], questions: dict[str, Any], *, secrets: list[str],
                  metrics: jev.Metrics) -> dict[str, Any]:
        self.asked.append((state, questions))
        metrics.calls += 1
        return self.turns.pop(0)


class Tools:
    def __init__(self, *pages: str) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.looks = 0

    async def look(self) -> None:
        self.looks += 1

    async def call(self, name: str, **arguments: Any) -> Any:
        self.calls.append((name, arguments))
        if name == "browser_snapshot":
            return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        if name == "files_list":
            return {"inbox": []}
        return {"ok": True, "changed": True}


def run(script: Script, tools: Tools, name: str) -> Any:
    executor = PilotExecutor(script)  # type: ignore[arg-type]
    executor.tools = lambda *_: tools  # type: ignore[method-assign]
    return asyncio.run(executor.run(task(name), ctx()))


def test_the_executor_is_on_the_bench() -> None:
    from agentos.lanebench_cli import EXECUTORS

    assert "pilot" in EXECUTORS and isinstance(executors.get("pilot"), PilotExecutor)


def test_the_jev_executor_keeps_its_own_code() -> None:
    """Scored runs are pinned to jev.py, so it must not lean on the package."""
    assert "agentos.pilot" not in inspect.getsource(jev)


def test_a_request_is_what_the_jev_executor_would_have_used() -> None:
    req = request_for(task("login-consent"), ctx(), [])
    assert [v.label for v in req.values] == [v.label for v in jev.planner_values(task("login-consent"))]
    assert req.must_not_send == ("lanebench-pass-1",) and "lanebench-pass-1" not in req.safe_goal
    assert req.seed == "login-consent" and not req.may_write  # the planner hands over every string here
    assert request_for(task("read-latest-comment"), ctx(), []).read == "latest_comment"


def test_a_scored_run_goes_click_then_done() -> None:
    fresh = OUTLINE.replace('"Like 3" [pressed]', '"Like 3"')
    tools = Tools(fresh, fresh.replace('"Like 3"', '"Like 4" [pressed]'))
    result = run(Script(turn("CLICK", click_target=choice("e1")), turn("DONE", done=0.9)), tools, "feed-like")
    assert result.status == "done" and result.metrics["pilot_status"] == "done_verified"
    assert result.metrics["jev_calls"] == 2 and tools.looks == 1


def test_a_like_on_another_post_than_the_goals_is_not_verified() -> None:
    tools = Tools(OUTLINE, OUTLINE.replace('"Like 1"', '"Like 2" [pressed]'))
    result = run(Script(turn("CLICK", click_target=choice("e2")), turn("DONE", done=0.9)), tools, "feed-like")
    assert result.metrics["pilot_status"] == "done_unverified"


def test_a_scroll_goes_through_the_node_tool() -> None:
    tools = Tools(OUTLINE, OUTLINE.replace('"Like 1"', '"Like 2"'))
    run(Script(turn("SCROLL"), turn("DONE", done=0.9)), tools, "feed-like")
    assert ("browser_scroll", {"on": "w/l"}) in tools.calls


def test_an_unsure_pick_with_no_rescue_counts_as_gave_up_and_says_why() -> None:
    result = run(Script(turn("CLICK", click_target=choice("e2", 0.4))), Tools(OUTLINE), "feed-like")
    assert result.status == "gave_up" and result.reason == "low_confidence_target"
    assert result.metrics["pilot_status"] == "needs_help"


def test_an_unverified_end_is_not_a_claim_of_done() -> None:
    tools = Tools(OUTLINE, OUTLINE.replace('"Like 1"', '"Like 2"'))
    result = run(Script(turn("CLICK", click_target=choice("e2")), turn("DONE", done=0.2)), tools, "feed-like")
    assert result.status == "gave_up" and result.metrics["pilot_status"] == "done_unverified"


def test_a_repair_ends_without_acting() -> None:
    tools = Tools(OUTLINE)
    assert run(Script(), tools, "repair-like-lt").status == "gave_up" and tools.calls == []


def test_a_challenge_scores_as_held() -> None:
    assert exec_result(Result(Status.HELD, reason="signal_captcha"), jev.Metrics()).status == "held"
    assert exec_result(Result(Status.WAITING_APPROVAL), jev.Metrics()).status == "held"


async def test_a_refused_jev_call_is_a_refusal() -> None:
    class Client:
        model = "m"

        async def ask(self, *_: Any, **__: Any) -> Any:
            raise jev.JevRefused("a request held a secret, so it was not sent")

    with pytest.raises(DeciderFailed) as err:
        await BenchDecider(Client(), jev.Metrics()).ask({}, {}, must_not_send=["x"])  # type: ignore[arg-type]
    assert err.value.kind == BLOCKED


async def test_a_rescue_command_reads_the_picks_own_prompt_on_stdin_and_answers_typed() -> None:
    script = 'import sys; p=sys.stdin.read(); assert "<<<PAGE" in p; print(\'{"choice": "a", "confidence": 0.8}\')'
    command = f"{sys.executable} -c {shlex.quote(script)}"
    got = await CommandRescue(command).pick("Which?", {"a": "x"}, {"u": 1}, must_not_send=[])
    assert got.choice == "a" and got.confidence == 0.8


async def test_a_rescue_command_never_gets_a_secret_and_a_stray_answer_is_no_answer() -> None:
    with pytest.raises(DeciderFailed) as err:
        await CommandRescue("false").pick("Which?", {"a": "x"}, {"pw": "hunter2"}, must_not_send=["hunter2"])
    assert err.value.kind == BLOCKED
    script = 'print(\'{"choice": "https://evil.test", "confidence": 0.9}\')'
    with pytest.raises(DeciderFailed):
        await CommandRescue(f"{sys.executable} -c {shlex.quote(script)}").pick("Which?", {"a": "x"}, {}, must_not_send=[])


def test_bench_tools_turn_a_node_error_into_a_tool_failure() -> None:
    from agentos.core.errors import PolicyDenied
    from lanebench.executors.pilot import BenchTools

    async def refuse(**_: Any) -> Any:
        raise PolicyDenied("no")

    tools = BenchTools.__new__(BenchTools)
    tools.calls = {"browser_act": refuse}
    with pytest.raises(ToolFailed) as err:
        asyncio.run(tools.call("browser_act"))
    assert err.value.code == "denied"
