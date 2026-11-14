"""Moved modules keep their old import paths for one release.

Each old path must be the new module itself, not a copy: a name read or patched through the old path
is the one the code uses. Stage Z deletes the shims and this table.
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

# old path -> new path
SHIMS = {
    "agentos.ops.lanecheck": "agentos.stealth.lanecheck",
    "agentos.ops.stealth": "agentos.stealth.stealth",
    "agentos.ops.stealthcheck": "agentos.stealth.stealthcheck",
    "agentos.ops.fingerprint": "agentos.stealth.fingerprint",
    "agentos.ops.tools": "agentos.stealth.tools",
    "agentos.ops.wsexport": "agentos.workspaces.transfer.export",
    "agentos.ops.wsimport": "agentos.workspaces.transfer.import_",
    "agentos.ops.lanefmt": "agentos.node.lanefmt",
    "agentos.ops.age": "agentos.core.age",
    "agentos.ops.s3": "agentos.ops.backup.s3",
    "agentos.node.engine_camoufox": "agentos.node.camoufox.engine",
    **{f"agentos.ops.redteam.{m}": f"agentos.stealth.redteam.{m}"
       for m in ("detectors", "gauntlet", "runner", "scenario", "server")},
}


def _shim_file(old: str) -> Path:
    return SRC / (old.replace(".", "/") + ".py")


@pytest.mark.parametrize(("old", "new"), sorted(SHIMS.items()))
def test_old_path_is_the_new_module(old, new):
    new_mod = importlib.import_module(new)
    assert importlib.import_module(old) is new_mod
    assert sys.modules[old] is new_mod
    parent, _, leaf = old.rpartition(".")
    assert getattr(importlib.import_module(parent), leaf) is new_mod  # `from agentos.ops import x`
    assert new_mod.__name__ == new


@pytest.mark.parametrize(("old", "new"), sorted(SHIMS.items()))
def test_every_name_in_the_shim_is_the_new_modules_object(old, new):
    """The shim's own names (its star import and the private names it lists) are the new module's
    objects, so a type checker reading the old path sees what the code runs."""
    new_mod = importlib.import_module(new)
    spec = importlib.util.spec_from_file_location(f"_shimcheck_{old.replace('.', '_')}", _shim_file(old))
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(probe)
    finally:
        sys.modules.pop(spec.name, None)
    names = [n for n in vars(probe) if not n.startswith("__") and n != "alias"]
    assert names, f"{old} re-exports nothing"
    for name in names:
        assert getattr(probe, name) is getattr(new_mod, name), f"{old}.{name} is not {new}.{name}"
    public = getattr(new_mod, "__all__", [n for n in vars(new_mod) if not n.startswith("_")])
    missing = [n for n in public if n not in vars(probe)]
    assert not missing, f"{old} does not re-export {missing}"


@pytest.mark.parametrize(("old", "new"), sorted(SHIMS.items()))
def test_a_patch_through_the_old_path_reaches_the_code(old, new, monkeypatch):
    monkeypatch.setattr(importlib.import_module(old), "_SHIM_PROBE", object(), raising=False)
    assert importlib.import_module(new)._SHIM_PROBE is importlib.import_module(old)._SHIM_PROBE


def test_each_new_module_is_defined_once():
    """No module file exists at both paths: the old file is only the shim."""
    for old, new in SHIMS.items():
        text = _shim_file(old).read_text()
        assert f'alias(__name__, "{new}")' in text, old
        assert len(text.splitlines()) < 12, f"{old} holds code; it should only be a shim"


def test_old_paths_warn_only_when_asked():
    code = ("import importlib, warnings\n"
            "with warnings.catch_warnings(record=True) as w:\n"
            "    warnings.simplefilter('always')\n"
            f"    importlib.import_module({next(iter(SHIMS))!r})\n"
            "print(sum(issubclass(x.category, DeprecationWarning) and 'moved to' in str(x.message) for x in w))")
    quiet = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                           env={"PATH": "/usr/bin:/bin"})
    loud = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                          env={"PATH": "/usr/bin:/bin", "AGENTOS_WARN_MOVED": "1"})
    assert quiet.stdout.strip() == "0"
    assert loud.stdout.strip() == "1"



def test_the_old_redteam_package_only_holds_shims():
    """agentos.ops.redteam stays a package of its own (aliasing it would load each module a second
    time from the new folder under the old name); every module in it is the moved module."""
    import agentos.ops.redteam as old_pkg
    import agentos.stealth.redteam as new_pkg

    assert Path(old_pkg.__file__).parent == SRC / "agentos" / "ops" / "redteam"
    from agentos.ops.redteam import detectors, server

    assert detectors is importlib.import_module("agentos.stealth.redteam.detectors")
    assert server.SITE == Path(new_pkg.__file__).parent / "site"
    moved = {p.stem for p in Path(new_pkg.__file__).parent.glob("*.py")} - {"__init__"}
    shims = {p.stem for p in Path(old_pkg.__file__).parent.glob("*.py")} - {"__init__"}
    assert moved == shims


# Names tests patch, and the module whose code reads each one (S3 split node/engine_camoufox.py).
# A patch only takes effect in the module that reads the name, so each stays defined there.
PATCHED = {
    "agentos.node.camoufox.engine": ("read_browser_config", "_host_cores", "_wait_for_pid"),
    "agentos.node.camoufox.window": ("_xlib",),
}


def test_a_patched_engine_name_is_defined_where_its_caller_reads_it():
    old = importlib.import_module("agentos.node.engine_camoufox")
    for home, names in PATCHED.items():
        mod = importlib.import_module(home)
        for name in names:
            assert getattr(mod, name).__module__ == home, f"{home}.{name} is defined elsewhere"
            if home == "agentos.node.camoufox.engine":
                assert getattr(old, name) is getattr(mod, name)  # a patch through the old path reaches it
    # The old path does not serve _xlib: a patch through it would miss place_window, so it fails loudly.
    assert not hasattr(old, "_xlib")


def test_no_name_is_defined_in_two_engine_modules():
    """A name defined twice is two objects: a patch of one never reaches code that reads the other."""
    import ast

    seen: dict[str, str] = {}
    for path in sorted((SRC / "agentos" / "node" / "camoufox").glob("*.py")):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            else:
                continue
            for name in names:
                if name == "log":  # one logger, agentos.engine.camoufox, named in each module
                    continue
                assert name not in seen, f"{name} is defined in {seen[name]} and {path.name}"
                seen[name] = path.name
