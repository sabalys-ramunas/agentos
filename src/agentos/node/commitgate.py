"""The commit gate (AUD-03): a publish an agent's click sets off waits for a person who approved
exactly what it will publish (docs/design/egress-guard.md section 8, ROUTING.md).

browser_act is authorized as a draft: what a click does is not known before it. From the agent's
first act until control leaves it, the lane's egress guard routes the rule hosts
(`LaneGuard.acting(gate=...)`, the drive) and hands every request that matches a commit rule, or
that it cannot tell from one (`<site>.unknown`, egress.LaneGuard._gate_rule), to the latest act's
ActGate, before it leaves. A login is not held: inside the vault's login window the guard lets go
what would be `<site>.unknown` (egress.LaneGuard._in_login), and only that. A `<site>.unknown`
request asks a person only when it carries what the agent typed on the lane and this act's own
press sent it; any other is held without a card (held_unknown: the page's telemetry, a sync, a
request while typing or between acts), listed in the act's `egress_blocked` as `unrecognised`
(docs/design/egress-guard.md 8.13). A commit rule's request always goes through the steps below:

1. The request is normalized (agentos.node.publish.capture): the text, target, media, visibility
   and every other field it sends, volatile ones dropped, as `{"publish": rule, "lane", "record"}`.
   The browser's signed-in account must be the lane's pinned one (AUD-12, else `wrong_account`).
   Who the body acts as is read and resolved (agentos.node.acting): the lane's own account; a
   surface through this lane (a Page, a company page), when the publish is authorized on the
   surface (`lane` is the surface id: its workspace must be in the grant, its Ask me first switch
   and its pace apply); or an identity agentos does not know, which always asks a person (the policy
   service, ROUTING.md) and is noted in the lane's gate log (`gate_identity`).
2. That content is authorized as a commit (`ctx.authorize(Effect.COMMIT, content=...)`), the same
   path every other approval takes: a standing approval bound to this content digest
   (`tool:browser_act` and the digest in `contents`) lets it go and uses one unit; an approved,
   unused one-off approval of exactly this content and grant is consumed (once: the store's
   consume is atomic) and lets it go, unless it is older than `publish_approval_max_age_s`
   (refused, approval_stale); a recent denial refuses (approval_denied); otherwise a pending
   approval is created with a `publish` card (agentos.policy.cards) and the request is aborted.
3. The act finishes (nothing was sent) and browser_act raises what the gate decided: the
   tool returns `approval_pending` with the approval id, and after approval the agent calls
   again with the same arguments. The act runs again, the page sends the request again, and it
   is normalized again: it leaves only when its digest is the approved one.
4. When a call with the same arguments was approved and the click now sends other content, the
   approval is retired and the call refused (publish_changed). The gate remembers which approval a
   call asked for in memory (ASKED): after a daemon restart a changed click simply asks again.

One publish per approval: a request with a digest this act already sent is aborted (a page that
submits twice, or retries its request after an error, does not post twice under one approval), and
once one publish of the act is held, every later one is held too. When one left (approved) before a
later one was held, the outcome says both: what was sent and what waits (raise_outcome), and the
sent one's approval stays used, never retired. What cannot be bound whole (agentos.node.publish
Unbindable, media by id only) is refused (publish_unbound). A human in control is never gated (the
guard judges nothing for a person). A grant below commit never reaches the gate: the guard refuses
that publish first (rule `commit`), as before.
"""

from __future__ import annotations

import contextlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from agentos.core.errors import AgentosError, ApprovalPending, NotFound, PublishChanged, PublishUnbound
from agentos.core.models import Effect
from agentos.node import acting, egress, publish
from agentos.policy.approvals import content_digest
from agentos.policy.errors import Denied
from agentos.policy.util import content_hash

log = logging.getLogger("agentos.node.commitgate")

ASKED_KEPT = 2000
# (grant, lane, call) -> the approval that call last asked for, so a changed click can be told
ASKED: OrderedDict[tuple[str, str, str], str] = OrderedDict()
SUMMARY_TEXT = 80
UNREADABLE = object()  # ActGate._pinned: the lane manager could not say which account the lane pins
KIND_WORDS = {"post": "a post", "comment": "a comment", "like": "a like", "follow": "a follow", "join": "a join",
              "profile": "a profile edit", egress.UNKNOWN: "an unrecognised request"}


