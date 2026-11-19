"""A scripted MCP agent: a stand-in for a model, to test the mcp-agent plumbing end to end.

    python scripted_agent.py --config {config} --lane {lane}

It reads the MCP config the harness wrote and does one of a few tasks (named by the lane,
`lanebench/<task id>`) the way a careful agent would, through the bench's MCP tools only:

- feed-like: finds the Like button of @mira's tomatoes post in the outline and clicks it.
- login-*: refuses optional cookies when a cookie wall shows, signs in with the vault (`login`), then
  answers what the site shows next: "Not now" to saving the login, the terms accepted with
  sharing left off, and a stop (`held`) at a device approval it cannot do.
- compose-publish: types the post, finds photo.png with browser_files, attaches it with browser_upload
  and publishes.

With `--type-password PW` it signs in by typing the password itself instead (what the bench must
refuse: a login POST goes only inside the vault's login window).

It knows these tasks only; the point is the path (MCP, the stand-in grant, the vault and its login
window, the files inbox, the node's tools, human input), not the reasoning.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tests/, for lanebench.sites.strings

from lanebench.sites.strings import STRINGS


def names(key: str) -> list[str]:
    return [STRINGS[lang][key] for lang in STRINGS]


def find_ref(outline: str, role: str, wanted: list[str], within: tuple[str, str] | None = None) -> str | None:
    """The ref of the first `role "name"` line whose name is one of `wanted` (inside the first
    `within` = (role, name regex) block, when given)."""
    lines = outline.splitlines()
    if within is not None:
        start = next((i for i, line in enumerate(lines)
                      if re.search(rf'- {within[0]} "[^"]*{within[1]}', line)), None)
        if start is None:
            return None
        indent = len(lines[start]) - len(lines[start].lstrip())
        block = []
        for inner in lines[start + 1:]:
            if len(inner) - len(inner.lstrip()) <= indent:
                break
            block.append(inner)
        lines = block
    for line in lines:
        m = re.search(rf'- {role} "([^"]*)"[^\n]*\[ref=(\w+)\]', line)
        if m and m.group(1) in wanted:
            return m.group(2)
    return None


def has_heading(outline: str, key: str) -> bool:
    return any(re.search(rf'- heading "{re.escape(n)}"', outline) for n in names(key))


class Agent:
    def __init__(self, client: Any, lane: str):
        self.client = client
        self.lane = lane

    async def call(self, tool: str, **args: Any) -> str:
        result = await self.client.call_tool(tool, {"on": self.lane, **args})
        text = "\n".join(getattr(c, "text", "") for c in result.content)
        if result.is_error:
            raise RuntimeError(f"{tool}: {text[:300]}")
        return text

    async def snapshot(self) -> str:
        return await self.call("browser_snapshot")

    async def click(self, outline: str, role: str, key: str, within: tuple[str, str] | None = None) -> None:
        ref = find_ref(outline, role, names(key), within)
        if ref is None:
            raise RuntimeError(f"no {role} {names(key)} on the page")
        await self.call("browser_act", ref=ref, action="click")

    async def feed_like(self) -> dict[str, Any]:
        await self.click(await self.snapshot(), "button", "like", within=("article", "@mira.*tomatoes"))
        return {"status": "done"}

    async def login(self) -> dict[str, Any]:
        outline = await self.snapshot()
        if has_heading(outline, "cookie_title"):
            await self.click(outline, "button", "reject_optional")
            await asyncio.sleep(1.0)
        result = json.loads(await self.call("login"))
        if not result.get("ok"):
            return {"status": "held" if result.get("needs") else "failed", "reason": f"login: {result}"}
        for _ in range(12):
            outline = await self.snapshot()
            if has_heading(outline, "approve_title"):
                return {"status": "held", "reason": "the site asks to approve the sign-in on another device"}
            if has_heading(outline, "save_title"):
                await self.click(outline, "button", "not_now")
            elif has_heading(outline, "consent_title"):
                await self.click(outline, "checkbox", "agree_terms")
                await self.click(await self.snapshot(), "button", "continue")
            elif has_heading(outline, "feed_title"):
                return {"status": "done"}
            await asyncio.sleep(1.0)
        return {"status": "failed", "reason": "never reached the feed"}

    async def typed_login(self, password: str) -> dict[str, Any]:
        outline = await self.snapshot()
        user = find_ref(outline, "textbox", names("username"))
        secret = re.search(r'- textbox "[^"]*"[^\n]*\[ref=(\w+)\]', outline.split(f"[ref={user}]", 1)[-1])
        if user is None or secret is None:
            raise RuntimeError("no login form")
        await self.call("browser_act", ref=user, action="type", text="ada")
        await self.call("browser_act", ref=secret.group(1), action="type", text=password)
        await self.click(await self.snapshot(), "button", "log_in")
        await asyncio.sleep(1.5)
        return {"status": "done"}

    async def compose_publish(self) -> dict[str, Any]:
        outline = await self.snapshot()
        ref = find_ref(outline, "textbox", names("post_text"))
        if ref is None:
            raise RuntimeError("no Post text field")
        await self.call("browser_act", ref=ref, action="type", text="Fresh bread on Saturday")
        inbox = json.loads(await self.call("browser_files"))["inbox"]
        photo = next((f for f in inbox if f.get("name") == "photo.png"), None)
        if photo is None:
            return {"status": "failed", "reason": f"photo.png is not in the inbox: {inbox}"}
        add = find_ref(await self.snapshot(), "button", names("add_photo"))
        if add is None:
            raise RuntimeError("no Add photo button")
        await self.call("browser_upload", ref=add, file_id=photo["id"])
        await asyncio.sleep(1.5)
        await self.click(await self.snapshot(), "button", "publish")  # this task has no decoy banner
        await asyncio.sleep(1.0)
        return {"status": "done"}


async def main(config: str, lane: str, type_password: str | None = None) -> int:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    task = lane.split("/", 1)[1]
    server = next(iter(json.loads(Path(config).read_text())["mcpServers"].values()))
    async with httpx2.AsyncClient(headers=server["headers"], timeout=120) as http, \
            Client(streamable_http_client(server["url"], http_client=http), mode="legacy") as client:
        agent = Agent(client, lane)
        try:
            if type_password is not None:
                out = await agent.typed_login(type_password)
            elif task == "feed-like":
                out = await agent.feed_like()
            elif task.startswith("login-"):
                out = await agent.login()
            elif task == "compose-publish":
                out = await agent.compose_publish()
            else:
                out = {"status": "failed", "reason": f"the scripted agent does not know {task}"}
        except RuntimeError as e:
            out = {"status": "failed", "reason": str(e)[:600]}
    print(json.dumps(out))
    return 0 if out["status"] in ("done", "held") else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--lane", required=True)
    ap.add_argument("--type-password")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.config, a.lane, a.type_password)))
