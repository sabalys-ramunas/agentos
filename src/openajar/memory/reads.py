"""Reading memory, for MemoryService: get one entry (with its versions and provenance), search, and
list. Each read returns a MemoryRead whose taint the caller's family receives.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from openajar.memory.common import (
    WARN_AGENT,
    WARN_UNTRUSTED,
    MemoryRead,
    fts_query,
    labels_of,
    parse_enum,
    trim,
)
from openajar.memory.errors import (
    EntryNotFound,
    OutOfScope,
)
from openajar.memory.models import (
    DAY,
    Caller,
    EntryType,
    State,
    Trust,
    iso,
    parse_when,
)
from openajar.memory.store import (
    WorkspaceDB,
)


class ReadsMixin:
    """MemoryService's read methods."""

    def _version_view(self, db: WorkspaceDB, row: dict, v: dict, *, cloud_redact: bool,
                      valid_to: float | None) -> dict[str, Any]:
        title, body, attrs = self._content(db, v)
        return {
            "version": v["version"], "at": iso(v["at"]), "trust": v["trust"], "labels": labels_of(v),
            "title": self._serve(db, row, title, cloud_redact=cloud_redact),
            "body": self._serve(db, row, body, cloud_redact=cloud_redact),
            "attrs": self._serve_attrs(db, row, attrs, cloud_redact=cloud_redact, etype=row["type"]),
            "forgotten": title is None, "valid_from": iso(v["valid_from"]), "valid_to": iso(valid_to),
            "supersedes": v["supersedes"], "prov": self._prov(db, v),
            "version_hash": v["version_hash"], "prev_hash": v["prev_hash"],
        }

    @staticmethod
    def _prov(db: WorkspaceDB, v: dict[str, Any]) -> dict[str, Any]:
        """A version's provenance. A version imported under its own workspace id keeps its
        stored provenance (and so its hash); `imported_from` comes from the file's import
        record instead."""
        prov = json.loads(v["prov"])
        if prov.get("imported_from") is None:
            raw = db.meta("imported_from")
            if raw:
                imported = json.loads(raw)
                if v["seq"] <= imported.get("head_seq", 0):
                    prov["imported_from"] = {k: imported.get(k) for k in ("node", "workspace", "node_pubkey")}
        return prov

    def get(self, caller: Caller, entry_id: str, *, version: int | None = None, history: bool = False,
            as_of: Any = None, workspace: str | None = None) -> MemoryRead:
        """One entry: body, attributes, links, trust, labels and provenance. `version` picks a
        version; `as_of` the version true in the world at that time (facts); `history` adds
        every version (the content of forgotten versions is gone)."""
        ws, db, row = self._locate(caller, entry_id, workspace)
        level = self._privacy(caller, ws)
        withheld = self._withheld(caller, row, level, explicit=True)
        if withheld == "private" and not caller.can_read_private():
            raise OutOfScope(f"{entry_id} is private and your grant does not include private memory")
        cloud_redact = caller.cloud and level == "redacted-cloud"
        versions = db.versions(entry_id)
        ends = self._valid_ends(versions)
        chosen = self._chosen_version(entry_id, versions, ends, version=version, as_of=as_of)
        view = self._version_view(db, row, chosen, cloud_redact=cloud_redact, valid_to=ends[chosen["version"]])
        stone = db.tombstone(entry_id)
        links, linked_from = _links_of(db, entry_id)
        labels = self._served_labels(row, level)
        data: dict[str, Any] = {
            "workspace": ws, "id": entry_id, "type": row["type"], "key": row["key"], "state": row["state"],
            "current": row["current"], "audience": row["audience"], "trust": view["trust"],
            "labels": labels, "pinned": bool(row["pinned"]), "suspicious": bool(row["suspicious"]),
            "flagged": json.loads(row["flagged"]) if row.get("flagged") else [],
            "writer": row["writer"], "lane": row["lane"], "subject": row["subject"],
            "created_at": iso(row["created_at"]), "updated_at": iso(row["updated_at"]),
            "written_at": view["at"],  # this version's write; updated_at moves on a forget or a review
            "expires_at": iso(row["expires_at"]), "reviewed_at": iso(row["reviewed_at"]),
            "links": links, "linked_from": linked_from, **{k: view[k] for k in (
                "version", "title", "body", "attrs", "valid_from", "valid_to", "prov", "version_hash", "prev_hash")},
        }
        if row["type"] in (EntryType.FACT, EntryType.DOCUMENT, EntryType.PREFERENCE, EntryType.CONTACT) and \
                Trust(view["trust"]) in (Trust.AGENT, Trust.UNTRUSTED):
            data["warning"] = WARN_UNTRUSTED if view["trust"] == Trust.UNTRUSTED else WARN_AGENT
        if stone:
            data["tombstone"] = {"at": iso(stone["at"]), "by": stone["by"], "reason": stone["reason"]}
        taint: set[str] = set()
        if withheld == "private":
            data.update(body=None, attrs=None, withheld="private: this workspace sends private entries to cloud "
                        "models by title only")
        elif view["title"] is not None:
            taint |= set(labels)
        if history:
            data["history"] = []
            for v in versions:
                hv = self._version_view(db, row, v, cloud_redact=cloud_redact, valid_to=ends[v["version"]])
                if withheld == "private":
                    hv.update(body=None, attrs=None)
                elif hv["title"] is not None:
                    taint |= set(hv["labels"])
                data["history"].append(hv)
        return MemoryRead(data, self._apply_taint(caller, taint))

    def _chosen_version(self, entry_id: str, versions: list[dict], ends: dict[int, float | None], *,
                        version: int | None, as_of: Any) -> dict:
        """The version asked for, the one true in the world at `as_of`, or the current one."""
        chosen: dict | None = None
        if version is not None:
            chosen = next((v for v in versions if v["version"] == version), None)
            if chosen is None:
                raise EntryNotFound(f"{entry_id} has no version {version}")
        elif as_of is not None:
            t = parse_when(as_of, now=self.clock(), name="as_of")
            for v in versions:
                start = v["valid_from"] if v["valid_from"] is not None else v["at"]
                end = ends[v["version"]]
                if start <= t and (end is None or t < end):
                    chosen = v
            if chosen is None:
                raise EntryNotFound(f"{entry_id} has no version true at {iso(t)}")
        else:
            chosen = versions[-1] if versions else None
        assert chosen is not None
        return chosen

    @staticmethod
    def _valid_ends(versions: list[dict]) -> dict[int, float | None]:
        """Effective `valid_to` per version: its own, else the next version's start (a later
        reading closes the earlier one)."""
        out: dict[int, float | None] = {}
        for i, v in enumerate(versions):
            end = v["valid_to"]
            if end is None and i + 1 < len(versions):
                nxt = versions[i + 1]
                end = nxt["valid_from"] if nxt["valid_from"] is not None else nxt["at"]
            out[v["version"]] = end
        return out

    def _filters(self, caller: Caller, *, types: Sequence[str] | None, trust_min: str | None, since: Any,
                 until: Any, subject: str | None, writer: str | None, lane: str | None,
                 include_expired: bool) -> tuple[str, dict[str, Any]]:
        where, params = self._visible_sql(caller, include_expired=include_expired, alias="e")
        now = self.clock()
        if types:
            names = [parse_enum(EntryType, t, "types").value for t in types]
            where += " AND e.type IN (" + ", ".join(f":t{i}" for i in range(len(names))) + ")"
            params.update({f"t{i}": n for i, n in enumerate(names)})
        if trust_min:
            floor = parse_enum(Trust, trust_min, "trust_min").rank
            ok = [t.value for t in Trust if t.rank >= floor]
            where += " AND e.trust IN (" + ", ".join(f":r{i}" for i in range(len(ok))) + ")"
            params.update({f"r{i}": n for i, n in enumerate(ok)})
        for name, value, col, op in (("since", since, "updated_at", ">="), ("until", until, "updated_at", "<=")):
            t = parse_when(value, now=now, name=name)
            if t is not None:
                where += f" AND e.{col} {op} :{name}"
                params[name] = t
        for name, value in (("subject", subject), ("writer", writer), ("lane", lane)):
            if value:
                where += f" AND e.{name} = :{name}"
                params[name] = value
        return where, params

    def _hit(self, caller: Caller, db: WorkspaceDB, row: dict, level: str, snippet: str | None) -> tuple[dict, set]:
        withheld = self._withheld(caller, row, level)
        labels = self._served_labels(row, level)
        hit = {"id": row["id"], "type": row["type"], "key": row["key"], "title": row["title"],
               "trust": row["trust"], "labels": labels, "writer": row["writer"], "at": iso(row["updated_at"]),
               "state": row["state"], "subject": row["subject"], "suspicious": bool(row["suspicious"])}
        cloud_redact = caller.cloud and level == "redacted-cloud"
        hit["title"] = self._serve(db, row, row["title"], cloud_redact=cloud_redact)
        if withheld:
            hit["withheld"] = withheld
            return hit, set()
        if snippet is None:
            v = db.version(row["id"], row["current"])
            snippet = trim(db.blob(v["body_hash"]) if v else "", 160)
        hit["snippet"] = self._serve(db, row, snippet, cloud_redact=cloud_redact)
        return hit, set(labels)

    def search(self, caller: Caller, workspace: str, query: str = "", *, types: Sequence[str] | None = None,
               trust_min: str | None = None, since: Any = None, until: Any = None, subject: str | None = None,
               writer: str | None = None, lane: str | None = None, limit: int | None = None,
               include_expired: bool = False) -> MemoryRead:
        """Keyword search (FTS5, bm25) with the fixed re-rank of 4.10: exact key or subject
        matches first, then owner and node above agent above untrusted, live above expired,
        then relevance weighted by recency. Without a query: newest first. Suspicious and
        private-without-scope hits show title and id only and add no labels."""
        self._check_scope(caller, workspace)
        level = self._privacy(caller, workspace)
        lim = self.limits
        limit = max(1, min(int(limit or lim.search_limit_default), lim.search_limit_max))
        where, params = self._filters(caller, types=types, trust_min=trust_min, since=since, until=until,
                                      subject=subject, writer=writer, lane=lane, include_expired=include_expired)
        db = self._db(workspace)
        if db is None:
            return MemoryRead({"workspace": workspace, "query": query, "hits": [], "more": False})
        match = fts_query(query)
        now = self.clock()
        if match:
            rows = db.search(match, where, params, min(limit * 5, 500))
            q = query.strip()

            def rank(r: dict) -> tuple:
                exact = bool(q) and (r["key"] == q or r["subject"] == q)
                trust = Trust(r["trust"])
                tier = 2 if trust in (Trust.OWNER, Trust.NODE) else trust.rank
                live = r["state"] == State.LIVE and (r["expires_at"] is None or r["expires_at"] > now)
                age_days = max(0.0, (now - r["updated_at"]) / DAY)
                relevance = -float(r["score"]) * (0.5 + 0.5 / (1.0 + age_days / 30.0))
                return (exact, tier, live, relevance)

            rows.sort(key=rank, reverse=True)
        else:
            rows = db._all(f"SELECT rowid AS rid, e.*, NULL AS snip FROM entries e WHERE {where} "
                           f"ORDER BY e.updated_at DESC LIMIT {limit + 1}", params)
        more = len(rows) > limit
        hits: list[dict] = []
        taint: set[str] = set()
        for r in rows[:limit]:
            hit, labels = self._hit(caller, db, r, level, r.get("snip"))
            hits.append(hit)
            taint |= labels
        data = {"workspace": workspace, "query": query, "hits": hits, "more": more}
        return MemoryRead(data, self._apply_taint(caller, taint))

    def list_entries(self, caller: Caller, workspace: str, *, types: Sequence[str] | None = None,
                     trust_min: str | None = None, states: Sequence[str] | None = None, since: Any = None,
                     unreviewed: bool = False, limit: int = 50) -> MemoryRead:
        """Titles and metadata, newest first, no bodies (the CLI `list` and the panel
        timeline). `unreviewed` lists agent and untrusted entries not yet reviewed, suspicious
        first ("What agents learned")."""
        self._check_scope(caller, workspace)
        level = self._privacy(caller, workspace)
        db = self._db(workspace)
        if db is None:
            return MemoryRead({"workspace": workspace, "entries": []})
        if states:
            st = [parse_enum(State, x, "states").value for x in states]
            where, params = self._filters(caller, types=types, trust_min=trust_min, since=since, until=None,
                                          subject=None, writer=None, lane=None, include_expired=True)
            where = where.replace("e.state IN ('live', 'expired')", "e.state IN (" + ", ".join(
                f":s{i}" for i in range(len(st))) + ")")
            params.update({f"s{i}": s for i, s in enumerate(st)})
        else:
            where, params = self._filters(caller, types=types, trust_min=trust_min, since=since, until=None,
                                          subject=None, writer=None, lane=None, include_expired=False)
        order = "e.updated_at DESC"
        if unreviewed:
            where += " AND e.reviewed_at IS NULL AND e.trust IN ('agent', 'untrusted')"
            order = "e.suspicious DESC, e.updated_at DESC"
        rows = db._all(f"SELECT e.* FROM entries e WHERE {where} ORDER BY {order} LIMIT {max(1, min(limit, 1000))}",
                       params)
        out = []
        for r in rows:
            withheld = self._withheld(caller, r, level)
            item = {"id": r["id"], "type": r["type"], "key": r["key"], "title": r["title"], "trust": r["trust"],
                    "labels": self._served_labels(r, level), "state": r["state"], "version": r["current"],
                    "writer": r["writer"], "lane": r["lane"], "at": iso(r["updated_at"]),
                    "suspicious": bool(r["suspicious"]), "pinned": bool(r["pinned"]),
                    "reviewed_at": iso(r["reviewed_at"]), "expires_at": iso(r["expires_at"])}
            if withheld:
                item["withheld"] = withheld
            out.append(item)
        return MemoryRead({"workspace": workspace, "entries": out})


def _links_of(db: WorkspaceDB, entry_id: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """The entry's links by kind: to other entries, and from them."""
    links: dict[str, list[str]] = {}
    for link in db.links_from(entry_id):
        links.setdefault(link["kind"], []).append(link["dst"])
    linked_from: dict[str, list[str]] = {}
    for link in db.links_to(entry_id):
        linked_from.setdefault(link["kind"], []).append(link["src"])
    return links, linked_from
