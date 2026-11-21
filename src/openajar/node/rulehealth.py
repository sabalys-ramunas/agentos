"""Effect rule health: is each site's set of effect rules still the site's web client?
(docs/design/egress-guard.md 8.12)

The commit gate decides what publishes by hand-written rules (egress.EFFECT_RULES: host, path,
method and an operation name). Sites ship new frontend code all the time, and the 2026-11-04 canary
found every platform's rules wrong before that day. When a site renames its comment mutation, the
gate stays safe (the new name is `<site>.unknown`, held while an agent drives), but the old rule
goes quiet and the owner sees approvals for requests the gate cannot name. This module reads the
egress traces the node already writes and says, per site and per rule:

- when each commit and write rule last matched, how often, and on which lanes (`rule_hit` rows for
  write rules, `observed_commit` and `blocked` rows for commit rules, `observed_request` rows in
  observe mode);
- the shapes the rules do not name, seen on that site (`gate_classified` rows the gate held or let
  go, `unmatched_publish` rows of an agent's press, `unruled_request` rows of a lane's own host no
  rule names, and `observed_request` rows no write or commit rule matched), with counts, how each
  was decided, and whether its name looks like a write (`write_like`) and of which kind (`kind`);
- flags, each a sentence for the owner:
  - `stale_rule`: a verified (or owner promoted) commit or write rule that has not matched since a
    shape of its own kind appeared on its site (a `CreateComment` after `reddit.comment` went
    quiet), or that has not matched for `stale_days` while the site keeps sending unknown shapes
    that look like writes. The site has likely changed its client.
  - `new_unknowns`: at least `spike` new unknown shapes that look like writes on one site within
    `recent_h` hours.
  - `no_rules`: a lane whose own site list, or the site it is expected to be signed into, has
    hosts no effect rule names. A publish there is not gated; it is traced only.

Names and shapes only, never a value: every row read here holds names already (egress.request_shape).
The owner fixes a stale rule without a code change: `openajar lane gate-log <lane> --as-commit
<kind> --shape <id>` (openajar.node.gateshapes), which every running guard reads within seconds.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openajar.core import clock, globs
from openajar.node import egress, gateshapes

STALE_DAYS = 14     # a verified rule quiet this long, while its site sends unknown writes, is flagged
RECENT_H = 24       # new unknown shapes within this many hours...
SPIKE = 3           # ...at least this many on one site is flagged
SHAPES_SHOWN = 3    # candidate shapes a flag names

# the words of an operation name or a path that say it changes something (a camelCase name, a path, a
# dotted name are split into words; a trailing "s" is dropped). A heuristic for the owner's eyes: it
# never decides what the gate does
WRITE_WORDS = frozenset({
    "mutation", "create", "add", "post", "comment", "reply", "like", "unlike", "react", "reaction", "vote",
    "upvote", "downvote", "follow", "unfollow", "subscribe", "subscription", "join", "share", "publish",
    "submit", "send", "tweet", "retweet", "repost", "favorite", "favourite", "pin", "save", "delete", "remove",
    "update", "edit", "upload", "compose", "story", "configure", "quote", "status"})
# which kind of rule a write-like shape is most likely the new form of, first match wins (a
# PostComment is a comment, an UpdatePostVoteState a like)
KIND_WORDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("comment", frozenset({"comment", "reply"})),
    ("like", frozenset({"like", "unlike", "react", "reaction", "vote", "upvote", "downvote", "favorite",
                        "favourite"})),
    ("follow", frozenset({"follow", "unfollow"})),
    ("join", frozenset({"join", "subscribe", "subscription"})),
    ("post", frozenset({"post", "tweet", "retweet", "repost", "publish", "story", "share", "compose", "submit",
                        "configure", "status", "create"})),
)
_WORD_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")
_PUT_LIKE = frozenset({"PUT", "PATCH", "DELETE"})


def words(text: str | None) -> list[str]:
    """`usePolarisLikeMediaXIGLikeMutation` -> use, polaris, like, media, xig, like, mutation."""
    out = []
    for w in _WORD_RE.findall(text or ""):
        w = w.lower()
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def write_like(method: str, op: str | None, path: str, why: str | None = None) -> bool:
    """Does this shape look like it changes something others see? A PUT, PATCH or DELETE; a mutation;
    an operation (or, with none, a path) with a write word in it. A name like a query never does."""
    if method.upper() in _PUT_LIKE or (why or "").startswith(("a mutation", "an unknown mutation")):
        return True
    if op:
        if egress._QUERY_NAME.match(op):
            return False
        return any(w in WRITE_WORDS for w in words(op))
    return any(w in WRITE_WORDS for w in words(path))


def kind_of(op: str | None, path: str) -> str | None:
    """The rule kind (post, comment, like, follow, join) a write-like shape most likely is, or None."""
    found = set(words(op)) if op else set(words(path))
    return next((kind for kind, ws in KIND_WORDS if found & ws), None)


def _at(value: Any) -> float | None:
    with contextlib.suppress(TypeError, ValueError):
        at = datetime.fromisoformat(str(value))
        return (at if at.tzinfo else at.replace(tzinfo=UTC)).timestamp()
    return None


def site_of(host: str, rules: tuple[egress.EffectRule, ...]) -> str:
    """The site a host belongs to: the rules' site for it, else the site openajar knows it as
    (openajar.node.sites), else the host itself."""
    found = next((r.site for r in rules if globs.domain_match(r.host, host)), None)
    if found is not None:
        return found
    from openajar.node import sites

    known = sites.site_for(f"https://{host}/")
    return known.key if known is not None else host


def _known_read(rule: egress.EffectRule | None, op: str | None) -> bool:
    """Is an operation a strict_ops catch-all matched (in observe mode) a known read by its name?"""
    if rule is None or not op:
        return False
    known = egress.READ_OPS.get(rule.site)
    return bool((known is not None and known.match(op)) or egress._QUERY_NAME.match(op))


def owner_rules(promoted: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The owner's commit promotions (gateshapes) as rules for the health table."""
    return [{"id": p["rule"], "site": p.get("site"), "effect": "commit", "kind": str(p["rule"]).split(".")[1],
             "host": p.get("host"), "verified": f"promoted by {p.get('by') or 'the owner'} at {p.get('at') or '?'}",
             "owner": True}
            for p in promoted if p.get("as") == "commit" and isinstance(p.get("rule"), str)
            and len(str(p["rule"]).split(".")) >= 2]


