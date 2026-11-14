"""Brains against the REAL claude and codex CLIs on the node (no real credentials).

Pins the flags agentos relies on against the installed CLIs, and runs the real CLIs through
the full runner with deliberately invalid credentials, so the real stream formats, the auth
classification and the MCP callback with a child token are all exercised. Real
authenticated runs need the owner's own subscription (see src/agentos/brains/README.md).

Run on the node:  uv run pytest -m node tests/node/test_brains_real_cli.py
"""

import http.server
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import ClassVar

import pytest
from testkit.brains import FakeConfig, FakeCtx, FakeNotifier, FakePolicy, wait_status

from agentos.brains import cliprobe
from agentos.brains.models import BrainSpec, Budget
from agentos.brains.service import BrainService
from agentos.brains.settings import BrainsSettings
from agentos.core.models import Principal
from agentos.core.runtime import Runtime

pytestmark = pytest.mark.node

BIN = "/opt/agentos/node/bin"
INVALID_OAUTH = "sk-ant-oat01-agentos-node-test-invalid-" + "0" * 40
INVALID_OPENAI = "sk-agentos-node-test-invalid-" + "0" * 30
OWNER = "owner-token-node-test-" + "O" * 30
CHILD = "child-token-node-test-" + "C" * 30
P = Principal(id="node-test")

needs_cli = pytest.mark.skipif(not (Path(BIN) / "claude").exists() or not (Path(BIN) / "codex").exists(),
                               reason="run install_cli.sh on the node first")


@needs_cli
@pytest.mark.parametrize("runner", ["claude", "codex"])
def test_real_cli_has_every_pinned_flag(runner):
    cliprobe.clear_cache()
    info = cliprobe.probe(runner, bin_dirs=[BIN], use_cache=False)
    assert info.ok, info.problems
    assert info.version == cliprobe.PINNED[runner]["verified"], (
        f"{runner} is {info.version}; re-verify the flags and update PINNED")


class _HeaderLog(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list] = []

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        _HeaderLog.seen.append(self.headers.get("authorization"))
        self.send_response(401)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def mcp_server():
    _HeaderLog.seen = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _HeaderLog)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    srv.shutdown()


@pytest.fixture
def service(tmp_path, mcp_server, monkeypatch):
    monkeypatch.setenv("AGENTOS_TOKEN", OWNER)
    home = Path(tempfile.mkdtemp(prefix="agentos-brains-real-"))
    notifier = FakeNotifier()
    rt = Runtime(config=FakeConfig(home, token=OWNER), policy=FakePolicy(token=CHILD), notifier=notifier)
    settings = BrainsSettings(bin_dirs=[BIN], mcp_url=mcp_server, poll_seconds=0.2, stall_seconds=120)
    svc = BrainService(rt, home=home, settings=settings)
    svc.notifier = notifier
    yield svc
    user = svc._run_as()
    for rec in svc.store.list_tasks(limit=100):
        if user:
            shutil.rmtree(Path(user.pw_dir) / "tasks" / rec.id, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)


@needs_cli
async def test_real_claude_invalid_token_is_needs_auth_and_child_token_reaches_mcp(service):
    spec = BrainSpec.template("claude-real", "subscription", "anthropic")
    await service.add_brain(spec, credential=INVALID_OAUTH)
    r = await service.submit(P, prompt="Reply with OK.", workspace="nodetest", lanes=["nodetest/x"])
    st = await wait_status(service, r["id"], until=("queued", "failed"), timeout=120)
    attempt = st["attempts"][0]
    assert attempt["outcome"] == "auth_failed", (attempt, service.tail(r["id"])["events"])
    info = (await service.list_brains())[0]
    assert info["state"] == "needs_auth"
    assert any(n["kind"] == "brain.needs_auth" for n in service.notifier.sent)
    # the real claude called our MCP endpoint with the CHILD token, never the owner token
    assert _HeaderLog.seen and all(h == f"Bearer {CHILD}" for h in _HeaderLog.seen), _HeaderLog.seen
    if os.geteuid() == 0 and service._run_as():
        status = json.loads((service.attempt_dir(r["id"], 1) / "launch.json").read_text())
        assert status["run_as"]["uid"] == service._run_as().pw_uid
    await service.cancel(r["id"])
    # nothing agentos keeps for the task holds the credential, the child token or the owner token
    files = [p for p in service.task_dir(r["id"]).rglob("*") if p.is_file()]
    files += [p for p in (service.task_dir(r["id"]) / "home").resolve().rglob("*") if p.is_file()]
    blob = b"".join(p.read_bytes() for p in files)
    assert INVALID_OAUTH.encode() not in blob and CHILD.encode() not in blob and OWNER.encode() not in blob


@needs_cli
async def test_real_codex_invalid_key_is_needs_auth(service):
    spec = BrainSpec.template("codex-real", "metered", "openai")
    await service.add_brain(spec, credential=INVALID_OPENAI)
    r = await service.submit(P, prompt="Reply with OK.", workspace="nodetest", budget=Budget(max_usd=0.01),
                             ctx=FakeCtx(service.runtime, P))
    st = await wait_status(service, r["id"], until=("queued", "failed"), timeout=180)
    assert st["attempts"][0]["outcome"] == "auth_failed", service.tail(r["id"])["events"][-5:]
    assert (await service.list_brains())[0]["state"] == "needs_auth"
    await service.cancel(r["id"])


@needs_cli
async def test_real_claude_without_any_credential_is_never_started(service):
    await service.add_brain(BrainSpec.template("claude-none", "subscription", "anthropic"))
    r = await service.submit(P, prompt="hi", workspace="nodetest")
    assert r["status"] == "queued" and "sign in again" in r["status_reason"]
    assert r["attempts"] == []
    await service.cancel(r["id"])
