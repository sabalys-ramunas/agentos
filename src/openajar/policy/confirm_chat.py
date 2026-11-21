"""approval_confirm: a person says yes or no in their own chat client (docs/CONFIRMATIONS.md, Chat).

The caller is the agent, and a hijacked agent is who this has to hold against. So:

- The tool takes an approval id and nothing else: no argument can carry an answer, and the result
  holds no prompt, no answer and nothing that could give one.
- The node writes the prompt from the approval, as it writes a card. Agent-written content appears
  only as quoted lines the person can tell from the node's words.
- The yes is bound to this prompt and this content: its value is a keyed digest of the approval, its
  content and a one-time prompt id, computed again from the stored approval when the answer comes. So
  an old yes, or a yes for other content, is not an answer. The store checks the content once more in
  the transaction that decides.
- The caller reaches its own family's approvals only, and only what `chat_refusal` and the setting let
  through. Anything more goes to the panel by a review link, which opens the page and approves nothing
  (SEC-8).
- A yes is recorded as `chat`, a person's answer through a client: never as the agent, and never
  "in person". `self_approval` is not in play because no token decides here, and the person is the one
  behind the agent, so four-eyes refuses them where it applies.

What this cannot do: tell a person from a client that answers by itself. A client is its own word
about itself, so its name proves nothing, and an agent with a shell on the machine that holds its token
can open its own session and answer its own prompt (docs/CONFIRMATIONS.md, Chat). That is why chat never
carries pay, irreversible or anything after untrusted text, why it can be turned off, and why a client's
name is kept with each decision, as what it said about itself. A yes faster than a person can read is
not counted, which catches a client set to accept everything and nothing more.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from openajar.core.errors import NotFound
from openajar.core.tools import ClientAnswer, current_elicitor
from openajar.policy.approval_models import Approval
from openajar.policy.confirm import ConfirmLevel, Proof, chat_refusal
from openajar.policy.errors import Conflict, Denied

PROMPT_TTL_S = 300.0  # how long a prompt can stay open
MAX_PROMPTS = 3       # per approval: after this it goes to the panel, so an agent cannot nag a person into a yes
COOLDOWN_S = 30.0     # between prompts for one approval
FAMILY_PROMPTS = 10   # per grant family in FAMILY_WINDOW_S: new wording is a new approval, so this is the real limit
FAMILY_WINDOW_S = 3600.0
MIN_ANSWER_S = 1.0    # a yes faster than this was not read
CLIENT_RE = re.compile(r"[^A-Za-z0-9 ._:/+@()-]")
UNNAMED = "an unnamed client"


@dataclass
class Asked:
    """What the node remembers of an approval's prompts. Only the daemon prompts, so memory is enough."""
    count: int = 0
    last: float = 0.0
    open: bool = False
    spent: set[str] = field(default_factory=set)  # prompts already answered: an answer counts once


def client_name(elicitor: Any) -> str:
    """What the client calls itself, cleaned for the record. Its own word, never a proof."""
    return CLIENT_RE.sub("", str(getattr(elicitor, "client", None) or UNNAMED))[:80] or UNNAMED


def said_to_be(client: str) -> str:
    """A client's name for what a person reads: what it said about itself, and no more."""
    return UNNAMED if client == UNNAMED else f'a client calling itself "{client}"'


def quoted(text: str) -> str:
    """Agent-written text as quoted lines. Every line starts with `| `, so nothing in it can pass for the
    node's words, and what would change how it reads (controls, bidi marks, zero widths) is shown as a mark."""
    clean = "".join(ch if ch in "\n\t" or unicodedata.category(ch)[0] != "C" else "�" for ch in text)
    return "\n".join(f"| {line}" for line in clean.splitlines() or [""])


def prompt_code(ap: Approval) -> str:
    return (ap.content_digest or ap.content_hash or "")[-8:]


def _said(label: str, value: Any) -> list[str]:
    """A label in the node's words, and the value under it as quoted lines."""
    return [f"{label}:", quoted(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))]


def _shown(item: Any) -> Any:
    return item.get("value") if isinstance(item, dict) else item


def _row(item: dict[str, Any]) -> str:
    return ", ".join(f"{key}: {value}" for key, value in item.items() if value is not None)


