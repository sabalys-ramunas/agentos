"""connectors/reddit compiles strictly, with the commit request the canary saw on 2026-11-04."""

from __future__ import annotations

import re
from pathlib import Path

from agentos.node.egress import classify
from agentos.runner.connectors.compile import compile_path

ROOT = Path(__file__).resolve().parents[2] / "connectors" / "reddit"


def test_reddit_post_text_compiles_with_the_commit_gate():
    cc = compile_path(ROOT, commit_gate=True)
    op = cc.operations["post_text"]
    assert op.tool_name == "reddit_post_text" and op.effect == "commit" and not op.pending
    commit = cc.connector.operations["post_text"].commit
    assert commit is not None and commit.step == "post"
    # the commit request is the one the verified egress rule knows
    assert re.match(commit.request.url, "https://www.reddit.com/svc/shreddit/graphql")
    rule = classify("https://www.reddit.com/svc/shreddit/graphql", "POST", {"Content-Type": "application/json"},
                    body=b'{"operation": "CreatePost", "variables": {"input": {"title": "t"}}}')
    assert rule is not None and rule.id == "reddit.post"


def test_the_commit_waits_for_reddit_to_leave_the_composer():
    steps = compile_path(ROOT).connector.operations["post_text"].steps
    post = next(s for s in steps if s.get("label") == "post")
    cond = post["readback"]["condition"]
    assert cond["kind"] == "url"
    rx = re.compile(cond["matches"])
    assert not rx.match("https://www.reddit.com/r/test/submit/?type=TEXT")
    assert rx.match("https://www.reddit.com/r/test/comments/1demo01/third/")
    assert rx.match("https://www.reddit.com/r/test/")
