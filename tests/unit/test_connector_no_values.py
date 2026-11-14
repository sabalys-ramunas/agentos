"""No value a site gave reaches a problem text, for any connector: not the run's error, the step journal,
a notification, health, what the run's events carry, or the answer a caller gets. A failure says what kind of
value came and the field. The saved values themselves stay in the run's private step state, which is resume
state and is shown to nobody, so it is not scanned."""

from __future__ import annotations

import json

import pytest
from connector_samples import LANE as SOCIAL_LANE
from connector_samples import SITES, bind, comments_op, commit_op, connector, connector_site, live

from agentos.core.models import RunStatus, Signal
from agentos.runner.connectors.compile import compile_connector
from agentos.runner.connectors.outputs import outputs_from_saved
from agentos.runner.connectors.service import ConnectorService
from agentos.runner.driver import JsonResponse
from agentos.runner.fakes import OWNER as SOCIAL_OWNER
from agentos.runner.fakes import El, FakeLanes, FakeNotifier, FakeSite, Harness
from agentos.runner.siteapis.problems import from_run
from agentos.runner.util import Clock

MARK = "zz-private-marker-zz"
API, EP, LANE = "harbor-supplier", "orders", "biz-a/supplier"
FLOW = f"connector.{API}.{EP}"
PORTAL = "https://portal.northsupply.example"
PAGE = f"{PORTAL}/orders"
FEED = f"{PORTAL}/api/orders?page=1"
OWNER = {"kind": "human", "id": "owner", "owner": "owner"}


def read_json(**fields) -> dict:
    return {"type": "read_json", "save_as": "orders", "url": r"^https://portal\.northsupply\.example/api/orders",
            "items": "data.orders", "max_items": 100, "timeout_ms": 1000,
            "fields": fields or {"number": {"path": "id", "type": "string", "private": True}}}


def doc(read: dict, **endpoint) -> dict:
    return {"api": API, "domain": "portal.northsupply.example", "version": "1.0.0", "surfaces": [LANE],
            "endpoints": {EP: {"title": "Orders", "effect": "read", "outputs": {"orders": "orders"},
                               "steps": [{"type": "navigate", "url": PAGE}, read], **endpoint}}}


def portal(body=None, rows: list[El] | None = None) -> FakeSite:
    site = FakeSite()
    site.route(PAGE, lambda s, d: El("main", name="Orders").add(*(rows or [])))
    if body is not None:
        site.json[PAGE] = [JsonResponse(FEED, 200, json.dumps(body).encode())]
    return site


def harness(tmp_path, site: FakeSite, definition: dict) -> Harness:
    h = Harness(tmp_path, site=site, services={"lanes": FakeLanes([LANE]), "notifier": FakeNotifier()})
    h.events = []
    h.runner.subscribe(lambda _kind, doc: h.events.append(doc))
    h.svc = h.runtime.get("site_apis")
    h.svc.save(definition, by=OWNER)
    h.svc.activate(API, by=OWNER)
    return h


def told(h: Harness, run) -> str:
    """Everything a person, an agent or a controller can read of a run."""
    return json.dumps({
        "error": run.error, "proof": run.proof, "output": run.output,
        "journal": [r.model_dump(mode="json") for r in h.store.steps(run.id)],
        "notifications": h.runtime.get("notifier").sent, "health": h.runner.health.list(),
        "events": getattr(h, "events", []), "described": h.runner.describe(run, steps=True),
        "answer": from_run(run, now=Clock().now()).body()}, default=str)


def schema_of(number: dict) -> dict:
    return {"type": "object", "properties": {"orders": {"type": "array", "items": {
        "type": "object", "properties": {"number": number}}}}}


ORDERS = {"data": {"orders": [{"id": MARK}]}}


@pytest.mark.parametrize("rule, says", [
    ({"type": "string", "pattern": "^[0-9]+$"}, "number"), ({"type": "string", "enum": ["open", "paid"]}, "number"),
    ({"type": "string", "const": "open"}, "number"), ({"type": "string", "maxLength": 3}, "number"),
    ({"type": "integer"}, "number"), ({"type": "string", "not": {"pattern": "zz"}}, "number"),
    ({"type": "string", "x-agentos-host": "portal.northsupply.example"}, "number")])
