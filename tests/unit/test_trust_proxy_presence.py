"""A proxy label is part of every account that leaves through it, so changing where it points, or
moving a lane onto another exit, needs a person (docs/THREAT-MODEL.md, "A person is present").

The node token, an admin grant and the owner's device credential are all credentials an agent can
hold. Each may still add a brand-new label (no lane uses it yet, so no account changes) and make a
device on a proxy nothing else uses. None of them may point an existing label at another host, port
or credentials, move a lane to another device, or make a device beside lanes that already leave
through its proxy. The owner signed in to the panel, and the node's own CLI with its one-time code,
still do all of it.
"""

from __future__ import annotations

import asyncio

import pytest
from test_node_panel import TOKEN, Node, bearer
from test_presence import Device, Token, refused

from openajar.api.auth import PRESENCE_HEADER
from openajar.core.config import load_proxies
from openajar.core.models import LaneSpec

DE1 = {"label": "de1", "host": "10.0.0.2", "port": 8080, "username": "u", "password": "p1"}


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    yield n
    n.close()


def callers(node: Node) -> dict:
    return {"node_token": Token(node, TOKEN), "device": Device(node),
            "admin_grant": Token(node, node.grant(subject={"id": "boss-agent"}, admin=True))}


def host_of(node: Node, label: str) -> tuple:
    p = load_proxies(node.config)[label]
    return p.host, p.port, p.username, p.password


@pytest.mark.parametrize("who", ["node_token", "device", "admin_grant"])
def test_below_a_person_a_new_label_is_fine_and_changing_one_is_refused(node, who):
    caller = callers(node)[who]
    r = caller.req("POST", "/api/proxies", {**DE1, "label": f"new-{who.replace('_', '-')}"})
    assert r.status_code == 200, r.text  # a label no lane uses: no account changes
    c = node.client()
    h = node.sign_in(c)
    assert c.post("/api/proxies", json=DE1, headers=h).status_code == 200
    assert caller.req("POST", "/api/proxies", DE1).status_code == 200  # the same values change nothing
    for change in ({"host": "198.51.100.7"}, {"port": 9090}, {"username": "other"}, {"password": "p2"}):
        refused(caller.req("POST", "/api/proxies", {**DE1, **change, "confirm": True}))
    assert host_of(node, "de1") == ("10.0.0.2", 8080, "u", "p1")


