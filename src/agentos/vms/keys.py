"""Per-VM keys, made on the node: the key the node signs in with, and the VM's own host key.

Both are ed25519. The host key goes to the VM through cloud-init (`ssh_keys`), so the node knows
the VM's identity before it ever connects; the client key's public half goes to the provider as a
throwaway SSH key resource, deleted right after the server exists. A provider without cloud-init
(Morph) hands out its own instead (`adopt`).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentos.core.config import write_private


@dataclass(frozen=True)
class VmKeys:
    dir: Path
    client_key: Path          # private, 0600
    client_pub: str           # "ssh-ed25519 AAAA... agentos-vm"
    host_private: str         # OpenSSH PEM, for cloud-init only
    host_pub: str             # "ssh-ed25519 AAAA..."
    known_hosts: Path         # one line under the alias
    alias: str


def _pair(comment: str) -> tuple[str, str]:
    k = Ed25519PrivateKey.generate()
    priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                           serialization.NoEncryption()).decode()
    pub = k.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    return priv, f"{pub} {comment}"


def make(vms_dir: Path, vm_id: str) -> VmKeys:
    d = vms_dir / vm_id
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(vms_dir, 0o700)
    os.chmod(d, 0o700)
    client_priv, client_pub = _pair(f"agentos-{vm_id}")
    host_priv, host_pub = _pair(f"host-{vm_id}")
    alias = f"agentos-{vm_id}"
    key = d / "id_ed25519"
    write_private(key, client_priv)
    kh = d / "known_hosts"
    write_private(kh, f"{alias} {' '.join(host_pub.split()[:2])}\n")
    return VmKeys(dir=d, client_key=key, client_pub=client_pub, host_private=host_priv,
                  host_pub=" ".join(host_pub.split()[:2]), known_hosts=kh, alias=alias)


def forget(vms_dir: Path, vm_id: str) -> None:
    shutil.rmtree(vms_dir / vm_id, ignore_errors=True)


def cloud_init(keys: VmKeys) -> str:
    """User data: the pinned host key, keys only, no root password, nothing else."""
    host_priv = "\n".join("    " + line for line in keys.host_private.strip().splitlines())
    return (
        "#cloud-config\n"
        "ssh_pwauth: false\n"
        "disable_root: false\n"
        "ssh_deletekeys: true\n"
        "ssh_genkeytypes: []\n"
        "ssh_keys:\n"
        "  ed25519_private: |\n"
        f"{host_priv}\n"
        f"  ed25519_public: {keys.host_pub}\n"
    )


def adopt(vms_dir: Path, vm_id: str, server: Any) -> None:
    """A provider that hands out the login itself (Morph: the instance's own private key, and its SSH
    gateway's host keys, pinned when the provider was set up): they replace the key and the host key
    agentos made, in the same files (0600), under the same alias. Nothing to do for another provider."""
    d = vms_dir / vm_id
    key = getattr(server, "ssh_key", None)
    if key:
        write_private(d / "id_ed25519", key)
    hosts = getattr(server, "host_keys", None) or []
    if hosts:
        write_private(d / "known_hosts", "".join(f"agentos-{vm_id} {h}\n" for h in hosts))


def move(vms_dir: Path, old_id: str, new_id: str) -> None:
    """A paused machine's keys (agentos.vms.pausing): kept aside under paused/ while it sleeps, so a lane
    of the closed environment reaches nothing; back under its next VM id when it resumes, the pinned
    host key under that id's alias."""
    src, dst = vms_dir / old_id, vms_dir / new_id
    if not src.is_dir():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(vms_dir, 0o700)
    shutil.rmtree(dst, ignore_errors=True)
    src.rename(dst)
    kh = dst / "known_hosts"
    if kh.exists() and "/" not in new_id:
        lines = [" ".join([f"agentos-{new_id}", *ln.split()[1:]]) for ln in kh.read_text().splitlines() if ln.split()]
        write_private(kh, "".join(ln + "\n" for ln in lines))
