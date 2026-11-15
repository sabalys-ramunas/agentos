"""The errors of agent environments. Each code has an entry in agentos/core/error_catalog.toml."""

from __future__ import annotations

from typing import Any

from agentos.core.errors import AgentosError


class NoEnvironment(AgentosError):
    """A terminal or browser call without `on`, in a chat that has no environment open."""
    code = "no_environment"


class UnsavedWork(AgentosError):
    """environment_close found changes that are not committed, or commits that are not pushed.
    `details` lists them: {"dirty_files": [...], "unpushed": [...]}."""
    code = "unsaved_work"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class EnvironmentLimit(AgentosError):
    """Opening one more environment would pass a limit of this node: how many at once, how long one
    lives, or the money it may spend in a month."""
    code = "environment_limit"


class EnvironmentNotReady(AgentosError):
    """The chat has an environment, but a call cannot reach it right now: its machine is still
    starting, it is closing, or its tunnel is down (the node restarted, or the connection dropped).
    Nothing ran in its place on the node."""
    code = "environment_not_ready"


class EnvironmentFailed(AgentosError):
    """The environment's machine did not come up (no SSH, or its setup failed). Its VM is deleted."""
    code = "environment_failed"


class EnvironmentInUse(AgentosError):
    """environment_open named the title of an environment another chat of the same agent still works
    in (a call in the last 10 minutes). It is not taken from that chat."""
    code = "environment_in_use"


class EnvironmentAlreadyOpen(AgentosError):
    """environment_open in a chat that already has a live environment, with another repository than
    the one asked for. A chat has one environment at a time; nothing was opened."""
    code = "environment_already_open"


class RepoNotConnected(AgentosError):
    """environment_open named a repository the node cannot clone: it is not declared for workspace
    env with a credential in the vault, and it cannot be read without one (a private repository), or
    the node's credential for it was refused (a deploy key not added, a token revoked). Nothing was
    rented, or the machine was deleted. The human connects it on the node; the agent never does."""
    code = "repo_not_connected"
    access: Any = None  # what GitService.access found, when the node checked before renting


class RepoConnectFailed(AgentosError):
    """A token or deploy key a person gave on a repository card did not work, and nothing was kept.
    `reason` says which check failed (bad_token, cannot_see, sso, org_approval, org_policy,
    no_contents, rate_limited, unreachable, not_yet)."""
    code = "repo_connect_failed"

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason
