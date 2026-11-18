"""What a paid run may cost, before it runs: an estimate per session, and the guard (`--max-usd`).

The estimate is deliberately high: a session's tokens as a long one of these tasks spends them
(the tool list and the skill cached once, about fifteen turns that read the context again from the
cache, a few thousand tokens of output), priced at the dearest model an alias can mean. The real
cost comes from the transcript afterwards (Claude Code's `total_cost_usd`). The hard limit is
Claude Code's own `--max-budget-usd`, given each session as its share of `--max-usd`, so a run
cannot spend more than that whatever the estimate said.
"""

from __future__ import annotations

# USD per million tokens: input, output, cache write, cache read. An alias is priced as the dearest
# model it may mean (haiku: Haiku 4.5, not Haiku 5.5's tenth of it); a full model id may be added.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "haiku": (1.00, 5.00, 1.25, 0.10),
    "sonnet": (3.00, 15.00, 3.75, 0.30),
    "opus": (5.00, 25.00, 6.25, 0.50),
    "fable": (10.00, 50.00, 12.50, 1.00),
}
# A long session of these tasks, in tokens (input not cached, output, cache written, cache read).
SESSION_TOKENS = (10_000, 6_000, 50_000, 400_000)
FREE_AGENTS = ("perfect", "careless")
DEFAULT_MAX_USD = 2.0


class OverBudget(Exception):
    pass


def price_of(model: str) -> tuple[float, float, float, float] | None:
    m = model.lower()
    for name, price in PRICES.items():
        if name in m:
            return price
    return None


def session_usd(model: str) -> float | None:
    price = price_of(model)
    if price is None:
        return None
    return round(sum(t * p for t, p in zip(SESSION_TOKENS, price, strict=True)) / 1e6, 4)


def estimate(agent: str, model: str, sessions: int) -> float | None:
    """The run's estimated cost in USD: 0 for a scripted agent, None when the model's price is unknown."""
    if agent in FREE_AGENTS:
        return 0.0
    each = session_usd(model)
    return None if each is None else round(each * sessions, 2)


def guard(agent: str, model: str, sessions: int, max_usd: float) -> tuple[float | None, float]:
    """(the estimate, each session's cap). Raises OverBudget when the estimate is above max_usd, or the
    model's price is unknown (a model whose name holds none of the aliases above)."""
    est = estimate(agent, model, sessions)
    if agent in FREE_AGENTS:
        return 0.0, 0.0
    if est is None:
        raise OverBudget(f"no price known for the model {model!r}: use haiku, sonnet, opus or fable in its name")
    if est > max_usd:
        raise OverBudget(f"the estimate is ${est:.2f} for {sessions} sessions on {model}, above --max-usd "
                         f"{max_usd:.2f}: run fewer tasks or repeats, or raise --max-usd")
    return est, round(max_usd / max(1, sessions), 2)
