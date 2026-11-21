"""Checkpoints inside an environment (openajar.environments.checkpoints), watching as use
(openajar.environments.watching), the owner's resume of a save without a chat (openajar.environments.waking),
a saved environment's live-view page (openajar.node.viewer_saved), and what a stale handle hears.

The machines are test_environments_saves's: folders here, where every command the node sends runs for
real (git and the checkpoint scripts included), so a checkpoint and a rollback are real ones."""

# ruff: noqa: F811  (every test takes the `node` fixture test_environments_saves defines)
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from test_environments_saves import TITLE, disk, git, node, opened  # noqa: F401  (node is the fixture)

from openajar.core.models import utcnow
from openajar.environments import checkpoints, routing
from openajar.environments.errors import EnvironmentLimit, NoEnvironment
from openajar.policy.wrap import ToolCallError


def work(node, out) -> object:
    return disk(node, out["id"]) / "work" / "shop"


async def _wait(node, env_id: str):
    return await node["mgr"].wait_ready(env_id, 30)


# ------------------------------------------------------------------ checkpoints


async def test_checkpoint_change_and_roll_back(node):
    """A checkpoint keeps the work as it is (an edit, a new file; not .env or node_modules), with the agent's
    index and branch untouched. After a commit, more edits and new files, a rollback puts the branch back at
    the checkpoint's HEAD, the files as they were and removes the new ones, leaves ignored files alone, and
    checkpoints the work first so the rollback can be undone."""
    out = await opened(node, keep="save")
    w = work(node, out)
    (w / "app.js").write_text("console.log('before')\n")
    (w / "draft.md").write_text("a new file\n")
    (w / ".env").write_text("KEY=one\n")
    head = git(w, "rev-parse", "HEAD")
    status = git(w, "status", "--porcelain")

    made = await node["call"]("environment_checkpoint", on=out["on"], note="before\nthe refactor")
    n = made["checkpoint"]
    assert n == 1 and made["note"] == "before the refactor"
    assert made["repos"] == [{"dir": "/work/shop", "head": head[:12], "branch": "main", "files_changed": 2}]
    assert made["compare"] == "git diff refs/agentos/checkpoints/1" and "environment_rollback(checkpoint=1" in made["next"]
    assert "node_modules" in made["ignored"] and "—" not in made["next"]
    assert git(w, "status", "--porcelain") == status  # the index and work tree as they were
    assert git(w, "rev-parse", "refs/agentos/checkpoints/1^") == head
    assert set(git(w, "show", "--name-only", "--format=", "refs/agentos/checkpoints/1").split()) == {"app.js", "draft.md"}

    listed = await node["call"]("environment", on=out["on"])
    [row] = listed["checkpoints"]
    assert row["id"] == 1 and row["note"] == "before the refactor" and row["auto"] is None and row["at"]
    [repo] = row["repos"]  # GIT_STATE runs on the machine's folder here, so its path ends in /work/shop
    assert repo["dir"].endswith("/work/shop") and repo["head"] == head[:12] and repo["files"] == 2

    # the risky step: a commit, more edits, new files, a dependency installed, .env changed
    git(w, "add", "-A")
    git(w, "commit", "-qm", "the refactor")
    (w / "app.js").write_text("console.log('broken')\n")
    (w / "extra.js").write_text("// new since\n")
    (w / "node_modules" / "dep").mkdir(parents=True)
    (w / "node_modules" / "dep" / "index.js").write_text("1\n")
    (w / ".env").write_text("KEY=two\n")
    (w / "old.txt").unlink()

    back = await node["call"]("environment_rollback", on=out["on"], checkpoint=1)
    [r] = back["repos"]
    assert r["dir"] == "/work/shop" and r["branch"] == "main" and r["head"] == head[:12]
    assert r["commits_undone"] == 1 and r["files_removed"] == 1 and r["files_restored"] >= 2
    assert back["undo"] == {"checkpoint": 2, "rollback": "environment_rollback(checkpoint=2)"}
    assert git(w, "rev-parse", "HEAD") == head and git(w, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (w / "app.js").read_text() == "console.log('before')\n" and (w / "draft.md").exists()
    assert (w / "old.txt").exists() and not (w / "extra.js").exists()
    assert git(w, "status", "--porcelain") == status  # the index is at HEAD again, the rest is work
    assert (w / ".env").read_text() == "KEY=two\n" and (w / "node_modules" / "dep" / "index.js").exists()
    assert "run the install again" in back["next"] and "environment_rollback(checkpoint=2)" in back["next"]

    undone = await node["call"]("environment_rollback", on=out["on"], checkpoint=2)  # the rollback, undone
    assert undone["undo"]["checkpoint"] == 3
    assert (w / "app.js").read_text() == "console.log('broken')\n" and (w / "extra.js").exists()
    assert git(w, "log", "-1", "--format=%s") == "the refactor" and not (w / "old.txt").exists()
    rows = (await node["call"]("environment", on=out["on"]))["checkpoints"]
    assert [x["id"] for x in rows] == [3, 2, 1] and rows[0]["auto"] == "before rollback to 2"


async def test_rollback_refuses_mid_merge_and_a_checkpoint_that_is_not_there(node):
    out = await opened(node)
    w = work(node, out)
    await node["call"]("environment_checkpoint", on=out["on"])
    (w / "app.js").write_text("changed\n")
    with pytest.raises(ToolCallError) as e:
        await node["call"]("environment_rollback", on=out["on"], checkpoint=7)
    assert str(e.value).startswith("checkpoint_not_found") and "environment(on=...)" in str(e.value)
    (w / ".git" / "MERGE_HEAD").write_text(git(w, "rev-parse", "HEAD") + "\n")
    with pytest.raises(ToolCallError) as e:
        await node["call"]("environment_rollback", on=out["on"], checkpoint=1)
    assert str(e.value).startswith("rollback_refused") and "git merge --abort" in str(e.value)
    assert (w / "app.js").read_text() == "changed\n"  # nothing changed
    refs = git(w, "for-each-ref", "--format=%(refname)", "refs/agentos/checkpoints")
    assert refs.split() == ["refs/agentos/checkpoints/1"]  # and no automatic checkpoint was made


async def test_only_the_newest_are_kept(node, monkeypatch):
    monkeypatch.setattr(checkpoints, "KEEP", 3)
    out = await opened(node)
    w = work(node, out)
    for i in range(5):
        (w / "app.js").write_text(f"v{i}\n")
        await node["call"]("environment_checkpoint", on=out["on"], note=f"v{i}")
    refs = git(w, "for-each-ref", "--format=%(refname)", "refs/agentos/checkpoints").split()
    assert sorted(refs) == [f"refs/agentos/checkpoints/{n}" for n in (3, 4, 5)]
    assert [r["id"] for r in (await node["call"]("environment", on=out["on"]))["checkpoints"]] == [5, 4, 3]


async def test_no_repository_says_how(node):
    out = await opened(node, repo=None)
    with pytest.raises(ToolCallError) as e:
        await node["call"]("environment_checkpoint", on=out["on"])
    assert str(e.value).startswith("checkpoint_failed") and "git init" in str(e.value)


async def test_a_snapshot_keeps_the_checkpoints_and_a_files_only_resume_does_not(node):
    """The save's file part reads branches and stashes only: checkpoints neither confuse it nor ride in its
    bundle. A snapshot holds the whole disk, so its resume has them; a resume from the files has none."""
    from test_environments_saves import work_on_it

    out = await opened(node, keep="save")
    w = work(node, out)
    head = work_on_it(w)
    await node["call"]("environment_checkpoint", on=out["on"], note="kept on the disk")
    closed = await node["call"]("environment_close", on=out["on"])
    assert closed["saved"]["verified"] is True and closed["saved"]["commits"] == 2  # the branch's, not a checkpoint
    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    w2 = work(node, again)
    assert again["resumed"]["from"] == "snapshot"
    assert git(w2, "for-each-ref", "--format=%(refname)", "refs/agentos") == "refs/agentos/checkpoints/1"
    closed = await node["call"]("environment_close", on=again["on"], session="chat-2")
    await node["cloud"].delete_image(closed["saved"]["snapshot"]["id"])
    third = await node["call"]("environment_open", title=TITLE, session="chat-3")
    w3 = work(node, third)
    assert third["resumed"]["from"] == "files" and git(w3, "rev-parse", "HEAD") == head
    assert git(w3, "for-each-ref", "refs/agentos") == ""


async def test_the_owner_sees_what_changed_since_and_the_patch(node):
    out = await opened(node)
    w = work(node, out)
    await node["call"]("environment_checkpoint", on=out["on"], note="start")
    (w / "app.js").write_text("console.log('one')\nconsole.log('two')\n")
    (w / "new.txt").write_text("hello\n")
    (w / "old.txt").unlink()
    mgr = node["mgr"]
    env = mgr.get(out["id"])
    assert [r["id"] for r in (await mgr.detail(env))["checkpoints"]] == [1]
    rows = await mgr.checkpoint_summaries(env, [{"id": 1, "repos": [{"dir": "/work/shop", "head": None, "files": 0}]}])
    assert rows[0]["repos"][0]["diff"] == {"files_added": 1, "files_changed": 1, "files_removed": 1,
                                           "lines_added": 3, "lines_removed": 2}
    diff = await mgr.checkpoint_diff(env, 1)
    [repo] = diff["repos"]
    assert {f["path"]: f["status"] for f in repo["files"]} == {"app.js": "M", "new.txt": "A", "old.txt": "D"}
    assert "+hello" in repo["patch"] and repo["truncated"] is False
    checkpoints_cap = checkpoints.DIFF_MAX_BYTES
    try:
        checkpoints.DIFF_MAX_BYTES = 40
        cut = (await mgr.checkpoint_diff(env, 1))["repos"][0]
        assert len(cut["patch"].encode()) <= 41 and cut["truncated"] is True
    finally:
        checkpoints.DIFF_MAX_BYTES = checkpoints_cap


# ------------------------------------------------------------------ watching counts as use


async def test_watching_keeps_it_open_and_polling_does_not(node):
    """A live view open keeps a keep "save" environment from being saved idle; once it closes, the idle
    rule counts from then. The owner's page polling (detail: git and ports on the machine) is no use."""
    out = await opened(node, keep="save")
    mgr = node["mgr"]
    env = mgr.get(out["id"])
    stop = mgr.watch_lane(env.browser_lane)
    assert mgr.watch_lane("personal/reddit") is None
    watched = (await node["call"]("environment", on=out["on"]))["watched"]
    assert watched["viewers"] == 1 and watched["live_views"] == 1 and watched["since"]
    later = utcnow() + timedelta(minutes=mgr.settings.idle_minutes + 5)
    got = await mgr.reap_once(later)
    assert got["closed"] == [] and mgr.get(out["id"]).state == "ready"
    stop()
    stop()  # once is enough
    env = mgr.get(out["id"])
    assert env.watched_at is not None and mgr.watched(env) is None
    before = mgr._last_used(env)
    await mgr.detail(env)  # the web app's polling
    await mgr.detail(env)
    assert mgr._last_used(mgr.get(out["id"])) == before


async def test_a_port_link_in_use_is_watching(node):
    mgr = node["mgr"]
    out = await opened(node)
    stop = mgr.watch_lane(f"env/{out['id']}", "port")
    assert mgr.watched(mgr.get(out["id"]))["port_connections"] == 1
    stop()
    assert mgr.watched(mgr.get(out["id"])) is None


async def test_the_probes_put_the_lanes_last_use_back(node, monkeypatch):
    """probe_exec: the node reading the machine for a page leaves the terminal lane's last use as it was."""
    mgr, envs = node["mgr"], node["envs"]
    out = await opened(node)
    lane = f"env/{out['id']}"
    then = utcnow() - timedelta(hours=1)
    envs.store.touch(lane, then)
    real = envs.exec

    async def touching(lane_id, cmd, **kw):
        envs.store.touch(lane_id)
        return await real(lane_id, cmd, **kw)

    monkeypatch.setattr(envs, "exec", touching)
    await mgr.probe_exec(lane, "echo hi")
    assert envs.store.last_used(lane) == then


# ------------------------------------------------------------------ the owner resumes a save


async def _saved(node):
    out = await opened(node, keep="save", session=None)
    (work(node, out) / "notes.md").write_text("half done\n")
    closed = await node["call"]("environment_close", on=out["on"], note="next: the mobile menu", session=None)
    assert closed["saved"]["verified"] is True
    return out, closed["saved"]["save_id"]


async def test_the_owner_resumes_a_save_held_by_no_chat_and_the_next_open_takes_it(node):
    mgr = node["mgr"]
    _out, save_id = await _saved(node)
    env, how = await mgr.resume_saved(TITLE)
    assert how == "resumed" and env.binding == f"kept:{env.id}" and env.lease is None and env.resumed_by == "owner"
    env = await _wait(node, env.id)
    assert env.state == "ready" and env.resumed["save_id"] == save_id and env.resumed["from"] == "snapshot"
    assert (work(node, {"id": env.id}) / "notes.md").read_text() == "half done\n"
    view = mgr.owner_view(env, how)
    assert view["opened"] == "resumed" and view["links"]["browser"].startswith("http://localhost:")
    assert (await mgr.resume_saved(save_id))[1] == "already_open"  # a second press changes nothing
    listed = await node["call"]("environment", session=None)
    [row] = listed["open"]
    assert "resumed by the owner" in row["use"] and "takes it" in row["use"]

    again = await node["call"]("environment_open", title=TITLE, session=None)
    assert again["id"] == env.id and again["opened"] == "resumed" and again["resumed"]["save_id"] == save_id
    assert "next: the mobile menu" in again["resumed"]["note"] and again["next"].startswith("Resumed")
    with pytest.raises(ToolCallError) as e:  # that chat holds it now
        await node["call"]("environment_open", title=TITLE, session="chat-9")
    assert str(e.value).startswith("environment_in_use")


async def test_the_owners_resume_keeps_the_limits(node):
    mgr = node["mgr"]
    await _saved(node)
    mgr.settings.max_live = 0
    with pytest.raises(EnvironmentLimit) as e:
        await mgr.resume_saved(TITLE)
    assert "limit" in e.value.message and mgr.store.open() == []


async def test_the_idle_rule_saves_an_unwatched_resumed_one_again(node):
    mgr = node["mgr"]
    await _saved(node)
    env, _ = await mgr.resume_saved(TITLE)
    env = await _wait(node, env.id)
    later = utcnow() + timedelta(minutes=mgr.settings.idle_minutes + 1)
    await mgr.reap_once(later)
    assert mgr.get(env.id).state == "closed" and mgr.get(env.id).last_save_id


async def test_a_stale_handle_says_it_was_saved_and_how_to_come_back(node):
    out, _ = await _saved(node)
    rt, grant = node["rt"], node["grant"]
    with pytest.raises(NoEnvironment) as e:
        routing.route(rt, "terminal_exec", {"on": out["on"], "cmd": "ls"}, session=None, grant_id=grant.id,
                      family=grant.root_id)
    words = e.value.message
    assert "was saved at" in words and f'environment_open(title="{TITLE}") resumes it' in words
    assert "never resumed for you" in words and node["mgr"].store.open() == []  # and nothing was resumed
    env, _ = await node["mgr"].resume_saved(TITLE)
    with pytest.raises(NoEnvironment) as e:
        routing.route(rt, "terminal_exec", {"on": out["on"]}, session=None, grant_id=grant.id, family=grant.root_id)
    assert f"open again as {env.id}" in e.value.message and "no chat holds it" in e.value.message


def _bridge(loop, app) -> httpx.MockTransport:
    """A sync transport for the CLI (it runs in a thread) that answers from the ASGI app on the test's loop."""
    import asyncio

    def answer(request: httpx.Request) -> httpx.Response:
        async def go() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as c:
                r = await c.request(request.method, request.url.raw_path.decode(), headers=request.headers,
                                    content=request.content)
                headers = {k: v for k, v in r.headers.items() if k.lower() not in ("content-encoding", "content-length")}
                return httpx.Response(r.status_code, headers=headers, content=r.content)

        return asyncio.run_coroutine_threadsafe(go(), loop).result(30)

    return httpx.MockTransport(answer)


@pytest.fixture
def cli(node, monkeypatch, request):
    import asyncio
    from types import SimpleNamespace

    from node_config_support import patch_node_config
    from typer.testing import CliRunner

    from openajar.api import ApiSettings, build_api
    from openajar.environments import resume_cli

    home = node["mgr"].home
    api = build_api(node["rt"], ApiSettings(lane_pump_seconds=0, panel_dir=home / "no-panel"))
    cfg = node["rt"].get("config")
    toml = home / "node.toml"
    toml.write_text("")
    patch_node_config(request, monkeypatch, cfg, toml)

    async def run(*args: str):
        transport = _bridge(asyncio.get_running_loop(), api)
        monkeypatch.setattr(resume_cli, "_http_client", lambda: httpx.Client(transport=transport))
        return await asyncio.to_thread(CliRunner().invoke, resume_cli.app, ["--home", str(home), *args])

    return SimpleNamespace(run=run, api=api)


async def test_the_cli_resumes_a_save_as_the_owner_and_only_the_owner(node, cli):
    _out, save_id = await _saved(node)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=cli.api), base_url="http://localhost") as c:
        r = await c.post("/api/environments/resume", json={"ref": TITLE},
                         headers={"Authorization": f"Bearer {node['other_token']}"})
        assert r.status_code in (401, 403), r.text  # an agent's grant: never
    res = await cli.run("resume", "No such title")
    assert res.exit_code == 1 and "no save is named" in res.output
    res = await cli.run("resume", save_id)
    assert res.exit_code == 0, res.output
    [env] = node["mgr"].store.open()
    assert f"Resuming \"{TITLE}\" from save {save_id} (from its disk snapshot): environment {env.id}" in res.output
    assert f"/view/env/{env.id}-browser?t=" in res.output and "No chat holds it" in res.output
    assert env.binding == f"kept:{env.id}"
    res = await cli.run("resume", TITLE)
    assert res.exit_code == 0 and "is open already" in res.output
    await _wait(node, env.id)


