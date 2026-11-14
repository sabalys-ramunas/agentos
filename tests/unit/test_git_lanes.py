"""Git from inside an env lane and a brain task (agentos.git.service with agentos.env and agentos.brains):
a command a grant starts gets git config that points the declared hosts at the gateway with its own
token, clones and pushes through it, and never sees the deploy key, in its environment or its files.
The lane's token ends when the lane goes cold. On the fake host, workloads run on this machine."""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from test_policy_support import Clock, make_service
from test_vault_support import make_vault, private_home

from agentos.core.models import Effect
from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher
from agentos.git import keys
from agentos.git.gateway import Refused
from agentos.git.service import GitService

HOST, REPO, LANE = "example.test", "owner/app", "dev/code"

pytestmark = pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")


def git(*args, cwd=None):
    env = {"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout


@pytest.fixture
async def node(tmp_path):
    home = private_home(tmp_path)
    policy, rt = make_service(home, clock=Clock())
    vault = make_vault(home)
    await vault.start(control=False)
    rt.register("vault", vault)
    gitsvc = GitService(rt, home, allow_file=True, host_port=0, lane_port=0)
    rt.register("git", gitsvc)
    mgr = EnvManager(home, runtime=rt, host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0, max_timeout_s=60))
    rt.register("envs", mgr)
    mgr.create(LANE, preset="sandbox", overrides={"filesystem": "overlay", "network": "isolated"})
    upstream = tmp_path / "up.git"
    seed = tmp_path / "seed"
    git("init", "-q", "--bare", "-b", "main", str(upstream))
    git("init", "-q", "-b", "main", str(seed))
    (seed / "README").write_text("hi\n")
    git("add", ".", cwd=seed)
    git("commit", "-qm", "first", cwd=seed)
    git("push", "-q", str(upstream), "main", "main:feature", cwd=seed)
    private, public, _ = keys.generate_ed25519("lane-test")
    await vault.put_secret("git:app", private.reveal(), kind=keys.SSH_KIND, label="k",
                           scope=keys.ssh_scope(HOST, [REPO], public))
    gitsvc.remotes.allow("dev", f"{HOST}/{REPO}", url=str(upstream))
    g, _ = policy.issue(subject={"id": "agent-1"}, workspaces=["dev"], ceiling=Effect.IRREVERSIBLE)
    yield {"rt": rt, "mgr": mgr, "git": gitsvc, "grant": g, "upstream": upstream, "private": private.reveal(),
           "policy": policy, "home": home}
    await mgr.shutdown()
    await gitsvc.stop()
    await vault.stop()
    policy.close()


