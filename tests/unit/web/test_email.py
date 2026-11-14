"""Email on a node, as the app reads it: each route answers what the node really knows, and a refusal
goes back as the node said it. No test here types a password: the form is the node's own page."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import pytest

from agentos.runner.mail import api as mail_api
from agentos.runner.mail import connect as connecting
from agentos.runner.workflows.mail import Mailbox, MessageGone, Received

TOKEN = "t" * 43
BOX = "biz-a/mail"
SENDER = "Ana Berzina <ana@example.com>"
LIST = "list.biz-a.customers"
PASSWORD_HINT = "password"


class FakeMail:
    """What the node would read and send, without a server: the same calls as the real transport."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, EmailMessage]] = []
        self.inbox: list[Received] = []
        self.marked: list[tuple[int, bool]] = []

    async def verify(self, box: Mailbox, password: str) -> None:
        return None

    async def send(self, box: Mailbox, password: str, message: EmailMessage, *, to: str) -> None:
        self.sent.append((to, message))

    async def file_sent(self, box: Mailbox, password: str, raw: bytes, message_id: str) -> bool:
        return True

    async def list_messages(self, box: Mailbox, password: str, *, unread: bool, since: Any, limit: int) -> list[Received]:
        rows = [m for m in self.inbox if not unread or not m.seen]
        return sorted(rows, key=lambda m: m.uid, reverse=True)[:limit]

    async def read_message(self, box: Mailbox, password: str, message_id: int) -> Received:
        found = next((m for m in self.inbox if m.uid == message_id), None)
        if found is None:
            raise MessageGone("that message left the mailbox")
        return found

    async def mark(self, box: Mailbox, password: str, message_id: int, read: bool) -> None:
        self.marked.append((message_id, read))


class Net:
    """Nothing is looked up: an address with no table row has no servers, as on an offline computer."""

    async def mx(self, domain: str) -> list[str]:
        return []

    async def srv(self, name: str) -> list[Any]:
        return []

    async def addresses(self, host: str) -> list[str]:
        return []

    async def fetch(self, url: str) -> bytes | None:
        return None

    async def answers_tls(self, host: str, port: int, security: str) -> bool:
        return False


def message(uid: int, *, seen: bool = False, subject: str = "Pricing") -> Received:
    return Received(uid=uid, sender=SENDER, subject=subject, body="Hello,\nWhat does a pallet cost?", message_id=f"<m{uid}@example.com>",
                    seen=seen, to="me@gmail.com", date="Thu, 05 Nov 2026 10:00:00 +0000",
                    arrived=datetime(2026, 11, 5, 10, tzinfo=UTC),
                    attachments=({"name": "price.pdf", "type": "application/pdf"},))


@pytest.fixture
def mail(node) -> FakeMail:
    fake = FakeMail()
    service = node.require("workflows")
    service.mail.transport = fake

    async def password(box: Mailbox) -> str:
        return PASSWORD_HINT

    service.mail._password = password
    return fake


@pytest.fixture
def box(node, mail) -> Mailbox:
    service = node.require("workflows")
    return service.mail.add({"id": BOX, "address": "me@gmail.com", "imap_host": "imap.gmail.com", "smtp_host": "smtp.gmail.com",
                             "username": "me@gmail.com", "secret_ref": "mail:biz-a/mail", "provider": "gmail"}, person=True)


@pytest.fixture(autouse=True)
def no_lookup(monkeypatch):
    monkeypatch.setattr(mail_api, "NET", Net)


def bearer(client) -> None:
    client.cookies.clear()
    client.headers["authorization"] = f"Bearer {TOKEN}"


