"""What a site API definition may say (docs/SITE-APIS.md, "Definition"): hosts, effects, labels."""

from __future__ import annotations

import copy
import random
import time
from types import SimpleNamespace

import pytest
from connector_samples import SITES as SAMPLE_SITES
from connector_samples import connector, read_op

from agentos.core.template import render
from agentos.node.flow_driver import inside, own_filter, record_levels, tree_order
from agentos.runner.connectors.compile import ConnectorInvalid, compile_connector
from agentos.runner.connectors.lint import param_problems
from agentos.runner.connectors.models import Commit
from agentos.runner.connectors.steps import walk
from agentos.runner.fakes import El, _picked
from agentos.runner.models import ExtractField
from agentos.runner.readvalues import typed_value
from agentos.runner.siteapis.models import SAMPLE, compile_api, labelled, parse, shown_effect, to_connector
from agentos.runner.util import url_problem

SITES = {"instagram": ("instagram.com",)}


def _doc(**endpoint):
    doc = copy.deepcopy(SAMPLE)
    doc["endpoints"]["open_orders"].update(endpoint)
    return doc


def _with_url(url, **input_):
    doc = _doc()
    ep = doc["endpoints"]["open_orders"]
    ep["steps"][0]["url"] = url
    if input_:
        ep["input"] = {"type": "object", "properties": {k: {"type": "string", "maxLength": 40} for k in input_}}
    return doc


# ---- the contract's example ----


def test_the_contract_example_compiles_as_written():
    op = compile_api(SAMPLE, person=True, sites={}).operations["open_orders"]
    assert op.flow is not None and op.tool_name == "harbor_supplier_open_orders"
    meta = op.definition["connector"]
    assert meta["outputs"] == {"orders": "orders.items"} and meta["pace"]["per_hour"] == 60
    assert meta["surfaces"] == ["harbor/supplier"] and meta["site"] == "web"
    item = meta["output"]["properties"]["orders"]["items"]["properties"]
    assert item["number"] == {"type": "string", "maxLength": 40} and item["url"]["x-agentos-host"] == "portal.northsupply.example"


def test_cache_and_expect_change_the_implementation_not_the_contract():
    a = compile_api(SAMPLE, person=True, sites={}).operations["open_orders"]
    b = compile_api(_doc(cache_s=60, expect={"min_items": {"orders": 5}}), person=True, sites={}).operations["open_orders"]
    assert a.contract == b.contract and a.implementation != b.implementation


def test_a_read_caches_for_five_minutes_by_default_and_nothing_else_caches():
    doc = _doc()
    del doc["endpoints"]["open_orders"]["cache_s"]
    assert compile_api(doc, person=True, sites={}).operations["open_orders"].definition["connector"]["http"]["cache_s"] == 300
    with pytest.raises(ConnectorInvalid, match="only a read is cached"):
        parse(_doc(effect="draft", cache_s=60))
    with pytest.raises(ConnectorInvalid):
        parse(_doc(cache_s=86_401))


# ---- a domain with a row in the site table ----


def test_a_known_site_keeps_its_account_check():
    doc = {**copy.deepcopy(SAMPLE), "domain": "instagram.com"}
    doc["endpoints"]["open_orders"]["steps"][0]["url"] = "https://www.instagram.com/x?s=${status}"
    doc["endpoints"]["open_orders"]["steps"][1]["fields"]["url"]["host"] = "instagram.com"
    orders = doc["endpoints"]["open_orders"]["output"]["properties"]["orders"]
    orders["items"]["properties"]["url"]["x-agentos-host"] = "instagram.com"
    op = compile_api(doc, person=True, sites=SITES).operations["open_orders"]
    meta = op.definition["connector"]
    assert meta["site"] == "instagram" and meta["acting_as"] == {"kind": "account"}
    identity = [s for _, s in walk(op.definition["steps"]) if s["type"] == "assert_identity"]
    assert identity and identity[0]["acting_as"] == "account" and identity[0]["expect"] == "${acts_as_id}"
    assert [s["type"] for s in op.definition["steps"]][:2] == ["navigate", "assert_identity"]


