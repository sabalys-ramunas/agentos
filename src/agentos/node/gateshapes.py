"""The owner's gate log (docs/design/egress-guard.md 8.9): the request shapes the commit gate
classified without a rule, and the shapes the owner promoted from it.

While an agent drives a lane, the commit gate classifies every non-GET on a rule host fail closed
(egress.LaneGuard._gate_rule) and traces each shape no rule lists as `gate_classified` in the
lane's egress trace (host, path, method, an operation name and field names, never a value).
`log()` groups those rows by shape (host, path, method, operation) with counts, how each was
classified and why, so the owner can see what the gate held that publishes nothing (a site's own
logging, a read no rule names) and what it let go.

The owner promotes a shape (`agentos lane gate-log <lane> --allow-read <shape-id>` or
`--as-commit <name> --shape <shape-id>`, or POST /api/lanes/{ws}/{name}/gate-log):

Each promotion holds for exactly the host, path and method the shape was seen on, and on the lane
it was promoted from (`--all-lanes`: every lane of the node):

- to the read allowlist: on a GraphQL endpoint (a strict_ops read rule's), its operation becomes a
  known read on that endpoint, with the doc_ids it was seen with (a batch is still a read only when every operation in it is one; a
  request with no operation name cannot be promoted, it cannot be told from a publish); elsewhere a
  read rule for exactly that host, path and method (and operation, when it has one).
- to a named commit rule (`<site>.<kind>[.<detail>]`, kind one of egress.EFFECT_KINDS): the gate
  then holds it under that name instead of `<site>.unknown`. It can only make more requests wait
  for a person, never fewer. A shape only observe mode saw (`observed_request` rows no commit or
  write rule matched: a publish the gate takes for a read, or one a person made by hand while the
  owner observed) can be named this way too (`log(observed=True)`, `gate-log --observed`), never
  promoted to a read.

A read promotion never outranks a built-in rule: read rules come after the built-in ones, and a
promoted operation name counts as a read only on the catch-all it was seen on, which the built-in
commit and write rules of that endpoint come before. So no promotion turns a built-in commit or
write into a read (docs/design/egress-guard.md 8.12).

They are stored per node, not in code (GATE_SHAPES in AGENTOS_HOME, mode 600), each with who
promoted it and when, and every change writes an audit row to the action log (`gate_shape`). Every
lane's guard reads the file when it starts and again when it changes (`ShapeSource`, checked at most
every egress.SITES_TTL_S). Only the owner promotes: the node token or the owner's own panel
session; a member or a grant is refused (agentos.node.viewer_routes.gate_log_api_routes). Field
names are shown so the owner can judge a shape; they are not matched (optional fields come and go).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.core.errors import InvalidRequest, NotFound
from agentos.node import egress

_log = logging.getLogger(__name__)

GATE_SHAPES = "gate-shapes.json"
KEPT = 500  # promoted shapes a node keeps (a promotion past it is refused)
LOG_ROWS = 20000  # trace rows read for a gate log (the trace rolls at 4 MB)


def shape_id(host: str, path: str, method: str, op: str | None, doc_ids: Iterable[str] = ()) -> str:
    """A shape's stable id: its host, path, method, operation name and the doc_ids it names (Meta's
    persisted queries: one name with another doc_id is another shape)."""
    key: list[Any] = [host.lower(), path, method.upper(), op or ""]
    if doc_ids:
        key.append(sorted(set(doc_ids)))
    raw = json.dumps(key, separators=(",", ":"))
    return "sh_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def _at(row: dict[str, Any]) -> float | None:
    with contextlib.suppress(TypeError, ValueError):
        return datetime.fromisoformat(str(row.get("at"))).timestamp()
    return None


def trace_rows(lane_dir: Path) -> list[dict[str, Any]]:
    """The lane's egress trace, the rolled file first."""
    rows: list[dict[str, Any]] = []
    for name in (egress.TRACE_NAME + ".1", egress.TRACE_NAME):
        rows += egress.EgressTrace(lane_dir / name).read(limit=LOG_ROWS)
    return rows


