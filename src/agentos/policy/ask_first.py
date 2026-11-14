""""Ask me first": the per-routine and per-account switch that makes a commit wait for a person.

Routines run on their own. Each saved flow id (scope `flow`) and each lane or connector surface
(scope `lane`, for what an agent does there outside a routine) has a switch; no row means off. A
commit asks first when the switch that covers it is on (ROUTING.md, "Ask me first"; the channel's
`ask_first`, docs/PROTOCOL.md). Pay and irreversible never read it: they always ask.

Who may change it: anyone may turn it on (stricter); only a person turns it off. `set` takes
`person=` from the caller's layer (the CLI, the signed-in panel, the owner token, a controller
relaying a person), and an agent's off is refused. The setting follows the flow id across versions
and outlives a deleted flow, so an edit or a re-save never sheds a person's "ask me first". A new
routine an agent saves on a lane where a person set a routine to ask first starts on, with its
`why` (FlowService.save), so a copy under a new id does not shed it either.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentos.core.errors import InvalidRequest, PolicyDenied
from agentos.core.models import LANE_ID_RE
from agentos.policy.store import DB
from agentos.policy.util import iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS ask_first (
  scope TEXT NOT NULL, id TEXT NOT NULL, ask_first INTEGER NOT NULL,
  set_by TEXT NOT NULL, by_person INTEGER NOT NULL, set_at REAL NOT NULL, why TEXT,
  PRIMARY KEY (scope, id)
)
"""
SCOPES = ("flow", "lane")
AGENT_ENV = ("AGENTOS_BRAIN_TASK", "AGENTOS_LANE")  # set on what a brain or a lane runs (agentos.plugins.admin)
FLOW_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,250}$")
MAX_BY = 80  # of a controller's `by`
MAX_SET_BY = 120  # `controller:<by>` and the like


@dataclass(frozen=True)
class Setting:
    scope: str
    id: str
    ask_first: bool
    set_by: str
    by_person: bool
    set_at: float
    why: str | None = None  # the node set it for a reason of its own (a new routine next to a person's)

    def public(self) -> dict[str, Any]:
        """`Setting { scope, id, ask_first, set_by, by_person, set_at, why? }` (docs/PROTOCOL.md)."""
        return {"scope": self.scope, "id": self.id, "ask_first": self.ask_first, "set_by": self.set_by,
                "by_person": self.by_person, "set_at": iso(self.set_at), **({"why": self.why} if self.why else {})}


def inside_an_agent(env: Mapping[str, str] | None = None) -> bool:
    """Did a brain task or a lane start this process? For the CLI, whose caller is a person otherwise.
    It stops an agent that simply runs the command, and not one that clears the variable first."""
    env = os.environ if env is None else env
    return any(env.get(name) for name in AGENT_ENV)


def check_id(scope: Any, ident: Any) -> tuple[str, str]:
    """(scope, id) when they are well formed, else InvalidRequest."""
    if scope not in SCOPES:
        raise InvalidRequest("scope must be flow or lane")
    if not isinstance(ident, str):
        raise InvalidRequest(f"a {scope} id is a string")
    if scope == "flow" and not FLOW_ID_RE.match(ident):
        raise InvalidRequest(f"{ident[:80]!r} is not a flow id (lowercase letters, digits, _ . -)")
    if scope == "lane" and not LANE_ID_RE.match(ident):
        raise InvalidRequest(f"{ident[:80]!r} is not a lane id (workspace/name)")
    return scope, ident


def flow_of_target(target: str | None) -> str | None:
    """The flow id a call's target names, or None: 'flow:biz-a.post@abc' -> 'biz-a.post', and an
    installed operation 'connector:instagram.post@abc' -> 'connector.instagram.post' (its flow id)."""
    for prefix, flow_prefix in (("flow:", ""), ("connector:", "connector.")):
        if target and target.startswith(prefix) and "@" in target:
            name = target[len(prefix):].rsplit("@", 1)[0]
            return flow_prefix + name if name else None
    return None


