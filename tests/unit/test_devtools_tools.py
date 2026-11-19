"""browser_console and browser_network (agentos.node.devtools_tools) over the dev node's fake site:
what they show of a broken page, the egress check before a body is read, the grant and lane checks,
the untrusted label, and the vault scrub on what reaches the agent."""

from __future__ import annotations

from typing import Any

import pytest
from mcp import Client
from test_devtools_capture import Context, Page
from test_policy_support import Clock
from test_vault_support import PASSWORD, make_vault, private_home

from agentos.core.errors import HumanInControl, InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import AccountRef, Effect, LaneSpec
from agentos.core.runtime import Runtime
from agentos.dev.lanes import DevLaneManager
from agentos.dev.net import DevRequest
from agentos.dev.web import DevInteraction, new_tab
from agentos.mcp.registry import build_mcp
from agentos.mcp.toolsets import toolset_of
from agentos.node import devtools_tools, egress
from agentos.node import tools as node_tools
from agentos.node.devtools import MASKED
from agentos.policy.scrub import REDACTED
from agentos.policy.service import PolicyService
from agentos.runner.fakes import BASE

LANE = "biz-a/social"


def test_the_egress_check_follows_the_lane_list_and_the_grant() -> None:
    page = Page()
    page.context = Context()
    guard = egress.LaneGuard("biz-a/shop", sites=["shop.example"]).attach(page.context)

    class Ctx:
        principal = None
        grant = None

    assert devtools_tools.body_refusal(Ctx(), page, "shop.example") is None
    assert "not on this browser's site list" in devtools_tools.body_refusal(Ctx(), page, "cdn.other.example")
    guard.set_sites([])
    assert devtools_tools.body_refusal(Ctx(), page, "cdn.other.example") is None


# ---- the tools over the dev node ----

@pytest.fixture
async def node(tmp_path: Any) -> Any:
    lanes = DevLaneManager(make_page=new_tab)
    runtime = Runtime(interaction=DevInteraction())
    runtime.register("lanes", lanes)
    policy = PolicyService(tmp_path / "policy", runtime=runtime, clock=Clock())
    runtime.register("policy", policy)
    await lanes.create(LaneSpec(id=LANE, account=AccountRef(site="social", handle="@biza.coffee")))
    _, token = policy.issue(subject={"id": "reader"}, workspaces=["biz-a"], ceiling=Effect.READ)
    yield lanes, policy, token
    await lanes.shutdown()


def ctx_of(policy: Any, token: str, tool: str) -> Any:
    return policy.context_for_token(token, tool=tool, lane_id=LANE)


async def test_a_broken_profile_page_shows_its_error_and_its_failed_calls(node: Any) -> None:
    _, policy, token = node
    await node_tools.browser_navigate(ctx_of(policy, token, "browser_navigate"), on=LANE, url=f"{BASE}/u/biz-a")
    console = await devtools_tools.browser_console(ctx_of(policy, token, "browser_console"), on=LANE, level="error")
    assert console["untrusted"].startswith("page data:")
    [err] = console["messages"]
    assert err["kind"] == "pageerror" and err["text"] == "TypeError: user.stats is undefined"
    assert err["stack"].startswith("renderStats@https://social.example/static/app.js:212:17")
    assert console["counts"] == {"error": 1, "warning": 1, "info": 1}

    net = await devtools_tools.browser_network(ctx_of(policy, token, "browser_network"), on=LANE, failed_only=True)
    failed = {r["url"]: r for r in net["requests"]}
    assert failed[f"{BASE}/api/users/biz-a/stats"]["status"] == 500
    assert failed["https://cdn.social.example/avatars/biz-a.jpg"]["failure"] == "NS_ERROR_CONNECTION_REFUSED"

    stats_id = failed[f"{BASE}/api/users/biz-a/stats"]["id"]
    one = await devtools_tools.browser_network(ctx_of(policy, token, "browser_network"), on=LANE, id=stats_id)
    assert one["request"]["body"] == '{"error": "stats service timed out", "request_id": "req-4021"}'
    doc = next(r for r in (await devtools_tools.browser_network(ctx_of(policy, token, "browser_network"), on=LANE,
                                                               type="document"))["requests"])
    detail = await devtools_tools.browser_network(ctx_of(policy, token, "browser_network"), on=LANE, id=doc["id"])
    assert detail["request"]["response_headers"]["set-cookie"] == MASKED


async def test_a_cursor_returns_only_what_is_new_and_a_navigation_drops_old_bodies(node: Any) -> None:
    _, policy, token = node
    nav = ctx_of(policy, token, "browser_navigate")
    await node_tools.browser_navigate(nav, on=LANE, url=f"{BASE}/")
    first = await devtools_tools.browser_network(ctx_of(policy, token, "browser_network"), on=LANE)
    feed = next(r for r in first["requests"] if "/api/feed" in r["url"])
    assert f"access_token={MASKED}" in feed["url"] and "dev-feed-token" not in feed["url"]
    await node_tools.browser_navigate(nav, on=LANE, url=f"{BASE}/u/biz-a")
    newer = await devtools_tools.browser_network(ctx_of(policy, token, "browser_network"), on=LANE,
                                                 since=first["cursor"])
    assert newer["requests"] and all(int(r["id"][1:]) > first["cursor"] for r in newer["requests"])
    gone = await devtools_tools.browser_network(ctx_of(policy, token, "browser_network"), on=LANE, id=feed["id"])
    assert "no longer has this body" in gone["request"]["body_withheld"]