def _observed_unruled(row: dict[str, Any]) -> bool:
    """An `observed_request` row (observe mode) that no commit or write rule matched: a read rule's
    request (a GraphQL catch-all, the plumbing list) or one no rule names."""
    return row.get("kind") == "observed_request" and row.get("effect") not in ("commit", "write")


def log(rows: Iterable[dict[str, Any]], *, since: float | None = None,
        promoted: Iterable[dict[str, Any]] = (), observed: bool = False) -> list[dict[str, Any]]:
    """The `gate_classified` rows grouped by shape, most seen first: id, host, path, method, op, the
    field names (every name any row had, in order), how many field sets (`mixed` when more than one:
    several bodies on one path), `partial` when a row's names were not all of its body's, `decisions` and `why`
    with counts, the rule a GraphQL catch-all named, first and last seen, `count` (rows: one per
    shape a second at most) and `promoted` (read, or the commit rule's id) when it was.

    `observed`: the shapes observe mode traced that no commit or write rule matched are listed too
    (decision `observed`, `observed_only` when the gate never classified the shape). That is how the
    owner names a publish the gate takes for a read (a new mutation named like a query): such a
    shape can be promoted to a named commit rule, never to a read."""
    done = {p["id"]: p for p in promoted}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        seen = observed and _observed_unruled(row)
        if row.get("kind") != "gate_classified" and not seen:
            continue
        if seen:
            row = {**row, "decision": "observed", "why": "observe mode: no commit or write rule matched",
                   "rule": row.get("effect_rule")}
        at = _at(row)
        if since is not None and (at is None or at < since):
            continue
        shape = row.get("shape") if isinstance(row.get("shape"), dict) else {}
        host, path, method = str(row.get("host") or "?"), str(row.get("path") or "/"), str(row.get("method") or "?")
        op = shape.get("op") if isinstance(shape.get("op"), str) else None
        doc_ids = sorted({d for d in shape.get("doc_ids") or [] if isinstance(d, str)})
        sid = shape_id(host, path, method, op, doc_ids)
        entry = out.setdefault(sid, {"id": sid, "host": host, "path": path, "method": method, "op": op,
                                     "doc_ids": doc_ids, "fields": [], "field_sets": set(), "decisions": {}, "why": {},
                                     "rule": row.get("rule"), "count": 0, "first": row.get("at"),
                                     "last": row.get("at")})
        names = [f for f in shape.get("fields") or [] if isinstance(f, str)]
        entry["fields"] += [f for f in names if f not in entry["fields"]]
        entry["field_sets"].add(tuple(names))
        entry["partial"] = entry.get("partial", False) or bool(shape.get("partial"))
        for key, value in (("decisions", row.get("decision")), ("why", row.get("why"))):
            entry[key][str(value)] = entry[key].get(str(value), 0) + 1
        entry["count"] += 1
        entry["last"] = row.get("at")
    result = []
    for entry in out.values():
        entry["observed_only"] = set(entry["decisions"]) == {"observed"}
        entry["field_sets"] = len(entry["field_sets"])
        entry["mixed"] = entry["field_sets"] > 1  # several bodies on one path: no field set binds them
        if entry["id"] in done:
            p = done[entry["id"]]
            entry["promoted"] = "read" if p["as"] == "read" else p["rule"]
            entry["promoted_lanes"] = p.get("lanes")  # None: every lane of the node
        result.append(entry)
    return sorted(result, key=lambda e: (-e["count"], e["host"], e["path"]))