def summary(cap: publish.Capture, lane: str) -> str:
    """The node's own words for the approval, from the normalized record (never model text), with
    who it posts as when the gate resolved it (acting: a surface, or an identity it does not know)."""
    from agentos.policy.cards import site_label

    rec = cap.record
    if cap.kind == egress.UNKNOWN:  # no rule reads it: say what is known, never "a post"
        req = rec.get("request") if isinstance(rec.get("request"), dict) else {}
        field = rec.get("carries_typed")
        value = rec.get(field) if field in ("title", "text") else (rec.get("fields") or {}).get(field) if field else None
        quote = ""
        if isinstance(value, str) and value.strip():
            line = " ".join(value.split())
            quote = f': "{line[:SUMMARY_TEXT]}{"..." if len(line) > SUMMARY_TEXT else ""}"'
        carries = (f" It carries text the agent typed{quote or '.'}" if "carries_typed" in rec else "")
        return (f"Send a request agentos does not recognise to {site_label(cap.site)} through {lane} "
                f"({req.get('method', 'POST')} {cap.host}{req.get('path', '/')}).{carries}")
    what = KIND_WORDS.get(cap.kind, f"a {cap.kind}")
    target = rec.get("target")
    where = f" to {target}" if isinstance(target, str) and target else ""
    words = rec.get("title") if isinstance(rec.get("title"), str) else rec.get("text")
    quote = ""
    if isinstance(words, str) and words.strip():
        line = " ".join(words.split())
        quote = f': "{line[:SUMMARY_TEXT]}{"..." if len(line) > SUMMARY_TEXT else ""}"'
    media = len(rec.get("media") or [])
    with_media = f" with {media} file{'s' if media != 1 else ''}" if media else ""
    acts = rec.get("acts_as") if isinstance(rec.get("acts_as"), dict) else None
    status = acts.get("status") if acts else None
    as_ = (f" as {acts.get('label')}" if status == acting.SURFACE
           else f" as an identity agentos does not know ({acts.get('id') or 'unreadable'})"
           if status == acting.UNRECOGNIZED else "")
    return f"Publish {what}{where} on {site_label(cap.site)}{as_} through {lane}{with_media}{quote}"


