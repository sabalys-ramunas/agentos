"""Who may save a site API's flows (docs/SITE-APIS.md, "Provenance")."""

from __future__ import annotations

import pytest

from agentos.core.errors import PolicyDenied
from agentos.core.models import ActsAs, Principal
from agentos.runner.connectors.compile import ConnectorInvalid, compile_connector
from agentos.runner.connectors.surfaces import binds_to
from agentos.runner.flows import FlowService
from agentos.runner.models import NodeInstall, is_owner
from agentos.runner.siteapis.models import (
    SAMPLE,
    compile_for_test,
    install_for_test,
    local_saved_by,
    to_connector,
)
from agentos.runner.store import RunnerStore

OWNER = Principal(kind="human", id="owner")


@pytest.fixture
def flows(tmp_path):
    return FlowService(RunnerStore(tmp_path / "runner.db"))


def _flow():
    return compile_for_test().operations["open_orders"].flow


def test_local_counts_as_the_owner_only_when_a_person_activated_it():
    assert is_owner({"kind": "local", "id": "harbor", "installed_by": "owner", "owner": "owner"})
    assert not is_owner({"kind": "local", "id": "harbor", "owner": "owner"})
    assert not is_owner({"kind": "local", "id": "harbor", "installed_by": "claude-code", "owner": "owner"})


def test_an_install_saves_the_connector_flow_with_its_http_block(flows):
    (fv,) = install_for_test(flows)
    assert fv.id == "connector.harbor-supplier.open_orders" and fv.owner_saved
    assert fv.saved_by == {"kind": "local", "id": "harbor-supplier", "installed_by": "owner", "owner": "owner"}
    assert fv.definition.connector["http"] == {
        "cache_s": 300, "expect": {"min_items": {"orders": 1}},
        "untrusted_fields": ["orders[].number", "orders[].total"], "private_fields": []}
    assert fv.definition.connector["site"] == "web" and fv.definition.connector["acting_as"] == {"kind": "lane"}
    assert binds_to(fv) == fv.version  # not catalog: an edited endpoint loses its standing approvals


@pytest.mark.parametrize("kind", ["local", "catalog", "LOCAL", " catalog "])
def test_no_dict_or_principal_can_claim_an_install(flows, kind):
    claim = {"kind": kind, "id": "harbor-supplier", "installed_by": "owner", "owner": "owner"}
    for by in (claim, Principal(kind=kind, id="owner"), Principal(kind=kind, id="claude-code", owner="owner")):
        with pytest.raises(PolicyDenied):
            flows.save(_flow(), by=by)
    assert flows.store.get_flow("connector.harbor-supplier.open_orders") is None


def test_an_ordinary_principal_cannot_save_a_connector_flow(flows):
    for by in (OWNER, Principal(kind="agent", id="claude-code"), {"kind": "human", "id": "owner"}, "owner", None):
        with pytest.raises(PolicyDenied, match="reserved"):
            flows.save(_flow(), by=by)


def test_an_install_saves_nothing_but_connector_flows(flows):
    with pytest.raises(PolicyDenied, match="only save connector"):
        flows.save({"id": "biz-a.x.y", "lane": "biz-a/x", "steps": [{"type": "navigate", "url": "https://x.test/"}]},
                   by=local_saved_by("harbor", {"id": "owner"}))
    with pytest.raises(PolicyDenied, match="catalog or local"):
        flows.save(_flow(), by=NodeInstall({"kind": "human", "id": "owner", "owner": "owner"}))


def test_lane_identity_and_the_web_site_are_refused_in_a_catalog_document():
    doc = to_connector(SAMPLE, person=True, sites={})
    with pytest.raises(ConnectorInvalid, match="site must equal connector"):
        compile_connector(doc, sites={})
    doc = {**doc, "site": doc["connector"], "surfaces": []}
    with pytest.raises(ConnectorInvalid, match="only for a site API"):
        compile_connector(doc, sites={"harbor-supplier": ("portal.northsupply.example",)})


def test_a_lane_surface_has_no_id():
    assert ActsAs(kind="lane").label() == "lane session" and ActsAs(kind="lane").asset_id == ""
    with pytest.raises(ValueError, match="no id"):
        ActsAs(kind="lane", id="1")
