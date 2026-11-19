"""apt works at once on a fresh machine (agents tripped over `apt-get install` failing until they ran
`apt-get update`): the lists are fetched in the background after boot, and apt-get and apt wait for
that, or fetch them when there are none. Here the machine is a folder, apt-get a stand-in that logs."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.environments import machine

WAIT_S = 30.0

# An update logs, then waits until the test releases it (the gate file exists), then writes the lists.
FAKE_APT = """#!/bin/sh
echo "$(basename "$0") $*" >> {log}
if [ "$1" = update ] || [ "$2" = update ]; then
  while [ ! -e {gate} ]; do sleep 0.02; done
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
    gate = tmp_path / "release-the-update"

    def fake(*, held: bool = False) -> None:
        """apt-get and apt stand-ins; with held, an update waits until release() is called."""
        if not held:
            gate.touch()
        for name in ("apt-get", "apt"):
            f = root / "usr/bin" / name
            f.write_text(FAKE_APT.format(log=log, gate=gate, root=root))
            f.chmod(0o755)

    def sh(cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=False, env=path_env(root),
                              timeout=WAIT_S)

    yield SimpleNamespace(root=root, log=log, fake=fake, sh=sh, release=gate.touch)
    gate.touch()  # a held update still looping ends with the test


def path_env(root: Path) -> dict[str, str]:
    return {"PATH": f"{root}/usr/local/sbin:{root}/usr/bin:/usr/bin:/bin"}


def wait_for(cond, what: str) -> None:
    """Until cond() holds: a slow machine only takes longer, it never fails this."""
    deadline = time.monotonic() + WAIT_S
    while not cond():
        assert time.monotonic() < deadline, f"no {what} in {WAIT_S} s"
        time.sleep(0.02)


def calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def test_the_boot_step_returns_at_once_and_the_update_runs_behind_it(box):
    box.fake(held=True)
    # the update is held until the test releases it, so a boot step that waited for it would never
    # return (sh times out): returning at all is the proof, however slow the machine
    assert box.sh(machine.apt_ready(str(box.root))).returncode == 0
    pid = box.root / machine.APT_PID.lstrip("/")
    wait_for(lambda: "apt-get update -q" in calls(box.log), "background update")
    # an install now finds the update running and waits for it, with its own arguments kept
    err = box.root.parent / "install.err"
    with err.open("w") as f:
        install = subprocess.Popen(["sh", "-c", "apt-get install -y jq"], stdout=subprocess.DEVNULL, stderr=f,
                                   env=path_env(box.root))
    try:
        wait_for(lambda: "waiting" in err.read_text(), "wait message")
        assert install.poll() is None and "apt-get install -y jq" not in calls(box.log)
        box.release()
        assert install.wait(timeout=WAIT_S) == 0
    finally:
        install.kill()
    assert calls(box.log)[-1] == "apt-get install -y jq" and calls(box.log).count("apt-get update -q") == 1
    assert not pid.exists()


def test_with_no_lists_and_no_update_running_the_wrapper_fetches_them_first(box):
    box.fake()
    box.sh(machine.apt_ready(str(box.root)))
    pid = box.root / machine.APT_PID.lstrip("/")
    wait_for(lambda: "apt-get update -q" in calls(box.log) and not pid.exists(), "finished background update")
    for f in (box.root / "var/lib/apt/lists").iterdir():
        f.unlink()  # say the lists went
    box.log.unlink()
    assert box.sh("apt install -y ripgrep").returncode == 0
    assert calls(box.log) == ["apt update -q", "apt install -y ripgrep"]


def test_fresh_lists_start_no_update(box):
    box.fake()
    (box.root / "var/lib/apt/lists/x_Packages").touch()
    box.sh(machine.apt_ready(str(box.root)))
    time.sleep(0.3)
    assert "apt-get update -q" not in calls(box.log)
    assert os.access(box.root / "usr/local/sbin/apt-get", os.X_OK)


def test_setup_keeps_the_lists_it_fetched():
    text = machine.setup_script()
    assert "rm -rf /var/lib/apt/lists" not in text and "apt-get clean" in text