class ActGate:
    """egress.PublishGate for one browser_act call (see the module docstring)."""

    def __init__(self, ctx: Any, on: str, *, call: dict[str, Any], submit_like: bool):
        self.ctx = ctx
        self.on = on
        grant = getattr(getattr(ctx, "grant", None), "id", None) or getattr(getattr(ctx, "principal", None), "id", "")
        self.key = (str(grant), on, content_hash(call))
        self.submit_like = submit_like
        self.done = False
        self.uploads: Iterable[publish.Upload] = ()
        self.outcome: AgentosError | None = None
        self.sent: list[str] = []
        self.let_go: list[str] = []  # what this act's publishes that left said (summary), for the report
        # site key -> the account the lane's browser is signed into there ({site, user_id, source}),
        # read from its cookies (agentos.node.sites.account_identity, AUD-12); None when not known
        self.identity: Callable[[str], Awaitable[dict[str, str] | None]] | None = None
        # the lane's egress trace (the gate log reads it): an identity the gate does not know is noted there
        self.egress_trace: egress.EgressTrace | None = None
        self.acts_as: dict[str, Any] | None = None  # who the latest publish acts as (acting.public), for the trace
        # what of the agent's typed text a body carries (LaneGuard.carries_body, bound by `guarded`):
        # an unknown request asks a person only when it carries some, during a press of this act
        self.typed: Callable[[bytes | None], str | None] | None = None
        self.active = False  # the act runs (guarded sets it): a request between acts asks nobody
        self.held: list[dict[str, Any]] = []  # unknown requests held without a card (held_unknown)
        self.writes: list[str] = []  # write rules whose requests this act let go (a like, a follow): left()

    async def hold(self, rule: egress.EffectRule, request: Any, url: str, method: str) -> egress.Refusal | None:
        self.done = True
        body, headers = egress.LaneGuard._body(request)
        typed: str | None = None
        if rule.kind == egress.UNKNOWN:
            # a request no rule reads is a publish a person can judge only when it carries what the
            # agent typed and this act's own press sent it; anything else (the page's telemetry, a
            # sync, a request between acts or while typing) is held without a card (held_unknown)
            typed = self._carries_typed(body)
            if typed is None or not (self.submit_like and self.active):
                return self._held_unknown(rule, url, method, typed)
        try:
            cap = publish.capture(rule, url, method, headers, body, uploads=list(self.uploads))
        except publish.Unbindable as e:
            return self._unbound(rule, str(e))
        if typed is not None:  # where the typed words sit, for the card (the digest binds it with the rest)
            cap.record["carries_typed"] = self._typed_field(cap.record, typed)
        unbound = publish.unbound_media(cap.record)
        if unbound:
            return self._unbound(rule, f"it names media the node did not see uploaded in this lane, by id only "
                                       f"({', '.join(unbound[:3])})")
        account = await self._session_account(rule.site)
        if account is not None:  # bound too: approved as one account, sent as another, is other content
            cap.record["session_account"] = account
        pinned = self._pinned()
        if pinned is UNREADABLE:
            self.outcome = Denied("account_unreadable", f"the account pinned for {self.on} could not be read, so "
                                  "who this click would publish as cannot be checked; nothing was sent. Tell the human")
            return self._refuse(rule, "the lane's pinned account could not be read", "")
        wrong = self._wrong_account(rule, pinned, account)  # the account pin (AUD-12) first
        if wrong is not None:
            return wrong
        acts = acting.resolve(cap.acting, lane=self.on, pinned=pinned, session=account, surfaces=self._surfaces()) \
            if cap.acting is not None else None
        target = self.on  # what the publish is authorized on: the lane, or the surface it posts as
        self.acts_as = acting.public(acts)
        if acts is not None:
            cap.record["acts_as"] = acts
            if acts["status"] == acting.SURFACE:
                target = acts["surface"]
            elif acts["status"] == acting.UNRECOGNIZED:
                self._note_identity(rule, acts)
        if target != self.on and not self._covers(target):
            self.outcome = Denied("out_of_scope", f"this click would publish as {acts['label']} (the surface "
                                  f"{target}, through {self.on}), and this grant does not cover {target}; "
                                  "nothing was sent")
            return self._refuse(rule, f"it posts as {target}, which this grant does not cover", "")
        content = cap.content(target)
        digest = content_digest(content)
        if digest in self.sent:
            return self._refuse(rule, "this act already sent it once under its approval; a second copy is not sent",
                                digest)
        if self.outcome is not None:
            return self._refuse(rule, "an earlier publish of this act is not sent, so this one is not either", digest)
        old = self._changed(digest)
        if old is not None:
            self.outcome = PublishChanged(
                f"approval {old} was for other content than this click now sends; nothing was "
                "sent and that approval is retired. Take a new snapshot, check the page, and call browser_act "
                "again: a new approval will show what it sends now")
            return self._refuse(rule, "a person approved other content than this sends", digest, rule_name="publish_changed")
        try:
            await self.ctx.authorize(Effect.COMMIT, lane_id=target, domain=cap.host or None,
                                     summary=summary(cap, self.on), content=content)
        except ApprovalPending as e:
            self._remember(e.approval_id)
            self.outcome = e
            return self._refuse(rule, f"it waits for a person to approve exactly what it sends (approval "
                                      f"{e.approval_id})", digest)
        except AgentosError as e:
            self.outcome = e
            return self._refuse(rule, f"it was refused ({e.code})", digest)
        self.sent.append(digest)
        self.let_go.append(summary(cap, self.on))
        ASKED.pop(self.key, None)
        self._trace("ok", rule, digest, f"{rule.id}: approved, sent")
        return None

    # ---- what the gate holds without asking ------------------------------------------------

    def _carries_typed(self, body: bytes | None) -> str | None:
        try:
            return self.typed(body) if self.typed is not None else None
        except Exception:
            log.debug("could not check a body for typed text", exc_info=True)
            return None

    @staticmethod
    def _typed_field(record: dict[str, Any], typed: str) -> str:
        """The first field of the record whose value holds the typed text ("" when only the raw body does)."""
        for key in ("title", "text"):
            if isinstance(record.get(key), str) and egress.typed_matches(record[key], typed):
                return key
        for key, value in sorted((record.get("fields") or {}).items()):
            if isinstance(value, str) and egress.typed_matches(value, typed):
                return key
        return ""

    def left(self, rule: egress.EffectRule) -> None:
        """A request of a write rule (a like, a follow, a join) left during this act: its report says so."""
        if rule.id not in self.writes:
            self.writes.append(rule.id)

    def _held_unknown(self, rule: egress.EffectRule, url: str, method: str, typed: str | None) -> egress.Refusal:
        """Hold a request no rule reads without asking anyone (no card in Needs you): nothing shows it
        publishes, so a person could not judge it. It is not sent; the act's result lists it in
        `egress_blocked`, and the gate log and `agentos lane rules` show its shape."""
        parts = urlsplit(url)
        what = f"{method.upper()} {(parts.hostname or '').lower()}{parts.path or '/'}"
        self.held.append({"request": what, "typed": typed is not None})
        if typed is not None:
            why = ("it carries text the agent typed, but it was sent while typing or between acts, not by a "
                   "click or Enter of this act")
        else:
            why = ("no text the agent typed was found in it, so nothing shows that it publishes (the check "
                   "cannot see every way a page may carry text)")
        log.info("%s: the commit gate held %s (%s) without asking: %s", self.on, what, rule.id, why)
        return egress.Refusal(
            "unrecognised", f"agentos does not recognise this request ({what}): {why}. It was held and not sent, "
                            "and nobody was asked. If it was meant to publish, tell the human: the owner can "
                            f"name it in the gate log (agentos lane gate-log {self.on})")

    # ---- who it posts as (AUD-01 on the gate) ---------------------------------------------

    def _service(self, name: str) -> Any:
        runtime = getattr(self.ctx, "runtime", None)
        get = getattr(runtime, "get", None)
        try:
            return get(name) if callable(get) else None
        except Exception:  # noqa: BLE001 - a runtime without it: nothing to read
            return None

    def _pinned(self) -> Any:
        """The lane's pinned account (LaneSpec.account), None when it pins none (or the node has no
        lane manager), or UNREADABLE when the lane manager cannot say: hold() then refuses, since
        an account pin that cannot be read must not let a publish go as any account (AUD-12)."""
        lanes = self._service("lanes")
        try:
            return lanes.get(self.on).spec.account if lanes is not None else None
        except Exception:  # checked by hold(): fail closed
            log.warning("could not read the account pinned for %s; the publish is refused", self.on, exc_info=True)
            return UNREADABLE

    def _surfaces(self) -> list[Any]:
        surfaces = self._service("surfaces")
        try:
            return list(surfaces.list()) if surfaces is not None else []
        except Exception:  # the surfaces store could not be read: none resolve (fail closed)
            log.warning("could not read the surfaces for the commit gate on %s", self.on, exc_info=True)
            return []

    def _covers(self, target: str) -> bool:
        can = getattr(self.ctx, "can_access_lane", None)
        try:
            return bool(can(target)) if callable(can) else True
        except Exception:  # noqa: BLE001
            return False

    def _wrong_account(self, rule: egress.EffectRule, pinned: Any, account: dict[str, str] | None) -> egress.Refusal | None:
        """The account pin (AUD-12), before who the body acts as: a browser signed in to another
        account than the lane's pinned one publishes nothing, whatever was approved."""
        from agentos.node.sites import account_matches

        if pinned is None or account is None or account_matches(pinned, account) is not False:
            return None
        want = pinned.handle or pinned.user_id
        self.outcome = Denied("wrong_account", f"the browser is signed in to {account.get('site')} as "
                              f"{account.get('user_id')}, not as the lane's account {want}; nothing was sent. "
                              "Stop and tell the human")
        return self._refuse(rule, f"the browser is signed in as {account.get('user_id')}, not as {want}", "")

    def _note_identity(self, rule: egress.EffectRule, acts: dict[str, Any]) -> None:
        """An identity the gate does not know: one row in the lane's egress trace (the gate log lists
        them, ids and field names only) and a log line."""
        log.info("%s: %s acts as an identity agentos does not know (%s, %s): %s", self.on, rule.id,
                 acts.get("id") or "unreadable", acts.get("field") or "no field", acts.get("why"))
        if self.egress_trace is None:
            return
        try:
            self.egress_trace.write({"kind": "gate_identity", "lane": self.on, "rule": rule.id, "site": rule.site,
                                     "id": acts.get("id"), "field": acts.get("field"),
                                     "verified": bool(acts.get("verified")), "why": acts.get("why")})
        except Exception:
            log.debug("could not trace an unrecognized identity", exc_info=True)

    async def _session_account(self, site: str) -> dict[str, str] | None:
        if self.identity is None:
            return None
        try:
            found = await self.identity(site)
        except Exception:  # an account the node cannot read is not shown, never guessed
            log.debug("could not read the signed-in account on %s", site, exc_info=True)
            return None
        return {k: str(found[k]) for k in ("site", "user_id") if found.get(k)} if isinstance(found, dict) else None

    def _unbound(self, rule: egress.EffectRule, why: str) -> egress.Refusal:
        """Refuse what cannot be bound whole (fail closed): nobody could approve exactly what it sends."""
        if self.outcome is None:
            self.outcome = PublishUnbound(f"this click would publish ({rule.id}) but {why}, so no approval could "
                                          "bind exactly what it sends; nothing was sent")
        self._trace("denied", rule, "", f"{rule.id}: unbound: {why}")
        return egress.Refusal("commit_gate", f"this publishes ({rule.id}) and {why}; nothing was sent")

    def raise_outcome(self) -> None:
        """After the act: what the gate decided, as the tool's own outcome (approval_pending, a
        refusal, publish_changed), or nothing when every publish went (or none was made). When a
        publish of the act left (approved) before a later one was held, the outcome says both:
        what was sent, and what waits; the sent one's approval is used, never retired."""
        if self.outcome is None:
            return
        if (self.let_go or self.writes) and not getattr(self.outcome, "told_sent", False):
            told: list[str] = []
            if self.let_go:
                n = len(self.let_go)
                sent = "; ".join(self.let_go[:3]) + ("; ..." if n > 3 else "")
                told.append(f"{n} publish{'es' if n > 1 else ''} of this click already left under its approval and "
                            f"{'were' if n > 1 else 'was'} sent ({sent}).")
            if self.writes:
                words = ", ".join(f"{KIND_WORDS.get(w.split('.')[1], 'a request')} ({w})" for w in self.writes[:3])
                told.append(f"This click also sent {words}, which needs no approval.")
            # what was held did not leave; the rest of the act did, so never "nothing was sent"
            rest = self.outcome.message.replace("nothing was sent", "that request was not sent")
            message = " ".join(told) + " A later one did not leave: " + rest
            self.outcome.message = message
            self.outcome.args = (message,)
            self.outcome.told_sent = True  # type: ignore[attr-defined]
        raise self.outcome

    # ---- inside ---------------------------------------------------------------------------

    def _refuse(self, rule: egress.EffectRule, why: str, digest: str, *, rule_name: str = "commit_gate") -> egress.Refusal:
        self._trace("pending" if isinstance(self.outcome, ApprovalPending) else "denied", rule, digest,
                    f"{rule.id}: {why}")
        return egress.Refusal(rule_name, f"this publishes ({rule.id}) and {why}; nothing was sent")

    def _remember(self, approval_id: str) -> None:
        ASKED[self.key] = approval_id
        ASKED.move_to_end(self.key)
        while len(ASKED) > ASKED_KEPT:
            ASKED.popitem(last=False)

    def _changed(self, digest: str) -> str | None:
        """The id of an approval this same call got for other content, now retired; else None."""
        asked = ASKED.get(self.key)
        if asked is None:
            return None
        runtime = getattr(self.ctx, "runtime", None)
        approvals = runtime.get("approvals") if runtime is not None and callable(getattr(runtime, "get", None)) else None
        if approvals is None:
            return None
        try:
            ap = approvals.get(asked)
        except NotFound:
            ASKED.pop(self.key, None)
            return None
        if ap.state != "approved" or ap.content_digest == digest:
            return None
        approvals.retire(asked)
        ASKED.pop(self.key, None)
        return asked

    def _trace(self, status: str, rule: egress.EffectRule, digest: str, summary_: str) -> None:
        trace = getattr(self.ctx, "trace", None)
        if not callable(trace):
            return
        try:
            trace("commit_gate", lane=self.on, effect="commit", status=status, summary=summary_,
                  effect_rule=rule.id, verified_rule=bool(rule.verified), digest=digest,
                  **({"acts_as": self.acts_as} if self.acts_as else {}))
        except Exception:
            log.debug("could not trace the commit gate", exc_info=True)



