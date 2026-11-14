"""`agentos lane take | handback | stop | deny | login | view`: control lives in the running
daemon, so these call its HTTP API with the node token."""

from __future__ import annotations

from typing import Annotated

import typer

from agentos.commands.base import LaneIdArg, daemon_request, fail, initialized_config, lane_api_path
from agentos.commands.lanes import lane_app
from agentos.core.config import (
    NodeConfig,
)


def control_action(lane_id: str, action: str, note: str | None = None) -> tuple[dict, bool, NodeConfig]:
    cfg = initialized_config()
    body = {"action": action, **({"note": note} if note else {})}
    # a person's (a one-time code in panel.db beside the token): hand back and taking a held lane need one
    lane = daemon_request(cfg, "POST", lane_api_path(lane_id, "control"), body, person=True,
                          missing="this agentosd has no lane control API yet; update it and restart the service")
    return lane, bool(lane.get("changed", True)), cfg


@lane_app.command("take")
def lane_take(lane_id: LaneIdArg) -> None:
    """Take control of a lane: agents stop acting on it until you hand it back."""
    _, changed, cfg = control_action(lane_id, "take")
    if not changed:
        typer.echo(f"You already have control of {lane_id}.")
        return
    minutes = max(1, round(cfg.control_idle_seconds / 60))
    typer.echo(f"You have control of {lane_id}. Agents on it now get human_in_control.")
    typer.echo(f"Watch and drive it with `agentos lane view {lane_id} --control`. Hand it back with "
               f"`agentos lane handback {lane_id}`.")
    if cfg.control_idle_seconds > 0:
        typer.echo(f"Control goes back to agents after {minutes} min without input in the viewer.")


@lane_app.command("handback")
def lane_handback(
    lane_id: LaneIdArg,
    note: Annotated[str | None, typer.Option(
        "--note", help="What to tell the agents, for example \"I fixed the address; post it now\" "
                       "(at most 300 characters). They see it once, with what changed.")] = None,
) -> None:
    """Hand a lane back to agents (also resumes a stopped agent and clears a pending request). The
    next thing an agent sees on the lane says what changed while you had it."""
    if note is not None and len(note) > 300:
        raise fail("--note is at most 300 characters")
    lane, changed, _ = control_action(lane_id, "hand_back", note)
    if lane.get("warning"):
        typer.echo(lane["warning"])
        return
    typer.echo(f"Handed {lane_id} back to agents." if changed else f"Agents already have control of {lane_id}.")


@lane_app.command("stop")
def lane_stop(lane_id: LaneIdArg) -> None:
    """Stop the agent on a lane: its runs there are cancelled and nothing acts until you hand it back."""
    _, changed, _ = control_action(lane_id, "stop_agent")
    if changed:
        typer.echo(f"Stopped agents on {lane_id}. Nothing runs there until you hand it back with "
                   f"`agentos lane handback {lane_id}`.")
    else:
        typer.echo(f"Agents on {lane_id} are already stopped.")


@lane_app.command("deny")
def lane_deny(lane_id: LaneIdArg) -> None:
    """Refuse an agent's request for control back. You keep control."""
    _, changed, _ = control_action(lane_id, "deny_request")
    typer.echo(f"Refused the agent's request for {lane_id}. You keep control." if changed
               else f"No agent is asking for {lane_id} right now.")


LOGIN_TIMEOUT = 120.0   # a login step types, presses and waits for the page (agentos.node.login_assist)


LOGIN_ENDED = ("signed_in", "cancelled", "done", "failed")


def _login_call(cfg: NodeConfig, lane_id: str, action: str = "", body: dict | None = None) -> dict:
    path = lane_api_path(lane_id, "login") + (f"/{action}" if action else "")
    return daemon_request(cfg, "POST", path, body or {}, timeout=LOGIN_TIMEOUT, person=True,  # a person's
                          missing="this agentosd has no assisted login yet; update it and restart the service")


def _login_show(login: dict, shown: dict) -> None:
    """Print what changed since the last print: the step's words, the note, an error."""
    step = login.get("step") or {}
    key = (step.get("id"), login.get("state"))
    if key != shown.get("key"):
        shown["key"] = key
        for text in (step.get("heading"), step.get("message")):
            if text:
                typer.echo(text)
        if step.get("number"):
            typer.secho(f"\n    {step['number']}\n", bold=True)
        if login.get("note"):
            typer.secho(login["note"], fg=typer.colors.CYAN)
    if login.get("error") and login.get("error") != shown.get("error"):
        typer.secho(login["error"], fg=typer.colors.YELLOW, err=True)
    shown["error"] = login.get("error")


