"""Proof screenshots of an environment (agentos.environments.proofs), browser_screenshot's proof, width and
full_page, the app's proof routes, and the compare link for a pushed branch."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fakes import PNG_1X1, FakeInteraction, FakeLaneManager
from mcp import Client
from test_environments import node, opened  # noqa: F401  (node is a fixture)
from test_environments_isolation import _record, panel  # noqa: F401  (panel is a fixture)

from agentos.core.config import NodeConfig
from agentos.core.errors import InvalidRequest
from agentos.core.models import LaneSpec, utcnow
from agentos.core.runtime import Runtime
from agentos.environments import proofs as proofs_mod
from agentos.environments.models import Push
from agentos.environments.tools import compare_link
from agentos.mcp.registry import build_mcp
from agentos.node.tools import INSTRUCTIONS, tool_specs
from agentos.node.viewer_tokens import ViewTokens


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def token_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["t"][0]


@pytest.fixture
def tokens(node, tmp_path):  # noqa: F811
    clock = Clock()
    t = ViewTokens(tmp_path / "viewer.key", clock=clock)
    node["rt"].register("viewer_links", t)
    return t, clock


async def test_a_kept_proof_is_listed_with_a_signed_link_and_outlives_the_close(node, tokens):  # noqa: F811
    env = node["mgr"].get((await opened(node))["id"])
    mgr = node["mgr"]
    kept = mgr.keep_proof(env, PNG_1X1, title="checkout page after the fix", page_url="http://localhost:3000/cart",
                          width=1, height=1)
    assert set(kept) >= {"id", "title", "taken_at", "page_url", "width", "height", "url", "expires_at"}
    assert kept["url"].startswith(f"http://localhost:{node['rt'].get('config').mcp_port}/view/env/{env.id}-browser"
                                  f"/proof/{kept['id']}.png?t=")
    assert mgr.signed_proof("env", f"{env.id}-browser", kept["id"], token_of(kept["url"])) == PNG_1X1
    assert (mgr.home / "environments" / env.id / "proofs" / f"{kept['id']}.png").read_bytes() == PNG_1X1
    seen = await node["call"]("environment")
    assert [p["title"] for p in seen["proofs"]] == ["checkout page after the fix"] and seen["proofs"][0]["url"]
    closed = await node["call"]("environment_close", discard=True)
    assert [p["id"] for p in closed["proofs"]] == [kept["id"]]
    fresh = closed["proofs"][0]["url"]
    assert mgr.signed_proof("env", f"{env.id}-browser", kept["id"], token_of(fresh)) == PNG_1X1  # the VM is gone
    assert mgr.browser_environment(env.browser_lane) is None  # its browser is no environment's any more


async def test_a_proof_link_opens_only_its_proof_and_only_for_a_while(node, tokens):  # noqa: F811
    t, clock = tokens
    env = node["mgr"].get((await opened(node))["id"])
    mgr = node["mgr"]
    one = mgr.keep_proof(env, PNG_1X1, title="one")
    two = mgr.keep_proof(env, PNG_1X1, title="two")
    tok = token_of(one["url"])
    assert mgr.signed_proof("env", f"{env.id}-browser", two["id"], tok) is None  # another proof
    assert mgr.signed_proof("personal", f"{env.id}-browser", one["id"], tok) is None
    assert mgr.signed_proof("env", f"{env.id}-browser", one["id"], tok + "x") is None
    view_link = t.issue(env.browser_lane, "control").token  # a live view link is not a proof link
    assert mgr.signed_proof("env", f"{env.id}-browser", one["id"], view_link) is None
    clock.now += proofs_mod.PROOF_LINK_S + 1
    assert mgr.signed_proof("env", f"{env.id}-browser", one["id"], tok) is None


async def test_proofs_are_bounded_and_pruned_after_their_retention(node, tokens, monkeypatch):  # noqa: F811
    env = node["mgr"].get((await opened(node))["id"])
    mgr = node["mgr"]
    monkeypatch.setattr(proofs_mod, "PROOFS_MAX", 2)
    mgr.keep_proof(env, PNG_1X1, title="a")
    mgr.keep_proof(env, PNG_1X1, title="b")
    with pytest.raises(InvalidRequest, match="at most 2 proofs"):
        mgr.keep_proof(env, PNG_1X1, title="c")
    with pytest.raises(InvalidRequest, match="a PNG"):
        mgr.keep_proof(env, b"GIF89a", title="d")
    assert mgr.prune_proofs() == 0  # open: kept
    await node["call"]("environment_close", discard=True)
    later = utcnow() + timedelta(days=proofs_mod.PROOF_DAYS - 1)
    assert mgr.prune_proofs(later) == 0 and len(mgr.proofs(env)) == 2
    assert mgr.prune_proofs(utcnow() + timedelta(days=proofs_mod.PROOF_DAYS + 1)) == 1
    assert mgr.proofs(env) == []


async def test_the_lane_lookups_name_only_an_environments_own_lanes(node):  # noqa: F811
    env = node["mgr"].get((await opened(node))["id"])
    mgr = node["mgr"]
    assert mgr.workdir_of(env.terminal_lane) == env.workdir and mgr.workdir_of(env.browser_lane) is None
    assert mgr.browser_environment(env.browser_lane).id == env.id and mgr.is_browser_lane(env.browser_lane)
    assert not mgr.is_browser_lane(env.terminal_lane)
    for lane in ("personal/reddit", "env/eunknown0000-browser", "env/../x", f"env/{env.id}-browserx", ""):
        assert mgr.lane_environment(lane) is None and not mgr.is_browser_lane(lane)


# ------------------------------------------------------------------ git: the compare link

def test_the_compare_link_is_for_a_branch_on_github_other_than_the_default():
    env = SimpleNamespace(repo="github.com/me/shop", ref=None, branch="main")
    assert compare_link(env, "fix/cart") == "https://github.com/me/shop/compare/fix/cart?expand=1"
    assert compare_link(env, "main") is None  # the default branch it was opened on
    assert compare_link(SimpleNamespace(repo="github.com/me/shop", ref="main", branch="main"), "main")
    assert compare_link(SimpleNamespace(repo="gitlab.com/me/shop", ref=None, branch="main"), "x") is None
    assert compare_link(SimpleNamespace(repo=None, ref=None, branch=None), "x") is None


async def test_environment_gives_the_compare_link_after_a_push(node):  # noqa: F811
    out = await opened(node, repo="github.com/me/shop")
    mgr = node["mgr"]

    def pushed(e):
        e.pushed.append(Push(branch="fix/cart", commit="ec7699d43630d8bd", at=utcnow()))
        e.pushed.append(Push(branch="gone", commit="0" * 40, at=utcnow()))  # a delete: no pull request

    mgr.store.update(out["id"], pushed)
    seen = await node["call"]("environment")
    assert seen["git"]["compare"] == "https://github.com/me/shop/compare/fix/cart?expand=1"
    by_branch = {p["branch"]: p for p in seen["git"]["pushed"]}
    assert by_branch["fix/cart"]["compare"] == seen["git"]["compare"] and "compare" not in by_branch["gone"]
    closed = await node["call"]("environment_close", discard=True)
    assert any(p.get("compare") == seen["git"]["compare"] for p in closed["pushed"])


# ------------------------------------------------------------------ browser_screenshot

class Envs:
    """The environments service as browser_screenshot asks it."""

    def __init__(self, lane):
        self.lane, self.kept = lane, []

    def browser_environment(self, lane_id):
        return SimpleNamespace(id="eabcdefghjk") if lane_id == self.lane else None

    def keep_proof(self, env, png, **kw):
        self.kept.append((env.id, png, kw))
        return {"id": "pabcdefghjk", "title": kw["title"], "url": "http://localhost:7400/view/x?t=y"}


class ShotInteraction(FakeInteraction):
    def __init__(self):
        super().__init__()
        self.shots: list[dict] = []

    async def screenshot(self, page, **kw):
        self.shots.append(kw)
        return PNG_1X1


ENV_BROWSER = "env/eabcdefghjk-browser"


@pytest.fixture
async def shots(monkeypatch):
    from agentos.dev.lanes import FakePage

    sizes: list[dict] = []

    async def evaluate(self, script, *args):
        return {"w": 1920, "h": 960} if "innerWidth" in script else True

    async def set_viewport_size(self, size):
        sizes.append(size)

    monkeypatch.setattr(FakePage, "evaluate", evaluate, raising=False)
    monkeypatch.setattr(FakePage, "set_viewport_size", set_viewport_size, raising=False)
    monkeypatch.setattr("agentos.node.tools.shots.REFLOW_S", 0)
    lanes = FakeLaneManager(NodeConfig(name="test-node"))
    for lane in (ENV_BROWSER, "personal/reddit"):
        await lanes.create(LaneSpec(id=lane))
    inter, envs = ShotInteraction(), Envs(ENV_BROWSER)
    runtime = Runtime(config=lanes.config, lanes=lanes, interaction=inter, environments=envs)
    return runtime, inter, envs, sizes


def mcp(runtime) -> Client:
    return Client(build_mcp(runtime, [tool_specs], instructions=INSTRUCTIONS))


async def test_a_proof_is_kept_and_returned_with_its_link(shots):
    runtime, _, envs, _ = shots
    async with mcp(runtime) as client:
        result = await client.call_tool("browser_screenshot", {"on": ENV_BROWSER, "proof": "  cart  after the fix "})
    kinds = [b.type for b in result.content]
    assert not result.is_error and kinds == ["image", "text"]
    assert '"url":"http://localhost:7400/view/x?t=y"' in result.content[1].text
    assert envs.kept[0][2]["title"] == "cart after the fix" and envs.kept[0][2]["width"] == 1


async def test_width_lays_the_page_out_for_the_shot_and_back(shots):
    runtime, inter, _, sizes = shots
    async with mcp(runtime) as client:
        result = await client.call_tool("browser_screenshot", {"on": ENV_BROWSER, "width": 390, "full_page": True})
    assert not result.is_error and [b.type for b in result.content] == ["image"]
    assert sizes == [{"width": 390, "height": 960}, {"width": 1920, "height": 960}]
    assert inter.shots == [{"full_page": True}]


async def test_full_page_works_anywhere_and_width_and_proof_only_in_an_environment(shots):
    runtime, inter, envs, sizes = shots
    async with mcp(runtime) as client:
        plain = await client.call_tool("browser_screenshot", {"on": "personal/reddit", "full_page": True})
        assert not plain.is_error and inter.shots == [{"full_page": True}]
        for args in ({"width": 390}, {"proof": "x"}):
            r = await client.call_tool("browser_screenshot", {"on": "personal/reddit", **args})
            assert r.is_error and "environment's browser" in r.content[0].text
        bad = await client.call_tool("browser_screenshot", {"on": ENV_BROWSER, "width": 100})
    assert bad.is_error and sizes == [] and envs.kept == []


# ------------------------------------------------------------------ the app's routes

def test_the_app_lists_proofs_and_serves_their_png(panel, tmp_path):  # noqa: F811
    from test_api_members import owner

    api_node, mgr = panel
    boss = owner(api_node)
    env = _record(mgr, "eproofsagnt", member=None, root="gr_owner_agent")
    kept = mgr.keep_proof(env, PNG_1X1, title="home page", page_url="http://localhost:3000/", width=1, height=1)
    base = f"/api/orgs/local/environments/{env.id}"
    detail = boss.get(base)
    assert detail.status_code == 200, detail.text
    [listed] = detail.json()["proofs"]
    assert listed == {"id": kept["id"], "title": "home page", "taken_at": kept["taken_at"],
                      "page_url": "http://localhost:3000/", "width": 1, "height": 1,
                      "src": f"{base}/proofs/{kept['id']}.png"}
    png = boss.get(listed["src"])
    assert png.status_code == 200 and png.content == PNG_1X1 and png.headers["content-type"] == "image/png"
    assert boss.get(f"{base}/proofs/pnotthere00.png").status_code == 404
    assert boss.get("/api/orgs/local/environments/enothere000/proofs/pnotthere00.png").status_code == 404


async def test_every_environments_proofs_together_stay_under_the_nodes_cap(node, tokens, monkeypatch):  # noqa: F811
    """An agent that opens and closes environment after environment cannot fill the node's disk with
    proofs: past the node's cap the oldest proofs of closed environments go, and while only open ones
    hold it, a new proof is refused."""
    mgr = node["mgr"]
    old = mgr.get((await opened(node))["id"])
    mgr.keep_proof(old, PNG_1X1, title="old one")
    await node["call"]("environment_close", discard=True)
    monkeypatch.setattr(proofs_mod, "PROOF_NODE_BYTES_MAX", len(PNG_1X1) * 2)
    env = mgr.get((await opened(node, title="the next one"))["id"])
    mgr.keep_proof(env, PNG_1X1, title="first")
    mgr.keep_proof(env, PNG_1X1, title="second")  # the closed environment's proof makes room
    assert mgr.proofs(old) == [] and [p["title"] for p in mgr.proofs(env)] == ["first", "second"]
    with pytest.raises(InvalidRequest, match="keeps at most"):
        mgr.keep_proof(env, PNG_1X1, title="third")  # only an open one's left: refused, nothing removed
    assert len(mgr.proofs(env)) == 2
