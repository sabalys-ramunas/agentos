"""Old import paths of moved modules, kept for one release.

A shim at the old path star-imports the new module (so a reader and the type checker see its names)
and then calls `alias(__name__, "new.path")`: the old path becomes the new module itself in
sys.modules. `from old.path import x`, `old.path.x` and `monkeypatch.setattr(old.path, "x", ...)`
all reach the one module the code reads from, so a patch through the old path is never lost on a
copy. With AGENTOS_WARN_MOVED=1 every use of an old path warns (DeprecationWarning). Stage Z deletes
the shims.
"""

from __future__ import annotations

import importlib
import os
import sys
import warnings


def alias(old: str, new: str) -> None:
    """Make module `old` the module `new` (call it from the shim at `old`, as its last line)."""
    if os.environ.get("AGENTOS_WARN_MOVED") == "1":
        warnings.warn(f"{old} moved to {new}; import it from there", DeprecationWarning, stacklevel=3)
    sys.modules[old] = importlib.import_module(new)