async def test_a_body_off_the_lane_site_list_is_withheld(node: Any) -> None:
    lanes, policy, token = node
    await node_tools.browser_navigate(ctx_of(policy, token, "browser_navigate"), on=LANE, url=f"{BASE}/u/biz-a")
    async with lanes.using(LANE, principal_kind="human") as page:
        egress.guard_for(page).set_sites(["elsewhere.example"])
    net = await devtools_tools.browser_network(ctx_of(policy, token, "browser_network"), on=LANE, min_status=500)
    one = await devtools_tools.browser_network(ctx_of(policy, token, "browser_network"), on=LANE,
                                               id=net["requests"][0]["id"])
    assert "egress guard" in one["request"]["body_withheld"] and "body" not in one["request"]


async def test_wrong_ids_and_arguments_are_refused(node: Any) -> None:
    _, policy, token = node
    ctx = ctx_of(policy, token, "browser_network")
    with pytest.raises(NotFound):
        await devtools_tools.browser_network(ctx, on=LANE, id="r999")
    for bad in ({"id": "12"}, {"min_status": 42}, {"last": 0}, {"since": -1}, {"type": "pdf"},
                {"url_contains": "x" * 500}):
        with pytest.raises(InvalidRequest):
            await devtools_tools.browser_network(ctx, on=LANE, **bad)
    with pytest.raises(InvalidRequest):
        await devtools_tools.browser_console(ctx_of(policy, token, "browser_console"), on=LANE, level="fatal")


async def test_a_lane_outside_the_grant_and_a_person_in_control_stop_them(node: Any) -> None:
    lanes, policy, token = node
    with pytest.raises(PolicyDenied):
        await devtools_tools.browser_console(policy.context_for_token(token, tool="browser_console"), on="other/lane")
    await lanes.take_control(LANE, by="owner")
    with pytest.raises(HumanInControl):
        await devtools_tools.browser_network(ctx_of(policy, token, "browser_network"), on=LANE)


async def test_what_they_return_is_labelled_untrusted(node: Any, monkeypatch: Any) -> None:
    _, policy, token = node
    labelled: list[tuple[str, Any]] = []
    monkeypatch.setattr(devtools_tools, "label_page_text", lambda ctx, tool, url=None: labelled.append((tool, url)))
    await node_tools.browser_navigate(ctx_of(policy, token, "browser_navigate"), on=LANE, url=f"{BASE}/u/biz-a")
    await devtools_tools.browser_console(ctx_of(policy, token, "browser_console"), on=LANE)
    await devtools_tools.browser_network(ctx_of(policy, token, "browser_network"), on=LANE)
    assert [t for t, _ in labelled] == ["browser_console", "browser_network"]
    assert all(url == f"{BASE}/u/biz-a" for _, url in labelled)


async def test_vault_values_never_reach_the_agent(tmp_path: Any) -> None:
    vault = make_vault(private_home(tmp_path))
    await vault.add_entry(name="sh", kind="password", domain="example.org", lanes=[LANE], secret=PASSWORD)
    lanes = DevLaneManager(make_page=new_tab)
    await lanes.create(LaneSpec(id=LANE))
    runtime = Runtime(config=lanes.config, lanes=lanes, interaction=DevInteraction(), vault=vault)
    async with lanes.using(LANE, principal_kind="human") as page:
        net = page.context
        net.fetch(DevRequest(f"{BASE}/search?q={PASSWORD}", "GET", answer=f'{{"echo": "{PASSWORD}"}}'))
        net.devtools.add_console("error", "error", f"login failed for password {PASSWORD}", page=page)
    async with Client(build_mcp(runtime, [devtools_tools.tool_specs])) as c:
        listed = await c.call_tool("browser_network", {"on": LANE})
        one = await c.call_tool("browser_network", {"on": LANE, "id": "r1"})
        console = await c.call_tool("browser_console", {"on": LANE})
    for result in (listed, one, console):
        text = "\n".join(b.text for b in result.content if b.type == "text")
        assert not result.is_error and PASSWORD not in text and REDACTED in text
    await lanes.shutdown()


def test_they_are_reads_in_the_devtools_toolset_with_plain_descriptions() -> None:
    specs = devtools_tools.tool_specs(Runtime())
    assert [s.name for s in specs] == ["browser_console", "browser_network"]
    for spec in specs:
        assert spec.effect is Effect.READ and toolset_of(spec) == "devtools"
        assert "—" not in (spec.description or "")
