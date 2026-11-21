"""The in-page scripts, read once from this package with the shared helpers pasted in."""

from __future__ import annotations

from functools import cache
from importlib import resources


@cache
def script(name: str) -> str:
    here = resources.files(__package__)
    body = (here / f"{name}.js").read_text(encoding="utf-8")
    common = (here / "common.js").read_text(encoding="utf-8")
    return body.replace("/*COMMON*/", common) if "/*COMMON*/" in body else body
