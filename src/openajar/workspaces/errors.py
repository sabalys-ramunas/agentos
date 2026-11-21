"""Errors specific to workspaces. Codes follow openajar.core.errors."""

from __future__ import annotations

from openajar.core.errors import Conflict


class WorkspaceConflict(Conflict):
    """The request clashes with what exists: a workspace that is already there, a workspace that
    still has lanes, or a plan that would link accounts by IP. Nothing was changed. Its code is
    the shared `conflict`."""
