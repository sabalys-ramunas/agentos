"""Moving a workspace's memory between nodes, for MemoryService (design section 4.13): export its
rows as signed records, import them under a new workspace name, or drop them.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from agentos.core.errors import Conflict, InvalidRequest
from agentos.memory.common import (
    attr_text,
)
from agentos.memory.models import (
    EntryType,
    State,
    Trust,
    check_workspace,
)
from agentos.memory.store import SCHEMA_VERSION as STORE_SCHEMA
from agentos.memory.store import (
    WorkspaceDB,
    blob_hash,
    canonical,
    verify_rows,
    version_hash,
)


class PortableMixin:
    """MemoryService's export and import methods."""

    def export_records(self, workspace: str, *, node_pubkey: str | None = None) -> list[dict[str, Any]]:
        """The workspace's memory as records for `workspace/memory.jsonl`: a header, then every
        entry, version (with provenance and hashes), blob of content not forgotten, tombstone
        and link. Node side: the caller is the owner exporting the workspace."""
        check_workspace(workspace)
        db = self._db(workspace)
        if db is None:
            return []
        rows = db.dump()
        check = db.verify()
        header = {"record": "memory", "workspace": workspace, "schema": STORE_SCHEMA, "node": self.ports.node_id,
                  "node_pubkey": node_pubkey, "head": check["head"], "versions": check["versions"],
                  "entries": len(rows["entries"]), "chain_ok": check["ok"],
                  "imported_from": json.loads(db.meta("imported_from") or "null")}
        out: list[dict[str, Any]] = [header]
        for table, kind in (("entries", "entry"), ("versions", "version"), ("blobs", "blob"),
                            ("tombstones", "tombstone"), ("links", "link")):
            out.extend({"record": kind, **{k: v for k, v in row.items() if k != "rid"}} for row in rows[table])
        return out

    def has_memory(self, workspace: str) -> bool:
        db = self._db(workspace)
        return db is not None and not db.is_empty()

    def import_records(self, workspace: str, records: Sequence[dict[str, Any]], *,
                       source: dict[str, Any] | None = None) -> dict[str, Any]:
        """Bring an exported workspace's memory in as `workspace` (4.13). Trust and labels are
        kept (the export is encrypted to the owner). Under the same id every row is copied as
        it was, so every version hash and the chain are the source's; under a new id (`--as`)
        keys, decision scopes, lanes and lane subjects move to it, which changes the content
        hashes, so the chain is rebuilt and each version records the hash it had on the source.
        Forgotten entries stay forgotten (their content never left the source). Refuses a
        workspace that already has memory. The result says whether the chain verifies; an
        import that does not verify is removed again."""
        check_workspace(workspace)
        if not records:
            return {"workspace": workspace, "entries": 0, "versions": 0, "ok": True}
        header = records[0]
        if header.get("record") != "memory" or not isinstance(header.get("workspace"), str):
            raise InvalidRequest("the memory records have no header")
        if int(header.get("schema") or 0) > STORE_SCHEMA:
            raise InvalidRequest(f"the exported memory uses schema {header.get('schema')}; this agentos reads "
                                 f"{STORE_SCHEMA}")
        old = check_workspace(header["workspace"])
        rows = _rows_by_table(records[1:])
        if self.has_memory(workspace):
            raise Conflict(f"{workspace} already has memory on this node; an import never merges")
        _verify_export(rows, header)
        imported = {"node": header.get("node"), "node_pubkey": header.get("node_pubkey"), "workspace": old,
                    "head": header.get("head"), **(source or {})}
        if workspace != old:
            rows = self._rename_rows(rows, old, workspace, imported)
        imported["head_seq"] = max((int(v["seq"]) for v in rows["versions"]), default=0)
        fts = _search_rows(rows)
        db = self._db(workspace, create=True)
        assert db is not None
        try:
            db.load(rows, fts)
            db.set_meta("imported_from", canonical(imported))
            check = db.verify()
            if not check["ok"]:
                raise InvalidRequest("the imported memory does not verify: " + "; ".join(check["problems"][:3]))
        except BaseException:
            self.stores.drop(workspace)
            raise
        for e in rows["entries"]:
            if e["state"] != State.FORGOTTEN and (e["trust"] == Trust.OWNER or e["type"] == EntryType.DECISION):
                self._pin_evidence(db, e["id"])
        return {"workspace": workspace, "from": old, "entries": len(rows["entries"]), "versions": check["versions"],
                "forgotten": check["forgotten"], "head": check["head"], "ok": check["ok"],
                "same_hashes": workspace == old}

    def drop_workspace(self, workspace: str) -> None:
        """Delete one workspace's memory file (an import rolled back)."""
        self.stores.drop(check_workspace(workspace))

    @staticmethod
    def _rename_rows(rows: dict[str, list[dict[str, Any]]], old: str, new: str,
                     imported: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """`--as`: rewrite the workspace id in keys, lanes, lane subjects and decision scopes,
        then rebuild the chain (content hashes change with the content)."""
        def lane(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            ws, sep, rest = value.partition("/")
            return f"{new}/{rest}" if sep and ws == old else value

        def subject(value: Any) -> Any:
            if isinstance(value, str) and value.startswith("lane:"):
                return "lane:" + lane(value[5:])
            return value

        def key(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            first, sep, rest = value.partition(".")
            return f"{new}{sep}{rest}" if first == old and sep else value

        def attrs(data: dict[str, Any]) -> dict[str, Any]:
            out = dict(data)
            if isinstance(out.get("scope"), list):
                out["scope"] = [lane(p) for p in out["scope"]]
            if "lane" in out:
                out["lane"] = lane(out["lane"])
            if "subject" in out:
                out["subject"] = subject(out["subject"])
            return out

        blobs = {b["hash"]: dict(b) for b in rows["blobs"]}
        new_blobs: dict[str, dict[str, Any]] = {}
        versions = []
        prev: str | None = None
        for v in sorted(rows["versions"], key=lambda r: r["seq"]):
            v = dict(v)
            for col in ("title_hash", "body_hash"):
                if v[col] in blobs:
                    new_blobs[v[col]] = blobs[v[col]]
            if v["attrs_hash"] and v["attrs_hash"] in blobs:
                data = canonical(attrs(json.loads(blobs[v["attrs_hash"]]["data"])))
                nonce = blobs[v["attrs_hash"]]["nonce"]
                h = blob_hash(nonce, data)
                new_blobs[h] = {"hash": h, "nonce": nonce, "data": data}
                v["attrs_hash"] = h
            prov = json.loads(v["prov"]) if isinstance(v["prov"], str) else dict(v["prov"])
            prov["lane"] = lane(prov.get("lane"))
            prov["imported_from"] = {"node": imported.get("node"), "workspace": old,
                                     "version_hash": v["version_hash"]}
            v["prov"] = canonical(prov)
            v["prev_hash"] = prev
            v["version_hash"] = version_hash(v)
            prev = v["version_hash"]
            versions.append(v)
        entries = [{**e, "key": key(e.get("key")), "lane": lane(e.get("lane")), "subject": subject(e.get("subject"))}
                   for e in rows["entries"]]
        return {**rows, "entries": entries, "versions": versions, "blobs": list(new_blobs.values())}


_RECORD_TABLES = {"entry": "entries", "version": "versions", "blob": "blobs", "tombstone": "tombstones",
                  "link": "links"}


def _rows_by_table(records: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The exported records after the header, as the rows of each table."""
    rows: dict[str, list[dict[str, Any]]] = {t: [] for t in WorkspaceDB.TABLES}
    for rec in records:
        table = _RECORD_TABLES.get(rec.get("record")) if isinstance(rec, dict) else None
        if table is None:
            raise InvalidRequest("the memory records hold an unknown record")
        rows[table].append({k: v for k, v in rec.items() if k != "record"})
    return rows


def _verify_export(rows: dict[str, list[dict[str, Any]]], header: dict[str, Any]) -> None:
    """The export's chain verifies, and ends at the version its header names."""
    forgotten = {t["entry_id"] for t in rows["tombstones"]}
    check = verify_rows(rows["versions"], rows["blobs"], forgotten)
    if check["problems"]:
        raise InvalidRequest("the exported memory does not verify: " + "; ".join(check["problems"][:3]))
    if header.get("head") and check["head"] != header["head"]:
        raise InvalidRequest("the exported memory does not verify: its last version is not the one exported")


def _search_rows(rows: dict[str, list[dict[str, Any]]]) -> list[tuple[str, str, str, str]]:
    """The full-text rows of every live entry's current version: id, title, body, attribute text."""
    blobs = {b["hash"]: b["data"] for b in rows["blobs"]}
    current = {(v["entry_id"], v["version"]): v for v in rows["versions"]}
    fts = []
    for e in rows["entries"]:
        if e["state"] == State.FORGOTTEN:
            continue
        v = current.get((e["id"], e["current"]))
        if v is None:
            raise InvalidRequest(f"the exported memory has no current version for {e['id']}")
        raw = blobs.get(v["attrs_hash"]) if v["attrs_hash"] else None
        fts.append((e["id"], blobs.get(v["title_hash"]) or "", blobs.get(v["body_hash"]) or "",
                    attr_text(json.loads(raw) if raw else {})))
    return fts
