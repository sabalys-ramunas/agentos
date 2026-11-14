"""The operation registry: which connector operation performs a kind of action on a site, and
which egress effect rule proves it (docs/design/engagement-engine.md 5.1).

    registry = Registry(installed=lambda flow_id: ..., verified=lambda flow_id: ...)
    spec = registry.get("reddit", Kind.POST)               # OpSpec or None
    registry.valid(spec)                                   # True only when it may run
    registry.problem(spec)                                 # why not: no_spec, no_rule, rule_unverified,
                                                           # no_flow, op_unverified
    registry.register(OpSpec(...), replace=True)           # later briefs add kinds

The profile edits (brief B2, PROFILE_SPECS) are registered under their own kinds
(`profile_name`, `profile_headline`, `profile_about`, `profile_photo`, `profile_cover`): the
planner plans one `profile_edit` cap for them together, and agentos.engage.profile picks which
field an edit sets and gives its value as the target (the params come from the program, never
from Content). Their egress rules are not captured yet, so every one is `rule_unverified`.

An operation is valid only when all three hold:

- its `rule_id` names an EffectRule in agentos.node.egress.EFFECT_RULES whose `verified` is
  not empty (checked against a real account's traffic), so the egress guard can prove the write;
- its connector flow (`connector.<site>.<op>`) is installed on this node; and
- the installed operation is marked verified (`verified(flow_id)` is not empty): a canary on a
  throwaway account ran it end to end and read the effect back. The mark comes from the node's
  own ledger (agentos.runner.connectors.marks, written by the owner with `agentos connector
  verify`), never from the connector file or a bundle, and it names the implementation the
  canary ran: once the installed operation differs (any step, locator, readback, preflight,
  commit request or pace), the mark is stale and the operation is `op_unverified` again, with
  the reason `verified_stale` (`reason`, `describe`). Operations written from notes, never run,
  stay invalid (`op_unverified`) however good their rule is.

  A mark written by the node's own replays (`by: "replay"`, marks.mark_replay) counts only for a
  learned operation: `kind(flow_id)`, the installed flow's saved_by kind, must be `learned`
  (agentos.profiles.learn, ConnectorService.install_learned). Anywhere else it is
  `op_unverified` with the reason `replay_not_learned`: a catalog operation needs the owner's mark.

The executor checks validity before a window acts: one invalid operation aborts the whole window
and holds the surface (`no_valid_rule`). Nothing links a connector operation to a rule anywhere
else today; this table is that link, and a new operation is not used until it is listed here.

Kinds are strings so later briefs can add kinds the planner does not draw (a profile edit);
the engine's own kinds are agentos.engage.caps.Kind values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentos.engage.caps import Kind

if TYPE_CHECKING:
    from agentos.engage.ports import Content
    from agentos.node.egress import EffectRule

ParamsBuilder = Callable[["Content | None", "str | None"], dict[str, Any]]

NO_SPEC, NO_FLOW, NO_RULE, RULE_UNVERIFIED = "no_spec", "no_flow", "no_rule", "rule_unverified"
OP_UNVERIFIED = "op_unverified"
VERIFIED_STALE = "verified_stale"   # the reason an operation with a mark is op_unverified: it changed since
REPLAY_NOT_LEARNED = "replay_not_learned"  # or: its mark is a replay's, and it is not a learned operation
BY_REPLAY = "replay"   # agentos.runner.connectors.marks.BY_REPLAY
LEARNED_KIND = "learned"   # agentos.runner.models.LEARNED_KIND

REDDIT_POST_RE = re.compile(r"^https://(?:www\.)?reddit\.com/r/([A-Za-z0-9_]{2,21})/comments/([a-z0-9]{5,10})(?:/|$)")
SUBREDDIT_RE = re.compile(r"^(?:r/)?([A-Za-z0-9_]{2,21})$")
LINKEDIN_POST_RE = re.compile(r"^(?:https://www\.linkedin\.com/feed/update/)?(urn:li:(?:activity|share|ugcPost):[0-9]{10,25})/?$")
LINKEDIN_PROFILE_RE = re.compile(r"^https://www\.linkedin\.com/(in|company)/([A-Za-z0-9_-]{2,100})/?(?:[?#].*)?$")


class BadTarget(ValueError):
    """The target is not something this operation can act on (the executor skips it: `bad_target`)."""


def _text_params(content: Content | None, target: str | None) -> dict[str, Any]:
    """The default: {"text", "target"} when present. A real connector names its own params."""
    out: dict[str, Any] = {}
    if content is not None:
        out["text"] = content.text
        if content.attachments:
            out["attachments"] = list(content.attachments)
    if target is not None:
        out["target"] = target
    return out


def reddit_post_text_params(content: Content | None, target: str | None) -> dict[str, Any]:
    """connector.reddit.post_text: subreddit (the target, without r/), title and body."""
    assert content is not None and target is not None
    first = content.text.strip().splitlines()[0] if content.text.strip() else ""
    title = (content.title or first).strip()
    return {"subreddit": target.removeprefix("r/"), "title": title[:300], "body": content.text}


def _text(content: Content | None) -> str:
    if content is None or not content.text.strip():
        raise BadTarget("this operation needs words")
    return content.text


def _match(rx: re.Pattern[str], target: str | None, what: str) -> re.Match[str]:
    m = rx.match((target or "").strip())
    if m is None:
        raise BadTarget(f"{target!r} is not {what}")
    return m


def reddit_comment_params(content: Content | None, target: str | None) -> dict[str, Any]:
    """connector.reddit.comment: the post (its permalink) and the words."""
    m = _match(REDDIT_POST_RE, target, "a Reddit post permalink")
    return {"subreddit": m.group(1), "post_id": m.group(2), "text": _text(content)}


def reddit_join_params(content: Content | None, target: str | None) -> dict[str, Any]:
    """connector.reddit.join: the subreddit (r/name or name)."""
    return {"subreddit": _match(SUBREDDIT_RE, target, "a subreddit name").group(1)}


def linkedin_post_params(content: Content | None, target: str | None) -> dict[str, Any]:
    """connector.linkedin.post: the words, and the first attachment as the photo."""
    out: dict[str, Any] = {"text": _text(content)}
    if content is not None and content.attachments:
        out["image"] = content.attachments[0]
    return out


def linkedin_comment_params(content: Content | None, target: str | None) -> dict[str, Any]:
    """connector.linkedin.comment: the post (its urn or /feed/update/<urn>/ URL) and the words."""
    return {"post_urn": _match(LINKEDIN_POST_RE, target, "a LinkedIn post").group(1), "text": _text(content)}


def linkedin_react_params(content: Content | None, target: str | None) -> dict[str, Any]:
    """connector.linkedin.react: the post (its urn or /feed/update/<urn>/ URL)."""
    return {"post_urn": _match(LINKEDIN_POST_RE, target, "a LinkedIn post").group(1)}


def linkedin_follow_params(content: Content | None, target: str | None) -> dict[str, Any]:
    """connector.linkedin.follow: a member (/in/<name>/) or a company (/company/<name>/) page URL."""
    m = _match(LINKEDIN_PROFILE_RE, target, "a LinkedIn profile or company page")
    return {"profile_kind": m.group(1), "name": m.group(2)}


PROFILE_FILE_RE = re.compile(r"^in_[0-9a-f]{6,40}$")


def _profile_value(target: str | None) -> str:
    if not target or not target.strip():
        raise BadTarget("a profile edit needs the value it sets")
    return target


def linkedin_profile_name_params(content: Content | None, target: str | None) -> dict[str, Any]:
    """connector.linkedin.profile_name: the target is {"first", "last"} as JSON (agentos.engage.profile)."""
    try:
        name = json.loads(_profile_value(target))
        return {"first_name": str(name["first"]), "last_name": str(name["last"])}
    except (ValueError, KeyError, TypeError):
        raise BadTarget(f"{target!r} is not a first and last name") from None


def _profile_text(param: str) -> ParamsBuilder:
    def build(content: Content | None, target: str | None) -> dict[str, Any]:
        return {param: _profile_value(target)}
    build.__doc__ = f"connector.linkedin.profile_{param}: the target is the {param} itself."
    return build


def linkedin_profile_image_params(content: Content | None, target: str | None) -> dict[str, Any]:
    """connector.linkedin.profile_photo and profile_cover: a files_put id in the lane's inbox. A
    profile photo's edit row keeps its content hash (sha256:...), which the executor turns into
    the lane's copy right before the run (agentos.engage.profile.lane_file); a hash that reaches
    here unresolved is a bad target, never sent."""
    return {"image": _match(PROFILE_FILE_RE, target, "a file id from files_put").group(0)}


@dataclass(frozen=True)
class OpSpec:
    """One way to perform `kind` on `site`.

    flow_id:      the connector operation's flow id, `connector.<site>.<op>`
    rule_id:      the egress EffectRule id that proves the write (`reddit.post`)
    needs_text:   it takes words (a comment, a post): the executor asks the ContentPort first
    needs_target: it acts on something (a post, a profile, a community): from the content's
                  targets, else the TargetPort; none means the operation is skipped (`no_target`)
    params:       builds the connector params from (content, target)
    """
    site: str
    kind: str
    flow_id: str
    rule_id: str
    needs_text: bool = False
    needs_target: bool = False
    params: ParamsBuilder = _text_params

    def build(self, content: Content | None, target: str | None) -> dict[str, Any]:
        return self.params(content, target)


# The operations the engine knows (connectors/reddit and connectors/linkedin). Each stays invalid
# until its connector is installed and marked verified after a canary on this node
# (`op_unverified`); none is today (reddit post_text's 2026-11-04 canary ran an earlier
# implementation, so its mark is stale). LinkedIn's like is the connector operation `react`.
DEFAULT_SPECS: tuple[OpSpec, ...] = (
    OpSpec("reddit", Kind.POST, "connector.reddit.post_text", "reddit.post", needs_text=True, needs_target=True,
           params=reddit_post_text_params),
    OpSpec("reddit", Kind.COMMENT, "connector.reddit.comment", "reddit.comment", needs_text=True, needs_target=True,
           params=reddit_comment_params),
    OpSpec("reddit", Kind.JOIN, "connector.reddit.join", "reddit.join", needs_target=True, params=reddit_join_params),
    OpSpec("linkedin", Kind.POST, "connector.linkedin.post", "linkedin.post", needs_text=True,
           params=linkedin_post_params),
    OpSpec("linkedin", Kind.COMMENT, "connector.linkedin.comment", "linkedin.comment", needs_text=True,
           needs_target=True, params=linkedin_comment_params),
    OpSpec("linkedin", Kind.LIKE, "connector.linkedin.react", "linkedin.like", needs_target=True,
           params=linkedin_react_params),
    OpSpec("linkedin", Kind.FOLLOW, "connector.linkedin.follow", "linkedin.follow", needs_target=True,
           params=linkedin_follow_params),
)


# The profile edits (brief B2): one OpSpec per field, its kind `profile_<field>`. Not in
# DEFAULT_SPECS (the engagement kinds a planner draws); the Registry holds both.
PROFILE_SPECS: tuple[OpSpec, ...] = (
    OpSpec("linkedin", "profile_name", "connector.linkedin.profile_name", "linkedin.profile.name", needs_text=True,
           params=linkedin_profile_name_params),
    OpSpec("linkedin", "profile_headline", "connector.linkedin.profile_headline", "linkedin.profile.headline",
           needs_text=True, params=_profile_text("headline")),
    OpSpec("linkedin", "profile_about", "connector.linkedin.profile_about", "linkedin.profile.about",
           needs_text=True, params=_profile_text("about")),
    OpSpec("linkedin", "profile_photo", "connector.linkedin.profile_photo", "linkedin.profile.photo",
           params=linkedin_profile_image_params),
    OpSpec("linkedin", "profile_cover", "connector.linkedin.profile_cover", "linkedin.profile.cover",
           params=linkedin_profile_image_params),
)
ALL_SPECS: tuple[OpSpec, ...] = (*DEFAULT_SPECS, *PROFILE_SPECS)


def default_rules() -> tuple[EffectRule, ...]:
    from agentos.node.egress import EFFECT_RULES

    return EFFECT_RULES


def _mark(value: Any) -> tuple[str, bool]:
    """(the mark's text, stale) from what `verified(flow_id)` returned: a string, None, or a
    mark state with `text` and `stale` (agentos.runner.connectors.marks.MarkState)."""
    if value is None:
        return "", False
    if hasattr(value, "stale"):
        return str(getattr(value, "text", "") or ""), bool(value.stale)
    return str(value), False


def _by(value: Any) -> str:
    """Who wrote the mark: `owner` (a plain string counts as the owner's), or `replay`."""
    return str(getattr(value, "by", None) or "owner")


class Registry:
    """(site, kind) -> OpSpec, with the validity check. `installed(flow_id)` says whether a flow
    is installed on this node, `verified(flow_id)` the installed operation's mark from the node's
    ledger ("" or None: not verified, the default; a mark state with `stale` set: marked for
    another implementation); `kind(flow_id)` the installed flow's saved_by kind (a replay's mark
    counts only when it is `learned`; default: none, so no replay mark counts); `rules` defaults to
    the egress guard's EFFECT_RULES."""

    def __init__(self, specs: Iterable[OpSpec] = ALL_SPECS, *, installed: Callable[[str], bool] | None = None,
                 verified: Callable[[str], Any] | None = None, rules: Iterable[EffectRule] | None = None,
                 kind: Callable[[str], str | None] | None = None):
        self._specs: dict[tuple[str, str], OpSpec] = {}
        self._installed = installed or (lambda flow_id: False)
        self._verified = verified or (lambda flow_id: "")
        self._kind = kind or (lambda flow_id: None)
        self._rules = {r.id: r for r in (default_rules() if rules is None else rules)}
        for spec in specs:
            self.register(spec)

    def register(self, spec: OpSpec, *, replace: bool = False) -> None:
        key = (spec.site, str(spec.kind))
        if key in self._specs and not replace:
            raise ValueError(f"{spec.site} {spec.kind} is already registered (pass replace=True)")
        self._specs[key] = spec

    def get(self, site: str, kind: Kind | str) -> OpSpec | None:
        return self._specs.get((site, str(kind)))

    def specs(self, site: str | None = None) -> list[OpSpec]:
        return [s for (st, _), s in sorted(self._specs.items()) if site is None or st == site]

    def problem(self, spec: OpSpec | None) -> str | None:
        """None when the spec may run, else why not."""
        if spec is None:
            return NO_SPEC
        rule = self._rules.get(spec.rule_id)
        if rule is None:
            return NO_RULE
        if not rule.verified.strip():
            return RULE_UNVERIFIED
        if not self._installed(spec.flow_id):
            return NO_FLOW
        value = self._verified(spec.flow_id)
        text, stale = _mark(value)
        if stale or not text.strip() or self._replay_refused(spec.flow_id, value):
            return OP_UNVERIFIED
        return None

    def _replay_refused(self, flow_id: str, value: Any) -> bool:
        """A replay's mark on an operation that is not learned: never honoured."""
        return _by(value) == BY_REPLAY and self._kind(flow_id) != LEARNED_KIND

    def reason(self, spec: OpSpec | None) -> str | None:
        """Why an operation is op_unverified though it has a mark: `verified_stale` when the mark
        names another implementation than the one installed, `replay_not_learned` when a replay
        wrote it for an operation that is not learned. None otherwise."""
        if spec is None or self.problem(spec) != OP_UNVERIFIED:
            return None
        value = self._verified(spec.flow_id)
        if _mark(value)[1]:
            return VERIFIED_STALE
        return REPLAY_NOT_LEARNED if _mark(value)[0].strip() and self._replay_refused(spec.flow_id, value) else None

    def valid(self, spec: OpSpec | None) -> bool:
        return self.problem(spec) is None

    def describe(self, site: str | None = None) -> list[dict[str, Any]]:
        out = []
        for s in self.specs(site):
            value = self._verified(s.flow_id) if self._installed(s.flow_id) else None
            text, stale = _mark(value)
            refused = self._replay_refused(s.flow_id, value)
            out.append({"site": s.site, "kind": str(s.kind), "flow": s.flow_id, "rule": s.rule_id,
                        "verified": "" if stale or refused else text, "by": _by(value) if text else None,
                        "valid": self.valid(s), "problem": self.problem(s), "reason": self.reason(s)})
        return out


def valid(spec: OpSpec | None, registry: Registry) -> bool:
    """`registry.valid(spec)`, as a function."""
    return registry.valid(spec)
