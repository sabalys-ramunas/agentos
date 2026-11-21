"""What the desktop's helper draws (desk.py imports it from beside itself): the top bar, the dock's glass
pill and the small labels over it, in the openajar look (the Dawn design: a darkened strip of the wallpaper
for the top bar, a frosted pill for the dock, one violet accent for the agent). Pure drawing with PIL: no X
here, so every picture is made once per change and the X server only gets the finished pixels.

Sizes are real screen pixels on the usual 1920x1080 screen.
"""

import os
import subprocess
import time

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ACCENT = (109, 74, 255)        # the agent's violet: the top bar's dot, the commands window's prompt
WHITE = (255, 255, 255)

# the top bar
TOP_H = 54
TOP_PAD_L, TOP_PAD_R = 20, 22
MARK = 24
CAP_TOP, CAP_H = 9, 36
CAP_MAX = 760                  # the capsule's widest, before its words are shortened
TITLE_GAP = 28                 # the least room between the title and the capsule
TITLE_MIN = 220                # the title keeps at least this much, however long the capsule's words

# the dock
ICON = 56
GAP = 12
PAD_X = 12
PAD_TOP = 9
PAD_BOTTOM = 15
PILL_H = PAD_TOP + ICON + PAD_BOTTOM
PILL_R = 24
BOTTOM = 10                    # from the pill to the screen's bottom edge
BAND_H = PILL_H + BOTTOM       # the band the dock reserves (90 px): a maximized window stops above it
SEP = 19                       # the divider's extra room: 3 + 1 + 3 px and one more gap
TIP_H, TIP_ABOVE = 28, 98      # a tooltip's height, and its bottom edge from the screen's bottom


# ---------------------------------------------------------------- fonts


def _font_file(pattern):
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True, timeout=3,
                             check=False)
        return out.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


_FONTS = {}


def font(size, weight=400):
    """Inter at `size` px and `weight` (a variable Inter: its weight and optical size axes), cached."""
    key = (size, weight)
    if key in _FONTS:
        return _FONTS[key]
    path = _font_file("Inter Variable") or _font_file("sans-serif")
    try:
        f = ImageFont.truetype(path, size)
    except (OSError, TypeError, ValueError):
        f = ImageFont.load_default()
    try:
        values = []
        for axis in f.get_variation_axes():
            name = axis.get("name", b"")
            name = name.decode("latin-1", "replace") if isinstance(name, bytes) else str(name)
            lo, hi, default = axis.get("minimum", 0), axis.get("maximum", 0), axis.get("default", 0)
            want = weight if "eight" in name or name == "wght" else (size if "ptical" in name else default)
            values.append(max(lo, min(hi, want)))
        f.set_variation_by_axes(values)
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    _FONTS[key] = f
    return f


def fit(text, f, room):
    """`text` as it fits in `room` px of font `f`: cut at the end with an ellipsis when it is longer."""
    if f.getlength(text) <= room:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if f.getlength(text[:mid].rstrip() + "…") <= room:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + "…") if lo else ""


# ---------------------------------------------------------------- pictures


def icon(state, name, size, color=None):
    """One of the desktop's SVGs (icons/<name>.svg, or <name>.svg beside them) as RGBA at `size`, rendered at
    twice the size and reduced, kept as a PNG. `color` fills what the SVG draws in currentColor."""
    src = os.path.join(state, "icons", f"{name}.svg")
    if not os.path.exists(src):
        src = os.path.join(state, f"{name}.svg")
    tag = f"-{color[0]:02x}{color[1]:02x}{color[2]:02x}" if color else ""
    out = os.path.join(state, "icons", f"{name}-{size}{tag}.png")
    if not os.path.exists(out):
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if color:
            with open(src, encoding="utf-8") as f:
                body = f.read().replace("currentColor", "#{:02x}{:02x}{:02x}".format(*color))
            src = out + ".svg"
            with open(src, "w", encoding="utf-8") as f:
                f.write(body)
        subprocess.run(["rsvg-convert", "-w", str(size * 2), "-h", str(size * 2), src, "-o", out + ".2x"],
                       capture_output=True, timeout=10, check=True)
        Image.open(out + ".2x").convert("RGBA").resize((size, size), Image.LANCZOS).save(out)
        os.unlink(out + ".2x")
        if color:
            os.unlink(src)
    return Image.open(out).convert("RGBA")