def identities(rows: Iterable[dict[str, Any]], *, since: float | None = None) -> list[dict[str, Any]]:
    """The identities agentos does not know that publishes on the lane acted as (`gate_identity`
    rows, agentos.node.acting: an agent's publish the gate asked a person about, or a flow's it
    refused), grouped by site, id and field, most seen first: the rules, how many times, whether a
    flow sent it, why it is not known (the latest reason), first and last seen. Ids and field names
    only, never a value of the post."""
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row.get("kind") != "gate_identity":
            continue
        at = _at(row)
        if since is not None and (at is None or at < since):
            continue
        site, ident, path = str(row.get("site") or "?"), str(row.get("id") or ""), str(row.get("field") or "")
        entry = out.setdefault((site, ident, path), {
            "site": site, "id": ident or None, "field": path or None, "rules": [], "count": 0, "flow": False,
            "verified_field": bool(row.get("verified")), "first": row.get("at"), "last": row.get("at")})
        if row.get("rule") and row["rule"] not in entry["rules"]:
            entry["rules"].append(row["rule"])
        entry["count"] += 1
        entry["flow"] = entry["flow"] or bool(row.get("flow"))
        entry["why"] = row.get("why")
        entry["last"] = row.get("at")
    return sorted(out.values(), key=lambda e: (-e["count"], e["site"], e["id"] or ""))


# ---- the node's store ------------------------------------------------------------------------

def store_path(home: Path) -> Path:
    return Path(home) / GATE_SHAPES


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _read(path: Path) -> list[dict[str, Any]] | None:
    """The shapes a file holds; None when it cannot be read or parsed (missing: [])."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return None
    shapes = data.get("shapes") if isinstance(data, dict) else None
    return [s for s in shapes if isinstance(s, dict)] if isinstance(shapes, list) else None


def load(path: Path) -> list[dict[str, Any]]:
    """The promoted shapes. A file that cannot be parsed (a torn or hand-broken write) falls back to
    the copy the last save kept (`.bak`); when neither reads, none (and the gate stays fail closed)."""
    shapes = _read(path)
    if shapes is None:
        _log.warning("%s cannot be read; using its last copy %s", path, backup_path(path).name)
        shapes = _read(backup_path(path)) or []
    return [s for s in shapes if s.get("as") in ("read", "commit")]


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """One change to the store at a time, across processes (the daemon, the CLI with it stopped)."""
    import fcntl

    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _save(path: Path, shapes: list[dict[str, Any]]) -> None:
    """Write the store whole (a unique 600 temp file, fsync, an atomic rename), keeping the file it
    replaces as `.bak` when that one reads, so a torn write never drops every promotion."""
    from agentos.core.config import write_private

    if _read(path):
        write_private(backup_path(path), path.read_text())
    write_private(path, json.dumps({"version": 1, "shapes": shapes}, indent=1, sort_keys=True) + "\n")


def commit_rule_id(site: str, name: str) -> str:
    """`post`, `post.media` or `reddit.post.media` -> the rule id on `site`; refused unless it is
    `<site>.<kind>[.<detail>]` with a kind others see the account do (egress.EFFECT_KINDS)."""
    rid = name.strip()
    if not rid.startswith(site + "."):
        rid = f"{site}.{rid}"
    parts = rid.split(".")
    if not egress._RULE_ID_RE.match(rid) or parts[1] not in egress.EFFECT_KINDS:
        raise InvalidRequest(f"name the rule <kind>[.<detail>] with a kind of {', '.join(sorted(egress.EFFECT_KINDS))} "
                             f"(lower case letters, digits and _), like post.media; {name!r} is not one")
    if any(r.id == rid for r in egress.EFFECT_RULES):
        raise InvalidRequest(f"{rid} is a built-in rule; name it another way, like {rid}.extra")
    return rid


def promote(home: Path, lane_dir: Path, shape: str, *, as_: str, lane: str, name: str | None = None, by: str,
            base: tuple[egress.EffectRule, ...] | None = None, all_lanes: bool = False) -> dict[str, Any]:
    """Promote one shape of the lane's gate log to the read allowlist (`as_="read"`) or to a named
    commit rule (`as_="commit"`, `name`), for exactly the host, path and method it was seen on, and
    on `lane` only (a shape promoted again from another lane adds that lane), or on every lane of
    the node with `all_lanes`. Returns the stored entry; raises NotFound for a shape the lane's log
    does not have, InvalidRequest for one that cannot be promoted that way."""
    base = egress.EFFECT_RULES if base is None else base
    found = _logged(lane_dir, shape, as_)
    entry = _promotion(found, shape, as_=as_, lane=lane, name=name, by=by, base=base, all_lanes=all_lanes)
    _keep(home, entry, shape=shape, as_=as_, lane=lane)
    return entry


def _logged(lane_dir: Path, shape: str, as_: str) -> dict[str, Any]:
    """The shape in the lane's gate log (with what observe mode saw, for a commit)."""
    found = next((e for e in log(trace_rows(lane_dir), observed=as_ == "commit") if e["id"] == shape), None)
    if found is None:
        raise NotFound(f"no shape {shape} in this lane's gate log; list it with `agentos lane gate-log <lane>`"
                       + (" (--observed lists what observe mode saw)" if as_ == "commit" else ""))
    if as_ == "read" and found.get("observed_only"):  # never reached: log() lists them for commits only
        raise InvalidRequest("a shape only observe mode saw can be promoted to a commit rule, not to a read")
    return found