def test_a_subdomain_of_a_known_site_is_that_site():
    doc = to_connector({**copy.deepcopy(SAMPLE), "domain": "business.instagram.com"}, person=True, sites=SITES)
    assert doc["site"] == "instagram"


def test_lane_identity_cannot_be_written_into_a_connector_document():
    doc = to_connector(SAMPLE, person=True, sites={})
    doc.update({"connector": "web", "site": "web", "surfaces": []})
    with pytest.raises(ConnectorInvalid):
        compile_connector(doc, sites={})


# ---- hosts ----


@pytest.mark.parametrize("url", [
    "http://portal.northsupply.example/orders",
    "https://evil.example/orders",
    "https://northsupply.example/orders",
    "https://127.0.0.1/orders", "https://10.0.0.8/", "https://192.168.1.1/", "https://[::1]/", "https://2130706433/",
    "https://localhost/orders",
    "https://portal.northsupply.example:pw@evil.example/",
    "https://portal.northsupply.example@evil.example/",
    "https://evil.example\\.portal.northsupply.example/",
    "https://evil.example\\@portal.northsupply.example/",
    "https://portal.northsupply.example\\evil",
    "https://${host}/orders",
    "https://${host}.portal.northsupply.example/orders",
    "https://portal.northsupply.example${host}/orders",
    "https://portal.northsupply.example:${host}/orders",
    "${host}",
    "${ host }",
    " ${host}",
    "https:${host}",
])
def test_a_navigate_stays_on_a_literal_https_host_under_the_domain(url):
    with pytest.raises(ConnectorInvalid):
        compile_api(_with_url(url, host="x"), person=True, sites={})


@pytest.mark.parametrize("url", ["https://portal.northsupply.example/orders", "https://a.portal.northsupply.example:8443/o",
                                 "https://portal.northsupply.example/o/${host}?q=${host}", "https://portal.northsupply.example?q=${host}"])
def test_a_param_may_fill_the_path_and_the_query(url):
    assert compile_api(_with_url(url, host="x"), person=True, sites={}).operations


@pytest.mark.parametrize("domain", ["localhost", "127.0.0.1", "10.0.0.1", "192.168.0.1.nip.io", "router.local", "nas.lan", "printer.home.arpa",
                                    "db.internal", "svc.localhost", "x.onion", "[::1]", "PORTAL.example", "example"])
def test_a_private_or_odd_domain_is_refused(domain):
    doc = {**copy.deepcopy(SAMPLE), "domain": domain}
    doc["endpoints"]["open_orders"]["steps"][0]["url"] = f"https://{domain}/o"
    doc["endpoints"]["open_orders"]["steps"][1]["fields"]["url"]["host"] = domain
    with pytest.raises(ConnectorInvalid):
        compile_api(doc, person=True, sites={})


def test_what_a_page_said_never_steers_the_lane():
    doc = _with_url("https://portal.northsupply.example/next/${vars.orders.count}")
    with pytest.raises(ConnectorInvalid, match="never"):
        compile_api(doc, person=True, sites={})
    doc = _with_url("https://portal.northsupply.example/next/${trigger.url}")
    with pytest.raises(ConnectorInvalid, match="never"):
        compile_api(doc, person=True, sites={})


@pytest.mark.parametrize(("value", "encoded"), [
    ("a/b", "a%2Fb"), ("a?b=c", "a%3Fb%3Dc"), ("a#b", "a%23b"), ("a b", "a%20b"), ("a&admin=1", "a%26admin%3D1"),
    ("//evil.example", "%2F%2Fevil.example"), ("https://evil.example/", "https%3A%2F%2Fevil.example%2F"),
    ("..%2f", "..%252f"), ("%2e%2e", "%252e%252e"), ("\\evil", "%5Cevil"), ("a\r\nb", "a%0D%0Ab"), ("${vars.x}", "%24%7Bvars.x%7D")])
def test_a_callers_value_is_encoded_in_the_path_and_the_query(value, encoded):
    tpl = "https://portal.northsupply.example/o/${id}/edit?q=${id}"
    url = render(tpl, {"id": value}, url=True)
    assert url == f"https://portal.northsupply.example/o/{encoded}/edit?q={encoded}"
    assert url_problem(tpl, url, "portal.northsupply.example") is None


