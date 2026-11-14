"""`logs` streams: the node's own log lines, redacted on the node, capped in length and rate."""

import asyncio
import json
import threading
import time

import pytest
from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop

from agentos.node.channel import logstream, wire

VAULT_SECRET = "correct-horse-battery-staple"
GRANT_TOKEN = "agt2_" + "Q" * 60 + "." + "x" * 80
BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvd25lciJ9.c2lnbmF0dXJlLXRoYXQtaXMtbG9uZw"


class FakeVault:
    def scrub(self, text, lane_id=None):
        return text.replace(VAULT_SECRET, "[redacted]")


def line(msg, *, level="info", logger="agentos.node", ago=60.0, **fields):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - ago)) + ".000Z"
    return json.dumps({"ts": ts, "level": level, "logger": logger, "msg": msg, **fields}) + "\n"


@pytest.fixture
async def node(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake)
        runtime.register("vault", FakeVault())
        (home / "logs").mkdir(parents=True, exist_ok=True)
        await start(runtime)
        peer = await fake.peer()
        await asyncio.wait_for(runtime.channel.welcomed.wait(), 15)  # the fake sees the peer before the node is in
        yield runtime, peer, home / "logs" / "agentosd.jsonl"
        await stop(runtime)


async def open_logs(peer, params):
    stream_id = wire.ulid()
    sent = await peer.send("stream_open", {"stream_id": stream_id, "kind": "logs", "params": params})
    reply = await peer.reply_to(sent.id)
    return stream_id, reply


async def drain(peer, stream_id, *, until=None, timeout=5.0):
    """Lines from the stream until it closes (or `until(lines)` holds)."""
    data = b""
    async with asyncio.timeout(timeout):
        while True:
            closed = any(e.type == "stream_close" and e.body["stream_id"] == stream_id for e in peer.seen)
            while not peer.binary.empty():
                sid, chunk = wire.parse_stream_frame(peer.binary.get_nowait())
                assert sid == stream_id
                data += chunk
            lines = [json.loads(x) for x in data.decode().splitlines() if x]
            if closed or (until is not None and until(lines)):
                return lines
            await asyncio.sleep(0.05)


async def test_logs_are_redacted_on_the_node_and_cut_to_length(node):
    _, peer, path = node
    path.write_text("".join([
        line(f"logged in with {VAULT_SECRET}", lane="biz-a/social"),
        line(f"grant {GRANT_TOKEN} used"),
        line(f"header Authorization: {BEARER}", logger="agentos.channel"),
        line("the node token is node-owner-token, oops"),
        line("password=hunter2hunter2 in a url"),
        line("x" * 100_000 + VAULT_SECRET, level="warn"),
        line("too old to show", ago=7200),
        line("debug noise", level="debug"),
        "not json at all\n",
    ]))
    stream_id, ready = await open_logs(peer, {"since": "1h", "level": "info"})
    assert ready.type == "stream_ready", ready.body
    lines = await drain(peer, stream_id)
    text = json.dumps(lines)
    for secret in (VAULT_SECRET, GRANT_TOKEN, "eyJhbGci", "node-owner-token", "hunter2hunter2"):
        assert secret not in text, secret
    assert [x["message"][:14] for x in lines[:2]] == ["logged in with", "grant [redacte"]
    assert lines[0]["lane"] == "biz-a/social" and lines[0]["source"] == "lane"
    assert lines[2]["source"] == "channel" and lines[1]["source"] == "agentosd"
    oversized = lines[5]
    assert oversized["level"] == "warn" and len(oversized["message"]) < logstream.MAX_MESSAGE + 40
    assert "too old" not in text and "debug noise" not in text and lines[-1]["message"] == "not json at all"
    assert set(lines[0]) == {"at", "source", "level", "logger", "message", "lane"}
    closed = next(e for e in peer.seen if e.type == "stream_close" and e.body["stream_id"] == stream_id)
    assert closed.body["reason"] == "eof"


async def test_source_and_level_filter_the_lines(node):
    _, peer, path = node
    path.write_text(line("a schedule fired", logger="agentos.runner.schedules")
                    + line("a channel hiccup", logger="agentos.channel", level="error")
                    + line("plain info"))
    stream_id, _ = await open_logs(peer, {"source": "scheduler"})
    assert [x["message"] for x in await drain(peer, stream_id)] == ["a schedule fired"]
    stream_id, _ = await open_logs(peer, {"level": "error"})
    assert [x["message"] for x in await drain(peer, stream_id)] == ["a channel hiccup"]


