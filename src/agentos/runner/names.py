"""Ids and names a flow uses: flow ids, lane patterns, parameter names and the connector prefix.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase

FLOW_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}(\.[a-z0-9][a-z0-9_-]{0,40}){1,5}$")
LANE_PATTERN_RE = re.compile(r"^[a-z0-9*?\[\]-]{1,31}/[a-z0-9*?\[\]-]{1,41}$")
PARAM_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
RESERVED_PARAMS = frozenset({"on", "ctx", "wait_seconds", "idempotency_key", "vars", "trigger", "version"})
CONNECTOR_PREFIX = "connector."  # flow ids compiled from connectors (agentos.runner.connectors)
CONNECTOR_RESERVED = ("flow ids under connector.* are reserved for installed connectors "
                      "(agentos connector install); save the flow under another id")


def is_connector_id(flow_id: str) -> bool:
    return flow_id.startswith(CONNECTOR_PREFIX)
SCOPE_ROOTS = frozenset({"vars", "trigger"})


def is_lane_pattern(lane: str) -> bool:
    return any(c in lane for c in "*?[")


def lane_matches(pattern: str, lane: str) -> bool:
    return fnmatchcase(lane, pattern)
