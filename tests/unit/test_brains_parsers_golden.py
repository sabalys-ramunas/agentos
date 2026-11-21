"""Characterization of the stream parsers: every kind of line each `_on` handles, fed in order, and
the events, accounting and outcome that come out, pinned in tests/fixtures/brains/parser_golden.json.
Regenerate the file only when a change to the parsers is meant: OPENAJAR_REGEN_GOLDEN=1.
"""

import json
import os
from pathlib import Path

import pytest

from openajar.brains.parsers import ClaudeStreamParser, CodexJsonParser

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "brains" / "parser_golden.json"

CLAUDE = [
    {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-x", "claude_code_version": "2.1"},
    {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 5, "error_status": 503, "error": "overloaded"},
    {"type": "system", "subtype": "api_retry", "attempt": 2, "max_retries": 5, "error_status": 401, "error": ""},
    {"type": "system", "subtype": "api_retry", "attempt": 3, "max_retries": 5, "error_status": None, "error": "billing_error"},
    {"type": "system", "subtype": "api_retry", "attempt": 4, "max_retries": 5, "error_status": 429, "error": ""},
    {"type": "system", "subtype": "api_retry", "attempt": 5, "max_retries": 5, "error_status": 400, "error": "bad"},
    {"type": "system", "subtype": "hook"},
    {"type": "system"},
    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "utilization": 0.4, "rateLimitType": "five_hour"}},
    {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1900000000000, "rateLimitType": "five_hour"}},
    {"type": "rate_limit_event", "rate_limit_info": {"status": "blocked", "resetsAt": 1900000000}},
    {"type": "rate_limit_event"},
    {"type": "assistant", "message": {"id": "m1", "model": "claude-y", "usage": {"input_tokens": 10, "output_tokens": 3,
     "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7},
     "content": [{"type": "text", "text": "hello"}, {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}},
                 {"type": "thinking", "thinking": "hmm"}, {"type": "image"}]}},
    {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 12, "output_tokens": 2}, "content": []}},
    {"type": "assistant", "uuid": "u2", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error"}]},
     "error": "rate_limit"},
    {"type": "assistant", "message": {"id": "m3", "content": [{"type": "text", "text": "Invalid API key"}]},
     "is_api_error_message": True, "api_error_status": 401},
    {"type": "assistant", "message": {"id": "m4", "content": []}, "error": "weird", "api_error_status": 429},
    {"type": "assistant", "message": {"id": "m5", "content": [{"type": "text", "text": "overloaded_error 529"}]},
     "error": "unknown"},
    {"type": "assistant", "message": {"content": []}},
    {"type": "assistant"},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "file1\nfile2"}, {"type": "text"}]}},
    {"type": "user", "message": {"content": "plain"}},
    {"type": "user", "message": {"content": [{"type": "text", "text": "x"}]}},
    {"type": "user"},
    {"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 9,
     "total_cost_usd": 0.12, "usage": {"input_tokens": 5, "output_tokens": 50, "cache_read_input_tokens": 1,
                                        "cache_creation_input_tokens": 70}},
    {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": None},
    {"type": "result", "subtype": "error", "terminal_reason": "budget", "is_error": True, "result": "spent",
     "api_error_status": 429},
    {"type": "result", "is_error": True, "result": "nope", "api_error_status": 403},
    {"type": "result", "is_error": True, "result": "", "api_error_status": 500},
    {"type": "result", "is_error": True, "api_error_status": 404},
    {"type": "something_else", "session_id": "s2"},
    {"no_type": True},
]

CODEX = [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"type": "command_execution", "status": "in_progress"}},
    {"type": "item.updated", "item": {"type": "reasoning"}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "aggregated_output": "a\nb"}},
    {"type": "item.completed", "item": {"type": "mcp_tool_call", "tool": "t"}},
    {"type": "item.completed", "item": {"type": "web_search", "query": "q"}},
    {"type": "item.completed", "item": {"type": "file_change"}},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "think"}},
    {"type": "item.completed", "item": {"type": "error", "message": "429 Too Many Requests"}},
    {"type": "item.completed", "item": {"type": "todo_list"}},
    {"type": "item.completed"},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 4, "x": "y"}},
    {"type": "error", "message": "Reconnecting... 1/5"},
    {"type": "error", "message": "unauthorized 401"},
    {"type": "turn.failed", "error": {"message": "stream error 500"}},
    {"type": "turn.completed"},
    {"type": "turn.failed"},
    {"type": "other"},
    {},
]


def _state(p) -> dict:
    snap = p.snapshot()
    snap.update(signal_detail=p.signal_detail, last_error=p.last_error, final_seen=p.final_seen,
                result_is_error=p.result_is_error, limit_hit=p.limit_hit, result_text=p.result_text)
    return snap


def _run(parser_cls, stream):
    """Each line's events, the parser's state after it, and the outcomes at the end."""
    p = parser_cls()
    lines = [json.dumps(obj) for obj in stream]
    lines += ["not json", "[1]", "   ", json.dumps({"type": "user", "message": {"content": "x" * 70000}})]
    steps = [{"events": p.feed(line), "state": _state(p)} for line in lines]
    return {"steps": steps, "outcomes": [p.outcome(c) for c in (0, 1, None)]}


def _actual() -> dict:
    return {"claude": _run(ClaudeStreamParser, CLAUDE), "codex": _run(CodexJsonParser, CODEX)}


@pytest.mark.parametrize("runner", ["claude", "codex"])
def test_the_parsers_output_is_what_it_was(runner):
    actual = json.loads(json.dumps(_actual()))
    if os.environ.get("OPENAJAR_REGEN_GOLDEN"):
        GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
    assert actual[runner] == json.loads(GOLDEN.read_text())[runner]