# ---- flows running on their own ------------------------------------------------------------

def _known_surfaces(runtime: Any) -> list[Any]:
    """Every surface on the node; none when they cannot be read (an asset id is then unknown: fail closed)."""
    surfaces = runtime.get("surfaces") if runtime is not None else None
    try:
        return list(surfaces.list()) if surfaces is not None else []
    except Exception:  # noqa: BLE001
        return []


def flow_identity_check(runtime: Any, lane_id: str, guard: egress.LaneGuard
                        ) -> Callable[[egress.EffectRule, Any, str, str], Awaitable[egress.Refusal | None]] | None:
    """For a flow run on `lane_id` (the flow driver, egress.LaneGuard.flow_identity): a check that
    refuses a publish the flow's page sends as an identity agentos does not know (agentos.node.acting),
    since a flow running on its own cannot ask a person. What the flow publishes was authorized with
    the run (its approval binds the flow version and parameters); only who it posts as is read here,
    and a body it cannot read is refused too (nobody can say who it posts as). None (no check, no
    route) only for a lane known to be on no site where a login can post as another identity: its
    account, its name, its site list and the surfaces through it say so. A lane none of them
    describes is checked."""
    lanes = runtime.get("lanes") if runtime is not None else None
    try:
        spec = lanes.get(lane_id).spec if lanes is not None else None
    except Exception:  # noqa: BLE001 - a lane the manager cannot describe: checked (below)
        spec = None
    from agentos.node import sites

    expected = set(sites.expectation_for(spec).sites) if spec is not None else set()
    if spec is not None:
        expected |= {s.key for p in spec.sites or () for s in [sites.site_for("https://" + p.lstrip("*."))] if s}
    expected |= {s.site for s in _known_surfaces(runtime) if s.lane == lane_id}
    # only a lane whose own spec was read can be known to post as itself alone: its surfaces are
    # part of what it is, never all of it (fail closed)
    if spec is not None and expected and not expected & set(acting.ACTING):
        return None
    pinned = spec.account if spec is not None else None

    def refuse(rule: egress.EffectRule, acts: dict[str, Any]) -> egress.Refusal:
        log.warning("%s: a flow's publish (%s) acts as an identity agentos does not know (%s); it is aborted",
                    lane_id, rule.id, acts.get("id") or "unreadable")
        if guard.trace is not None:
            with contextlib.suppress(Exception):
                guard.trace.write({"kind": "gate_identity", "lane": lane_id, "rule": rule.id, "site": rule.site,
                                   "id": acts.get("id"), "field": acts.get("field"), "flow": True,
                                   "verified": bool(acts.get("verified")), "why": acts.get("why")})
        return egress.Refusal("identity_unrecognized", f"a flow running on its own would publish ({rule.id}) as "
                                                       f"{acts['label']}: {acts.get('why')}; nobody is there to "
                                                       "ask, so nothing was sent")

    async def check(rule: egress.EffectRule, request: Any, url: str, method: str) -> egress.Refusal | None:
        if rule.site not in acting.ACTING:
            return None
        body, headers = egress.LaneGuard._body(request)
        try:
            cap = publish.capture(rule, url, method, headers, body)
        except publish.Unbindable:  # who it posts as cannot be read, and nobody is there to ask
            return refuse(rule, {"id": None, "field": None, "label": "an identity agentos cannot read",
                                 "why": "the gate cannot read its body"})
        session = None
        context = getattr(guard, "_context", None)
        if context is not None and callable(getattr(context, "cookies", None)):
            try:
                session = await sites.account_identity(context, rule.site)
            except Exception:  # noqa: BLE001 - not read: the pin alone says who the lane is
                session = None
        acts = acting.resolve(cap.acting, lane=lane_id, pinned=pinned, session=session,
                              surfaces=_known_surfaces(runtime))
        if acts is None or acts["status"] != acting.UNRECOGNIZED:
            return None
        return refuse(rule, acts)

    return check
