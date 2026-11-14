"""The web app's API on a node.

`web/` speaks one API, the cloud's: `/api/orgs/{org}/...` (web/src/api/client.ts).
A node speaks it too, so the same app runs on a person's own computer with nothing branching in the
screens. A node is one org, `local`, with one computer in it.

Each module here owns one area and has `routes(api)`. It reshapes the node's real routes with
`base.read` and `base.write`, so auth, policy and tracing stay where they are. Whatever no module
claims goes through unchanged (`ThroughToTheNode`), which is right only when the two shapes match.
`web/scripts/contract.mjs` checks that against the types the screens are written to.
"""

from __future__ import annotations

import importlib
import pkgutil

from starlette.routing import BaseRoute, Mount

from agentos.api.app import Api
from agentos.api.web.base import ThroughToTheNode


def routes(api: Api) -> list[BaseRoute]:
    out: list[BaseRoute] = []
    for module in sorted(info.name for info in pkgutil.iter_modules(__path__)):
        if module == "base":
            continue
        out.extend(importlib.import_module(f"{__name__}.{module}").routes(api))
    out.append(Mount("/api/orgs/{org}", app=ThroughToTheNode()))
    return out
