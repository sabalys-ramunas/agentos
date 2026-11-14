"""Lane and workspace shapes for the panel, including the one-line health summary."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from agentos.api.jsonable import jsonable
from agentos.core.models import LaneInfo, LaneKind, parse_lane_id
from agentos.core.signals import ATTENTION, NEEDS_HUMAN

NEEDS_HUMAN_SIGNALS = NEEDS_HUMAN
ATTENTION_SIGNALS = ATTENTION
SIGNAL_WORDS = {
    "captcha": ("captcha", "captchas"),
    "checkpoint": ("security check", "security checks"),
    "rate_limited": ("rate limited", "rate limited"),
    "logged_out": ("logged out", "logged out"),
    "blocked": ("blocked", "blocked"),
}
DEFAULT_PRESET = {
    LaneKind.BROWSER: "account", LaneKind.ANDROID: "account",
    LaneKind.TERMINAL: "workbench", LaneKind.SERVICE: "service",
}
PALETTE = ("#4f7cff", "#16a085", "#d35400", "#8e44ad", "#c0392b", "#2c7a7b", "#b7791f", "#5a67d8")


def default_color(workspace_id: str) -> str:
    return PALETTE[sum(workspace_id.encode()) % len(PALETTE)]


def title_from_id(workspace_id: str) -> str:
    return " ".join(p.capitalize() for p in workspace_id.split("-"))


def lane_ws_path(lane_id: str, suffix: str) -> str:
    ws, name = parse_lane_id(lane_id)
    return f"/api/lanes/{quote(ws)}/{quote(name)}/{suffix}"


def lane_json(info: LaneInfo | dict, *, hints: dict | None, proxies: dict[str, Any], node: str,
              frozen: bool | None = None) -> dict:
    """Lane as in docs/API.md. Proxies show label, host and port only. `frozen`: whether a
    break-glass freeze covers the lane (None: no freeze service on this node)."""
    if isinstance(info, dict):
        info = LaneInfo.model_validate(info)
    spec = info.spec
    hints = hints or {}
    proxy = None
    if spec.proxy_label:
        p = proxies.get(spec.proxy_label)
        proxy = (p.public() if hasattr(p, "public") else {k: p.get(k) for k in ("label", "host", "port")}) if p \
            else {"label": spec.proxy_label, "host": None, "port": None}
    state = info.state.value
    out = {
        "id": spec.id,
        "workspace": spec.workspace,
        "name": spec.name,
        "kind": spec.kind.value,
        "engine": spec.engine,
        "preset": hints.get("preset") or DEFAULT_PRESET.get(spec.kind, "account"),
        "state": state,
        # the lane manager records the last signal a tool saw; hints (from fakes or extensions) win
        "signal": hints.get("signal") or (getattr(info.signal, "value", info.signal) if info.signal else "unknown"),
        "hold": info.hold,                         # why agents are held off (a bad signal, quarantine)
        "hold_until": jsonable(info.hold_until),   # when a self-clearing hold ends; None: a human clears it
        "proxy": proxy,
        "device": spec.device,                     # the device it runs on (AUD-02); None: its own
        "exit_ip": info.exit_ip,
        "last_used": jsonable(info.last_used),
        "node": hints.get("node") or node,
        "quarantine_reason": info.quarantine_reason,
        "logged_in_hint": hints.get("logged_in_hint") or "unknown",
        # who drives the lane right now (agent, human, paused), for the panel's status glyphs
        "control": {"state": getattr(info.control.state, "value", info.control.state), "by": info.control.by,
                    "since": jsonable(info.control.since),
                    "takeback_requested": info.control.takeback_requested,
                    "takeback_reason": info.control.takeback_reason,
                    "takeback_by": info.control.takeback_by,
                    "last_human_input": jsonable(info.control.last_human_input)},
        "frozen": frozen,
        # a claim for a multi-step job (holder, purpose, since, expires_in_s) and callers waiting
        "claim": info.claim,
        "queue_length": info.queue_length,
        "viewer": None,
    }
    if state in ("warm", "active") and (info.viewer is not None or spec.kind == LaneKind.BROWSER):
        out["viewer"] = {
            "ws_path": lane_ws_path(spec.id, "vnc"),
            "display": info.viewer.display if info.viewer else None,
        }
    if spec.kind == LaneKind.TERMINAL:
        out["terminal"] = {"ws_path": lane_ws_path(spec.id, "terminal")}
    return out


def needs_human(lane: dict) -> bool:
    return lane["state"] == "quarantined" or lane["signal"] in NEEDS_HUMAN_SIGNALS


def workspace_health(lanes: list[dict], pending_approvals: int = 0) -> tuple[str, str]:
    """(health, summary). Summaries read as one calm line: "6 lanes, all ok"."""
    n = len(lanes)
    if n == 0:
        return "idle", "No lanes yet"
    count = f"{n} lane" if n == 1 else f"{n} lanes"
    quarantined = sum(1 for lane in lanes if lane["state"] == "quarantined")
    by_signal: dict[str, int] = {}
    for lane in lanes:
        if lane["signal"] in SIGNAL_WORDS:
            by_signal[lane["signal"]] = by_signal.get(lane["signal"], 0) + 1
    problems = []
    if quarantined:
        problems.append(f"{quarantined} quarantined")
    for sig in ("blocked", "checkpoint", "logged_out", "captcha", "rate_limited"):
        k = by_signal.get(sig, 0)
        if k:
            one, many = SIGNAL_WORDS[sig]
            problems.append(f"{k} {one if k == 1 else many}")
    if pending_approvals:
        problems.append(f"{pending_approvals} waiting for approval")

    if quarantined or by_signal.get("blocked"):
        health = "blocked"
    elif any(by_signal.get(s) for s in ATTENTION_SIGNALS) or pending_approvals:
        health = "attention"
    elif all(lane["state"] == "cold" for lane in lanes):
        health = "idle"
    else:
        health = "ok"

    if problems:
        return health, f"{count}, " + ", ".join(problems)
    if health == "idle":
        return health, f"{count}, sleeping"
    warm = sum(1 for lane in lanes if lane["state"] in ("warm", "active", "warming"))
    return health, f"{count}, all ok" + (f", {warm} open" if warm else "")


# "for each company: these 6 surfaces"
TEMPLATES: dict[str, dict] = {
    "social-6": {
        "id": "social-6",
        "name": "Social, 6 surfaces",
        "description": "Instagram, Facebook, Reddit, Medium, a blog and LinkedIn for one company.",
        "lanes": [
            {"name": n, "kind": "browser", "preset": "account"}
            for n in ("instagram", "facebook", "reddit", "medium", "blog", "linkedin")
        ],
    },
    "personal-4": {
        "id": "personal-4",
        "name": "Personal, 4 surfaces",
        "description": "Facebook, Instagram, Reddit and LinkedIn for one person.",
        "lanes": [
            {"name": n, "kind": "browser", "preset": "account"}
            for n in ("facebook", "instagram", "reddit", "linkedin")
        ],
    },
    "workbench": {
        "id": "workbench",
        "name": "Workbench",
        "description": "One trusted terminal lane.",
        "lanes": [{"name": "shell", "kind": "terminal", "preset": "workbench"}],
    },
}
