"""Sending to people: lists, suppression, the daily limit, unsubscribe and the ledger.

The hard rule (docs/WORKFLOWS.md, "Sending to people"). A list is
what the owner supplied, and only a person changes one. Anyone may suppress an address; only a person
lifts it. Only a person raises a daily limit, up to the node's ceiling. An unsubscribe token is
opaque, so a controller relaying one learns nothing about who it is for.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agentos.core.errors import InvalidRequest, NotFound, PolicyDenied
from agentos.runner.util import Clock
from agentos.runner.workflows.addresses import base_address, normal_address
from agentos.runner.workflows.store import WorkflowStore, today

DEFAULT_DAILY_LIMIT = 200
MAX_DAILY_LIMIT = 2000
MAX_LIST_SIZE = 50_000
LIST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
UNSUBSCRIBE_SUBJECT_RE = re.compile(r"unsubscribe-([A-Za-z0-9_-]{16,64})")
TOKENS_SCHEMA = """
CREATE TABLE IF NOT EXISTS send_tokens (
  token TEXT PRIMARY KEY, workspace TEXT NOT NULL, address TEXT NOT NULL, UNIQUE (workspace, address));
"""


@dataclass(frozen=True)
class Unsubscribe:
    token: str
    url: str | None
    mailto: str | None

    @property
    def link(self) -> str:
        return self.url or self.mailto or ""

    def footer(self) -> str:
        return f"\n\n--\nTo stop these messages: {self.link}"

    def headers(self) -> dict[str, str]:
        parts = [f"<{x}>" for x in (self.url, self.mailto) if x]
        out = {"List-Unsubscribe": ", ".join(parts)}
        if self.url:
            out["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        return out


class Sends:
    def __init__(self, store: WorkflowStore, clock: Clock, *, timezone: Callable[[], str | None] = lambda: None,
                 public_base: Callable[[], str | None] = lambda: None):
        self.store, self.clock = store, clock
        self.timezone, self.public_base = timezone, public_base
        with store.runner_store._lock:
            store.runner_store._db.executescript(TOKENS_SCHEMA)
        self._fold_suppressions()

    def _fold_suppressions(self) -> None:
        """A suppression is kept for the person. One kept with its tag, from before that, moves over."""
        for row in self.store._rows("SELECT workspace, address, by, why, at FROM send_suppressed WHERE address LIKE '%+%@%'"):
            base = base_address(row["address"])
            if base != row["address"]:
                self.store._exec("INSERT OR IGNORE INTO send_suppressed (workspace, address, by, why, at) VALUES (?, ?, ?, ?, ?)",
                                 (row["workspace"], base, row["by"], row["why"], row["at"]))
                self.store._exec("DELETE FROM send_suppressed WHERE workspace = ? AND address = ?", (row["workspace"], row["address"]))

    # ---- the day ----
    def _tz(self) -> Any:
        name = self.timezone()
        return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo

    def day(self) -> str:
        return today(self.clock.now(), self._tz())

    def next_day(self) -> datetime:
        """The owner's next midnight, when a used-up limit opens again."""
        local = self.clock.now().astimezone(self._tz())
        return (local + timedelta(days=1)).replace(hour=0, minute=0, second=1, microsecond=0)

    # ---- lists: only what the owner supplied ----
    def save_list(self, list_id: str | None, *, workspace: str, name: str, people: list[Any], person: bool, by: str) -> dict:
        if not person:
            raise PolicyDenied("only a person supplies a list: an agent cannot add people to send to")
        if not workspace:
            raise InvalidRequest("a list belongs to a workspace")
        if not isinstance(people, list) or len(people) > MAX_LIST_SIZE:
            raise InvalidRequest(f"a list holds at most {MAX_LIST_SIZE} people")
        if not name or len(name) > 120:
            raise InvalidRequest("a list needs a name of at most 120 characters")
        list_id = list_id or f"list.{workspace}.{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}"
        if not LIST_ID_RE.match(list_id):
            raise InvalidRequest(f"invalid list id {list_id!r}")
        known = self.store.list_info(list_id)
        if known is not None and known["workspace"] != workspace:
            raise InvalidRequest(f"{list_id} belongs to {known['workspace']}")
        rows: dict[str, tuple[str, str | None]] = {}
        bad = 0
        for item in people:
            raw = item if isinstance(item, dict) else {"email": item}
            email = raw.get("email")
            address = normal_address(email.strip() if isinstance(email, str) else email)  # a file's cell may carry spaces
            if address is None:
                bad += 1
                continue
            label = raw.get("name")
            rows.setdefault(base_address(address), (address, label[:120] if isinstance(label, str) else None))
        self.store.put_list(list_id, workspace, name, list(rows.values()), by, self.clock.now())
        return {**self.public_list(list_id), "refused": bad}

    def public_list(self, list_id: str) -> dict[str, Any]:
        """`RoutineList`: its size, never its addresses."""
        info = self.store.list_info(list_id)
        if info is None:
            raise NotFound(f"there is no list {list_id} on this node")
        return {"id": info["id"], "name": info["name"], "workspace": info["workspace"],
                "size": self.store.list_size(list_id),
                "suppressed": self.store.suppressed_on_list(list_id, info["workspace"])}

    def lists(self, workspace: str | None = None) -> list[dict[str, Any]]:
        return [self.public_list(x["id"]) for x in self.store.lists(workspace)]

    def people(self, list_id: str, *, person: bool) -> dict[str, Any]:
        """Who is on a list, for a person in person. A name that came from a lead is a stranger's text."""
        if not person:
            raise PolicyDenied("only the owner, in person, sees who is on a list")
        if self.store.list_info(list_id) is None:
            raise NotFound(f"there is no list {list_id} on this node")
        return {"list": list_id, "people": [{"email": row["address"], "name": row["name"], "lead": bool(row["lead"])}
                                            for row in self.store.people(list_id)]}

    # ---- suppression ----
    def suppress(self, workspace: str, email: Any, *, by: str, why: str | None = None) -> bool:
        address = normal_address(email)
        if address is None or not workspace:
            raise InvalidRequest("a suppression needs a workspace and an email address")
        return self.store.suppress(workspace, address, by, why, self.clock.now())

    def unsuppress(self, workspace: str, email: Any, *, person: bool) -> bool:
        if not person:
            raise PolicyDenied("only a person lifts a suppression")
        address = normal_address(email)
        if address is None:
            raise InvalidRequest("that is not an email address")
        return self.store.unsuppress(workspace, address)

    # ---- unsubscribe ----
    def unsubscribe_for(self, workspace: str, address: str, *, mailbox: str | None) -> Unsubscribe | None:
        """The link for one person, whichever tag this message used, or None when there is no way
        for it to work: a message that cannot carry an unsubscribe does not go."""
        address = base_address(address)
        rows = self.store._rows("SELECT token FROM send_tokens WHERE workspace = ? AND address = ?", (workspace, address))
        if rows:
            token = rows[0]["token"]
        else:
            token = secrets.token_urlsafe(24)
            self.store._exec("INSERT OR IGNORE INTO send_tokens (token, workspace, address) VALUES (?, ?, ?)",
                             (token, workspace, address))
            token = self.store._rows("SELECT token FROM send_tokens WHERE workspace = ? AND address = ?",
                                     (workspace, address))[0]["token"]
        base = self.public_base()
        url = f"{base.rstrip('/')}/{token}" if base else None
        mailto = f"mailto:{mailbox}?subject=unsubscribe-{token}" if mailbox else None
        return Unsubscribe(token, url, mailto) if url or mailto else None

    def unsubscribe(self, token: Any) -> bool:
        """A link was used. A token that means nothing does nothing, and says nothing."""
        if not isinstance(token, str) or not TOKEN_RE.match(token):
            return False
        rows = self.store._rows("SELECT workspace, address FROM send_tokens WHERE token = ?", (token,))
        if not rows:
            return False
        self.store.suppress(rows[0]["workspace"], rows[0]["address"], "unsubscribe", "they unsubscribed", self.clock.now())
        return True

    def unsubscribe_from_subject(self, subject: str) -> bool:
        found = UNSUBSCRIBE_SUBJECT_RE.search(subject or "")
        return bool(found) and self.unsubscribe(found.group(1))

    # ---- the daily limit ----
    def limit_of(self, account: str) -> int:
        limit = self.store.daily_limit(account)
        return DEFAULT_DAILY_LIMIT if limit is None else limit

    def set_limit(self, account: str, limit: Any, *, person: bool, by: str) -> int:
        if not person:
            raise PolicyDenied("only a person changes a daily limit")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= MAX_DAILY_LIMIT:
            raise InvalidRequest(f"a daily limit is a whole number from 0 to {MAX_DAILY_LIMIT}")
        self.store.set_daily_limit(account, limit, by, self.clock.now())
        return limit

    def limit_view(self, step_id: str, list_id: str | None, account: str) -> dict[str, Any]:
        """`RoutineSendLimit`, worked out here so the panel never guesses."""
        info = self.public_list(list_id) if list_id and self.store.list_info(list_id) else None
        return {"step": step_id, "list": list_id, "list_size": info["size"] if info else 0,
                "daily_limit": self.limit_of(account), "sent_today": self.store.sent_on(account, self.day()),
                "suppressed": info["suppressed"] if info else 0, "unsubscribe": True}
