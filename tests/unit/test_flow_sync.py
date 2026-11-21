"""`openajar flows pull | diff | push` (openajar.runner.flowsync) against the dev node, over HTTP, with
the agent grant: what a shell on this computer does through its connection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dev_node_support import dev_node_thread
from typer.testing import CliRunner

from openajar.client.mcp_http import McpSession
from openajar.runner.cli import app
from openajar.runner.fakes import BASE

runner = CliRunner()
SEEDED = Path("biz-a") / "biz-a.social.post.yaml"


@pytest.fixture
def node(tmp_path, monkeypatch):
    with dev_node_thread(tmp_path / "node") as dev:
        monkeypatch.setenv("OPENAJAR_TOKEN", dev.agent_token)
        yield dev


def flows(node, *args: str, input: str | None = None):
    return runner.invoke(app, ["flows", *args, "--url", f"http://127.0.0.1:{node.port}"], input=input)


def session(node) -> McpSession:
    return McpSession(node.port, node.agent_token, host="127.0.0.1").__enter__()


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_pull_writes_each_flow_in_its_workspace_and_a_second_pull_writes_nothing(node, tmp_path):
    root = tmp_path / "flows"
    first = flows(node, "pull", str(root))
    assert first.exit_code == 0, first.output
    assert f"wrote {root / SEEDED}" in first.output and "1 written, 0 unchanged, 0 refused" in first.output
    text = (root / SEEDED).read_text()
    assert text.startswith("id: biz-a.social.post\ntitle: Post a photo\nlane: biz-a/social\n")
    again = flows(node, "pull", str(root))
    assert "0 written, 1 unchanged" in again.output
    diff = flows(node, "diff", str(root), "--exit-code")
    assert diff.exit_code == 0 and "0 to push, 1 the same" in diff.output


def test_edit_diff_push_then_pull_is_byte_stable(node, tmp_path):
    root = tmp_path / "flows"
    flows(node, "pull", str(root))
    path = root / SEEDED
    path.write_text(path.read_text().replace("title: Post a photo", "title: Post a photo to the feed"))
    diff = flows(node, "diff", str(root), "--exit-code")
    assert diff.exit_code == 1
    assert "--- node: biz-a.social.post" in diff.output
    assert "-title: Post a photo\n+title: Post a photo to the feed" in diff.output
    assert "1 to push, 0 the same" in diff.output

    declined = flows(node, "push", str(root), input="n\n")
    assert declined.exit_code == 1 and "Nothing was saved." in declined.output
    pushed = flows(node, "push", str(root), "--yes")
    assert pushed.exit_code == 0, pushed.output
    assert "saved biz-a.social.post sha256:" in pushed.output

    before = tree(root)
    pulled = flows(node, "pull", str(root))
    assert "0 written, 1 unchanged" in pulled.output and tree(root) == before
    assert flows(node, "diff", str(root), "--exit-code").exit_code == 0
    assert flows(node, "push", str(root), "--yes").output.startswith("Nothing to push")
    with session(node) as s:
        assert s.call("flow_get", {"name": "biz-a.social.post"})["definition"]["title"] == "Post a photo to the feed"


def test_a_new_flow_file_is_saved_as_an_agents_version(node, tmp_path):
    root = tmp_path / "flows"
    new = root / "biz-b" / "biz-b.social.like.yaml"
    new.parent.mkdir(parents=True)
    new.write_text(f"""\
id: biz-b.social.like
lane: biz-b/social
effect: draft
steps:
- type: navigate
  url: {BASE}/p/1
- type: click
  locator:
    role: button
    name: Like
    within:
      role: article
      name: Post by Ieva
  publishes: false
""")
    diff = flows(node, "diff", str(root))
    assert "+id: biz-b.social.like" in diff.output and "only on the node: biz-a.social.post" in diff.output
    pushed = flows(node, "push", str(root), "--yes")
    assert pushed.exit_code == 0, pushed.output
    assert "saved biz-b.social.like" in pushed.output
    with session(node) as s:
        got = s.call("flow_get", {"name": "biz-b.social.like"})
    # pushed with the agents' grant: an agent's "publishes": false never lowers a flow below its steps
    assert got["declared_effect"] == "draft" and got["effect"] == "commit"
    assert "only the owner can mark a step" in got["effect_note"]
    assert not (root / SEEDED).exists()  # push never pulls, and never removes


def test_push_validates_everything_first_and_saves_nothing_on_an_error(node, tmp_path):
    root = tmp_path / "flows"
    flows(node, "pull", str(root))
    path = root / SEEDED
    path.write_text(path.read_text().replace("title: Post a photo", "title: Changed"))
    bad = root / "biz-a" / "biz-a.social.bad.yaml"
    bad.write_text("id: biz-a.social.bad\nlane: biz-a/social\nsteps:\n- type: teleport\n")
    result = flows(node, "push", str(root), "--yes")
    assert result.exit_code == 1
    assert f"{bad}: invalid flow: steps.0" in result.output
    assert "1 flow files do not validate; nothing was compared" in result.output
    with session(node) as s:
        assert s.call("flow_get", {"name": "biz-a.social.post"})["definition"]["title"] == "Post a photo"


def test_pull_never_writes_a_flow_that_holds_a_secret(node, tmp_path):
    with session(node) as s:
        s.call("flow_save", {"definition": {
            "id": "biz-a.social.login", "lane": "biz-a/social",
            "params": {"type": "object", "properties": {"password": {"type": "string", "default": "hunter2-real"}}},
            "steps": [{"type": "navigate", "url": f"{BASE}/"}]}})
    root = tmp_path / "flows"
    result = flows(node, "pull", str(root))
    assert result.exit_code == 1
    assert "refused biz-a.social.login: param password looks like a credential" in result.output
    assert (root / SEEDED).exists() and not (root / "biz-a" / "biz-a.social.login.yaml").exists()
    assert "hunter2-real" not in json.dumps(tree(root), default=str)


def test_pull_takes_one_workspace(node, tmp_path):
    root = tmp_path / "flows"
    result = flows(node, "pull", str(root), "--workspace", "biz-b")
    assert result.exit_code == 0 and "0 written, 0 unchanged" in result.output and not root.exists()


def test_pull_never_overwrites_a_file_that_does_not_read(node, tmp_path):
    root = tmp_path / "flows"
    (root / "biz-a").mkdir(parents=True)
    half = "id: biz-a.social.post\nlane: biz-a/social\nsteps:\n- type: navig"
    (root / SEEDED).write_text(half)
    result = flows(node, "pull", str(root))
    assert result.exit_code == 1
    assert "refused biz-a.social.post:" in result.output and "does not read as a flow" in result.output
    assert (root / SEEDED).read_text() == half


def test_a_save_that_waits_for_a_person_is_reported_not_retried(tmp_path):
    from openajar.runner.fakes import post_flow
    from openajar.runner.flows import parse_definition
    from openajar.runner.flowsync import Change, push

    pending = {"status": "approval_pending", "approval_id": "ap_7", "tool": "flow_save",
               "summary": "Save flow biz-a.social.post for biz-a/social"}

    class Session:
        def __init__(self):
            self.calls = []

        def call(self, tool, arguments=None):
            self.calls.append(tool)
            return pending

    change = Change("biz-a.social.post", "changed", tmp_path / "f.yaml", parse_definition(post_flow()))
    session = Session()
    [done] = push(session, [change])
    assert done.pending == pending and done.version is None and session.calls == ["flow_save"]
