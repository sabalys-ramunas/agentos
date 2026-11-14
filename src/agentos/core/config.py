"""Node configuration: `$AGENTOS_HOME/node.toml` (default home /var/lib/agentos)."""

from __future__ import annotations

import ipaddress
import json
import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, ValidationError, model_validator

from agentos.core import fs
from agentos.core.aio import maybe_await
from agentos.core.ceiling import problems as ceiling_problems
from agentos.core.models import ProxyConfig, parse_lane_id
from agentos.core.toolsets import FULL_ALIASES, NAMES, OTHER

DEFAULT_HOME = Path("/var/lib/agentos")
# The smallest per-lane memory cap: a warm lane on a logged-in social feed takes 0.8 to 1 GB
# (browser, Xvfb framebuffer, x11vnc, driver), and MemoryHigh at 90% of a smaller cap throttles it.
LANE_MEMORY_MIN_MB = 1536
# The cap a lane gets when node.toml does not set lane_memory_max_mb. Ten lanes at this ceiling are
# 15 GB of a 16 GB node, and at the 0.8 to 1 GB they really take about 10 GB, so ten fit with room.
# It is also the smallest cap the check below allows, so no lower value is on offer.
LANE_MEMORY_DEFAULT_MB = 1536


class NodeConfig(BaseModel):
    name: str = "node-1"
    home: Path = DEFAULT_HOME
    ssh_host: str = ""  # SSH alias of this node, used in tunnel hints (empty: the machine's hostname)

    # MCP server (localhost only; reach it over SSH or, from M5, the mesh)
    mcp_host: str = "127.0.0.1"
    mcp_bind_any: bool = False  # allow a non-loopback mcp_host (only behind a firewall or a VPN)
    mcp_port: int = 7400
    mcp_token: str = ""
    mcp_toolsets: list[str] = ["core"]  # what tools/list shows by default (agentos.mcp.toolsets); "full": all

    # Displays and live views: slot N uses :display_base+N and viewer_base+N. x11vnc listens on
    # home/vnc/display-<n>.sock, not a port; vnc_base is unused and stays only so node.toml files
    # written before still load.
    display_base: int = 100
    vnc_base: int = 5910
    viewer_base: int = 6100
    max_slots: int = 10

    # Lane lifecycle
    max_warm_lanes: int = 3
    idle_seconds: int = 600
    busy_timeout: float = 60.0
    queue_timeout: float = 60.0      # a call waits this long in a lane's queue before it fails with busy
    claim_max_seconds: int = 900     # while others wait, a lane claim is not renewed past this
    self_check: bool = True
    control_idle_seconds: int = 600  # a human's control returns to agents after this idle time
    memory_floor_mb: int = 800       # no new lane warms below this much available memory
    disk_floor_bytes: int = 2 * 1024**3  # no lane warms with less free disk under its profile
    # Per-lane memory cap (a systemd scope), LANE_MEMORY_MIN_MB or more, and always asks for a scope.
    # Unset: LANE_MEMORY_DEFAULT_MB inside the default scopes (lane_scopes). 0: no memory cap.
    # The memory watchdog (agentos.node.memwatch) holds lanes to the same cap, scope or not.
    lane_memory_max_mb: int | None = None
    lane_cpu_max_pct: int | None = None    # optional per-lane CPU cap, percent of one core (a systemd scope)
    lane_scopes: bool = True               # each lane's browser in its own systemd scope (CPU shared per lane)
    lane_os: list[str] = ["linux"]         # the OS a new lane's identity claims (the node's own); empty: Camoufox's mix
    view_token_seconds: int = 900    # lifetime of a signed viewer link
    # Session keepalive (agentos.keepalive): light read-only visits that keep idle lanes signed in.
    # Off until the owner turns it on, because it opens real accounts unattended.
    keepalive: bool = False
    # The agenda (agentos.agenda, docs/AGENDA.md): a request in Needs you for an entry planned weeks
    # ahead stays open until the entry's start, but never more than this many days after it was proposed.
    agenda_approval_max_days: int = 14

    # Controller channel (docs/PROTOCOL.md), written by `agentos node join`. The node key is
    # secrets/node.key; with controller_url and node_id set, agentosd keeps the channel up.
    controller_url: str = ""      # wss://<controller>/v1/node/connect (plain ws only to localhost)
    node_id: str = ""             # this node's id in the controller, from enrollment
    org_id: str = ""
    controller_pubkey: str = ""   # pinned at enrollment: revoke and config_update must carry its signature
    grant_pubkey: str = ""        # the org's grant key: grants it signs verify on this node
    # Where each signed audit checkpoint head goes (agentos.node.anchors): "controller" (only once
    # the node has joined one, so a single node sends nothing) and/or "s3://bucket/prefix" (a
    # bucket with Object Lock on). [] turns anchoring off.
    audit_anchors: list[str] = ["controller"]
    audit_anchor_retain_days: int = 400   # how long S3 Object Lock keeps each head

    # The controller ceiling (agentos.policy.ceiling): what a controller may do here, set only on
    # the node. The defaults are for a hosted controller; `agentos up` writes the full ceiling.
    controller_max_effect: str = "commit"
    controller_specials: list[str] = []
    controller_streams: list[str] = ["view"]
    # who wrote the ceiling: "default" (nobody: the hosted default, or a node from before the
    # ceiling), "join" (`agentos node join` to a controller someone else runs), "self_hosted"
    # (`agentos up` or `node join --self-hosted`) or "owner" (the owner pinned it by hand).
    # `agentos up` raises it to the full ceiling only from default (unchanged) or join.
    controller_ceiling_by: str = "default"
    # Standing approvals a controller sends (agentos.node.channel.standing), bounded here by the
    # owner whatever the controller says: uses per approval, unused uses held at once per workspace,
    # how many days one may run, and how long one covers calls after its controller last confirmed
    # it (a revoke sent while the node was offline is lost; the controller resends its set on connect).
    controller_standing_max_count: int = 50
    controller_standing_max_active: int = 200
    controller_standing_max_days: int = 30
    controller_standing_confirm_hours: int = 24

    @model_validator(mode="after")
    def _limits_make_sense(self) -> NodeConfig:
        problems = []
        if self.max_slots < 1:
            problems.append("max_slots must be at least 1")
        if not 1 <= self.max_warm_lanes <= max(self.max_slots, 1):
            problems.append(f"max_warm_lanes must be between 1 and max_slots ({self.max_slots})")
        if not 0 <= self.mcp_port <= 65535:  # 0 picks a free port
            problems.append("mcp_port must be between 0 and 65535")
        for name in ("vnc_base", "viewer_base"):
            port = getattr(self, name)
            if not 1 <= port <= 65535 - self.max_slots:
                problems.append(f"{name} must leave room for {self.max_slots} ports below 65536")
        if self.display_base < 1:
            problems.append("display_base must be at least 1")
        if self.idle_seconds < 0 or self.control_idle_seconds < 0 or self.busy_timeout < 0 or self.queue_timeout < 0:
            problems.append("idle_seconds, control_idle_seconds, busy_timeout and queue_timeout cannot be negative")
        if self.claim_max_seconds < 1:
            problems.append("claim_max_seconds must be at least 1")
        if not 10 <= self.view_token_seconds <= 86400:
            problems.append("view_token_seconds must be between 10 and 86400")
        if not 1 <= self.controller_standing_max_count <= self.controller_standing_max_active <= 1_000_000:
            problems.append("controller_standing_max_count must be at least 1 and at most "
                            "controller_standing_max_active (1000000 at most)")
        if not 1 <= self.controller_standing_max_days <= 90:
            problems.append("controller_standing_max_days must be between 1 and 90")
        if not 1 <= self.agenda_approval_max_days <= 90:
            problems.append("agenda_approval_max_days must be between 1 and 90")
        if not 1 <= self.controller_standing_confirm_hours <= 720:
            problems.append("controller_standing_confirm_hours must be between 1 and 720")
        if self.lane_memory_max_mb and self.lane_memory_max_mb < LANE_MEMORY_MIN_MB:
            problems.append(f"lane_memory_max_mb must be 0 (no cap) or at least {LANE_MEMORY_MIN_MB} (a warm lane on "
                            "a logged-in feed takes 0.8 to 1 GB, and reclaim starts at 90% of the cap)")
        if self.lane_cpu_max_pct is not None and self.lane_cpu_max_pct < 10:
            problems.append("lane_cpu_max_pct must be at least 10 (percent of one core)")
        if set(self.lane_os) - {"windows", "macos", "linux"}:
            problems.append("lane_os may list only windows, macos and linux")
        if self.mcp_toolsets != ["core"]:
            unknown = [n for n in self.mcp_toolsets if n.strip().lower() not in {*NAMES, OTHER, *FULL_ALIASES}]
            if unknown:
                problems.append(f"mcp_toolsets may list only full or these toolsets: {', '.join(NAMES)} "
                                f"(unknown: {', '.join(unknown)[:200]})")
        problems += ceiling_problems(self.controller_max_effect, self.controller_specials, self.controller_streams,
                                     self.controller_ceiling_by)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ---- paths ----
    @property
    def db_path(self) -> Path:
        return self.home / "agentos.db"

    @property
    def secrets_dir(self) -> Path:
        return self.home / "secrets"

    @property
    def proxies_path(self) -> Path:
        return self.secrets_dir / "proxies.json"

    def lane_dir(self, lane_id: str) -> Path:
        workspace, name = parse_lane_id(lane_id)
        return self.home / "lanes" / workspace / name

    def profile_dir(self, lane_id: str) -> Path:
        return self.lane_dir(lane_id) / "profile"

    def fingerprint_path(self, lane_id: str) -> Path:
        """The lane's pin. A lane on a device (AUD-02) keeps a byte-for-byte copy of the device's
        pin here, so whoever reads a lane's pin (its timezone, its screen) reads the device's."""
        return self.lane_dir(lane_id) / "fingerprint.json"

    @property
    def devices_dir(self) -> Path:
        return self.home / "devices"

    def device_dir(self, name: str) -> Path:
        from agentos.core.models import DEVICE_NAME_RE

        if not DEVICE_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid device name {name!r}")
        return self.devices_dir / name

    def device_pin_path(self, name: str) -> Path:
        """A device's pinned fingerprint, the one its lanes launch with."""
        return self.device_dir(name) / "fingerprint.json"

    def shots_dir(self, lane_id: str) -> Path:
        return self.lane_dir(lane_id) / "shots"

    @property
    def enrolled(self) -> bool:
        """Joined to a controller (`agentos node join`)."""
        return bool(self.controller_url and self.node_id and self.controller_pubkey)


