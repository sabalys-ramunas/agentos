"""Raster pictures for the fake tabs: an RGB canvas with boxes, encoded as PNG for the panel and tools."""

from __future__ import annotations

import struct
import zlib

Color = tuple[int, int, int]


class Raster:
    """A width x height picture, 3 bytes a pixel, row by row. Built once, then only read."""

    def __init__(self, width: int, height: int, background: Color = (255, 255, 255)):
        self.width = width
        self.height = height
        self.rgb = bytearray(bytes(background) * (width * height))
        self._png: bytes | None = None

    def rect(self, x: int, y: int, w: int, h: int, color: Color) -> None:
        x0, y0 = max(x, 0), max(y, 0)
        x1, y1 = min(x + w, self.width), min(y + h, self.height)
        if x1 <= x0 or y1 <= y0:
            return
        run = bytes(color) * (x1 - x0)
        for row in range(y0, y1):
            start = (row * self.width + x0) * 3
            self.rgb[start:start + len(run)] = run

    def outline(self, x: int, y: int, w: int, h: int, color: Color, thickness: int = 2) -> None:
        self.rect(x, y, w, thickness, color)
        self.rect(x, y + h - thickness, w, thickness, color)
        self.rect(x, y, thickness, h, color)
        self.rect(x + w - thickness, y, thickness, h, color)

    def png(self) -> bytes:
        if self._png is None:
            self._png = encode_png(self)
        return self._png


def encode_png(raster: Raster) -> bytes:
    stride = raster.width * 3
    raw = b"".join(b"\x00" + bytes(raster.rgb[row * stride:(row + 1) * stride]) for row in range(raster.height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    header = struct.pack(">IIBBBBB", raster.width, raster.height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))
