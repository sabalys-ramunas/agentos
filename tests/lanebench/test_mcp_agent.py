"""The mcp-agent executor off the node: the tools it serves, the stand-in grant's `secret`, the
repair format in the prompt, stream-json and plain output, transcripts and metrics, and the
fixture's classification of a login POST."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lanebench import gates, harness, report
from lanebench.executors import RunContext, ToolTrace
from lanebench.executors.mcp_agent import (
    PATCH_EXAMPLE,
    PATCH_KEYS,
    BenchContext,
    McpAgentExecutor,
    agent_specs,
    final_text,
    grants_secret,
    locator_schema,
    parse_result,
    prompt_for,
    trace_metrics,
    transcript_metrics,
)
from lanebench.sites.state import PASSWORD
from lanebench.tasks import load_all

TASKS = {t.id: t for t in load_all()}

STREAM = "\n".join(json.dumps(e) for e in [
    {"type": "system", "subtype": "init", "tools": ["mcp__lanebench__browser_snapshot"]},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": '{"status": "failed"}'}]}},
    {"type": "result", "subtype": "success", "num_turns": 7, "total_cost_usd": 0.0123, "duration_ms": 4200,
     "is_error": False, "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 9000},
     "result": 'Liked it.\n{"status": "done", "answer": {"latest_comment": "hi"}}'},
])


def bare_runtime(tmp_path: Path):
    from openajar.core.config import NodeConfig
    from openajar.core.runtime import Runtime

    return Runtime(config=NodeConfig(home=tmp_path))


def test_the_agent_gets_the_browser_lane_tool_set(tmp_path):
    """compose-publish needs files and uploads, a login task the vault: what a node serves."""
    names = {s.name for s in agent_specs(bare_runtime(tmp_path))}
    assert {"browser_snapshot", "browser_act", "browser_upload", "browser_file_put", "browser_files", "login", "vault_fill",
            "vault_list", "browser_control", "howto"} <= names
    assert not names & {"browser_freeze", "workflow_run", "mail_send", "brain_run"}


async def test_secret_is_granted_on_login_tasks_only():
    from openajar.core.errors import PolicyDenied
    from openajar.core.models import Effect

    assert grants_secret(TASKS["login-save-login"]) and not grants_secret(TASKS["feed-like"])
    login = BenchContext(runtime=None, tool="login", lane="lanebench/login-lt", ceiling="draft", sites=["127.0.0.1"],
                         secret=True)
    await login.authorize(Effect.SECRET, lane_id="lanebench/login-lt")
    with pytest.raises(PolicyDenied):  # still this lane only
        await login.authorize(Effect.SECRET, lane_id="lanebench/feed-like")
    with pytest.raises(PolicyDenied):  # and a secret grant raises no ceiling
        await login.authorize(Effect.COMMIT)
    with pytest.raises(PolicyDenied):
        await BenchContext(runtime=None, tool="login", lane="lanebench/feed-like", ceiling="commit",
                           sites=["127.0.0.1"]).authorize(Effect.SECRET)


def test_no_goal_hands_over_the_password():
    for task in TASKS.values():
        assert PASSWORD not in task.goal, task.id
    for task in TASKS.values():
        if task.mode == "login_assist":
            assert "vault" in task.goal, task.id


def test_the_fixture_names_no_login_post_and_the_interstitials_as_writes():
    from openajar.node.egress import classify

    rules = harness.bench_effect_rules("127.0.0.1")
    base = "http://127.0.0.1:6528"
    assert classify(f"{base}/api/login", "POST", rules=rules) is None  # as on a real site: the vault's window
    assert classify(f"{base}/api/publish", "POST", rules=rules).effect == "commit"
    for path in ("cookies", "consent", "save-login", "device-approve", "like"):
        assert classify(f"{base}/api/{path}", "POST", rules=rules).effect == "write", path


def test_the_repair_format_comes_from_the_locator_model():
    from openajar.core.locator import Locator

    text = locator_schema()
    for key in PATCH_KEYS:
        assert f"- {key}: " in text
        assert key in Locator.model_fields
    assert '"exact", "contains", "regex"' in text and "css" not in text
    assert gates.patch_problems(PATCH_EXAMPLE) == []
    prompt = prompt_for(TASKS["repair-like-lt"], RunContext(task=TASKS["repair-like-lt"], lane_id="lanebench/x",
                                                            base_url="http://127.0.0.1:1", runtime=None,
                                                            trace=ToolTrace(), lane_sites=["127.0.0.1"]))
    assert text in prompt and json.dumps(PATCH_EXAMPLE, ensure_ascii=False) in prompt


@pytest.mark.parametrize("bad", [{"role": "button", "name": ["Like", "Patinka"]},
                                 {"role": "button", "name": "Like", "name_match": "exact"},
                                 {"role": "button", "name": "Like", "within": {"text": "@mira"}}])
def test_the_patches_models_wrote_are_still_refused(bad):
    assert any(p.startswith("not a locator") for p in gates.patch_problems(bad))


def test_stream_json_and_plain_text_results():
    assert final_text(STREAM).startswith("Liked it.")
    got = parse_result(STREAM, 0)
    assert got.status == "done" and got.answer == {"latest_comment": "hi"}
    assert parse_result('thinking\n{"status": "held", "reason": "device"}\n', 0).status == "held"
    assert transcript_metrics(STREAM) == {"turns": 7, "cost_usd": 0.0123, "agent_ms": 4200, "input_tokens": 1200,
                                          "output_tokens": 300, "cache_read_tokens": 9000, "is_error": False}
    assert transcript_metrics("plain words") == {}


def test_trace_metrics_count_calls_and_errors_by_code():
    trace = ToolTrace()
    trace.add("browser_snapshot", {"on": "a/b"}, ok=True, error=None, ms=1)
    trace.add("browser_act", {"on": "a/b"}, ok=False, error="InvalidRequest: no ref", ms=1, code="invalid")
    trace.add("browser_act", {"on": "a/b"}, ok=False, error="PolicyDenied: above", ms=1)
    m = trace_metrics(trace.rows)
    assert m["calls"] == 3 and m["tool_errors"] == 2
    assert m["errors_by_code"] == {"invalid": 1, "PolicyDenied": 1}
    rep = report.build([{"id": "t", "passed": True, "success": True, "gates": {},
                         "result": {"metrics": {**m, "turns": 4, "cost_usd": 0.5}}}], executor="mcp-agent", seed=1,
                       started=0)
    assert rep["summary"]["agent"]["calls_total"] == 3 and rep["summary"]["agent"]["cost_usd_total"] == 0.5
    assert "Agent: 3 calls, 2 tool errors, median 4 turns, $0.50." in report.summary(rep)


async def test_the_executor_keeps_a_transcript_and_reads_stream_json(tmp_path, monkeypatch):
    """The agent command's output goes to <task>.log in the transcript folder, its stderr next to it."""
    script = tmp_path / "agent.py"
    script.write_text("import json, sys\n"
                      f"print({STREAM!r})\n"
                      "print('a warning', file=sys.stderr)\n")
    task = TASKS["feed-like"]
    trace = ToolTrace()
    trace.add("browser_snapshot", {"on": "lanebench/feed-like"}, ok=True, error=None, ms=1)
    ctx = RunContext(task=task, lane_id="lanebench/feed-like", base_url="http://127.0.0.1:1", runtime=None,
                     trace=trace, lane_sites=["127.0.0.1"], work_dir=tmp_path, transcripts=tmp_path / "t")
    ex = McpAgentExecutor(agent_cmd=f"{sys.executable} {script} {{config}} {{transcript}}")
    result = await ex._agent(task, ctx, "http://127.0.0.1:1/mcp", "tok", tmp_path)
    assert result.status == "done" and result.metrics["turns"] == 7
    assert (tmp_path / "t" / "feed-like.log").read_text().strip() == STREAM
    assert (tmp_path / "t" / "feed-like.err").read_text().strip() == "a warning"