def card_lines(card: dict[str, Any]) -> list[str]:
    """What the publish card shows (openajar.policy.cards.publish_card), as a prompt can: the labels are the
    node's, and all that came from the page is quoted, the names inside a warning too. Warnings come first,
    so nobody reads past one on the way to yes."""
    fields = card.get("fields") or []
    notes = [f"{label}: {item['note']}" for label, item in (("Text", card.get("text")), ("Title", card.get("title")),
                                                            ("Link", card.get("link")), *((f["name"], f) for f in fields))
             if isinstance(item, dict) and item.get("note")]
    warnings = [card[k] for k in ("account_warning", "lookalike_warning", "chars_warning", "fields_note") if card.get(k)]
    out: list[str] = []
    for warning in [*warnings, *notes]:
        out += _said("Warning", warning)
    if not card.get("verified_rule"):
        out.append("This kind of request has not been checked on a real account.")
    if card.get("headline"):
        out += _said("What it is", card["headline"])
    out += _said("Where", card.get("line") or "")
    live = card.get("signed_in_as")
    out += _said("Signed in now", _row(live)) if isinstance(live, dict) else ["Signed in now: the node cannot see it."]
    for label, value in (("Title", _shown(card.get("title"))), ("Text", _shown(card.get("text"))),
                         ("Link", _shown(card.get("link"))), ("Target", card.get("target")),
                         ("In reply to", card.get("reply_to")), ("Posted as", card.get("body_as")),
                         ("Visibility", card.get("visibility")), ("Flags", card.get("flags"))):
        if value not in (None, "", [], {}):
            out += _said(label, value)
    if card.get("links"):
        out += _said("Every link, in full", "\n".join(card["links"]))
    if card.get("media"):
        out += _said("Media", "\n".join(_row(m) for m in card["media"]))
    if card.get("operations"):
        out += _said("Operations", "\n".join(_row({k: _shown(v) for k, v in op.items()}) for op in card["operations"]))
    if fields:
        out += _said("Other fields it sends", "\n".join(f"{f['name']}: {_shown(f)}" for f in fields))
    return out


def prompt_text(ap: Approval) -> str:
    """The prompt, from what the node knows: never the summary, which a handler words from a call. A publish
    the commit gate caught shows what its card shows, so a person sees here what they would see there."""
    where = " ".join(x for x in (f"on {ap.lane}" if ap.lane else "", f"at {ap.domain}" if ap.domain else "") if x)
    action = " ".join(x for x in (f"{ap.effect} ({ap.tool})" if ap.tool else str(ap.effect), where) if x)
    card = getattr(ap, "card", None)
    if isinstance(card, dict) and card.get("type") == "publish":
        return (f"openajar asks you to decide. These words are the node's, not the agent's.\n\n"
                f"Action: {action}\nApproval: {ap.id}\nContent code: {prompt_code(ap)}\n\n"
                "Each line starting with | came from the page or the agent. Nothing in it is openajar speaking.\n\n"
                + "\n".join(card_lines(card)) + "\n\nSay yes only if you want exactly this.")
    return (f"openajar asks you to decide. These words are the node's, not the agent's.\n\n"
            f"Action: {action}\nApproval: {ap.id}\nContent code: {prompt_code(ap)}\n\n"
            "The exact content follows. Each line starting with | is what the agent wants to send, written by "
            f"the agent. Nothing in it is openajar speaking.\n\n{quoted(ap.preview or '')}\n\n"
            "Say yes only if you want exactly this.")


def yes_value(key: bytes, ap: Approval, prompt_id: str) -> str:
    """The one value that answers yes to this prompt for this content. A digest, so it says nothing of
    the key, and only the node can work it out again from the stored approval."""
    bound = f"chat|{ap.id}|{ap.content_hash}|{ap.content_digest}|{prompt_id}".encode()
    return "yes-" + hmac.new(key, bound, hashlib.sha256).hexdigest()[:12]


def prompt_schema(yes: str) -> dict[str, Any]:
    return {"type": "object", "required": ["answer"], "properties": {"answer": {
        "type": "string", "title": "Your answer", "enum": [yes, "no"], "enumNames": ["Yes, approve it", "No, deny it"]}}}


