"""One canonical email address (docs/EMAIL.md, "Sending").

`normal_address` is the only way an address gets in: whatever is looked up, counted, compared, stored
or sent has been through it, so what the rules checked is what the server is told. It takes a plain
addr-spec and nothing else, because a mail library reads a comment or a display name away and the
message reaches someone the rules never looked at. `%` and `!` are old routes, and go the same way.

`base_address` is for comparing. `jane+news@acme.com` reaches Jane, so a tag does not get past a
suppression or a daily rule, while the mail still goes to the address as given. It knows `+` only: a
provider's own rules, like Gmail ignoring dots, are not worked out here.
"""

from __future__ import annotations

import re
from typing import Any

from openajar.vault.domains import normalize_host

LOCAL_RE = re.compile(r"[A-Za-z0-9&*+/=?^_`{|}~#$-]+(?:\.[A-Za-z0-9&*+/=?^_`{|}~#$-]+)*")
DOMAIN_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})")
MAX_LOCAL = 64
MAX_ADDRESS = 254


def normal_address(text: Any) -> str | None:
    """The one form of an address, or None when it is not a plain one. The local part is lowered as
    well as the domain: two spellings of a person must not be two people to the rules."""
    if not isinstance(text, str) or len(text) > 2 * MAX_ADDRESS:
        return None
    local, at, domain = text.rpartition("@")
    if not at or len(local) > MAX_LOCAL or not LOCAL_RE.fullmatch(local) or domain.endswith("."):
        return None
    try:
        domain = normalize_host(domain)
    except ValueError:
        return None
    address = f"{local.lower()}@{domain}"
    return address if DOMAIN_RE.fullmatch(domain) and len(address) <= MAX_ADDRESS else None


def base_address(address: str) -> str:
    """A canonical address without its `+tag`: what two addresses of one person have in common."""
    local, _, domain = address.rpartition("@")
    return f"{local.split('+', 1)[0] or local}@{domain}"


def base_sql(column: str) -> str:
    """`base_address` in SQL, for a join."""
    plus = f"instr({column}, '+')"
    at = f"instr({column}, '@')"
    return f"CASE WHEN {plus} > 1 AND {plus} < {at} THEN substr({column}, 1, {plus} - 1) || substr({column}, {at}) ELSE {column} END"
