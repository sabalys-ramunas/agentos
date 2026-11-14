"""Writing memory, for MemoryService: plan a write (validate, compute trust, say whether the owner must
approve), store it, and the owner's own acts on an entry (confirm, review, forget).
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from agentos.core.errors import Conflict, InvalidRequest
from agentos.memory.common import (
    Prepared,
    WritePlan,
    attr_text,
    labels_of,
    trim,
)
from agentos.memory.errors import (
    BadEvidence,
    EntryNotFound,
    MemoryConflict,
    OutOfScope,
    OwnerTrustRequired,
    TooLarge,
    TrustTooLow,
    TypeNotAllowed,
)
from agentos.memory.models import (
    DAY,
    LABELS,
    OWNER_ONLY_ATTRS,
    Audience,
    Caller,
    EntryType,
    LinkKind,
    State,
    Trust,
    WriteRequest,
    iso,
    parse_ref,
    validate_attrs,
)
from agentos.memory.scan import strings_of, suspicious_reasons
from agentos.memory.store import (
    NewVersion,
    WorkspaceDB,
    canonical,
    sha256,
)

log = logging.getLogger("agentos.memory")


@dataclass
class _Links:
    pairs: list[tuple[str, str]] = field(default_factory=list)
    derived_trust: list[Trust] = field(default_factory=list)
    derived_labels: set[str] = field(default_factory=set)


@dataclass
class _Trusted:
    trust: Trust
    labels: set[str]
    needs_owner: bool
    confirmed_by: str | None
    ever: set[str]


def _rate_key(caller: Caller) -> str | None:
    """The rate limits' key: the grant family, else the principal. The owner and the node have none."""
    if caller.in_person or caller.is_node:
        return None
    return caller.family if caller.family is not None else f"principal:{caller.principal}"


