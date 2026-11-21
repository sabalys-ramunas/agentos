"""Plain words for what happened, shared by Needs you and History.

Everything here is worked out from fields the node really has: a run's status, its error, the account
(`ws/name`), the approval's effect and the requester's id. Nothing is guessed from free text beyond the
few failure phrases below, and a failure nobody can name is said as "Something went wrong on the site."
A module of this package with no route of its own: it only has `routes(api)`, so the package can list it.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.routing import BaseRoute

from openajar.api.app import Api

# A run's own status, in the app's verdict words (History) and the approval's outcome (Needs you).
OUTCOME_OF_RUN = {
    "succeeded_verified": "done",
    "succeeded_unverified": "done",
    "partial": "partial",
    "failed": "failed",
    "cancelled": "not_run",
    "queued": "pending",
    "running": "pending",
    "waiting_approval": "pending",
}

# Why something failed, from the node's own words, in plain ones. The first match wins.
_FAILURE_WORDS = (
    # The agent's own mistake (a bad argument, a ref from an old snapshot) or the node's own state: the
    # site did nothing wrong, so the line must not blame it.
    (re.compile(r"ref must look|ref is empty|field required|from the latest browser_snapshot|take a new snapshot",
                re.IGNORECASE), "The agent's request was not valid, so nothing happened on the site."),
    (re.compile(r"stopped answering|browser was closed", re.IGNORECASE),
     "The page stopped answering, so its browser was closed. It opens again on the next job."),
    (re.compile(r"\bis busy\b|\bbusy:", re.IGNORECASE), "Another job was using the account, so this one did not run."),
    (re.compile(r"quarantin", re.IGNORECASE), "The account was switched off for safety, so the agent stopped."),
    (re.compile(r"captcha", re.IGNORECASE), "The site asked for proof that a real person was there."),
    (re.compile(r"checkpoint", re.IGNORECASE), "The site wanted to check that it was really you."),
    (re.compile(r"logged[_ ]out|sign(ed)? ?in|login", re.IGNORECASE), "The account was signed out."),
    (re.compile(r"wrong[_ ]account", re.IGNORECASE), "The account was signed in as someone else."),
    (re.compile(r"blocked", re.IGNORECASE), "The site refused the account."),
    (re.compile(r"rate[_ ]limit", re.IGNORECASE), "The site asked the account to slow down."),
    (re.compile(r"page changed|not show|selector|element", re.IGNORECASE),
     "The site did not look as expected, so the agent stopped."),
    (re.compile(r"time ?out|timed out|deadline", re.IGNORECASE), "It took too long, so the agent stopped."),
    (re.compile(r"denied|declined|said no", re.IGNORECASE), "Someone said no, so nothing was done."),
    (re.compile(r"expired|nobody answered", re.IGNORECASE), "Nobody answered in time, so nothing was done."),
)
SOMETHING_WENT_WRONG = "Something went wrong on the site."

# An approved action's outcome, said once, in the words of what the run really did.
OUTCOME_TITLE = {
    "done": "The approved action went through.",
    "partial": "Only part of the approved action went through.",
    "failed": "The approved action did not work.",
    "not_run": "The approved action was stopped before it ran.",
    "pending": "The approved action is still running.",
}

_AGENTS = {"claude-code": "Claude Code", "claude-app": "Claude", "claude": "Claude", "codex": "Codex",
           "chatgpt-app": "ChatGPT", "chatgpt": "ChatGPT"}


def error_text(value: Any) -> str | None:
    """A run's error as the node keeps it: its message, or its words when it is only text."""
    if isinstance(value, dict):
        value = value.get("message") or value.get("code") or None
    text = str(value).strip() if value else ""
    return text[:500] or None


def named_failure(detail: str | None) -> str | None:
    """The reason in one plain sentence, or None when the node's words name no known cause."""
    for pattern, words in _FAILURE_WORDS:
        if detail and pattern.search(detail):
            return words
    return None


def plain_failure(detail: str | None) -> str:
    """The reason in one plain sentence, or the honest default when no known words match."""
    return named_failure(detail) or SOMETHING_WENT_WRONG


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("-", " ").replace("_", " ")).strip()


def agent_name(principal: str | None) -> str:
    """"claude-code" reads as "Claude Code". An agent named for the computer it runs on (`openajar connect` names
    a grant `claude-code@alex-macbook-pro-3`) reads as "Claude Code (on alex macbook pro 3)". A person's
    name is never guessed from an id."""
    if not principal:
        return "An agent"
    base, _, host = principal.partition("@")
    name = _AGENTS.get(base.lower()) or _words(base).capitalize() or "An agent"
    where = _words(host)
    return f"{name} (on {where})" if where else name


def agent_brand(principal: str | None) -> str | None:
    lowered = (principal or "").lower()
    for word, brand in (("claude", "claude"), ("codex", "codex"), ("chatgpt", "chatgpt"), ("openai", "chatgpt"),
                        ("gpt", "chatgpt"), ("gemini", "gemini"), ("cursor", "cursor")):
        if word in lowered:
            return brand
    return None


def site_of(lane: str | None) -> tuple[str, str]:
    """`biz-a/instagram` is the Instagram account of biz-a: (site key, label). The site is the account's
    own name, so a lane that is only called `shop` reads as Shop, which is what the person named it."""
    name = (lane or "").split("/", 1)[-1]
    key = name.lower()
    label = name.replace("-", " ").replace("_", " ").strip().capitalize() or "the site"
    return key, label


def place_of(lane: str | None, workspace_names: dict[str, str]) -> str:
    """"Aster Coffee's Instagram": the account and whose it is, as the person would say it."""
    workspace = (lane or "").split("/", 1)[0] if lane else ""
    _, label = site_of(lane)
    owner = workspace_names.get(workspace, workspace)
    return f"{owner}'s {label}" if owner else label


def outcome_for(run: dict[str, Any] | None) -> dict[str, Any] | None:
    """What an approved action came to, from the run it started (None when no run has started it)."""
    if run is None:
        return None
    state = OUTCOME_OF_RUN.get(str(run.get("status")), "unknown")
    if state == "done" and run.get("status") == "succeeded_unverified":
        title = "The approved action ran, but nobody could check it afterwards."
    else:
        title = OUTCOME_TITLE.get(state, "The approved action has no outcome yet.")
    failed = state in ("failed", "partial", "not_run")
    detail = error_text(run.get("error"))
    proof = run.get("proof") or {}
    return {
        "state": state,
        "title": title,
        "reason": named_failure(detail) if failed else None,
        "detail": detail,
        "source": "run",
        "at": run.get("finished_at") or run.get("started_at") or run.get("created_at"),
        "run_id": run.get("id"),
        "proof": {"url": proof.get("url"), "screenshot": proof.get("screenshot")} if proof.get("url") else None,
        "cleared_at": None,
        "cleared_by": None,
    }


def routes(api: Api) -> list[BaseRoute]:
    """No route of its own: the shared words above are used by the decision and history routes."""
    del api
    return []