# ------------------------------------------------------------------ the saved live view


def _viewer(node):
    from openajar.node.viewer_app import build_viewer_app

    rt = node["rt"]
    app = build_viewer_app(rt.require("lanes"), rt.require("config"), environments=lambda: node["mgr"])
    return app, app.state.viewer


async def test_a_saved_environments_link_shows_its_page_and_the_owner_resumes_it(node):
    from openajar.node.viewer_tokens import ViewMode

    out, _ = await _saved(node)
    app, viewer = _viewer(node)
    lane = f"env/{out['id']}-browser"
    agent_link = viewer.tokens.issue(lane, ViewMode.CONTROL)
    owner_link = viewer.tokens.issue(lane, ViewMode.CONTROL, owner=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://node.test") as c:
        r = await c.get(agent_link.path())
        assert r.status_code == 200 and "This environment is saved" in r.text
        assert TITLE not in r.text and "Resume</button>" not in r.text
        r = await c.get(owner_link.path())
        assert r.status_code == 200 and f"{TITLE} is saved" in r.text and "next: the mobile menu" in r.text
        assert "Resume</button>" in r.text and "form-action 'self'" in r.headers["content-security-policy"]
        resume = f"/view/{lane}/resume?t={owner_link.token}"
        r = await c.post(resume)  # no Origin: not this page
        assert r.status_code == 403 and node["mgr"].store.open() == []
        r = await c.post(f"/view/{lane}/resume?t={agent_link.token}", headers={"origin": "http://node.test"})
        assert r.status_code == 403  # an agent's link is not the owner
        r = await c.post(resume, headers={"origin": "http://node.test"})
        assert r.status_code == 303
        [env] = node["mgr"].store.open()
        assert r.headers["location"].startswith(f"/view/env/{env.id}-browser?t=")
        assert env.binding == f"kept:{env.id}" and env.resumed_by == "owner"
        r = await c.get(agent_link.path())
        assert r.status_code == 200 and "open again" in r.text


async def test_a_starting_environments_link_reloads_until_it_is_up(node):
    """A resumed one whose browser is not up yet: a page that reloads itself, for any holder of its link."""
    from openajar.node.viewer_tokens import ViewMode

    class Starting:
        def browser_story(self, lane_id):
            return {"id": "eabcdefghjk", "title": TITLE, "state": "starting"}

    from openajar.node.viewer_app import build_viewer_app

    rt = node["rt"]
    app = build_viewer_app(rt.require("lanes"), rt.require("config"), environments=lambda: Starting())
    link = app.state.viewer.tokens.issue("env/eabcdefghjk-browser", ViewMode.CONTROL)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://node.test") as c:
        r = await c.get(link.path())
    assert r.status_code == 200 and f"{TITLE} is starting" in r.text and 'http-equiv="refresh"' in r.text


async def test_an_account_lane_still_says_no_such_lane(node):
    from openajar.node.viewer_tokens import ViewMode

    app, viewer = _viewer(node)
    link = viewer.tokens.issue("personal/reddit", ViewMode.CONTROL)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://node.test") as c:
        r = await c.get(link.path())
    assert r.status_code == 404 and "No such lane" in r.text
