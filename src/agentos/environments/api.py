"""The repository card's routes on the node's API (docs/API.md, "Repository cards").

    POST /api/repo-connect/{id}/form        a link to the node's own page for the card's token (kind
                                            repo_token, agentos.environments.form): the page checks it with
                                            the host and with git, keeps it, and closes the card. The app
                                            waits on GET /api/forms/trips/{trip}. The owner in person, with a
                                            fresh passkey check
    POST /api/repo-connect/{id}/deploy-key  the node's deploy key for the card's repository (made if the
                                            vault has none), to add at the host. The owner in person
    POST /api/repo-connect/{id}/check       can a machine clone it now (after the key was added)? Closes the
                                            card when it can. The owner in person
    POST /api/repo-connect/{id}/decline     not now: the card closes, and the agent hears no. The owner in person
    POST /api/environments/{id}/download    {"path"}: pack a file or a folder under the environment's /work and
                                            answer with its one-use download link (agentos.environments.downloads),
                                            for `agentos environments download`. The owner
    POST /api/environments/prepare          {"repo", "ref", "force"}: start the build of the repository's prepared
                                            machine (agentos.environments.prepbuild), for `agentos environments
                                            prepare`, which follows its steps in the store. The owner
    POST /api/environments/resume           {"ref"}: resume a save (its title or save id) held by no chat
                                            (agentos.environments.waking), for `agentos environments resume`. The
                                            owner

`{id}` is the card's approval id, which the questions list marks with `repo_connect`
(agentos.environments.connect.annotate). Every route answers only the owner signed in with a person
present (Presence.PERSON), so no agent's token, grant or review link reaches it, and the form's link
also needs an owner with a passkey to confirm with it first (step_up_required, the panel's usual
step-up). No route here takes a token: the web app never holds one (docs/WEB-NODE.md). The token is
typed on the node's own page, whose link is its credential, works for 15 minutes, and ends once used.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from starlette.requests import Request
from starlette.routing import Route

from agentos.api.access import Presence
from agentos.api.app import Api, access_of, endpoint, principal_of, read_body
from agentos.api.errors import ApiError
from agentos.api.members import OWNER_ID
from agentos.api.route_ids import path_id
from agentos.core.errors import AgentosError, NotFound
from agentos.environments import connect
from agentos.environments import form as token_form
from agentos.node import forms as node_forms

FRESH_CHECK = "Confirm with your passkey first: a token opens your code to this computer."


class DownloadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str


class PrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    ref: str | None = None
    force: bool = False


def _owner_has_passkey(api: Api) -> bool:
    try:
        return bool(api.members.store.credentials(OWNER_ID))
    except Exception:  # noqa: BLE001  (cannot tell: ask for the passkey, toward safe)
        return True


def routes(api: Api) -> list[Route]:
    def mgr() -> Any:
        return api.svc("environments")

    def traced(request: Request, action: str, summary: str, error: AgentosError | None = None) -> None:
        if error is None:
            access_of(request).trace(action, summary=summary, approval=path_id(request))
        else:
            access_of(request).trace(action, status="error", summary=f"{summary}: {error.code}",
                                     approval=path_id(request))

    async def token_page(request: Request) -> dict:
        """The link to the card's token page, for the owner in person. The app opens it, and never the token."""
        access = access_of(request)
        if _owner_has_passkey(api) and access.presence < Presence.VERIFIED:
            access.trace("repo.token_form", status="denied", summary="a token needs a fresh passkey check")
            raise ApiError("step_up_required", FRESH_CHECK)
        m = mgr()
        row = connect.request(m.store, m.runtime.get("approvals"), path_id(request))
        trip = token_form.open_trip(node_forms.trips_of(api.runtime), row)
        traced(request, "repo.token_form", f"opened the token page for {row['repo']} on this computer")
        return {**trip.public(), "repo": row["repo"]}

    async def deploy_key(request: Request) -> dict:
        out = await connect.deploy_key(mgr(), path_id(request))
        traced(request, "repo.deploy_key", f"showed the deploy key {out['name']} for {out['repo']}")
        return out

    async def check(request: Request) -> dict:
        try:
            out = await connect.check(mgr(), path_id(request))
        except AgentosError as e:
            traced(request, "repo.check", "the repository is not connected yet", e)
            raise
        traced(request, "repo.check", f"{out['repo']} is connected")
        return out

    async def decline(request: Request) -> dict:
        m = mgr()
        p = principal_of(request)
        row = connect.decline(m.store, m.runtime.get("approvals"), path_id(request), by=f"{p.kind}:{p.id}")
        traced(request, "repo.decline", f"said not now to connecting {row['repo']}")
        return {"repo": row["repo"], "declined": True}

    async def download(request: Request) -> dict:
        """Pack a file or a folder off the environment's machine for the owner (the node's CLI): the same
        packing, cap and one-use link the agent's environment_download gives."""
        m = mgr()
        body = await read_body(request, DownloadBody)
        try:
            env = m.get(path_id(request))
        except (NotFound, ValueError, KeyError):
            raise NotFound(f"No environment {request.path_params['id']!r}.") from None
        try:
            out = await m.download_link(env, body.path, grant_id=None, by="owner")
        except AgentosError as e:
            access_of(request).trace("environment.download", status="error", lane=env.terminal_lane,
                                     summary=f"could not pack {body.path}: {e.code}")
            raise
        access_of(request).trace("environment.download", lane=env.terminal_lane,
                                 summary=f"packed {out['path']} ({out['files']} files, {out['bytes']} bytes) for a "
                                         "download link")
        return out

    async def prepare(request: Request) -> dict:
        """Start a prepared machine's build now (`agentos environments prepare`); the CLI follows its steps
        in the store. The owner."""
        body = await read_body(request, PrepareBody)
        out = await mgr().prepare_now(body.repo, body.ref, force=body.force)
        access_of(request).trace("environment.prepare", summary=f"started the prepared machine build of {out['repo']}")
        return out

    async def resume(request: Request) -> dict:
        """Resume a saved environment held by no chat (`agentos environments resume`). The owner."""
        body = await read_body(request, ResumeBody)
        m = mgr()
        try:
            env, how = await m.resume_saved(body.ref)
        except AgentosError as e:
            access_of(request).trace("environment.resume", status="error", summary=f"{body.ref[:80]}: {e.code}")
            raise
        access_of(request).trace("environment.resume", lane=env.terminal_lane,
                                 summary=f"resumed \"{env.title}\" as {env.id}" if how == "resumed" else
                                 f"\"{env.title}\" was open already ({env.id})")
        return m.owner_view(env, how)

    in_person = {"owner": True, "presence": Presence.PERSON}
    return [
        Route("/api/environments/{id}/download", endpoint(api, download, owner=True, action="environment.download"),
              methods=["POST"]),
        Route("/api/environments/prepare", endpoint(api, prepare, owner=True, action="environment.prepare"),
              methods=["POST"]),
        Route("/api/environments/resume", endpoint(api, resume, owner=True, action="environment.resume"),
              methods=["POST"]),
        Route("/api/repo-connect/{id}/form", endpoint(api, token_page, step_up=True, **in_person), methods=["POST"]),
        Route("/api/repo-connect/{id}/deploy-key", endpoint(api, deploy_key, **in_person), methods=["POST"]),
        Route("/api/repo-connect/{id}/check", endpoint(api, check, **in_person), methods=["POST"]),
        Route("/api/repo-connect/{id}/decline", endpoint(api, decline, **in_person), methods=["POST"]),
    ]
