"""Where a VM is made when the provider has no room for the planned server type right now.

The planned type is tried first in the planned location, then in the other locations of the same
network zone (nbg1, fsn1 and hel1 are all eu-central): an image and a snapshot can be used in every
location, and the firewall and the SSH key are project-wide. Only then the plan's fallback types,
each in the same order. The plan never holds a type whose disk is smaller than the image's (the
planner reads the image's disk size), and a type the provider refuses for its disk is skipped in
every location. When every try fails, NoRoom says what was tried and what to do next.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from openajar.vms.models import VmError

log = logging.getLogger("openajar.vms")

# The provider is out of this type here right now: the next location or type may have room.
RETRY_CODES = frozenset({"resource_unavailable", "placement_error", "unavailable"})
# Hetzner's words when an image does not fit a type's disk (service_error): that type fails everywhere.
DISK_WORDS = "disk is bigger than server type disk"
# Hetzner's network zones, for a provider that cannot list its locations right now.
ZONES = {"nbg1": "eu-central", "fsn1": "eu-central", "hel1": "eu-central", "ash": "us-east", "hil": "us-west",
         "sin": "ap-southeast"}
RETRY_WAIT_WORDS = "try again in 5 minutes"


class NoRoom(VmError):
    """Every server type and location tried had no room now. `tried` is [(server type, location)]."""

    def __init__(self, message: str, tried: list[tuple[str, str]]):
        super().__init__(message, provider_code="resource_unavailable")
        self.tried = tried


def zone_locations(location: str, zones: dict[str, str]) -> list[str]:
    """`location` first, then the other locations of its network zone, in the order the provider lists them."""
    zone = zones.get(location)
    return [location, *[loc for loc, z in zones.items() if zone is not None and z == zone and loc != location]]


def offered(types: list[dict] | None, server_type: str, location: str) -> tuple[bool, float | None]:
    """Is `server_type` sold in `location` now (by the provider's list), and its price there. Unknown
    (no list): not offered, so only the planned location is tried."""
    for t in types or []:
        if t.get("name") != server_type:
            continue
        price = (t.get("price_hourly_eur") or {}).get(location)
        where = t.get("available")
        return price is not None and (where is None or location in where), price
    return False, None


def listed(types: list[dict], zones: dict[str, str], server_types: list[str], location: str,
           skip: set[tuple[str, str]] | frozenset = frozenset()) -> list[tuple[str, str]]:
    """(server type, location) of `server_types` in the network zone of `location` that the provider's
    list (`types`, its `available` locations) says can be ordered right now, minus `skip` (the ones that
    just had no room). A type whose `available` is None (the provider does not say) is never listed."""
    out = []
    for name in server_types:
        where = next((t.get("available") for t in types if t.get("name") == name), None)
        if where is not None:
            out += [(name, loc) for loc in zone_locations(location, zones) if loc in where and (name, loc) not in skip]
    return out


def plan_tries(first: tuple[str, float | None], fallbacks: list[tuple[str, float | None]], location: str,
               zones: dict[str, str], types: list[dict] | None) -> list[tuple[str, str, float | None]]:
    """(server type, location, EUR an hour) in the order they are tried: each type in the planned
    location, then in the other locations of its zone that sell it now."""
    out: list[tuple[str, str, float | None]] = []
    for server_type, price in [first, *fallbacks]:
        out.append((server_type, location, price))
        for loc in zone_locations(location, zones)[1:]:
            ok, there = offered(types, server_type, loc)
            if ok:
                out.append((server_type, loc, there if there is not None else price))
    return out


def _places(locations: list[str]) -> str:
    return locations[0] if len(locations) == 1 else f"{', '.join(locations[:-1])} or {locations[-1]}"


def no_room_words(provider: str, tried: list[tuple[str, str]]) -> str:
    """"Hetzner has no room for cx23 in nbg1, fsn1 or hel1 now, nor for cpx22 in nbg1; try again in 5 minutes"."""
    by_type: dict[str, list[str]] = {}
    for server_type, loc in tried:
        by_type.setdefault(server_type, [])
        if loc not in by_type[server_type]:
            by_type[server_type].append(loc)
    parts = [f"{t} in {_places(locs)}" for t, locs in by_type.items()]
    name = provider.capitalize() if provider else "The provider"
    head = f"{name} has no room for {parts[0]} now"
    if len(parts) > 1:
        head += ", nor for " + ", ".join(parts[1:])
    return f"{head}; {RETRY_WAIT_WORDS}"


async def create_first(provider: str, tries: list[tuple[str, str, float | None]],
                       create: Callable[[str, str], Awaitable[Any]], *, vm_id: str = ""
                       ) -> tuple[Any, str, str, float | None]:
    """Create the server with the first (type, location) that has room: (server, type, location, price).
    Raises NoRoom when none had room, or the provider's own error for anything else."""
    tried: list[tuple[str, str]] = []
    skip: set[str] = set()
    last: VmError | None = None
    for i, (server_type, loc, price) in enumerate(tries):
        if server_type in skip:
            continue
        try:
            return await create(server_type, loc), server_type, loc, price
        except VmError as e:
            if DISK_WORDS in e.message:
                skip.add(server_type)  # the image does not fit this type anywhere
            elif e.provider_code not in RETRY_CODES:
                raise
            tried.append((server_type, loc))
            last = e
            nxt = next(((t, lc) for t, lc, _p in tries[i + 1:] if t not in skip), None)
            if nxt is not None:
                log.info("VM %s: %s has no room in %s (%s), trying %s in %s", vm_id, server_type, loc,
                         e.provider_code or e.message[:80], *nxt)
    if not tried:
        raise VmError("no server type to try")
    if last is not None and DISK_WORDS in last.message and all(t in skip for t, _lc in tried):
        raise VmError(f"{provider.capitalize() or 'The provider'}: the image does not fit the disk of "
                      f"{', '.join(dict.fromkeys(t for t, _lc in tried))}; ask for a bigger size",
                      provider_code="image_too_big")
    raise NoRoom(no_room_words(provider, tried), tried)
