"""Surfaces, bindings and connector tools (docs/design/connectors.md 4.3 and 4.9, Brief C;
AUD-01): a lane is a login, a surface is who you post as, and grants, tools and
schedules target the surface while its binding picks the lane."""

import pytest
from connector_samples import (
    SITES,
    bind,
    comments_op,
    commit_op,
    connector,
    connector_site,
    live,
    page_op,
    read_op,
)
from pydantic import ValidationError
from test_policy_support import Clock

from agentos.core.errors import InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import ActsAs, Effect, Principal, RunStatus, Surface
from agentos.core.tools import bind_request_token
from agentos.mcp.registry import build_mcp
from agentos.policy.errors import Denied
from agentos.policy.service import PolicyService
from agentos.policy.wrap import wrap_tool
from agentos.runner import tools as runner_tools
from agentos.runner.connectors.compile import compile_connector
from agentos.runner.connectors.models import Connector
from agentos.runner.connectors.service import ConnectorService
from agentos.runner.connectors.tools import connector_status, connectors_list
from agentos.runner.fakes import OWNER, FakeCtx, FakeLanes, Harness

AGENT = Principal(kind="agent", id="claude-code", thread="T-1")
PAGE = {"id": "biz-a/social-page", "lane": "personal/social", "acts_as": {"kind": "fb_page", "id": "biz-a", "name": "biz-a"}}


def _harness(tmp_path, ops=None, *, lanes=("biz-a/social", "biz-b/social", "personal/social"), site=None):
    h = Harness(tmp_path, site=site or connector_site(posts=[{"id": "1", "caption": "Autumn"}]),
                services={"lanes": FakeLanes(list(lanes))})
    live(h.driver)
    doc = connector(ops if ops is not None else {"profile_summary": read_op(), "page_summary": page_op()})
    ConnectorService(h.flows, sites=SITES).install(compile_connector(doc, sites=SITES), by=OWNER)
    return h


def _tool(h, name):
    return next(s for s in runner_tools.tool_specs(h.runtime) if s.name == name)


def _with_policy(h, tmp_path):
    svc = PolicyService(tmp_path / "policy", runtime=h.runtime, clock=Clock())
    h.runtime.register("policy", svc)
    return svc


# ---------------------------------------------------------------- the surface model


def test_a_surface_pins_who_it_acts_as():
    s = Surface(id="biz-a/facebook-page", site="facebook", lane="personal/facebook",
                acts_as={"kind": "fb_page", "id": "100089000000001", "name": "biz-a"})
    assert s.workspace == "biz-a" and s.account_language == "en"
    assert s.acts_as.label() == "biz-a (Page)" and s.acts_as.asset_id == "100089000000001"
    assert ActsAs(kind="account", user_id="51234567").label() == "51234567 (account)"
    for bad in ({"kind": "account"}, {"kind": "account", "user_id": "1", "id": "2"}, {"kind": "fb_page"},
                {"kind": "li_company", "user_id": "1"}, {"kind": "someone"}):
        with pytest.raises(ValidationError):
            ActsAs(**bad)
    for field, value in (("id", "biz-a"), ("lane", "Personal/FB"), ("site", "Facebook"),
                         ("account_language", "english")):
        with pytest.raises(ValidationError):
            Surface(**{**s.model_dump(), field: value})


def test_only_the_owner_binds_and_removes_surfaces(tmp_path):
    h = _harness(tmp_path)
    surfaces = h.runner.surfaces
    surfaces.sites = SITES
    with pytest.raises(PolicyDenied, match="only the owner"):
        surfaces.put({**PAGE, "site": "social"}, by=AGENT)
    with pytest.raises(PolicyDenied, match="only the owner"):
        surfaces.put({**PAGE, "site": "social"}, by={"kind": "human", "id": "va", "owner": "owner"})
    s, created = surfaces.put({**PAGE, "site": "social"}, by=OWNER)
    assert created and s.created_by == "owner" and s.lane == "personal/social"
    again, created = surfaces.put({**PAGE, "site": "social", "title": "biz-a on social"}, by=OWNER)
    assert not created and again.created_at == s.created_at and again.title == "biz-a on social"
    with pytest.raises(InvalidRequest, match="not a site agentos knows"):
        surfaces.put({**PAGE, "site": "myspace"}, by=OWNER)
    with pytest.raises(InvalidRequest, match="does not exist"):
        surfaces.put({**PAGE, "site": "social", "lane": "personal/nowhere"}, by=OWNER)
    with pytest.raises(InvalidRequest, match="invalid surface"):
        surfaces.put({**PAGE, "site": "social", "acts_as": {"kind": "fb_page"}}, by=OWNER)
    h.scheduler.add(flow="connector.social.page_summary", on="biz-a/social-page", cron="0 10 * * 2", principal=OWNER)
    with pytest.raises(InvalidRequest, match="remove them first"):
        surfaces.remove("biz-a/social-page", by=OWNER)
    with pytest.raises(PolicyDenied):
        surfaces.remove("biz-a/social-page", by=AGENT)
    for sch in h.scheduler.list():
        h.scheduler.remove(sch.id)
    assert surfaces.remove("biz-a/social-page", by=OWNER).id == "biz-a/social-page"
    with pytest.raises(NotFound):
        surfaces.get("biz-a/social-page")


