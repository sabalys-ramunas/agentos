"""Default routing: terminal and browser calls go to the caller's environment, only when it holds it.

The one wrapper every MCP call goes through (agentos.policy.wrap, and agentos.mcp.registry's
allow-all wrapper) asks `route()` before the call. `on` may name an environment instead of a lane:
its handle (the `on` environment_open gave), or, for a client whose MCP session holds it, its id or
env/<id>. route() then puts the lane for this tool's kind in `on` (`env/<id>` or `env/<id>-browser`).
A title, or the id of a machine another chat holds, never reaches a live one (which_environment);
a handle that is not the machine's any more is no_environment. A tool in TERMINAL or BROWSER called
without `on` goes to the session's own machine (a client that sends `mcp-session-id`); a client that
sends none (MCP 2026-07-28, stdio) is refused with which_environment while its agent has a machine
open, since another chat's could be the one picked. Any other `on` (an account lane,
"workspace/name") is left exactly as it was. `route()` returns the environment it chose, and the
wrapper adds `environment: {id, title}` to the call's result. Studio's page checks (STUDIO) get the
environment's browser proxy, so `ui_check(url="http://localhost:3000", on=...)` checks the VM's
port 3000. After the call, `after()` counts it on the environment (last call, the recent calls
list, the MCP client that made it).
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
PICKS_LANE = frozenset({"environment_open", "terminal_job_cancel", "shell_write", "shell_close"})

TERMINAL = frozenset({
    "terminal_exec", "file_edit", "file_read", "file_write", "terminal_install", "terminal_job_start",
    "terminal_upload", "terminal_upload_check", "shell_open", "environment_upload",
    "environment_download",
})  # not terminal_jobs: without `on` it already lists every run the caller may see (`agentos run --list`)
BROWSER = frozenset({
    "browser_act", "browser_console", "browser_dialog", "browser_navigate",
    "browser_network", "browser_record", "browser_screenshot", "browser_scroll", "browser_snapshot", "browser_tabs",
    "browser_upload", "browser_wait", "browser_file_delete", "browser_file_get", "browser_files", "browser_file_put", "browser_claim",
    "browser_close", "browser_control", "browser_open", "browser_queue", "browser_release", "browser_request_control", "browser_view",
    "browser_wait_handback", "login", "vault_fill",
})  # login and vault_fill: the vault types a login the owner bound to the environment (agentos.environments.logins)
STUDIO = frozenset({"ui_check", "ui_diff", "ui_baseline", "system_check"})

# tools that are not routed without `on` (terminal_jobs lists every run the caller may see; a job id
# finds its own lane), whose `on` may still name an environment: its handle becomes its terminal lane,
# and the lease is checked. The job tools take `on` because agents are told to pass it on every call
# (round 3, T2: "terminal_job_read has no argument 'on'"); the handler checks it is the job's lane.
ON_ONLY = frozenset({"terminal_jobs", "terminal_job_read", "terminal_job_cancel"})

# what `on` says on a routed tool: short, since it is on about 25 tools an agent reads every turn. The default
# list's own tools say only where it comes from (the intro and the agentos-lanes skill say an account browser's
# `on` is its workspace/name); the account browsers' tools, outside it, name both
ON_TEXT = {"terminal": "From environment_open, or a terminal's workspace/name.",
           "browser": "From environment_open, or an account browser's workspace/name."}
ON_TEXT_ENV = "From environment_open."


def on_text(tool: str) -> str:
    """The description of a routed tool's `on`: the default list's and an environment's own tools never
    offer an account browser."""
    from agentos.core.toolsets import CORE_TOOLS

    if tool.startswith("environment_") or tool in STUDIO or tool in CORE_TOOLS:
        return ON_TEXT_ENV
    return ON_TEXT.get(kind_of(tool) or "terminal", ON_TEXT["terminal"])


def kind_of(tool: str) -> str | None:
    """"terminal" or "browser" for a tool whose `on` may be left out, else None."""
    if tool in TERMINAL:
        return "terminal"
    if tool in BROWSER:
        return "browser"
    return None


def routes(tool: str) -> bool:
    return tool in TERMINAL or tool in BROWSER or tool in ON_ONLY


def _service(runtime: Any) -> Any:
    get = getattr(runtime, "get", None)
    return get(SERVICE) if callable(get) else None


def wants_route(on: Any, tool: str | None = None) -> bool:
    """Does route() decide this `on`? None (the session's environment; not for an ON_ONLY tool), a
    handle, a title or an id (no "/": never a lane), or an environment's lane env/<id>; an account
    lane ("workspace/name") is left as it is."""
    if not on:
        return tool not in ON_ONLY
    return isinstance(on, str) and ("/" not in on or on.startswith("env/"))


def is_handle(on: Any) -> bool:
    """Is `on` shaped as an environment's handle (agentos.environments.picking), a secret the caller
    holds? Traces and logs scrub it."""
    from agentos.environments.picking import handle_env

    return isinstance(on, str) and handle_env(on) is not None


def env_ref(env: Any) -> dict[str, str]:
    """What a result carries about the environment the call went to."""
    return {"id": env.id, "title": env.title}


def with_env(result: Any, env: Any) -> Any:
    """`result` with `environment: {id, title}` when route() chose `env` (a dict result only)."""
    if env is None or not isinstance(result, dict) or "environment" in result:
        return result
    return {**result, "environment": env_ref(env)}


# a command's result: the hosts the environment's network refused while it ran (terminal_job_read: since its job began)
NETWORK_TOOLS = frozenset({"terminal_exec", "terminal_job_read"})


def now(runtime: Any) -> Any:
    """The environments' clock now, for finish()'s `since` (None on a node without environments)."""
    clock = getattr(_service(runtime), "_clock", None)
    return clock() if callable(clock) else None


def finish(runtime: Any, tool: str, result: Any, env: Any, since: Any = None) -> Any:
    """with_env, and on a command's result `network.blocked_now` when the environment's proxy refused a
    host while it ran: curl over HTTPS shows only "CONNECT tunnel failed, response 403", so the result
    names the host and the owner's command (evals, 2026-11-18)."""
    result = with_env(result, env)
    if tool not in NETWORK_TOOLS or not isinstance(result, dict) or "network" in result:
        return result
    svc = _service(runtime)
    blocked_since = getattr(svc, "blocked_since", None)
    if not callable(blocked_since):
        return result
    try:
        if env is None:  # a job read without `on`: the job's own lane
            found = svc.env_of_lane(result.get("lane"))
            env = found[0] if found else None
        if tool == "terminal_job_read":
            from datetime import datetime

            since = datetime.fromisoformat(str(result.get("started_at")))
        hosts = blocked_since(env, since) if env is not None else []
    except (TypeError, ValueError, AttributeError):
        return result
    if not hosts:
        return result
    return {**result, "network": {"blocked_now": hosts[:5], "fix": (
        f"refused: not on this environment's network list. Do not retry; the owner allows a host with "
        f"agentos environments allow --host {hosts[0]}")}}


def binding(session: str | None, grant_id: str | None) -> str:
    from agentos.environments.manager import binding_of

    return binding_of(session, grant_id)


def route(runtime: Any, tool: str, kwargs: dict[str, Any], *, session: str | None, grant_id: str | None,
          family: str | None = None) -> Any:
    """Fill `on` for a routed tool (in place) and return the environment it goes to, else None. No
    `on`: the session's own (no_environment with none; which_environment for a client with no
    session while its agent has a machine open). `on` naming an environment the caller holds (its
    handle; its id or env/<id> for the session that holds it): its lane for this tool's kind. A
    title or the id of a live machine: which_environment. An account lane, or env/<x> naming none
    of the family's (the policy refuses it as not found): left alone. `family`: the caller's grant
    root (None: the owner with no policy)."""
    from agentos.environments.errors import NoEnvironment

    on = kwargs.get("on")
    if not routes(tool) or not wants_route(on, tool):
        return None
    kind = kind_of(tool) or "terminal"
    svc = _service(runtime)
    if svc is None:
        if on:
            return None
        raise NoEnvironment("this node has no environments, so name the terminal or browser with `on`")
    if not callable(getattr(svc, "pick", None)):  # a service that only resolves lanes (a test's stand-in)
        if not on:
            kwargs["on"] = svc.resolve(binding(session, grant_id), kind, family)
        return None
    if on:
        env = svc.reach(on, binding(session, grant_id), family)
        if env is None:
            if "/" in on:
                return None
            raise NoEnvironment(f"no open environment of yours is named \"{on[:80]}\". on takes the on your "
                                "environment_open gave, or an account browser \"workspace/name\"; environment() lists "
                                "your agent's machines")
        if env.state == "failed":
            from agentos.environments.errors import EnvironmentFailed

            raise EnvironmentFailed(f"environment {env.id} (\"{env.title}\") did not start and its VM is deleted: "
                                    f"{env.error or 'no reason was kept'}. Nothing ran. environment_open(title=...) "
                                    "opens a new one")
        if not env.open:  # saved since, perhaps: when, and how to come back (agentos.environments.comeback)
            from agentos.environments.comeback import closed_words

            raise NoEnvironment(closed_words(svc, env))
    else:
        env = svc.pick(binding(session, grant_id), family, kind)
    if env.state == "starting":
        clock = getattr(svc, "_clock", None)
        not_yet(env, clock() if callable(clock) else None)
    kwargs["on"] = env.browser_lane if kind == "browser" else env.terminal_lane
    return env


def not_yet(env: Any, now: Any = None) -> None:
    """A call to a machine that is still starting runs nothing: it could race the bring-up (a git
    command while the repository is still being cloned breaks the clone) or find half a machine.
    environment_not_ready says what the bring-up does now and how long it has taken."""
    from agentos.core.models import utcnow
    from agentos.environments.bringup import starting_words
    from agentos.environments.errors import EnvironmentNotReady

    raise EnvironmentNotReady(f"environment {env.id} is {starting_words(env, now or utcnow())}. Nothing ran: a call "
                              "now could race its setup (a git command while the repository is cloned breaks the "
                              "clone). Wait with environment(on=\"<your on>\"): it waits up to a minute and says "
                              "ready when it is; do not open another one")


def _summary(tool: str, kwargs: dict[str, Any]) -> str:
    action, ref = kwargs.get("action"), kwargs.get("ref")
    if isinstance(action, str) and action and isinstance(ref, str) and ref:  # browser_act: "click e12"
        return f"{action} {ref}"
    if tool == "browser_tabs" and isinstance(kwargs.get("select"), int):
        return f"tab {kwargs['select']}"
    if tool == "browser_dialog" and isinstance(kwargs.get("accept"), bool):
        return "accept" if kwargs["accept"] else "dismiss"
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
    A command's exit code (`result`'s, terminal_exec) and a failed call's error code are kept with it."""
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


