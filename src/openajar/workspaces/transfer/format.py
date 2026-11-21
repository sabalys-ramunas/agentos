"""The export file's format: its name and versions, the members it holds, what it never holds, and
the errors an export or import raises. The layout is described in export.py."""

from __future__ import annotations

import re
from typing import Any

from openajar.core.errors import Conflict, InvalidRequest, OpenajarError
from openajar.ops import backup

FORMAT = "agentos-workspace-export"


FORMAT_VERSION = 3


READ_VERSIONS = (1, 2, 3)   # format 1 has no memory, formats 1 and 2 no agenda


SUFFIX = ".agentos"


META = "agentos-export"


DATA = "workspace"


PARTIAL_PREFIX = ".partial-"


MIN_PASSPHRASE = backup.MIN_PASSPHRASE


MAX_JSON = 64 * 1024 * 1024


JSON_MEMBERS = ("workspace.json", "vault.json", "flows.json", "schedules.json", "grants.json", "proxies.json")


MEMORY_MEMBER = "memory.jsonl"


AGENDA_MEMBER = "agenda.json"


COUNTS = ("lanes", "profile_files", "profile_bytes", "fingerprints", "vault_entries", "flows", "flow_versions",
          "schedules", "grants", "proxies", "memory_entries", "memory_versions", "agenda_entries")


NOT_EXPORTED = [
    "proxy hosts and credentials (labels and an endpoint digest only, unless with_proxies)",
    "grant tokens (bearer secrets): grants travel as definitions and are re-issued on import",
    "webhook tokens and secrets of schedules: the importing node makes new ones",
    "machine secrets (brain subscription tokens, child grant tokens): they belong to the node",
    "runs, step journals, proofs, screenshots and recordings (history stays where it happened)",
    "approvals of agenda entries, and entries that ran or finished: an entry comes in as a draft to propose again",
    "proxy-rule override records and the activity log",
    "browser caches, profile lock files, symlinks and each lane's shots/ and files/ directories",
    "terminal and service lanes (only browser lanes move for now)",
    "the traces memory provenance cites: each version keeps a snapshot of its evidence, the traces stay put",
]


_GLOB = re.compile(r"[*?\[]")


class ExportRefused(Conflict):
    """The export or import cannot go ahead as asked; nothing was changed."""
    code = "export_refused"


class ExportError(OpenajarError):
    code = "export_failed"


class BadExport(InvalidRequest):
    """The file cannot be used: wrong passphrase or key, damaged, truncated, altered, not an export."""
    code = "bad_export"


def memory_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {"memory_entries": sum(1 for r in records if r.get("record") == "entry"),
            "memory_versions": sum(1 for r in records if r.get("record") == "version")}