def test_connector_keys_cannot_take_the_nodes_own_tool_prefixes():
    for key in ("files", "browser", "lane", "flow", "vault", "connector"):
        with pytest.raises(ValidationError, match="reserved"):
            Connector.model_validate({**connector(), "connector": key, "site": key})


# ---------------------------------------------------------------- binding a run


async def test_a_page_surface_runs_on_the_login_it_acts_through(tmp_path):
    h = _harness(tmp_path)
    bind(h, **PAGE)
    out = await _tool(h, "social_page_summary").handler(FakeCtx(h.runtime), on="biz-a/social-page", wait_seconds=0)
    await h.runner.run_until_idle()
    run = h.runner.get(out["id"])
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert run.lane == "personal/social" and run.surface == "biz-a/social-page" and run.workspace == "biz-a"
    assert run.params == {"acts_as_id": "biz-a", "account_language": "en"}  # filled by the binding
    src = run.output["source"]
    assert src["surface"] == "biz-a/social-page" and src["lane"] == "personal/social"
    assert h.runner.describe(run)["surface"] == "biz-a/social-page"
    ident = next(r for r in h.store.steps(run.id) if r.path == "1").result["identity"]
    assert ident == {"acting_as": "fb_page", "id": "biz-a", "source": "url", "status": "match"}


async def test_the_caller_never_decides_who_a_run_acts_as(tmp_path):
    h = _harness(tmp_path)
    bind(h)
    bind(h, **PAGE)
    fid = "connector.social.profile_summary"
    with pytest.raises(PolicyDenied, match="acts_as_id come from the surface's binding"):
        await _tool(h, "social_profile_summary").handler(FakeCtx(h.runtime), on="biz-a/social", acts_as_id="biz-b")
    # every other entry point (the panel API, the CLI, events, schedules) goes through runner.submit
    with pytest.raises(PolicyDenied, match="account_language come from the surface's binding"):
        h.runner.submit(flow_id=fid, params={"account_language": "lt"}, lane="biz-a/social", principal=OWNER)
    run, _ = h.runner.submit(flow_id=fid, params={}, lane="biz-a/social", principal=OWNER, source="api")
    assert run.surface == "biz-a/social" and run.lane == "biz-a/social" and run.params["acts_as_id"] == "biz-a"
    with pytest.raises(InvalidRequest, match="runs on a surface"):
        h.runner.submit(flow_id=fid, params={}, principal=OWNER)
    with pytest.raises(InvalidRequest, match="no surface 'biz-b/social'"):
        h.runner.submit(flow_id=fid, params={}, lane="biz-b/social", principal=OWNER)
    with pytest.raises(InvalidRequest, match="acts as account; surface biz-a/social-page acts as fb_page"):
        h.runner.submit(flow_id=fid, params={}, lane="biz-a/social-page", principal=OWNER)
    with pytest.raises(InvalidRequest, match="runs on personal/social, not biz-a/social"):
        h.runner.submit(flow_id="connector.social.page_summary", params={}, surface="biz-a/social-page",
                        lane="biz-a/social", principal=OWNER)
    bind(h, id="biz-a/social-de", account_language="de")
    with pytest.raises(InvalidRequest, match="covers the account languages en, lt"):
        h.runner.submit(flow_id=fid, params={}, lane="biz-a/social-de", principal=OWNER)
    with pytest.raises(InvalidRequest, match="not a connector operation"):
        h.save({"id": "biz-a.social.look", "lane": "biz-a/social", "effect": "read",
                "steps": [{"type": "navigate", "url": "https://social.example/"}]})
        h.runner.submit(flow_id="biz-a.social.look", surface="biz-a/social", principal=OWNER)
    with pytest.raises(PolicyDenied, match="call its connector tool"):
        await runner_tools.flow_run(FakeCtx(h.runtime), name=fid, on="biz-a/social")


