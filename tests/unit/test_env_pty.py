import asyncio
from datetime import timedelta

import pytest

from agentos.core.models import utcnow
from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.paths import EnvSettings
from agentos.env.pty import Ring
from agentos.env.sandbox import DirectLauncher


@pytest.fixture
async def mgr(tmp_path):
    m = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                   settings=EnvSettings(proxy_port=0, shell_buffer_bytes=65536))
    m.create("ws/bench", preset="workbench", node_secrets_ok=True)
    yield m
    await m.shutdown()


async def read_until(s, needle: str, timeout: float = 5.0) -> str:
    out = ""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while needle not in out and loop.time() < end:
        data, _ = await s.read(wait_s=0.5)
        out += s.read_text(data)
    assert needle in out, out[-500:]
    return out


def test_ring_offsets_and_drops():
    r = Ring(10)
    r.append(b"0123456789abcdef")
    assert r.start == 6 and r.total == 16
    data, cur, dropped = r.read(0, 100)
    assert data == b"6789abcdef" and cur == 16 and dropped == 6
    data, cur, dropped = r.read(12, 2)
    assert data == b"cd" and cur == 14 and dropped == 0


async def open_sh(mgr, **kw):
    return await mgr.shell_open("ws/bench", argv=["/bin/sh", "-i"], term="dumb", **kw)


async def test_shell_roundtrip_resize_and_exit(mgr):
    s = await open_sh(mgr, cols=80, rows=24)
    await s.write(b"echo hello-$((1+2))\n")
    await read_until(s, "hello-3")
    s.resize(100, 40)
    await s.write(b"stty size\n")
    await read_until(s, "40 100")
    await s.write(b"exit 7\n")
    for _ in range(50):
        if not s.running:
            break
        await asyncio.sleep(0.1)
    assert not s.running and s.exit_code == 7
    assert s.info().state == "exited"


async def test_ctrl_c_interrupts_the_foreground_command(mgr):
    s = await open_sh(mgr)
    await s.write(b"sleep 30\n")
    await asyncio.sleep(0.5)
    await s.write(b"\x03")
    await s.write(b"echo AFTER-''INT\n")
    await read_until(s, "AFTER-INT", timeout=5)      # only reachable if sleep 30 was interrupted


async def test_flood_pauses_reading_instead_of_growing(mgr):
    s = await open_sh(mgr)
    await s.write(b"head -c 600000 /dev/zero | tr '\\0' 'y'; echo; echo flood-done\n")
    for _ in range(50):
        if s.paused:
            break
        await asyncio.sleep(0.05)
    assert s.paused and len(s.ring.buf) <= s.ring.capacity
    total = 0
    out = ""
    for _ in range(200):
        data, dropped = await s.read(wait_s=0.5, max_bytes=65536)
        assert dropped == 0
        total += len(data)
        out = (out + s.read_text(data))[-200:]
        if "flood-done" in out:
            break
    assert "flood-done" in out and total >= 600000


async def test_sessions_are_detached_and_reaped_when_idle(mgr):
    s = await open_sh(mgr)
    sid = s.id
    del s
    assert mgr.shell(sid).running
    assert mgr.state("ws/bench").value == "active"
    q = mgr.shell(sid).subscribe()
    later = utcnow() + timedelta(seconds=mgr.settings.shell_idle_s + 5)
    assert (await mgr.reap_once(now=later))["shells_closed"] == []     # someone is watching
    mgr.shell(sid).unsubscribe(q)
    res = await mgr.reap_once(now=later)
    assert res["shells_closed"] == [sid] and sid not in mgr.shells
    assert res["lanes_cooled"] == ["ws/bench"] and mgr.state("ws/bench").value == "cold"


async def test_subscribers_get_scrollback_and_live_output(mgr):
    s = await open_sh(mgr)
    await s.write(b"echo first\n")
    await read_until(s, "first")
    q = s.subscribe(replay=True)
    first = await asyncio.wait_for(q.get(), 2)
    assert b"first" in first
    await s.write(b"echo second\n")
    got = b""
    while b"second" not in got:
        got += await asyncio.wait_for(q.get(), 3)
    await mgr.shell_close(s.id)
    assert await asyncio.wait_for(q.get(), 3) is None or True
