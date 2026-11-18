"""The built web app at `/` (a checkout's web/dist, or the copy `agentos web install` unpacked; see
agentos.core.webapp), with SPA fallback and cache headers. Files are served as they are, from under
that folder only: a path or link that leads out of it is refused.

Hashed files under /assets are immutable for a year; everything else (index.html, the
icon) is revalidated on every load so a new build shows up at once.
"""

from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from agentos.api.app import Api
from agentos.api.errors import error_body

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"

# a checkout with web/, before the web app is built
NOT_BUILT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>agentos</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font: 16px system-ui, sans-serif; max-width: 36rem; margin: 4rem auto; padding: 0 1rem">
<h1>The web app is not built yet</h1>
<p>The API and MCP are running. Build the web app once with <code>pnpm install &amp;&amp; pnpm build</code>
in <code>web/</code> (Node.js 22 or later; <code>npx pnpm@10.33.0</code> works without installing pnpm),
then start agentos again. Or point <code>AGENTOS_PANEL_DIR</code> at a built copy.</p>
</body></html>"""

# an installation without the app's source (no web/ beside it) and no installed copy (agentos web install)
NO_APP = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>agentos</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font: 16px system-ui, sans-serif; max-width: 36rem; margin: 4rem auto; padding: 0 1rem">
<h1>This node has no web app</h1>
<p>The API and MCP are running. Add the web app with <code>agentos web install</code> on this computer,
then reload this page.</p>
<p>To watch an environment, open the live view link the agent gives you (it starts with
<code>/view/</code>), or run <code>agentos environments list</code>, <code>agentos environments show ID</code>
and <code>agentos open --terminal</code> on the node.</p>
</body></html>"""


def placeholder(panel_dir: Path) -> str:
    """The page for a node with no built app: how to build it, or, with no source to build, what to
    use instead."""
    if (panel_dir.parent / "package.json").is_file() or (panel_dir.parent.parent / "web" / "package.json").is_file():
        return NOT_BUILT
    return NO_APP


def _resolve(root: Path, rel: str) -> Path | None:
    """A file under root, or None. Refuses anything that escapes root."""
    if not rel or "\x00" in rel:
        return None
    try:
        candidate = (root / rel).resolve()
        candidate.relative_to(root)
    except (ValueError, OSError):
        return None
    return candidate if candidate.is_file() else None


def routes(api: Api) -> list[Route]:
    async def serve(request: Request) -> Response:
        path: str = request.path_params.get("path", "")
        if path == "api" or path.startswith("api/"):
            # `not_served` marks a route this computer does not have, so the app shows "not here" and not an error.
            return JSONResponse(
                {**error_body("not_found", "No such API route."), "not_served": True}, status_code=404
            )
        panel_dir = api.panel_dir
        root = panel_dir.resolve() if panel_dir.exists() else None
        index = _resolve(root, "index.html") if root else None  # a linked index.html is held to root too
        if root is None or index is None:
            return HTMLResponse(placeholder(panel_dir), headers={"Cache-Control": REVALIDATE})
        file = _resolve(root, path)
        if file is not None and file.name != "index.html":
            cache = IMMUTABLE if path.startswith("assets/") else REVALIDATE
            return FileResponse(file, headers={"Cache-Control": cache})
        if path.startswith("assets/"):
            # A stale hashed asset: answer 404 rather than HTML the browser would try to run.
            return Response("Not found", status_code=404, media_type="text/plain")
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": REVALIDATE})

    return [Route("/{path:path}", serve, methods=["GET", "HEAD"])]
