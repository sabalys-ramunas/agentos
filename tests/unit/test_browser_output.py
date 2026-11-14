"""The browser's own output (agentos.node.browser_output): the Playwright driver's stderr goes
through the log scrubbers, and error tails (browser logs, call logs) leave agents' errors."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
import time

import pytest

from agentos.core import logs
from agentos.node import browser_output
from agentos.node.browser_output import browser_error, capture_driver_stderr, mask_urls, split_error

TYPED = "typed-value-4417"  # a vault value the browser echoed
CRASH = (
    "BrowserType.launch_persistent_context: Target page, context or browser has been closed\n"
    "Browser logs:\n\n"
    "<launching> /opt/camoufox/camoufox-bin -no-remote -profile /var/lib/agentos/lanes/biz-a/x\n"
    f"[pid=812][err] JavaScript error: https://www.instagram.com/accounts/password/reset/confirm/?uidb36=4f&token=a1b2c3-9f8e7d#u={TYPED}, line 3\n"
    f"[pid=812][out] console.log: login as {TYPED}\n"
    "Call log:\n  - <launching> /opt/camoufox/camoufox-bin\n"
)


@pytest.fixture(autouse=True)
def root_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)
    logs.clear_scrubbers()


def journal() -> io.StringIO:
    stream = io.StringIO()
    logs.setup(None, stream=stream, journal=False)
    logs.add_scrubber(lambda text: text.replace(TYPED, "[redacted]"))  # what the vault's scrub does
    return stream


def test_urls_lose_their_query_and_fragment_but_keep_host_and_path():
    assert mask_urls("GET https://x.example/cb?code=4/0AY0e-g7&state=s#access_token=t, then") == \
        "GET https://x.example/cb?..., then"
    assert mask_urls("see https://u:p@x.example/a/b.") == "see https://x.example/a/b."
    assert mask_urls("no url here") == "no url here"


def test_an_error_splits_into_its_message_and_the_browser_output_after_it():
    head, tail = split_error(Exception(CRASH))
    assert head == "BrowserType.launch_persistent_context: Target page, context or browser has been closed"
    assert tail.startswith("Browser logs:") and "Call log:" in tail
    old = "Timeout 30000ms exceeded.\n" + "=" * 27 + " logs " + "=" * 27 + "\nnavigating to https://a/?t=1"
    assert split_error(old) == ("Timeout 30000ms exceeded.", "=" * 27 + " logs " + "=" * 27 +
                                "\nnavigating to https://a/?t=1")
    assert split_error(Exception("one line")) == ("one line", "")


def test_browser_error_keeps_the_message_and_logs_the_output_scrubbed_and_masked():
    stream = journal()
    message = browser_error(Exception(CRASH), "biz-a/instagram")
    assert message == "BrowserType.launch_persistent_context: Target page, context or browser has been closed"
    out = stream.getvalue()
    assert "biz-a/instagram: browser output with the error" in out and "console.log: login as [redacted]" in out
    assert TYPED not in out and "token=a1b2c3" not in out and "uidb36" not in out
    assert "https://www.instagram.com/accounts/password/reset/confirm/?..." in out


def test_a_launch_failure_reaches_the_lane_error_without_the_browser_output():
    from agentos.core.errors import EngineError

    err = EngineError(f"biz-a/x: the browser failed to start: {browser_error(Exception(CRASH), 'biz-a/x')}")
    assert TYPED not in err.message and "Browser logs" not in err.message


@pytest.fixture
def driver_stderr(monkeypatch):
    """capture_driver_stderr on the real Playwright module, undone afterwards."""
    from playwright._impl import _transport

    monkeypatch.setattr(_transport, "_get_stderr_fileno", _transport._get_stderr_fileno)
    monkeypatch.setattr(_transport, "get_driver_env", _transport.get_driver_env)
    monkeypatch.setattr(browser_output, "_write_fd", None)
    assert capture_driver_stderr()
    assert capture_driver_stderr()  # idempotent
    return _transport._get_stderr_fileno()


def test_the_driver_stderr_goes_through_the_log_scrubbers(driver_stderr):
    stream = journal()
    line = f"[pid=9][err] Navigation to https://accounts.example/login?email={TYPED}&next=/ failed for {TYPED}"
    # the way Playwright starts its driver: stderr=_get_stderr_fileno()
    subprocess.run([sys.executable, "-c", f"import sys; sys.stderr.write({line!r} + '\\n')"],
                   stderr=driver_stderr, check=True)
    deadline = time.monotonic() + 5
    while "browser stderr" not in stream.getvalue() and time.monotonic() < deadline:
        time.sleep(0.02)
    out = stream.getvalue()
    assert "browser stderr: [pid=9][err] Navigation to https://accounts.example/login?... failed for [redacted]" in out
    assert TYPED not in out


def test_a_playwright_without_the_hook_leaves_stderr_alone(monkeypatch, caplog):
    from playwright._impl import _transport

    monkeypatch.setattr(browser_output, "_write_fd", None)
    monkeypatch.delattr(_transport, "_get_stderr_fileno")
    with caplog.at_level(logging.WARNING, "agentos.browser"):
        assert capture_driver_stderr() is False
    assert "unscrubbed" in caplog.text


async def test_a_lane_close_failure_logs_the_message_not_the_browser_output(tmp_path):
    """lanes.py and engine_camoufox.py used to log str(e): a close that failed put the browser's
    output, URLs with their tokens included, in the journal next to the message."""
    from test_lanes import make

    from agentos.core.models import LaneSpec

    stream = journal()
    mgr, engine, *_ = make(tmp_path)
    await mgr.create(LaneSpec(id="biz-a/instagram"))
    async with mgr.using("biz-a/instagram"):
        pass
    session = engine.sessions[-1]

    async def broken_close():
        raise RuntimeError(CRASH)

    session.close = broken_close
    await mgr.close("biz-a/instagram", principal_kind="owner")
    await mgr.shutdown()
    out = stream.getvalue()
    head = next(line for line in out.splitlines() if "browser close failed" in line)
    assert "Target page, context or browser has been closed" in head and "Browser logs" not in head
    assert "token=a1b2c3" not in out and TYPED not in out


def test_a_download_error_keeps_no_url_query():
    from agentos.node.engine_camoufox import _first_line

    assert _first_line(Exception("net::ERR_FAILED at https://cdn.example/f.zip?sig=abc123\nmore")) == \
        "net::ERR_FAILED at https://cdn.example/f.zip?..."


def test_a_chatty_driver_is_drained_fast_and_a_long_line_is_cut_at_a_space():
    """The drain read one byte at a time (about 1.5 MB/s) and held a whole line before cutting it, so a
    driver writing megabytes blocked on a full pipe. Now: buffered, and a line is held up to a cap."""
    import os
    import threading

    stream = journal()
    read_fd, write_fd = os.pipe()
    drain = threading.Thread(target=browser_output._drain, args=(read_fd,), daemon=True)
    drain.start()
    long_line = b"head words " + b"x" * (8 * 1024 * 1024) + f" {TYPED}\n".encode()
    started = time.monotonic()
    with os.fdopen(write_fd, "wb") as pipe:
        pipe.write(long_line + b"next line\n")
    drain.join(10)
    elapsed = time.monotonic() - started
    out = stream.getvalue()
    assert not drain.is_alive() and elapsed < 3.0
    assert "browser stderr: head words [line cut]" in out and "browser stderr: next line" in out
    assert TYPED not in out and "xxxx" not in out  # cut at the last space before the cap: no half word


# ---- the browser's own output, through the driver ----


@pytest.fixture
def lines(monkeypatch):
    """A journal for _log_line, with no browsers named and a fresh per-browser budget."""
    monkeypatch.setattr(browser_output, "_browsers", {})
    monkeypatch.setattr(browser_output, "_profiles", {})
    monkeypatch.setattr(browser_output, "_budget", browser_output._Budget(browser_output.MAX_LINES_PER_MINUTE))
    return journal()


def feed(*texts: str) -> None:
    for text in texts:
        browser_output._log_line(text.encode(), cut=False)


def test_the_driver_gets_pw_browser_in_its_own_environment_only(monkeypatch):
    from agentos.node.typist import protocol_logging

    monkeypatch.setenv("DEBUG", "pw:custom")
    env = browser_output._driver_env(lambda: {"DEBUG": "pw:custom", "PATH": "/bin"})()
    assert env["DEBUG"] == "pw:custom,pw:browser" and env["PATH"] == "/bin"
    assert browser_output._driver_env(lambda: {"DEBUG": "pw:browser"})()["DEBUG"] == "pw:browser"
    assert browser_output._driver_env(dict)()["DEBUG"] == "pw:browser"
    assert __import__("os").environ["DEBUG"] == "pw:custom"  # the daemon's own environment is untouched
    assert not protocol_logging(env)  # typing a secret stays allowed: pw:browser logs no protocol


def test_capture_patches_the_driver_env_of_the_real_playwright(driver_stderr):
    from playwright._impl import _transport

    assert "pw:browser" in _transport.get_driver_env()["DEBUG"]


def test_a_browser_line_carries_its_lane_and_is_scrubbed_and_masked(lines):
    browser_output.watch_browser(812, "biz-a/instagram")
    feed(f"2026-11-04T10:00:00.000Z pw:browser [pid=812][err] JavaScript error: "
         f"https://www.instagram.com/accounts/login/?next={TYPED}, line 3",
         "  pw:browser [pid=812][out] console.error: Policies: +2ms",  # the terminal format
         "2026-11-04T10:00:00.000Z pw:browser [pid=812][out]",          # nothing said: not logged
         "driver warning: something")                                   # the driver's own line
    out = lines.getvalue().splitlines()
    first = next(line for line in out if "JavaScript error" in line)
    assert "INFO" in first and "[lane=biz-a/instagram]" in first
    assert "https://www.instagram.com/accounts/login/?..." in first and TYPED not in first
    assert any("[lane=biz-a/instagram] browser stderr: [pid=812][out] console.error: Policies:" in line
               and line.endswith("Policies:") for line in out)
    assert not any(line.endswith("[pid=812][out]") for line in out)
    assert any("WARN " in line and "browser stderr: driver warning: something" in line and "lane=" not in line
               for line in out)


def test_lines_before_the_engine_knows_the_pid_find_the_lane_by_profile(lines, monkeypatch):
    profile = "/var/lib/agentos/lanes/biz-a/instagram/profile"
    browser_output.watch_profile(profile, "biz-a/instagram")
    argv = f"/opt/camoufox/camoufox-bin\0-no-remote\0-profile\0{profile}\0-juggler-pipe\0".encode()
    def fake_open(path, mode):
        if path != "/proc/77/cmdline":
            raise FileNotFoundError(path)
        return io.BytesIO(argv)

    monkeypatch.setattr(browser_output, "open", fake_open, raising=False)
    feed(f"pw:browser <launching> /opt/camoufox/camoufox-bin -no-remote -profile {profile} -juggler-pipe about:blank",
         "pw:browser [pid=77][out] Juggler listening to the pipe",
         "pw:browser [pid=78][out] another browser")
    out = lines.getvalue().splitlines()
    assert "[lane=biz-a/instagram]" in next(line for line in out if "<launching>" in line)
    assert "[lane=biz-a/instagram]" in next(line for line in out if "Juggler listening" in line)
    assert "lane=" not in next(line for line in out if "another browser" in line)
    browser_output.forget_browser(77, profile)
    assert browser_output._browsers == {} and browser_output._profiles == {}


def test_a_chatty_browser_is_capped_per_minute_and_the_rest_is_counted(lines, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(browser_output, "_clock", lambda: now[0])
    browser_output.watch_browser(5, "biz-a/x")
    limit = browser_output.MAX_LINES_PER_MINUTE
    feed(*(f"pw:browser [pid=5][err] noisy {i}" for i in range(limit + 7)))
    feed("pw:browser [pid=6][err] a quiet browser keeps its own budget")
    out = lines.getvalue()
    assert out.count("noisy") == limit and f"noisy {limit - 1}" in out and f"noisy {limit}" not in out
    assert "a quiet browser keeps its own budget" in out and "skipped" not in out
    now[0] += 61
    feed("pw:browser [pid=5][err] a new minute")
    out = lines.getvalue().splitlines()
    skipped = next(i for i, line in enumerate(out) if "skipped" in line)
    assert "7 lines of browser pid 5 skipped" in out[skipped] and "[lane=biz-a/x]" in out[skipped]
    assert "a new minute" in out[skipped + 1]
    feed(*(f"pw:browser [pid=5][err] again {i}" for i in range(limit + 2)))  # the minute's first line is used
    browser_output.forget_browser(5)  # a closed browser reports what it was still owed
    assert "3 lines of browser pid 5 skipped" in lines.getvalue()
