"""apt works at once on a fresh machine (agents tripped over `apt-get install` failing until they ran
`apt-get update`): the lists are fetched in the background after boot, and apt-get and apt wait for
that, or fetch them when there are none. Here the machine is a folder, apt-get a stand-in that logs."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from agentos.environments import machine

FAKE_APT = """#!/bin/sh
echo "$(basename "$0") $*" >> {log}
if [ "$1" = update ] || [ "$2" = update ]; then
  sleep {delay}
  mkdir -p {root}/var/lib/apt/lists
  touch {root}/var/lib/apt/lists/archive.ubuntu.com_ubuntu_dists_noble_main_binary-amd64_Packages
fi
exit 0
"""


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "m"
    for d in ("usr/bin", "var/lib/apt/lists", "run"):
        (root / d).mkdir(parents=True)
    log = tmp_path / "apt.log"

    def fake(delay: float) -> None:
        for name in ("apt-get", "apt"):
            f = root / "usr/bin" / name
            f.write_text(FAKE_APT.format(log=log, delay=delay, root=root))
            f.chmod(0o755)

    def sh(cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=False,
                              env={"PATH": f"{root}/usr/local/sbin:{root}/usr/bin:/usr/bin:/bin"})

    return root, log, fake, sh


def calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_the_boot_step_returns_at_once_and_the_update_runs_behind_it(box):
    root, log, fake, sh = box
    fake(delay=1.0)
    t0 = time.monotonic()
    assert sh(machine.apt_ready(str(root))).returncode == 0
    assert time.monotonic() - t0 < 0.8  # the boot does not wait for the lists
    # an install right away waits for the update, then runs with its own arguments
    out = sh("apt-get install -y jq")
    assert out.returncode == 0 and "waiting" in out.stderr
    assert calls(log)[-1] == "apt-get install -y jq" and calls(log).count("apt-get update -q") == 1
    assert not (root / machine.APT_PID.lstrip("/")).exists()


def test_with_no_lists_and_no_update_running_the_wrapper_fetches_them_first(box):
    root, log, fake, sh = box
    fake(delay=0.0)
    sh(machine.apt_ready(str(root)))
    time.sleep(0.3)
    for f in (root / "var/lib/apt/lists").iterdir():
        f.unlink()  # say the lists went
    log.unlink()
    assert sh("apt install -y ripgrep").returncode == 0
    assert calls(log) == ["apt update -q", "apt install -y ripgrep"]


def test_fresh_lists_start_no_update(box):
    root, log, fake, sh = box
    fake(delay=0.0)
    (root / "var/lib/apt/lists/x_Packages").touch()
    sh(machine.apt_ready(str(root)))
    time.sleep(0.3)
    assert "apt-get update -q" not in calls(log)
    assert os.access(root / "usr/local/sbin/apt-get", os.X_OK)


def test_setup_keeps_the_lists_it_fetched():
    text = machine.setup_script()
    assert "rm -rf /var/lib/apt/lists" not in text and "apt-get clean" in text
