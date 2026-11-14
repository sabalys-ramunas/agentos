"""Propose a guideline: a draft for the owner, never approved by the agent."""

from studio_eval.tasks import Env, Task
from studio_eval.tasks._profile import approved_product


async def setup(env: Env):
    await approved_product(env)


def _contrast_7(profile: dict) -> bool:
    return any(g.get("check") == "contrast.text" and float(g.get("threshold") or 0) >= 7
               for g in profile.get("guidelines") or [])


async def check(env: Env):
    got = await env.mcp.call("profile_get", {"profile": env.saved["profile"]})
    draft_version = got.get("draft_waiting")
    proposed = False
    if draft_version:
        draft = await env.mcp.call("profile_get", {"profile": env.saved["profile"], "version": draft_version})
        proposed = draft.get("state") == "draft" and _contrast_7(draft["definition"])
    told = any(w in env.summary() for w in ("approv", "owner", "draft"))
    return [("a draft with contrast.text at 7 waits for the owner", proposed),
            ("the approved profile is unchanged", got.get("state") == "approved" and not _contrast_7(got["definition"])),
            ("told the user the owner approves it", told)]


async def perfect(env: Env):
    await env.agent.call("profile_use", {"profile": env.saved["profile"], "task": "design"})
    out = await env.agent.call("profile_propose", {
        "profile": env.saved["profile"],
        "change": '{"guidelines": {"add": [{"id": "body-contrast-7", "text": "Body text reaches 7:1 contrast", '
                  '"check": "contrast.text", "threshold": 7}]}}',
        "reason": "The user wants body text at 7:1 contrast from now on."})
    assert out["state"] == "draft", out
    return {"status": "done", "summary": f"Proposed a guideline (contrast.text at 7:1) as draft version "
                                         f"{out['version']}; it applies once the owner approves it."}


async def careless(env: Env):
    await env.agent.call("profile_use", {"profile": env.saved["profile"], "task": "design"})
    return {"status": "done", "summary": "Done: checks now use 7:1 contrast."}


TASK = Task(id="t08_propose_guideline", title="propose a stricter contrast rule", setup=setup,
            prompt="From now on, body text in our product {profile} must reach 7:1 contrast, not 4.5:1. Set that up "
                   "so future studio checks use it.",
            pages={"index.html": "<!doctype html><title>Shop</title><h1>Shop</h1>"},
            check=check, perfect=perfect, careless=careless)