@dataclass
class _Tally:
    """How often one rule (or one unknown shape) was seen in the traces, when, and on which lanes."""

    count: int = 0
    first: float | None = None
    last: float | None = None
    lanes: set[str] = field(default_factory=set)

    def add(self, lane: str, at: float | None) -> None:
        self.count += 1
        self.lanes.add(lane)
        if at is not None:
            self.first = at if self.first is None else min(self.first, at)
            self.last = at if self.last is None else max(self.last, at)


@dataclass
class _Readings:
    """What the egress traces say: each rule's matches, the shapes no rule names (by shape id, in
    the report's own form), and the oldest row's time."""

    rules: tuple[egress.EffectRule, ...]
    promoted_ids: set[Any]
    hits: dict[str, _Tally] = field(default_factory=dict)
    unknown: dict[str, dict[str, Any]] = field(default_factory=dict)
    oldest: float | None = None

    def hit(self, rule_id: str, lane: str, at: float | None) -> None:
        self.hits.setdefault(rule_id, _Tally()).add(lane, at)

    def shape(self, row: dict[str, Any], lane: str, at: float | None, source: str, decision: str) -> None:
        sh = row.get("shape") if isinstance(row.get("shape"), dict) else {}
        host, path, method = str(row.get("host") or "?"), str(row.get("path") or "/"), str(row.get("method") or "?")
        op = sh.get("op") if isinstance(sh.get("op"), str) else None
        doc_ids = sorted({d for d in sh.get("doc_ids") or [] if isinstance(d, str)})
        sid = gateshapes.shape_id(host, path, method, op, doc_ids)
        e = self.unknown.setdefault(sid, {
            "id": sid, "site": site_of(host, self.rules), "host": host, "path": path, "method": method, "op": op,
            "count": 0, "decisions": {}, "sources": {}, "lanes": set(), "first": None, "last": None,
            "write_like": False, "kind": None, "promoted": sid in self.promoted_ids})
        e["count"] += 1
        e["lanes"].add(lane)
        e["decisions"][decision] = e["decisions"].get(decision, 0) + 1
        e["sources"][source] = e["sources"].get(source, 0) + 1
        e["write_like"] = e["write_like"] or write_like(method, op, path, row.get("why"))
        e["kind"] = e["kind"] or (kind_of(op, path) if e["write_like"] else None)
        if at is not None:
            e["first"] = at if e["first"] is None else min(e["first"], at)
            e["last"] = at if e["last"] is None else max(e["last"], at)


