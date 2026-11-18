"""A live check of the Morph Cloud provider (agentos.vms.morph) with a real API key: what the unit
tests could only fake. It spends a few cents and cleans up after itself.

Run it with the key in a file of mode 600 (never on a command line) and a scratch home:

    uv run python scripts/morph_live_check.py --key-file ~/.agentos/secrets/morph.token --home /tmp/morph-check

or with no --key-file, and it asks for the key (hidden). It uses agentos's own code for every step:

1. the key is checked (GET /instance), and the SSH gateway's host key is pinned (ssh-keyscan);
2. one morph-small instance (2 vCPU, 4 GB, 32 GB: 2 MCUs an hour, about USD 0.10) boots from
   Morph's base image with a 20-minute TTL as a backstop, labelled agentos-check;
3. SSH as agentos does it (agentos.vms.ssh.ssh_base: the instance's own key, the pinned host key,
   `<instance id>@ssh.cloud.morph.so`, BatchMode): `uname -a` and `id`;
4. the forwards the environments use: -D (a SOCKS proxy, on a Unix socket as the browser tunnel has
   it) reaching a server on the instance's loopback, -R (the git gateway's) reaching a server here,
   and -L to a Unix socket on the instance (the "vm" browser's live view);
5. a process started on the instance and a file in /tmp, then pause (timed), an SSH attempt while
   paused (it must fail: wake on SSH is off), resume (timed), and the same process still running;
6. a snapshot of the instance and its delete (labels as metadata, status ready);
7. everything is deleted again (the instance and the snapshot), even when a step failed.

It prints one line per step (ok or FAILED, with what Morph or ssh said), the time each took, and the
estimated cost. What it checks is listed as "unverified" in docs/design/vms.md until it has run.

The whole round trip of an environment (open with keep="save", close: paused, open: resumed) on a
node of your own, after this check passes:

    agentos vm setup --provider morph --home /tmp/morph-node          # the key, the gateway pinned
    agentos vm image build dev --home /tmp/morph-node                 # the dev snapshot (a few minutes)
    agentos dev up --no-connect --port 7463 --home /tmp/morph-node
    # then, from an agent connected to that node: environment_open(title="morph check", repo=...,
    # keep="save"), start a dev server with lane_job_start, environment_close(note="..."): `machine` is
    # "paused"; environment_open(title="morph check"): resumed.from is "paused" and the dev server's
    # resumed.services say running; environment_close(forget=true, discard=true) deletes it.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import http.server
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from agentos.env.models import RemoteHost
from agentos.environments.tunnels import tunnel_argv
from agentos.vms import keys as vmkeys
from agentos.vms.morph import MorphProvider, gateway_known_hosts, pin_gateway
from agentos.vms.provider import ServerSpec
from agentos.vms.ssh import ssh_base

LABELS = {"agentos-check": "morph-live"}
TTL_S = 20 * 60
STEPS: list[tuple[str, bool, str, float]] = []


def step(name: str, ok: bool, said: str = "", took: float = 0.0) -> None:
    STEPS.append((name, ok, said, took))
    print(f"{'ok    ' if ok else 'FAILED'} {name}" + (f" ({took:.1f} s)" if took else "") + (f": {said}" if said else ""),
          flush=True)


def run_ssh(remote: RemoteHost, command: str, timeout: float = 60) -> tuple[int, str]:
    p = subprocess.run([*ssh_base(remote), command], capture_output=True, text=True, timeout=timeout, check=False)
    return p.returncode, (p.stdout + p.stderr).strip()


class Here(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"from the node")

    def log_message(self, *a):
        return


def forwards(remote: RemoteHost, work: Path) -> None:
    """-D on a Unix socket, -R and -L to a Unix socket, through Morph's gateway, each on its own."""
    server = http.server.HTTPServer(("127.0.0.1", 0), Here)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    here_port = server.server_address[1]
    code, said = run_ssh(remote, "nohup python3 -m http.server 8123 --bind 127.0.0.1 >/dev/null 2>&1 & "
                                 "nohup python3 -c \"import socket,os;p='/tmp/agentos-check.sock';"
                                 "os.path.exists(p) and os.unlink(p);s=socket.socket(socket.AF_UNIX);s.bind(p);s.listen(1);"
                                 "c,_=s.accept();c.sendall(b'from the instance socket')\" >/dev/null 2>&1 & sleep 1; echo up")
    step("start a web server and a Unix socket on the instance", code == 0, said[-200:])
    socks = work / "socks.sock"
    argv = tunnel_argv(remote, socks_path=str(socks), gateway_port=here_port,
                       forwards=((str(work / "vnc.sock"), "/tmp/agentos-check.sock"),))
    t0 = time.monotonic()
    tunnel = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(30):
            if socks.exists() or tunnel.poll() is not None:
                break
            time.sleep(0.5)
        if tunnel.poll() is not None:
            step("the tunnel (-D, -R and -L together)", False, (tunnel.stderr.read() or "")[-300:])
            return
        step("the tunnel (-D, -R and -L together) is up", True, took=time.monotonic() - t0)
        # -D: a SOCKS5 CONNECT to the instance's loopback, over the Unix socket
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(15)
            s.connect(str(socks))
            s.sendall(b"\x05\x01\x00")
            assert s.recv(2) == b"\x05\x00"
            s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + (8123).to_bytes(2, "big"))
            reply = s.recv(10)
            s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
            body = s.recv(200)
            step("-D: the SOCKS proxy reaches the instance's loopback", reply[1] == 0 and b"HTTP/" in body,
                 body[:40].decode(errors="replace"))
        except (OSError, AssertionError) as e:
            step("-D: the SOCKS proxy reaches the instance's loopback", False, repr(e))
        code, said = run_ssh(remote, "curl -s --max-time 10 http://127.0.0.1:3129/ || "
                                     "python3 -c \"import urllib.request as u;print(u.urlopen('http://127.0.0.1:3129/').read().decode())\"")
        step("-R: the instance reaches a server on the node (the git gateway's way)", "from the node" in said, said[-120:])
        try:
            u = socket.socket(socket.AF_UNIX)
            u.settimeout(15)
            u.connect(str(work / "vnc.sock"))
            got = u.recv(100)
            step("-L to a Unix socket on the instance (the vm browser's live view)", b"instance socket" in got,
                 got.decode(errors="replace"))
        except OSError as e:
            step("-L to a Unix socket on the instance (the vm browser's live view)", False, repr(e))
    finally:
        tunnel.terminate()
        server.shutdown()


