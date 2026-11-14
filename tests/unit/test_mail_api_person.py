"""The panel's own mailbox form on the node (B13): the server names a person typed are checked on the
form, the form says who opened it, and the panel can ask whether a form is still open and cancel it.
Only the owner in person may ask or cancel; a token may not."""

from __future__ import annotations

import pytest
from test_mail_api import BOX, Net, local
from test_node_panel import TOKEN, Node, bearer

from agentos.runner.mail import api as mail_api
from agentos.runner.mail import connect as connecting
from agentos.runner.mail.discovery import Found, Server

TYPED = {
    "imap": {"host": "imap.smallmail.example", "port": 993, "security": "tls"},
    "smtp": {"host": "smtp.smallmail.example", "port": 465, "security": "tls"},
}


@pytest.fixture
def node(tmp_path, monkeypatch):
    n = Node(tmp_path)
    monkeypatch.setattr(mail_api, "NET", Net)
    n.wf = n.runtime.require("workflows")
    yield n
    n.close()


def typed_link(client, csrf, address="sales@smallmail.example", **extra):
    return client.post("/api/mail/form-links", json={"address": address, "id": BOX, **TYPED, **extra}, headers=csrf)


def test_the_panel_form_shows_the_names_typed_and_says_the_owner_opened_it(node):
    with node.client() as client:
        csrf = node.sign_in(client)
        r = typed_link(client, csrf)
        assert r.status_code == 200, r.text
        link = r.json()
        assert link["found"]["imap"]["host"] == "imap.smallmail.example" and link["found"]["source"] == "typed"
        page = local(node).get(link["path"])
    assert page.status_code == 200
    assert "imap.smallmail.example:993" in page.text and "smtp.smallmail.example:465" in page.text
    assert "what you typed" in page.text
    assert "You opened this from your panel" in page.text and "An agent asked" not in page.text


def test_both_names_typed_look_nothing_up(node, monkeypatch):
    def no_lookup():
        raise AssertionError("nothing is looked up when both names are typed")

    monkeypatch.setattr(mail_api, "NET", no_lookup)
    with node.client() as client:
        csrf = node.sign_in(client)
        assert typed_link(client, csrf).status_code == 200


def test_one_name_typed_with_nothing_found_is_refused(node):
    with node.client() as client:
        csrf = node.sign_in(client)
        r = client.post("/api/mail/form-links", json={"address": "sales@smallmail.example", "id": BOX, "imap": TYPED["imap"]}, headers=csrf)
    assert r.status_code == 400 and "both need typing in" in r.json()["error"]["message"]


def test_the_owner_asks_whether_the_form_is_open_and_cancels_it(node):
    with node.client() as client:
        csrf = node.sign_in(client)
        token = typed_link(client, csrf).json()["path"].rsplit("/", 1)[1]
        assert client.get(f"/api/mail/form/{token}/state", headers=csrf).json()["state"] == "open"
        # a token holder may not see or end the form the owner opened
        assert client.get(f"/api/mail/form/{token}/state", headers=bearer(TOKEN)).status_code == 403
        assert client.delete(f"/api/mail/form/{token}", headers=bearer(TOKEN)).status_code == 403
        assert client.delete(f"/api/mail/form/{token}", headers=csrf).json() == {"state": "ended", "ended": True}
        assert client.get(f"/api/mail/form/{token}/state", headers=csrf).json() == {"state": "ended"}
        assert client.delete(f"/api/mail/form/{token}", headers=csrf).json() == {"state": "ended", "ended": False}
        ended = local(node).get(f"/api/mail/form/{token}")
    assert ended.status_code == 404 and "cancelled" in ended.text
    assert node.wf.mail.get(BOX) is None


def test_a_form_an_agent_asked_for_says_so(node):
    found = Found("me@gmail.com", Server("imap.example.net", 993, "tls", "me@gmail.com", typed=True),
                  Server("smtp.example.net", 465, "tls", "me@gmail.com", typed=True), "typed", "typed")
    token, _ = connecting.jobs_for(node.wf.mail).open("me@gmail.com", BOX, found, by="owner", asked_by="agent")
    page = local(node).get(f"/api/mail/form/{token}")
    assert page.status_code == 200
    assert "An agent asked to connect this mailbox" in page.text and "You opened this from your panel" not in page.text


def test_a_cancelled_job_is_gone_for_good(node):
    jobs = connecting.jobs_for(node.wf.mail)
    token, _ = jobs.open("me@gmail.com", BOX, None, by="owner")
    assert jobs.end(token) is True
    assert jobs.get(token) is None and jobs.end(token) is False
