"""`agentos config show` and `agentos config example`: every node setting, what it means, its
resolved value, and where the value came from.

Sources, in the order they win: an environment variable, node.toml, the built-in default.
Secret settings are never printed, only whether they are set. Every NodeConfig field needs an
entry in DESCRIPTIONS (a test fails otherwise), so the commented example never falls behind.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from agentos.core.config import NodeConfig, config_path

SECRET_FIELDS = frozenset({"mcp_token"})
# Settings an environment variable overrides at runtime (where the code reads it).
ENV_OVERRIDES = {"home": "AGENTOS_HOME", "ssh_host": "AGENTOS_SSH_HOST"}
EXTRA_ENV = {"AGENTOS_PANEL_DIR": "where the built web app is served from (default: <install>/web/dist, then "
                                  "<home>/web/app from agentos web install)",
             "AGENTOS_WEB_RELEASE_URL": "where agentos web install downloads from (default: the GitHub releases)",
             "AGENTOS_NO_WEB": "1: agentos web update (the installer) leaves the web app out"}

# What each setting means, in the order `config example` prints them, grouped.
GROUPS: list[tuple[str, dict[str, str]]] = [
    ("This node", {
        "name": "The node's name, shown by whoami and in the panel.",
        "home": "Where the node keeps its data (AGENTOS_HOME wins over this).",
        "ssh_host": "The SSH alias other computers use for this node, printed in tunnel hints.",
    }),
    ("MCP server and live views (the viewer runs on the same port)", {
        "mcp_host": "The address agentosd listens on. Keep it on loopback and reach it over SSH or a VPN.",
        "mcp_bind_any": "Allow a non-loopback mcp_host. Only behind a firewall or a VPN.",
        "mcp_port": "The port for MCP, the panel and the live view.",
        "mcp_token": "The node token agents use. Written by `agentos node init`; never shared in chat.",
        "mcp_toolsets": "The toolsets an agent's tool list starts with: core (the lane loop) plus any of files, "
                        "control, vault, flows, connectors, memory, workspaces, admin, terminals, brains, "
                        "automation; or full for every tool. An MCP URL can ask for its own with "
                        "?toolsets=..., and an agent loads more with tools_more.",
    }),
    ("Displays (slot N uses display_base+N and viewer_base+N)", {
        "display_base": "The first X display number for lanes.",
        "vnc_base": "Unused. x11vnc listens on a unix socket under AGENTOS_HOME/vnc that only agentosd's "
                    "user can open, behind the signed relay.",
        "viewer_base": "A reserved range kept free for older live-view ports; doctor checks it is unused.",
        "max_slots": "How many displays exist, so how many lanes can be warm at most.",
    }),
    ("Lanes", {
        "max_warm_lanes": "How many browsers run at once; the least recently used idle one closes first.",
        "idle_seconds": "An idle warm lane closes after this many seconds.",
        "busy_timeout": "How long a call waits for a free warm slot before it fails with busy.",
        "queue_timeout": "How long a call waits in a lane's queue (behind other calls or a claim) before busy.",
        "claim_max_seconds": "While others wait, a lane claim is not renewed past this many seconds.",
        "self_check": "Check a lane's exit IP against its proxy before every warm-up.",
        "control_idle_seconds": "A human's control of a lane returns to agents after this idle time.",
        "memory_floor_mb": "No lane warms below this much available memory.",
        "disk_floor_bytes": "No lane warms with less free disk than this under its profile.",
        "lane_memory_max_mb": "The memory cap per lane. Unset means 1536, so ten lanes fit on 16 GB; it is the "
                              "MemoryMax of each lane's systemd scope wherever lanes get their default scopes (see "
                              "lane_scopes, reclaim from 90% of it). A number (at least 1536, since a warm lane on a "
                              "logged-in feed takes 0.8 to 1 GB) always asks for a scope, even under a capped "
                              "service. A watchdog holds lanes to the cap everywhere: it warns at 80% (a card in "
                              "Needs you) and, when a lane stays at 90% or more, closes its browser gracefully "
                              "(cookies kept) once nothing is mid-way on it. 0 turns the cap and the watchdog off.",
        "lane_cpu_max_pct": "An optional CPU cap per lane in percent of one core (150 is one and a half cores); "
                            "unset means no cap.",
        "lane_scopes": "Run each lane's browser in its own systemd scope in agentos-lanes.slice, so lanes share "
                       "the CPU per lane, all lanes together weigh like one service against agentosd, and a "
                       "runaway lane cannot take the others down (as root under systemd, on cgroup v2, when no "
                       "memory cap is set on agentosd's unit or a slice above it; a cap below always asks for a "
                       "scope, and agentos doctor says whether systemd made it).",
        "lane_os": "The OS a new lane's identity claims, drawn on its first launch (windows, macos, linux; one is "
                   "picked when several are listed). Linux by default: the node's own OS, so text, widgets and "
                   "canvas agree with what the identity says. Empty means Camoufox's own mix. A lane keeps its "
                   "pinned OS.",
        "view_token_seconds": "How long a signed live-view link works.",
        "keepalive": "Session keepalive: a short read-only visit to each idle lane's site every few days, inside "
                     "the lane's waking hours, so its login does not go stale. Off by default because it opens "
                     "real accounts unattended. `agentos lane keepalive --node on` turns it on; a lane opts out "
                     "with `agentos lane keepalive <lane> --off`.",
    }),
    ("Agenda (planned actions, docs/AGENDA.md)", {
        "agenda_approval_max_days": "The longest a request for an agenda entry waits in Needs you (1 to 90 days). "
                                    "It stays open until the entry's start (its time, or its window's start, "
                                    "plus 30 minutes), but at most this many days after it was proposed; an "
                                    "entry for as soon as approved gets the whole of it. When it closes "
                                    "undecided, the entry goes back to draft with the reason; propose it again. "
                                    "Other requests keep the node's approval time (a day by default).",
    }),
    ("Controller channel (written by `agentos node join`)", {
        "controller_url": "The controller this node dials out to (wss://, plain ws only to localhost).",
        "node_id": "This node's id at the controller, from enrollment.",
        "org_id": "The organization this node belongs to at the controller.",
        "controller_pubkey": "The controller's key, pinned at enrollment.",
        "grant_pubkey": "The organization's grant key: grants it signs work on this node.",
    }),
    ("Audit anchors (copies of the log's signed checkpoints, kept off this node)", {
        "audit_anchors": "Where each checkpoint head goes: \"controller\" (once this node has joined one) and "
                         "\"s3://bucket/prefix\" (a bucket with Object Lock). An empty list turns it off.",
        "audit_anchor_retain_days": "How many days S3 Object Lock keeps each head, so nobody can delete it sooner.",
    }),
    ("Controller ceiling (what a controller may do here; set only on this node)", {
        "controller_max_effect": "The highest effect a controller's grant may use here: read, draft, commit, pay "
                                 "or irreversible.",
        "controller_specials": "Specials a controller's grant may use here: secret, exec, install_overlay, "
                               "install_base, service, brain, memory, vm. Empty means none.",
        "controller_streams": "What a controller may open: view (watch a live view, read screenshots and proof), "
                              "control (take a lane over and type), terminal (attach to a shell; typing also "
                              "needs control, and exec in controller_specials) and logs (read the node's redacted logs).",
        "controller_ceiling_by": "Who set the ceiling: default, join, self_hosted or owner. `agentos up` "
                                 "raises it to the full ceiling only from join, or from default while it is "
                                 "still the hosted default; write owner to keep it as you set it.",
        "controller_standing_max_count": "The most uses one standing approval from a controller may cover here.",
        "controller_standing_max_active": "The most unused uses a controller's standing approvals may hold at "
                                          "once in one workspace.",
        "controller_standing_max_days": "How many days a controller's standing approval may run here (1 to 90).",
        "controller_standing_confirm_hours": "A controller's standing approval stops covering calls this long after "
                                             "its controller last confirmed it, until it connects and sends its "
                                             "set again (1 to 720).",
    }),
]
DESCRIPTIONS = {name: text for _, fields in GROUPS for name, text in fields.items()}
# A value to show for settings that are unset by default (the example line must still parse).
UNSET_EXAMPLES: dict[str, Any] = {"lane_memory_max_mb": 1536, "lane_cpu_max_pct": 150}


def config_report(config: NodeConfig, home: Path) -> list[dict[str, Any]]:
    path = config_path(home)
    try:
        in_file = tomllib.loads(path.read_text()) if path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError):
        in_file = {}
    rows = []
    for name, field in type(config).model_fields.items():
        value = getattr(config, name)
        env = ENV_OVERRIDES.get(name)
        if env and os.environ.get(env):
            source = f"env {env}"
            value = os.environ[env] if name != "home" else value
        elif name in in_file:
            default = field.get_default(call_default_factory=True) if not field.is_required() else None
            source = path.name + (" (same as default)" if in_file[name] == default else "")
        else:
            source = "default"
        if name in SECRET_FIELDS:
            value = "(set, hidden)" if value else "(not set)"
        elif isinstance(value, Path):
            value = str(value)
        rows.append({"name": name, "value": value, "source": source,
                     "description": DESCRIPTIONS.get(name) or field.description or ""})
    return rows


def render(rows: list[dict[str, Any]]) -> list[str]:
    width = max(len(r["name"]) for r in rows)
    lines = []
    for r in rows:
        value = r["value"]
        shown = value if isinstance(value, str) else repr(value)
        lines.append(f"{r['name']:<{width}}  {shown:<28}  {r['source']}")
    for env, meaning in EXTRA_ENV.items():
        state = os.environ.get(env)
        lines.append(f"{env}: {state if state else '(not set)'}  ({meaning})")
    return lines


def _toml_value(value: Any) -> str:
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value)  # a TOML basic string has JSON's escapes


def example() -> list[str]:
    """A commented node.toml: every setting, what it means, and its default (commented out, so
    copying the file changes nothing until a line is uncommented)."""
    defaults = NodeConfig()
    lines = ["# agentos node settings (node.toml in AGENTOS_HOME, default /var/lib/agentos).",
             "# Every line is commented out and shows the default. Uncomment a line to change it,",
             "# then restart agentosd. `agentos config show` prints what is in force and why.",
             "#",
             "# Environment variables win over this file:"]
    lines += [f"#   {env}: {name}" for name, env in ENV_OVERRIDES.items()]
    lines += [f"#   {env}: {meaning}" for env, meaning in EXTRA_ENV.items()]
    seen: set[str] = set()
    fields = type(defaults).model_fields
    groups = [*GROUPS, ("Other", {n: "" for n in fields if n not in DESCRIPTIONS})]
    for title, entries in groups:
        names = [n for n in entries if n in fields]
        if not names:
            continue
        lines += ["", f"# ---- {title} ----"]
        for name in names:
            seen.add(name)
            if entries[name]:
                lines.append(f"# {entries[name]}")
            value = getattr(defaults, name)
            if name in SECRET_FIELDS:
                lines.append(f'# {name} = "..."')
            elif value is None:
                shown = UNSET_EXAMPLES.get(name)
                lines.append(f"# {name} = {_toml_value(shown)}   # unset by default; an example value" if shown
                             is not None else f"# {name} is unset by default")
            else:
                lines.append(f"# {name} = {_toml_value(value)}")
    return lines

