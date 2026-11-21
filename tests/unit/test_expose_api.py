"""/api/exposes (openajar.node.expose.api, mounted by openajar.api.mounts): the owner's computer (the
device credential) and a person may open an expose or a forward; an agent's grant, an admin grant and
the node token may not, and no MCP tool can. Starting it is in the action log; a lease renewal is not.

And `openajar expose` and `openajar forward` on the owner's computer (openajar.client.expose), with a
fake node panel and a fake ssh."""

from __future__ import annotations

import io
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_node_panel import TOKEN, Node
from test_presence import Device, Token

from openajar.client.errors import ClientError
from openajar.client.expose import READY_LINE, LinkRunner, hold_script, local_args, parse_for, reverse_args
from openajar.client.owner import PanelError
from openajar.core.ratelimit import RateLimiter
from openajar.node.expose.service import ExposeService

LANE = "biz-a/instagram"
EM_DASH = "—"


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)
    home = Path(tempfile.mkdtemp(prefix="ax", dir="/tmp"))
    n.expose = ExposeService(n.runtime, home=home)
    n.runtime.register("expose", n.expose)
    yield n
    n.close()
    shutil.rmtree(home, ignore_errors=True)


def body(**kw):
    return {"kind": "expose", "lane": LANE, "name": "myapp", "port": 5173, **kw}


@pytest.mark.parametrize("who", ["agent_grant", "admin_grant", "node_token"])
def test_no_credential_an_agent_can_hold_opens_an_expose_or_a_forward(node, who):
    token = {"node_token": TOKEN,
             "admin_grant": node.grant(subject={"id": "boss-agent"}, admin=True, workspaces=["*"]),
             "agent_grant": node.grant(subject={"id": "claude-code@mac"}, ceiling="commit")}[who]
    caller = Token(node, token)
    for method, url, payload in [("POST", "/api/exposes", body()),
                                 ("POST", "/api/exposes", {"kind": "forward", "lane": "dev/web", "port": 3000}),
                                 ("GET", "/api/exposes", None)]:
        r = caller.req(method, url, payload)
        assert r.status_code == 403 and EM_DASH not in r.text, (who, url, r.text)
        assert r.json()["error"]["code"] in ("presence_required", "denied"), r.text
    assert node.expose.links == {}


def test_the_owners_computer_opens_one_and_it_is_in_the_action_log(node):
    device = Device(node)
    r = device.req("POST", "/api/exposes", body(ttl_s=600))
    assert r.status_code == 200, r.text
    link = r.json()
    assert link["host"] == "myapp.expose.openajar.invalid" and link["state"] == "waiting"
    assert link["by"].endswith("on macbook") and link["socket"].endswith(".sock")
    assert device.req("GET", "/api/exposes").json()["links"][0]["id"] == link["id"]
    hb = device.req("POST", f"/api/exposes/{link['id']}/heartbeat")
    assert hb.status_code == 200, hb.text
    r = device.req("POST", f"/api/exposes/{link['id']}/ready")  # sshd never bound the socket
    assert r.status_code == 400 and "not on the node yet" in r.text
    r = device.client.request("DELETE", f"/api/exposes/{link['id']}", headers=device.h)
    assert r.status_code == 200 and r.json()["state"] == "ended"
    gone = device.req("POST", f"/api/exposes/{link['id']}/heartbeat")
    assert gone.status_code == 404 and "owner ended it" in gone.text
    tools = [t.tool for t in node.runtime.traces.query(limit=100)[0]]
    assert "panel.expose.create" in tools and "panel.expose.end" in tools
    assert "panel.expose.heartbeat" not in tools


def test_bad_requests_say_what_to_send(node):
    device = Device(node)
    for payload, words in [({"kind": "tunnel", "lane": LANE}, "kind must be"),
                           ({"kind": "expose", "lane": "nolane"}, ""),
                           (body(name="Bad Name!"), "lowercase"),
                           (body(port="80"), "port must be"),
                           (body(lane="biz-a/nobody"), "")]:
        r = device.req("POST", "/api/exposes", payload)
        assert 400 <= r.status_code < 500 and EM_DASH not in r.text, (payload, r.text)
        assert words in r.text


