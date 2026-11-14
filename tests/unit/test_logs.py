"""Structured logs (agentos.core.logs): ids from the context on every line, JSON out, secrets
scrubbed before anything is written, and `agentos logs` reading them back with filters."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os

import httpx2
import pytest
from fakes import FakeInteraction, FakeLaneManager, mcp_port_for_tests
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from typer.testing import CliRunner

from agentos.core import logs
from agentos.core.config import NodeConfig, save_proxies
from agentos.core.errors import EngineError
from agentos.core.models import Effect, LaneSpec, ProxyConfig
from agentos.core.tools import ToolSpec
from agentos.ops import logs as reader

NODE_TOKEN = "node-token-for-the-logs-test"
PROXY_PASSWORD = "hunter2-proxy"
VAULT_VALUE = "correct horse battery staple 42"  # a login entry's password
MACHINE_VALUE = "machine secret for the brain 7"  # a machine secret (a brain's subscription token)

log = logging.getLogger("agentos.test.logs")


@pytest.fixture(autouse=True)
def root_logging():
    """setup() replaces the root handlers; put pytest's back afterwards."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)
    logs.clear_scrubbers()


def capture(**kw) -> io.StringIO:
    stream = io.StringIO()
    logs.setup(None, stream=stream, **kw)
    return stream


# ---- context ----

async def test_ids_come_from_the_context_and_nest():
    assert logs.current() == {}
    with logs.log_context(lane="biz-a/instagram", request="rq_1"):
        assert logs.current() == {"lane": "biz-a/instagram", "workspace": "biz-a", "request": "rq_1"}
        with logs.log_context(run="run_1", grant=None):
            logs.update(grant="g_1")
            assert logs.current()["run"] == "run_1" and logs.current()["grant"] == "g_1"
        assert "run" not in logs.current() and "grant" not in logs.current()
    assert logs.current() == {}
    with pytest.raises(ValueError, match="unknown log fields: token"):
        logs.log_context(token="x").__enter__()


async def test_a_task_keeps_its_parents_ids_and_never_changes_them():
    seen = {}

    async def child(name):
        logs.update(run=name)
        await asyncio.sleep(0)
        seen[name] = logs.current()

    with logs.log_context(request="rq_parent"):
        await asyncio.gather(asyncio.create_task(child("a")), asyncio.create_task(child("b")))
        assert logs.current() == {"request": "rq_parent"}
    assert seen == {"a": {"request": "rq_parent", "run": "a"}, "b": {"request": "rq_parent", "run": "b"}}


# ---- format and scrubbing ----

def test_a_json_line_carries_the_ids_and_the_journal_priority():
    stream = capture(journal=True)
    with logs.log_context(lane="personal/reddit", grant="g_7", request="rq_9"):
        log.warning("opened %s", "it")
    raw = stream.getvalue().strip()
    assert raw.startswith("<4>{")
    line = json.loads(raw[3:])
    assert line["msg"] == "opened it" and line["level"] == "warn" and line["logger"] == "agentos.test.logs"
    assert (line["lane"], line["workspace"], line["grant"], line["request"]) == ("personal/reddit", "personal",
                                                                                "g_7", "rq_9")


def test_a_service_user_under_systemd_also_keeps_the_file_it_can_read(tmp_path):
    stream = io.StringIO()
    logs.setup(tmp_path, stream=stream, journal=True, file_too=True)
    log.warning("kept twice")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert stream.getvalue().startswith("<4>{")
    [line] = [json.loads(raw) for raw in (tmp_path / "logs" / logs.LOG_FILE).read_text().splitlines()]
    assert line["msg"] == "kept twice"
    logs.setup(tmp_path / "root", stream=io.StringIO(), journal=True, file_too=False)  # root reads the journal
    assert not (tmp_path / "root" / "logs").exists()


