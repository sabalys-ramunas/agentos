"""Where env state lives under OPENAJAR_HOME, and the package's own settings (env.toml)."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from openajar.core.models import parse_lane_id


class EnvSettings(BaseModel):
    """Optional `$OPENAJAR_HOME/env.toml`. Every field has a safe default."""
    cgroup_root: str = "/sys/fs/cgroup/agentos-env"
    max_concurrent_exec: int = 4          # per lane
    max_output_bytes: int = 65536         # per stream, per exec (head + tail kept)
    default_timeout_s: float = 60.0
    max_timeout_s: float = 3600.0
    shell_idle_s: float = 1800.0          # detached shells with no activity are closed after this
    shell_exited_keep_s: float = 300.0
    shell_buffer_bytes: int = 1_048_576
    max_shells_per_lane: int = 8
    max_shells: int = 32
    lane_idle_s: float = 900.0            # warm lanes with no workload go cold after this
    service_budget_mb: int = 1536         # always-warm RAM budget for services (separate from browsers)
    reaper_interval_s: float = 15.0
    proxy_port: int = 3128                # the forwarder's port inside each lane netns
    install_proxy_port: int = 3129
    install_timeout_s: float = 900.0
    # pinned install sources; apt hosts are read from the node's apt sources when empty
    apt_hosts: list[str] = Field(default_factory=list)
    pip_index: str = "https://pypi.org/simple"
    pip_hosts: list[str] = Field(default_factory=lambda: ["pypi.org", "files.pythonhosted.org"])
    npm_registry: str = "https://registry.npmjs.org/"
    npm_hosts: list[str] = Field(default_factory=lambda: ["registry.npmjs.org"])
    locked_apt_allowlist: list[str] = Field(default_factory=list)
    # extra host dirs exposed read-only to isolated lanes (beyond /usr and a sanitized /etc)
    extra_host_lowers: list[str] = Field(default_factory=list)
    extra_etc_masks: list[str] = Field(default_factory=list)
    agent_create_presets: list[str] = Field(default_factory=lambda: ["sandbox", "locked"])
    # the most an agent's terminal_create may ask for (the owner's `openajar env create` is not capped)
    agent_max_memory_mb: int = 8192
    agent_max_cpus: float = 4.0
    agent_max_disk_mb: int = 32768
    # background commands (terminal_job_start): their output is logged on the node, outside the lane
    max_jobs_per_lane: int = 4
    max_jobs: int = 16
    job_log_max_bytes: int = 64 * 1024 * 1024   # per stream; past it the output is counted, not kept
    jobs_keep_per_lane: int = 20                # finished jobs kept (meta and logs) per lane
    # synced working trees (terminal_upload, terminal_job_start's sync): chunks kept on the node per lane
    upload_max_mb: int = 4096
    upload_chunk_max_bytes: int = 4 * 1024 * 1024


def load_settings(home: Path) -> EnvSettings:
    path = home / "env.toml"
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    return EnvSettings(**data)


class EnvPaths:
    def __init__(self, home: Path):
        self.home = Path(home)

    @property
    def db(self) -> Path:
        return self.home / "env.db"

    @property
    def run(self) -> Path:
        return self.home / "run" / "env"

    @property
    def lock(self) -> Path:
        return self.home / "run" / "env.lock"

    @property
    def images(self) -> Path:
        return self.home / "images"

    @property
    def mask(self) -> Path:
        return self.images / ".mask"

    @property
    def empty(self) -> Path:
        return self.images / ".empty"

    @property
    def volumes(self) -> Path:
        return self.home / "volumes"

    @property
    def templates(self) -> Path:
        return self.home / "templates"

    @property
    def instance(self) -> str:
        """Short stable id of this OPENAJAR_HOME, so several homes on one node never collide."""
        return hashlib.sha256(str(self.home.resolve()).encode()).hexdigest()[:10]

    def lane_dir(self, lane_id: str) -> Path:
        ws, name = parse_lane_id(lane_id)
        return self.home / "lanes" / ws / name

    def upper(self, lane_id: str) -> Path:
        return self.lane_dir(lane_id) / "upper"

    def upper_image(self, lane_id: str) -> Path:
        return self.lane_dir(lane_id) / "upper.img"

    def ovl_work(self, lane_id: str) -> Path:
        return self.lane_dir(lane_id) / ".ovl-work"

    def workdir(self, lane_id: str) -> Path:
        """env0 (workbench) working directory."""
        return self.lane_dir(lane_id) / "work"

    def logs(self, lane_id: str) -> Path:
        return self.lane_dir(lane_id) / "logs"

    def jobs(self, lane_id: str) -> Path:
        """Background commands' meta and output (terminal_job_start), on the node, never inside the lane."""
        return self.logs(lane_id) / "jobs"

    def sync(self, lane_id: str) -> Path:
        """Chunks uploaded for a working-tree sync and the last applied manifests, outside the lane."""
        return self.lane_dir(lane_id) / "sync"

    def run_dir(self, key: str) -> Path:
        return self.run / key

    def image_dir(self, name: str, version: int) -> Path:
        return self.images / f"{name}@{version}"

    def volume(self, name: str) -> Path:
        return self.volumes / name
