"""The Needs-you queue: approvals, standing approvals and questions (runtime service "approvals").

States: pending -> approved | denied | expired; approved -> consumed | expired.
- A one-off approval binds to a content_hash (sha256 over canonical JSON of what the node
  will do, including a stated cost and the routing facts) and to the requesting grant, and
  is used once: by the retried call (consume_approved) or by one parked run (consume).
- A standing approval pre-approves an effect on a workspace or lane pattern, for a count
  and/or until a time ("20 posts to biz-a lanes until Friday"), optionally per day. It is
  bound to what the owner saw (AUD-10): `targets` name what it covers (`flow:<id>@<version>`,
  `connector:<site>.<op>@<contract>` or `tool:<name>`), and for a content-carrying effect
  (commit) the call's content must be one the owner approved (`contents`, digests from
  content_digest) or the call must run a named flow version or connector operation with no
  parameters (a template). A connector target names the contract hash, so it survives a
  repair that only changes locators and lapses when the contract changes. One without targets never covers a
  content-carrying effect, so an injected page cannot publish arbitrary text under it.
  One a controller sent (agentos.node.channel.standing) records its `issuer` and covers a call
  only while `issued_ok` says the node trusts that issuer and its ceiling allows the effect, and
  only within `issued_confirm_s` of the last time its controller confirmed it (`confirmed_at`).
- A publish the commit gate caught (agentos.node.commitgate, AUD-03) is an action approval whose
  content is `{"publish": <rule id>, "lane", "record"}` (is_publish): what the request would
  publish, normalized. A retried click consumes it only while it is younger than the policy's
  `publish_approval_max_age_s` (consume_approved `max_age_s`, AUD-20); the gate retires one whose
  click now sends other content (retire).
- A question (`human_ask`) is an approval of kind "question"; approving it records the
  answer, which `approval_wait` returns.
- Who may decide: the owner, an admin grant (the
  owner's own delegate, as before), or a person's grant (`sub.kind = "human"`, a member) whose
  `approve` special covers the approval's effect and lane, for pay up to its `max_usd`. A grant
  family never decides what it asked for, and where the node's four-eyes policy says so, the
  person behind the requesting agent (`requester_owner`) does not decide it either. Denying
  needs only that the decider sees the approval. Routing (who is pushed) never widens this.
- Each action approval records routing facts the node computed itself: `workspace` (from the
  lane), `requester_owner`, `target` (`flow:<id>@<version>` or `tool:<name>`), `amount_usd`
  (pay) and `not_after` (when the run goes stale); a decision records `decided_via`.
- How strong a yes must be (docs/CONFIRMATIONS.md): `requires` is computed once, when the approval is
  created, and the approval keeps it, so a setting changed later cannot lower a pending approval. The
  one exception is a person, signed in, on purpose (`apply_setting_to_waiting`), and `requires_basis`
  keeps what that must not go below. `methods` are the ways that could reach it. A decision records `decided_method` and `decided_level`,
  worked out by the node from how it arrived, never taken from a caller. An approve below `requires` is
  `proof_too_weak`; denying never needs a level.
Every check-then-change runs in one IMMEDIATE transaction, so a decision made twice, or two
calls racing for the last standing use, resolve to exactly one winner.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Principal
from agentos.policy import cards, confirm
from agentos.policy.approval_binding import (
    CONTENT_EFFECTS,
    ActionBinding,
    content_digest,
)
from agentos.policy.approval_models import (
    DECIDED_VIA,
    MAX_WAIT_S,
    NOT_WHOLE,
    SEND_PAGE,
    SEND_TOOL,
    STATES,
    Approval,
    ApprovalPolicy,
    _requester_owner,
    _row_to_approval,
    decision_problem,
    visible_to,
)
from agentos.policy.confirm import ConfirmLevel, MethodRegistry, Person, Proof
from agentos.policy.confirmation import ConfirmationConfig
from agentos.policy.errors import Conflict, Denied, ProofTooWeak, SeeWholeCard
from agentos.policy.grants import Grant, owner_block
from agentos.policy.redact import redact, redact_text
from agentos.policy.scrub import walk
from agentos.policy.standing import StandingApprovalsMixin
from agentos.policy.store import DB
from agentos.policy.traces import EventBus
from agentos.policy.util import b64u, iso, new_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  effect TEXT, tool TEXT, lane TEXT, domain TEXT,
  summary TEXT NOT NULL,
  content_hash TEXT, preview TEXT, options TEXT, answer TEXT, reason TEXT,
  requester TEXT NOT NULL, grant_id TEXT, grant_path TEXT,
  created_at REAL NOT NULL, expires_at REAL NOT NULL,
  decided_by TEXT, decided_at REAL, note TEXT, consumed_at REAL,
  content_digest TEXT, flow_target TEXT,
  workspace TEXT, requester_owner TEXT, target TEXT, amount_usd REAL, not_after REAL, decided_via TEXT,
  follows TEXT, card TEXT, content_json TEXT, edit TEXT,
  requires TEXT, methods TEXT, decided_method TEXT, decided_level TEXT, decided_detail TEXT, requires_basis TEXT,
  binding TEXT, decided_grant TEXT
);
CREATE INDEX IF NOT EXISTS approvals_state ON approvals(state, created_at);
CREATE INDEX IF NOT EXISTS approvals_match ON approvals(content_hash, grant_id, state);
CREATE TABLE IF NOT EXISTS standing (
  id TEXT PRIMARY KEY,
  effects TEXT NOT NULL, workspaces TEXT NOT NULL, lanes TEXT NOT NULL,
  grant_id TEXT,
  max_uses INTEGER, uses INTEGER NOT NULL DEFAULT 0,
  per_day INTEGER, day TEXT, day_uses INTEGER NOT NULL DEFAULT 0,
  starts_at REAL NOT NULL, expires_at REAL NOT NULL,
  created_by TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL, note TEXT,
  targets TEXT NOT NULL DEFAULT '[]', contents TEXT NOT NULL DEFAULT '[]', shape INTEGER NOT NULL DEFAULT 0,
  issuer TEXT, withdrawn_at REAL, confirmed_at REAL
);
CREATE TABLE IF NOT EXISTS outcome_clears (
  approval_id TEXT PRIMARY KEY, cleared_at REAL NOT NULL, cleared_by TEXT NOT NULL
);
"""
# columns added after the first release, for databases created before them
_MIGRATIONS = (("approvals", "content_digest", "TEXT"), ("approvals", "flow_target", "TEXT"),
               ("standing", "targets", "TEXT NOT NULL DEFAULT '[]'"),
               ("standing", "contents", "TEXT NOT NULL DEFAULT '[]'"),
               ("approvals", "workspace", "TEXT"), ("approvals", "requester_owner", "TEXT"),
               ("approvals", "target", "TEXT"), ("approvals", "amount_usd", "REAL"),
               ("approvals", "not_after", "REAL"), ("approvals", "decided_via", "TEXT"),
               ("approvals", "follows", "TEXT"),
               ("approvals", "card", "TEXT"), ("approvals", "content_json", "TEXT"), ("approvals", "edit", "TEXT"),
               ("standing", "shape", "INTEGER NOT NULL DEFAULT 0"),
               ("standing", "issuer", "TEXT"), ("standing", "withdrawn_at", "REAL"),
               ("standing", "confirmed_at", "REAL"),
               ("approvals", "requires", "TEXT"), ("approvals", "methods", "TEXT"),
               ("approvals", "decided_method", "TEXT"), ("approvals", "decided_level", "TEXT"),
               ("approvals", "decided_detail", "TEXT"), ("approvals", "requires_basis", "TEXT"),
               ("approvals", "binding", "TEXT"), ("approvals", "decided_grant", "TEXT"))
