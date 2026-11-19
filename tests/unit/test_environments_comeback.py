# ruff: noqa: F811 - `node` is test_environments_saves's fixture, imported by its name
"""Coming back to an environment (builder I3): services restarted and awaited on resume, one call to
stop with a note and one to finish for good, the environment found by its repository, the browser's
sign-ins across a save, a port on the person's computer, and a real pull request.

The node is test_environments_saves's: a fake cloud whose machines are folders, every save and restore
run for real on them. GitHub is an httpx.MockTransport; the SOCKS proxy of a tunnel is a small server
on a Unix socket here."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_environments_saves import REPO, TITLE, disk, node, opened, work_on_it  # noqa: F401 - the fixture

from agentos.core.errors import InvalidRequest, PolicyDenied
from agentos.environments import comeback, ports, pulls, saving, signins
from agentos.environments.saves import open_payload
from agentos.git import keys as keymod
from agentos.git import login
from agentos.policy.wrap import ToolCallError

STATE = {"cookies": [{"name": "sid", "value": "cookie-secret-1", "domain": "shop.test", "path": "/",
                      "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Lax", "junk": 1}],
         "origins": [{"origin": "http://localhost:3000", "localStorage": [{"name": "token", "value": "ls-secret"}]},
                     {"origin": "file:///etc", "localStorage": [{"name": "x", "value": "y"}]}]}


@pytest.fixture(autouse=True)
def quick_waits(monkeypatch):
    monkeypatch.setattr(comeback, "SERVICE_WAIT_S", 0.3)
    monkeypatch.setattr(comeback, "POLL_S", 0.05)


# ------------------------------------------------------------------ 1. services on resume


async def test_a_service_that_does_not_come_back_gets_a_ready_restart_call(node, monkeypatch):
    """One service listens again (port 3000, which the fake machine's ss reports); one runs but its
    port never listens; one does not start at all."""
    out = await opened(node, keep="save")
    lane = f"env/{out['id']}"
    for cmd in ("npm run dev -- --port 3000", "vite --port 4000", "./worker --port 5000"):
        await node["envs"].job_start(lane, cmd, cwd="/work/shop")
    await node["call"]("environment_close")
    real = node["machines"].job_start

    async def start(lane_id, cmd, **kw):
        if "worker" in cmd:
            raise RuntimeError("worker: not found")
        return await real(lane_id, cmd, **kw)

    monkeypatch.setattr(node["envs"], "job_start", start)
    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    by_cmd = {s["cmd"]: s for s in again["resumed"]["services"]}
    assert by_cmd["npm run dev -- --port 3000"]["running"] and by_cmd["npm run dev -- --port 3000"]["listening"]
    slow = by_cmd["vite --port 4000"]
    assert slow["running"] and slow["listening"] is False and "did not listen" in slow["why"]
    dead = by_cmd["./worker --port 5000"]
    assert dead["running"] is False and "not found" in dead["why"]
    assert dead["restart"] == 'terminal_job_start(cmd="./worker --port 5000", cwd="/work/shop")'
    assert "running_jobs_lost" not in again["resumed"]  # two came back
    assert any("./worker" in m for m in again["resumed"]["missing"])
    assert again["resumed"]["expires_at"] == again["expires_at"]


async def test_nothing_restarts_lists_the_lost_jobs(node, monkeypatch):
    out = await opened(node, keep="save")
    await node["envs"].job_start(f"env/{out['id']}", "pnpm dev --port 5321", cwd="/work/shop")
    await node["call"]("environment_close")

    async def broken(*_a, **_kw):
        raise RuntimeError("the terminal is gone")

    monkeypatch.setattr(node["envs"], "job_start", broken)
    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    assert again["resumed"]["running_jobs_lost"] == ["pnpm dev --port 5321 (port 5321)"]
    assert again["resumed"]["services"][0]["restart"].startswith('terminal_job_start(cmd="pnpm dev --port 5321"')


# scripts.LISTENING's lines from ubuntu:24.04 in docker (2026-11-16): a job started the way agentos
# starts one (port 3000, under the node's workload shell), a server left running with nohup (port 4000),
# and one outside /work (port 5000, not listed)
PROCS = [
    "3000 0 L3dvcmsvYXBw cHl0aG9uMwAtbQBodHRwLnNlcnZlcgAzMDAwAA==",
    "3000 1 L3dvcmsvYXBw L2Jpbi9zaAAtYwBjZCAnL3dvcmsvYXBwJyAmJiBweXRob24zIC1tIGh0dHAuc2VydmVyIDMwMDAA",
    "3000 2 L3Jvb3Q= c2gALWMAZWNobyAkJCA+IC90bXAvYWdlbnRvcy13bC9qLWRldjEucGdpZDsgIiRAIjsgcmM9JD87IHRyYXAgIiIgVEVSTTsga2lsbCAtcyBURVJNIC0tIC0kJCAyPi9kZXYvbnVsbDsgcm0gLWYgL3RtcC9hZ2VudG9zLXdsL2otZGV2MS5wZ2lkOyBleGl0ICRyYwBzaAAvYmluL3NoAC1jAGNkICcvd29yay9hcHAnICYmIHB5dGhvbjMgLW0gaHR0cC5zZXJ2ZXIgMzAwMAA=",
    "4000 0 L3dvcmsvb3RoZXI= cHl0aG9uMwAtbQBodHRwLnNlcnZlcgA0MDAwAA==",
]


def test_what_listens_in_work_gives_the_command_that_was_started():
    assert comeback.listening(PROCS) == [
        {"cmd": "python3 -m http.server 3000", "cwd": "/work/app", "port": 3000},   # the job, as it was started
        {"cmd": "python3 -m http.server 4000", "cwd": "/work/other", "port": 4000},
    ]
    assert comeback.listening(["junk", "3000 0 !!! ???", ""]) == []
    # a login shell and sshd above it are not the command; a process title ("npm run dev") is kept whole
    def line(port, depth, cwd, *argv):
        return f"{port} {depth} {base64.b64encode(cwd.encode()).decode()} " + base64.b64encode(
            b"\0".join(a.encode() for a in argv) + b"\0").decode()
    chain = [line(5173, 0, "/work/shop", "node", "/work/shop/node_modules/.bin/vite"),
             line(5173, 1, "/work/shop", "npm run dev"), line(5173, 2, "/work/shop", "-bash"),
             line(5173, 3, "/", "sshd-session: root@pts/0")]
    assert comeback.listening(chain) == [{"cmd": "npm run dev", "cwd": "/work/shop", "port": 5173}]


def test_a_listener_a_job_has_is_that_jobs():
    from agentos.environments.saves import Service

    jobs = [Service(cmd="python3 -m http.server 3000", cwd="/work/app", port=None),
            Service(cmd="pnpm dev --port 5173", cwd="/work/shop", port=5173)]
    found = comeback.listening(PROCS) + [{"cmd": "node vite", "cwd": "/work/shop", "port": 5173}]
    got = comeback.with_listening(jobs, found, Service)
    assert [(s.cmd, s.port) for s in got] == [("python3 -m http.server 3000", 3000), ("pnpm dev --port 5173", 5173),
                                              ("python3 -m http.server 4000", 4000)]
    assert jobs[0].port is None                                       # the node's own list is left as it was


async def test_a_dev_server_the_node_lost_track_of_comes_back_on_resume(node, monkeypatch):
    """Round 3 (T2): the node restarted while the dev server's job ran, so it had no job to save and the
    resume started nothing (resumed.services: []). The save now records what listened on the machine."""
    out = await opened(node, keep="save")
    real = node["envs"].exec

    async def exec_(lane_id, cmd, **kw):
        res = await real(lane_id, cmd, **kw)
        if cmd.startswith("bash -s") and kw.get("stdin") == saving.scripts.SAVE:
            res = res.model_copy(update={"stdout": res.stdout.replace("ok=1", "\n".join(
                "proc=" + p for p in PROCS) + "\nok=1")})
        return res

    monkeypatch.setattr(node["envs"], "exec", exec_)
    assert node["envs"].job_list(f"env/{out['id']}") == []            # no job the node knows
    await node["call"]("environment_close")
    monkeypatch.setattr(node["envs"], "exec", real)
    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    rows = {s["cmd"]: s for s in again["resumed"]["services"]}
    assert rows["python3 -m http.server 3000"]["cwd"] == "/work/app" and rows["python3 -m http.server 3000"]["running"]
    assert rows["python3 -m http.server 3000"]["listening"] is True    # the fake machine's ss says 3000 listens
    assert set(rows) == {"python3 -m http.server 3000", "python3 -m http.server 4000"}
    assert "running_jobs_lost" not in again["resumed"]


def test_restart_call_quotes_the_command():
    assert comeback.restart_call('echo "hi"', None) == 'terminal_job_start(cmd="echo \\"hi\\"")'


# ------------------------------------------------------------------ 2. stop with a note, finish for good


async def test_close_with_a_note_saves_it_and_says_what_the_pause_costs(node):
    out = await opened(node, keep="save")
    work_on_it(disk(node, out["id"]) / "work" / "shop")
    closed = await node["call"]("environment_close", note="tests pass; next: the mobile menu")
    assert closed["keep"] == "save" and closed["saves"]["made"] == closed["saved"]["save_id"]
    # 1.5 GB at EUR 0.0119 a GB: about EUR 0.02 a month; a snapshot resumes in about a minute
    assert closed["pause"] == "saved: about EUR 0.02 a month, resumes in about a minute"
    assert closed["pause"] in closed["next"] and "continue Black Friday page in agentos" in closed["next"]
    listed = await node["call"]("environment", session="chat-2")
    assert listed["saved"][0]["note"] == "tests pass; next: the mobile menu"
    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    assert "tests pass; next: the mobile menu" in again["resumed"]["note"]
    now = await node["call"]("environment", session="chat-2")
    assert now["note"] == "tests pass; next: the mobile menu"


async def test_a_discard_close_says_what_happened_to_the_saves(node):
    out = await opened(node, keep="save")
    await node["call"]("environment_save", note="first")
    closed = await node["call"]("environment_close", discard=True)
    assert "keep" not in closed and "saved" not in closed
    assert closed["saves"]["made"] is None and len(closed["saves"]["left"]) == 1
    assert "without a save" in closed["next"] and "environment_forget" in closed["next"]
    assert closed["machine"] == "deleted" and out["id"] == closed["id"]


async def test_forget_true_deletes_the_machine_and_every_save(node):
    out = await opened(node, keep="save")
    await node["call"]("environment_save")
    await node["call"]("environment_save")
    work_on_it(disk(node, out["id"]) / "work" / "shop")
    with pytest.raises(ToolCallError) as e:  # unpushed work: forget alone is refused, nothing is lost
        await node["call"]("environment_close", forget=True)
    assert e.value.code == "unsaved_work" and "discard=true" in e.value.message
    assert node["mgr"].get(out["id"]).open
    gone = await node["call"]("environment_close", forget=True, discard=True)
    assert gone["machine"] == "deleted" and len(gone["saves"]["forgot"]) == 2 and gone["saves"]["snapshots_deleted"] == 2
    assert node["mgr"].saves().all() == [] and not node["cloud"].snapshots
    listed = await node["call"]("environment")
    assert listed["saved"] == [] and listed["open"] == []


async def test_forget_true_on_a_live_one_deletes_it(node):
    await opened(node, keep="live")
    gone = await node["call"]("environment_close", forget=True)
    assert gone["machine"] == "deleted" and gone["saves"] == {"made": None, "forgot": [], "snapshots_deleted": 0}


# ------------------------------------------------------------------ 3. found without the exact title


async def test_environment_lists_the_family_also_with_one_attached(node):
    await opened(node, keep="save")
    await node["call"]("environment_close")
    await opened(node, title="Docs fix", session="chat-2")
    here = await node["call"]("environment", session="chat-2")
    assert [(o["title"], o.get("this_chat")) for o in here["open"]] == [("Docs fix", True)]
    assert [s["title"] for s in here["saved"]] == [TITLE]


async def test_a_client_without_a_session_id_never_hears_this_chat(node):
    """MCP 2026-07-28 and stdio send no session id: every chat of the agent looks the same, so nothing
    claims a computer is this chat's without its handle, and the open says to pass it on every call."""
    out = await opened(node, session=None)
    assert out["on"].startswith(out["id"] + ".") and f'keep on="{out["on"]}" and pass it on every call' in out["next"]
    here = await node["call"]("environment", session=None)
    assert here["environment"] is None and "this_chat" not in here["open"][0] and "on" not in here["open"][0]
    assert "cannot tell which machine is yours" in here["note"]
    named = await node["call"]("environment", on=out["on"], session=None)
    assert named["id"] == out["id"] and named["on"] == out["on"] and named["open"][0]["this_chat"] is True
    mine = await opened(node, title="Docs fix", session="chat-9")
    assert mine["on"].startswith(mine["id"] + ".") and "calls without `on` go here" in mine["next"]
    listed = await node["call"]("environment", session="chat-9")
    assert [(o["title"], o.get("this_chat")) for o in listed["open"]] == [(TITLE, None), ("Docs fix", True)]
    assert "in use by another chat" in listed["open"][0]["use"] and "id" not in listed["open"][0]


async def test_open_by_repo_alone_resumes_the_one_save(node):
    out = await opened(node, keep="save")
    await node["call"]("environment_close")
    again = await node["call"]("environment_open", repo=REPO, session="chat-2")
    assert again["title"] == TITLE and again["resumed"]["save_id"] and again["id"] != out["id"]


async def test_open_by_repo_alone_attaches_the_one_open(node):
    out = await opened(node)
    again = await node["call"]("environment_open", repo=REPO)  # this chat's
    assert again["id"] == out["id"]
    mgr = node["mgr"]  # another chat: the open one's title (environment_in_use while this chat works in it)
    env = mgr.get(out["id"])
    assert comeback.by_repo(mgr, env.family, REPO, saves_of=env.family) == TITLE


async def test_open_by_repo_alone_with_several_lists_them(node):
    await opened(node, keep="save")
    await node["call"]("environment_close")
    await opened(node, title="Checkout redesign", keep="save", session="chat-2")
    await node["call"]("environment_close", session="chat-2")
    with pytest.raises(ToolCallError) as e:
        await node["call"]("environment_open", repo=REPO, session="chat-3")
    assert e.value.code == "environment_choice"
    assert '"Checkout redesign"' in e.value.message and f'"{TITLE}"' in e.value.message
    assert 'environment_open(title="Checkout redesign")' in e.value.message  # the newest first


async def test_open_by_repo_alone_with_none_opens_one_named_after_it(node):
    out = await node["call"]("environment_open", repo=REPO)
    assert out["title"] == "shop" and out["state"] == "ready"


async def test_open_with_neither_title_nor_repo_says_what_it_needs(node):
    with pytest.raises(ToolCallError) as e:
        await node["call"]("environment_open")
    assert "needs a title" in e.value.message


# ------------------------------------------------------------------ 4. sign-ins across a save


class FakeContext:
    def __init__(self, state=None):
        self.state, self.cookies, self.scripts = state, [], []

    async def storage_state(self):
        return self.state

    async def add_cookies(self, cookies):
        self.cookies += cookies

    async def add_init_script(self, script):
        self.scripts.append(script)


def with_sessions(node) -> dict:
    """The lane manager's live sessions, by lane, and every lane asked for."""
    lanes = node["rt"].get("lanes")
    sessions: dict = {}
    asked: list[str] = []
    listeners: list = []

    def live(lane_id):
        asked.append(lane_id)
        return sessions.get(lane_id)

    lanes.live_session = live
    lanes.add_session_listener = listeners.append
    return {"sessions": sessions, "asked": asked, "listeners": listeners}


async def test_sign_ins_are_saved_sealed_and_come_back(node):
    lanes = with_sessions(node)
    out = await opened(node, keep="save")
    lanes["sessions"][f"env/{out['id']}-browser"] = SimpleNamespace(context=FakeContext(STATE))
    lanes["sessions"]["shop/main"] = SimpleNamespace(context=FakeContext({"cookies": [{"name": "owner"}]}))
    closed = await node["call"]("environment_close")
    m, path = node["mgr"].saves().find(closed["saved"]["save_id"])
    assert m.signins == 1 and m.keep_signins
    assert "cookie-secret-1" not in (path / "manifest.json").read_text()
    assert b"cookie-secret-1" not in (path / signins.PAYLOAD).read_bytes()  # sealed with the saves key
    with tempfile.TemporaryDirectory() as t:
        files = open_payload(m, path, Path(t), await node["mgr"]._saves_key(create=False))
        kept = json.loads(files[signins.PAYLOAD].read_text())
    assert kept["cookies"][0]["value"] == "cookie-secret-1" and "junk" not in kept["cookies"][0]
    assert [o["origin"] for o in kept["origins"]] == ["http://localhost:3000"]  # never a file: origin
    assert set(lanes["asked"]) <= {f"env/{out['id']}-browser"}  # no other lane, the owner's included

    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    new_lane = f"env/{again['id']}-browser"
    assert again["resumed"]["signins"].startswith("the browser's sign-ins are back (1 cookie")
    # the browser was not up yet: they wait for it, and the node's browser gets them when it starts
    assert signins.pending(node["mgr"], again["id"]) == kept
    ctx = FakeContext()
    for listener in lanes["listeners"]:
        await listener(new_lane, SimpleNamespace(context=ctx))
    assert ctx.cookies == kept["cookies"] and "ls-secret" in ctx.scripts[0]
    assert signins.pending(node["mgr"], again["id"]) is None  # once: the profile keeps them now


async def test_sign_ins_opt_out_per_environment(node):
    lanes = with_sessions(node)
    out = await opened(node, keep="save", signins=False)
    lanes["sessions"][f"env/{out['id']}-browser"] = SimpleNamespace(context=FakeContext(STATE))
    closed = await node["call"]("environment_close")
    m, path = node["mgr"].saves().find(closed["saved"]["save_id"])
    assert m.signins == 0 and m.keep_signins is False and not (path / signins.PAYLOAD).exists()
    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    assert "opted out" in again["resumed"]["signins"]
    assert node["mgr"].get(again["id"]).keep_signins is False  # the resume keeps the choice


async def test_a_vm_browser_opens_with_the_newest_sign_ins_and_keeps_them_on_close(node):
    mgr = node["mgr"]
    out = await opened(node, keep="save")
    env = mgr.get(out["id"])
    assert signins.for_launch(mgr, env) is None  # a fresh context
    signins.remember(mgr, env.id, STATE)  # a lane closed while idle, or a resume
    assert signins.for_launch(mgr, env)["cookies"][0]["name"] == "sid"
    mgr.store.update(env.id, lambda e: setattr(e, "keep_signins", False))
    assert signins.for_launch(mgr, mgr.get(env.id)) is None
    await node["call"]("environment_close", discard=True)
    assert signins.pending(mgr, env.id) is None  # the node keeps no copy past the close


def test_clean_keeps_only_what_playwright_reads():
    assert signins.clean(None) is None and signins.clean({"cookies": [], "origins": []}) is None
    got = signins.clean({"cookies": [{"name": "a", "value": "b", "domain": "x.test", "sameSite": "weird"},
                                     {"name": "no-domain", "value": "v"}]})
    assert got == {"cookies": [{"name": "a", "value": "b", "domain": "x.test"}], "origins": []}


# ------------------------------------------------------------------ 5. a port on the person's computer


async def fake_socks(path: str, upstream_port: int) -> asyncio.AbstractServer:
    """ssh -D's side: SOCKS5 without auth, CONNECT to 127.0.0.1:<the asked port> mapped to `upstream_port`."""
    asked: list[int] = []

    async def client(r, w):
        await r.readexactly(3)
        w.write(b"\x05\x00")
        head = await r.readexactly(4)
        await r.readexactly(4 if head[3] == 1 else 16)
        (port,) = struct.unpack(">H", await r.readexactly(2))
        asked.append(port)
        up_r, up_w = await asyncio.open_connection("127.0.0.1", upstream_port)
        w.write(b"\x05\x00\x00\x01" + bytes(4) + b"\x00\x00")
        await w.drain()
        await asyncio.gather(ports._pipe(r, up_w), ports._pipe(up_r, w))

    server = await asyncio.start_unix_server(client, path)
    server.asked = asked  # type: ignore[attr-defined]
    return server


async def dev_server() -> asyncio.AbstractServer:
    async def answer(r, w):
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        await w.drain()
        w.close()

    return await asyncio.start_server(answer, "127.0.0.1", 0)


@pytest.fixture
async def proxied(node):
    """The environment's tunnel with a SOCKS socket that reaches a dev server here."""
    folder = tempfile.mkdtemp(prefix="ag-", dir="/tmp")
    dev = await dev_server()
    socks = await fake_socks(os.path.join(folder, "socks"), dev.sockets[0].getsockname()[1])
    out = await opened(node)
    tunnel = node["mgr"].tunnel_of(out["id"])
    tunnel.socks_path = os.path.join(folder, "socks")
    yield {"out": out, "socks": socks}
    socks.close()
    dev.close()


async def test_environment_port_on_a_laptop_gives_a_local_url_that_reaches_the_machine(node, proxied):
    node["mgr"].on_laptop = True
    got = await node["call"]("environment_port", port=3000)
    assert got["url"] == f"http://127.0.0.1:{got['local_port']}/" and "tunnel" not in got
    assert got["listening"] is True and got["url"] in got["next"]
    async with httpx.AsyncClient() as http:
        resp = await http.get(got["url"])
    assert resp.text == "hello" and proxied["socks"].asked == [3000]
    again = await node["call"]("environment_port", port=3000)
    assert again["local_port"] == got["local_port"]  # the same forward
    env = await node["call"]("environment")
    assert [p["link"] for p in env["ports"] if p["port"] == 3000] == [got["url"]]
    await node["call"]("environment_close")
    with pytest.raises(httpx.ConnectError):  # it closed with the environment
        async with httpx.AsyncClient() as http:
            await http.get(got["url"])


async def test_environment_port_on_a_server_node_gives_the_ssh_line(node, proxied, monkeypatch):
    monkeypatch.setenv("AGENTOS_SSH_HOST", "my-node")
    got = await node["call"]("environment_port", port=4000)
    p = got["local_port"]
    assert got["tunnel"] == f"ssh -N -L {p}:127.0.0.1:{p} my-node" and got["tunnel"] in got["next"]
    assert got["listening"] is False and "Nothing listens on port 4000" in got["next"]


async def test_environment_port_refuses_odd_ports(node, proxied):
    for bad in (22, 3129, 0, 70000):
        with pytest.raises(ToolCallError):
            await node["call"]("environment_port", port=bad)


# ------------------------------------------------------------------ 6. a real pull request


class FakeGitHub:
    """api.github.com for one repository: GET the repo, POST and GET pulls. Never a merge."""

    def __init__(self, *, exists: bool = False, status: int = 201):
        self.calls: list[tuple[str, str, dict]] = []
        self.exists, self.status = exists, status

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.calls.append((request.method, request.url.path, {**body, "auth": request.headers.get("authorization"),
                                                               "query": dict(request.url.params)}))
        assert "merge" not in request.url.path
        if request.method == "GET" and request.url.path == "/repos/me/shop":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.method == "POST" and request.url.path == "/repos/me/shop/pulls":
            if self.exists:
                return httpx.Response(422, json={"message": "Validation Failed",
                                                 "errors": [{"message": "A pull request already exists for me:sale."}]})
            if self.status != 201:
                return httpx.Response(self.status, json={"message": "Bad credentials tok-secret"})
            return httpx.Response(201, json={"html_url": "https://github.com/me/shop/pull/7", "number": 7,
                                             "state": "open", "draft": body.get("draft", False)})
        if request.method == "GET" and request.url.path == "/repos/me/shop/pulls":
            return httpx.Response(200, json=[{"html_url": "https://github.com/me/shop/pull/3", "number": 3,
                                              "state": "open", "draft": False}])
        return httpx.Response(404, json={"message": "Not Found"})


class FakeGateway:
    def __init__(self, creds, secrets):
        self.creds, self.secrets, self.read = creds, secrets, []

    async def credentials(self):
        return self.creds

    async def _read_secret(self, ref, purpose):
        self.read.append((ref, purpose))
        return self.secrets.get(ref)


def cred(ref, kind=keymod.TOKEN_KIND, repos=("me/shop",), any_repo=False):
    return keymod.Credential(ref=ref, kind=kind, host="github.com", repos=tuple(repos), label="", any_repo=any_repo)


def fake_mgr(github: FakeGitHub, creds, secrets, pushed=("sale",)):
    gw = FakeGateway(creds, secrets)
    mgr = SimpleNamespace(github_transport=httpx.MockTransport(github.handler), gateway=gw,
                          _get=lambda name: SimpleNamespace(gateway=gw) if name == "git" else None,
                          push_lists=lambda env: {"pushed": [{"branch": b} for b in pushed], "deleted": []})
    env = SimpleNamespace(repo="github.com/me/shop", own_branches=["sale"])
    return mgr, env


async def test_pull_request_opens_with_the_repos_token_into_the_default_branch():
    gh = FakeGitHub()
    mgr, env = fake_mgr(gh, [cred("git:shop-deploy", kind=keymod.SSH_KIND), cred("git:shop-token")],
                        {"git:shop-token": "tok-secret"})
    got = await pulls.open_pull_request(mgr, env, title="Thirty percent off", body="Tested: npm test", draft=True)
    assert got == {"url": "https://github.com/me/shop/pull/7", "number": 7, "branch": "sale", "base": "main",
                   "draft": True, "state": "open", "created": True, "repo": "github.com/me/shop"}
    post = next(c for c in gh.calls if c[0] == "POST")[2]
    assert post["head"] == "sale" and post["base"] == "main" and post["auth"] == "Bearer tok-secret"
    assert mgr.gateway.read == [("git:shop-token", "open a pull request on github.com/me/shop")]  # never the key


async def test_pull_request_uses_the_github_login_when_the_repo_has_no_token():
    gh = FakeGitHub()
    mgr, env = fake_mgr(gh, [cred(login.REF, repos=(), any_repo=True)], {login.REF: "gho_login"})
    got = await pulls.open_pull_request(mgr, env, title="Fix", base="develop")
    assert got["base"] == "develop" and [c[0] for c in gh.calls] == ["POST"]


async def test_pull_request_returns_the_open_one():
    gh = FakeGitHub(exists=True)
    mgr, env = fake_mgr(gh, [cred("git:shop-token")], {"git:shop-token": "tok"})
    got = await pulls.open_pull_request(mgr, env, title="Fix")
    assert got["url"] == "https://github.com/me/shop/pull/3" and got["created"] is False
    assert gh.calls[-1][2]["query"] == {"head": "me:sale", "state": "open"}


async def test_pull_request_only_for_its_own_branches_and_with_a_token():
    gh = FakeGitHub()
    mgr, env = fake_mgr(gh, [cred("git:shop-token")], {"git:shop-token": "tok"})
    with pytest.raises(PolicyDenied):
        await pulls.open_pull_request(mgr, env, title="Fix", branch="main")
    mgr, env = fake_mgr(gh, [cred("git:shop-token")], {"git:shop-token": "tok"}, pushed=("someone-else",))
    env.own_branches = []
    with pytest.raises(PolicyDenied):
        await pulls.open_pull_request(mgr, env, title="Fix")
    mgr, env = fake_mgr(gh, [cred("git:shop-deploy", kind=keymod.SSH_KIND)], {})
    with pytest.raises(pulls.PullRequestFailed) as e:
        await pulls.open_pull_request(mgr, env, title="Fix")
    assert "agentos setup github" in e.value.message and gh.calls == []
    env.repo = "gitlab.com/me/shop"
    with pytest.raises(InvalidRequest):
        await pulls.open_pull_request(mgr, env, title="Fix")


async def test_a_refused_pull_request_never_shows_the_token():
    gh = FakeGitHub(status=401)
    mgr, env = fake_mgr(gh, [cred("git:shop-token")], {"git:shop-token": "tok-secret"})
    with pytest.raises(pulls.PullRequestFailed) as e:
        await pulls.open_pull_request(mgr, env, title="Fix", base="main")
    assert "401" in e.value.message and "tok-secret" not in e.value.message


async def test_environment_pull_request_tool_records_the_pull_request(node):
    gh = FakeGitHub()
    out = await opened(node)
    mgr = node["mgr"]

    def on_github(e):
        e.repo, e.own_branches = "github.com/me/shop", ["sale"]

    mgr.store.update(out["id"], on_github)
    mgr.github_transport = httpx.MockTransport(gh.handler)
    gw = FakeGateway([cred("git:shop-token")], {"git:shop-token": "tok"})
    node["rt"].register("git", SimpleNamespace(gateway=gw))
    got = await node["call"]("environment_pull_request", title="Thirty percent off", branch="sale")
    assert got["url"] == "https://github.com/me/shop/pull/7" and got["url"] in got["next"]
    env = await node["call"]("environment")
    assert env["git"]["pull_requests"] == [{"url": got["url"], "number": 7, "branch": "sale", "base": "main"}]
    assert any(c["tool"] == "environment_pull_request" and c["ok"] for c in env["calls"])