@pytest.mark.parametrize("value", [".", ".."])
def test_a_callers_value_cannot_walk_out_of_the_path(value):
    tpl = "https://portal.northsupply.example/o/${id}/edit"
    assert "dot segment" in url_problem(tpl, render(tpl, {"id": value}, url=True), "portal.northsupply.example")
    tpl = "https://portal.northsupply.example/o?q=${id}"  # in a query it is just text
    assert url_problem(tpl, render(tpl, {"id": value}, url=True), "portal.northsupply.example") is None


# ---- effects ----


@pytest.mark.parametrize("effect", ["pay", "irreversible"])
def test_pay_and_irreversible_are_not_endpoints(effect):
    with pytest.raises(ConnectorInvalid, match="not an endpoint"):
        parse(_doc(effect=effect))


def _input_steps(publishes=None):
    click = {"type": "click", "locator": {"role": "button", "name": "Filter"}, **({"publishes": publishes} if publishes is not None else {})}
    return [SAMPLE["endpoints"]["open_orders"]["steps"][0], click, SAMPLE["endpoints"]["open_orders"]["steps"][1]]


def test_effect_is_checked_against_the_steps():
    with pytest.raises(ConnectorInvalid, match="step"):
        compile_api(_doc(steps=_input_steps(False), cache_s=None), person=True, sites={})  # a read with an input step
    with pytest.raises(ConnectorInvalid, match="commit"):
        compile_api(_doc(effect="draft", steps=_input_steps(), cache_s=0), person=True, sites={})  # not vouched at all
    assert compile_api(_doc(effect="draft", steps=_input_steps(False), cache_s=0), person=True, sites={}).operations


def test_an_agents_input_step_counts_as_commit_whatever_it_says():
    doc = parse(_doc(effect="draft", steps=_input_steps(False), cache_s=0))
    ep = doc.endpoints["open_orders"]
    assert shown_effect(ep, person=False) == "commit" and shown_effect(ep, person=True) == "draft"
    with pytest.raises(ConnectorInvalid, match="commit"):
        compile_api(doc, person=False, sites={})


def test_a_commit_endpoint_meets_the_connector_rules_in_full():
    with pytest.raises(ConnectorInvalid) as e:
        compile_api(_doc(effect="commit", steps=_input_steps(), cache_s=0), person=True, sites={})
    text = str(e.value)
    assert "names its commit step" in text


# ---- labels ----


def test_text_from_a_page_is_untrusted_and_only_a_person_unmarks_it():
    doc = copy.deepcopy(SAMPLE)
    field = doc["endpoints"]["open_orders"]["steps"][1]["fields"]["number"]
    assert "untrusted" not in field
    http = compile_api(doc, person=True, sites={}).operations["open_orders"].definition["connector"]["http"]
    assert http["untrusted_fields"] == ["orders[].number", "orders[].total"]
    field["untrusted"] = False
    agent = compile_api(doc, person=False, sites={}).operations["open_orders"].definition
    assert "orders[].number" in agent["connector"]["http"]["untrusted_fields"]
    assert next(s for s in agent["steps"] if s["type"] == "extract")["fields"]["number"]["untrusted"] is True
    person = compile_api(doc, person=True, sites={}).operations["open_orders"].definition
    assert person["connector"]["http"]["untrusted_fields"] == ["orders[].total"]


def test_a_free_attribute_is_page_text_too_and_a_typed_one_is_not():
    doc = copy.deepcopy(SAMPLE)
    fields = doc["endpoints"]["open_orders"]["steps"][1]["fields"]
    fields["hint"] = {"attr": "title", "max_chars": 80}
    http = compile_api(doc, person=True, sites={}).operations["open_orders"].definition["connector"]["http"]
    assert "orders[].hint" in http["untrusted_fields"] and "orders[].url" not in http["untrusted_fields"]


@pytest.mark.parametrize("field", [
    {"attr": "aria-label", "pattern": ".*"}, {"attr": "aria-label", "pattern": ".*", "untrusted": False},
    {"attr": "title", "max_chars": 80, "untrusted": False}, {"read": "text", "pattern": "^A"},
    {"attr": "href", "format": "uri"}, {"attr": "href", "format": "uri", "untrusted": False},
])
def test_what_an_agent_wrote_is_untrusted_whatever_it_says(field):
    assert labelled(field, person=False)["untrusted"] is True