# the longest a request node code opens with its own expiry waits (create_action `expires_at`):
# the agenda's cap (agenda_approval_max_days) is at most this
MAX_REQUEST_S = 90 * 86400


_DEFAULT_PREVIEW: Any = object()  # ask_for_action: no preview given, so the content as JSON


def _check_decision_args(decision: str, edit: dict | None, via: str | None) -> None:
    if decision not in ("approve", "deny"):
        raise InvalidRequest("decision must be approve or deny")
    if edit is not None and decision != "approve":
        raise InvalidRequest("only an approval can carry an edit; a denial needs only a reason")
    if via is not None and not DECIDED_VIA.match(via):
        raise InvalidRequest("via is a short lowercase word such as passkey or owner_token")


def _answer(ap: Approval, answer: str | None, note: str | None) -> str:
    """The answer to a question: one of its options when it has any, at most 2000 characters."""
    value = (answer if answer is not None else note or "").strip()
    if not value:
        choose = f", one of: {', '.join(ap.options)}" if ap.options else ""
        raise InvalidRequest(f"answering a question needs an answer{choose}")
    if ap.options and value not in ap.options:
        raise InvalidRequest(f"the answer must be one of: {', '.join(ap.options)}")
    return value[:2000]


def _decided_grant(proof: Proof, decider: Grant | None, refuse_family: str | None) -> str | None:
    """The person behind this decision, for the agenda (approver_of): a member's grant, or the
    owner on the node or in the panel. None when it proves no person (a bearer token)."""
    if proof.level is None:
        return None
    if decider is not None:
        return decider.id
    return "owner" if refuse_family in (None, "owner") else None


def _decided_by(decider: Grant) -> str:
    if decider.owner_token:
        return "owner"
    s = decider.subject
    return s.id if s.kind == "human" else f"{s.kind}:{s.id}"


def ask_for_action(store: Any, binding: ActionBinding, requester: Principal, reason: str | None, *,
                   grant_id: str | None, grant_path: str | None, preview: Any = _DEFAULT_PREVIEW,
                   **card: Any) -> Approval:
    """The one way a card asking the owner to approve an action is made, on the approvals service
    `store`: it binds `binding` (its content_hash and content_digest, ActionBinding), and whoever acts
    checks the same binding again (action_binding_problem). `preview` defaults to the content as
    indented JSON; `card` passes create_action's other facts (ttl_s, flow_target, amount_usd,
    not_after, follows, untrusted, floor, publish)."""
    if preview is _DEFAULT_PREVIEW:
        preview = None if binding.content is None else json.dumps(binding.content, indent=2, sort_keys=True,
                                                                  default=str)
    return store.create_action(
        effect=binding.effect, tool=binding.tool, lane=binding.lane, domain=binding.domain,
        summary=binding.summary, content_hash=binding.content_hash, content_digest=binding.content_digest,
        preview=preview, requester=requester, grant_id=grant_id, grant_path=grant_path, reason=reason,
        content=binding.content, binding=binding.form, **card)


