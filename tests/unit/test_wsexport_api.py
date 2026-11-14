"""POST /api/workspaces/{id}/export and POST /api/workspaces/import on the node's panel app, between two
nodes running the real services (see test_wsexport_support for the stores)."""

from __future__ import annotations

import json

import pytest
from fakes import FakeInteraction
from starlette.testclient import TestClient
from test_wsexport_support import IG_PASSWORD, LOG_N, PASSPHRASE, Node

from agentos.api import ApiSettings, build_api
from agentos.api.auth import PRESENCE_HEADER, PanelStore
from agentos.core import age
from agentos.node.daemon import build_node_app, build_services, node_runtime
from agentos.workspaces.transfer import export as wsexport

SERVICES = ["agentos.api.events", "agentos.policy.service", "agentos.vault.service", "agentos.workspaces.service",
            "agentos.runner.service"]


class Panel:
    def __init__(self, node: Node, tmp_path):
        self.node = node
        self.runtime = node_runtime(node.cfg, node.lanes, FakeInteraction())
        build_services(self.runtime, SERVICES)
        api = build_api(self.runtime, ApiSettings(lane_pump_seconds=0, panel_dir=tmp_path / "none"))
        self.app = build_node_app(node.cfg, self.runtime, api_app=api, panel=True)
        self.owner = {"Authorization": f"Bearer {node.cfg.mcp_token}"}

    def in_person(self) -> dict[str, str]:
        """The node token plus a one-time code, as the on-node CLI sends it: a person at the node."""
        import secrets

        code = "cli-" + secrets.token_hex(4)
        PanelStore.for_home(self.node.cfg.home).add_code(code, ttl_seconds=60)
        return {**self.owner, PRESENCE_HEADER: code}

    def client(self) -> TestClient:
        return TestClient(self.app, base_url="http://localhost:7400")

    def close(self) -> None:
        self.runtime.get("policy").close()
        self.runtime.get("workspaces").store.close()


@pytest.fixture
def panels(tmp_path, monkeypatch):
    real = wsexport.export_workspace

    async def quick(*args, **kw):  # a cheap scrypt work factor in tests (real exports use age's 2^18)
        return await real(*args, **{**kw, "log_n": LOG_N})

    monkeypatch.setattr(wsexport, "export_workspace", quick)
    a, b = Node(tmp_path, "node-a"), Node(tmp_path, "node-b")
    a.build_acme()
    pa, pb = Panel(a, tmp_path), Panel(b, tmp_path)
    yield pa, pb
    for p in (pa, pb):
        p.close()
        p.node.close()


