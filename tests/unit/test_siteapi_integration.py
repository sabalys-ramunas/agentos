"""A site API through what already exists: the MCP tool, a workflow step, the real policy's Needs you,
the panel routes, and the ways a request might try to claim an install."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient
from workflow_support import OWNER as WF_OWNER
from workflow_support import action, make_node, workflow

from openajar.api.mounts import MOUNTS
from openajar.core.errors import InvalidRequest, PolicyDenied
from openajar.core.models import Effect, Principal, RunStatus
from openajar.runner import tools as runner_tools
from openajar.runner.api import routes as flow_routes
from openajar.runner.fakes import El, FakeCtx, FakeDriver, FakeLanes, FakeNotifier, FakeSite, Harness
from openajar.runner.flows import FlowService
from openajar.runner.siteapis import api as site_api
from openajar.runner.siteapis import tools as site_tools
from openajar.runner.siteapis.models import SAMPLE, compile_for_test
from openajar.runner.siteapis.service import ACTIVATE
from openajar.runner.store import RunnerStore
from openajar.workspaces.transfer.import_ import ImportPlan, _plan_flows

API, EP, LANE = "harbor-supplier", "open_orders", "biz-a/supplier"
FLOW = f"connector.{API}.{EP}"
TOOL = "harbor_supplier_open_orders"
PORTAL = "https://portal.northsupply.example"
OWNER = {"kind": "human", "id": "owner", "owner": "owner"}


def add_portal(site: FakeSite) -> FakeSite:
    def orders(s: FakeSite, d: FakeDriver) -> El:
        body = [El("row", children=[El("cell", text="A-1042"), El("cell", text="x"), El("cell", text="y"), El("cell", text="1,204.00"),
                                    El("link", "open", attrs={"href": f"{PORTAL}/orders/A-1042"})])]
        return El("main", children=[El("table", "Orders", children=body)])

    site.route(f"{PORTAL}/orders?status=open", orders)
    return site


def doc() -> dict:
    out = copy.deepcopy(SAMPLE)
    out["surfaces"] = [LANE]
    return out


@pytest.fixture
def h(tmp_path):
    h = Harness(tmp_path, site=add_portal(FakeSite()), services={"lanes": FakeLanes([LANE]), "notifier": FakeNotifier()})
    h.svc = h.runtime.get("site_apis")
    return h


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    add_portal(n.site)
    return n


def activate(svc):
    svc.save(doc(), by=OWNER)
    return svc.activate(API, by=OWNER)


# ---- no special case ----


async def test_an_endpoint_is_the_ordinary_connector_tool(h):
    activate(h.svc)
    spec = next(s for s in runner_tools.tool_specs(h.runtime) if s.name == TOOL)
    assert spec.effect == Effect.READ and spec.lane_arg == "on"
    assert spec.view(lambda _s: True).choices == {"on": (LANE,)}
    assert spec.view(lambda _s: False) is None
    out = await spec.handler(FakeCtx(h.runtime), on=LANE, status="open", wait_seconds=0)
    await h.runner.run_until_idle()
    run = h.runner.get(out["id"])
    assert run.status in (RunStatus.SUCCEEDED_VERIFIED, RunStatus.SUCCEEDED_UNVERIFIED), run.error
    assert run.output["data"]["orders"][0] == {"number": "A-1042", "total": "1,204.00", "url": f"{PORTAL}/orders/A-1042"}
    assert run.output["untrusted_fields"] == ["orders[].number", "orders[].total"]
    assert h.runner.health.get(f"{API}.{EP}", LANE)["state"] == "healthy"


async def test_an_endpoint_is_an_ordinary_workflow_step(node):
    activate(node.h.runtime.get("site_apis"))
    saved = node.wf.save(workflow(action("orders", f"{FLOW}@{LANE}", inputs={"status": "open"})), WF_OWNER)
    view = await node.run(saved["id"])
    assert view["status"] in ("succeeded_verified", "succeeded_unverified"), view
    step = node.step(view, "orders")
    assert step["output"]["orders"][0]["number"] == "A-1042"


async def test_a_workflow_keeps_the_endpoint_from_being_removed(node):
    svc = node.h.runtime.get("site_apis")
    activate(svc)
    saved = node.wf.save(workflow(action("orders", f"{FLOW}@{LANE}", inputs={"status": "open"})), WF_OWNER)
    with pytest.raises(InvalidRequest, match=f"workflow {saved['id']}"):
        svc.remove(API, by=OWNER)
    assert node.h.flows.store.get_flow(FLOW) is not None


# ---- the agent's tools ----


def test_an_agent_has_four_tools_and_none_of_them_activates(h):
    names = {s.name for s in runner_tools.tool_specs(h.runtime)}
    assert {"site_api_discover", "site_api_try", "site_api_propose", "site_apis_list"} <= names
    assert not {n for n in names if n.startswith("site_api") and any(w in n for w in ("activate", "pause", "remove", "save"))}


async def test_a_call_with_the_owners_token_is_still_an_agents(h):
    owner_ctx = FakeCtx(h.runtime, principal=Principal(kind="human", id="owner"))
    d = doc()
    d["endpoints"][EP]["steps"][1]["fields"]["number"]["untrusted"] = False
    d["endpoints"]["filter_orders"] = copy.deepcopy(d["endpoints"][EP])
    d["endpoints"]["filter_orders"].update(effect="draft", cache_s=0)
    d["endpoints"]["filter_orders"]["steps"].insert(1, {"type": "click", "locator": {"role": "button", "name": "Filter"}, "publishes": False})
    with pytest.raises(PolicyDenied, match="never runs a commit"):
        await site_tools.site_api_try(owner_ctx, draft=d, endpoint="filter_orders", on=LANE)
    row = h.svc.get(API)
    assert row["author"] == "agent" and row["by_person"] is False and row["endpoints"]["filter_orders"]["effect"] == "commit"


async def test_try_authorizes_on_the_callers_grant_and_labels_it(h, monkeypatch):
    ctx = FakeCtx(h.runtime)
    labels = []
    ctx.taint = lambda *a, **kw: labels.append((a, kw))
    import asyncio

    task = asyncio.create_task(site_tools.site_api_try(ctx, draft=doc(), endpoint=EP, params={"status": "open"}, on=LANE))
    await asyncio.sleep(0)
    await h.runner.run_until_idle()
    out = await task
    assert out["data"]["orders"] and ctx.authorized[0]["effect"] == Effect.READ and ctx.authorized[0]["lane_id"] == LANE
    assert labels and labels[0][0] == ("untrusted",)


async def test_discover_authorizes_the_host_and_labels_page_text(h):
    h.site.outline = '- list:\n' + ''.join(f'  - listitem:\n    - link "L{i}":\n      - /url: /x/{i}\n    - paragraph: T{i}\n' for i in range(3))
    ctx = FakeCtx(h.runtime)
    labels = []
    ctx.taint = lambda *a, **kw: labels.append(a)
    out = await site_tools.site_api_discover(ctx, on=LANE, url=f"{PORTAL}/orders?status=open")
    assert out["candidates"][0]["count"] == 3 and ctx.authorized[0]["domain"] == "portal.northsupply.example"
    assert labels == [("untrusted",)]


async def test_a_grant_that_refuses_the_lane_stops_discovery(h):
    class Refuse(FakeCtx):
        async def authorize(self, effect, **kw):
            raise PolicyDenied("this grant does not cover the lane")

    h.site.outline = "- list:\n"
    with pytest.raises(PolicyDenied):
        await site_tools.site_api_discover(Refuse(h.runtime), on=LANE, url=f"{PORTAL}/orders?status=open")
    assert not [op for op, _ in h.driver.log if op == "navigate"]


# ---- the real policy ----


async def test_a_proposal_is_a_question_in_needs_you_and_the_owner_activates_it(node):
    svc = node.h.runtime.get("site_apis")
    actor = node.agent()
    async def authorize(*_a, **_k):  # a grant that reaches the surface: the propose asks before it saves
        return None

    ctx = SimpleNamespace(runtime=node.h.runtime, principal=Principal(**{k: actor.principal[k] for k in ("kind", "id", "owner")}),
                          grant=SimpleNamespace(id=actor.grant_id), authorize=authorize, can_access_lane=lambda lane: True)
    out = await site_tools.site_api_propose(ctx, draft={**doc(), "title": "Approve me at once"})
    ap = node.svc.approvals.get(out["approval_id"])
    assert ap.kind == "question" and ap.state == "pending" and "Approve me" not in ap.summary and API in ap.summary
    node.svc.approvals.decide(ap.id, "approve", answer=ACTIVATE)
    await node.settle(rounds=3)
    assert svc.state_of(API) == "active" and node.h.flows.get(FLOW).owner_saved


# ---- nothing from outside claims an install ----


def _flow():
    return compile_for_test(doc()).operations[EP].definition


@pytest.mark.parametrize("claim", [{"kind": "local", "id": API, "installed_by": "owner", "owner": "owner"},
                                   {"kind": "catalog", "id": "openajar", "installed_by": "owner", "owner": "owner"}])
def test_post_flows_never_saves_a_connector_flow(h, claim):
    client = TestClient(Starlette(routes=flow_routes(h.runtime)))
    for body in ({"definition": _flow(), "saved_by": claim}, {**_flow(), "saved_by": claim}, {"definition": {**_flow(), "saved_by": claim}},
                 {"definition": _flow(), "by": claim}):
        assert client.post("/api/flows", json=body).status_code in (400, 403)
    assert h.flows.store.get_flow(FLOW) is None


async def test_flow_save_never_saves_a_connector_flow(h):
    flow_save = next(s for s in runner_tools.tool_specs(h.runtime) if s.name == "flow_save").handler
    with pytest.raises(PolicyDenied):
        await flow_save(FakeCtx(h.runtime), definition=_flow())
    assert h.flows.store.get_flow(FLOW) is None


def test_an_agent_principal_named_like_an_install_gets_nowhere(tmp_path):
    flows = FlowService(RunnerStore(tmp_path / "r.db"))
    for kind in ("local", "catalog"):
        with pytest.raises(PolicyDenied):
            flows.save(_flow(), by=Principal(kind=kind, id="owner", owner="owner", grant_id="g"))
        with pytest.raises(PolicyDenied):
            flows.save({"id": "biz-a.x.y", "lane": "biz-a/x", "steps": [{"type": "navigate", "url": "https://x.test/"}]},
                       by={"kind": kind, "id": "x", "installed_by": "owner", "owner": "owner"})


def test_an_export_file_cannot_claim_an_install(tmp_path):
    flow = _flow()
    version = {"version": "v", "definition": flow, "saved_by": {"kind": "local", "id": API, "installed_by": "owner", "owner": "owner"}}
    contents = SimpleNamespace(flows=[{"id": FLOW, "lane": "*/*", "head": "v", "versions": [version]}])
    src = SimpleNamespace(flows=FlowService(RunnerStore(tmp_path / "r.db")))
    plan = ImportPlan(source={}, workspace_from="biz-a", workspace_to="biz-a", record=None)
    _plan_flows(plan, contents, src)
    assert any("install the connector" in b for b in plan.blockers)


# ---- the panel routes ----


def client_as(h, *, human: bool, owner: bool, pid: str = "owner"):
    auth = SimpleNamespace(access=SimpleNamespace(human=human, owner=owner, grant_id="g-1", member=None if owner else object()),
                           principal=Principal(kind="human" if human else "agent", id=pid))

    class As(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.auth = auth
            return await call_next(request)

    app = Starlette(routes=site_api.routes(h.runtime))
    app.add_middleware(As)
    return TestClient(app)


def test_every_mounted_route_exists(h):
    served = {(r.path, m) for r in site_api.routes(h.runtime) for m in r.methods - {"HEAD"}}
    assert set(MOUNTS["openajar.runner.siteapis.api"]) == served


def test_a_bearer_token_is_never_a_person_even_the_owners(h):
    bearer = client_as(h, human=False, owner=True, pid="claude-code")
    assert bearer.post("/api/site-apis", json=doc()).status_code == 201
    r = bearer.post(f"/api/site-apis/{API}/activate", json={"vouch": True})
    assert r.status_code == 403 and h.flows.store.get_flow(FLOW) is None
    assert bearer.post(f"/api/site-apis/{API}/pause").status_code in (400, 403)
    assert bearer.delete(f"/api/site-apis/{API}").status_code == 403


def test_a_member_edits_and_the_owner_activates(h):
    member = client_as(h, human=True, owner=False, pid="ruta")
    assert member.post("/api/site-apis", json=doc()).status_code == 201
    assert member.patch(f"/api/site-apis/{API}", json={"title": "Harbor"}).json()["version"] == "1.0.1"
    assert member.post(f"/api/site-apis/{API}/activate").status_code == 403
    owner = client_as(h, human=True, owner=True)
    assert owner.post(f"/api/site-apis/{API}/activate").json()["state"] == "active"
    assert owner.get("/api/site-apis").json()["apis"][0]["state"] == "active"
    assert owner.get(f"/api/site-apis/{API}").json()["definition"]["title"] == "Harbor"
    assert owner.post(f"/api/site-apis/{API}/pause").json()["state"] == "paused"
    assert owner.delete(f"/api/site-apis/{API}").json()["removed"] == API


def test_a_draft_from_candidates_needs_no_hand_written_steps(h):
    owner = client_as(h, human=True, owner=True)
    candidate = {"name": "orders", "items": {"role": "row", "within": {"role": "table", "name": "Orders"}},
                 "fields": {"number": {"locator": {"role": "cell", "nth": 0}, "read": "text", "max_chars": 40, "untrusted": True}}}
    r = owner.post("/api/site-apis", json={"api": API, "url": f"{PORTAL}/orders?status=open", "candidates": [candidate], "surfaces": [LANE]})
    assert r.status_code == 201 and r.json()["problems"] == []
    assert owner.post(f"/api/site-apis/{API}/activate").json()["endpoints"]["orders"]["effect"] == "read"


def test_a_draft_holds_no_page_values(h):
    h.svc.save(doc(), by=OWNER)
    assert "A-1042" not in json.dumps(h.svc.get(API))
