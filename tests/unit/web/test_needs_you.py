"""One count of what needs a person, as the node says it (`/api/status`) and as Needs you shows it.

The status count is the sum of its parts: what to decide, what did not go through after a yes, accounts
that need a hand (an agent's login request among them), login codes and runs that stopped halfway. Each run counts once. The web app's count.ts sums the same parts
from the lists, so the rail, the status pill and the Needs you header agree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentos.core.models import RunStatus
from agentos.node.login_assist import service_for
from agentos.runner.models import Run

LANE = "biz-a/social"


def _consume(node) -> str:
    """Marks the first pending action the seed holds as said yes and used, and returns its id."""
    store = node.require("approvals")
    pending = [row for row in store.db.execute("SELECT id FROM approvals WHERE state = 'pending' "
                                               "AND kind != 'question' LIMIT 1").fetchall()]
    approval_id = pending[0][0]
    now = datetime.now(UTC).timestamp()
    store.db.execute("UPDATE approvals SET state = 'consumed', decided_by = 'owner', decided_at = ?, "
                     "consumed_at = ? WHERE id = ?", (now, now, approval_id))
    return approval_id


def _run(node, approval_id: str, status: RunStatus, *, decision_needed: bool = False) -> str:
    now = datetime.now(UTC)
    run = Run(id=f"run_{status.value}_{approval_id[-6:]}", lane=LANE, flow_id="biz-a.social.post", status=status,
              approval_id=approval_id, finished_at=now, created_at=now - timedelta(minutes=1),
              error={"code": "element_not_found", "message": "the Post button was not on the page"}
              if status == RunStatus.FAILED else None,
              decision={"needed": "Did the post go through?"} if decision_needed else {})
    node.require("runner").store.insert_run(run)
    return run.id


def test_status_counts_a_yes_whose_run_failed(client, node) -> None:
    before = client.get("/api/status").json()
    approval_id = _consume(node)
    _run(node, approval_id, RunStatus.FAILED)
    status = client.get("/api/status").json()
    detail = status["needs_you_detail"]
    assert detail["outcomes"] == 1 and detail["runs"] == 0 and "plans" not in detail
    # the pending yes left the count when it was decided; its failed run came back into it
    assert status["needs_you_all"] == before["needs_you_all"]
    # the older count is what it always was: the decided yes left it, and it does not list failed runs
    assert status["needs_you"] == before["needs_you"] - 1


def test_a_run_stopped_halfway_is_counted_once(client, node) -> None:
    before = client.get("/api/status").json()
    approval_id = _consume(node)
    _run(node, approval_id, RunStatus.PARTIAL, decision_needed=True)
    status = client.get("/api/status").json()
    detail = status["needs_you_detail"]
    # The run is also an approved action that did not go through; it is one thing, in the stopped count.
    assert detail["runs"] == 1 and detail["outcomes"] == 0
    assert status["needs_you_all"] == before["needs_you_all"]


def test_an_agents_login_request_is_a_hand_and_counted(client, node) -> None:
    before = client.get("/api/status").json()
    lane = client.get(f"/api/lanes/{LANE.split('/')[0]}/{LANE.split('/')[1]}").json()
    assert lane["login_request"] is None
    service_for(node).request(LANE, why="Social signed this lane out.", by="claude-code")
    lane = client.get(f"/api/lanes/{LANE.split('/')[0]}/{LANE.split('/')[1]}").json()
    assert lane["login_request"]["why"] == "Social signed this lane out."
    assert lane["login_request"]["by"] == "claude-code" and lane["login_request"]["at"]
    status = client.get("/api/status").json()
    assert LANE in status["needs_you_detail"]["logins"]
    assert LANE in status["needs_you_detail"]["hands"]
    assert status["needs_you_all"] == before["needs_you_all"] + 1
    # the older count has no card for a login request, so it and its list stay as they were
    assert LANE not in status["needs_you_detail"]["lanes"] and status["needs_you"] == before["needs_you"]
    listed = {item["id"]: item for item in client.get("/api/lanes").json()}
    assert listed[LANE]["login_request"]["why"] == "Social signed this lane out."


def test_a_login_request_leaves_the_count_when_a_login_starts(client, node) -> None:
    service_for(node).request(LANE, why="Social signed this lane out.", by="claude-code")
    assert LANE in client.get("/api/status").json()["needs_you_detail"]["logins"]
    assert service_for(node).requests().get(LANE) is not None
    service_for(node)._requests.pop(LANE, None)  # what a started login does
    assert LANE not in client.get("/api/status").json()["needs_you_detail"]["logins"]


def test_the_status_parts_are_all_there(client) -> None:
    detail = client.get("/api/status").json()["needs_you_detail"]
    assert {"approvals", "questions", "codes", "lanes", "hands", "outcomes", "runs", "logins"} <= set(detail)
