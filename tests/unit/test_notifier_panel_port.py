"""The approval's review link opens the panel this node serves, on its own port, not 7400."""

from __future__ import annotations

import json

from test_policy_support import Clock

from agentos.core.config import NodeConfig
from agentos.core.runtime import Runtime
from agentos.policy.service import PolicyService


def service(home, port: int) -> PolicyService:
    rt = Runtime()
    cfg = NodeConfig(home=home, mcp_token="owner-token-xyz", mcp_port=port)
    rt.register("config", cfg)
    return PolicyService(home, node_config=cfg, runtime=rt, clock=Clock())


def test_the_review_link_uses_the_nodes_own_port(tmp_path):
    svc = service(tmp_path, 7801)
    try:
        assert svc.notifier.review_url("ap_x") == "http://localhost:7801/local/needs-you#ap_x"
    finally:
        svc.close()


def test_a_saved_config_with_the_old_default_still_follows_the_node(tmp_path):
    (tmp_path / "secrets").mkdir(mode=0o700)
    (tmp_path / "secrets" / "notifier.json").write_text(json.dumps({"panel_url": "http://localhost:7400"}))
    svc = service(tmp_path, 7801)
    try:
        assert svc.notifier.review_url("ap_x").startswith("http://localhost:7801/")
    finally:
        svc.close()


def test_a_panel_url_the_owner_set_wins(tmp_path):
    svc = service(tmp_path, 7801)
    try:
        svc.notifier.update_config({"panel_url": "http://box.lan:9000"})
        assert svc.notifier.review_url("ap_x") == "http://box.lan:9000/local/needs-you#ap_x"
    finally:
        svc.close()