async def test_a_page_surface_is_reachable_from_biz_a_and_not_from_biz_b(tmp_path):
    h = _harness(tmp_path)
    bind(h, **PAGE)
    svc = _with_policy(h, tmp_path)
    _, tok_a = svc.issue(subject={"id": "agent-a"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    _, tok_b = svc.issue(subject={"id": "agent-b"}, workspaces=["biz-b"], ceiling=Effect.COMMIT)
    handler = _tool(h, "social_page_summary").handler
    out = await handler(svc.context_for_token(tok_a, tool="social_page_summary"), on="biz-a/social-page",
                        wait_seconds=0)
    await h.runner.run_until_idle()
    run = h.runner.get(out["id"])
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert run.lane == "personal/social" and run.grant_id != "owner"
    with pytest.raises(PolicyDenied, match="does not cover surface biz-a/social-page"):
        await handler(svc.context_for_token(tok_b, tool="social_page_summary"), on="biz-a/social-page")
    # the biz-a grant reaches the Page, never the personal login it acts through
    with pytest.raises(Denied, match="does not cover personal/social"):
        svc.context_for_token(tok_a).precheck(Effect.READ, lane_id="personal/social")
    with pytest.raises(PolicyDenied, match="does not cover"):
        h.runner.submit(flow_id="connector.social.page_summary", surface="biz-a/social-page",
                        principal={"kind": "agent", "id": "agent-b", "grant_id": svc.resolve(tok_b)[1].id})


# ---------------------------------------------------------------- tools


def test_one_tool_per_operation_named_site_op(tmp_path):
    h = _harness(tmp_path, {"profile_summary": read_op(), "post_photo": commit_op(), "list_comments": comments_op()})
    specs = {s.name: s for s in runner_tools.tool_specs(h.runtime)}
    assert {"social_profile_summary", "social_post_photo", "social_list_comments", "connectors_list",
            "connector_status"} <= set(specs)
    assert not any(n.startswith("flow_connector") for n in specs)
    post = specs["social_post_photo"]
    assert post.effect == Effect.COMMIT and post.lane_arg == "on"
    from agentos.mcp.registry import tool_params

    params = {p.name: p for p in tool_params(post)}
    assert set(params) == {"on", "caption", "image", "wait_seconds", "idempotency_key", "not_before"}
    assert params["on"].required and params["caption"].required and not params["wait_seconds"].required
    assert "social.post_photo 1.0.0, effect commit" in post.description
    assert "Runs with no model" in post.description and "\u2014" not in post.description


async def test_tools_list_offers_only_the_surfaces_a_grant_can_see(tmp_path):
    h = _harness(tmp_path)
    bind(h)                                           # biz-a/social, its own login
    bind(h, id="biz-b/social", lane="biz-b/social", acts_as={"kind": "account", "user_id": "biz-b"})
    bind(h, **PAGE)                                   # biz-a's Page through personal/social
    svc = _with_policy(h, tmp_path)
    _, tok_a = svc.issue(subject={"id": "agent-a"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    _, tok_b = svc.issue(subject={"id": "agent-b"}, workspaces=["biz-b"], ceiling=Effect.COMMIT)
    _, tok_p = svc.issue(subject={"id": "agent-p"}, workspaces=["personal"], ceiling=Effect.COMMIT)
    server = build_mcp(h.runtime, [runner_tools.tool_specs], wrap_tool,
                       visible=lambda tok, specs: svc.visible_tools(tok, specs, runtime=h.runtime))

    async def seen(tok):
        with bind_request_token(tok):
            return {t.name: t for t in await server.list_tools()}

    a = await seen(tok_a)
    assert a["social_profile_summary"].input_schema["properties"]["on"]["enum"] == ["biz-a/social"]
    assert a["social_page_summary"].input_schema["properties"]["on"]["enum"] == ["biz-a/social-page"]
    assert "biz-a/social-page as biz-a (Page) (unknown)" in a["social_page_summary"].description
    b = await seen(tok_b)
    assert b["social_profile_summary"].input_schema["properties"]["on"]["enum"] == ["biz-b/social"]
    assert "social_page_summary" not in b  # no Page surface in biz-b: no tool
    p = await seen(tok_p)
    assert "social_profile_summary" not in p and "social_page_summary" not in p  # the login is not a surface
    assert "connectors_list" in p
    with bind_request_token(tok_b):
        result = await server.call_tool("social_page_summary", {"on": "biz-a/social-page"})
    assert result.is_error and "not_found" in result.content[0].text


async def test_connectors_list_and_status_show_only_the_callers_surfaces(tmp_path):
    h = _harness(tmp_path)
    bind(h)
    bind(h, **PAGE)
    svc = _with_policy(h, tmp_path)
    _, tok_a = svc.issue(subject={"id": "agent-a"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    ctx = svc.context_for_token(tok_a, tool="connectors_list")
    out = await connectors_list(ctx)
    rows = {r["operation"]: r for r in out["operations"]}
    page = rows["social.page_summary"]
    assert page["tool"] == "social_page_summary" and page["effect"] == "read" and page["acts_as"] == "fb_page"
    assert page["contract"].startswith("sha256:") and page["version"] == "1.0.0"
    assert page["surfaces"] == [{"surface": "biz-a/social-page", "acts_as": "biz-a (Page)", "account_language": "en",
                                 "health": "unknown"}]  # no lane: the grant does not cover personal/social
    assert [s["surface"] for s in rows["social.profile_summary"]["surfaces"]] == ["biz-a/social"]
    assert rows["social.profile_summary"]["surfaces"][0]["lane"] == "biz-a/social"
    await _tool(h, "social_page_summary").handler(ctx, on="biz-a/social-page", wait_seconds=0)
    await h.runner.run_until_idle()
    st = await connector_status(svc.context_for_token(tok_a, tool="connector_status"), name="social_page_summary",
                                on="biz-a/social-page")
    assert st["health"]["state"] == "healthy" and st["last_run"]["status"] == "succeeded_verified"
    assert st["last_run"]["surface"] == "biz-a/social-page"


# ---------------------------------------------------------------- schedules


async def test_a_schedule_targets_the_surface_and_binds_at_every_fire(tmp_path):
    h = _harness(tmp_path)
    bind(h, **PAGE)
    s, _ = h.scheduler.add(flow="connector.social.page_summary", on="biz-a/social-page", cron="*/5 * * * *",
                           principal=OWNER)
    assert s.surface == "biz-a/social-page" and s.lane == "personal/social"
    with pytest.raises(PolicyDenied, match="acts_as_id come from the surface's binding"):
        h.scheduler.add(flow="connector.social.page_summary", on="biz-a/social-page", cron="0 10 * * 2",
                        params={"acts_as_id": "biz-b"}, principal=OWNER)
    with pytest.raises(InvalidRequest, match="no surface"):
        h.scheduler.add(flow="connector.social.page_summary", on="biz-b/social-page", cron="0 10 * * 2",
                        principal=OWNER)
    h.clock.advance(600)
    stats = await h.scheduler.tick()
    assert stats["fired"] == 1
    run = h.runner.get(h.scheduler.get(s.id).state["last_run_id"])
    assert run.surface == "biz-a/social-page" and run.lane == "personal/social" and run.schedule_id == s.id
    await h.runner.run_until_idle()
    assert h.runner.get(run.id).status == RunStatus.SUCCEEDED_VERIFIED


# ---------------------------------------------------------------- the panel API and the CLI


def test_the_panel_api_binds_surfaces_and_queues_runs_through_them(tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from agentos.runner.connectors import api

    h = _harness(tmp_path)
    h.runner.surfaces.sites = SITES
    c = TestClient(Starlette(routes=api.routes(h.runtime)))
    r = c.post("/api/surfaces", json={**PAGE, "site": "social"})
    assert r.status_code == 201 and r.json()["lane"] == "personal/social"
    assert c.post("/api/surfaces", json={**PAGE, "site": "social", "title": "biz-a"}).status_code == 200
    assert [s["id"] for s in c.get("/api/surfaces").json()] == ["biz-a/social-page"]
    assert c.get("/api/surfaces/biz-a/social-page").json()["acts_as"]["kind"] == "fb_page"
    assert c.get("/api/surfaces/biz-a/nope").status_code == 404
    assert c.post("/api/surfaces", json={**PAGE, "site": "social", "lane": "nope"}).status_code == 400
    ops = {o["operation"]: o for o in c.get("/api/connectors").json()}
    assert ops["social.page_summary"]["surfaces"][0]["surface"] == "biz-a/social-page"
    r = c.post("/api/connectors/social.page_summary/run", json={"on": "biz-a/social-page"})
    assert r.status_code == 202 and r.json()["surface"] == "biz-a/social-page" and r.json()["lane"] == "personal/social"
    r = c.post("/api/connectors/social.page_summary/run", json={"on": "biz-a/social-page",
                                                               "params": {"acts_as_id": "biz-b"}})
    assert r.status_code == 403 and r.json()["error"]["code"] == "denied"
    assert c.post("/api/connectors/social.page_summary/run", json={"surface": "x"}).status_code == 400
    assert c.delete("/api/surfaces/biz-a/social-page").json() == {"deleted": "biz-a/social-page"}


def test_the_mounts_serve_the_connector_routes_as_owner_routes():
    from agentos.api.mounts import MOUNTS

    mounted = MOUNTS["agentos.runner.connectors.api"]
    assert ("/api/connectors/{operation}/run", "POST") in mounted and ("/api/surfaces", "POST") in mounted


def test_the_cli_binds_lists_runs_and_unbinds(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentos.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    h = Harness(tmp_path)  # the same runner.db the CLI opens
    ConnectorService(h.flows, sites=SITES).install(compile_connector(connector(), sites=SITES), by=OWNER)
    bind(h)
    cli = CliRunner()
    r = cli.invoke(app, ["connector", "bind", "biz-a/facebook-page", "--site", "facebook", "--lane",
                         "personal/facebook", "--acts-as", "fb_page", "--id", "100089000000001", "--name", "biz-a"])
    assert r.exit_code == 0, r.output
    assert "biz-a/facebook-page  bound: facebook as biz-a (Page) through personal/facebook (en)" in r.output
    r = cli.invoke(app, ["connector", "surfaces"])
    assert "biz-a/facebook-page" in r.output and "biz-a/social" in r.output
    r = cli.invoke(app, ["connector", "run", "social.profile_summary", "--on", "biz-a/social"])
    assert r.exit_code == 0 and "queued on biz-a/social through biz-a/social" in r.output
    r = cli.invoke(app, ["connector", "run", "social.profile_summary", "--on", "biz-a/social", "-p", "acts_as_id=x"])
    assert r.exit_code == 1 and "come from the surface's binding" in r.output
    r = cli.invoke(app, ["connector", "bind", "biz-a/x", "--site", "myspace", "--lane", "biz-a/x", "--acts-as",
                         "account", "--id", "1"])
    assert r.exit_code == 1 and "not a site agentos knows" in r.output
    assert cli.invoke(app, ["connector", "health"]).exit_code == 0
    r = cli.invoke(app, ["connector", "unbind", "biz-a/facebook-page"])
    assert r.exit_code == 0 and "removed" in r.output and "\u2014" not in r.output


def test_the_panels_flow_run_plans_a_connector_operation_on_its_surface(tmp_path):
    from agentos.api.bridge import FlowsAdapter

    h = _harness(tmp_path)
    bind(h, **PAGE)
    panel = FlowsAdapter(h.flows, h.runner)
    plan = panel.plan("connector.social.page_summary", params={}, on="biz-a/social-page")
    assert plan["lane"] == "biz-a/social-page" and plan["acting_lane"] == "personal/social"
    assert plan["params"] == {} and plan["content"]["surface"] == "biz-a/social-page"
    assert plan["content"]["operation"] == "social.page_summary" and "as biz-a (Page)" in plan["summary"]
    run = panel.run("connector.social.page_summary", params=plan["params"], principal=OWNER, on=plan["lane"])
    assert run["surface"] == "biz-a/social-page" and run["lane"] == "personal/social"
    with pytest.raises(PolicyDenied):
        panel.plan("connector.social.page_summary", params={"acts_as_id": "biz-b"}, on="biz-a/social-page")
