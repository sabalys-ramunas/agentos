"""A grant a freeze revoked says so (QA 2026-11-07 integration 2, finding 3): freezing one lane
revokes a grant that reaches only that lane, and every later call with it answers `frozen` and names
the freeze, on /mcp and on the API, instead of "send the token from node.toml". A wider grant is
kept (QA 2026-11-10 finding 36) and refused on the frozen lane only."""

from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx2
from test_dev_up import dev_node

INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "qa", "version": "0"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@asynccontextmanager
async def owner(node):
    async with httpx2.AsyncClient(base_url=f"http://localhost:{node.port}") as panel:
        code = parse_qs(urlsplit(node.panel_link).fragment)["code"][0]
        assert (await panel.post("/api/session", json={"code": code})).status_code == 200
        panel.headers["X-Agentos-CSRF"] = (await panel.get("/api/status")).json()["csrf"]
        yield panel


async def test_a_grant_revoked_by_a_freeze_answers_frozen_and_names_the_freeze():
    async with dev_node() as node, owner(node) as panel:
        made = await panel.post("/api/grants", json={"subject": "one-ws", "workspaces": ["biz-a"],
                                                     "ceiling": "commit", "ttl": "1d"})
        assert made.status_code == 200, made.text
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{node.port}",
                                      headers={"Authorization": f"Bearer {made.json()['token']}",
                                               **MCP_HEADERS}) as agent, \
                httpx2.AsyncClient(base_url=f"http://127.0.0.1:{node.port}",
                                   headers={"Authorization": f"Bearer {node.agent_token}", **MCP_HEADERS}) as wide:
            assert (await agent.post("/mcp", json=INITIALIZE)).status_code == 200
            froze = await panel.post("/api/freeze", json={"scope": "workspace", "target": "biz-a",
                                                          "reason": "odd post"})
            assert froze.status_code == 200, froze.text
            assert (await wide.post("/mcp", json=INITIALIZE)).status_code == 200  # the dev grant reaches more
            for answer in (await agent.post("/mcp", json=INITIALIZE), await agent.get("/api/lanes")):
                assert answer.status_code == 409, answer.text
                error = answer.json()["error"]
                assert error["code"] == "frozen"
                assert "freeze of workspace biz-a" in error["message"] and "odd post" in error["message"]
                assert "node.toml" not in answer.text and "—" not in answer.text
                assert "www-authenticate" not in answer.headers  # no sign-in hint for the client to follow


async def test_a_grant_revoked_by_hand_still_answers_unauthorized():
    async with dev_node() as node, owner(node) as panel, httpx2.AsyncClient(
            base_url=f"http://127.0.0.1:{node.port}",
            headers={"Authorization": f"Bearer {node.agent_token}", **MCP_HEADERS}) as agent:
        grants = (await panel.get("/api/grants")).json()
        grant = next(g for g in grants if (g.get("subject") or "").endswith("claude-code@dev") and not g.get("revoked"))
        assert (await panel.post(f"/api/grants/{grant['id']}/revoke")).status_code == 200
        answer = await agent.post("/mcp", json=INITIALIZE)
        assert answer.status_code == 401 and answer.json()["error"]["code"] == "unauthorized"


async def test_a_grant_that_reaches_only_a_frozen_scope_is_not_made_during_the_freeze():
    """QA finding 19: a root grant minted during a freeze worked after the unfreeze. One that reaches
    beyond the frozen scope is made: the freeze gate refuses it inside the scope (finding 36 of 2026-11-10)."""
    async with dev_node() as node, owner(node) as panel:
        froze = await panel.post("/api/freeze", json={"scope": "workspace", "target": "biz-a", "reason": "odd post"})
        assert froze.status_code == 200, froze.text
        wanted = {"subject": "late-agent", "workspaces": ["biz-a"], "ceiling": "commit", "ttl": "1d"}
        refused = await panel.post("/api/grants", json=wanted)
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert error["code"] == "frozen" and "workspace biz-a is frozen" in error["message"]
        assert "—" not in error["message"]
        wider = await panel.post("/api/grants", json={**wanted, "workspaces": ["biz-a", "biz-b"]})
        assert wider.status_code == 200, wider.text
        elsewhere = await panel.post("/api/grants", json={**wanted, "workspaces": ["biz-b"]})
        assert elsewhere.status_code == 200, elsewhere.text
        late = [g for g in (await panel.get("/api/grants")).json()
                if (g.get("subject") or "").endswith("late-agent") and not g.get("revoked")]
        assert len(late) == 2  # the refused one was never made
