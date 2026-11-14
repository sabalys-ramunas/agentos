"""The confirmation setting (docs/CONFIRMATIONS.md, The setting): how strong a yes must be.

One setting with a system default, read by the routing that already exists. `commit.when` IS
`[routing] ask_first_default`: there is no second switch, and the per-routine and per-account
switches of Ask me first go on working above it. A level says how strong the yes must be once
something asks; it never decides whether it asks.

The floors hold when `policy.toml` loads (a value under a floor refuses to load, like an unknown
effect, and so does turning off what cannot be turned off) and on every change by a person. `pay` and
`irreversible` always ask, at `signed_in` or above; `after_untrusted` stays at `signed_in` or above;
chat is never accepted for those. A level no ready method reaches refuses to start, or everything at
that level would wait until it expired. `requires` reads the setting once, when an approval is created,
and the approval keeps it, so a change later cannot lower a pending approval, unless a person says so
(`ApprovalStore.apply_setting_to_waiting`).

A change is stored in `policy.db`, over what `policy.toml` says, so the CLI and the panel reach a
daemon that is already running.

A controller's `config_update` is its own layer, stored apart from the node's and replaced whole by
each one. What is in force is the stricter of the two, row by row (`_with_layer`), so a controller can
only ever ask for more: what it sends cannot lower or undo what the owner set or `policy.toml` says. A
lower value is not refused, it has no effect. Only what cannot be read is refused, and logged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from agentos.core.errors import InvalidRequest
from agentos.policy.confirm import ALWAYS_ASK, LEVEL_WORDS, ConfirmLevel, MethodRegistry, default_registry
from agentos.policy.errors import Denied, MethodUnavailable
from agentos.policy.store import DB
from agentos.policy.util import iso

log = logging.getLogger("agentos.policy")

SCHEMA = """
CREATE TABLE IF NOT EXISTS confirmation_setting (
  id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL, set_by TEXT NOT NULL, set_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS confirmation_controller (
  id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL, set_by TEXT NOT NULL, set_at REAL NOT NULL
);
"""
LevelWord = Literal["chat", "signed_in", "present", "device_key"]
FLOOR = ConfirmLevel.SIGNED_IN  # pay, irreversible and what follows untrusted text: never below this, never chat
UNLISTED = ConfirmLevel.SIGNED_IN  # what asks and has no row (a command, a question): as a member's check today
ROWS = ("commit", "pay", "irreversible", "after_untrusted")
OWNER_ONLY = ("pay", "irreversible", "after_untrusted", "controller_max_level", "publish_in_chat")
Role = Literal["owner", "admin"]
OPTIONAL = frozenset(m.id for m in default_registry().all() if getattr(m, "optional", False))


class UnreachableLevel(ValueError):
    """`policy.toml` asks for a level no ready method reaches: the node refuses to start."""


def _level_at_floor(row: str, word: str) -> str:
    if ConfirmLevel.of(word) < FLOOR:
        raise ValueError(f"{row} never goes below {FLOOR.word}, and is never answered in chat")
    return word


def _only_optional(methods: list[str]) -> list[str]:
    for method in methods:
        if method not in OPTIONAL:
            raise ValueError(f"{method[:40]!r} cannot be turned off, so that a person can always decide")
    return methods


class CommitRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    when: Literal["auto", "ask"] = "auto"
    level: LevelWord = "chat"


class AskRow(BaseModel):
    """A row that always asks. `when` is a floor, not a default a setting can lower."""
    model_config = ConfigDict(extra="forbid")
    when: str = "ask"
    level: LevelWord = "present"

    @field_validator("when")
    @classmethod
    def _always(cls, v: str) -> str:
        if v != "ask":
            raise ValueError("pay and irreversible always ask: their when cannot be changed")
        return v

    @field_validator("level")
    @classmethod
    def _floor(cls, v: str) -> str:
        return _level_at_floor("pay and irreversible", v)


class UntrustedRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: LevelWord = "signed_in"

    @field_validator("level")
    @classmethod
    def _floor(cls, v: str) -> str:
        return _level_at_floor("after_untrusted", v)


class LayerRow(BaseModel):
    """A row as a controller sends it: what it names and nothing else. No floor here, because a value
    in the layer can only add: a lower one has no effect."""
    model_config = ConfigDict(extra="forbid")
    when: Literal["auto", "ask"] | None = None
    level: LevelWord | None = None


class WorkspaceRows(BaseModel):
    """What holds in one workspace. It only ever adds to the node's rows: see `requires` and `asks_in`,
    so a workspace cannot ask for less than the node does."""
    model_config = ConfigDict(extra="forbid")
    commit: CommitRow | None = None
    pay: AskRow | None = None
    irreversible: AskRow | None = None
    after_untrusted: UntrustedRow | None = None
    methods_off: list[str] = Field(default_factory=list)

    @field_validator("methods_off")
    @classmethod
    def _off(cls, v: list[str]) -> list[str]:
        return _only_optional(v)


class ConfirmationConfig(BaseModel):
    """`[confirmation]` in policy.toml. The rows of docs/CONFIRMATIONS.md, the methods turned off, and
    the most the node believes of a controller's proof (never set by a controller)."""
    model_config = ConfigDict(extra="forbid")
    commit: CommitRow = Field(default_factory=CommitRow)
    pay: AskRow = Field(default_factory=AskRow)
    irreversible: AskRow = Field(default_factory=AskRow)
    after_untrusted: UntrustedRow = Field(default_factory=UntrustedRow)
    methods_off: list[str] = Field(default_factory=list)
    controller_max_level: LevelWord = "present"
    # the owner's choice, off by default: a publish the commit gate caught can be answered in chat though the
    # agent read a page first. Off, because the chat client is the agent's own: an agent with a shell can open
    # its own MCP session and answer its own prompt, and no check on the node tells it from a person
    # (docs/CONFIRMATIONS.md, docs/THREAT-MODEL.md)
    publish_in_chat: bool = False
    workspaces: dict[str, WorkspaceRows] = Field(default_factory=dict)

    @field_validator("methods_off")
    @classmethod
    def _off(cls, v: list[str]) -> list[str]:
        return _only_optional(v)

    def off_in(self, workspace: str | None) -> list[str]:
        """The ways to confirm turned off where an approval lives: the node's, and its workspace's."""
        rows = self.workspaces.get(workspace or "")
        return sorted({*self.methods_off, *(rows.methods_off if rows is not None else [])})

    def asks_in(self, workspace: str | None) -> bool:
        """`commit.when` for a workspace: the node's, or its own where that asks."""
        rows = self.workspaces.get(workspace or "")
        return self.commit.when == "ask" or (rows is not None and rows.commit is not None and rows.commit.when == "ask")

    def publish_takes_chat(self, workspace: str | None = None) -> bool:
        """Is a publish let off `after_untrusted`? Only as long as nobody asked for more there."""
        rows = self.workspaces.get(workspace or "")
        asked = [self.after_untrusted, *([rows.after_untrusted] if rows is not None and rows.after_untrusted else [])]
        return self.publish_in_chat and all(ConfirmLevel.of(row.level) <= FLOOR for row in asked)

    def requires(self, effect: str | None, *, untrusted: bool = False, floor: ConfirmLevel | None = None,
                 workspace: str | None = None, publish: bool = False) -> ConfirmLevel:
        """What an approval for `effect` requires: the strictest thing that applies. `untrusted`: the
        call follows untrusted text (the downgrade or the trifecta). `floor`: its caller's own minimum.
        `publish`: a publish the commit gate caught, past no trifecta, which `publish_takes_chat` may let off.
        A question has no effect, and no row."""
        row = {"commit": self.commit, "pay": self.pay, "irreversible": self.irreversible}.get(effect or "")
        level = ConfirmLevel.of(row.level) if row is not None else UNLISTED
        if effect in ALWAYS_ASK:
            level = max(level, FLOOR)
        if publish and effect == "commit" and self.publish_takes_chat(workspace):
            untrusted = False
        if untrusted:
            level = max(level, FLOOR, ConfirmLevel.of(self.after_untrusted.level))
        rows = self.workspaces.get(workspace or "")
        if rows is not None:
            mine = {"commit": rows.commit, "pay": rows.pay, "irreversible": rows.irreversible}.get(effect or "")
            if mine is not None:
                level = max(level, ConfirmLevel.of(mine.level))
            if untrusted and rows.after_untrusted is not None:
                level = max(level, ConfirmLevel.of(rows.after_untrusted.level))
            if "chat" in rows.methods_off:
                level = max(level, FLOOR)
        return max(level, floor) if floor is not None else level

    def public(self) -> dict[str, Any]:
        return self.model_dump()

    def enforced(self) -> dict[str, Any]:
        """What the inventory reports (docs/PROTOCOL.md): the rows in force, metadata only."""
        return self.model_dump(include={*ROWS, "methods_off"})


def _raise_row(row: dict[str, Any], adds: dict[str, Any]) -> None:
    """The stricter of a row and what a layer adds to it: `ask` if either asks, the higher level."""
    if adds.get("when") == "ask":
        row["when"] = "ask"
    if "level" in adds and ConfirmLevel.of(adds["level"]) > ConfirmLevel.of(row["level"]):
        row["level"] = adds["level"]


def _with_layer(own: ConfirmationConfig, layer: dict[str, Any]) -> ConfirmationConfig:
    """What is in force: the node's setting, and on top of it what a controller asks for. Nothing here
    can lower a row, so nothing a controller sends undoes what the node holds."""
    merged = own.model_dump()
    for key in ROWS:
        _raise_row(merged[key], layer.get(key, {}))
    merged["methods_off"] = sorted({*merged["methods_off"], *layer.get("methods_off", [])})
    merged["publish_in_chat"] = merged["publish_in_chat"] and layer.get("publish_in_chat", True)
    for name, adds in layer.get("workspaces", {}).items():
        rows = merged["workspaces"].setdefault(name, {"methods_off": []})
        for key in ROWS:
            if key in adds and rows.get(key):
                _raise_row(rows[key], adds[key])
            elif key in adds:
                rows[key] = adds[key]
        rows["methods_off"] = sorted({*rows["methods_off"], *adds.get("methods_off", [])})
    return ConfirmationConfig.model_validate(merged)


class ConfirmationStore:
    """The setting in force: `policy.toml`, what a person changed over it, and what a controller asks
    for on top. Runtime service "confirmation"."""

    def __init__(self, db: DB, *, base: ConfirmationConfig, clock: Callable[[], float],
                 methods: MethodRegistry | None = None):
        self.db = db
        self.base = base
        self.clock = clock
        self.methods = methods or default_registry()
        with db.lock:
            db.conn.executescript(SCHEMA)
        self._check_reachable()

    def _check_reachable(self) -> None:
        """What `policy.toml` asks for, against the methods this node has. A person's change is checked
        as it is made, and a controller's as it arrives."""
        rows = [(key, getattr(self.base, key)) for key in ROWS]
        for name, workspace in self.base.workspaces.items():
            rows += [(f"{key} in {name}", getattr(workspace, key)) for key in ROWS if getattr(workspace, key) is not None]
        for where, row in rows:
            if ConfirmLevel.of(row.level) > self.methods.highest_ready():
                raise UnreachableLevel(f"policy.toml asks for {row.level} for {where}, and no way to confirm on this "
                                       f"node reaches {row.level}, so all of it would wait until it expired. "
                                       "Lower it in policy.toml.")

    def _stored(self) -> dict[str, Any]:
        row = self.db.one("SELECT body FROM confirmation_setting WHERE id = 1")
        try:
            body = json.loads(row["body"]) if row is not None else {}
        except ValueError:
            log.error("the stored confirmation setting cannot be read; using policy.toml")
            return {}
        return body if isinstance(body, dict) else {}

    def _merged(self, stored: dict[str, Any]) -> ConfirmationConfig:
        merged = self.base.model_dump()
        for key, value in stored.items():
            if key in ROWS and isinstance(value, dict) and key in merged:
                merged[key].update(value)
            else:
                merged[key] = value
        # what policy.toml says of after_untrusted holds: a file raised later is not undone by an older change
        if ConfirmLevel.of(self.base.after_untrusted.level) > ConfirmLevel.of(merged["after_untrusted"]["level"]):
            merged["after_untrusted"]["level"] = self.base.after_untrusted.level
        return ConfirmationConfig.model_validate(merged)

    def own(self) -> ConfirmationConfig:
        """The node's own setting, with no controller in it: what a person changes. A stored value
        under a floor (a hand-edited row) is never in force."""
        try:
            return self._merged(self._stored())
        except (ValidationError, ValueError, KeyError) as exc:
            log.error("the stored confirmation setting breaks a floor; using policy.toml (%s)", type(exc).__name__)
            return self.base

    def _layer(self) -> dict[str, Any]:
        row = self.db.one("SELECT body FROM confirmation_controller WHERE id = 1")
        try:
            body = json.loads(row["body"]) if row is not None else {}
        except ValueError:
            log.error("the controller's confirmation layer cannot be read; using the node's own setting")
            return {}
        return body if isinstance(body, dict) else {}

    def current(self) -> ConfirmationConfig:
        """What is in force, read again on every use, so a change reaches a daemon that is already
        running: the stricter of the node's own setting and a controller's layer, row by row."""
        own, layer = self.own(), self._layer()
        if not layer:
            return own
        try:
            return _with_layer(own, layer)
        except (ValidationError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.error("the controller's confirmation layer cannot be used; ignoring it (%s)", type(exc).__name__)
            return own

    def ask_first_default(self, workspace: str | None = None) -> bool:
        """`commit.when`: "Ask me first" for a routine or account nobody set."""
        return self.current().asks_in(workspace)

    # ---- changing it ----
    def change(self, patch: dict[str, Any], *, by: str, role: Role) -> ConfirmationConfig:
        """A person's change, whole or not at all: a value under a floor refuses the change. `role`:
        `owner` may change every row; `admin` only `commit` (and may turn a method off)."""
        if not isinstance(patch, dict) or not patch:
            raise InvalidRequest("name what to change")
        with self.db.tx():  # checked against what the node holds at this moment, across processes
            now = self.own()
            for key, value in patch.items():
                self._check(key, value, now, role)
            self._write(patch, by=by)
        return self.current()

    # ---- a controller's layer ----
    def apply_controller(self, patch: Any, *, by: str) -> dict[str, list[dict[str, str]]]:
        """A controller's `config_update`: its layer, replaced whole. `None` drops it. Whatever is in it
        can only ask for more, so only what cannot be read is refused, field by field, and left out of
        the layer. `controller_max_level` is no part of it: the limit is the owner's, on the node."""
        if patch is None:
            self.clear_controller()
            return {"applied": [], "refused": []}
        if not isinstance(patch, dict):
            return {"applied": [], "refused": [{"field": "confirmation", "why": "not an object"}]}
        layer: dict[str, Any] = {}
        refused: list[dict[str, str]] = []
        for key, value in patch.items():
            try:
                layer[key] = self._read_layer(key, value)
            except (InvalidRequest, MethodUnavailable) as exc:
                refused.append({"field": str(key)[:40], "why": exc.message[:200]})
        self.db.execute("INSERT INTO confirmation_controller (id, body, set_by, set_at) VALUES (1, ?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET body = excluded.body, set_by = excluded.set_by, "
                        "set_at = excluded.set_at", (json.dumps(layer, sort_keys=True), by[:120], self.clock()))
        return {"applied": [{"field": k} for k in layer], "refused": refused}

    def clear_controller(self) -> None:
        """Drop the layer. What the node holds on its own is in force again."""
        self.db.execute("DELETE FROM confirmation_controller")

    def _read_layer(self, key: str, value: Any) -> Any:
        if key == "publish_in_chat":
            if not isinstance(value, bool):
                raise InvalidRequest("publish_in_chat is true or false")
            return value
        if key == "methods_off":
            self._check_off(value, ConfirmationConfig(), "owner")
            return sorted(set(value))
        if key == "workspaces":
            if not isinstance(value, dict) or not all(isinstance(rows, dict) for rows in value.values()):
                raise InvalidRequest("workspaces maps a workspace to its rows")
            return {str(name): self._read_workspace(str(name), rows) for name, rows in value.items()}
        if key not in ROWS:
            raise InvalidRequest(f"a controller does not send {str(key)[:40]!r}")
        return self._adds(key, value)

    def _read_workspace(self, name: str, rows: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in rows.items():
            if key not in (*ROWS, "methods_off"):
                raise InvalidRequest(f"{name[:40]} has no row {str(key)[:40]!r}")
            read = self._read_layer(key, value)
            if read:
                out[key] = read
        return out

    def _adds(self, key: str, value: Any) -> dict[str, Any]:
        """What this row can add, and only that: a row's default never comes in with a part of it. A
        level nothing reaches is not taken, or a controller could leave every approval waiting."""
        try:
            row = LayerRow.model_validate(value)
        except ValidationError as exc:
            raise InvalidRequest(f"{key}: {exc.errors()[0]['msg']}") from None
        out: dict[str, Any] = {}
        if key == "commit" and row.when == "ask":
            out["when"] = "ask"
        if row.level is not None:
            level = ConfirmLevel.of(row.level)
            if level > self.methods.highest_ready():
                raise MethodUnavailable(f"no way to confirm on this node reaches {level.word}")
            if key == "commit" or level >= FLOOR:
                out["level"] = row.level
        return out

    def _check(self, key: str, value: Any, now: ConfirmationConfig, role: Role) -> None:
        if key == "methods_off":
            self._check_off(value, now, role)
            return
        if key == "workspaces":
            self._check_workspaces(value, now, role)
            return
        if key == "publish_in_chat":
            if role != "owner":
                raise Denied("owner_only", "only the owner changes publish_in_chat")
            if not isinstance(value, bool):
                raise InvalidRequest("publish_in_chat is true or false")
            return
        if key == "controller_max_level":
            if role != "owner":
                raise Denied("owner_only", "the limit on a controller is set by the owner, on the node")
            if value not in LEVEL_WORDS:
                raise InvalidRequest(f"a level is one of {', '.join(LEVEL_WORDS)}")
            return
        if key not in ROWS:
            raise InvalidRequest(f"there is no row {str(key)[:40]!r}: rows are {', '.join(ROWS)}")
        if role == "admin" and key in OWNER_ONLY:
            raise Denied("owner_only", f"only the owner changes {key}")
        if not isinstance(value, dict) or not value:
            raise InvalidRequest(f"{key} takes when or level")
        row = type(getattr(now, key))
        try:
            checked = row.model_validate({**getattr(now, key).model_dump(), **value})
        except ValidationError as exc:
            problem = exc.errors()[0]
            floor = problem["type"] == "value_error"
            raise (Denied("floor", problem["msg"].removeprefix("Value error, ")) if floor
                   else InvalidRequest(f"{key}: {problem['msg']}")) from None
        level = ConfirmLevel.of(checked.level)
        if key == "after_untrusted" and level < ConfirmLevel.of(self.base.after_untrusted.level):
            raise Denied("floor", f"policy.toml holds after_untrusted at {self.base.after_untrusted.level}: lower it there")
        if level > self.methods.highest_ready():
            raise MethodUnavailable(f"no way to confirm on this node reaches {level.word}, so nothing could be "
                                    "approved at that level yet")

    def _check_off(self, value: Any, now: ConfirmationConfig, role: Role) -> None:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise InvalidRequest("methods_off is a list of method ids")
        for method in value:
            known = next((m for m in self.methods.all() if m.id == method), None)
            if known is None or not getattr(known, "optional", False):
                raise InvalidRequest(f"{method[:40]!r} cannot be turned off")
        if role != "owner" and set(now.methods_off) - set(value):
            raise Denied("owner_only", "only the owner turns a way to confirm back on")

    def _check_workspaces(self, value: Any, now: ConfirmationConfig | None = None, role: Role = "owner") -> None:
        try:
            rows = TypeAdapter(dict[str, WorkspaceRows]).validate_python(value)
        except ValidationError as exc:
            raise InvalidRequest(f"workspaces: {exc.errors()[0]['msg']}") from None
        if role != "owner" and now is not None:  # the list is replaced whole: a workspace left out counts too
            for name, held in now.workspaces.items():
                new = rows.get(name)
                if set(held.methods_off) - set(new.methods_off if new is not None else []):
                    raise Denied("owner_only", "only the owner turns a way to confirm back on")
        for name, row in rows.items():
            self._check_off(row.methods_off, ConfirmationConfig(), "owner")
            for part in (row.commit, row.pay, row.irreversible, row.after_untrusted):
                if part is not None and ConfirmLevel.of(part.level) > self.methods.highest_ready():
                    raise MethodUnavailable(f"no way to confirm on this node reaches {part.level}, in {name[:40]}")

    def _write(self, patch: dict[str, Any], *, by: str) -> None:
        stored = self._stored()
        for key, value in patch.items():
            stored[key] = {**stored.get(key, {}), **value} if key in ROWS else value
        self._merged(stored)  # the whole result holds every floor, or nothing is written
        self.db.execute("INSERT INTO confirmation_setting (id, body, set_by, set_at) VALUES (1, ?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET body = excluded.body, set_by = excluded.set_by, "
                        "set_at = excluded.set_at", (json.dumps(stored, sort_keys=True), by[:120], self.clock()))

    # ---- what people read ----
    def public(self) -> dict[str, Any]:
        row = self.db.one("SELECT set_by, set_at FROM confirmation_setting WHERE id = 1")
        return {**self.current().public(), "defaults": ConfirmationConfig().public(),
                "set_by": row["set_by"] if row is not None else None,
                "set_at": iso(row["set_at"]) if row is not None else None,
                "can_turn_off": [m.id for m in self.methods.all() if getattr(m, "optional", False)],
                "controller": self._layer_public(),
                # the node's own workspace rows, with no controller in them: what a change of one
                # workspace starts from, since a change of `workspaces` replaces the whole map
                "own_workspaces": {name: {k: v for k, v in rows.items() if v is not None}
                                   for name, rows in self.own().model_dump()["workspaces"].items()}}

    def _layer_public(self) -> dict[str, Any] | None:
        """What a controller asks for, apart from what the node holds: the rows above are the two together."""
        row = self.db.one("SELECT set_by, set_at FROM confirmation_controller WHERE id = 1")
        return {**self._layer(), "set_by": row["set_by"], "set_at": iso(row["set_at"])} if row is not None else None

    def methods_public(self) -> list[dict[str, Any]]:
        off = set(self.current().methods_off)
        return [{"id": m.id, "label": m.label, "level": m.level.word, "status": m.status, "on": m.id not in off,
                 "can_turn_off": bool(getattr(m, "optional", False))} for m in self.methods.all()]
