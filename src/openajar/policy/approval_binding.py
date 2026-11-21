"""What an approval binds to: the content digest a standing approval lists, the ActionBinding whose
hash a one-off approval holds (and the check that a stored one still matches the call), and the
targets (`flow:`, `connector:`, `tool:`) a call names. Pure functions of the call; the store is
openajar.policy.approvals.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from typing import Any

from openajar.policy.util import content_hash

CONTENT_EFFECTS = ("commit",)  # effects whose content is published; standing approvals bind it
_TARGET_RE = re.compile(r"^(flow:[a-z0-9][a-z0-9_.-]{0,250}@[A-Za-z0-9._:-]{1,128}|tool:[a-z][a-z0-9_]{0,63}"
                        r"|connector:[a-z][a-z0-9-]{0,30}\.[a-z][a-z0-9_]{0,40}@[A-Za-z0-9._:-]{1,128})$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FLOW_CONTENT_KEYS = {"flow", "version", "lane", "params"}  # openajar.runner.flows approval_content
# openajar.runner.connectors.surfaces SurfaceService.approval: a connector run on a surface
_CONNECTOR_CONTENT_KEYS = {"operation", "contract", "surface", "acts_as", "params"}
NAMED_TARGETS = ("flow:", "connector:")  # targets that name what runs, not only the tool
_PUBLISH_CONTENT_KEYS = {"publish", "lane", "record"}  # openajar.node.publish Capture.content


def is_publish(content: Any) -> bool:
    """Is this the content of a publish the commit gate caught (openajar.node.publish)?"""
    return isinstance(content, dict) and set(content) == _PUBLISH_CONTENT_KEYS \
        and isinstance(content.get("publish"), str) and isinstance(content.get("record"), dict)


def publish_acts(content: Any) -> dict[str, Any] | None:
    """Who a publish the commit gate caught acts as, when the gate resolved it (openajar.node.acting:
    `status` self, surface or unrecognized), else None."""
    if not is_publish(content):
        return None
    acts = content["record"].get("acts_as")
    return acts if isinstance(acts, dict) and isinstance(acts.get("status"), str) else None


def content_digest(content: Any) -> str:
    """The digest a standing approval's `contents` list holds for a call: sha256 over the
    canonical JSON of the `content` the node passes to authorize (for a flow run: flow id,
    version, lane and params)."""
    return content_hash(content)


# How an action approval binds what it is for (Approval.binding). None: a card made before
# ActionBinding (read for one release; a later stage drops the legacy forms).
BINDING_CALL = "call"    # the policy gate's: the hash covers routing facts only the gate knows
BINDING_KEYED = "keyed"  # one operation the node queues itself: whoever holds the key, params and content can hash it again
_BINDING_EXTRA = frozenset({"cost", "facts", "taint"})


@dataclass(frozen=True)
class ActionBinding:
    """What one action approval is for, and the one way its digests are made.

    `content_digest` is content_digest(`content`): what the owner reads, checked again by whoever
    acts (the runner at dispatch, a trigger proposal, a workflow step). `content_hash`
    covers the whole action: the tool, effect, lane, domain, summary and content, plus `params` (the
    exact params a queued run is submitted with) and `key` (the one operation it is for) when given,
    and the policy gate's stated cost and routing facts (`extra`). A field that is absent is left out,
    so the policy gate's hashes are the ones it made before this class existed."""

    effect: str
    tool: str | None
    lane: str | None
    params: Any = None
    content: Any = None
    key: str | None = None
    domain: str | None = None
    summary: str = ""
    extra: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.extra and not set(self.extra) <= _BINDING_EXTRA:
            raise ValueError(f"an action binding's extra holds only {sorted(_BINDING_EXTRA)}")

    @property
    def form(self) -> str:
        return BINDING_KEYED if self.key is not None or self.params is not None else BINDING_CALL

    def bound(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tool": self.tool, "effect": self.effect, "lane": self.lane, "domain": self.domain,
                               "summary": self.summary, "content": self.content}
        for k, v in (self.extra or {}).items():
            if v:
                out[k] = v
        if self.params is not None:
            out["params"] = self.params
        if self.key is not None:
            out["key"] = self.key
        return out

    @property
    def content_hash(self) -> str:
        return content_hash(self.bound())

    @property
    def content_digest(self) -> str:
        return content_digest(self.content)


def is_legacy_hash(value: Any) -> bool:
    """A bare sha256 hex: how trigger proposals wrote content_hash before ActionBinding."""
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def action_binding_problem(ap: Any, binding: ActionBinding, *, legacy: tuple[str, ...] = ()) -> str | None:
    """None when the action approval `ap` covers exactly `binding`, else what is wrong, worded to
    follow "approval <id> ". The lane the card names and the content digest are always checked; the
    whole-action hash too when the card is keyed (and not edited: an approved edit moved the digest, which is checked). A card made
    before ActionBinding (`binding` None) holds the hash of its caller's old form: one of `legacy` must
    match when any is given. Fails closed: an approval that binds nothing covers nothing."""
    if getattr(ap, "kind", None) != "action":
        return "is not an approval of an action"
    form = getattr(ap, "binding", None)
    digest = getattr(ap, "content_digest", None)
    have = str(getattr(ap, "content_hash", None) or "")
    if form is not None and digest is None:
        return "binds no content"
    lane = getattr(ap, "lane", None)
    if lane is not None and binding.lane is not None and lane != binding.lane:
        return f"is for {lane}, not {binding.lane}"
    if digest is not None and not hmac.compare_digest(str(digest), binding.content_digest):
        return "is for different content"
    if form == BINDING_KEYED and not getattr(ap, "edit", None):
        if not hmac.compare_digest(have, binding.content_hash):
            return "is for a different action"
    elif form is None and legacy:
        if not any(hmac.compare_digest(have, h) for h in legacy):
            return "is for a different action"
    elif digest is None:
        return "binds no content"
    return None


def call_targets(tool: str | None, content: Any, lane_id: str | None) -> set[str]:
    """What a call is, in standing-approval target terms: `tool:<name>`, `flow:<id>@<version>`
    when the content is a flow run's approval content for this lane, and
    `connector:<site>.<op>@<contract>` when it is a connector run's for this surface (the
    contract hash survives a locator-only repair, docs/design/connectors.md 4.12)."""
    out = {f"tool:{tool}"} if tool else set()
    if (isinstance(content, dict) and set(content) == _FLOW_CONTENT_KEYS and content.get("lane") == lane_id
            and isinstance(content.get("flow"), str) and isinstance(content.get("version"), str)):
        out.add(f"flow:{content['flow']}@{content['version']}")
    if (isinstance(content, dict) and set(content) == _CONNECTOR_CONTENT_KEYS and content.get("surface") == lane_id
            and isinstance(content.get("operation"), str) and isinstance(content.get("contract"), str)):
        out.add(f"connector:{content['operation']}@{content['contract']}")
    return out


def named_target(tool: str | None, content: Any, lane_id: str | None) -> str | None:
    """The flow or connector target of a call, for an approval's `flow_target` and routing."""
    return next((t for t in sorted(call_targets(tool, content, lane_id)) if t.startswith(NAMED_TARGETS)), None)
