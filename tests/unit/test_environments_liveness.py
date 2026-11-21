"""A machine that stops for good while its environment is open (openajar.environments.liveness; evals t12): the
node asks the provider, twice, and only then says it is gone; every call that needs the machine fails with
machine_gone and one next step; environment() shows it; environment_close ends it without a save; an open with
the same title starts a new machine instead of handing back the dead one; a job on it is lost with the true
reason; and none of these checks is the agent's use of the machine. The fixture is test_environments.py's (a
fake cloud, fake lanes, no SSH)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_environments import TITLE, node, opened  # noqa: F401  (node is a fixture)

from openajar.core import catalog
from openajar.environments import liveness, routing
from openajar.policy.wrap import ToolCallError

REPO = "example.test/me/shop"


def _server(node, env_id):  # noqa: F811
    env = node["mgr"].get(env_id)
    return node["prov"].servers[node["vms"].store.get(env.vm_id).provider_id]


@pytest.fixture
def quick(node):  # noqa: F811
    node["mgr"].gone_recheck_s = 0.0
    return node


async def _killed(node, out):  # noqa: F811
    """The harness's docker kill: the provider says exited, and the node finds out."""
    _server(node, out["id"]).status = "exited"
    assert await node["mgr"].machine_gone(node["mgr"].get(out["id"]), cause="test")
    return node["mgr"].get(out["id"])


# ------------------------------------------------------------------ gone, or only not answering


async def test_the_provider_decides_and_a_running_machine_is_never_called_gone(quick):
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    env = mgr.get(out["id"])
    asked = []
    real = quick["prov"].get_server

    async def get_server(sid):
        asked.append(sid)
        return await real(sid)

    quick["prov"].get_server = get_server
    assert await mgr.machine_gone(env) is None  # ssh failed once, the machine runs: a blip, nothing said
    assert await mgr.machine_gone(env) is None and len(asked) == 1  # not asked again for ALIVE_AGAIN_S
    assert mgr.get(env.id).gone_at is None and mgr.get(env.id).shown_state(mgr._clock()) != "gone"


async def test_a_provider_that_does_not_answer_or_changes_its_mind_says_nothing(quick, monkeypatch):
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    env = mgr.get(out["id"])

    async def broken(sid):
        raise TimeoutError("api down")

    monkeypatch.setattr(quick["prov"], "get_server", broken)
    assert await mgr.machine_gone(env) is None
    monkeypatch.undo()
    mgr._alive_seen().clear()
    server = _server(quick, env.id)
    answers = iter(["exited", "running"])  # it came back by the second look (docker restarted it)
    real = quick["prov"].get_server

    async def flaky(sid):
        s = await real(sid)
        s.status = next(answers)
        return s

    monkeypatch.setattr(quick["prov"], "get_server", flaky)
    assert await mgr.machine_gone(env) is None and server is not None and mgr.get(env.id).gone_at is None


@pytest.mark.parametrize(("status", "words"), [("exited", "says its server is exited"),
                                               ("off", "says its server is off"),
                                               (None, "no longer has its server")])
async def test_a_stopped_or_deleted_machine_is_gone_with_why(quick, status, words):
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    if status is None:
        quick["prov"].servers.pop(quick["vms"].store.get(mgr.get(out["id"]).vm_id).provider_id)
    else:
        _server(quick, out["id"]).status = status
    why = await mgr.machine_gone(mgr.get(out["id"]))
    assert words in why
    env = mgr.get(out["id"])
    assert env.gone_at is not None and env.gone_why == why and env.shown_state(mgr._clock()) == "gone"
    assert out["id"] not in mgr._tunnels  # nothing to reach: its tunnel is stopped


async def test_the_checks_are_never_the_agents_use_of_the_machine(quick):
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    env = mgr.get(out["id"])
    before = (env.last_call_at, quick["envs"].store.last_used(env.terminal_lane), len(quick["machine"].ran))
    await mgr.machine_gone(env)
    _server(quick, env.id).status = "exited"
    mgr._alive_seen().clear()
    await mgr.machine_gone(mgr.get(env.id))
    after = mgr.get(env.id)
    assert (after.last_call_at, quick["envs"].store.last_used(env.terminal_lane), len(quick["machine"].ran)) == before


# ------------------------------------------------------------------ what the agent hears


async def test_every_call_that_needs_the_machine_says_machine_gone_with_one_next_step(quick):
    out = await opened(quick, repo=REPO)
    await _killed(quick, out)
    for tool, args in (("terminal_exec", {"cmd": "sh build.sh"}),
                       ("terminal_job_start", {"cmd": "sh build.sh"}), ("environment_save", {}),
                       ("environment_port", {"port": 3000})):
        with pytest.raises(ToolCallError, match="^machine_gone") as err:
            await quick["call"](tool, on=out["on"], **args)
        words = str(err.value)
        assert f'environment_open(title="{TITLE}")' in words and "no save" in words and "lost" in words, words
        assert words.startswith("machine_gone: Nothing ran: environment ") and "stopped at" in words
        assert "is exited" in words and "\u2014" not in words
    assert "same title" in catalog.entry("machine_gone").agent


