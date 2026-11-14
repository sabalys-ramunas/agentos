"""Scope gaps closed by the grant families change: a trigger proposal checks a lane pattern as a pattern,
a brain task is steered only by the grant that started it (or one it was narrowed from), a brain
default is set for a workspace the grant covers (or by a grant for every workspace), an engage item is
withdrawn only by its asker's grant, a reserved subject needs the node's own issuer, and a device
credential's grants stay inside the device ceiling. Two grants may share a subject id; the grant path is
what tells them apart."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from events_support import World, no_quiet
from test_engage_items import RD, Rig, call
from test_engage_items import Ctx as EngageCtx
from test_node_panel import TOKEN, Node
from test_policy_support import make_service
from test_presence import Device, Token
from test_wsexport_api import panels  # noqa: F401 - the two-node panel fixture

from agentos.api.auth import SESSION_COOKIE
from agentos.brains import tool_specs
from agentos.brains.testing import FakeCtx, add, wait_status
from agentos.brains.testing import make_service as make_brains
from agentos.core.errors import InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect, Principal
from agentos.engage import tools as engage_tools
from agentos.engage.errors import OwnerOnly
from agentos.events.tools import trigger_propose
from agentos.policy.grants import Grant, GrantBlock, Subject

ISSUER = "node:scope-gaps"
KID = "ed25519:" + "0" * 32
DAY = 86400


def grant_of(root: str, *, workspaces: list[str] | None = None, lanes: list[str] | None = None,
             admin: bool = False) -> Grant:
    """A real Grant whose root id is `root`, in the shape a verified token gives (not signed). Two of
    these with one subject id and different roots are two families."""
    block = GrantBlock(v=2, id=root, iat=1_800_000_000, iss=ISSUER, aud=[ISSUER], kid=KID,
                       sub=Subject(id="claude-code"), workspaces=workspaces, lanes=lanes,
                       ceiling=Effect.COMMIT, admin=admin or None)
    return Grant([block])


# ---------------------------------------------------------------- 1. trigger proposals and lane patterns


class EventCtx:
    """A tool context under a real grant: lanes are the grant's, authorize and trace are recorded."""

    def __init__(self, runtime, grant: Grant):
        self.runtime, self.grant = runtime, grant
        self.principal = Principal(kind="agent", id="claude-code", grant_id=grant.id)
        self.can_access_lane = grant.lane_allowed
        self.authorized: list = []

    async def authorize(self, effect, **kw):
        self.authorized.append((effect, kw))

    def trace(self, kind, **fields):
        return None


def proposal(lane: str, grant_lanes: tuple[str, ...] = ("biz-a/instagram",)) -> dict:
    return {"name": "IG comments", "match": {"source": "watch", "workspace": "biz-a", "lane": lane},
            "target": {"kind": "brain"}, "instruction": "Draft replies.",
            "grant": {"ceiling": "draft", "lanes": list(grant_lanes)}}


async def test_a_pattern_inside_the_grant_can_be_proposed(tmp_path):
    w = World(tmp_path, settings=no_quiet())
    ctx = EventCtx(w.runtime, grant_of("gr_aaaaaaaaaa", lanes=["biz-a/*"]))
    out = await trigger_propose(ctx, trigger=proposal("biz-a/insta*"))
    assert out["state"] == "pending" and len(ctx.authorized) == 1
    assert len(w.approvals.created) == 1


@pytest.mark.parametrize("lane", ["*", "biz-b/*"])
async def test_a_pattern_wider_than_the_grant_is_refused_before_any_approval(tmp_path, lane):
    w = World(tmp_path, settings=no_quiet())
    ctx = EventCtx(w.runtime, grant_of("gr_aaaaaaaaaa", lanes=["biz-a/*"]))
    with pytest.raises(PolicyDenied, match="grant reaches"):
        await trigger_propose(ctx, trigger=proposal(lane))
    assert ctx.authorized == [] and w.approvals.created == []


async def test_a_grant_for_one_lane_cannot_propose_a_pattern_over_it(tmp_path):
    """The old check tested only the pattern's first concrete lane (biz-a/0), which this grant holds."""
    w = World(tmp_path, settings=no_quiet())
    ctx = EventCtx(w.runtime, grant_of("gr_aaaaaaaaaa", lanes=["biz-a/0"]))
    with pytest.raises(PolicyDenied, match="grant reaches"):
        await trigger_propose(ctx, trigger=proposal("biz-a/*", ("biz-a/0",)))
    assert ctx.authorized == [] and w.approvals.created == []