async def test_an_agent_that_writes_its_own_transcript_keeps_it(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text("import sys, pathlib\n"
                      f"pathlib.Path(sys.argv[1]).write_text({STREAM!r})\n"
                      "print('{\"status\": \"held\"}')\n")
    task = TASKS["login-device"]
    ctx = RunContext(task=task, lane_id="lanebench/login-device", base_url="http://127.0.0.1:1", runtime=None,
                     trace=ToolTrace(), lane_sites=["127.0.0.1"], work_dir=tmp_path, transcripts=tmp_path)
    ex = McpAgentExecutor(agent_cmd=f"{sys.executable} {script} {{transcript}}")
    result = await ex._agent(task, ctx, "http://127.0.0.1:1/mcp", "tok", tmp_path)
    assert result.status == "held"  # stdout's line, plain text
    assert result.metrics["cost_usd"] == 0.0123  # from the transcript the agent wrote
    assert (tmp_path / "login-device.log").read_text() == STREAM


def test_the_run_home_is_on_disk_not_in_the_temp_dir(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.delenv("OPENAJAR_TEST_ROOT", raising=False)
    assert harness.home_root() == harness.HOME_ROOT
    assert not str(harness.HOME_ROOT).startswith(tempfile.gettempdir())
    assert harness.home_root(tmp_path / "h") == tmp_path / "h" and (tmp_path / "h").is_dir()
    monkeypatch.setenv("OPENAJAR_TEST_ROOT", str(tmp_path / "env"))
    assert harness.home_root() == tmp_path / "env"


async def test_the_run_vault_holds_the_fixture_login_for_bench_lanes(tmp_path):
    from openajar.vault.policy import lane_bound

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    async with harness.bench_vault(home) as vault:
        (entry,) = await vault.list_entries()
        assert entry.name == "lanebench-ada" and entry.domain == "127.0.0.1"
        assert lane_bound(entry.lanes, "lanebench/login-lt") and not lane_bound(entry.lanes, "biz/reddit")
