"""What an environment's machine is made of: the image it boots, and the setup that runs on it.

The setup is one script, `setup.sh` next to this file: git, curl, build tools, Node 22, Python with
uv, and /work. It is the only list of what an environment has:

- on a cloud VM booted from Ubuntu, cloud-init runs it on the first boot (`cloud_config()`), and the
  environment is `starting` until it has written its marker;
- a `dev` snapshot (`agentos vm image build dev`) or the docker provider's image
  (deploy/environments/Dockerfile) already ran it, so the marker is there and it does nothing.
"""

from __future__ import annotations

import base64
from importlib import resources

READY_MARKER = "/var/lib/agentos-env/ready"
SETUP_PATH = "/usr/local/lib/agentos-env/setup.sh"
SNAPSHOT_IMAGE = "snapshot:dev"
BASE_IMAGE = "ubuntu-24.04"
WORK = "/work"

# One command over SSH: "ready", or what cloud-init says (its status, and the end of its log on an error).
SETUP_STATE = (f"if [ -f {READY_MARKER} ]; then echo ready; "
               "elif command -v cloud-init >/dev/null 2>&1; then s=$(cloud-init status 2>/dev/null || true); "
               "echo \"$s\"; case \"$s\" in *error*) tail -n 20 /var/log/cloud-init-output.log 2>/dev/null;; esac; "
               "else echo 'no cloud-init and no setup marker'; fi")


def boot_done_without_setup(state: str) -> bool:
    """SETUP_STATE's first line says the boot is over and the setup never ran: the node runs it."""
    return state.startswith(("status: done", "status: disabled")) or state == "no cloud-init and no setup marker"


def setup_script() -> str:
    return resources.files("agentos.environments").joinpath("setup.sh").read_text()


def cloud_config() -> str:
    """The cloud-config keys added after the node's own (agentos.vms.keys.cloud_init): the setup
    script as a file, and running it once on the first boot."""
    b64 = base64.b64encode(setup_script().encode()).decode()
    return ("write_files:\n"
            f"  - path: {SETUP_PATH}\n"
            "    permissions: '0755'\n"
            "    encoding: b64\n"
            f"    content: {b64}\n"
            "runcmd:\n"
            f"  - [sh, {SETUP_PATH}]\n")