async def test_a_trigger_that_hears_a_whole_workspace_needs_all_of_it_whatever_lane_it_wakes(tmp_path):
    """No match.lane: it hears every lane of its workspace, so a target lane in the grant is not enough."""
    w = World(tmp_path, settings=no_quiet())
    ctx = EventCtx(w.runtime, grant_of("gr_aaaaaaaaaa", lanes=["biz-a/*"]))
    trigger = {"name": "All of biz-b", "match": {"source": "watch", "workspace": "biz-b"},
               "target": {"kind": "agent", "lane": "biz-a/claude"}, "instruction": "Read it."}
    with pytest.raises(PolicyDenied, match="grant reaches"):
        await trigger_propose(ctx, trigger=trigger)
    assert ctx.authorized == [] and w.approvals.created == []


# ---------------------------------------------------------------- 2 and 3. brain tasks and brain defaults

P = Principal(id="claude-code", thread="T-9")


class GrantedCtx(FakeCtx):
    """A brains tool context whose calls run under `grant`; its path says who started what."""

    def __init__(self, runtime, grant: Grant, principal: Principal = P):
        super().__init__(runtime, principal)
        self.grant = grant


def tools(svc):
    return {s.name: s for s in tool_specs(svc.runtime)}


@pytest.fixture
def brains(tmp_path):
    return make_brains(tmp_path)


async def test_only_the_grant_that_started_a_task_steers_it(brains):
    """Same principal id, other root grant: not the starter, nor is a narrower child of the starter's
    grant (a brain's callback grant). The starter's grant may cancel."""
    await add(brains, "claude-max", "subscription", "anthropic")
    t = tools(brains)
    starter = GrantedCtx(brains.runtime, grant_of("gr_aaaaaaaaaa", workspaces=["personal"]))
    out = await t["brain_run"].handler(starter, prompt="FAKE:slow:20", workspace="personal")
    await wait_status(brains, out["id"], until=("running",))
    stranger = GrantedCtx(brains.runtime, grant_of("gr_bbbbbbbbbb", workspaces=["personal"]))
    for name in ("brain_cancel", "brain_resume"):
        with pytest.raises(NotFound):
            await t[name].handler(stranger, id=out["id"])
    assert stranger.authorized == []  # refused before anything is asked
    child = GrantedCtx(brains.runtime, SimpleNamespace(path="/gr_aaaaaaaaaa/gr_cccccccccc/", admin=False))
    with pytest.raises(NotFound):
        await t["brain_cancel"].handler(child, id=out["id"])
    assert await t["brain_tasks"].handler(child) == []  # no lanes it reaches: not its task to see
    assert "grant_path" not in await t["brain_status"].handler(starter, id=out["id"])
    listed = await t["brain_tasks"].handler(starter)
    assert [x["id"] for x in listed] == [out["id"]] and all("grant_path" not in x for x in listed)
    sibling = GrantedCtx(brains.runtime, grant_of("gr_aaaaaaaaaa", workspaces=["personal"]))
    await t["brain_cancel"].handler(sibling, id=out["id"])
    await wait_status(brains, out["id"], until=("cancelled",))


async def test_brain_use_sets_a_default_only_for_a_covered_workspace_or_by_a_grant_for_all(brains):
    await add(brains, "claude-max", "subscription", "anthropic")
    t = tools(brains)
    narrow = GrantedCtx(brains.runtime, grant_of("gr_aaaaaaaaaa", workspaces=["biz-a"]))
    with pytest.raises(PolicyDenied, match="biz-b"):
        await t["brain_use"].handler(narrow, name="claude-max", workspace="biz-b")
    with pytest.raises(PolicyDenied, match="every workspace"):
        await t["brain_use"].handler(narrow, name="claude-max")  # the node's default
    assert narrow.authorized == []
    assert await t["brain_use"].handler(narrow, name="claude-max", workspace="biz-a") == \
        {"workspace": "biz-a", "default_brain": "claude-max"}
    every = GrantedCtx(brains.runtime, grant_of("gr_cccccccccc", workspaces=["*"]))  # what `agentos connect` issues
    assert await t["brain_use"].handler(every, name="claude-max") == \
        {"workspace": None, "default_brain": "claude-max"}


# ---------------------------------------------------------------- 4. engage item withdraw


class FamilyCtx(EngageCtx):
    """An agent's context under the grant at `path`. Every one has the same subject id."""

    def __init__(self, runtime, path: str, **kw):
        super().__init__(runtime, Principal(kind="agent", id="claude-code"), **kw)
        self.grant = SimpleNamespace(path=path, admin=False)


