"""`agentos run` (agentos.client.run, agentos.client.run_cli) against an in-process node: the real env
tools and EnvManager on the fake host (commands run on this machine), called the way McpSession calls
them, with approvals the test decides. test_client_run_e2e.py does the same over HTTP with real grants."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.cli import app
from agentos.client import run as runs
from agentos.client import run_cli
from agentos.client.mcp_http import ToolError
from agentos.client.ops import Client
from agentos.client.state import Connection
from agentos.core.errors import AgentosError, ApprovalPending
from agentos.core.models import Effect, Principal
from agentos.core.runtime import Runtime
from agentos.env import tool_specs
from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher

EM_DASH = "—"


class FakeNode:
    """The node's env tools on their own event loop thread, behind a synchronous `call` like McpSession's.
    `ask` names tools whose EXEC waits for a person once; `decision` is what the person says."""

    def __init__(self, home: Path):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.mgr = EnvManager(home, host=FakeHost(), launcher=DirectLauncher(), use_lock=False,
                              settings=EnvSettings(proxy_port=0, max_timeout_s=60))
        self.runtime = Runtime(envs=self.mgr)
        self.tools = {s.name: s.handler for s in tool_specs(self.runtime)}
        self.ask: set[str] = set()
        self.decision = "approved"
        self.approvals: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.interrupt_reads_after: int | None = None  # raise KeyboardInterrupt on this lane_job_read
        self.timeout = 60.0

    def close(self) -> None:
        asyncio.run_coroutine_threadsafe(self.mgr.shutdown(), self.loop).result(15)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)

    def call(self, tool: str, arguments: dict | None = None):
        args = dict(arguments or {})
        self.calls.append((tool, args))
        if tool == "approval_wait":
            self.approvals[args["approval_id"]] = self.decision
            return {"approval_id": args["approval_id"], "state": self.decision}
        if tool == "lane_job_read" and self.interrupt_reads_after is not None:
            self.interrupt_reads_after -= 1
            if self.interrupt_reads_after < 0:
                self.interrupt_reads_after = None
                raise KeyboardInterrupt
        node = self

        class Ctx:
            runtime = node.runtime
            principal = Principal(id="claude-code@testmac")

            async def authorize(self, effect, *, lane_id=None, domain=None, summary=None, content=None, facts=None):
                if effect == Effect.EXEC and tool in node.ask:
                    aid = f"ap-{tool}"
                    if node.approvals.get(aid) != "approved":
                        raise ApprovalPending(f"waiting for the owner to approve: {summary}", aid)

            def trace(self, kind, **fields):
                pass

        try:
            out = asyncio.run_coroutine_threadsafe(self.tools[tool](Ctx(), **args), self.loop).result(60)
        except ApprovalPending as e:
            return {"status": "approval_pending", "approval_id": e.approval_id,
                    "summary": e.message.removeprefix("waiting for the owner to approve: ")}
        except AgentosError as e:
            raise ToolError(f"{e.code}: {e.message}") from None
        return json.loads(json.dumps(out, default=str))

    def close_session(self) -> None:
        pass


class SessionAdapter:
    def __init__(self, node: FakeNode):
        self.node = node
        self.timeout = 60.0

    def call(self, tool, arguments=None):
        return self.node.call(tool, arguments)

    def close(self):
        pass


@pytest.fixture
def node(tmp_path):
    n = FakeNode(tmp_path / "node-home")
    yield n
    n.close()


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "My Repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("build/\n*.log\n")
    (root / "Makefile").write_text("check:\n\t@echo checked\n")
    (root / "tests").mkdir()
    (root / "tests" / "t.sh").write_text("#!/bin/sh\necho tests ran in $(basename \"$PWD\")\nexit ${CODE:-0}\n")
    (root / "tests" / "t.sh").chmod(0o755)
    (root / "build").mkdir()
    (root / "build" / "out.o").write_text("ignored")
    (root / "debug.log").write_text("ignored")
    git(root, "add", "Makefile", ".gitignore", "tests/t.sh")
    (root / "notes.txt").write_text("untracked, not ignored\n")
    monkeypatch.chdir(root)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return root


@pytest.fixture
def cli(node, monkeypatch, tmp_path):
    """`agentos run` wired to the fake node, as connection `mynode` with an exec grant."""
    conn = Connection(name="mynode", ssh_host="mynode", remote_port=7400, local_port=7401, token_store="file",
                      service="launchd", credential="agent_grant", agent_specials=["exec", "memory_read"])
    monkeypatch.setattr(Client, "pick", lambda self, name: conn)
    monkeypatch.setattr(Client, "connections", lambda self: [conn])
    monkeypatch.setattr(run_cli, "open_session", lambda c, conn: SessionAdapter(node))
    return conn


def invoke(*args: str):
    result = CliRunner().invoke(app, ["run", *args])
    assert EM_DASH not in result.output
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def lane_dest(node: FakeNode) -> Path:
    return node.mgr.paths.workdir("dev/run-my-repo") / "src" / "My-Repo"


# ---------------------------------------------------------------- the tree


def test_the_tree_is_what_git_keeps(repo):
    files = runs.list_files(repo)
    assert files == [".gitignore", "Makefile", "notes.txt", "tests/t.sh"]
    (repo / "notes.txt").unlink()  # deleted here: not sent
    tree = runs.build_tree(repo, files)
    assert [e["path"] for e in tree.manifest["files"]] == [".gitignore", "Makefile", "tests/t.sh"]
    assert tree.manifest["files"][2]["mode"] == 0o755
    assert runs.default_lane(repo) == "dev/run-my-repo" and runs.dest_for(repo) == "src/My-Repo"
    assert runs.relative_cwd(repo, repo / "tests") == "tests" and runs.relative_cwd(repo, repo) is None


def test_hashes_are_cached_by_stat(repo, tmp_path):
    cache = runs.HashCache(tmp_path / "c.json")
    runs.build_tree(repo, ["Makefile"], cache)
    cache.save()
    again = runs.HashCache(tmp_path / "c.json")
    st = (repo / "Makefile").stat()
    assert again.get("Makefile", st) is not None
    (repo / "Makefile").write_text("changed:\n")
    assert again.get("Makefile", (repo / "Makefile").stat()) is None


def test_exit_codes_follow_how_the_job_ended():
    assert runs.exit_code_of({"state": "exited", "exit_code": 3}) == 3
    assert runs.exit_code_of({"state": "exited", "exit_code": None, "signal": "SIGKILL"}) == 137
    assert runs.exit_code_of({"state": "cancelled"}) == 130
    assert runs.exit_code_of({"state": "timed_out"}) == 124
    assert runs.command_line(["make", "check"]) == "make check"
    assert runs.command_line(["sh", "-c", "a && b"]) == "sh -c 'a && b'"
    assert runs.command_line(["make check && make lint"]) == "make check && make lint"


# ---------------------------------------------------------------- agentos run


def test_run_syncs_streams_and_passes_the_exit_code(cli, node, repo):
    r = invoke("--", "sh", "-c", "make check; echo to-stderr >&2; exit 3")
    assert r.exit_code == 3, r.output
    assert r.stdout == "checked\n"
    err = r.stderr
    assert "agentos run on mynode, lane dev/run-my-repo" in err
    assert "creating lane dev/run-my-repo (sandbox, files kept between runs, network off)" in err
    assert "tree: 4 files (" in err and "synced into src/My-Repo: 4 changed, 0 deleted" in err
    assert "to-stderr" in err and "agentos run: exit 3 in" in err
    dest = lane_dest(node)
    assert sorted(p.name for p in dest.iterdir()) == [".gitignore", "Makefile", "notes.txt", "tests"]
    created = next(a for t, a in node.calls if t == "env_create")
    assert created == {"on": "dev/run-my-repo", "preset": "sandbox", "filesystem": "overlay", "memory_mb": 4096,
                       "cpus": 2.0, "disk_mb": 8192}
    start = next(a for t, a in node.calls if t == "lane_job_start")
    assert start["env"] == {"PYTHONUNBUFFERED": "1"} and start["cwd"] == "src/My-Repo"


def test_a_second_run_sends_only_changes_and_deletes(cli, node, repo):
    assert invoke("--", "true").exit_code == 0
    (repo / "dep").mkdir()
    r = invoke("--", "sh", "-c", "mkdir -p .venv && touch .venv/cached")
    assert "sent 0 new chunks (0 B)" in r.stderr and "0 changed, 0 deleted" in r.stderr
    (repo / "notes.txt").unlink()
    (repo / "Makefile").write_text("check:\n\t@echo checked again\n")
    r = invoke("--", "make", "check")
    assert r.exit_code == 0 and r.stdout == "checked again\n"
    assert "sent 2 new chunks" in r.stderr  # the Makefile and the manifest
    assert "1 changed, 1 deleted" in r.stderr
    dest = lane_dest(node)
    assert not (dest / "notes.txt").exists() and (dest / ".venv" / "cached").exists()
    r = invoke("--fresh", "--", "ls", "-a")
    assert ".venv" not in r.stdout and "wiped first (--fresh)" in r.stderr


def test_the_command_runs_in_the_same_subdirectory(cli, node, repo, monkeypatch):
    monkeypatch.chdir(repo / "tests")
    r = invoke("--", "./t.sh")
    assert r.exit_code == 0 and r.stdout == "tests ran in tests\n"
    assert "(in src/My-Repo/tests)" in r.stderr


def test_ctrl_c_cancels_the_job(cli, node, repo):
    node.interrupt_reads_after = 1
    r = invoke("--", "sh", "-c", "echo started; sleep 30")
    assert r.exit_code == 130, r.output
    assert r.stdout == "started\n"  # written once, before and after the cancel
    assert "cancelling job" in r.stderr and "agentos run: cancelled" in r.stderr
    job = node.mgr.job_list()[0]
    assert job.state == "cancelled"
    assert ("lane_job_cancel", {"job": job.id}) in node.calls


def test_detach_follow_logs_list_and_cancel(cli, node, repo):
    r = invoke("--detach", "--", "sh", "-c", "echo out; sleep 0.5; echo done; exit 4")
    assert r.exit_code == 0
    jid = next(line.split()[2] for line in r.stderr.splitlines() if line.startswith("started job"))
    assert f"agentos run --follow {jid}" in r.stderr and "--node" not in r.stderr
    f = invoke("--follow", jid)
    assert f.exit_code == 4 and f.stdout == "out\ndone\n" and "exit 4" in f.stderr
    lg = invoke("--logs", jid)
    assert lg.exit_code == 4 and lg.stdout == "out\ndone\n"
    ls = invoke("--list")
    assert jid in ls.stdout and "exited" in ls.stdout and "dev/run-my-repo" in ls.stdout
    r = invoke("--detach", "--", "sleep", "30")
    jid2 = next(line.split()[2] for line in r.stderr.splitlines() if line.startswith("started job"))
    running = invoke("--logs", jid2)
    assert running.exit_code == 0 and "still running" in running.stderr
    c = invoke("--cancel", jid2)
    assert c.exit_code == 0 and "cancelled" in c.stderr
    assert invoke("--follow", jid, "--list").exit_code == 1


def test_an_approval_is_waited_for_and_named(cli, node, repo):
    node.ask = {"env_create", "lane_job_start"}
    r = invoke("--", "true")
    assert r.exit_code == 0, r.output
    assert "waiting for approval: Create sandbox terminal lane dev/run-my-repo" in r.stderr
    assert "waiting for approval: Sync 4 files (4 changed, 0 deleted) into src/My-Repo and run" in r.stderr
    assert "http://localhost:7401/needs-you" in r.stderr


def test_a_denied_approval_runs_nothing(cli, node, repo):
    node.ask = {"lane_job_start"}
    node.decision = "denied"
    r = invoke("--", "touch", "ran")
    assert r.exit_code == 1 and "the approval was denied, so nothing ran" in r.stderr
    assert not node.mgr.job_list()


def test_a_grant_without_exec_says_how_to_get_it(cli, node, repo):
    cli.agent_specials = ["memory_read"]
    r = invoke("--", "true")
    assert r.exit_code == 1
    assert "cannot run commands (it has no exec special)" in r.stderr
    assert "agentos connect mynode --name mynode --special exec --special memory_read" in r.stderr
    assert not node.calls


def test_outside_git_needs_a_lane(cli, node, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = invoke("--", "true")
    assert r.exit_code == 1 and "not inside one" in r.stderr
    node.mgr.create("dev/box", preset="sandbox")
    r = invoke("--lane", "dev/box", "--no-sync", "--", "echo", "hi")
    assert r.exit_code == 0 and r.stdout == "hi\n"


def test_node_errors_are_one_line_with_the_fix(cli, node, repo):
    r = invoke("--lane", "dev/run-my-repo", "--timeout", "5", "--", "true")
    assert r.exit_code == 0
    r = invoke("--follow", "job-0000000000")
    assert r.exit_code == 1 and r.stderr.startswith("error: not_found: no job job-0000000000")


def test_no_command_shows_the_help():
    r = CliRunner().invoke(app, ["run"])
    assert r.exit_code == 2 and "Run one command on a node" in r.output


def test_the_owner_never_reads_the_words_meant_for_the_agent():
    """QA 2026-11-10, tester B 10: `agentos run` printed "Tell the user what waits and wait with approval_wait"."""
    why = ("This call follows untrusted text this grant read (a web page on example.com), so a person must see it "
           "before it runs, even where exec would run on its own. Tell the user what waits and wait with "
           "approval_wait; do not rephrase or split the call to get around it.")
    shown = runs.person_words(why)
    assert shown.startswith("This call follows untrusted text") and "example.com" in shown
    assert "Tell the user" not in shown and "approval_wait" not in shown and "rephrase" not in shown
    assert runs.person_words(None) == "" and runs.person_words("A plain reason.") == "A plain reason."
