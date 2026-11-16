"""The title names the computer (agentos.environments, 2026-11-16).

MCP 2026-07-28 clients (Claude Code, T3 Code) send no session id, so every chat of one agent binds to
its grant. Three testers of 2026-11-15 then shared one environment: a second chat's environment_open
with a new title got the first chat's machine, another was told to close someone else's work, and
call logs mixed. Now a title names one computer of the grant family: a new title opens one, an open
one's title attaches to it (and says what differs from what was asked). Calls without `on` go to a
session's own (a client that sends a session id, as before), else to the only open one, else they
are refused with which_environment. `on` takes a title, an id or env/<id>.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_environments import TITLE, node, opened  # noqa: F401  (node is a fixture)

from agentos.core import catalog
from agentos.core.tools import elicitor_var
from agentos.environments import routing
from agentos.mcp import toolsets as ts
from agentos.mcp.registry import unknown_arguments
from agentos.policy.wrap import ToolCallError

NONE = None  # a client that sends no MCP session id (the 2026-07-28 protocol, stdio)


async def test_two_chats_without_session_ids_each_open_their_own(node):  # noqa: F811
    a = await opened(node, title="Shop page", session=NONE)
    b = await opened(node, title="Docs fix", session=NONE)
    assert a["id"] != b["id"] and a["opened"] == b["opened"] == "new"
    assert {e.title for e in node["mgr"].store.open()} == {"Shop page", "Docs fix"}
    # with both open, a call without `on` could go to either: refused, naming each with its `on`
    for tool, kw in (("lane_exec", {"cmd": "ls"}), ("environment", {}), ("environment_save", {})):
        with pytest.raises(ToolCallError, match="^which_environment") as err:
            await node["call"](tool, session=NONE, **kw)
        words = str(err.value)
        assert f'"Shop page" (on="env/{a["id"]}")' in words and f'"Docs fix" (on="env/{b["id"]}")' in words
        assert "Call again with on" in words and "—" not in words
    lines = catalog.mcp_lines("which_environment", "")
    assert lines and "`on`" in lines[0] and "close" not in lines[0]
    # nothing ran on either machine for those calls
    assert not [c for c in node["mgr"].get(a["id"]).calls if c.tool == "lane_exec"]


async def test_one_open_takes_calls_without_on_and_the_result_names_it(node):  # noqa: F811
    a = await opened(node, session=NONE)
    ran = await node["call"]("lane_exec", cmd="npm test", session=NONE)
    assert ran["exit_code"] == 0 and ran["environment"] == {"id": a["id"], "title": TITLE}
    seen = await node["call"]("environment", session=NONE)
    assert seen["id"] == a["id"] and seen["title"] == TITLE


async def test_the_session_binding_of_a_legacy_client_is_unchanged(node):  # noqa: F811
    a = await opened(node, title="Shop page", session="chat-1")
    b = await opened(node, title="Docs fix", session="chat-2")
    # two are open, but each session's calls go to its own: no which_environment
    one = await node["call"]("lane_exec", cmd="pwd", session="chat-1")
    two = await node["call"]("lane_exec", cmd="pwd", session="chat-2")
    assert one["environment"]["id"] == a["id"] and two["environment"]["id"] == b["id"]
    assert (await node["call"]("environment", session="chat-2"))["id"] == b["id"]
    # a session that opened none hears no_environment, never another chat's computer
    with pytest.raises(ToolCallError, match="^no_environment"):
        await node["call"]("lane_exec", cmd="pwd", session="chat-3")
    assert node["mgr"].get(a["id"]).binding == "session:chat-1"
    assert node["mgr"].get(b["id"]).binding == "session:chat-2"


async def test_an_attach_says_what_differs_from_what_was_asked(node):  # noqa: F811
    a = await opened(node, session=NONE, keep="none", hours=2)
    again = await node["call"]("environment_open", title=TITLE.lower(), keep="save", hours=1, size="large",
                               ref="develop", session=NONE)
    assert again["id"] == a["id"] and again["opened"] == "attached"
    assert again["keep"] == "none"  # it keeps how it was opened
    assert set(again["applied"]) == {"keep", "expires_at", "size", "ref"}
    assert again["applied"]["keep"] == "none"
    assert len(again["differs"]) == 4 and any(d.startswith('keep: you asked for "save"') for d in again["differs"])
    assert again["next"].startswith("Attached: an environment with this title was already open")
    assert "Not as you asked: " in again["next"] and "another title" in again["next"]
    assert len(node["mgr"].store.open()) == 1 and "—" not in again["next"]
    # the same title with nothing that differs: attached, and no applied or differs
    plain = await node["call"]("environment_open", title=TITLE, session=NONE)
    assert plain["opened"] == "attached" and "differs" not in plain and "applied" not in plain


async def test_on_names_an_environment_by_title_id_or_lane(node):  # noqa: F811
    a = await opened(node, title="Shop page", session=NONE)
    b = await opened(node, title="Docs fix", session=NONE)
    by_title = await node["call"]("lane_exec", on="docs FIX", cmd="pwd", session=NONE)
    assert by_title["environment"] == {"id": b["id"], "title": "Docs fix"}
    by_id = await node["call"]("lane_exec", on=a["id"], cmd="pwd", session=NONE)
    assert by_id["environment"]["id"] == a["id"]
    by_lane = await node["call"]("lane_exec", on=f"env/{a['id']}", cmd="pwd", session=NONE)
    assert by_lane["environment"]["id"] == a["id"]
    assert (await node["call"]("environment", on="Shop page", session=NONE))["id"] == a["id"]
    shop = await opened(node, title="eshop", session=NONE)  # a title that looks like an id
    assert (await node["call"]("environment", on="eshop", session=NONE))["id"] == shop["id"]
    assert (await node["call"]("lane_exec", on="eshop", cmd="pwd", session=NONE))["environment"]["id"] == shop["id"]
    await node["call"]("environment_close", on="eshop", session=NONE)
    with pytest.raises(ToolCallError, match="^no_environment") as err:
        await node["call"]("lane_exec", on="Nothing like it", cmd="pwd", session=NONE)
    assert '"Shop page"' in str(err.value) and "workspace/name" in str(err.value)
    closed = await node["call"]("environment_close", on="Docs fix", session=NONE)
    assert closed["id"] == b["id"] and closed["title"] == "Docs fix"
    # with one left, calls without `on` go to it again
    assert (await node["call"]("lane_exec", cmd="pwd", session=NONE))["environment"]["id"] == a["id"]


async def test_on_leaves_account_lanes_alone_and_maps_a_browser_tool(node):  # noqa: F811
    a = await opened(node, session=NONE)
    rt, grant = node["rt"], node["grant"]
    account = {"on": "personal/reddit"}  # an account lane always has a "/": never a title
    assert routing.route(rt, "browser_navigate", account, session=None, grant_id=grant.id,
                         family=grant.root_id) is None
    assert account == {"on": "personal/reddit"}
    for on in (TITLE, a["id"], f"env/{a['id']}"):
        kw = {"on": on}
        env = routing.route(rt, "browser_navigate", kw, session=None, grant_id=grant.id, family=grant.root_id)
        assert env.id == a["id"] and kw["on"] == f"env/{a['id']}-browser", on
    other = {"on": f"env/{a['id']}"}  # another family's caller: left as it was (the policy refuses it)
    assert routing.route(rt, "lane_exec", other, session=None, grant_id="g2", family="g2") is None
    assert other == {"on": f"env/{a['id']}"}


async def test_the_most_useful_error_wins(node):  # noqa: F811
    a = await opened(node, session=NONE)
    node["mgr"].settings.max_size = "medium"
    # the title of an open one with a size past the limit: the limit, not an attach
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


async def test_a_discarding_close_without_a_session_names_the_environment(node):  # noqa: F811
    a = await opened(node, session=NONE)
    with pytest.raises(ToolCallError, match="^invalid") as err:
        await node["call"]("environment_close", discard=True, session=NONE)
    assert 'environment_close(on="<its title>", discard=true)' in str(err.value)
    assert node["mgr"].get(a["id"]).open
    out = await node["call"]("environment_close", on=TITLE, discard=True, session=NONE)
    assert out["state"] == "closed"


async def test_the_call_log_names_the_client(node):  # noqa: F811
    a = await opened(node, session=NONE)
    token = elicitor_var.set(SimpleNamespace(client="claude-code 2.1.3"))
    try:
        await node["call"]("lane_exec", cmd="git status", session=NONE)
    finally:
        elicitor_var.reset(token)
    seen = await node["call"]("environment", on=a["id"], session=NONE)
    [call] = [c for c in seen["calls"] if c["tool"] == "lane_exec"]
    assert call["client"] == "claude-code 2.1.3"


async def test_a_kept_environment_let_go_is_not_picked(node):  # noqa: F811
    node["mgr"].settings.allow_keep_live = True
    a = await opened(node, session=NONE, keep="live")
    out = await node["call"]("environment_close", session=NONE)
    assert out["machine"] == "kept"
    with pytest.raises(ToolCallError, match="^no_environment"):  # nobody holds it: not "the one open"
        await node["call"]("lane_exec", cmd="ls", session=NONE)
    back = await node["call"]("environment_open", title=TITLE, session=NONE)
    assert back["id"] == a["id"] and back["opened"] == "attached"
    assert (await node["call"]("lane_exec", cmd="ls", session=NONE))["environment"]["id"] == a["id"]
    await node["call"]("environment_close", on=TITLE, final=True, session=NONE)


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
    tools = [SimpleNamespace(name=n, description=n, input_schema={}) for n in ("environment_open", "lane_exec")]
    out, error, _ = ts.more({"core": tools}, frozenset({"core"}), "environment_open", lambda _k: False)
    assert error is None and [t["name"] for t in out["tools"]] == ["environment_open"]
    whole, _, _ = ts.more({"core": tools}, frozenset({"core"}), "core", lambda _k: False)
    assert len(whole["tools"]) == 2