def test_off_systemd_lines_go_to_the_file_as_json_and_to_the_terminal_readable(tmp_path):
    stream = io.StringIO()
    logs.setup(tmp_path, stream=stream, journal=False)
    with logs.log_context(run="run_3"):
        log.info("step done")
    stored = (tmp_path / "logs" / logs.LOG_FILE).read_text().strip()
    assert json.loads(stored)["run"] == "run_3"
    assert "INFO  agentos.test.logs [run=run_3] step done" in stream.getvalue()


def test_message_traceback_and_ids_are_scrubbed_and_a_failing_scrubber_withholds_the_line():
    stream = capture(journal=True)
    logs.add_scrubber(lambda text: text.replace("s3cret", "[redacted]"))
    try:
        raise RuntimeError("boom s3cret")
    except RuntimeError:
        with logs.log_context(lane="ws/s3cret"):
            log.exception("failed with %s", "s3cret")
    line = json.loads(stream.getvalue().strip()[3:])
    assert "s3cret" not in stream.getvalue()
    assert line["msg"] == "failed with [redacted]" and "RuntimeError: boom [redacted]" in line["exc"]
    assert line["lane"] == "ws/[redacted]"

    def broken(text):
        raise ValueError("nope")

    logs.add_scrubber(broken)
    log.info("s3cret again")
    last = json.loads(stream.getvalue().strip().splitlines()[-1][3:])
    assert last["msg"] == logs.WITHHELD


def test_a_scrubber_that_logs_does_not_recurse_or_leak():
    stream = capture(journal=True)

    def chatty(text):
        log.warning("scrubbing %s", text)  # would print the unscrubbed text
        return text.replace("pw", "**")

    logs.add_scrubber(chatty)
    log.info("the pw is here")
    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0][3:])["msg"] == "the ** is here"


def test_under_systemd_means_stderr_is_the_journal_stream(tmp_path, monkeypatch):
    with (tmp_path / "f").open("w") as f:
        st = os.fstat(f.fileno())
        monkeypatch.setenv("JOURNAL_STREAM", f"{st.st_dev}:{st.st_ino}")
        assert logs.under_systemd(f)
        monkeypatch.setenv("JOURNAL_STREAM", "1:2")
        assert not logs.under_systemd(f)


# ---- no secret reaches a log line ----

