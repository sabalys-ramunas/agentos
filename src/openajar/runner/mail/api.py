"""Mailboxes on the node's API (docs/EMAIL.md, "Owner API, CLI and tools").

    POST   /api/mail/discover        {address}: what was found, nothing secret, and its `digest`
    POST   /api/mail/connect         {id, address, password, digest?, imap?, smtp?, confirmed?}: node only
    GET    /api/mail/requests        what agents asked to connect: node only
    POST   /api/mail/requests/{id}/link   the link for one card: node only
    GET    /api/mail                 the mailboxes
    DELETE /api/mail/{id}            removes it and its password (`biz-a~mail` for `biz-a/mail`)
    GET    /api/mail/{id}/messages   ?unread&since&limit
    GET    /api/mail/form/{token}    the page for one job; POST sends it
    POST   /api/mail/form-links      {address, id, imap?, smtp?}: the link to the form for a mailbox typed in the
                                     panel; the servers a person typed are shown on it to check (additive)
    GET    /api/mail/form/{token}/state   open or ended, for the panel that waits on it: in person (additive)
    DELETE /api/mail/form/{token}    the owner cancels the form: in person (additive)
    POST   /api/mail/send            {mailbox, to, subject, body, reply_to_id?}: one message, in person
    GET    /api/mail/{id}/messages/{uid}   one message, whole, as text: in person
    POST   /api/mail/{id}/messages/{uid}/mark   {read}: in person

`/api/mail/connect` takes a password, so it answers only the owner signed in to this node's own
panel. A bearer token is refused, whoever holds it: an agent, a controller, an admin grant. A
controller reaches a node through the channel and has no way to this route, and a member's call
never reaches it. It uses the hosts a `digest` from `/discover` stands for, or the two a person
typed, and never looks anything up itself. The link for a card is the same: only that owner gets it.
The form's token is its own auth, and that page is served only over https or on this computer, and
it can add a mailbox but never change one. The body is read by hand into a `Secret`, so no error can
echo a value, and nothing here traces, logs or returns it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from openajar.api.app import Api, access_of, endpoint, read_json_object
from openajar.api.errors import ApiError
from openajar.core.errors import InvalidRequest, OpenajarError
from openajar.core.models import LANE_ID_RE, Effect
from openajar.runner.mail import connect as connecting
from openajar.runner.mail import form, outbox
from openajar.runner.mail import tools as mail_tools
from openajar.runner.mail.discovery import discover
from openajar.runner.mail.net import RealNet
from openajar.vault.secret import Secret

NOT_HERE = ("A mailbox password goes straight to the node: sign in to this node's own panel, or run `openajar mail connect` "
            "on it. A token or a controller cannot send one.")
NOT_SHOWN = "Those are not the servers you were shown. Look them up again, and say yes to what you see."
IN_PERSON = "Reading a mailbox here needs the owner signed in to the panel. An agent uses mail_list, which labels what it read."
NET = RealNet


def mailbox_id(request: Request) -> str:
    return request.path_params["id"].replace("~", "/", 1)


def in_person(request: Request, action: str, words: str = NOT_HERE) -> None:
    """The owner, signed in, at this node's own panel: a session, never a token, never a member."""
    auth = request.state.auth
    access = auth.access
    if auth.session is None or auth.via != "cookie" or not access.human or access.member is not None or not access.owner:
        access.trace(action, status="denied", summary="this needs the owner in person")
        raise ApiError("denied", words)