class AskFirst:
    """The switches, in the policy database. Runtime service "ask_first"."""

    def __init__(self, db: DB, *, clock: Callable[[], float], default: bool | Callable[[], bool] = False):
        self.db = db
        self.clock = clock
        self._default = default  # a switch nobody set: the setting's commit.when, read at each use
        db.execute(SCHEMA)
        if not any(c["name"] == "why" for c in db.all("PRAGMA table_info(ask_first)")):
            db.execute("ALTER TABLE ask_first ADD COLUMN why TEXT")

    @property
    def default(self) -> bool:
        return self._default() if callable(self._default) else self._default

    def default_for(self, scope: str, ident: str) -> bool:
        """A switch nobody set: the node's default, or its workspace's where that asks. An account's id
        names its workspace."""
        if scope == "lane" and callable(self._default):
            return self._default(ident.partition("/")[0])
        return self.default

    def get(self, scope: str, ident: str) -> Setting | None:
        row = self.db.one("SELECT * FROM ask_first WHERE scope = ? AND id = ?", (scope, ident))
        return _setting(row) if row is not None else None

    def chosen_off(self, scope: str, ident: str) -> bool:
        """Did a person choose "Runs on its own" here? No row means nobody chose, whatever the node's
        default says, and only a person's off is ever stored (`set`), so this is the one reading of a
        row that lets the worker's own words go without asking."""
        s = self.get(scope, ident)
        return s is not None and not s.ask_first and s.by_person

    def is_on(self, scope: str, ident: str | None) -> bool:
        if not ident:
            return False
        s = self.get(scope, ident)
        return s.ask_first if s is not None else self.default_for(scope, ident)

    def list(self, scope: str | None = None) -> list[Setting]:
        if scope is None:
            rows = self.db.all("SELECT * FROM ask_first ORDER BY scope, id")
        else:
            rows = self.db.all("SELECT * FROM ask_first WHERE scope = ? ORDER BY id", (scope,))
        return [_setting(r) for r in rows]

    def set(self, scope: str, ident: str, on: bool, *, by: str, person: bool, why: str | None = None) -> Setting:
        """Turn a switch on or off. Off needs a person: an agent's off is refused when the switch is
        on, and is a no-op (nothing recorded) when it is already off."""
        scope, ident = check_id(scope, ident)
        if not isinstance(on, bool):
            raise InvalidRequest("ask_first is true or false")
        by = (str(by) or "unknown")[:MAX_SET_BY]
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM ask_first WHERE scope = ? AND id = ?", (scope, ident)).fetchone()
            current = _setting(row) if row is not None else None
            if not on and not person:
                if (current.ask_first if current is not None else self.default_for(scope, ident)):
                    raise PolicyDenied(f"only a person turns Ask me first off ({scope} {ident}); ask the owner to "
                                       "turn it off in the panel")
                return current or Setting(scope, ident, False, "default", False, 0.0)
            now = self.clock()
            conn.execute("INSERT INTO ask_first (scope, id, ask_first, set_by, by_person, set_at, why) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(scope, id) DO UPDATE SET "
                         "ask_first = excluded.ask_first, set_by = excluded.set_by, "
                         "by_person = excluded.by_person, set_at = excluded.set_at, why = excluded.why",
                         (scope, ident, int(on), by, int(person), now, why))
        return Setting(scope, ident, on, by, person, now, why)

    def for_call(self, flow_id: str | None, lane_id: str | None, through: str | None = None) -> str | None:
        """What makes a commit ask first, in words, or None: the routine's switch, else the
        account's, else that of the lane a connector surface posts through (`through`). A commit on
        no lane is decided by the routing table (it always asks)."""
        if flow_id and self.is_on("flow", flow_id):
            return f"routine {flow_id}"
        for lane in (lane_id, through):
            if lane and self.is_on("lane", lane):
                return f"account {lane}"
        return None

    def flow_view(self, flow_id: str) -> dict[str, Any]:
        """`{ ask_first, ask_first_by }` for a flow in the inventory and the panel."""
        s = self.get("flow", flow_id)
        if s is None:  # nobody set it: the node's default
            return {"ask_first": self.default, "ask_first_by": None}
        return {"ask_first": s.ask_first, "ask_first_by": "person" if s.by_person else "agent",
                **({"ask_first_why": s.why} if s.why else {})}


def open_store(home: Path) -> AskFirst:
    """The switches in `$AGENTOS_HOME/policy.db`, without the rest of the policy service (the CLI)."""
    from agentos.policy.confirmation import ConfirmationStore
    from agentos.policy.routing import load_policy_config

    db = DB(Path(home) / "policy.db", SCHEMA)
    setting = ConfirmationStore(db, base=load_policy_config(Path(home)).confirmation, clock=time.time)
    return AskFirst(db, clock=time.time, default=setting.ask_first_default)


def _setting(row: Any) -> Setting:
    return Setting(scope=row["scope"], id=row["id"], ask_first=bool(row["ask_first"]), set_by=row["set_by"],
                   by_person=bool(row["by_person"]), set_at=float(row["set_at"]), why=row["why"])
