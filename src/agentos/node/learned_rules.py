"""Learned profile rules (docs/design/learn-a-site.md section 3): the commit request of a profile
field the node learned from its own egress trace, as an effect rule the gate classifies by.

When an agent edits a profile field once inside a learn window (agentos.node.egress.learn), the
lane's guard releases that one request and records its shape (host, path, method, operation
name). The node, never the agent, writes it here as `<site>.profile.learned_<field>`: kind fixed
`profile` (a commit, gated and metered like any publish), for exactly that host, path, method and
operation. `verified` stays empty until the node's model-free replay passed (set_verified).

What keeps it safe:

- Only node code writes the file (`learned-rules.json` in AGENTOS_HOME, mode 600, whole-file atomic
  writes with a `.bak` of the last good one). No MCP tool, API route or CLI command writes it, so an
  agent cannot create, change or verify a rule.
- A rule names only the reviewed endpoint that saves its field (agentos.node.sites
  PROFILE_EDIT_ENDPOINTS: method, exact host, whole path, operation name); a field with none cannot
  be learned. It names only a host a built-in rule names (as gateshapes.promote refuses others), a field of
  `[a-z_]{1,30}`, and a method that writes (POST, PUT, PATCH, DELETE). A request a built-in commit,
  write or draft rule already matches is refused: it is that rule's, never a profile edit. So is one
  that would make the site's reads commits: no operation name where a read rule matches (a GraphQL
  endpoint), or an operation that is a known read.
- The guard compiles learned rules after every built-in commit, write and draft rule and before
  the read catch-alls (ordered()), so a learned rule never reclassifies a post, comment, like,
  follow or join: a request one of those matches is classified by it first. And it never turns a
  request into a read: a learned rule is a commit.
- An entry that does not validate when the file is read (a hand edit) is skipped and logged: it
  never becomes a rule.

Every lane's guard reads the file when it starts and again when it changes (source(): mtime and
size, checked at most every egress.SITES_TTL_S, as gateshapes.ShapeSource does).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.core.errors import InvalidRequest, NotFound
from agentos.node import egress

_log = logging.getLogger(__name__)

LEARNED_RULES = "learned-rules.json"
KEPT = 200  # learned rules a node keeps
KIND = "profile"
METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_FIELD_RE = re.compile(r"^[a-z_]{1,30}$")
_SITE_RE = re.compile(r"^[a-z0-9]+$")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_BY_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")
PATH_MAX = 512


def rule_id(site: str, field: str) -> str:
    return f"{site}.{KIND}.learned_{field}"


def store_path(home: Path) -> Path:
    return Path(home) / LEARNED_RULES


def _backup(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _read(path: Path) -> list[dict[str, Any]] | None:
    """The entries a file holds; None when it cannot be read or parsed (missing: [])."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return None
    rules = data.get("rules") if isinstance(data, dict) else None
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else None


def load(path: Path) -> list[dict[str, Any]]:
    """The stored entries. A file that cannot be parsed falls back to the copy the last save kept
    (`.bak`); when neither reads, none (the gate then holds what a rule would have named)."""
    rules = _read(path)
    if rules is None:
        _log.warning("%s cannot be read; using its last copy %s", path, _backup(path).name)
        rules = _read(_backup(path)) or []
    return rules


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _save(path: Path, rules: list[dict[str, Any]]) -> None:
    """The whole store: a 600 temp file, fsync and an atomic rename (write_private), keeping the file
    it replaces as `.bak` when that one reads."""
    from agentos.core.config import write_private

    if _read(path):
        write_private(_backup(path), path.read_text())
    write_private(path, json.dumps({"version": 1, "rules": rules}, indent=1, sort_keys=True) + "\n")


def _site_of(host: str, base: tuple[egress.EffectRule, ...]) -> str | None:
    return next((r.site for r in base if egress.globs.domain_match(r.host, host)), None)


def _shadowed(host: str, path: str, method: str, op: str | None,
              base: tuple[egress.EffectRule, ...]) -> egress.EffectRule | None:
    """The built-in commit, write or draft rule this exact request would match, if any."""
    return next((r for r in base if r.effect != "read" and r.matches(host, path, method, {}, lambda: op)), None)


