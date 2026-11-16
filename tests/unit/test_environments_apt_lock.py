"""A fresh Ubuntu VM runs unattended-upgrades in its first minutes, holding the dpkg lock, and an
environment's first open failed on "Could not get lock /var/lib/dpkg/lock-frontend" (2026-11-15).
Both scripts that install on the machine make every apt-get wait for the lock first."""

from __future__ import annotations

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "src/agentos/environments"
SETTING = "echo 'DPkg::Lock::Timeout \"600\";' > /etc/apt/apt.conf.d/90agentos-lock-timeout"


@pytest.mark.parametrize("name", ["setup.sh", "browser.sh"])
def test_every_apt_get_waits_for_the_dpkg_lock(name):
    text = (SCRIPTS / name).read_text()
    lines = text.splitlines()
    assert SETTING in lines or ("  " + SETTING) in lines, f"{name} sets no lock timeout"
    setting_at = next(i for i, line in enumerate(lines) if SETTING in line)
    first_apt = next(i for i, line in enumerate(lines) if "apt-get" in line and not line.lstrip().startswith("#"))
    assert setting_at < first_apt, f"{name}: the lock timeout is set after the first apt-get"
    # Updates never run by themselves during a session (agentos.environments.machine, 2026-11-16: a
    # mid-session upgrade reloaded systemd and restarted the network while a browser started), but the
    # package stays: setup and the dev image build apply them before anyone works, and a quiet keep="live"
    # machine gets them later. Never removed or purged.
    for line in lines:
        if not line.lstrip().startswith("#"):
            assert not ("unattended-upgrades" in line and ("remove" in line or "purge" in line)), line
