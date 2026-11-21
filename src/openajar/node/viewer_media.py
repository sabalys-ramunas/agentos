"""What an environment's signed links open in the live view's app (openajar.node.viewer_app): a proof's PNG,
a video proof's MP4 and its GIF preview, and a frame of its replay. Each link opens that one file and nothing
of the lane; the environments service checks the link."""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, Response

log = logging.getLogger("openajar.viewer")

PROOF_MEDIA = {"mp4": "video/mp4", "gif": "image/gif"}  # a video proof's files (openajar.environments.proofs)


def _error(status: int, code: str, message: str) -> Response:
    from openajar.node.viewer_app import _error as error

    return error(status, code, message)


def proof(viewer: Any, request: Request) -> Response:
    """An environment's proof image (openajar.environments.proofs), for its own signed link: the
    environments service checks the link, and it opens that one image and nothing of the lane."""
    return _signed_image(viewer, request, "signed_proof", "proof", "image/png",
                              "This proof link is not valid or has expired. Ask the agent for a fresh one "
                              "(environment() lists them).")

def proof_media(viewer: Any, request: Request) -> Response:
    """A video proof's MP4 or its GIF preview, for its proof's signed link. Served from its file with
    range requests, so a player seeks without loading it all."""
    ext = request.url.path.rsplit(".", 1)[-1]
    svc = viewer.environments() if viewer.environments is not None else None
    read = getattr(svc, "signed_proof_path", None)
    params, path = request.path_params, None
    if callable(read) and ext in PROOF_MEDIA:
        try:
            path = read(params["workspace"], params["name"], params["proof"], ext, request.query_params.get("t"))
        except Exception:
            log.warning("could not read an environment's proof", exc_info=True)
    if path is None:
        return _error(403, "denied", "This proof link is not valid or has expired. Ask the agent for a fresh "
                                     "one (environment() lists them).")
    return FileResponse(path, media_type=PROOF_MEDIA[ext], headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; media-src 'self'; img-src 'self'",
        "Referrer-Policy": "no-referrer"})

def replay_frame(viewer: Any, request: Request) -> Response:
    """A frame of an environment's replay (openajar.environments.replay), for the owner's signed link:
    it opens that environment's frames and nothing of the lane."""
    return _signed_image(viewer, request, "signed_frame", "frame", "image/jpeg",
                              "This replay link is not valid or has expired. Load the replay again for fresh "
                              "links.")

def _signed_image(viewer: Any, request: Request, reader: str, key: str, media: str, refused: str) -> Response:
    svc = viewer.environments() if viewer.environments is not None else None
    read = getattr(svc, reader, None)
    params = request.path_params
    data = None
    if callable(read):
        try:
            data = read(params["workspace"], params["name"], params[key], request.query_params.get("t"))
        except Exception:
            log.warning("could not read an environment's %s", key, exc_info=True)
    if not data:
        return _error(403, "denied", refused)
    return Response(data, media_type=media, headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'", "Referrer-Policy": "no-referrer"})
