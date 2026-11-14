"""An unknown proxy label is refused without listing the labels that exist.

A grant that covers one workspace must not learn the proxy labels of the others. So the error for an
unknown label names only the label that was asked for, says it is not configured, and points the owner
at the list that only a person runs. These tests cover the three ways to hit that error: a plan, an
update of a workspace's default proxy, and the workspace_plan tool under a grant for one workspace.
Runs against an in-memory lane manager. No browser, no proxy."""

from __future__ import annotations

import pytest

from agentos.core.errors import InvalidRequest
from agentos.workspaces import tools as ws_tools
from agentos.workspaces.fakes import FakeCtx, Harness

# The labels the Harness has by default. An error that names any of them leaks the node's labels.
EXISTING_LABELS = ("lt1", "lt2")


def assert_hides_the_labels(message: str) -> None:
    assert "'lt9' is not configured" in message, message
    for label in EXISTING_LABELS:
        assert label not in message, f"the error names proxy label {label!r}: {message}"
    assert "\u2014" not in message, message


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path / "home")
    yield harness
    harness.close()


def test_plan_with_an_unknown_label_names_no_label(h):
    h.service.create_workspace("acme")
    empty = Harness(h.home / "empty", proxies=())
    try:
        empty.service.create_workspace("acme")
        with pytest.raises(InvalidRequest, match="no proxies are set up on this node yet"):
            empty.service.plan("social-6", ["acme"], proxy_per_workspace={"acme": "lt9"})
    finally:
        empty.close()
    with pytest.raises(InvalidRequest) as refused:
        h.service.plan("social-6", ["acme"], proxy_per_workspace={"acme": "lt9"})
    assert_hides_the_labels(str(refused.value))


def test_update_default_proxy_with_an_unknown_label_names_no_label(h):
    h.service.create_workspace("acme")
    with pytest.raises(InvalidRequest) as refused:
        h.service.update_workspace("acme", {"default_proxy": "lt9"})
    assert_hides_the_labels(str(refused.value))


async def test_workspace_plan_under_a_grant_for_one_workspace_names_no_label(h):
    h.service.create_workspace("biz-a")
    h.service.create_workspace("biz-b")
    ctx = FakeCtx(h.runtime, allowed=lambda lane_id: lane_id.startswith("biz-a/"))
    with pytest.raises(InvalidRequest) as refused:
        await ws_tools.workspace_plan(ctx, template="social-6", workspaces=["biz-a"],
                                      proxy_per_workspace={"biz-a": "lt9"})
    assert_hides_the_labels(str(refused.value))
