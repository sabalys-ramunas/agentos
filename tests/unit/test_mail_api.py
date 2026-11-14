"""The owner's routes and the one-job page: who may send a password, and that it stays put."""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from starlette.testclient import TestClient
from test_node_panel import TOKEN, Node, bearer
from test_vault_support import make_vault, private_home  # noqa: F401

from agentos.core.config import NodeConfig  # noqa: F401
from agentos.runner.mail import api as mail_api
from agentos.runner.mail import connect as connecting
from agentos.runner.mail import tools as mail_tools
from agentos.runner.mail.discovery import Found, Server, Srv
from agentos.runner.workflows.mail import Mailbox, SignInFailed

PASSWORD = "the-very-secret-password"
BOX = "biz-a/inbox"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


class Verifier:
    def __init__(self):
        self.fail, self.calls = None, []

    async def verify(self, box, password):
        self.calls.append((box.imap_host, password))
        if self.fail:
            raise SignInFailed(*self.fail)


class Net:
    """Nothing real: only the provider table answers."""

    async def mx(self, domain):
        return []

    async def srv(self, name):
        return []

    async def addresses(self, host):
        return ["93.184.216.34"]

    async def fetch(self, url):
        return None

    async def answers_tls(self, host, port, security):
        return False


@pytest.fixture
def node(tmp_path, monkeypatch):
    n = Node(tmp_path)
    monkeypatch.setattr(mail_api, "NET", Net)
    n.wf = n.runtime.require("workflows")
    n.wf.mail.transport = Verifier()
    yield n
    n.close()


def local(node) -> TestClient:
    """A browser on this computer, as through the SSH tunnel."""
    return TestClient(node.app, base_url="http://localhost:7400", client=("127.0.0.1", 50000))


def body(**kw):
    return {"id": BOX, "address": "me@gmail.com", "password": PASSWORD, **kw}


def low_found() -> Found:
    return Found("a@example.net", Server("imap.example.net", 993, "tls", "a@example.net"),
                 Server("smtp.example.net", 465, "tls", "a@example.net"), "srv", "low")


def job(node, found=None, address="me@gmail.com"):
    from agentos.runner.mail.discovery import from_provider
    from agentos.runner.mail.providers import by_id

    token, _ = connecting.jobs_for(node.wf.mail).open(address, BOX, found or from_provider(address, by_id("gmail")), by="agent")
    return f"/api/mail/form/{token}", token


def post_form(client, url, **fields):
    """As a browser does: what the page showed goes back with what was typed."""
    page = client.get(url)
    found = re.search(r'name=shown value="([0-9a-f]+)"', page.text)
    if found and "shown" not in fields:
        fields["shown"] = found.group(1)
    return client.post(url, content=urlencode(fields), headers=FORM)


def discovered(client, csrf, address="me@gmail.com"):
    return client.post("/api/mail/discover", json={"address": address}, headers=csrf).json()


def everything_on_disk(node) -> bytes:
    return b"".join(p.read_bytes() for p in node.config.home.rglob("*") if p.is_file() and "vault" not in p.name)


# ---- POST /api/mail/connect: this node's own panel, nothing else ----

def test_the_node_token_is_refused_even_though_it_is_the_owners(node):
    with node.client() as client:
        r = client.post("/api/mail/connect", json=body(), headers=bearer(TOKEN))
    assert r.status_code == 403 and r.json()["error"]["code"] == "denied" and "own panel" in r.json()["error"]["message"]
    assert node.wf.mail.transport.calls == []


@pytest.mark.parametrize("kw", [{"ceiling": "commit"}, {"ceiling": "commit", "admin": True}])
def test_a_grant_is_refused_whatever_it_holds(node, kw):
    try:
        token = node.grant(**kw)
    except TypeError:
        pytest.skip("this policy has no such grant")
    with node.client() as client:
        assert client.post("/api/mail/connect", json=body(), headers=bearer(token)).status_code == 403
    assert node.wf.mail.transport.calls == []