def test_a_person_still_points_a_label_elsewhere(node):
    c = node.client()
    h = node.sign_in(c)
    assert c.post("/api/proxies", json=DE1, headers=h).status_code == 200
    moved = {**DE1, "host": "198.51.100.7"}
    r = c.post("/api/proxies", json=moved, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "confirm_required", r.text
    assert c.post("/api/proxies", json={**moved, "confirm": True}, headers=h).status_code == 200
    assert host_of(node, "de1")[0] == "198.51.100.7"


def test_the_nodes_cli_changes_a_label_with_its_one_time_code(node):
    """`openajar proxy add` on the node writes proxies.json itself; a CLI that goes through agentosd
    sends the one-time code from panel.db beside the node token, and that is a person."""
    c = node.client()
    assert c.post("/api/proxies", json=DE1, headers=bearer(TOKEN)).status_code == 200
    node.store.add_code("cli-proxy-code", ttl_seconds=60)
    r = c.post("/api/proxies", json={**DE1, "password": "p2"},
               headers={**bearer(TOKEN), PRESENCE_HEADER: "cli-proxy-code"})
    assert r.status_code == 200, r.text
    assert host_of(node, "de1")[3] == "p2"


@pytest.mark.parametrize("who", ["node_token", "device", "admin_grant"])
def test_below_a_person_no_lane_moves_to_another_device(node, who):
    caller = callers(node)[who]
    c = node.client()
    r = c.post("/api/browser-devices", json={"name": "biz-a-pc", "proxy_label": "lt1"}, headers=bearer(TOKEN))
    assert r.status_code == 200, r.text  # a device on a proxy nothing else uses
    refused(caller.req("POST", "/api/lanes/biz-a/instagram/device", {"device": "biz-a-pc", "confirm": True}))
    assert node.lanes.get("biz-a/instagram").spec.device is None
    h = node.sign_in(c)
    r = c.post("/api/lanes/biz-a/instagram/device", json={"device": "biz-a-pc", "confirm": True}, headers=h)
    assert r.status_code == 200, r.text
    assert node.lanes.get("biz-a/instagram").spec.device == "biz-a-pc"


@pytest.mark.parametrize("who", ["node_token", "device", "admin_grant"])
def test_below_a_person_no_device_joins_the_exit_of_lanes_of_their_own(node, who):
    """--yes on `openajar device create` puts a second computer on the IP of accounts that already
    leave through it: the sites see an office they never saw. That is a person's call."""
    asyncio.run(node.lanes.create(LaneSpec(id="biz-a/facebook", proxy_label="lt1")))
    caller = callers(node)[who]
    body = {"name": "biz-a-pc", "proxy_label": "lt1", "confirm": True}
    refused(caller.req("POST", "/api/browser-devices", body))
    c = node.client()
    assert c.get("/api/browser-devices", headers=bearer(TOKEN)).json() == []
    h = node.sign_in(c)
    assert c.post("/api/browser-devices", json=body, headers=h).status_code == 200


@pytest.mark.parametrize("who", ["node_token", "device", "admin_grant"])
def test_below_a_person_a_new_lane_never_shares_an_accounts_exit(node, who):
    """A second browser on the IP of an account that already leaves through it is a new computer in
    that account's office. A lane on a label nothing uses stays open to the owner's token."""
    c = node.client()
    for label in ("de1", "de2"):
        assert c.post("/api/proxies", json={**DE1, "label": label}, headers=bearer(TOKEN)).status_code == 200
    asyncio.run(node.lanes.create(LaneSpec(id="biz-a/facebook", proxy_label="de1")))
    caller = callers(node)[who]
    refused(caller.req("POST", "/api/lanes", {"id": "biz-b/reddit", "proxy_label": "de1"}))
    assert "biz-b/reddit" not in {i.spec.id for i in node.lanes.list()}
    assert caller.req("POST", "/api/lanes", {"id": "biz-b/medium", "proxy_label": "de2"}).status_code == 200
    h = node.sign_in(c)
    assert c.post("/api/lanes", json={"id": "biz-b/reddit", "proxy_label": "de1"}, headers=h).status_code == 200


@pytest.mark.parametrize("who", ["node_token", "device", "admin_grant"])
def test_below_a_person_no_template_or_device_lane_lands_behind_an_accounts_exit(node, who):
    """The same rule for a template's lanes and for a lane made on a device (its proxy is the device's)."""
    c = node.client()
    for label in ("de1", "de2"):
        assert c.post("/api/proxies", json={**DE1, "label": label}, headers=bearer(TOKEN)).status_code == 200
    asyncio.run(node.lanes.create(LaneSpec(id="biz-a/facebook", proxy_label="de1")))
    caller = callers(node)[who]
    refused(caller.req("POST", "/api/templates/apply", {"template": "social-6", "workspaces": ["biz-a"],
                                                        "proxy_per_workspace": {"biz-a": "de1"}}))
    assert c.post("/api/browser-devices", json={"name": "biz-a-pc", "proxy_label": "de2"},
                  headers=bearer(TOKEN)).status_code == 200
    assert caller.req("POST", "/api/lanes", {"id": "biz-a/medium", "device": "biz-a-pc"}).status_code == 200
    refused(caller.req("POST", "/api/lanes", {"id": "biz-a/blog", "device": "biz-a-pc"}))
    assert {i.spec.id for i in node.lanes.list()} & {"biz-a/reddit", "biz-a/blog"} == set()
