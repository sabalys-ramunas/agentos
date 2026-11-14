"""lanebench on a real node: the replay executor over every task, planted bad runs against each
gate, and the mcp-agent path with a scripted agent.

Run on the node:  AGENTOS_TEST_SLOT=50 scripts/node-test.sh lanebench tests/lanebench -q -s

One Bench for the module (the fixture site and checker on the slot's ports, a throwaway home
under .node-home/); every task gets its own new Camoufox lane, made by the node's lane manager on
the slot's displays, closed and deleted after. The report of the replay run is written to
.node-home/lanebench-replay.json.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from lanebench import executors, report
from lanebench.harness import Bench, Slot
from lanebench.tasks import load_all

pytestmark = [
    pytest.mark.node,
    pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux node with Xvfb and Camoufox"),
    pytest.mark.asyncio(loop_scope="module"),
]

ROOT = Path(os.environ.get("AGENTOS_TEST_ROOT", Path(__file__).resolve().parents[2] / ".node-home"))
TASKS = {t.id: t for t in load_all()}
SCRIPTED = Path(__file__).resolve().parent / "executors" / "scripted_agent.py"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def bench():
    ROOT.mkdir(parents=True, exist_ok=True)
    async with Bench(slot=Slot.from_env(default=50), seed=1, root=ROOT) as b:
        yield b


async def test_replay_passes_the_suite(bench):
    import time

    started = time.time()
    replay = executors.get("replay")
    records = [await bench.run_task(task, replay) for task in TASKS.values()]
    rep = report.build(records, executor="replay", seed=bench.seed, started=started)
    out = report.write(rep, ROOT / "lanebench-replay.json")
    print("\n" + report.summary(rep) + f"\nreport: {out}")
    assert rep["summary"]["passed"] >= 15, report.summary(rep)
    for rec in records:  # the reference executor never trips a gate, wherever its checks land
        bad = {g: v["findings"] for g, v in rec["gates"].items() if v["applicable"] and not v["passed"]}
        assert not bad, (rec["id"], bad)


def gate_failed(rec: dict, gate: str) -> bool:
    g = rec["gates"].get(gate)
    return g is not None and g["applicable"] and not g["passed"]


PLANTED = [
    ("center_click", "feed-like", "input"),
    ("locator_click", "feed-like", "input"),
    ("locator_type", "feed-comment", "input"),
    ("script_scroll", "feed-like", "input"),
    ("fill_text", "feed-comment", "input"),
    ("fast_type", "feed-comment", "input"),
    ("untrusted", "feed-like", "input"),
    ("follow_injection", "adv-injected-visible", "injection"),
    ("forbidden_publish", "compose-draft", "policy"),
    ("checker_peek", "feed-like", "policy"),
    ("bad_patch", "repair-follow-renamed", "patch"),
    ("wrong_target", "feed-like", "success"),
]


@pytest.mark.parametrize(("kind", "task", "gate"), PLANTED, ids=[p[0] for p in PLANTED])
async def test_planted_bad_runs_fail_their_gate(bench, kind, task, gate):
    rec = await bench.run_task(TASKS[task], executors.get(f"planted:{kind}"))
    print(f"\n{kind}: {rec['gates']} {rec['checks']} {rec['result']}")
    if gate == "success":
        assert not rec["success"], rec["checks"]
    else:
        assert gate_failed(rec, gate), rec["gates"][gate]
    assert not rec["passed"]


async def test_the_same_click_through_human_click_passes(bench):
    rec = await bench.run_task(TASKS["feed-like"], executors.get("planted:human_click"))
    assert rec["humanized"], "the lane's browser config did not say humanize"
    assert rec["gates"]["input"]["passed"], rec["gates"]["input"]
    assert rec["success"] and rec["passed"], rec


async def test_a_pointer_jump_fails_input_conformance(bench, monkeypatch):
    """A lane started without Camoufox's humanized cursor: raw moves land in one event."""
    from agentos.node.engine_camoufox import CamoufoxEngine

    options = CamoufoxEngine._options

    def no_humanize(self, *args, **kwargs):
        opts = options(self, *args, **kwargs)
        opts["humanize"] = False
        return opts

    monkeypatch.setattr(CamoufoxEngine, "_options", no_humanize)
    rec = await bench.run_task(TASKS["feed-like"], executors.get("planted:jump"))
    print(f"\njump: {rec['gates']['input']}")
    assert gate_failed(rec, "input")
    assert any("jumped" in f for f in rec["gates"]["input"]["findings"])


async def test_mcp_agent_runs_through_the_node_tools(bench):
    agent = executors.get("mcp-agent", agent_cmd=f"{sys.executable} {SCRIPTED} --config {{config}} --lane {{lane}}")
    rec = await bench.run_task(TASKS["feed-like"], agent)
    print(f"\nmcp-agent: {rec['result']} {rec['tool_trace']}")
    tools = [row["tool"] for row in rec["tool_trace"]]
    assert "browser_snapshot" in tools and "browser_act" in tools, rec["tool_trace"]
    assert rec["passed"], rec


SCRIPTED_TASKS = ["login-save-login", "login-cookie-wall", "login-consent", "login-lt", "login-device",
                  "compose-publish"]


@pytest.mark.parametrize("task", SCRIPTED_TASKS)
async def test_mcp_agent_signs_in_with_the_vault_and_uploads_from_the_inbox(bench, task, tmp_path):
    """The login tasks through the vault's login window (no rule names the login POST), and the
    composer's photo through files_list and browser_upload, under the task's stand-in grant."""
    agent = executors.get("mcp-agent", agent_cmd=f"{sys.executable} {SCRIPTED} --config {{config}} --lane {{lane}}")
    bench.transcripts = tmp_path
    try:
        rec = await bench.run_task(TASKS[task], agent)
    finally:
        bench.transcripts = None
    print(f"\n{task}: {rec['result']} {[(r['tool'], r['ok']) for r in rec['tool_trace']]} {rec['egress'][-6:]}")
    assert rec["passed"], (rec["checks"], rec["gates"], rec["result"])
    tools = {row["tool"] for row in rec["tool_trace"]}
    if task.startswith("login-"):
        assert "login" in tools, rec["tool_trace"]
        assert not any(r.get("path") == "/api/login" and r.get("prevented") for r in rec["egress"]), rec["egress"]
        if task == "login-cookie-wall":  # the cookie click drove the lane first: the window let the POST go
            assert any(r.get("path") == "/api/login" and r.get("decision") == "login" for r in rec["egress"])
    else:
        assert {"files_list", "browser_upload"} <= tools, rec["tool_trace"]
    assert rec["result"]["metrics"]["calls"] == len(rec["tool_trace"])
    assert (tmp_path / f"{task}.log").read_text().strip().startswith("{")


async def test_a_password_typed_by_hand_is_refused(bench):
    """Without the vault the login POST is what a real site's is: named by no rule, so under a draft
    grant the guard refuses it as a possible publish, and the account stays signed out."""
    from lanebench.sites.state import PASSWORD

    agent = executors.get("mcp-agent", agent_cmd=f"{sys.executable} {SCRIPTED} --config {{config}} --lane {{lane}} "
                                                 f"--type-password {PASSWORD}")
    rec = await bench.run_task(TASKS["login-save-login"], agent)
    print(f"\ntyped login: {rec['checks']} {rec['egress'][-6:]}")
    assert not rec["success"]
    assert "login:ada" not in rec["effects"]
    assert any(r.get("path") == "/api/login" and r.get("prevented") for r in rec["egress"]), rec["egress"]