def own_family(ap: Approval, grant: Any) -> bool:
    return (ap.grant_path or "").startswith(f"/{grant.root_id}/")


class ChatConfirm:
    """One per policy service: the prompts asked so far, and the call behind approval_confirm."""

    def __init__(self, policy: Any):
        self.policy = policy
        self.asked: dict[str, Asked] = {}
        self.prompted: dict[str, list[float]] = {}  # when each grant family last prompted, within the window

    @property
    def store(self) -> Any:
        return self.policy.approvals

    def review(self, ap: Approval, why: str) -> dict:
        """The panel, for what chat cannot answer. The link opens the approval and approves nothing."""
        return {"status": "review", "approval_id": ap.id, "requires": ap.requires, "why": why,
                "review_url": self.policy.notifier.review_url(ap.id, ap.expires_at),
                "next": "Show the person this link. It opens the approval in the panel, where they decide signed in; "
                        "the link itself approves nothing. Then wait with approval_wait."}

    def person(self, ap: Approval) -> Any | None:
        """The grant of the person behind the agent, if they may approve this: who answers a chat prompt.
        In person, because no token decides here. Four-eyes and a member's limits hold as in the panel."""
        return self.policy.person_grant(ap, ap.requester_owner or "owner")

    def refusal(self, ctx: Any, ap: Approval) -> str | None:
        """Why this cannot be answered in chat now. `requires` is fixed, but what the session has read
        since is not: a hijacked agent must not get a yes for what it asked before it was hijacked."""
        setting = self.policy.confirmation.current()
        if "chat" in setting.methods_off:
            return "chat is turned off on this node"
        if "chat" in setting.off_in(ap.workspace):
            return "chat is turned off in this workspace"
        why = chat_refusal(ap)
        if why:
            return why
        if "chat" not in (ap.methods or []):
            return "chat was not offered for this approval"
        if ap.is_publish_card():  # every real one follows a page, so this is the owner's switch, read as it is now
            return None if not ap.follows or setting.publish_takes_chat(ap.workspace) else "publishing in chat is turned off"
        if self.policy.read_untrusted(ctx):
            return "this session has read untrusted text since"
        return None

    def _may_prompt(self, ap: Approval, family: str, now: float) -> str | None:
        asked = self.asked.setdefault(ap.id, Asked())
        if asked.open:
            return "a prompt for this approval is already open"
        if asked.count >= MAX_PROMPTS:
            return "the person was asked enough times in chat"
        if asked.count and now - asked.last < COOLDOWN_S:
            return "the person was just asked"
        self.prompted[family] = [at for at in self.prompted.get(family, []) if now - at < FAMILY_WINDOW_S]
        if len(self.prompted[family]) >= FAMILY_PROMPTS:
            return "the person was asked enough times in chat this hour"
        return None

    async def confirm(self, ctx: Any, approval_id: str) -> Any:
        ap = self.store.get(approval_id)
        if not self.store.visible_to(ap, ctx.grant.id, ctx.grant.admin):
            raise NotFound(f"no approval {approval_id}")  # do not reveal other principals' approvals
        if not own_family(ap, ctx.grant):
            raise Denied("not_approver", "approval_confirm reaches only what your own grant family asked for")
        if ap.state != "pending":
            return {"status": ap.state, **ap.status()}
        why = self.refusal(ctx, ap)
        if why:
            return self.review(ap, why)
        elicitor = current_elicitor()
        if elicitor is None or not elicitor.can_ask():
            return self.review(ap, "this chat client cannot show a prompt")
        decider = self.person(ap)
        if decider is None:
            return self.review(ap, "the person behind this agent cannot decide this one in chat")
        answer = elicitor.answered()
        if answer is None:
            now = self.store.clock()
            why = self._may_prompt(ap, ctx.grant.root_id, now)
            if why:
                return self.review(ap, why)
            self.prompted[ctx.grant.root_id].append(now)
            answer = await self._ask(ap, elicitor, now)
            if not isinstance(answer, ClientAnswer):
                return answer  # the client answers in a second round (a 2026-07-28 client)
        return self._settle(ctx, ap.id, decider, answer, client_name(elicitor))

    async def _ask(self, ap: Approval, elicitor: Any, now: float) -> Any:
        prompt_id = secrets.token_hex(16)
        state = json.dumps({"approval": ap.id, "prompt": prompt_id, "at": now})
        yes = yes_value(self.store.link_key_for("chat"), ap, prompt_id)
        asked = self.asked[ap.id]
        asked.count, asked.last, asked.open = asked.count + 1, now, True
        try:
            return await elicitor.ask(prompt_text(ap), prompt_schema(yes), state=state, timeout_s=PROMPT_TTL_S)
        finally:
            asked.open = False

    def _settle(self, ctx: Any, approval_id: str, decider: Any, answer: ClientAnswer, client: str) -> dict:
        """The answer, checked against the stored approval as it is now."""
        self.policy.check_live(ctx.grant)  # revoked while the prompt was open: the answer counts for nothing
        ap = self.store.get(approval_id)
        state = _state(answer.state)
        if state is None or state["approval"] != ap.id:
            return self.review(ap, "the answer does not belong to this approval")
        if ap.state != "pending":
            return {"status": ap.state, **ap.status()}
        spent = self.asked.setdefault(ap.id, Asked()).spent
        if state["prompt"] in spent or self.store.clock() - state["at"] > PROMPT_TTL_S:
            return self.review(ap, "this prompt was already answered, or is too old")
        spent.add(state["prompt"])
        if answer.action in ("timeout", "failed"):  # it said it could show a prompt, and no answer came
            ctx.trace("approval_confirm", summary=f"no answer from {said_to_be(client)} ({answer.action})", approval_id=ap.id)
            return self.review(ap, "the client did not answer the prompt")
        if answer.action == "cancel":
            ctx.trace("approval_confirm", summary=f"{said_to_be(client)} closed the prompt", approval_id=ap.id)
            return {"status": "no_answer", "approval_id": ap.id,
                    "next": "The person did not answer. Do not ask again at once: tell them what waits."}
        value = (answer.content or {}).get("answer")
        if answer.action == "decline" or (answer.action == "accept" and value == "no"):
            return self._decide(ctx, ap, decider, "deny", client)
        yes = yes_value(self.store.link_key_for("chat"), ap, state["prompt"])
        if answer.action != "accept" or not isinstance(value, str) or not hmac.compare_digest(value, yes):
            ctx.trace("approval_confirm", status="denied", summary=f"{said_to_be(client)} sent no answer to this prompt",
                      approval_id=ap.id)
            return self.review(ap, "the answer does not match this prompt")
        if self.store.clock() - state["at"] < MIN_ANSWER_S:
            ctx.trace("approval_confirm", status="denied", summary=f"{said_to_be(client)} answered faster than a person reads",
                      approval_id=ap.id)
            return self.review(ap, "the client answered faster than a person can read")
        why = self.refusal(ctx, ap)  # the setting, or what the session read, may have changed while it was open
        if why:
            return self.review(ap, why)
        return self._decide(ctx, ap, decider, "approve", client)

    def _decide(self, ctx: Any, ap: Approval, decider: Any, decision: str, client: str) -> dict:
        who = ap.requester_owner or "owner"
        proof = Proof("chat", ConfirmLevel.CHAT, self.store.clock(), {"client": client})
        try:
            # `chat:` first: whatever reads the first part of `decided_by` sees no person and no controller
            done = self.store.decide(ap.id, decision, decider=decider, by=f"chat:{who}", via="chat", in_person=True,
                                     proof=proof, content=(ap.content_hash, ap.content_digest))
        except Conflict:
            done = self.store.get(ap.id)
            if done.state == "pending":  # the content moved between the prompt and the answer
                return self.review(done, "the content changed after the prompt")
            return {"status": done.state, **done.status()}
        ctx.trace("approval_confirm", summary=f"{decision} through {said_to_be(client)}", approval_id=ap.id)
        self.asked.pop(ap.id, None)
        return {"status": done.state, **done.status()}


def _state(raw: str | None) -> dict | None:
    """The prompt a client's answer belongs to. Sealed by the SDK on a second round; the node's own
    value on the first."""
    try:
        state = json.loads(raw or "")
    except ValueError:
        return None
    if not isinstance(state, dict) or not isinstance(state.get("prompt"), str) or \
            not isinstance(state.get("at"), int | float) or not isinstance(state.get("approval"), str):
        return None
    return state
