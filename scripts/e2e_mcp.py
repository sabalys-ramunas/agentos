#!/usr/bin/env python3
"""End-to-end check of a running node through MCP only, exactly as an agent sees it.

    ssh -N -L 17400:localhost:7400 mynode &
    AGENTOS_URL=http://localhost:17400/mcp AGENTOS_TOKEN=... \\
      uv run python scripts/e2e_mcp.py --lane personal/reddit --url https://www.reddit.com/r/popular/

Add --upvote to click the first post's upvote button by its snapshot ref and check that the
button's own state changed (the M1 acceptance). The token is read from the environment and
never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def short(value: str, n: int = 300) -> str:
    value = value.replace("\n", " | ")
    return value if len(value) <= n else value[:n] + " ..."


async def call(client, tool: str, args: dict, *, quiet: bool = False):
    t0 = time.monotonic()
    result = await client.call_tool(tool, args)
    dt = time.monotonic() - t0
    status = "ERROR" if result.is_error else "ok"
    if not quiet:
        print(f"[{dt:5.1f}s] {tool} {json.dumps(args)} -> {status}: {short(text_of(result))}")
    return result


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--upvote", action="store_true", help="click the first post's upvote (Reddit)")
    ap.add_argument("--close", action="store_true", help="close the lane at the end")
    ap.add_argument("--shot", default="e2e-shot.png")
    a = ap.parse_args()

    url = os.environ.get("AGENTOS_URL", "http://localhost:17400/mcp")
    token = os.environ.get("AGENTOS_TOKEN")
    if not token:
        print("set AGENTOS_TOKEN", file=sys.stderr)
        return 2

    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=180) as http,
        Client(streamable_http_client(url, http_client=http), mode="legacy") as client,
    ):
        tools = await client.list_tools()
        print("tools:", ", ".join(sorted(t.name for t in tools.tools)))
        await call(client, "whoami", {})
        await call(client, "browsers_list", {})
        opened = await call(client, "browser_open", {"on": a.lane})
        if opened.is_error:
            return 1
        nav = await call(client, "browser_navigate", {"on": a.lane, "url": a.url})
        if nav.is_error:
            return 1
        snap = await call(client, "browser_snapshot", {"on": a.lane}, quiet=True)
        outline = text_of(snap)
        print(f"snapshot: {len(outline)} chars, {outline.count('[ref=')} refs")
        logged_in = bool(re.search(r"(Expand user menu|Open user menu|User Avatar|Create post|\bCreate\b)", outline))
        print("looks logged in:", logged_in)

        if a.upvote:
            m = re.search(r'button "Upvote"[^\n]*\[ref=((?:f\d+)?e\d+)\]', outline, re.IGNORECASE)
            if not m:
                # Reddit's vote buttons are unnamed: in each post's "Actions available for this
                # post" group the first button is the upvote, then the score, then the downvote.
                m = re.search(r'"Actions available for this post":\n(?:[^\n]*\n){0,3}?\s*- button \[ref=((?:f\d+)?e\d+)\]',
                              outline)
            if not m:
                print("no upvote button ref found in the snapshot")
                return 1
            ref = m.group(1)
            print("upvote ref:", ref)
            before = outline[max(0, m.start() - 600):m.start()]
            before = outline[max(0, m.start() - 1500):m.start()]
            titles = re.findall(r'link "([^"]{12,140})" \[ref', before)
            print("target post (nearest title above the button):", titles[-1] if titles else "unknown")
            act = await call(client, "browser_act", {"on": a.lane, "ref": ref, "action": "click"})
            if act.is_error:
                return 1
            try:
                body = json.loads(text_of(act))
            except ValueError:
                body = {}
            print("read-back: changed =", body.get("changed"), "| detail =", body.get("detail"))

        shot = await call(client, "browser_screenshot", {"on": a.lane}, quiet=True)
        for c in shot.content:
            if getattr(c, "type", "") == "image":
                Path(a.shot).write_bytes(base64.b64decode(c.data))
                print("screenshot saved:", a.shot)
        await call(client, "browser_view", {"on": a.lane})
        if a.close:
            await call(client, "browser_close", {"on": a.lane})
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
