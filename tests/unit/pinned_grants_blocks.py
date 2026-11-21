"""Grant blocks for the pins (test_pinned_grants, test_pinned_explain): v1 blocks with ids that
never repeat, each child naming its parent."""

from __future__ import annotations

import itertools

from openajar.policy.grants import GrantBlock

_IDS = (f"gr_{i:016d}" for i in itertools.count())


def block(parent: GrantBlock | None = None, **kw) -> GrantBlock:
    gid = next(_IDS)
    return GrantBlock(id=gid, rid="rv_" + gid[3:], iat=0, parent=parent.id if parent else None, **kw)