def test_no_mcp_tool_opens_one(tmp_path):
    """Every tool this build serves (as `openajar tools` lists them): none exposes or forwards."""
    from openajar.core.config import NodeConfig
    from openajar.core.runtime import Runtime
    from openajar.core.tools import ToolSpec
    from openajar.mcp.providers import feature_providers, tool_providers
    from openajar.plugins.entries import PLUGINS

    runtime = Runtime(config=NodeConfig(home=tmp_path))
    providers = [*tool_providers(), *feature_providers(tuple(m for plugin in PLUGINS for m in plugin.tools))]
    tools = [spec.name for provider in providers for spec in provider(runtime) if isinstance(spec, ToolSpec)]
    assert len(tools) > 50 and not [t for t in tools if "expose" in t or "forward" in t]


# ---- the owner's computer ------------------------------------------------------------------

class FakePanel:
    def __init__(self, heartbeats: list | None = None) -> None:
        self.calls: list[tuple] = []
        self.heartbeats = list(heartbeats or [])

    def expose_create(self, device, body):
        self.calls.append(("create", body))
        return {"id": "l1", "socket": "/var/lib/agentos/run/expose/l1.sock", "owner_uid": 998,
                "url": "https://myapp.expose.openajar.invalid/", "expires_at": "2026-11-08T12:00:00Z",
                "heartbeat_s": 1, "lease_s": 30}

    def expose_ready(self, device, link_id):
        self.calls.append(("ready", link_id))
        return {"state": "active", "routed": True}

    def expose_heartbeat(self, device, link_id):
        self.calls.append(("heartbeat", link_id))
        if self.heartbeats:
            item = self.heartbeats.pop(0)
            if isinstance(item, BaseException):
                raise item
        return {"state": "active"}

    def expose_end(self, device, link_id):
        self.calls.append(("end", link_id))
        return {"state": "ended"}


class FakeProc:
    def __init__(self, lines: str = READY_LINE + "\n", exits_after: int | None = None) -> None:
        self.stdout = io.StringIO(lines)
        self.stderr = io.StringIO("")
        self.code: int | None = None
        self.polls = 0
        self.exits_after = exits_after
        self.terminated = False

    def poll(self):
        self.polls += 1
        if self.exits_after is not None and self.polls > self.exits_after:
            self.code = 255
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


def make_runner(panel: FakePanel, proc: FakeProc, out: list[str], started: list | None = None) -> LinkRunner:
    conn = SimpleNamespace(name="mynode", ssh_host="mynode", local_port=7400, local=False)
    t = [0.0]

    def popen(args, **kw):
        if started is not None:
            started.append(args)
        return proc

    def sleep(s):
        t[0] += s

    return LinkRunner(conn=conn, device="dev-cred", ssh="/usr/bin/ssh", panel=panel, out=out.append,
                      popen=popen, sleep=sleep, clock=lambda: t[0])


def test_expose_runs_ssh_r_says_ready_and_ends_when_its_time_is_up():
    panel = FakePanel([None, PanelError(404, "not_found", "it ended: its time is up")])
    out: list[str] = []
    started: list = []
    outcome = make_runner(panel, FakeProc(), out, started).expose(lane=LANE, name="myapp", port=5173, ttl_s=7200)
    assert outcome.code == 0 and "time is up" in outcome.message
    assert panel.calls[0] == ("create", {"kind": "expose", "lane": LANE, "name": "myapp", "port": 5173,
                                         "ttl_s": 7200})
    assert ("ready", "l1") in panel.calls
    args = started[0]
    assert "-R" in args and args[args.index("-R") + 1] == "/var/lib/agentos/run/expose/l1.sock:localhost:5173"
    assert "BatchMode=yes" in args and "ExitOnForwardFailure=yes" in args and "ControlPath=none" in args
    assert any("https://myapp.expose.openajar.invalid/ now opens your localhost:5173" in line for line in out)


def test_a_dropped_ssh_session_ends_it_on_the_node():
    panel = FakePanel()
    proc = FakeProc(exits_after=60)
    outcome = make_runner(panel, proc, []).expose(lane=LANE, name="myapp", port=5173, ttl_s=None)
    assert outcome.code == 1 and "SSH session closed" in outcome.message
    assert panel.calls[-1] == ("end", "l1") and "ttl_s" not in panel.calls[0][1]


def test_ctrl_c_ends_it_on_the_node_then_closes_ssh():
    panel = FakePanel([KeyboardInterrupt()])
    proc = FakeProc()
    outcome = make_runner(panel, proc, []).expose(lane=LANE, name="myapp", port=5173, ttl_s=None)
    assert outcome.code == 0 and outcome.message == "Ended."
    assert panel.calls[-1] == ("end", "l1") and proc.terminated


def test_a_node_that_stops_answering_ends_it_after_the_lease():
    down = PanelError(0, "unreachable", "the node did not answer")
    panel = FakePanel([down] * 1000)
    proc = FakeProc()
    outcome = make_runner(panel, proc, []).expose(lane=LANE, name="myapp", port=5173, ttl_s=None)
    assert outcome.code == 1 and "lease" in outcome.message and proc.terminated


def test_ssh_that_never_says_ready_fails_and_ends_the_reservation():
    panel = FakePanel()
    proc = FakeProc(lines="", exits_after=3)
    with pytest.raises(ClientError, match="did not start"):
        make_runner(panel, proc, []).expose(lane=LANE, name="myapp", port=5173, ttl_s=None)
    assert panel.calls[-1] == ("end", "l1") and ("ready", "l1") not in panel.calls


def test_the_remote_command_quotes_the_path_and_refuses_an_odd_one():
    script = hold_script("/var/lib/agentos/run/expose/ab12.sock", 998)
    assert "s=/var/lib/agentos/run/expose/ab12.sock;" in script and "chown 998" in script
    assert READY_LINE in script and "sudo -n chown 998" in script
    for bad in ("/tmp/x.sock; rm -rf /", "relative.sock", "/a/../b.sock", "/a/b c.sock", "/a/$(id).sock"):
        with pytest.raises(ClientError):
            hold_script(bad, 998)
        with pytest.raises(ClientError):
            local_args("ssh", "h", 8080, bad)
    args = reverse_args("ssh", "mynode", "/srv/a/run/expose/x.sock", 5173, 0)
    assert args[-2] == "mynode" and args[args.index("-R") + 1] == "/srv/a/run/expose/x.sock:localhost:5173"
    assert local_args("ssh", "h", 8080, "/srv/a/x.sock")[-3:] == ["-L", "127.0.0.1:8080:/srv/a/x.sock", "h"]


def test_for_takes_a_short_duration():
    assert parse_for(None) is None and parse_for("90") == 90 and parse_for("30m") == 1800
    assert parse_for("2h") == 7200 and parse_for("1d") == 86400
    for bad in ("0", "2 hours", "-1h", "1w"):
        with pytest.raises(ClientError):
            parse_for(bad)


def test_forward_listens_on_localhost_only(monkeypatch):
    import openajar.client.expose as mod

    monkeypatch.setattr(mod, "pick_port", lambda preferred, reserved, explicit: preferred)
    monkeypatch.setattr(mod, "port_in_use", lambda port: True)
    panel = FakePanel([PanelError(404, "not_found", "it ended: the owner ended it")])
    started: list = []
    out: list[str] = []
    outcome = make_runner(panel, FakeProc(), out, started).forward(lane="dev/web", port=3000, local=None)
    assert panel.calls[0] == ("create", {"kind": "forward", "lane": "dev/web", "port": 3000})
    assert started[0][started[0].index("-L") + 1] == "127.0.0.1:3000:/var/lib/agentos/run/expose/l1.sock"
    assert outcome.code == 1 and any("http://localhost:3000 now reaches port 3000" in line for line in out)
