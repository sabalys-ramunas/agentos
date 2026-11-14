"""The context pack, for MemoryService (design section 4.12): what an agent reads first in a
workspace, filled section by section within a token budget.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agentos.memory.common import (
    CANDIDATES,
    CHARS_PER_TOKEN,
    HOW_MORE,
    NO_TASK_FACTS,
    NOTE_CHARS,
    RELEVANT_CHARS,
    WARN_AGENT,
    WARN_SUSPICIOUS,
    WARN_UNTRUSTED,
    MemoryRead,
    fts_query,
    scope_matches,
    trim,
)
from agentos.memory.models import (
    DAY,
    Caller,
    EntryType,
    Trust,
    iso,
)
from agentos.memory.store import (
    WorkspaceDB,
)

log = logging.getLogger("agentos.memory")


class PackMixin:
    """MemoryService's context pack methods."""

    def context(self, caller: Caller, workspace: str, *, task: str | None = None,
                budget_tokens: int | None = None) -> MemoryRead:
        """The pack for starting work, assembled without a model inside `budget_tokens`
        (4 characters a token): rules first, then briefs, relevant, recent runs, notes. Records
        a context receipt for the caller's family."""
        self._check_scope(caller, workspace)
        level = self._privacy(caller, workspace)
        lim = self.limits
        budget = int(budget_tokens or lim.context_budget_default)
        budget = max(lim.context_budget_min, min(budget, lim.context_budget_max))
        limit_chars = budget * CHARS_PER_TOKEN
        digest = self.decisions_digest(workspace)
        pack: dict[str, Any] = {
            "workspace": workspace, "privacy": level, "digest": digest, "budget_tokens": budget,
            "rules": [], "briefs": [], "relevant": [], "recent_runs": [], "notes": [],
            "omitted": {"rules": 0, "briefs": 0, "relevant": 0, "recent_runs": 0, "notes": 0, "how": HOW_MORE},
        }
        db = self._db(workspace)
        taint: set[str] = set()
        if db is not None:
            self._fill_pack(caller, db, level, pack, task, limit_chars, taint)
        if caller.family and self.ports.policy is not None:
            record = getattr(self.ports.policy, "record_receipt", None)
            if callable(record):
                try:
                    record(caller.family, workspace, digest)
                except Exception:
                    log.exception("recording the context receipt failed")
        return MemoryRead(pack, self._apply_taint(caller, taint))

    def _fill_pack(self, caller: Caller, db: WorkspaceDB, level: str, pack: dict, task: str | None,
                   limit_chars: int, taint: set[str]) -> None:
        where, params = self._visible_sql(caller)
        rows = db.entries(where, params, order="created_at", limit=100000)
        fill = _PackFill(self, caller, db, level, pack, limit_chars)
        fill.rules(rows)
        fill.briefs(rows)
        hits = fill.relevant(rows, task)
        fill.recent_runs(rows)
        fill.notes(rows, hits)
        fill.fit_budget()
        for _section, _item, labels in fill.items:
            taint |= labels