def _login_ask(cfg: NodeConfig, lane_id: str, login: dict) -> dict:
    """The step's fields in the terminal (hidden input for secrets), or its buttons as a numbered list."""
    step = login["step"]
    choices = step.get("choices") or []
    if step.get("fields"):
        if choices:
            typer.echo("Other ways to sign in: " + ", ".join(f"[{i}] {c['label']}" for i, c in enumerate(choices, 1)))
            raw = typer.prompt("Press Enter to fill in the form, or pick a number", default="",
                               show_default=False).strip()
            if raw.isdigit() and 1 <= int(raw) <= len(choices):
                return _login_call(cfg, lane_id, "step", {"step": step["id"], "choice": choices[int(raw) - 1]["id"]})
        values: dict[str, str] = {}
        for f in step["fields"]:
            values[f["name"]] = typer.prompt(f["label"], hide_input=bool(f.get("secret")))
        try:
            return _login_call(cfg, lane_id, "step", {"step": step["id"], "values": values})
        finally:
            values.clear()
    for i, c in enumerate(choices, 1):
        typer.echo(f"  [{i}] {c['label']}")
    while True:
        raw = typer.prompt("Pick one (or t to take control, c to cancel)").strip().lower()
        if raw == "c":
            return _login_call(cfg, lane_id, "cancel")
        if raw == "t":
            return _login_call(cfg, lane_id, "take")
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return _login_call(cfg, lane_id, "step", {"step": step["id"], "choice": choices[int(raw) - 1]["id"]})


@lane_app.command("login")
def lane_login(
    lane_id: LaneIdArg,
    url: Annotated[str | None, typer.Option(help="The sign-in page to open. Default: the site's own login page "
                                                 "from the sites table, or the page the lane shows.")] = None,
) -> None:
    """Log a lane in by typing into the terminal: agentos reads each sign-in step, asks you for exactly
    its fields (passwords and codes hidden), and types them into the lane's browser at a person's pace.
    Agents stop on the lane while it runs. Nothing you type is logged or shown to an agent."""
    cfg = initialized_config()
    login = _login_call(cfg, lane_id, "", {"url": url} if url else {})
    shown: dict = {}
    try:
        while True:
            _login_show(login, shown)
            state = login.get("state")
            if state in LOGIN_ENDED:
                break
            if state == "ask":
                login = _login_ask(cfg, lane_id, login)
            elif state == "stopped" or state == "paused":
                typer.echo(f"Take control in the live view (`agentos lane view {lane_id} --control`), then come back.")
                raw = typer.prompt("Press Enter to read the page again, or c to cancel", default="",
                                   show_default=False).strip().lower()
                login = _login_call(cfg, lane_id, "cancel" if raw == "c" else "resume")
            else:  # working, waiting: the node answers when the login changes
                path = lane_api_path(lane_id, "login") + f"?after={login.get('version', 0)}&wait=25"
                login = daemon_request(cfg, "GET", path, timeout=40.0, person=True)
    except (KeyboardInterrupt, typer.Abort):
        typer.echo("")
        login = _login_call(cfg, lane_id, "cancel")
        typer.echo(f"Login on {lane_id} cancelled. Nothing you typed was kept.")
        raise typer.Exit(130) from None
    if login.get("state") == "signed_in" and login.get("can_save"):
        if typer.confirm("Save these to the vault for next time? (the login tool then signs in with no form)",
                         default=True):
            saved = _login_call(cfg, lane_id, "save")
            typer.echo(saved.get("note") or "Saved.")
        else:
            _login_call(cfg, lane_id, "cancel")


@lane_app.command("view")
def lane_view_link(
    lane_id: LaneIdArg,
    control: Annotated[bool, typer.Option("--control", help="A link that can take control, not only watch.")] = False,
    ttl: Annotated[int | None, typer.Option(help="Link lifetime in seconds (default view_token_seconds).",
                                            min=30, max=86400)] = None,
) -> None:
    """Print a signed live-view link for a lane and the SSH tunnel that reaches it."""
    from agentos.node.daemon import ssh_host

    cfg = initialized_config()
    body: dict = {"mode": "control" if control else "view"}
    if ttl is not None:
        body["ttl_s"] = ttl
    link = daemon_request(cfg, "POST", lane_api_path(lane_id, "view-link"), body, person=True,  # the owner's own link
                          missing="this agentosd has no lane control API yet; update it and restart the service")
    port = cfg.mcp_port
    kind = "control link" if control else "view-only link"
    typer.echo(f"Live view of {lane_id} ({kind}, valid until {link.get('expires_at', '?')}):")
    typer.echo(f"  http://localhost:{port}{link.get('url', '')}")
    typer.echo("From your Mac, keep this tunnel open while you use it:")
    typer.echo(f"  ssh -N -L {port}:localhost:{port} {ssh_host(cfg)}")
