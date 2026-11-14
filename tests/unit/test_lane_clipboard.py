"""Reading a lane's clipboard with xclip (agentos.node.lane_clipboard), with the subprocess faked."""

from __future__ import annotations

import asyncio

import pytest

from agentos.core import catalog
from agentos.node import lane_clipboard
from agentos.node.lane_clipboard import ClipboardTooLarge, ClipboardUnreadable, XclipMissing, read_clipboard

COPIED = "Kopija ąčęėįšųūž € 42"


class Stream:
    def __init__(self, data: bytes = b"", *, hang: bool = False):
        self.data, self.hang = data, hang

    async def read(self, n: int) -> bytes:
        if self.hang:
            await asyncio.sleep(3600)
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk


class Proc:
    def __init__(self, out: bytes = b"", err: bytes = b"", code: int = 0, *, hang: bool = False):
        self.stdout, self.stderr = Stream(out, hang=hang), Stream(err)
        self.code, self.returncode, self.killed = code, None, False

    async def wait(self) -> int:
        self.returncode = -9 if self.killed else self.code
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class Spawn:
    def __init__(self, proc: Proc | None = None, error: Exception | None = None):
        self.proc, self.error, self.calls = proc, error, []

    async def __call__(self, *argv, **kw):
        self.calls.append((argv, kw))
        if self.error is not None:
            raise self.error
        return self.proc


async def test_reads_the_lanes_clipboard_as_utf8_text_without_a_shell():
    spawn = Spawn(Proc(COPIED.encode()))
    assert await read_clipboard(":100", "/var/lib/agentos/vnc/display-100.xauth", spawn=spawn) == COPIED
    (argv, kw), = spawn.calls
    assert argv == ("xclip", "-o", "-selection", "clipboard", "-t", "UTF8_STRING")
    assert kw["env"]["DISPLAY"] == ":100" and kw["env"]["XAUTHORITY"] == "/var/lib/agentos/vnc/display-100.xauth"
    assert set(kw["env"]) <= {"DISPLAY", "XAUTHORITY", "PATH"}  # nothing else of the node's environment
    assert "shell" not in kw


async def test_an_empty_clipboard_reads_as_nothing():
    assert await read_clipboard(":100", None, spawn=Spawn(Proc(b""))) == ""


@pytest.mark.parametrize("err", [b"Error: target UTF8_STRING not available\n", b""])
async def test_no_text_on_the_clipboard_or_no_owner_reads_as_nothing(err):
    """An image only, or nobody owns the clipboard: xclip exits 1 saying the target is not there."""
    assert await read_clipboard(":100", None, spawn=Spawn(Proc(b"", err, 1))) == ""


async def test_a_browser_that_does_not_answer_times_out_and_is_killed():
    proc = Proc(hang=True)
    with pytest.raises(ClipboardUnreadable) as e:
        await read_clipboard(":100", None, spawn=Spawn(proc), timeout=0.05)
    assert proc.killed and "Copy again" in e.value.message


async def test_xclip_missing_is_its_own_error_and_its_fix_installs_it():
    with pytest.raises(XclipMissing) as e:
        await read_clipboard(":100", None, spawn=Spawn(error=FileNotFoundError("xclip")))
    assert e.value.code == "xclip_missing" and lane_clipboard.INSTALL_XCLIP in e.value.message
    assert lane_clipboard.INSTALL_XCLIP in catalog.entry("xclip_missing").fix


async def test_over_the_cap_is_refused_whole_never_cut_short():
    proc = Proc(b"x" * 2048)
    with pytest.raises(ClipboardTooLarge) as e:
        await read_clipboard(":100", None, spawn=Spawn(proc), limit=1024)
    assert proc.killed and "smaller part" in e.value.message
    assert await read_clipboard(":100", None, spawn=Spawn(Proc(b"x" * 1024)), limit=1024) == "x" * 1024


async def test_text_that_is_not_utf8_is_refused():
    with pytest.raises(ClipboardUnreadable) as e:
        await read_clipboard(":100", None, spawn=Spawn(Proc(b"caf\xe9")))
    assert "not text" in e.value.message


async def test_another_xclip_failure_says_what_xclip_said_and_what_to_do():
    with pytest.raises(ClipboardUnreadable) as e:
        await read_clipboard(":100", None, spawn=Spawn(Proc(b"", b"Error: Can't open display: :100\n", 1)))
    assert "Can't open display" in e.value.message and "agentos doctor" in e.value.message


def test_the_words_a_person_reads_have_no_em_dash():
    for code in ("xclip_missing", "clipboard_too_large", "clipboard_unreadable"):
        entry = catalog.entry(code)
        assert "—" not in entry.meaning + entry.fix + entry.agent
    assert "—" not in XclipMissing().message