def _promotion(found: dict[str, Any], shape: str, *, as_: str, lane: str, name: str | None, by: str,
               base: tuple[egress.EffectRule, ...], all_lanes: bool) -> dict[str, Any]:
    """The entry promote() stores, once the shape can be promoted that way."""
    site = next((r.site for r in base if egress.globs.domain_match(r.host, found["host"])), None)
    if site is None:
        raise InvalidRequest(f"{found['host']} is not a host the commit gate knows: no effect rule names it, so "
                             "the gate does not route its requests and a rule there would hold nothing")
    entry: dict[str, Any] = {"id": shape, "as": as_, "site": site, "host": found["host"], "path": found["path"],
                             "method": found["method"], "op": found["op"], "doc_ids": found["doc_ids"],
                             "fields": found["fields"][:egress.SHAPE_MAX_FIELDS],
                             "lanes": None if all_lanes else [lane],
                             "by": by, "at": datetime.now(UTC).isoformat(timespec="seconds"), "rule": None}
    if as_ == "read":
        if _catch_all(base, found) is not None and not found["op"]:
            raise InvalidRequest("this GraphQL call names no operation, so it cannot be told from a publish; "
                                 "it stays held")
        if not found["op"]:
            _body_bound(found, base)
    elif as_ == "commit":
        if not name:
            raise InvalidRequest("name the commit rule, like post.media")
        entry["rule"] = commit_rule_id(site, name)
    else:
        raise InvalidRequest("promote a shape as read or as commit")
    return entry


def _keep(home: Path, entry: dict[str, Any], *, shape: str, as_: str, lane: str) -> None:
    """Store the promotion in place of any other of this shape: the same promotion from another
    lane adds that lane; a rule name another shape holds, or a full store, refuses it."""
    path = store_path(home)
    with _locked(path):
        before = next((s for s in load(path) if s.get("id") == shape and s.get("as") == as_
                       and s.get("rule") == entry["rule"]), None)
        if before is not None and entry["lanes"] is not None:  # the same promotion, from another lane too
            had = before.get("lanes")
            entry["lanes"] = None if had is None else sorted({*had, lane}) if isinstance(had, list) else [lane]
        shapes = [s for s in load(path) if s.get("id") != shape]
        if any(s.get("rule") == entry["rule"] for s in shapes if entry["rule"]):
            raise InvalidRequest(f"another shape is promoted as {entry['rule']} already; name this one another way")
        if len(shapes) >= KEPT:
            raise InvalidRequest(f"this node keeps {KEPT} promoted shapes; remove one first")
        _save(path, shapes + [entry])