def _local_url(url: Any) -> bool:
    """Is it a page on the machine itself (localhost, 127.0.0.1, [::1])?"""
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost")


@contextlib.contextmanager
def studio_proxy(runtime: Any, tool: str, kwargs: dict[str, Any], *, session: str | None,
                 grant_id: str | None, family: str | None = None) -> Iterator[str | None]:
    """For a studio check of an environment's page: its review browser goes through the
    environment's tunnel for this call. `on` names the environment (its handle); without it, a
    session's own. A client with no session and no `on` checks what the node sees, and a localhost
    url then is which_environment (it would be the node's, not the machine's). Yields the
    environment's terminal lane (so the call is counted on it), or None when no environment is meant.
    With an environment whose tunnel is not up (starting, closing, the node restarted, the connection
    dropped) it raises environment_not_ready: checking the node's own localhost instead would answer
    about the wrong computer."""
    if tool not in STUDIO:
        yield None
        return
    svc = _service(runtime)
    from agentos.environments.errors import NoEnvironment, WhichEnvironment

    b = binding(session, grant_id)
    on = kwargs.get("on")
    env = None
    if svc is not None and on:
        env = svc.reach(on, b, family)
        if env is None or not env.open:
            raise NoEnvironment(f"on=\"{str(on)[:80]}\" names no open environment of yours; {tool} takes the on "
                                "your environment_open gave")
    elif svc is not None:
        try:
            env = svc.resolve_or_none(b, family)
        except WhichEnvironment:
            if _local_url(kwargs.get("url")):
                raise WhichEnvironment(f"{tool} of a localhost page checks your environment's machine only with "
                                       "its on: pass the on your environment_open gave. This client sends no "
                                       "session id, so agentos cannot tell which machine is yours") from None
            env = None
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
                                  "Call environment(on=...) until it says ready (the node starts the tunnel again within "
                                  "a minute), then check again; do not open another environment")
    from agentos.studio.browser.pool import review_proxy

    token = review_proxy.set({"server": proxy.server, "bypass": "<-loopback>"})
    try:
        yield env.terminal_lane
    finally:
        review_proxy.reset(token)
