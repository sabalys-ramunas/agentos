import asyncio
import base64
import shlex
import sys
import time

import pytest

from agentos.core.errors import PolicyDenied
from agentos.env.execs import Capture, encode_output
from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager, program_allowed
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher


@pytest.fixture
async def mgr(tmp_path):
    m = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                   settings=EnvSettings(proxy_port=0, install_proxy_port=0, max_output_bytes=4000))
    m.create("ws/bench", preset="workbench", node_secrets_ok=True)
    yield m
    await m.shutdown()


def test_capture_keeps_head_and_tail():
    c = Capture(100)
    for _ in range(50):
        c.feed(b"0123456789")
    assert c.total == 500 and len(c.head) == 25 and len(c.tail) == 75 and c.dropped == 400
    text, enc = encode_output(c)
    assert enc == "utf-8" and "[agentos: 400 bytes truncated here, of 500 in all; the start and the end are kept]" in text


def test_encode_output_handles_multibyte_cuts_and_binary():
    c = Capture(8)
    c.feed("ééééééééééé".encode())
    text, enc = encode_output(c)
    assert enc == "utf-8" and "truncated" in text
    b = Capture(100)
    b.feed(b"\x00\x01\xff")
    text, enc = encode_output(b)
    assert enc == "base64" and base64.b64decode(text) == b"\x00\x01\xff"


async def test_exec_basic_exit_codes_and_stdin(mgr):
    r = await mgr.exec("ws/bench", "echo hello; echo oops >&2; exit 3")
    assert r.exit_code == 3 and r.stdout == "hello\n" and r.stderr == "oops\n" and not r.timed_out
    assert r.duration_s >= 0
    r = await mgr.exec("ws/bench", "cat", stdin="from stdin")
    assert r.stdout == "from stdin"


async def test_exec_output_is_capped_with_a_marker(mgr):
    r = await mgr.exec("ws/bench", f"{shlex.quote(sys.executable)} -c \"print('x' * 200000)\"")
    assert r.truncated and r.stdout_bytes == 200001 and "bytes truncated here" in r.stdout
    assert len(r.stdout) < 5000


async def test_exec_is_binary_safe(mgr):
    r = await mgr.exec("ws/bench", "printf '\\000\\001\\377abc'")
    assert r.stdout_encoding == "base64" and base64.b64decode(r.stdout) == b"\x00\x01\xffabc"


async def test_exec_timeout_terminates_then_kills(mgr):
    r = await mgr.exec("ws/bench", "sleep 30", timeout_s=0.3)
    assert r.timed_out and r.exit_code is None and r.signal == "SIGTERM"
    t = time.monotonic()
    r = await mgr.exec("ws/bench", "trap '' TERM; sleep 30", timeout_s=0.3)
    assert r.timed_out and r.signal == "SIGKILL" and time.monotonic() - t < 6


async def test_background_children_do_not_hold_the_exec(mgr):
    t = time.monotonic()
    r = await mgr.exec("ws/bench", "sleep 30 & echo started")
    assert r.stdout == "started\n" and time.monotonic() - t < 5


async def test_concurrent_execs_in_one_lane(mgr):
    t = time.monotonic()
    rs = await asyncio.gather(*(mgr.exec("ws/bench", f"sleep 0.5; echo {i}") for i in range(4)))
    assert [r.stdout for r in rs] == [f"{i}\n" for i in range(4)]
    assert time.monotonic() - t < 1.9


async def test_locked_lanes_run_allowlisted_programs_only(mgr):
    mgr.create("ws/locked", preset="locked", allowed_programs=["echo"])
    r = await mgr.exec("ws/locked", "echo 'a b'")
    assert r.stdout == "a b\n"
    with pytest.raises(PolicyDenied, match="allowlisted programs only"):
        await mgr.exec("ws/locked", "sh -c 'echo no'")
    with pytest.raises(PolicyDenied):
        await mgr.shell_open("ws/locked")
    assert program_allowed("/usr/bin/python3", ["python3"])
    assert not program_allowed("/root/python3", ["python3"])
    assert program_allowed("/opt/x/run", ["/opt/x/run"]) and not program_allowed("run", ["/opt/x/run"])


async def test_isolated_lane_warm_and_cool_with_fake_mechanisms(mgr):
    mgr.create("ws/sb", preset="sandbox")
    r = await mgr.exec("ws/sb", "echo inside")
    assert r.stdout == "inside\n"
    host = mgr.host
    kinds = [c[0] for c in host.calls]
    assert "tmpfs" in kinds and kinds.count("overlay") == 8 and "netns" in kinds and "mask" in kinds
    assert mgr.state("ws/sb").value == "warm"
    await mgr.cool("ws/sb")
    assert not host.mounted and not host.netns
    assert mgr.state("ws/sb").value == "cold"


async def test_wait_exit_looks_often_at_first_and_less_after_a_while(monkeypatch):
    """A command over a VM's shared ssh ends in about 20 ms: waits that doubled from 5 ms to 50 ms saw
    that end 13 ms late. The first second is looked at every 10 ms at most, the rest every 50 ms."""
    from agentos.env import lanes

    class Proc:
        def __init__(self, after: int):
            self.after, self.returncode = after, None

    waits: list[float] = []

    async def sleep(s):
        waits.append(round(s, 5))
        if len(waits) >= proc.after:
            proc.returncode = 0

    monkeypatch.setattr(lanes.asyncio, "sleep", sleep)
    proc = Proc(8)
    assert await lanes.wait_exit(proc) == 0  # type: ignore[arg-type]
    assert waits == [0.002, 0.003, 0.0045, 0.00675, 0.01, 0.01, 0.01, 0.01]
    waits.clear()
    monkeypatch.setattr(lanes, "EXIT_POLL_FAST_S", 0.0)  # past the first second
    proc = Proc(9)
    await lanes.wait_exit(proc)  # type: ignore[arg-type]
    assert waits[-1] == 0.05 and max(waits) == 0.05
