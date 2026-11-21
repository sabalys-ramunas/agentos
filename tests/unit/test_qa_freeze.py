"""QA bug hunt on the break-glass freeze (openajar.freeze). Every test asserts the behaviour the spec
(the module docstrings) promises and is marked xfail(strict) while the bug
stands, so a fix turns it into a failure that says "remove the xfail"."""

from __future__ import annotations

import asyncio
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from freeze_support import close, make_node, started
from testkit.timing import until

from openajar.core import signing
from openajar.core.errors import InvalidRequest
from openajar.core.models import Effect
from openajar.policy import bind_request_token, wrap_tool
from openajar.policy.errors import Denied
from openajar.policy.freeze_store import FreezeStore, scope_key


@pytest.fixture
async def node(tmp_path):
    n = await started(make_node(tmp_path))
    yield n
    await close(n)


def _state(n, grant_id: str) -> str:
    return {g["id"]: g["state"] for g in n.policy.list_grants(include_inactive=True)}[grant_id]


def _survives(policy, token: str) -> bool:
    try:
        principal, grant = policy.resolve(token)
        policy.authorize_call(policy.context(grant, principal=principal, tool="t"), "read", lane_id="biz-a/one")
    except Denied:
        return False
    return True


# ---- grants that survive the sweep ----------------------------------------------------------------


async def test_a_node_freeze_revokes_every_agent_grant_even_one_without_lanes(node):
    # A node freeze revokes "every agent grant". Reach.grant() treats an authority block
    # with no workspaces and no lanes ([] patterns) as reaching nothing, so it is skipped.
    g, _ = node.policy.issue(subject={"id": "web-agent"}, domains=["example.com"], ceiling=Effect.COMMIT)
    report = await node.freeze.freeze("node", None, reason="stop everything", by="owner")
    await node.freeze.unfreeze("node", None, by="owner")
    assert g.id in {x["id"] for x in report["revoked"]["grants"]}
    assert _state(node, g.id) == "revoked"


async def test_a_local_freeze_revokes_a_controller_grant_that_reaches_only_the_lane(tmp_path):
    from openajar.policy import tokens
    from openajar.policy.grants import GrantBlock
    from openajar.policy.issuers import SERVICE_NAME, TrustedIssuers
    from openajar.policy.util import new_id

    org = Ed25519PrivateKey.generate()
    issuers = TrustedIssuers(tmp_path)
    issuers.trust("org:test", signing.public_b64(org))
    n = await started(make_node(tmp_path, extra={SERVICE_NAME: issuers}))
    try:
        block = {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "iat": int(time.time()),
                 "sub": {"kind": "agent", "id": "helper", "owner": "owner"}, "lanes": ["biz-a/one"],
                 "ceiling": "draft", "exp": int(time.time()) + 600, "depth": 2}
        tok = tokens.mint(org, GrantBlock.model_validate(block).to_data())
        assert _survives(n.policy, tok)  # a live org grant over biz-a/one
        await n.freeze.freeze("lane", "biz-a/one", reason="the page asked for the password", by="owner")
        await n.freeze.unfreeze("lane", "biz-a/one", by="owner")
        assert not _survives(n.policy, tok), "unfreezing restored an agent's grant over the frozen lane"
    finally:
        await close(n)
        issuers.close()


async def test_a_repeated_mesh_freeze_runs_the_sweep_again(node):
    # The promise: "Freezing again runs the sweep again". A local re-freeze does; apply_mesh does not.
    node.freeze.apply_mesh({"frozen": True, "reason": "first", "by": "owner", "freeze_id": "fz_first"})
    await asyncio.sleep(0.2)
    g, _ = node.agent(workspaces=["biz-a"], sub="granted-since", during_freeze=True)
    node.freeze.apply_mesh({"frozen": True, "reason": "again", "by": "owner", "freeze_id": "fz_second"})
    assert await until(lambda: _state(node, g.id) == "revoked")


# ---- the mesh ---------------------------------------------------------------------------------------


async def test_a_mesh_freeze_during_a_reconnect_still_reaches_the_node(tmp_path):
    from channel_support import start_controller, wait_for
    from test_freeze_mesh import start

    ctl = await start_controller(tmp_path / "controller")
    hub = ctl.controller.hub
    a = await start(tmp_path / "a", "node-a", ctl, ["personal/reddit"])
    await wait_for(lambda: len(hub.sessions) == 1)
    a_config = a.config
    await a.close()
    await wait_for(lambda: len(hub.sessions) == 0)
    real_register = hub.register
    fired: dict = {}

    async def register(session):  # the owner presses mesh freeze in the gap of connect._session
        if not fired:
            fired["out"] = await ctl.controller.mesh_freeze(ctl.org, reason="an agent went rogue", by="owner")
        await real_register(session)

    hub.register = register
    a2 = None
    try:
        a2 = await start(tmp_path / "a", "node-a", ctl, ["personal/reddit"], config=a_config)
        await wait_for(lambda: len(hub.sessions) == 1)
        freeze = a2.runtime.get("freeze")
        deadline = time.monotonic() + 2.0
        while not freeze.is_frozen() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert fired["out"]["frozen"] is True
        assert freeze.is_frozen(), (f"node-a is online and unfrozen; the controller still lists it as pending: "
                                    f"{ctl.controller.mesh_status(ctl.org)['pending']}")
    finally:
        hub.register = real_register
        if a2 is not None:
            await a2.close()
        await ctl.close()