def _server(body: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = body.get(key)
    if value is not None and not isinstance(value, dict):
        raise InvalidRequest(f"{key} is an object: host, port, security, username.")
    return value


def routes(api: Api) -> list[Route]:
    return _MailRoutes(api).routes()


class _MailRoutes:
    """The mail routes: each handler is a method, `api` the panel's API they are served under."""

    def __init__(self, api: Api) -> None:
        self.api = api

    def mail(self) -> Any:
        return self.api.svc("workflows").mail

    def approvals(self) -> Any:
        return self.api.runtime.get("approvals")  # the store itself: `api.svc` gives the panel's view of it

    async def mail_discover(self, request: Request) -> dict:
        body = await read_json_object(request)
        found = await discover(body.get("address"), NET())
        connecting.jobs_for(self.mail()).show(found)
        return found.public()

    async def mail_connect(self, request: Request) -> Any:
        in_person(request, "mail.connect")
        body = await read_json_object(request)
        password = body.pop("password", None)
        if not isinstance(password, str) or not password:
            raise InvalidRequest("A password is needed.")
        secret = Secret(password)
        del password
        address = connecting.discovery.split_address(body.get("address"))[0]
        imap, smtp = _server(body, "imap"), _server(body, "smtp")
        found = connecting.jobs_for(self.mail()).shown(body.get("digest"), address)
        if found is None and not (imap and smtp):
            raise connecting.MailConfirmHosts(NOT_SHOWN)
        found = connecting.apply_typed(found, address, imap=imap, smtp=smtp)
        box = await connecting.connect(self.mail(), found, secret, mailbox_id=str(body.get("id") or ""),
                                       confirmed=body.get("confirmed") is True, in_person=True)
        connecting.close_requests(self.mail(), self.approvals(), box.id)
        access_of(request).trace("mail.connect", lane=box.id, summary=f"Connect mailbox {box.id}")
        return self.mail().view(box)

    async def requests_list(self, request: Request) -> dict:
        in_person(request, "mail.requests")
        return {"requests": connecting.waiting(self.mail(), self.approvals())}

    async def request_link(self, request: Request) -> dict:
        """The link exists only here, for the owner at this node, and only while the card is open."""
        in_person(request, "mail.request.link")
        ask = connecting.request(self.mail(), self.approvals(), request.path_params["id"])
        jobs = connecting.jobs_for(self.mail())
        jobs.room()
        found = await connecting.discover_quietly(ask["address"], NET())
        token, job = jobs.open(ask["address"], ask["mailbox"], found, by="owner", asked_by="agent")
        access_of(request).trace("mail.request.link", lane=ask["mailbox"], summary=f"Open the form for mailbox {ask['mailbox']}")
        return {**connecting.link_for(self.api.runtime, token, job), "found": found.public() if found else None}

    async def mail_list(self, request: Request) -> list[dict]:
        return [self.mail().view(b) for b in self.mail().list()]

    async def mail_remove(self, request: Request) -> dict:
        in_person(request, "mail.remove", "Only the owner signed in to the panel removes a mailbox.")
        box_id = mailbox_id(request)
        access_of(request).trace("mail.remove", lane=box_id, summary=f"Remove mailbox {box_id}")
        return {"removed": await self.mail().remove(box_id, person=True)}

    async def mail_messages(self, request: Request) -> dict:
        in_person(request, "mail.messages", IN_PERSON)
        box = self.mail().require(mailbox_id(request))
        q = request.query_params
        with mail_tools.reading(box):
            rows = await self.mail().list_messages(box, unread=q.get("unread") in ("1", "true"), since=mail_tools.since_time(q.get("since")),
                                              limit=int(q.get("limit") or 20) if (q.get("limit") or "20").isdigit() else 20)
        return {"mailbox": box.id, "messages": [mail_tools.row(m) for m in rows]}

    async def form_get(self, request: Request) -> Response:
        job = connecting.jobs_for(self.mail()).get(request.path_params["token"])
        if job is None:
            return form.ended()
        return form.render(job) if form.direct(request) else form.not_direct()

    async def form_post(self, request: Request) -> Response:
        token = request.path_params["token"]
        jobs = connecting.jobs_for(self.mail())
        job = jobs.get(token)
        if job is None:
            return form.ended()
        if not form.direct(request) or not form.same_origin(request):
            return form.not_direct()
        fields = form.read_form(await request.body())
        raw = fields.pop("password", "")
        known = job.found
        try:
            if not raw:
                raise InvalidRequest("A password is needed.")
            box = await jobs.run(token, Secret(raw), shown=fields.get("shown"),
                                 imap=form.typed_fields(fields, "imap", known.imap if known else None),
                                 smtp=form.typed_fields(fields, "smtp", known.smtp if known else None),
                                 confirmed=fields.get("confirm") == "yes")
        except connecting.MailJobEnded:
            return form.ended()
        except OpenajarError as e:
            alive = jobs.get(token)
            return form.render(alive, error=e.message, status=400) if alive is not None else form.ended()
        connecting.close_requests(self.mail(), self.approvals(), box.id)
        return form.done(box.address, box.id)

    def wf(self) -> Any:
        return self.api.svc("workflows")

    async def form_link(self, request: Request) -> dict:
        """A link to the form, for a mailbox the person typed in the panel. The panel gets the link and
        never the field: the password is typed on this computer, in the form the link opens."""
        in_person(request, "mail.form.link")
        body = await read_json_object(request)
        address = connecting.discovery.split_address(body.get("address"))[0]
        box_id = str(body.get("id") or "")
        if not LANE_ID_RE.match(box_id):
            raise InvalidRequest("A mailbox id looks like a lane id, 'harbor/mail'.")
        if self.mail().get(box_id) is not None:
            raise connecting.taken(box_id)
        # The server names a person typed in the panel (additive): they go over what was found, and the form
        # shows them to check. With both typed, nothing is looked up.
        imap, smtp = _server(body, "imap"), _server(body, "smtp")
        jobs = connecting.jobs_for(self.mail())
        jobs.room()
        found = None if imap and smtp else await connecting.discover_quietly(address, NET())
        if imap or smtp:
            found = connecting.apply_typed(found, address, imap=imap, smtp=smtp)
        token, job = jobs.open(address, box_id, found, by="owner")
        access_of(request).trace("mail.form.link", lane=box_id, summary=f"Open the form for mailbox {box_id}")
        return {**connecting.link_for(self.api.runtime, token, job), "found": found.public() if found else None}

    async def form_state(self, request: Request) -> dict:
        """Whether a form the owner opened is still open, for the panel that waits on it (additive). A link
        that ended and one that never was look the same, as on the page itself."""
        in_person(request, "mail.form.state")
        job = connecting.jobs_for(self.mail()).get(request.path_params["token"])
        if job is None:
            return {"state": "ended"}
        return {"state": "open", "expires_at": datetime.fromtimestamp(job.expires_at, UTC).isoformat()}

    async def form_end(self, request: Request) -> dict:
        """The owner cancels a form they opened: the link ends now, as one that ran out (additive)."""
        in_person(request, "mail.form.end")
        ended = connecting.jobs_for(self.mail()).end(request.path_params["token"])
        access_of(request).trace("mail.form.end", summary="Cancel a mailbox form")
        return {"state": "ended", "ended": ended}

    async def mail_send(self, request: Request) -> dict:
        """One message from a mailbox, in person. It is the agent's `mail_send`, with the same checks and
        the same ledger; the one difference is who asks, and a person's yes is the authorization here."""
        in_person(request, "mail.send", "Only the owner signed in to the panel sends mail from a mailbox.")
        body = await read_json_object(request)
        box = self.mail().require(str(body.get("mailbox") or ""))
        subject, text = body.get("subject"), body.get("body")
        if not isinstance(subject, str) or not isinstance(text, str):
            raise InvalidRequest("subject and body are text.")
        address = outbox.recipient(body.get("to"))
        reply = body.get("reply_to_id")
        number = mail_tools.message_number(reply) if reply not in (None, "") else None
        access = access_of(request)
        await access.authorize(Effect.COMMIT, action="mail.send", lane_id=box.id, summary=f"Send an email from {box.id}",
                               content={"mailbox": box.id, "to": address, "subject": subject, "body": text, "reply_to": number})
        outbox.check(self.wf(), box, address, reply=number is not None)
        parent = None
        if number is not None:
            with mail_tools.reading(box):
                parent = await self.mail().read_message(box, number)
        return await outbox.send_one(self.wf(), box, address, subject, text, parent=parent)

    async def mail_message(self, request: Request) -> dict:
        """One message, whole, as text: HTML is reduced on the node and nothing in it is fetched."""
        in_person(request, "mail.message", IN_PERSON)
        box = self.mail().require(mailbox_id(request))
        with mail_tools.reading(box):
            message = await self.mail().read_message(box, mail_tools.message_number(request.path_params["uid"]))
        return mail_tools.whole(message)

    async def mail_mark(self, request: Request) -> dict:
        in_person(request, "mail.mark", IN_PERSON)
        box = self.mail().require(mailbox_id(request))
        number = mail_tools.message_number(request.path_params["uid"])
        body = await read_json_object(request)
        if not isinstance(body.get("read"), bool):
            raise InvalidRequest("read is true or false.")
        await access_of(request).authorize(Effect.DRAFT, action="mail.mark", lane_id=box.id, summary=f"Mark a message in {box.id}")
        with mail_tools.reading(box):
            await self.mail().mark(box, number, body["read"])
        access_of(request).trace("mail.mark", lane=box.id, summary=f"Mark message {number} as {'read' if body['read'] else 'unread'}")
        return {"mailbox": box.id, "id": str(number), "read": body["read"]}

    def e(self, fn: Any, **kw: Any) -> Any:
        return endpoint(self.api, fn, **kw)

    def routes(self) -> list[Route]:
        e = self.e
        base = "/api/mail"
        return [
            Route(f"{base}/discover", e(self.mail_discover, owner=True, action="mail.discover"), methods=["POST"]),
            Route(f"{base}/connect", e(self.mail_connect, owner=True, action="mail.connect"), methods=["POST"]),
            Route(f"{base}/requests", e(self.requests_list, owner=True, action="mail.requests"), methods=["GET"]),
            Route(f"{base}/requests/{{id}}/link", e(self.request_link, owner=True, action="mail.request.link"), methods=["POST"]),
            Route(f"{base}/form/{{token}}", e(self.form_get, public=True, action="mail.form"), methods=["GET"]),
            Route(f"{base}/form/{{token}}", e(self.form_post, public=True, action="mail.form.send"), methods=["POST"]),
            Route(f"{base}/form/{{token}}", e(self.form_end, owner=True, action="mail.form.end"), methods=["DELETE"]),
            Route(f"{base}/form/{{token}}/state", e(self.form_state, owner=True, action="mail.form.state"), methods=["GET"]),
            Route(base, e(self.mail_list, owner=True, action="mail.list"), methods=["GET"]),
            Route(f"{base}/{{id}}", e(self.mail_remove, owner=True, action="mail.remove"), methods=["DELETE"]),
            Route(f"{base}/{{id}}/messages", e(self.mail_messages, owner=True, action="mail.messages"), methods=["GET"]),
            Route(f"{base}/{{id}}/messages/{{uid}}", e(self.mail_message, owner=True, action="mail.message"), methods=["GET"]),
            Route(f"{base}/{{id}}/messages/{{uid}}/mark", e(self.mail_mark, owner=True, action="mail.mark"), methods=["POST"]),
            Route(f"{base}/form-links", e(self.form_link, owner=True, action="mail.form.link"), methods=["POST"]),
            Route(f"{base}/send", e(self.mail_send, owner=True, action="mail.send"), methods=["POST"]),
        ]
