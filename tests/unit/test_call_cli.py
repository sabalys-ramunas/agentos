"""`agentos call <tool>` (agentos.client.call): any MCP tool from a shell, under the agents' grant.

The approval loop runs against a scripted session; the end-to-end tests call the dev node over HTTP
with the agent grant `agentos dev up` prints, as a connection would."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from dev_node_support import dev_node_thread
from typer.testing import CliRunner

from agentos.cli import app
from agentos.client import call as call_mod
from agentos.client.call import CallError, StillPending, call_tool, parse_arguments
from agentos.client.errors import ClientError
from agentos.client.mcp_http import ToolError

runner = CliRunner()
PENDING = {"status": "approval_pending", "approval_id": "ap_1", "tool": "flow_run",
           "summary": "Run flow biz-a.social.post on biz-a/social"}


class Scripted:
    """A session that answers each call from a script: (tool, result or exception)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, arguments=None):
        self.calls.append((tool, dict(arguments or {})))
        want, answer = self.answers.pop(0)
        assert want == tool, (want, tool)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self):
        pass


def test_arguments_from_json_a_file_pairs_and_on(tmp_path):
    file = tmp_path / "args.json"
    file.write_text('{"params": {"caption": "Autumn"}}')
    assert parse_arguments(["limit=5", "name=biz-a.social.post", 'params={"caption": "x"}', "flag=true"],
                           '{"limit": 1, "wait_seconds": 0}', "biz-a/social") == {
        "limit": 5, "wait_seconds": 0, "name": "biz-a.social.post", "params": {"caption": "x"}, "flag": True,
        "on": "biz-a/social"}
    assert parse_arguments(None, f"@{file}", None) == {"params": {"caption": "Autumn"}}
    with pytest.raises(ClientError, match="--arg needs key=value"):
        parse_arguments(["oops"], None, None)
    with pytest.raises(ClientError, match="--json must be an object"):
        parse_arguments(None, "[1]", None)


def test_a_result_comes_back_as_it_is():
    session = Scripted(("whoami", {"principal": "claude-code@dev"}))
    assert call_tool(session, "whoami", {}) == {"principal": "claude-code@dev"}


def test_a_tool_error_carries_the_nodes_own_text():
    session = Scripted(("flow_run", ToolError("not_found: flow 'x' is not saved on this node\nfix: list first")))
    with pytest.raises(CallError, match="^not_found: flow 'x' is not saved"):
        call_tool(session, "flow_run", {"name": "x"})


def test_without_wait_a_call_that_waits_for_a_person_says_so():
    session = Scripted(("flow_run", PENDING))
    with pytest.raises(StillPending) as caught:
        call_tool(session, "flow_run", {"name": "biz-a.social.post"})
    assert caught.value.result == PENDING and len(session.calls) == 1  # never retried in a loop


def test_with_wait_an_approved_call_runs_again_with_the_same_arguments():
    args = {"name": "biz-a.social.post", "params": {"caption": "Autumn"}}
    session = Scripted(("flow_run", PENDING),
                       ("approval_wait", {"approval_id": "ap_1", "state": "pending"}),
                       ("approval_wait", {"approval_id": "ap_1", "state": "approved"}),
                       ("flow_run", {"id": "run_1", "status": "succeeded_verified"}))
    said = []
    result = call_tool(session, "flow_run", args, wait_s=300, say=said.append, sleep=lambda _: None)
    assert result == {"id": "run_1", "status": "succeeded_verified"}
    assert session.calls[0] == session.calls[-1] == ("flow_run", args)
    assert session.calls[1] == ("approval_wait", {"approval_id": "ap_1", "timeout_s": 60.0})
    assert said[0].startswith("Waiting for a person to approve: Run flow biz-a.social.post")
    assert said[-1] == "Approved. Calling it again with the same arguments."


def test_with_wait_a_denied_call_fails_and_is_not_called_again():
    session = Scripted(("flow_run", PENDING),
                       ("approval_wait", {"approval_id": "ap_1", "state": "denied", "note": "not today"}))
    with pytest.raises(CallError, match="flow_run: the approval was denied: not today"):
        call_tool(session, "flow_run", {}, wait_s=300, sleep=lambda _: None)
    assert [c[0] for c in session.calls] == ["flow_run", "approval_wait"]


