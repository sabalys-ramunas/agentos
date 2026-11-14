"""Profiles in the panel: one person or business, written once, shown on every attached account (agentos.profiles).

  GET    /api/profiles?workspace=                  every profile: name, kind, accounts, and a draft waiting
  POST   /api/profiles                             {profile, workspace, kind, display_name, ...}: save a whole profile
  GET    /api/profiles/sites                       every site an account can be on: its login domain and its
                                                   profile fields with their length limits (mapping.sites)
  GET    /api/profiles/{ws}/{id}?version=          one profile, and any draft waiting
  PATCH  /api/profiles/{ws}/{id}                   {changes}: the latest version with these fields merged in
                                                   (a null removes a field)
  GET    /api/profiles/{ws}/{id}/status            per account: each site field and how far it is
  POST   /api/profiles/{ws}/{id}/approve           {version}: approve the draft the owner read (required)
  POST   /api/profiles/{ws}/{id}/reject            {version, note?}: turn it down
  POST   /api/profiles/{ws}/{id}/attach            {surface, site?, overrides?}: an account takes this profile
  POST   /api/profiles/{ws}/{id}/detach            {surface}
  POST   /api/profiles/{ws}/{id}/mark              {surface, fields}: these manual fields were changed in the lane
  POST   /api/profiles/{ws}/{id}/photo/{kind}?name=  the body is the image (avatar or cover): kept, then set
  GET    /api/profile-photos/{id}                  a stored photo by the hex of its sha256, as an image

Only the owner in person: the signed-in panel, on a session cookie. A bearer token is refused, the owner token
included, because agents hold it (they save drafts through the MCP tools instead). So is a member and a grant.
Every save here is the owner's, so it goes live at once, and the accounts are brought in line from it with no
approval card (the save is the consent): learned fields by the profile sync, LinkedIn's by the engagement
engine. A lane with "Ask me first" on still waits for the owner. Without the profiles service the list answers
`available: false`.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from agentos.api.app import Api, access_of, endpoint, owner_in_person, principal_of, read_json_object
from agentos.api.errors import ApiError
from agentos.core.errors import InvalidRequest, NotFound

SERVICE = "profiles"
PERSON_ONLY = "Only the owner signed in to the panel can read or change profiles here. A token cannot, the owner token included."
PHOTO_KINDS = ("avatar", "cover")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_HEADERS = {"x-content-type-options": "nosniff", "cache-control": "private, max-age=3600",
                 "content-security-policy": "default-src 'none'; sandbox"}


def _text(body: dict[str, Any], key: str, *, required: bool = True) -> str | None:
    value = body.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"{key} is text.")
    return value.strip()


def _version(body: dict[str, Any]) -> int | None:
    version = body.get("version")
    if version is None:
        return None
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise InvalidRequest("version is a version number.")
    return version


def routes(api: Api) -> list[Route]:
    def person(request: Request, action: str) -> Any:
        """The owner in person (owner_in_person: a session cookie, not a member, never a bearer token), or a
        refusal that is traced. Only then do the service calls below pass `as_owner=True` / `in_person=True`.
        Returns the principal the service records."""
        if not owner_in_person(request):
            access_of(request).trace(action, status="denied", summary="profiles are the owner's, in person")
            raise ApiError("denied", PERSON_ONLY)
        return principal_of(request)

    def ref_of(request: Request) -> str:
        return f"{request.path_params['ws']}/{request.path_params['id']}"

    async def listing(request: Request) -> dict[str, Any]:
        person(request, "profiles.list")
        svc = api.optional(SERVICE)
        if svc is None:
            return {"available": False, "profiles": []}
        workspace = request.query_params.get("workspace") or None
        return {"available": True, "profiles": await asyncio.to_thread(svc.list, workspace)}

    async def sites(request: Request) -> list[dict[str, Any]]:
        from agentos.profiles.mapping import sites as site_list

        person(request, "profiles.sites")
        return site_list()

    async def save(request: Request) -> dict[str, Any]:
        who = person(request, "profiles.save")
        svc = api.svc(SERVICE)
        body = await read_json_object(request)
        out = await asyncio.to_thread(svc.save, body, by=who, as_owner=True)
        access_of(request).trace("profiles.save", summary=f"saved profile {out['profile']} version {out['version']}")
        return out

    async def get(request: Request) -> dict[str, Any]:
        person(request, "profiles.get")
        svc = api.svc(SERVICE)
        raw = request.query_params.get("version")
        if raw is not None and not raw.isdigit():
            raise InvalidRequest("version is a number.")
        return await asyncio.to_thread(svc.get, ref_of(request), version=int(raw) if raw else None)

    async def update(request: Request) -> dict[str, Any]:
        who = person(request, "profiles.update")
        svc = api.svc(SERVICE)
        body = await read_json_object(request)
        changes = body.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise InvalidRequest("changes are the fields to set, like {\"headline\": \"...\"}.")
        ref = ref_of(request)
        out = await asyncio.to_thread(svc.update, ref, changes, by=who, as_owner=True)
        access_of(request).trace("profiles.update", summary=f"changed {', '.join(sorted(changes))} on {ref}")
        return out

    async def status(request: Request) -> dict[str, Any]:
        person(request, "profiles.status")
        svc = api.svc(SERVICE)
        return await asyncio.to_thread(svc.status, ref_of(request))

    async def decide(request: Request, approve: bool) -> dict[str, Any]:
        action = "profiles.approve" if approve else "profiles.reject"
        who = person(request, action)
        svc = api.svc(SERVICE)
        body = await read_json_object(request)
        version = _version(body)
        if version is None:  # never "the latest": another draft may have arrived since the owner looked
            raise InvalidRequest(f"say which version to {'approve' if approve else 'reject'}: the one you read.")
        note = _text(body, "note", required=False)
        ref = ref_of(request)
        out = await asyncio.to_thread(svc.decide, ref, version, approve=approve, by=who, in_person=True, note=note)
        access_of(request).trace(action, summary=f"{'approved' if approve else 'rejected'} {ref} version {out['version']}")
        return out

    async def approve(request: Request) -> dict[str, Any]:
        return await decide(request, True)

    async def reject(request: Request) -> dict[str, Any]:
        return await decide(request, False)

    async def attach(request: Request) -> dict[str, Any]:
        who = person(request, "profiles.attach")
        svc = api.svc(SERVICE)
        body = await read_json_object(request)
        surface = _text(body, "surface")
        site = _text(body, "site", required=False)
        overrides = body.get("overrides") or {}
        if not isinstance(overrides, dict) or not all(isinstance(v, str) for v in overrides.values()):
            raise InvalidRequest("overrides maps a site field to the text this account shows.")
        ref = ref_of(request)
        out = await asyncio.to_thread(svc.attach, ref, surface, by=who, site=site, overrides=overrides, as_owner=True)
        access_of(request).trace("profiles.attach", lane=surface, summary=f"attached {surface} to {ref}")
        return out

    async def detach(request: Request) -> dict[str, Any]:
        who = person(request, "profiles.detach")
        svc = api.svc(SERVICE)
        body = await read_json_object(request)
        surface = _text(body, "surface")
        ref = ref_of(request)
        out = await asyncio.to_thread(svc.detach, ref, surface, by=who, as_owner=True)
        access_of(request).trace("profiles.detach", lane=surface, summary=f"detached {surface} from {ref}")
        return out

    async def mark(request: Request) -> dict[str, Any]:
        who = person(request, "profiles.mark")
        svc = api.svc(SERVICE)
        body = await read_json_object(request)
        surface = _text(body, "surface")
        fields = body.get("fields")
        if not isinstance(fields, list) or not fields or not all(isinstance(f, str) for f in fields):
            raise InvalidRequest("fields lists the site fields that now show the profile's value.")
        ref = ref_of(request)

        def run() -> dict[str, Any]:
            pv = svc.for_surface(surface)
            if pv is None or f"{pv.definition.workspace}/{pv.definition.id}" != ref:
                raise NotFound(f"{surface} is not attached to {ref}")
            return svc.mark_synced(surface, fields, by=who)

        out = await asyncio.to_thread(run)
        access_of(request).trace("profiles.mark", lane=surface, summary=f"{', '.join(fields)} on {surface} done by hand")
        return out

    async def photo_put(request: Request) -> dict[str, Any]:
        from agentos.profiles.store import MAX_PHOTO_BYTES

        who = person(request, "profiles.photo")
        svc = api.svc(SERVICE)
        kind = request.path_params["kind"]
        if kind not in PHOTO_KINDS:
            raise InvalidRequest("a profile photo is the avatar or the cover.")
        name = (request.query_params.get("name") or f"{kind}.jpg").strip()[:200]
        limit = f"the photo is larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB"
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_PHOTO_BYTES:
            raise InvalidRequest(limit)
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():  # read up to the limit, and no further
            total += len(chunk)
            if total > MAX_PHOTO_BYTES:
                raise InvalidRequest(limit)
            chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            raise InvalidRequest("send the image as the request body.")
        ref = ref_of(request)
        ws = request.path_params["ws"]

        def run() -> dict[str, Any]:
            kept = svc.put_photo(ws, name, data)
            return {**svc.update(ref, {"photos": {kind: kept["photo"]}}, by=who, as_owner=True), "photo": kept}

        out = await asyncio.to_thread(run)
        access_of(request).trace("profiles.photo", summary=f"set the {kind} of {ref}")
        return out

    async def photo_get(request: Request) -> Response:
        person(request, "profiles.photo.get")
        svc = api.svc(SERVICE)
        sha = request.path_params["id"]
        if not SHA_RE.match(sha):
            raise InvalidRequest("a photo is named by the 64 hex digits of its sha256.")
        path, meta = await asyncio.to_thread(svc.store.photo, f"sha256:{sha}")
        return FileResponse(path, media_type=meta["type"], headers=IMAGE_HEADERS)

    def r(path: str, fn: Any, method: str, action: str) -> Route:
        return Route(path, endpoint(api, fn, owner=True, action=action), methods=[method])

    one = "/api/profiles/{ws}/{id}"
    return [
        r("/api/profiles", listing, "GET", "profiles.list"),
        r("/api/profiles", save, "POST", "profiles.save"),
        r("/api/profiles/sites", sites, "GET", "profiles.sites"),
        r(one, get, "GET", "profiles.get"),
        r(one, update, "PATCH", "profiles.update"),
        r(f"{one}/status", status, "GET", "profiles.status"),
        r(f"{one}/approve", approve, "POST", "profiles.approve"),
        r(f"{one}/reject", reject, "POST", "profiles.reject"),
        r(f"{one}/attach", attach, "POST", "profiles.attach"),
        r(f"{one}/detach", detach, "POST", "profiles.detach"),
        r(f"{one}/mark", mark, "POST", "profiles.mark"),
        r(f"{one}/photo/{{kind}}", photo_put, "POST", "profiles.photo"),
        r("/api/profile-photos/{id}", photo_get, "GET", "profiles.photo.get"),
    ]
