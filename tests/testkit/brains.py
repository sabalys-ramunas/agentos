"""Test helpers for brains: fake vault, policy, notifier and tool context, plus a service
wired to the fake CLIs. Used by tests/unit/test_brains*.py and tests/node/test_brains*.py.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from agentos.brains import cliprobe
from agentos.brains.models import BrainSpec
from agentos.brains.service import BrainService
from agentos.brains.settings import BrainsSettings
from agentos.core.errors import ApprovalPending, PolicyDenied
from agentos.core.models import Effect, Principal
from agentos.core.runtime import Runtime
from agentos.dev import fakecli
from agentos.policy.grants import fake_grant

OAUTH = "sk-ant-oat01-" + "T" * 60          # fake subscription token
API_KEY = "sk-ant-api03-" + "K" * 60        # fake metered key
OWNER_TOKEN = "owner-token-" + "O" * 40     # the owner's own agentos token
CHILD_TOKEN = "child-token-" + "C" * 40


class FakeVault:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    async def put_secret(self, ref, value, *, kind, label, scope):
        self.calls.append(("put", ref))
        self.data[ref] = value

    async def get_secret(self, ref, *, purpose):
        self.calls.append(("get", ref))
        return self.data.get(ref)

    async def delete_secret(self, ref):
        self.calls.append(("delete", ref))
        self.data.pop(ref, None)


class FakePolicy:
    def __init__(self, *, token: str = CHILD_TOKEN, allowance: dict | None = None, taint: set[str] | None = None):
        self.token = token
        self.allowance = allowance
        self._taint = taint or set()
        self.minted: list[dict] = []
        self.revoked: list[str] = []

    async def mint_child_token(self, principal, **kw):
        self.minted.append({"principal": principal.id, **kw})
        return {"token": self.token, "grant_id": f"g_{len(self.minted)}"}

    async def revoke(self, grant_id):
        self.revoked.append(grant_id)

    async def brain_allowance(self, principal):
        return self.allowance

    async def taint_for(self, principal):
        return self._taint

    async def is_owner_token(self, token):
        return token == OWNER_TOKEN


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def notify(self, title, body, *, kind, urgent, data):
        self.sent.append({"title": title, "body": body, "kind": kind, "urgent": urgent, "data": data})


class FakeConfig:
    def __init__(self, home: Path, token: str = OWNER_TOKEN):
        self.home = home
        self.mcp_token = token
        self.mcp_port = 7499


class FakeCtx:
    """A ToolContext: records authorize calls; can deny or demand approval per effect."""

    def __init__(self, runtime: Runtime, principal: Principal | None = None, *, deny: set[Effect] | None = None,
                 pending: set[Effect] | None = None):
        self.runtime = runtime
        self.principal = principal or Principal(id="claude-code", thread="T-1")
        self.deny = deny or set()
        self.pending = pending or set()
        self.authorized: list[dict] = []
        self.traces: list[tuple[str, dict]] = []
        self.grant = fake_grant(self.principal.id)

    async def authorize(self, effect, *, lane_id=None, domain=None, summary=None, content=None):
        self.authorized.append({"effect": effect, "summary": summary, "content": content})
        if effect in self.deny:
            raise PolicyDenied(f"{effect} denied")
        if effect in self.pending:
            raise ApprovalPending(f"{effect} needs you", approval_id="ap_test")

    def trace(self, kind, **fields):
        self.traces.append((kind, fields))


def make_service(tmp: Path, *, runtime: Runtime | None = None, python: str | None = None,
                 bin_dir: Path | None = None, decide_transport: httpx.AsyncBaseTransport | None = None,
                 **settings: Any) -> BrainService:
    cliprobe.clear_cache()
    bins = bin_dir or tmp / "bin"
    fakecli.install(bins, python=python)
    defaults: dict[str, Any] = {
        "bin_dirs": [str(bins)], "isolation": "none", "run_as": None, "poll_seconds": 0.1,
        "cancel_grace_seconds": 0.5, "cooldown_base_seconds": 60, "notify_on_finish": True,
        "scrub_subprocess_env": False,  # same behavior with or without bwrap on the test machine
        "chat_python": sys.executable,  # the system Python need not exist on a test machine
        "confinement": "off"}  # the brain is the test's own user; tests/unit/test_brains_confine.py checks it
    defaults.update(settings)
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    rt = runtime if runtime is not None else Runtime()
    if not rt.has("config"):
        rt.register("config", FakeConfig(home))
    svc = BrainService(rt, home=home, settings=BrainsSettings(**defaults), online_check=lambda: True,
                       local_check=lambda spec: (True, None), allow_file_git=True,
                       decide_transport=decide_transport)
    rt.register("brains", svc)
    return svc


async def add(svc: BrainService, name: str, kind: str, provider: str, credential: str | None = None,
              **overrides: Any) -> BrainSpec:
    spec = BrainSpec.template(name, kind, provider, **overrides)
    if credential is None and spec.credential == "oauth_token":
        credential = OAUTH
    elif credential is None and spec.credential == "api_key":
        credential = API_KEY
    await svc.add_brain(spec, credential=credential)
    return spec


async def wait_status(svc: BrainService, task_id: str, *, until: tuple[str, ...] | None = None,
                      timeout: float = 15.0, tick: bool = True) -> dict:
    """Poll (driving the monitor by hand) until the task leaves the active states."""
    until = until or ("succeeded_unverified", "failed", "cancelled", "interrupted")
    deadline = time.time() + timeout
    st = svc.status(task_id)
    while time.time() < deadline:
        if tick:
            await svc.tick()
        st = svc.status(task_id)
        if st["status"] in until:
            return st
        await asyncio.sleep(0.1)
    raise AssertionError(f"task {task_id} stayed {st['status']}: {st.get('status_reason')}")


def dump(svc: BrainService, task_id: str) -> dict:
    """The fake CLI's record of what reached the brain (argv, env, prompt, MCP config)."""
    import json
    return json.loads((svc.task_dir(task_id) / "work" / "fake-dump.json").read_text())


def all_task_bytes(svc: BrainService, task_id: str) -> bytes:
    """Every byte agentos wrote for a task (transcripts, status, launch, logs, brain home)."""
    out = b""
    for p in sorted(svc.task_dir(task_id).rglob("*")):
        if p.is_file() and p.name != "fake-dump.json" and p.name != "prompt.txt":
            out += p.read_bytes()
    return out