def home_from_env() -> Path:
    return Path(os.environ.get("AGENTOS_HOME", DEFAULT_HOME))


def config_path(home: Path | None = None) -> Path:
    return (home or home_from_env()) / "node.toml"


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_mcp_bind(config: NodeConfig) -> None:
    """The MCP endpoint holds the keys to every lane: loopback only unless node.toml opts out."""
    if not config.mcp_bind_any and not is_loopback_host(config.mcp_host):
        raise RuntimeError(f"mcp_host {config.mcp_host!r} is not a loopback address; reach the node over "
                           "SSH, or set mcp_bind_any = true in node.toml if a firewall protects it")


def config_problems(exc: ValidationError) -> str:
    """A ValidationError as text without input values (they can include the MCP token)."""
    parts = []
    for err in exc.errors(include_input=False, include_url=False):
        where = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        msg = str(err.get("msg", "invalid")).removeprefix("Value error, ")
        parts.append(f"{where}: {msg}" if where else msg)
    return "; ".join(parts) or "invalid settings"


def load_config(home: Path | None = None) -> NodeConfig:
    home = home or home_from_env()
    path = config_path(home)
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    data["home"] = str(home)
    return NodeConfig(**data)


def write_private(path: Path, text: str) -> None:
    """Write a file that holds secrets so it is never readable by anyone else, not even for a
    moment (agentos.core.fs: 600 temp file, fsync, atomic rename, directory fsync)."""
    fs.write_private(path, text)


