"""The controller ceiling as node.toml writes it: its words, its defaults and the check the config
runs when it loads. What the ceiling allows and refuses is agentos.policy.ceiling.
"""

from __future__ import annotations

from typing import Any

from agentos.core.models import SPECIALS

EFFECTS = ("read", "draft", "commit", "pay", "irreversible")
STREAMS = ("view", "control", "terminal", "logs")
DEFAULT_MAX_EFFECT = "commit"
DEFAULT_SPECIALS: tuple[str, ...] = ()
DEFAULT_STREAMS: tuple[str, ...] = ("view",)
SET_BY = ("default", "join", "self_hosted", "owner")  # node.toml controller_ceiling_by


def problems(max_effect: Any, specials: Any, streams: Any, set_by: Any = "default") -> list[str]:
    """What is wrong with a ceiling as written in node.toml (empty when it is valid)."""
    out = []
    if max_effect not in EFFECTS:
        out.append(f"controller_max_effect must be one of {', '.join(EFFECTS)}")
    if not isinstance(specials, list | tuple) or any(s not in SPECIALS for s in specials):
        out.append(f"controller_specials may list {', '.join(SPECIALS)}")
    if not isinstance(streams, list | tuple) or any(s not in STREAMS for s in streams):
        out.append(f"controller_streams may list {', '.join(STREAMS)}")
    if set_by not in SET_BY:
        out.append(f"controller_ceiling_by must be one of {', '.join(SET_BY)}")
    return out