async def test_a_request_with_a_grant_token_a_proxy_password_and_a_vault_value_logs_none_of_them(tmp_path):
    """Drive a real HTTP MCP request through policy and vault whose tool logs everything it got
    (message, traceback, error), then grep every output: the file, the terminal, the journal."""
    from test_vault_support import make_vault, private_home

    from agentos.node.daemon import build_services, install_log_scrubbers, node_runtime, serve

    home = private_home(tmp_path)
    make_vault(home)  # a key-file vault on disk: the service unseals it when it starts
    config = NodeConfig(home=home, mcp_token=NODE_TOKEN, mcp_port=mcp_port_for_tests())
    save_proxies(config, {"lt1": ProxyConfig(label="lt1", host="5.6.7.8", port=1, username="proxyuser",
                                             password=PROXY_PASSWORD)})
    terminal, journal = io.StringIO(), io.StringIO()
    logs.setup(home, stream=terminal, journal=False)
    journal_handler = logging.StreamHandler(journal)
    journal_handler.setFormatter(logs.JsonFormatter(syslog_prefix=True))
    journal_handler.addFilter(logs.ContextFilter())
    logging.getLogger().addHandler(journal_handler)
    install_log_scrubbers(config)

    manager = FakeLaneManager(config)
    await manager.create(LaneSpec(id="personal/reddit"))
    runtime = node_runtime(config, manager, FakeInteraction())
    build_services(runtime, ["agentos.policy.service", "agentos.vault.service"])

    async def leaky(ctx, *, on: str, note: str) -> str:
        from agentos.mcp.registry import current_request_token

        log.info("leaky got %s with bearer %s", note, current_request_token())
        try:
            raise RuntimeError(f"inner failure quoting {note}")
        except RuntimeError:
            log.exception("leaky traceback")
        raise EngineError(f"outer failure quoting {note} and {current_request_token()}")

    providers = [lambda rt: [ToolSpec(name="leaky_tool", handler=leaky, effect=Effect.READ)]]
    port: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()
    task = asyncio.create_task(serve(config, runtime, stop=stop, on_started=port.set_result, providers=providers,
                                     panel=False))
    try:
        url = f"http://localhost:{await asyncio.wait_for(port, 15)}/mcp"
        vault = runtime.require("vault")
        await vault.add_entry(name="reddit", kind="password", domain="reddit.com", lanes=["personal/reddit"],
                              username="someone", secret=VAULT_VALUE)
        await vault.put_secret("brains/claude/credential", MACHINE_VALUE, kind="oauth_token")
        grant, token = runtime.require("policy").issue(subject={"id": "agent-1"}, workspaces=["personal"],
                                                       ceiling=Effect.READ)
        note = f"proxy {PROXY_PASSWORD} vault {VAULT_VALUE} machine {MACHINE_VALUE} token {token}"
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            Client(streamable_http_client(url, http_client=http), mode="legacy") as client,
        ):
            result = await client.call_tool("leaky_tool", {"on": "personal/reddit", "note": note})
        assert result.is_error
    finally:
        stop.set()
        await asyncio.wait_for(task, 20)

    stored = (home / "logs" / logs.LOG_FILE).read_text()
    for name, text in {"file": stored, "terminal": terminal.getvalue(), "journal": journal.getvalue()}.items():
        assert "leaky got" in text and "leaky traceback" in text, name
        for secret in (token, PROXY_PASSWORD, VAULT_VALUE, MACHINE_VALUE, NODE_TOKEN):
            assert secret not in text, (name, secret[:4])
    lines = [json.loads(raw) for raw in stored.splitlines()]
    got = next(line for line in lines if line["msg"].startswith("leaky got"))
    assert got["lane"] == "personal/reddit" and got["workspace"] == "personal" and got["grant"] == grant.id
    assert got["tool"] == "leaky_tool" and got["request"].startswith("rq_")
    assert "inner failure quoting" in next(line for line in lines if line["msg"] == "leaky traceback")["exc"]


# ---- agentos logs ----

def write_lines(home, lines, name=logs.LOG_FILE):
    folder = home / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / name).open("a") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


LINES = [
    {"ts": "2026-11-04T08:00:00.000Z", "level": "info", "logger": "agentos.node", "msg": "old", "lane": "biz-a/ig",
     "workspace": "biz-a"},
    {"ts": "2026-11-04T09:00:00.000Z", "level": "warn", "logger": "agentos.node", "msg": "slow", "lane": "biz-a/x",
     "workspace": "biz-a", "grant": "g_1", "run": "run_1"},
    {"ts": "2026-11-04T09:30:00.000Z", "level": "debug", "logger": "agentos.mcp", "msg": "noise", "grant": "g_2"},
    {"ts": "2026-11-04T09:40:00.000Z", "level": "error", "logger": "agentos.node", "msg": "boom",
     "lane": "personal/reddit", "workspace": "personal", "request": "rq_1"},
]


def test_query_filters_on_ids_level_and_time():
    since = reader.parse_since("2026-11-04T08:30:00Z")
    pick = lambda **kw: [line["msg"] for line in reader.select(LINES, reader.Query(**kw))]
    assert pick(lane="biz-a/*") == ["old", "slow"]
    assert pick(grant="g_1") == ["slow"] and pick(run="run_1") == ["slow"] and pick(request="rq_1") == ["boom"]
    assert pick(workspace="personal") == ["boom"]
    assert pick(level="warn") == ["slow", "boom"] and pick() == ["old", "slow", "noise", "boom"]
    assert pick(since=since, level="info") == ["slow", "boom"]
    assert reader.parse_since("10m", now=1000.0) == 400.0 and reader.parse_since("1d", now=86400.0) == 0.0
    with pytest.raises(ValueError):
        reader.parse_since("yesterday")
    with pytest.raises(ValueError):
        reader.parse_level("loud")


