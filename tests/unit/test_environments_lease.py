"""One chat holds a live machine: the lease (agentos.environments.picking, the owner's rule of 2026-11-16).

MCP 2026-07-28 clients (Claude Code, T3 Code) send no session id, so agentos cannot tell two chats of
one agent apart. Builder I1 let the title name the computer, and a second chat then attached to the
first chat's machine by its title, or reached it with a call without `on`. Now environment_open gives
the calling chat the machine's handle, `on`, and only calls that carry it reach the machine. A call
without `on` from such a client is refused even with one machine open; a title or an id never reaches
a live machine; the title of one another chat holds is environment_in_use, and take_over=true moves it
once that chat has been quiet (a fresh handle; the old one stops working). Listings never show another
chat's handle. A client that sends a session id holds its machines by the session, as before.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_environments import TITLE, node, opened  # noqa: F401  (node is a fixture)

from agentos.core import catalog
from agentos.core.models import utcnow
from agentos.core.tools import elicitor_var
from agentos.environments import routing
from agentos.environments.picking import HANDLE_RE, lease_of
from agentos.mcp import toolsets as ts
from agentos.mcp.registry import unknown_arguments
from agentos.policy.wrap import ToolCallError, bind_request_token, wrap_tool

NONE = None  # a client that sends no MCP session id (the 2026-07-28 protocol, stdio)


def later(node, minutes: float) -> None:  # noqa: F811
    at = utcnow() + timedelta(minutes=minutes)
    node["mgr"]._clock = lambda: at


# ------------------------------------------------------------------ the handle


async def test_two_stateless_chats_each_hold_their_own_and_never_reach_the_other(node):  # noqa: F811
    a = await opened(node, title="Shop page", session=NONE)
    b = await opened(node, title="Docs fix", session=NONE)
    assert a["opened"] == b["opened"] == "new" and a["id"] != b["id"]
    for out in (a, b):
        assert HANDLE_RE.match(out["on"]) and out["on"].startswith(out["id"] + ".")
        assert f'keep on="{out["on"]}" and pass it on every call' in out["next"] and "—" not in out["next"]
    # the node keeps only the hash: no record holds the handle itself
    env = node["mgr"].get(a["id"])
    assert env.lease == lease_of(a["on"]) and a["on"] not in env.model_dump_json()
    ran = await node["call"]("terminal_exec", on=a["on"], cmd="pwd", session=NONE)
    assert ran["exit_code"] == 0 and ran["environment"] == {"id": a["id"], "title": "Shop page"}
    ran = await node["call"]("terminal_exec", on=b["on"], cmd="pwd", session=NONE)
    assert ran["environment"]["id"] == b["id"]
    # the other chat's machine by its title, its id or its lane: refused, and nothing ran there
    for on in ("Shop page", a["id"], f"env/{a['id']}", f"env/{a['id']}-browser", f"{a['id']}-browser"):
        with pytest.raises(ToolCallError, match="^which_environment") as err:
            await node["call"]("terminal_exec", on=on, cmd="rm -rf /work", session=NONE)
        words = "lane's name, not an on" if on.endswith(("-browser", f"/{a['id']}")) else "pass the on from your"
        assert words in str(err.value) and a["on"] not in str(err.value)
    # a handle with the right id and a made-up secret is nobody's
    with pytest.raises(ToolCallError, match="^no_environment"):
        await node["call"]("terminal_exec", on=a["id"] + ".aaaaaaaaaaaaaaaa", cmd="pwd", session=NONE)
    assert [c.summary for c in node["mgr"].get(a["id"]).calls if c.tool == "terminal_exec"] == ["pwd"]


async def test_a_call_without_on_is_refused_even_with_one_machine_open(node):  # noqa: F811
    a = await opened(node, session=NONE)
    for tool, kw in (("terminal_exec", {"cmd": "ls"}), ("terminal_job_start", {"cmd": "npm run dev"}),
                     ("environment_save", {}), ("environment_close", {"discard": True}),
                     ("environment_port", {"port": 3000})):
        with pytest.raises(ToolCallError, match="^which_environment") as err:
            await node["call"](tool, session=NONE, **kw)
        words = str(err.value)
        assert "sends no session id" in words and 'on="<that on>"' in words and "—" not in words
        assert a["id"] not in words and a["on"] not in words  # no way into a machine: its title only (round 3)
        assert 'Your agent has 1 machine open: "Black Friday page"' in words
    assert node["mgr"].get(a["id"]).open
    lines = catalog.mcp_lines("which_environment", "")
    assert lines and "on" in lines[0] and "environment_open" in lines[0] and "close" not in lines[0]
    # with nothing open, the same call hears no_environment: open one first
    await node["call"]("environment_close", on=a["on"], session=NONE)
    with pytest.raises(ToolCallError, match="^no_environment"):
        await node["call"]("terminal_exec", cmd="ls", session=NONE)


async def test_whoami_and_studio_name_no_machine_without_its_handle(node):  # noqa: F811
    a = await opened(node, session=NONE)
    rt = node["rt"]
    ctx = SimpleNamespace(runtime=rt, grant=node["grant"], meta=SimpleNamespace(session=None))
    assert node["mgr"].bound_to_call(ctx) is None
    # a localhost page without `on` would be the node's: refused; a public url checks what the node sees
    with pytest.raises(Exception, match="only with its on"), routing.studio_proxy(
            rt, "ui_check", {"url": "http://localhost:3000"}, session=None, grant_id=node["grant"].id,
            family=node["grant"].root_id):
        pass
    with routing.studio_proxy(rt, "ui_check", {"url": "https://example.com"}, session=None,
                              grant_id=node["grant"].id, family=node["grant"].root_id) as lane:
        assert lane is None
    with routing.studio_proxy(rt, "ui_check", {"url": "http://localhost:3000", "on": a["on"]}, session=None,
                              grant_id=node["grant"].id, family=node["grant"].root_id) as lane:
        assert lane == f"env/{a['id']}"


async def test_whoami_names_the_machine_its_session_holds_through_the_service(node):  # noqa: F811
    """whoami (agentos.node) asks the "environments" service by its method bound_to_call(ctx), never
    importing agentos.environments: the real tool against the real manager names the session's own
    machine, and nothing for another chat's session or a client with no session."""
    from agentos.node.tools import tool_specs as node_specs

    [spec] = [s for s in node_specs(node["rt"]) if s.name == "whoami"]
    whoami = wrap_tool(spec, node["rt"])

    async def who(session):
        with bind_request_token(node["token"], session=session):
            return await whoami()

    a = await opened(node)  # session chat-1
    mine = await who("chat-1")
    assert mine["environment"]["id"] == a["id"] and mine["environment"]["state"] == "ready"
    assert f"go to environment {a['id']}" in mine["next"]
    for session in ("chat-2", NONE):
        other = await who(session)
        assert other["environment"] is None and "environment_open(title=" in other["next"]


