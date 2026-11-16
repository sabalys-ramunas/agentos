"""What an environment's machine is made of: the image it boots, and the setup that runs on it.

The setup is one script, `setup.sh` next to this file: git, curl, build tools, Node 22 with npm and
pnpm (corepack), Python with uv, and /work. It is the only list of what an environment has:

- on a cloud VM booted from Ubuntu, cloud-init runs it on the first boot (`cloud_config()`), and the
  environment is `starting` until it has written its marker;
- a `dev` snapshot (`agentos vm image build dev`) or the docker provider's image
  (deploy/environments/Dockerfile) already ran it, so the marker is there and it does nothing.
"""

from __future__ import annotations

import base64
import shlex
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


# Run on every machine once its setup is done: a `dev` snapshot made before setup.sh enabled pnpm
# has the ready marker, so the setup never runs there again. It does nothing where pnpm is there
# already, and a machine without network still comes up (it is best effort).
TOOLS_TOP_UP = ("command -v pnpm >/dev/null 2>&1 || { (grep -q '^COREPACK_ENABLE_DOWNLOAD_PROMPT=' "
                "/etc/environment || echo 'COREPACK_ENABLE_DOWNLOAD_PROMPT=0' >> /etc/environment) && "
                "corepack enable && COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack install -g pnpm@latest; } "
                ">/dev/null 2>&1; true")
TOOLS_TOP_UP_S = 60.0

# Package lists on every machine, at once (agents tripped over `apt-get install` failing until they
# ran `apt-get update`: a `dev` snapshot has no lists, IMAGE_CLEANUP removes them). After its setup the
# node runs apt_ready() on the machine: it puts small apt-get and apt wrappers in /usr/local/sbin
# (before /usr/bin in root's PATH), and starts `apt-get update` in the background when the lists are
# missing or a day old, so the boot never waits for it. A wrapper waits for that update, or fetches the
# lists itself when there are none, then runs the real command with its own arguments and exit status.
APT_READY_S = 20.0
APT_PID = "/run/agentos-apt-refresh.pid"


def apt_wrapper(name: str, root: str = "") -> str:
    real = f"{root}/usr/bin/{name}"
    return f"""#!/bin/sh
# agentos: the package lists are fetched in the background when this machine starts. Wait for that,
# or fetch them now, so an install works at once; then the real {name}.
pid=$(cat {root}{APT_PID} 2>/dev/null || true)
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  echo "agentos: the package lists are being fetched; waiting for them (at most 3 minutes)" >&2
  i=0
  while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 180 ]; do sleep 1; i=$((i + 1)); done
fi
case " $* " in
  *" update "*|*" clean "*) ;;
  *) ls {root}/var/lib/apt/lists/*_Packages* >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive {real} update -q >&2 || true ;;
esac
exec {real} "$@"
"""


def apt_ready(root: str = "") -> str:
    """The command the node runs after setup (see APT_READY_S): returns at once, never fails the boot."""
    lines = ["set -u", f"mkdir -p {root}/usr/local/sbin {root}/run"]
    for name in ("apt-get", "apt"):
        b64 = base64.b64encode(apt_wrapper(name, root).encode()).decode()
        dest = f"{root}/usr/local/sbin/{name}"
        lines.append(f"[ -x {root}/usr/bin/{name} ] && echo {b64} | base64 -d > {dest}.new && chmod 755 {dest}.new "
                     f"&& mv {dest}.new {dest}")
    lists = f"{root}/var/lib/apt/lists"
    update = (f"echo $$ > {root}{APT_PID}; DEBIAN_FRONTEND=noninteractive {root}/usr/bin/apt-get update -q "
              f"> {root}/run/agentos-apt-update.log 2>&1; rm -f {root}{APT_PID}")
    lines.append(f"if ! ls {lists}/*_Packages* >/dev/null 2>&1 || "
                 f"[ -n \"$(find {lists} -maxdepth 1 -name '*_Packages*' -mmin +1440 2>/dev/null | head -n 1)\" ]; then "
                 f"p=$(cat {root}{APT_PID} 2>/dev/null || true); "
                 f"if [ -z \"$p\" ] || ! kill -0 \"$p\" 2>/dev/null; then "
                 # its own session: the command's process group ends with the command (agentos.vms.ssh)
                 f"if command -v setsid >/dev/null 2>&1; then setsid -f sh -c {shlex.quote(update)} "
                 f">/dev/null 2>&1 </dev/null; else nohup sh -c {shlex.quote(update)} >/dev/null 2>&1 </dev/null & "
                 "fi; fi; fi")
    lines.append("true")
    return "\n".join(lines) + "\n"


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
