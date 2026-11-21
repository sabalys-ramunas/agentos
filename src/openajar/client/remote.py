"""Read a node's MCP port and token over SSH, without the token touching a command line.

The script goes to the node on stdin (`ssh <host> sh -s`), so the remote command line is
just `sh -s` and works whatever the login shell is. It prints only the `name`, `kind`,
`mcp_port` and `mcp_token` lines of node.toml; the reply comes back over the SSH pipe and
stays in this process's memory. The token is the node's own (the owner token): connect uses
it for a few calls through the tunnel (to issue the agent grant and the owner's device
credential) and never stores it. A controller's connect stub says `kind = "controller"`; its
`mcp_token` is already an agent credential.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field

from openajar.client.errors import NodeConfigError, NodeMissing, SshError, scrub

TOKEN_RE = re.compile(r"^[A-Za-z0-9_.~+/=-]{16,2048}$")  # a grant token is about 650 characters
HOST_RE = re.compile(r"^[A-Za-z0-9_.@:%\[\]-]{1,255}$")
SSH_TIMEOUT = 45
CONTROLLER_TOKEN_PREFIX = "aga_"  # openajar.controller: its agent token

# Only top-level keys, before the first [table], so a `name` inside a table is never picked up.
SCRIPT = """\
d={home}
f="$d/node.toml"
can_sudo() {{ command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; }}
if [ -r "$f" ]; then
  c=$(cat "$f") || exit 3
elif can_sudo && sudo -n test -r "$f"; then
  c=$(sudo -n cat "$f") || exit 3
elif [ -e "$f" ] || {{ [ -d "$d" ] && [ ! -x "$d" ]; }}; then
  echo "openajar-error=unreadable $(id -un)"
  exit 0
else
  echo "openajar-error=missing"
  exit 0
fi
printf '%s\\n' "$c" | sed -n -E -e '/^[[:space:]]*\\[/q' \\
  -e '/^[[:space:]]*(name|kind|mcp_port|mcp_token)[[:space:]]*=/p'
"""


@dataclass(frozen=True)
class NodeSettings:
    mcp_port: int
    token: str = field(repr=False)
    name: str | None = None
    kind: str = "node"  # "controller": the token is the controller's agent token, not an owner token

    @property
    def controller(self) -> bool:
        return self.kind == "controller" or self.token.startswith(CONTROLLER_TOKEN_PREFIX)


def valid_host(host: str) -> str:
    if not HOST_RE.match(host) or host.startswith("-"):
        raise SshError(f"{host!r} is not an SSH host", "pass a host from your ~/.ssh/config, for example mynode")
    return host


def remote_script(remote_home: str) -> str:
    if not remote_home.startswith("/") or "\n" in remote_home:
        raise NodeConfigError(f"--remote-home must be an absolute path, got {remote_home!r}")
    return SCRIPT.format(home=shlex.quote(remote_home.rstrip("/") or "/"))


def ssh_read_command(ssh: str, host: str) -> list[str]:
    return [ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-T", host, "sh", "-s"]


def explain_ssh_failure(host: str, stderr: str) -> SshError:
    text = stderr.strip()
    last = text.splitlines()[-1] if text else "no error message"
    lowered = text.lower()
    if "could not resolve hostname" in lowered or "name or service not known" in lowered:
        return SshError(f"SSH does not know the host {host!r}",
                        "check the spelling, or add a Host entry for it to ~/.ssh/config")
    if "host key verification failed" in lowered or "remote host identification has changed" in lowered:
        return SshError(f"SSH has not accepted {host}'s host key",
                        f"run `ssh {host}` once in a terminal, check the key and accept it")
    if "permission denied" in lowered or "too many authentication failures" in lowered:
        return SshError(f"{host} refused the SSH login",
                        "openajar logs in with keys only (no password prompts): load your key with ssh-add "
                        "or add it to the node's authorized_keys")
    unreachable = ("timed out", "no route to host", "network is unreachable", "connection refused",
                   "connection closed", "connection reset", "host is down")
    if any(word in lowered for word in unreachable):
        return SshError(f"cannot reach {host}: {last}",
                        "check that the node is up and your network works, then run connect again")
    return SshError(f"ssh {host} failed: {last}")


def parse_reply(host: str, remote_home: str, stdout: str) -> NodeSettings:
    lines = stdout.splitlines()
    for line in lines:
        if line.startswith("openajar-error=missing"):
            raise NodeMissing(
                f"{host} has no {remote_home}/node.toml",
                "install openajar there and run `openajar node init` (or pass --remote-home "
                "if OPENAJAR_HOME is elsewhere)")
        if line.startswith("openajar-error=unreadable"):
            user = line.partition(" ")[2] or "this user"
            raise NodeConfigError(
                f"{remote_home}/node.toml on {host} is not readable as {user}",
                "connect as a user who can read it (root on a standard install), or allow `sudo -n cat` for it")
    try:
        data = tomllib.loads("\n".join(line for line in lines if not line.startswith("openajar-")))
    except tomllib.TOMLDecodeError:
        raise NodeConfigError(f"could not parse node.toml from {host}") from None
    token = data.get("mcp_token")
    if not isinstance(token, str) or not token:
        raise NodeConfigError(f"node.toml on {host} has no mcp_token", "run `openajar node init` on the node")
    if not TOKEN_RE.match(token):
        raise NodeConfigError(f"the mcp_token in node.toml on {host} has characters openajar does not expect",
                              "run `openajar node init --new-token` on the node and connect again")
    port = data.get("mcp_port", 7400)
    if not isinstance(port, int) or not 0 < port < 65536:
        raise NodeConfigError(f"node.toml on {host} has an invalid mcp_port: {port!r}")
    name = data.get("name") if isinstance(data.get("name"), str) else None
    kind = "controller" if data.get("kind") == "controller" else "node"
    return NodeSettings(mcp_port=port, token=token, name=name, kind=kind)


def read_node(ssh: str, host: str, remote_home: str, timeout: float = SSH_TIMEOUT) -> NodeSettings:
    """Run the read script on the node. Raises SshError or NodeConfigError with a hint."""
    script = remote_script(remote_home)
    try:
        proc = subprocess.run(ssh_read_command(ssh, valid_host(host)), check=False, input=script, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SshError(f"cannot reach {host}: no answer in {int(timeout)} seconds",
                       "check that the node is up, then run connect again") from None
    except OSError as exc:
        raise SshError(f"could not run ssh: {exc}") from None
    if proc.returncode == 255:
        raise explain_ssh_failure(host, proc.stderr)
    if proc.returncode != 0:
        detail = scrub(proc.stderr.strip(), *re.findall(r"[A-Za-z0-9_-]{32,}", proc.stdout))
        raise NodeConfigError(f"reading node.toml on {host} failed (exit {proc.returncode})"
                              + (f": {detail.splitlines()[-1]}" if detail else ""))
    return parse_reply(host, remote_home, proc.stdout)
