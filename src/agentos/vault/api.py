"""Panel API for the vault (docs/API.md, "Vault"). Mounted under /api by the panel.

    GET    /vault            -> [ {id, name, kind, domain, lanes, created_at, last_used} ]
    POST   /vault            {name, kind, domain, lanes, hosts?, username?, secret, totp?}
                             -> 201 entry (the secret is write-only, never echoed)
    DELETE /vault/{id}       -> {ok, id}
    GET    /vault/status     -> {initialized, state, mode, key_version, entries}
    POST   /vault/unseal     {passphrase?} -> status
    POST   /vault/seal       -> status

Authentication and CSRF are the panel's middleware; these handlers assume an owner.
Error bodies never contain request values (validation errors drop the input).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agentos.core.errors import AgentosError, InvalidRequest
from agentos.core.http_errors import error_body
from agentos.core.http_errors import status_for as shared_status_for
from agentos.vault.models import EntryKind
from agentos.vault.secret import Secret

MAX_BODY = 256 * 1024

# This route's own answers where the shared map (agentos.core.http_errors) says otherwise: a locked-out unseal
# is 429 here (the shared map says 403), and an entry or vault that already exists, or a busy vault, is 409 here
# (the shared map says 500, as do the other surfaces).
OWN_STATUS = {"vault_locked_out": 429, "vault_entry_exists": 409, "vault_exists": 409, "vault_busy": 409}


def status_for(e: AgentosError) -> int:
    return OWN_STATUS.get(e.code, shared_status_for(e))


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(error_body(code, message), status_code=status)


class CreateEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=200)
    kind: EntryKind
    domain: str | None = Field(default=None, max_length=300)
    lanes: list[str] = Field(default_factory=list, max_length=64)
    hosts: list[str] = Field(default_factory=list, max_length=64)
    username: SecretStr | None = None
    secret: SecretStr | None = None
    totp: SecretStr | None = None


class Unseal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: SecretStr | None = None


def _validation_message(e: ValidationError) -> str:
    parts = []
    for err in e.errors(include_input=False, include_url=False, include_context=False):
        loc = ".".join(str(p) for p in err.get("loc", ())) or "body"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid request body"


async def _body(request: Request, model: type[BaseModel]) -> BaseModel:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise InvalidRequest("request body too large")
    try:
        data = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise InvalidRequest("request body must be JSON") from None
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise InvalidRequest(_validation_message(e)) from None


def _secret(value: SecretStr | None) -> Secret | None:
    return Secret(value.get_secret_value()) if value is not None else None


def routes(runtime: Any) -> list[Route]:
    def vault() -> Any:
        return runtime.require("vault")

    def guarded(handler):
        async def wrapped(request: Request) -> JSONResponse:
            try:
                return await handler(request)
            except AgentosError as e:
                return _error(e.code, e.message, status_for(e))
        wrapped.__name__ = handler.__name__
        return wrapped

    async def list_entries(request: Request) -> JSONResponse:
        entries = await vault().list_entries()
        return JSONResponse([e.public() for e in entries])

    async def create_entry(request: Request) -> JSONResponse:
        body = await _body(request, CreateEntry)
        assert isinstance(body, CreateEntry)
        meta = await vault().add_entry(
            name=body.name, kind=body.kind, domain=body.domain, lanes=body.lanes, hosts=body.hosts,
            username=_secret(body.username), secret=_secret(body.secret), totp=_secret(body.totp),
        )
        return JSONResponse(meta.public(), status_code=201)

    async def delete_entry(request: Request) -> JSONResponse:
        meta = await vault().remove_entry(request.path_params["id"])
        return JSONResponse({"ok": True, "id": meta.id})

    async def get_status(request: Request) -> JSONResponse:
        return JSONResponse(vault().status())

    async def unseal(request: Request) -> JSONResponse:
        body = await _body(request, Unseal)
        assert isinstance(body, Unseal)
        return JSONResponse(await vault().unseal(_secret(body.passphrase)))

    async def seal(request: Request) -> JSONResponse:
        return JSONResponse(vault().seal())

    return [
        Route("/vault/status", guarded(get_status), methods=["GET"]),
        Route("/vault/unseal", guarded(unseal), methods=["POST"]),
        Route("/vault/seal", guarded(seal), methods=["POST"]),
        Route("/vault", guarded(list_entries), methods=["GET"]),
        Route("/vault", guarded(create_entry), methods=["POST"]),
        Route("/vault/{id}", guarded(delete_entry), methods=["DELETE"]),
    ]
