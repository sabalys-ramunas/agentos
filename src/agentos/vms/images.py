"""What `agentos vm image build` runs on its build VM: the default script and the cleanup after it."""

from __future__ import annotations

# What `agentos vm image build` installs by default: enough to clone, build and test most projects.
DEFAULT_IMAGE_SCRIPT = """set -eu
export DEBIAN_FRONTEND=noninteractive
cloud-init status --wait >/dev/null 2>&1 || true
apt-get update -q
apt-get install -y -q --no-install-recommends ca-certificates curl git build-essential pkg-config \\
  python3 python3-venv python3-pip docker.io docker-compose-v2 jq unzip zip rsync
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y -q nodejs
systemctl enable docker
"""
# The end of every image build with the default script: no package lists, no caches, no keys, no
# machine id, so each VM booted from the snapshot is new.
IMAGE_CLEANUP = """apt-get clean
rm -rf /var/lib/apt/lists/* /root/.cache
cloud-init clean --logs --seed
rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys
truncate -s 0 /etc/machine-id
sync
"""


def image_script(extra: str = "") -> str:
    """The default image script, with `extra` (more to install) run before the cleanup."""
    return DEFAULT_IMAGE_SCRIPT + (extra.rstrip() + "\n" if extra else "") + IMAGE_CLEANUP