def _read_row(found: _Readings, row: dict[str, Any], lane: str, by_id: dict[str, egress.EffectRule]) -> None:
    """One trace row: a match of a rule, a shape no rule names, or neither."""
    at = _at(row.get("at"))
    if at is not None:
        found.oldest = at if found.oldest is None else min(found.oldest, at)
    kind = row.get("kind")
    if kind == "rule_hit" and isinstance(row.get("rule"), str):
        found.hit(row["rule"], lane, at)
    elif kind in ("observed_commit", "blocked") and isinstance(row.get("effect_rule"), str):
        if not row["effect_rule"].endswith("." + egress.UNKNOWN):
            found.hit(row["effect_rule"], lane, at)
    elif kind == "gate_classified" and row.get("decision") != "read":
        found.shape(row, lane, at, "gate", str(row.get("decision")))
    elif kind == "unmatched_publish":
        found.shape(row, lane, at, "press", "let go")
    elif kind == "unruled_request":
        found.shape(row, lane, at, "unruled", "let go")
    elif kind == "observed_request":
        _read_observed(found, row, lane, at, by_id)


def _read_observed(found: _Readings, row: dict[str, Any], lane: str, at: float | None,
                   by_id: dict[str, egress.EffectRule]) -> None:
    """An observe-mode row: a match of a commit or write rule, or a shape no rule reads by name."""
    rid = row.get("effect_rule")
    rule = by_id.get(rid) if isinstance(rid, str) else None
    if rule is not None and rule.effect in ("commit", "write"):
        found.hit(rule.id, lane, at)
    elif rid is None or (rule is not None and rule.strict_ops and not _known_read(
            rule, (row.get("shape") or {}).get("op") if isinstance(row.get("shape"), dict) else None)):
        found.shape(row, lane, at, "observed", "observed")


def _rule_table(rules: tuple[egress.EffectRule, ...], promoted: list[Mapping[str, Any]],
                hits: dict[str, _Tally]) -> list[dict[str, Any]]:
    """One row per commit and write rule (the owner's promotions too), with what the traces say.
    `_last` (the last match as a number) is for the flags and is dropped before the report goes out."""
    table = [{"id": r.id, "site": r.site, "effect": r.effect, "kind": r.kind, "host": r.host,
              "verified": r.verified or None, "owner": False}
             for r in rules if r.effect in ("commit", "write") and r.kind in egress.EFFECT_KINDS]
    seen_ids = {t["id"] for t in table}
    table += [t for t in owner_rules(promoted) if t["id"] not in seen_ids]
    for t in table:
        h = hits.get(t["id"])
        t.update({"count": h.count if h else 0, "first": clock.iso_or_none(h.first, timespec="seconds") if h else None,
                  "last": clock.iso_or_none(h.last, timespec="seconds") if h else None,
                  "lanes": sorted(h.lanes) if h else [], "_last": h.last if h else None})
    return table


def _stale_flags(table: list[dict[str, Any]], shapes: list[dict[str, Any]], *, now: float, stale_days: float,
                 oldest: float | None) -> list[dict[str, Any]]:
    """`stale_rule`: a verified or promoted rule quiet since shapes of its kind (or, quiet for
    `stale_days`, any write-like shapes) appeared on its site. One flag per site and candidate set."""
    stale: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for t in table:
        if not (t["verified"] or t["owner"]):
            continue  # an unverified rule (an older client) going quiet says nothing
        last = t["_last"]
        after = [e for e in shapes if e["site"] == t["site"] and e["write_like"] and not e["promoted"]
                 and e["first"] is not None and (last is None or e["first"] > last)]
        same = [e for e in after if e["kind"] == t["kind"]]
        quiet = last is not None and now - last > stale_days * 86400
        candidates = same or (after if quiet else [])
        if not candidates:
            continue
        group = stale.setdefault((t["site"], tuple(e["id"] for e in candidates)),
                                 {"site": t["site"], "kind": t["kind"] if same else None, "rules": [],
                                  "candidates": candidates})
        group["rules"].append(f"{t['id']} last matched {clock.iso_or_none(last, timespec="seconds")}" if last is not None
                              else f"{t['id']} never matched in what the traces keep (since {clock.iso_or_none(oldest, timespec="seconds")})")
        group.setdefault("ids", []).append(t["id"])
    return [_stale_flag(g) for g in stale.values()]


