"""The node's webhooks: what is recorded, what is sent, how it is signed, when it is tried again,
and what is refused. Nothing touches the network: names come from a fake resolver, and the sender
records what it would have sent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agentos.api.events import EventBus
from agentos.core.errors import InvalidRequest, NotFound
from agentos.webhooks.outbound import OutboundRefused, OutboundUnresolved, Reply
from agentos.webhooks.service import (
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    WebhookService,
    approval_event,
    run_event,
    signature,
)

PUBLIC = "93.184.216.34"
URL = "https://hooks.example.com/agentos/receive?token=abc123"


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Receiver:
    """Records every request the service would make, and answers with the next status given."""

    def __init__(self, *answers: int | tuple[int, str]):
        self.answers = list(answers)
        self.sent: list[tuple[str, bytes, dict[str, str]]] = []

    async def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> Reply:
        self.sent.append((url, body, headers))
        answer = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, tuple):
            return Reply(answer[0], answer[1])
        return Reply(answer)


async def public(host: str, port: int) -> list[str]:
    return [PUBLIC]


async def private(host: str, port: int) -> list[str]:
    return ["10.0.0.7"]


async def nothing(host: str, port: int) -> list[str]:
    raise OSError("no such host")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def receiver() -> Receiver:
    return Receiver()


@pytest.fixture
def svc(tmp_path: Path, clock: Clock, receiver: Receiver):
    service = WebhookService(tmp_path, clock=clock, resolver=public, sender=receiver)
    yield service
    service.store.close()


APPROVAL = {"id": "ap_1", "kind": "action", "state": "pending", "effect": "commit", "workspace": "biz-a",
            "lane": "biz-a/social", "summary": "Post the photo", "expires_at": "2026-11-08T00:00:00Z"}


async def make(svc: WebhookService, events: list[str] | None = None) -> dict[str, Any]:
    return await svc.create(URL, events or ["approval.requested", "approval.decided", "approval.expired", "run.failed"])


# ---- making a webhook ----

async def test_a_webhook_is_made_with_its_secret_shown_once(svc: WebhookService) -> None:
    made = await make(svc)
    assert made["secret"].startswith("whsec_") and len(made["secret"]) > 30
    assert made["origin"] == "https://hooks.example.com"
    assert "secret" not in svc.list()[0] and "url" not in svc.list()[0]
    assert svc.list()[0]["origin"] == "https://hooks.example.com"  # the path, with its token, never comes back


async def test_the_address_and_the_secret_are_sealed_on_disk(svc: WebhookService, tmp_path: Path) -> None:
    made = await make(svc)
    raw = sqlite3.connect(tmp_path / "webhooks.db").execute("SELECT url_sealed, secret_sealed FROM webhooks").fetchall()
    [(url_sealed, secret_sealed)] = raw
    assert b"abc123" not in url_sealed and made["secret"].encode() not in secret_sealed
    key = tmp_path / "secrets" / "webhooks.key"
    assert key.exists() and (key.stat().st_mode & 0o777) == 0o600


async def test_a_sealed_field_moved_to_another_webhook_is_never_read_or_sent_to(svc: WebhookService, tmp_path: Path, receiver: Receiver) -> None:
    first = await make(svc, ["run.failed"])
    second = await make(svc, ["run.failed"])
    db = sqlite3.connect(tmp_path / "webhooks.db")
    with db:
        db.execute("UPDATE webhooks SET secret_sealed = (SELECT secret_sealed FROM webhooks WHERE id = ?) "
                   "WHERE id = ?", (first["id"], second["id"]))
    db.close()
    assert [hook.id for hook in svc.store.hooks()] == [first["id"]]  # the moved one is not read
    assert svc.store.hook(second["id"]) is None
    svc.on_event(_event("run", {"id": "r1", "status": "failed"}))
    await svc.deliver_due()
    assert len(receiver.sent) == 1 and receiver.sent[0][0] == URL  # only the readable webhook was sent to
    dropped = svc.store.db.one("SELECT state FROM deliveries WHERE webhook_id = ?", (second["id"],))
    assert dropped["state"] == "dropped"
    svc.delete(second["id"])  # still removable, by id alone
    assert svc.store.exists(second["id"]) is False


@pytest.mark.parametrize("url", [
    "http://hooks.example.com/x",
    "https://127.0.0.1/x",
    "https://10.0.0.5/x",
    "https://localhost:8443/x",
    "https://hooks.example.com:9000/x",
])
async def test_a_refused_address_makes_no_webhook(svc: WebhookService, url: str) -> None:
    with pytest.raises(OutboundRefused):
        await svc.create(url, ["run.failed"])
    assert svc.list() == []


async def test_a_name_that_resolves_to_a_private_address_is_refused(tmp_path: Path, clock: Clock) -> None:
    service = WebhookService(tmp_path, clock=clock, resolver=private, sender=Receiver())
    with pytest.raises(OutboundRefused, match="private network"):
        await service.create(URL, ["run.failed"])
    assert service.list() == []
    service.store.close()


async def test_a_name_that_does_not_resolve_is_refused_at_creation(tmp_path: Path, clock: Clock) -> None:
    service = WebhookService(tmp_path, clock=clock, resolver=nothing, sender=Receiver())
    with pytest.raises(OutboundUnresolved):
        await service.create(URL, ["run.failed"])
    service.store.close()


async def test_at_least_one_known_event_is_needed(svc: WebhookService) -> None:
    with pytest.raises(InvalidRequest):
        await svc.create(URL, [])
    with pytest.raises(InvalidRequest):
        await svc.create(URL, ["approval.granted"])


# ---- what is recorded ----

async def test_only_the_events_asked_for_are_recorded(svc: WebhookService) -> None:
    await make(svc, ["run.failed"])
    svc.on_event(_event("approval", APPROVAL))
    assert svc.store.db.one("SELECT COUNT(*) AS n FROM deliveries")["n"] == 0
    svc.on_event(_event("run", {"id": "run_9", "status": "failed", "lane": "biz-a/social", "summary": "boom"}))
    [row] = svc.store.db.all("SELECT kind FROM deliveries")
    assert row["kind"] == "run.failed"


async def test_an_approval_is_recorded_once_per_outcome(svc: WebhookService) -> None:
    await make(svc)
    for state in ("pending", "pending", "approved", "approved", "expired"):
        svc.on_event(_event("approval", {**APPROVAL, "state": state, "decided_by": "owner"}))
    kinds = [row["kind"] for row in svc.store.db.all("SELECT kind FROM deliveries ORDER BY created_at, rowid")]
    assert kinds == ["approval.requested", "approval.decided", "approval.expired"]


async def test_a_standing_approval_and_a_consumed_one_are_not_events(svc: WebhookService) -> None:
    await make(svc)
    svc.on_event(_event("approval", {"standing": {"id": "sa_1"}, "state": "created"}))
    svc.on_event(_event("approval", {**APPROVAL, "state": "consumed"}))
    assert svc.store.db.one("SELECT COUNT(*) AS n FROM deliveries")["n"] == 0


def test_an_approval_event_carries_no_content_and_a_short_summary() -> None:
    long = "x" * 900
    kind, event_id, data = approval_event({**APPROVAL, "summary": long, "preview": {"text": "secret body"}})
    assert kind == "approval.requested" and event_id == "approval:ap_1:requested"
    assert "preview" not in data and len(data["summary"]) <= 500 and data["summary"].endswith("...")


def test_a_failed_run_is_the_only_run_event() -> None:
    assert run_event({"id": "r", "status": "succeeded_verified"}) is None
    assert run_event({"id": "r", "status": "failed", "flow_id": "biz-a.post"})[2]["flow"] == "biz-a.post"


# ---- sending ----

async def test_a_delivery_is_signed_over_its_own_timestamp_and_body(svc: WebhookService, receiver: Receiver, clock: Clock) -> None:
    made = await make(svc, ["approval.requested"])
    svc.on_event(_event("approval", APPROVAL))
    assert await svc.deliver_due() == 1
    url, body, headers = receiver.sent[0]
    assert url == URL
    timestamp = headers["X-Agentos-Timestamp"]
    expected = "sha256=" + hmac.new(made["secret"].encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert headers["X-Agentos-Signature"] == expected == signature(made["secret"], timestamp, body)
    assert headers["X-Agentos-Event"] == "approval.requested" and headers["X-Agentos-Delivery"]
    envelope = json.loads(body)
    assert set(envelope) == {"type", "at", "data"} and envelope["data"]["id"] == "ap_1"
    [entry] = svc.deliveries(made["id"])
    assert entry["state"] == "delivered" and entry["attempts"] == 1 and entry["status"] == 200


async def test_a_failed_delivery_is_tried_again_on_the_schedule_then_stops(svc: WebhookService, receiver: Receiver, clock: Clock) -> None:
    await make(svc, ["run.failed"])
    receiver.answers = [500] * MAX_ATTEMPTS
    svc.on_event(_event("run", {"id": "r1", "status": "failed"}))
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        await svc.deliver_due()
        [row] = svc.store.db.all("SELECT state, attempts, next_attempt_at FROM deliveries")
        assert row["state"] == "pending" and row["attempts"] == attempt
        assert row["next_attempt_at"] == pytest.approx(clock.now + delay)
        clock.now = row["next_attempt_at"]  # the time it is due
    await svc.deliver_due()
    [row] = svc.store.db.all("SELECT state, attempts, last_error FROM deliveries")
    assert row["state"] == "failed" and row["attempts"] == MAX_ATTEMPTS
    assert "stops here" in row["last_error"]
    assert len(receiver.sent) == MAX_ATTEMPTS
    assert len({headers["X-Agentos-Delivery"] for _, _, headers in receiver.sent}) == 1  # one delivery, many tries


async def test_a_retry_after_is_honoured_up_to_an_hour(svc: WebhookService, receiver: Receiver, clock: Clock) -> None:
    await make(svc, ["run.failed"])
    receiver.answers = [(429, "900"), (503, "999999")]
    svc.on_event(_event("run", {"id": "r1", "status": "failed"}))
    await svc.deliver_due()
    [row] = svc.store.db.all("SELECT next_attempt_at FROM deliveries")
    assert row["next_attempt_at"] == pytest.approx(clock.now + 900)
    clock.now = row["next_attempt_at"]
    await svc.deliver_due()
    [row] = svc.store.db.all("SELECT next_attempt_at FROM deliveries")
    assert row["next_attempt_at"] == pytest.approx(clock.now + 3600)


async def test_a_refusal_from_the_receiver_stops_at_once(svc: WebhookService, receiver: Receiver) -> None:
    await make(svc, ["run.failed"])
    receiver.answers = [404]
    svc.on_event(_event("run", {"id": "r1", "status": "failed"}))
    await svc.deliver_due()
    [row] = svc.store.db.all("SELECT state, attempts, last_status FROM deliveries")
    assert (row["state"], row["attempts"], row["last_status"]) == ("failed", 1, 404)


async def test_a_redirect_is_not_followed_and_says_so(svc: WebhookService, receiver: Receiver) -> None:
    await make(svc, ["run.failed"])
    receiver.answers = [(302, "")]
    svc.on_event(_event("run", {"id": "r1", "status": "failed"}))
    await svc.deliver_due()
    [row] = svc.store.db.all("SELECT state, last_error FROM deliveries")
    assert row["state"] == "failed" and "redirects are not followed" in row["last_error"]


async def test_a_name_that_turns_private_before_sending_is_refused(svc: WebhookService, tmp_path: Path) -> None:
    """The check is made again when the request is sent, and a sender that is not ours is not used."""
    await make(svc, ["run.failed"])
    svc.on_event(_event("run", {"id": "r1", "status": "failed"}))
    service = WebhookService(tmp_path, clock=svc.clock, resolver=private)  # the real sender, no network
    try:
        await service.deliver_due()
        [row] = service.store.db.all("SELECT state, last_error FROM deliveries")
        assert row["state"] == "failed" and "private network" in row["last_error"]
    finally:
        service.store.close()


async def test_a_delivery_that_fails_unforeseen_is_recorded_and_the_rest_still_go(svc: WebhookService, receiver: Receiver, monkeypatch) -> None:
    await make(svc, ["run.failed"])
    svc.on_event(_event("run", {"id": "first", "status": "failed"}))
    svc.on_event(_event("run", {"id": "second", "status": "failed"}))
    real = svc._attempt
    calls = {"n": 0}

    async def flaky(row):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("something nobody planned for")
        await real(row)

    monkeypatch.setattr(svc, "_attempt", flaky)
    assert await svc.deliver_due() == 2
    states = [row["state"] for row in svc.store.db.all("SELECT state FROM deliveries ORDER BY created_at, rowid")]
    assert states == ["failed", "delivered"]
    assert len(receiver.sent) == 1


async def test_a_turned_off_webhook_sends_nothing(svc: WebhookService, receiver: Receiver) -> None:
    made = await make(svc, ["run.failed"])
    svc.on_event(_event("run", {"id": "r1", "status": "failed"}))
    svc.update(made["id"], enabled=False)
    await svc.deliver_due()
    assert receiver.sent == []
    [row] = svc.store.db.all("SELECT state, last_error FROM deliveries")
    assert row["state"] == "dropped" and "turned off" in row["last_error"]
    with pytest.raises(InvalidRequest):
        svc.test(made["id"])


async def test_a_test_sends_one_ping_to_that_webhook_alone(svc: WebhookService, receiver: Receiver) -> None:
    made = await make(svc, ["run.failed"])
    other = await svc.create("https://other.example.com/x", ["run.failed"])
    delivery_id = svc.test(made["id"])
    await svc.deliver_due()
    assert [url for url, _, _ in receiver.sent] == [URL]
    assert json.loads(receiver.sent[0][1])["type"] == "ping"
    assert svc.deliveries(made["id"])[0]["id"] == delivery_id
    assert svc.deliveries(other["id"]) == []


async def test_rotating_the_secret_signs_with_the_new_one_only(svc: WebhookService, receiver: Receiver) -> None:
    made = await make(svc, ["run.failed"])
    secret = svc.rotate(made["id"])
    assert secret != made["secret"]
    svc.on_event(_event("run", {"id": "r1", "status": "failed"}))
    await svc.deliver_due()
    _, body, headers = receiver.sent[0]
    assert headers["X-Agentos-Signature"] == signature(secret, headers["X-Agentos-Timestamp"], body)
    assert headers["X-Agentos-Signature"] != signature(made["secret"], headers["X-Agentos-Timestamp"], body)


async def test_delete_removes_the_webhook_and_what_it_recorded(svc: WebhookService) -> None:
    made = await make(svc, ["run.failed"])
    svc.on_event(_event("run", {"id": "r1", "status": "failed"}))
    svc.delete(made["id"])
    assert svc.list() == []
    assert svc.store.db.one("SELECT COUNT(*) AS n FROM deliveries")["n"] == 0
    with pytest.raises(NotFound):
        svc.get(made["id"])


async def test_the_events_on_the_node_bus_are_recorded(svc: WebhookService) -> None:
    bus = EventBus()
    svc.attach(bus)
    svc.attach(bus)  # once only
    await make(svc, ["approval.requested"])
    bus.publish("approval", APPROVAL)
    bus.publish("approval", {"standing": {"id": "sa_1"}, "state": "created"})
    assert svc.store.db.one("SELECT COUNT(*) AS n FROM deliveries")["n"] == 1


def test_an_error_in_a_listener_never_reaches_the_publisher() -> None:
    bus = EventBus()

    def broken(event: Any) -> None:
        raise RuntimeError("boom")

    bus.listen(broken)
    assert bus.publish("approval", APPROVAL).kind == "approval"


def _event(kind: str, data: dict[str, Any]):
    from agentos.api.events import Event
    from agentos.core.models import utcnow

    return Event(id=1, kind=kind, data=data, at=utcnow())
