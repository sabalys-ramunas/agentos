"""Who did something on the node, and how, in one wording (QA-2 #8).

The stores keep what they always kept (`decided_by` and `decided_via` on an approval, `principal`,
`principal_kind` and `args.via` on a trace): other code reads those spellings, so they do not change.
What a person reads (Activity, an approval's "decided by") goes through `actor`: the owner is one
name, the node's token is never the owner in person (an agent may hold it), and the node's CLI with
its one-time code is the owner on the node, in person."""

from __future__ import annotations

from typing import Any

OWNER = "the owner"
NODE_TOKEN = "the node's token"
DEVICE = "the owner's computer (its saved credential)"
OWNER_SPELLINGS = frozenset({"owner", "human:owner", "node:owner", "owner:cli"})
# shown as the owner, but not a hand-made setting change: Telegram only answers approvals
OWNER_NAMES = OWNER_SPELLINGS | {"telegram:owner"}
IN_PERSON_ON_NODE = "on the node, in person"

# how a decision or a panel action arrived, as approvals (decided_via) and panel traces (args.via) say it
HOW = {
    "session": "in the panel",
    "passkey": "with a passkey",
    "code": "signed in with a code",
    "cli": IN_PERSON_ON_NODE,
    "push_link": "from the phone link",
    "standing": "by a standing approval",
    "chat": "in chat",
    "telegram": "from Telegram",
    "token": "with its grant's token",
}
NO_PERSON = frozenset({"owner_token", "node_token"})  # the node's token: who holds it is not known


def actor(by: str | None, via: str | None) -> tuple[str | None, str | None]:
    """(who, how) for a person to read, from a stored `by` and `via`."""
    if by is None and via is None:
        return None, None
    if via in NO_PERSON:
        return NODE_TOKEN, None
    if via == "device":
        return DEVICE, None
    if by in OWNER_NAMES:
        if via is None:
            return OWNER, IN_PERSON_ON_NODE if by == "owner:cli" else None
        return OWNER, HOW.get(via)
    return by, HOW.get(via) if via else None


def decided_by(by: str | None) -> str | None:
    """An approval's `decided_by` as the API shows it: the owner under one name."""
    return "owner" if by in OWNER_NAMES else by


def trace_actor(event: dict[str, Any]) -> tuple[str | None, str | None]:
    """(who, how) for an Activity entry."""
    args = event.get("args") if isinstance(event.get("args"), dict) else {}
    via = args.get("via") if isinstance(args.get("via"), str) else None
    kind, principal = event.get("principal_kind"), event.get("principal")
    if kind == "node" and via is None:
        via = "node_token"
    if kind in ("human", "node") and principal == "owner":
        return actor("owner", via)
    return actor(event.get("handle") or principal, via)