@pytest.mark.parametrize("field", [
    {"attr": "datetime", "format": "date-time"}, {"read": "text", "format": "integer"}, {"attr": "value", "format": "number"},
    {"attr": "href", "format": "uri", "host": "portal.northsupply.example"},
])
def test_only_a_checked_format_is_not_page_text_in_what_an_agent_wrote(field):
    assert not labelled(field, person=False).get("untrusted")


def test_a_persons_pattern_still_holds_a_field():
    assert not labelled({"attr": "data-id", "pattern": "^[0-9]+$"}, person=True).get("untrusted")
    assert not labelled({"attr": "title", "max_chars": 80, "untrusted": False}, person=True).get("untrusted")


def test_a_private_field_is_listed_and_stays_private():
    doc = copy.deepcopy(SAMPLE)
    doc["endpoints"]["open_orders"]["steps"][1]["fields"]["total"]["private"] = True
    http = compile_api(doc, person=False, sites={}).operations["open_orders"].definition["connector"]["http"]
    assert http["private_fields"] == ["orders[].total"]


def test_an_endpoint_returns_declared_fields_only():
    doc = _doc(outputs={"orders": "orders.items.0.number"})
    with pytest.raises(ConnectorInvalid, match="must name what an extract"):
        compile_api(doc, person=True, sites={})
    doc = _doc(outputs={"page": "page_text"}, expect={})
    with pytest.raises(ConnectorInvalid, match="must name what an extract"):
        compile_api(doc, person=True, sites={})
    op = compile_api(_doc(outputs={"orders": "orders", "n": "orders.count"}), person=True, sites={}).operations["open_orders"]
    schema = op.definition["connector"]["output"]
    assert schema["additionalProperties"] is False and schema["properties"]["n"]["type"] == "integer"
    assert schema["properties"]["orders"]["items"]["additionalProperties"] is False


# ---- what the engine gained ----


def _boxes():
    cards = [El("generic", children=[El("heading", name=f"H{i}", attrs={"level": "3"}), El("paragraph", text=f"P{i}")]) for i in range(3)]
    grid = El("generic", children=cards)
    root = El("main", children=[El("heading", name="Top", attrs={"level": "1"}), grid])
    for parent in (root, grid, *cards):
        for c in parent.children:
            c.parent = parent
    return root, cards


def test_the_fake_and_the_node_pick_the_same_records():
    _, cards = _boxes()
    heads = [c.children[0] for c in cards]
    assert _picked(heads, "card") == cards
    assert _picked([*heads], None) == heads
    assert record_levels(["0/1/0/0", "0/1/1/0", "0/1/2/0"], "card") == [1, 1, 1]
    assert record_levels(["0/1/0/2/0", "0/1/1/2/0"], "card") == [2, 2]
    assert record_levels(["0/1/0", "0/1/0/4/0", "0/1/1"], "outermost") == [0, None, 0]
    assert record_levels(["0/1"], "card") == [0]


def _tree(rng: random.Random, n: int) -> list[str]:
    """Paths of a random tree with shadow roots in it, in tree order."""
    paths = ["0"]
    for _ in range(n):
        parent = rng.choice(paths)
        paths.append(f"{parent}/{rng.choice([-1, 0, 1, 2, 3])}")
    return sorted(set(paths), key=tree_order)


@pytest.mark.parametrize("seed", range(8))
def test_outermost_and_own_filter_keep_what_the_plain_reading_keeps(seed):
    rng = random.Random(seed)
    tree = _tree(rng, 300)
    picked = sorted(rng.sample(tree, 120), key=tree_order)
    walls = rng.sample(tree, 30)
    assert record_levels(picked, "outermost") == [None if any(inside(o, p) for o in picked if o != p) else 0 for p in picked]
    nested = [w for w in walls if inside("0", w)]
    assert own_filter(picked, "0", walls) == [not any(inside(w, p) for w in nested) for p in picked]