def _body_bound(found: dict[str, Any], base: tuple[egress.EffectRule, ...]) -> None:
    """A read promoted for a request that names no operation is bound by its body's field names
    (egress.body_covered). Refused where that cannot bind it: on a host the rules know publishes
    on, for a shape that mixes several bodies on one path, one whose names are not all of its
    body's, or one that names an operation or a persisted query."""
    publishes = next((r for r in base if r.effect == "commit" and egress.globs.domain_match(r.host, found["host"])),
                     None)
    if publishes is not None:
        raise InvalidRequest(f"{found['host']} is a host the rules know publishes on ({publishes.id}), and this "
                             "request names no operation, so it cannot be told from a publish; it stays held")
    if found["field_sets"] > 1:
        raise InvalidRequest(f"this shape mixes {found['field_sets']} bodies on one path, so no one field set "
                             "binds it; it stays held")
    if found.get("partial"):
        raise InvalidRequest("the gate could not list every field of this body, so no field set binds it; "
                             "it stays held")
    named = sorted(set(found["fields"]) & egress.OP_FIELDS)
    if named or found.get("doc_ids"):
        raise InvalidRequest(f"this body names an operation ({', '.join(named) or 'a doc_id'}) the gate cannot "
                             "read; it stays held")


def remove(home: Path, shape: str) -> dict[str, Any]:
    """Undo a promotion: the gate classifies that shape fail closed again."""
    path = store_path(home)
    with _locked(path):
        shapes = load(path)
        gone = next((s for s in shapes if s.get("id") == shape), None)
        if gone is None:
            raise NotFound(f"no promoted shape {shape} on this node")
        _save(path, [s for s in shapes if s.get("id") != shape])
    return gone


def audit(traces: Any, entry: dict[str, Any], *, lane: str, by: str, removed: bool = False) -> None:
    """The action log's row for a change to the gate's allowlist (`gate_shape`): who, which shape,
    as what. Names only, as the gate log shows them."""
    if traces is None:
        return
    from agentos.policy.traces import TraceEvent

    what = "read" if entry["as"] == "read" else f"commit rule {entry['rule']}"
    shape = f"{entry['method']} {entry['host']}{entry['path']}" + (f" {entry['op']}" if entry.get("op") else "")
    summary = (f"removed {shape} from the gate's {what}" if removed
               else f"promoted {shape} to the gate's {'read allowlist' if entry['as'] == 'read' else what}"
                    f" {scope(entry.get('lanes'))}")
    traces.write(TraceEvent(kind="gate_shape", tool="lane.gate_log", status="ok", principal=by,
                            principal_kind="human", lane=lane, summary=summary,
                            args={"shape": entry["id"], "as": entry["as"], "rule": entry.get("rule"),
                                  "lanes": entry.get("lanes"), "removed": removed}))


def scope(lanes: list[str] | None) -> str:
    """Where a promotion applies, as the gate log and the action log say it."""
    return "on every lane of the node" if lanes is None else "on " + ", ".join(lanes)


# ---- what the gate reads -------------------------------------------------------------------

@dataclass(frozen=True)
class OwnerRules:
    """The promoted shapes as rules: commit rules (before the built-in ones), read rules (after
    them) and known read operations per site (GraphQL endpoints)."""
    commit: tuple[egress.EffectRule, ...] = ()
    read: tuple[egress.EffectRule, ...] = ()
    # (host, path) -> operation name -> the doc_ids it was promoted with ("": with none): a name is
    # trusted only on the endpoint it was seen on, never on the site's every strict endpoint
    read_ops: dict[tuple[str, str], dict[str, frozenset[str]]] = field(default_factory=dict)