async def test_jobs_of_a_machine_list_only_with_its_handle(node):  # noqa: F811
    a = await opened(node, session=NONE)
    await node["envs"].job_start(f"env/{a['id']}", "npm run dev", cwd="/work")
    assert [j for j in await node["call"]("terminal_jobs", session=NONE) if j["lane"].startswith("env/")] == []
    mine = await node["call"]("terminal_jobs", on=a["on"], session=NONE)
    assert [j["lane"] for j in mine] == [f"env/{a['id']}"]
    with pytest.raises(ToolCallError, match="^which_environment"):
        await node["call"]("terminal_jobs", on=f"env/{a['id']}", session=NONE)


async def test_the_job_tools_take_on_and_check_it_is_the_jobs_machine(node):  # noqa: F811
    """Round 3 (T2): told to pass `on` on every call, terminal_job_read refused it ("has no argument 'on'")."""
    a = await opened(node, title="Shop page", session=NONE)
    b = await opened(node, title="Docs fix", session=NONE)
    job = await node["envs"].job_start(f"env/{a['id']}", "sleep 30", cwd="/work")
    read = await node["call"]("terminal_job_read", job=job.id, on=a["on"], session=NONE)
    assert read["state"] == "running" and read["environment"]["id"] == a["id"]
    assert (await node["call"]("terminal_job_read", job=job.id, session=NONE))["state"] == "running"  # without: as before
    with pytest.raises(ToolCallError, match="^invalid: job .* is not on the machine that on names") as err:
        await node["call"]("terminal_job_read", job=job.id, on=b["on"], session=NONE)
    assert a["on"] not in str(err.value) and b["on"] not in str(err.value)
    with pytest.raises(ToolCallError, match="^which_environment"):  # a title never reaches a machine
        await node["call"]("terminal_job_cancel", job=job.id, on="Shop page", session=NONE)
    with pytest.raises(ToolCallError, match="^invalid: job"):
        await node["call"]("terminal_job_cancel", job=job.id, on=b["on"], session=NONE)
    assert node["envs"].job(job.id).running                          # nothing was cancelled by a wrong on
    done = await node["call"]("terminal_job_cancel", job=job.id, on=a["on"], session=NONE)
    assert done["state"] == "cancelled"


