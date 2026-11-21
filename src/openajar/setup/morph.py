"""`openajar setup`'s Morph Cloud step: the API key (checked, kept 0600), the SSH gateway's host key
pinned, and provider = "morph" in vms.toml (openajar.vms.morph)."""

from __future__ import annotations

import asyncio
from typing import Any

from openajar.core.errors import OpenajarError

MORPH_HOURLY = "about EUR 0.09 an hour for a small one, estimated"


def morph_check(token: str, api_url: str) -> str | None:
    """None when Morph takes the key, else what is wrong, in words."""
    from openajar.vms.morph import MorphProvider

    try:
        asyncio.run(MorphProvider(token, api_url=api_url).check())
    except OpenajarError as e:
        return e.message
    return None


def use_morph(ctx: Any, settings: Any) -> None:
    """Ask for the key (three tries), keep it, pin the gateway, and make Morph the provider."""
    from openajar.core.config import write_private
    from openajar.vms.cli import use_provider
    from openajar.vms.models import MORPH_TOKEN_FILE
    from openajar.vms.morph import pin_gateway

    ui, home = ctx.ui, ctx.home
    path = home / MORPH_TOKEN_FILE
    token = None
    if path.exists() and morph_check(path.read_text().strip(), settings.morph_api_url) is None:
        ui.say("The saved Morph API key works.")
    else:
        ui.say("Make an API key at cloud.morph.so (API keys). openajar rents instances in that account.")
        for _ in range(3):
            token = ui.secret("Paste the Morph API key (hidden; Enter to skip)")
            if not token:
                break
            problem = morph_check(token, settings.morph_api_url)
            if problem is None:
                break
            ui.say(f"That key did not work: {problem}")
            token = None
        if not token:
            ui.say("No Morph key saved. Run `openajar setup vms` when you have one.")
            ctx.todo.append("VMs")
            return
        write_private(path, token + "\n")
        ui.say(f"The key works. It is saved in {path}, and only you can read it.")
    try:
        pinned = pin_gateway(home, settings.morph_ssh_host)
    except OpenajarError as e:
        ui.say(f"The host key of Morph's SSH gateway could not be read: {e.message}")
        ctx.todo.append("VMs")
        return
    ui.say(f"Pinned the host key of {settings.morph_ssh_host}: {pinned[0] if pinned else 'read'}.")
    use_provider(home, "morph")
    ui.say("Run `openajar vm image build dev` next: environments then start in seconds, and saves pause with "
           "their memory.")
    ctx.vms = f"Morph Cloud ({MORPH_HOURLY})"