async def test_a_followed_stream_sends_new_lines_until_the_controller_closes_it(node):
    _, peer, path = node
    path.write_text(line("before"))
    stream_id, ready = await open_logs(peer, {"follow": True, "since": "10m"})
    assert ready.type == "stream_ready"
    assert [x["message"] for x in await drain(peer, stream_id, until=lambda lines: lines)] == ["before"]
    with path.open("a") as handle:
        handle.write(line(f"after {VAULT_SECRET}", ago=0))
    lines = await drain(peer, stream_id, until=lambda lines: lines)
    assert lines[0]["message"] == "after [redacted]"
    # rotation: the old file is renamed and a new one starts
    path.rename(path.with_name("agentosd.jsonl.1"))
    path.write_text(line("in the new file", ago=0))
    assert (await drain(peer, stream_id, until=lambda lines: lines))[0]["message"] == "in the new file"
    await peer.send("stream_close", {"stream_id": stream_id, "reason": "closed"})
    # the reader thread ends within a poll of the close (0.5 s each for the worker and the reader);
    # waited for, not slept on, because a busy runner took longer than a fixed 1.2 s
    async with asyncio.timeout(10):
        while [t for t in threading.enumerate() if t.name == "channel-logs"]:
            await asyncio.sleep(0.05)


async def test_a_flood_of_lines_is_capped_and_says_how_many_it_left_out(node):
    _, peer, path = node
    path.write_text("")
    stream_id, _ = await open_logs(peer, {"follow": True, "since": "1m"})
    await asyncio.sleep(0.3)
    started = time.monotonic()
    with path.open("a") as handle:
        handle.write("".join(line(f"flood {i}", ago=0) for i in range(3000)))
    await peer.send("stream_credit", {"stream_id": stream_id, "bytes": 8 << 20})
    lines = await drain(peer, stream_id, until=lambda lines: any("left out" in x["message"] for x in lines),
                        timeout=10)
    elapsed = time.monotonic() - started
    floods = [x for x in lines if x["message"].startswith("flood")]
    assert len(floods) <= logstream.BURST + logstream.RATE * (elapsed + 1)
    note = next(x for x in lines if "left out" in x["message"])
    assert note["level"] == "warn" and int(note["message"].split()[0]) > 0


@pytest.mark.parametrize("params", [{"follow": "yes"}, {"source": "kernel"}, {"level": "loud"},
                                    {"since": "yesterday"}, {"since": 5}])
async def test_bad_params_are_refused_before_the_stream_opens(node, params):
    _, peer, _ = node
    _, reply = await open_logs(peer, params)
    assert reply.type == "error" and reply.body["code"] == "invalid"


def test_the_rate_is_a_token_bucket():
    now = [0.0]
    rate = logstream.Rate(rate=10, burst=5, clock=lambda: now[0])
    assert sum(rate.allow() for _ in range(20)) == 5 and rate.dropped == 15
    now[0] += 1.0
    assert sum(rate.allow() for _ in range(20)) == 5  # refilled to the burst, not past it


def test_a_scrubber_that_fails_withholds_the_line(monkeypatch):
    monkeypatch.setattr("agentos.core.logs._scrubbers", [lambda text: 1 / 0])
    scrub = logstream.Scrubber(None, None)
    assert scrub("anything") == logstream.WITHHELD


def test_under_systemd_the_lines_come_from_the_journal(monkeypatch, tmp_path):
    commands = []

    class FakeJournal:
        def __init__(self, argv, **kwargs):
            commands.append(argv)
            entry = {"MESSAGE": line(f"from the journal {VAULT_SECRET}").strip(), "PRIORITY": "6"}
            plain = {"MESSAGE": "systemd: Started agentosd.", "PRIORITY": "5", "__REALTIME_TIMESTAMP": "0"}
            self.stdout = iter([json.dumps(entry) + "\n", json.dumps(plain) + "\n"])

        def terminate(self):
            pass

        def wait(self):
            return 0

    monkeypatch.setattr(logstream.subprocess, "Popen", FakeJournal)
    reader = logstream.Reader(tmp_path, logstream.Query({"since": "1h"}), journal=True)
    reader._run()
    got = []
    while not reader.lines.empty():
        got.append(reader.lines.get_nowait())
    scrub = logstream.Scrubber(type("R", (), {"get": lambda self, name: FakeVault()})(), None)
    messages = [logstream.shape(x, scrub)["message"] for x in got if x not in (None, logstream.LIVE)]
    assert messages == ["from the journal [redacted]", "systemd: Started agentosd."]
    assert commands[0][:3] == ["journalctl", "-u", "agentosd"] and "-f" not in commands[0]


def test_a_line_too_long_to_parse_is_read_as_plain_text():
    parsed = logstream._parse("{" + "x" * (logstream.MAX_RAW + 10))
    assert parsed["ts"] == "" and len(parsed["msg"]) == logstream.MAX_INPUT