class _PackFill:
    """Fills one context pack, section by section, within `limit_chars`. A section that runs out of
    room takes nothing more (so the pack is deterministic), and every item's labels are kept for
    the taint the caller receives."""

    def __init__(self, svc: Any, caller: Caller, db: WorkspaceDB, level: str, pack: dict, limit_chars: int):
        self.svc = svc
        self.caller = caller
        self.db = db
        self.level = level
        self.pack = pack
        self.limit_chars = limit_chars
        self.cloud_redact = caller.cloud and level == "redacted-cloud"
        self.lanes = list(caller.scope.lanes) + ([caller.lane] if caller.lane else [])
        self.used: set[str] = set()
        self.size = len(json.dumps(pack)) + 120   # room for the omitted counts and warnings
        self.items: list[tuple[str, dict, set[str]]] = []
        self.closed: set[str] = set()
        self.since = 0.0   # set as the relevant section starts; recent runs use the same cut

    # ---- the parts every section uses

    def content(self, r: dict) -> tuple[str | None, str | None, dict]:
        v = self.db.version(r["id"], r["current"])
        if v is None:
            return None, None, {}
        title, body, attrs = self.svc._content(self.db, v)
        return (self.serve(r, title), self.serve(r, body), attrs or {})

    def serve(self, r: dict, text: str | None) -> str | None:
        return self.svc._serve(self.db, r, text, cloud_redact=self.cloud_redact)

    def withheld(self, r: dict) -> str | None:
        return self.svc._withheld(self.caller, r, self.level)

    def labels(self, r: dict, withheld: str | None = None) -> set[str]:
        return set() if withheld else set(self.svc._served_labels(r, self.level))

    def skip(self, section: str) -> bool:
        if section in self.closed:
            self.pack["omitted"][section] += 1
            return True
        return False

    def add(self, section: str, item: dict, labels: set[str]) -> bool:
        cost = len(json.dumps(item)) + 2
        if self.size + cost > self.limit_chars:
            self.pack["omitted"][section] += 1
            if section != "rules":
                self.closed.add(section)
            return False
        self.size += cost
        self.pack[section].append(item)
        self.items.append((section, item, labels))
        self.used.add(item["id"])
        return True

    # ---- the sections, in the order they are filled

    def rules(self, rows: list[dict]) -> None:
        """Live decisions in scope and owner preferences. When they do not fit whole, only their
        titles are shown."""
        rules: list[tuple[dict, dict, set[str]]] = []
        for r in rows:
            is_rule = r["type"] == EntryType.DECISION or (
                r["type"] == EntryType.PREFERENCE and r["trust"] == Trust.OWNER)
            if not is_rule:
                continue
            title, body, attrs = self.content(r)
            if r["type"] == EntryType.DECISION and not scope_matches(attrs.get("scope") or [], self.lanes):
                continue
            withheld = self.withheld(r)
            item = {"id": r["id"], "type": r["type"], "title": title, "body": None if withheld else body,
                    "enforced": bool(attrs.get("enforced_by"))}
            rules.append((r, item, self.labels(r, withheld)))
        omitted = self.pack["omitted"]
        full = sum(len(json.dumps(i)) + 2 for _r, i, _l in rules)
        if self.size + full > self.limit_chars and rules:
            for _r, item, labels in rules:
                item.pop("body", None)
                labels.clear()
            omitted["rules_bodies"] = len(rules)
            omitted["warning"] = ("the rules alone exceed the budget, so only their titles are shown: "
                                  "read each with memory_get before you act")
        for _r, item, labels in rules:
            self.add("rules", item, labels)
        if omitted["rules"]:
            omitted["warning"] = (f"{omitted['rules']} rules did not fit in the budget at all: "
                                  "call memory_search with types=['decision'] before you act")

    def briefs(self, rows: list[dict]) -> None:
        """Pinned owner documents."""
        for r in rows:
            if r["type"] != EntryType.DOCUMENT or not r["pinned"] or r["trust"] != Trust.OWNER or r["id"] in self.used:
                continue
            if self.skip("briefs"):
                continue
            title, body, attrs = self.content(r)
            withheld = self.withheld(r)
            summary = None if withheld else self.serve(r, attrs.get("summary"))
            self.add("briefs", {"id": r["id"], "title": title, "summary": summary, "chars": len(body or "")},
                     self.labels(r, withheld))

    def relevant(self, rows: list[dict], task: str | None) -> list[dict]:
        """Keyword hits for the task, owner and node first (agent and untrusted hits go to notes).
        Without hits, the newest owner facts. Returns the hits, which order the notes."""
        hits: list[dict] = []
        if task and fts_query(task):
            fwhere, fparams = self.svc._visible_sql(self.caller, alias="e")
            hits = self.db.search(fts_query(task), fwhere, fparams, CANDIDATES)
        self.since = self.svc.clock() - self.svc.limits.recent_runs_days * DAY
        relevant = hits or sorted((r for r in rows if r["type"] == EntryType.FACT and r["trust"] == Trust.OWNER),
                                  key=lambda r: r["updated_at"], reverse=True)[:NO_TASK_FACTS]
        for r in relevant:
            if r["id"] in self.used or r["trust"] not in (Trust.OWNER, Trust.NODE) or r["type"] == EntryType.DECISION:
                continue
            if r["type"] == EntryType.RUN_SUMMARY and r["updated_at"] >= self.since:
                continue      # listed under recent_runs
            if self.skip("relevant"):
                continue
            title, body, _attrs = self.content(r)
            withheld = self.withheld(r)
            item = {"id": r["id"], "type": r["type"], "trust": r["trust"], "title": title,
                    "at": iso(r["updated_at"])}
            if withheld:
                item["withheld"] = withheld
            else:
                item["body"] = trim(body, RELEVANT_CHARS)
            self.add("relevant", item, self.labels(r, withheld))
        return hits

    def recent_runs(self, rows: list[dict]) -> None:
        """One line per recent run, newest first."""
        for r in sorted((r for r in rows if r["type"] == EntryType.RUN_SUMMARY and r["updated_at"] >= self.since),
                        key=lambda r: r["updated_at"], reverse=True):
            if r["id"] in self.used or self.skip("recent_runs"):
                continue
            _title, _body, attrs = self.content(r)
            self.add("recent_runs", {"id": r["id"], "title": r["title"], "status": attrs.get("status"),
                                     "lane": attrs.get("lane"), "at": iso(r["updated_at"])}, self.labels(r))

    def notes(self, rows: list[dict], hits: list[dict]) -> None:
        """Agent notes before untrusted ones; task hits first, then newest."""
        order = {rid: i for i, rid in enumerate(r["id"] for r in hits)}
        candidates = [r for r in rows if r["trust"] in (Trust.AGENT, Trust.UNTRUSTED) and r["id"] not in self.used]
        candidates.sort(key=lambda r: (r["trust"] == Trust.UNTRUSTED, order.get(r["id"], len(order)),
                                       -r["updated_at"]))
        for r in candidates:
            if self.skip("notes"):
                continue
            self.add("notes", *self._note(r))

    def _note(self, r: dict) -> tuple[dict, set[str]]:
        withheld = self.withheld(r)
        item = {"id": r["id"], "type": r["type"], "trust": r["trust"], "from": r["writer"],
                "at": iso(r["updated_at"])}
        if withheld:
            item.update(title=self.serve(r, r["title"]), withheld=withheld,
                        warning=WARN_SUSPICIOUS if withheld == "suspicious" else WARN_AGENT
                        if r["trust"] == Trust.AGENT else WARN_UNTRUSTED)
            return item, set()
        title, body, _attrs = self.content(r)
        data = title if not body else f"{title}: {body}"
        item.update(data=trim(data, NOTE_CHARS),
                    warning=WARN_UNTRUSTED if r["trust"] == Trust.UNTRUSTED else WARN_AGENT)
        return item, self.labels(r)

    def fit_budget(self) -> None:
        """The hard guarantee: never over the budget, dropping from the end."""
        sections = ("notes", "recent_runs", "relevant", "briefs", "rules")
        while len(json.dumps(self.pack)) > self.limit_chars:
            for section in sections:
                if self.pack[section]:
                    dropped = self.pack[section].pop()
                    self.pack["omitted"][section] += 1
                    self.items[:] = [t for t in self.items if t[1] is not dropped]
                    break
            else:
                break
