"""Engagement refusals with stable codes (catalogued in agentos/core/error_catalog.toml)."""

from __future__ import annotations

from agentos.core.errors import InvalidRequest, PolicyDenied


class ProgramInvalid(InvalidRequest):
    """The program does not validate. Nothing was saved."""
    code = "program_invalid"


class OwnerOnly(PolicyDenied):
    """Only the owner may do this: approve a program, raise a cap, choose `normal` risk, turn on
    `auto` comments or the `followers_of` source (docs/design/engagement-engine.md 5.10)."""
    code = "owner_only"


class CapCeiling(InvalidRequest):
    """The owner asked for a cap above the hard ceiling (the `normal` column of the caps table)."""
    code = "cap_ceiling"


__all__ = ["CapCeiling", "OwnerOnly", "ProgramInvalid"]
