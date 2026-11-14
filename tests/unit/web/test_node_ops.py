"""This computer's environments, Web Bot identity and settings, as the app reads them (agentos.api.routes_ops).

A service here is a harmless `sleep` in a fake host, the same setup the env tests use. Nothing here
restarts the node or changes an account's real profile.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher
from agentos.webbotauth import keys, settings

LOCAL = "/api/orgs/local/nodes/dev"
TOKEN = "t" * 43


def as_token(client: TestClient) -> None:
    client.cookies.clear()
    client.headers.pop("x-agentos-csrf", None)
    client.headers["authorization"] = f"Bearer {TOKEN}"


@pytest.fixture
def envs(client: TestClient, tmp_path: Path) -> Iterator[EnvManager]:
    """The environment service, registered on this node the way the daemon registers it."""
    mgr = EnvManager(tmp_path / "envhome", host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0))
    client.app.state.api.runtime.register("envs", mgr)
    mgr.create("biz-a/term", kind="terminal", preset="sandbox")
    mgr.create("biz-a/svc", kind="service", preset="service",
               service={"command": ["/bin/sleep", "60"]})
    yield mgr
    asyncio.run(mgr.shutdown())


# ---- environments ---------------------------------------------------------------------------

def test_a_new_node_runs_no_environments_and_offers_its_images(client: TestClient) -> None:
    body = client.get(f"{LOCAL}/environments").json()
    assert body["lanes"] == [] and body["services"] == []
    [dev_box] = [t for t in body["images"]["templates"] if t["name"] == "dev-box"]
    assert dev_box["description"] and isinstance(dev_box["placeholder"], bool)
    assert body["images"]["built"] == []


def test_environments_list_terminals_services_and_images(client: TestClient, envs: EnvManager) -> None:
    body = client.get(f"{LOCAL}/environments").json()
    assert [lane["id"] for lane in body["lanes"]] == ["biz-a/term"]
    assert body["lanes"][0]["kind"] == "terminal" and body["lanes"][0]["isolation"]
    [service] = body["services"]
    assert service["lane"] == "biz-a/svc" and service["name"] == "svc" and service["state"] == "stopped"
    assert isinstance(body["images"]["templates"], list) and isinstance(body["images"]["built"], list)


def test_the_owner_starts_and_stops_a_service_and_reads_its_log(client: TestClient, envs: EnvManager) -> None:
    started = client.post(f"{LOCAL}/services/biz-a/svc/start")
    assert started.status_code == 200, started.text
    assert started.json()["lane"] == "biz-a/svc"
    logs = client.get(f"{LOCAL}/services/biz-a/svc/logs", params={"lines": 5})
    assert logs.status_code == 200 and logs.json()["lane"] == "biz-a/svc" and "log" in logs.json()
    stopped = client.post(f"{LOCAL}/services/biz-a/svc/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["desired"] == "stopped"


def test_a_terminal_is_not_a_service(client: TestClient, envs: EnvManager) -> None:
    refused = client.post(f"{LOCAL}/services/biz-a/term/start")
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid"


def test_a_service_name_must_be_a_lane_id(client: TestClient, envs: EnvManager) -> None:
    refused = client.post(f"{LOCAL}/services/Biz-A/svc/start")
    assert refused.status_code == 400
    assert client.post(f"{LOCAL}/services/biz-a/nothing/start").status_code == 404


def test_a_token_cannot_start_or_stop_a_service(client: TestClient, envs: EnvManager) -> None:
    as_token(client)
    for action in ("start", "stop"):
        refused = client.post(f"{LOCAL}/services/biz-a/svc/{action}")
        assert refused.status_code == 403, action
        assert refused.json()["error"]["code"] == "denied"
    assert envs.service_status("biz-a/svc").state == "stopped"


# ---- identity -------------------------------------------------------------------------------

def first_account(client: TestClient) -> str:
    accounts = client.get(f"{LOCAL}/identity").json()["accounts"]
    assert accounts, "the seeded node has accounts to show"
    return accounts[0]["lane"]


def make_identity(client: TestClient, lane: str) -> dict[str, Any]:
    """An identity made the way `agentos identity init` makes one, for the seeded account."""
    cfg = client.app.state.api.config
    cfg.profile_dir(lane).mkdir(parents=True, exist_ok=True)
    key = keys.create_key(cfg.secrets_dir, lane)
    ident = settings.LaneIdentity(lane_id=lane, signature_agent="https://agents.example", keyid=key.keyid,
                                  profile=settings.profile_binding(cfg.profile_dir(lane)))
    settings.save(cfg.secrets_dir, ident)
    return {"key": key, "cfg": cfg}


def test_an_account_without_an_identity_says_so(client: TestClient) -> None:
    accounts = client.get(f"{LOCAL}/identity").json()["accounts"]
    assert accounts and all(a["state"] == "none" and a["hosts"] == [] for a in accounts)


def test_a_site_needs_an_identity_first(client: TestClient) -> None:
    lane = first_account(client)
    refused = client.post(f"{LOCAL}/identity/{lane}/sites", json={"origin": "https://example.com", "reason": "research"})
    assert refused.status_code == 400
    assert "agentos identity init" in refused.json()["error"]["message"]


def test_the_owner_adds_and_removes_a_site_and_the_key_stays_here(client: TestClient) -> None:
    lane = first_account(client)
    made = make_identity(client, lane)
    key_file = keys.key_path(made["cfg"].secrets_dir, lane).read_text()
    private = json.loads(key_file).get("d") if key_file.strip().startswith("{") else None

    added = client.post(f"{LOCAL}/identity/{lane}/sites", json={"origin": "https://example.com", "reason": "read-only research"})
    assert added.status_code == 200, added.text
    view = added.json()
    assert view["state"] == "signing" and view["keyid"] == made["key"].keyid
    assert [h["origin"] for h in view["hosts"]] == ["https://example.com"]
    assert view["hosts"][0]["reason"] == "read-only research"
    if private:
        assert private not in added.text, "a private key is never shown"

    removed = client.delete(f"{LOCAL}/identity/{lane}/sites", params={"origin": "https://example.com"})
    assert removed.status_code == 200 and removed.json()["state"] == "off" and removed.json()["hosts"] == []
    gone = client.delete(f"{LOCAL}/identity/{lane}/sites", params={"origin": "https://example.com"})
    assert gone.status_code == 404


def test_a_token_cannot_add_or_remove_a_site(client: TestClient) -> None:
    lane = first_account(client)
    make_identity(client, lane)
    as_token(client)
    added = client.post(f"{LOCAL}/identity/{lane}/sites", json={"origin": "https://example.com", "reason": "x"})
    assert added.status_code == 403 and added.json()["error"]["code"] == "denied"
    removed = client.delete(f"{LOCAL}/identity/{lane}/sites", params={"origin": "https://example.com"})
    assert removed.status_code == 403


# ---- settings -------------------------------------------------------------------------------

def test_settings_show_every_value_and_never_a_secret(client: TestClient) -> None:
    rows = client.get(f"{LOCAL}/settings")
    assert rows.status_code == 200, rows.text
    by_name = {row["name"]: row for row in rows.json()}
    assert by_name["name"]["value"] == "dev"
    assert by_name["mcp_token"]["value"] == "(set, hidden)"
    assert all(row["source"] and row["description"] for row in rows.json())
    assert TOKEN not in rows.text


def test_a_secret_named_setting_shows_only_whether_it_is_set(client: TestClient) -> None:
    from agentos.api.routes_ops import _settings_rows

    cfg = client.app.state.api.config
    rows = {row["name"]: row["value"] for row in _settings_rows(cfg)}
    assert rows["mcp_token"] in ("(set, hidden)", "(not set)")
    assert all(TOKEN not in str(value) for value in rows.values())
