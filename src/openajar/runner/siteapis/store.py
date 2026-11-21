"""Site API drafts, in runner.db beside the flows an activated one becomes.

A row is one API from its first draft to its removal. `definition` is the working copy; `installed`
is what is live (`{version, digest, endpoints: {name: implementation hash}}`), so a person can edit
an active API without touching what runs, until they activate the edit. `person`: a person wrote or
vouched for this content, so its `publishes: false` marks and unmarked fields count. A draft holds a
definition and nothing a page said beyond locators: never a cookie, a token or a run's output.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openajar.core import migrate
from openajar.core.errors import ServiceUnavailable
from openajar.runner.siteapis.migrations import MIGRATIONS, SCHEMA_V1
from openajar.runner.store import RunnerStore
from openajar.runner.util import iso


@dataclass(slots=True)
class Draft:
    api: str
    state: str
    definition: dict[str, Any]
    digest: str
    person: bool
    author: dict[str, Any]
    proposal: dict[str, Any] | None
    installed: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class DraftStore:
    def __init__(self, store: RunnerStore):
        self._exec, self._rows = store._exec, store._rows
        if str(store.path) in (":memory:", "memory"):
            self._exec(SCHEMA_V1)
            return
        try:  # the file may be named otherwise in a test; the set is the same
            migrate.migrate(Path(store.path).parent, [dataclasses.replace(MIGRATIONS, db=Path(store.path).name)], backup=False)
        except migrate.MigrationError as e:
            raise ServiceUnavailable(f"cannot open site API drafts: {e}") from None

    @staticmethod
    def _draft(row: Any) -> Draft:
        return Draft(api=row["api"], state=row["state"], definition=json.loads(row["definition"]), digest=row["digest"],
                     person=bool(row["person"]), author=json.loads(row["author"]),
                     proposal=json.loads(row["proposal"]) if row["proposal"] else None,
                     installed=json.loads(row["installed"]) if row["installed"] else None,
                     created_at=datetime.fromisoformat(row["created_at"]), updated_at=datetime.fromisoformat(row["updated_at"]))

    def get(self, api: str) -> Draft | None:
        rows = self._rows("SELECT * FROM site_api_drafts WHERE api=?", (api,))
        return self._draft(rows[0]) if rows else None

    def list(self) -> list[Draft]:
        return [self._draft(r) for r in self._rows("SELECT * FROM site_api_drafts ORDER BY api")]

    def put(self, d: Draft) -> None:
        self._exec("INSERT INTO site_api_drafts (api, state, definition, digest, person, author, proposal, installed, "
                   "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(api) DO UPDATE SET "
                   "state=excluded.state, definition=excluded.definition, digest=excluded.digest, person=excluded.person, "
                   "author=excluded.author, proposal=excluded.proposal, installed=excluded.installed, "
                   "updated_at=excluded.updated_at",
                   (d.api, d.state, json.dumps(d.definition, sort_keys=True), d.digest, int(d.person), json.dumps(d.author),
                    json.dumps(d.proposal) if d.proposal else None, json.dumps(d.installed) if d.installed else None,
                    iso(d.created_at), iso(d.updated_at)))

    def delete(self, api: str) -> bool:
        return self._exec("DELETE FROM site_api_drafts WHERE api=?", (api,)).rowcount > 0
