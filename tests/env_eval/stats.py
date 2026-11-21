"""How sure a change in a pass rate is (docs/EVALS.md, "How sure a number is"). Merged from the evals/ suite of
2026-11-20: a pass rate with its 95% interval, and whether two rates differ more than chance would make them.

With one run of a task on each side (the free scripts, which are deterministic) any drop counts. With repeats
(a model's runs, `--repeat 3`), a task's finished rate changes for real when Fisher's exact test gives p below
0.05, "likely" below 0.15; 3 of 3 against 0 of 3 is only likely (p = 0.10), 5 of 5 against 0 of 5 is real
(p = 0.008), which is why a release compares 5 runs a side. Anything else is no real change.
"""

from __future__ import annotations

import math

P_REAL, P_LIKELY = 0.05, 0.15


def wilson(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% interval of a pass rate (Wilson's score interval: sound for small n and for 0 or n passes)."""
    if n == 0:
        return (0.0, 1.0)
    p = passes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def fisher(pass_a: int, n_a: int, pass_b: int, n_b: int) -> float:
    """Two-sided Fisher's exact test: how likely a split this uneven is when nothing changed."""
    if n_a == 0 or n_b == 0:
        return 1.0
    total_pass, n = pass_a + pass_b, n_a + n_b

    def prob(k: int) -> float:
        return math.comb(n_a, k) * math.comb(n_b, total_pass - k) / math.comb(n, total_pass)

    observed = prob(pass_a)
    low, high = max(0, total_pass - n_b), min(n_a, total_pass)
    return min(1.0, sum(p for k in range(low, high + 1) if (p := prob(k)) <= observed * (1 + 1e-9)))


def strength(pass_a: int, n_a: int, pass_b: int, n_b: int) -> tuple[str | None, float]:
    """('real' | 'likely' | None, p) for a change from a's rate to b's."""
    p = fisher(pass_a, n_a, pass_b, n_b)
    return ("real" if p < P_REAL else "likely" if p < P_LIKELY else None), p