def _read_overlap(site: str, host: str, path: str, method: str, op: str | None,
                  base: tuple[egress.EffectRule, ...]) -> str | None:
    """Why a rule for this request would turn the site's reads into commits (every read of a GraphQL
    endpoint, a known read operation, a read rule's whole path), or None."""
    reads = [r for r in base if r.effect == "read" and r.matches(host, path, method, {}, lambda: op)]
    if not reads:
        return None
    if op is None:
        return f"it names no operation, and the read rule {reads[0].id} names this request"
    known = egress.READ_OPS.get(site)
    if (known is not None and known.match(op)) or egress._QUERY_NAME.match(op):
        return f"{op} is a known read"
    return None


def _unreviewed(site: str, field: str, host: str, path: str, method: str, op: str | None) -> str | None:
    """Why this request is not the reviewed endpoint that saves `field` on `site`
    (agentos.node.sites.PROFILE_EDIT_ENDPOINTS), or None."""
    from agentos.node.sites import profile_edit_endpoint

    ep = profile_edit_endpoint(site, field)
    if ep is None:
        return f"{field} on {site} has no reviewed edit endpoint, so it cannot be learned"
    if (method, host, op or None) != (ep.method, ep.host, ep.op) or re.fullmatch(ep.path, path) is None:
        return f"this is not the reviewed endpoint that saves {field} on {site} ({ep.method} {ep.host})"
    return None


def invalid(e: Any, base: tuple[egress.EffectRule, ...] | None = None) -> str | None:
    """Why a stored entry cannot be a learned rule (a hand edit, a file from another version), or None."""
    base = egress.EFFECT_RULES if base is None else base
    if not isinstance(e, dict):
        return "not an object"
    for key in ("id", "site", "field", "host", "path", "method", "kind"):
        if not isinstance(e.get(key), str):
            return f"{key} missing or not a string"
    if e["kind"] != KIND:
        return "kind is not profile"
    if not _FIELD_RE.match(e["field"]) or not _SITE_RE.match(e["site"]):
        return "field or site is not a name"
    if e["id"] != rule_id(e["site"], e["field"]):
        return "id is not its site's and field's"
    if e["method"] not in METHODS:
        return "method is not one that writes"
    if not _HOST_RE.match(e["host"]):
        return "host is not a host name"
    path = e["path"]
    if not path.startswith("/") or len(path) > PATH_MAX or any(c.isspace() for c in path):
        return "path is not one a request has"
    op = e.get("op")
    if op is not None and not (isinstance(op, str) and (egress._SHAPE_NAME_RE.match(op)
                                                        or egress._DOTTED_NAME_RE.match(op))):
        return "op is not an operation name"
    if _site_of(e["host"], base) != e["site"]:
        return "site is not the one the built-in rules give its host"
    why = _unreviewed(e["site"], e["field"], e["host"], path, e["method"], op)
    if why is not None:
        return why
    if _shadowed(e["host"], path, e["method"], op, base) is not None:
        return "a built-in rule names this request"
    why = _read_overlap(e["site"], e["host"], path, e["method"], op, base)
    if why is not None:
        return why
    if not isinstance(e.get("verified", ""), str):
        return "verified is not a string"
    return None


def _compile(e: dict[str, Any]) -> egress.EffectRule:
    op = e.get("op")
    return egress.EffectRule(
        e["id"], e["site"], e["host"], re.escape(e["path"]) + "$", method=e["method"], effect="commit",
        op=("^" + re.escape(op) + "$") if op else None, verified=e.get("verified") or "",
        note=f"learned by the node at {e.get('at') or '?'} (the {e['field']} field)")


def compile_rules(entries: Any, base: tuple[egress.EffectRule, ...] | None = None) -> tuple[egress.EffectRule, ...]:
    """The valid entries as effect rules; an invalid one is skipped, with the reason logged."""
    base = egress.EFFECT_RULES if base is None else base
    out: list[egress.EffectRule] = []
    for e in entries or ():
        why = invalid(e, base)
        if why is not None:
            _log.warning("a learned rule %s is skipped: %s",
                         str(e.get("id") if isinstance(e, dict) else "?")[:60], why)
            continue
        out.append(_compile(e))
    return tuple(out)


def ordered(owner_commit: tuple[egress.EffectRule, ...], base: tuple[egress.EffectRule, ...],
            learned: tuple[egress.EffectRule, ...], owner_read: tuple[egress.EffectRule, ...]
            ) -> tuple[egress.EffectRule, ...]:
    """The guard's classification order. With no learned rule it is exactly the order without them
    (owner commits, the built-in rules, owner reads). With some, every built-in commit, write and
    draft rule comes first, then the learned rules, then the built-in reads (their order kept): no
    built-in read precedes a commit or write rule its path could overlap (tests prove it), so moving
    the reads after them changes no built-in classification, and a learned rule sits before the read
    catch-all of its endpoint and after every rule that names a post, comment, like, follow or join."""
    if not learned:
        return owner_commit + base + owner_read
    acting = tuple(r for r in base if r.effect != "read")
    reads = tuple(r for r in base if r.effect == "read")
    return owner_commit + acting + learned + reads + owner_read


