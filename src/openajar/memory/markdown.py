"""A workspace's memory as Markdown: one text per entry type, with provenance footnotes.

`openajar memory export --md` writes these files, and the panel's memory export (format=md) serves them.
"""

from __future__ import annotations

import json
from typing import Any

from openajar.memory.models import Caller, iso
from openajar.memory.service import MemoryService

OWNER = Caller.owner(session="cli")  # the owner as the CLI reads: the export reads every entry as the owner
MD_TYPES = ("decision", "preference", "document", "fact", "contact", "run_summary")
TYPE_TITLES = {"decision": "Decisions", "preference": "Preferences", "document": "Documents", "fact": "Facts",
               "contact": "Contacts", "run_summary": "Run summaries"}


def markdown_export(svc: MemoryService, workspace: str) -> dict[str, str]:
    """One Markdown text per type with provenance footnotes: {file name: text}."""
    ids = [r["id"] for r in svc.export_records(workspace) if r["record"] == "entry"]
    by_type: dict[str, list[dict[str, Any]]] = {}
    forgotten = 0
    for entry_id in ids:
        e = svc.get(OWNER, entry_id, workspace=workspace).data
        if e["state"] == "forgotten":
            forgotten += 1
            continue
        by_type.setdefault(e["type"], []).append(e)
    out: dict[str, str] = {}
    for etype in MD_TYPES:
        entries = by_type.get(etype)
        if not entries:
            continue
        lines = [f"# {workspace}: {TYPE_TITLES[etype]}", ""]
        notes: list[str] = []
        for e in entries:
            n = len(notes) + 1
            lines += [f"## {e['title']} [^{n}]", ""]
            if e.get("body"):
                lines += [e["body"], ""]
            if e.get("attrs"):
                lines += [f"Attributes: `{json.dumps(e['attrs'], sort_keys=True)}`", ""]
            prov = e.get("prov") or {}
            who = (prov.get("principal") or {}).get("id") or e.get("writer")
            parts = [f"{e['id']}@{e['version']}", f"{e['trust']} trust", f"written by {who} at {e['updated_at']}"]
            if prov.get("confirmed_by"):
                parts.append(f"confirmed by {prov['confirmed_by']}")
            if e["state"] != "live":
                parts.append(e["state"])
            for ev in prov.get("evidence") or []:
                parts.append(f"evidence {ev.get('trace')} ({ev.get('tool')}, {ev.get('at')})")
            parts.append(e["version_hash"])
            notes.append(f"[^{n}]: " + "; ".join(parts))
        out[f"{workspace}-{etype}.md"] = "\n".join(lines + notes) + "\n"
    if forgotten:
        out[f"{workspace}-README.md"] = (f"# {workspace}: memory export\n\nExported {iso(svc.clock())}. "
                                         f"{forgotten} forgotten entries are not included (their content is gone).\n")
    return out
