"""Email on a node, as the app reads it (web/src/api/types.ts, `Mailbox` to `MailRoutine`).

Mailboxes, messages and sends are the node's `/api/mail` (runner/mail). Leads and lists are
`/api/workflows` (runner/workflows). Every route here runs one of those inside this request, so who
may read or send is still decided by the node, and a refusal goes back as it was.

What the node does not keep is `null` and the screens say so: a week of replies, a count of planned
sends, a letter a list is sent with, or an address's country. Nothing is filled in from a guess.
A message id is the mailbox and the node's message number, `biz-a~mail~12`, because a path cannot
hold a slash. A password never passes through here: a link to the node's own form is all it makes.
"""

from __future__ import annotations

from datetime import UTC
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute

from agentos.api.app import Api, read_json_object
from agentos.api.errors import ApiError
from agentos.api.web.base import Refused, org_route, read, write
from agentos.runner.mail.form import SOURCES
from agentos.runner.mail.providers import by_id

SOURCE_KEY_WORDS = {"linkedin.com": "linkedin"}
TRIGGER_KEYS = ("kind", "label", "cron", "lane", "from", "subject", "url", "hook")


def tilde(box_id: str) -> str:
    """`biz-a/mail` in a path: a slash would be read as a new segment."""
    return box_id.replace("/", "~")


def untilde(box_id: str) -> str:
    return box_id.replace("~", "/", 1)


def composite(box_id: str, number: Any) -> str:
    return f"{tilde(box_id)}~{number}"


def split_message(value: str) -> tuple[str, str]:
    """The mailbox and the node's message number, from `biz-a~mail~12`."""
    head, _, number = str(value).rpartition("~")
    if not head or not number.isdigit():
        raise ApiError("invalid", "That message is not one the app can open. Open it again from the list.")
    return untilde(head), number


def domain_of(address: str) -> str:
    return address.rpartition("@")[2]


