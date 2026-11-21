"""The approval records (Approval, StandingApproval, the node's ApprovalPolicy and four-eyes rules),
how a row becomes one, and who may decide one (visible_to, decision_problem). No store here: that
is openajar.policy.approvals.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openajar.core import globs
from openajar.core.models import Effect
from openajar.policy.errors import Denied
from openajar.policy.grants import Grant
from openajar.policy.redact import REDACTED
from openajar.policy.util import iso

STATES = ("pending", "approved", "denied", "expired", "consumed")
MAX_WAIT_S = 120.0
DECIDED_VIA = re.compile(r"^[a-z][a-z0-9_:-]{0,39}$")  # passkey, code, owner_token, session, push_link, ...
# what says a preview is not the whole content: a person can only say yes in chat to what the prompt shows
NOT_WHOLE = ("[preview truncated:", "...[truncated ", "[nested too deep]", REDACTED, "[redacted:", "[withheld")
# a send card's messages are served this many at a time (ApprovalStore.send_messages)
SEND_PAGE = 25
SEND_TOOL = "workflow_send"


class FourEyesRule(BaseModel):
    """`[approvals] four_eyes = [{workspace = "biz-*", effects = ["pay"]}]` in policy.toml: in
    matching workspaces, for these effects (all when left out), the person behind the requesting
    agent does not decide its approval; someone else must. A rule for every workspace ("*") also
    holds an action that names no lane (no workspace), so leaving the lane out never skips it."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace: str = "*"
    effects: list[str] | None = None

    @field_validator("workspace")
    @classmethod
    def _ws(cls, v: str) -> str:
        return globs.validate_segment(v)

    @field_validator("effects")
    @classmethod
    def _effects(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            for e in v:
                Effect(e)
        return v

    def matches(self, workspace: str | None, effect: str | None) -> bool:
        where = self.workspace == "*" if workspace is None else globs.segment_match(self.workspace, workspace)
        return where and (self.effects is None or effect in self.effects)


class ApprovalPolicy(BaseModel):
    """The `[approvals]` table of policy.toml (default: no four-eyes anywhere)."""
    model_config = ConfigDict(extra="forbid")
    four_eyes: list[FourEyesRule] = Field(default_factory=list)


class Approval(BaseModel):
    id: str
    kind: Literal["action", "question"]
    state: str
    effect: str | None = None
    tool: str | None = None
    lane: str | None = None
    domain: str | None = None
    summary: str
    content_hash: str | None = None
    preview: str | None = None
    options: list[str] | None = None
    answer: str | None = None
    reason: str | None = None
    requester: dict
    grant_id: str | None = None
    grant_path: str | None = None
    created_at: float
    expires_at: float
    decided_by: str | None = None
    decided_at: float | None = None
    note: str | None = None
    consumed_at: float | None = None
    content_digest: str | None = None  # see content_digest(); what a standing approval can bind
    flow_target: str | None = None     # flow:<id>@<version> when the call ran a flow
    binding: str | None = None         # BINDING_CALL or BINDING_KEYED (ActionBinding); None: made before it
    # routing facts the node computed itself (never model text), and how the decision was made
    workspace: str | None = None
    requester_owner: str | None = None  # sub.owner of the requesting grant: the person behind the agent
    target: str | None = None           # flow:<id>@<version> or tool:<name>
    amount_usd: float | None = None     # node-parsed, pay only
    not_after: float | None = None      # when the waiting run goes stale
    decided_via: str | None = None      # passkey, code, owner_token, session, push_link, ...
    # how strong a yes must be and where it can be given, fixed when it was created (docs/CONFIRMATIONS.md)
    requires: str | None = None
    methods: list[str] | None = None
    requires_basis: dict | None = None  # what held it up when it was made: {untrusted, floor}, never public
    # what the node worked out for the decision. None: it proves no person (a bearer token, a push's deny)
    decided_method: str | None = None
    decided_level: str | None = None
    decided_detail: dict | None = None  # nothing secret and no content
    # the grant the decision was made with: "owner" (the owner, in person or on the node), a member's
    # grant id, an admin grant id; None when a token other than the owner's decided without one
    decided_grant: str | None = None
    # the call follows untrusted content its context read: where from (kind and host, never the
    # text), newest first; None for a call that follows none (ROUTING.md, "After untrusted content")
    follows: list[dict] | None = None

    def follows_words(self) -> str | None:
        """"a web page at evil.example and an email" for the card and the push, or None."""
        return follows_words(self.follows)

    def is_publish_card(self) -> bool:
        """The node's card of a publish the commit gate caught: what a chat prompt shows in place of the
        preview, so it is what says whether the prompt shows the content."""
        return isinstance(self.card, dict) and self.card.get("type") == "publish"

    def preview_cut(self) -> bool:
        """The preview does not show the whole content: it was cut, a value in it was withheld, or the
        card shows an image a text prompt cannot."""
        images = self.card.get("images") if isinstance(self.card, dict) else None
        return bool(images) or any(mark in (self.preview or "") for mark in NOT_WHOLE)

    def needs_whole_view(self) -> bool:
        """A send card whose preview does not show every message: only someone who was served every
        page of them (ApprovalStore.send_messages) approves it, so no path approves it from the cut."""
        cut = any(mark in (self.preview or "") for mark in ("[preview truncated:", "...[truncated ", "[nested too deep]"))
        return self.kind == "action" and self.tool == SEND_TOOL and (cut or not self.preview)

    def chat_can_answer(self) -> bool:
        """Said in `approval_pending`, so an agent knows to offer approval_confirm."""
        return "chat" in (self.methods or [])
    # the node-built card (openajar.policy.cards), the content it binds when a param can be edited,
    # and the approver's edit: {"params": {...}, "by", "at", "digest_before"}
    card: dict | None = None
    content: Any = None
    edit: dict | None = None

    def public(self) -> dict:
        """The API shape (docs/API.md Needs you)."""
        return {
            "id": self.id, "kind": self.kind, "state": self.state, "effect": self.effect, "tool": self.tool,
            "content_digest": self.content_digest, "flow_target": self.flow_target,
            "lane": self.lane, "domain": self.domain, "summary": self.summary, "content_hash": self.content_hash,
            "preview": {"text": self.preview} if self.preview else None, "options": self.options,
            "answer": self.answer, "reason": self.reason, "requested_by": self.requester,
            "created_at": iso(self.created_at), "expires_at": iso(self.expires_at),
            "decided_by": self.decided_by, "decided_at": iso(self.decided_at), "note": self.note,
            "workspace": self.workspace, "requester_owner": self.requester_owner, "target": self.target,
            "amount_usd": self.amount_usd, "not_after": iso(self.not_after), "decided_via": self.decided_via,
            "follows": self.follows, "after_untrusted": self.follows_words(),
            "card": self.card, "edit": self._edit_public(),
            "requires": self.requires, "methods": self.methods, "decided_method": self.decided_method,
            "decided_level": self.decided_level, "decided_detail": self.decided_detail,
            "messages": self.messages_info(),
        }

    def messages_info(self) -> dict | None:
        """A send card's whole list, for the panel's "show all": how many messages, how many pages
        (GET /api/approvals/{id}/messages), and whether approving needs every page read first."""
        if self.tool != SEND_TOOL:
            return None
        messages = self.content.get("messages") if isinstance(self.content, dict) else None
        if not isinstance(messages, list):
            return {"count": None, "pages": 0, "page_size": SEND_PAGE, "read_all_first": self.needs_whole_view()}
        return {"count": len(messages), "pages": max(1, -(-len(messages) // SEND_PAGE)), "page_size": SEND_PAGE,
                "read_all_first": self.needs_whole_view()}

    def _edit_public(self) -> dict | None:
        if not self.edit:
            return None
        return {"params": sorted(self.edit.get("params") or {}), "by": self.edit.get("by"),
                "at": iso(self.edit.get("at"))}

    def status(self) -> dict:
        """What an agent sees from approval_wait."""
        out = {"approval_id": self.id, "kind": self.kind, "state": self.state, "summary": self.summary,
               "created_at": iso(self.created_at), "expires_at": iso(self.expires_at),
               "decided_at": iso(self.decided_at)}
        if self.note:
            out["note"] = self.note
        if self.reason and self.kind != "question":  # why a person must decide (node-written, as the owner reads it)
            out["why"] = self.reason
        if self.state == "pending":
            out.update(self.confirm_hint())
        if self.kind == "question":
            out["answer"] = self.answer
            out["options"] = self.options
        elif self.state == "approved" and self.edit:
            out["edited"] = sorted(self.edit.get("params") or {})
            out["next"] = ("the approver edited it before approving; the run uses the edited version (see "
                           "run_status), so do not call again")
        elif self.state == "approved" and self.tool == "flow_run":
            out["next"] = ("the run starts on its own within a few seconds: follow it with run_status (calling "
                           "flow_run again returns the same run)")
        elif self.state == "approved":
            out["next"] = f"call {self.tool} again with the same arguments" if self.tool else "retry the call"
        return out

    def confirm_hint(self) -> dict:
        """What an agent is told about a pending approval: how strong the yes must be and whether a
        person can give it in chat. Nothing here answers it."""
        out: dict[str, Any] = {"requires": self.requires, "can_answer_in_chat": self.chat_can_answer()}
        if self.chat_can_answer():
            out["offer"] = "the person can answer this in their chat client: call approval_confirm(approval_id)"
        else:
            out["offer"] = "call approval_confirm(approval_id) for a review link to show the person"
        return out


class StandingApproval(BaseModel):
    id: str
    effects: list[str]
    workspaces: list[str]
    lanes: list[str]
    grant_id: str | None
    max_uses: int | None
    uses: int
    per_day: int | None
    starts_at: float
    expires_at: float
    created_by: str
    created_at: float
    revoked_at: float | None
    note: str | None
    targets: list[str] = []
    contents: list[str] = []
    shape: bool = False  # covers any content of its named targets on its lanes ("always allow this shape")
    issuer: str | None = None  # `org:<id>` for one a controller sent (node/channel/standing.py); None: this node's
    withdrawn_at: float | None = None  # its controller no longer sends it; it may send it again, uses kept
    confirmed_at: float | None = None  # when its controller last sent it, or last reconciled while connected

    def state(self, now: float) -> str:
        """active, used_up, expired, revoked, or withdrawn (its controller no longer sends it)."""
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at <= now:
            return "expired"
        if self.withdrawn_at is not None:
            return "withdrawn"
        return "used_up" if self.max_uses is not None and self.uses >= self.max_uses else "active"

    def covers_publishing(self) -> bool:
        """Whether it can cover a content-carrying effect at all (it names a target)."""
        return bool(self.targets)

    def public(self) -> dict:
        d = self.model_dump()
        for k in ("starts_at", "expires_at", "created_at", "revoked_at", "withdrawn_at"):
            d[k] = iso(d[k])
        d["remaining"] = None if self.max_uses is None else max(0, self.max_uses - self.uses)
        d["covers_publishing"] = self.covers_publishing()
        return d


ORIGIN_WORDS = {"web_page": "a web page", "email": "an email", "mail_event": "an email",
                "webhook": "a webhook body", "webhook_event": "a webhook body",
                "memory_note": "a memory note marked untrusted", "connector_output": "connector output",
                "event": "an event", "tool_output": "tool output", "terminal_output": "terminal output",
                "program_output": "program output", "model_output": "model output",
                "web_content": "web content", "untrusted_content": "untrusted content",
                "turn_seed": "what woke this turn"}


def _origin_word(kind: str) -> str:
    if kind in ORIGIN_WORDS:
        return ORIGIN_WORDS[kind]
    words = kind.replace("_", " ")
    return f"{'an' if words[:1] in 'aeiou' else 'a'} {words}"


def follows_words(origins: list[dict] | None, limit: int = 3) -> str | None:
    """Untrusted origins in words: kind and host, never the text. An origin without a host is left
    out when the same kind is also named with one."""
    if not origins:
        return None
    named = {o.get("kind") for o in origins if o.get("host")}
    origins = [o for o in origins if o.get("host") or o.get("kind") not in named]
    words = []
    for o in origins[:limit]:
        w = _origin_word(str(o.get("kind") or "untrusted_content"))
        words.append(f"{w} at {o['host']}" if o.get("host") else w)
    more = len(origins) - limit
    if more > 0:
        words.append(f"{more} more")
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _row_to_approval(r: Any) -> Approval:
    d = dict(r)
    d["requester"] = json.loads(d["requester"])
    d["options"] = json.loads(d["options"]) if d["options"] else None
    d["follows"] = json.loads(d["follows"]) if d.get("follows") else None
    d["methods"] = json.loads(d["methods"]) if d.get("methods") is not None else None
    d["requires_basis"] = json.loads(d["requires_basis"]) if d.get("requires_basis") else None
    d["decided_detail"] = json.loads(d["decided_detail"]) if d.get("decided_detail") else None
    for key, column in (("card", "card"), ("content", "content_json"), ("edit", "edit")):
        raw = d.pop(column, None)
        d[key] = json.loads(raw) if raw else None
    return Approval(**d)


def _row_to_standing(r: Any) -> StandingApproval:
    d = dict(r)
    for k in ("effects", "workspaces", "lanes", "targets", "contents"):
        d[k] = json.loads(d.get(k) or "[]")
    d.pop("day", None)
    d.pop("day_uses", None)
    d["shape"] = bool(d.get("shape"))
    return StandingApproval(**d)


def visible_to(ap: Approval, grant: Grant | str, admin: bool = False) -> bool:
    """The owner and admin grants see every approval; a person's grant (a member) sees the
    approvals on lanes it covers (a question: of the workspace it was asked in), and its own;
    an agent's grant sees its own family's. `grant` may be a grant id (with `admin`)."""
    if isinstance(grant, str):
        return admin or (ap.grant_path is not None and f"/{grant}/" in ap.grant_path)
    if grant.owner_token or grant.admin:
        return True
    if ap.grant_path is not None and f"/{grant.id}/" in ap.grant_path:
        return True
    return grant.human and grant.covers(ap.lane, ap.workspace)


def decision_problem(ap: Approval, decider: Grant, decision: str = "approve", *,
                     four_eyes: list[FourEyesRule] | tuple[FourEyesRule, ...] = (),
                     refuse_family: str | None = None, in_person: bool = False) -> Denied | None:
    """Why `decider` may not make `decision` on `ap`, or None when it may. Pure: the caller checks
    that the decider grant is live (ApprovalStore.decide does, through `check_live`). The notifier
    asks the same question to find who can decide a new approval (`ApprovalStore.can_decide`).

    1. the decider is a person (`sub.kind = "human"`), the owner, or an admin grant (`not_approver`);
    2. a grant family never decides what it asked for (`self_approval`): `refuse_family` names it
       explicitly, else the decider's own root, except for a person deciding in person (`in_person`:
       a signed-in panel session, the node CLI); the node token as a bearer is the "owner" family;
    3. approving: its `approve` covers the effect and lane (Grant.approve_problem), and for pay a
       known amount at most its `max_usd` (`not_approver`); denying: it only has to see the approval;
    4. four-eyes: where a rule matches the workspace and effect, the person behind the requesting
       agent (`requester_owner`) does not approve it (`four_eyes`)."""
    if not (decider.owner_token or decider.admin or decider.human):
        return Denied("not_approver", "only a person decides approvals; an agent's grant never does")
    family = refuse_family
    if family is None and not in_person:
        family = decider.root_id
    if family is not None and (ap.grant_path or "").startswith(f"/{family}/"):
        return Denied("self_approval", "a grant cannot decide what it, or a grant narrowed from the same "
                                       "root, asked for; the owner decides it in the panel or on the node")
    if decision == "deny":
        if not visible_to(ap, decider):
            return Denied("not_approver", "this member cannot see this approval")
        return None
    if decider.human and not decider.owner_token and not decider.admin:
        problem = decider.approve_problem(ap.effect if ap.kind == "action" else None, ap.lane,
                                          workspace=ap.workspace, amount_usd=ap.amount_usd)
        if problem:
            return Denied("not_approver", problem)
    person = decider.subject.id if decider.human else decider.subject.owner
    if ap.kind == "action" and person == _requester_owner(ap) and any(
            r.matches(ap.workspace, ap.effect) for r in four_eyes):
        where = ap.workspace or "every workspace"
        return Denied("four_eyes", f"four-eyes is on for {ap.effect} in {where}: {person} asked for this "
                                   "through their own agent, so someone else decides it")
    return None


def _requester_owner(ap: Approval) -> str:
    return ap.requester_owner or str(ap.requester.get("owner") or "owner")