# ------------------------------------------------------------------ a held title, take_over


async def test_the_title_of_a_held_machine_is_in_use_and_take_over_waits_for_quiet(node):  # noqa: F811
    a = await opened(node, session=NONE)
    with pytest.raises(ToolCallError, match="^environment_in_use") as err:
        await node["call"]("environment_open", title=TITLE, session=NONE)
    words = str(err.value)
    assert f'environment_open(title="{TITLE} 2")' in words and "take_over=true" in words and "10 min" in words
    assert a["on"] not in words and "—" not in words
    lines = catalog.mcp_lines("environment_in_use", words)
    assert lines and "take_over" in lines[0] and "another title" in lines[0]
    later(node, 5)  # quiet, but not long enough
    with pytest.raises(ToolCallError, match="^environment_in_use") as err:
        await node["call"]("environment_open", title=TITLE, take_over=True, session=NONE)
    assert "(5 min from now)" in str(err.value)
    ok = await node["call"]("terminal_exec", on=a["on"], cmd="echo still mine", session=NONE)  # the holder works on
    assert ok["exit_code"] == 0
    assert node["mgr"].get(a["id"]).lease == lease_of(a["on"]) and len(node["mgr"].store.open()) == 1


async def test_take_over_after_the_quiet_time_gives_a_fresh_handle_and_the_old_one_stops(node):  # noqa: F811
    a = await opened(node, session=NONE)
    node["mgr"].settings.take_over_minutes = 3  # a setting (environments.toml)
    later(node, 4)
    with pytest.raises(ToolCallError, match="^environment_in_use") as err:  # without take_over: still refused
        await node["call"]("environment_open", title=TITLE, session=NONE)
    assert "quiet for 4 min" in str(err.value) and "take_over=true" in str(err.value)
    b = await node["call"]("environment_open", title=TITLE, take_over=True, session=NONE)
    assert b["id"] == a["id"] and b["opened"] == "taken_over" and b["on"] != a["on"]
    assert "Taken over" in b["next"] and "work is on it" in b["next"] and "on no longer works" in b["next"]
    with pytest.raises(ToolCallError, match="^no_environment") as err:
        await node["call"]("terminal_exec", on=a["on"], cmd="ls", session=NONE)
    assert "took environment" in str(err.value) and "over at" in str(err.value)
    assert (await node["call"]("terminal_exec", on=b["on"], cmd="ls", session=NONE))["exit_code"] == 0
    env = node["mgr"].get(a["id"])
    assert env.handoffs[-1].lease == lease_of(a["on"]) and env.lease == lease_of(b["on"])
    # the move counts as a call: the chat that lost it cannot take it straight back
    with pytest.raises(ToolCallError, match="^environment_in_use"):
        await node["call"]("environment_open", title=TITLE, take_over=True, session=NONE)