def test_outermost_and_own_filter_are_not_quadratic():
    paths = [f"0/1/{i}" for i in range(200_000)]
    t0 = time.monotonic()
    assert record_levels(paths, "outermost") == [0] * len(paths)
    assert all(own_filter(paths, "0/1", paths))
    assert time.monotonic() - t0 < 5


def test_a_card_never_climbs_out_of_a_shadow_root():
    assert record_levels(["0/-1/3/0", "0/-1/4/0"], "card") == [1, 1]
    assert record_levels(["0/2/-1/0/0", "0/3/-1/0/0"], "card") == [1, 1]


def test_capture_keeps_one_group_and_no_match_is_no_value():
    f = ExtractField(read="text", capture=r"(€\d+)", max_chars=20)
    assert typed_value("Nida €180 a month", f, "https://x.test/")[0] == "€180"
    assert typed_value("Nida", f, "https://x.test/")[0] is None
    with pytest.raises(ValueError, match="one group"):
        ExtractField(read="text", capture=r"€\d+", max_chars=20)


@pytest.mark.parametrize("field", [
    {"read": "text", "capture": r"(\d+)?€", "max_chars": 20},
    {"read": "text", "capture": r"(\d+)?€", "format": "integer"},
    {"read": "text", "capture": r"(\d+)?€", "format": "number"},
    {"read": "text", "capture": r"(\d+)?€", "format": "date-time"},
    {"read": "text", "capture": r"(\d+)?€", "pattern": "^[0-9]+$", "max_chars": 20},
    {"attr": "href", "capture": r"(/x)?€", "format": "uri", "host": "x.test"},
])
def test_an_optional_group_that_matched_nothing_is_no_value(field):
    assert typed_value("€", ExtractField(**field), "https://x.test/") == (None, False, None)


# ---- where a caller's param may reach a page ----


TAB = {"tab": {"type": "string", "maxLength": 40}}
CLICK = {"role": "button", "name": "Open"}


def _params(*steps, props=None, **endpoint):
    status = {"status": {"type": "string", "enum": ["open", "late"], "default": "open"}}
    doc = _doc(input={"type": "object", "properties": {**status, **(props or TAB)}}, effect="draft", cache_s=0, **endpoint)
    ep = doc["endpoints"]["open_orders"]
    ep["steps"] = [ep["steps"][0], *steps, ep["steps"][1]]
    return doc


def _click(locator, **rest):
    return {"type": "click", "publishes": False, "locator": locator, **rest}


def _extract(**rest):
    return {"type": "extract", "save_as": "more", "max_items": 5, "items": {"role": "row"},
            "fields": {"a": {"read": "text", "max_chars": 40}}, **rest}


READ = {"read": "text", "max_chars": 40}


@pytest.mark.parametrize("doc", [
    _params(_click({"css": "[data-tab='${tab}']"})),
    _params(_click({"role": "button", "name": "${tab}"})),
    _params(_click({"role": "button", "names": {"en": "${tab}"}})),
    _params(_click({"role": "button", "attr": {"name": "data-tab", "equals": "${tab}"}})),
    _params(_click({"role": "button", "attr": {"name": "data-tab", "regex": "^${tab}$"}})),
    _params(_click({"role": "button", "within": {"role": "region", "name": "${tab}"}})),
    _params(_click({"role": "button", "within": {"role": "region", "within": {"css": "#${tab}"}}})),
    _params({"type": "wait_for", "locator": {"css": ".${tab}"}}),
    _params({"type": "scroll_to", "locator": {"css": ".${tab}"}}),
    _params(_click(CLICK, readback={"locator": {"css": ".${tab}"}, "condition": {"kind": "exists"}})),
    _params({"type": "readback", "locator": {"css": ".${tab}"}, "condition": {"kind": "exists"}}),
    _params({"type": "branch", "if": {"locator": {"css": ".${tab}"}, "condition": {"kind": "exists"}}, "then": []}),
    _params({"type": "branch", "if": {"param": "tab", "is_set": True}, "then": [_click({"css": ".${tab}"})]}),
    _params(_extract(items={"css": "${tab}"})),
    _params(_extract(items={"role": "row", "within": {"role": "table", "name": "${tab}"}})),
    _params(_extract(fields={"a": {**READ, "locator": {"css": "${tab}"}}})),
    _params(_extract(fields={"a": {**READ, "locator": {"role": "cell", "within": {"css": "${tab}"}}}})),
    _params(_extract(fields={"a": {**READ, "pattern": "${tab}"}})),
    _params(_extract(fields={"a": {**READ, "capture": "(${tab})"}})),
    _params(_extract(fields={"a": {"attr": "href", "format": "uri", "host": "${tab}.northsupply.example"}})),
    _params(discard=[_click({"css": ".${tab}"})]),
    _params({"type": "select", "locator": {"css": ".${tab}"}, "option": "a", "publishes": False}),
], ids=lambda d: str(d["endpoints"]["open_orders"]["steps"][1:-1] or "discard")[:60])
def test_no_param_in_a_locator_or_an_extract_at_any_depth(doc):
    with pytest.raises(ConnectorInvalid, match="param"):
        compile_api(doc, person=True, sites={})


