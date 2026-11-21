"""Small statistics the red team's detectors use: medians, percentiles, skewness and the
Kolmogorov-Smirnov tests that tell a uniform or machine-made spread of timings from a human one.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence


def median(xs: Sequence[float]) -> float | None:
    return statistics.median(xs) if xs else None


def percentile(xs: Sequence[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def ks_uniform(xs: Sequence[float]) -> float | None:
    """Kolmogorov-Smirnov distance between the sample and a uniform distribution over its own range.
    Near 0: the values look drawn from `random.uniform(a, b)`. People's timings are skewed (log-normal)
    and sit far from it."""
    if len(xs) < 3:
        return None
    s = sorted(xs)
    lo, hi = s[0], s[-1]
    if hi - lo <= 0:
        return 1.0
    n = len(s)
    return max(max(abs((i + 1) / n - (x - lo) / (hi - lo)), abs(i / n - (x - lo) / (hi - lo)))
               for i, x in enumerate(s))


def looks_uniform(xs: Sequence[float]) -> bool:
    """True when the sample cannot be told from a flat random range (KS under 1.22/sqrt(n), p above
    about 0.1) and it is not skewed. Needs 60 values (at 30, a steady typist is flagged a third of
    the time; at 60, 6%; a flat range 95%). People's timings are log-normal, skewed well
    past 0.6, and fail this on the skew alone."""
    if len(xs) < 60:
        return False
    ks, sk = ks_uniform(xs), skewness(xs)
    return ks is not None and ks < 1.22 / math.sqrt(len(xs)) and (sk is None or abs(sk) < 0.6)


def ks_2samp(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Two-sample Kolmogorov-Smirnov distance (0 the same distribution, 1 disjoint)."""
    if len(a) < 5 or len(b) < 5:
        return None
    sa, sb = sorted(a), sorted(b)
    i = j = 0
    d = 0.0
    while i < len(sa) and j < len(sb):
        x = min(sa[i], sb[j])
        while i < len(sa) and sa[i] <= x:
            i += 1
        while j < len(sb) and sb[j] <= x:
            j += 1
        d = max(d, abs(i / len(sa) - j / len(sb)))
    return d


def ks_critical(n: int, m: int, alpha_c: float = 1.95) -> float:
    """The KS distance two samples of these sizes exceed by chance with p about 0.001."""
    return alpha_c * math.sqrt((n + m) / (n * m))


def skewness(xs: Sequence[float]) -> float | None:
    if len(xs) < 3:
        return None
    m = statistics.fmean(xs)
    sd = statistics.pstdev(xs)
    if sd == 0:
        return 0.0
    return sum(((x - m) / sd) ** 3 for x in xs) / len(xs)


def rounded(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x, nd)
