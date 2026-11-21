"""Colour maths for contrast.text: WCAG 2.x ratio (the pass or fail), APCA Lc (an extra number
only, since APCA is not part of the WCAG 3 draft), alpha compositing and a suggested text colour."""

from __future__ import annotations

RGB = tuple[float, float, float]
RGBA = tuple[float, float, float, float]

WHITE: RGB = (255.0, 255.0, 255.0)


def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb: RGB) -> float:
    """WCAG 2.x relative luminance of an sRGB colour (0 to 255 per channel)."""
    r, g, b = rgb[:3]
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def ratio(fg: RGB, bg: RGB) -> float:
    """WCAG 2.x contrast ratio, 1 to 21."""
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def over(top: RGBA | list[float], bottom: RGB) -> RGB:
    """`top` (with alpha 0 to 1) painted over an opaque `bottom`."""
    r, g, b, a = (list(top) + [1.0])[:4]
    return (r * a + bottom[0] * (1 - a), g * a + bottom[1] * (1 - a), b * a + bottom[2] * (1 - a))


def flatten(layers_top_first: list[list[float]], canvas: RGB = WHITE) -> RGB:
    """The colour seen through background layers listed from the nearest to the farthest."""
    bg = canvas
    for layer in reversed(layers_top_first):
        bg = over(layer, bg)
    return bg


def apca_lc(text: RGB, bg: RGB) -> float:
    """APCA Lc (0.0.98G-4g constants). Positive for dark text on light, negative for light on dark."""

    def y(c: RGB) -> float:
        return sum(((v / 255.0) ** 2.4) * k for v, k in zip(c[:3], (0.2126729, 0.7151522, 0.0721750), strict=True))

    def clamp(v: float) -> float:
        return v if v > 0.022 else v + (0.022 - v) ** 1.414

    yt, yb = clamp(y(text)), clamp(y(bg))
    if abs(yb - yt) < 0.0005:
        return 0.0
    if yb > yt:
        sapc = (yb**0.56 - yt**0.57) * 1.14
        out = 0.0 if sapc < 0.1 else sapc - 0.027
    else:
        sapc = (yb**0.65 - yt**0.62) * 1.14
        out = 0.0 if sapc > -0.1 else sapc + 0.027
    return round(out * 100, 1)


def to_hex(rgb: RGB | list[float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v))):02x}" for v in list(rgb)[:3])


def suggest(fg: RGB, bg: RGB, minimum: float) -> str | None:
    """The nearest colour on the line from `fg` to black or to white that meets `minimum` on `bg`."""
    best: tuple[int, RGB] | None = None
    for target in ((0.0, 0.0, 0.0), WHITE):
        for step in range(1, 101):
            t = step / 100
            c = tuple(f + (g - f) * t for f, g in zip(fg, target, strict=True))
            if ratio(c, bg) >= minimum + 0.05:
                if best is None or step < best[0]:
                    best = (step, c)  # type: ignore[assignment]
                break
    return to_hex(best[1]) if best else None