def test_a_call_through_a_controller_has_no_session_and_no_way_in(node):
    """A controller reaches a node by the channel's tools, and holds a token at most."""
    request = SimpleNamespace(state=SimpleNamespace(auth=SimpleNamespace(
        session=None, via="bearer", access=SimpleNamespace(human=False, member=None, owner=True, trace=lambda *a, **k: None))))
    from agentos.api.errors import ApiError

    with pytest.raises(ApiError):
        mail_api.in_person(request, "mail.connect")


def test_a_member_is_refused_even_in_a_session(node):
    from agentos.api.errors import ApiError

    request = SimpleNamespace(state=SimpleNamespace(auth=SimpleNamespace(
        session=object(), via="cookie", access=SimpleNamespace(human=True, member=object(), owner=False, trace=lambda *a, **k: None))))
    with pytest.raises(ApiError):
        mail_api.in_person(request, "mail.connect")


def test_a_cookie_without_csrf_is_refused(node):
    with node.client() as client:
        node.sign_in(client)
        assert client.post("/api/mail/connect", json=body()).status_code == 403


def test_the_owner_at_the_node_connects_and_the_password_goes_nowhere_else(node, caplog):
    caplog.set_level(logging.DEBUG)
    with node.client() as client:
        csrf = node.sign_in(client)
        r = client.post("/api/mail/connect", json=body(digest=discovered(client, csrf)["digest"]), headers=csrf)
        assert r.status_code == 200, r.text
        assert r.json()["imap"]["host"] == "imap.gmail.com" and PASSWORD not in r.text
        listed = client.get("/api/mail", headers=csrf)
    assert BOX in listed.text and PASSWORD not in listed.text
    assert PASSWORD not in caplog.text
    assert PASSWORD.encode() not in everything_on_disk(node)


def test_a_failure_saves_nothing_and_says_which_in_plain_words(node, caplog):
    caplog.set_level(logging.DEBUG)
    node.wf.mail.transport.fail = ("imap", "sign_in")
    with node.client() as client:
        csrf = node.sign_in(client)
        r = client.post("/api/mail/connect", json=body(digest=discovered(client, csrf)["digest"]), headers=csrf)
    assert r.status_code == 400 and r.json()["error"]["code"] == "mail_connect_failed"
    assert "apppasswords" in r.text and PASSWORD not in r.text and PASSWORD not in caplog.text
    assert node.wf.mail.get(BOX) is None
    assert PASSWORD.encode() not in everything_on_disk(node)


def test_a_bad_body_never_echoes_a_value(node):
    with node.client() as client:
        csrf = node.sign_in(client)
        r = client.post("/api/mail/connect", json=body(address="not an address"), headers=csrf)
    assert r.status_code == 400 and PASSWORD not in r.text


def test_discovery_needs_no_password_and_says_what_it_found(node):
    with node.client() as client:
        csrf = node.sign_in(client)
        r = client.post("/api/mail/discover", json={"address": "me@gmail.com"}, headers=csrf)
    out = r.json()
    assert out["imap"]["host"] == "imap.gmail.com" and out["needs_app_password"] and out["help_url"]


def test_a_person_can_type_the_servers_on_the_panel(node):
    with node.client() as client:
        csrf = node.sign_in(client)
        r = client.post("/api/mail/connect", headers=csrf, json=body(
            address="a@example.net", imap={"host": "10.0.0.2", "port": 993, "security": "tls"},
            smtp={"host": "10.0.0.2", "port": 587, "security": "starttls"}))
    assert r.status_code == 200 and node.wf.mail.get(BOX).private_hosts == ["10.0.0.2"]


def test_removing_takes_the_id_with_a_tilde(node):
    with node.client() as client:
        csrf = node.sign_in(client)
        client.post("/api/mail/connect", json=body(digest=discovered(client, csrf)["digest"]), headers=csrf)
        assert client.delete("/api/mail/biz-a~inbox", headers=csrf).json() == {"removed": True}
    assert node.wf.mail.get(BOX) is None


def test_reading_through_the_api_needs_the_owner_in_person(node):
    with node.client() as client:
        r = client.get("/api/mail/biz-a~inbox/messages", headers=bearer(TOKEN))
    assert r.status_code == 403


