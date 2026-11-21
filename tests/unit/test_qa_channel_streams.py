"""QA for the channel's stream table: how a stream id matches the binary frames that carry it.

A stream id is a ULID. The frames carry its 16 raw bytes and come back as the canonical (upper case)
text, so the table must key streams the same way. Bugs are strict xfails pinned to their reason.
"""

from __future__ import annotations

import pytest

from openajar.node.channel import wire
from openajar.node.channel.streams import StreamTable


def table() -> StreamTable:
    async def send_text(envelope):
        return None

    async def send_binary(frame):
        return None

    return StreamTable(send_text, send_binary)


async def test_a_stream_opened_with_its_canonical_id_receives_its_frames():
    streams = table()
    stream_id = wire.ulid()
    pipe = streams.new(stream_id, "file")
    await streams.on_binary(wire.stream_frame(stream_id, b"hello"))
    assert pipe.received_bytes == 5 and pipe.inbox.get_nowait() == b"hello"


async def test_a_frame_for_a_stream_that_is_not_open_is_dropped_without_closing_anything():
    streams = table()
    pipe = streams.new(wire.ulid(), "file")
    await streams.on_binary(wire.stream_frame(wire.ulid(), b"stray"))
    assert pipe.received_bytes == 0 and not pipe.closed


async def test_a_lower_case_stream_id_is_accepted_and_its_frames_still_arrive():
    streams = table()
    stream_id = wire.ulid()
    lower = stream_id.lower()
    assert wire.is_ulid(lower)  # the open is accepted, so the frames have to reach it
    pipe = streams.new(lower, "file")
    await streams.on_binary(wire.stream_frame(lower, b"hello"))
    assert pipe.received_bytes == 5


async def test_a_writer_with_no_credit_gives_up_as_stalled():
    from openajar.node.channel.streams import STALL_AFTER, StreamStalled

    assert table().new(wire.ulid(), "vnc").stall_after == STALL_AFTER  # every stream has a stall timeout

    async def send(_):
        return None

    pipe = StreamTable(send, send, stall_after=0.05).new(wire.ulid(), "vnc", credit=0)
    with pytest.raises(StreamStalled):
        await pipe.write(b"x")
