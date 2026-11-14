"""The client's parts on their own: the remote script, error mapping, the MCP client, quoting."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import typer
from test_client_fakes import NodeServer
from typer.testing import CliRunner

from agentos.client import app as client_app
from agentos.client.errors import NodeConfigError, PortInUse, SshError, TokenStoreError
from agentos.client.mcp_http import Failure, McpError, McpSession, ToolError, probe
from agentos.client.ports import pick_port
from agentos.client.remote import explain_ssh_failure, parse_reply, remote_script, valid_host
from agentos.client.state import Paths, default_name, valid_name
from agentos.client.tokens import KeychainStore, helper_script
from agentos.client.tunnel import ServiceState, TunnelSpec, systemd_quote
from agentos.core.config import NodeConfig, save_config

TOKEN = "Tk_" + "q1W2e3R4t5" * 4


# ---- names and hosts ----


@pytest.mark.parametrize(("host", "name"), [
    ("mynode", "mynode"),
    ("root@192.0.2.10", "192-0-2-10"),
    ("Node.Example.COM", "node-example-com"),
    ("@@@", "node"),
])
def test_default_name(host, name):
    assert default_name(host) == name
    assert valid_name(default_name(host))


@pytest.mark.parametrize("host", ["-oProxyCommand=evil", "a b", "host;rm", "", "x" * 256])
def test_rejects_hosts_that_could_be_ssh_options_or_shell(host):
    with pytest.raises(SshError):
        valid_host(host)


# ---- reading node.toml on the node ----


def run_script(home: Path, remote_home: str | None = None) -> subprocess.CompletedProcess:
    script = remote_script(remote_home or str(home))
    return subprocess.run(["/bin/sh", "-s"], input=script, capture_output=True, text=True, check=False,
                          env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})


def test_script_prints_only_the_three_top_level_keys(tmp_path):
    save_config(NodeConfig(home=tmp_path, name="node-7", mcp_port=7431, mcp_token=TOKEN, ssh_host="secret-host"))
    with (tmp_path / "node.toml").open("a") as f:
        f.write('\n[vault]\nname = "not-the-node"\nmcp_token = "nope"\n')
    proc = run_script(tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert sorted(line.split(" = ")[0] for line in lines) == ["mcp_port", "mcp_token", "name"]
    assert "secret-host" not in proc.stdout and "not-the-node" not in proc.stdout
    node = parse_reply("mynode", str(tmp_path), proc.stdout)
    assert (node.name, node.mcp_port, node.token) == ("node-7", 7431, TOKEN)
    assert TOKEN not in repr(node)


def test_script_reports_missing_and_unreadable(tmp_path):
    proc = run_script(tmp_path / "nowhere")
    assert proc.stdout.strip() == "agentos-error=missing"
    save_config(NodeConfig(home=tmp_path, mcp_token=TOKEN))
    (tmp_path / "node.toml").chmod(0)
    passwordless_sudo = subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode == 0 \
        if shutil.which("sudo", path="/usr/bin:/bin") else False
    if os.access(tmp_path / "node.toml", os.R_OK) or passwordless_sudo:
        # root reads mode 000 anyway, and the script rightly uses passwordless sudo (CI runners have it)
        (tmp_path / "node.toml").chmod(0o600)
        return
    try:
        proc = run_script(tmp_path)
    finally:
        (tmp_path / "node.toml").chmod(0o600)
    assert proc.stdout.startswith("agentos-error=unreadable ")


def test_remote_home_is_quoted(tmp_path):
    odd = tmp_path / "with space; and $(dollar)"
    save_config(NodeConfig(home=odd, mcp_token=TOKEN, mcp_port=7999))
    assert parse_reply("h", str(odd), run_script(odd).stdout).mcp_port == 7999
    with pytest.raises(NodeConfigError):
        remote_script("relative/path")


@pytest.mark.parametrize(("stdout", "message"), [
    ("agentos-error=missing\n", "has no /var/lib/agentos/node.toml"),
    ("agentos-error=unreadable bob\n", "not readable as bob"),
    ('name = "n"\nmcp_port = 7400\n', "has no mcp_token"),
    ('mcp_token = "has spaces and \\"quotes\\" in it"\n', "does not expect"),
    (f'mcp_token = "{TOKEN}"\nmcp_port = 99999\n', "invalid mcp_port"),
    ("this is = not toml [\n", "could not parse"),
])
def test_parse_reply_errors(stdout, message):
    with pytest.raises(NodeConfigError, match=message.replace("(", r"\(")) as info:
        parse_reply("mynode", "/var/lib/agentos", stdout)
    assert TOKEN not in str(info.value.message) + str(info.value.hint)


@pytest.mark.parametrize(("stderr", "message"), [
    ("ssh: Could not resolve hostname agentvn: nodename nor servname provided", "does not know the host"),
    ("Host key verification failed.", "host key"),
    ("root@1.2.3.4: Permission denied (publickey).", "refused the SSH login"),
    ("ssh: connect to host 1.2.3.4 port 22: Connection refused", "cannot reach"),
    ("ssh: connect to host 1.2.3.4 port 22: No route to host", "cannot reach"),
    ("kex_exchange_identification: something odd", "ssh mynode failed"),
])
def test_ssh_failures_are_explained(stderr, message):
    assert message in explain_ssh_failure("mynode", stderr).message


# ---- MCP over HTTP ----


@pytest.fixture
def server(tmp_path):
    srv = NodeServer(tmp_path / "node", TOKEN, name="units").start()
    yield srv
    srv.stop()


def test_session_reads_whoami_and_lanes(server):
    with McpSession(server.port, TOKEN) as session:
        assert session.server["name"] == "agentos"
        assert session.session_id
        who = session.call("whoami")
        assert who["node"] == "units" and who["lanes"]["total"] == 2
        lanes = session.call("lanes_list")["lanes"]
        assert {lane["id"] for lane in lanes} == {"personal/reddit", "biz-a/instagram"}
        with pytest.raises(ToolError, match="lane_not_found"):
            session.call("lane_view", {"on": "nobody/nothing"})


def test_wrong_token_is_unauthorized(server):
    with pytest.raises(McpError) as info:
        probe(server.port, "wrong-" + TOKEN)
    assert info.value.failure is Failure.UNAUTHORIZED


def test_closed_port_is_unreachable():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(McpError) as info:
        probe(port, TOKEN, timeout=2)
    assert info.value.failure is Failure.UNREACHABLE


def test_accept_then_close_is_no_answer():
    """What a tunnel looks like when agentosd is down on the node: ssh accepts, then hangs up."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]

    def hang_up() -> None:
        try:
            while True:
                conn, _ = listener.accept()
                conn.close()
        except OSError:  # the listener closed at the end of the test
            pass

    threading.Thread(target=hang_up, daemon=True).start()
    try:
        with pytest.raises(McpError) as info:
            probe(port, TOKEN, timeout=2)
        assert info.value.failure is Failure.NO_ANSWER
    finally:
        listener.close()


