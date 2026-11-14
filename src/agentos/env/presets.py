"""Presets to dials, dials to an honest description.

A beginner picks a preset; the dials (network, filesystem, processes, programs, volumes,
base image, limits) can be overridden one by one. `resolve()` returns the profile, the
cgroup limits and the policy flags, and refuses combinations that would break a promise
(for example a service sharing the node network, where it could listen on a public port).
"""

from __future__ import annotations

import re
from typing import Any

from agentos.core.errors import InvalidRequest
from agentos.core.models import IsolationProfile, LaneKind, Preset
from agentos.env.models import EnvLane, Limits, Policy

VOLUME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
HOST_RE = re.compile(r"^(\*\.|\.)?[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)*(:\d{1,5})?$")
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(:\d{1,5})?$")

WORKBENCH_WARNING = (
    "Workbench is env0: commands run directly on the node, as the user agentosd runs as, with no "
    "filesystem, process or network isolation, so they can read the vault and the node token. Use it "
    "only for work you would run on the node yourself."
)

_PRESETS: dict[Preset, tuple[IsolationProfile, Limits, Policy]] = {
    Preset.ACCOUNT: (
        IsolationProfile(network="isolated", filesystem="overlay", processes="isolated", programs="base"),
        Limits(memory_mb=1536, cpus=1.0, pids=512, disk_mb=4096),
        Policy(shell=False, exec=False, install=False, egress_default="allow", tier=0),
    ),
    Preset.WORKBENCH: (
        IsolationProfile(network="shared", filesystem="shared", processes="shared", programs="overlay"),
        Limits(),
        Policy(shell=True, exec=True, install=True, seccomp=False, tier=2, warning=WORKBENCH_WARNING),
    ),
    Preset.SANDBOX: (
        IsolationProfile(network="off", filesystem="ephemeral", processes="isolated", programs="overlay"),
        Limits(memory_mb=1024, cpus=1.0, pids=256, disk_mb=512),
        Policy(shell=True, exec=True, install=True, egress_default="allow", tier=0),
    ),
    Preset.SERVICE: (
        IsolationProfile(network="isolated", filesystem="overlay", processes="isolated", programs="base"),
        Limits(memory_mb=512, cpus=0.5, pids=256, disk_mb=2048),
        Policy(shell=False, exec=True, install=False, egress_default="deny", tier=0),
    ),
    Preset.LOCKED: (
        IsolationProfile(network="isolated", filesystem="overlay", processes="isolated", programs="overlay"),
        Limits(memory_mb=512, cpus=0.5, pids=128, disk_mb=1024),
        Policy(shell=False, exec=True, install=True, signed_only=True, noexec_writable=True,
               egress_default="deny", tier=0),
    ),
    # docs/design/always-on-agents.md section 5.1; the runner's egress hosts are added by brief B1
    Preset.AGENT: (
        IsolationProfile(network="isolated", filesystem="overlay", processes="isolated", programs="base"),
        Limits(memory_mb=1536, cpus=1.0, pids=512, disk_mb=8192),
        Policy(shell=True, exec=True, install=False, egress_default="deny", tier=0),
    ),
}

DIAL_KEYS = {"network", "egress_allowlist", "filesystem", "processes", "programs", "volumes", "base_image"}
LIMIT_KEYS = {"memory_mb", "cpus", "pids", "disk_mb", "tmp_mb"}


def preset_defaults(preset: Preset) -> tuple[IsolationProfile, Limits, Policy]:
    profile, limits, policy = _PRESETS[preset]
    return profile.model_copy(deep=True), limits.model_copy(deep=True), policy.model_copy(deep=True)


def normalize_host(entry: str) -> str:
    e = entry.strip().lower().rstrip(".")
    if not (HOST_RE.match(e) or IP_RE.match(e)):
        raise InvalidRequest(f"not a host name for the egress allowlist: {entry!r}")
    return e


def resolve(
    preset: Preset | str,
    *,
    kind: LaneKind | str = LaneKind.TERMINAL,
    overrides: dict[str, Any] | None = None,
) -> tuple[IsolationProfile, Limits, Policy]:
    """Preset plus dial overrides -> (profile, limits, policy). Raises InvalidRequest."""
    preset = Preset(preset)
    kind = LaneKind(kind)
    profile, limits, policy = preset_defaults(preset)
    overrides = dict(overrides or {})

    unknown = set(overrides) - DIAL_KEYS - LIMIT_KEYS
    if unknown:
        raise InvalidRequest(f"unknown dial(s): {', '.join(sorted(unknown))}")

    dials = {k: v for k, v in overrides.items() if k in DIAL_KEYS and v is not None}
    if dials:
        profile = profile.model_copy(update=dials)
    lim = {k: v for k, v in overrides.items() if k in LIMIT_KEYS}
    if lim:
        limits = limits.model_copy(update=lim)

    _check_profile(profile, kind)
    _settle_limits(profile, limits, preset)
    # programs dial: base -> installs refused, overlay -> lane_install allowed
    policy.install = profile.programs == "overlay" and preset != Preset.ACCOUNT
    _tier(profile, policy)
    return profile, limits, policy


DIAL_WORDS = {"network": ("shared", "isolated", "off"), "filesystem": ("shared", "overlay", "ephemeral"),
              "processes": ("shared", "isolated"), "programs": ("base", "overlay")}


