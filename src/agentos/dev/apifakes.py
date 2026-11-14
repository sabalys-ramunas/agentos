"""In-memory stand-ins for every service the API uses (tests and `python -m agentos.dev.apidemo`).

They follow agentos.api.services exactly, so they double as executable documentation of
the shapes the API expects.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from agentos.core.errors import InvalidRequest, LaneExists, LaneNotFound, NotFound
from agentos.core.models import (
    LaneInfo,
    LaneKind,
    LaneSpec,
    LaneState,
    Principal,
    ProxyConfig,
    ViewerInfo,
    utcnow,
)

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201e5a7d8e30000000049454e44ae426082"
)


class FakeLanes:
    def __init__(self, viewer_port: int = 6110):
        self._lanes: dict[str, LaneInfo] = {}
        self._hints: dict[str, dict] = {}
        self.viewer_port = viewer_port

    def add(self, lane_id: str, *, kind: LaneKind = LaneKind.BROWSER, state: LaneState = LaneState.COLD,
            proxy: str | None = None, signal: str = "ok", logged_in: str = "ok") -> LaneInfo:
        info = LaneInfo(spec=LaneSpec(id=lane_id, kind=kind, proxy_label=proxy), state=state,
                        exit_ip="203.0.113.10" if proxy else None)
        if state in (LaneState.WARM, LaneState.ACTIVE):
            # viewer_port here is the live view's upstream: the lane's x11vnc (no websockify any more)
            info.viewer = ViewerInfo(display=":110", vnc_port=self.viewer_port, viewer_port=None)
        self._lanes[lane_id] = info
        self._hints[lane_id] = {"signal": signal, "logged_in_hint": logged_in}
        return info

    def hints(self, lane_id: str) -> dict:
        return self._hints.get(lane_id, {})

    def list(self) -> list[LaneInfo]:
        return list(self._lanes.values())

    def get(self, lane_id: str) -> LaneInfo:
        if lane_id not in self._lanes:
            raise LaneNotFound(f"No lane {lane_id}.")
        return self._lanes[lane_id]

    async def create(self, spec: LaneSpec) -> LaneInfo:
        if spec.id in self._lanes:
            raise LaneExists(f"Lane {spec.id} already exists.")
        info = LaneInfo(spec=spec)
        self._lanes[spec.id] = info
        self._hints[spec.id] = {"signal": "unknown", "logged_in_hint": "needs_login"}
        return info

    async def open(self, lane_id: str) -> LaneInfo:
        info = self.get(lane_id)
        info.state = LaneState.WARM
        info.last_used = utcnow()
        info.viewer = ViewerInfo(display=":110", vnc_port=self.viewer_port, viewer_port=None)
        return info

    async def close(self, lane_id: str) -> LaneInfo:
        info = self.get(lane_id)
        info.state, info.viewer = LaneState.COLD, None
        return info

    def view(self, lane_id: str) -> ViewerInfo | None:
        return self.get(lane_id).viewer

    @asynccontextmanager
    async def using(self, lane_id: str):
        info = self.get(lane_id)
        yield {"page_for": info.spec.id}


class FakeInteraction:
    async def screenshot(self, page: Any) -> bytes:
        return PNG_1PX


class FakeApprovals:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.standing: dict[str, dict] = {}
        self._ids = itertools.count(1)

    def add(self, lane: str, *, effect: str = "commit", summary: str = "Post 1 photo", text: str = "Hello",
            state: str = "pending") -> dict:
        aid = f"ap_{next(self._ids)}"
        now = utcnow()
        item = {
            "id": aid, "kind": "action", "state": state, "effect": effect, "lane": lane, "summary": summary,
            "content_hash": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
            "preview": {"text": text, "image": f"/api/approvals/{aid}/preview.png"},
            "requested_by": {"kind": "agent", "id": "claude-code", "thread": "T-91"},
            "created_at": now.isoformat(), "expires_at": (now + timedelta(hours=24)).isoformat(),
            "decided_by": None, "note": None,
        }
        self.items[aid] = item
        return item

    def ask(self, question: str, *, options: list[str] | None = None, asked_by: str = "claude-code") -> dict:
        """A human_ask question: an approval of kind "question", answered with text."""
        aid = f"ap_{next(self._ids)}"
        now = utcnow()
        item = {
            "id": aid, "kind": "question", "state": "pending", "effect": None, "lane": None, "summary": question,
            "options": options, "answer": None, "content_hash": None, "preview": None,
            "requested_by": {"kind": "agent", "id": asked_by, "thread": None},
            "created_at": now.isoformat(), "expires_at": (now + timedelta(hours=24)).isoformat(),
            "decided_by": None, "decided_at": None, "note": None,
        }
        self.items[aid] = item
        return item

    def list(self, *, state: str | None = None, limit: int = 50, kind: str | None = None) -> list[dict]:
        return [a for a in self.items.values()
                if (state is None or a["state"] == state) and (kind is None or a["kind"] == kind)][:limit]

    def get(self, approval_id: str) -> dict:
        if approval_id not in self.items:
            raise NotFound(f"No approval {approval_id}.")
        return self.items[approval_id]

    def decide(self, approval_id: str, *, decision: str, principal: Principal, note: str | None = None,
               answer: str | None = None) -> dict:
        item = self.get(approval_id)
        if item["state"] != "pending":
            from agentos.api.errors import ApiError
            raise ApiError("conflict", f"Approval {approval_id} is already {item['state']}.")
        if item["kind"] == "question" and decision == "approve":
            value = (answer if answer is not None else note or "").strip()
            if not value:
                raise InvalidRequest("answering a question needs an answer")
            if item["options"] and value not in item["options"]:
                raise InvalidRequest(f"the answer must be one of: {', '.join(item['options'])}")
            item["answer"] = value
        item.update(state="approved" if decision == "approve" else "denied", decided_by=principal.id, note=note,
                    decided_at=utcnow().isoformat())
        return item

    def preview_image(self, approval_id: str) -> bytes | None:
        return PNG_1PX if approval_id in self.items else None

    def list_standing(self) -> list[dict]:
        return list(self.standing.values())

    def create_standing(self, *, lane: str, effect: str, ttl_seconds: int, principal: Principal,
                        note: str | None) -> dict:
        sid = f"st_{len(self.standing) + 1}"
        item = {"id": sid, "lane": lane, "effect": effect, "note": note,
                "expires_at": (utcnow() + timedelta(seconds=ttl_seconds)).isoformat(), "created_by": principal.id}
        self.standing[sid] = item
        return item

    def revoke_standing(self, standing_id: str) -> dict:
        if standing_id not in self.standing:
            raise NotFound(f"No standing approval {standing_id}.")
        return {"ok": True, "revoked": self.standing.pop(standing_id)}


class FakeTraces:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def add(self, **fields: Any) -> dict:
        ev = {"id": f"t{len(self.events) + 1}", "at": utcnow().isoformat(), "kind": "tool_call",
              "tool": "browser_navigate", "lane": None, "principal": "claude-code", "effect": "read",
              "status": "ok", "summary": "", "cost": {"tokens": 0, "usd": 0.0}, "run_id": None, **fields}
        self.events.append(ev)
        return ev

    def query(self, *, lane=None, workspace=None, kind=None, before=None, limit=100) -> list[dict]:
        out = [e for e in reversed(self.events)
               if (lane is None or e["lane"] == lane)
               and (workspace is None or str(e["lane"] or "").startswith(workspace + "/"))
               and (kind is None or e["kind"] == kind)
               and (before is None or e["at"] < before)]
        return out[:limit]

    def cost_today(self) -> dict:
        return {"tokens": sum(e["cost"]["tokens"] for e in self.events),
                "usd": round(sum(e["cost"]["usd"] for e in self.events), 4)}


class FakeRunner:
    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}
        self.schedules: dict[str, dict] = {}

    def add_run(self, flow: str, status: str, **extra: Any) -> dict:
        rid = f"run_{len(self.runs) + 1}"
        run = {"id": rid, "flow": flow, "status": status, "lane": extra.pop("lane", None),
               "started_at": utcnow().isoformat(), "finished_at": None, "cost": {"tokens": 0, "usd": 0.0},
               "proof": extra.pop("proof", None), "steps": extra.pop("steps", []), **extra}
        self.runs[rid] = run
        return run

    def list_runs(self, *, status=None, limit=50) -> list[dict]:
        return [r for r in self.runs.values() if status is None or r["status"] == status][:limit]

    def get_run(self, run_id: str) -> dict:
        if run_id not in self.runs:
            raise NotFound(f"No run {run_id}.")
        return self.runs[run_id]

    def list_schedules(self) -> list[dict]:
        return list(self.schedules.values())

    def get_schedule(self, schedule_id: str) -> dict:
        if schedule_id not in self.schedules:
            raise NotFound(f"No schedule {schedule_id}.")
        return self.schedules[schedule_id]

    def create_schedule(self, *, flow, cron, watch, params, deadman_minutes, principal) -> dict:
        sid = f"sc_{len(self.schedules) + 1}"
        fire = utcnow() + timedelta(hours=1)
        item = {"id": sid, "flow": flow, "kind": "cron" if cron else "watch", "lane": "biz-a/instagram",
                "cron": cron, "watch": watch, "params": params, "deadman_minutes": deadman_minutes,
                "enabled": True, "next_run": fire.isoformat(), "next_fire_at": fire.isoformat(),
                "last_run": None, "grant_id": "owner", "created_by": principal.id}
        self.schedules[sid] = item
        return item

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> dict:
        item = self.get_schedule(schedule_id)
        item.update(enabled=enabled, next_fire_at=item["next_run"] if enabled else None)
        return item

    def delete_schedule(self, schedule_id: str) -> None:
        if self.schedules.pop(schedule_id, None) is None:
            raise NotFound(f"No schedule {schedule_id}.")


class FakeFlows:
    def __init__(self, runner: FakeRunner) -> None:
        self.runner = runner
        self.flows = {
            "biz-a.instagram.post": {
                "id": "biz-a.instagram.post", "lane": "biz-a/instagram", "version": 3, "effect": "commit",
                "description": "Post one photo with a caption.",
                "params": {"type": "object", "required": ["caption"], "properties": {
                    "caption": {"type": "string", "maxLength": 2200},
                    "image": {"type": "string", "format": "uri"}}},
                "last_run": None,
            }
        }

    def list(self) -> list[dict]:
        return list(self.flows.values())

    async def run(self, flow_id: str, *, params: dict, principal: Principal) -> dict:
        if flow_id not in self.flows:
            raise NotFound(f"No flow {flow_id}.")
        await asyncio.sleep(0)
        return self.runner.add_run(flow_id, "queued", lane=self.flows[flow_id]["lane"], params=params)


class FakeBrains:
    def __init__(self) -> None:
        self.brains = [
            {"name": "claude-max", "kind": "subscription", "provider": "anthropic", "state": "ready",
             "seats": {"in_use": 1, "total": 2}, "window_used_pct": 40},
            {"name": "ollama-local", "kind": "local", "provider": "ollama", "state": "ready",
             "seats": {"in_use": 0, "total": 1}, "window_used_pct": None},
            {"name": "anthropic-api", "kind": "metered", "provider": "anthropic", "state": "ready",
             "seats": {"in_use": 0, "total": 4}, "window_used_pct": None},
        ]
        self.tasks = {"bt_1": {"id": "bt_1", "brain": "claude-max", "status": "running",
                               "transcript_tail": ["Drafting captions"]}}

    def list(self) -> list[dict]:
        return self.brains

    def set_order(self, order: list[str]) -> None:
        by = {b["name"]: b for b in self.brains}
        if set(order) != set(by):
            from agentos.core.errors import InvalidRequest
            raise InvalidRequest("order must name every brain exactly once")
        self.brains = [by[n] for n in order]

    def list_tasks(self, *, limit=50) -> list[dict]:
        return list(self.tasks.values())[:limit]

    def get_task(self, task_id: str) -> dict:
        if task_id not in self.tasks:
            raise NotFound(f"No task {task_id}.")
        return self.tasks[task_id]


class FakeVault:
    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}
        self.secrets: dict[str, str] = {}  # what a real vault seals; never returned

    def list(self) -> list[dict]:
        # A careless vault that leaks extra fields: the API must whitelist.
        return [{**e, "secret": self.secrets[e["id"]], "username": "owner@example.com"} for e in self.entries.values()]

    def create(self, *, name, kind, domain, lanes, username, secret, principal, totp=None) -> dict:
        vid = f"v_{len(self.entries) + 1}"
        entry = {"id": vid, "name": name, "kind": kind, "domain": domain, "lanes": lanes,
                 "created_at": utcnow().isoformat(), "last_used": None}
        self.entries[vid] = entry
        self.secrets[vid] = secret
        return {**entry, "secret": secret}

    def delete(self, entry_id: str) -> None:
        if self.entries.pop(entry_id, None) is None:
            raise NotFound(f"No vault entry {entry_id}.")


class FakePolicy:
    def __init__(self, panel_token: str = "panel-token-123") -> None:
        self.panel_token = panel_token
        self.grants: dict[str, dict] = {}

    def authenticate_panel(self, token: str) -> Principal | None:
        if secrets.compare_digest(token, self.panel_token):
            return Principal(kind="human", id="owner-phone")
        return None

    def list_grants(self) -> list[dict]:
        return list(self.grants.values())

    def issue_grant(self, *, subject, workspaces, lanes, ceiling, specials, budget, ttl, principal) -> dict:
        gid = f"g_{len(self.grants) + 1}"
        token = "agt_" + secrets.token_urlsafe(16)
        grant = {"id": gid, "subject": subject, "workspaces": workspaces, "lanes": lanes, "ceiling": ceiling,
                 "specials": specials, "budget": budget, "ttl": ttl, "revoked": False,
                 "issued_at": utcnow().isoformat(), "token": token}
        self.grants[gid] = grant
        return grant

    def revoke_grant(self, grant_id: str) -> dict:
        if grant_id not in self.grants:
            raise NotFound(f"No grant {grant_id}.")
        self.grants[grant_id]["revoked"] = True
        return self.grants[grant_id]


class FakeProxies:
    def __init__(self) -> None:
        self.items: dict[str, ProxyConfig] = {
            "lt1": ProxyConfig(label="lt1", host="203.0.113.10", port=12323, username="u", password="p"),
        }

    def list(self) -> list[ProxyConfig]:
        return list(self.items.values())

    def add(self, proxy: ProxyConfig) -> ProxyConfig:
        self.items[proxy.label] = proxy
        return proxy


class FakeNotifier:
    def __init__(self) -> None:
        self.settings: dict = {"channels": [{"kind": "ntfy", "topic_hint": "agentos-...", "enabled": True}],
                               "quiet_hours": None, "webhook_secret": "s3cret"}
        self.tested = 0

    def get_settings(self) -> dict:
        return self.settings

    def put_settings(self, settings: dict) -> dict:
        self.settings = {**self.settings, **settings}
        return self.settings

    def test(self) -> dict:
        self.tested += 1
        return {"ok": True, "detail": "Sent a test notification to 1 channel."}


class FakeTerminal:
    """Echoes input back, upper-cased, and records resizes."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.resizes: list[tuple[int, int]] = []
        self.closed = False

    async def read(self) -> str | None:
        return await self.queue.get()

    async def write(self, data: str) -> None:
        if data == "exit\r":
            await self.queue.put(None)
        else:
            await self.queue.put(data.upper())

    async def resize(self, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))
        await self.queue.put(f"[{cols}x{rows}]")

    async def close(self) -> None:
        self.closed = True


