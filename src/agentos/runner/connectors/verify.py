"""Mark a connector operation verified (or not) after a canary on a throwaway account.

    mark_installed(ledger, "reddit.comment", installed_definition, note="...", day=date(2026, 11, 6))
    doc, version, proof = mark_verified(Path("connectors/reddit"), "comment", note="...", ledger=ledger)

A mark is honoured only from the node's own ledger (agentos.runner.connectors.marks), and only
while the installed operation is the implementation the canary ran (its proof hash,
compile.proof_hash). `mark_installed` marks the operation as installed on this node: the usual
case, after a canary ran it here. `mark_verified` marks the connector in a directory the owner
names (`--dir`): it also records the canary's words and the proof hash in that connector.json
(information for a reader, never a mark anywhere else) with a new patch version, so the repo says
what was proved; the ledger mark names the same proof hash, which a version bump does not change.

Only the owner does this, from a shell on the node (`agentos connector verify`): no MCP tool or
API route marks an operation. The connector is the installed copy or an explicit directory,
never whatever connectors/ the shell happens to be in.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from agentos.runner.connectors.compile import (
    CONNECTOR_FILE,
    ConnectorInvalid,
    compile_connector,
    load_connector,
    proof_hash,
)
from agentos.runner.connectors.marks import MarkLedger
from agentos.runner.connectors.models import SEMVER_RE

NOTE_MAX = 200


def verified_text(day: date, domain: str, note: str) -> str:
    note = " ".join(note.split())
    if not note:
        raise ConnectorInvalid("say what the canary proved (--note): the account, the object and how it was read back")
    if len(note) > NOTE_MAX:
        raise ConnectorInvalid(f"the note is at most {NOTE_MAX} characters")
    return f"{day.isoformat()} {domain} canary: {note}"


def bump_patch(version: str) -> str:
    if not SEMVER_RE.match(version or ""):
        raise ConnectorInvalid(f"the connector's version {version!r} is not MAJOR.MINOR.PATCH, so it cannot be "
                               "bumped: fix it in connector.json first")
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def mark_installed(ledger: MarkLedger, operation: str, definition: Mapping[str, Any] | None, *,
                   domain: str, note: str = "", day: date | None = None, revoke: bool = False,
                   version: str | None = None) -> dict[str, Any] | None:
    """Mark (or with `revoke`, unmark) the installed operation `operation` (site.op) in the ledger.
    `definition` is its installed FlowDefinition document. Returns the mark, None after a revoke."""
    if revoke:
        if not ledger.remove(operation):
            raise ConnectorInvalid(f"{operation} is not marked verified on this node")
        return None
    if definition is None:
        raise ConnectorInvalid(f"{operation} is not installed on this node: install the connector the canary ran, "
                               "or pass --dir with the connector directory")
    text = verified_text(day or date.today(), domain, note)
    return ledger.put(operation, proof=proof_hash(definition), text=text, version=version)


def mark_verified(path: Path | str, operation: str, *, note: str = "", day: date | None = None,
                  revoke: bool = False, ledger: MarkLedger | None = None) -> tuple[dict[str, Any], str, str]:
    """Set (or with `revoke`, clear) `operation`'s mark in the connector at `path` (a
    connectors/<site>/ directory or its connector.json): the canary's words and the proof hash go
    into the file with a new patch version, and, with `ledger`, the mark into the node's ledger.
    Returns (the new document, the new version, the proof hash). Raises ConnectorInvalid; nothing
    is written unless the result compiles strictly."""
    doc, root = load_connector(path)
    ops = doc.get("operations") or {}
    if operation not in ops:
        raise ConnectorInvalid(f"{doc.get('connector')} has no operation {operation!r} "
                               f"(it has {', '.join(sorted(ops)) or 'none'})")
    op = ops[operation]
    key = f"{doc.get('connector')}.{operation}"
    if revoke:
        if not op.get("verified") and (ledger is None or ledger.get(key) is None):
            raise ConnectorInvalid(f"{key} is not marked verified")
        op.pop("verified", None)
        op.pop("verified_hash", None)
    else:
        text = verified_text(day or date.today(), str(doc.get("domain") or doc.get("site")), note)
        # kept next to the title and description, where a reader of the file looks
        head = [k for k in ("title", "description") if k in op]
        rest = {k: v for k, v in op.items() if k not in head and k not in ("verified", "verified_hash")}
        ops[operation] = {**{k: op[k] for k in head}, "verified": text, "verified_hash": "", **rest}
    doc["version"] = bump_patch(str(doc.get("version") or "0.0.0"))
    proof = compile_connector(doc, root=root).operations[operation].proof  # compiles strictly, or nothing is written
    if not revoke:
        ops[operation]["verified_hash"] = proof
        compile_connector(doc, root=root)
    (root / CONNECTOR_FILE).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    if ledger is not None:
        if revoke:
            ledger.remove(key)
        else:
            ledger.put(key, proof=proof, text=ops[operation]["verified"], version=doc["version"])
    return doc, doc["version"], proof
