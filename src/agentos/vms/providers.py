"""Which provider vms.toml names, made from its settings and its secret: Hetzner (a token), Docker
(nothing), Morph Cloud (an API key and the gateway's pinned host keys)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentos.vms.models import MORPH_TOKEN_FILE, TOKEN_FILE, VmError, VmSettings


def token_path(home: Path, settings: VmSettings) -> Path:
    """Where the provider's secret is kept (docker has none: the Hetzner path, never read)."""
    return Path(home) / (MORPH_TOKEN_FILE if settings.provider == "morph" else TOKEN_FILE)


def configured(home: Path, settings: VmSettings) -> bool:
    return settings.provider == "docker" or token_path(home, settings).exists()


def make(home: Path, settings: VmSettings) -> Any:
    """The provider for this home's settings. VmError when its secret is not set up."""
    s = settings
    if s.provider == "docker":
        from agentos.vms.docker import DockerProvider

        return DockerProvider(image=s.docker_image, docker=s.docker_bin, cpus=s.docker_cpus,
                              memory_gb=s.docker_memory_gb, pids=s.docker_pids)
    path = token_path(home, s)
    if s.provider == "morph":
        if not path.exists():
            raise VmError("VMs are not set up on this node: run `agentos vm setup --provider morph` with a Morph "
                          "API key")
        from agentos.vms.morph import MorphProvider, gateway_known_hosts

        return MorphProvider(path.read_text(), api_url=s.morph_api_url, ssh_host=s.morph_ssh_host,
                             base_image=s.morph_base_image, usd_per_mcu=s.morph_usd_per_mcu,
                             storage_usd_per_gb_month=s.morph_storage_usd_per_gb_month,
                             eur_per_usd=s.morph_eur_per_usd, host_keys=gateway_known_hosts(home))
    if not path.exists():
        raise VmError("VMs are not set up on this node: run `agentos vm setup` with a Hetzner "
                      "Cloud API token (read and write)")
    from agentos.vms.hetzner import HetznerProvider

    return HetznerProvider(path.read_text(), api_url=s.api_url)
