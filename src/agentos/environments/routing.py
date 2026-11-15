"""Default routing: terminal and browser calls without `on` go to the chat's environment.

The one wrapper every MCP call goes through (agentos.policy.wrap, and agentos.mcp.registry's
allow-all wrapper) asks `route()` before the call: a tool in TERMINAL or BROWSER called without `on`
gets the bound environment's lane (`env/<id>` or `env/<id>-browser`), or fails with no_environment.
A call with `on` is left exactly as it was. Studio's page checks (STUDIO) called without `on` get
the environment's browser proxy, so `ui_check(url="http://localhost:3000")` checks the VM's port
3000. After the call, `after()` counts it on the environment (last call, the recent calls list).

The environment is the one bound to the MCP session (`mcp-session-id`) of the call, else to its grant,
and of the caller's grant family only: a session id another agent sends finds nothing of it.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

log = logging.getLogger("agentos.environments")

SERVICE = "environments"

# tools with no `on` whose handler finds the lane (a new environment's env/<id>, a job's or a shell's
# lane) and authorizes on it: a special held for some lanes only (the `environments` special agentos
# connect gives: env/*) is checked there, not before the handler runs
PICKS_LANE = frozenset({"environment_open", "lane_job_cancel", "shell_write", "shell_close"})

TERMINAL = frozenset({
    "lane_exec", "lane_file_edit", "lane_file_read", "lane_file_write", "lane_install", "lane_job_start",
    "lane_upload", "lane_upload_check", "shell_open",
})  # not lane_jobs_list: without `on` it already lists every run the caller may see (`agentos run --list`)
BROWSER = frozenset({
    "browser_act", "browser_console", "browser_dialog", "browser_dialog_answer", "browser_navigate",
    "browser_network", "browser_screenshot", "browser_scroll", "browser_snapshot", "browser_tab", "browser_tabs",
    "browser_upload", "browser_wait", "files_delete", "files_get", "files_list", "files_put", "lane_claim",
    "lane_close", "lane_control", "lane_open", "lane_queue", "lane_release", "lane_request_control", "lane_view",
    "lane_wait_control",
})
STUDIO = frozenset({"ui_check", "ui_diff", "ui_baseline", "system_check"})

LANE_ARG_TEXT = "Lane id \"workspace/name\"; leave it out for this chat's environment."


def kind_of(tool: str) -> str | None:
    """"terminal" or "browser" for a tool whose `on` may be left out, else None."""
    if tool in TERMINAL:
        return "terminal"
    if tool in BROWSER:
        return "browser"
    return None


def routes(tool: str) -> bool:
    return tool in TERMINAL or tool in BROWSER


def _service(runtime: Any) -> Any:
    get = getattr(runtime, "get", None)
    return get(SERVICE) if callable(get) else None


def binding(session: str | None, grant_id: str | None) -> str:
    from agentos.environments.manager import binding_of

    return binding_of(session, grant_id)


def route(runtime: Any, tool: str, kwargs: dict[str, Any], *, session: str | None, grant_id: str | None,
          family: str | None = None) -> None:
    """Fill `on` for a routed tool called without it (in place). Raises no_environment when the chat
    has none (naming the family's open ones, or the chat that took it over); leaves every other call
    alone. `family`: the caller's grant root (None: the owner with no policy)."""
    kind = kind_of(tool)
    if kind is None or kwargs.get("on"):
        return
    svc = _service(runtime)
    if svc is None:
        from agentos.environments.errors import NoEnvironment

        raise NoEnvironment("this node has no environments, so name the lane with `on`")
    kwargs["on"] = svc.resolve(binding(session, grant_id), kind, family)


def _summary(tool: str, kwargs: dict[str, Any]) -> str:
    for key in ("cmd", "url", "action", "text", "path", "name", "packages"):
        value = kwargs.get(key)
        if isinstance(value, str) and value:
            return value if key in ("cmd", "url") else f"{key} {value}"
        if isinstance(value, list) and value:
            return f"{key} {' '.join(str(v) for v in value)}"
    ref = kwargs.get("ref")
    return f"ref {ref}" if isinstance(ref, str) else ""


def after(runtime: Any, tool: str, lane_id: str | None, kwargs: dict[str, Any], *, ok: bool,
          scrub: list[str] | tuple[str, ...] = (), result: Any = None, error: str | None = None) -> None:
    """Count a call on an environment's lane (any tool, with or without `on`). Its summary is kept
    scrubbed as the audit log's are (`scrub`: exact values to take out too, like the call's token).
    A command's exit code (`result`'s, lane_exec) and a failed call's error code are kept with it."""
    if not isinstance(lane_id, str) or not lane_id.startswith("env/"):
        return
    svc = _service(runtime)
    if svc is None:
        return
    code = result.get("exit_code") if isinstance(result, dict) else None
    try:
        svc.record_call(lane_id, tool, ok=ok, summary=_summary(tool, kwargs), scrub=scrub,
                        exit_code=code if isinstance(code, int) else None,
                        error=error.split(":", 1)[0].strip() if error else None)
    except Exception:
        log.exception("could not record %s on %s", tool, lane_id)


@contextlib.contextmanager
def studio_proxy(runtime: Any, tool: str, kwargs: dict[str, Any], *, session: str | None,
                 grant_id: str | None, family: str | None = None) -> Iterator[str | None]:
    """For a studio check called without `on` in a chat with an environment: its review browser goes
    through the environment's tunnel for this call. Yields the environment's terminal lane (so the
    call is counted on it), or None when the chat has no environment (the check sees what the node
    sees, as before environments). With an environment whose tunnel is not up (starting, closing,
    the node restarted, the connection dropped) it raises environment_not_ready: checking the node's
    own localhost instead would answer about the wrong computer."""
    if tool not in STUDIO or kwargs.get("on"):
        yield None
        return
    svc = _service(runtime)
    env = svc.resolve_or_none(binding(session, grant_id), family) if svc is not None else None
    if env is None:
        yield None
        return
    proxy = svc.live_proxy_for(env) if env.state == "ready" else None
    if proxy is None:
        from agentos.environments.errors import EnvironmentNotReady

        if env.state == "starting":
            why = "is still starting (the machine boots and installs its tools)"
        elif env.state == "closing":
            why = "is closing"
        else:
            why = "cannot be reached right now: its tunnel is down (the node restarted, or the connection dropped)"
        raise EnvironmentNotReady(f"environment {env.id} {why}, so {tool} would check this node instead of it. "
                                  "Call environment() until it says ready (the node starts the tunnel again within "
                                  "a minute), then check again; do not open another environment")
    from agentos.studio.browser.pool import review_proxy

    token = review_proxy.set({"server": proxy.server, "bypass": "<-loopback>"})
    try:
        yield env.terminal_lane
    finally:
        review_proxy.reset(token)
