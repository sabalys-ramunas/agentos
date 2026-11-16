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

# One command over SSH: "ready" and whether pnpm is there (a second line, `tools: pnpm` or `tools: no
# pnpm`, so the node tops up pnpm only where it is missing), or what cloud-init says (its status, and the
# end of its log on an error). While cloud-init still runs it waits for it here, up to SETUP_WAIT_S, so the
# answer comes the moment the boot is over instead of at the next poll.
SETUP_WAIT_S = 20
SETUP_STATE = (f"if [ -f {READY_MARKER} ]; then echo ready; "
               "if command -v pnpm >/dev/null 2>&1; then echo 'tools: pnpm'; else echo 'tools: no pnpm'; fi; "
               "elif command -v cloud-init >/dev/null 2>&1; then "
               f"if command -v timeout >/dev/null 2>&1; then timeout {SETUP_WAIT_S} cloud-init status --wait "
               ">/dev/null 2>&1; fi; s=$(cloud-init status 2>/dev/null || true); "
               "echo \"$s\"; case \"$s\" in *error*) tail -n 20 /var/log/cloud-init-output.log 2>/dev/null;; esac; "
               "else echo 'no cloud-init and no setup marker'; fi")


def needs_pnpm(state: str) -> bool:
    """SETUP_STATE said "ready": is pnpm missing (an older snapshot), or did it not say?"""
    return "tools: pnpm" not in state.splitlines()[1:2]


# Run on a machine whose setup is done but has no pnpm (needs_pnpm): a `dev` snapshot made before
# setup.sh enabled pnpm has the ready marker, so the setup never runs there again. It does nothing where
# pnpm is there already, and a machine without network still comes up (it is best effort).
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


# Swap on every machine (field report 2026-11-16: the kernel OOM-killed a 4.1 GB Nest compile on an
# 8 GB box with no swap). The node runs SWAP_ON on each bring-up and after a snapshot it keeps working
# on; nothing goes in /etc/fstab or the image, so a machine from a save makes its own again. Its size
# is the machine's memory, at most SWAP_MAX_MB (as Fedora's zram default).
#
# zram first: swap compressed in memory (JS heaps pack about 3 to 1), with no disk at all, so a save's
# whole-disk snapshot never holds process memory (a save promises it holds none) and does not grow by
# gigabytes; it needs only the zram module of Ubuntu's kernel, no package. A kernel without zram gets a
# swapfile, at most a quarter of the free disk and none under 1 GB free for it, with swappiness 10 so it
# is used under pressure only; SWAP_OFF takes it away (swapoff, delete, fstrim) before every snapshot
# (scripts.SCRUB). A container (the docker provider) gets none: its kernel is this computer's.
# Prints one line, `swap: <what>`, for the machine's notes (swap_words).
SWAP_FILE = "/agentos.swap"
SWAP_MAX_MB = 8192
SWAP_ON_S = 30.0
SWAP_ON = f"""set -u
if [ -n "$(tail -n +2 /proc/swaps 2>/dev/null)" ]; then
  echo "swap: $(awk 'NR > 1 {{ s += $3 }} END {{ printf "%d", s / 1024 }}' /proc/swaps) MB"; exit 0
fi
if [ -f /.dockerenv ] || grep -qa 'container=' /proc/1/environ 2>/dev/null; then
  echo "swap: none (a container)"; exit 0
fi
mb=$(awk '/^MemTotal:/ {{ printf "%d", $2 / 1024 }}' /proc/meminfo)
[ "$mb" -gt {SWAP_MAX_MB} ] && mb={SWAP_MAX_MB}
if [ "$mb" -ge 512 ] && modprobe zram 2>/dev/null; then
  dev=$(zramctl --find --size "${{mb}}M" --algorithm zstd 2>/dev/null || zramctl --find --size "${{mb}}M" 2>/dev/null || true)
  if [ -n "$dev" ] && mkswap "$dev" >/dev/null 2>&1 && swapon --priority 100 "$dev" 2>/dev/null; then
    echo "swap: $mb MB zram"; exit 0
  fi
  [ -n "$dev" ] && zramctl --reset "$dev" 2>/dev/null
fi
free=$(df -Pm / | awk 'NR == 2 {{ print $4 }}')
[ "$mb" -gt $((free / 4)) ] && mb=$((free / 4))
if [ "$mb" -lt 1024 ]; then
  echo "swap: none (too little free disk)"; exit 0
fi
rm -f {SWAP_FILE}
if fallocate -l "${{mb}}M" {SWAP_FILE} 2>/dev/null && chmod 600 {SWAP_FILE} && mkswap {SWAP_FILE} >/dev/null 2>&1 \\
    && swapon --discard {SWAP_FILE} 2>/dev/null; then
  sysctl -q vm.swappiness=10 2>/dev/null || true
  echo "swap: $mb MB file"; exit 0
fi
rm -f {SWAP_FILE}
echo "swap: none (the kernel refused it)"
"""
# Before a snapshot: the swapfile (if this machine has one) goes, and its blocks are trimmed so the
# image holds neither them nor what was swapped out. zram needs nothing: it is memory.
SWAP_OFF = (f"if grep -q '^{SWAP_FILE} ' /proc/swaps 2>/dev/null; then swapoff {SWAP_FILE} 2>/dev/null && "
            f"rm -f {SWAP_FILE}; else rm -f {SWAP_FILE}; fi; "
            "command -v fstrim >/dev/null 2>&1 && fstrim / >/dev/null 2>&1; true")


def swap_words(stdout: str) -> str | None:
    """SWAP_ON's line, for the machine's notes: "8 GB, compressed in memory (zram)", "4 GB swapfile",
    "none (a container)"."""
    for line in (stdout or "").splitlines():
        if not line.startswith("swap: "):
            continue
        what = line[6:].strip()
        parts = what.split()
        if len(parts) >= 2 and parts[1] == "MB" and parts[0].isdigit():
            size = int(parts[0])
            size_words = (f"{size / 1024:.1f}".removesuffix(".0") + " GB") if size >= 1024 else f"{size} MB"
            kind = parts[2] if len(parts) > 2 else ""
            return {"zram": f"{size_words}, compressed in memory (zram)", "file": f"{size_words} swapfile"}.get(
                kind, size_words)
        return what[:80]
    return None


def boot_done_without_setup(state: str) -> bool:
    """SETUP_STATE's first line says the boot is over and the setup never ran: the node runs it."""
    return state.startswith(("status: done", "status: disabled")) or state == "no cloud-init and no setup marker"


def setup_script() -> str:
    return resources.files("agentos.environments").joinpath("setup.sh").read_text()


def setup_install() -> str:
    """setup.sh as a step of an image build (`agentos vm image build dev`): written where cloud-init
    puts it, then run, so a VM booted from the snapshot has the ready marker and pnpm, and its setup
    check is one call (without it the node ran the whole setup on every boot: apt, then corepack)."""
    return (f"mkdir -p {SETUP_PATH.rsplit('/', 1)[0]}\n"
            f"cat > {SETUP_PATH} <<'AGENTOS_SETUP_SCRIPT'\n{setup_script().rstrip()}\nAGENTOS_SETUP_SCRIPT\n"
            f"sh {SETUP_PATH}\n")


def dev_image_script() -> str:
    """What `agentos vm image build dev` (and `agentos setup`'s offer of it) runs on the build VM: the
    default image script with the environment's setup and its browser, so an environment booted from
    the snapshot has both and its setup check is one call."""
    from agentos.environments.vmbrowser import install_script
    from agentos.vms.manager import image_script

    return image_script(setup_install() + install_script())


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