# ---- tunnel units, ports, the token store ----


def test_ssh_args_and_systemd_quoting():
    spec = TunnelSpec(name="n", ssh="/usr/bin/ssh", host="root@[fe80::1%en0]", local_port=7401, remote_port=7400)
    assert spec.ssh_args()[-3:] == ["-L", "7401:localhost:7400", "root@[fe80::1%en0]"]
    assert systemd_quote("/usr/bin/ssh") == "/usr/bin/ssh"
    assert systemd_quote("fe80::1%en0") == "fe80::1%%en0"
    assert systemd_quote("/path with space/ssh") == '"/path with space/ssh"'
    assert systemd_quote("$HOME") == "$$HOME"
    assert systemd_quote('a"b') == '"a\\"b"'


def test_service_state_labels():
    assert ServiceState(installed=True, loaded=True, running=True).label == "running"
    assert ServiceState(installed=True, loaded=True, running=False, detail="last exit 255").label \
        == "stopped (last exit 255)"
    assert ServiceState(installed=True, loaded=False, running=False).label == "not loaded"


def test_pick_port_skips_reserved_and_busy_ports():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert pick_port(taken, set(), explicit=False) != taken
        with pytest.raises(PortInUse):
            pick_port(taken, set(), explicit=True)
    free = pick_port(taken, {taken}, explicit=False)
    assert free != taken