def test_mailboxes_say_only_what_the_node_knows(client, box, node) -> None:
    [row] = client.get("/api/orgs/local/email/mailboxes").json()
    assert row["id"] == BOX
    assert row["address"] == "me@gmail.com"
    assert row["site"] == "gmail.com"
    assert row["provider"] == "Gmail"
    assert row["workspace"] == "biz-a"
    assert row["computer"] == "dev"
    assert row["health"] == "unchecked"
    assert row["checked_at"] is None and row["problem"] is None
    assert row["new_count"] is None and row["arrivals"] is None and row["week"] is None
    assert row["planned_today"] is None and row["routines"] is None
    assert row["sent_today"] == 0
    assert row["daily_limit"] == node.require("workflows").sends.limit_of(BOX)
    assert row["incoming"] == "imap.gmail.com:993"
    assert row["outgoing"] == "smtp.gmail.com:465"


def test_a_read_that_failed_says_so_in_words(client, box, node) -> None:
    node.require("workflows").mail.note_health(BOX, False, "The server closed the connection.")
    [row] = client.get("/api/orgs/local/email/mailboxes").json()
    assert row["health"] == "cannot_read"
    assert row["problem"]["body"] == "The server closed the connection."
    assert row["problem"]["next"]
    assert row["checked_at"]


def test_discovery_finds_a_known_provider_and_hands_out_no_password(client) -> None:
    answer = client.post("/api/orgs/local/email/discover", json={"address": "me@gmail.com"})
    assert answer.status_code == 200, answer.text
    found = answer.json()
    assert found["result"] == "found"
    assert found["provider"] == "Gmail"
    assert found["password"] == "app_password"
    assert found["incoming"] == "imap.gmail.com:993"
    assert found["computers"] == [{"id": "dev", "word": "This computer", "has_screen": True}]
    assert "passw" not in " ".join(found).replace("password", "")