async def test_concurrent_opens_of_one_title_give_one_machine_to_one_chat(node):  # noqa: F811
    call = node["call"]
    res = await asyncio.gather(call("environment_open", title="One", wait_s=5, session=NONE),
                               call("environment_open", title="One", wait_s=5, session=NONE),
                               return_exceptions=True)
    won = [r for r in res if isinstance(r, dict)]
    lost = [r for r in res if isinstance(r, ToolCallError)]
    assert len(won) == 1 and len(lost) == 1 and str(lost[0]).startswith("environment_in_use")
    assert len(node["vms"].store.recent()) == 1


async def test_a_kept_machine_let_go_is_nobodys_until_its_title_takes_it(node):  # noqa: F811
    node["mgr"].settings.allow_keep_live = True
    a = await opened(node, session=NONE, keep="live")
    out = await node["call"]("environment_close", on=a["on"], session=NONE)
    assert out["machine"] == "kept" and "on no longer works" in out["next"]
    with pytest.raises(ToolCallError, match="^no_environment.*let go"):
        await node["call"]("terminal_exec", on=a["on"], cmd="ls", session=NONE)
    with pytest.raises(ToolCallError, match="^no_environment"):  # nobody holds it: never "the one open"
        await node["call"]("terminal_exec", cmd="ls", session=NONE)
    back = await node["call"]("environment_open", title=TITLE, session=NONE)  # no take_over needed
    assert back["id"] == a["id"] and back["opened"] == "attached" and back["on"] != a["on"]
    assert (await node["call"]("terminal_exec", on=back["on"], cmd="ls", session=NONE))["environment"]["id"] == a["id"]
    await node["call"]("environment_close", on=back["on"], final=True, session=NONE)


# ------------------------------------------------------------------ listings


async def test_listings_show_no_handle_of_a_machine_the_caller_does_not_hold(node):  # noqa: F811
    a = await opened(node, title="Shop page", session=NONE)
    b = await opened(node, title="Docs fix", session=NONE)
    plain = await node["call"]("environment", session=NONE)
    assert plain["environment"] is None and "cannot tell which machine is yours" in plain["note"]
    for row in plain["open"]:
        assert set(row) == {"title", "repo", "state", "last_call_at", "use"}
        assert row["use"].startswith("held by the chat that opened it")  # with no session id it may be this one
    full = await node["call"]("environment", on=a["on"], session=NONE)
    assert full["id"] == a["id"] and full["on"] == a["on"]
    rows = {r["title"]: r for r in full["open"]}
    assert rows["Shop page"]["this_chat"] is True and rows["Shop page"]["id"] == a["id"]
    assert "id" not in rows["Docs fix"] and "on" not in rows["Docs fix"]
    text = str(plain) + str(full)
    assert b["on"] not in text and b["on"].split(".")[1] not in text
    later(node, 11)
    quiet = await node["call"]("environment", session=NONE)
    assert all("take_over=true" in r["use"] for r in quiet["open"])
    # the owner's views see every machine, never a handle
    summary = node["mgr"].summary(node["mgr"].get(a["id"]))
    detail = await node["mgr"].detail(node["mgr"].get(a["id"]))
    assert a["on"] not in str(summary) + str(detail) and "lease" not in summary and "lease" not in detail


async def test_no_handle_reaches_a_file_on_the_node(node, tmp_path):  # noqa: F811
    """The node keeps only a handle's hash: the traces, the audit log and the store never hold it, used,
    stale or wrong."""
    a = await opened(node, session=NONE)
    await node["call"]("terminal_exec", on=a["on"], cmd="pwd", session=NONE)
    await node["call"]("environment", on=a["on"], session=NONE)
    wrong = a["id"] + ".zzzzzzzzzzzzzzzz"
    with pytest.raises(ToolCallError):
        await node["call"]("terminal_exec", on=wrong, cmd="pwd", session=NONE)
    secret, bad = a["on"].split(".")[1].encode(), wrong.split(".")[1].encode()
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files and not [p for p in files if secret in p.read_bytes() or bad in p.read_bytes()]


