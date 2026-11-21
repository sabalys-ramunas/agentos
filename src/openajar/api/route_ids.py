"""What the resource routes share: an id from a route's path, checked, and the grant family a
token acts under."""

from __future__ import annotations

import re
from typing import Any

from starlette.requests import Request

from openajar.core.errors import NotFound

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def path_id(request: Request, key: str = "id") -> str:
    value = request.path_params[key]
    if not ID_RE.match(value):
        raise NotFound(f"No item {value!r}.")
    return value


def grant_family(access: Any) -> str | None:
    """The root grant id of the grant a token acts under: the family every grant narrowed from it
    belongs to. A narrowed grant's own id (`access.grant_id`) is only its leaf, so a check on it
    misses what the family's parent asked for."""
    grant = getattr(getattr(access, "ctx", None), "grant", None)
    return getattr(grant, "root_id", None) or access.grant_id
