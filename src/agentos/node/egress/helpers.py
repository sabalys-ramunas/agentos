"""Small readers the guard shares: a request's body and frame, a press's mode, short paths and errors."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from agentos.core.models import ActionKind

# Schemes that load nothing from a host but are harmless to follow (anything else that is not a
# web URL, such as javascript:, data: or blob:, can run code or open what the guard cannot judge).
QUIET_SCHEMES = frozenset({"about"})

SUBMIT_KEYS = re.compile(r"(^|\+)(Enter|NumpadEnter| |Space)$")


def _post_data(request: Any) -> bytes | None:
    """A request's body, or None when it cannot be read (it went away)."""
    try:
        return request.post_data_buffer
    except Exception:  # noqa: BLE001
        return None


def _in_subframe(request: Any) -> bool:
    """Does this navigation (a redirect hop included) load in a subframe? A popup's first request
    has no frame yet: a page, never a frame."""
    try:
        return request.frame.parent_frame is not None
    except Exception:  # noqa: BLE001
        return False


def _press_mode(kind: str, text: str | None) -> str | None:
    """"click" for a click, "key" for what presses Enter or Space (a press of one, a newline typed
    or filled, the Enter that closes a dropdown select), None for actions that follow nothing."""
    if kind == ActionKind.CLICK.value:
        return "click"
    if kind == ActionKind.PRESS.value:
        return "key" if text and SUBMIT_KEYS.search(text) else None
    if kind in (ActionKind.TYPE.value, ActionKind.FILL.value):
        return "key" if text and ("\n" in text or "\r" in text) else None
    if kind == ActionKind.SELECT.value:
        return "key"
    return None


_PRINTABLE_KEY = re.compile(r"^(?:Shift\+)?(?:Key|Digit)?(.)$")


def printable_key(key: str | None) -> str | None:
    """The character a press of `key` enters into a field ("a", "Shift+A", "KeyA", "Digit5", "Space"),
    or None for a key that enters none (Enter, Tab, Backspace, Control+a)."""
    if not key:
        return None
    if key in ("Space", "Shift+Space"):
        return " "
    m = _PRINTABLE_KEY.match(key)
    return m.group(1) if m else None


def _tab_before_newline(text: str) -> bool:
    nl = min((i for i in (text.find("\n"), text.find("\r")) if i >= 0), default=-1)
    return nl > 0 and "\t" in text[:nl]


def _short(path: str, limit: int = 120) -> str:
    return path if len(path) <= limit else path[:limit - 3] + "..."


def _path(url: str) -> str:
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return "?"


