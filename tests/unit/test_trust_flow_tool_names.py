"""A saved flow may not take a tool name that another flow or a static tool already owns.

A flow's MCP tool name comes from its id: 'biz-a.instagram.post' becomes 'flow_biz_a_instagram_post'.
Two ids can map to the same name, and today the sort order decides who keeps the plain name. An agent
whose grant covers workspace biz-b can save 'biz-a.instagram-post' on lane 'biz-b/instagram'. That id
sorts first, so it takes the plain name from biz-a's own flow, and biz-a's flow drops to a hashed name.
An agent that calls 'flow_biz_a_instagram_post' then runs the biz-b flow. The fix refuses such a save
with a "tool name" error. A new version of a flow still saves, and so does a flow that serves no tool.
"""

import pytest

from openajar.core.errors import InvalidRequest, NotFound
from openajar.core.models import Principal
from openajar.runner.fakes import Harness
from openajar.runner.flows import STATIC_TOOL_NAMES, tool_name_for

OWNER = Principal(kind="human", id="owner", owner="owner")
AGENT_B = Principal(kind="agent", id="agent-biz-b", owner="owner")


def _flow(flow_id, lane, **kw):
    base = {"id": flow_id, "lane": lane, "steps": [{"type": "navigate", "url": "https://x.test/"}]}
    base.update(kw)
    return base


def test_a_new_id_cannot_take_another_flows_tool_name(tmp_path):
    h = Harness(tmp_path)
    h.flows.save(_flow("biz-a.instagram.post", "biz-a/instagram"), by=OWNER)

    with pytest.raises(InvalidRequest, match="tool name") as err:
        h.flows.save(_flow("biz-a.instagram-post", "biz-b/instagram"), by=AGENT_B)
    assert "biz-a.instagram.post" not in str(err.value)

    assert h.flows.tool_names()["biz-a.instagram.post"] == "flow_biz_a_instagram_post"
    with pytest.raises(NotFound):
        h.flows.get("biz-a.instagram-post")


def test_a_new_id_cannot_take_a_static_tool_name(tmp_path):
    assert tool_name_for("record.start") in STATIC_TOOL_NAMES  # the premise: 'flow_record_start'
    h = Harness(tmp_path)

    with pytest.raises(InvalidRequest, match="tool name"):
        h.flows.save(_flow("record.start", "biz-a/social"), by=AGENT_B)
    with pytest.raises(NotFound):
        h.flows.get("record.start")


def test_a_new_version_of_the_same_flow_still_saves(tmp_path):
    h = Harness(tmp_path)
    v1, _ = h.flows.save(_flow("biz-a.instagram.post", "biz-a/instagram"), by=OWNER)

    v2, created = h.flows.save(_flow("biz-a.instagram.post", "biz-a/instagram", title="Post v2"), by=OWNER)

    assert created and v2.version != v1.version
    assert h.flows.tool_names()["biz-a.instagram.post"] == "flow_biz_a_instagram_post"


def test_a_flow_that_serves_no_tool_may_use_a_colliding_id(tmp_path):
    h = Harness(tmp_path)
    h.flows.save(_flow("biz-a.instagram.post", "biz-a/instagram"), by=OWNER)

    _, created = h.flows.save(_flow("biz-a.instagram-post", "biz-b/instagram", expose_as_tool=False), by=AGENT_B)

    assert created
    assert h.flows.tool_names()["biz-a.instagram.post"] == "flow_biz_a_instagram_post"