def _catch_all(base: tuple[egress.EffectRule, ...], shape: dict[str, Any]) -> egress.EffectRule | None:
    url = f"https://{shape['host']}{shape['path']}"
    host, path = egress.host_of(url) or "", egress.norm_path(url)
    return next((r for r in base if r.effect == "read" and r.strict_ops
                 and r.matches(host, path, str(shape["method"]).upper(), {}, lambda: None)), None)


_ID_RE = re.compile(r"^sh_[0-9a-f]{12}$")
_SITE_RE = re.compile(r"^[a-z0-9]+$")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_GATED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def invalid(s: Any, base: tuple[egress.EffectRule, ...]) -> str | None:
    """Why a stored entry cannot be a promotion (a hand edit, a file from another version), or
    None. Strict: every field of its kind present and of its type, the id its own shape's, the
    site the one the rules give its host, a commit rule's name one promote() would accept."""
    if not isinstance(s, dict):
        return "not an object"
    return _invalid_fields(s) or _invalid_shape(s, base) or _invalid_promotion(s)


def _invalid_fields(s: dict[str, Any]) -> str | None:
    """Every field present and of its type: names, doc ids and the lanes it holds for."""
    for key, kind in (("id", str), ("as", str), ("site", str), ("host", str), ("path", str), ("method", str),
                      ("fields", list)):
        if not isinstance(s.get(key), kind):
            return f"{key} missing or not a {kind.__name__}"
    op = s.get("op")
    if op is not None and not (isinstance(op, str) and (egress._SHAPE_NAME_RE.match(op)
                                                        or egress._DOTTED_NAME_RE.match(op))):
        return "op is not an operation name"
    if not all(isinstance(f, str) for f in s["fields"]):
        return "fields are not names"
    doc_ids = s.get("doc_ids", [])
    if not isinstance(doc_ids, list) or not all(isinstance(d, str) and egress._DOC_ID_RE.match(d) for d in doc_ids):
        return "doc_ids are not doc_ids"
    if "lanes" not in s or not (s["lanes"] is None or (isinstance(s["lanes"], list) and s["lanes"]
                                                         and all(isinstance(x, str) for x in s["lanes"]))):
        return "lanes is neither a list of lanes nor null (every lane)"
    return None


def _invalid_shape(s: dict[str, Any], base: tuple[egress.EffectRule, ...]) -> str | None:
    """The id its own shape's, a host and path a request has, a gated method, its host's site."""
    if not _ID_RE.match(s["id"]) or s["id"] != shape_id(s["host"], s["path"], s["method"], s.get("op"),
                                                         s.get("doc_ids", [])):
        return "id is not its shape's"
    if not _HOST_RE.match(s["host"]) or not s["path"].startswith("/") or any(c.isspace() for c in s["path"]):
        return "host or path is not one a request has"
    if s["method"] not in _GATED_METHODS:
        return "method is not one the gate classifies"
    site = next((r.site for r in base if egress.globs.domain_match(r.host, s["host"])), None)
    if not _SITE_RE.match(s["site"]) or s["site"] != site:
        return "site is not the one the rules give its host"
    return None


def _invalid_promotion(s: dict[str, Any]) -> str | None:
    """A commit promotion names a rule promote() accepts; a read names none, and a read with no
    operation has a body that names none either."""
    if s["as"] == "commit":
        if not isinstance(s.get("rule"), str):
            return "a commit promotion names no rule"
        try:
            if commit_rule_id(s["site"], s["rule"]) != s["rule"]:
                return "rule is not a commit rule of its site"
        except InvalidRequest:
            return "rule is not a commit rule promote() accepts"
        return None
    if s["as"] == "read":
        if s.get("rule") is not None:
            return "a read promotion names a rule"
        if s.get("op") is None and (set(s["fields"]) & egress.OP_FIELDS or s.get("doc_ids", [])):
            return "a read with no operation whose body names one"
        return None
    return "as is neither read nor commit"


