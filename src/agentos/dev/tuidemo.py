"""A fake node for `agentos dash --demo` and the dashboard tests.

FakeNode holds realistic data (four companies with six lanes each plus five personal
lanes, 29 in all; approvals, a question, a lane under human control, a lane that wants
control back, a failed run, a node that is down) behind a Starlette app speaking the
panel API in docs/API.md, live events included. `transport()` serves it in-process
through a streaming ASGI transport, so nothing listens on a socket. To try the real
network path, run `uvicorn agentos.dev.tuidemo:create_app --factory --port 7499` and
`agentos dash --url http://127.0.0.1:7499 --token demo`.

Knobs for tests: `reachable`, `unavailable` (resource names answering 503), `events_enabled`,
and `requests` (every call with its JSON body).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import struct
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from agentos.core import clock
from agentos.core.signals import TUI_NEED

DEMO_TOKEN = "demo"
TZ = "Europe/Vilnius"

COMPANIES = [
    ("biz-a", "Biz A", "#6c8cff"),
    ("biz-b", "Biz B", "#e0a050"),
    ("biz-c", "Biz C", "#5fb8a8"),
    ("biz-d", "Biz D", "#c27bd6"),
]
SURFACES = ["instagram", "facebook", "reddit", "medium", "blog", "linkedin"]
PROXIES = {
    "biz-a": ("lt1", "203.0.113.10", 12323),
    "biz-b": ("lt2", "203.0.113.11", 12323),
    "biz-c": ("de1", "203.0.113.40", 12323),
    "biz-d": ("de2", "203.0.113.77", 12323),
    "personal": ("home", "198.51.100.2", 3128),
}


@dataclass
class Call:
    method: str
    path: str
    params: dict
    body: Any


def tiny_png(width: int = 48, height: int = 30, rgb: tuple[int, int, int] = (40, 44, 52)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    row = b"\x00" + bytes(rgb) * width
    raw = zlib.compress(row * height)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class FakeNode:
    def __init__(self, now: datetime | None = None, *, token: str | None = DEMO_TOKEN):
        self.now = now or datetime.now(UTC).replace(microsecond=0)
        self.token = token
        self.reachable = True
        self.unavailable: set[str] = set()
        self.events_enabled = True
        self.requests: list[Call] = []
        self._subscribers: list[asyncio.Queue] = []
        self._seq = 0
        self._ids = 900
        self._build()
        self.app = self._make_app()

    # ---- test helpers ----
    def calls(self, method: str, path: str) -> list[Any]:
        """Bodies of every request with this method and path."""
        return [c.body for c in self.requests if c.method == method and c.path == path]

    def writes(self) -> list[Call]:
        return [c for c in self.requests if c.method != "GET"]

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def emit(self, event: str, data: Any) -> None:
        self._seq += 1
        frame = f"id: {self._seq}\nevent: {event}\ndata: {json.dumps(data)}\n\n".encode()
        for q in list(self._subscribers):
            q.put_nowait(frame)

    def go_down(self) -> None:
        self.reachable = False
        for q in list(self._subscribers):
            q.put_nowait(None)

    def come_back(self) -> None:
        self.reachable = True

    def transport(self) -> httpx.AsyncBaseTransport:
        return FakeTransport(self)

    def lane(self, lane_id: str) -> dict:
        return self.lanes[lane_id]

    def next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}_{self._ids}"

    # ---- fixture data ----
    def _ago(self, **kw) -> str:
        return clock.iso_or_none(self.now - timedelta(**kw), z=True)

    def _in(self, **kw) -> str:
        return clock.iso_or_none(self.now + timedelta(**kw), z=True)

    def _build(self) -> None:
        self.workspaces: list[dict] = []
        self.lanes: dict[str, dict] = {}
        for ws, name, color in COMPANIES:
            self.workspaces.append({"id": ws, "name": name, "color": color})
            for i, surface in enumerate(SURFACES):
                self._add_lane(ws, surface, last_used=self._ago(hours=3 + i * 5))
        self.workspaces.append({"id": "personal", "name": "Personal", "color": "#8a93a6"})
        for surface in ("reddit", "linkedin", "medium"):
            self._add_lane("personal", surface, last_used=self._ago(days=1, hours=2))
        self._add_lane("personal", "shell", kind="terminal", engine=None, preset="workbench",
                       node="node-2", proxy=False, last_used=self._ago(hours=20))
        self._add_lane("personal", "notes-sync", kind="service", engine=None, preset="service",
                       node="node-2", proxy=False, last_used=self._ago(hours=9))

        # an agent's environment: its terminal and browser lanes in workspace env, named by the title elsewhere
        self.workspaces.append({"id": "env", "name": "env", "color": "#5a616e"})
        self._add_lane("env", "esrkyfqerme", kind="terminal", engine=None, preset="workbench", proxy=False,
                       last_used=self._ago(minutes=3))
        self._add_lane("env", "esrkyfqerme-browser", proxy=False, last_used=self._ago(minutes=3))
        self.environments = [
            {"id": "esrkyfqerme", "title": "qa-env-2026-11-15", "state": "working",
             "repo": "github.com/sabalys-ramunas/agentos", "branch": "fix/history-words",
             "created_at": self._ago(minutes=40), "closed_at": None, "expires_at": self._in(minutes=80),
             "minutes_left": 80, "cost_eur": 0.011,
             "lanes": {"terminal": "env/esrkyfqerme", "browser": "env/esrkyfqerme-browser"}},
            {"id": "p2lcxk", "title": "pricing page copy", "state": "closed",
             "repo": "github.com/sabalys-ramunas/site", "branch": "copy/pricing", "created_at": self._ago(hours=5),
             "closed_at": self._ago(hours=4), "expires_at": self._ago(hours=1), "minutes_left": None,
             "cost_eur": 0.008, "lanes": {"terminal": "env/p2lcxk", "browser": "env/p2lcxk-browser"}},
        ]
        self.environment_details = {"esrkyfqerme": {"git": {"ahead": 2, "pushed": [{"branch": "fix/history-words"}]},
                                                    "proofs": [{"id": "pf1"}, {"id": "pf2"}, {"id": "pf3"}]}}

        L = self.lanes
        # biz-a: all ok, two warm, posts scheduled later today
        L["biz-a/instagram"].update(state="warm", last_used=self._ago(minutes=2))
        L["biz-a/linkedin"].update(state="active", last_used=self._ago(seconds=12))
        # biz-b: a captcha, and a lane the owner is driving
        L["biz-b/instagram"].update(state="warm", signal="captcha", last_used=self._ago(minutes=4))
        L["biz-b/facebook"].update(state="warm", last_used=self._ago(minutes=1),
                                   control=self._control("human", since=self._ago(minutes=3),
                                                         last_input=self._ago(seconds=40)))
        # biz-c: the owner took over and the agent asks for it back
        L["biz-c/linkedin"].update(state="warm", last_used=self._ago(minutes=6), control=self._control(
            "human", since=self._ago(minutes=11), last_input=self._ago(minutes=7),
            takeback=("Finish the 14:00 post, the draft is ready", "claude-code")))
        # biz-d: quarantined after a failed self-check, and a logged-out account
        L["biz-d/blog"].update(state="quarantined", signal="unknown",
                               quarantine_reason="exit IP 203.0.113.9 does not match proxy de2 (203.0.113.77)")
        L["biz-d/reddit"].update(signal="logged_out", logged_in_hint="needs_login")
        for lane in L.values():
            if lane["state"] in ("warm", "active"):
                lane["viewer"] = {"display": ":110", "url": f"/view/{lane['id']}"}

        self.nodes = [
            {"id": "node-1", "state": "up", "degraded": False, "capabilities": ["browser", "terminal"],
             "cpu": 23.5, "mem": {"used_mb": 2240, "total_mb": 3900}, "warm_lanes": 5, "max_warm_lanes": 6,
             "last_seen": self._ago(seconds=2), "timezone": TZ},
            {"id": "node-2", "state": "down", "degraded": True, "capabilities": ["terminal", "service"],
             "cpu": None, "mem": None, "warm_lanes": 0, "max_warm_lanes": 2,
             "last_seen": self._ago(minutes=42), "timezone": TZ},
        ]

        self.approvals: dict[str, dict] = {}
        for a in [
            {"id": "ap_7f3", "effect": "commit", "lane": "biz-a/instagram",
                 "summary": "Post 1 photo with caption to instagram.com as biz-a",
                 "text": "Autumn menu is here. Pumpkin flat white is back until December. #coffee #vilnius",
                 "image": True, "by": ("agent", "claude-code", "T-91"), "created": {"minutes": 6}},
            {"id": "ap_81c", "effect": "commit", "lane": "biz-b/linkedin",
                 "summary": "Publish 1 article (612 words) to linkedin.com as biz-b",
                 "text": "Why we moved our tile production back to Kaunas\n\nThree years ago we ...",
                 "image": False, "by": ("agent", "codex", "T-12"), "created": {"minutes": 14}},
            {"id": "ap_92d", "effect": "irreversible", "lane": "biz-d/facebook",
                 "summary": "Delete 3 comments on facebook.com as biz-d (spam on the Oct 30 post)",
                 "text": "1. \"Get 10k followers fast...\"\n2. \"Crypto giveaway...\"\n3. \"DM me for promo...\"",
                 "image": False, "by": ("flow", "biz-d.facebook.moderate", None), "created": {"minutes": 25}},
            {"id": "ap_a04", "effect": "pay", "lane": "personal/medium",
                 "summary": "Pay 5.00 USD for a Medium membership as personal",
                 "text": "Medium membership, monthly, 5.00 USD, card ending 4242",
                 "image": False, "by": ("agent", "claude-app", "T-3"), "created": {"hours": 2}},
        ]:
            self.approvals[a["id"]] = {
                "id": a["id"], "state": "pending", "effect": a["effect"], "lane": a["lane"],
                "summary": a["summary"], "content_hash": "sha256:" + hashlib.sha256(a["text"].encode()).hexdigest()[:16],
                "preview": {"text": a["text"],
                            "image": f"/api/approvals/{a['id']}/preview.png" if a["image"] else None},
                "requested_by": {"kind": a["by"][0], "id": a["by"][1], "thread": a["by"][2]},
                "created_at": self._ago(**a["created"]), "expires_at": self._in(minutes=50),
            }
        self.questions: dict[str, dict] = {
            "q_51": {"id": "q_51", "state": "open", "lane": "biz-c/blog",
                     "question": "Two cover images fit the post. Use the bright one or the dark one?",
                     "options": ["bright", "dark"],
                     "asked_by": {"kind": "agent", "id": "codex", "thread": "T-14"},
                     "created_at": self._ago(minutes=9)},
        }

        self.flows = {f["id"]: f for f in [
            self._flow("biz-a.instagram.post", {"caption": "string", "image": "path"}, "commit"),
            self._flow("biz-a.linkedin.post", {"text": "string"}, "commit"),
            self._flow("biz-a.reddit.reply", {"thread_url": "url", "text": "string"}, "commit"),
            self._flow("biz-a.blog.publish", {"draft_id": "string"}, "commit"),
            self._flow("biz-b.instagram.post", {"caption": "string", "image": "path"}, "commit"),
            self._flow("biz-b.linkedin.post", {"text": "string"}, "commit"),
            self._flow("biz-c.medium.post", {"draft_id": "string"}, "commit"),
            self._flow("biz-c.linkedin.post", {"text": "string"}, "commit"),
            self._flow("biz-d.facebook.moderate", {}, "irreversible"),
            self._flow("personal.reddit.digest", {"subreddits": "list"}, "read"),
        ]}

        self.runs: dict[str, dict] = {}
        for r in [
            ("r_120", "biz-a.linkedin.post", "running", {"minutes": 1}, None, None, None),
            ("r_119", "biz-b.linkedin.post", "waiting_approval", {"minutes": 14}, None, None, None),
            ("r_118", "biz-c.medium.post", "failed", {"minutes": 38}, {"minutes": 36},
             None, "Publish button never became enabled. Readback: no new story on the profile."),
            ("r_117", "biz-a.instagram.post", "succeeded_verified", {"hours": 2}, {"hours": 2, "seconds": -75},
             {"url": "https://www.instagram.com/p/C9xYz1/", "id": "C9xYz1", "screenshot": "shots/r_117.png"},
             None),
            ("r_116", "biz-a.reddit.reply", "succeeded_unverified", {"hours": 3}, {"hours": 3, "seconds": -40},
             None, "Reply sent, permalink not visible yet. Re-check scheduled."),
            ("r_115", "biz-a.blog.publish", "partial", {"hours": 5}, {"hours": 5, "seconds": -90},
             {"url": "https://biz-a.example/blog/autumn-menu", "id": None, "screenshot": None},
             "Post is live, the LinkedIn cross-post did not go out."),
            ("r_114", "biz-b.instagram.post", "succeeded_verified", {"hours": 7}, {"hours": 7, "seconds": -60},
             {"url": "https://www.instagram.com/p/C9wQ02/", "id": "C9wQ02", "screenshot": None}, None),
            ("r_113", "personal.reddit.digest", "cancelled", {"hours": 9}, {"hours": 9, "seconds": -5},
             None, "node-2 went down"),
            ("r_112", "biz-c.linkedin.post", "succeeded_verified", {"hours": 23}, {"hours": 23, "seconds": -50},
             {"url": "https://www.linkedin.com/feed/update/urn:li:activity:7254/", "id": "7254",
              "screenshot": None}, None),
        ]:
            rid, flow, status, started, finished, proof, error = r
            lane = self.flows[flow]["lane"]
            self.runs[rid] = {"id": rid, "flow": flow, "lane": lane, "status": status,
                              "started_at": self._ago(**started),
                              "finished_at": self._ago(**finished) if finished else None,
                              "proof": proof, "error": error, "trigger": "schedule",
                              "cost": {"tokens": 0, "usd": 0.0} if status != "failed" else
                              {"tokens": 18200, "usd": 0.09},
                              "steps": [{"at": self._ago(**started), "tool": "browser_navigate",
                                         "status": "ok", "summary": "opened the composer"},
                                        {"at": self._ago(**started), "tool": "browser_act",
                                         "status": "ok" if status != "failed" else "error",
                                         "summary": "clicked Publish" if status != "failed"
                                         else "Publish stayed disabled"}]}
        self.flows["biz-c.medium.post"]["last_run"] = {"id": "r_118", "status": "failed"}

        self.schedules: dict[str, dict] = {s["id"]: s for s in [
            self._schedule("s_1", "biz-a.instagram.post", "0 13 * * *", {"hours": 3}),
            self._schedule("s_2", "biz-a.linkedin.post", "0 15 * * 1-5", {"hours": 5}),
            self._schedule("s_3", "biz-b.instagram.post", "30 9 * * *", {"hours": 23, "minutes": 30}),
            self._schedule("s_4", "biz-c.medium.post", "0 9 * * 2", {"days": 5, "hours": 23}),
            self._schedule("s_5", "biz-d.facebook.moderate", None, {"minutes": 12},
                           watch={"url": "https://www.facebook.com/bizd/posts", "every_minutes": 30}),
            self._schedule("s_6", "personal.reddit.digest", "0 8 * * *", None, enabled=False),
        ]}

        self.brains = [
            {"name": "claude-max", "kind": "subscription", "provider": "anthropic", "state": "ready",
             "seats": {"in_use": 1, "total": 2}, "window_used_pct": 40},
            {"name": "codex-pro", "kind": "subscription", "provider": "openai", "state": "cooling_down",
             "seats": {"in_use": 0, "total": 1}, "window_used_pct": 96},
            {"name": "ollama-local", "kind": "local", "provider": "ollama", "state": "ready",
             "seats": {"in_use": 0, "total": 1}, "window_used_pct": None},
            {"name": "anthropic-api", "kind": "metered", "provider": "anthropic", "state": "ready",
             "seats": {"in_use": 0, "total": 4}, "window_used_pct": None},
        ]
        self.tasks: dict[str, dict] = {t["id"]: t for t in [
            {"id": "t_31", "brain": "claude-max", "lane": "biz-a/linkedin", "status": "running",
             "started_at": self._ago(minutes=1), "summary": "Draft and post the Thursday update",
             "tail": ["> reading the last 5 posts for tone", "> drafting 3 options",
                      "> picked option 2 (shortest)", "> browser_act click Start a post"]},
            {"id": "t_30", "brain": "codex-pro", "lane": "biz-c/blog", "status": "waiting",
             "started_at": self._ago(minutes=12), "summary": "Pick a cover image and publish",
             "tail": ["> two candidates fit the 16:9 slot", "> asked the owner (q_51)"]},
            {"id": "t_29", "brain": "claude-max", "lane": "personal/shell", "status": "failed",
             "started_at": self._ago(minutes=50), "finished_at": self._ago(minutes=42),
             "summary": "Update the notes index", "tail": ["> git pull", "! node-2 stopped answering"]},
            {"id": "t_28", "brain": "claude-max", "lane": "biz-a/instagram", "status": "succeeded_verified",
             "started_at": self._ago(hours=2), "finished_at": self._ago(hours=2, seconds=-75),
             "summary": "Post the autumn menu photo", "tail": ["> posted", "> readback ok: C9xYz1"]},
        ]}

        self.activity: list[dict] = []
        trace = [
            ({"seconds": 12}, "tool", "browser_act", "biz-a/linkedin", "claude-code", "draft", "ok",
             "clicked Start a post", 0.004, "r_120"),
            ({"minutes": 1}, "tool", "browser_navigate", "biz-a/linkedin", "claude-code", "read", "ok",
             "opened linkedin.com/company/biz-a", 0.002, "r_120"),
            ({"minutes": 3}, "control", "lane_take", "biz-b/facebook", "owner", None, "ok",
             "owner took control", None, None),
            ({"minutes": 4}, "tool", "browser_navigate", "biz-b/instagram", "claude-code", "read", "ok",
             "instagram.com shows a challenge, signal captcha", 0.001, None),
            ({"minutes": 6}, "approval", "approval_request", "biz-a/instagram", "claude-code", "commit",
             "approval_pending", "Post 1 photo with caption to instagram.com as biz-a", None, None),
            ({"minutes": 7}, "control", "browser_request_control", "biz-c/linkedin", "claude-code", None, "ok",
             "asked for control back: Finish the 14:00 post", None, None),
            ({"minutes": 9}, "question", "ask_owner", "biz-c/blog", "codex", None, "approval_pending",
             "asked which cover image to use", None, None),
            ({"minutes": 14}, "tool", "flow_run", "biz-b/linkedin", "codex", "commit", "approval_pending",
             "Publish 1 article (612 words) to linkedin.com as biz-b", 0.03, "r_119"),
            ({"minutes": 36}, "run", "flow_run", "biz-c/medium", "flow", "commit", "failed",
             "Publish button never became enabled", 0.09, "r_118"),
            ({"minutes": 37}, "tool", "browser_act", "biz-c/medium", "flow", "commit", "error",
             "click Publish: the button stayed disabled", None, "r_118"),
            ({"minutes": 42}, "node", "node_health", None, "node-2", None, "error",
             "node-2 stopped answering", None, None),
            ({"minutes": 44}, "tool", "browser_act", "biz-c/linkedin", "claude-code", "commit",
             "human_in_control", "refused: a human has control of this lane", None, None),
            ({"hours": 1}, "tool", "browser_open", "biz-d/blog", "owner", "read", "error",
             "self-check failed, lane quarantined", None, None),
            ({"hours": 2}, "run", "flow_run", "biz-a/instagram", "flow", "commit", "succeeded_verified",
             "posted 1 photo, readback found C9xYz1", 0.03, "r_117"),
            ({"hours": 2, "minutes": 1}, "tool", "browser_act", "biz-a/instagram", "flow", "commit", "ok",
             "clicked Share", None, "r_117"),
            ({"hours": 3}, "run", "flow_run", "biz-a/reddit", "flow", "commit", "succeeded_unverified",
             "reply sent, permalink not visible yet", 0.02, "r_116"),
            ({"hours": 4}, "tool", "browser_act", "biz-d/facebook", "claude-code", "irreversible", "denied",
             "delete comment: outside this grant's ceiling", None, None),
            ({"hours": 5}, "run", "flow_run", "biz-a/blog", "flow", "commit", "partial",
             "post live, LinkedIn cross-post missing", 0.05, "r_115"),
            ({"hours": 7}, "run", "flow_run", "biz-b/instagram", "flow", "commit", "succeeded_verified",
             "posted 1 photo, readback found C9wQ02", 0.03, "r_114"),
            ({"hours": 8}, "tool", "browser_snapshot", "personal/reddit", "claude-app", "read", "ok",
             "read r/selfhosted front page", 0.01, None),
            ({"hours": 9}, "run", "flow_run", "personal/reddit", "flow", "read", "cancelled",
             "digest cancelled, node-2 went down", None, "r_113"),
            ({"hours": 11}, "tool", "browser_navigate", "biz-b/reddit", "claude-code", "read", "ok",
             "opened r/tiles", 0.001, None),
            ({"hours": 13}, "tool", "browser_act", "biz-c/facebook", "claude-code", "draft", "ok",
             "saved a draft reply", 0.004, None),
            ({"hours": 23}, "run", "flow_run", "biz-c/linkedin", "flow", "commit", "succeeded_verified",
             "posted the weekly update, readback found 7254", 0.04, "r_112"),
        ]
        for i, (when, kind, tool, lane, who, effect, status, summary, usd, run_id) in enumerate(reversed(trace)):
            self.activity.append({"id": f"ev_{i:04d}", "at": self._ago(**when), "kind": kind, "tool": tool,
                                  "lane": lane, "principal": who, "effect": effect, "status": status,
                                  "summary": summary,
                                  "cost": {"tokens": int((usd or 0) * 220000), "usd": usd} if usd else None,
                                  "run_id": run_id})

        self.status = {"node": "node-1", "version": "0.1.0", "csrf": "csrf-demo",
                       "needs_you": 0, "cost_today": {"tokens": 412000, "usd": 1.84},
                       "brain": {"name": "claude-max", "state": "ready", "offline_ready": True},
                       "nodes": [{"id": n["id"], "state": n["state"], "degraded": n["degraded"]}
                                 for n in self.nodes],
                       "timezone": TZ}

    def _control(self, state: str, *, since: str | None = None, last_input: str | None = None,
                 takeback: tuple[str, str] | None = None) -> dict:
        return {"state": state, "by": "owner" if state != "agent" else None, "since": since,
                "takeback_requested": takeback is not None,
                "takeback_reason": takeback[0] if takeback else None,
                "takeback_by": takeback[1] if takeback else None, "last_human_input": last_input}

    def _add_lane(self, ws: str, name: str, *, kind: str = "browser", engine: str | None = "camoufox",
                  preset: str = "account", node: str = "node-1", proxy: bool = True,
                  last_used: str | None = None) -> None:
        lane_id = f"{ws}/{name}"
        p = PROXIES.get(ws)
        proxy = proxy and p is not None
        self.lanes[lane_id] = {
            "id": lane_id, "workspace": ws, "name": name, "kind": kind, "engine": engine, "preset": preset,
            "state": "cold", "signal": "ok" if kind == "browser" else "unknown",
            "proxy": {"label": p[0], "host": p[1], "port": p[2]} if proxy and p else None,
            "exit_ip": p[1] if proxy and p else None, "last_used": last_used, "node": node,
            "quarantine_reason": None, "logged_in_hint": "ok" if kind == "browser" else "unknown",
            "control": self._control("agent"),
        }

    def _flow(self, flow_id: str, params: dict, effect: str) -> dict:
        ws, surface = flow_id.split(".")[:2]
        return {"id": flow_id, "lane": f"{ws}/{surface}", "version": 3,
                "params": {k: {"type": v} for k, v in params.items()}, "effect": effect, "last_run": None}

    def _schedule(self, sid: str, flow: str, cron: str | None, next_in: dict | None, *,
                  watch: dict | None = None, enabled: bool = True) -> dict:
        return {"id": sid, "flow": flow, "cron": cron, "watch": watch, "params": {}, "enabled": enabled,
                "next_fire_at": self._in(**next_in) if next_in and enabled else None,
                "lane": self.flows[flow]["lane"], "deadman_minutes": 90 if cron else None,
                "last_run": None, "timezone": TZ}

    # ---- views ----
    def _workspaces_view(self) -> list[dict]:
        out = []
        for ws in self.workspaces:
            lanes = [lane for lane in self.lanes.values() if lane["workspace"] == ws["id"]]
            out.append({**ws, "health": "ok", "summary": f"{len(lanes)} lanes", "lanes": lanes})
        return out

    def _needs_you(self) -> int:
        lanes = self.lanes.values()
        return (len(self.approvals) + len(self.questions)
                + sum(1 for lane in lanes if lane["control"]["takeback_requested"])
                + sum(1 for lane in lanes if lane["state"] == "quarantined"
                      or lane["signal"] in TUI_NEED))

    def _trace(self, tool: str, lane: str | None, summary: str, *, effect: str | None = None,
               status: str = "ok", kind: str = "control") -> None:
        event = {"id": self.next_id("ev"), "at": clock.iso_or_none(datetime.now(UTC), z=True), "kind": kind, "tool": tool,
                 "lane": lane, "principal": "owner", "effect": effect, "status": status,
                 "summary": summary, "cost": None, "run_id": None}
        self.activity.append(event)
        self.emit("trace", event)

    # ---- the app ----
    def _make_app(self) -> Starlette:
        r = Route
        return Starlette(routes=[
            r("/api/status", self.h_status),
            r("/api/workspaces", self.h_workspaces),
            r("/api/lanes/{ws}/{name}", self.h_lane),
            r("/api/lanes/{ws}/{name}/open", self.h_lane_open, methods=["POST"]),
            r("/api/lanes/{ws}/{name}/close", self.h_lane_close, methods=["POST"]),
            r("/api/lanes/{ws}/{name}/control", self.h_lane_control, methods=["POST"]),
            r("/api/lanes/{ws}/{name}/view-link", self.h_view_link, methods=["POST"]),
            r("/api/lanes/{ws}/{name}/screenshot", self.h_screenshot),
            r("/api/nodes", self.h_nodes),
            r("/api/approvals", self.h_approvals),
            r("/api/approvals/batch", self.h_approvals_batch, methods=["POST"]),
            r("/api/approvals/{id}/approve", self.h_approve, methods=["POST"]),
            r("/api/approvals/{id}/deny", self.h_deny, methods=["POST"]),
            r("/api/approvals/{id}/preview.png", self.h_preview_png),
            r("/api/questions", self.h_questions),
            r("/api/questions/{id}/answer", self.h_answer, methods=["POST"]),
            r("/api/activity", self.h_activity),
            r("/api/runs", self.h_runs),
            r("/api/runs/{id}", self.h_run),
            r("/api/flows", self.h_flows),
            r("/api/flows/{id}/run", self.h_flow_run, methods=["POST"]),
            r("/api/schedules", self.h_schedules, methods=["GET", "POST"]),
            r("/api/schedules/{id}", self.h_schedule, methods=["DELETE", "PATCH"]),
            r("/api/brains", self.h_brains),
            r("/api/brains/tasks", self.h_tasks),
            r("/api/brains/tasks/{id}", self.h_task),
            r("/api/events", self.h_events),
            r("/api/orgs/local/environments", self.h_environments),
            r("/api/orgs/local/environments/{id}", self.h_environment),
        ])

    def _gate(self, request: Request, resource: str) -> Response | None:
        if self.token is not None and request.headers.get("authorization") != f"Bearer {self.token}":
            return _err(401, "unauthorized", "missing or wrong token")
        if resource in self.unavailable:
            return _err(503, "unavailable", f"{resource} is not configured on this node")
        return None

    async def _body(self, request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _lane_or_404(self, request: Request) -> dict | Response:
        lane_id = f"{request.path_params['ws']}/{request.path_params['name']}"
        lane = self.lanes.get(lane_id)
        return lane if lane is not None else _err(404, "lane_not_found", f"no lane {lane_id}")

    async def h_status(self, request: Request) -> Response:
        if g := self._gate(request, "status"):
            return g
        return JSONResponse({**self.status, "needs_you": self._needs_you()})

    async def h_workspaces(self, request: Request) -> Response:
        if g := self._gate(request, "workspaces"):
            return g
        return JSONResponse(self._workspaces_view())

    async def h_lane(self, request: Request) -> Response:
        if g := self._gate(request, "workspaces"):
            return g
        lane = self._lane_or_404(request)
        return lane if isinstance(lane, Response) else JSONResponse(lane)

    async def h_lane_open(self, request: Request) -> Response:
        if g := self._gate(request, "workspaces"):
            return g
        lane = self._lane_or_404(request)
        if isinstance(lane, Response):
            return lane
        if lane["node"] == "node-2":
            return _err(503, "unavailable", "node-2 is not answering")
        lane.update(state="warm", last_used=clock.iso_or_none(datetime.now(UTC), z=True), quarantine_reason=None,
                    viewer={"display": ":111", "url": f"/view/{lane['id']}"})
        self.emit("lane", lane)
        return JSONResponse(lane)

    async def h_lane_close(self, request: Request) -> Response:
        if g := self._gate(request, "workspaces"):
            return g
        lane = self._lane_or_404(request)
        if isinstance(lane, Response):
            return lane
        lane.update(state="cold", viewer=None)
        self.emit("lane", lane)
        return JSONResponse(lane)

    async def h_lane_control(self, request: Request) -> Response:
        if g := self._gate(request, "workspaces"):
            return g
        lane = self._lane_or_404(request)
        if isinstance(lane, Response):
            return lane
        action = (await self._body(request)).get("action")
        now = clock.iso_or_none(datetime.now(UTC), z=True)
        c = lane["control"]
        if action == "take":
            lane["control"] = self._control("human", since=now)
        elif action == "hand_back":
            lane["control"] = self._control("agent")
        elif action == "stop_agent":
            lane["control"] = self._control("paused", since=now)
            for run in self.runs.values():
                if run["lane"] == lane["id"] and run["status"] in ("running", "queued", "waiting_approval"):
                    run["status"] = "cancelled"
                    run["error"] = "stopped by owner"
                    self.emit("run", run)
        elif action == "deny_request":
            if not c["takeback_requested"]:
                return _err(409, "conflict", "no pending request for control")
            c.update(takeback_requested=False, takeback_reason=None, takeback_by=None)
        else:
            return _err(400, "invalid", f"unknown action {action!r}")
        self.emit("control", {"lane": lane["id"], "control": lane["control"]})
        self._trace(f"lane_{action}", lane["id"], f"owner: {action.replace('_', ' ')}")
        return JSONResponse(lane)

    async def h_view_link(self, request: Request) -> Response:
        if g := self._gate(request, "workspaces"):
            return g
        lane = self._lane_or_404(request)
        if isinstance(lane, Response):
            return lane
        mode = (await self._body(request)).get("mode", "view")
        if mode not in ("view", "control"):
            return _err(400, "invalid", "mode must be view or control")
        if lane["state"] not in ("warm", "active"):
            return _err(409, "conflict", "lane is cold, open it first")
        token = f"{mode[0]}{self.next_id('vt')}"
        return JSONResponse({"url": f"/view/{lane['id']}?t={token}",
                             "expires_at": clock.iso_or_none(datetime.now(UTC) + timedelta(minutes=15), z=True)})

    async def h_screenshot(self, request: Request) -> Response:
        if g := self._gate(request, "workspaces"):
            return g
        lane = self._lane_or_404(request)
        if isinstance(lane, Response):
            return lane
        if lane["state"] not in ("warm", "active"):
            return _err(409, "conflict", "lane is cold, open it first")
        return Response(tiny_png(), media_type="image/png")

    async def h_nodes(self, request: Request) -> Response:
        if g := self._gate(request, "nodes"):
            return g
        return JSONResponse(self.nodes)

    async def h_approvals(self, request: Request) -> Response:
        if g := self._gate(request, "approvals"):
            return g
        state = request.query_params.get("state", "pending")
        return JSONResponse([a for a in self.approvals.values() if a["state"] == state])

    def _decide(self, approval_id: str, decision: str, note: str | None) -> dict | None:
        a = self.approvals.pop(approval_id, None)
        if a is None:
            return None
        a = {**a, "state": "approved" if decision == "approve" else "denied", "note": note}
        self.emit("approval", a)
        self._trace(f"approval_{decision}", a["lane"], f"owner {a['state']}: {a['summary']}",
                    effect=a["effect"], kind="approval")
        return a

    async def h_approve(self, request: Request) -> Response:
        if g := self._gate(request, "approvals"):
            return g
        a = self._decide(request.path_params["id"], "approve", (await self._body(request)).get("note"))
        return JSONResponse(a) if a else _err(404, "not_found", "no such pending approval")

    async def h_deny(self, request: Request) -> Response:
        if g := self._gate(request, "approvals"):
            return g
        a = self._decide(request.path_params["id"], "deny", (await self._body(request)).get("note"))
        return JSONResponse(a) if a else _err(404, "not_found", "no such pending approval")

    async def h_approvals_batch(self, request: Request) -> Response:
        if g := self._gate(request, "approvals"):
            return g
        body = await self._body(request)
        decision = body.get("decision")
        if decision not in ("approve", "deny"):
            return _err(400, "invalid", "decision must be approve or deny")
        done = [a for i in body.get("ids", []) if (a := self._decide(i, decision, None))]
        return JSONResponse(done)

    async def h_preview_png(self, request: Request) -> Response:
        if g := self._gate(request, "approvals"):
            return g
        return Response(tiny_png(rgb=(120, 90, 60)), media_type="image/png")

    async def h_questions(self, request: Request) -> Response:
        if g := self._gate(request, "questions"):
            return g
        return JSONResponse(list(self.questions.values()))

    async def h_answer(self, request: Request) -> Response:
        if g := self._gate(request, "questions"):
            return g
        q = self.questions.pop(request.path_params["id"], None)
        if q is None:
            return _err(404, "not_found", "no such open question")
        q = {**q, "state": "answered", "answer": (await self._body(request)).get("answer")}
        self.emit("question", q)
        return JSONResponse(q)

    async def h_activity(self, request: Request) -> Response:
        if g := self._gate(request, "activity"):
            return g
        qp = request.query_params
        events = self.activity
        if qp.get("lane"):
            events = [e for e in events if e["lane"] == qp["lane"]]
        if qp.get("workspace"):
            events = [e for e in events if (e["lane"] or "").split("/")[0] == qp["workspace"]]
        if qp.get("kind"):
            events = [e for e in events if e["kind"] == qp["kind"]]
        limit = int(qp.get("limit", 100))
        return JSONResponse(list(reversed(events[-limit:])))

    async def h_environments(self, request: Request) -> Response:
        if g := self._gate(request, "environments"):
            return g
        state = request.query_params.get("state") or "open"
        envs = [e for e in self.environments if state == "all" or e["state"] not in ("closed", "failed")]
        return JSONResponse({"environments": envs, "available": True})

    async def h_environment(self, request: Request) -> Response:
        if g := self._gate(request, "environments"):
            return g
        env = next((e for e in self.environments if e["id"] == request.path_params["id"]), None)
        if env is None:
            return _err(404, "not_found", "no such environment")
        return JSONResponse({**env, **self.environment_details.get(env["id"], {"git": None, "proofs": []})})

    async def h_runs(self, request: Request) -> Response:
        if g := self._gate(request, "runs"):
            return g
        runs = sorted(self.runs.values(), key=lambda r: r["started_at"] or "", reverse=True)
        if request.query_params.get("status"):
            runs = [r for r in runs if r["status"] == request.query_params["status"]]
        return JSONResponse([{k: v for k, v in r.items() if k != "steps"} for r in runs])

    async def h_run(self, request: Request) -> Response:
        if g := self._gate(request, "runs"):
            return g
        run = self.runs.get(request.path_params["id"])
        return JSONResponse(run) if run else _err(404, "not_found", "no such run")

    async def h_flows(self, request: Request) -> Response:
        if g := self._gate(request, "flows"):
            return g
        return JSONResponse(list(self.flows.values()))

    async def h_flow_run(self, request: Request) -> Response:
        if g := self._gate(request, "flows"):
            return g
        flow = self.flows.get(request.path_params["id"])
        if flow is None:
            return _err(404, "not_found", "no such flow")
        rid = self.next_id("r")
        run = {"id": rid, "flow": flow["id"], "lane": flow["lane"], "status": "queued",
               "started_at": clock.iso_or_none(datetime.now(UTC), z=True), "finished_at": None, "proof": None, "error": None,
               "trigger": "manual", "cost": None, "params": (await self._body(request)).get("params", {}),
               "steps": []}
        self.runs[rid] = run
        self.emit("run", run)
        return JSONResponse(run)

    async def h_schedules(self, request: Request) -> Response:
        if g := self._gate(request, "schedules"):
            return g
        if request.method == "GET":
            return JSONResponse(list(self.schedules.values()))
        body = await self._body(request)
        flow = body.get("flow")
        if flow not in self.flows:
            return _err(400, "invalid", f"unknown flow {flow!r}")
        cron = body.get("cron")
        if not cron or len(str(cron).split()) != 5:
            return _err(400, "invalid", "cron needs 5 fields")
        sid = self.next_id("s")
        s = self._schedule(sid, flow, cron, {"hours": 1})
        s["params"] = body.get("params") or {}
        self.schedules[sid] = s
        self.emit("schedule", s)
        return JSONResponse(s)

    async def h_schedule(self, request: Request) -> Response:
        if g := self._gate(request, "schedules"):
            return g
        sid = request.path_params["id"]
        s = self.schedules.get(sid)
        if s is None:
            return _err(404, "not_found", "no such schedule")
        if request.method == "DELETE":
            del self.schedules[sid]
            self.emit("schedule", {**s, "deleted": True})
            return Response(status_code=204)
        body = await self._body(request)
        if "enabled" in body:
            s["enabled"] = bool(body["enabled"])
            s["next_fire_at"] = self._in(hours=1) if s["enabled"] else None
        self.emit("schedule", s)
        return JSONResponse(s)

    async def h_brains(self, request: Request) -> Response:
        if g := self._gate(request, "brains"):
            return g
        return JSONResponse(self.brains)

    async def h_tasks(self, request: Request) -> Response:
        if g := self._gate(request, "brains"):
            return g
        return JSONResponse([{k: v for k, v in t.items() if k != "tail"} for t in self.tasks.values()])

    async def h_task(self, request: Request) -> Response:
        if g := self._gate(request, "brains"):
            return g
        task = self.tasks.get(request.path_params["id"])
        return JSONResponse(task) if task else _err(404, "not_found", "no such task")

    async def h_events(self, request: Request) -> Response:
        if g := self._gate(request, "events"):
            return g
        if not self.events_enabled:
            return _err(404, "not_found", "Not Found")
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)

        async def stream():
            try:
                yield b": connected\n\n"
                while True:
                    frame = await queue.get()
                    if frame is None:
                        return
                    yield frame
            finally:
                with contextlib.suppress(ValueError):
                    self._subscribers.remove(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})


class _QueueStream(httpx.AsyncByteStream):
    def __init__(self, queue: asyncio.Queue, task: asyncio.Task, disconnected: asyncio.Event):
        self._queue = queue
        self._task = task
        self._disconnected = disconnected

    async def __aiter__(self):
        while True:
            message = await self._queue.get()
            kind = message["type"]
            if kind == "http.response.body":
                if message.get("body"):
                    yield message["body"]
                if not message.get("more_body", False):
                    return
            elif kind == "error":
                raise httpx.ReadError(str(message["exc"]))
            elif kind == "done":
                return

    async def aclose(self) -> None:
        self._disconnected.set()
        if not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task


class FakeTransport(httpx.AsyncBaseTransport):
    """Serves an ASGI app in-process and streams the response body as it is produced
    (httpx's own ASGITransport waits for the whole body, which never comes for SSE)."""

    def __init__(self, node: FakeNode):
        self.node = node

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.node.reachable:
            raise httpx.ConnectError("connection refused", request=request)
        body = await request.aread()
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = body
        self.node.requests.append(Call(request.method, unquote(request.url.path),
                                       dict(request.url.params), parsed))
        return await _asgi(self.node.app, request, body)


async def _asgi(app, request: httpx.Request, body: bytes) -> httpx.Response:
    queue: asyncio.Queue = asyncio.Queue()
    disconnected = asyncio.Event()
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        await queue.put(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": request.method, "scheme": request.url.scheme,
        "path": unquote(request.url.path), "raw_path": request.url.raw_path.split(b"?")[0],
        "query_string": request.url.query, "root_path": "",
        "headers": [(k.lower(), v) for k, v in request.headers.raw],
        "server": (request.url.host, request.url.port or 80), "client": ("127.0.0.1", 50000),
    }

    async def run():
        try:
            await app(scope, receive, send)
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as a dropped connection
            await queue.put({"type": "error", "exc": exc})
        finally:
            await queue.put({"type": "done"})

    task = asyncio.create_task(run())
    start = await queue.get()
    if start["type"] != "http.response.start":
        await queue.put(start)
        raise httpx.RemoteProtocolError("the app sent no response", request=request)
    return httpx.Response(start["status"], headers=start.get("headers", []),
                          stream=_QueueStream(queue, task, disconnected), request=request)


def create_app() -> Starlette:
    """`uvicorn agentos.dev.tuidemo:create_app --factory` serves the demo node over HTTP."""
    return FakeNode().app