async def check(key: str, home: Path) -> None:
    prov = MorphProvider(key, host_keys=[])
    t0 = time.monotonic()
    await prov.check()
    step("the key works (GET /instance)", True, took=time.monotonic() - t0)
    prints = pin_gateway(home)
    prov.host_keys = gateway_known_hosts(home)
    step("pinned the gateway's host key", bool(prov.host_keys), "; ".join(prints)[:200])
    server = None
    snaps: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="agentos-morph-check-"))
    try:
        t0 = time.monotonic()
        keys = vmkeys.make(home / "vms", "vm_check")
        server = await prov.create_server(ServerSpec(name="agentos-morph-check", server_type="morph-small",
                                                     image=await prov.resolve_image("ubuntu-24.04"), location="morph",
                                                     user_data="", labels=LABELS, ttl_s=TTL_S))
        vmkeys.adopt(home / "vms", "vm_check", server)
        for _ in range(60):
            got = await prov.get_server(server.id)
            if got is not None and got.status == "running":
                break
            await asyncio.sleep(2)
        step(f"instance {server.id} is up", got is not None and got.status == "running", took=time.monotonic() - t0)
        remote = RemoteHost(vm_id="vm_check", host=prov.ssh_host, port=22, user=server.ssh_user,
                            key_path=str(keys.client_key), known_hosts=str(keys.known_hosts), host_alias=keys.alias)
        t0 = time.monotonic()
        code, said = run_ssh(remote, "uname -a; id -un; command -v systemctl cloud-init apt-get python3 | tr '\\n' ' '")
        step("SSH as agentos does it (own key, pinned gateway, no secret on argv)", code == 0, said[-300:],
             time.monotonic() - t0)
        if code != 0:
            return
        forwards(remote, work)
        code, said = run_ssh(remote, "echo kept > /tmp/agentos-check.txt; nohup sleep 100000 >/dev/null 2>&1 & "
                                     "echo $! > /tmp/agentos-check.pid; cat /tmp/agentos-check.pid")
        pid = said.strip().splitlines()[-1] if code == 0 else ""
        step("a process to outlive the pause", code == 0 and pid.isdigit(), f"pid {pid}")
        t0 = time.monotonic()
        await prov.pause_server(server.id)
        got = await prov.get_server(server.id)
        step("pause", got is not None and got.status == "paused", took=time.monotonic() - t0)
        code, said = run_ssh(remote, "true", timeout=40)
        still = await prov.get_server(server.id)
        step("SSH while paused does not wake it", code != 0 and still is not None and still.status == "paused",
             said[-120:])
        t0 = time.monotonic()
        resumed = await prov.resume_server(server.id)
        vmkeys.adopt(home / "vms", "vm_check", resumed)
        code, said = run_ssh(remote, f"cat /tmp/agentos-check.txt; kill -0 {pid} && echo running")
        step("resume, with the process still running", code == 0 and "running" in said and "kept" in said, said[-120:],
             time.monotonic() - t0)
        t0 = time.monotonic()
        sid = await prov.create_snapshot(server.id, "agentos live check", {**LABELS, "agentos-save": "scheck"})
        snaps.append(sid)
        img = await prov.get_image(sid)
        listed = await prov.list_images({"agentos-save": "scheck"})
        step("a snapshot, with its labels as metadata", (img or {}).get("status") == "available"
             and [i["id"] for i in listed] == [sid], f"{sid}, {img}", time.monotonic() - t0)
    finally:
        for sid in snaps:
            try:
                await prov.delete_image(sid)
                step(f"deleted snapshot {sid}", True)
            except Exception as e:  # noqa: BLE001 - say so, and go on cleaning
                step(f"deleted snapshot {sid}", False, str(e))
        if server is not None:
            try:
                await prov.delete_server(server.id)
                step(f"deleted instance {server.id}", await prov.get_server(server.id) is None)
            except Exception as e:  # noqa: BLE001
                step(f"deleted instance {server.id}", False, f"{e}; delete it at cloud.morph.so (its TTL ends it in "
                                                              f"{TTL_S // 60} minutes anyway)")
        leftovers = await prov.list_servers(LABELS)
        step("no instance labelled agentos-check is left", not leftovers, ", ".join(s.id for s in leftovers))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--key-file", type=Path, help="a file of mode 600 with the Morph API key")
    ap.add_argument("--home", type=Path, default=None, help="a scratch folder for the keys (default: a temp one)")
    args = ap.parse_args()
    key = args.key_file.read_text().strip() if args.key_file else getpass.getpass("Morph API key (hidden): ").strip()
    home = args.home or Path(tempfile.mkdtemp(prefix="agentos-morph-home-"))
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    began = time.monotonic()
    try:
        asyncio.run(check(key, home))
    except Exception as e:  # noqa: BLE001 - the step that failed, in words
        step("the check stopped", False, f"{type(e).__name__}: {getattr(e, 'message', e)}")
    minutes = (time.monotonic() - began) / 60
    print(f"\n{sum(ok for _n, ok, _s, _t in STEPS)} of {len(STEPS)} steps ok, {minutes:.1f} minutes: about "
          f"{2 * minutes / 60:.2f} MCU (USD {2 * minutes / 60 * 0.05:.3f}) for the instance.")
    return 0 if all(ok for _n, ok, _s, _t in STEPS) else 1


if __name__ == "__main__":
    sys.exit(main())