def _check_profile(profile: IsolationProfile, kind: LaneKind) -> None:
    """Each dial one of its words, the allowlist and volumes well formed, and the dials agree."""
    for dial, words in DIAL_WORDS.items():
        value = getattr(profile, dial)
        if value not in words:
            listed = ", ".join(words[:-1]) + f" or {words[-1]}"
            raise InvalidRequest(f"{dial} must be {listed}, not {value!r}")
    profile.egress_allowlist = sorted({normalize_host(h) for h in profile.egress_allowlist})
    for v in profile.volumes:
        if not VOLUME_RE.match(v):
            raise InvalidRequest(f"volume names are lowercase letters, digits and dashes: {v!r}")
    if profile.egress_allowlist and profile.network != "isolated":
        raise InvalidRequest("an egress allowlist needs network=isolated (off has no egress, shared has no forwarder)")
    if kind == LaneKind.SERVICE and profile.network == "shared":
        raise InvalidRequest(
            "service lanes never share the node network, so nothing they open can listen outside "
            "127.0.0.1 or the lane; use network=isolated or off")


def _settle_limits(profile: IsolationProfile, limits: Limits, preset: Preset) -> None:
    """A disk limit only where the filesystem can hold one, and every limit positive."""
    if profile.filesystem == "shared" and limits.disk_mb is not None and preset != Preset.WORKBENCH:
        limits.disk_mb = None
    if profile.filesystem == "ephemeral" and not limits.disk_mb:
        limits.disk_mb = 512
    for name in ("memory_mb", "pids", "disk_mb", "tmp_mb"):
        val = getattr(limits, name)
        if val is not None and val <= 0:
            raise InvalidRequest(f"{name} must be positive")
    if limits.cpus is not None and limits.cpus <= 0:
        raise InvalidRequest("cpus must be positive")


def _tier(profile: IsolationProfile, policy: Policy) -> None:
    """The isolation tier (docs/ARCHITECTURE.md section 5) and the warning a weaker one carries."""
    if profile.network == "shared":
        policy.tier = 2
    elif profile.filesystem == "shared" or profile.processes == "shared":
        policy.tier = 1
    else:
        policy.tier = 0
    all_shared = profile.network == "shared" and profile.filesystem == "shared" and profile.processes == "shared"
    if all_shared:
        policy.seccomp = False
        policy.warning = WORKBENCH_WARNING
    elif policy.tier > 0 and not policy.warning:
        shared = [d for d in ("network", "filesystem", "processes") if getattr(profile, d) == "shared"]
        policy.warning = (f"Weaker isolation: this lane shares the node {', '.join(shared)}. "
                          "Do not bind secrets or private data to it.")


def describe(lane: EnvLane, *, mechanisms: bool = True) -> str:
    """One honest line about what the lane really gets on this node."""
    p, lim, pol = lane.profile, lane.limits, lane.policy
    if lane.remote is not None:
        r = lane.remote
        return (f"a disposable cloud machine ({r.label or r.provider}, {r.host}): its own kernel, disk and "
                "network, reached over SSH from the node; nothing of the node is on it. Deleted by agentos "
                "when its time is up.")
    if not lane.isolated:
        # the user it really runs as, worked out now (a lane made on a root node keeps working after
        # migrate-service-user, as the service user): never a stored word (QA 2026-11-10, finding 29)
        from agentos.env import exposure
        return exposure.label()
    parts: list[str] = []
    ns = ["mount", "cgroup"]
    if p.processes == "isolated":
        ns = ["pid", "ipc", "uts", *ns]
    if p.network != "shared":
        ns.append("net")
    parts.append(f"bubblewrap sandbox on the shared kernel ({', '.join(ns)} namespaces)")
    if p.filesystem == "ephemeral":
        parts.append(f"private ephemeral filesystem (tmpfs, {lim.disk_mb} MB, wiped when cold)")
    elif p.filesystem == "overlay":
        cap = f", capped at {lim.disk_mb} MB" if lim.disk_mb else ", no disk cap"
        parts.append(f"private persistent overlay{cap}")
    else:
        parts.append("the node's real filesystem, read-write")
    if p.filesystem != "shared":
        base = f"base image {p.base_image}" if p.base_image else "the node's /usr"
        parts.append(f"over {base} and a sanitized /etc, read-only for programs")
    if p.network == "off":
        parts.append("network off (loopback only)")
    elif p.network == "isolated":
        if p.egress_allowlist:
            allow = f"only to {', '.join(p.egress_allowlist)}"
        else:
            allow = "deny-all" if pol.egress_default == "deny" else "to any public host"
        parts.append(f"no route out except the node's HTTP(S) forwarder ({allow})")
    else:
        parts.append("the node's network, unfiltered")
    if pol.seccomp:
        parts.append("seccomp filter and reduced root capabilities")
    lims = []
    if lim.memory_mb:
        lims.append(f"{lim.memory_mb} MB")
    if lim.cpus:
        lims.append(f"{lim.cpus:g} CPU")
    if lim.pids:
        lims.append(f"{lim.pids} pids")
    if lims:
        parts.append("cgroup limits " + ", ".join(lims))
    if pol.signed_only:
        parts.append("allowlisted programs only, signed apt installs only, writable dirs noexec")
    text = "; ".join(parts) + "."
    if mechanisms:
        text += " Not a VM: a kernel exploit would escape it."
        if lane.preset == Preset.LOCKED:
            text += " No microVM or encryption at rest on this node yet."
    if pol.warning:
        text += " " + pol.warning
    from agentos.env import exposure
    if exposure.reaches_node(lane):  # a shared filesystem or process table: say so, every time
        text += " " + exposure.sandbox_label(lane)
    return text