async def test_a_value_the_output_schema_refuses_is_not_quoted(tmp_path, rule, says):
    h = harness(tmp_path, portal(ORDERS), doc(read_json(), output=schema_of(rule)))
    run = await h.run(FLOW, surface=LANE)
    assert run.status == RunStatus.FAILED and run.error["code"] == "output_contract_violation", run.error
    assert says in run.error["message"]
    assert MARK not in told(h, run)


@pytest.mark.parametrize("body", [
    {"data": {"orders": [{"id": 3, "amount": {"value": MARK}}]}},
    {"data": {"orders": [{"id": 3, "amount": MARK}]}},
    {"data": {"orders": [MARK]}}, {"data": {"orders": MARK}}, {"data": MARK}])
async def test_a_value_of_the_wrong_kind_is_named_by_its_kind(tmp_path, body):
    fields = {"number": {"path": "id", "type": "integer"}, "total": {"path": "amount.value", "type": "number"}}
    h = harness(tmp_path, portal(body), doc(read_json(**fields)))
    run = await h.run(FLOW, surface=LANE)
    assert run.error["code"] == "output_contract_violation", run.error
    assert MARK not in told(h, run)


async def test_a_read_json_format_does_not_quote_what_it_refused(tmp_path):
    fields = {"when": {"path": "id", "type": "string", "format": "date-time"}}
    h = harness(tmp_path, portal(ORDERS), doc(read_json(**fields)))
    run = await h.run(FLOW, surface=LANE)
    assert run.error["code"] == "output_contract_violation" and "date-time" in run.error["message"]
    assert MARK not in told(h, run)


def extract(**field) -> dict:
    return {"type": "extract", "save_as": "orders", "max_items": 9, "items": {"role": "listitem"},
            "fields": {"number": {"attr": "data-id", **field}}}


@pytest.mark.parametrize("field, value, says", [
    ({"pattern": "^[0-9]+$"}, MARK, "its pattern"), ({"format": "integer"}, MARK, "an integer"),
    ({"format": "number"}, MARK, "a number"), ({"format": "date-time"}, MARK, "a date-time"),
    ({"format": "uri"}, f"ftp://{MARK}/x", "a URL"),
    ({"capture": "^(zz.*)$", "pattern": "^[0-9]+$"}, MARK, "its pattern"),
    ({"capture": "^(zz.*)$", "format": "integer"}, MARK, "an integer")])
async def test_what_an_extract_refused_is_not_quoted(tmp_path, field, value, says):
    rows = [El("listitem", "Row", attrs={"data-id": value})]
    h = harness(tmp_path, portal(rows=rows), doc(extract(**field)))
    run = await h.run(FLOW, surface=LANE)
    assert run.error["code"] == "output_contract_violation", run.error
    assert says in run.error["message"] and "number" in run.error["message"]
    assert MARK not in told(h, run)


def test_a_problem_says_the_field_from_the_schema_and_never_a_key_of_the_data():
    meta = {"outputs": {"rows": "rows"}, "output": {"type": "object", "properties": {"rows": {
        "type": "object", "additionalProperties": {"type": "integer"}}}}}
    _, problems = outputs_from_saved(meta, {"rows": {MARK: "x"}})
    assert problems and MARK not in " ".join(problems)
    _, problems = outputs_from_saved({"outputs": {"rows": "rows"}, "output": {"type": "object", "properties": {"rows": {
        "type": "object", "additionalProperties": False}}}}, {"rows": {MARK: 1}})
    assert problems and MARK not in " ".join(problems)


# ---- identity, preflight, readback ----


def social(tmp_path, *, site=None, ops=None) -> Harness:
    site = site or connector_site()
    h = Harness(tmp_path, site=site, services={"lanes": FakeLanes([SOCIAL_LANE]), "notifier": FakeNotifier()})
    live(h.driver)
    d = connector(ops if ops is not None else {"post_photo": commit_op(), "list_comments": comments_op()})
    ConnectorService(h.flows, sites=SITES).install(compile_connector(d, sites=SITES), by=SOCIAL_OWNER)
    bind(h, account_language="en")
    return h


