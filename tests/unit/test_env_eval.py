"""The environments eval's parts that need no Docker (tests/env_eval, docs/EVALS.md): reading a model's
transcript, the wrong-turn rules, the timings, the recording proxy, the history's compare with its
thresholds and noise floors, and the budget guard for paid runs."""

from __future__ import annotations

import asyncio
import json
import socket

import pytest
from env_eval import budget, history
from env_eval.metrics import (
    claude_metrics,
    codex_metrics,
    final_json,
    open_times,
    shell_file_io,
    sleeps,
    time_to_ready,
    trace_metrics,
    wrong_turns,
)
from env_eval.proxy import McpProxy, Recorder, error_code, messages, tool_tokens


def row(tool, args=None, *, ok=True, text="", code=None, started=0.0, ended=1.0, session="s1", images=0):
    return {"tool": tool, "args": args or {}, "ok": ok, "text": text, "code": code, "started": started,
            "ended": ended, "session": session, "images": images}


# ---- a model's transcript ----

# What `claude -p --output-format stream-json --verbose` writes, cut to the events the eval reads.
STREAM = "\n".join(json.dumps(e) for e in [
    {"type": "system", "subtype": "init", "model": "claude-haiku-4-5", "tools": ["mcp__agentos__environment_open"]},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1",
                                                     "name": "mcp__agentos__environment_open",
                                                     "input": {"title": "Fix it"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                "content": [{"type": "text", "text": "{\"on\": \"x\"}"}]}]}},
    {"type": "result", "subtype": "success", "num_turns": 7, "total_cost_usd": 0.0412, "duration_ms": 61000,
     "duration_api_ms": 20000, "is_error": False,
     "result": 'All done.\n{"status": "done", "summary": "pushed fix and opened the pull request"}',
     "usage": {"input_tokens": 31, "output_tokens": 1450, "cache_read_input_tokens": 180000,
               "cache_creation_input_tokens": 22000}},
])


def test_a_claude_transcript_gives_turns_tokens_cost_and_the_answer():
    m = claude_metrics("not json\n" + STREAM)
    assert m["turns"] == 7 and m["cost_usd"] == 0.0412 and m["model"] == "claude-haiku-4-5"
    assert (m["input_tokens"], m["output_tokens"], m["cache_read_tokens"], m["cache_write_tokens"]) == \
        (31, 1450, 180000, 22000)
    assert final_json(m["answer_text"]) == {"status": "done", "summary": "pushed fix and opened the pull request"}


def test_a_transcript_cut_short_still_names_the_model():
    cut = STREAM.splitlines()[0]
    assert claude_metrics(cut) == {"answer_text": "", "model": "claude-haiku-4-5"}


def test_a_codex_transcript_sums_its_turns():
    text = "\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "x"},
        {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 50}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"status": "done"}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 500, "cached_input_tokens": 0, "output_tokens": 20}},
    ])
    m = codex_metrics(text)
    assert m == {"answer_text": '{"status": "done"}', "turns": 2, "input_tokens": 1500, "cache_read_tokens": 800,
                 "output_tokens": 70}


# ---- wrong turns ----

@pytest.mark.parametrize(("cmd", "how"), [
    ("cat src/price.js", "read"),
    ("cd /work/x && head -40 README.md", "read"),
    ("sed -n '1,20p' src/a.js", "read"),
    ("sed -i 's/a/b/' src/a.js", "write"),
    ("cat > src/a.js <<'EOF'\nx\nEOF", "write"),
    ("echo hi > notes.txt", "write"),
    ("npm test 2>&1 | tail -5", None),          # a reader at the end of a pipe reads no file
    ("git log --oneline | head -3", None),
    ("echo ok > /dev/null", None),
    ("npm run build", None),
])
def test_file_reads_and_writes_by_shell_are_found(cmd, how):
    assert shell_file_io(cmd) == how


