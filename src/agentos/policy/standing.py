"""Standing approvals: the part of ApprovalStore that adds, lists, revokes and consumes them, and
those a controller issues (agentos.node.channel.standing). A mixin, so the store stays one object
with one database; the rules are in agentos.policy.approvals' docstring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agentos.core import globs
from agentos.core.errors import InvalidRequest, NotFound
from agentos.policy.approval_binding import (
    _DIGEST_RE,
    _TARGET_RE,
    NAMED_TARGETS,
    call_targets,
    content_digest,
)
from agentos.policy.approval_models import StandingApproval, _row_to_standing, decision_problem
from agentos.policy.errors import Conflict, Denied
from agentos.policy.grants import Grant
from agentos.policy.redact import redact_text
from agentos.policy.util import new_id, utc_day


class StandingApprovalsMixin:
    # ---- standing approvals ----
    def add_standing(self, *, effects: list[str], workspaces: list[str] | None = None,
                     lanes: list[str] | None = None, until: float, count: int | None = None,
                     per_day: int | None = None, grant_id: str | None = None, starts_at: float | None = None,
                     by: str, note: str | None = None, targets: list[str] | None = None,
                     contents: list[str] | None = None, shape: bool = False) -> StandingApproval:
        """See the module docstring. `targets` are `flow:<id>@<version>`,
        `connector:<site>.<op>@<contract>` or `tool:<name>`;
        `contents` are content digests (content_digest) the owner saw. One without targets is
        accepted but never covers a content-carrying effect (binding_problem says why).
        `shape` ("always allow this shape"): any content of its named flow versions or connector
        operations on its lanes, so it needs named targets and no contents; consume_standing applies
        every other rule to it as to a template (the policy may still require exact content, for
        example after untrusted text)."""
        now = self.clock()
        effects, workspaces, lanes, targets, contents = self.standing_values(
            effects=effects, workspaces=workspaces, lanes=lanes, until=until, count=count, per_day=per_day,
            targets=targets, contents=contents, shape=shape)
        sa = StandingApproval(id=new_id("sa"), effects=effects, workspaces=workspaces, lanes=lanes, grant_id=grant_id,
                              max_uses=count, uses=0, per_day=per_day, starts_at=starts_at or now, expires_at=until,
                              created_by=by, created_at=now, revoked_at=None,
                              note=redact_text(note, max_len=300) if note else None, targets=targets,
                              contents=contents, shape=shape)
        self.db.execute(
            "INSERT INTO standing (id, effects, workspaces, lanes, grant_id, max_uses, uses, per_day, starts_at, "
            "expires_at, created_by, created_at, note, targets, contents, shape) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sa.id, json.dumps(effects), json.dumps(workspaces), json.dumps(lanes), grant_id, count, per_day,
             sa.starts_at, until, by, now, sa.note, json.dumps(targets), json.dumps(contents), int(shape)))
        self.bus.publish("approval", {"standing": sa.public(), "state": "created"})
        return sa

    def standing_values(self, *, effects: list[str], workspaces: list[str] | None, lanes: list[str] | None,
                        until: float, count: int | None, per_day: int | None, targets: list[str] | None,
                        contents: list[str] | None, shape: bool = False
                        ) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
        """The rules every standing approval follows, wherever it comes from: (effects, workspaces,
        lanes, targets, contents) sorted and checked, or InvalidRequest."""
        now = self.clock()
        targets = sorted(set(targets or []))
        contents = sorted(set(contents or []))
        if shape and (not targets or any(not t.startswith(NAMED_TARGETS) for t in targets) or contents):
            raise InvalidRequest("a shape approval names flow versions or connector operations (targets), and no "
                                 "contents: it covers what they post on its lanes")
        bad_t = [t for t in targets if not _TARGET_RE.match(t)]
        if bad_t:
            raise InvalidRequest(f"standing approval targets look like flow:<id>@<version>, "
                                 f"connector:<site>.<op>@<contract> or tool:<name>, not {bad_t[0][:80]!r}")
        if any(not _DIGEST_RE.match(c) for c in contents):
            raise InvalidRequest("standing approval contents are content digests (sha256:<64 hex>)")
        effects = sorted(set(effects))
        if not effects:
            raise InvalidRequest("a standing approval needs at least one effect")
        bad = [e for e in effects if e not in self.standing_effects]
        if bad:
            raise InvalidRequest(f"a standing approval cannot cover {', '.join(bad)} "
                                 f"(allowed: {', '.join(self.standing_effects)})")
        workspaces = [globs.validate_segment(w) for w in workspaces or []]
        lanes = [globs.validate_lane_pattern(p) for p in lanes or []]
        if not workspaces and not lanes:
            raise InvalidRequest("a standing approval needs a workspace or a lane pattern")
        if until <= now:
            raise InvalidRequest("a standing approval must end in the future")
        if count is not None and count < 1:
            raise InvalidRequest("count must be at least 1")
        if per_day is not None and per_day < 1:
            raise InvalidRequest("per_day must be at least 1")
        return effects, workspaces, lanes, targets, contents

    def binding_problem(self, effects: list[str], targets: list[str] | None) -> str | None:
        """Why a standing approval for these effects would cover nothing that publishes, or None.
        The policy API and CLI refuse such approvals; ApprovalStore.add_standing accepts them."""
        content = sorted(set(effects) & set(self.content_effects))
        if content and not targets:
            return (f"a standing approval for {', '.join(content)} must name what it covers: a flow version "
                    "(flow:<id>@<version>), a connector operation (connector:<site>.<op>@<contract>) or a tool "
                    "(tool:<name>), plus the contents the owner approved (content digests, or from_approvals); a "
                    "flow version or connector operation with no parameters is a template")
        return None

    def binding_from(self, approval_ids: list[str], *, lane: str | None = None,
                     deciders: list[Grant] | None = None) -> tuple[list[str], list[str], list[str]]:
        """(targets, contents, lanes) taken from approvals the owner saw: their tool (and flow
        version), their content digest and their lane. For "approve this, and the same again".

        Only what a person already approved (approved or consumed): a pending one would let whoever
        makes the standing approval decide it without deciding it. With `lane`, every approval must be
        for that lane, so "the same again" never moves to another account. With `deciders` (a member's
        grants), one of them must be able to approve each approval itself (decision_problem: their
        role's approve, four-eyes), so a standing approval is never a way round deciding."""
        targets, contents, lanes = set(), set(), set()
        for i in approval_ids:
            ap = self.get(i)
            if ap.kind != "action" or ap.content_digest is None or ap.lane is None or not (
                    ap.flow_target or ap.tool):
                raise InvalidRequest(f"approval {i} carries no content to approve again")
            if ap.state not in ("approved", "consumed"):
                raise InvalidRequest(f"approval {i} is {ap.state}: approve it first, then allow the same again")
            if lane is not None and ap.lane != lane:
                raise InvalidRequest(f"approval {i} is for {ap.lane}, not {lane}")
            if deciders is not None:
                problems = [decision_problem(ap, g, "approve", four_eyes=self.policy.four_eyes, in_person=True)
                            for g in deciders]
                if all(p is not None for p in problems):
                    raise problems[0] if problems else Denied("not_approver", "nobody here may approve this")
            targets.add(ap.flow_target or f"tool:{ap.tool}")
            contents.add(ap.content_digest)
            lanes.add(ap.lane)
        return sorted(targets), sorted(contents), sorted(lanes)

    def list_standing(self, *, active_only: bool = True) -> list[StandingApproval]:
        sql = "SELECT * FROM standing"
        params: list[Any] = []
        if active_only:
            sql += " WHERE revoked_at IS NULL AND expires_at > ?"
            params.append(self.clock())
        return [_row_to_standing(r) for r in self.db.all(sql + " ORDER BY created_at DESC", params)]

    def revoke_standing(self, standing_id: str, *, by: str) -> StandingApproval:
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM standing WHERE id = ?", (standing_id,)).fetchone()
            if row is None:
                raise NotFound(f"no standing approval {standing_id}")
            if row["revoked_at"] is not None:
                raise Conflict(f"standing approval {standing_id} is already revoked")
            conn.execute("UPDATE standing SET revoked_at = ? WHERE id = ?", (self.clock(), standing_id))
        sa = _row_to_standing(self.db.one("SELECT * FROM standing WHERE id = ?", (standing_id,)))
        self.bus.publish("approval", {"standing": sa.public(), "state": "revoked", "by": by})
        return sa

    # ---- standing approvals a controller sent (agentos.node.channel.standing) ----
    def put_issued_standing(self, standing_id: str, *, issuer: str, effects: list[str], workspaces: list[str],
                            lanes: list[str], until: float, count: int, per_day: int | None, uses: int, by: str,
                            note: str | None, targets: list[str], contents: list[str],
                            starts_at: float | None = None) -> tuple[StandingApproval | None, str]:
        """Add or replace one a controller sent, by its id, under the rules every standing approval
        follows (InvalidRequest otherwise). Returns (it, "created" | "updated" | "unchanged"), (it,
        "revoked") when it was revoked here, since an id never comes back, or (None, "conflict") when
        the id is this node's own or another issuer's. Uses only grow, so a controller that counted
        fewer never refills it; a withdrawn one comes back with its uses."""
        effects, workspaces, lanes, targets, contents = self.standing_values(
            effects=effects, workspaces=workspaces, lanes=lanes, until=until, count=count, per_day=per_day,
            targets=targets, contents=contents)
        now = self.clock()
        note = redact_text(note, max_len=300) if note else None
        fields = (json.dumps(effects), json.dumps(workspaces), json.dumps(lanes), count, per_day,
                  starts_at or now, until, by, note, json.dumps(targets), json.dumps(contents))
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM standing WHERE id = ?", (standing_id,)).fetchone()
            if row is not None and row["issuer"] != issuer:
                return None, "conflict"
            if row is not None and row["revoked_at"] is not None:
                return _row_to_standing(row), "revoked"
            if row is None:
                conn.execute(
                    "INSERT INTO standing (effects, workspaces, lanes, max_uses, per_day, starts_at, expires_at, "
                    "created_by, note, targets, contents, id, uses, created_at, issuer, confirmed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*fields, standing_id, max(0, uses), now, issuer, now))
            else:
                conn.execute(
                    "UPDATE standing SET effects = ?, workspaces = ?, lanes = ?, max_uses = ?, per_day = ?, "
                    "starts_at = ?, expires_at = ?, created_by = ?, note = ?, targets = ?, contents = ?, "
                    "uses = MAX(uses, ?), withdrawn_at = NULL, confirmed_at = ? WHERE id = ?",
                    (*fields, max(0, uses), now, standing_id))
            sa = _row_to_standing(conn.execute("SELECT * FROM standing WHERE id = ?", (standing_id,)).fetchone())
        same = row is not None and _row_to_standing(row).model_copy(update={"confirmed_at": sa.confirmed_at}) == sa
        state = "created" if row is None else ("unchanged" if same else "updated")
        if state != "unchanged":
            self.bus.publish("approval", {"standing": sa.public(), "state": state})
        return sa, state

    def revoke_unseen_standing(self, standing_id: str, *, issuer: str, by: str) -> bool:
        """An id `issuer` revoked before this node ever held it: kept as a revoked row covering
        nothing, so it can never be added later, however many revocations follow. False when the
        id is already held (by anyone)."""
        now = self.clock()
        with self.db.tx() as conn:
            done = conn.execute(
                "INSERT OR IGNORE INTO standing (id, effects, workspaces, lanes, max_uses, uses, starts_at, "
                "expires_at, created_by, created_at, revoked_at, note, issuer) "
                "VALUES (?, '[]', '[]', '[]', 0, 0, ?, ?, ?, ?, ?, ?, ?)",
                (standing_id, now, now, by, now, now, "revoked before it arrived", issuer)).rowcount
        return bool(done)

    def issued_standing(self, issuer: str) -> list[StandingApproval]:
        """Every standing approval `issuer` sent, in any state."""
        return [_row_to_standing(r) for r in self.db.all(
            "SELECT * FROM standing WHERE issuer = ? ORDER BY created_at", (issuer,))]

    def confirm_issued(self, issuer: str) -> int:
        """Its controller reconciled with this node (it sent its whole set, and the channel is up):
        every one it holds here counts as confirmed now. Returns how many."""
        with self.db.tx() as conn:
            return conn.execute("UPDATE standing SET confirmed_at = ? WHERE issuer = ? AND revoked_at IS NULL "
                                "AND withdrawn_at IS NULL", (self.clock(), issuer)).rowcount

    def withdraw_standing(self, standing_id: str, *, issuer: str) -> bool:
        """Its controller no longer sends it (a lane moved, the panel's list changed): it stops
        covering calls but is not revoked, so it can come back with its uses."""
        with self.db.tx() as conn:
            done = conn.execute("UPDATE standing SET withdrawn_at = ? WHERE id = ? AND issuer = ? AND revoked_at "
                                "IS NULL AND withdrawn_at IS NULL", (self.clock(), standing_id, issuer)).rowcount
        if done:
            sa = _row_to_standing(self.db.one("SELECT * FROM standing WHERE id = ?", (standing_id,)))
            self.bus.publish("approval", {"standing": sa.public(), "state": "withdrawn"})
        return bool(done)

    def consume_standing(self, effect: str, lane_id: str, chain_ids: list[str], *, tool: str | None = None,
                         content: Any = None, bound_only: bool = False) -> str | None:
        """Use one unit of a matching standing approval, atomically. Returns its id or None.
        Matching: effect, lane or workspace, grant, the targets (when it names any), and for a
        content-carrying effect the content (see the module docstring). `bound_only` (a call after
        untrusted content): only one bound to what the owner saw counts, for every effect: the
        call's exact content in `contents`, or a named flow version or connector operation run
        with no parameters. A free-form target (a tool alone, or none) never covers it."""
        if effect not in self.standing_effects:
            return None
        now = self.clock()
        call = _StandingCall(effect=effect, lane_id=lane_id, chain_ids=chain_ids, content=content,
                             bound_only=bound_only, now=now, day=utc_day(now),
                             targets=call_targets(tool, content, lane_id))
        with self.db.tx() as conn:
            rows = conn.execute("SELECT * FROM standing WHERE revoked_at IS NULL AND withdrawn_at IS NULL "
                                "AND starts_at <= ? AND expires_at > ? ORDER BY expires_at, created_at",
                                (now, now)).fetchall()
            used: str | None = None
            for r in rows:
                if not self._standing_covers(r, call):
                    continue
                day_uses = r["day_uses"] if r["day"] == call.day else 0
                conn.execute("UPDATE standing SET uses = uses + 1, day = ?, day_uses = ? WHERE id = ?",
                             (call.day, day_uses + 1, r["id"]))
                used = r["id"]
                break
        if used is not None:
            sa = _row_to_standing(self.db.one("SELECT * FROM standing WHERE id = ?", (used,)))
            self.bus.publish("approval", {"standing": sa.public(), "state": "used"})
        return used

    def _standing_covers(self, r: Any, call: _StandingCall) -> bool:
        """Does standing approval row `r` cover this call now, with a use left?"""
        if call.effect not in json.loads(r["effects"]):
            return False
        if not self._issued_live(r, call):
            return False
        if r["grant_id"] and r["grant_id"] not in call.chain_ids:
            return False
        # workspace patterns match like a grant's (biz-* covers biz-a), BUG-11
        if not (any(globs.segment_match(w, call.workspace) for w in json.loads(r["workspaces"]))
                or any(globs.lane_match(p, call.lane_id) for p in json.loads(r["lanes"]))):
            return False
        targets = set(json.loads(r["targets"] or "[]"))
        contents = set(json.loads(r["contents"] or "[]"))
        if targets and not targets & call.targets:
            return False
        if not self._binds(r, call, targets, contents):
            return False
        if r["max_uses"] is not None and r["uses"] >= r["max_uses"]:
            return False
        day_uses = r["day_uses"] if r["day"] == call.day else 0
        return not (r["per_day"] is not None and day_uses >= r["per_day"])

    def _issued_live(self, r: Any, call: _StandingCall) -> bool:
        """One a controller sent covers a call only while this node trusts its issuer and the owner's
        controller ceiling allows the effect (both checked now, not when it arrived), and only while
        its controller keeps confirming it: a revoke the node never received (sent while it was
        offline) bounds its life to this."""
        if r["issuer"] and not (self.issued_ok is not None and self.issued_ok(r["issuer"], call.effect)):
            return False
        return not (r["issuer"] and (r["confirmed_at"] or r["created_at"]) < call.now - self.issued_confirm_s)

    def _binds(self, r: Any, call: _StandingCall, targets: set[str], contents: set[str]) -> bool:
        """Is the call's content one this standing approval covers?"""
        named_flow = any(t.startswith(NAMED_TARGETS) for t in targets & call.targets)
        bound = bool(contents and call.digest() in contents) or (not contents and call.template and named_flow)
        if call.bound_only and not bound:
            return False
        if call.effect in self.content_effects:
            # bound to what the owner saw: a named target, and an approved content or a
            # flow version run as a template (no parameters), AUD-10
            shaped = bool(r["shape"]) and isinstance(call.content, dict) \
                and isinstance(call.content.get("params"), dict) and named_flow and not contents
            # a shape the owner allowed (any content of that flow version or connector operation)
            # never covers a call after untrusted content: bound_only skipped it above
            return bool(targets) and (bound or shaped)
        return not (contents and call.digest() not in contents)


@dataclass
class _StandingCall:
    """One call consume_standing matches against the standing approvals."""
    effect: str
    lane_id: str
    chain_ids: list[str]
    content: Any
    bound_only: bool
    now: float
    day: str
    targets: set[str]
    _digest: list[str] = field(default_factory=list)

    @property
    def workspace(self) -> str:
        return self.lane_id.split("/", 1)[0]

    @property
    def template(self) -> bool:
        """A named flow version or connector operation run with no parameters."""
        return isinstance(self.content, dict) and self.content.get("params") == {} and any(
            t.startswith(NAMED_TARGETS) for t in self.targets)

    def digest(self) -> str:
        """The content's digest, hashed only when a standing approval binds content."""
        if not self._digest:
            self._digest.append(content_digest(self.content))
        return self._digest[0]