def test_addresses_query_strings_and_otp_codes_never_leave_in_a_line():
    scrub = logstream.Scrubber(None, None)
    text = scrub("lane exit IP 203.0.113.7 (2a02:6b8::2:242) on https://example.com/a/b?sid=abc123#frag, "
                 "2FA code: 482913, agentos 1.2.3")
    for leaked in ("203.0.113.7", "2a02:6b8", "sid=", "abc123", "#frag", "482913"):
        assert leaked not in text, (leaked, text)
    assert "https://example.com/a/b?..." in text and "agentos 1.2.3" in text


def test_an_address_split_by_the_input_cut_is_not_kept_in_half():
    scrub = logstream.Scrubber(None, None)
    text = "[redacted]" * (logstream.MAX_INPUT // 10)
    raw = text[: logstream.MAX_INPUT - 6] + " 203.0.113.7 tail"
    assert "203.0" not in scrub(raw, limit=100_000)


@pytest.mark.parametrize("ts, at", [
    ("2026-11-04T10:00:00.123Z", "2026-11-04T10:00:00.123Z"),
    ("2026-11-04T10:00:00+00:00", "2026-11-04T10:00:00.000Z"),
    ("password=hunter2", None),
    ("203.0.113.7", None),
    ("", None),
    (12345, None),
])
def test_the_at_field_is_sent_only_when_it_is_a_time(ts, at):
    shaped = logstream.shape({"ts": ts, "msg": "x"}, logstream.Scrubber(None, None))
    assert shaped["at"] == at


def test_a_line_written_while_the_backlog_is_read_is_followed(tmp_path):
    path = tmp_path / "logs" / "agentosd.jsonl"
    path.parent.mkdir()
    path.write_text(line("old"))

    class Racing(logstream.Reader):
        def _backlog(self, follower=None):
            yield from super()._backlog(follower)
            with path.open("a") as handle:  # written after the backlog was read, before following began
                handle.write(line("meanwhile", ago=0))

    reader = Racing(tmp_path, logstream.Query({"since": "10m", "follow": True}), journal=False)
    reader.start()
    got = []
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and [x.get("msg") for x in got].count("meanwhile") < 1:
            try:
                item = reader.lines.get(timeout=0.2)
            except Exception:  # noqa: BLE001, S112
                continue
            if item not in (None, logstream.LIVE):
                got.append(item)
        time.sleep(1.2)  # a second copy would come from the tail
        while not reader.lines.empty():
            item = reader.lines.get_nowait()
            if item not in (None, logstream.LIVE):
                got.append(item)
    finally:
        reader.stop()
    assert [x["msg"] for x in got] == ["old", "meanwhile"]


def test_lines_before_since_are_skipped_without_parsing(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "agentosd.jsonl"
    path.parent.mkdir()
    path.write_text("".join(line(f"old {i}", ago=7200) for i in range(500)) + line("new"))
    calls = []
    real = json.loads
    monkeypatch.setattr(logstream.json, "loads", lambda raw, *a, **k: calls.append(1) or real(raw, *a, **k))
    reader = logstream.Reader(tmp_path, logstream.Query({"since": "10m"}), journal=False)
    reader._run()
    got = [x for x in iter(reader.lines.get_nowait, None) if x is not logstream.LIVE]
    assert [x["msg"] for x in got] == ["new"] and len(calls) < 5


def test_the_backlog_scan_and_each_line_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(logstream, "MAX_RAW", 4096)
    monkeypatch.setattr(logstream, "MAX_SCAN", 64 * 1024)
    path = tmp_path / "logs" / "agentosd.jsonl"
    path.parent.mkdir()
    lines = [line(f"line {i}") for i in range(2000)]
    path.write_text("".join(lines[:1000]) + "z" * 50_000 + "\n" + "".join(lines[1000:]))
    reads = []
    real = logstream._read_line
    monkeypatch.setattr(logstream, "_read_line", lambda handle: reads.append(1) or real(handle))
    reader = logstream.Reader(tmp_path, logstream.Query({"since": "1h"}), journal=False)
    reader._run()
    got = [x["msg"] for x in iter(reader.lines.get_nowait, None) if x is not logstream.LIVE]
    assert got[-1] == "line 1999" and "line 0" not in got  # the newest, within MAX_SCAN bytes
    assert len(reads) < 1500


def test_an_overlong_line_is_cut_and_the_next_line_still_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(logstream, "MAX_RAW", 1000)
    raw = tmp_path / "f"
    raw.write_bytes(b"y" * 10_000 + b"\n" + line("after").encode())
    with raw.open("rb") as handle:
        first, second = logstream._read_line(handle), logstream._read_line(handle)
    assert len(first) <= 1001 and json.loads(second)["msg"] == "after"