def compile_rules(shapes: Iterable[dict[str, Any]], base: tuple[egress.EffectRule, ...],
                  lane: str | None = None) -> OwnerRules:
    """The promotions that apply on `lane` (those promoted on it or on every lane) as rules, each
    for exactly its host, path and method. An entry that is not valid (invalid()) is skipped, with
    the reason logged: it never becomes a rule, so the gate holds what it would have named."""
    commit: list[egress.EffectRule] = []
    read: list[egress.EffectRule] = []
    ops: dict[tuple[str, str], dict[str, set[str]]] = {}
    for s in shapes:
        why = invalid(s, base)
        if why is not None:
            _log.warning("a promoted gate shape %s is skipped: %s", str(s.get("id") if isinstance(s, dict) else "?")[:20],
                         why)
            continue
        if s["lanes"] is not None and lane not in s["lanes"]:
            continue
        host, path, method, op, site = s["host"], s["path"], s["method"], s.get("op"), s["site"]
        where = (host, egress.norm_path(f"https://{host}{path}"))
        exact = re.escape(path) + "$"
        named = "^" + re.escape(op) + "$" if op else None
        note = f"promoted from the gate log by {s.get('by') or 'the owner'} at {s.get('at') or '?'}"
        if s["as"] == "commit":
            commit.append(egress.EffectRule(s["rule"], site, host, exact, method=method, op=named, note=note))
            continue
        if _catch_all(base, s) is not None:
            if named:  # a GraphQL operation: a known read on this endpoint (a batch still needs every one)
                ops.setdefault(where, {}).setdefault(op, set()).update(s.get("doc_ids") or [""])
            continue
        # an operation-named read elsewhere is still checked whole: every operation a known read
        # one that names no operation is bound by its body's field names (egress.body_covered), and never
        # where the rules know a publish
        if named is None and any(r.effect == "commit" and egress.globs.domain_match(r.host, host) for r in base):
            _log.warning("a promoted gate shape %s is skipped: a read with no operation on a host that publishes",
                         s["id"])
            continue
        read.append(egress.EffectRule(f"{site}.read.owner_{s['id'][3:]}", site, host, exact, method=method,
                                      effect="read", op=named, strict_ops=named is not None, note=note,
                                      fields=None if named else frozenset(s["fields"])))
        if named:
            ops.setdefault(where, {}).setdefault(op, set()).update(s.get("doc_ids") or [""])
    return OwnerRules(tuple(commit), tuple(read),
                      {k: {name: frozenset(d) for name, d in v.items()} for k, v in ops.items()})


class ShapeSource:
    """The node's promoted shapes, read again only when the file changes (its mtime and size)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._seen: tuple[float, int] | None = None
        self._shapes: tuple[dict[str, Any], ...] = ()

    def __call__(self) -> tuple[dict[str, Any], ...]:
        try:
            st = self.path.stat()
            mark = (st.st_mtime, st.st_size)
        except OSError:
            mark = (0.0, -1)
        if mark != self._seen:
            self._seen = mark
            self._shapes = tuple(load(self.path)) if mark[1] >= 0 else ()
        return self._shapes


_SOURCES: dict[Path, ShapeSource] = {}


def source(home: Path) -> Callable[[], tuple[dict[str, Any], ...]]:
    """One source per node home, shared by its lanes' guards."""
    path = store_path(home)
    if path not in _SOURCES:
        _SOURCES[path] = ShapeSource(path)
    return _SOURCES[path]


def since_of(text: str | None, now: float | None = None) -> float | None:
    """`--since`: a duration back from now (30m, 24h, 7d) or an ISO date or time (UTC when no zone)."""
    if not text:
        return None
    from agentos.policy.util import _DURATION_RE, parse_duration

    now = time.time() if now is None else now
    if _DURATION_RE.match(text):
        return now - parse_duration(text)
    try:
        at = datetime.fromisoformat(text.strip())
    except ValueError:
        raise InvalidRequest(f"since takes a duration (30m, 24h, 7d) or an ISO date: {text!r}") from None
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).timestamp()
