"""The node's forms for a secret (openajar.node.forms). The web app never holds the secret; it only starts a
trip and reads how far it got.

    POST /api/forms                  {kind, fields?}: the owner, in person. Starts one trip, answers its form_url
    GET  /api/forms/trips/{id}       the owner: the trip's stage (waiting, done, ended)
    GET  /api/forms/{token}          the page, for the trip's token (public: the token is the credential)
    POST /api/forms/{token}          what the page sends (urlencoded, or multipart for a file)

Only the owner in person starts a trip, never a grant or a token. The fields a trip takes are plain text
the app may see (a name, a site, a label); the secret is typed on the page and goes nowhere else.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from openajar.api.app import Api, access_of, endpoint, read_body
from openajar.api.errors import ApiError
from openajar.core.errors import InvalidRequest
from openajar.node import forms as node_forms
from openajar.runner.mail import form as mail_form

MAX_FIELD = 200
NOT_YOURS = "Only the owner, signed in here, can open a form for a secret. An agent or a grant never can."


class FormStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1, max_length=40)
    fields: dict[str, str] = Field(default_factory=dict, max_length=12)


def _check_fields(kind: node_forms.Kind, fields: dict[str, str]) -> dict[str, str]:
    """Only the kind's own plain fields, each present where needed. Messages name a field, never its value."""
    for name, value in fields.items():
        if name not in kind.takes:
            raise InvalidRequest(f"{name} is not a field of this form.")
        if not isinstance(value, str) or len(value) > MAX_FIELD:
            raise InvalidRequest(f"{name} is text of at most {MAX_FIELD} characters.")
    for name in kind.needs:
        if not fields.get(name):
            raise InvalidRequest(f"Give the {name} this form is for.")
    return dict(fields)


def kinds() -> dict[str, node_forms.Kind]:
    """Every kind a trip can be, by name: the node's own (openajar.node.forms), the workspace kinds
    (openajar.workspaces.form), the login (openajar.node.login_form) and a repository's token
    (openajar.environments.form), each built on node.forms."""
    from openajar.environments.form import RepoToken
    from openajar.node.login_form import LoginForm
    from openajar.workspaces.form import WorkspaceExport, WorkspaceImport

    items: list[node_forms.Kind] = [node_forms.VaultUnlock(), node_forms.VaultEntry(), node_forms.ProxyForm(),
                                    WorkspaceExport(), WorkspaceImport(), LoginForm(), RepoToken()]
    return {item.name: item for item in items}


START_KINDS = ("vault_unlock", "vault_entry", "proxy", "workspace_export", "workspace_import")


def routes(api: Api) -> list[Route]:
    def trips() -> node_forms.Trips:
        return node_forms.trips_of(api.runtime)

    async def start(request: Request) -> dict[str, Any]:
        access = access_of(request)
        if not access.owner or access.member is not None or not _in_person(request):
            access.trace("form.start", status="denied", summary="a form for a secret: the owner in person only")
            raise ApiError("denied", NOT_YOURS)
        body = await read_body(request, FormStart)
        if body.kind not in START_KINDS:
            raise InvalidRequest("Ask for a form this computer has: " + ", ".join(START_KINDS) + ".")
        kind = kinds()[body.kind]
        fields = _check_fields(kind, body.fields)
        trip = trips().start(body.kind, "owner", fields, subject=fields.get("workspace") or fields.get("label"))
        access.trace("form.start", summary=f"Open the {body.kind.replace('_', ' ')} form on this computer")
        return trip.public()

    async def state(request: Request) -> dict[str, Any]:
        access_of(request)
        trip = trips().by_id(request.path_params["id"])
        if trip is None:
            raise ApiError("not_found", "That form has ended. Start it again.")
        return trip.public()

    async def page_get(request: Request) -> Response:
        trip = trips().by_token(request.path_params["token"])
        if trip is None:
            return node_forms.ended()
        if not mail_form.direct(request):
            return mail_form.not_direct()
        return await kinds()[trip.kind].page(trip, api, trips())

    async def page_post(request: Request) -> Response:
        trip = trips().by_token(request.path_params["token"])
        if trip is None:
            return node_forms.ended()
        if not mail_form.direct(request) or not mail_form.same_origin(request):
            return mail_form.not_direct()
        kind = kinds()[trip.kind]
        if kind.multipart:
            form = await request.form()
        else:
            form = mail_form.read_form(await request.body())
        return await kind.submit(trip, api, form, trips())

    return [
        Route("/api/forms", endpoint(api, start, owner=True), methods=["POST"]),
        Route("/api/forms/trips/{id}", endpoint(api, state, owner=True), methods=["GET"]),
        Route("/api/forms/{token}", endpoint(api, page_get, public=True, action="form.page"), methods=["GET"]),
        Route("/api/forms/{token}", endpoint(api, page_post, public=True, action="form.send"), methods=["POST"]),
    ]


def _in_person(request: Request) -> bool:
    principal = getattr(request.state, "principal", None)
    return principal is None or getattr(principal, "kind", None) == "human"