async def test_the_session_binding_of_a_legacy_client_is_unchanged(node):  # noqa: F811
    a = await opened(node, title="Shop page", session="chat-1")
    b = await opened(node, title="Docs fix", session="chat-2")
    assert HANDLE_RE.match(a["on"]) and "calls without `on` go here" in a["next"]
    one = await node["call"]("terminal_exec", cmd="pwd", session="chat-1")
    two = await node["call"]("terminal_exec", cmd="pwd", session="chat-2")
    assert one["environment"]["id"] == a["id"] and two["environment"]["id"] == b["id"]
    assert (await node["call"]("environment", session="chat-2"))["id"] == b["id"]
    # its own by id or lane works; the other session's does not, nor any title
    assert (await node["call"]("terminal_exec", on=f"env/{a['id']}", cmd="pwd", session="chat-1"))["exit_code"] == 0
    with pytest.raises(ToolCallError, match="^which_environment"):
        await node["call"]("terminal_exec", on=a["id"], cmd="pwd", session="chat-2")
    with pytest.raises(ToolCallError, match="^which_environment"):
        await node["call"]("terminal_exec", on="Docs fix", cmd="pwd", session="chat-2")
    # a session that holds none hears no_environment, never another chat's computer
    with pytest.raises(ToolCallError, match="^no_environment"):
        await node["call"]("terminal_exec", cmd="pwd", session="chat-3")
    # its own title gives it back to the same session
    again = await node["call"]("environment_open", title="Shop page", session="chat-1")
    assert again["opened"] == "attached" and again["id"] == a["id"]
    assert node["mgr"].get(a["id"]).binding == "session:chat-1"
    assert node["mgr"].get(b["id"]).binding == "session:chat-2"


async def test_an_attach_says_what_differs_from_what_was_asked(node):  # noqa: F811
    a = await opened(node, keep="none", hours=2)
    again = await node["call"]("environment_open", title=TITLE.lower(), keep="save", hours=1, size="large",
                               ref="develop")
    assert again["id"] == a["id"] and again["opened"] == "attached"
    assert again["keep"] == "none"  # it keeps how it was opened
    assert set(again["applied"]) == {"keep", "expires_at", "size", "ref"}
    assert len(again["differs"]) == 4 and any(d.startswith('keep: you asked for "save"') for d in again["differs"])
    assert again["next"].startswith("Attached: this machine was already open")
    assert "Not as you asked: " in again["next"] and "another title" in again["next"]
    assert len(node["mgr"].store.open()) == 1 and "—" not in again["next"]


async def test_a_lane_name_as_on_reaches_the_sessions_own_machine_and_names_the_mix_up_else(node):  # noqa: F811
    """Haiku, 2026-11-18: browser_navigate(on="<id>-browser"), the open's lane name without env/. The chat
    whose session holds the machine reaches it, as with env/<id>; any other caller hears it is a lane's
    name, and a lane name of no machine of its own is no_environment with the same words."""
    a = await opened(node, session="s-a")
    ran = await node["call"]("terminal_exec", on=f"{a['id']}-browser", cmd="pwd", session="s-a")
    assert ran["exit_code"] == 0 and ran["environment"]["id"] == a["id"]
    with pytest.raises(ToolCallError, match="^which_environment") as err:
        await node["call"]("terminal_exec", on=f"{a['id']}-browser", cmd="pwd", session="s-b")
    assert "is the browser lane's name, not an on" in str(err.value) and a["on"] not in str(err.value)
    with pytest.raises(ToolCallError, match="^no_environment") as err:
        await node["call"]("terminal_exec", on="eaaaaaaaaaa-browser", cmd="pwd", session="s-a")
    assert "looks like a lane's name" in str(err.value) and "—" not in str(err.value)


async def test_the_open_result_names_no_lane(node):  # noqa: F811
    """An open's result carries `on` and no lane names, so nothing in it looks like another `on`."""
    a = await opened(node, session=NONE)
    assert "lanes" not in a and "-browser" not in str(a.get("next"))


