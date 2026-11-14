"""Computers on a node: this one computer, as the app's Computers pages read it (web/src/api/types.ts).

Each route is called as the app calls it, `/api/orgs/local/...`, and a refusal is checked as the app
sees it: the status and the envelope it reads.
"""

from __future__ import annotations

from agentos import __version__
from agentos.api.web.computers import FLEET_NODE

NODE = "dev"


def test_the_list_is_one_row_this_node(client) -> None:
    [row] = client.get("/api/orgs/local/nodes").json()
    assert set(FLEET_NODE) <= row.keys()
    assert row["id"] == NODE
    assert row["version"] == __version__
    assert row["state"] in ("up", "degraded")
    assert isinstance(row["degraded"], bool)
    assert "lanes" not in row  # the list is the fleet row, not the detail


def test_one_node_is_this_node_in_detail(client) -> None:
    node = client.get(f"/api/orgs/local/nodes/{NODE}").json()
    assert node["id"] == NODE
    assert node["os"] and node["arch"]
    assert set(node["load"]) == {"cpu_pct", "mem_used_mb", "mem_total_mb", "disk_free_mb"}
    assert node["lanes"], "the seeded node has lanes, and they are listed"
    lane = node["lanes"][0]
    assert lane["id"].count("/") == 1 and lane["node"] == NODE
    assert lane["account"] is None and lane["signal_detail"] is None


def test_a_frozen_account_reads_frozen_on_the_computer_as_on_accounts(client) -> None:
    # The computer's rows and Accounts read the same lane, so they must agree on its freeze and its check.
    freeze = {"scope": "lane", "target": "biz-a/social", "reason": "a login we did not expect"}
    assert client.post("/api/orgs/local/freeze", json=freeze).status_code == 200
    lanes = {lane["id"]: lane for lane in client.get(f"/api/orgs/local/nodes/{NODE}").json()["lanes"]}
    accounts = {lane["id"]: lane for ws in client.get("/api/orgs/local/workspaces").json() for lane in ws["lanes"]}
    assert lanes["biz-a/social"]["frozen"] is True and accounts["biz-a/social"]["frozen"] is True
    assert lanes["biz-a/shop"]["frozen"] is False
    for lane_id, lane in lanes.items():
        assert lane["check"] == accounts[lane_id]["check"], lane_id
        assert lane["login_request"] == accounts[lane_id].get("login_request"), lane_id


def test_another_computer_is_not_found(client) -> None:
    response = client.get("/api/orgs/local/nodes/elsewhere")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_system_is_what_the_node_reads_and_never_enrolled(client) -> None:
    system = client.get(f"/api/orgs/local/nodes/{NODE}/system").json()
    assert system["version"] == __version__
    assert system["protocol"] == 1
    assert system["os"] and system["arch"]
    assert system["enrolled_at"] is None and system["enrolled_by"] is None
    assert system["reported_at"].endswith("Z")


def test_programs_name_agentos_itself(client) -> None:
    programs = client.get(f"/api/orgs/local/nodes/{NODE}/programs").json()
    assert isinstance(programs, list)
    [agentos] = [item for item in programs if item["name"] == "agentos"]
    assert agentos["scope"] == "node"
    assert agentos["version"] == __version__
    assert agentos["pinned_hash"] is None


def test_services_show_agentosd_running(client) -> None:
    services = client.get(f"/api/orgs/local/nodes/{NODE}/services").json()
    [daemon] = [item for item in services if item["name"] == "agentosd"]
    assert daemon["state"] == "running"
    assert daemon["cpu_pct"] is None and daemon["mem_mb"] is None


def test_health_is_the_doctor_checklist(client) -> None:
    checks = client.get(f"/api/orgs/local/nodes/{NODE}/health").json()
    assert checks and all(check["status"] in ("ok", "warn", "fail") for check in checks)
    assert all(isinstance(check["detail"], str) and check["at"] for check in checks)


def test_connections_are_not_reported_on_a_node(client) -> None:
    assert client.get(f"/api/orgs/local/nodes/{NODE}/connections?range=30d").json() is None


def test_metrics_have_no_points_because_none_are_kept(client) -> None:
    metrics = client.get(f"/api/orgs/local/nodes/{NODE}/metrics?range=1h").json()
    assert metrics == {"range": "1h", "bucket_seconds": 60, "points": []}


def test_metrics_refuse_an_unknown_range(client) -> None:
    response = client.get(f"/api/orgs/local/nodes/{NODE}/metrics?range=1y")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid"


def test_join_codes_are_an_empty_list(client) -> None:
    assert client.get("/api/orgs/local/nodes/join-tokens").json() == []


