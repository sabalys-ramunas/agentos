"""CSS colours to OKLab, and the distance between two of them (delta E OK).

Parses what token files and computed styles hold: hex, named colours, rgb()/rgba(), hsl()/hsla(),
hwb(), lab(), lch(), oklab(), oklch() and color() in srgb, srgb-linear, display-p3, a98-rgb,
prophoto-rgb, rec2020, xyz, xyz-d50 and xyz-d65. No gamut mapping: values are compared in OKLab
as they are. The matrices are the ones in the CSS Color 4 sample code.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# Thresholds: below 0.005 absorbs 8-bit
# rounding; 0.02 is one just noticeable difference in OkLCh per CSS Color 4.
EXACT_DE = 0.005
NEAR_DE = 0.02


@dataclass(frozen=True)
class OKLab:
    L: float
    a: float
    b: float
    alpha: float = 1.0

    def css(self) -> str:
        body = f"{_num(self.L)} {_num(self.a)} {_num(self.b)}"
        return f"oklab({body})" if self.alpha >= 0.9995 else f"oklab({body} / {_num(self.alpha, 3)})"

    def hex(self) -> str:
        """The nearest 8-bit sRGB, clipped, for messages a person reads."""
        r, g, b = oklab_to_srgb(self)
        h = "#" + "".join(f"{round(min(1.0, max(0.0, c)) * 255):02x}" for c in (r, g, b))
        if self.alpha < 0.9995:
            h += f"{round(self.alpha * 255):02x}"
        return h


def _num(v: float, places: int = 5) -> str:
    s = f"{v:.{places}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def delta_e(c1: OKLab, c2: OKLab) -> float:
    """Delta E OK: the Euclidean distance in OKLab. Alpha is not part of it."""
    return math.sqrt((c1.L - c2.L) ** 2 + (c1.a - c2.a) ** 2 + (c1.b - c2.b) ** 2)


# --- matrices -----------------------------------------------------------------------------

def _mul(m, v):
    return [m[i][0] * v[0] + m[i][1] * v[1] + m[i][2] * v[2] for i in range(3)]


_LIN_SRGB_TO_XYZ = [
    [0.41239079926595934, 0.357584339383878, 0.1804807884018343],
    [0.21263900587151027, 0.715168678767756, 0.07219231536073371],
    [0.01933081871559182, 0.11919477979462598, 0.9505321522496607],
]
_XYZ_TO_LIN_SRGB = [
    [3.2409699419045226, -1.537383177570094, -0.4986107602930034],
    [-0.9692436362808796, 1.8759675015077202, 0.04155505740717559],
    [0.05563007969699366, -0.20397695888897652, 1.0569715142428786],
]
_LIN_P3_TO_XYZ = [
    [0.4865709486482162, 0.26566769316909306, 0.1982172852343625],
    [0.2289745640697488, 0.6917385218365064, 0.079286914093745],
    [0.0, 0.04511338185890264, 1.043944368900976],
]
_LIN_A98_TO_XYZ = [
    [0.5766690429101305, 0.1855582379065463, 0.1882286462349947],
    [0.29734497525053605, 0.6273635662554661, 0.07529145849399788],
    [0.02703136138641234, 0.07068885253582723, 0.9913375368376388],
]
_LIN_2020_TO_XYZ = [
    [0.6369580483012914, 0.14461690358620832, 0.1688809751641721],
    [0.2627002120112671, 0.6779980715188708, 0.05930171646986196],
    [0.0, 0.028072693049087428, 1.060985057710791],
]
_LIN_PROPHOTO_TO_XYZ_D50 = [
    [0.7977604896723027, 0.13518583717574031, 0.0313493495815248],
    [0.2880711282292934, 0.7118432178101014, 0.00008565396060525902],
    [0.0, 0.0, 0.8251046025104601],
]
_D50_TO_D65 = [
    [0.955473421488075, -0.02309845494876471, 0.06325924320057072],
    [-0.0283697093338637, 1.0099953980813041, 0.021041441191917323],
    [0.012314014864481998, -0.020507649298898964, 1.330365926242124],
]
_XYZ_TO_LMS = [
    [0.8190224379967030, 0.3619062600528904, -0.1288737815209879],
    [0.0329836539323885, 0.9292868615863434, 0.0361446663506424],
    [0.0481771893596242, 0.2642395317527308, 0.6335478284694309],
]
_LMS_TO_OKLAB = [
    [0.2104542683093140, 0.7936177747023054, -0.0040720430116193],
    [1.9779985324311684, -2.4285922420485799, 0.4505937096174110],
    [0.0259040424655478, 0.7827717124575296, -0.8086757549230774],
]
_OKLAB_TO_LMS = [
    [1.0, 0.3963377773761749, 0.2158037573099136],
    [1.0, -0.1055613458156586, -0.0638541728258133],
    [1.0, -0.0894841775298119, -1.2914855480194092],
]
_LMS_TO_XYZ = [
    [1.2268798758459243, -0.5578149944602171, 0.2813910456659647],
    [-0.0405757452148008, 1.1122868032803170, -0.0717110580655164],
    [-0.0763729366746601, -0.4214933324022432, 1.5869240198367816],
]
_D50_WHITE = (0.3457 / 0.3585, 1.0, (1.0 - 0.3457 - 0.3585) / 0.3585)


def _srgb_to_linear(c: float) -> float:
    s = -1.0 if c < 0 else 1.0
    a = abs(c)
    return c / 12.92 if a <= 0.04045 else s * ((a + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    s = -1.0 if c < 0 else 1.0
    a = abs(c)
    return c * 12.92 if a <= 0.0031308 else s * (1.055 * a ** (1 / 2.4) - 0.055)


def _a98_to_linear(c: float) -> float:
    return math.copysign(abs(c) ** (563 / 256), c)


def _prophoto_to_linear(c: float) -> float:
    a = abs(c)
    return c / 16 if a <= 16 / 512 else math.copysign(a ** 1.8, c)


def _rec2020_to_linear(c: float) -> float:
    alpha, beta = 1.09929682680944, 0.018053968510807
    a = abs(c)
    return c / 4.5 if a < beta * 4.5 else math.copysign(((a + alpha - 1) / alpha) ** (1 / 0.45), c)


def xyz_to_oklab(xyz, alpha: float = 1.0) -> OKLab:
    lms = [math.copysign(abs(v) ** (1 / 3), v) for v in _mul(_XYZ_TO_LMS, xyz)]
    L, a, b = _mul(_LMS_TO_OKLAB, lms)
    return OKLab(L, a, b, alpha)


def srgb_to_oklab(r: float, g: float, b: float, alpha: float = 1.0) -> OKLab:
    """sRGB channels in 0..1 (gamma encoded)."""
    lin = [_srgb_to_linear(c) for c in (r, g, b)]
    return xyz_to_oklab(_mul(_LIN_SRGB_TO_XYZ, lin), alpha)


def oklab_to_srgb(c: OKLab) -> tuple[float, float, float]:
    lms = [v ** 3 for v in _mul(_OKLAB_TO_LMS, [c.L, c.a, c.b])]
    lin = _mul(_XYZ_TO_LIN_SRGB, _mul(_LMS_TO_XYZ, lms))
    r, g, b = (_linear_to_srgb(v) for v in lin)
    return r, g, b


def _lab_d50_to_xyz_d65(L: float, a: float, b: float):
    k, e = 24389 / 27, 216 / 24389
    f1 = (L + 16) / 116
    f0 = a / 500 + f1
    f2 = f1 - b / 200
    x = f0 ** 3 if f0 ** 3 > e else (116 * f0 - 16) / k
    y = ((L + 16) / 116) ** 3 if L > k * e else L / k
    z = f2 ** 3 if f2 ** 3 > e else (116 * f2 - 16) / k
    xyz50 = [x * _D50_WHITE[0], y * _D50_WHITE[1], z * _D50_WHITE[2]]
    return _mul(_D50_TO_D65, xyz50)


def _hsl_to_srgb(h: float, s: float, l: float):
    h = h % 360

    def f(n):
        k = (n + h / 30) % 12
        a = s * min(l, 1 - l)
        return l - a * max(-1, min(k - 3, 9 - k, 1))

    return f(0), f(8), f(4)


def _hwb_to_srgb(h: float, w: float, bl: float):
    if w + bl >= 1:
        g = w / (w + bl)
        return g, g, g
    r, g, b = _hsl_to_srgb(h, 1, 0.5)
    return tuple(c * (1 - w - bl) + w for c in (r, g, b))


def from_space(space: str, comps, alpha: float = 1.0) -> OKLab:
    """A colour given as a colour space name and three components (the DTCG structured form, or
    the inside of color()). Components for srgb-like spaces are 0..1; hsl and hwb take
    saturation and lightness as 0..100 as DTCG writes them."""
    c = [0.0 if v in (None, "none") else float(v) for v in comps]
    space = space.lower()
    if space == "srgb":
        return srgb_to_oklab(*c, alpha=alpha)
    if space == "srgb-linear":
        return xyz_to_oklab(_mul(_LIN_SRGB_TO_XYZ, c), alpha)
    if space == "display-p3":
        return xyz_to_oklab(_mul(_LIN_P3_TO_XYZ, [_srgb_to_linear(v) for v in c]), alpha)
    if space == "a98-rgb":
        return xyz_to_oklab(_mul(_LIN_A98_TO_XYZ, [_a98_to_linear(v) for v in c]), alpha)
    if space == "rec2020":
        return xyz_to_oklab(_mul(_LIN_2020_TO_XYZ, [_rec2020_to_linear(v) for v in c]), alpha)
    if space == "prophoto-rgb":
        xyz50 = _mul(_LIN_PROPHOTO_TO_XYZ_D50, [_prophoto_to_linear(v) for v in c])
        return xyz_to_oklab(_mul(_D50_TO_D65, xyz50), alpha)
    if space in ("xyz", "xyz-d65"):
        return xyz_to_oklab(c, alpha)
    if space == "xyz-d50":
        return xyz_to_oklab(_mul(_D50_TO_D65, c), alpha)
    if space == "hsl":
        return srgb_to_oklab(*_hsl_to_srgb(c[0], c[1] / 100, c[2] / 100), alpha=alpha)
    if space == "hwb":
        return srgb_to_oklab(*_hwb_to_srgb(c[0], c[1] / 100, c[2] / 100), alpha=alpha)
    if space == "lab":
        return xyz_to_oklab(_lab_d50_to_xyz_d65(*c), alpha)
    if space == "lch":
        L, C, H = c
        return xyz_to_oklab(_lab_d50_to_xyz_d65(L, C * math.cos(math.radians(H)), C * math.sin(math.radians(H))), alpha)
    if space == "oklab":
        return OKLab(c[0], c[1], c[2], alpha)
    if space == "oklch":
        L, C, H = c
        return OKLab(L, C * math.cos(math.radians(H)), C * math.sin(math.radians(H)), alpha)
    raise ValueError(f"unknown colour space {space!r}")


# --- parsing CSS --------------------------------------------------------------------------

NAMED = dict(
    item.split("=") for item in ["aliceblue=f0f8ff", "antiquewhite=faebd7", "aqua=00ffff", "aquamarine=7fffd4", "azure=f0ffff", "beige=f5f5dc", "bisque=ffe4c4", "black=000000", "blanchedalmond=ffebcd", "blue=0000ff", "blueviolet=8a2be2", "brown=a52a2a", "burlywood=deb887", "cadetblue=5f9ea0", "chartreuse=7fff00", "chocolate=d2691e", "coral=ff7f50", "cornflowerblue=6495ed", "cornsilk=fff8dc", "crimson=dc143c", "cyan=00ffff", "darkblue=00008b", "darkcyan=008b8b", "darkgoldenrod=b8860b", "darkgray=a9a9a9", "darkgreen=006400", "darkgrey=a9a9a9", "darkkhaki=bdb76b", "darkmagenta=8b008b", "darkolivegreen=556b2f", "darkorange=ff8c00", "darkorchid=9932cc", "darkred=8b0000", "darksalmon=e9967a", "darkseagreen=8fbc8f", "darkslateblue=483d8b", "darkslategray=2f4f4f", "darkslategrey=2f4f4f", "darkturquoise=00ced1", "darkviolet=9400d3", "deeppink=ff1493", "deepskyblue=00bfff", "dimgray=696969", "dimgrey=696969", "dodgerblue=1e90ff", "firebrick=b22222", "floralwhite=fffaf0", "forestgreen=228b22", "fuchsia=ff00ff", "gainsboro=dcdcdc", "ghostwhite=f8f8ff", "gold=ffd700", "goldenrod=daa520", "gray=808080", "green=008000", "greenyellow=adff2f", "grey=808080", "honeydew=f0fff0", "hotpink=ff69b4", "indianred=cd5c5c", "indigo=4b0082", "ivory=fffff0", "khaki=f0e68c", "lavender=e6e6fa", "lavenderblush=fff0f5", "lawngreen=7cfc00", "lemonchiffon=fffacd", "lightblue=add8e6", "lightcoral=f08080", "lightcyan=e0ffff", "lightgoldenrodyellow=fafad2", "lightgray=d3d3d3", "lightgreen=90ee90", "lightgrey=d3d3d3", "lightpink=ffb6c1", "lightsalmon=ffa07a", "lightseagreen=20b2aa", "lightskyblue=87cefa", "lightslategray=778899", "lightslategrey=778899", "lightsteelblue=b0c4de", "lightyellow=ffffe0", "lime=00ff00", "limegreen=32cd32", "linen=faf0e6", "magenta=ff00ff", "maroon=800000", "mediumaquamarine=66cdaa", "mediumblue=0000cd", "mediumorchid=ba55d3", "mediumpurple=9370db", "mediumseagreen=3cb371", "mediumslateblue=7b68ee", "mediumspringgreen=00fa9a", "mediumturquoise=48d1cc", "mediumvioletred=c71585", "midnightblue=191970", "mintcream=f5fffa", "mistyrose=ffe4e1", "moccasin=ffe4b5", "navajowhite=ffdead", "navy=000080", "oldlace=fdf5e6", "olive=808000", "olivedrab=6b8e23", "orange=ffa500", "orangered=ff4500", "orchid=da70d6", "palegoldenrod=eee8aa", "palegreen=98fb98", "paleturquoise=afeeee", "palevioletred=db7093", "papayawhip=ffefd5", "peachpuff=ffdab9", "peru=cd853f", "pink=ffc0cb", "plum=dda0dd", "powderblue=b0e0e6", "purple=800080", "rebeccapurple=663399", "red=ff0000", "rosybrown=bc8f8f", "royalblue=4169e1", "saddlebrown=8b4513", "salmon=fa8072", "sandybrown=f4a460", "seagreen=2e8b57", "seashell=fff5ee", "sienna=a0522d", "silver=c0c0c0", "skyblue=87ceeb", "slateblue=6a5acd", "slategray=708090", "slategrey=708090", "snow=fffafa", "springgreen=00ff7f", "steelblue=4682b4", "tan=d2b48c", "teal=008080", "thistle=d8bfd8", "tomato=ff6347", "turquoise=40e0d0", "violet=ee82ee", "wheat=f5deb3", "white=ffffff", "whitesmoke=f5f5f5", "yellow=ffff00", "yellowgreen=9acd32"]
)

_FUNC = re.compile(r"^\s*([a-z-]+)\(\s*(.*?)\s*\)\s*$", re.IGNORECASE | re.DOTALL)


def _split_args(body: str) -> tuple[list[str], str | None]:
    alpha = None
    if "/" in body:
        body, alpha = body.split("/", 1)
        alpha = alpha.strip()
    parts = [p for p in re.split(r"[\s,]+", body.strip()) if p]
    return parts, alpha


def _number(tok: str, pct_scale: float = 1.0) -> float:
    tok = tok.strip()
    if tok == "none":
        return 0.0
    if tok.endswith("%"):
        return float(tok[:-1]) / 100 * pct_scale
    return float(tok)


def _hue(tok: str) -> float:
    tok = tok.strip().lower()
    if tok == "none":
        return 0.0
    for unit, f in (("deg", 1.0), ("grad", 0.9), ("rad", 180 / math.pi), ("turn", 360.0)):
        if tok.endswith(unit):
            return float(tok[: -len(unit)]) * f
    return float(tok)


def _alpha(tok: str | None) -> float:
    if tok is None:
        return 1.0
    return max(0.0, min(1.0, _number(tok)))


def parse_color(text: str) -> OKLab | None:
    """A CSS colour as OKLab, or None when it is not a colour this can read (currentcolor,
    system colours, color-mix, a var() left in)."""
    if not isinstance(text, str):
        return None
    s = text.strip().lower()
    if not s:
        return None
    if s == "transparent":
        return OKLab(0.0, 0.0, 0.0, 0.0)
    if s in NAMED:
        s = "#" + NAMED[s]
    if s.startswith("#"):
        return _parse_hex(s[1:])
    m = _FUNC.match(s)
    if not m:
        return None
    fn, body = m.group(1), m.group(2)
    if "var(" in body or "calc(" in body or body.startswith("from "):
        return None
    reader = _COLOR_FUNCTIONS.get(fn)
    if reader is None:
        return None
    try:
        return reader(fn, body)
    except (ValueError, ZeroDivisionError):
        return None


def _parse_hex(h: str) -> OKLab | None:
    if len(h) in (3, 4):
        h = "".join(ch * 2 for ch in h)
    if len(h) not in (6, 8) or not re.fullmatch(r"[0-9a-f]+", h):
        return None
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
    return srgb_to_oklab(r, g, b, a)


def _three_and_alpha(body: str) -> tuple[list[str], str | None] | None:
    """The three channels and the alpha of a legacy or modern rgb()/hsl()/hwb(), or None."""
    parts, alpha = _split_args(body)
    if alpha is None and len(parts) == 4:
        alpha = parts.pop()
    return (parts, alpha) if len(parts) == 3 else None


def _rgb_function(fn: str, body: str) -> OKLab | None:
    args = _three_and_alpha(body)
    if args is None:
        return None
    parts, alpha = args
    ch = [_number(p, 255.0) / 255 for p in parts]
    return srgb_to_oklab(*ch, alpha=_alpha(alpha))


def _hsl_function(fn: str, body: str) -> OKLab | None:
    args = _three_and_alpha(body)
    if args is None:
        return None
    parts, alpha = args
    h = _hue(parts[0])
    p1 = float(parts[1].rstrip("%")) if parts[1] != "none" else 0.0
    p2 = float(parts[2].rstrip("%")) if parts[2] != "none" else 0.0
    return from_space("hwb" if fn == "hwb" else "hsl", [h, p1, p2], _alpha(alpha))


def _lab_function(fn: str, body: str) -> OKLab | None:
    """lab(), oklab(), lch() and oklch(): percentages scale to each channel's reference range."""
    parts, alpha = _split_args(body)
    if len(parts) != 3:
        return None
    if fn == "lab":
        comps = [_number(parts[0], 100.0), _number(parts[1], 125.0), _number(parts[2], 125.0)]
    elif fn == "oklab":
        comps = [_number(parts[0], 1.0), _number(parts[1], 0.4), _number(parts[2], 0.4)]
    elif fn == "lch":
        comps = [_number(parts[0], 100.0), _number(parts[1], 150.0), _hue(parts[2])]
    else:
        comps = [_number(parts[0], 1.0), _number(parts[1], 0.4), _hue(parts[2])]
    return from_space(fn, comps, _alpha(alpha))


def _color_function(fn: str, body: str) -> OKLab | None:
    """color(<space> c1 c2 c3 [/ alpha])."""
    parts, alpha = _split_args(body)
    if len(parts) != 4:
        return None
    space, comps = parts[0], [_number(p) for p in parts[1:]]
    return from_space(space, comps, _alpha(alpha))


# A CSS colour function, as the reader of its arguments.
_COLOR_FUNCTIONS = {"rgb": _rgb_function, "rgba": _rgb_function, "hsl": _hsl_function, "hsla": _hsl_function,
                    "hwb": _hsl_function, "lab": _lab_function, "oklab": _lab_function, "lch": _lab_function,
                    "oklch": _lab_function, "color": _color_function}
