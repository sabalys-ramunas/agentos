"""Install an openajar node over SSH, for `openajar connect <user@host>` on a host that has none.

Two scripts go to the host on stdin (`ssh <host> sh -s`), like the node read in remote.py:

1. `probe_script`: read only. Which OS and CPU, whether the login user is root or can
   `sudo -n`, and whether openajar is there already (a node.toml, the agentosd unit or
   /opt/agentos). connect installs only when none of those are there.
2. `install_script`: as root (or through `sudo -n`, never a password prompt): the openajar code
   in /opt/agentos-src, then the node installer with the hardened unit
   (`deploy/install-node.sh --systemd --user agentos --hardened`). The installer writes
   node.toml and starts agentosd under systemd, which is what `openajar up` does by hand on a
   box you sit at; connect then goes on exactly as for any node.

Where the code comes from (`install_source`):
- OPENAJAR_GIT_URL set: the node clones it (shallow, OPENAJAR_GIT_REF or main).
- otherwise, when this openajar runs from a checkout: that checkout is sent to the node with rsync
  over the same SSH login (as scripts/sync-node.sh does, its web/dist build
  included), so a private repository works and the node gets exactly the code on this computer.
- otherwise the public repository (GIT_URL), at OPENAJAR_GIT_REF or main.

`--dry-run` prints every script and command, and runs only the probe.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from openajar.client.errors import ClientError, SshError
from openajar.client.remote import explain_ssh_failure, ssh_read_command, valid_host

# The repository a fresh node installs from: the public one, unless OPENAJAR_GIT_URL names another
# (any URL `git clone` takes, with access from the node).
GIT_URL = "https://github.com/sabalys-ramunas/agentos.git"
GIT_REF = "main"
SOURCE_DIR = "/opt/agentos-src"
INSTALL_ARGS = ("--systemd", "--user", "agentos", "--hardened")
SERVICE_USER = "openajar"
OPENAJAR_BIN = "/opt/agentos/.venv/bin/openajar"
PROBE_TIMEOUT = 45
INSTALL_TIMEOUT = 45 * 60
SUPPORTED = {"ubuntu": (22, 4), "debian": (12, 0)}  # the installer uses apt and systemd
ARCHES = ("x86_64", "amd64", "aarch64", "arm64")
URL_RE = re.compile(r"^[A-Za-z0-9+_.:@/~%-]{4,512}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
# What a checkout sent to a node leaves out (scripts/sync-node.sh's list, and this computer's node settings)
SEND_EXCLUDES = (
    "/.venv/", ".git", "__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache", ".DS_Store",
    "/poc/shots/", "/poc/profiles/", "/poc/fingerprints/", "/poc/secrets.json", "/.agentos-home/",
    "/.agentos-node.env", "/web/node_modules/", "/web/test-results/", "/web/screenshots/", "/web/.dev-node-*",
    "/redteam/site/",
    "/redteam/detect/.venv/", "/panel/",  # /panel/: what the removed classic panel left in an old checkout
)

PROBE = """\
home={home}
echo "openajar-kernel=$(uname -s 2>/dev/null)"
echo "openajar-arch=$(uname -m 2>/dev/null)"
if [ -r /etc/os-release ]; then
  echo "openajar-os=$(. /etc/os-release; echo "${{ID:-unknown}} ${{VERSION_ID:-0}}")"
fi
if [ "$(id -u)" = 0 ]; then echo "openajar-sudo=root"
elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then echo "openajar-sudo=yes"
else echo "openajar-sudo=no $(id -un)"; fi
S=; [ "$(id -u)" = 0 ] || S="sudo -n"
if [ -e "$home/node.toml" ] || $S test -e "$home/node.toml" 2>/dev/null; then echo "agentos-node=yes"; fi
for p in /etc/systemd/system/agentosd.service /opt/agentos; do
  [ -e "$p" ] && echo "openajar-installed=$p"
done
command -v openajar >/dev/null 2>&1 && echo "openajar-installed=$(command -v openajar)"
command -v systemctl >/dev/null 2>&1 && systemctl cat agentosd.service >/dev/null 2>&1 \
  && echo "openajar-installed=agentosd.service"