def test_a_sleep_is_long_in_a_loop():
    assert sleeps("npm run dev & sleep 1") == 1
    assert sleeps("sleep 3; curl localhost:3000") == 3
    assert sleeps("until curl -s localhost:3000; do sleep 1; done") == 10


def test_the_wrong_turns_a_careful_agent_does_not_take():
    rows = [
        row("environment_open", {"title": "x"}, text=json.dumps({"opened": "new", "on": "h", "state": "ready"})),
        row("lane_exec", {"cmd": "ls"}, ok=False, code="which_environment"),
        row("lane_exec", {"on": "h", "cmd": "cat package.json"}, text=json.dumps({"exit_code": 0})),
        row("lane_exec", {"on": "h", "cmd": "npm test"}, text=json.dumps({"exit_code": 1})),
        row("lane_exec", {"on": "h", "cmd": "npm test"}, text=json.dumps({"exit_code": 1})),
        row("lane_exec", {"on": "h", "cmd": "(npm run dev &); sleep 5"}, text=json.dumps({"exit_code": 0})),
        *[row("lane_job_read", {"on": "h", "job": "j1"}) for _ in range(3)],
        row("tools_call", {"name": "environment_open", "arguments": {"title": "y"}},
            text=json.dumps({"opened": "new", "on": "h2", "state": "ready"})),
    ]
    turns = wrong_turns(rows)
    assert len(turns) == 6, turns
    assert turns[0] == "lane_exec without the on its environment_open gave (refused: which_environment)"
    assert "read a file through lane_exec" in turns[1]
    assert turns[2] == "repeated the failing lane_exec unchanged"
    assert "waited with sleep" in turns[3]
    assert turns[4].startswith("polled job j1 3 times")
    assert turns[5] == "opened another new environment (2 where the task needs 1)"
    assert wrong_turns(rows[-1:], new_environments=2) == []


def test_a_lane_name_as_on_is_a_wrong_turn_even_when_it_worked():
    """Haiku passed lanes.browser as on (2026-11-18). Refused or not (a session holder reaches its own
    machine by it), it is not the on the skill teaches; a handle is never taken for one."""
    rows = [row("browser_navigate", {"on": "ea4rfkkx9sw-browser", "url": "http://localhost:3000"}, ok=False,
                code="which_environment"),
            row("lane_exec", {"on": "env/ea4rfkkx9sw", "cmd": "ls"}),
            row("lane_exec", {"on": "ea4rfkkx9sw.fexswp5nylnkryff", "cmd": "ls"}),
            row("browser_navigate", {"on": "personal/reddit", "url": "https://example.com"})]
    turns = wrong_turns(rows)
    assert turns == [("browser_navigate with a lane's name as on ('ea4rfkkx9sw-browser'), not the on its "
                     "environment_open gave (refused: which_environment)"),
                     "lane_exec with a lane's name as on ('env/ea4rfkkx9sw'), not the on its environment_open gave"]


def test_the_first_navigate_is_timed():
    rows = [row("browser_navigate", {"url": "a"}, started=3, ended=13.5), row("browser_navigate", started=14, ended=15)]
    assert trace_metrics(rows)["first_navigate_s"] == 10.5
    assert "first_navigate_s" not in trace_metrics([row("lane_exec", started=0, ended=1)])


def test_a_retry_after_a_fix_or_in_another_chat_is_no_repeat():
    fail = row("lane_exec", {"cmd": "npm test"}, text=json.dumps({"exit_code": 1}))
    fixed = row("lane_file_edit", {"path": "a.js", "old": "x", "new": "y"})
    again = row("lane_exec", {"cmd": "npm test"}, text=json.dumps({"exit_code": 0}))
    assert wrong_turns([fail, fixed, again]) == []
    assert wrong_turns([fail, {**fail, "session": "s2"}]) == []
    assert wrong_turns([row("lane_job_read", {"job": "j", "until_port": 3000}) for _ in range(5)]) == []


