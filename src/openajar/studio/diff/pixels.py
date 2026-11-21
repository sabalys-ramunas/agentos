"""The pixel half of a diff: which parts of two screenshots differ, on a grid of small cells.

Anti-aliasing is tolerated the way pixelmatch does it in spirit: a pixel counts as changed only
when its colour lies outside the range of the other image's 3 x 3 neighbourhood by more than
`THRESHOLD` in some channel, so a glyph edge rendered a fraction of a pixel apart is not a
change while a solid fill that changed colour is. Images of different heights (a page that
grew) are compared over the taller one, the shorter extended with its own bottom row.

Pillow is imported inside functions; it comes with the studio extra.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from openajar.core.errors import ServiceUnavailable
from openajar.studio.contracts import Rect

THRESHOLD = 10        # per channel, 0..255, beyond the neighbourhood range
CELL = 8              # CSS px per grid cell
MIN_REGION_PX = 6     # fewer changed pixels than this in a region is rounding, not a change
PILLOW_MISSING = ("Pillow is not installed here, so screenshots cannot be compared. "
                  "Install the studio extra (uv sync --extra studio), then call again.")


def pil() -> Any:
    try:
        from PIL import Image
    except ImportError:
        raise ServiceUnavailable(PILLOW_MISSING) from None
    return Image


@dataclass
class Grid:
    """Changed pixels counted per cell. `cell` is in image px; `scale` turns CSS px into them."""

    cols: int
    rows: int
    cell: int
    scale: float
    counts: list[int]           # changed pixels per cell, row by row
    changed: int                # changed pixels in the whole image
    total: int                  # pixels in the whole image
    width: int                  # image px
    height: int

    def cells_of(self, rect: Rect, pad: float = 0.0) -> tuple[int, int, int, int]:
        """The cell columns and rows [c0, c1) x [r0, r1) a CSS rect touches."""
        s = self.cell / self.scale
        c0 = max(0, int((rect.x - pad) // s))
        r0 = max(0, int((rect.y - pad) // s))
        c1 = min(self.cols, int(-(-(rect.x + rect.w + pad) // s)))
        r1 = min(self.rows, int(-(-(rect.y + rect.h + pad) // s)))
        return c0, c1, r0, r1


def _open(data: bytes):
    image = pil()
    with image.open(io.BytesIO(data)) as im:
        return im.convert("RGB")


def _extend(im, w: int, h: int):
    if im.width == w and im.height == h:
        return im
    image = pil()
    out = image.new("RGB", (w, h), im.getpixel((0, max(0, im.height - 1))))
    out.paste(im, (0, 0))
    if im.height < h:   # the rows below: the last row repeated, so a plain background stays plain
        last = im.crop((0, im.height - 1, im.width, im.height)).resize((im.width, h - im.height))
        out.paste(last, (0, im.height))
    return out


def image_size(data: bytes) -> tuple[int, int]:
    image = pil()
    with image.open(io.BytesIO(data)) as im:
        return im.width, im.height


def compare(before: bytes, after: bytes, *, scale: float = 1.0, masks: list[Rect] | None = None,
            threshold: int = THRESHOLD) -> Grid:
    """Changed pixels between two PNGs of one screen, with `masks` (CSS px) left out."""
    image = pil()
    from PIL import ImageChops, ImageDraw, ImageFilter

    a, b = _open(before), _open(after)
    w, h = max(a.width, b.width), max(a.height, b.height)
    a, b = _extend(a, w, h), _extend(b, w, h)
    lo_a, hi_a = a.filter(ImageFilter.MinFilter(3)), a.filter(ImageFilter.MaxFilter(3))
    lo_b, hi_b = b.filter(ImageFilter.MinFilter(3)), b.filter(ImageFilter.MaxFilter(3))
    out_b = ImageChops.lighter(ImageChops.subtract(lo_a, b), ImageChops.subtract(b, hi_a))
    out_a = ImageChops.lighter(ImageChops.subtract(lo_b, a), ImageChops.subtract(a, hi_b))
    d = ImageChops.lighter(out_a, out_b)
    r, g, bl = d.split()
    worst = ImageChops.lighter(ImageChops.lighter(r, g), bl)
    mask = worst.point(lambda v: 255 if v > threshold else 0)
    if masks:
        draw = ImageDraw.Draw(mask)
        for m in masks:
            draw.rectangle((int(m.x * scale), int(m.y * scale),
                            int((m.x + m.w) * scale) - 1, int((m.y + m.h) * scale) - 1), fill=0)
    changed = mask.histogram()[255]
    cell = max(1, round(CELL * scale))
    cols, rows = -(-w // cell), -(-h // cell)
    if cols * cell != w or rows * cell != h:
        padded = image.new("L", (cols * cell, rows * cell), 0)
        padded.paste(mask, (0, 0))
        mask = padded
    # the mean of a cell times its area is the count of changed pixels in it
    means = mask.reduce(cell).tobytes() if cell > 1 else mask.tobytes()
    area = cell * cell
    counts = [round(v * area / 255) if v else 0 for v in means]
    return Grid(cols=cols, rows=rows, cell=cell, scale=scale, counts=counts, changed=changed,
                total=w * h, width=w, height=h)


def regions(grid: Grid, cells: set[int]) -> list[tuple[Rect, int]]:
    """Connected groups of `cells` (touching on a side or a corner) as (CSS rect, changed px),
    biggest first. Groups with fewer than MIN_REGION_PX changed pixels are dropped."""
    out = []
    todo = set(cells)
    cols = grid.cols
    while todo:
        start = todo.pop()
        stack = [start]
        c0 = c1 = start % cols
        r0 = r1 = start // cols
        px = 0
        while stack:
            i = stack.pop()
            px += grid.counts[i]
            c, r = i % cols, i // cols
            c0, c1, r0, r1 = min(c0, c), max(c1, c), min(r0, r), max(r1, r)
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if not dr and not dc:
                        continue
                    nc, nr = c + dc, r + dr
                    if 0 <= nc < cols and 0 <= nr < grid.rows:
                        j = nr * cols + nc
                        if j in todo:
                            todo.remove(j)
                            stack.append(j)
        if px < MIN_REGION_PX:
            continue
        s = grid.cell / grid.scale
        x, y = c0 * s, r0 * s
        out.append((Rect(x=x, y=y, w=min((c1 + 1) * s, grid.width / grid.scale) - x,
                         h=min((r1 + 1) * s, grid.height / grid.scale) - y), px))
    out.sort(key=lambda t: (-t[0].w * t[0].h, t[0].y, t[0].x))
    return out


def side_by_side(before: bytes, after: bytes, rect: Rect, *, scale: float = 1.0, pad: float = 8,
                 max_width: int = 1600) -> bytes:
    """One PNG: the area `rect` (CSS px) in `before` on the left and in `after` on the right."""
    image = pil()
    parts = []
    for data in (before, after):
        im = _open(data)
        left = max(0, int((rect.x - pad) * scale))
        top = max(0, int((rect.y - pad) * scale))
        right = min(im.width, round((rect.x + rect.w + pad) * scale))
        bottom = min(im.height, round((rect.y + rect.h + pad) * scale))
        if right <= left or bottom <= top:   # outside a shorter image: an empty panel of its size
            parts.append(image.new("RGB", (max(1, round((rect.w + 2 * pad) * scale)),
                                           max(1, round((rect.h + 2 * pad) * scale))), (255, 255, 255)))
        else:
            parts.append(im.crop((left, top, right, bottom)))
    gap = 6
    w = parts[0].width + gap + parts[1].width
    h = max(p.height for p in parts)
    out = image.new("RGB", (w, h), (136, 136, 136))
    out.paste(parts[0], (0, 0))
    out.paste(parts[1], (parts[0].width + gap, 0))
    if w > max_width:
        out = out.resize((max_width, max(1, round(h * max_width / w))), image.Resampling.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
