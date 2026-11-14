"""Every call the app makes must reach a route on a node.

    python tests/unit/web/test_every_call.py [filter]    # what is still not served, with the method

Reads the calls in `web/src/api` and in `web/src/features` (every `http<...>` and fetch of a path, which
a screen makes outside the client), and looks each up in the node's routes, as the node would: a
contract route first, then the same path without the org. `STILL_TO_BUILD` shrinks to nothing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from starlette.routing import Match, Mount

ROOT = Path(__file__).resolve().parents[3]
CLIENT = ROOT / "web" / "src" / "api"
FEATURES = ROOT / "web" / "src" / "features"
ORG_PREFIX = "/api/orgs/local"

# Client calls written with a value in the last place of the path, spelled out.
SITE_ACTIONS = ("read", "pause", "resume", "activate", "decline", "repair", "accept-empty", "keep-updated")
# A lane's sign-in step, as the app spells each action of `login/${action}`.
LOGIN_ACTIONS = ("refresh", "take", "resume", "cancel")


def calls() -> set[tuple[str, str]]:
    """(method, path), with every value in the path as `x`."""
    found: set[tuple[str, str]] = set()
    sources = [*CLIENT.glob("*.ts"), *FEATURES.rglob("*.ts"), *FEATURES.rglob("*.tsx")]
    for file in sorted(sources):
        if ".test." in file.name:
            continue
        text = file.read_text()
        for match in re.finditer(r"(?:path`|'|`)(/(?:api|v1)/[^'`\n]+)['`]", text):  # a prefix alone is no call
            tail = text[match.end():match.end() + 220]
            following = re.search(r"(?:path`|'|`)/(?:api|v1)/", tail)
            if following:
                tail = tail[:following.start()]
            method = re.search(r"method:\s*'(\w+)'", tail)
            raw = match.group(1).split("?")[0]
            for path in spelled(raw):
                found.add((method.group(1) if method else "GET", path))
    return found


def spelled(raw: str) -> list[str]:
    path = raw.replace("${org}", "local")
    if path.endswith("site-apis/${api}/${action.kind}"):
        return [re.sub(r"\$\{[^}]*\}", "x", path.replace("${action.kind}", kind)) for kind in SITE_ACTIONS]
    if path.endswith("/login/${action}"):
        return [re.sub(r"\$\{[^}]*\}", "x", path.replace("${action}", kind)) for kind in LOGIN_ACTIONS]
    return [re.sub(r"\$\{[^}]*\}", "x", path)]


def served(routes: list[Any], method: str, path: str) -> bool:
    scope = {"type": "http", "method": method, "path": path, "root_path": "", "headers": [], "path_params": {}}
    for route in routes:
        if isinstance(route, Mount) or getattr(route, "path", "") == "/{path:path}":
            continue
        kind, _ = route.matches(scope)
        if kind is Match.FULL:
            return True
    return False


def unserved(app: Any) -> list[tuple[str, str]]:
    routes = list(app.routes)
    missing = []
    for method, path in sorted(calls(), key=lambda item: (item[1], item[0])):
        bare = "/api" + path[len(ORG_PREFIX):] if path.startswith(ORG_PREFIX + "/") else path
        if not served(routes, method, path) and not served(routes, method, bare):
            missing.append((method, path))
    return missing


def test_the_app_reads_its_calls() -> None:
    assert len(calls()) > 150


if __name__ == "__main__":
    from agentos.api.app import ApiSettings, build_api
    from agentos.api.auth import PanelStore
    from agentos.dev.apifakes import demo_runtime

    wanted = sys.argv[1] if len(sys.argv) > 1 else ""
    app = build_api(demo_runtime(), ApiSettings(store=PanelStore(None), owner_token="t" * 43))
    left = [item for item in unserved(app) if wanted in item[1]]
    for method, path in left:
        print(f"{method:7}{path}")
    print(f"\n{len(left)} not served")


def test_no_two_modules_claim_a_route() -> None:
    """The first module in the alphabet would win without a word, and the other would be dead code."""
    from collections import Counter

    from starlette.routing import Route

    from agentos.api.app import ApiSettings, build_api
    from agentos.api.auth import PanelStore
    from agentos.dev.apifakes import demo_runtime

    app = build_api(demo_runtime(), ApiSettings(store=PanelStore(None), owner_token="t" * 43))
    seen = Counter((method, route.path) for route in app.routes if isinstance(route, Route)
                   for method in (route.methods or ()) if method != "HEAD")
    assert [key for key, times in seen.items() if times > 1] == []
