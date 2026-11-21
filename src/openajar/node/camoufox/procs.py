"""Finding a lane's Camoufox processes: the browser binary's name and the profile it runs."""

from __future__ import annotations

import os
from pathlib import Path

from openajar.node.display import iter_processes

BROWSER_BIN = "camoufox-bin"


# ---- process lookup ---------------------------------------------------------------

def _is_browser(argv: list[str]) -> bool:
    return bool(argv) and os.path.basename(argv[0]) == BROWSER_BIN


def _profile_forms(profile_dir: Path) -> set[str]:
    return {str(profile_dir), str(profile_dir.resolve())}


def browser_pids(profile_dir: Path, *, main_only: bool = True) -> list[int]:
    """camoufox-bin processes running this profile (the main process unless main_only=False)."""
    forms = _profile_forms(profile_dir)
    out = []
    for pid, argv in iter_processes():
        if _is_browser(argv) and forms.intersection(argv) and not (main_only and "-contentproc" in argv):
            out.append(pid)
    return out


def browser_pids_under(root: Path) -> list[int]:
    """camoufox-bin processes whose profile lives anywhere under root."""
    prefixes = tuple(f.rstrip("/") + "/" for f in _profile_forms(root))
    return [pid for pid, argv in iter_processes()
            if _is_browser(argv) and any(a.startswith(prefixes) for a in argv[1:])]
