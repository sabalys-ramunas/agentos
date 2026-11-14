"""An item that could never run (QA-2 #10): a comment under a comment cap of 0, or an item for a lane
the lane manager does not have. Both were queued (`queued`, no warnings) and sat until they expired
(7 to 30 days), with only a history line. Each is refused when it is submitted, with nothing queued,
and an item that waits when its cap drops to 0 or its lane goes away is ended with a notice."""

from __future__ import annotations

import pytest
from test_engage_items import OWNER, OWNER_P, POST_URL, RD, Ctx, Rig, call

from agentos.core.errors import InvalidRequest, LaneNotFound
from agentos.core.runtime import Runtime
from agentos.engage import tools
from agentos.engage.service import lane_gone_check


class LanesWithout:
    """A lane manager (the node's `lanes`, read through get and list) that has other lanes but not the
    surface's: get raises LaneNotFound for it, as the real manager does."""

    def __init__(self, *known: str):
        self.known = set(known)

    def get(self, lane_id: str):
        if lane_id not in self.known:
            raise LaneNotFound(f"no lane named {lane_id}. Create it with: agentos lane create {lane_id}")
        return {"id": lane_id}

    def list(self):
        return [{"id": lane_id} for lane_id in sorted(self.known)]


def refusal(fn, ctx, **kw) -> InvalidRequest:
    """The InvalidRequest the tool raises. If it accepts the item instead, fail and say what it queued."""
    try:
        out = call(fn, ctx, **kw)
    except InvalidRequest as e:
        return e
    pytest.fail(f"accepted, not refused: status {out['status']!r}, warnings {out['warnings']}")


def test_a_comment_under_a_comment_cap_of_zero_is_refused_at_submit(tmp_path):
    rig = Rig(tmp_path)
    rig.svc.set_cap(RD, "comment", 0, by=OWNER)            # the owner stopped comments on this account
    ctx = Ctx(rig.runtime, OWNER_P, in_person=True)
    err = refusal(tools.engage_item_submit, ctx, surface=RD, kind="comment", text="Nice latte art",
                  targets=[POST_URL])
    assert "cap" in str(err) and "nothing was queued" in str(err) and "\u2014" not in str(err)
    assert rig.svc.store.items(RD) == []


def test_an_item_for_a_lane_that_is_gone_is_refused_at_submit(tmp_path):
    rig = Rig(tmp_path)
    rig.inbox.lane_gone = lane_gone_check(Runtime(lanes=LanesWithout("biz-b/reddit")))  # no lane for RD
    ctx = Ctx(rig.runtime, OWNER_P, in_person=True)
    err = refusal(tools.engage_item_submit, ctx, surface=RD, kind="post", text="Fresh beans today",
                  title="Fresh beans", targets=["r/coffee"])
    assert "lane" in str(err) and "nothing was queued" in str(err)
    assert rig.svc.store.items(RD) == []


def test_a_waiting_item_whose_cap_drops_to_zero_ends_with_a_notice(tmp_path):
    rig = Rig(tmp_path)
    item = rig.add("comment", "Nice latte art")
    other = rig.add("post", "Fresh beans today", title="Fresh beans")
    rig.svc.set_cap(RD, "comment", 0, by=OWNER)
    out = rig.inbox.sweep()
    assert out["ended"] == [item["id"]], out
    assert rig.inbox.get(item["id"])["status"] == "withdrawn"
    assert rig.inbox.get(other["id"])["status"] == "queued"
    notice = rig.svc.store.notices(RD)[0]
    assert notice["kind"] == "item_ended" and item["id"] in notice["text"] and "cap" in notice["text"]
    assert rig.inbox.sweep()["ended"] == []  # once


def test_a_waiting_item_whose_lane_goes_away_ends_with_a_notice(tmp_path):
    rig = Rig(tmp_path)
    item = rig.add("post", "Fresh beans today", title="Fresh beans")
    rig.inbox.lane_gone = lane_gone_check(Runtime(lanes=LanesWithout("biz-b/reddit")))
    out = rig.inbox.sweep()
    assert out["ended"] == [item["id"]], out
    assert rig.inbox.get(item["id"])["status"] == "withdrawn"
    assert "lane" in rig.svc.store.notices(RD)[0]["text"]