def _stale_flag(g: dict[str, Any]) -> dict[str, Any]:
    candidates = g["candidates"]
    lanes = sorted({lane for e in candidates for lane in e["lanes"]})
    rules_said = "; ".join(g["rules"])
    names = ", ".join(_name(e) for e in candidates[:SHAPES_SHOWN])
    more = f" and {len(candidates) - SHAPES_SHOWN} more" if len(candidates) > SHAPES_SHOWN else ""
    kind = g["kind"] or "post"
    return {
        "kind": "stale_rule", "site": g["site"], "rule": g["ids"][0], "rules": g["ids"], "lanes": lanes,
        "shapes": [e["id"] for e in candidates],
        "message": f"{rules_said}. Since then {g['site']} sent {len(candidates)} unknown "
                   f"{'shape that looks' if len(candidates) == 1 else 'shapes that look'} like "
                   f"{'a ' + g['kind'] if g['kind'] else 'writes'}: {names}{more}. The site has likely changed "
                   f"its web client. Check the shape in the gate log, then name it: openajar lane gate-log "
                   f"{lanes[0] if lanes else '<lane>'} --as-commit {kind}.new --shape {candidates[0]['id']}"}


def _spike_flags(shapes: list[dict[str, Any]], flags: list[dict[str, Any]], *, now: float, recent_h: float,
                 spike: int) -> list[dict[str, Any]]:
    """`new_unknowns`: at least `spike` new write-like shapes on one site within `recent_h` hours,
    unless a stale rule's flag (in `flags`) names them all already."""
    out: list[dict[str, Any]] = []
    recent = now - recent_h * 3600
    for site in sorted({e["site"] for e in shapes}):
        new = [e for e in shapes if e["site"] == site and e["write_like"] and not e["promoted"]
               and e["first"] is not None and e["first"] >= recent]
        if len(new) < spike:
            continue
        flagged = {s for f in flags if f["site"] == site for s in f["shapes"]}
        if all(e["id"] in flagged for e in new):
            continue  # a stale rule's flag names them already
        out.append({
            "kind": "new_unknowns", "site": site, "lanes": sorted({lane for e in new for lane in e["lanes"]}),
            "shapes": [e["id"] for e in new],
            "message": f"{site} sent {len(new)} new unknown shapes that look like writes in the last "
                       f"{recent_h:g} h ({', '.join(_name(e) for e in new[:SHAPES_SHOWN])}"
                       f"{' and more' if len(new) > SHAPES_SHOWN else ''}). Its web client may have changed; "
                       "the gate log has them"})
    return out


def _unruled_flag(u: dict[str, Any], shapes: list[dict[str, Any]]) -> dict[str, Any]:
    """`no_rules` for one lane (from no_rules), naming the shapes no rule reads seen there. Adds
    `shapes` to `u` too, as the report lists it."""
    seen = [e for e in shapes if u["lane"] in e["lanes"] and e["sources"].get("unruled")]
    u["shapes"] = [e["id"] for e in seen]
    what = (f"{', '.join(u['sites'])} ({', '.join(u['hosts'])})" if u["sites"] and u["hosts"]
            else ", ".join(u["sites"] or u["hosts"]))
    return {
        "kind": "no_rules", "site": (u["sites"] or u["hosts"])[0], "lanes": [u["lane"]], "shapes": u["shapes"],
        "message": f"{u['lane']}: no effect rules for {what}, so publishes there are not gated (no approval is "
                   "asked; an agent's press is only traced)"
                   + (f". {len(seen)} request {'shape' if len(seen) == 1 else 'shapes'} no rule reads seen there"
                      if seen else "")}


