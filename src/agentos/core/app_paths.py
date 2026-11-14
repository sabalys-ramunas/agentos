"""Paths of the web app's contract that the node hands out beyond the API (a lane's live view), so
a feature can give one without importing the API that serves it."""

from __future__ import annotations

from urllib.parse import quote

ORG = "local"
"""A node is one org to the app: `/local/needs-you`. Not on the reserved list in web/DESIGN.md."""


def lane_viewer_path(workspace: str, name: str) -> str:
    """Where the app opens this lane's live view on this node: the same socket the node serves."""
    return f"/api/orgs/{ORG}/lanes/{quote(workspace, safe='')}/{quote(name, safe='')}/vnc"
