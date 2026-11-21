"""Brand: the product and business profiles of a workspace, as the app's Brand page and Needs you read them.

    GET  /api/orgs/{org}/brand?workspace=              product and business profiles, with a waiting draft
    GET  /api/orgs/{org}/brand/{ws}/{id}               one: brand (tokens flattened), guidelines, references,
                                                       and the waiting draft with the agent's reason and diff
    GET  /api/orgs/{org}/brand/proposals               waiting drafts, in a shape Needs you lists beside approvals
    POST /api/orgs/{org}/brand/{ws}/{id}/approve       {version}
    POST /api/orgs/{org}/brand/{ws}/{id}/reject        {version, note?}

Every one runs the node's own profile routes (routes_profiles), so the node decides who may: the owner in
person, on a signed-in session. Reading stays that narrow on purpose. A profile is a business's or a person's
identity, the node never shows it to a token or a member, and agents read the approved one through
profile_use, which checks their grant. Widening who may read is a decision for the owner, not for a page.
So a member and a token are refused here as they are on the node, `proposals` included: only the owner can
decide a draft, and the app's Needs you reads that refusal as "no brand proposals for you" (useBrandProposals).

A brand proposal is a profile draft, not an approval. It is decided with approve or reject on the profile,
always naming the version that was read, and never through the approval routes.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.routing import BaseRoute

from openajar.api.app import Api, read_json_object
from openajar.api.web.base import org_route, read, write
from openajar.core.errors import InvalidRequest

KINDS = ("product", "business")
TOKENS_MAX = 400
TEXT_CAP = 2000
DIFF_LINES = 60


def cap(text: Any, n: int = TEXT_CAP) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _token_value(value: Any) -> str:
    if isinstance(value, dict):
        if isinstance(value.get("hex"), str):
            return value["hex"]
        if "value" in value and "unit" in value:
            return f"{value['value']}{value['unit']}"
    if isinstance(value, (list, tuple)):
        return ", ".join(_token_value(v) for v in value)[:200]
    return str(value)[:200]


def flat_tokens(tree: Any, path: str = "", kind: str | None = None, out: list[dict[str, Any]] | None = None
                ) -> list[dict[str, Any]]:
    """Design Tokens JSON as a list: each token's path, type (inherited from its group) and value as text."""
    out = [] if out is None else out
    if not isinstance(tree, dict) or len(out) >= TOKENS_MAX:
        return out
    kind = tree.get("$type", kind) if isinstance(tree.get("$type", kind), str) else kind
    if "$value" in tree:
        out.append({"name": path, "type": kind, "value": _token_value(tree["$value"])})
        return out
    for key, sub in tree.items():
        if not key.startswith("$"):
            flat_tokens(sub, f"{path}.{key}" if path else key, kind, out)
    return out


