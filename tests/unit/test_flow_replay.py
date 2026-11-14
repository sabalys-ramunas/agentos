"""`agentos flows test` (agentos.runner.flowtest): a flow run offline on a recording, and what it reports.

The worked example in examples/flows (docs/WORKFLOWS.md, "Flows in git") runs here, so CI keeps it
passing."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.core.locator import Locator
from agentos.runner import flowcli
from agentos.runner.cli import app
from agentos.runner.fakes import resolve_all, resolve_one
from agentos.runner.flowfiles import load_yaml
from agentos.runner.flowtest import parse_outline

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "flows"
FLOW = "acme/acme.linkedin.post.yaml"
runner = CliRunner()


def flows_test(*args: str):
    return runner.invoke(app, ["flows", "test", *args])


@pytest.fixture
def example(tmp_path) -> Path:
    shutil.copytree(EXAMPLE, tmp_path / "flows")
    return tmp_path / "flows"


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def test_the_worked_example_passes():
    result = flows_test(str(EXAMPLE), "--verbose")
    assert result.exit_code == 0, result.output
    assert "PASS posts the text as the company page  (succeeded_verified)" in result.output
    assert "PASS stops before posting when the Post button is gone  (partial)" in result.output
    assert "step 1        assert_identity  done" in result.output
    assert "2 of 2 cases passed" in result.output


def test_a_test_runs_from_its_flow_file_too():
    result = flows_test(str(EXAMPLE / FLOW))
    assert result.exit_code == 0 and "2 of 2 cases passed" in result.output


def test_a_locator_the_page_does_not_have_fails_and_says_where(example):
    edit(example / FLOW, "name: Start a post", "name: Start a new post")
    result = flows_test(str(example))
    assert result.exit_code == 1
    assert "FAIL posts the text as the company page  (failed)" in result.output
    assert ('status: expected succeeded_verified, got failed: step 3 (click): element_not_found: no element matches '
            'button "Start a new post"') in result.output
    assert "ran: expected steps 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, got 0, 1, 2" in result.output
    assert "0 of 2 cases passed" in result.output


def test_a_readback_that_no_longer_holds_fails(example):
    edit(example / FLOW, "contains: ${text}", "equals: ${text}")  # the article also names the page
    result = flows_test(str(example))
    assert result.exit_code == 1
    assert "status: expected succeeded_verified, got partial: step 9 (readback): readback_failed" in result.output
    assert "found Acme Bakery Fresh rye is out of the oven. Come by before noon! Like" in result.output


def test_what_would_be_published_is_checked(example):
    test = example / "acme" / "acme.linkedin.post.test.yaml"
    edit(test, "fields:\n        textbox \"Text editor for creating content\": Fresh rye",
         "fields:\n        textbox \"Text editor for creating content\": Stale rye")
    result = flows_test(str(example), "--json")
    assert result.exit_code == 1
    first = json.loads(result.output)[0]["cases"][0]
    assert not first["passed"] and first["status"] == "succeeded_verified"
    assert first["published"] == [{"pressed": 'button "Post"', "fields": {
        'textbox "Text editor for creating content"': "Fresh rye is out of the oven. Come by before noon!"}}]
    assert first["problems"][0].startswith("published: expected button \"Post\" with textbox ")


def test_each_case_fills_the_recording_with_its_own_params(example):
    test = example / "acme" / "acme.linkedin.post.test.yaml"
    test.write_text(test.read_text().replace("Fresh rye is out of the oven. Come by before noon!",
                                             "Croissants are 20% off today"))
    result = flows_test(str(example))
    assert result.exit_code == 0, result.output


def test_params_are_checked_against_the_flows_schema(example):
    edit(example / "acme" / "acme.linkedin.post.test.yaml", "text: Fresh rye", "txt: Fresh rye")
    result = flows_test(str(example))
    assert result.exit_code == 1 and "invalid params for acme.linkedin.post" in result.output


def test_a_test_file_that_cannot_run_is_an_error(example):
    (example / "acme" / "company-page.recording.yaml").unlink()
    result = flows_test(str(example))
    assert result.exit_code == 1
    assert "ERROR the recording" in result.output and "company-page.recording.yaml does not exist" in result.output


def test_no_tests_is_an_error(tmp_path):
    result = flows_test(str(tmp_path))
    assert result.exit_code == 1 and "no tests in" in result.output


# ---- outlines, as recordings keep them ----

OUTLINE = """\
- banner:
  - link "Home" [ref=e1] [cursor=pointer]:
    - /url: /feed/
- main:
  - heading "Acme Bakery" [level=1]
  - button "Like" [pressed] [ref=e2]
  - textbox "Search": rye bread
  - region "Posts":
    - article:
      - paragraph: First post
      - text: "quoted text"
    - article:
      - paragraph: Second post
"""


def test_an_outline_reads_as_the_tree_locators_resolve_on():
    root = parse_outline(OUTLINE, "Feed")
    link = resolve_one(root, Locator(role="link", name="Home"))
    assert link.attrs == {"href": "/feed/"}  # refs and cursor are where it sits, not what it is
    assert resolve_one(root, Locator(role="heading", level=1)).name == "Acme Bakery"
    assert resolve_one(root, Locator(role="button", name="Like")).attrs["aria-pressed"] == "true"
    assert resolve_one(root, Locator(role="textbox", name="Search")).attrs["value"] == "rye bread"
    posts = resolve_all(root, Locator(role="article", within=Locator(role="region", name="Posts")))
    assert len(posts) == 2
    from agentos.runner.fakes import own_text

    assert own_text(posts[0]) == "First post quoted text"
    assert own_text(root.children[0]) == "Home"  # a link's name is the text it shows


def test_capture_keeps_the_outline_without_refs_and_puts_params_back(tmp_path, monkeypatch):
    snapshot = ("lane: acme/linkedin\nurl: https://www.linkedin.example/feed/\nsignal: ok\nrefs: 2\n"
                "untrusted: page text\ntitle: Feed\n\n"
                '- button "Post" [ref=e1] [cursor=pointer] (hint: send)\n- paragraph: Fresh rye today  \n')
    calls = []

    class Session:
        def call(self, tool, arguments=None):
            calls.append((tool, arguments))
            return snapshot if tool == "browser_snapshot" else {"ok": True}

        def close(self):
            pass

    from contextlib import contextmanager

    @contextmanager
    def fake_open(node=None, url=None, **_):
        yield Session()

    monkeypatch.setattr("agentos.client.call.open_session", fake_open)
    rec = tmp_path / "feed.recording.yaml"
    result = runner.invoke(app, ["flows", "capture", str(rec), "--on", "acme/linkedin", "--goto",
                                 "https://www.linkedin.example/feed/", "-p", "text=Fresh rye today"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["flows", "capture", str(rec), "--on", "acme/linkedin", "--after",
                                 'click button "Post"'])
    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls] == ["browser_navigate", "browser_snapshot", "browser_snapshot"]
    data = load_yaml(rec.read_text(), "rec")
    first, second = data["pages"]
    assert first == {"url": "https://www.linkedin.example/feed/", "title": "Feed",
                     "outline": '- button "Post"\n- paragraph: ${text}\n'}
    assert second["after"] == 'click button "Post"' and "Fresh rye today" in second["outline"]
    assert flowcli.outline_of(snapshot)["outline"].count("[ref=") == 0