class WritesMixin:
    """MemoryService's write and owner-act methods."""

    def plan_write(self, caller: Caller, workspace: str, request: WriteRequest | dict[str, Any]) -> WritePlan:
        """Every check `write` makes, without storing: the trust and labels the version would
        get and whether it needs the owner's approval first (`needs_owner_trust`). Raises the
        same refusals as `write`."""
        return self._prepare(caller, workspace, WriteRequest.parse(request)).plan

    def write(self, caller: Caller, workspace: str, request: WriteRequest | dict[str, Any]) -> dict[str, Any]:
        """Create an entry or a new version of one. Raises OwnerTrustRequired when the write
        needs the owner (a decision, a new version of an owner entry, an owner-only attribute)
        and the caller carries no approval for it."""
        req = WriteRequest.parse(request)
        prep = self._prepare(caller, workspace, req)
        plan = prep.plan
        if plan.needs_owner_trust and caller.approval is None:
            raise OwnerTrustRequired(plan.canonical, plan.summary, plan.digest)
        if caller.approval is not None and caller.approval.digest and caller.approval.digest != plan.digest:
            raise Conflict("the approval was given for different content; propose the write again")
        return self._store_prepared(caller, workspace, prep)

    def _check_capacity(self, db: WorkspaceDB, prep: Prepared) -> None:
        lim = self.limits
        if prep.target is None and db.live_count() >= lim.live_entries:
            raise TooLarge(f"{db.workspace} already has {lim.live_entries} live entries; forget or supersede old "
                           "ones first")
        size = len((prep.req.title + prep.req.body).encode()) + len(canonical(prep.attrs).encode())
        if db.content_bytes() + size > lim.workspace_bytes:
            raise TooLarge(f"the memory of {db.workspace} is at its size cap ({lim.workspace_bytes // (1024 * 1024)} MB)")

    def _expiry(self, etype: EntryType, trust: Trust, now: float) -> float | None:
        lim = self.limits
        days = 0
        if etype == EntryType.RUN_SUMMARY:
            days = lim.run_summary_days
        elif etype in (EntryType.FACT, EntryType.PREFERENCE):
            if trust == Trust.UNTRUSTED:
                days = lim.untrusted_fact_days
            elif trust == Trust.AGENT:
                days = lim.agent_fact_days
        return now + days * DAY if days else None

    def _prepare(self, caller: Caller, workspace: str, req: WriteRequest) -> Prepared:
        """Every check a write makes, in order, and what it would store. Raises the first refusal."""
        etype = req.type
        self._check_may_write(caller, workspace, etype, req.audience)
        level = self._privacy(caller, workspace)
        attrs = self._checked_content(req)
        db = self._db(workspace)
        target, expect = self._superseded(caller, workspace, db, req)
        links = self._links(caller, workspace, db, req)
        # evidence (4.3): checked against the workspace and the writer's family
        evidence, ev_labels, source_url, ev_lane = self._check_evidence(caller, workspace, req.evidence)
        trusted = self._trust(caller, etype, attrs, target, level, links, ev_labels)
        suspicious: list[str] = []
        if trusted.trust == Trust.UNTRUSTED:
            allowed = self.ports.egress_allowed
            text = "\n".join([req.title, req.body, *strings_of(attrs)])
            suspicious = suspicious_reasons(text, allowed=(lambda h: allowed(workspace, h)) if allowed else None)
        valid_from, valid_to = self._validity(etype, attrs)

        canon = {
            "workspace": workspace, "type": etype.value, "title": req.title, "body": req.body, "key": req.key,
            "attrs": attrs, "audience": req.audience.value, "supersedes": req.supersedes,
            "relates": req.relates, "derived_from": req.derived_from, "contests": req.contests,
            "evidence": req.evidence,
        }
        digest = sha256(canonical(canon))
        plan = WritePlan(workspace=workspace, trust=trusted.trust, labels=sorted(trusted.labels),
                         needs_owner_trust=trusted.needs_owner, canonical=canon, digest=digest,
                         summary=self._summary(caller, workspace, req, target), suspicious=suspicious)
        prov = {
            "write_trace": caller.write_trace,
            "principal": {"id": caller.principal, "kind": caller.kind, "thread": caller.thread},
            "grant": ({"id": caller.grant_id, "root": caller.grant_root, "path": caller.grant_path}
                      if caller.grant_id else None),
            "in_person": caller.in_person, "node": self.ports.node_id,
            "lane": caller.lane or ev_lane, "source_url": source_url,
            "run_id": caller.run_id, "brain_task_id": caller.brain_task_id,
            "evidence": evidence, "taint_at_write": sorted(trusted.ever & LABELS),
            "derived_from": list(req.derived_from), "confirmed_by": trusted.confirmed_by, "imported_from": None,
        }
        if target is not None:
            entry_id, version = target["id"], target["current"] + 1
        else:
            entry_id, version = "mem_" + secrets.token_hex(8), 1
        return Prepared(plan=plan, req=req, attrs=attrs, db=db, target=target, expect=expect, entry_id=entry_id,
                        version=version, prov=prov, links=links.pairs, valid_from=valid_from, valid_to=valid_to,
                        rate_key=_rate_key(caller))

    def _check_may_write(self, caller: Caller, workspace: str, etype: EntryType, audience: Audience) -> None:
        self._check_scope(caller, workspace, write=True)
        if etype == EntryType.RUN_SUMMARY and not caller.is_node:
            raise TypeNotAllowed("only the node writes run summaries")
        if not caller.scope.may_write(etype):
            raise TypeNotAllowed(f"your grant may not write {etype.value} entries")
        if etype == EntryType.CONTACT and not caller.can_read_private():
            raise TypeNotAllowed("contacts are private: your grant needs memory with private on to write them")
        if audience == Audience.OWNER and not caller.can_read_owner_audience():
            raise OutOfScope("only the owner (or an admin grant) may write owner-only entries")

    def _checked_content(self, req: WriteRequest) -> dict[str, Any]:
        """Sizes and secrets (4.11, 4.14). Returns the validated attributes."""
        lim = self.limits
        etype = req.type
        if len(req.title) > lim.title_chars:
            raise TooLarge(f"the title is {len(req.title)} characters; the cap is {lim.title_chars}")
        if len(req.body) > lim.body_limit(etype):
            raise TooLarge(f"the body is {len(req.body)} characters; a {etype.value} holds at most "
                           f"{lim.body_limit(etype)}. Keep files in lane files or volumes and reference them")
        if len(req.evidence) > lim.evidence_per_version:
            raise TooLarge(f"at most {lim.evidence_per_version} evidence traces per version")
        if len(req.relates) + len(req.derived_from) + len(req.contests) > lim.links_per_version:
            raise TooLarge(f"at most {lim.links_per_version} links per version")
        self.scanner.check(req.title, req.body, attrs=req.attrs)
        attrs = validate_attrs(etype, req.attrs)
        if len(canonical(attrs).encode()) > lim.attrs_bytes:
            raise TooLarge(f"the attributes are over {lim.attrs_bytes} bytes")
        return attrs

    @staticmethod
    def _superseded(caller: Caller, workspace: str, db: WorkspaceDB | None,
                    req: WriteRequest) -> tuple[dict[str, Any] | None, int | None]:
        """The entry this write makes a new version of, and the version it expects to be current.
        Without `supersedes`, a key must not be taken already."""
        if not req.supersedes:
            if req.key and db is not None:
                taken = db.entry_by_key(req.key)
                if taken is not None:
                    raise MemoryConflict(f"the key {req.key} is taken; pass supersedes to write a new version",
                                         {"id": taken["id"], "version": taken["current"], "trust": taken["trust"]})
            return None, None
        target_id, expect = parse_ref(req.supersedes)
        target = db.entry(target_id) if db is not None else None
        if target is None or target["state"] == State.FORGOTTEN or (
                target["audience"] == Audience.OWNER and not caller.can_read_owner_audience()):
            raise EntryNotFound(f"no memory entry {target_id} in {workspace}")
        if target["type"] != req.type.value:
            raise InvalidRequest(f"{target_id} is a {target['type']}; a new version keeps its type")
        if "private" in labels_of(target) and not caller.can_read_private():
            raise OutOfScope(f"{target_id} is private and your grant does not include private memory")
        if req.key and target["key"] and req.key != target["key"]:
            raise InvalidRequest(f"{target_id} has the key {target['key']}; a new version keeps it")
        if target["current"] != expect:
            raise MemoryConflict(f"version {expect} is no longer current",
                                 {"id": target_id, "version": target["current"], "trust": target["trust"]})
        return target, expect

    @staticmethod
    def _links(caller: Caller, workspace: str, db: WorkspaceDB | None, req: WriteRequest) -> _Links:
        """The links a write makes: to entries in the same workspace, visible, not forgotten. What it
        is derived from passes its trust and labels on."""
        out = _Links()
        for kind, ids in ((LinkKind.RELATES, req.relates), (LinkKind.DERIVED_FROM, req.derived_from),
                          (LinkKind.CONTESTS, req.contests)):
            for dst in ids:
                other = db.entry(dst) if db is not None else None
                if other is None or other["state"] == State.FORGOTTEN or (
                        other["audience"] == Audience.OWNER and not caller.can_read_owner_audience()):
                    raise InvalidRequest(f"{kind.value}: no memory entry {dst} in {workspace}")
                if kind == LinkKind.DERIVED_FROM:
                    out.derived_trust.append(Trust(other["trust"]))
                    out.derived_labels |= set(labels_of(other))
                out.pairs.append((kind.value, dst))
        return out

    def _trust(self, caller: Caller, etype: EntryType, attrs: dict[str, Any], target: dict[str, Any] | None,
               level: str, links: _Links, ev_labels: set[str]) -> _Trusted:
        """Trust (4.4) and labels (4.5), and whether the owner must approve the write."""
        base, labels, ever = self._base_trust(caller, etype, level, links, ev_labels)
        owner_attrs = [a for a in OWNER_ONLY_ATTRS.get(etype, ()) if attrs.get(a)]
        needs_owner = False
        if not caller.in_person:
            if etype == EntryType.DECISION or owner_attrs:
                needs_owner = True
            elif target is not None and Trust(target["trust"]) == Trust.OWNER:
                if base == Trust.UNTRUSTED:
                    raise TrustTooLow(base.value, Trust.OWNER.value)
                needs_owner = True
        trust = base
        confirmed_by: str | None = None
        if needs_owner and caller.approval is not None:
            trust = Trust.OWNER
            confirmed_by = caller.approval.id
        if trust == Trust.OWNER:
            labels.discard("untrusted")
        if target is not None and trust.rank < Trust(target["trust"]).rank and not needs_owner:
            raise TrustTooLow(trust.value, target["trust"])
        return _Trusted(trust=trust, labels=labels, needs_owner=needs_owner, confirmed_by=confirmed_by, ever=ever)

    def _base_trust(self, caller: Caller, etype: EntryType, level: str, links: _Links,
                    ev_labels: set[str]) -> tuple[Trust, set[str], set[str]]:
        """The trust a write gets from who writes it and what it read, its labels, and every label
        the writer's grant family ever received."""
        derived_trust, derived_labels = links.derived_trust, links.derived_labels
        ever: set[str] = set()
        if caller.in_person:
            base = Trust.OWNER
        elif caller.is_node:
            base = Trust.lowest(Trust.NODE, *derived_trust) if derived_trust else Trust.NODE
        else:
            if caller.family and self.ports.policy is not None:
                ever = set(self.ports.policy.taint_ever(caller.family))
            seen = ever | ev_labels | derived_labels
            base = Trust.UNTRUSTED if "untrusted" in seen or Trust.UNTRUSTED in derived_trust else Trust.AGENT
        labels = set()
        if not caller.in_person:
            labels |= (ever | ev_labels | derived_labels) & LABELS
        else:
            labels |= derived_labels & {"private"}
        if etype == EntryType.CONTACT or level == "local-only":
            labels.add("private")
        return base, labels, ever

    def _validity(self, etype: EntryType, attrs: dict[str, Any]) -> tuple[float | None, float | None]:
        valid_from = attrs.get("valid_from")
        valid_to = attrs.get("valid_to")
        if etype == EntryType.FACT and valid_from is None:
            valid_from = self.clock()
        if valid_from is not None and valid_to is not None and valid_to < valid_from:
            raise InvalidRequest("valid_to is before valid_from")
        return valid_from, valid_to

    def _check_evidence(self, caller: Caller, workspace: str,
                        trace_ids: Sequence[str]) -> tuple[list[dict], set[str], str | None, str | None]:
        if not trace_ids:
            return [], set(), None, None
        policy = self.ports.policy
        if policy is None:
            raise BadEvidence("this node cannot check evidence traces (no policy service), so none can be cited")
        family = None if (caller.in_person or caller.is_node) else caller.family
        if family is None and not (caller.in_person or caller.is_node):
            raise BadEvidence("the caller has no grant family, so its evidence cannot be checked")
        out: list[dict] = []
        labels: set[str] = set()
        source_url: str | None = None
        lane: str | None = None
        for tid in trace_ids:
            snap = policy.trace_snapshot(tid) if policy.trace_visible(tid, family, workspace) else None
            if snap is None:
                raise BadEvidence(f"trace {tid} does not exist, is not in {workspace}, or is not visible to your "
                                  "grant; cite only traces your own calls made in this workspace")
            labels |= set(snap.get("labels") or ()) & LABELS
            at = snap.get("at")
            out.append({"trace": tid, "tool": snap.get("tool"), "at": iso(at) if isinstance(at, int | float) else at,
                        "status": snap.get("status"), "summary": trim(snap.get("summary"), 200) or None})
            lane = lane or snap.get("lane")
            if source_url is None:
                source_url = policy.lane_url_at(tid)
        return out, labels, source_url, lane

    @staticmethod
    def _summary(caller: Caller, workspace: str, req: WriteRequest, target: dict | None) -> str:
        """The line the owner reads on the approval card. Written by the node, not the agent."""
        what = "a decision" if req.type == EntryType.DECISION else f"a {req.type.value}"
        head = f"{caller.principal} wants to record {what} for {workspace}: {trim(req.title, 120)}."
        if target is not None:
            head = (f"{caller.principal} wants to replace {target['id']}@{target['current']} ({target['trust']} trust) "
                    f"in {workspace} with: {trim(req.title, 120)}.")
        return f"{head} {trim(req.body, 300)}".strip()

    def _require_owner(self, caller: Caller, what: str) -> None:
        if not caller.in_person:
            raise OutOfScope(f"only the owner in person (the signed-in panel or the node CLI) can {what}")

    def confirm(self, caller: Caller, entry_id: str, version: int, *, edit: dict[str, Any] | None = None,
                workspace: str | None = None) -> dict[str, Any]:
        """Promote version `version` (it must be current) to owner trust: a new version by the
        owner, `confirmed_by` the owner's session, `derived_from` the confirmed version. `edit`
        may change title, body and attrs first."""
        self._require_owner(caller, "confirm memory")
        ws, db, row = self._locate(caller, entry_id, workspace)
        if row["state"] == State.FORGOTTEN:
            raise EntryNotFound(f"{entry_id} was forgotten")
        if row["current"] != version:
            raise MemoryConflict(f"version {version} is no longer current",
                                 {"id": entry_id, "version": row["current"], "trust": row["trust"]})
        v = db.version(entry_id, version)
        assert v is not None
        title, body, attrs = self._content(db, v)
        edit = dict(edit or {})
        unknown = set(edit) - {"title", "body", "attrs"}
        if unknown:
            raise InvalidRequest(f"confirm can edit title, body and attrs, not {sorted(unknown)}")
        req = WriteRequest.parse({
            "type": row["type"], "title": edit.get("title", title), "body": edit.get("body", body or ""),
            "attrs": edit.get("attrs", attrs or {}), "supersedes": f"{entry_id}@{version}",
            "audience": row["audience"]})
        prep = self._prepare(caller, ws, req)
        prep.plan.labels = sorted(set(prep.plan.labels) | (set(labels_of(row)) & {"private"}))
        prep.prov["confirmed_by"] = caller.session or caller.principal
        prep.prov["derived_from"] = [f"{entry_id}@{version}"]
        prep.prov["confirmed_from"] = {"trust": row["trust"], "version_hash": v["version_hash"]}
        result = self._store_prepared(caller, ws, prep)
        db.set_fields(entry_id, reviewed_at=self.clock())
        self._event("memory_confirm", {"workspace": ws, "id": entry_id, "version": result["version"],
                                       "from_version": version, "trust": Trust.OWNER.value})
        return result

    def _store_prepared(self, caller: Caller, workspace: str, prep: Prepared) -> dict[str, Any]:
        plan, req = prep.plan, prep.req
        untrusted = plan.trust == Trust.UNTRUSTED
        if prep.rate_key is not None:
            self._rates.check(prep.rate_key, untrusted, self.limits)
        db = prep.db or self._db(workspace, create=True)
        assert db is not None
        self._check_capacity(db, prep)
        now = self.clock()
        nv = NewVersion(
            entry_id=prep.entry_id, version=prep.version, at=now, type=req.type.value, audience=req.audience.value,
            trust=plan.trust.value, labels=plan.labels, title=req.title, body=req.body, attrs=prep.attrs or None,
            valid_from=prep.valid_from, valid_to=prep.valid_to, supersedes=prep.expect, prov=prep.prov,
            key=req.key or (prep.target["key"] if prep.target else None), subject=prep.attrs.get("subject"),
            writer=caller.principal, lane=prep.prov.get("lane"), pinned=bool(prep.attrs.get("pinned")),
            suspicious=bool(plan.suspicious), expires_at=self._expiry(req.type, plan.trust, now), links=prep.links,
            fts_attrs=attr_text(prep.attrs))
        stored = db.append(nv, expect_current=prep.expect)
        if prep.rate_key is not None:
            self._rates.record(prep.rate_key, untrusted)
        if plan.trust == Trust.OWNER or req.type == EntryType.DECISION:
            self._pin_evidence(db, prep.entry_id)
        self._event("memory_write", {"workspace": workspace, "id": prep.entry_id, "version": prep.version,
                                     "version_hash": stored["version_hash"], "trust": plan.trust.value,
                                     "type": req.type.value, "principal": caller.principal,
                                     "confirmed_by": prep.prov.get("confirmed_by")})
        return {"workspace": workspace, "id": prep.entry_id, "version": prep.version,
                "ref": f"{prep.entry_id}@{prep.version}", "type": req.type.value, "key": nv.key,
                "trust": plan.trust.value, "labels": plan.labels, "suspicious": plan.suspicious,
                "expires_at": iso(nv.expires_at), "version_hash": stored["version_hash"]}

    def _pin_evidence(self, db: WorkspaceDB, entry_id: str) -> None:
        """Owner-trust and decision entries keep their evidence traces through trace retention
        (4.3): every trace cited by any version of the entry is pinned to it. A confirmed note
        pins what the agent cited. Never fails the write."""
        pin = getattr(self.ports.policy, "pin", None)
        if not callable(pin):
            return
        traces: list[str] = []
        for v in db.versions(entry_id):
            for item in json.loads(v["prov"]).get("evidence") or []:
                tid = item.get("trace") if isinstance(item, dict) else None
                if tid and tid not in traces:
                    traces.append(tid)
        for tid in traces:
            try:
                pin(tid, entry_id)
            except Exception:
                log.exception("pinning evidence trace %s for %s failed", tid, entry_id)

    def _unpin(self, entry_id: str) -> None:
        unpin = getattr(self.ports.policy, "unpin", None)
        if not callable(unpin):
            return
        try:
            unpin(entry_id)
        except Exception:
            log.exception("releasing the evidence pins of %s failed", entry_id)

    def review(self, caller: Caller, entry_id: str, *, workspace: str | None = None) -> dict[str, Any]:
        """Mark an entry reviewed without promoting it ("Dismiss" in What agents learned)."""
        self._require_owner(caller, "review memory")
        ws, db, _row = self._locate(caller, entry_id, workspace)
        now = self.clock()
        db.set_fields(entry_id, reviewed_at=now)
        return {"workspace": ws, "id": entry_id, "reviewed_at": iso(now)}

    def plan_forget(self, caller: Caller, entry_id: str, reason: str, *,
                    workspace: str | None = None) -> dict[str, Any]:
        """What the owner co-signs to forget an entry: `{canonical, digest, summary}`."""
        ws, _db, row = self._locate(caller, entry_id, workspace)
        self._check_scope(caller, ws, write=True)
        reason = self._check_reason(reason)
        canon = {"workspace": ws, "forget": entry_id, "reason": reason}
        title = trim(row.get("title") or "", 120)
        summary = (f"{caller.principal} wants to forget {entry_id} ({row['type']}, {row['trust']} trust) in {ws}: "
                   f"{title}. Reason: {trim(reason, 200)}. This erases its content in every version")
        return {"workspace": ws, "canonical": canon, "digest": sha256(canonical(canon)), "summary": summary}

    def _check_reason(self, reason: str) -> str:
        reason = (reason or "").strip()
        if not reason:
            raise InvalidRequest("say why the entry is forgotten (the reason is kept, the content is not)")
        if len(reason) > 500:
            raise TooLarge("the reason is over 500 characters")
        self.scanner.check(reason)
        return reason

    def forget(self, caller: Caller, entry_id: str, reason: str, *, workspace: str | None = None) -> dict[str, Any]:
        """Erase the entry's title, body and attributes in every version (4.9). The version
        rows, provenance and hashes stay, so the chain still verifies. Only the owner in
        person, or a caller carrying the owner's approval for exactly this forget."""
        plan = self.plan_forget(caller, entry_id, reason, workspace=workspace)
        ws = plan["workspace"]
        if not caller.in_person:
            if caller.approval is None:
                raise OwnerTrustRequired(plan["canonical"], plan["summary"], plan["digest"])
            if caller.approval.digest and caller.approval.digest != plan["digest"]:
                raise Conflict("the approval was given for a different forget; ask again")
        _ws, db, row = self._locate(caller, entry_id, ws)
        if row["state"] == State.FORGOTTEN:
            stone = db.tombstone(entry_id) or {}
            return {"workspace": ws, "id": entry_id, "state": "forgotten", "already": True,
                    "at": iso(stone.get("at"))}
        if caller.in_person:
            by = caller.session or caller.principal
        else:
            assert caller.approval is not None
            by = f"{caller.principal} (approval {caller.approval.id})"
        now = self.clock()
        erased = db.forget(entry_id, at=now, by=by, reason=plan["canonical"]["reason"])
        self._unpin(entry_id)
        self._event("memory_forget", {"workspace": ws, "id": entry_id, "by": by})
        return {"workspace": ws, "id": entry_id, "state": "forgotten", "versions": row["current"],
                "blobs_erased": erased, "at": iso(now),
                "note": "copies an agent already read, earlier exports and backups are not reached"}
