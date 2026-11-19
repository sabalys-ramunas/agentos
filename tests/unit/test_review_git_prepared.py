"""Review repros (git and prepared machines). Each is xfail(strict) with its finding id: a fix flips it."""

from __future__ import annotations

import pytest
from test_environments_prepared import REPO, FakeGit, builds_done, node  # noqa: F401  (node is a fixture)
from test_environments_prproofs import ENV, PNG, GitHub, setup

from agentos.environments import prepbuild, pulls


class RecordingGit(FakeGit):
    """FakeGit that remembers which grant each VM's gateway token was minted for."""

    def __init__(self):
        super().__init__()
        self.grant_of: dict[str, str] = {}

    def vm_token(self, lane, *, grant_id, repo):
        self.grant_of[lane] = grant_id
        return super().vm_token(lane, grant_id=grant_id, repo=repo)


@pytest.mark.xfail(strict=True, reason="G1: a build an agent's open starts gets an owner-grant gateway token")
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


@pytest.mark.xfail(strict=True, reason="G2: a prepared machine built from one ref is booted for another")
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


@pytest.mark.xfail(strict=True, reason="G3: the prepared build's scrub removes the SSH keys Morph's gateway needs")
def test_review_G3_scrub_keeps_ssh_where_the_provider_keeps_it():
    assert "AGENTOS_KEEP_SSH" in prepbuild.SCRUB


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
