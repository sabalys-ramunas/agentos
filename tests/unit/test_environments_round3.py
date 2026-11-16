"""Round 3 of the agent testers (2026-11-16): one machine count, a wait for a free machine, a wait for
ready without polling, words for clients that send no session id, failed opens the agent closed, and
a way to keep local commits without a push.

T1 and T3 met two limits with different words ("its limit is 2 at once ... Ask for fewer hours or a
smaller size" and "vms.toml max_live = 3; destroy one first"), polled environment(on) until ready,
and slept and retried by hand while the node was full.
"""

from __future__ import annotations

import asyncio

import pytest
from test_environments import node, opened  # noqa: F401  (node is a fixture)

from agentos.core import catalog
from agentos.environments.errors import EnvironmentFull
from agentos.mcp.registry import failure_result
from agentos.policy.wrap import ToolCallError

NONE = None  # a client that sends no MCP session id


# ------------------------------------------------------------------ one count


async def test_a_full_node_is_environment_full_with_one_count_and_a_fix_that_fits(node):  # noqa: F811
    node["mgr"].settings.max_live = 1
    await opened(node, title="Mine", session=NONE)
    with pytest.raises(ToolCallError, match="^environment_full") as err:
        await node["call"]("environment_open", title="Second", session=NONE, wait_s=0)
    words = str(err.value)
    assert "already runs 1 machine, its limit at once" in words and "The soonest may free at" in words
    assert "\"Mine\"" in words and "destroy" not in words and "fewer hours" not in words
    assert "—" not in words and len(words) < 600
    entry = catalog.entry("environment_full")
    assert entry.retryable and "environment_close(on=...)" in entry.agent and "fewer hours" not in entry.agent
    assert node["vms"].store.recent()[0].lane_id.startswith("env/") and len(node["vms"].store.live()) == 1


async def test_the_vm_limit_is_never_below_the_environments_count(node):  # noqa: F811
    node["mgr"].settings.max_live = 5
    node["vms"].settings.max_live = 3
    assert node["vms"].max_live == 5
    node["mgr"].settings.max_live = 2
    assert node["vms"].max_live == 3  # vms.toml may allow more (the owner's own VMs)


async def test_a_machine_still_being_deleted_counts_and_frees_in_a_minute(node):  # noqa: F811
    node["mgr"].settings.max_live = 1
    a = await opened(node, title="Gone soon", session=NONE)
    await node["call"]("environment_close", on=a["on"], session=NONE)
    rec = node["vms"].store.recent()[0]
    rec.state = "deleting"  # its close still deletes it on the provider
    node["vms"].store.put(rec)
    with pytest.raises(ToolCallError, match="^environment_full") as err:
        await node["call"]("environment_open", title="Next", session=NONE, wait_s=0)
    assert "(1 closing now)" in str(err.value) and "in a minute" in str(err.value)


def test_the_error_carries_its_own_retry_after():
    from types import SimpleNamespace

    from agentos.policy.redactor import Redactor

    spec = SimpleNamespace(name="environment_open")
    res = failure_result(spec, EnvironmentFull("this node already runs 2 machines", retry_after=240), Redactor(None))
    text = res.content[0].text
    assert "retry_after: 240" in text and "fix: Wait and call environment_open again" in text
    res = failure_result(spec, EnvironmentFull("full"), Redactor(None))
    assert "retry_after: 60" in res.content[0].text  # the catalog's when the error has none


# ------------------------------------------------------------------ wait for a free machine


async def test_open_waits_for_a_machine_to_free_and_then_opens(node):  # noqa: F811
    node["mgr"].settings.max_live = 1
    node["mgr"].slot_wait_s = 5.0
    a = await opened(node, title="First", session=NONE)
    waiting = asyncio.create_task(node["call"]("environment_open", title="Second", session=NONE))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    await node["call"]("environment_close", on=a["on"], session=NONE)
    b = await asyncio.wait_for(waiting, 5)
    assert b["opened"] == "new" and b["title"] == "Second" and b["state"] == "ready"


async def test_open_that_waited_in_vain_says_how_long(node):  # noqa: F811
    node["mgr"].settings.max_live = 1
    node["mgr"].slot_wait_s = 1.2
    await opened(node, title="First", session=NONE)
    with pytest.raises(ToolCallError, match="^environment_full") as err:
        await node["call"]("environment_open", title="Second", session=NONE)
    assert "none freed in the 1 s this call waited" in str(err.value)


# ------------------------------------------------------------------ wait for ready without polling


async def test_environment_on_waits_for_ready_and_the_not_ready_words_name_on(node):  # noqa: F811
    node["machine"].setup = "running"  # cloud-init still runs: the machine stays starting
    node["mgr"].ready_wait_s = 5.0
    a = await opened(node, title="Slow", session=NONE, wait_s=0)
    assert a["state"] == "starting" and f'environment(on="{a["on"]}") waits up to a minute' in a["next"]
    await asyncio.sleep(0.2)  # the bring-up is at its setup check
    with pytest.raises(ToolCallError, match="^environment_not_ready") as err:
        await node["call"]("lane_exec", on=a["on"], cmd="git status", session=NONE)
    words = str(err.value)
    assert "is starting for " in words and "so far)" in words and 'environment(on="<your on>")' in words
    assert 'environment(on="<your on>")' in catalog.entry("environment_not_ready").agent
    seen = asyncio.create_task(node["call"]("environment", on=a["on"], session=NONE))
    await asyncio.sleep(0.3)
    assert not seen.done()  # it waits on the node, no poll loop in the agent
    node["machine"].setup = "ready"
    out = await asyncio.wait_for(seen, 10)
    assert out["state"] == "ready"