def test_a_join_code_is_refused_with_the_reason(client) -> None:
    response = client.post("/api/orgs/local/nodes/join-tokens", json={"name": "mac-mini"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert "runs on its own" in error["message"]


def test_a_join_code_to_revoke_is_not_found(client) -> None:
    response = client.delete("/api/orgs/local/nodes/join-tokens/nope")
    assert response.status_code == 404


def test_this_computer_cannot_be_removed_from_itself(client) -> None:
    response = client.delete(f"/api/orgs/local/nodes/{NODE}")
    assert response.status_code == 409
    assert "cannot be removed" in response.json()["error"]["message"]


def test_schedules_say_when_they_fire_and_what_ran(client) -> None:
    schedules = client.get(f"/api/orgs/local/nodes/{NODE}/schedules").json()
    assert isinstance(schedules, list)
    for schedule in schedules:
        assert schedule["id"] and schedule["flow"] is not None
        assert isinstance(schedule["recent_fires"], list) and isinstance(schedule["next_fires"], list)
        assert all(
            fire["at"].endswith("Z")
            for fire in schedule["recent_fires"] + [{"at": t} for t in schedule["next_fires"]]
        )


def test_tools_are_the_ones_the_node_offers_agents(client) -> None:
    tools = client.get(f"/api/orgs/local/nodes/{NODE}/tools").json()
    assert len(tools) > 10
    names = [tool["name"] for tool in tools]
    assert len(names) == len(set(names))
    assert all(tool["effect"] for tool in tools)


def test_the_log_streams_live_lines_as_events(client) -> None:
    with client.stream("GET", f"/api/orgs/local/nodes/{NODE}/logs/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    assert body == "" or "event: log" in body or ": keep-alive" in body


def test_the_log_stream_refuses_a_source_it_does_not_know(client) -> None:
    response = client.get(f"/api/orgs/local/nodes/{NODE}/logs/stream?source=nowhere")
    assert response.status_code == 400


def test_topology_is_this_computer_with_its_workspaces_and_lanes(client) -> None:
    topology = client.get("/api/orgs/local/topology?window=1h").json()
    assert topology["window_seconds"] == 3600
    types = {entity["type"] for entity in topology["entities"]}
    assert {"node", "workspace", "lane"} <= types
    lane_ids = {entity["id"] for entity in topology["entities"] if entity["type"] == "lane"}
    assert all(
        link["to"] in {f"node:{NODE}"} | {e["id"] for e in topology["entities"] if e["type"] == "workspace"}
        for link in topology["links"]
        if link["from"] in lane_ids
    )
    assert isinstance(topology["active"], list) and isinstance(topology["recent"], list)


def test_topology_refuses_an_unknown_window(client) -> None:
    assert client.get("/api/orgs/local/topology?window=1y").status_code == 400


def test_topology_events_are_calls_in_the_range(client) -> None:
    events = client.get(
        "/api/orgs/local/topology/events?from=2020-01-01T00:00:00Z&to=2030-01-01T00:00:00Z"
    ).json()
    assert isinstance(events, list)
    assert all({"at", "lane", "principal", "tool", "effect", "status"} <= event.keys() for event in events)


def test_topology_events_need_a_range_that_reads(client) -> None:
    assert client.get("/api/orgs/local/topology/events?from=yesterday&to=now").status_code == 400


def test_activity_is_whole_buckets_that_add_up(client) -> None:
    metrics = client.get("/api/orgs/local/metrics/activity?range=24h").json()
    assert metrics["range"] == "24h" and metrics["bucket_seconds"] == 3600
    assert len(metrics["buckets"]) == 24
    assert metrics["totals"]["events"] == sum(bucket["events"] for bucket in metrics["buckets"])
    assert set(metrics["buckets"][0]["runs"]) == {
        "succeeded_verified",
        "succeeded_unverified",
        "partial",
        "failed",
    }


def test_activity_refuses_an_unknown_range(client) -> None:
    assert client.get("/api/orgs/local/metrics/activity?range=1y").status_code == 400


def test_a_schedule_shows_its_next_slots_and_what_it_ran(client) -> None:
    flows = client.get("/api/flows").json()
    assert flows, "the seeded node has routines to schedule"
    created = client.post(
        "/api/orgs/local/schedules",
        json={
            "flow": flows[0]["id"],
            "cron": "0 9 * * *",
            "params": {"caption": "Hello", "image": "photo.png"},
        },
    )
    assert created.status_code in (200, 201), created.text
    schedules = client.get(f"/api/orgs/local/nodes/{NODE}/schedules").json()
    [row] = [item for item in schedules if item["id"] == created.json()["id"]]
    assert len(row["next_fires"]) == 3
    assert row["next_fires"] == sorted(row["next_fires"]) and row["next_fires"][0].endswith("Z")
    assert row["recent_fires"] == [] and row["last_run"] is None
    assert row["cron"] == "0 9 * * *"


def test_next_fires_are_planned_slots_in_utc(client) -> None:
    from agentos.api.routes_computers import next_fires

    slots = next_fires("30 8 * * *", "Europe/Vilnius")
    assert len(slots) == 3 and all(slot.endswith("Z") for slot in slots)
    assert next_fires("not a cron", None) == []
    assert next_fires(None, None) == []


def test_a_call_on_this_computer_is_on_the_map_and_in_activity(client, node) -> None:
    from agentos.policy.traces import Cost, TraceEvent

    traces = node.get("traces")
    start = TraceEvent(
        kind="tool_start",
        tool="shop.read",
        lane="biz-a/shop",
        effect="read",
        status="started",
        principal="agent-1",
        principal_kind="agent",
    )
    traces.write(start)
    traces.write(
        TraceEvent(
            kind="tool_end",
            tool="shop.read",
            lane="biz-a/shop",
            effect="read",
            status="ok",
            principal="agent-1",
            principal_kind="agent",
            parent_id=start.id,
            cost=Cost(tokens=42, usd=0.01),
        )
    )
    traces.flush()

    events = client.get(
        "/api/orgs/local/topology/events?from=2020-01-01T00:00:00Z&to=2030-01-01T00:00:00Z"
    ).json()
    [call] = [event for event in events if event["tool"] == "shop.read"]
    assert call["lane"] == "biz-a/shop" and call["status"] == "succeeded_unverified"

    topology = client.get("/api/orgs/local/topology?window=1h").json()
    link = next(
        item for item in topology["links"] if item["from"] == "lane:biz-a/shop" and item["kind"] == "runs_on"
    )
    assert link["calls"] == 1 and link["failed"] == 0 and link["last_at"]
    assert any(item["tool"] == "shop.read" for item in topology["recent"])

    metrics = client.get("/api/orgs/local/metrics/activity?range=24h&lane=biz-a/shop").json()
    assert metrics["totals"]["events"] == 1
    assert metrics["totals"]["cost"]["tokens"] == 42
