"""What a proof's video and preview are, read by the node itself: an MP4's length and picture size from
its boxes, a GIF's size from its header. The files come from the environment's machine, where the agent
is root, so nothing the machine says about them is taken as it is. Standard library only, no ffprobe on
the node.
"""

from __future__ import annotations

import struct
from pathlib import Path

MOOV_MAX = 16 << 20  # the index of a few minutes of video is far smaller
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    """(type, body start, body end) of each box in data[start:end]."""
    pos, end = start, len(data) if end is None else end
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        head = 8
        if size == 1:
            if pos + 16 > end:
                return
            size, head = struct.unpack(">Q", data[pos + 8:pos + 16])[0], 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            return
        yield kind, pos + head, pos + size
        pos += size


def _moov(path: Path) -> bytes | None:
    """The moov box of an MP4 (after its ftyp), read without loading the media data."""
    with path.open("rb") as f:
        first = f.read(12)
        if len(first) < 12 or first[4:8] != b"ftyp":
            return None
        f.seek(0)
        while True:
            head = f.read(8)
            if len(head) < 8:
                return None
            size, kind = struct.unpack(">I4s", head)
            skip = 8
            if size == 1:
                size, skip = struct.unpack(">Q", f.read(8))[0], 16
            elif size == 0:
                return f.read(MOOV_MAX) if kind == b"moov" else None
            if size < skip:
                return None
            if kind == b"moov":
                return f.read(min(size - skip, MOOV_MAX)) if size - skip <= MOOV_MAX else None
            f.seek(size - skip, 1)


def mp4_facts(path: Path) -> dict | None:
    """{duration_s, width, height} of an MP4 (its movie header and its video track's header), or None
    when it is not one."""
    try:
        moov = _moov(Path(path))
    except (OSError, struct.error):
        return None
    if not moov:
        return None
    duration, width, height = None, 0, 0
    stack = [(0, len(moov))]
    while stack:
        start, end = stack.pop()
        for kind, body, stop in _boxes(moov, start, end):
            if kind in CONTAINERS - {b"moov"}:
                stack.append((body, stop))
            elif kind == b"mvhd" and stop - body >= 32:
                v1 = moov[body] == 1
                scale, length = (struct.unpack(">IQ", moov[body + 20:body + 32]) if v1
                                 else struct.unpack(">II", moov[body + 12:body + 20]))
                duration = round(length / scale, 2) if scale else None
            elif kind == b"tkhd" and stop - body >= 84:
                off = body + (88 if moov[body] == 1 else 76)
                if off + 8 <= stop:
                    w, h = struct.unpack(">II", moov[off:off + 8])
                    if w and h and not width:
                        width, height = w >> 16, h >> 16
    if duration is None:
        return None
    return {"duration_s": duration, "width": width or None, "height": height or None}


def gif_size(path: Path) -> tuple[int, int] | None:
    """A GIF's width and height from its header, or None when it is not one."""
    try:
        with Path(path).open("rb") as f:
            head = f.read(10)
    except OSError:
        return None
    if len(head) < 10 or head[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    w, h = struct.unpack("<HH", head[6:10])
    return w, h