# ---- the page ----

def agent_ctx(node, principal_id="agent-1"):
    async def authorize(*a, **k):
        return None

    return SimpleNamespace(runtime=node.runtime, principal=SimpleNamespace(id=principal_id, kind="agent", owner="owner"),
                           trace=lambda *a, **k: None, authorize=authorize)


def cards(node):
    return [a for a in node.runtime.require("approvals").list(state="pending") if "mailbox" in a.summary]


async def test_an_agent_gets_a_card_for_a_person_and_never_a_link(node):
    out = await mail_tools.mail_connect(agent_ctx(node), address="me@gmail.com", mailbox=BOX)
    said = str(out)
    assert "/api/mail/form" not in said and "url" not in out and "path" not in out and "tunnel" not in out
    assert out["status"] == "needs_a_person" and "person" in out["next"]
    assert connecting.jobs_for(node.wf.mail)._jobs == {}


async def test_the_card_names_the_address_and_gives_one_line_and_no_link(node):
    await mail_tools.mail_connect(agent_ctx(node), address="me@gmail.com", mailbox=BOX)
    card, = cards(node)
    assert "me@gmail.com" in card.summary and f"agentos mail connect me@gmail.com --id {BOX}" in card.summary
    assert "http" not in card.summary and "/api/mail/form" not in card.summary and "—" not in card.summary
    public = str(card.public())
    assert "/api/mail/form" not in public and "token" not in card.summary


async def test_an_address_cannot_put_a_command_on_the_line_a_person_runs(node):
    await mail_tools.mail_connect(agent_ctx(node), address="`reboot`$x@example.net", mailbox=BOX)
    card, = cards(node)
    assert "'`reboot`$x@example.net'" in card.summary


async def test_an_agent_cannot_name_a_mailbox_that_exists(node):
    with node.client() as client:
        csrf = node.sign_in(client)
        client.post("/api/mail/connect", json=body(digest=discovered(client, csrf)["digest"]), headers=csrf)
    with pytest.raises(connecting.MailboxTaken) as e:
        await mail_tools.mail_connect(agent_ctx(node), address="evil@example.net", mailbox=BOX)
    assert "person" in e.value.message and cards(node) == []


async def test_asking_twice_makes_one_card(node):
    one = await mail_tools.mail_connect(agent_ctx(node), address="me@gmail.com", mailbox=BOX)
    two = await mail_tools.mail_connect(agent_ctx(node), address="me@gmail.com", mailbox=BOX)
    assert one["approval_id"] == two["approval_id"] and len(cards(node)) == 1


async def card_id(node, address="me@gmail.com"):
    return (await mail_tools.mail_connect(agent_ctx(node), address=address, mailbox=BOX))["approval_id"]


async def test_only_the_owner_at_the_node_gets_the_link_from_a_card(node):
    ap = await card_id(node)
    with node.client() as client:
        r = client.post(f"/api/mail/requests/{ap}/link", headers=bearer(TOKEN))
    assert r.status_code == 403 and "/api/mail/form" not in r.text


async def test_nobody_gets_a_link_with_no_session_even_from_this_computer(node):
    ap = await card_id(node)
    with local(node) as client:
        assert client.post(f"/api/mail/requests/{ap}/link").status_code in (401, 403)
        assert client.get("/api/mail/requests").status_code in (401, 403)


async def test_the_owner_opens_the_card_connects_and_the_card_closes(node):
    ap = await card_id(node)
    with node.client() as client:
        csrf = node.sign_in(client)
        waiting = client.get("/api/mail/requests", headers=csrf).json()["requests"]
        assert waiting == [{"id": ap, "address": "me@gmail.com", "mailbox": BOX}]
        link = client.post(f"/api/mail/requests/{ap}/link", headers=csrf).json()
        assert link["path"].startswith("/api/mail/form/") and "ssh -N -L" in link["tunnel"]
        assert post_form(local(node), link["path"], password=PASSWORD).status_code == 200
    assert node.wf.mail.get(BOX).address == "me@gmail.com" and cards(node) == []


