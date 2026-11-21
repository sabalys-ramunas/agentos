"""The owner's side of openajar: connect this computer's MCP clients to a node.

`openajar connect <ssh-host>` reads the node's MCP port and token over SSH, runs a persistent
SSH tunnel under launchd (a systemd user unit on Linux), and through it, with the node token
used once and never stored, gets the agents' own grant and the owner's device credential. It
keeps both in the macOS Keychain (chmod 600 files on Linux), in separate items, and registers
the server with Claude Code. This package runs on the owner's computer, never on a node.

The main CLI mounts `app` at the top level: `app.add_typer(client_app)`.
"""

from openajar.client.cli import app

__all__ = ["app"]