def test_keychain_errors_never_echo_the_token(tmp_path):
    loud = tmp_path / "security"
    loud.write_text("#!/bin/sh\ncat >&2\necho 'User interaction is not allowed.' >&2\nexit 51\n")
    loud.chmod(0o755)
    with pytest.raises(TokenStoreError) as info:
        KeychainStore(str(loud)).put("mynode", TOKEN)
    assert TOKEN not in info.value.message
    assert "--token-store file" in (info.value.hint or "")


def test_helper_script_reads_the_file_then_the_keychain(tmp_path):
    paths = Paths(tmp_path)
    with_keychain = helper_script(paths, "/usr/bin/security")
    assert "/usr/bin/security find-generic-password -s agentos -a \"$name\" -w" in with_keychain
    assert "security" not in helper_script(paths, None)
    script = tmp_path / "helper"
    script.write_text(helper_script(paths, None))
    script.chmod(0o700)
    bad = subprocess.run([str(script), "../etc"], capture_output=True, text=True, check=False)
    assert bad.returncode == 2


def test_xdg_config_home_is_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    paths = Paths(tmp_path / "home")
    assert paths.connections_dir == tmp_path / "xdg/agentos/connections"
    assert paths.systemd_user_dir == tmp_path / "xdg/systemd/user"


# ---- packaging ----


def test_mounts_at_the_top_level_of_a_typer_app():
    main = typer.Typer()

    @main.command()
    def version() -> None:
        typer.echo("v")

    main.add_typer(client_app)
    out = CliRunner().invoke(main, ["--help"]).output
    for command in ("connect", "disconnect", "nodes", "status", "version"):
        assert command in out


