"""The live-view link of an environment whose browser is not there: a small page instead of an error.

An agent gives the person the live view of its environment's browser (env/<id>-browser). Once that
environment is saved and its machine closed (idle, its time limit, or the agent's close), the lane is
gone, and the link used to say "No such lane". The viewer (openajar.node.viewer_app) asks this module
first, and the environments service (its `browser_story`, openajar.environments.waking) says what
became of it:

- saved: the signed-in owner (an owner link, or the owner's panel session in this browser) sees its
  title, when it was saved and the agent's note (as text), and a Resume button. The button posts the
  form to `POST /view/env/<id>-browser/resume`, which needs that same person (the session cookie,
  SameSite strict, and this page's own Origin), resumes the save held by no chat (`resume_saved`, as
  the web app's route does), and sends the browser on to the new environment's live view, an owner
  link. Anyone else is told only that it is saved.
- starting (a resumed one, before its browser is up): a page that reloads itself every few seconds,
  so the live view shows once it is ready.
- open again (resumed meanwhile): the owner gets a link to its live view; anyone else is told it is
  open again.
- closed with no save: it says so.

An expired link shows this page only to the signed-in owner; anyone else gets the usual expired page.
The page has no script: a plain form, a strict CSP (form-action 'self'), and the viewer's own styles.
`watch` is the other half of the live view's tie to environments: an open relay counts as use
(openajar.environments.watching).
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

log = logging.getLogger("openajar.viewer")

REFRESH_S = 3
ASSETS_PATH = "/viewer-assets"


def _service(viewer: Any) -> Any:
    get = getattr(viewer, "environments", None)
    try:
        return get() if callable(get) else None
    except Exception:  # noqa: BLE001 - no environments service: no story to tell
        return None


def watch(viewer: Any, lane_id: str) -> Callable[[], None] | None:
    """An open live view of an environment's browser counts as use while it is open; the function to
    call when it closes, or None."""
    svc = _service(viewer)
    begin = getattr(svc, "watch_lane", None)
    if not callable(begin):
        return None
    try:
        return begin(lane_id, "live_view")
    except Exception:  # the live view works without it
        log.warning("could not note that %s is watched", lane_id, exc_info=True)
        return None


def _story(viewer: Any, lane_id: str) -> dict | None:
    tell = getattr(_service(viewer), "browser_story", None)
    if not callable(tell):
        return None
    try:
        return tell(lane_id)
    except Exception:  # the usual page instead
        log.warning("could not tell what became of %s", lane_id, exc_info=True)
        return None


def _headers(viewer: Any, request: Request) -> dict[str, str]:
    out = dict(viewer._security_headers(request))
    out["Content-Security-Policy"] = ("default-src 'none'; style-src 'self'; img-src 'self' data:; "
                                      "form-action 'self'; base-uri 'none'; frame-ancestors 'self'")
    return out


def _card(viewer: Any, request: Request, title: str, body: str, *, status: int = 200,
          refresh: bool = False) -> HTMLResponse:
    base = request.scope.get("root_path", "")
    again = f'<meta http-equiv="refresh" content="{REFRESH_S}">' if refresh else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">{again}<title>{html.escape(title)} · openajar</title>
<link rel="stylesheet" href="{base}{ASSETS_PATH}/viewer.css"></head>
<body class="error-page"><main class="error-card saved-card" role="main">
<div class="error-mark" aria-hidden="true"></div>
{body}</main></body></html>"""
    return HTMLResponse(page, status_code=status, headers=_headers(viewer, request))


def _when(iso: str | None) -> str:
    return (iso or "").replace("T", " ").replace("Z", " UTC")[:20].strip() or "earlier"


def _owner_here(viewer: Any, request: Request, link: Any) -> bool:
    return bool(link is not None and link.owner) or viewer._person_here(request, origin=False)