async def test_a_lane_command_clones_and_pushes_without_ever_seeing_the_key(node):
    mgr, g = node["mgr"], node["grant"]
    env = await mgr.exec(LANE, "env", grant_id=g.id)
    lines = dict(x.split("=", 1) for x in env.stdout.splitlines() if "=" in x)
    count = int(lines["GIT_CONFIG_COUNT"])
    pairs = [(lines[f"GIT_CONFIG_KEY_{i}"], lines[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)]
    port = mgr._rts[LANE].git_port
    assert (f"url.http://127.0.0.1:{port}/{HOST}/.insteadOf", f"git@{HOST}:") in pairs
    assert pairs[-1][1].startswith("x-agentos-git: agtg_")
    script = ("set -e; git clone -q git@example.test:owner/app.git app; cd app; git checkout -q feature; "
              "echo more > f; git add f; git -c user.name=a -c user.email=a@x commit -qm more; "
              "git push -q origin feature; git rev-parse HEAD")
    res = await mgr.exec(LANE, script, grant_id=g.id, timeout_s=60)
    assert res.exit_code == 0, res.stderr
    assert git("rev-parse", "feature", cwd=node["upstream"]).strip() == res.stdout.strip().splitlines()[-1]
    # the lane saw a token for the gateway and nothing of the key: not in its environment, its output, its files
    body = [ln for ln in node["private"].splitlines() if not ln.startswith("-----")]
    seen = env.stdout + res.stdout + res.stderr
    assert not any(part in seen for part in body) and "PRIVATE KEY" not in seen
    root = mgr.paths.workdir(LANE)
    for path in [p for p in root.rglob("*") if p.is_file()] if root.exists() else []:
        data = path.read_bytes()
        assert b"PRIVATE KEY" not in data and not any(part.encode() in data for part in body), path
    [ev] = [e for e in node["policy"].traces.query(kind="git_push", limit=10)[0] if e.args.get("result") == "pushed"]
    assert ev.grant_id == g.id and ev.lane == LANE and ev.args["vault_ref"] == "git:app"


async def test_a_lane_with_no_network_reaches_its_declared_remote_and_nothing_else(node):
    mgr, g = node["mgr"], node["grant"]
    mgr.create("dev/offline", preset="sandbox", overrides={"filesystem": "overlay"})
    assert mgr.get("dev/offline").profile.network == "off"
    res = await mgr.exec("dev/offline", "git clone -q https://example.test/owner/app.git app && cat app/README",
                         grant_id=g.id, timeout_s=60)
    assert res.exit_code == 0 and res.stdout == "hi\n", res.stderr
    assert "HTTP_PROXY" not in (await mgr.exec("dev/offline", "env", grant_id=g.id)).stdout


async def test_without_a_grant_or_a_declaration_a_command_gets_no_git_config(node):
    mgr = node["mgr"]
    plain = await mgr.exec(LANE, "env")
    assert "GIT_CONFIG_COUNT" not in plain.stdout
    node["git"].remotes.deny("dev", f"{HOST}/{REPO}")
    undeclared = await mgr.exec(LANE, "env", grant_id=node["grant"].id)
    assert "GIT_CONFIG_COUNT" not in undeclared.stdout


async def test_the_lane_token_ends_when_the_lane_goes_cold(node):
    mgr, g, gw = node["mgr"], node["grant"], node["git"].gateway
    env = await mgr.exec(LANE, "printenv GIT_CONFIG_VALUE_$((GIT_CONFIG_COUNT-1))", grant_id=g.id)
    token = env.stdout.strip().split(": ", 1)[1]
    assert gw.caller(token, listener_lane=LANE).grant_id == g.id
    await mgr.cool(LANE)
    with pytest.raises(Refused, match="no longer valid"):
        gw.caller(token, listener_lane=LANE)


async def test_the_tools_pass_the_callers_grant(node):
    from agentos.core.models import Principal
    from agentos.env import tool_specs

    class Ctx:
        principal = Principal(id="agent-1")
        grant = node["grant"]
        runtime = node["rt"]

        async def authorize(self, *a, **kw):
            return None

        def trace(self, *a, **kw):
            return None

    t = {s.name: s.handler for s in tool_specs(node["rt"])}
    out = await t["lane_exec"](Ctx(), on=LANE, cmd="printenv GIT_CONFIG_COUNT")
    assert int(out["stdout"].strip()) > 0


async def test_a_brain_task_gets_a_gitconfig_and_its_bundle_clones_through_the_gateway(node, tmp_path):
    import asyncio

    from agentos.brains import bundle
    from agentos.brains.models import ContextBundle, GitRef
    from agentos.git.service import gitconfig_text
    from agentos.git.upstream import config_env

    pairs = await node["git"].task_pairs("bt_1", grant_id=node["grant"].id, workspace="dev", lanes=[], ttl_s=600)
    assert pairs and pairs[-1][1].startswith("x-agentos-git: agtg_")
    text = gitconfig_text(pairs)
    assert node["private"].splitlines()[1] not in text
    work = tmp_path / "task" / "work"
    b = ContextBundle(summary="go", git=GitRef(remote=f"https://{HOST}/{REPO}.git", ref="feature"))
    await asyncio.to_thread(bundle.materialize, b, work, git_env=config_env(pairs))
    assert (work / "README").read_text() == "hi\n"
    node["git"].task_end("bt_1")
    with pytest.raises(Refused):
        node["git"].gateway.caller(pairs[-1][1].split(": ", 1)[1], listener_lane=None)
    events = node["policy"].traces.query(kind="git_fetch", limit=10)[0]
    assert any(e.args.get("via") == "brain task bt_1" for e in events)
    assert json.dumps([e.args for e in events]).count("agtg_") == 0


async def test_git_remotes_tool_says_what_the_node_can_reach_and_never_a_secret(node):
    from agentos.core.errors import PolicyDenied
    from agentos.git.tools import tool_specs

    [spec] = tool_specs(node["rt"])
    ctx = node["policy"].context_for_grant(node["grant"].id, tool="git_remotes")
    out = await spec.handler(ctx, workspace="dev", remote="git@example.test:owner/app.git")
    [r] = out["remotes"]
    assert r["remote"] == f"{HOST}/{REPO}" and r["push"] and r["credential"]["type"] == "ssh"
    assert out["check"]["can_push"] and out["check"]["owner_runs_on_the_node"] == []
    other = await spec.handler(ctx, workspace="dev", remote="https://github.com/me/other")
    assert other["check"]["can_fetch"] is False
    assert other["check"]["owner_runs_on_the_node"] == ["agentos git remote allow dev github.com/me/other",
                                                        "agentos git key add --host github.com --repo me/other"]
    assert "PRIVATE" not in json.dumps(out) and node["private"].splitlines()[1] not in json.dumps(out)
    with pytest.raises(PolicyDenied):
        await spec.handler(ctx, workspace="prod")