async def test_environment_shows_the_machine_gone_and_asks_nothing_of_it(quick):
    out = await opened(quick, repo=REPO)
    await _killed(quick, out)
    ran = len(quick["machine"].ran)
    st = await quick["call"]("environment", on=out["on"])
    assert st["state"] == "gone" and st["machine_gone"]["why"].endswith("is exited")
    assert st["next"] == st["machine_gone"]["next"] and "environment_open" in st["next"]
    assert len(quick["machine"].ran) == ran  # no git, no ports: nothing on the dead machine


async def test_the_list_says_so_too_with_when_and_why(quick):
    """The web app's list (EnvSummary) carries machine_gone, so its cards say why without reading each one."""
    out = await opened(quick, repo=REPO)
    env = await _killed(quick, out)
    listed = quick["mgr"].summary(env)
    assert listed["state"] == "gone" and listed["machine_gone"]["why"].endswith("is exited")
    assert listed["machine_gone"]["at"]
    assert "machine_gone" not in quick["mgr"].summary(quick["mgr"].get((await opened(quick, title="Another", repo=REPO))["id"]))


async def test_environment_finds_it_out_itself_when_git_does_not_answer(quick, monkeypatch):
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    _server(quick, out["id"]).status = "exited"

    async def no_answer(env):
        return None

    monkeypatch.setattr(mgr, "git_repos", no_answer)
    st = await quick["call"]("environment", on=out["on"])
    assert st["state"] == "gone" and "machine_gone" in st


async def test_closing_it_needs_no_discard_and_says_what_is_left(quick):
    out = await opened(quick, repo=REPO)
    await _killed(quick, out)
    closed = await quick["call"]("environment_close", on=out["on"])
    assert closed["state"] == "closed" and closed["saves"] == {"made": None, "left": []}
    assert "had stopped" in closed["next"] and f'environment_open(title="{TITLE}")' in closed["next"]
    assert quick["mgr"].get(out["id"]).closed_reason.startswith("its machine stopped")


async def test_the_same_title_opens_a_new_machine_instead_of_the_dead_one(quick):
    out = await opened(quick, repo=REPO)
    await _killed(quick, out)
    again = await opened(quick, repo=REPO)
    assert again["id"] != out["id"] and again["opened"] == "new" and again["state"] == "ready"
    assert again["replaced"]["id"] == out["id"] and again["replaced"]["why"].endswith("is exited")
    assert again["next"].startswith("New machine: the one this title had stopped")
    assert quick["mgr"].get(out["id"]).state == "closed"


async def test_an_open_notices_a_dead_machine_nobody_called_since(quick):
    """The harness killed it and the agent's next call is the open itself: still never an attach."""
    out = await opened(quick, repo=REPO)
    _server(quick, out["id"]).status = "exited"
    again = await opened(quick, repo=REPO)
    assert again["opened"] == "new" and again["replaced"]["id"] == out["id"]


async def test_the_words_name_the_save_to_resume_when_there_is_one(quick, monkeypatch):
    out = await opened(quick, repo=REPO)
    env = await _killed(quick, out)
    from datetime import UTC, datetime

    save = SimpleNamespace(id="s1234567890", saved_at=datetime(2026, 11, 21, 9, 30, tzinfo=UTC))
    monkeypatch.setattr(quick["mgr"], "saves", lambda: SimpleNamespace(latest=lambda family, title: save))
    words = quick["mgr"].gone_words(env)
    assert "from its save of 2026-11-21T09:30:00Z (s1234567890)" in words and "after that save is lost" in words
    assert len(liveness.ran_nothing(words)) < 500  # the wrapper's limit on an error's message


async def test_a_job_read_still_reads_and_calls_elsewhere_still_route(quick):
    """A job's output is on the node: terminal_job_read is not refused (route leaves ON_ONLY tools alone)."""
    out = await opened(quick, repo=REPO)
    env = await _killed(quick, out)
    kwargs = {"on": out["on"], "job": "job-0123456789"}
    assert routing.route(quick["rt"], "terminal_job_read", kwargs, session="chat-1", grant_id=quick["grant"].id,
                         family=quick["grant"].root_id).id == env.id


# ------------------------------------------------------------------ the reaper


async def test_the_reaper_finds_a_dead_machine_by_its_tunnel_and_ends_it_after_idle_minutes(quick):
    mgr = quick["mgr"]
    out = await opened(quick, repo=REPO)
    _server(quick, out["id"]).status = "exited"
    mgr._tunnels[out["id"]].alive = False  # ssh -N ended when the container died
    await mgr.reap_once()
    env = mgr.get(out["id"])
    assert env.gone_at is not None and env.open and out["id"] not in mgr._tunnels  # never started again
    later = env.gone_at + timedelta(minutes=mgr.settings.idle_minutes + 1)
    got = await mgr.reap_once(later)
    assert out["id"] in got["gone"] and mgr.get(out["id"]).state == "closed"


def test_the_gone_statuses_are_the_providers_words_for_stopped():
    assert {"exited", "dead", "off"} <= liveness.GONE_STATUSES and "running" not in liveness.GONE_STATUSES
    assert "starting" not in liveness.GONE_STATUSES and "initializing" not in liveness.GONE_STATUSES