def test_an_item_is_withdrawn_only_by_the_grant_that_asked(tmp_path):
    rig = Rig(tmp_path)
    asked = call(engage_tools.engage_item_submit, FamilyCtx(rig.runtime, "/gr_aaaaaaaaaa/", approve=True),
                 surface=RD, kind="post", text="Hello coffee people", targets=["r/coffee"])
    assert asked["status"] == "queued"
    assert asked["requested_by"] == {"kind": "agent", "id": "claude-code", "owner": "owner"}
    stored = rig.inbox.get(asked["id"])
    assert stored["requested_by"]["grant_path"] == "/gr_aaaaaaaaaa/"  # kept on the node
    assert "grant_path" not in rig.inbox.public(stored)["requested_by"]
    with pytest.raises(OwnerOnly):  # the same subject id under another root grant: not the asker
        call(engage_tools.engage_item_withdraw, FamilyCtx(rig.runtime, "/gr_bbbbbbbbbb/"), item_id=asked["id"])
    with pytest.raises(OwnerOnly):  # nor a narrower child of the asker's grant
        call(engage_tools.engage_item_withdraw, FamilyCtx(rig.runtime, "/gr_aaaaaaaaaa/gr_cccccccccc/"),
             item_id=asked["id"])
    assert rig.inbox.get(asked["id"])["status"] == "queued"
    out = call(engage_tools.engage_item_withdraw, FamilyCtx(rig.runtime, "/gr_aaaaaaaaaa/"), item_id=asked["id"])
    assert out["status"] == "withdrawn"


# ---------------------------------------------------------------- 5. reserved subjects


@pytest.fixture
def svc(tmp_path):
    s, _ = make_service(tmp_path)
    yield s
    s.close()


def test_a_reserved_subject_needs_the_nodes_own_issuer(svc):
    with pytest.raises(InvalidRequest, match="reserved"):
        svc.issue(subject={"id": "site-key:k1"}, workspaces=["biz-a"])
    grant, token = svc.issue(subject={"id": "site-key:k1"}, workspaces=["biz-a"], _reserved=True)
    principal, same = svc.resolve(token)
    assert principal.id == "site-key:k1" and same.id == grant.id


# ---------------------------------------------------------------- 6. the device ceiling


def test_the_device_ceiling_bounds_what_a_device_grant_holds(svc):
    ceiling = svc.device_ceiling()
    assert isinstance(ceiling, Grant)
    within = {"subject": {"id": "device-claude"}, "workspaces": ["*"], "ceiling": Effect.COMMIT,
              "ttl_s": 90 * DAY, "within": ceiling}
    _, token = svc.issue(**within)
    assert token
    for change, words in (({"ceiling": Effect.IRREVERSIBLE}, "ceiling"), ({"ttl_s": 91 * DAY}, "exp"),
                          ({"specials": {"exec": {}}}, "specials.exec")):
        with pytest.raises(InvalidRequest, match=words):
            svc.issue(**{**within, **change})


# ---------------------------------------------------------------- 7. the device credential over HTTP


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    yield n
    n.close()


def test_the_device_credential_issues_grants_only_within_the_device_ceiling(node):
    device = Device(node)
    before = len(node.policy.list_grants(include_inactive=True))
    wide = device.req("POST", "/api/grants",
                      {"subject": "device-claude", "workspaces": ["biz-a"], "ceiling": "irreversible", "ttl": "7d"})
    assert 400 <= wide.status_code < 500 and "token" not in wide.json()
    assert "ceiling" in wide.text
    assert len(node.policy.list_grants(include_inactive=True)) == before
    fits = device.req("POST", "/api/grants",
                      {"subject": "device-claude", "workspaces": ["*"], "ceiling": "commit", "ttl": "90d"})
    assert fits.status_code == 200 and fits.json()["token"], fits.text
    # the node token, which `agentos connect` uses, is not bounded
    owner = Token(node, TOKEN).req("POST", "/api/grants", {"subject": "node-claude", "workspaces": ["biz-a"],
                                                          "ceiling": "irreversible", "ttl": "7d"})
    assert owner.status_code == 200, owner.text


# ---------------------------------------------------------------- 8. workspace import reissuing grants


def test_a_device_credential_cannot_make_an_import_reissue_grants(panels):  # noqa: F811
    _, pb = panels
    with pb.client() as client:
        made = client.post("/api/session/device", json={"label": "macbook"}, headers=pb.owner)
        assert made.status_code == 200, made.text
        client.cookies.set(SESSION_COOKIE, made.json()["session"])
        csrf = {"X-Agentos-CSRF": made.json()["csrf"]}
        refused = client.post("/api/workspaces/import?reissue_grants=true", content=b"not an export", headers=csrf)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "presence_required"
        # the same credential without reissue_grants gets as far as the passphrase check
        plain = client.post("/api/workspaces/import", content=b"not an export", headers=csrf)
        assert plain.status_code == 400 and "X-Agentos-Passphrase" in plain.json()["error"]["message"]
