"""docs/PROTOCOL.md's examples come from a recorded session (tests/fixtures/protocol/messages.json):
the doc must show the fixture, and a new session must still have the fixture's shape."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import protocol_capture as capture

ROOT = Path(__file__).resolve().parents[2]


def _doc_tool():
    spec = importlib.util.spec_from_file_location("protocol_doc", ROOT / "scripts" / "protocol_doc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_doc_shows_the_fixture():
    tool = _doc_tool()
    doc = tool.DOC.read_text()
    assert tool.render(doc, capture.load_fixture()) == doc, "docs/PROTOCOL.md is stale: make protocol-doc"


def test_the_fixture_covers_every_message_of_the_session_and_hides_secrets():
    examples = capture.load_fixture()
    keys = [e["key"] for e in examples]
    assert len(keys) == len(set(keys))
    for want in ("controller_to_node challenge", "node_to_controller hello", "controller_to_node invoke",
                 "node_to_controller result", "node_to_controller approval_request", "controller_to_node revoke",
                 "controller_to_node lane_control", "controller_to_node mesh_freeze on"):
        assert want in keys
    text = capture.FIXTURE.read_text()
    assert "agt2_<per-call grant token>" in text and "<base64url Ed25519 signature>" in text
    assert "darwin" not in text and "/Users/" not in text and "/tmp/" not in text


async def test_a_new_session_still_has_the_fixtures_shape(tmp_path):
    """Fails when the node or the controller changes a message: re-record with make protocol-doc
    (and read the diff of docs/PROTOCOL.md: it is the protocol change)."""
    recorded = {e["key"]: e for e in capture.select(await capture.session(tmp_path))}
    fixture = {e["key"]: e for e in capture.load_fixture()}
    problems = [f"{key}: not sent any more" for key in fixture if key not in recorded]
    problems += [f"{key}: new message (make protocol-doc)" for key in recorded if key not in fixture]
    for key in fixture.keys() & recorded.keys():
        problems += [f"{key} {p}" for p in capture.shape_problems(capture.shape(fixture[key]["envelope"]),
                                                                 capture.shape(recorded[key]["envelope"]))]
    assert problems == [], "\n".join(problems)


def test_shape_problems_name_what_changed():
    want = capture.shape({"a": 1, "b": {"c": "x"}, "d": [{"e": True}], "n": None})
    assert capture.shape_problems(want, capture.shape({"a": 2.5, "b": {"c": "y"}, "d": [], "n": 3})) == []
    got = capture.shape({"a": "1", "b": {}, "d": [{"e": True, "f": 1}], "n": None})
    assert capture.shape_problems(want, got) == [".a: int is now str", ".b.c: gone", ".d[0].f: new (re-record: make protocol-doc)"]
