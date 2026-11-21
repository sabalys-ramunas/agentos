#!/usr/bin/env python3
"""docs/PROTOCOL.md's message examples, generated from tests/fixtures/protocol/messages.json.

  scripts/protocol_doc.py            render the fixture into the doc (between the markers)
  scripts/protocol_doc.py --check    exit 1 when the doc is not what the fixture renders (CI)
  scripts/protocol_doc.py --record   record a new fixture from a real session first
                                     (tests/unit/protocol_capture.py), then render

`make protocol-doc` records and renders. tests/unit/test_protocol_doc.py fails when the code's
messages no longer have the fixture's shape, so the doc cannot drift from the wire.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "PROTOCOL.md"
FIXTURE = ROOT / "tests" / "fixtures" / "protocol" / "messages.json"
ENVELOPE_KEY = "node_to_controller event approval"
MARKERS = {
    "envelope": ("<!-- protocol-envelope:start (generated: make protocol-doc) -->", "<!-- protocol-envelope:end -->"),
    "examples": ("<!-- protocol-examples:start (generated: make protocol-doc) -->", "<!-- protocol-examples:end -->"),
}
SIDES = {"node_to_controller": "Node to controller", "controller_to_node": "Controller to node"}
VARIANTS = {
    "result image": "with an image (the bytes follow on a pushed stream)",
    "result approval_pending": "waiting for a human's approval",
    "mesh_freeze on": "freeze",
    "mesh_freeze off": "lift",
}


def label(example: dict) -> str:
    side = SIDES.get(example["direction"], example["direction"])
    rest = example["key"].split(" ", 1)[1]
    kind = example["type"]
    if rest in VARIANTS:
        return f"{side}: `{kind}`, {VARIANTS[rest]}"
    extra = rest[len(kind):].strip()
    if kind == "event" and extra.startswith("node."):
        return f"{side}: `event`, kind `node` ({extra[5:].replace('+', ', ')} report)"
    if kind == "event":
        return f"{side}: `event`, kind `{extra}`"
    if kind == "error":
        return f"{side}: `error`, code `{extra}`"
    return f"{side}: `{kind}`"


def block(envelope: dict) -> str:
    return "```json\n" + json.dumps(envelope, indent=2, ensure_ascii=False) + "\n```"


def render_examples(examples: list[dict]) -> str:
    lines = [
        "One message of each kind, recorded from a real session between the open-source controller and a",
        "node (tests/unit/protocol_capture.py), in the order a connection sees them. Ids, keys, signatures,",
        "tokens and times are replaced by placeholders, and long text and lists are cut (`...`, two items at",
        "most), so read the fields, not the values. Generated: `make protocol-doc` records the session and",
        "rewrites this section, and CI fails when the code's messages change shape or this section is stale.",
        "",
    ]
    for example in examples:
        lines += [f"#### {label(example)}", "", block(example["envelope"]), ""]
    return "\n".join(lines).rstrip() + "\n"


def render(doc: str, examples: list[dict]) -> str:
    by_key = {e["key"]: e for e in examples}
    parts = {"examples": render_examples(examples)}
    if ENVELOPE_KEY in by_key:
        parts["envelope"] = block(by_key[ENVELOPE_KEY]["envelope"]) + "\n"
    for name, body in parts.items():
        start, end = MARKERS[name]
        pattern = re.compile(re.escape(start) + r"\n.*?" + re.escape(end), re.DOTALL)
        if not pattern.search(doc):
            raise SystemExit(f"docs/PROTOCOL.md has no {start} ... {end} markers")
        doc = pattern.sub(lambda _m, s=start, b=body, e=end: f"{s}\n{b}{e}", doc)
    return doc


def record() -> None:
    sys.path.insert(0, str(ROOT / "tests" / "unit"))
    import protocol_capture as capture

    with tempfile.TemporaryDirectory(prefix="openajar-protocol-") as home:
        seen = asyncio.run(capture.session(Path(home)))
    capture.write_fixture(capture.select(seen))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the doc is stale")
    parser.add_argument("--record", action="store_true", help="record a new fixture first")
    args = parser.parse_args()
    if args.record:
        record()
    examples = json.loads(FIXTURE.read_text())["messages"]
    doc = DOC.read_text()
    new = render(doc, examples)
    if args.check:
        if new != doc:
            print("docs/PROTOCOL.md's examples are stale: make protocol-doc", file=sys.stderr)
            return 1
        return 0
    if new != doc:
        DOC.write_text(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