def test_export_streams_and_import_uploads(panels):
    pa, pb = panels
    with pa.client() as client:
        refused = client.post("/api/workspaces/acme/export", json={"passphrase": PASSPHRASE}, headers=pa.owner)
        assert refused.status_code == 403 and "presence_required" in refused.text  # sessions leave with a person only
        r = client.post("/api/workspaces/acme/export", json={"passphrase": PASSPHRASE}, headers=pa.in_person())
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith('attachment; filename="agentos-acme-')
    summary = json.loads(r.headers["x-agentos-export"])
    assert summary["counts"]["lanes"] == 2 and summary["counts"]["vault_entries"] == 3
    blob = r.content
    assert blob.startswith(b"age-encryption.org/v1\n") and IG_PASSWORD.encode() not in blob
    assert not list((pa.node.home / "tmp").iterdir())  # the encrypted temp file is gone after streaming

    with pb.client() as client:
        dry = client.post("/api/workspaces/import?dry_run=true", content=blob,
                          headers={**pb.owner, "X-Agentos-Passphrase": PASSPHRASE})
        assert dry.status_code == 200 and dry.json()["applied"] is False and dry.json()["plan"]["ok"] is True
        assert pb.node.lanes.list() == []
        refused = client.post("/api/workspaces/import?reissue_grants=true", content=blob,
                              headers={**pb.owner, "X-Agentos-Passphrase": PASSPHRASE})
        assert refused.status_code == 403 and "presence_required" in refused.text  # the node token is no person
        PanelStore.for_home(pb.node.cfg.home).add_code("cli-import", ttl_seconds=60)
        done = client.post("/api/workspaces/import?reissue_grants=true", content=blob,
                           headers={**pb.owner, PRESENCE_HEADER: "cli-import", "X-Agentos-Passphrase": PASSPHRASE})
        assert done.status_code == 201, done.text
        result = done.json()["result"]
        assert result["lanes"] == ["acme/instagram", "acme/reddit"] and result["grants"][0]["token"]
        assert "—" not in done.text and IG_PASSWORD not in done.text
        again = client.post("/api/workspaces/import", content=blob,
                            headers={**pb.owner, "X-Agentos-Passphrase": PASSPHRASE})
        assert again.status_code == 409 and again.json()["applied"] is False
        assert "already exist" in again.json()["plan"]["blockers"][0]
        url = "/api/workspaces/import?as=acme2&on_vault_conflict=rename"
        # a copy of acme's logged-in browsers is a second browser for each account: a person's call
        clone = client.post(url, content=blob, headers={**pb.owner, "X-Agentos-Passphrase": PASSPHRASE})
        assert clone.status_code == 403 and "presence_required" in clone.text
        assert client.post(url + "&dry_run=true", content=blob,
                           headers={**pb.owner, "X-Agentos-Passphrase": PASSPHRASE}).status_code == 200
        PanelStore.for_home(pb.node.cfg.home).add_code("cli-import-2", ttl_seconds=60)
        renamed = client.post(url, content=blob,
                              headers={**pb.owner, PRESENCE_HEADER: "cli-import-2", "X-Agentos-Passphrase": PASSPHRASE})
        assert renamed.status_code == 201, renamed.text
    assert {i.spec.id for i in pb.node.lanes.list()} == {"acme/instagram", "acme/reddit", "acme2/instagram",
                                                         "acme2/reddit"}
    assert not list((pb.node.home / "tmp").iterdir())


def test_recipient_export_and_identity_import(panels):
    pa, pb = panels
    identity, recipient = age.generate_identity()
    with pa.client() as client:
        r = client.post("/api/workspaces/acme/export", json={"recipients": [recipient]}, headers=pa.in_person())
    assert r.status_code == 200, r.text
    with pb.client() as client:
        wrong = client.post("/api/workspaces/import", content=r.content,
                            headers={**pb.owner, "X-Agentos-Passphrase": PASSPHRASE})
        assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "bad_export"
        ok = client.post("/api/workspaces/import?dry_run=1", content=r.content,
                         headers={**pb.owner, "X-Agentos-Identity": identity})
        assert ok.status_code == 200 and ok.json()["plan"]["ok"] is True


def test_bad_requests(panels):
    pa, pb = panels
    with pa.client() as client:
        for body, message in (({}, "always encrypted"), ({"passphrase": "short"}, "at least 12"),
                              ({"recipients": "age1x"}, "list of age public keys"),
                              ({"passphrase": PASSPHRASE, "recipients": ["age1x"]}, "not both")):
            r = client.post("/api/workspaces/acme/export", json=body, headers=pa.in_person())
            assert r.status_code == 400 and message in r.json()["error"]["message"], (body, r.text)
        missing = client.post("/api/workspaces/ghost/export", json={"passphrase": PASSPHRASE}, headers=pa.in_person())
        assert missing.status_code == 404
    with pb.client() as client:
        r = client.post("/api/workspaces/import", content=b"x", headers=pb.owner)
        assert r.status_code == 400 and "X-Agentos-Passphrase" in r.json()["error"]["message"]
        r = client.post("/api/workspaces/import", content=b"", headers={**pb.owner, "X-Agentos-Passphrase": "x" * 12})
        assert r.status_code == 400 and "request body" in r.json()["error"]["message"]
        r = client.post("/api/workspaces/import", content=b"junk", headers={**pb.owner, "X-Agentos-Passphrase": "x" * 12})
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_export"
        r = client.post("/api/workspaces/import?dry_run=maybe", content=b"junk",
                        headers={**pb.owner, "X-Agentos-Passphrase": "x" * 12})
        assert r.status_code == 400
    assert not list((pb.node.home / "tmp").iterdir())
