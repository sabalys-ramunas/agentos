"""Freeze errors. They subclass the core errors, so every surface maps them to stable codes."""

from __future__ import annotations

from agentos.core.errors import AgentPaused, PolicyDenied


class Frozen(AgentPaused):
    """The lane, its workspace, this node or the whole mesh is frozen: no agent acts there until
    the owner unfreezes it, and grants that reached it were revoked. Do not retry; tell your user."""
    code = "frozen"


class UnfreezeDenied(PolicyDenied):
    """Only the owner unfreezes, signed in (with a passkey check when the node has members) or on
    the node itself. Agents never do."""
    code = "unfreeze_denied"