# ---- timings and sums ----

def test_open_times_wait_for_ready_and_say_what_the_open_was():
    rows = [
        row("environment_open", text=json.dumps({"on": "h", "opened": "new", "state": "starting"}), started=10,
            ended=12),
        row("environment", {"on": "h"}, text=json.dumps({"state": "starting"}), started=12, ended=20),
        row("environment", {"on": "h"}, text=json.dumps({"state": "ready"}), started=20, ended=25.5),
        row("environment_open", text=json.dumps({"on": "h2", "opened": "new", "state": "ready",
                                                 "app": {"listening": True},
                                                 "prepared": {"used": True, "warm": "from the pool"}}),
            started=40, ended=43),
    ]
    assert open_times(rows) == [
        {"opened": "new", "ready_s": 15.5, "app_listening": False, "prepared": False, "warm": False,
         "resumed_from": None},
        {"opened": "new", "ready_s": 3, "app_listening": True, "prepared": True, "warm": True, "resumed_from": None}]
    assert time_to_ready(rows, t0=5) == 20.5


def test_tokens_read_are_the_result_texts_over_four():
    rows = [row("lane_exec", text="x" * 400, started=0, ended=2), row("browser_screenshot", text="y" * 40, images=1,
                                                                       started=2, ended=2.5),
            row("lane_exec", ok=False, code="busy", text="z" * 4)]
    m = trace_metrics(rows)
    assert m["tokens_read"] == 111 and m["images"] == 1 and m["calls"] == 3
    assert m["calls_by_tool"] == {"lane_exec": 2, "browser_screenshot": 1}
    assert m["errors_by_code"] == {"busy": 1} and m["tool_errors"] == 1
    assert m["seconds_by_tool"] == {"lane_exec": 3.0, "browser_screenshot": 0.5}


# ---- the proxy ----

def test_sse_and_json_bodies_give_their_messages():
    sse = (b'event: message\ndata: {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}\n\n'
           b'data: {"jsonrpc": "2.0", "method": "notifications/progress"}\n\n')
    assert [m.get("id") for m in messages(sse, "text/event-stream")] == [1, None]
    batch = json.dumps([{"jsonrpc": "2.0", "id": 2, "result": {}}, {"jsonrpc": "2.0", "id": 3, "result": {}}])
    assert [m["id"] for m in messages(batch.encode(), "application/json")] == [2, 3]
    assert messages(b"not json", "application/json") == []


def test_the_surface_estimate_is_the_toolset_budget_tests():
    tools = [{"name": "a", "description": "x" * 100, "inputSchema": {"type": "object"}}]
    assert tool_tokens(tools) == len(json.dumps(tools[0])) // 4
    assert error_code("which_environment: pass the on") == "which_environment"
    assert error_code("the node said something long") == "error"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_the_proxy_forwards_and_records_what_the_agent_read():
    """A stand-in MCP server answers tools/list in JSON and tools/call as an SSE stream; the proxy passes
    both through unchanged and records the call, its result's text and the listing's size."""
    import httpx
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    tools = [{"name": "environment_open", "description": "Your own computer.", "inputSchema": {"type": "object"}}]

    async def mcp(request: Request) -> Response:
        msg = json.loads(await request.body())
        if msg["method"] == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}},
                                headers={"mcp-session-id": "abc"})
        body = {"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": "environment_full: wait"}], "isError": True}}
        return Response(f"event: message\ndata: {json.dumps(body)}\n\n", media_type="text/event-stream")

    port, proxy_port = _free_port(), _free_port()
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=[Route("/mcp", mcp, methods=["POST"])]), host="127.0.0.1",
                                           port=port, log_level="warning", lifespan="off"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        async with McpProxy(f"http://127.0.0.1:{port}", proxy_port, Recorder()) as proxy, httpx.AsyncClient() as c:
            listed = await c.post(proxy.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert listed.json()["result"]["tools"] == tools and listed.headers["mcp-session-id"] == "abc"
            called = await c.post(proxy.url, headers={"mcp-session-id": "abc"}, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "environment_open", "arguments": {"title": "x"}}})
            assert "environment_full: wait" in called.text
            rec = proxy.recorder
            assert [(c.tool, c.args, c.ok, c.code, c.text, c.session) for c in rec.calls] == [
                ("environment_open", {"title": "x"}, False, "environment_full", "environment_full: wait", "abc")]
            assert rec.listings[0].tools == 1 and rec.listings[0].tokens == tool_tokens(tools)
    finally:
        server.should_exit = True
        await serving