class FakeEnvs:
    def __init__(self) -> None:
        self.sessions: list[FakeTerminal] = []

    async def open_terminal(self, lane_id: str, *, principal: Principal, cols: int = 80, rows: int = 24):
        term = FakeTerminal()
        await term.queue.put(f"$ connected to {lane_id} ({cols}x{rows})\r\n")
        self.sessions.append(term)
        return term


def demo_runtime(viewer_port: int = 6110):
    """A runtime with every fake registered and a little seed data."""
    from agentos.core.runtime import Runtime

    lanes = FakeLanes(viewer_port=viewer_port)
    for ws in ("biz-a", "biz-b"):
        for name in ("instagram", "facebook", "reddit", "medium", "blog", "linkedin"):
            lanes.add(f"{ws}/{name}", proxy="lt1")
    lanes.add("biz-b/reddit", proxy="lt1", signal="captcha")
    lanes.add("biz-a/instagram", proxy="lt1", state=LaneState.WARM)
    lanes.add("personal/shell", kind=LaneKind.TERMINAL)
    approvals = FakeApprovals()
    approvals.add("biz-a/instagram", summary="Post 1 photo with caption to instagram.com as biz-a",
                  text="Fresh rye this morning.")
    traces = FakeTraces()
    traces.add(lane="biz-a/instagram", summary="Opened instagram.com", cost={"tokens": 1200, "usd": 0.01})
    runner = FakeRunner()
    runner.add_run("biz-a.instagram.post", "succeeded_verified", lane="biz-a/instagram",
                   proof={"url": "https://instagram.com/p/abc", "id": "abc", "screenshot": None})
    return Runtime(lanes=lanes, interaction=FakeInteraction(), approvals=approvals, traces=traces, runner=runner,
                   flows=FakeFlows(runner), brains=FakeBrains(), vault=FakeVault(), policy=FakePolicy(),
                   proxies=FakeProxies(), notifier=FakeNotifier(), envs=FakeEnvs())