def test_files_are_read_oldest_first(tmp_path):
    write_lines(tmp_path, LINES[:1], f"{logs.LOG_FILE}.2")
    write_lines(tmp_path, LINES[1:2], f"{logs.LOG_FILE}.1")
    write_lines(tmp_path, LINES[2:])
    assert [line["msg"] for line in reader.file_lines(tmp_path, follow=False)] == ["old", "slow", "noise", "boom"]


def test_follow_picks_up_new_lines_and_a_rotation(tmp_path):
    write_lines(tmp_path, LINES[:1])
    current = tmp_path / "logs" / logs.LOG_FILE
    steps = iter([
        lambda: write_lines(tmp_path, LINES[1:2]),
        lambda: (current.rename(current.with_name(f"{logs.LOG_FILE}.1")), write_lines(tmp_path, LINES[3:])),
        lambda: None,
    ])
    done = []

    def sleep(_):
        step = next(steps, None)
        if step is None:
            done.append(True)
        else:
            step()

    got = [line["msg"] for line in reader.file_lines(tmp_path, follow=True, sleep=sleep, stop=lambda: bool(done))]
    assert got == ["old", "slow", "boom"]


def test_journal_entries_become_lines():
    ours = {"MESSAGE": json.dumps(LINES[1]), "PRIORITY": "4", "__REALTIME_TIMESTAMP": "1762246800000000"}
    theirs = {"MESSAGE": "Started agentosd.service", "PRIORITY": "6", "__REALTIME_TIMESTAMP": "1762246800000000",
              "SYSLOG_IDENTIFIER": "systemd"}
    raw = {"MESSAGE": list(b"bytes \xff here"), "PRIORITY": "3", "__REALTIME_TIMESTAMP": "1762246800000000"}
    assert reader.from_journal(ours) == LINES[1]
    plain = reader.from_journal(theirs)
    assert plain["msg"] == "Started agentosd.service" and plain["logger"] == "systemd" and plain["level"] == "info"
    assert plain["ts"] == "2025-11-04T09:00:00.000Z"
    assert reader.from_journal(raw)["level"] == "error" and reader.from_journal(raw)["msg"].startswith("bytes ")
    assert reader.journal_command("agentosd", 100.0, True) == [
        "journalctl", "-u", "agentosd", "-o", "json", "--no-pager", "-q", "--since", "@100", "-f"]


def test_journal_lines_read_journalctl_output():
    class Proc:
        stdout = io.StringIO("\n".join([json.dumps({"MESSAGE": json.dumps(line)}) for line in LINES] + ["junk", ""]))

        def terminate(self):
            pass

        def wait(self):
            return 0

    calls = []
    out = list(reader.journal_lines("agentosd", None, False, popen=lambda cmd, **kw: calls.append(cmd) or Proc()))
    assert [line["msg"] for line in out] == ["old", "slow", "noise", "boom"] and calls[0][:3] == ["journalctl", "-u",
                                                                                                   "agentosd"]


def test_agentos_logs_prints_filtered_lines(tmp_path, monkeypatch):
    from agentos.cli import app

    write_lines(tmp_path, LINES)
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    run = CliRunner().invoke(app, ["logs", "--source", "file", "--lane", "biz-a/*", "--level", "warn"])
    assert run.exit_code == 0, run.output
    assert run.output.splitlines() == ["2026-11-04 09:00:00 WARN  agentos.node [lane=biz-a/x grant=g_1 run=run_1] slow"]
    as_json = CliRunner().invoke(app, ["logs", "--source", "file", "--request", "rq_1", "--json"])
    assert [json.loads(raw) for raw in as_json.output.splitlines()] == [LINES[3]]
    bad = CliRunner().invoke(app, ["logs", "--source", "file", "--since", "soon"])
    assert bad.exit_code == 2 and "cannot read --since" in bad.output
    empty = tmp_path / "empty"
    monkeypatch.setenv("AGENTOS_HOME", str(empty))
    none = CliRunner().invoke(app, ["logs", "--source", "file"])
    assert none.exit_code == 1 and "no log in" in none.output
