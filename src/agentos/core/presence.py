"""How sure the node is that a person made a request: one ordered level (docs/THREAT-MODEL.md).

The panel API sets it on every request's Access (agentos.api.access) from the credential the request
arrived with, and the node's other HTTP routes (agentos.node.viewer_app) read it from there.
"""

from __future__ import annotations

from enum import IntEnum


class Presence(IntEnum):
    """How sure the node is that a person, not an agent, made this request (docs/THREAT-MODEL.md).
    Ordered, weakest first. A route names its minimum (`endpoint(..., presence=...)`); a handler whose
    need depends on the body asks `Access.require_presence`. Nothing the caller sends sets it: it comes
    from the credential the request arrived with.

    The line that matters is PERSON: below it sit the credentials an agent can hold (a grant, the node
    token, the device credential in the Mac's login Keychain), so approving, answering, turning a
    protection off, widening a lane's sites, enrolling a passkey and unfreezing all need PERSON."""
    AGENT = 0       # a grant's bearer token: an agent's, an app's, an admin grant
    NODE_TOKEN = 1  # the node's own token (node.toml mcp_token): the owner's authority, but agents may hold it
    DEVICE = 2      # the owner's device credential (`agentos connect`): in the login Keychain of that computer
    PERSON = 3      # a panel session from a one-time code or a passkey, or the node's own CLI (panel.db on disk)
    VERIFIED = 4    # PERSON with a passkey check inside the step-up window

    @property
    def word(self) -> str:
        return self.name.lower()


NEEDS_A_PERSON = ("This needs you in person: sign in to the panel (a passkey, or `agentos panel login` on the "
                  "node) and do it there. A token or this computer's saved credential cannot, because an agent "
                  "may hold them.")



def presence_of(request: object, home: object = None) -> Presence:
    """The presence of a request the panel API authenticated (`request.state.auth.access`). A request
    with none came through the bare node-token check (no panel), so it is the node token, or a person
    when it carries a one-time code from the node's CLI that this node's panel.db (under `home`) holds."""
    auth = getattr(getattr(request, "state", None), "auth", None)
    presence = getattr(getattr(auth, "access", None), "presence", None)
    if isinstance(presence, Presence):
        return presence
    from agentos.api.auth import PRESENCE_HEADER, PanelStore

    headers = getattr(request, "headers", None) or {}
    code = (headers.get(PRESENCE_HEADER) or "").strip()
    if code and home is not None and PanelStore.for_home(home).use_code(code):  # type: ignore[arg-type]
        return Presence.PERSON
    return Presence.NODE_TOKEN