[ -d /run/systemd/system ] && echo "openajar-systemd=yes"
exit 0
"""

INSTALL = """\
# openajar-install-node: written by `openajar connect --install`
set -eu
S=; [ "$(id -u)" = 0 ] || S="sudo -n"
url={url}
ref={ref}
src={src}
if ! command -v git >/dev/null 2>&1; then
  echo "==> Installing git"
  $S env DEBIAN_FRONTEND=noninteractive apt-get update -qq
  $S env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git
fi
if [ -d "$src/.git" ]; then
  echo "==> Updating $src"
  $S git -C "$src" fetch --depth 1 origin "$ref"
  $S git -C "$src" reset --hard FETCH_HEAD
else
  echo "==> Cloning $url ($ref) into $src"
  $S git clone --depth 1 --branch "$ref" "$url" "$src"
fi
$S "$src/deploy/install-node.sh" {args}
"""

# Before a checkout is sent: rsync on the node, and an empty source directory.
PREPARE = """\
# openajar-prepare-source: written by `openajar connect --install`
set -eu
S=; [ "$(id -u)" = 0 ] || S="sudo -n"
if ! command -v rsync >/dev/null 2>&1; then
  echo "==> Installing rsync"
  $S env DEBIAN_FRONTEND=noninteractive apt-get update -qq
  $S env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync
fi
$S mkdir -p {src}
"""

INSTALL_CHECKOUT = """\
# openajar-install-node: written by `openajar connect --install` (code sent from {origin})
set -eu
S=; [ "$(id -u)" = 0 ] || S="sudo -n"
src={src}
[ -x "$src/deploy/install-node.sh" ] || {{ echo "error: $src has no deploy/install-node.sh: the copy did not arrive" >&2; exit 1; }}
$S "$src/deploy/install-node.sh" {args}
"""

LOGIN = """\
# openajar-panel-login
S=; [ "$(id -u)" = 0 ] || S="sudo -n"
$S -u {user} env OPENAJAR_HOME={home} {bin} panel login --base-url {base}
"""


def git_url() -> str:
    """The repository to install from: OPENAJAR_GIT_URL, else the public one."""
    url = os.environ.get("OPENAJAR_GIT_URL") or GIT_URL
    if not URL_RE.match(url) or url.startswith("-"):
        raise ClientError(f"OPENAJAR_GIT_URL {url!r} does not look like a git URL")
    return url


def local_checkout() -> Path | None:
    """The openajar checkout this code runs from (its pyproject.toml names openajar and it has the node
    installer), or None for an installed package."""
    root = Path(__file__).resolve().parents[3]
    try:
        project = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    if not re.search(r'^name = "openajar"', project, re.MULTILINE) or not (root / "deploy" / "install-node.sh").is_file():
        return None
    return root


@dataclass(frozen=True)
class Source:
    """Where a fresh node's code comes from: a git URL the node clones, or this computer's checkout."""
    url: str = ""
    ref: str = ""
    checkout: Path | None = None

    @property
    def sends(self) -> bool:
        return self.checkout is not None


def install_source() -> Source:
    """OPENAJAR_GIT_URL when it is set, else the checkout this runs from, else the public repository."""
    if os.environ.get("OPENAJAR_GIT_URL"):
        return Source(url=git_url(), ref=git_ref())
    checkout = local_checkout()
    if checkout is not None:
        return Source(checkout=checkout)
    return Source(url=git_url(), ref=git_ref())


def git_ref() -> str:
    ref = os.environ.get("OPENAJAR_GIT_REF") or GIT_REF
    if not REF_RE.match(ref) or ref.startswith("-"):
        raise ClientError(f"OPENAJAR_GIT_REF {ref!r} is not a branch or tag name")
    return ref


def probe_script(remote_home: str) -> str:
    return PROBE.format(home=shlex.quote(remote_home.rstrip("/") or "/"))


def install_script(url: str, ref: str) -> str:
    return INSTALL.format(url=shlex.quote(url), ref=shlex.quote(ref), src=shlex.quote(SOURCE_DIR),
                          args=" ".join(INSTALL_ARGS))


def source_install_script(source: Source) -> str:
    """The script that installs from `source` (the checkout must have been sent first)."""
    if source.checkout is None:
        return install_script(source.url, source.ref)
    origin = re.sub(r"[^A-Za-z0-9._/~ -]", "?", str(source.checkout))[:200]
    return INSTALL_CHECKOUT.format(origin=origin, src=shlex.quote(SOURCE_DIR), args=" ".join(INSTALL_ARGS))


def prepare_script() -> str:
    return PREPARE.format(src=shlex.quote(SOURCE_DIR))


def send_command(rsync: str, ssh: str, host: str, checkout: Path, *, as_root: bool) -> list[str]:
    """rsync of the checkout into SOURCE_DIR on the host, over the same SSH login (sudo -n when not root).
    The copy belongs to root and nobody else can change it (the node installer's rule for its code)."""
    excludes = [f"--exclude={e}" for e in SEND_EXCLUDES]
    return [rsync, "-az", "--delete", "--no-owner", "--no-group", "--chmod=go-w",
            "-e", f"{shlex.quote(ssh)} -o BatchMode=yes -o ConnectTimeout=15",
            f"--rsync-path={'rsync' if as_root else 'sudo -n rsync'}", *excludes,
            f"{checkout}/", f"{valid_host(host)}:{SOURCE_DIR}/"]


def login_script(remote_home: str, local_port: int) -> str:
    return LOGIN.format(user=SERVICE_USER, home=shlex.quote(remote_home), bin=OPENAJAR_BIN,
                        base=shlex.quote(f"http://localhost:{local_port}"))


@dataclass
class HostFacts:
    kernel: str = ""
    arch: str = ""
    os_id: str = ""
    os_version: str = ""
    sudo: str = "no"        # root, yes (sudo -n works) or no
    user: str = ""
    node: bool = False      # a node.toml at the remote home
    installed: list[str] = field(default_factory=list)  # other signs of openajar (the unit, /opt/agentos)
    systemd: bool = False

    @property
    def has_openajar(self) -> bool:
        return self.node or bool(self.installed)

    @property
    def os_label(self) -> str:
        return f"{self.os_id} {self.os_version}".strip() or self.kernel or "an unknown system"


def parse_probe(stdout: str) -> HostFacts:
    facts = HostFacts()
    for line in stdout.splitlines():
        key, _, value = line.partition("=")
        value = value.strip()
        if key == "openajar-kernel":
            facts.kernel = value
        elif key == "openajar-arch":
            facts.arch = value
        elif key == "openajar-os":
            facts.os_id, _, facts.os_version = value.partition(" ")
        elif key == "openajar-sudo":
            facts.sudo, _, facts.user = value.partition(" ")
        elif key == "agentos-node":
            facts.node = True
        elif key == "openajar-installed":
            facts.installed.append(value)
        elif key == "openajar-systemd":
            facts.systemd = True
    return facts


def _version(text: str) -> tuple[int, int]:
    parts = (re.findall(r"\d+", text) + ["0", "0"])[:2]
    return int(parts[0]), int(parts[1])


def refusal(host: str, facts: HostFacts) -> ClientError | None:
    """Why this host cannot get a node from connect, or None when it can."""
    supported = "the node installer supports Ubuntu 22.04 or later (24.04 is tested) or Debian 12, on x86_64 or arm64"
    why, hint = None, supported
    least = SUPPORTED.get(facts.os_id)
    if facts.kernel != "Linux":
        why = f"it runs {facts.kernel or 'an unknown system'}, not Linux"
        hint = f"{supported}. A Mac cannot be a node yet: try `openajar dev up --seed` instead"
    elif least is None or _version(facts.os_version) < least:
        why = f"it runs {facts.os_label}"
    elif facts.arch not in ARCHES:
        why = f"it has a {facts.arch or 'unknown'} CPU"
    elif not facts.systemd:
        why = "it does not run systemd, which the node's service needs"
        hint = "install by hand there (deploy/README.md), then run connect again"
    elif facts.sudo == "no":
        who = facts.user or "the SSH user"
        why = f"{who} cannot run sudo without a password"
        hint = (f"connect as root (`openajar connect root@{host.rsplit('@', 1)[-1]}`), or give {who} "
                "passwordless sudo, then run connect again")
    if why is None:
        return None
    return ClientError(f"{host} has no openajar node, and connect cannot install one there: {why}", hint)


def install_lines(host: str, source: Source) -> list[str]:
    first = (f"send this checkout ({source.checkout}) to {SOURCE_DIR} with rsync over SSH" if source.sends
             else f"git clone --depth 1 --branch {source.ref} {source.url} {SOURCE_DIR}")
    return [first,
            f"{SOURCE_DIR}/deploy/install-node.sh {' '.join(INSTALL_ARGS)}",
            "(as root on " + host + ": the hardened agentosd unit, running as the unprivileged openajar user)"]


class RemoteInstaller:
    def __init__(self, ssh: str, out: Callable[[str], None] = print, rsync: str | None = None):
        self.ssh = ssh
        self.out = out
        self.rsync = rsync

    def _command(self, host: str) -> list[str]:
        return ssh_read_command(self.ssh, valid_host(host))

    def probe(self, host: str, remote_home: str) -> HostFacts:
        try:
            proc = subprocess.run(self._command(host), input=probe_script(remote_home), capture_output=True,
                                  text=True, timeout=PROBE_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            raise SshError(f"cannot reach {host}: no answer in {PROBE_TIMEOUT} seconds") from None
        except OSError as exc:
            raise SshError(f"could not run ssh: {exc}") from None
        if proc.returncode == 255:
            raise explain_ssh_failure(host, proc.stderr)
        return parse_probe(proc.stdout)

    def show(self, host: str, script: str) -> None:
        self.out(f"  $ {' '.join(shlex.quote(a) for a in self._command(host))} <<'EOF'")
        for line in script.rstrip("\n").splitlines():
            self.out(f"  {line}")
        self.out("  EOF")

    def show_plan(self, host: str, source: Source, *, as_root: bool) -> None:
        """Every script and command an install from `source` runs (the dry run)."""
        if source.checkout is not None:
            self.show(host, prepare_script())
            cmd = send_command(self.rsync or "rsync", self.ssh, host, source.checkout, as_root=as_root)
            self.out(f"  $ {' '.join(shlex.quote(a) for a in cmd)}")
        self.show(host, source_install_script(source))

    def send(self, host: str, checkout: Path, *, as_root: bool) -> None:
        """Copy this computer's checkout to SOURCE_DIR on the host."""
        if not self.rsync:
            raise ClientError("`rsync` is not on PATH, so this checkout cannot be sent to the node",
                              "install rsync here, or set OPENAJAR_GIT_URL to a repository the node can clone")
        try:
            prep = subprocess.run(self._command(host), input=prepare_script(), text=True, timeout=10 * 60,
                                  check=False)
            if prep.returncode == 0:
                self.out(f"==> Sending {checkout} to {host}:{SOURCE_DIR}")
                sent = subprocess.run(send_command(self.rsync, self.ssh, host, checkout, as_root=as_root),
                                      text=True, timeout=INSTALL_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            raise ClientError(f"sending the code to {host} did not finish in time",
                              "check the connection, then run connect again (it is safe to repeat)") from None
        except OSError as exc:
            raise SshError(f"could not run ssh or rsync: {exc}") from None
        if prep.returncode != 0:
            raise ClientError(f"could not prepare {SOURCE_DIR} on {host} (exit {prep.returncode})",
                              "its last lines above say why; fix that and run connect again (it is safe to repeat)")
        if sent.returncode != 0:
            raise ClientError(f"rsync could not send this checkout to {host} (exit {sent.returncode})",
                              "its lines above say why; fix that and run connect again (it is safe to repeat)")

    def install(self, host: str, source: Source, *, as_root: bool = True) -> None:
        """Send the code when it comes from this checkout, then run the install script, its output
        going straight to this terminal."""
        if source.checkout is not None:
            self.send(host, source.checkout, as_root=as_root)
        try:
            proc = subprocess.run(self._command(host), input=source_install_script(source), text=True,
                                  timeout=INSTALL_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            raise ClientError(f"the install on {host} did not finish in {INSTALL_TIMEOUT // 60} minutes",
                              f"look at it on the node (`ssh {host}`), then run connect again") from None
        except OSError as exc:
            raise SshError(f"could not run ssh: {exc}") from None
        if proc.returncode == 255:
            raise SshError(f"the SSH connection to {host} broke during the install",
                           "run connect again: the installer is safe to repeat")
        if proc.returncode != 0:
            raise ClientError(f"the node installer on {host} failed (exit {proc.returncode})",
                              "its last lines above say why; fix that and run connect again (it is safe to repeat)")

    def sign_in_link(self, host: str, remote_home: str, local_port: int) -> str | None:
        """A one-time panel sign-in link that opens through this computer's tunnel, or None."""
        try:
            proc = subprocess.run(self._command(host), input=login_script(remote_home, local_port),
                                  capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False)
        except (subprocess.TimeoutExpired, OSError):
            return None
        found = re.search(r"https?://\S+", proc.stdout) if proc.returncode == 0 else None
        return found.group(0) if found else None