def brand_out(brand: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(brand, dict):
        return None
    theme = brand.get("theme") if isinstance(brand.get("theme"), dict) else None
    return {
        "tokens": flat_tokens(brand.get("tokens") or {}),
        "fonts": [{"family": cap(f.get("family"), 120), "role": f.get("role"), "weights": list(f.get("weights") or [])}
                  for f in brand.get("fonts") or [] if isinstance(f, dict)],
        "logo": cap(brand.get("logo"), 500),
        "components": [{"name": cap(c.get("name"), 120), "attr": cap(c.get("attr"), 200),
                        "class_pattern": cap(c.get("class_pattern"), 200)}
                       for c in brand.get("components") or [] if isinstance(c, dict)],
        "theme": {"how": theme.get("how") or "media", "name": theme.get("name"), "light": theme.get("light"),
                  "dark": theme.get("dark")} if theme else None,
        "widths": list(brand.get("widths") or []),
        "engine": brand.get("engine"),
    }


def row_out(row: dict[str, Any]) -> dict[str, Any]:
    ref = row["profile"]
    workspace, _, profile = ref.partition("/")
    return {"ref": ref, "workspace": workspace, "profile": profile, "kind": row.get("kind"),
            "display_name": cap(row.get("display_name"), 200), "headline": cap(row.get("headline"), 300),
            "version": row.get("version"), "state": row.get("state"), "draft_waiting": row.get("draft_waiting")}


def guidelines_out(guidelines: Any) -> list[dict[str, Any]]:
    return [{"id": g.get("id"), "text": cap(g.get("text")), "check": g.get("check"),
             "threshold": g.get("threshold"), "advisory": bool(g.get("advisory"))}
            for g in guidelines or [] if isinstance(g, dict)]


def profile_out(got: dict[str, Any], draft: dict[str, Any] | None) -> dict[str, Any]:
    d = got.get("definition") or {}
    ref = f"{got['workspace']}/{got['profile']}"
    return {
        "ref": ref, "workspace": got["workspace"], "profile": got["profile"], "kind": d.get("kind"),
        "display_name": cap(d.get("display_name"), 200), "headline": cap(d.get("headline"), 300),
        "version": got.get("version"), "state": got.get("state"), "draft_waiting": got.get("draft_waiting"),
        "website": cap(d.get("website"), 500), "created_at": got.get("created_at"), "decided_at": got.get("decided_at"),
        "brand": brand_out(d.get("brand")),
        "guidelines": guidelines_out(d.get("guidelines")),
        "references": [{"name": cap(r.get("name"), 200), "url": cap(r.get("url"), 1000), "note": cap(r.get("note"), 500)}
                       for r in d.get("references") or [] if isinstance(r, dict)],
        # where it runs; the vault entries an environment needs are names of secrets, so they stay out
        "environments": [{"name": cap(e.get("name"), 200), "url": cap(e.get("url"), 1000)}
                         for e in d.get("environments") or [] if isinstance(e, dict)],
        "draft": draft,
    }


def draft_out(version: dict[str, Any]) -> dict[str, Any]:
    d = version.get("definition") or {}
    brand = d.get("brand") if isinstance(d.get("brand"), dict) else {}
    return {"version": version.get("version"), "created_at": version.get("created_at"),
            "created_by": cap(version.get("created_by"), 120), "reason": cap(version.get("reason"), 600),
            "diff": [cap(line, 400) for line in (version.get("diff") or [])[:DIFF_LINES]],
            # the draft's own tokens and guidelines, so the owner sees each changed value beside today's
            "tokens": flat_tokens(brand.get("tokens") or {}),
            "guidelines": guidelines_out(d.get("guidelines"))}


def _path(ws: str, pid: str) -> str:
    return f"/api/profiles/{quote(ws, safe='')}/{quote(pid, safe='')}"


def _version(body: dict[str, Any]) -> int:
    v = body.get("version")
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise InvalidRequest("Say which version you read: version is a number.")
    return v


def routes(api: Api) -> list[BaseRoute]:
    async def brand_rows(request: Request, workspace: str | None) -> dict[str, Any]:
        listed = await read(request, "/api/profiles", query={"workspace": workspace})
        rows = [row_out(r) for r in listed.get("profiles") or [] if r.get("kind") in KINDS]
        return {"available": bool(listed.get("available")), "profiles": rows}

    async def listing(request: Request) -> dict[str, Any]:
        return await brand_rows(request, request.query_params.get("workspace") or None)

    async def one(request: Request) -> dict[str, Any]:
        ws, pid = request.path_params["ws"], request.path_params["id"]
        got = await read(request, _path(ws, pid))
        if (got.get("definition") or {}).get("kind") not in KINDS:
            raise InvalidRequest("This profile is not a product or a business, so it has no brand.")
        draft = None
        if got.get("draft_waiting"):
            draft = draft_out(await read(request, _path(ws, pid), query={"version": got["draft_waiting"]}))
        return profile_out(got, draft)

    async def proposals(request: Request) -> list[dict[str, Any]]:
        listed = await read(request, "/api/profiles")  # the owner in person, or the node's refusal
        out = []
        for row in listed.get("profiles") or []:
            if row.get("kind") not in KINDS or not row.get("draft_waiting"):
                continue
            ws, _, pid = row["profile"].partition("/")
            draft = draft_out(await read(request, _path(ws, pid), query={"version": row["draft_waiting"]}))
            out.append({"id": f"brand:{row['profile']}@{draft['version']}", "kind": "brand", "ref": row["profile"],
                        "workspace": ws, "display_name": cap(row.get("display_name"), 200),
                        "version": draft["version"], "created_at": draft["created_at"],
                        "created_by": draft["created_by"], "reason": draft["reason"], "diff": draft["diff"]})
        return sorted(out, key=lambda p: p["created_at"] or "", reverse=True)

    async def decide(request: Request, verb: str) -> dict[str, Any]:
        ws, pid = request.path_params["ws"], request.path_params["id"]
        body = await read_json_object(request)
        sent: dict[str, Any] = {"version": _version(body)}
        if verb == "reject" and isinstance(body.get("note"), str) and body["note"].strip():
            sent["note"] = body["note"]
        out = await write(request, "POST", f"{_path(ws, pid)}/{verb}", sent)
        return {"ref": f"{ws}/{pid}", "version": out.get("version", sent["version"]),
                "state": "approved" if verb == "approve" else "rejected"}

    async def approve(request: Request) -> dict[str, Any]:
        return await decide(request, "approve")

    async def reject(request: Request) -> dict[str, Any]:
        return await decide(request, "reject")

    return [
        org_route(api, "/brand", listing, ["GET"]),
        org_route(api, "/brand/proposals", proposals, ["GET"], owner=True),
        org_route(api, "/brand/{ws}/{id}", one, ["GET"]),
        org_route(api, "/brand/{ws}/{id}/approve", approve, ["POST"]),
        org_route(api, "/brand/{ws}/{id}/reject", reject, ["POST"]),
    ]
