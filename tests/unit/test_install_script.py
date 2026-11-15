"""scripts/install.sh, the one line for a person's computer, against fake uv, curl and sudo."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"

FAKE_UV = """#!/bin/sh
echo "uv $*" >> "$FAKE_LOG"
if [ "$1 $2" = "tool install" ]; then
  mkdir -p "$HOME/.local/bin"
  printf '#!/bin/sh\\necho "agentos $*" >> "$FAKE_LOG"\\n[ "$1" = --version ] && echo 0.0.0-test\\nexit 0\\n' \\
    > "$HOME/.local/bin/agentos"
  chmod +x "$HOME/.local/bin/agentos"
fi
"""


def fake_bin(tmp_path: Path, *, with_uv: bool) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    tools = {
        "curl": '#!/bin/sh\necho "curl $*" >> "$FAKE_LOG"\n'
                f"printf '%s' '{FAKE_UV_INSTALLER}'\n",
        "sudo": '#!/bin/sh\necho "sudo $*" >> "$FAKE_LOG"\nexit 1\n',
        "git": '#!/bin/sh\nexit 0\n',
    }
    if with_uv:
        tools["uv"] = FAKE_UV
    for name, body in tools.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    return bin_dir, log


# what the fake curl serves as uv's installer: it puts the fake uv in ~/.local/bin
FAKE_UV_INSTALLER = ('mkdir -p "$HOME/.local/bin"; cat > "$HOME/.local/bin/uv" <<"EOF"\n' + FAKE_UV + 'EOF\n'
                     'chmod +x "$HOME/.local/bin/uv"\n').replace("'", "'\\''")


def run(tmp_path: Path, bin_dir: Path, log: Path, **env: str) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return subprocess.run(["/bin/sh", str(SCRIPT)], capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL,
                          env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_LOG": str(log), **env})


def test_installs_uv_then_agentos_from_git_and_prints_the_next_step(tmp_path):
    bin_dir, log = fake_bin(tmp_path, with_uv=False)
    proc = run(tmp_path, bin_dir, log, AGENTOS_GIT_URL="https://git.example.test/agentos.git")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    calls = log.read_text()
    assert "curl -LsSf https://astral.sh/uv/install.sh" in calls
    assert "uv tool install --force --python 3.12 agentos[local] @ git+https://git.example.test/agentos.git@main" in calls
    assert "sudo" not in calls  # never on the person's computer
    assert "Next step: agentos setup" in proc.stdout
    assert "agentos setup" not in calls  # no terminal attached: it prints the step instead of running it

    again = run(tmp_path, bin_dir, log, AGENTOS_GIT_URL="https://git.example.test/agentos.git", AGENTOS_GIT_REF="v1")
    assert again.returncode == 0, again.stderr
    assert log.read_text().count("curl ") == 1  # uv is there now
    assert "agentos[local] @ git+https://git.example.test/agentos.git@v1" in log.read_text()
    assert "—" not in SCRIPT.read_text()


def test_installs_from_the_public_repository_by_default(tmp_path):
    bin_dir, log = fake_bin(tmp_path, with_uv=True)
    proc = run(tmp_path, bin_dir, log)
    assert proc.returncode == 0, proc.stderr
    assert "agentos[local] @ git+https://github.com/sabalys-ramunas/agentos.git@main" in log.read_text()
