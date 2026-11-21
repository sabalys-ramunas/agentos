"""Local changes onto an environment's machine, for real (the docker provider): terminal_upload and
terminal_job_start(sync=...) without `on` go to the chat's environment, nobody is asked, and the files
land on the machine next to the clone, where the job's command puts them into the repository.
Skipped unless docker answers and the image is built, as tests/unit/test_environments_docker.py."""

from __future__ import annotations

import base64
import hashlib

from test_environments_docker import HOST, REPO, node, pytestmark  # noqa: F401  (node is a fixture)

from openajar.core.models import Effect
from openajar.env import tool_specs as term_specs
from openajar.env.sync import Entry, Manifest
from openajar.environments.tools import tool_specs as env_tool_specs
from openajar.policy.wrap import bind_request_token, wrap_tool


def _chunk(data: bytes) -> tuple[str, str]:
    return hashlib.sha256(data).hexdigest(), base64.b64encode(data).decode()


async def test_local_changes_reach_the_environment_by_upload_and_sync(node):  # noqa: F811
    rt, policy = node["rt"], node["policy"]
    _grant, token = policy.issue(subject={"id": "agent-1"}, workspaces=["*"], ceiling=Effect.COMMIT,
                                 specials={"vm": {}, "exec": {}})
    calls = {s.name: wrap_tool(s, rt) for s in [*env_tool_specs(rt), *term_specs(rt)]}

    async def call(name, **kw):
        with bind_request_token(token, session="chat-sync"):
            return await calls[name](**kw)

    env = await call("environment_open", title="Local work", repo=f"{HOST}/{REPO}", wait_s=150)
    assert env["state"] == "ready", env

    # the human's uncommitted change, as `openajar run` sends a tree: chunks, then a manifest of them
    changed = b"<title>Shop</title><h1>Forty percent off</h1>\n"
    new = b"console.log('banner')\n"
    files = {"index.html": changed, "banner.js": new}
    chunks = dict(_chunk(data) for data in files.values())
    manifest = Manifest(files=[Entry(path=p, size=len(d), chunks=[_chunk(d)[0]]) for p, d in files.items()])
    msha, mb64 = _chunk(manifest.model_dump_json().encode())
    missing = await call("terminal_upload_check", chunks=[*chunks, msha])
    assert sorted(missing["missing"]) == sorted([*chunks, msha]) and missing["lane"] == f"env/{env['id']}"
    stored = await call("terminal_upload", chunks={**chunks, msha: mb64})
    assert stored.get("status") != "approval_pending" and stored["stored"] == 3, stored

    job = await call("terminal_job_start", cmd="cp -a ~/local/. /work/shop/ && cd /work/shop && git status --short",
                     sync={"dest": "local", "manifest": [msha]})
    assert job.get("status") != "approval_pending", job
    text, offset = "", 0
    for _ in range(30):
        out = await call("terminal_job_read", job=job["id"], stdout_offset=offset, wait_s=2)
        text, offset = text + out["stdout"], out["next_stdout_offset"]
        if out["state"] != "running":
            break
    assert out["state"] == "exited" and out["exit_code"] == 0, out
    assert "M index.html" in text and "?? banner.js" in text, text
    seen = await call("terminal_exec", cmd="cat index.html", cwd="/work/shop")
    assert "Forty percent off" in seen["stdout"]
    closed = await call("environment_close", discard=True)
    assert closed["state"] == "closed"
