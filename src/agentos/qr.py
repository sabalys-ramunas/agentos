"""A small QR code encoder for the terminal (`agentos members invite` prints the invite link as one).

Byte mode, error correction level M, versions 1 to 10 (up to 213 bytes: an invite link is about
100). It follows ISO/IEC 18004 the way Project Nayuki's reference encoder lays it out: function
patterns, Reed-Solomon blocks, the zigzag placement, all eight masks scored, format and version
bits. No dependency, nothing leaves the machine.
"""

from __future__ import annotations

# level M: error correction codewords per block and blocks, for versions 1..10
_ECC_PER_BLOCK = (10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
_BLOCKS = (1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
_FORMAT_M = 0  # the two format bits of level M (L=1, M=0, Q=3, H=2)
MAX_VERSION = 10


class QrTooLong(ValueError):
    pass


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(len(result)):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < len(result):
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _raw_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        numalign = ver // 7 + 2
        result -= (25 * numalign - 10) * numalign - 55
        if ver >= 7:
            result -= 36
    return result


def data_capacity(ver: int) -> int:
    """Data codewords at level M."""
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver - 1] * _BLOCKS[ver - 1]


def _alignment_positions(ver: int) -> list[int]:
    if ver == 1:
        return []
    size = ver * 4 + 17
    numalign = ver // 7 + 2
    step = (ver * 8 + numalign * 3 + 5) // (numalign * 4 - 4) * 2
    return list(reversed([size - 7 - i * step for i in range(numalign - 1)] + [6]))


def format_bits(mask: int) -> int:
    data = _FORMAT_M << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data << 10 | rem) ^ 0x5412


def version_bits(ver: int) -> int:
    rem = ver
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return ver << 12 | rem


def _codewords(data: bytes, ver: int) -> list[int]:
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in range(n - 1, -1, -1))

    put(0b0100, 4)  # byte mode
    put(len(data), 8 if ver < 10 else 16)
    for b in data:
        put(b, 8)
    capacity = data_capacity(ver) * 8
    put(0, min(4, capacity - len(bits)))
    put(0, (-len(bits)) % 8)
    out = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(out) < capacity // 8:
        out.append(pad)
        pad ^= 0xEC ^ 0x11
    return out


def _interleave(data: list[int], ver: int) -> list[int]:
    nblocks, ecc_len = _BLOCKS[ver - 1], _ECC_PER_BLOCK[ver - 1]
    raw = _raw_modules(ver) // 8
    short = nblocks - raw % nblocks
    short_len = raw // nblocks
    divisor = rs_divisor(ecc_len)
    blocks: list[list[int]] = []
    k = 0
    for i in range(nblocks):
        n = short_len - ecc_len + (0 if i < short else 1)
        dat = data[k:k + n]
        k += n
        ecc = rs_remainder(dat, divisor)
        if i < short:
            dat = dat + [0]
        blocks.append(dat + ecc)
    out = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= short:
                out.append(blk[i])
    return out


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


class _Grid:
    def __init__(self, ver: int):
        self.ver = ver
        self.size = ver * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def set(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def function_patterns(self) -> None:
        n = self.size
        for i in range(n):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _alignment_positions(self.ver)
        last = len(pos) - 1
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.format(0)  # reserve the format areas; drawn again with the chosen mask
        if self.ver >= 7:
            bits = version_bits(self.ver)
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self.set(a, b, bit)
                self.set(b, a, bit)

    def format(self, mask: int) -> None:
        n = self.size
        bits = format_bits(mask)
        bit = [(bits >> i) & 1 == 1 for i in range(15)]
        for i in range(6):
            self.set(8, i, bit[i])
        self.set(8, 7, bit[6])
        self.set(8, 8, bit[7])
        self.set(7, 8, bit[8])
        for i in range(9, 15):
            self.set(14 - i, 8, bit[i])
        for i in range(8):
            self.set(n - 1 - i, 8, bit[i])
        for i in range(8, 15):
            self.set(8, n - 15 + i, bit[i])
        self.set(8, n - 8, True)  # always dark

    def place(self, codewords: list[int]) -> None:
        n = self.size
        total = len(codewords) * 8
        i = 0
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.fixed[y][x] and i < total:
                        self.dark[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        test = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fixed[y][x] and test(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self) -> int:
        n, m = self.size, self.dark
        score = 0
        lines = [m[y] for y in range(n)] + [[m[y][x] for y in range(n)] for x in range(n)]
        finder = ([True, False, True, True, True, False, True, False, False, False, False],
                  [False, False, False, False, True, False, True, True, True, False, True])
        for line in lines:
            run, prev = 0, None
            for v in line:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, v
            if run >= 5:
                score += run - 2
            for k in range(n - 10):
                if line[k:k + 11] in (list(finder[0]), list(finder[1])):
                    score += 40
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)
        score += (abs(dark * 20 - n * n * 10) + n * n - 1) // (n * n) * 10
        return score


def encode(text: str | bytes) -> list[list[bool]]:
    """The QR code for `text` as rows of modules (True: dark), without the quiet zone."""
    data = text.encode() if isinstance(text, str) else text
    for ver in range(1, MAX_VERSION + 1):
        header = 4 + (8 if ver < 10 else 16)
        if header + len(data) * 8 <= data_capacity(ver) * 8:
            break
    else:
        raise QrTooLong(f"{len(data)} bytes do not fit a version {MAX_VERSION} QR code")
    codewords = _interleave(_codewords(data, ver), ver)
    best: _Grid | None = None
    best_score = 0
    for mask in range(8):
        g = _Grid(ver)
        g.function_patterns()
        g.place(codewords)
        g.apply_mask(mask)
        g.format(mask)
        score = g.penalty()
        if best is None or score < best_score:
            best, best_score = g, score
    assert best is not None
    return best.dark


def render(text: str, *, quiet: int = 4, color: bool = True) -> str:
    """Two module rows per line with half blocks. With `color`, black on white whatever the
    terminal's theme (a phone camera reads that best); without it, dark modules are drawn as
    blocks for a light background."""
    modules = encode(text)
    n = len(modules)
    size = n + 2 * quiet

    def dark(x: int, y: int) -> bool:
        x, y = x - quiet, y - quiet
        return 0 <= x < n and 0 <= y < n and modules[y][x]

    lines = []
    for y in range(0, size, 2):
        row = []
        for x in range(size):
            top, bottom = dark(x, y), (y + 1 < size and dark(x, y + 1))
            row.append("█" if top and bottom else "▀" if top else "▄" if bottom else " ")
        line = "".join(row)
        lines.append(f"\x1b[30;47m{line}\x1b[0m" if color else line)
    return "\n".join(lines)