def rounded_mask(size, radius, scale=4):
    w, h = size
    big = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * scale - 1, h * scale - 1), radius * scale, fill=255)
    return big.resize(size, Image.LANCZOS)


def ring(size, radius, alpha, inset=0, scale=4):
    """A 1 px line round a rounded box, as an alpha mask (`alpha` 0..255)."""
    w, h = size
    big = Image.new("L", (w * scale, h * scale), 0)
    o = inset * scale
    ImageDraw.Draw(big).rounded_rectangle((o, o, w * scale - 1 - o, h * scale - 1 - o), radius * scale,
                                          outline=alpha, width=scale)
    return big.resize(size, Image.LANCZOS)


def top_light(size, radius, alpha, scale=4):
    """The inset light along a rounded box's top edge (CSS inset 0 1px 0): a 1 px line that follows the
    corners, fading into them."""
    w, h = size
    full = Image.new("L", (w * scale, h * scale), 0)
    d = ImageDraw.Draw(full)
    d.rounded_rectangle((0, 0, w * scale - 1, h * scale - 1), radius * scale, fill=alpha)
    d.rounded_rectangle((0, scale, w * scale - 1, h * scale - 1 + scale), radius * scale, fill=0)
    return full.resize(size, Image.LANCZOS)


def tile(app_icon, tile_icon, size):
    """Another app's own icon on our wrapper tile, so every dock entry has the same shape and light."""
    base = tile_icon.copy() if tile_icon is not None else Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if tile_icon is None:
        ImageDraw.Draw(base).rounded_rectangle((3, 3, size - 4, size - 4), size // 4.3, fill=(69, 76, 94, 255))
    glyph = app_icon.resize((round(size * 0.52),) * 2, Image.LANCZOS)
    off = (size - glyph.size[0]) // 2
    base.alpha_composite(glyph, (off, off - 1))
    return base


def _shadow(size, box, radius, dy, blur, spread, alpha):
    """CSS box-shadow: a rounded box's shadow on a layer of `size`, as an alpha mask."""
    m = Image.new("L", size, 0)
    x0, y0, x1, y1 = box
    ImageDraw.Draw(m).rounded_rectangle((x0 - spread, y0 - spread + dy, x1 + spread, y1 + spread + dy),
                                        max(0, radius + spread), fill=alpha)
    return m.filter(ImageFilter.GaussianBlur(blur / 2))


# ---------------------------------------------------------------- the top bar


def clock(now, tz_min):
    """("Fri 20 Nov", "19:22") at the node's own time (its UTC offset in minutes)."""
    t = time.gmtime(now + tz_min * 60)
    return f"{time.strftime('%a', t)} {t.tm_mday} {time.strftime('%b', t)}", time.strftime("%H:%M", t)


def bar(strip, title, words, mark, now, tz_min):
    """The top bar from `strip` (the wallpaper's top TOP_H rows): the mark and the title on the left, what the
    agent does in a dark glass capsule in the middle, the date and the time on the right. `words` is
    (bold, rest, you): `bold` (the agent's name) in white, `rest` after it, `you` for a person's own
    control (a white dot instead of the violet one)."""
    w = strip.size[0]
    im = strip.convert("RGBA")
    shade = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(shade)
    for y in range(TOP_H):  # a navy fade, darker at the top
        d.line((0, y, w, y), fill=(5, 8, 26, round(255 * (0.62 - 0.32 * y / (TOP_H - 1)))))
    d.line((0, TOP_H - 1, w, TOP_H - 1), fill=(255, 255, 255, 18))
    im.alpha_composite(shade)
    mid = TOP_H // 2
    draw = ImageDraw.Draw(im)

    # the right: the date, then the time
    date, hm = clock(now, tz_min)
    fd, ft = font(17, 500), font(19, 600)
    tw, dw = ft.getlength(hm), fd.getlength(date)
    tx = w - TOP_PAD_R - tw
    base = mid + 7  # one baseline for both
    draw.text((tx, base), hm, font=ft, fill=WHITE + (255,), anchor="ls")
    draw.text((tx - 12 - dw, base), date, font=fd, fill=WHITE + (184,), anchor="ls")
    right_edge = tx - 12 - dw

    # the middle: the capsule
    bold, rest, you = words
    fb, fr = font(17, 600), font(17, 400)
    # centred, so half of it must clear the clock on the right and leave the title some room on the left
    half = min(right_edge - w / 2 - TITLE_GAP, w / 2 - (TOP_PAD_L + MARK + 11 + TITLE_MIN) - TITLE_GAP)
    room = max(120, min(CAP_MAX, int(2 * half)) - 52)
    if bold:
        rest_w = fr.getlength(rest)
        bold = fit(bold, fb, max(60, room - rest_w))
        if fb.getlength(bold) + rest_w > room:
            rest = fit(rest, fr, room - fb.getlength(bold))
    else:
        rest = fit(rest, fr, room)
    text_w = (fb.getlength(bold) if bold else 0) + fr.getlength(rest)
    cw = round(14 + 10 + 10 + text_w + 18)
    cx0 = (w - cw) // 2
    box = (cx0, CAP_TOP, cx0 + cw, CAP_TOP + CAP_H)
    glass = im.crop(box).filter(ImageFilter.GaussianBlur(8))
    glass = ImageEnhance.Brightness(glass).enhance(0.7)
    glass.alpha_composite(Image.new("RGBA", glass.size, (8, 11, 32, 107)))
    glass.paste(Image.new("RGBA", glass.size, WHITE + (255,)), (0, 0), ring(glass.size, CAP_H // 2, 36))
    glass.paste(Image.new("RGBA", glass.size, WHITE + (255,)), (0, 0), top_light(glass.size, CAP_H // 2, 31))
    im.paste(glass, box[:2], rounded_mask(glass.size, CAP_H // 2))
    dot = WHITE if you else ACCENT
    dx, dy = cx0 + 14 + 5, CAP_TOP + CAP_H // 2
    _dot(im, (dx, dy), dot, 0.22 if you else 0.35)
    x = cx0 + 14 + 10 + 10
    draw = ImageDraw.Draw(im)
    if bold:
        draw.text((x, base - 1), bold, font=fb, fill=WHITE + (255,), anchor="ls")
        x += fb.getlength(bold)
    draw.text((x, base - 1), rest, font=fr, fill=WHITE + (204,), anchor="ls")

    # the left: the mark and the title, as far as the capsule leaves room
    lx = TOP_PAD_L
    if mark is not None:
        im.alpha_composite(mark, (lx, mid - MARK // 2))
        lx += MARK + 11
    ftitle = font(19, 600)
    title = fit(title, ftitle, cx0 - TITLE_GAP - lx)
    if title:
        draw.text((lx, base), title, font=ftitle, fill=WHITE + (255,), anchor="ls")
    return im.convert("RGB")


def _dot(im, center, color, halo):
    """The status dot: 10 px with a 3 px halo of its own colour, drawn at 4x and reduced."""
    s = 4
    cx, cy = center
    layer = Image.new("RGBA", (16 * s, 16 * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((0, 0, 16 * s - 1, 16 * s - 1), fill=color + (round(255 * halo),))
    d.ellipse((3 * s, 3 * s, 13 * s - 1, 13 * s - 1), fill=color + (255,))
    im.alpha_composite(layer.resize((16, 16), Image.LANCZOS), (cx - 8, cy - 8))


# ---------------------------------------------------------------- the dock


def layout(width, n_pinned, n_rest):
    """The pill's left edge and width, each icon's x and the divider's x (None without other apps)."""
    n = n_pinned + n_rest
    pw = PAD_X * 2 + n * ICON + (n - 1) * GAP + (SEP if n_rest else 0)
    left = (width - pw) // 2
    xs, x = [], left + PAD_X
    for i in range(n):
        if n_rest and i == n_pinned:
            x += SEP
        xs.append(x)
        x += ICON + GAP
    sep = (left + PAD_X + n_pinned * (ICON + GAP) - GAP + 12 + 3) if n_rest else None
    return left, pw, xs, sep


def band(wall, entries, sep_x, left, pw):
    """The dock's band over the bottom BAND_H rows of `wall` (the wallpaper): a frosted pill (what is behind
    it blurred, livelier and lighter, a lit rim) with each entry's icon and a dot under each one that runs.
    `entries` is [(x, icon, running)]."""
    ww, wh = wall.size
    y0 = wh - BAND_H
    im = wall.crop((0, y0, ww, wh)).convert("RGBA")
    top = BAND_H - BOTTOM - PILL_H
    box = (left, top, left + pw, top + PILL_H)
    im = Image.composite(Image.new("RGBA", im.size, (0, 0, 0, 255)), im,
                         _shadow(im.size, box, PILL_R, 12, 30, -8, 115))
    # what is behind the pill, with room round it for the blur, inside the wallpaper
    around = (max(0, left - 40), max(0, y0 + top - 40), min(ww, left + pw + 40), min(wh, y0 + top + PILL_H + 40))
    src = wall.crop(around).convert("RGBA").filter(ImageFilter.GaussianBlur(20))
    ox, oy = left - around[0], y0 + top - around[1]
    glass = src.crop((ox, oy, ox + pw, oy + PILL_H))
    glass = ImageEnhance.Color(glass).enhance(1.7)
    glass = ImageEnhance.Brightness(glass).enhance(1.5)
    glass.alpha_composite(Image.new("RGBA", glass.size, WHITE + (56,)))
    glass.paste(Image.new("RGBA", glass.size, WHITE + (255,)), (0, 0), ring(glass.size, PILL_R, 82))
    glass.paste(Image.new("RGBA", glass.size, WHITE + (255,)), (0, 0), top_light(glass.size, PILL_R, 140))
    im.paste(glass, box[:2], rounded_mask(glass.size, PILL_R))
    draw = ImageDraw.Draw(im)
    if sep_x is not None:
        draw.line((sep_x, top + PAD_TOP + 6, sep_x, top + PAD_TOP + 6 + 43), fill=WHITE + (102,), width=1)
    for x, pic, running in entries:
        iy = top + PAD_TOP
        im.alpha_composite(pic, (x, iy))
        if running:
            _small_dot(im, (x + ICON / 2, iy + ICON + 7.5))
    return im.convert("RGB")


def _small_dot(im, center):
    s = 4
    layer = Image.new("RGBA", (5 * s, 5 * s), (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((0, 0, 5 * s - 1, 5 * s - 1), fill=WHITE + (242,))
    im.alpha_composite(layer.resize((5, 5), Image.LANCZOS), (round(center[0] - 2.5), round(center[1] - 2.5)))


def label(under, text, dot=False):
    """A tooltip (or the dock's note, with the violet dot) over `under`, what the screen shows there: a dark
    rounded label, 13 px Inter, a faint rim."""
    w, h = under.size
    im = under.convert("RGBA")
    plate = Image.new("RGBA", (w, h), (20, 22, 28, 235))
    im.paste(plate, (0, 0), Image.composite(plate.getchannel("A"), Image.new("L", (w, h), 0), rounded_mask((w, h), 7)))
    im.paste(Image.new("RGBA", (w, h), WHITE + (255,)), (0, 0), ring((w, h), 7, 31))
    f = font(13, 500)
    draw = ImageDraw.Draw(im)
    x = 11
    if dot:
        _dot(im, (11 + 5, h // 2), ACCENT, 0.3)
        x = 11 + 10 + 8
    draw.text((x, h / 2), text, font=f, fill=WHITE + (255,), anchor="lm")
    return im.convert("RGB")


def label_width(text, dot=False):
    return round(font(13, 500).getlength(text)) + 22 + (18 if dot else 0)
