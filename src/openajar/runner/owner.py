"""Who counts as the owner: in-person answers, the node's own installs and the owner's vouch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# who answered a card, as the approvals store wrote it: (decided_by, decided_via). The owner signed in
# to the panel, and the node's own CLI. The node token writes "human:owner" too, but it may be held by
# an agent, so what tells them apart is how the answer came: its via is owner_token.
IN_PERSON = frozenset({("human:owner", "session"), ("owner:cli", None), ("owner", None)})


def answered_in_person(approval: Any, *, relayed: bool = False) -> bool:
    """A person answered this card. `relayed`: a controller's signed message counts as one too."""
    by = str(approval.decided_by or "")
    return (by, approval.decided_via) in IN_PERSON or (relayed and by.split(":")[0] == "controller")


OWNER_INSTALL_KINDS = frozenset({"catalog", "local"})  # installs the owner chose: they count as the owner's vouch
# a connector the node generated from what an agent did once in a lane (openajar.profiles.learn,
# ConnectorService.install_learned): the node's own install, but never the owner's vouch
LEARNED_KIND = "learned"
INSTALL_KINDS = OWNER_INSTALL_KINDS | {LEARNED_KIND}  # saved_by kinds only the node's own installers write
TRY_SOURCE = "site_api_try"  # Run.source of a site API's try: counts against pace, says nothing of its health


@dataclass(frozen=True, slots=True)
class NodeInstall:
    """`by` for FlowService.save when the node itself installs a connector.* flow: ConnectorService
    as `catalog` (or `learned`, from install_learned), SiteApiService as `local`. A plain object on
    purpose: no JSON body, principal or export can be one, so nothing from outside can claim those
    kinds."""
    saved_by: dict[str, Any]


def is_owner(principal: Any) -> bool:
    """The human owner of the node (not an agent, a flow, or another human such as a VA).
    A signed, reviewed catalog connector the owner installed counts as the owner's vouch
    (`{"kind": "catalog", "installed_by": <owner>}`, docs/design/connectors.md 4.4), and so does a
    site API the owner activated (`{"kind": "local", "installed_by": <owner>}`, docs/SITE-APIS.md).
    A learned connector (`{"kind": "learned"}`) never does: the node wrote it from an agent's
    actions, so its `"publishes": false` marks are not the owner's word."""
    if principal is None:
        return False
    p = principal if isinstance(principal, dict) else principal.model_dump()
    if p.get("kind") == LEARNED_KIND:
        return False
    if p.get("kind") in OWNER_INSTALL_KINDS:
        return bool(p.get("installed_by")) and p.get("installed_by") == p.get("owner", "owner")
    return p.get("kind") == "human" and bool(p.get("id")) and p.get("id") == p.get("owner", "owner")
