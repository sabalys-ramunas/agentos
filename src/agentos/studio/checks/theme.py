"""theme.leak: given the same screen in light and dark, the colours that did not change where the
page's own custom properties did. Comparing two snapshots needs no browser and is tested alone."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Leak:
    path: str          # the element's DOM path in both snapshots
    prop: str          # "bg", "color" or "border"
    value: str         # "r,g,b,a", the same in both themes
    vars: tuple[str, ...]   # the custom properties whose light value this copies
    dark_var_value: str     # what the first of them became in dark


def _vname(key: str) -> str:
    return key.split(" ", 1)[1] if key.startswith("body ") else key


def compare(light: dict[str, Any], dark: dict[str, Any], kept: list[str] | None = None) -> tuple[list[Leak], str | None]:
    """Leaks, and a reason when nothing could be compared. A colour counts only when it equals the
    light value of a custom property that changed, and no unchanged property has that value (then
    the colour may well come from the unchanged one). A text colour that stays fixed together with
    the surface under it, and a small mark that keeps both its fill and its letters, are fixed on
    purpose (a brand colour, initials on an avatar): their paths go to `kept`."""
    lv, dv = light.get("vars", {}), dark.get("vars", {})
    changed: dict[str, list[str]] = {}
    unchanged: set[str] = set()
    for k, v in lv.items():
        if k not in dv:
            continue
        if dv[k] != v:
            changed.setdefault(v, []).append(k)
        else:
            unchanged.add(v)
    if not changed:
        return [], "no colour custom property changed between the two themes, so there is nothing to compare against"
    leaks: list[Leak] = []
    lels, dels = light.get("els", {}), dark.get("els", {})
    kept = kept if kept is not None else []
    for path, lrec in lels.items():
        drec = dels.get(path)
        if not drec:
            continue
        for prop in ("bg", "color", "border"):
            a, b = lrec.get(prop), drec.get(prop)
            if not a or a != b or a not in changed or a in unchanged:
                continue
            if prop == "color" and lrec.get("inherits") and drec.get("inherits"):
                continue  # inherited: the cause is the ancestor that set it
            if prop == "border" and lrec.get("border_inherits") and drec.get("border_inherits"):
                continue  # currentColor: follows the text colour
            if prop == "color" and lrec.get("on") and lrec.get("on") == drec.get("on"):
                kept.append(path)
                continue  # fixed together with the surface under it (white on a brand colour): on purpose
            if prop == "bg" and lrec.get("mark") and drec.get("mark") and lrec.get("color") == drec.get("color"):
                kept.append(path)
                continue  # a mark (initials on an avatar) keeps its fill and its letters in both themes
            names = tuple(sorted({_vname(k) for k in changed[a]}))
            leaks.append(Leak(path, prop, a, names, dv[changed[a][0]]))
    return leaks, None


def rgba_hex(v: str) -> str:
    parts = [float(x) for x in v.split(",")]
    h = "#" + "".join(f"{round(c):02x}" for c in parts[:3])
    return h if len(parts) < 4 or parts[3] >= 1 else f"{h}{round(parts[3] * 255):02x}"