class ApprovalStore(StandingApprovalsMixin):
    """Runtime service "approvals"."""

    # set by PolicyService: raises Denied when a decider grant is revoked, expired or unknown
    check_live: Callable[[Grant], None] | None = None
    # set by PolicyService: (tool, lane, content) -> the approval card (agentos.policy.cards) or None
    card_builder: Callable[[str | None, str | None, Any], dict | None] | None = None
    # set by PolicyService: (issuer, effect) -> whether a standing approval that issuer sent may cover it now
    issued_ok: Callable[[str, str], bool] | None = None
    # set by PolicyService (node.toml controller_standing_confirm_hours): how long one a controller sent
    # covers calls after its controller last confirmed it
    issued_confirm_s: float = 24 * 3600.0
    # set by PolicyService: (approval, param) -> (bytes, mime) of the image its card shows
    card_files: Callable[[Any, str], tuple[bytes, str]] | None = None
    # set by PolicyService on a node: the vault's scrub (agentos.policy.scrub.runtime_scrub), so the
    # summary, preview and card of an approval (the panel, a push, the controller) never hold a vault value
    scrub: Callable[[str], str] | None = None
    # set by PolicyService: the confirmation setting in force, read when an approval is created
    confirmation: Callable[[], ConfirmationConfig] = ConfirmationConfig
    # set by PolicyService: may this person decide this approval, by their own grants?
    may_decide: Callable[[Any, str], bool] = staticmethod(lambda ap, person: person == "owner")
    # set by the panel: does this person hold a passkey? Without a panel nobody can be asked for one
    passkey_holder: Callable[[str], bool] | None = None

    def __init__(self, db: DB, *, bus: EventBus, notifier: Any = None, link_key: bytes,
                 clock: Callable[[], float] = time.time, approval_ttl_s: int = 86400,
                 standing_effects: list[str] | None = None, content_effects: list[str] | None = None,
                 policy: ApprovalPolicy | None = None):
        self.db = db
        with db.lock:
            db.conn.executescript(SCHEMA)
            for table, column, decl in _MIGRATIONS:
                cols = {row[1] for row in db.conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    db.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.content_effects = list(CONTENT_EFFECTS if content_effects is None else content_effects)
        self.bus = bus
        self.notifier = notifier
        self.clock = clock
        self.approval_ttl_s = approval_ttl_s
        self.standing_effects = standing_effects or ["commit"]
        self.policy = policy or ApprovalPolicy()
        self._link_key = link_key
        self.methods: MethodRegistry = confirm.default_registry()  # PolicyService gives it the device table
        self._waiters: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}

    # ---- creating ----
    def create_action(self, *, effect: str, tool: str | None, lane: str | None, domain: str | None,
                      summary: str, content_hash: str, preview: str | None, requester: Principal,
                      grant_id: str | None, grant_path: str | None, reason: str | None,
                      ttl_s: int | None = None, content_digest: str | None = None,
                      flow_target: str | None = None, amount_usd: float | None = None,
                      not_after: float | None = None, follows: list[dict] | None = None, content: Any = None,
                      card: dict | None = None, untrusted: bool = False,
                      floor: ConfirmLevel | None = None, publish: bool = False,
                      binding: str | None = None, expires_at: float | None = None) -> Approval:
        """`amount_usd` (pay) and `not_after` are facts the node computed; the workspace, the
        requester's owner and the target are taken from the lane, the requester and the tool.
        `follows`: the untrusted origins (kind, host) the call follows, for an "after reading X" card.
        `content` is what the approval binds: the card is built from it (card_builder), and it is
        kept only when the card has a param the approver may edit. The summary, the preview and the
        card pass the vault's scrub (`scrub`) before they are stored, shown, pushed or sent.
        `untrusted`: the call follows untrusted text, whatever `follows` shows (the trifecta, an effect
        outside `[taint] downgrade`). `floor`: the creator's own minimum for `requires`. `publish`: a
        publish the commit gate caught, past no trifecta (ConfirmationConfig.requires).
        `expires_at`: when the request closes, set by node code for a request that should wait
        longer (or shorter) than `approval_ttl_s` (the agenda's, until its entry's start); kept
        within MAX_REQUEST_S, and ignored when it is not in the future."""
        now = self.clock()
        expires = now + (ttl_s or self.approval_ttl_s)
        if expires_at is not None and expires_at > now:
            expires = min(float(expires_at), now + MAX_REQUEST_S)
        if card is None and content is not None and self.card_builder is not None:
            try:
                card = self.card_builder(tool, lane, content)
            except Exception:  # noqa: BLE001 - a card is a view, never a reason to refuse
                card = None
        # a send card keeps its messages, so the person can read every one (send_messages)
        kept = content if (card and card.get("editable")) or tool == SEND_TOOL else None
        if self.scrub is not None:  # fails closed: a string the vault cannot check is withheld
            summary, preview, card = walk(summary, self.scrub), walk(preview, self.scrub), walk(card, self.scrub)
        ap = Approval(id=new_id("ap"), kind="action", state="pending", effect=effect, tool=tool, lane=lane,
                      domain=domain, summary=redact_text(summary, max_len=1000), content_hash=content_hash,
                      preview=redact_text(preview, max_len=2000) if preview else None, reason=reason,
                      requester=_requester(requester), grant_id=grant_id, grant_path=grant_path,
                      created_at=now, expires_at=expires, content_digest=content_digest,
                      flow_target=flow_target, workspace=lane.split("/", 1)[0] if lane else None,
                      requester_owner=requester.owner, target=flow_target or (f"tool:{tool}" if tool else None),
                      amount_usd=amount_usd, not_after=not_after, follows=follows or None, card=card,
                      content=kept, binding=binding)
        self._fix_requires(ap, untrusted=untrusted or bool(follows), floor=floor, publish=publish)
        self._insert(ap)
        self._announce(ap)
        return ap

    def ask_for_action(self, binding: ActionBinding, requester: Principal, reason: str | None, *,
                       grant_id: str | None, grant_path: str | None, preview: Any = _DEFAULT_PREVIEW,
                       **card: Any) -> Approval:
        """See the module's ask_for_action."""
        return ask_for_action(self, binding, requester, reason, grant_id=grant_id, grant_path=grant_path,
                              preview=preview, **card)

    def create_question(self, *, question: str, options: list[str] | None, requester: Principal,
                        grant_id: str | None, grant_path: str | None, ttl_s: int | None = None,
                        workspace: str | None = None) -> Approval:
        """`workspace`: the one workspace the asking grant works in (Grant.workspace()), so members
        of it can answer; without one only the owner and members of every workspace can."""
        question = question.strip()
        if not question or len(question) > 2000:
            raise InvalidRequest("a question needs 1 to 2000 characters")
        if options is not None:
            options = [o.strip() for o in options if o and o.strip()]
            if not 1 <= len(options) <= 10 or any(len(o) > 200 for o in options) or len(set(options)) != len(options):
                raise InvalidRequest("options must be 1 to 10 distinct strings of at most 200 characters")
        now = self.clock()
        ap = Approval(id=new_id("ap"), kind="question", state="pending", summary=redact_text(question, max_len=2000),
                      options=options, requester=_requester(requester), grant_id=grant_id, grant_path=grant_path,
                      created_at=now, expires_at=now + (ttl_s or self.approval_ttl_s), workspace=workspace,
                      requester_owner=requester.owner)
        self._fix_requires(ap)
        self._insert(ap)
        self._announce(ap)
        return ap

    def _fix_requires(self, ap: Approval, *, untrusted: bool = False, floor: ConfirmLevel | None = None,
                      publish: bool = False) -> None:
        """`requires` and `methods`, from the setting as it is now: the approval keeps them."""
        setting = self.confirmation()
        ap.requires = setting.requires(ap.effect if ap.kind == "action" else None, untrusted=untrusted, floor=floor,
                                       workspace=ap.workspace or (ap.lane or "").partition("/")[0] or None,
                                       publish=publish).word
        ap.methods = self.methods.reaching(ap, self.person(_requester_owner(ap)), off=setting.off_in(ap.workspace))
        ap.requires_basis = {"untrusted": untrusted, "floor": floor.word if floor is not None else None,
                             **({"publish": True} if publish else {})}

    def apply_setting_to_waiting(self) -> list[dict]:
        """A person's way out of a level that leaves approvals stuck: `requires` and `methods` of what
        waits, worked out again from the setting in force and from the facts that held each one up, so
        a floor stays a floor. An approval from before those facts were kept counts as following
        untrusted text, so it never takes chat. Only a person's change reaches this, never a controller's.
        Returns what moved."""
        moved: list[dict] = []
        with self.db.tx() as conn:
            rows = conn.execute("SELECT * FROM approvals WHERE state = 'pending' AND expires_at > ?",
                                (self.clock(),)).fetchall()
            for row in rows:
                ap = _row_to_approval(row)
                was, methods = ap.requires, ap.methods
                basis = ap.requires_basis or {"untrusted": True}
                floor = ConfirmLevel.of(basis["floor"]) if basis.get("floor") else None
                self._fix_requires(ap, untrusted=bool(basis.get("untrusted")), floor=floor,
                                   publish=bool(basis.get("publish")))
                if (ap.requires, ap.methods) == (was, methods):
                    continue
                conn.execute("UPDATE approvals SET requires = ?, methods = ?, requires_basis = ? WHERE id = ? "
                             "AND state = 'pending'", (ap.requires, json.dumps(ap.methods),
                                                       json.dumps(ap.requires_basis), ap.id))
                if ap.requires != was:
                    moved.append({"id": ap.id, "was": was, "now": ap.requires})
        for item in moved:
            self._publish(self.get(item["id"]))
        return moved

    def person(self, person_id: str) -> Person:
        return Person(person_id, passkey=bool(self.passkey_holder and self.passkey_holder(person_id)))

    def backfill_requires(self) -> int:
        """Approvals waiting when this node learned levels: what each needed then, never less, never
        chat. A decided one keeps no level: it was not decided with one."""
        if self.db.one("SELECT 1 FROM approvals WHERE requires IS NULL AND state = 'pending' LIMIT 1") is None:
            return 0  # the usual case: no write lock for a CLI that only starts up
        setting = self.confirmation()
        with self.db.tx() as conn:
            rows = conn.execute("SELECT id, kind, effect, follows FROM approvals WHERE requires IS NULL "
                                "AND state = 'pending'").fetchall()
            for r in rows:
                word = setting.requires(r["effect"] if r["kind"] == "action" else None, untrusted=bool(r["follows"]),
                                        floor=confirm.legacy_requires(r["effect"] if r["kind"] == "action" else None))
                conn.execute("UPDATE approvals SET requires = ?, methods = '[]' WHERE id = ?", (word.word, r["id"]))
        return len(rows)

    def _insert(self, ap: Approval) -> None:
        d = ap.model_dump()
        d["requester"] = json.dumps(d["requester"])
        d["options"] = json.dumps(d["options"]) if d["options"] is not None else None
        d["follows"] = json.dumps(d["follows"]) if d["follows"] else None
        d["methods"] = json.dumps(d["methods"]) if d["methods"] is not None else None
        d["requires_basis"] = json.dumps(d["requires_basis"]) if d["requires_basis"] else None
        d["decided_detail"] = json.dumps(d["decided_detail"]) if d["decided_detail"] else None
        for key, column in (("card", "card"), ("content", "content_json"), ("edit", "edit")):
            value = d.pop(key)
            d[column] = json.dumps(value, sort_keys=True, default=str) if value is not None else None
        cols = ", ".join(d)
        self.db.execute(f"INSERT INTO approvals ({cols}) VALUES ({', '.join(':' + k for k in d)})", d)

    def _announce(self, ap: Approval) -> None:
        self._publish(ap)
        if self.notifier is None:
            return
        who = ap.requester.get("id", "an agent")
        if ap.kind == "question":
            title = f"{who} asks"
            body = ap.summary + (f"\nOptions: {', '.join(ap.options)}" if ap.options else "")
        else:
            title = f"{ap.lane or 'agentos'}: {ap.effect} needs you"
            body = ap.summary
            after = ap.follows_words()
            if after:  # an "after reading X" approval: the owner checks it was their idea
                title = f"{ap.lane or 'agentos'}: {ap.effect} after reading {after}"
                body = (f"This follows untrusted text from {after}. Check that it is what you want, "
                        f"not what the text asked for.\n\n{body}")
            if ap.preview:
                body += f"\n\n{ap.preview[:500]}"
                if len(ap.preview) > 500:
                    body += f"\n[{len(ap.preview) - 500} more characters: open the panel to see them]"
        self.notifier.send({"type": f"{ap.kind}.created", "approval_id": ap.id, "title": title, "body": body,
                            "actionable": ap.kind == "action", "expires_ts": ap.expires_at,
                            **({"after_untrusted": ap.follows_words()} if ap.follows else {})})

    def _publish(self, ap: Approval) -> None:
        self.bus.publish("approval", ap.public())
        for loop, ev in list(self._waiters.get(ap.id, ())):  # may run on policy.db's worker thread
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass

    # ---- reading ----
    def get(self, approval_id: str) -> Approval:
        self._expire_one(approval_id)
        row = self.db.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if row is None:
            raise NotFound(f"no approval {approval_id}")
        return _row_to_approval(row)

    def list(self, *, state: str | None = None, limit: int = 50, viewer: str | None = None,
             kind: str | None = None, viewer_grant: Grant | None = None) -> list[Approval]:
        """Newest first. `viewer` (a grant id) keeps that grant family's own approvals;
        `viewer_grant` keeps what that grant may see (visible_to): for a member, the approvals on
        the lanes it covers."""
        if viewer_grant is not None and not (viewer_grant.owner_token or viewer_grant.admin):
            if not viewer_grant.human:
                return self.list(state=state, limit=limit, viewer=viewer_grant.id, kind=kind)
            want = max(1, min(int(limit), 500))
            out: list[Approval] = []
            offset = 0
            while len(out) < want:
                page = self.list_rows(state=state, limit=500, kind=kind, offset=offset)
                out += [a for a in page if self.visible_to(a, viewer_grant)]
                if len(page) < 500:
                    break
                offset += 500
            return out[:want]
        return self.list_rows(state=state, limit=limit, viewer=viewer, kind=kind)

    def list_rows(self, *, state: str | None = None, limit: int = 50, viewer: str | None = None,
                  kind: str | None = None, offset: int = 0) -> list[Approval]:
        self.sweep()
        if state is not None and state not in STATES:
            raise InvalidRequest(f"state must be one of {', '.join(STATES)}")
        if kind is not None and kind not in ("action", "question"):
            raise InvalidRequest("kind must be action or question")
        where, params = [], []
        if state:
            where.append("state = ?")
            params.append(state)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if viewer is not None:
            where.append("grant_path LIKE ?")
            params.append(f"%/{viewer}/%")
        sql = "SELECT * FROM approvals" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY created_at DESC, id LIMIT ? OFFSET ?"
        params += [max(1, min(int(limit), 500)), max(0, int(offset))]
        return [_row_to_approval(r) for r in self.db.all(sql, params)]

    visible_to = staticmethod(visible_to)

    def can_decide(self, ap: Approval, grant: Grant, decision: str = "approve") -> bool:
        """Could this (live) grant make `decision` on `ap`, with this node's four-eyes policy? The
        pure check behind decide(), for the notifier: who to tell about a new approval."""
        return decision_problem(ap, grant, decision, four_eyes=self.policy.four_eyes) is None

    def count_pending(self, family: str) -> int:
        """Pending approvals and questions of a grant family (its root grant id)."""
        prefix = f"/{family}/"
        row = self.db.one("SELECT COUNT(*) AS n FROM approvals WHERE substr(grant_path, 1, ?) = ? "
                          "AND state = 'pending' AND expires_at > ?", (len(prefix), prefix, self.clock()))
        return int(row["n"]) if row else 0

    # ---- deciding ----
    def decide(self, approval_id: str, decision: str, *, by: str | None = None, note: str | None = None,
               answer: str | None = None, refuse_family: str | None = None, decider: Grant | None = None,
               via: str | None = None, in_person: bool = False, edit: dict | None = None,
               proof: Proof | None = None, content: tuple[str | None, str | None] | None = None,
               seen: str | None = None) -> Approval:
        """Approve or deny (or answer) a pending approval.

        `decider` is the grant of whoever decides: the owner's, an admin grant, or a member's
        (a person's grant with `approve`). It must be live (`check_live`), and decision_problem
        says whether it may decide this approval: `not_approver`, `self_approval` or `four_eyes`.
        `by` is what Activity shows (a member's handle; default: the decider's subject id) and
        `via` how the person proved it (passkey, code, owner_token, ...). `in_person`: a signed-in
        panel session (no grant family rule for a person deciding there).

        Without `decider` (the node CLI, a controller relaying the owner's decision) the caller is
        the owner: only `refuse_family`, the root grant id of a decider acting under a token, is
        checked (a grant family never decides what it asked for, BUG-5), plus four-eyes as the owner.

        `edit` ({param: new text}) approves with an edit: only the card's editable params (a connector
        or flow param shown verbatim, such as a caption). The approval then binds the edited content
        (its content_digest), the parked run uses it (agentos.runner releases it with the edit), and
        a retried call with the old content never consumes it.

        `proof` says how strong this yes is. Only the node's own code makes one (a panel route, the
        chat tool, the channel); without it the node words it from how the decision arrived
        (confirm.arrival). An approve below what the approval requires is `proof_too_weak`. `content`
        ({content_hash}, {content_digest}): what the person saw, checked in this same transaction.
        `seen`: for a send card whose preview is cut (Approval.needs_whole_view), the token the last
        page of send_messages gave; without it an approve is `see_whole_card`, whatever the path."""
        _check_decision_args(decision, edit, via)
        if decider is not None and self.check_live is not None:
            self.check_live(decider)
        if by is None:
            by = _decided_by(decider) if decider is not None else "owner"
        now = self.clock()
        note = redact_text(note.strip(), max_len=1000) if note and note.strip() else None
        proof = proof or confirm.arrival(via=via, decider=decider, in_person=in_person, at=now)
        setting = self.confirmation()
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                raise NotFound(f"no approval {approval_id}")
            ap = _row_to_approval(row)
            problem = self._decision_problem(ap, decision, decider, refuse_family, in_person)
            if problem is not None:
                raise problem
            self._check_still_open(conn, ap, now, decision, content=content, seen=seen)
            if decision == "approve":
                proof = self._strong_enough(ap, proof, setting.off_in(ap.workspace))
            new_state = "approved" if decision == "approve" else "denied"
            value = _answer(ap, answer, note) if ap.kind == "question" and decision == "approve" else None
            expires = now + self.approval_ttl_s if new_state == "approved" else ap.expires_at
            if edit is not None:
                self._apply_edit(conn, ap, edit, by=by[:120], now=now)
            level = proof.level.word if proof.level is not None else None
            grant = _decided_grant(proof, decider, refuse_family)
            conn.execute("UPDATE approvals SET state = ?, decided_by = ?, decided_at = ?, note = ?, answer = ?, "
                         "expires_at = ?, decided_via = ?, decided_method = ?, decided_level = ?, decided_detail = ?, "
                         "decided_grant = ? WHERE id = ? AND state = 'pending'",
                         (new_state, by[:120], now, note, value, expires, via, proof.method, level,
                          json.dumps(proof.detail, sort_keys=True) if proof.detail else None, grant, ap.id))
        ap = self.get(approval_id)
        self._publish(ap)
        return ap

    def _decision_problem(self, ap: Approval, decision: str, decider: Grant | None, refuse_family: str | None,
                          in_person: bool) -> Denied | None:
        """decision_problem for this decider; without one, the owner deciding in person."""
        if decider is not None:
            return decision_problem(ap, decider, decision, four_eyes=self.policy.four_eyes,
                                    refuse_family=refuse_family, in_person=in_person)
        return decision_problem(ap, Grant([owner_block()], owner_token=True), decision,
                                four_eyes=self.policy.four_eyes, refuse_family=refuse_family, in_person=True)

    def _check_still_open(self, conn: Any, ap: Approval, now: float, decision: str, *,
                          content: tuple[str | None, str | None] | None, seen: str | None) -> None:
        """Still pending (one past its time expires now), for the content the person saw, and for a
        cut send card, seen whole."""
        if ap.state == "pending" and ap.expires_at <= now:
            conn.execute("UPDATE approvals SET state = 'expired' WHERE id = ?", (ap.id,))
            ap.state = "expired"
        if ap.state != "pending":
            raise Conflict(f"approval {ap.id} is already {ap.state}")
        if content is not None and content != (ap.content_hash, ap.content_digest):
            raise Conflict(f"approval {ap.id} holds other content than the person saw")
        if decision == "approve" and ap.needs_whole_view() and not self._seen_ok(ap, seen):
            raise SeeWholeCard("this card shows only part of the messages it sends. Open it in the panel, "
                               "read every message (show all), and approve it there", ap.id)

    # ---- a send card's whole list ----
    def _page_sig(self, ap: Approval, what: str) -> str:
        msg = f"agentos-send-list-v1|{ap.id}|{ap.content_digest}|{what}".encode()
        return b64u(hmac.new(self._link_key, msg, hashlib.sha256).digest())

    def _seen_ok(self, ap: Approval, seen: str | None) -> bool:
        return isinstance(seen, str) and hmac.compare_digest(self._page_sig(ap, "seen-all"), seen)

    def send_messages(self, approval_id: str, page: int = 0, cursor: str | None = None) -> dict:
        """One page of a send card's messages, each as it is sent (redacted and scrubbed as the card
        is). Pages are read in order: page 0 needs nothing, each later one the `cursor` the page
        before it gave, and the last page gives `seen`, the token an approve of a cut card needs
        (decide). A message with a secret or vault value in it shows it redacted, as every card
        does, and is marked `withheld`."""
        ap = self.get(approval_id)
        if ap.tool != SEND_TOOL:
            raise InvalidRequest("this approval sends no messages")
        messages = ap.content.get("messages") if isinstance(ap.content, dict) else None
        if not isinstance(messages, list):
            raise Conflict("this card was made before agentos kept every message on it, so they cannot be shown. "
                           "Deny it and retry the run: the new card shows every message")
        pages = max(1, -(-len(messages) // SEND_PAGE))
        if not isinstance(page, int) or isinstance(page, bool) or page < 0 or page >= pages:
            raise InvalidRequest(f"page is 0 to {pages - 1}")
        if page > 0 and not (isinstance(cursor, str) and hmac.compare_digest(self._page_sig(ap, f"page:{page}"), cursor)):
            raise InvalidRequest("read the pages in order: each page gives the cursor of the next")
        shown = [self._shown_message(m) for m in messages]
        withheld = sum(1 for _, whole in shown if not whole)
        rows = [m for m, _ in shown[page * SEND_PAGE:(page + 1) * SEND_PAGE]]
        last = page == pages - 1
        out: dict[str, Any] = {"approval_id": ap.id, "count": len(messages), "page": page, "pages": pages,
                               "page_size": SEND_PAGE, "messages": rows, "withheld": withheld,
                               "next": None if last else {"page": page + 1, "cursor": self._page_sig(ap, f"page:{page + 1}")},
                               "seen": None}
        if last and ap.state == "pending":
            out["seen"] = self._page_sig(ap, "seen-all")
        return out

    def _shown_message(self, message: Any) -> tuple[dict, bool]:
        """A message as the person reads it, and whether that is the whole of it."""
        safe = redact(message, max_len=200_000)  # as the preview measures it: a long body is shown whole
        if self.scrub is not None:
            safe = walk(safe, self.scrub)
        if not isinstance(safe, dict):
            return {"to": None, "inputs": safe}, False
        whole = safe == message and not any(mark in json.dumps(safe, ensure_ascii=False, default=str)
                                            for mark in NOT_WHOLE)
        return {**safe, **({} if whole else {"withheld": True})}, whole

    def _strong_enough(self, ap: Approval, proof: Proof, off: list[str]) -> Proof:
        """The proof its method stands behind, or why it is not enough for this approval."""
        checked = self.methods.verify(ap, proof, off=off)
        need = confirm.requires_of(ap)
        if checked.person is not None and not self.may_decide(ap, checked.person):
            raise Denied("not_approver", "the person whose device signed cannot decide this approval")
        if checked.counts is None or checked.counts < need:
            raise ProofTooWeak(need.word, f"this needs {need.word}, and this decision "
                                          f"{f'reached {checked.counts.word}' if checked.counts else 'proves no person'}. "
                                          "Decide it in the panel, signed in", ap.id)
        return checked

    def _apply_edit(self, conn: Any, ap: Approval, edit: dict, *, by: str, now: float) -> None:
        """Bind the approval to the edited content (inside decide's transaction)."""
        if ap.kind != "action":
            raise InvalidRequest("a question is answered, not edited")
        new = cards.edited_content(ap.content, ap.card, edit)
        changed = sorted(k for k in edit if ap.content["params"].get(k) != new["params"][k])
        card = cards.with_edit(ap.card, new["params"])
        record = {"params": {k: new["params"][k] for k in changed}, "by": by, "at": now,
                  "digest_before": ap.content_digest}
        summary = redact_text(f"{ap.summary} (approved with an edit by {by}: {', '.join(changed)})", max_len=1000)
        conn.execute("UPDATE approvals SET content_digest = ?, content_json = ?, card = ?, edit = ?, summary = ?, "
                     "preview = ? WHERE id = ? AND state = 'pending'",
                     (content_digest(new), json.dumps(new, sort_keys=True), json.dumps(card, sort_keys=True),
                      json.dumps(record, sort_keys=True), summary, _preview_text(new), ap.id))

    def batch(self, ids: list[str], decision: str, *, by: str | None = None, note: str | None = None,
              refuse_family: str | None = None, decider: Grant | None = None,
              via: str | None = None, in_person: bool = False, proof: Proof | None = None) -> dict[str, str]:
        out = {}
        for i in ids:
            try:
                out[i] = self.decide(i, decision, by=by, note=note, refuse_family=refuse_family, decider=decider,
                                     via=via, in_person=in_person, proof=proof).state
            except (Conflict, NotFound, InvalidRequest, Denied) as e:
                out[i] = f"{e.code}: {e.message}"
        return out

    def chat_clients(self) -> list[dict]:
        """The chat clients decisions came through, by the name each gave itself: what the owner reads to
        see whether one answers by itself (docs/CONFIRMATIONS.md)."""
        rows = self.db.all("SELECT json_extract(decided_detail, '$.client') AS client, COUNT(*) AS decisions, "
                           "MAX(decided_at) AS last FROM approvals WHERE decided_method = 'chat' "
                           "GROUP BY client ORDER BY last DESC LIMIT 50")
        return [{"client": r["client"], "decisions": r["decisions"], "last": iso(r["last"])} for r in rows]

    # ---- matching a retried call ----
    def consume_approved(self, content_hash: str, grant_id: str, *, max_age_s: float | None = None) -> Approval | None:
        """The approved, unused approval for this exact content and grant, consumed (one winner).
        `max_age_s`: one asked for longer ago than this is stale: it is expired instead and the
        call refused (Denied approval_stale), so the agent asks again and a person sees it anew."""
        now = self.clock()
        stale: Approval | None = None
        with self.db.tx() as conn:
            # an approval with an edit binds the edited content, which only its parked run uses
            row = conn.execute("SELECT * FROM approvals WHERE content_hash = ? AND grant_id = ? AND kind = 'action' "
                               "AND state = 'approved' AND edit IS NULL AND expires_at > ? ORDER BY decided_at LIMIT 1",
                               (content_hash, grant_id, now)).fetchone()
            if row is None:
                return None
            if max_age_s is not None and now - row["created_at"] > max_age_s:
                conn.execute("UPDATE approvals SET state = 'expired' WHERE id = ? AND state = 'approved'", (row["id"],))
                stale = _row_to_approval(row)
        if stale is not None:
            self._publish(self.get(stale.id))
            raise Denied("approval_stale", f"approval {stale.id} was asked for {_age_words(now - stale.created_at)} "
                         f"ago, longer than the {_age_words(max_age_s or 0)} a publish approval holds; nothing was "
                         "sent. Call again to ask afresh")
        with self.db.tx() as conn:
            cur = conn.execute("UPDATE approvals SET state = 'consumed', consumed_at = ? "
                               "WHERE id = ? AND state = 'approved'", (now, row["id"]))
            if cur.rowcount != 1:
                return None
        ap = self.get(row["id"])
        self._publish(ap)
        return ap

    def retire(self, approval_id: str) -> bool:
        """End an approved, unused approval that no longer matches what its call would do (the
        commit gate saw the click send other content): approved -> expired. True when it did."""
        with self.db.tx() as conn:
            cur = conn.execute("UPDATE approvals SET state = 'expired' WHERE id = ? AND state = 'approved'",
                               (approval_id,))
            done = cur.rowcount == 1
        if done:
            self._publish(self.get(approval_id))
        return done

    def withdraw(self, approval_id: str) -> bool:
        """End a request whose action changed or was called off before anyone decided (an agenda
        entry edited or cancelled while it waited): pending or approved, unused -> expired, so a
        late decision on it releases nothing. True when it did."""
        with self.db.tx() as conn:
            cur = conn.execute("UPDATE approvals SET state = 'expired' WHERE id = ? AND kind = 'action' "
                               "AND state IN ('pending', 'approved')", (approval_id,))
            done = cur.rowcount == 1
        if done:
            self._publish(self.get(approval_id))
        return done

    # ---- releasing one parked run (the runner) ----
    def consume(self, approval_id: str) -> bool:
        """Use an approved action approval for exactly one parked run: approved -> consumed,
        atomically. True for the one caller that consumed it; False if it is not approved
        (pending, denied, expired, or already consumed by another run or a retried call)."""
        now = self.clock()
        with self.db.tx() as conn:
            cur = conn.execute("UPDATE approvals SET state = 'consumed', consumed_at = ? WHERE id = ? "
                               "AND kind = 'action' AND state = 'approved' AND expires_at > ?",
                               (now, approval_id, now))
            won = cur.rowcount == 1
        if won:
            self._publish(self.get(approval_id))
        return won

    def find_pending(self, content_hash: str, grant_id: str) -> Approval | None:
        row = self.db.one("SELECT * FROM approvals WHERE content_hash = ? AND grant_id = ? AND state = 'pending' "
                          "AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
                          (content_hash, grant_id, self.clock()))
        return _row_to_approval(row) if row else None

    def recent_denied(self, content_hash: str, family: str, cooldown_s: int) -> Approval | None:
        """The owner's recent denial of this exact content for any grant of the family (its root
        grant id), so a derived grant cannot ask again."""
        if cooldown_s <= 0:
            return None
        prefix = f"/{family}/"
        row = self.db.one("SELECT * FROM approvals WHERE content_hash = ? AND substr(grant_path, 1, ?) = ? "
                          "AND state = 'denied' AND decided_at > ? ORDER BY decided_at DESC LIMIT 1",
                          (content_hash, len(prefix), prefix, self.clock() - cooldown_s))
        return _row_to_approval(row) if row else None

    def recent_denied_after_untrusted(self, family: str, cooldown_s: int) -> Approval | None:
        """The owner's recent denial of any call that followed untrusted content, for any grant of
        the family: while it lasts, such calls are refused instead of asking again (taint_cooldown)."""
        if cooldown_s <= 0:
            return None
        prefix = f"/{family}/"
        row = self.db.one("SELECT * FROM approvals WHERE follows IS NOT NULL AND substr(grant_path, 1, ?) = ? "
                          "AND state = 'denied' AND decided_at > ? ORDER BY decided_at DESC LIMIT 1",
                          (len(prefix), prefix, self.clock() - cooldown_s))
        return _row_to_approval(row) if row else None

    # ---- time ----
    def _expire_one(self, approval_id: str) -> None:
        self.db.execute("UPDATE approvals SET state = 'expired' WHERE id = ? AND state IN ('pending', 'approved') "
                        "AND expires_at <= ?", (approval_id, self.clock()))

    def sweep(self) -> int:
        """Expire pending and unused approved approvals past their time."""
        now = self.clock()
        with self.db.tx() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM approvals WHERE state IN ('pending', 'approved') AND expires_at <= ?", (now,))]
            if ids:
                conn.execute("UPDATE approvals SET state = 'expired' WHERE state IN ('pending', 'approved') "
                             "AND expires_at <= ?", (now,))
        for i in ids:
            row = self.db.one("SELECT * FROM approvals WHERE id = ?", (i,))
            if row:
                self._publish(_row_to_approval(row))
        return len(ids)

    async def wait(self, approval_id: str, timeout_s: float, *, poll_s: float = 0.25) -> Approval:
        """Long-poll until the approval leaves pending or the timeout (at most 120 s) passes."""
        timeout_s = max(0.0, min(float(timeout_s), MAX_WAIT_S))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        ev = asyncio.Event()
        entry = (loop, ev)
        self._waiters.setdefault(approval_id, []).append(entry)
        try:
            while True:
                ap = self.get(approval_id)
                remaining = deadline - loop.time()
                if ap.state != "pending" or remaining <= 0:
                    return ap
                ev.clear()
                try:
                    await asyncio.wait_for(ev.wait(), timeout=min(poll_s, remaining))
                except TimeoutError:
                    pass
        finally:
            waiters = self._waiters.get(approval_id, [])
            if entry in waiters:
                waiters.remove(entry)
            if not waiters:
                self._waiters.pop(approval_id, None)

    # ---- signed links (phone buttons) ----
    # No link approves (SEC-8): "review" opens the approval in the panel, where the owner
    # approves it signed in; "deny" decides from the push, which is always safe.
    def link_key_for(self, purpose: str) -> bytes:
        """A key of its own for `purpose`, from the link key, so no two uses share one."""
        return hmac.new(self._link_key, f"agentos-{purpose}".encode(), hashlib.sha256).digest()

    def sign_action(self, approval_id: str, decision: str, exp: int) -> str:
        msg = f"{approval_id}|{decision}|{exp}".encode()
        return b64u(hmac.new(self._link_key, msg, hashlib.sha256).digest())

    def _check_link(self, approval_id: str, purpose: str, exp: str | int, sig: str) -> None:
        try:
            exp_i = int(exp)
        except (TypeError, ValueError):
            raise InvalidRequest("bad link") from None
        if not hmac.compare_digest(self.sign_action(approval_id, purpose, exp_i), sig or ""):
            raise InvalidRequest("this link is not valid")
        if exp_i < self.clock():
            raise Conflict("this link has expired")

    def check_review_link(self, approval_id: str, exp: str | int, sig: str) -> Approval:
        """A push's Review link, for the panel: the approval it opens, if the link is the node's
        own and still valid. It decides nothing; the owner approves in the signed-in panel."""
        self._check_link(approval_id, "review", exp, sig)
        return self.get(approval_id)

    def decide_by_link(self, approval_id: str, decision: str, exp: str | int, sig: str) -> Approval:
        """The push's one-tap Deny. Approving by link is refused, even with a valid signature."""
        if decision == "approve":
            raise Denied("approve_in_panel", "a link cannot approve; open the approval in the panel and approve it "
                                             "there, signed in")
        if decision != "deny":
            raise InvalidRequest("this link is not valid")
        self._check_link(approval_id, decision, exp, sig)
        return self.decide(approval_id, decision, by="owner:push", via="push_link")

    # ---- "Got it" on an approved action whose run did not work (the panel's failed list) ----
    def clear_outcome(self, approval_id: str, *, by: str) -> Approval:
        """A person has seen that the approved action did not work: it leaves Needs you. Only an approved
        action has an outcome to clear; clearing twice changes nothing."""
        ap = self.get(approval_id)
        if ap.state not in ("approved", "consumed"):
            raise InvalidRequest("only an approved action has an outcome to clear")
        self.db.execute("INSERT OR IGNORE INTO outcome_clears (approval_id, cleared_at, cleared_by) VALUES (?, ?, ?)",
                        (approval_id, self.clock(), by[:120]))
        return ap

    def cleared_outcomes(self) -> dict[str, dict]:
        """Approval id to {at, by} for every outcome a person has cleared."""
        rows = self.db.all("SELECT approval_id, cleared_at, cleared_by FROM outcome_clears")
        return {r["approval_id"]: {"at": r["cleared_at"], "by": r["cleared_by"]} for r in rows}


def _age_words(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 120:
        return f"{max(minutes, 1)} minute{'s' if minutes != 1 else ''}"
    return f"{minutes // 60} hours"


def _preview_text(content: Any) -> str:
    """The preview of edited content (as PolicyService writes one, at most 2000 characters)."""
    safe = redact(content)
    text = safe if isinstance(safe, str) else json.dumps(safe, indent=1, ensure_ascii=False, default=str)
    return text if len(text) <= 2000 else text[:1900] + f"\n[preview truncated: {len(text) - 1900} more characters]"


def _requester(p: Principal) -> dict:
    return {"kind": p.kind, "id": p.id, "owner": p.owner, "thread": p.thread, "grant_id": p.grant_id}
