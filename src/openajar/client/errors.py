"""Errors the client commands turn into one clean `error: ...` line and a hint."""

from __future__ import annotations


class ClientError(Exception):
    """Something the owner can fix. `hint` says how, in one line."""

    def __init__(self, message: str, hint: str | None = None, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or []  # extra lines to show, such as the end of the tunnel log


class SshError(ClientError):
    """The node could not be read over SSH (unknown host, unreachable, login refused)."""


class NodeConfigError(ClientError):
    """SSH worked, but the node has no usable node.toml."""


class NodeMissing(NodeConfigError):
    """SSH worked and the host has no node.toml at all: connect can offer to install one."""


class TokenStoreError(ClientError):
    """The Keychain (or the token file) refused to store or read the token."""


class PortInUse(ClientError):
    pass


class TunnelError(ClientError):
    """launchd or systemd refused the tunnel, or it did not come up."""


class VerifyError(ClientError):
    """The tunnel is installed but the MCP endpoint did not answer with the token."""


class NotConnected(ClientError):
    pass


def scrub(text: str, *secrets: str) -> str:
    """Mask secrets in text that came back from a subprocess, before anyone sees it."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text
