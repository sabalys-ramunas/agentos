"""Point the CLI's node config at a test node's, without leaking it into later tests.

Patching `openajar.core.config.load_config` alone is not enough: a module the CLI imports for the
first time while the patch is on (`from openajar.core.config import load_config`, such as
openajar.commands.base, imported lazily by the environment CLIs) binds the fake, and monkeypatch only
undoes the attribute it set. Every later test in the process then read this test's node: seen as
"this node has no vault yet" in test_environments_friction.py after test_environments_checkpoints.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from openajar.core import config as config_mod


def patch_node_config(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, cfg: Any,
                      toml: Path) -> None:
    real = {"config_path": config_mod.config_path, "load_config": config_mod.load_config}
    fakes = {"config_path": lambda h=None: toml, "load_config": lambda h=None: cfg}
    for name, fake in fakes.items():
        monkeypatch.setattr(config_mod, name, fake)

    def restore() -> None:
        for mod in list(sys.modules.values()):
            names = getattr(mod, "__dict__", {})
            for name, fake in fakes.items():
                if names.get(name) is fake:
                    setattr(mod, name, real[name])

    request.addfinalizer(restore)