async def test_a_card_that_was_answered_gives_no_link(node):
    ap = await card_id(node)
    node.runtime.require("approvals").decide(ap, "deny", by="owner", note="no")
    with node.client() as client:
        csrf = node.sign_in(client)
        assert client.post(f"/api/mail/requests/{ap}/link", headers=csrf).status_code == 404


async def test_a_full_set_of_jobs_is_said_before_any_discovery(node, monkeypatch):
    ap = await card_id(node)
    jobs = connecting.jobs_for(node.wf.mail)
    for i in range(connecting.MAX_JOBS):
        jobs.open("a@example.net", f"biz-a/m{i}", None, by="owner")
    asked = []
    monkeypatch.setattr(mail_api, "NET", lambda: asked.append(1) or Net())
    with node.client() as client:
        csrf = node.sign_in(client)
        r = client.post(f"/api/mail/requests/{ap}/link", headers=csrf)
    assert r.status_code != 200 and asked == []


async def test_a_mailbox_connected_at_the_cli_closes_its_card_at_the_next_poll(node):
    ap = await card_id(node)
    box = Mailbox(id=BOX, address="me@gmail.com", imap_host="imap.gmail.com", smtp_host="smtp.gmail.com", username="me@gmail.com", secret_ref="r")
    node.wf.mail.add(box.model_dump(), person=True)

    async def handle(_):
        return 0

    await node.wf.mail.poll_due(box, handle, approvals=node.runtime.require("approvals"))
    assert node.runtime.require("approvals").get(ap).state != "pending"


# ---- nothing but a person changes a mailbox ----

def connected(node, client):
    csrf = node.sign_in(client)
    assert client.post("/api/mail/connect", json=body(digest=discovered(client, csrf)["digest"]), headers=csrf).status_code == 200
    node.wf.mail.transport.calls.clear()
    return csrf


def test_a_token_for_the_form_cannot_replace_a_mailbox(node):
    """The attack as it was: a job for an id that exists, its own server, `confirm=yes`, any password."""
    with node.client() as client:
        connected(node, client)
        before = node.wf.mail.get(BOX).model_dump()
        url, _ = job(node, low_found(), "a@example.net")
        r = post_form(local(node), url, password="any", confirm="yes", imap_host="evil.example", imap_port="993", imap_security="tls",
                      smtp_host="evil.example", smtp_port="465", smtp_security="tls")
    assert r.status_code == 400 and node.wf.mail.get(BOX).model_dump() == before and node.wf.mail.transport.calls == []


def test_a_token_for_the_form_cannot_change_the_hosts_of_the_same_address(node):
    with node.client() as client:
        connected(node, client)
        before = node.wf.mail.get(BOX).model_dump()
        url, _ = job(node)
        r = post_form(local(node), url, password="any", imap_host="evil.example", imap_port="993", imap_security="tls")
    assert r.status_code == 400 and node.wf.mail.get(BOX).model_dump() == before and node.wf.mail.transport.calls == []


def test_a_mailbox_made_while_a_job_waited_is_not_taken_over(node):
    url, _ = job(node)
    with node.client() as client:
        connected(node, client)
        assert post_form(local(node), url, password="any").status_code == 400
    assert node.wf.mail.transport.calls == []


def test_the_owner_at_the_node_can_change_a_mailbox_in_person(node):
    with node.client() as client:
        csrf = connected(node, client)
        r = client.post("/api/mail/connect", headers=csrf, json=body(imap={"host": "10.0.0.2", "port": 993, "security": "tls"},
                                                                     smtp={"host": "10.0.0.2", "port": 465, "security": "tls"}))
    assert r.status_code == 200 and node.wf.mail.get(BOX).imap_host == "10.0.0.2"


# ---- what was shown is what is used ----

class Shifting(Net):
    """Says one thing to the person, and another a moment later."""

    calls = 0

    async def srv(self, name):
        type(self).calls += 1
        host = "mail.first.example" if type(self).calls <= 3 else "mail.hostile.example"
        return [Srv(host, 993)] if "imaps" in name or "submissions" in name else []