async def test_environment_on_answers_after_its_wait_with_what_it_does(node):  # noqa: F811
    node["machine"].setup = "running"
    a = await opened(node, title="Slow", session=NONE, wait_s=0)
    out = await node["call"]("environment", on=a["on"], session=NONE)  # ready_wait_s is short in the fixture
    assert out["state"] == "starting" and out["doing"].startswith("starting for ")
    assert 'environment(on="<your on>") again: it waits up to a minute' in out["next"]


# ------------------------------------------------------------------ words for clients with no session id


def later(node, minutes: float) -> None:  # noqa: F811
    from datetime import timedelta

    from agentos.core.models import utcnow

    at = utcnow() + timedelta(minutes=minutes)
    node["mgr"]._clock = lambda: at


async def test_with_no_session_id_the_list_and_in_use_never_say_another_chat(node):  # noqa: F811
    a = await opened(node, title="Mine", session=NONE)
    seen = await node["call"]("environment", session=NONE)
    (row,) = seen["open"]
    assert row["use"].startswith("held by the chat that opened it (if that is you, pass the on")
    assert "another chat" not in row["use"] and a["on"] not in str(seen)
    with pytest.raises(ToolCallError, match="^environment_in_use") as err:
        await node["call"]("environment_open", title="Mine", session=NONE)
    words = str(err.value)
    assert "held by the chat that opened it" in words and "another chat of your agent" not in words
    assert 'environment(on="<your on>")' in words and len(words) < 600
    later(node, 4)
    with pytest.raises(ToolCallError, match="^environment_in_use") as err:
        await node["call"]("environment_open", title="Mine", take_over=True, session=NONE)
    assert "take_over=true was seen, and it works only once that chat has made no call for 10 min" in str(err.value)
    assert "(6 min from now)" in str(err.value)
    # a client that sends a session id: a machine it does not hold is surely another chat's
    with pytest.raises(ToolCallError, match="^environment_in_use") as err:
        await node["call"]("environment_open", title="Mine", session="chat-9")
    assert "in use by another chat of your agent" in str(err.value)


async def test_a_call_without_on_names_the_titles_of_the_open_machines(node):  # noqa: F811
    a = await opened(node, title="Shop page", session=NONE)
    await opened(node, title="Docs fix", session=NONE)
    with pytest.raises(ToolCallError, match="^which_environment") as err:
        await node["call"]("lane_exec", cmd="pwd", session=NONE)
    words = str(err.value)
    assert 'Your agent has 2 machines open: "Shop page", "Docs fix"' in words and "lost it" not in words
    assert a["on"] not in words and a["id"] not in words


# ------------------------------------------------------------------ failed opens


async def test_a_failed_open_the_agent_closed_leaves_the_failed_list(node):  # noqa: F811
    node["machine"].setup = "status: error\nE: Unable to locate package nodejs"
    with pytest.raises(ToolCallError, match="^environment_failed"):
        await node["call"]("environment_open", title="Scratch", session=NONE)
    seen = await node["call"]("environment", session=NONE)
    (failed,) = seen["failed"]
    closed = await node["call"]("environment_close", on=failed["id"], session=NONE)
    assert closed["state"] == "failed"
    assert "failed" not in await node["call"]("environment", session=NONE)
    assert node["mgr"].get(failed["id"]).state == "failed"  # the history keeps it


async def test_a_failed_open_is_off_the_list_once_its_title_is_open_again(node):  # noqa: F811
    node["machine"].setup = "status: error\nE: Unable to locate package nodejs"
    with pytest.raises(ToolCallError, match="^environment_failed"):
        await node["call"]("environment_open", title="Scratch", session=NONE)
    node["machine"].setup = "ready"
    await opened(node, title="Scratch", session=NONE)
    assert "failed" not in await node["call"]("environment", session=NONE)


# ------------------------------------------------------------------ local commits


async def test_local_commits_can_go_home_without_a_push(node):  # noqa: F811
    a = await opened(node, title="Readme word", repo="example.test/me/shop", session=NONE)
    node["machine"].git = ("repo=/work/shop\nbranch=t1/readme\nahead=1\ndirty_count=0\n"
                           "unpushed=984008b README: say new VM\n")
    seen = await node["call"]("environment", on=a["on"], session=NONE)
    assert "environment_download" in seen["next"] and not seen["next"].startswith("commit and push")
    with pytest.raises(ToolCallError, match="^unsaved_work") as err:
        await node["call"]("environment_close", on=a["on"], session=NONE)
    words = str(err.value)
    assert "git bundle create /work/unpushed.bundle --branches --not --remotes in /work/shop" in words
    assert 'environment_download(path="/work/unpushed.bundle")' in words and "discard=true" in words
    assert "environment_download" in catalog.entry("unsaved_work").agent
