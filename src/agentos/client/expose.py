"""`agentos expose` and `agentos forward`, on the owner's computer.

expose: the owner's dev server (localhost:<port> here) in one browser lane on the node.

1. Ask the node (the owner's device credential, `POST /api/exposes`) for the lane's address
   `https://<name>.expose.agentos.invalid` and a socket path on the node.
2. `ssh -R <socket>:localhost:<port> <host>`, with the same host, key and options as the
   connection's tunnel (a second SSH session: the tunnel runs with `ControlPath=none`, so nothing
   can add a forward to it). A short remote command waits for sshd's socket, hands it to agentosd's
   user when the SSH user is root, and says ready.
3. `POST /api/exposes/{id}/ready`: the node checks the socket and routes the address in the lane.
4. Renew the lease every few seconds through the tunnel. Ctrl-C, `--for`, the SSH session ending
   or the node no longer answering ends it, on both sides.

forward: one port of a terminal or service lane, here. The node opens a socket into the lane's own
network namespace, and `ssh -L 127.0.0.1:<local>:<socket> <host>` makes it localhost:<local>.

Standard library only, like the rest of the client, so the commands start fast.
"""

from __future__ import annotations

import contextlib
import queue
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agentos.client.errors import ClientError
from agentos.client.owner import NodePanel, PanelError
from agentos.client.ports import pick_port, port_in_use
from agentos.client.state import Connection

READY_TIMEOUT_S = 30.0
READY_LINE = "agentos-expose-ready"
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+\.sock$")
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_for(text: str | None) -> int | None:
    """`--for`: `90` (seconds), `30m`, `2h`, `1d`; None keeps the node's default (2 hours)."""
    if text is None:
        return None
    m = _DURATION_RE.match(text)
    if not m or int(m.group(1)) <= 0:
        raise ClientError(f"--for {text!r} is not a duration", "use 30m, 2h or 1d")
    return int(m.group(1)) * _UNITS[m.group(2)]


def ssh_base(ssh: str) -> list[str]:
    """What every agentos SSH session uses (agentos.client.tunnel.TunnelSpec), with a quicker dead
    peer check: a link must end soon after the network does."""
    return [ssh, "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2", "-o", "ConnectTimeout=15", "-o", "ControlMaster=no",
            "-o", "ControlPath=none"]


def hold_script(socket_path: str, owner_uid: int) -> str:
    """Runs on the node in the `ssh -R` session: wait for sshd's socket, hand it to agentosd's user
    (sshd made it as the SSH user, mode 600), say ready, and hold the session until we close it."""
    if not SAFE_PATH_RE.match(socket_path) or ".." in socket_path:
        raise ClientError("the node answered with a socket path this computer will not use",
                          "is the node running a newer or older agentos? Update both")
    s = shlex.quote(socket_path)
    u = int(owner_uid)
    return (f"s={s}; i=0; while [ ! -S \"$s\" ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done; "
            f"[ -S \"$s\" ] || {{ echo 'agentos: the forwarded socket did not appear on the node' >&2; exit 3; }}; "
            f"if [ \"$(stat -c %u \"$s\")\" != {u} ]; then chown {u} \"$s\" 2>/dev/null || sudo -n chown {u} \"$s\" "
            f"|| {{ echo 'agentos: could not hand the socket to agentosd (sign in as root or as its user)' >&2; "
            f"exit 4; }}; fi; echo {READY_LINE}; exec cat >/dev/null")


def reverse_args(ssh: str, host: str, socket_path: str, port: int, owner_uid: int) -> list[str]:
    return [*ssh_base(ssh), "-T", "-R", f"{socket_path}:localhost:{port}", host, hold_script(socket_path, owner_uid)]


def local_args(ssh: str, host: str, local_port: int, socket_path: str) -> list[str]:
    if not SAFE_PATH_RE.match(socket_path) or ".." in socket_path:
        raise ClientError("the node answered with a socket path this computer will not use",
                          "is the node running a newer or older agentos? Update both")
    return [*ssh_base(ssh), "-N", "-L", f"127.0.0.1:{local_port}:{socket_path}", host]


@dataclass
class Outcome:
    code: int
    message: str