def test_the_digest_says_exactly_what_was_shown(node):
    with node.client() as client:
        csrf = node.sign_in(client)
        out = discovered(client, csrf)
    assert re.fullmatch(r"[0-9a-f]{32,64}", out["digest"])


def test_connect_uses_the_hosts_shown_and_never_looks_again(node, monkeypatch):
    monkeypatch.setattr(mail_api, "NET", Shifting)
    Shifting.calls = 0
    with node.client() as client:
        csrf = node.sign_in(client)
        shown = discovered(client, csrf, "a@example.net")
        assert shown["imap"]["host"] == "mail.first.example" and shown["needs_yes"]
        made = Shifting.calls
        r = client.post("/api/mail/connect", headers=csrf,
                        json=body(address="a@example.net", digest=shown["digest"], confirmed=True))
    assert r.status_code == 200, r.text
    assert Shifting.calls == made and node.wf.mail.get(BOX).imap_host == "mail.first.example"


def test_a_yes_with_no_digest_covers_nothing(node, monkeypatch):
    monkeypatch.setattr(mail_api, "NET", Shifting)
    with node.client() as client:
        csrf = node.sign_in(client)
        r = client.post("/api/mail/connect", headers=csrf, json=body(address="a@example.net", confirmed=True))
    assert r.status_code == 400 and r.json()["error"]["code"] == "mail_confirm_hosts" and node.wf.mail.transport.calls == []


@pytest.mark.parametrize("digest", ["0" * 64, "", "nonsense", 5, None])
def test_a_digest_nobody_was_shown_is_refused(node, digest):
    with node.client() as client:
        csrf = node.sign_in(client)
        r = client.post("/api/mail/connect", headers=csrf, json=body(digest=digest, confirmed=True))
    assert r.status_code == 400 and r.json()["error"]["code"] == "mail_confirm_hosts" and node.wf.mail.transport.calls == []


def test_a_digest_for_one_address_does_not_cover_another(node):
    with node.client() as client:
        csrf = node.sign_in(client)
        shown = discovered(client, csrf, "me@gmail.com")
        r = client.post("/api/mail/connect", headers=csrf, json=body(address="you@gmail.com", digest=shown["digest"]))
    assert r.status_code == 400 and node.wf.mail.transport.calls == []


def test_a_low_trust_result_still_needs_the_yes_with_its_digest(node, monkeypatch):
    monkeypatch.setattr(mail_api, "NET", Shifting)
    Shifting.calls = 0
    with node.client() as client:
        csrf = node.sign_in(client)
        shown = discovered(client, csrf, "a@example.net")
        r = client.post("/api/mail/connect", headers=csrf, json=body(address="a@example.net", digest=shown["digest"]))
    assert r.status_code == 400 and r.json()["error"]["code"] == "mail_confirm_hosts"


def test_an_https_result_is_bound_to_what_was_shown_too(node):
    found = Found("a@example.net", Server("imap.example.net", 993, "tls", "a@example.net"),
                  Server("smtp.example.net", 465, "tls", "a@example.net"), "autoconfig", "https")
    url, _ = job(node, found, "a@example.net")
    with local(node) as client:
        page = client.get(url)
        assert "imap.example.net:993" in page.text and f'name=shown value="{found.digest()}"' in page.text
        assert post_form(client, url, password=PASSWORD, shown="0" * 64).status_code == 400
        assert node.wf.mail.transport.calls == []
        assert post_form(client, url, password=PASSWORD).status_code == 200


def test_a_page_whose_hosts_were_not_shown_does_not_connect(node):
    url, _ = job(node)
    with local(node) as client:
        r = client.post(url, content=urlencode({"password": PASSWORD}), headers=FORM)
    assert r.status_code == 400 and node.wf.mail.transport.calls == []


