"""MCP tools (static and dynamic flow tools), the panel API routes, and the CLI."""

import asyncio
import inspect
import json
import typing

import pytest
from pydantic import TypeAdapter
from starlette.applications import Starlette
from starlette.testclient import TestClient
from typer.testing import CliRunner

from agentos.core.errors import InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect, RunStatus
from agentos.core.tools import TOOL_NAME_RE
from agentos.runner import api
from agentos.runner.cli import app as cli_app
from agentos.runner.fakes import BASE, FakeApprovals, FakeCtx, FakePolicy, Harness, post_flow
from agentos.runner.schedules import sign_webhook
from agentos.runner.tools import tool_specs

PARAMS = {"caption": "Autumn", "image": "files/a.jpg"}


def spec(specs, name):
    return next(s for s in specs if s.name == name)


async def test_tool_specs_include_typed_flow_tools(tmp_path):
    h = Harness(tmp_path)
    h.save(post_flow())
    h.save({"id": "any.social.read", "lane": "*/social", "params": {"properties": {
        "mode": {"type": "string", "enum": ["quick", "full"], "default": "quick"}}},
        "steps": [{"type": "navigate", "url": f"{BASE}/"}]})
    with pytest.raises(InvalidRequest, match="tool name"):  # a static tool's name is never a flow's
        h.save({"id": "record.start", "lane": "biz-a/social", "steps": [{"type": "navigate", "url": f"{BASE}/"}]})
    h.save({"id": "biz-a.hidden.flow", "lane": "biz-a/social", "expose_as_tool": False,
            "steps": [{"type": "navigate", "url": f"{BASE}/"}]})
    specs = tool_specs(h.runtime)
    names = [s.name for s in specs]
    assert len(names) == len(set(names)) and all(TOOL_NAME_RE.match(n) for n in names)
    assert {"flows_list", "flow_run", "run_status", "run_cancel", "flow_save", "flow_record_start",
            "flow_record_stop", "flow_biz_a_social_post", "flow_any_social_read"} <= set(names)
    assert "flow_biz_a_hidden_flow" not in names

    post = spec(specs, "flow_biz_a_social_post")
    assert post.effect == Effect.COMMIT and post.lane_arg is None and "biz-a.social.post" in post.description
    sig = inspect.signature(post.handler)
    assert sig.parameters["caption"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["caption"].default is inspect.Parameter.empty
    assert "on" not in sig.parameters
    hints = typing.get_type_hints(post.handler, include_extras=True)
    assert TypeAdapter(hints["caption"]).json_schema() == {"type": "string", "description": "post text"}
    assert post.handler.__agentos_input_schema__["required"] == ["caption", "image"]

    read = spec(specs, "flow_any_social_read")
    rsig = inspect.signature(read.handler)
    assert read.lane_arg == "on" and rsig.parameters["on"].default is inspect.Parameter.empty
    assert rsig.parameters["mode"].default == "quick"
    assert TypeAdapter(rsig.parameters["mode"].annotation).json_schema()["enum"] == ["quick", "full"]
    assert spec(specs, "flow_run").hidden_without == "runner"


async def test_flow_run_tool_authorizes_the_flows_effect_and_returns_proof(tmp_path):
    h = Harness(tmp_path, dispatch_interval_s=0.05)
    fv = h.save(post_flow())
    ctx = FakeCtx(h.runtime)
    await h.runner.start()
    try:
        out = await spec(tool_specs(h.runtime), "flow_run").handler(ctx, name=fv.id, params=PARAMS, wait_seconds=5)
    finally:
        await h.runner.stop()
    assert out["status"] == "succeeded_verified" and out["version"] == fv.version
    assert out["summary"].startswith("done and verified")
    auth = ctx.authorized[0]
    assert auth["effect"] == Effect.COMMIT and auth["lane_id"] == "biz-a/social" and auth["domain"] == "social.example"
    assert "biz-a.social.post" in auth["summary"] and "caption='Autumn'" in auth["summary"]
    assert auth["content"] == {"flow": fv.id, "version": fv.version, "lane": "biz-a/social", "params": PARAMS}
    assert ctx.traces[0]["kind"] == "flow_run"
    run = h.runner.get(out["id"])
    assert run.authorized and run.principal["id"] == "claude-code"


async def test_dynamic_flow_tool_runs_the_flow(tmp_path):
    h = Harness(tmp_path, dispatch_interval_s=0.05)
    h.save(post_flow())
    tool = spec(tool_specs(h.runtime), "flow_biz_a_social_post")
    ctx = FakeCtx(h.runtime)
    await h.runner.start()
    try:
        out = await tool.handler(ctx, caption="From a typed tool", image="b.jpg", wait_seconds=5,
                                 idempotency_key="k-1")
        dup = await tool.handler(ctx, caption="From a typed tool", image="b.jpg", idempotency_key="k-1")
    finally:
        await h.runner.stop()
    assert out["status"] == "succeeded_verified"
    assert dup["duplicate"] and dup["id"] == out["id"]
    assert h.site.state["posts"][0]["caption"] == "From a typed tool" and len(h.site.state["posts"]) == 1


async def test_flow_run_parks_on_approval_and_resumes(tmp_path):
    approvals = FakeApprovals()
    h = Harness(tmp_path, services={"approvals": approvals})
    fv = h.save(post_flow())
    ctx = FakeCtx(h.runtime, policy=FakePolicy(approvals))
    out = await spec(tool_specs(h.runtime), "flow_run").handler(ctx, name=fv.id, params=PARAMS, wait_seconds=0)
    assert out["status"] == "waiting_approval" and out["approval_id"] == "ap_1" and "approve" in out["message"]
    approvals.set("ap_1", "approved")
    await h.runner.poll_approvals()
    await h.runner.run_until_idle()
    assert h.runner.get(out["id"]).status == RunStatus.SUCCEEDED_VERIFIED


async def test_flow_run_rejects_bad_input_before_queueing(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    run_tool = spec(tool_specs(h.runtime), "flow_run").handler
    ctx = FakeCtx(h.runtime)
    with pytest.raises(InvalidRequest, match="invalid params"):
        await run_tool(ctx, name=fv.id, params={"caption": "x"})
    with pytest.raises(InvalidRequest, match="bound to lane"):
        await run_tool(ctx, name=fv.id, params=PARAMS, on="biz-b/social")
    with pytest.raises(NotFound):
        await run_tool(ctx, name="biz-a.nope.flow")
    assert ctx.authorized == [] and h.store.list_runs() == []


async def test_status_cancel_save_and_list_tools(tmp_path):
    h = Harness(tmp_path)
    specs = tool_specs(h.runtime)
    ctx = FakeCtx(h.runtime)
    saved = await spec(specs, "flow_save").handler(ctx, definition=post_flow())
    assert saved["created"] and saved["tool"] == "flow_biz_a_social_post" and saved["warnings"] == []
    assert ctx.authorized[-1]["effect"] == Effect.DRAFT
    with pytest.raises(InvalidRequest, match="snapshot refs"):
        await spec(specs, "flow_save").handler(ctx, definition={**post_flow(), "steps": [
            {"type": "click", "locator": {"ref": "e4"}}]})

    listing = await spec(specs, "flows_list").handler(ctx)
    assert listing["flows"][0]["id"] == "biz-a.social.post"
    # QA 2026-11-15 P1-3: a flow under another namespace, or bound to a pattern, is found by its lane
    await spec(specs, "flow_save").handler(ctx, definition={**post_flow(), "id": "canary.social.post"})
    await spec(specs, "flow_save").handler(ctx, definition={**post_flow(), "id": "any.social.post", "lane": "*/social"})
    by_lane = await spec(specs, "flows_list").handler(ctx, lane="biz-a/social")
    assert {f["id"] for f in by_lane["flows"]} == {"biz-a.social.post", "canary.social.post", "any.social.post"}
    assert by_lane["lane"] == "biz-a/social" and by_lane["total"] == 3
    assert (await spec(specs, "flows_list").handler(ctx, prefix="biz-a"))["total"] == 1
    assert (await spec(specs, "flows_list").handler(ctx, lane="biz-b/blog"))["total"] == 0

    run = h.submit("biz-a.social.post", PARAMS)
    status = await spec(specs, "run_status").handler(ctx, id=run.id)
    assert status["status"] == "queued" and ctx.authorized[-1] == {
        "effect": Effect.READ, "lane_id": "biz-a/social", "domain": None, "summary": None, "content": None}
    cancelled = await spec(specs, "run_cancel").handler(ctx, id=run.id)
    assert cancelled["status"] == "cancelled" and ctx.authorized[-1]["effect"] == Effect.DRAFT


async def test_flow_get_reads_a_definition_only_inside_the_grant(tmp_path):
    from test_policy_support import Clock

    from agentos.policy.service import PolicyService

    h = Harness(tmp_path / "runner")
    policy = PolicyService(tmp_path / "policy", runtime=h.runtime, clock=Clock())
    h.runtime.register("policy", policy)
    first = h.save(post_flow())
    second = h.save(post_flow(title="Post a photo, again"))
    get = spec(tool_specs(h.runtime), "flow_get").handler
    _, mine = policy.issue(subject={"id": "agent-a"}, workspaces=["biz-a"], ceiling=Effect.DRAFT)
    _, other = policy.issue(subject={"id": "agent-b"}, workspaces=["biz-b"], ceiling=Effect.COMMIT)

    got = await get(policy.context_for_token(mine, tool="flow_get"), name="biz-a.social.post")
    assert got["version"] == second.version and got["head"] and got["effect"] == "commit"
    assert got["definition"] == second.definition.canonical() and got["definition"]["title"] == "Post a photo, again"
    old = await get(policy.context_for_token(mine, tool="flow_get"), name="biz-a.social.post", version=first.version)
    assert not old["head"] and old["definition"]["title"] == "Post a photo"
    with pytest.raises(NotFound, match="is not saved on this node"):  # outside the grant: as if it were not there
        await get(policy.context_for_token(other, tool="flow_get"), name="biz-a.social.post")
    with pytest.raises(PolicyDenied, match="installed connector operation"):
        await get(policy.context_for_token(mine, tool="flow_get"), name="connector.linkedin.post")


async def test_record_tools_make_a_draft(tmp_path):
    h = Harness(tmp_path)
    specs = tool_specs(h.runtime)
    ctx = FakeCtx(h.runtime)
    started = await spec(specs, "flow_record_start").handler(ctx, on="biz-a/social")
    assert started["recording"]
    rec = h.flows.recorder
    rec.observe("biz-a/social", {"action": "navigate", "url": f"{BASE}/compose"})
    rec.observe("biz-a/social", {"action": "fill", "locator": {"role": "textbox", "name": "Write a caption..."},
                                 "text": "Hi"})
    out = await spec(specs, "flow_record_stop").handler(ctx, on="biz-a/social", name="biz-a.social.caption")
    assert out["saved"] and out["id"] == "biz-a.social.caption" and out["events"] == 2
    assert out["definition"]["params"]["required"] == ["caption"]
    assert h.flows.get("biz-a.social.caption").version == out["version"]
    with pytest.raises(NotFound):
        await spec(specs, "flow_record_stop").handler(ctx, on="biz-a/social")


# ---------------------------------------------------------------- API


def client(h):
    app = Starlette(routes=api.routes(h.runtime) + api.public_routes(h.runtime))
    return TestClient(app)


def test_api_flows_runs_and_schedules(tmp_path):
    h = Harness(tmp_path)
    c = client(h)
    r = c.post("/api/flows", json={"definition": post_flow()})
    assert r.status_code == 201 and r.json()["version"].startswith("sha256:")
    flows = c.get("/api/flows").json()
    assert flows[0]["id"] == "biz-a.social.post" and flows[0]["params"]["required"] == ["caption", "image"]
    detail = c.get("/api/flows/biz-a.social.post").json()
    assert len(detail["versions"]) == 1 and detail["definition"]["lane"] == "biz-a/social"

    r = c.post("/api/flows/biz-a.social.post/run", json={"params": PARAMS})
    assert r.status_code == 202 and r.json()["status"] == "queued"
    run_id = r.json()["id"]
    assert h.runner.get(run_id).principal == {"kind": "human", "id": "owner", "owner": "owner",
                                              "thread": None, "grant_id": None}
    assert c.post("/api/flows/biz-a.social.post/run", json={"params": {"caption": 1}}).json()["error"]["code"] == \
        "invalid"
    runs = c.get("/api/runs?status=queued").json()
    assert [x["id"] for x in runs] == [run_id]
    assert c.get(f"/api/runs/{run_id}").json()["steps"] == []
    assert c.get("/api/runs/run_nope").status_code == 404
    assert c.post(f"/api/runs/{run_id}/cancel").json()["status"] == "cancelled"
    assert c.post(f"/api/runs/{run_id}/decide", json={"decision": "retry"}).status_code == 400

    r = c.post("/api/schedules", json={"flow": "biz-a.social.post", "cron": "0 9 * * 1-5", "params": PARAMS,
                                       "deadman_minutes": 1500})
    assert r.status_code == 201 and r.json()["cron"]["timezone"] == "Europe/Vilnius" and r.json()["next_run"]
    sid = r.json()["id"]
    assert c.post("/api/schedules", json={"flow": "biz-a.social.post", "cron": "0 9 * * *", "bogus": 1}
                  ).status_code == 400
    assert c.post(f"/api/schedules/{sid}/disable").json()["enabled"] is False
    assert [s["id"] for s in c.get("/api/schedules").json()] == [sid]
    assert c.delete(f"/api/schedules/{sid}").json() == {"deleted": sid}
    assert c.get(f"/api/schedules/{sid}").status_code == 404


def test_api_webhook_route(tmp_path):
    h = Harness(tmp_path)
    h.save(post_flow())
    c = client(h)
    r = c.post("/api/schedules", json={"flow": "biz-a.social.post", "params": PARAMS, "webhook": True})
    setup = r.json()["webhook_setup"]
    body = b"{}"
    ts = str(int(h.clock.now().timestamp()))
    headers = {"X-Agentos-Timestamp": ts, "X-Agentos-Signature": sign_webhook(setup["secret"], ts, body),
               "X-Agentos-Delivery": "evt_1"}
    ok = c.post(setup["url_path"], content=body, headers=headers)
    assert ok.status_code == 202 and ok.json()["status"] == "queued"
    assert c.post(setup["url_path"], content=body, headers=headers).json()["duplicate"] is True
    assert c.post(setup["url_path"], content=body, headers={**headers, "X-Agentos-Signature": "x"}).status_code == 401
    listed = json.dumps(c.get("/api/schedules").json())
    assert setup["secret"] not in listed


# ---------------------------------------------------------------- CLI


def test_cli_flows_runs_schedules_watches(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    cli = CliRunner()
    flow_file = tmp_path / "post.json"
    flow_file.write_text(json.dumps(post_flow()))

    r = cli.invoke(cli_app, ["flows", "save", str(flow_file)])
    assert r.exit_code == 0 and "saved" in r.output
    r = cli.invoke(cli_app, ["flows", "save", str(flow_file)])
    assert "unchanged" in r.output
    r = cli.invoke(cli_app, ["flows", "list"])
    assert "biz-a.social.post" in r.output and "never run" in r.output
    assert '"lane": "biz-a/social"' in cli.invoke(cli_app, ["flows", "show", "biz-a.social.post"]).output

    r = cli.invoke(cli_app, ["flows", "run", "biz-a.social.post", "-p", "caption=Hi", "-p", "image=a.jpg"])
    assert r.exit_code == 0 and "queued" in r.output
    run_id = r.output.split()[0]
    r = cli.invoke(cli_app, ["flows", "run", "biz-a.social.post", "-p", "caption=Hi"])
    assert r.exit_code == 1 and "invalid params" in r.output

    r = cli.invoke(cli_app, ["runs", "list"])
    assert run_id in r.output and "queued" in r.output
    r = cli.invoke(cli_app, ["runs", "show", run_id, "--json"])
    assert json.loads(r.output)["status"] == "queued"
    assert "cancelled" in cli.invoke(cli_app, ["runs", "cancel", run_id]).output
    assert cli.invoke(cli_app, ["runs", "show", "run_nope"]).exit_code == 1

    r = cli.invoke(cli_app, ["schedules", "add", "biz-a.social.post", "--cron", "30 9 * * 1-5",
                             "-p", "caption=Weekly", "-p", "image=a.jpg", "--deadman", "1500"])
    assert r.exit_code == 0, r.output
    sid = r.output.split()[0]
    assert sid in cli.invoke(cli_app, ["schedules", "list"]).output
    r = cli.invoke(cli_app, ["schedules", "add", "biz-a.social.post", "--webhook", "-p", "caption=x",
                             "-p", "image=a.jpg"])
    assert "secret (shown once)" in r.output

    r = cli.invoke(cli_app, ["watches", "add", "biz-a.social.post", "--url", f"{BASE}/shop", "--role", "status",
                             "--name", "Shop status", "--every", "120", "-p", "caption=x", "-p", "image=a.jpg"])
    assert r.exit_code == 0, r.output
    wid = r.output.split()[0]
    assert wid in cli.invoke(cli_app, ["watches", "list"]).output
    assert "removed" in cli.invoke(cli_app, ["watches", "remove", wid]).output
    assert "removed" in cli.invoke(cli_app, ["schedules", "remove", sid]).output
    assert "removed" in cli.invoke(cli_app, ["flows", "remove", "biz-a.social.post"]).output


def test_cli_run_is_executed_by_the_daemon(tmp_path, monkeypatch):
    """The CLI only queues; a runner on the same home picks the run up."""
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    h = Harness(tmp_path)
    h.save(post_flow())
    r = CliRunner().invoke(cli_app, ["flows", "run", "biz-a.social.post", "-p", "caption=From CLI",
                                     "-p", "image=a.jpg"])
    run_id = r.output.split()[0]
    asyncio.run(h.runner.run_until_idle())
    run = h.runner.get(run_id)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED and run.source == "cli"
    assert h.site.state["posts"][0]["caption"] == "From CLI"