# ---- the freeze files, the CLI and the daemon --------------------------------------------------------


async def test_a_cli_unfreeze_during_the_sweep_is_not_undone(tmp_path):
    n = await started(make_node(tmp_path, stop_wait_s=0.5))
    try:
        entered, release = asyncio.Event(), asyncio.Event()

        async def wedged():
            async with n.lanes.using("biz-a/one", principal_kind="agent"):
                entered.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    await release.wait()  # a browser call that takes a moment to let go
                    raise

        w = asyncio.create_task(wedged())
        await entered.wait()
        sweep = asyncio.create_task(n.freeze.freeze("lane", "biz-a/one", reason="r", by="owner"))
        await asyncio.sleep(0.1)
        cli = FreezeStore(tmp_path)
        key = scope_key("lane", "biz-a/one")
        assert cli.path(key).exists()
        cli.remove(key)  # what `openajar unfreeze lane biz-a/one` does on the node's shell
        await sweep
        n.freeze.reload()  # the daemon's next poll
        release.set()
        await asyncio.gather(w, return_exceptions=True)
        assert not cli.path(key).exists(), "the marker the CLI deleted was written again"
        assert not n.freeze.is_frozen("biz-a/one")
    finally:
        await close(n)


async def test_unfreeze_never_reports_success_for_a_freeze_that_comes_back(node):
    (node.freeze.store.dir / "galaxy.far-away.frozen").write_text("{}")  # a marker this version cannot parse
    node.freeze.reload()
    assert node.freeze.is_frozen("biz-a/one") and node.freeze.problem
    out = await node.freeze.unfreeze("node", None, by="owner")
    node.freeze.reload()  # the daemon's next poll (it also spawns a full sweep for the record)
    lied = out["unfrozen"] and out["still_frozen_by"] is None and node.freeze.is_frozen("biz-a/one")
    assert not lied, f"unfreeze said {out['unfrozen']=} {out['still_frozen_by']=}, the node is frozen again"


# ---- brain tasks ------------------------------------------------------------------------------------


async def test_a_freeze_cancels_an_open_brain_task_behind_500_finished_ones(tmp_path):
    from types import SimpleNamespace

    from openajar.brains.models import TaskRecord, TaskStatus
    from openajar.brains.store import Store

    store = Store(tmp_path / "brains" / "brains.db")
    store.insert_task(TaskRecord(id="bt_old_queued", workspace="biz-a", principal_id="agent-1",
                                 status=TaskStatus.QUEUED, lanes=["biz-a/one"]))
    store.x("UPDATE tasks SET created_at = created_at - 3600 WHERE id = 'bt_old_queued'")
    for i in range(500):
        store.insert_task(TaskRecord(id=f"bt_done_{i}", workspace="biz-a", principal_id="agent-1",
                                     status=TaskStatus.SUCCEEDED_UNVERIFIED, lanes=["biz-a/one"]))
    cancelled: list[str] = []

    async def cancel(task_id, principal=None, *, wait=False):
        cancelled.append(task_id)

    brains = SimpleNamespace(store=store, cancel=cancel)
    n = await started(make_node(tmp_path, extra={"brains": brains}))
    try:
        report = await n.freeze.freeze("node", None, reason="stop everything", by="owner")
        assert "bt_old_queued" in report["brain_tasks_cancelled"], "a queued brain task was left to start later"
    finally:
        await close(n)
        store.close()


# ---- what agents and links are told -------------------------------------------------------------------


async def test_lane_control_on_a_frozen_lane_does_not_say_you_may_act(node):
    from openajar.node.control_tools import tool_specs as control_specs

    fns = {s.name: wrap_tool(s, node.runtime) for s in control_specs(node.runtime)}
    await node.freeze.freeze("lane", "biz-a/one", reason="stop", by="owner")
    with bind_request_token(node.config.mcp_token):  # the node token Claude Code drives lanes with
        out = await fns["browser_control"](on="biz-a/one")
    assert out["agent_may_act"] is False, out["next"]
    assert "frozen" in out["next"]


async def test_a_garbled_link_signature_is_refused_as_not_valid(node):
    params = node.freeze.link_params("lane", "biz-a/one")
    try:
        await node.freeze.redeem_link({**params, "s": "été"})
        outcome = "accepted"
    except InvalidRequest:
        outcome = "not valid"
    except Exception as exc:  # noqa: BLE001
        outcome = type(exc).__name__
    assert outcome == "not valid"
    assert not node.freeze.is_frozen("biz-a/one")
