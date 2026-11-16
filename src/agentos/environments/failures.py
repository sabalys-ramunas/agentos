"""Why an environment did not come up, in words an agent acts on, and the error environment_open raises.

A bring-up that fails records its cause on the environment (error, error_code and, for
environment_failed, error_reason: capacity, network, image, clone or setup). The record stays in the
history and in environment()'s `failed` list. environment_open raises the error itself, never a
result that looks like an open: the error catalog gives each reason its own fix line, and the words
give a concrete wait where waiting helps.
"""

from __future__ import annotations

from typing import Any

from agentos.core.errors import AgentosError
from agentos.environments.errors import (
    EnvironmentFailed,
    EnvironmentLimit,
    RepoNotConnected,
    RestoreFailed,
)
from agentos.environments.models import SIZES, Environment

NETWORK_WORDS = ("unreachable", "public ipv4", "could not reach")
FAILED_SHOWN = 3
CLASSES: dict[str, type[AgentosError]] = {"environment_limit": EnvironmentLimit, "repo_not_connected": RepoNotConnected,
                                          "restore_failed": RestoreFailed}


def bigger_size(env: Environment, top: str) -> str | None:
    """The next size above the one the environment asked for, up to `top` (max_size)."""
    names = list(SIZES)
    have = env.machine.size if env.machine.size in names else "small"
    i = names.index(have) + 1
    return names[i] if i < len(names) and i <= names.index(top) else None


def classify(e: BaseException, env: Environment, top: str) -> tuple[str | None, str]:
    """(reason, words) for what failed a bring-up. reason None: no known cause."""
    msg = str(getattr(e, "message", e)) or type(e).__name__
    reason = getattr(e, "reason", None)
    if isinstance(e, EnvironmentFailed) and reason:
        return reason, msg.removeprefix(f"{reason}: ")
    code = getattr(e, "provider_code", None)
    bigger = bigger_size(env, top)
    if code == "image_too_big":
        tip = f' Ask for size="{bigger}".' if bigger else ""
        return "image", f"{msg}.{tip}"
    if code in ("resource_unavailable", "placement_error", "unavailable"):
        tip = f', or ask for size="{bigger}" (another server type)' if bigger else ""
        return "capacity", f"{msg}{tip}. Nothing bills: no machine was made"
    if getattr(e, "code", None) == "vm_error" and any(w in msg.lower() for w in NETWORK_WORDS):
        return "network", f"{msg.rstrip('.')}. Try again in 2 minutes" if "try again in" not in msg else msg
    return None, msg


def error_for(env: Environment) -> AgentosError:
    """The error environment_open raises for a failed one, with its id, title and cause."""
    code = env.error_code or "environment_failed"
    if code == "repo_not_connected":  # its words say the machine was deleted, and its fix must fit (repos.MESSAGE_MAX)
        return RepoNotConnected(env.error or code)
    head = f"environment {env.id} did not start: "
    if code == "environment_failed":
        return EnvironmentFailed(head + (env.error or "no reason was given"), reason=env.error_reason)
    return CLASSES.get(code, EnvironmentFailed)(head + (env.error or code))


def failed_rows(mgr: Any, family: str | None, reaches: Any, *, since_s: float = 3600) -> list[dict]:
    """The family's opens that failed in the last hour, newest first, for environment()'s `failed`."""
    now = mgr._clock()
    out = []
    for e in mgr.store.recent(50):
        if e.state != "failed" or (family is not None and e.family != family) or not reaches(e):
            continue
        if (now - e.created_at).total_seconds() > since_s:
            continue
        out.append({"id": e.id, "title": e.title, "error_code": e.error_code, "reason": e.error_reason,
                    "error": (e.error or "")[:300], "at": mgr.summary(e)["created_at"]})
        if len(out) >= FAILED_SHOWN:
            break
    return out