def test_an_approval_the_person_edited_is_not_called_again():
    session = Scripted(("flow_run", PENDING),
                       ("approval_wait", {"approval_id": "ap_1", "state": "approved", "edited": ["caption"]}))
    out = call_tool(session, "flow_run", {}, wait_s=300, sleep=lambda _: None)
    assert out["edited"] == ["caption"] and len(session.calls) == 2


@pytest.fixture
def scripted(monkeypatch):
    holder = {}

    @contextmanager
    def fake_open(node=None, url=None, **_):
        holder["where"] = (node, url)
        yield holder["session"]

    monkeypatch.setattr(call_mod, "open_session", fake_open)
    return holder


def test_cli_prints_the_result_as_json(scripted):
    scripted["session"] = Scripted(("lanes_list", {"lanes": [{"id": "biz-a/social"}]}))
    result = runner.invoke(app, ["call", "lanes_list", "--node", "dev"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"lanes": [{"id": "biz-a/social"}]}
    assert scripted["where"] == ("dev", None)


def test_cli_exits_2_while_a_person_has_not_approved(scripted):
    scripted["session"] = Scripted(("flow_run", PENDING))
    result = runner.invoke(app, ["call", "flow_run", "--arg", "name=biz-a.social.post", "--on", "biz-a/social"])
    assert result.exit_code == 2
    assert '"status": "approval_pending"' in result.output
    assert "waiting for a person to approve: Run flow biz-a.social.post on biz-a/social" in result.output
    assert scripted["session"].calls == [("flow_run", {"name": "biz-a.social.post", "on": "biz-a/social"})]


def test_cli_exits_1_with_the_nodes_error(scripted):
    scripted["session"] = Scripted(("browser_act", ToolError("denied: commit is above your grant's ceiling")))
    result = runner.invoke(app, ["dev", "call", "browser_act", "--json", '{"ref": "e1"}'])
    assert result.exit_code == 1
    assert "error: denied: commit is above your grant's ceiling" in result.output


def test_url_needs_a_token(monkeypatch):
    monkeypatch.delenv("AGENTOS_TOKEN", raising=False)
    result = runner.invoke(app, ["call", "whoami", "--url", "http://127.0.0.1:9"])
    assert result.exit_code == 1 and "--url needs the token in $AGENTOS_TOKEN" in result.output


# ---- end to end, over HTTP, with the agent grant ----


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    with dev_node_thread(tmp_path_factory.mktemp("dev-node")) as dev:
        yield dev


def test_call_reaches_the_node_as_the_agent(node, monkeypatch):
    monkeypatch.setenv("AGENTOS_TOKEN", node.agent_token)
    result = runner.invoke(app, ["call", "grant_info", "--url", f"http://127.0.0.1:{node.port}"])
    assert result.exit_code == 0, result.output
    assert "claude-code@dev" in result.output  # the agent grant, never the node's owner token
    result = runner.invoke(app, ["call", "flow_get", "--arg", "name=biz-a.social.post", "--url",
                                 f"http://127.0.0.1:{node.port}"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["definition"]["id"] == "biz-a.social.post"


def test_call_shows_a_refusal_from_the_node(node, monkeypatch):
    monkeypatch.setenv("AGENTOS_TOKEN", node.agent_token)
    result = runner.invoke(app, ["call", "flow_get", "--arg", "name=biz-a.social.gone",
                                 "--url", f"http://127.0.0.1:{node.port}"])
    assert result.exit_code == 1 and "error: not_found: flow 'biz-a.social.gone' is not saved" in result.output


def test_a_wrong_token_is_refused(node, monkeypatch):
    monkeypatch.setenv("AGENTOS_TOKEN", "not-a-token")
    result = runner.invoke(app, ["call", "whoami", "--url", f"http://127.0.0.1:{node.port}"])
    assert result.exit_code == 1 and "the node refused the token" in result.output
