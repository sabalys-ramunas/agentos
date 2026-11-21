"""The shop's data, from a seed. The site on the machine and the checkers on the harness both import
this file, so a checker knows the right answer without asking the site."""

from __future__ import annotations

import hashlib
import random

ADJECTIVES = ["Kestrel", "Juniper", "Basalt", "Harbor", "Lumen", "Tundra", "Saffron", "Cobalt", "Willow",
              "Ember", "Quartz", "Meadow"]
NOUNS = ["Tent", "Lantern", "Kettle", "Backpack", "Hammock", "Stove"]
PAGE_SIZE = 10
COUNTRIES = ["Lithuania", "Latvia", "Estonia", "Poland", "Finland"]
USERNAME, PASSWORD = "mira", "tomato-ladder-42"
DRAFTS = ["Spring sale banner", "Old price list", "Team photo caption"]


def _n(seed: int, salt: str, low: int, high: int) -> int:
    digest = hashlib.sha256(f"{salt}:{seed}".encode()).hexdigest()
    return low + int(digest[:8], 16) % (high - low + 1)


def catalogue(seed: int) -> list[dict]:
    """36 products in a seeded order with seeded prices (cents)."""
    rng = random.Random(1000 + seed)
    products = [{"name": f"{a} {n}"} for a in ADJECTIVES for n in NOUNS][:36]
    rng.shuffle(products)
    for i, p in enumerate(products):
        p["id"] = 100 + i
        p["price_cents"] = rng.randrange(1500, 24000, 5)
    return products


def target(seed: int) -> dict:
    """The product a task asks about: never on the first page of the catalogue."""
    return catalogue(seed)[PAGE_SIZE + _n(seed, "target", 0, 2 * PAGE_SIZE - 1)]


def money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def order(seed: int) -> dict:
    """The order a task asks for: two products, a quantity each, and the buyer's details."""
    items = catalogue(seed)
    first, second = items[_n(seed, "o1", 0, 9)], items[_n(seed, "o2", 12, 30)]
    lines = [{"id": first["id"], "name": first["name"], "qty": _n(seed, "q1", 1, 3)},
             {"id": second["id"], "name": second["name"], "qty": 1}]
    total = sum(next(p["price_cents"] for p in items if p["id"] == line["id"]) * line["qty"] for line in lines)
    return {"lines": lines, "name": "Mira Jonaite", "email": "mira@example.test",
            "country": COUNTRIES[_n(seed, "country", 0, len(COUNTRIES) - 1)],
            "delivery": ["standard", "express"][_n(seed, "delivery", 0, 1)], "gift": bool(_n(seed, "gift", 0, 1)),
            "total_cents": total}


def report_code(seed: int) -> str:
    return f"RPT-{_n(seed, 'report', 100000, 999999)}"


def login_code(seed: int) -> str:
    return str(_n(seed, "code", 100000, 999999))


def account_number(seed: int) -> str:
    return f"LT{_n(seed, 'acct', 10**9, 10**10 - 1)}"


def draft_to_delete(seed: int) -> str:
    return DRAFTS[_n(seed, "draft", 0, len(DRAFTS) - 1)]