# ---- the history and its compare ----

def result(task, *, finished=True, seconds=20.0, tokens=2000, calls=8, wrong=0, agent="perfect", cost=None):
    r = {"task": task, "agent": agent, "finished": finished, "seconds": seconds, "ready_s": 5.0, "calls": calls,
         "tokens_read": tokens, "tool_errors": 0, "wrong_turns": ["x"] * wrong, "surface_tokens": 9400,
         "opens": [{"ready_s": 5.0}]}
    if cost is not None:
        r["model_metrics"] = {"cost_usd": cost, "turns": 9}
    return r


def rec(results, commit="aaa", at="2026-11-18T10:00:00+00:00"):
    r = history.record(results, agent="perfect", model="haiku", commit=commit, dirty=False, host="h", at=at, repeat=1)
    r["task_set"] = sorted({x["task"] for x in results})
    return r


def test_a_summary_takes_medians_over_repeats_and_the_finished_rate():
    s = history.summarize([result("t01", seconds=10), result("t01", seconds=30, finished=False),
                           result("t01", seconds=20), result("t02", tokens=1000)])
    assert s["tasks"]["t01"]["finished"] == pytest.approx(0.667, abs=0.001)
    assert s["tasks"]["t01"]["seconds_s"] == 20 and s["tasks"]["t01"]["runs"] == 3
    assert s["totals"]["tokens_read"] == 3000 and s["totals"]["finished"] == pytest.approx(1.667, abs=0.001)
    assert s["totals"]["surface_tokens"] == 9400


@pytest.mark.parametrize(("metric", "old", "new", "word", "bad"), [
    ("t01.seconds_s", 20.0, 22.0, "same", False),         # 10% and 2 s: under both the threshold and the floor
    ("t01.seconds_s", 2.0, 4.5, "same", False),           # 125% but only 2.5 s: noise
    ("t01.seconds_s", 20.0, 30.0, "worse", True),         # 50% and 10 s
    ("t01.seconds_s", 30.0, 20.0, "better", False),
    ("t01.tokens_read", 2000, 2150, "same", False),       # 150 tokens: under the floor
    ("t01.tokens_read", 2000, 2500, "worse", True),
    ("t01.tokens_read", 4000, 4350, "worse", False),      # over the floor, under 10%
    ("total.surface_tokens", 9400, 9500, "worse", False),  # 100 tokens is 1%
    ("total.surface_tokens", 9400, 9800, "worse", True),
    ("t01.wrong_turns", 0, 1, "worse", True),             # any new wrong turn
    ("t01.finished", 1.0, 0.667, "worse", True),
    ("t01.finished", 0.667, 1.0, "better", False),
    ("t01.cost_usd", 0.04, 0.05, "same", False),          # a cent is noise
])
def test_a_regression_is_past_its_threshold_and_its_noise_floor(metric, old, new, word, bad):
    assert history.verdict(metric, old, new) == (word, bad)


