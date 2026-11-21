"""The node facts an approval card shows (openajar.core.cardfacts.CardFacts), registered in every node
runtime as `card_facts`: the effect rules (openajar.node.egress), who a body acts as (openajar.node.acting),
the sites' accounts (openajar.node.sites) and a lane's inbox (openajar.node.files). Each one reads the same
module attribute policy.cards read before, at call time, so a patched or reloaded rule table counts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openajar.node import acting


class NodeCardFacts:
    @property
    def unknown_kind(self) -> str:
        from openajar.node import egress

        return egress.UNKNOWN

    @property
    def surface(self) -> str:
        return acting.SURFACE

    @property
    def unrecognized(self) -> str:
        return acting.UNRECOGNIZED

    def effect_rule(self, rule_id: str) -> Any | None:
        from openajar.node import egress

        return next((r for r in egress.EFFECT_RULES if r.id == rule_id), None)

    def card_line(self, acts: Mapping[str, Any] | None) -> str | None:
        return acting.card_line(acts)

    def account_matches(self, expected: Any, identity: Mapping[str, Any] | None) -> bool | None:
        from openajar.node.sites import account_matches

        return account_matches(expected, identity)

    def site_key(self, name: str) -> str:
        from openajar.node.sites import site_key

        return site_key(name)

    def site(self, key: str) -> Any | None:
        from openajar.node.sites import site

        return site(key)

    def lane_files(self, config: Any, lane: str) -> Any:
        from openajar.node.files import FilesConfig, LaneFiles

        return LaneFiles(config.lane_dir(lane) / "files", FilesConfig.from_node(config))
