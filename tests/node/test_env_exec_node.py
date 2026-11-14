# ruff: noqa: F811  (pytest fixtures imported from test_env_node_support)
import asyncio
import base64
import hashlib
import time

import pytest
from test_env_node_support import mgr, needs_node, shell_until  # noqa: F401

pytestmark = [pytest.mark.node, needs_node]


async def test_binary_output_is_exact(mgr):
    mgr.create("ex/bin")
    r = await mgr.exec("ex/bin", "head -c 40000 /dev/urandom > /tmp/r; sha256sum /tmp/r | cut -d' ' -f1 >&2; cat /tmp/r")
    assert r.stdout_encoding == "base64" and not r.truncated
    data = base64.b64decode(r.stdout)
    assert len(data) == 40000 and hashlib.sha256(data).hexdigest() == r.stderr.strip()


async def test_zombies_are_reaped_by_the_lane_init(mgr):
    mgr.create("ex/zomb")
    script = ("for i in 1 2 3 4 5; do (sleep 0.1 &) ; done; "
              "python3 -c 'import os; [os.fork() or os._exit(0) for _ in range(5)]; import time; time.sleep(0.5)'; "
              "sleep 1; ps -eo stat= | grep -c '^Z' || true")
    r = await mgr.exec("ex/zomb", script)
    assert r.stdout.strip() == "0"
    s = await mgr.shell_open("ex/zomb", term="dumb")
    await s.write(b"(sleep 0.2 &); (sleep 0.2 &); sleep 1; echo Z=$(ps -eo stat= | grep -c '^Z')\n")
    await shell_until(s, "Z=0")
    await mgr.shell_close(s.id)


async def test_concurrent_execs_share_the_lane(mgr):
    mgr.create("ex/conc", overrides={"filesystem": "overlay", "disk_mb": 64})
    await mgr.warm("ex/conc")
    t = time.monotonic()
    rs = await asyncio.gather(*(mgr.exec("ex/conc", f"sleep 1; echo {i} >> /root/log; echo {i}") for i in range(4)))
    assert sorted(r.stdout for r in rs) == [f"{i}\n" for i in range(4)]
    assert time.monotonic() - t < 3.5
    r = await mgr.exec("ex/conc", "sort /root/log | tr '\\n' ' '")
    assert r.stdout.split() == ["0", "1", "2", "3"]


async def test_timeouts_escalate_from_term_to_kill(mgr):
    mgr.create("ex/sig")
    r = await mgr.exec("ex/sig", "sleep 30", timeout_s=1)
    assert r.timed_out
    t = time.monotonic()
    r = await mgr.exec("ex/sig", "trap '' TERM; sleep 30 & wait", timeout_s=1)
    assert r.timed_out and time.monotonic() - t < 8
    r = await mgr.exec("ex/sig", "kill -9 $$")
    assert r.signal == "SIGKILL" and r.exit_code == 137


async def test_shell_signals_resize_and_detach(mgr):
    mgr.create("ex/sh")
    s = await mgr.shell_open("ex/sh", cols=80, rows=24, term="dumb")
    await s.write(b"stty size\n")
    await shell_until(s, "24 80")
    s.resize(132, 50)
    await s.write(b"stty size\n")
    await shell_until(s, "50 132")
    await s.write(b"sleep 60\n")
    await asyncio.sleep(0.5)
    await s.write(b"\x03echo INT''ERRUPTED\n")
    await shell_until(s, "INTERRUPTED", timeout=5)
    await s.write(b"tty; echo TTY''-OK\n")
    out = await shell_until(s, "TTY-OK")
    assert "not a tty" not in out                    # bubblewrap exposes the PTY as /dev/console
    sid = s.id
    del s
    r = await mgr.exec("ex/sh", "echo exec-sees-own-pidns")
    assert r.stdout == "exec-sees-own-pidns\n"
    assert mgr.shell(sid).running
    await mgr.shell_close(sid)


async def test_pty_flood_is_bounded_and_the_node_stays_responsive(mgr):
    mgr.settings.shell_buffer_bytes = 256 * 1024
    mgr.create("ex/flood")
    s = await mgr.shell_open("ex/flood", term="dumb")
    await s.write(b"yes flood | head -c 20000000; echo; echo FLOOD''-DONE\n")
    for _ in range(100):
        if s.paused:
            break
        await asyncio.sleep(0.05)
    assert s.paused and len(s.ring.buf) <= s.ring.capacity
    t = time.monotonic()
    r = await mgr.exec("ex/flood", "echo responsive")
    assert r.stdout == "responsive\n" and time.monotonic() - t < 5
    q = s.subscribe(replay=False)                    # a live viewer drains without pausing
    got = 0
    end = time.monotonic() + 60
    tail = b""
    while time.monotonic() < end:
        chunk = await asyncio.wait_for(q.get(), 10)
        got += len(chunk)
        tail = (tail + chunk)[-64:]
        if b"FLOOD-DONE" in tail:
            break
    assert b"FLOOD-DONE" in tail
    s.unsubscribe(q)
    await mgr.shell_close(s.id)