def test_a_preflight_locator_takes_no_param_either():
    commit = Commit(step="send", preflight=[{"kind": "count", "locator": {"css": ".${tab}"}, "equals": 1}])
    op = SimpleNamespace(input={"properties": TAB}, steps=[], discard=[], commit=commit)
    assert [where for where, _ in param_problems(op)] == ["commit.preflight.0"]


def test_a_catalog_connector_keeps_its_params_in_locators():
    op = compile_connector(connector({"read": read_op()}), sites=SAMPLE_SITES).operations["read"]
    assert "${acts_as_id}" in str(op.definition["steps"]) and op.flow is not None


def test_a_page_value_is_no_more_welcome_in_a_locator_than_a_param():
    with pytest.raises(ConnectorInvalid, match="param"):
        compile_api(_params(_click({"css": ".${vars.orders.count}"})), person=True, sites={})


def test_a_callers_value_never_becomes_a_regex():
    for step in ({"type": "readback", "locator": CLICK, "condition": {"kind": "text", "matches": "${tab}"}},
                 _click(CLICK, readback={"condition": {"kind": "text", "matches": "^${tab}"}}),
                 {"type": "branch", "if": {"locator": CLICK, "condition": {"kind": "text", "matches": "${tab}"}}, "then": []}):
        with pytest.raises(ConnectorInvalid, match="regex"):
            compile_api(_params(step), person=True, sites={})


@pytest.mark.parametrize("step", [
    {"type": "select", "locator": CLICK, "param": "tab", "publishes": False},
    {"type": "select", "locator": CLICK, "option": "${tab}", "publishes": False},
    {"type": "select", "locator": CLICK, "option": "tab-${tab}", "publishes": False},
    {"type": "select", "locator": CLICK, "option": "${vars.orders.count}", "publishes": False},
])
def test_a_select_takes_a_param_only_with_an_enum(step):
    with pytest.raises(ConnectorInvalid, match="enum"):
        compile_api(_params(step), person=True, sites={})
    if "vars" not in str(step):
        compile_api(_params(step, props={"tab": {"type": "string", "enum": ["a", "b"]}}), person=True, sites={})


def test_a_file_is_not_a_way_in():
    step = {"type": "upload", "locator": CLICK, "param": "tab", "publishes": False}
    with pytest.raises(ConnectorInvalid, match="upload"):
        compile_api(_params(step), person=True, sites={})


@pytest.mark.parametrize("step", [
    {"type": "fill", "locator": CLICK, "text": "${tab}", "publishes": False},
    {"type": "fill", "locator": CLICK, "param": "tab", "publishes": False},
    {"type": "type", "locator": CLICK, "text": "tab ${tab}", "publishes": False},
    {"type": "navigate", "url": "https://portal.northsupply.example/tabs/${tab}"},
    {"type": "branch", "if": {"param": "tab", "equals": "a"}, "then": [_click(CLICK)]},
    _click(CLICK, readback={"condition": {"kind": "text", "equals": "${tab}"}}),
    {"type": "select", "locator": CLICK, "option": "literal", "publishes": False},
])
def test_a_param_still_reaches_a_page_through_a_url_or_a_value(step):
    assert compile_api(_params(step), person=True, sites={}).operations["open_orders"].flow is not None
