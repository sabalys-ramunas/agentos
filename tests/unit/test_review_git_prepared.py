"""Review repros (git and prepared machines). Each is xfail(strict) with its finding id: a fix flips it."""

from __future__ import annotations

import asyncio

import pytest
from test_environments_prepared import REPO, FakeGit, builds_done, node  # noqa: F401  (node is a fixture)
from test_environments_prproofs import ENV, PNG, GitHub, setup

from agentos.environments import prepbuild, pulls


class RecordingGit(FakeGit):
    """FakeGit that remembers which grant each VM's gateway token was minted for."""

    def __init__(self):
        super().__init__()
        self.grant_of: dict[str, str] = {}
        self.fetch_only: dict[str, bool] = {}

    def vm_token(self, lane, *, grant_id, repo, fetch_only=False):
        self.grant_of[lane] = grant_id
        self.fetch_only[lane] = fetch_only
        return super().vm_token(lane, grant_id=grant_id, repo=repo)


async def test_review_G1_agent_started_build_token_is_the_agents_grant(node):  # noqa: F811
    git = RecordingGit()
    node.rt.register("git", git)
    out = await node.call("environment_open", title="Shop", repo=REPO)
    assert out["state"] == "ready", out
    await builds_done(node.mgr)
    agent_grant = node.mgr.get(out["id"]).grant_id
    builds = {lane: g for lane, g in git.grant_of.items() if lane.startswith("env/prep-")}
    assert builds, "the open started a prepared build with a gateway token"
    # the repository's setup runs there with this token: it must carry the opener's grant, never the owner's
    assert set(builds.values()) == {agent_grant}
    assert all(git.fetch_only[lane] for lane in builds)  # and it fetches only


async def test_review_G1_the_owners_build_from_the_cli_is_the_owners_and_fetch_only(node):  # noqa: F811
    git = RecordingGit()
    node.rt.register("git", git)
    assert (await node.mgr.prepare_now(REPO))["started"]
    await builds_done(node.mgr)
    [lane] = [lane for lane in git.grant_of if lane.startswith("env/prep-")]
    assert git.grant_of[lane] == "owner" and git.fetch_only[lane]


async def test_review_G1_a_revoked_grant_stops_its_build(node, monkeypatch):  # noqa: F811
    git = RecordingGit()
    node.rt.register("git", git)
    monkeypatch.setattr(prepbuild, "GRANT_CHECK_S", 0.01)
    real_exec, revoked = node.machine.exec, {}

    async def exec_(lane_id, cmd, **kw):
        if lane_id.startswith("env/prep-") and "pnpm install --frozen-lockfile" in cmd:
            node.policy.revoke(git.grant_of[lane_id])  # the agent's grant ends while its build's setup runs
            revoked["at"] = lane_id
            await asyncio.sleep(1)
        return await real_exec(lane_id, cmd, **kw)

    monkeypatch.setattr(node.envs, "exec", exec_)
    out = await node.call("environment_open", title="Shop", repo=REPO)
    assert out["state"] == "ready", out
    await builds_done(node.mgr)
    [rec] = node.mgr.prepared_store().all()
    assert revoked and rec.state == "failed" and "no longer holds" in rec.error, rec
    assert not node.prov.snapshots
    assert not [r for r in node.vms.store.live() if r.lane_id.startswith("env/prep-")]  # its machine is gone


async def test_review_G2_prepared_key_is_per_commit(node):  # noqa: F811
    # The agent's branch has the same .agentos/environment.toml and lockfile as main (config_probe answers
    # the same for every ref), but its setup may run any script of that branch. The key ignores the commit.
    first = await node.call("environment_open", title="Agent branch", repo=REPO, ref="agent/tampered")
    await builds_done(node.mgr)
    [rec] = node.mgr.prepared_store().all()
    assert rec.state == "ready"
    assert any("git checkout -q agent/tampered" in c for c in node.machine.cmds("env/prep-"))  # built from it
    await node.call("environment_close", on=first["on"], discard=True)
    other = await node.call("environment_open", title="Main work", repo=REPO, session="chat-2")
    assert other["prepared"]["used"] is False, "an open of the default branch booted the agent branch's image"
    await node.call("environment_close", on=other["on"], discard=True)
    await builds_done(node.mgr)
    # the default branch's own build is shared; the branch's build stays the branch's (newest per ref)
    assert {r.ref for r in node.mgr.prepared_store().all() if r.state == "ready"} == {None, "agent/tampered"}
    again = await node.call("environment_open", title="Branch again", repo=REPO, ref="agent/tampered", session="chat-3")
    assert again["prepared"]["used"] is True  # the same ref keeps its speedup
    await node.call("environment_close", on=again["on"], discard=True)
    main = await node.call("environment_open", title="Main again", repo=REPO, session="chat-4")
    assert main["prepared"]["used"] is True


def test_review_G3_scrub_keeps_ssh_where_the_provider_keeps_it():
    assert "AGENTOS_KEEP_SSH" in prepbuild.SCRUB


async def test_review_G3_the_build_runs_the_scrub_with_the_providers_prefix(node, monkeypatch):  # noqa: F811
    monkeypatch.setattr(type(node.prov), "script_prefix", "export AGENTOS_KEEP_SSH=1\n", raising=False)
    real_exec, ran = node.machine.exec, []

    async def exec_(lane_id, cmd, **kw):
        if cmd.endswith(prepbuild.SCRUB):
            ran.append(cmd)
            cmd = prepbuild.SCRUB  # the fake machine answers the scrub itself
        return await real_exec(lane_id, cmd, **kw)

    monkeypatch.setattr(node.envs, "exec", exec_)
    assert (await node.mgr.prepare_now(REPO))["started"]
    await builds_done(node.mgr)
    assert ran == ["export AGENTOS_KEEP_SSH=1\n" + prepbuild.SCRUB]
    assert node.mgr.prepared_store().all()[0].state == "ready"


@pytest.mark.xfail(strict=True, reason="G4: a file already on agentos-proofs is trusted by name, not content")
async def test_review_G4_a_planted_proof_is_not_shown_as_the_proof(tmp_path):
    gh = GitHub()
    mgr, env, _ = setup(tmp_path, gh)
    # something else (the agent's own gateway push, say) put a file with the proof's name on the branch first
    gh.blobs["planted"] = b"not the screenshot"
    gh.trees["t-planted"] = {f"{ENV}/pshot000001.png": "planted"}
    gh.commits["c-planted"] = {"tree": "t-planted", "parents": []}
    gh.ref = "c-planted"
    await pulls.open_pull_request(mgr, env, title="Fix", proofs=["cart after the fix"])
    assert gh.files()[f"{ENV}/pshot000001.png"] == PNG