def save_config(cfg: NodeConfig) -> Path:
    path = config_path(cfg.home)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json", exclude_none=True)  # TOML has no null: unset means default
    data.pop("home", None)
    write_private(path, tomli_w.dumps(data))
    return path


def load_proxies(cfg: NodeConfig) -> dict[str, ProxyConfig]:
    """Proxies by label. A malformed file raises ValueError naming only the label and the bad
    fields, never their values (pydantic's own messages quote the input, password included)."""
    if not cfg.proxies_path.exists():
        return {}
    try:
        raw = json.loads(cfg.proxies_path.read_text())
    except ValueError:
        raise ValueError(f"{cfg.proxies_path} is not valid JSON") from None
    if not isinstance(raw, dict):
        raise ValueError(f"{cfg.proxies_path} must map labels to proxy entries")  # noqa: TRY004 (callers treat any malformed file as ValueError)
    out: dict[str, ProxyConfig] = {}
    for label, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"proxy {label!r} in {cfg.proxies_path} is not an object")  # noqa: TRY004
        try:
            out[label] = ProxyConfig(label=label, **entry)
        except (ValidationError, TypeError) as e:
            fields = sorted({".".join(str(x) for x in err["loc"]) for err in e.errors()}) \
                if isinstance(e, ValidationError) else ["unexpected fields"]
            raise ValueError(f"proxy {label!r} in {cfg.proxies_path} is invalid: check {', '.join(fields)}") from None
    return out


# Fields an agent environment's tunnel sets (agentos.environments keeps those in memory, never saved). A
# saved proxy that leaves them at their default keeps proxies.json as it was: host, port and credentials.
_ENV_ONLY_DEFAULTS = {"scheme": "http", "check_exit": True}


async def proxies_by_label(service: Any, config: NodeConfig | None) -> dict[str, Any]:
    """The proxies by label: from the node's proxies service when there is one (an item is a ProxyConfig
    or a dict with `label`), else from the node's proxies file. Unreadable or no config: none."""
    if service is not None:
        items = await maybe_await(service.list())
        return {(p.label if hasattr(p, "label") else p["label"]): p for p in items}
    if config is not None:
        try:
            return load_proxies(config)
        except (OSError, ValueError):
            return {}
    return {}


def save_proxies(cfg: NodeConfig, proxies: dict[str, ProxyConfig]) -> None:
    cfg.secrets_dir.mkdir(parents=True, exist_ok=True)
    cfg.secrets_dir.chmod(0o700)
    out = {}
    for p in proxies.values():
        entry = p.model_dump(exclude={"label"})
        out[p.label] = {k: v for k, v in entry.items() if _ENV_ONLY_DEFAULTS.get(k, object()) != v}
    write_private(cfg.proxies_path, json.dumps(out, indent=2))