def test_compare_picks_the_last_run_of_the_same_kind_or_a_named_commit(tmp_path):
    path = tmp_path / "evals.jsonl"
    first = rec([result("t01")], commit="aaa", at="2026-11-18T10:00:00+00:00")
    second = rec([result("t01", tokens=1500)], commit="bbb", at="2026-11-18T11:00:00+00:00")
    third = rec([result("t01", tokens=3000, wrong=1)], commit="ccc", at="2026-11-18T12:00:00+00:00")
    other = {**rec([result("t01")]), "model": "sonnet", "at": "2026-11-18T11:30:00+00:00"}
    for r in (first, second, other, third):
        history.append(path, r)
    hist = history.load(path)
    assert history.pick(hist, hist[-1])["commit"] == "bbb"
    assert history.pick(hist, hist[-1], against="aaa")["commit"] == "aaa"
    rows = {r["metric"]: r for r in history.compare(history.pick(hist, hist[-1]), hist[-1])}
    assert rows["t01.tokens_read"]["regressed"] and rows["t01.wrong_turns"]["regressed"]
    assert rows["t01.tokens_read"]["change"] == 1.0
    assert not rows["t01.seconds_s"]["regressed"]
    assert any("REGRESSION" in line for line in history.describe(list(rows.values())))


def test_the_run_compares_only_the_same_task_set(tmp_path):
    from env_eval.run import compare_lines

    one = rec([result("t01")], commit="aaa")
    two = rec([result("t01"), result("t02")], commit="bbb", at="2026-11-18T11:00:00+00:00")
    lines, regressed = compare_lines([two], [one, two], None)
    assert "the first run of this kind" in lines[0] and not regressed


# ---- the budget guard ----

def test_scripts_are_free_and_a_paid_run_is_estimated_high():
    assert budget.estimate("perfect", "haiku", 50) == 0.0
    each = budget.session_usd("haiku")
    assert 0.05 < each < 0.5                      # priced as Haiku 4.5, the dearer one the alias may mean
    assert budget.estimate("claude", "claude-sonnet-5-5", 2) == round(2 * budget.session_usd("sonnet"), 2)


def test_the_guard_refuses_past_max_usd_and_an_unknown_price():
    est, cap = budget.guard("claude", "haiku", 6, 2.0)
    assert est <= 2.0 and cap == round(2.0 / 6, 2)
    with pytest.raises(budget.OverBudget, match="above --max-usd"):
        budget.guard("claude", "opus", 40, 1.0)
    with pytest.raises(budget.OverBudget, match="no price known"):
        budget.guard("codex", "gpt-something", 1, 5.0)
    assert budget.guard("careless", "anything", 99, 0.0) == (0.0, 0.0)


async def test_the_github_stand_in_keeps_the_proofs_agentos_sends_it(tmp_path):
    """agentos.environments.prproofs.send against the stand-in: the files land on agentos-proofs, a second
    send adds only what is new, and the stand-in lists them for a checker."""
    import httpx

    from agentos.environments import prproofs
    from env_eval.ghstub import GitHubStub

    files = {"p1.mp4": b"mp4" * 100, "p1.gif": b"gif" * 50}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)

    class Mgr:
        def proof_path(self, env_id, proof_id, ext):
            return tmp_path / f"{proof_id}.{ext}"

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with GitHubStub(port, repos={"evals/shop": tmp_path}) as gh:
        async with httpx.AsyncClient(base_url=gh.url) as http:
            auth = {"Authorization": "Bearer t"}
            sent, skipped = await prproofs.send(Mgr(), http, "evals/shop", auth, "t", "e1",
                                                [{"id": "p1", "kind": "video"}])
            assert sent == {"p1": {"mp4": 300, "gif": 150}} and skipped == []
            assert gh.files("evals/shop") == {"e1/p1.mp4": files["p1.mp4"], "e1/p1.gif": files["p1.gif"]}
            again, _ = await prproofs.send(Mgr(), http, "evals/shop", auth, "t", "e1", [{"id": "p1", "kind": "video"}])
            assert again == sent and len(gh.commits) == 1  # nothing new: no second commit
