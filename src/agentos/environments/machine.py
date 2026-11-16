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
from datetime import UTC, datetime
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
    if not root:
        lines.append(UPDATES_OFF.rstrip().removesuffix("true").rstrip())  # no updates mid-session (UPDATES_OFF)
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


# Updates on an environment's machine: never in the middle of a session (field report 2026-11-16: at
# 07:35 UTC the VM's apt-daily-upgrade ran unattended-upgrades, which did `systemctl daemon-reload` three
# times, upgraded systemd, re-executed it and restarted networkd, resolved, timesyncd, journald and udevd
# while an agent worked; the reloads made systemd print an old OOM kill again, which the agent read as a
# new one). Security against stability, and the choice:
#
# - Off during a session: the apt-daily and apt-daily-upgrade timers are disabled and APT::Periodic is
#   0 (UPDATES_CONF), so neither unattended-upgrades nor a list refresh starts on its own; needrestart,
#   which a package install of the agent's own runs, only lists what it would restart (NEEDRESTART_CONF).
# - Applied where nobody works yet: when a fresh Ubuntu VM is set up (setup.sh runs unattended-upgrade,
#   the security pocket, before the machine is handed over; on a fresh VM Ubuntu's own run held the dpkg
#   lock at that moment anyway, so the boot takes about as long as before), and when the `dev` snapshot is
#   built (`agentos vm image build dev` runs the setup on a fresh VM, so each new snapshot is patched up to
#   its day, and has this policy baked in). Rebuild the snapshot now and then (docs/ENVIRONMENTS.md).
# - A keep "live" machine lives for weeks: it gets the security updates at a quiet moment (UPGRADE_QUIET:
#   no call and no job for EnvSettings.upgrade_quiet_minutes, at most once a day), never restarting a
#   service for them; when it was never quiet for UPGRADE_LATE_DAYS, or a new kernel waits for a restart,
#   its machine notes say so (Machine.updates), so the owner sees it.
# - Machines that live a session (keep "none") or that come back from a save run what they were built
#   with; a save's machine got its patches when its base or snapshot was made.
# Written on every first boot of a fresh VM (cloud_config's bootcmd, before apt-daily can start) and on
# every bring-up (apt_ready), so a `dev` snapshot made before this policy gets it too. It reloads systemd
# only when a timer is still on.
UPDATES_CONF = "/etc/apt/apt.conf.d/99agentos-updates"
NEEDRESTART_CONF = "/etc/needrestart/conf.d/90-agentos.conf"
UPDATES_OFF = f"""mkdir -p /etc/apt/apt.conf.d /etc/needrestart/conf.d
cat > {UPDATES_CONF} <<'AGENTOS_APT'
// agentos: no updates in the middle of a session (agentos.environments.machine)
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::Unattended-Upgrade "0";
APT::Periodic::AutocleanInterval "0";
AGENTOS_APT
cat > {NEEDRESTART_CONF} <<'AGENTOS_NR'
# agentos: list what would restart, restart nothing (agentos.environments.machine)
$nrconf{{restart}} = 'l';
$nrconf{{kernelhints}} = 0;
AGENTOS_NR
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  for t in apt-daily.timer apt-daily-upgrade.timer; do
    systemctl is-enabled --quiet "$t" 2>/dev/null && systemctl disable --now "$t" >/dev/null 2>&1
  done
fi
true
"""
UPGRADE_STAMP = "/var/lib/agentos-env/upgraded"   # when the last security upgrade ended (UTC)
UPGRADE_PID = "/run/agentos-upgrade.pid"
UPGRADE_LOG = "/var/log/agentos-upgrade.log"
UPGRADE_EVERY_H = 24
UPGRADE_LATE_DAYS = 7
UPGRADE_S = 30.0                                    # the ssh that starts (or asks after) an upgrade
_UPGRADE_RUN = (f"echo $$ > {UPGRADE_PID}; NEEDRESTART_MODE=l DEBIAN_FRONTEND=noninteractive unattended-upgrade "
                f"> {UPGRADE_LOG} 2>&1; mkdir -p {UPGRADE_STAMP.rsplit('/', 1)[0]}; "
                f"date -u +%Y-%m-%dT%H:%M:%SZ > {UPGRADE_STAMP}; rm -f {UPGRADE_PID}")


def upgrade_script(*, start: bool) -> str:
    """How the machine's security updates stand, one line `upgrade: <none|running|done <when>|due <when>|started>`
    (and `reboot: required` when a new kernel waits for a restart); with `start`, an upgrade that is due
    (none for UPGRADE_EVERY_H) starts in a session of its own and the command returns at once."""
    due = (f"mkdir -p {UPGRADE_STAMP.rsplit('/', 1)[0]}; setsid -f sh -c {shlex.quote(_UPGRADE_RUN)} "
           ">/dev/null 2>&1 </dev/null; echo \"upgrade: started\"" if start else
           f"echo \"upgrade: due $(cat {UPGRADE_STAMP} 2>/dev/null)\"")
    return f"""set -u
command -v unattended-upgrade >/dev/null 2>&1 || {{ echo "upgrade: none"; exit 0; }}
p=$(cat {UPGRADE_PID} 2>/dev/null || true)
if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then echo "upgrade: running"; exit 0; fi
[ -f /var/run/reboot-required ] && echo "reboot: required"
if [ -n "$(find {UPGRADE_STAMP} -mmin -{UPGRADE_EVERY_H * 60} 2>/dev/null)" ]; then
  echo "upgrade: done $(cat {UPGRADE_STAMP})"; exit 0
fi
{due}
"""


# Before a disk snapshot (scripts.SCRUB): a security upgrade that runs ends first, so no snapshot holds a
# half-done dpkg. At most UPGRADE_WAIT_S (a save's step has 120 s).
UPGRADE_WAIT_S = 90
UPGRADE_WAIT = (f'p=$(cat {UPGRADE_PID} 2>/dev/null || true); i=0; while [ -n "$p" ] && kill -0 "$p" 2>/dev/null '
                f'&& [ $i -lt {UPGRADE_WAIT_S} ]; do sleep 1; i=$((i+1)); done')


def updates_words(stdout: str, now: datetime) -> str | None:
    """upgrade_script's lines, for the machine's notes: when the security updates were last applied, that
    they run now, that they wait (no quiet moment for UPGRADE_LATE_DAYS), and a restart a kernel waits for."""
    lines = (stdout or "").splitlines()
    state = next((ln[9:].strip() for ln in lines if ln.startswith("upgrade: ")), None)
    reboot = any(ln.strip() == "reboot: required" for ln in lines)
    if state is None or state == "none":
        return None
    if state == "running" or state == "started":
        text = "security updates are being applied now (a quiet moment)"
    else:
        kind, _, when = state.partition(" ")
        at = _when(when)
        if kind == "done":
            text = f"security updates applied {at:%Y-%m-%d %H:%M} UTC" if at else "security updates applied"
        elif at is None or (now - at).days >= UPGRADE_LATE_DAYS:
            since = f" since {at:%Y-%m-%d}" if at else ""
            text = (f"security updates waiting{since}: they run only when the machine is quiet (no call and no "
                    "job for a while); stop its jobs for a moment, or rebuild it")
        else:
            text = f"security updates applied {at:%Y-%m-%d %H:%M} UTC; the next at a quiet moment"
    if reboot:
        text += "; a new kernel waits for a restart of the machine"
    return text


def _when(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
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

    return image_script(UPDATES_OFF + setup_install() + install_script())


def cloud_config() -> str:
    """The cloud-config keys added after the node's own (agentos.vms.keys.cloud_init): the setup
    script as a file, and running it once on the first boot."""
    b64 = base64.b64encode(setup_script().encode()).decode()
    off = base64.b64encode(UPDATES_OFF.encode()).decode()
    return ("bootcmd:\n"  # before apt-daily can start on the first boot (UPDATES_OFF)
            f"  - [sh, -c, 'echo {off} | base64 -d | sh']\n"
            "write_files:\n"
            f"  - path: {SETUP_PATH}\n"
            "    permissions: '0755'\n"
            "    encoding: b64\n"
            f"    content: {b64}\n"
            "runcmd:\n"
            f"  - [sh, {SETUP_PATH}]\n")