def test_discovery_of_an_unknown_domain_says_not_found(client) -> None:
    answer = client.post("/api/orgs/local/email/discover", json={"address": "ana@nowhere.invalid"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["result"] == "not_found"
    # the names are typed in, and the form opens on this computer: the panel names it, and invents no other
    assert answer.json()["computers"] == [{"id": "dev", "word": "This computer", "has_screen": True}]


def test_discovery_refuses_a_bad_address(client) -> None:
    refused = client.post("/api/orgs/local/email/discover", json={"address": "not an address"})
    assert refused.status_code >= 400


def test_a_form_link_is_for_the_owner_and_holds_no_field(client, box) -> None:
    answer = client.post("/api/orgs/local/email/form-links", json={"address": "jane@gmail.com", "id": "biz-a/new"})
    assert answer.status_code == 200, answer.text
    link = answer.json()
    assert "/api/mail/form/" in link["url"]
    assert link["expires_at"]
    assert set(link) == {"url", "expires_at", "computer", "token"}
    assert link["url"].endswith(f"/api/mail/form/{link['token']}")


def test_a_token_gets_no_form_link(client) -> None:
    bearer(client)
    refused = client.post("/api/orgs/local/email/form-links", json={"address": "jane@gmail.com", "id": "biz-a/new"})
    assert refused.status_code == 403


def test_a_form_link_for_a_mailbox_that_exists_is_refused(client, box) -> None:
    refused = client.post("/api/orgs/local/email/form-links", json={"address": "me@gmail.com", "id": BOX})
    assert refused.status_code >= 400


TYPED = {"host": "imap.smallmail.example", "port": 993, "security": "tls"}


def test_the_names_a_person_typed_reach_the_node_form(client, box, node) -> None:
    answer = client.post("/api/orgs/local/email/form-links", json={
        "address": "sales@smallmail.example", "id": "biz-a/sales", "imap": TYPED,
        "smtp": {"host": "smtp.smallmail.example", "port": 465, "security": "tls"}})
    assert answer.status_code == 200, answer.text
    token = answer.json()["token"]
    job = connecting.jobs_for(node.require("workflows").mail).get(token)
    assert job is not None and job.found is not None
    assert (job.found.imap.host, job.found.imap.port, job.found.imap.typed) == ("imap.smallmail.example", 993, True)
    assert job.found.smtp.host == "smtp.smallmail.example"


def test_one_typed_name_with_nothing_found_is_refused(client) -> None:
    refused = client.post("/api/orgs/local/email/form-links", json={"address": "sales@smallmail.example", "id": "biz-a/sales", "imap": TYPED})
    assert refused.status_code == 400 and "both need typing in" in refused.json()["error"]["message"]


def test_the_panel_asks_whether_its_form_is_open_and_ends_it(client, box) -> None:
    answer = client.post("/api/orgs/local/email/form-links", json={"address": "jane@gmail.com", "id": "biz-a/new"})
    token = answer.json()["token"]
    assert client.get(f"/api/orgs/local/email/form-links/{token}/state").json()["state"] == "open"
    assert client.delete(f"/api/orgs/local/email/form-links/{token}").json() == {"state": "ended"}
    assert client.get(f"/api/orgs/local/email/form-links/{token}/state").json() == {"state": "ended", "expires_at": None}


def test_a_token_cannot_ask_or_end_a_form_link(client) -> None:
    answer = client.post("/api/orgs/local/email/form-links", json={"address": "jane@gmail.com", "id": "biz-a/new"})
    token = answer.json()["token"]
    bearer(client)
    assert client.get(f"/api/orgs/local/email/form-links/{token}/state").status_code == 403
    assert client.delete(f"/api/orgs/local/email/form-links/{token}").status_code == 403


def test_messages_list_newest_first_as_text(client, box, mail) -> None:
    mail.inbox.extend([message(3, seen=True, subject="Older"), message(7, subject="Newest")])
    rows = client.get("/api/orgs/local/email/messages").json()
    assert [row["subject"] for row in rows] == ["Newest", "Older"]
    first = rows[0]
    assert first["id"] == "biz-a~mail~7"
    assert first["mailbox"] == BOX
    assert first["from_name"] == "Ana Berzina"
    assert first["from_address"] == "ana@example.com"
    assert first["unread"] is True
    assert first["attachments"] == [{"name": "price.pdf", "type": "application/pdf"}]
    assert first["clip"].startswith("Hello")
    assert first["at"] == "2026-11-05T10:00:00Z"
    assert first["body"] == []
    assert rows[1]["unread"] is False


def test_one_message_carries_its_whole_text_as_one_text_block(client, box, mail) -> None:
    mail.inbox.append(message(7))
    whole = client.get("/api/orgs/local/email/messages/biz-a~mail~7").json()
    assert whole["body"] == [{"kind": "text", "text": "Hello,\nWhat does a pallet cost?"}]


def test_a_message_that_left_is_gone(client, box, mail) -> None:
    refused = client.get("/api/orgs/local/email/messages/biz-a~mail~99")
    assert refused.status_code == 404


def test_a_message_id_the_app_did_not_get_from_us_is_refused(client, box) -> None:
    refused = client.get("/api/orgs/local/email/messages/not-a-message")
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"


def test_marking_a_message_is_the_owners_and_reaches_the_server(client, box, mail) -> None:
    mail.inbox.append(message(7))
    answer = client.post("/api/orgs/local/email/messages/biz-a~mail~7/mark", json={"read": True})
    assert answer.status_code == 200, answer.text
    assert mail.marked == [(7, True)]


def test_a_token_cannot_mark_a_message(client, box, mail) -> None:
    mail.inbox.append(message(7))
    bearer(client)
    refused = client.post("/api/orgs/local/email/messages/biz-a~mail~7/mark", json={"read": True})
    assert refused.status_code == 403
    assert mail.marked == []


def test_a_send_goes_out_once_and_counts_against_the_day(client, box, mail) -> None:
    body = {"mailbox": BOX, "to": "bo@example.com", "subject": "Hello", "body": "Hi there"}
    answer = client.post("/api/orgs/local/email/send", json=body)
    assert answer.status_code == 200, answer.text
    assert [to for to, _ in mail.sent] == ["bo@example.com"]
    [row] = client.get("/api/orgs/local/email/mailboxes").json()
    assert row["sent_today"] == 1


def test_a_second_message_to_the_same_person_the_same_day_goes_through_a_list(client, box, mail) -> None:
    body = {"mailbox": BOX, "to": "cy@example.com", "subject": "Hello", "body": "Hi"}
    assert client.post("/api/orgs/local/email/send", json=body).status_code == 200
    refused = client.post("/api/orgs/local/email/send", json=body)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "use_a_list"
    assert len(mail.sent) == 1


def test_a_token_cannot_send(client, box, mail) -> None:
    bearer(client)
    body = {"mailbox": BOX, "to": "bo@example.com", "subject": "Hello", "body": "Hi"}
    assert client.post("/api/orgs/local/email/send", json=body).status_code == 403
    assert mail.sent == []


def test_a_reply_must_be_in_the_mailbox_it_is_sent_from(client, box, mail) -> None:
    refused = client.post("/api/orgs/local/email/send", json={"mailbox": "other/mail", "to": "bo@example.com", "subject": "Re",
                                                              "body": "Hi", "reply_to": "biz-a~mail~7"})
    assert refused.status_code == 400
    assert mail.sent == []


def test_a_list_nobody_made_has_no_batch_and_no_list(client) -> None:
    assert client.get("/api/orgs/local/email/batches").json() == []
    assert client.get("/api/orgs/local/email/lists").json() == []


def test_a_batch_that_is_not_here_is_not_found(client) -> None:
    assert client.get("/api/orgs/local/email/batches/batch_nope").status_code == 404


def test_deciding_a_batch_needs_a_list_of_addresses(client) -> None:
    refused = client.post("/api/orgs/local/email/batches/batch_nope/decide", json={"keep": "all"})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"


def test_a_list_can_be_read_by_its_id_and_not_found_otherwise(client, node) -> None:
    assert client.get("/api/orgs/local/email/lists/list.nope").status_code == 404


def test_a_list_cannot_be_paused_or_paced_on_this_node(client, node) -> None:
    refused = client.patch("/api/orgs/local/email/lists/list.nope", json={"state": "paused"})
    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "unavailable"


def test_a_routine_that_is_not_here_is_not_found(client) -> None:
    assert client.get("/api/orgs/local/email/routines/wf_nope").status_code == 404


def test_the_mailboxes_are_refused_without_a_session(client) -> None:
    client.cookies.clear()
    refused = client.get("/api/orgs/local/email/mailboxes")
    assert refused.status_code == 401


LIST_ID = "list.biz-a.customers"


def owner_actor():
    from agentos.runner.workflows.service import Actor

    return Actor.owner()


def agent_actor():
    from agentos.runner.workflows.service import Actor

    return Actor({"kind": "agent", "id": "claude-code"}, None, False, "agent:claude-code")


def leads_of(node) -> Any:
    """A list a person saved, and a batch an agent proposed to it: the node's own two steps."""
    service = node.require("workflows")
    service.sends.save_list(LIST_ID, workspace="biz-a", name="Customers", people=[{"email": "ana@example.com", "name": "Ana"}],
                            person=True, by="owner")
    proposed = service.leads.propose(LIST_ID, [
        {"email": "bo@acme.example", "name": "Bo Lind", "company": "Acme", "source": "https://www.linkedin.com/in/bo",
         "why": "Buys pallets"},
        {"email": "cy@north.example", "name": "Cy", "company": "North", "source": "Harbor directory", "why": "Listed"},
    ], agent_actor())
    assert proposed["received"] == 2
    return service


def test_a_waiting_batch_shows_its_leads_and_their_sources(client, node) -> None:
    leads_of(node)
    [batch] = client.get("/api/orgs/local/email/batches").json()
    assert batch["list"] == LIST_ID
    assert batch["list_name"] == "Customers"
    assert batch["workspace"] == "biz-a"
    assert batch["status"] == "waiting"
    assert batch["found"] == 2
    assert batch["mailbox"] is None
    assert {lead["address"] for lead in batch["leads"]} == {"bo@acme.example", "cy@north.example"}
    kinds = {lead["address"]: lead["source"] for lead in batch["leads"]}
    assert kinds == {"bo@acme.example": "linkedin", "cy@north.example": "directory"}
    linked = next(lead for lead in batch["leads"] if lead["address"] == "bo@acme.example")
    assert linked["source_url"] == "https://www.linkedin.com/in/bo"
    assert linked["company"] == "Acme" and linked["why"] == "Buys pallets"
    assert linked["role"] is None and linked["country"] is None and linked["look"] is None
    assert {row["key"] for row in batch["sources"]} == {"linkedin", "directory"}
    detail = client.get(f"/api/orgs/local/email/batches/{batch['id']}").json()
    assert detail["id"] == batch["id"]
    assert len(detail["leads"]) == 2


def test_taking_one_lead_puts_only_that_one_on_the_list(client, node) -> None:
    leads_of(node)
    [batch] = client.get("/api/orgs/local/email/batches").json()
    decided = client.post(f"/api/orgs/local/email/batches/{batch['id']}/decide", json={"keep": ["bo@acme.example"]})
    assert decided.status_code == 200, decided.text
    assert decided.json() == {"list": LIST_ID}
    [summary] = client.get("/api/orgs/local/email/lists").json()
    assert summary["size"] == 2
    assert summary["batch_waiting"] is None
    assert client.get("/api/orgs/local/email/batches").json() == []


def test_a_batch_cannot_be_decided_twice(client, node) -> None:
    leads_of(node)
    [batch] = client.get("/api/orgs/local/email/batches").json()
    client.post(f"/api/orgs/local/email/batches/{batch['id']}/decide", json={"keep": []})
    again = client.post(f"/api/orgs/local/email/batches/{batch['id']}/decide", json={"keep": []})
    assert again.status_code == 400


def test_a_batch_cannot_keep_an_address_that_is_not_in_it(client, node) -> None:
    leads_of(node)
    [batch] = client.get("/api/orgs/local/email/batches").json()
    refused = client.post(f"/api/orgs/local/email/batches/{batch['id']}/decide", json={"keep": ["stranger@x.example"]})
    assert refused.status_code == 400


def test_a_list_shows_its_size_and_its_people_to_the_owner(client, node) -> None:
    leads_of(node)
    [summary] = client.get("/api/orgs/local/email/lists").json()
    assert summary["name"] == "Customers"
    assert summary["size"] == 1
    assert summary["people"] is None and summary["state"] is None and summary["pace"] is None
    assert summary["first_list"] in (True, False)
    one = client.get(f"/api/orgs/local/email/lists/{LIST_ID}").json()
    assert [person["address"] for person in one["people"]] == ["ana@example.com"]


def test_a_routine_shows_its_trigger_and_its_steps(client, node) -> None:
    service = node.require("workflows")
    saved = service.save({"name": "Say hello", "workspace": "biz-a", "trigger": {"kind": "manual"}, "steps": []}, owner_actor())
    answer = client.get(f"/api/orgs/local/email/routines/{saved['id']}")
    assert answer.status_code == 200, answer.text
    routine = answer.json()
    assert routine["name"] == "Say hello"
    assert routine["workspace"] == "biz-a"
    assert routine["trigger"]["kind"] == "manual"
    assert routine["steps"] == []
    assert routine["list"] is None and routine["limit"] is None
    assert routine["run"] is None and routine["people"] is None and routine["follow_up"] is None


def test_who_is_on_a_list_is_for_the_owner_in_person_only(client, node) -> None:
    leads_of(node)
    bearer(client)
    assert client.get(f"/api/orgs/local/email/lists/{LIST_ID}").status_code == 403
    assert client.get(f"/api/workflows/lists/{LIST_ID}/people").status_code == 403


def test_a_token_cannot_read_a_whole_message(client, box, mail) -> None:
    mail.inbox.append(message(7))
    bearer(client)
    assert client.get("/api/orgs/local/email/messages/biz-a~mail~7").status_code == 403