class LearnedRules:
    """The node's learned profile rules (see the module docstring). Node code only."""

    def __init__(self, home: Path, *, base: tuple[egress.EffectRule, ...] | None = None):
        self.path = store_path(home)
        self.base = egress.EFFECT_RULES if base is None else base

    def add(self, site: str, field: str, *, host: str, path: str, method: str, op: str | None) -> str:
        """Store the learned commit request of `field` on `site` (unverified) and return its rule
        id. A rule of the same id is replaced, and its `verified` reset."""
        site, host, method = str(site).strip().lower(), str(host).strip().lower(), str(method).strip().upper()
        if not _FIELD_RE.match(str(field)):
            raise InvalidRequest(f"a learned field is 1 to 30 of a-z and _; {field!r} is not one")
        if method not in METHODS:
            raise InvalidRequest(f"a learned rule names a request that writes (POST, PUT, PATCH, DELETE), not {method}")
        known = _site_of(host, self.base)
        if known is None:
            raise InvalidRequest(f"{host} is not a host the commit gate knows: no effect rule names it, so the "
                                 "gate does not route its requests and a rule there would hold nothing")
        if known != site:
            raise InvalidRequest(f"{host} belongs to {known} in the built-in rules, not to {site}")
        entry: dict[str, Any] = {"id": rule_id(site, field), "site": site, "field": field, "kind": KIND,
                                 "host": host, "path": path, "method": method, "op": op or None, "verified": "",
                                 "verified_by": None, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        shadow = _shadowed(host, path, method, op or None, self.base)
        if shadow is not None:
            raise InvalidRequest(f"this request is the built-in rule {shadow.id}'s, never a profile edit")
        why = invalid(entry, self.base)
        if why is not None:
            raise InvalidRequest(f"cannot learn this request: {why}")
        with _locked(self.path):
            rules = [r for r in load(self.path) if r.get("id") != entry["id"]]
            if len(rules) >= KEPT:
                raise InvalidRequest(f"this node keeps {KEPT} learned rules; remove one first")
            _save(self.path, rules + [entry])
        return entry["id"]

    def set_verified(self, rule_id: str, by: str) -> None:
        """The node's model-free replay passed: the rule is verified (`<date> <by>`)."""
        if not _BY_RE.match(str(by)):
            raise InvalidRequest("verified by is a short name (letters, digits and _.:@-)")
        with _locked(self.path):
            rules = load(self.path)
            found = next((r for r in rules if r.get("id") == rule_id), None)
            if found is None:
                raise NotFound(f"no learned rule {rule_id} on this node")
            now = datetime.now(UTC)
            found["verified"] = f"{now.date().isoformat()} {by}"
            found["verified_by"] = by
            found["verified_at"] = now.isoformat(timespec="seconds")
            _save(self.path, rules)

    def remove(self, rule_id: str) -> bool:
        with _locked(self.path):
            rules = load(self.path)
            kept = [r for r in rules if r.get("id") != rule_id]
            if len(kept) == len(rules):
                return False
            _save(self.path, kept)
        return True

    def get(self, rule_id: str) -> dict[str, Any] | None:
        return next((dict(r) for r in load(self.path) if r.get("id") == rule_id), None)

    def list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in load(self.path)]

    def effect_rules(self) -> list[egress.EffectRule]:
        return list(compile_rules(load(self.path), self.base))


class RuleSource:
    """The node's learned entries, read again only when the file changes (its mtime and size)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._seen: tuple[float, int] | None = None
        self._entries: tuple[dict[str, Any], ...] = ()

    def __call__(self) -> tuple[dict[str, Any], ...]:
        try:
            st = self.path.stat()
            mark = (st.st_mtime, st.st_size)
        except OSError:
            mark = (0.0, -1)
        if mark != self._seen:
            self._seen = mark
            self._entries = tuple(load(self.path)) if mark[1] >= 0 else ()
        return self._entries


_SOURCES: dict[Path, RuleSource] = {}


def source(home: Path) -> Callable[[], tuple[dict[str, Any], ...]]:
    """One source per node home, shared by its lanes' guards."""
    path = store_path(home)
    if path not in _SOURCES:
        _SOURCES[path] = RuleSource(path)
    return _SOURCES[path]