def test_client_imports_stay_light():
    code = ("import sys, agentos.client.cli; "
            "print(sorted(m for m in ('mcp', 'uvicorn', 'camoufox', 'playwright', 'httpx2') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


# ---- the Claude Code integration ----

INTEGRATION = Path(__file__).resolve().parents[2] / "integrations" / "claude-code"
SKILLS = Path(__file__).resolve().parents[2] / "src" / "agentos" / "client" / "skills"


def frontmatter(path: Path) -> dict[str, str]:
    head = path.read_text().split("---\n")[1]
    return dict(line.split(": ", 1) for line in head.strip().splitlines())


def test_skills_name_the_tools_they_rely_on():
    offload = SKILLS / "agentos-offload/SKILL.md"
    lanes = SKILLS / "agentos-lanes/SKILL.md"
    meta = frontmatter(offload)
    assert meta["name"] == "agentos-offload" and meta["disable-model-invocation"] == "true"
    assert frontmatter(lanes)["name"] == "agentos-lanes"
    text = offload.read_text()
    for word in ("brain_run", '"prompt"', '"workspace"', '"lanes"', '"context_bundle"', '"git"', '"remote"',
                 '"ref"', '"files"', '"summary"', "brain_status", "brain_tail", "brain_tasks", "agentos connect"):
        assert word in text
    text = lanes.read_text()
    for word in ("browser_snapshot", "browser_act", "lane_view", "lane_request_control", "lane_wait_control",
                 "human_in_control", "agent_paused", "Read back"):
        assert word in text
    for path in [*INTEGRATION.rglob("*"), *SKILLS.rglob("*")]:
        if path.is_file():
            assert "\u2014" not in path.read_text(), path


def test_install_script_copies_only_when_run(tmp_path):
    script = INTEGRATION / "install.sh"
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    dry = subprocess.run(["/bin/sh", str(script), "--dry-run"], env=env, capture_output=True, text=True, check=True)
    assert "would copy" in dry.stdout and not (tmp_path / ".claude").exists()

    subprocess.run(["/bin/sh", str(script)], env=env, capture_output=True, text=True, check=True)
    for skill in ("agentos-environment", "agentos-offload", "agentos-lanes", "agentos-studio"):
        installed = tmp_path / ".claude/skills" / skill / "SKILL.md"
        assert installed.read_text() == (SKILLS / skill / "SKILL.md").read_text()

    subprocess.run(["/bin/sh", str(script), "--uninstall"], env=env, capture_output=True, text=True, check=True)
    assert list((tmp_path / ".claude/skills").iterdir()) == []

    other = tmp_path / "claude-config"
    subprocess.run(["/bin/sh", str(script)], env={**env, "CLAUDE_CONFIG_DIR": str(other)}, capture_output=True,
                   text=True, check=True)
    assert (other / "skills/agentos-lanes/SKILL.md").exists()


def test_a_refusal_carries_the_nodes_reason():
    """A 409 frozen (or any refusal with the shared error body) reaches the caller as its code and fix,
    not a bare HTTP status."""
    import http.server
    import json as _json
    import threading

    class Refuse(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # the stdlib calls it by this name
            self.rfile.read(int(self.headers.get("content-length") or 0))
            body = _json.dumps({"error": {"code": "frozen", "message": "the owner froze biz-a/social",
                                          "fix": "ask the owner to unfreeze it"}}).encode()
            self.send_response(409)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Refuse)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with pytest.raises(McpError) as info:
            probe(httpd.server_address[1], TOKEN, timeout=5)
        assert "frozen: the owner froze biz-a/social" in info.value.detail
        assert "ask the owner to unfreeze it" in info.value.detail
    finally:
        httpd.shutdown()


# ---- Codex config.toml, the skills and the bridge, without a node ----


def test_codex_editing_keeps_the_owners_file_as_it_was(tmp_path):
    import tomllib

    from agentos.client.codex import CodexConfig

    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    before = 'model = "x"\r\n[mcp_servers.other]\r\ncommand = "o"\r\n'
    real.write_bytes(before.encode())
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").symlink_to(real)
    cfg = CodexConfig(home)
    assert cfg.register("agentos-n", "/bin/agentos", ["mcp-bridge", "n"])
    assert (home / ".codex/config.toml").is_symlink()  # the dotfiles copy is what changed
    text = real.read_bytes().decode()
    assert "\n" not in text.replace("\r\n", "")  # still CRLF throughout
    assert tomllib.loads(text)["mcp_servers"]["agentos-n"]["args"] == ["mcp-bridge", "n"]
    assert not cfg.register("agentos-n", "/bin/agentos", ["mcp-bridge", "n"])
    assert cfg.unregister("agentos-n") and real.read_bytes().decode() == before

    # a comment the owner wrote after our table, at the end of the file, survives both ways
    real.write_text('[mcp_servers.agentos-n]\ncommand = "a"\n# mine: keep\n')
    cfg.register("agentos-n", "/bin/agentos", ["mcp-bridge", "n"])
    assert "# mine: keep" in real.read_text()
    cfg.unregister("agentos-n")
    assert real.read_text().strip() == "# mine: keep"


def test_skills_never_overwrite_or_remove_the_owners_own(tmp_path):
    from agentos.client import skills

    mine = tmp_path / ".claude/skills/agentos-lanes/SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("---\nname: my-lanes\n---\nmy own notes\n")
    assert skills.install(tmp_path) == "kept your own agentos-lanes"
    assert mine.read_text() == "---\nname: my-lanes\n---\nmy own notes\n"
    assert skills.remove(tmp_path) == ["agentos-environment", "agentos-offload", "agentos-studio"] and mine.exists()


def test_the_bridge_always_answers(tmp_path):
    import io
    import json as _json
    import socket

    from agentos.client.bridge import UNAUTHORIZED, UNREACHABLE, Bridge

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    reopened = []
    out = io.BytesIO()
    bridge = Bridge(name="n", host="127.0.0.1", port=port, path="/mcp", token=lambda: "t" * 20, out=out,
                    reopen=lambda: reopened.append(1))
    import agentos.client.bridge as bridge_mod
    bridge_mod.TUNNEL_WAIT, old = 1.0, bridge_mod.TUNNEL_WAIT
    try:
        bridge.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    finally:
        bridge_mod.TUNNEL_WAIT = old
    answer = _json.loads(out.getvalue())
    assert reopened == [1] and answer["error"]["code"] == UNREACHABLE and "tunnel did not come back" in \
        answer["error"]["message"]

    out2 = io.BytesIO()
    empty = Bridge(name="n", host="127.0.0.1", port=port, path="/mcp", token=lambda: None, out=out2)
    empty.forward({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert _json.loads(out2.getvalue())["error"]["code"] == UNAUTHORIZED

    out3 = io.BytesIO()
    Bridge(name="n", host="127.0.0.1", port=port, path="/mcp", token=lambda: "t" * 20, out=out3).handle(b"5")
    assert _json.loads(out3.getvalue())["error"]["code"] == -32600