def report(traces: Mapping[str, list[dict[str, Any]]], *, specs: Iterable[Any] = (),
           rules: tuple[egress.EffectRule, ...] | None = None, promoted: Iterable[Mapping[str, Any]] = (),
           now: float | None = None, stale_days: float = STALE_DAYS, recent_h: float = RECENT_H,
           spike: int = SPIKE) -> dict[str, Any]:
    """Rule health from the lanes' egress traces (`traces`: lane id -> rows), the lanes' specs (for
    their site lists), the effect rules and the owner's promotions. See the module docstring."""
    rules = egress.EFFECT_RULES if rules is None else rules
    now = time.time() if now is None else now
    promoted = list(promoted)
    by_id = {r.id: r for r in rules}
    found = _Readings(rules, {p.get("id") for p in promoted})
    for lane, rows in traces.items():
        for row in rows:
            _read_row(found, row, lane, by_id)

    table = _rule_table(rules, promoted, found.hits)
    shapes = sorted(found.unknown.values(), key=lambda e: (-e["count"], e["host"], e["path"]))
    flags = _stale_flags(table, shapes, now=now, stale_days=stale_days, oldest=found.oldest)
    flags += _spike_flags(shapes, flags, now=now, recent_h=recent_h, spike=spike)
    unruled = [u for u in (no_rules(spec, rules) for spec in specs) if u is not None]
    flags += [_unruled_flag(u, shapes) for u in unruled]
    for t in table:
        t.pop("_last")
    for e in shapes:
        e["lanes"] = sorted(e["lanes"])
        e["first"], e["last"] = clock.iso_or_none(e["first"], timespec="seconds"), clock.iso_or_none(e["last"], timespec="seconds")
    return {"generated_at": clock.iso_or_none(now, timespec="seconds"), "since": clock.iso_or_none(found.oldest, timespec="seconds"), "stale_days": stale_days, "rules": table,
            "unknown": shapes, "unruled": unruled, "flags": flags}


def _name(e: Mapping[str, Any]) -> str:
    return f"{e['op']} ({e['id']})" if e.get("op") else f"{e['method']} {e['host']}{e['path']} ({e['id']})"


def covered(entry: str, rules: tuple[egress.EffectRule, ...]) -> bool:
    """Does an effect rule name a host of this site list entry (or is it a host openajar treats as
    part of a ruled platform: its CDNs, its sign-in and captcha hosts)?"""
    base = entry.removeprefix("*.")
    if "@cc" in base or entry in egress.IDENTITY_SITES:
        return True
    ruled = {r.site for r in rules}
    if any(entry in hosts for platform, hosts in egress.PLATFORM_SITES.items() if platform in ruled):
        return True
    return any(globs.domain_match(r.host, h) for r in rules for h in (base, "www." + base))


def no_rules(spec: Any, rules: tuple[egress.EffectRule, ...] | None = None) -> dict[str, Any] | None:
    """A browser lane on a site the effect rules do not cover: the entries of its site list no rule
    names (frame entries aside), and the sites it is expected to be signed into that have no rules.
    None when every one is covered."""
    from openajar.node import sites

    rules = egress.EFFECT_RULES if rules is None else rules
    if str(getattr(spec, "kind", "browser")) != "browser":
        return None
    hosts = [e for e in getattr(spec, "sites", None) or [] if not e.startswith(egress.FRAME_PREFIX)
             and not covered(e, rules)]
    ruled = {r.site for r in rules}
    try:
        expected = sorted(sites.expectation_for(spec).sites - ruled)
    except Exception:  # noqa: BLE001 - a spec without a name or account: nothing expected
        expected = []
    names = [(sites.site(k).name if sites.site(k) else k) for k in expected]
    if not hosts and not names:
        return None
    return {"lane": spec.id, "sites": names, "hosts": hosts}


def lane_traces(home: Path, lane: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Every lane's egress trace (the rolled file first), or one lane's."""
    from openajar.core.layout import lane_dirs

    out = {}
    for d in lane_dirs(home):
        if lane is None or d.id == lane:
            out[d.id] = gateshapes.trace_rows(d.path)
    return out


def for_node(home: Path, specs: Iterable[Any], *, lane: str | None = None,
             rules: tuple[egress.EffectRule, ...] | None = None, **kw: Any) -> dict[str, Any]:
    """report() for a node home: its lanes' traces, its promoted shapes, the given lane specs."""
    specs = [s for s in specs if lane is None or s.id == lane]
    promoted = gateshapes.load(gateshapes.store_path(home))
    return report(lane_traces(home, lane), specs=specs, rules=rules, promoted=promoted, **kw)