@dataclass
class LinkRunner:
    """Hold one expose or forward open: the SSH session and the lease, until one of them ends."""
    conn: Connection
    device: str
    ssh: str
    panel: NodePanel
    out: Callable[[str], None] = print
    popen: Callable[..., Any] = subprocess.Popen
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    link: dict[str, Any] = field(default_factory=dict)
    proc: Any = None
    _lines: queue.Queue = field(default_factory=queue.Queue)
    _errors: list[str] = field(default_factory=list)

    # ---- the SSH session -------------------------------------------------------------------

    def _start(self, args: list[str]) -> None:
        try:
            self.proc = self.popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        except OSError as e:
            raise ClientError(f"could not run ssh: {e.strerror}") from None
        for stream, sink in ((self.proc.stdout, self._lines), (self.proc.stderr, None)):
            if stream is not None:
                threading.Thread(target=self._read, args=(stream, sink), daemon=True).start()

    def _read(self, stream: Any, sink: queue.Queue | None) -> None:
        for line in stream:
            line = line.rstrip("\n")
            if sink is not None:
                sink.put(line)
            elif line.strip():
                self._errors.append(line.strip())
                del self._errors[:-5]

    def _ssh_said(self) -> str:
        return self._errors[-1] if self._errors else f"ssh exited ({self.proc.poll()})"

    def _wait_ready(self, ready: Callable[[], bool]) -> None:
        deadline = self.clock() + READY_TIMEOUT_S
        while self.clock() < deadline:
            if self.proc.poll() is not None:
                raise ClientError(f"the SSH forward did not start: {self._ssh_said()}",
                                  f"check that `ssh {self.conn.ssh_host}` works without a prompt, and that it "
                                  "signs in as root or as agentosd's own user (the socket lives in agentosd's "
                                  "folder)")
            if ready():
                return
            self.sleep(0.1)
        raise ClientError(f"the SSH forward did not come up within {int(READY_TIMEOUT_S)} seconds")

    def _stop_ssh(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        with contextlib.suppress(Exception):
            self.proc.terminate()
            self.proc.wait(timeout=5)
        if self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.proc.kill()

    def _end(self) -> None:
        if self.link.get("id"):
            with contextlib.suppress(PanelError):
                self.panel.expose_end(self.device, self.link["id"])

    # ---- the two commands ------------------------------------------------------------------

    def expose(self, *, lane: str, name: str, port: int, ttl_s: int | None) -> Outcome:
        body: dict[str, Any] = {"kind": "expose", "lane": lane, "name": name, "port": port}
        if ttl_s is not None:
            body["ttl_s"] = ttl_s
        self.link = self._create(body)
        try:
            self._start(reverse_args(self.ssh, self.conn.ssh_host, self.link["socket"], port,
                                     int(self.link.get("owner_uid", 0))))

            def said_ready() -> bool:
                with contextlib.suppress(queue.Empty):
                    return self._lines.get(timeout=0.05) == READY_LINE
                return False

            self._wait_ready(said_ready)
            done = self._call(self.panel.expose_ready, self.link["id"])
            self.link.update(done)
            url = self.link.get("url") or f"https://{name}.expose.agentos.invalid/"
            self.out(f"{url} now opens your localhost:{port} in {lane} on {self.conn.name}, "
                     f"until {self.link.get('expires_at', '?')} (UTC).")
            if not done.get("routed"):
                self.out("The lane is asleep: the address works as soon as it wakes.")
            self.out("Only that lane reaches it. Press Ctrl-C to end it.")
            return self._hold()
        except BaseException:
            self._end()
            self._stop_ssh()
            raise

    def forward(self, *, lane: str, port: int, local: int | None) -> Outcome:
        local_port = pick_port(local or port, set(), explicit=local is not None)
        self.link = self._create({"kind": "forward", "lane": lane, "port": port})
        try:
            self._start(local_args(self.ssh, self.conn.ssh_host, local_port, self.link["socket"]))
            self._wait_ready(lambda: port_in_use(local_port))
            self.out(f"http://localhost:{local_port} now reaches port {port} in {lane} on {self.conn.name}, "
                     f"until {self.link.get('expires_at', '?')} (UTC). Press Ctrl-C to end it.")
            return self._hold()
        except BaseException:
            self._end()
            self._stop_ssh()
            raise

    def _create(self, body: dict[str, Any]) -> dict[str, Any]:
        link = self._call(self.panel.expose_create, body)
        if not isinstance(link, dict) or not link.get("id") or not link.get("socket"):
            raise ClientError(f"{self.conn.name} answered without a link", "is the node older than this client?")
        return link

    def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(self.device, *args)
        except PanelError as exc:
            if exc.status == 401:
                raise ClientError(f"{self.conn.name} no longer accepts the owner credential (expired or ended)",
                                  f"run `agentos connect {self.conn.ssh_host} --name {self.conn.name}` for a "
                                  "new one") from None
            if exc.missing:
                raise ClientError(f"{self.conn.name} cannot expose or forward yet",
                                  "update agentos on the node") from None
            raise ClientError(f"{self.conn.name}: {exc.message}", exc.hint) from None

    def _hold(self) -> Outcome:
        """Renew until something ends it. Ctrl-C ends it on the node first, then the SSH session."""
        every = float(self.link.get("heartbeat_s") or 10.0)
        lease = float(self.link.get("lease_s") or 30.0)
        last_ok = self.clock()
        try:
            while True:
                waited = 0.0
                while waited < every:
                    if self.proc.poll() is not None:
                        self._end()
                        return Outcome(1, f"Ended: the SSH session closed ({self._ssh_said()}).")
                    self.sleep(0.5)
                    waited += 0.5
                try:
                    self.panel.expose_heartbeat(self.device, self.link["id"])
                    last_ok = self.clock()
                except PanelError as exc:
                    if exc.status == 404:
                        self._stop_ssh()
                        why = exc.message.removeprefix("it ended: ")
                        code = 0 if "time is up" in why else 1
                        return Outcome(code, f"Ended on the node: {why}.")
                    if self.clock() - last_ok > lease:
                        self._stop_ssh()
                        return Outcome(1, "Ended: the node has not answered for longer than the lease, so it "
                                          "ended the link on its side too.")
        except KeyboardInterrupt:
            self._end()
            self._stop_ssh()
            return Outcome(0, "Ended.")


def runner(client: Any, conn: Connection, out: Callable[[str], None] = print) -> LinkRunner:
    """A LinkRunner for a connection, with its owner credential and this computer's ssh."""
    if conn.local:
        raise ClientError(f"{conn.name} is this computer: open localhost directly",
                          "expose and forward work over an SSH connection to a node")
    device = client.owner_device(conn)
    ssh = client.tools.ssh
    if not ssh:
        raise ClientError("`ssh` is not on PATH", "install OpenSSH")
    return LinkRunner(conn=conn, device=device, ssh=ssh, panel=NodePanel(conn.local_port), out=out)
