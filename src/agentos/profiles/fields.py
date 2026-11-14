"""The small pieces every profile model builds on: the field limits, the photo digest, the strict base
model, and how a typed value is cleaned. agentos.profiles.models and agentos.profiles.sections both
import them, so neither imports the other in a circle.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict

PHOTO_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# What a field may hold. The site limits (agentos.profiles.mapping.SITES) are
# checked when a profile is resolved for an account, so one long bio can still serve sites that
# take it while a site that does not reports a resolve problem.
LIMITS = {"display_name": 100, "first": 50, "last": 50, "headline": 220, "bio.short": 300, "bio.long": 2600,
          "location": 100, "url": 300, "voice.personality": 1000, "voice.tone": 200, "voice.rule": 200,
          "voice.example": 500, "item": 100, "work.description": 2000}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def clean_text(value: Any, what: str, limit: int, *, lines: bool = False) -> str:
    """A value as it will be typed on a site: trimmed, within `limit`, and free of control and
    invisible characters (they hide text from the owner who approves it). Line breaks only where
    `lines` allows them."""
    text = str(value).replace("\r\n", "\n")
    if lines:
        text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = unicodedata.normalize("NFC", text.strip())
    if not text:
        raise ValueError(f"{what} is empty (leave it out instead)")
    if len(text) > limit:
        raise ValueError(f"{what} has {len(text)} characters; at most {limit}")
    for ch in text:
        if lines and ch == "\n":
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp"):
            raise ValueError(f"{what} has a control or invisible character (U+{ord(ch):04X}); write plain text")
    return text


def clean_url(value: Any, what: str) -> str:
    url = clean_text(value, what, LIMITS["url"])
    if not re.match(r"^https?://[^\s/$.?#][^\s]*$", url, re.IGNORECASE):
        raise ValueError(f"{what} must be a full link starting with https://, not {url[:40]!r}")
    return url
