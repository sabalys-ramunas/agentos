"""The node facts an approval card shows, as a contract below both (openajar.policy.cards reads them).

A card is built in policy, but some of what it says only the node knows: which effect rule a publish
matched, who a body acts as, whether the signed-in account is the lane's, and where a lane's inbox is.
The node registers an implementation as the runtime service `card_facts` (openajar.node.cardfacts, in
every node runtime). Without one a card leaves those facts out, as it does for any fact it cannot read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

SERVICE = "card_facts"


class CardFacts(Protocol):
    @property
    def unknown_kind(self) -> str:
        """The kind of a publish no effect rule reads (openajar.node.egress.UNKNOWN)."""

    @property
    def surface(self) -> str:
        """An `acts_as` status: the body posts as a surface (openajar.node.acting.SURFACE)."""

    @property
    def unrecognized(self) -> str:
        """An `acts_as` status: an identity openajar does not know (openajar.node.acting.UNRECOGNIZED)."""

    def effect_rule(self, rule_id: str) -> Any | None:
        """The effect rule with this id (it has `.site` and `.verified`), or None."""

    def card_line(self, acts: Mapping[str, Any] | None) -> str | None:
        """The identity line of a publish card ("Posts as ...")."""

    def account_matches(self, expected: Any, identity: Mapping[str, Any] | None) -> bool | None:
        """Whether the signed-in account is the lane's pinned one (openajar.node.sites.account_matches)."""

    def site_key(self, name: str) -> str:
        """A site name as the node keys it."""

    def site(self, key: str) -> Any | None:
        """The node's record of a site (its `.identity` says whether the node can read the account)."""

    def lane_files(self, config: Any, lane: str) -> Any:
        """A lane's inbox and outbox, from the node config alone."""
