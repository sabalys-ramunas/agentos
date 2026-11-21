"""Audit errors. New classes only: they subclass the core errors, so the API maps them by class."""

from __future__ import annotations

from openajar.core.errors import InvalidRequest, NotFound


class ShareInvalid(NotFound):
    """A share link that is malformed, forged, expired or revoked. One code for all of them, so a
    link that does not work says why without saying more."""

    code = "share_invalid"


class BundleInvalid(InvalidRequest):
    """An export bundle that cannot be read at all (not a zip, no manifest). A bundle that reads
    but does not verify is a verify report, not this error."""

    code = "bundle_invalid"
