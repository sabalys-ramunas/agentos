"""The bring-up's steps and their seconds, for an environment's detail (`bringup`).

bringup.Steps measures each step as it ends: {step, at_s, seconds, ok}, `at_s` seconds after the
bring-up began, so steps that ran at once overlap. The record is kept on the environment when it
becomes ready and when it fails (Environment.bringup); while it starts, the detail reads the bring-up
running here (LIVE) and adds the steps in progress. The view also names the work the open did not
need: a prepared machine's setup (`skipped: "prepared machine"`, with the seconds its build's setup
took less the refresh here, `saved_s`) and a warm machine's rent and boot (`skipped: "warm machine"`,
their measured seconds kept: the claim and a check still ran). A resume says what it woke from.
"""

from __future__ import annotations

from typing import Any

# environment id -> its bringup.Steps while the bring-up runs on this node
LIVE: dict[str, Any] = {}

WARM_STEPS = ("VM create", "VM ready")
RESTORE_FROM = ("snapshot", "paused", "files")


def record(steps: Any, *, ok: bool) -> dict:
    """What Environment.bringup keeps: {total_s, ok, steps}."""
    return {"total_s": round(steps.total(), 1), "ok": ok, "steps": [dict(s) for s in steps.done]}


def ended(env_id: str, steps: Any) -> None:
    if LIVE.get(env_id) is steps:
        LIVE.pop(env_id, None)


def view(mgr: Any, env: Any) -> dict | None:
    """The detail's `bringup`: {total_s, ok, steps, running, restore_from?}, or None when there is no
    record (an environment from before steps were kept, or one starting on another process). While it
    starts: the steps done so far, `running`, `total_s` so far and `ok` null."""
    live = LIVE.get(env.id)
    if live is not None and env.state == "starting":
        out: dict[str, Any] = {"total_s": round(live.total(), 1), "ok": None, "steps": [dict(s) for s in live.done],
                               "running": live.running()}
    elif env.bringup:
        kept = env.bringup
        out = {"total_s": kept.get("total_s"), "ok": kept.get("ok"),
               "steps": [dict(s) for s in kept.get("steps") or []], "running": []}
    else:
        return None
    _skipped(mgr, env, out["steps"])
    out["steps"].sort(key=lambda s: (s.get("at_s") or 0.0, s.get("step") or ""))
    source = (env.resumed or {}).get("from")
    if source in RESTORE_FROM:
        out["restore_from"] = source
    return out


def _skipped(mgr: Any, env: Any, steps: list[dict]) -> None:
    by = {s.get("step"): s for s in steps}
    if getattr(env, "warm_claimed", False):
        for name in WARM_STEPS:
            s = by.get(name)
            if s is None:
                s = {"step": name, "at_s": 0.0, "seconds": 0.0, "ok": True}
                steps.append(s)
            s["skipped"] = "warm machine"
    p = env.prepared or {}
    anchor = by.get("refresh") or by.get("checkout")
    if not p.get("image") or "setup" in by or anchor is None:
        return  # no prepared machine was booted, or its checkout did not happen (yet)
    steps.append({"step": "setup", "at_s": round((anchor.get("at_s") or 0.0) + (anchor.get("seconds") or 0.0), 1),
                  "seconds": 0.0, "ok": True, "skipped": "prepared machine", "saved_s": _saved_s(mgr, env)})


def _saved_s(mgr: Any, env: Any) -> float | None:
    """The setup seconds a prepared machine saved: as environment()'s prepared.setup_s.saved, else the
    build's own setup seconds."""
    try:
        saved = ((mgr.prepared_view(env).get("prepared") or {}).get("setup_s") or {}).get("saved")
    except Exception:  # noqa: BLE001 - the build's seconds instead
        saved = None
    if saved is None:
        saved = ((env.prepared or {}).get("built") or {}).get("setup_s")
    return round(float(saved), 1) if saved is not None else None
