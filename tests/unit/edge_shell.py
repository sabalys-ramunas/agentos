"""Run the shell helpers (deploy/, scripts/) in a sandbox.

Every external command the scripts could use to reach a real host or service is replaced by a
PATH shim that only logs its arguments: ssh, sync rsync (when a test needs it), uv, and chmod
outside the sandbox. HOME is the sandbox, so nothing reads the real ~/.local/bin. Real rsync is
left alone for the installer, which needs it to copy the checkout into the sandbox prefix.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash") or "/bin/bash"
INSTALLER = REPO / "deploy" / "install-node.sh"
SYNC = REPO / "scripts" / "sync-node.sh"
NODE_TEST = REPO / "scripts" / "node-test.sh"

# GNU realpath -m does not exist on macOS, and the installer uses it.
REALPATH = f"""#!{sys.executable}
import os, sys
for arg in [a for a in sys.argv[1:] if a != "-m"] or ["."]:
    print(os.path.realpath(arg))
"""

# Logs the call. `uv sync` builds a .venv/bin/agentos wrapper around the test interpreter's CLI.
# SHIM_CLI=log makes that wrapper log and exit, so no test ever writes node data for real.
UV = f"""#!/bin/bash
echo "uv $*" >> "$SHIM_LOG"
if [ "$1" = sync ]; then
  mkdir -p .venv/bin
  cat > .venv/bin/agentos <<'WRAP'
#!/bin/bash
echo "agentos $*" >> "$SHIM_LOG"
if [ "${{SHIM_CLI:-run}}" = log ]; then exit 0; fi
exec "{sys.executable}" -c 'import sys; from agentos.cli import app; sys.argv[0] = "agentos"; app()' "$@"
WRAP
  chmod +x .venv/bin/agentos
fi
exit 0
"""

# Anything under the sandbox is really changed. Absolute paths outside it are only logged,
# so a test that aims chmod at /etc cannot change /etc.
CHMOD = """#!/bin/bash
echo "chmod $*" >> "$SHIM_LOG"
for arg in "$@"; do
  case "$arg" in
    /*) case "$arg" in "$SANDBOX"/*) ;; *) exit 0 ;; esac ;;
  esac
done
exec /bin/chmod "$@"
"""

LOG_ONLY = """#!/bin/bash
echo "$(basename "$0") $*" >> "$SHIM_LOG"
exit 0
"""

# Stands in for `npx --yes pnpm@x <command>` in a web/ folder: logs the call, and for `build` writes dist the way a
# build does (SHIM_NPX_FAIL=1 makes the build fail, after it has written part of dist).
NPX = """#!/bin/bash
echo "npx $*" >> "$SHIM_LOG"
if [ "$3" = build ]; then
  mkdir -p dist
  echo "built $(cat src/version.txt 2>/dev/null)" > dist/index.html
  if [ "${SHIM_NPX_FAIL:-0}" = 1 ]; then echo broken > dist/index.html; exit 1; fi
fi
exit 0
"""

SHIMS = {"realpath": REALPATH, "uv": UV, "chmod": CHMOD, "rsync": LOG_ONLY, "ssh": LOG_ONLY, "npx": NPX}


def sandbox_env(root: Path, *, shims: tuple[str, ...], cli: str = "run") -> dict[str, str]:
    """A PATH with the named shims in front, and an environment rooted at `root`."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in shims:
        path = bin_dir / name
        path.write_text(SHIMS[name])
        path.chmod(0o755)
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "SANDBOX": str(root),
        "SHIM_LOG": str(root / "shim.log"),
        "SHIM_CLI": cli,
        "AGENTOS_HOME": "",
        "AGENTOS_INIT_ARGS": "",
        "AGENTOS_NODE": "mynode",  # scripts/node-env.sh: the dev node and its dev directory
        "AGENTOS_DEV_ROOT": "/srv/agentos-dev",
    }


def run_script(script: Path, args: list[str], env: dict[str, str], cwd: Path, **extra: str):
    return subprocess.run(
        [BASH, str(script), *args],
        env={**env, **extra},
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def shim_text(env: dict[str, str]) -> str:
    """Everything the shims logged. An ssh command spans lines, so tests search this text."""
    log = Path(env["SHIM_LOG"])
    return log.read_text() if log.exists() else ""


def shim_calls(env: dict[str, str]) -> list[str]:
    return shim_text(env).splitlines()


DEV_FLAGS = ("--dev", "--skip-apt", "--skip-browser")


def installer(env: dict[str, str], cwd: Path, *, prefix: str, home: str, flags: tuple[str, ...] = DEV_FLAGS):
    return run_script(INSTALLER, list(flags), env, cwd, INSTALL_PREFIX=prefix, AGENTOS_HOME=home)


def has_rsync() -> bool:
    return shutil.which("rsync") is not None