PHOTO = {"caption": "Autumn at the lake", "image": "photo.jpg"}


async def test_the_wrong_account_is_not_named(tmp_path):
    site = connector_site()
    site.identity = {"site": "social", "user_id": MARK, "source": "cookie uid"}
    h = social(tmp_path, site=site)
    run = await h.run("connector.social.post_photo", PHOTO, lane=SOCIAL_LANE)
    assert run.error["code"] == "wrong_account" and run.error["found"] == "another account"
    assert MARK not in told(h, run)


async def test_what_the_composer_held_is_not_quoted(tmp_path):
    h = social(tmp_path)

    def rewrite(op, target, d):
        if op == "fill":
            d.values["caption"] += MARK

    h.driver.after_hooks.insert(0, rewrite)
    run = await h.run("connector.social.post_photo", PHOTO, lane=SOCIAL_LANE)
    assert run.error["code"] == "preflight_mismatch" and "another value" in run.error["message"]
    assert MARK not in told(h, run)


async def test_what_a_readback_found_is_not_quoted(tmp_path):
    op = commit_op()
    op["steps"].insert(1, {"type": "readback", "locator": {"role": "heading", "name": "Feed", "within": {"role": "main"}},
                           "condition": {"kind": "attribute", "name": "data-x", "equals": "open"}})
    site = connector_site()

    def home(s, d):
        return El("document", "Home").add(El("main").add(El("heading", "Feed", attrs={"data-x": MARK})))

    site.route("https://social.example/", home)
    h = social(tmp_path, site=site, ops={"post_photo": op})
    run = await h.run("connector.social.post_photo", PHOTO, lane=SOCIAL_LANE)
    assert run.error["code"] == "readback_failed" and run.error["found"] == "another value", run.error
    assert MARK not in told(h, run)


# ---- what a template carries: an id read earlier, in a URL, a locator or a check ----
# Only the catalog has this: a site API's URL takes params only and its locators take none.


POST_ID = "987654321098"


def following(*steps: dict) -> dict:
    op = comments_op()
    op["steps"][2]["fields"]["id"]["pattern"] = "^[0-9]{1,12}$"
    op["steps"][3]["steps"][1:1] = list(steps)
    return op


def posted(tmp_path, op: dict) -> Harness:
    site = connector_site(posts=[{"id": POST_ID, "caption": "Autumn"}])
    return social(tmp_path, site=site, ops={"list_comments": op})


async def test_a_saved_id_carried_into_a_url_does_not_reach_the_failure(tmp_path):
    h = posted(tmp_path, following())

    def challenge(op, target, d):
        if op == "navigate" and POST_ID in str(target):
            d.site.signal = Signal.CHECKPOINT

    h.driver.after_hooks.append(challenge)
    run = await h.run("connector.social.list_comments", {}, lane=SOCIAL_LANE)
    assert run.error["code"] == "signal_checkpoint", run.error
    assert POST_ID not in json.dumps(run.error)


async def test_a_saved_id_carried_into_a_locator_does_not_reach_the_failure(tmp_path):
    wait = {"type": "wait_for", "timeout_ms": 100, "locator": {"role": "button", "name": "${vars.post.id}", "within": {"role": "main"}}}
    h = posted(tmp_path, following(wait))
    run = await h.run("connector.social.list_comments", {}, lane=SOCIAL_LANE)
    assert run.error["cause"] == "element_not_found", run.error
    assert POST_ID not in json.dumps(run.error)


async def test_a_saved_id_carried_into_a_check_does_not_reach_the_failure(tmp_path):
    check = {"type": "readback", "timeout_ms": 0, "condition": {"kind": "url", "contains": "x${vars.post.id}"}}
    h = posted(tmp_path, following(check))
    run = await h.run("connector.social.list_comments", {}, lane=SOCIAL_LANE)
    assert run.error["code"] == "readback_failed", run.error
    assert POST_ID not in json.dumps([run.error, run.proof])
