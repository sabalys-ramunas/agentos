"""Screen sets: states x widths x themes, in a fixed order."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import ValidationError

from openajar.core.errors import InvalidRequest
from openajar.studio.contracts import DEFAULT_WIDTHS, Screen, Theme

DEFAULT_THEMES = (Theme.LIGHT, Theme.DARK)
DEFAULT_STATES = ("default",)
MAX_SCREENS = 60


def screens(widths: Iterable[int] | None = None, themes: Iterable[Theme | str] | None = None,
            states: Iterable[str] | None = None, *, height: int = 900, scale: float = 1.0) -> list[Screen]:
    """Every state at every width in every theme, state first, then width, then theme. Repeats
    are dropped. With nothing given: DEFAULT_WIDTHS, light and dark, the default state."""
    ws = list(dict.fromkeys(int(w) for w in (DEFAULT_WIDTHS if widths is None else widths)))
    try:
        ts = list(dict.fromkeys(Theme(t) for t in (DEFAULT_THEMES if themes is None else themes)))
    except ValueError:
        raise InvalidRequest(f"a theme is light or dark, not {list(themes or [])!r}"[:200]) from None
    ss = list(dict.fromkeys(str(s).strip() for s in (DEFAULT_STATES if states is None else states)))
    if not ws or not ts or not ss or any(not s for s in ss):
        raise InvalidRequest("a screen set needs at least one width, one theme and one named state")
    if len(ws) * len(ts) * len(ss) > MAX_SCREENS:
        raise InvalidRequest(f"{len(ws) * len(ts) * len(ss)} screens is more than {MAX_SCREENS}: "
                             "ask for fewer widths, themes or states")
    try:
        return [Screen(width=w, height=height, theme=t, state=s, scale=scale) for s in ss for w in ws for t in ts]
    except ValidationError as e:
        first = e.errors()[0]
        raise InvalidRequest(f"screen {'.'.join(str(p) for p in first['loc'])}: {first['msg']} "
                             "(width 240 to 3840, height 240 to 4000, scale 1 to 3)") from None
