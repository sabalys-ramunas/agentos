"""A product profile the owner approved, for the brand tasks (the runner saves it as the agent and
approves it as the owner, as a person would in the panel)."""

import json

from studio_eval.tasks import Env

TOKENS = {
    "color": {
        "$type": "color",
        "brand": {"$value": "#2563eb"},
        "ink": {"$value": "#111111"},
        "paper": {"$value": "#ffffff"},
        "night": {"$value": "#111111"},
        "moon": {"$value": "#eeeeee"},
    },
}


async def approved_product(env: Env, ref: str = "personal/shop", *, guidelines: list | None = None) -> str:
    ws, pid = ref.split("/")
    doc = {"profile": pid, "workspace": ws, "kind": "product", "display_name": "Shop",
           "brand": {"tokens": TOKENS}, "environments": [{"name": "dev", "url": env.base.rstrip("/")}]}
    if guidelines:
        doc["guidelines"] = guidelines
    saved = await env.mcp.call("profile_save", {"profile": json.dumps(doc)})
    env.studio.owner("profile", "approve", ref, "--version", str(saved["version"]))
    env.saved["profile"] = ref
    return ref