def test_the_page_says_what_it_is_and_holds_nothing_active(node):
    url, _ = job(node)
    with local(node) as client:
        r = client.get(url)
    text = r.text
    assert "imap.gmail.com:993" in text and "smtp.gmail.com:465" in text and "App password" in text
    assert "your own agentos node" in text and "it does not reach the cloud" in text
    assert "myaccount.google.com/apppasswords" in text and 'rel="noreferrer"' in text
    assert "<script" not in text and "src=" not in text and "—" not in text
    assert "default-src 'none'" in r.headers["content-security-policy"] and r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["x-frame-options"] == "DENY"


def test_the_page_works_once(node):
    url, _ = job(node)
    with local(node) as client:
        ok = post_form(client, url, password=PASSWORD)
        assert ok.status_code == 200 and "Connected" in ok.text and PASSWORD not in ok.text
        assert client.get(url).status_code == 404
        assert post_form(client, url, password=PASSWORD).status_code == 404
    assert len(node.wf.mail.transport.calls) == 1 and node.wf.mail.transport.calls[0][1] == PASSWORD


def test_a_wrong_password_comes_back_without_the_password_in_the_page(node, caplog):
    caplog.set_level(logging.DEBUG)
    url, _ = job(node)
    node.wf.mail.transport.fail = ("imap", "sign_in")
    with local(node) as client:
        r = post_form(client, url, password=PASSWORD)
    assert r.status_code == 400 and "app password" in r.text and "tries left" in r.text
    assert PASSWORD not in r.text and PASSWORD not in caplog.text and node.wf.mail.get(BOX) is None
    assert PASSWORD.encode() not in everything_on_disk(node)


def test_a_low_trust_result_needs_the_box_ticked(node):
    url, _ = job(node, low_found(), "a@example.net")
    with local(node) as client:
        page = client.get(url)
        assert "These are my mail servers" in page.text and "Only DNS or a guess" in page.text
        refused = post_form(client, url, password=PASSWORD)
        assert refused.status_code == 400 and node.wf.mail.transport.calls == []
        assert post_form(client, url, password=PASSWORD, confirm="yes").status_code == 200


def test_servers_typed_on_the_page_are_used_as_typed(node):
    url, _ = job(node, low_found(), "a@example.net")
    with local(node) as client:
        r = post_form(client, url, password=PASSWORD, imap_host="10.9.9.9", imap_port="993", imap_security="tls",
                      smtp_host="10.9.9.9", smtp_port="587", smtp_security="starttls")
    assert r.status_code == 200 and node.wf.mail.transport.calls[0][0] == "10.9.9.9"


def test_no_tls_cannot_be_chosen_on_the_page(node):
    url, _ = job(node, low_found(), "a@example.net")
    with local(node) as client:
        r = post_form(client, url, password=PASSWORD, confirm="yes", imap_host="m.example.net", imap_port="143", imap_security="plain")
    assert r.status_code == 400 and node.wf.mail.transport.calls == []


def test_a_browser_elsewhere_over_plain_http_gets_no_form(node):
    url, _ = job(node)
    with node.client() as client:  # not this computer, not https
        page = client.get(url)
        assert page.status_code == 403 and "<form" not in page.text
        assert post_form(client, url, password=PASSWORD).status_code == 403
    assert node.wf.mail.transport.calls == []


def test_https_is_fine_from_anywhere(node):
    url, _ = job(node)
    with TestClient(node.app, base_url="https://localhost:7400") as client:
        assert "<form" in client.get(url).text


def test_a_post_from_another_site_is_refused(node):
    url, _ = job(node)
    with local(node) as client:
        r = client.post(url, content=urlencode({"password": PASSWORD}), headers={**FORM, "Origin": "https://evil.example"})
    assert r.status_code == 403 and node.wf.mail.transport.calls == []


def test_a_token_nobody_issued_gets_the_same_page(node):
    with local(node) as client:
        assert client.get("/api/mail/form/made-up-token").status_code == 404


def test_what_the_page_shows_is_escaped(node):
    server = Server("imap.example.net", 993, "tls", "a@example.net")
    found = Found("a@example.net", server, server, "ispdb", "https", words='<script>alert("x")</script>')
    url, _ = job(node, found, "a@example.net")
    with local(node) as client:
        assert "<script>alert" not in client.get(url).text