def page(viewer: Any, request: Request, lane_id: str, link: Any, *, error: str | None = None) -> Response | None:
    """The page for `lane_id`'s live-view link when its lane is not there (`link`: the link, verified;
    None when it expired), or None to show the usual error."""
    story = _story(viewer, lane_id)
    if story is None:
        return None
    owner = _owner_here(viewer, request, link)
    if link is None and not owner:
        return None
    title = html.escape(story.get("title") or "this environment")
    state = story.get("state")
    if state == "starting":
        return _card(viewer, request, "Starting", f"<h1>{title} is starting</h1><p>Its machine and browser come "
                     "up now. This page shows the live view once they are ready.</p>", refresh=True)
    if state == "open_again":
        if not owner:
            return _card(viewer, request, "Open again", "<h1>This environment is open again</h1><p>Ask the agent for "
                         "the live view link of its new machine.</p>")
        new = viewer._owner_link(story["lane"])
        href = html.escape(new.path(request.scope.get("root_path", "")))
        return _card(viewer, request, "Open again", f"<h1>{title} is open again</h1><p>It runs as {story['again']}."
                     f"</p><p class=\"saved-actions\"><a class=\"btn btn-primary\" href=\"{href}\">Show its live view</a>"
                     "</p>")
    if state != "saved":
        return _card(viewer, request, "Closed", "<h1>This environment is closed</h1><p>Its machine is deleted and "
                     "it has no save to resume.</p>", status=410)
    if not owner:
        return _card(viewer, request, "Saved", "<h1>This environment is saved</h1><p>Its machine is closed. The "
                     "owner can resume it.</p>")
    return _card(viewer, request, "Saved", _saved_body(request, story, title, error), status=409 if error else 200)


def expired_page(viewer: Any, request: Request) -> Response | None:
    """`page` for a link that expired: only the signed-in owner sees it."""
    try:
        lane_id = viewer._lane_id(request.path_params)
    except ValueError:
        return None
    return page(viewer, request, lane_id, None)


def _saved_body(request: Request, story: dict, title: str, error: str | None) -> str:
    base = request.scope.get("root_path", "")
    workspace, name = str(request.path_params.get("workspace", "")), str(request.path_params.get("name", ""))
    action = f"{base}/view/{quote(workspace)}/{quote(name)}/resume?t={quote(request.query_params.get('t') or '')}"
    note = story.get("note")
    how = "the paused machine wakes in seconds" if story.get("paused") else "a new machine from its save"
    rows = [f"<p>Saved {html.escape(_when(story.get('saved_at')))}"
            + (f" ({html.escape(str(story['reason']))})" if story.get("reason") else "") + ".</p>"]
    if note:
        rows.append(f"<p class=\"saved-note-label\">The agent's note</p><p class=\"saved-note\">{html.escape(note)}</p>")
    if error:
        rows.append(f"<p class=\"saved-error\" role=\"alert\">{html.escape(error)}</p>")
    rows.append(f"<form method=\"post\" action=\"{html.escape(action)}\" class=\"saved-actions\">"
                f"<button class=\"btn btn-primary\" type=\"submit\">Resume</button></form>"
                f"<p class=\"saved-hint\">Resume rents it within this computer's limits ({how}). No chat holds it: "
                "the next agent that opens its title takes it.</p>")
    return f"<h1>{title} is saved</h1>" + "".join(rows)


async def resume(viewer: Any, request: Request) -> Response:
    """The Resume button: the owner in person (the panel session and this page's Origin) resumes the save,
    then goes on to the new environment's live view."""
    from openajar.node.viewer_tokens import TokenError

    try:
        lane_id = viewer._lane_id(request.path_params)
    except ValueError:
        return viewer._error_page(request, 404, "No such lane", "This is not a lane address.")
    link = None
    try:
        link = viewer.tokens.verify(request.query_params.get("t"), lane_id)
    except TokenError as e:
        if e.code != "expired":
            return viewer._error_page(request, 403, "Link not valid", e.message)
    person = bool(link is not None and link.owner) or viewer._person_here(request, origin=True)
    if not person or not viewer._same_origin(request, required=True):
        return viewer._error_page(request, 403, "Only the owner", "Only the owner resumes a saved environment: sign "
                                  "in to the panel on this computer in this browser, then press Resume again.")
    story = _story(viewer, lane_id)
    back = f"{request.scope.get('root_path', '')}/view/{request.path_params['workspace']}/" \
           f"{request.path_params['name']}?t={quote(request.query_params.get('t') or '')}"
    if story is None or story.get("state") != "saved":
        return RedirectResponse(back, status_code=303)
    svc = _service(viewer)
    try:
        env, _how = await svc.resume_saved(story["save_id"])
    except Exception as e:  # noqa: BLE001 - a limit or a failure: the page says it, with its fix
        message = getattr(e, "message", None) or "The resume did not start. Try again, or resume it from the web app."
        return page(viewer, request, lane_id, link, error=message) or viewer._error_page(request, 409, "Not resumed",
                                                                                       message)
    new = viewer._owner_link(env.browser_lane)
    return RedirectResponse(new.path(request.scope.get("root_path", "")), status_code=303)