def iso_of(text: str | None) -> str:
    """An RFC 2822 date from a message header, as ISO, or "" when it does not say one."""
    try:
        when = parsedate_to_datetime(text or "")
    except (TypeError, ValueError, IndexError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def server(value: dict[str, Any] | None) -> str | None:
    return f"{value['host']}:{value['port']}" if value else None


def provider_name(provider_id: str | None) -> str | None:
    row = by_id(provider_id)
    return row.name if row is not None else None


def health_of(health: dict[str, Any] | None) -> tuple[str, str | None, dict[str, str] | None]:
    """What the node knows about the last read: a poll that worked, one that did not, or none yet."""
    if not health:
        return "unchecked", None, None
    at = health.get("at")
    if health.get("ok"):
        return "good", at, None
    problem = {"since": at or "", "title": "This mailbox cannot be read",
               "body": health.get("words") or "The last read did not finish.", "next": "Connect it again"}
    return "cannot_read", at, problem


def mailbox_of(api: Api, row: dict[str, Any], sent_today: int, daily_limit: int) -> dict[str, Any]:
    health, checked_at, problem = health_of(row.get("health"))
    address = row["address"]
    return {
        "id": row["id"],
        "address": address,
        "site": domain_of(address),
        "provider": provider_name(row.get("provider")),
        "workspace": row["id"].split("/", 1)[0],
        "computer": api.node_name,
        "health": health,
        "problem": problem,
        "checked_at": checked_at,
        "new_count": None,
        "arrivals": None,
        "sent_today": sent_today,
        "planned_today": None,
        "daily_limit": daily_limit,
        "week": None,
        "routines": None,
        "incoming": server(row.get("imap")) or "",
        "outgoing": server(row.get("smtp")) or "",
    }


def discovery_of(api: Api, address: str, found: dict[str, Any] | None) -> dict[str, Any]:
    """`MailDiscovery`. `found` is None when no servers were found: the person types them in."""
    domain = domain_of(address)
    if found is None:
        # The names are typed in, and the form still opens on this computer: it is the one computer there is to name.
        return {"address": address, "domain": domain, "result": "not_found", "provider": None, "site": None,
                "password": None, "incoming": None, "outgoing": None, "source": None, "odd_part": None,
                "computers": [{"id": api.node_name, "word": "This computer", "has_screen": True}]}
    return {
        "address": found["address"],
        "domain": domain,
        "result": "needs_a_look" if found.get("needs_yes") else "found",
        "provider": provider_name(found.get("provider")),
        "site": domain,
        "password": "app_password" if found.get("needs_app_password") else "normal",
        "incoming": server(found.get("imap")),
        "outgoing": server(found.get("smtp")),
        "source": SOURCES.get(found.get("source") or "", found.get("source")),
        "odd_part": None,
        "computers": [{"id": api.node_name, "word": "This computer", "has_screen": True}],
    }


def message_of(box_id: str, row: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    """`MailMessage`. The list carries a clip and no body; one message carries its whole text, as text."""
    name, address = parseaddr(row.get("from") or "")
    return {
        "id": composite(box_id, row["id"]),
        "mailbox": box_id,
        "kind": "mail",
        "from_name": name,
        "from_address": address,
        "subject": row.get("subject") or "",
        "clip": row.get("clip") or "",
        "at": iso_of(row.get("date")),
        "unread": not row.get("seen", False),
        "body": [] if text is None else [{"kind": "text", "text": text}],
        "attachments": [{"name": item.get("name") or "", "type": item.get("type") or None}
                        for item in row.get("attachments") or []],
        "did": None,
        "list": None,
    }


def source_of(text: str) -> tuple[str, str, str | None]:
    """A lead's source as the app shows it: its kind, its name, and its link when it is one."""
    parts = urlsplit(text if "://" in text else "")
    host = (parts.hostname or "").removeprefix("www.")
    if parts.scheme in ("http", "https") and host:
        key = next((SOURCE_KEY_WORDS[name] for name in SOURCE_KEY_WORDS if host == name or host.endswith("." + name)), "directory")
        return key, host, text
    return "directory", text, None


def lead_of(row: dict[str, Any]) -> dict[str, Any]:
    key, word, url = source_of(row.get("source") or "")
    address = row["email"]
    return {
        "id": address,
        "name": row.get("name") or "",
        "role": None,
        "company": row.get("company") or "",
        "address": address,
        "country": None,
        "source": key,
        "source_word": word,
        "source_url": url,
        "found_at": row.get("added_at") or "",
        "why": row.get("why") or "",
        "look": None,
    }


BATCH_STATUS = {"waiting": "waiting", "taken": "taken", "picked": "taken", "dropped": "dropped", "expired": "dropped"}


def batch_of(batch: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """`LeadBatch`. The node shows who was left out only in person, so `suppressed` is not listed here."""
    leads = [lead_of(row) for row in batch.get("leads") or []]
    tally: dict[tuple[str, str], int] = {}
    for lead in leads:
        tally[(lead["source"], lead["source_word"])] = tally.get((lead["source"], lead["source_word"]), 0) + 1
    sources = [{"key": key, "word": word, "note": f"{count} {'person' if count == 1 else 'people'}"}
               for (key, word), count in tally.items()]
    return {
        "id": batch["id"],
        "list": batch["list"],
        "list_name": names.get(batch["list"], batch["list"]),
        "workspace": batch["workspace"],
        "mailbox": None,
        "by": batch.get("by") or "",
        "at": batch.get("at") or "",
        "found": batch.get("received", len(leads)),
        "sources": sources,
        "leads": leads,
        "suppressed": None,
        "status": BATCH_STATUS.get(batch.get("state") or "", "dropped"),
    }


def list_of(row: dict[str, Any], waiting: dict[str, str], first: bool) -> dict[str, Any]:
    """`MailList` as the node keeps it: its name, size and suppressed count. The rest is null."""
    return {
        "id": row["id"],
        "name": row["name"],
        "workspace": row["workspace"],
        "size": row["size"],
        "mailbox": None,
        "address": None,
        "mailbox_limit": None,
        "other_per_day": None,
        "state": None,
        "pace": None,
        "people": None,
        "days": None,
        "suppressed": None,
        "first_list": first,
        "countries": None,
        "routine": None,
        "letter": None,
        "batch_waiting": waiting.get(row["id"]),
    }


def person_of(row: dict[str, Any]) -> dict[str, Any]:
    """`ListPerson`. A person who came from a lead says so; nobody's state is kept on the list yet."""
    return {"id": row["email"], "name": row.get("name") or "", "company": "", "address": row["email"],
            "source_word": "From a lead" if row.get("lead") else "Added by a person", "state": None}


def routine_of(row: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """`MailRoutine`: the workflow as the node saves it. Its run history and who it chases are not
    kept in this shape yet, so they are null."""
    limits = row.get("send_limits") or []
    first = limits[0] if limits else None
    list_id = first.get("list") if first else None
    trigger = row.get("trigger") or {}
    return {
        "id": row["id"],
        "name": row["name"],
        "workspace": row.get("workspace") or "",
        "mailbox": None,
        "address": None,
        "list": {"id": list_id, "name": names.get(list_id, list_id)} if list_id else None,
        "trigger": {key: trigger[key] for key in TRIGGER_KEYS if key in trigger},
        "steps": row.get("steps") or [],
        "run": None,
        "people": None,
        "next_at": row.get("next_at"),
        "limit": first,
        "follow_up": None,
    }


class Handlers:
    """The handlers of the routes below, sharing one Api (and what they keep between calls)."""

    def __init__(self, api: Api) -> None:
        self.api = api

    def wf(self) -> Any:
        return self.api.svc("workflows")

    async def list_names(self, request: Request) -> dict[str, str]:
        return {row["id"]: row["name"] for row in await read(request, "/api/workflows/lists")}

    async def waiting_for(self, request: Request) -> tuple[dict[str, Any], dict[str, str]]:
        review = await read(request, "/api/workflows/leads")
        waiting = {batch["list"]: batch["id"] for batch in review["batches"]}
        return review, waiting

    async def mailboxes(self, request: Request) -> list[dict[str, Any]]:
        service = self.wf()
        day = service.sends.day()
        out = []
        for row in await read(request, "/api/mail"):
            out.append(mailbox_of(self.api, row, service.sends.store.sent_on(row["id"], day), service.sends.limit_of(row["id"])))
        return out

    async def discover(self, request: Request) -> dict[str, Any]:
        body = await read_json_object(request)
        address = str(body.get("address") or "")
        try:
            found = await write(request, "POST", "/api/mail/discover", {"address": address})
        except Refused as refused:
            if refused.reply.status != 404:
                raise
            return discovery_of(self.api, address, None)
        return discovery_of(self.api, address, found)

    async def form_link(self, request: Request) -> dict[str, Any]:
        body = await read_json_object(request)
        # The server names a person typed go to the node, which shows them on its form to check. Nothing else does.
        typed = {key: body[key] for key in ("imap", "smtp") if isinstance(body.get(key), dict)}
        out = await write(request, "POST", "/api/mail/form-links", {"address": body.get("address"), "id": body.get("id"), **typed})
        return {"url": out["url"], "expires_at": out["expires_at"], "computer": self.api.node_name, "token": out["path"].rsplit("/", 1)[-1]}

    async def form_state(self, request: Request) -> dict[str, Any]:
        token = request.path_params["token"]
        out = await read(request, f"/api/mail/form/{token}/state")
        return {"state": out["state"], "expires_at": out.get("expires_at")}

    async def form_end(self, request: Request) -> dict[str, Any]:
        token = request.path_params["token"]
        out = await write(request, "DELETE", f"/api/mail/form/{token}")
        return {"state": out["state"]}

    async def messages(self, request: Request) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for box in await read(request, "/api/mail"):
            listed = await read(request, f"/api/mail/{tilde(box['id'])}/messages", query={"limit": 20})
            rows += [message_of(box["id"], row) for row in listed["messages"]]
        return sorted(rows, key=lambda row: row["at"], reverse=True)

    async def message(self, request: Request) -> dict[str, Any]:
        box_id, number = split_message(request.path_params["id"])
        whole = await read(request, f"/api/mail/{tilde(box_id)}/messages/{number}")
        return message_of(box_id, whole, whole.get("body") or "")

    async def mark(self, request: Request) -> Any:
        box_id, number = split_message(request.path_params["id"])
        body = await read_json_object(request)
        return await write(request, "POST", f"/api/mail/{tilde(box_id)}/messages/{number}/mark", {"read": body.get("read")})

    async def send(self, request: Request) -> Any:
        body = await read_json_object(request)
        reply_to = body.get("reply_to")
        number = None
        if reply_to:
            box_id, number = split_message(reply_to)
            if box_id != body.get("mailbox"):
                raise ApiError("invalid", "That message is not in this mailbox, so it cannot be replied to from it.")
        return await write(request, "POST", "/api/mail/send", {
            "mailbox": body.get("mailbox"), "to": body.get("to"), "subject": body.get("subject"),
            "body": body.get("body"), "reply_to_id": number,
        })

    async def batches(self, request: Request) -> list[dict[str, Any]]:
        review, _ = await self.waiting_for(request)
        names = await self.list_names(request)
        out = []
        for summary in review["batches"]:
            out.append(batch_of(await read(request, f"/api/workflows/leads/{summary['id']}"), names))
        return out

    async def batch(self, request: Request) -> dict[str, Any]:
        names = await self.list_names(request)
        return batch_of(await read(request, f"/api/workflows/leads/{request.path_params['id']}"), names)

    async def decide(self, request: Request) -> dict[str, Any]:
        body = await read_json_object(request)
        keep = body.get("keep")
        if not isinstance(keep, list) or not all(isinstance(address, str) for address in keep):
            raise ApiError("invalid", "keep is the list of addresses to take, which may be empty.")
        out = await write(request, "POST", f"/api/workflows/leads/{request.path_params['id']}", {"take": keep})
        return {"list": out["list"] if out["state"] in ("taken", "picked") else None}

    async def lists(self, request: Request) -> list[dict[str, Any]]:
        review, waiting = await self.waiting_for(request)
        first = review["note"] is not None
        return [list_of(row, waiting, first) for row in await read(request, "/api/workflows/lists")]

    async def one_list(self, request: Request) -> dict[str, Any]:
        review, waiting = await self.waiting_for(request)
        list_id = request.path_params["id"]
        row = next((item for item in await read(request, "/api/workflows/lists") if item["id"] == list_id), None)
        if row is None:
            raise ApiError("not_found", "There is no list with that name on this computer.")
        out = list_of(row, waiting, review["note"] is not None)
        people = await read(request, f"/api/workflows/lists/{list_id}/people")
        out["people"] = [person_of(member) for member in people["people"]]
        return out

    async def change_list(self, request: Request) -> Response:
        await read_json_object(request)
        raise ApiError("unavailable", "This computer cannot pause a list or change its pace yet. The daily limit is set "
                                      "for each mailbox, and the owner sets it.")

    async def routine(self, request: Request) -> dict[str, Any]:
        row = await read(request, f"/api/workflows/{request.path_params['id']}")
        return routine_of(row, await self.list_names(request))


def routes(api: Api) -> list[BaseRoute]:
    h = Handlers(api)

    return [
        org_route(api, "/email/mailboxes", h.mailboxes, ["GET"]),
        org_route(api, "/email/discover", h.discover, ["POST"]),
        org_route(api, "/email/form-links", h.form_link, ["POST"]),
        org_route(api, "/email/form-links/{token}/state", h.form_state, ["GET"]),
        org_route(api, "/email/form-links/{token}", h.form_end, ["DELETE"]),
        org_route(api, "/email/messages", h.messages, ["GET"]),
        org_route(api, "/email/messages/{id}", h.message, ["GET"]),
        org_route(api, "/email/messages/{id}/mark", h.mark, ["POST"]),
        org_route(api, "/email/send", h.send, ["POST"]),
        org_route(api, "/email/batches", h.batches, ["GET"]),
        org_route(api, "/email/batches/{id}", h.batch, ["GET"]),
        org_route(api, "/email/batches/{id}/decide", h.decide, ["POST"]),
        org_route(api, "/email/lists", h.lists, ["GET"]),
        org_route(api, "/email/lists/{id}", h.one_list, ["GET"]),
        org_route(api, "/email/lists/{id}", h.change_list, ["PATCH"]),
        org_route(api, "/email/routines/{id}", h.routine, ["GET"]),
    ]

