"""Profile refusals with stable codes (catalogued in agentos/core/error_catalog.toml)."""

from __future__ import annotations

from agentos.core.errors import InvalidRequest


class ProfileInvalid(InvalidRequest):
    """The profile does not validate, or a value is too long for a site one of its accounts is on.
    Nothing was saved."""
    code = "profile_invalid"


class ProfileSecret(ProfileInvalid):
    """A value in the profile looks like a credential (a key, a token, a password). Profiles never
    hold secrets: they name vault entries. Nothing was saved; the message names the field, never
    the value."""
    code = "secret_in_profile"


__all__ = ["ProfileInvalid", "ProfileSecret"]