async def test_on_leaves_account_lanes_alone_and_maps_a_browser_tool(node):  # noqa: F811
    a = await opened(node, session=NONE)
    rt, grant = node["rt"], node["grant"]
    account = {"on": "personal/reddit"}  # an account lane always has a "/": never a title
    assert routing.route(rt, "browser_navigate", account, session=None, grant_id=grant.id,
                         family=grant.root_id) is None
    assert account == {"on": "personal/reddit"}
    kw = {"on": a["on"]}
    env = routing.route(rt, "browser_navigate", kw, session=None, grant_id=grant.id, family=grant.root_id)
    assert env.id == a["id"] and kw["on"] == f"env/{a['id']}-browser"
    other = {"on": a["on"]}  # another family's caller with this family's handle: nobody's
    with pytest.raises(Exception, match="not the handle of a machine of yours"):
        routing.route(rt, "terminal_exec", other, session=None, grant_id="g2", family="g2")
    lane = {"on": f"env/{a['id']}"}  # another family by lane: left as it was (the policy refuses it)
    assert routing.route(rt, "terminal_exec", lane, session=None, grant_id="g2", family="g2") is None


async def test_the_most_useful_error_wins(node):  # noqa: F811
    a = await opened(node, session=NONE)
    node["mgr"].settings.max_size = "medium"
    # the title of an open one with a size past the limit: the limit, not environment_in_use
    with pytest.raises(ToolCallError, match="^environment_limit") as err:
        await node["call"]("environment_open", title=TITLE, size="large", session=NONE)
    assert "biggest environment on this node is medium" in str(err.value)
    with pytest.raises(ToolCallError, match="^invalid"):
        await node["call"]("environment_open", title=TITLE, keep="forever", session=NONE)
    with pytest.raises(ToolCallError, match="^environment_limit"):
        await node["call"]("environment_open", title=TITLE, hours=500, session=NONE)
    # a repository the open one does not have: pick another title, never close or discard it
    with pytest.raises(ToolCallError, match="^environment_already_open") as err:
        await node["call"]("environment_open", title=TITLE, repo="example.test/me/other", session=NONE)
    assert "another title" in str(err.value) and "discard" not in str(err.value)
    assert [e.id for e in node["mgr"].store.open()] == [a["id"]]


async def test_the_call_log_names_the_client(node):  # noqa: F811
    a = await opened(node, session=NONE)
    token = elicitor_var.set(SimpleNamespace(client="claude-code 2.1.3"))
    try:
        await node["call"]("terminal_exec", on=a["on"], cmd="git status", session=NONE)
    finally:
        elicitor_var.reset(token)
    seen = await node["call"]("environment", on=a["on"], session=NONE)
    [call] = [c for c in seen["calls"] if c["tool"] == "terminal_exec"]
    assert call["client"] == "claude-code 2.1.3"


# ------------------------------------------------------------------ errors that help


def test_an_unknown_argument_names_the_real_ones_with_a_did_you_mean(node):  # noqa: F811
    from agentos.environments.tools import tool_specs

    spec = next(s for s in tool_specs(node["rt"]) if s.name == "environment_open")
    known = frozenset({"title", "repo", "ref", "size", "hours", "wait_s", "keep"})
    words = unknown_arguments(spec, known, {"name": "x", "repository": "y", "title": "t"})
    assert words.startswith("invalid: environment_open has no arguments")
    assert "'name' (did you mean 'title'?)" in words and "'repository' (did you mean 'repo'?)" in words
    assert "It takes: title, repo, ref, size, hours, wait_s, keep." in words
    assert unknown_arguments(spec, known, {"title": "t"}) is None


def test_tools_more_with_a_tool_name_returns_that_tool_only():
    tools = [SimpleNamespace(name=n, description=n, input_schema={}) for n in ("environment_open", "terminal_exec")]
    out, error, _ = ts.more({"core": tools}, frozenset({"core"}), "environment_open", lambda _k: False)
    assert error is None and [t["name"] for t in out["tools"]] == ["environment_open"]
    whole, _, _ = ts.more({"core": tools}, frozenset({"core"}), "core", lambda _k: False)
    assert len(whole["tools"]) == 2
