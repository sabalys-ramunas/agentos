"""Flows as files (openajar.runner.flowfiles): the YAML, its offline checks, and `openajar flows validate`."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from openajar.core.errors import InvalidRequest
from openajar.runner.cli import app
from openajar.runner.fakes import BASE, post_flow
from openajar.runner.flowfiles import (
    file_for,
    load_flow,
    load_yaml,
    readable,
    render,
    secret_problems,
    workspace_dir,
)
from openajar.runner.flows import parse_definition

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "flows"
runner = CliRunner()

BRANCHY = {
    "id": "biz-b.social.reply", "lane": "biz-*/social", "effect": "draft",
    "description": "Two lines,\nkept as a block.",
    "params": {"type": "object", "properties": {"reply": {"type": "string"}, "quick": {"type": "boolean"}}},
    "steps": [
        {"type": "navigate", "url": f"{BASE}/p/1"},
        {"type": "branch", "if": {"param": "quick", "equals": True},
         "then": [{"type": "fill", "locator": {"role": "textbox", "name": "Reply"}, "param": "reply",
                   "publishes": False}],
         "else": [{"type": "pause", "min_ms": 10, "max_ms": 20}]},
    ],
}


def flows() -> list[dict]:
    example = load_yaml((EXAMPLE / "acme" / "acme.linkedin.post.yaml").read_text(), "example")
    return [post_flow(), BRANCHY, example]


@pytest.mark.parametrize("raw", flows(), ids=lambda f: f["id"])
def test_a_rendered_flow_reads_back_as_the_same_version_and_the_same_bytes(raw):
    defn = parse_definition(raw)
    text = render(defn)
    again = parse_definition(load_yaml(text, "rendered"))
    assert again.content_hash() == defn.content_hash()
    assert render(again) == text  # byte-stable: a pull after a push writes nothing new


def test_the_file_leaves_defaults_out_but_keeps_what_a_reader_needs():
    data = readable(parse_definition(post_flow()))
    assert list(data)[:3] == ["id", "title", "lane"] and data["effect"] == "commit"
    assert all(step["type"] for step in data["steps"])  # each step says what it is
    assert "expose_as_tool" not in data and "match" not in str(data) and "own" not in data["steps"][1]["locator"]
    branch = render(parse_definition(BRANCHY))
    assert "description: |-\n  Two lines,\n  kept as a block." in branch
    assert "- type: fill" in branch and "if:\n    param: quick" in branch


def test_layout_is_the_lanes_workspace(tmp_path):
    defn = parse_definition(post_flow())
    assert file_for(tmp_path, defn) == tmp_path / "biz-a" / "biz-a.social.post.yaml"
    assert workspace_dir("*/social") == workspace_dir("biz-*/social") == "_any"


def write(tmp_path: Path, rel: str, text: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def validate(*paths: Path):
    return runner.invoke(app, ["flows", "validate", *map(str, paths)])


def test_validate_passes_the_example():
    result = validate(EXAMPLE)
    assert result.exit_code == 0, result.output
    assert "1 of 1 flows valid, 0 errors" in result.output


@pytest.mark.parametrize(("text", "says"), [
    ("id: biz-a.social.post\nlane: [unclosed\n", "not valid YAML (line 3): expected ',' or ']'"),
    ("id: biz-a.social.post\nid: biz-a.social.other\n", "the key 'id' is given twice"),
    ("- just\n- a list\n", "it must hold one flow (a mapping with id, lane, steps), not list"),
    ("id: biz-a.social.post\nlane: biz-a/social\nsteps:\n- type: teleport\n", "invalid flow: steps.0"),
    (("id: biz-a.social.post\nlane: biz-a/social\nsteps:\n- type: fill\n  locator: {role: textbox}\n"
     "  param: caption\n"), "param 'caption' is not declared"),
    (("id: biz-a.social.post\nlane: biz-a/social\nsteps:\n- type: click\n  locator: {role: button, name: Share}\n"
     "  writes: true\n"), "writes, so it must declare a readback"),
    ("id: biz-a.social.post\nlane: biz-a/social\nsteps:\n- type: click\n  locator: {ref: e12}\n",
     "snapshot refs (like e12) change on every page load"),
])
def test_validate_says_what_is_wrong_and_where(tmp_path, text, says):
    path = write(tmp_path, "biz-a/biz-a.social.post.yaml", text)
    result = validate(tmp_path)
    assert result.exit_code == 1
    assert f"{path}: error: " in result.output and says in result.output, result.output


def test_validate_checks_the_file_name_and_folder(tmp_path):
    good = render(parse_definition(post_flow()))
    write(tmp_path, "biz-a/post.yaml", good)
    write(tmp_path, "biz-b/biz-a.social.post.yaml", good)
    result = validate(tmp_path)
    assert result.exit_code == 1
    assert "the file is named post.yaml but holds flow biz-a.social.post: name it biz-a.social.post.yaml" \
        in result.output
    assert f"so it belongs in {tmp_path / 'biz-a'}, not {tmp_path / 'biz-b'}" in result.output
    assert "is also in" in result.output  # one file per flow


def test_validate_reports_warnings_and_strict_fails_on_them(tmp_path):
    flow = post_flow()
    flow["steps"] = flow["steps"][:4]  # no readback left
    write(tmp_path, "biz-a/biz-a.social.post.yaml", render(parse_definition(flow)))
    assert validate(tmp_path).exit_code == 0
    strict = runner.invoke(app, ["flows", "validate", "--strict", str(tmp_path)])
    assert strict.exit_code == 1 and "warning: the flow's effect is commit but it declares no readback" in strict.output


def test_validate_checks_tests_and_recordings(tmp_path):
    write(tmp_path, "biz-a/biz-a.social.post.yaml", render(parse_definition(post_flow())))
    write(tmp_path, "biz-a/biz-a.social.post.test.yaml", "recording: missing.recording.yaml\nparams: {}\n")
    write(tmp_path, "biz-a/bad.recording.yaml", "format: 1\npages: []\n")
    result = validate(tmp_path)
    assert result.exit_code == 1
    assert "the recording missing.recording.yaml does not exist" in result.output
    assert "bad.recording.yaml: error: " in result.output and "pages" in result.output


def test_no_flow_files_is_an_error(tmp_path):
    result = validate(tmp_path)
    assert result.exit_code == 1 and "no flow files in" in result.output


# ---- secrets never sit in a flow file ----


def with_steps(*steps, **params):
    return parse_definition({"id": "biz-a.site.login", "lane": "biz-a/site",
                             "params": {"type": "object", "properties": params},
                             "steps": [{"type": "navigate", "url": f"{BASE}/"}, *steps]})


def test_a_literal_typed_into_a_password_field_is_a_secret():
    defn = with_steps({"type": "fill", "locator": {"role": "textbox", "name": "Password"}, "text": "hunter22"})
    assert "looks like a password field" in secret_problems(defn)[0]
    templated = with_steps({"type": "fill", "locator": {"role": "textbox", "name": "Password"}, "param": "pw"},
                           pw={"type": "string"})
    assert secret_problems(templated) == []  # a param is filled at run time, from outside the file


def test_a_default_for_a_credential_param_is_a_secret():
    for name in ("password", "api_token", "otp_code", "session_id"):
        defn = with_steps(**{name: {"type": "string", "default": "s3cret-value"}})
        assert "looks like a credential" in secret_problems(defn)[0], name
    assert secret_problems(with_steps(caption={"type": "string", "default": "Autumn"})) == []


def test_a_value_the_node_masked_is_never_written_back():
    defn = with_steps({"type": "type", "locator": {"role": "textbox", "name": "Search"}, "text": "[redacted]"})
    assert "masked a value from its vault" in secret_problems(defn)[0]


def test_validate_refuses_a_flow_file_that_holds_a_secret(tmp_path):
    defn = with_steps({"type": "fill", "locator": {"role": "textbox", "name": "Password"}, "text": "hunter22"})
    path = write(tmp_path, "biz-a/biz-a.site.login.yaml", render(defn))
    ff = load_flow(path, root=tmp_path)
    assert not ff.ok and "a secret never sits in a flow" in ff.errors[0]


def test_load_yaml_names_the_file():
    with pytest.raises(InvalidRequest, match=r"^x.yaml is not valid YAML"):
        load_yaml("a: [", "x.yaml")


def test_the_example_is_in_the_form_pull_writes():
    path = EXAMPLE / "acme" / "acme.linkedin.post.yaml"
    flow = load_flow(path, root=EXAMPLE)
    assert flow.ok and render(flow.definition) == path.read_text()
