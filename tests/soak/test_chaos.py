"""Chaos on a real node: things break while an agent's call is in flight (docs/BENCH.md).

    AGENTOS_TEST_SLOT=53 scripts/node-test.sh soak -m soak tests/soak/test_chaos.py -q

Each case asserts what must hold afterwards: no lane is left wedged (the next calls work, or fail
with the documented error and work once the cause is gone), holds and quarantines are what they
should be, the call that was cut is reported to the agent as failed and in the action log, no
secret reaches agentosd's logs (checked by the `node` fixture), and nothing of the slot is left
running once the daemon stops (also the fixture).
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest
from soak_support import (
    CAPTCHA,
    FEED,
    LONG_TEXT,
    call,
    control,
    kill_our_process,
    lanes,
    name_field,
    ok,
    reported_failure,
    until,
)

from agentos.dev.slotnode import (
    SITE,
    browsers_under,
    json_of,
    leftovers,
    main_browsers_under,
    mcp_client,
    slot_display_processes,
    text_of,
)

pytestmark = [pytest.mark.soak]


async def outcome_of(task: asyncio.Task, timeout: float = 120):
    try:
        return await asyncio.wait_for(task, timeout)
    except Exception as e:  # noqa: BLE001 - the agent's view of a cut call
        return e


async def tool_ends(client, lane: str, tool: str) -> list[dict]:
    rows = json_of(await ok(client, "traces_query", {"lane": lane, "tool": tool, "limit": 50}))
    items = rows.get("events", []) if isinstance(rows, dict) else rows
    return [r for r in items if isinstance(r, dict) and r.get("kind") == "tool_end"]


async def test_the_browser_killed_mid_action(node):
    lane = "chaos/browser"
    daemon = node([lane])
    async with mcp_client(daemon.url, daemon.agent_token) as client:
        ref = await name_field(client, lane)
        [main] = main_browsers_under(daemon.home)
        typing = asyncio.create_task(call(client, "browser_act", {"on": lane, "ref": ref, "action": "fill",
                                                                   "text": LONG_TEXT}))
        await asyncio.sleep(2.0)  # a few characters in
        assert not typing.done(), "the fill finished before the browser was killed: make LONG_TEXT longer"
        kill_our_process(main, daemon.home)
        cut = await outcome_of(typing)
        assert reported_failure(cut), f"the cut fill was reported as done: {text_of(cut)[:600]}"
        ends = await tool_ends(client, lane, "browser_act")
        assert ends and ends[0].get("status") != "ok", ends[:2]

        # not wedged: the lane goes cold by itself, or the next call brings it back (one retry, as documented)
        await until(lambda: not browsers_under(daemon.home), 20, "the dead browser's processes are gone")
        nav = await call(client, "browser_navigate", {"on": lane, "url": FEED})
        if nav.is_error:
            assert "engine_error" in text_of(nav), text_of(nav)
            nav = await ok(client, "browser_navigate", {"on": lane, "url": FEED})
        assert '"status": 200' in text_of(nav).replace('"status":200', '"status": 200'), text_of(nav)[:600]
        assert (await lanes(client))[lane]["state"] in ("warm", "active")
        assert len(main_browsers_under(daemon.home)) == 1
        displays = slot_display_processes(daemon.slot)
        assert len([p for p in displays if "Xvfb" in p[1]]) == 1, f"one display for one lane: {displays}"
        await ok(client, "lane_close", {"on": lane})
    assert not leftovers(daemon)


async def test_agentosd_killed_mid_action_then_restarted(node, proxy):
    act, held, moved = "chaos/act", "chaos/held", "chaos/moved"
    daemon = node([act, held, moved], max_warm_lanes=2)
    async with mcp_client(daemon.url, daemon.agent_token) as client:
        # a hold: a captcha page holds the lane for a human
        await ok(client, "browser_navigate", {"on": held, "url": CAPTCHA})
        assert (await control(client, held))["state"] == "paused"
        # a quarantine: the gateway's exit moves between two opens
        await ok(client, "lane_open", {"on": moved})
        await ok(client, "lane_close", {"on": moved})
        exit_before, proxy.exit_ip = proxy.exit_ip, "203.0.113.99"
        reopened = json_of(await ok(client, "lane_open", {"on": moved}))
        assert reopened["state"] == "quarantined", reopened
        proxy.exit_ip = exit_before  # the quarantine waits for a human, whatever the exit does now

        ref = await name_field(client, act)
        before = {"browsers": browsers_under(daemon.home), "displays": slot_display_processes(daemon.slot)}
        assert before["browsers"] and before["displays"]
        typing = asyncio.create_task(call(client, "browser_act", {"on": act, "ref": ref, "action": "fill",
                                                                   "text": LONG_TEXT}))
        await asyncio.sleep(2.0)
        assert not typing.done()
        daemon.kill()
        cut = await outcome_of(typing, 30)
        assert reported_failure(cut), f"the cut fill was reported as done: {cut!r}"

    orphans = {"browsers": browsers_under(daemon.home), "displays": slot_display_processes(daemon.slot)}
    daemon.start()
    await until(lambda: not leftovers(daemon), 45,
                f"the restarted daemon clears what the killed one left ({orphans})")
    async with mcp_client(daemon.url, daemon.agent_token) as client:
        state = await lanes(client)
        assert state[act]["state"] == "cold", state[act]
        assert state[moved]["state"] == "quarantined", state[moved]
        hold = await control(client, held)
        assert hold["state"] == "paused" and "captcha" in (hold.get("reason") or ""), hold
        # the held lane stays held for agents, the quarantined lane stays quarantined
        refused = await call(client, "browser_snapshot", {"on": held})
        assert refused.is_error and "agent_paused" in text_of(refused), text_of(refused)[:400]
        still = await call(client, "lane_open", {"on": moved})
        assert (still.is_error and text_of(still).startswith("quarantined")) or \
            json_of(still).get("state") == "quarantined", text_of(still)[:400]
        assert not [p for p in main_browsers_under(daemon.home)], "a quarantined lane started a browser"
        # the cut call is in the action log, and not as done
        ends = await tool_ends(client, act, "browser_act")
        assert ends and ends[0].get("status") != "ok", f"the cut browser_act has no failed end: {ends[:2]}"
        # and the lane works again
        ref = await name_field(client, act)
        done = json_of(await ok(client, "browser_act", {"on": act, "ref": ref, "action": "fill", "text": "after"}))
        assert done.get("ok") is True, done
        await ok(client, "lane_close", {"on": act})


async def test_the_proxy_unreachable_mid_action(node, proxy):
    lane = "chaos/proxy"
    daemon = node([lane])
    async with mcp_client(daemon.url, daemon.agent_token) as client:
        await ok(client, "browser_navigate", {"on": lane, "url": FEED})
        loading = asyncio.create_task(call(client, "browser_navigate", {"on": lane,
                                                                         "url": f"{SITE}/slow/5000/form.html"}))
        await asyncio.sleep(1.5)
        assert not loading.done()
        seen = len(proxy.requests)
        proxy.stop()
        cut = await outcome_of(loading)
        assert reported_failure(cut) or "form.html" not in json_of(cut).get("url", ""), \
            f"the cut navigation was reported as loaded: {text_of(cut)[:600]}"

        # while it is down: calls fail fast and say so, nothing goes around the proxy
        down = await call(client, "browser_navigate", {"on": lane, "url": FEED}, timeout=90)
        assert reported_failure(down), text_of(down)[:600]
        assert len(proxy.requests) == seen
        assert (await lanes(client))[lane]["state"] in ("warm", "active", "cold", "quarantined")
        await ok(client, "lane_close", {"on": lane}, timeout=60)
        cold_open = json_of(await ok(client, "lane_open", {"on": lane}, timeout=90))
        assert cold_open["state"] == "quarantined", f"a lane whose proxy is down must not open: {cold_open}"
        assert not browsers_under(daemon.home), "a quarantined lane keeps no browser"

        proxy.start()
        back = json_of(await ok(client, "lane_open", {"on": lane}))
        assert back["state"] == "warm", back
        await ok(client, "browser_navigate", {"on": lane, "url": FEED})
        await ok(client, "lane_close", {"on": lane})


async def test_the_disk_fills_near_the_floor(node):
    lane, other = "chaos/disk", "chaos/disk2"
    margin = 300 * 1024**2
    daemon = node([lane, other], start=False)
    daemon.set_node(disk_floor_bytes=shutil.disk_usage(daemon.home).free - margin)
    daemon.start()
    filler = daemon.home / "filler.bin"
    try:
        async with mcp_client(daemon.url, daemon.agent_token) as client:
            ref = await name_field(client, lane)
            typing = asyncio.create_task(call(client, "browser_act", {"on": lane, "ref": ref, "action": "fill",
                                                                       "text": LONG_TEXT}))
            await asyncio.sleep(1.5)
            with filler.open("wb") as f:  # below the floor while the fill types
                os.posix_fallocate(f.fileno(), 0, margin + 200 * 1024**2)
            assert shutil.disk_usage(daemon.home).free < daemon.config().disk_floor_bytes
            typed = await outcome_of(typing)
            assert not reported_failure(typed), f"a warm lane's fill failed below the floor: {typed!r}"
            assert json_of(typed).get("ok") is True

            refused = await call(client, "lane_open", {"on": other})
            assert refused.is_error and "resources_low" in text_of(refused), text_of(refused)[:600]
            assert "disk" in text_of(refused).lower()
            assert len(main_browsers_under(daemon.home)) == 1, "the refused lane started nothing"
            assert (await lanes(client))[other]["state"] == "cold"
            await ok(client, "lane_close", {"on": lane})

            filler.unlink()
            assert json_of(await ok(client, "lane_open", {"on": other}))["state"] == "warm"
            await ok(client, "lane_close", {"on": other})
    finally:
        filler.unlink(missing_ok=True)
