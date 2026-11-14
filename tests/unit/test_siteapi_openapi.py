"""The OpenAPI document: valid 3.1, per key, with no site data, and every path in it answers."""

from __future__ import annotations

import json

import pytest
from openapi_spec_validator import OpenAPIV31SpecValidator
from siteapi_http_support import API, OTHER, ROWS, SURFACE, Node, definition, search_endpoint

from agentos.runner.fakes import FakeClock
from agentos.runner.flows import FlowService
from agentos.runner.siteapis import openapi as oas
from agentos.runner.siteapis.models import install_for_test
from agentos.runner.store import RunnerStore

DOC = f"/v1/sites/{API}/openapi.json"


@pytest.fixture
async def node(tmp_path):
    async with Node(tmp_path) as n:
        yield n


def valid(doc):
    errors = [str(e)[:300] for e in OpenAPIV31SpecValidator(doc).iter_errors()]
    assert not errors, errors
    return doc


async def test_the_document_is_valid_3_1_with_the_contracts_marks(node):
    key = node.key(ceiling="commit")
    async with node.client(key) as c:
        r = await c.get(DOC)
    doc = valid(r.json())
    assert doc["openapi"] == "3.1.0" and doc["info"]["version"] == "1.0.0" and doc["servers"][0]["url"] == node.url
    assert doc["components"]["securitySchemes"]["bearer"] == {"type": "http", "scheme": "bearer",
                                                              "bearerFormat": "aos_ key"}
    assert doc["security"] == [{"bearer": []}]
    read = doc["paths"][f"/v1/sites/{API}/open_orders"]
    assert set(read) == {"get", "post"} and read["get"]["operationId"] == f"{API}_open_orders"
    assert read["get"]["x-agentos-effect"] == "read" and read["get"]["x-agentos-cache-s"] == 300
    assert read["get"]["x-agentos-untrusted-fields"] == ["orders[].number", "orders[].total"]
    assert read["post"]["operationId"] == f"{API}_open_orders_post"  # an operationId is unique in a document
    assert set(doc["paths"][f"/v1/sites/{API}/send_note"]) == {"post"}
    commit = doc["paths"][f"/v1/sites/{API}/send_note"]["post"]
    assert commit["operationId"] == f"{API}_send_note"
    idem = next(p for p in commit["parameters"] if p.get("name") == "Idempotency-Key")
    assert idem["required"] is True
    ids = [op["operationId"] for item in doc["paths"].values() for op in item.values()]
    assert len(ids) == len(set(ids))
    assert read["get"]["responses"]["202"] == {"$ref": "#/components/responses/Pending"}
    assert read["get"]["responses"]["503"] == {"$ref": "#/components/responses/Problem"}
    problem = doc["components"]["schemas"]["Problem"]
    assert {"type", "title", "status", "code", "detail"} <= set(problem["required"])


async def test_input_and_output_go_in_as_they_are_less_the_marks(node):
    key = node.key()
    async with node.client(key) as c:
        doc = (await c.get(DOC)).json()
    text = json.dumps(doc)
    assert "x-agentos-host" not in text and "x-agentos-binding" not in text
    assert "acts_as_id" not in text and "account_language" not in text
    get = doc["paths"][f"/v1/sites/{API}/open_orders"]["get"]
    status = next(p for p in get["parameters"] if p.get("name") == "status")
    assert status["schema"]["enum"] == ["open", "late"] and status["required"] is False
    data = get["responses"]["200"]["content"]["application/json"]["schema"]["allOf"][1]["properties"]["data"]
    assert data["properties"]["orders"]["items"]["properties"]["url"]["format"] == "uri"


async def test_the_document_holds_no_site_data_no_key_and_no_sample(node):
    key = node.key(surfaces=[SURFACE])
    async with node.client(key) as c:
        assert (await c.get(f"/v1/sites/{API}/open_orders")).status_code == 200  # the cache now holds a good result
        r = await c.get(DOC)
    text = r.text
    for number, total in ROWS:
        assert number not in text and total not in text
    assert key not in text and key[:8] not in text and "example\":" not in text and "examples" not in text


def test_samples_are_dropped_from_an_output_and_kept_in_an_input():
    schema = {"type": "object", "default": {"a": 1}, "examples": [{"a": "from a page"}], "x-agentos-host": "h",
              "properties": {"default": {"type": "string", "example": "from a page", "x-agentos-host": "h"},
                             "x-agentos-mark": {"type": "string"}},
              "items": {"default": 1}, "anyOf": [{"examples": [1]}]}
    out = oas.clean(schema, samples=False)
    assert out == {"type": "object", "properties": {"default": {"type": "string"}, "x-agentos-mark": {"type": "string"}},
                   "items": {}, "anyOf": [{}]}
    assert oas.clean(schema, samples=True)["examples"] == [{"a": "from a page"}]


async def test_a_key_gets_only_its_endpoints_and_surfaces(node):
    limited = node.key(endpoints=["open_orders"], surfaces=[SURFACE], ceiling="commit")
    async with node.client(limited) as c:
        doc = valid((await c.get(DOC)).json())
    assert set(doc["paths"]) == {f"/v1/sites/{API}/open_orders", f"/v1/sites/{API}/runs/{{run}}"}
    assert doc["components"]["parameters"]["On"]["schema"]["enum"] == [SURFACE]
    assert OTHER not in json.dumps(doc)


async def test_an_endpoint_above_the_ceiling_is_left_out(node):
    reader = node.key()
    async with node.client(reader) as c:
        doc = valid((await c.get(DOC)).json())
        listing = (await c.get("/v1/sites")).json()["apis"][0]
    assert f"/v1/sites/{API}/send_note" not in doc["paths"]
    assert [e["endpoint"] for e in listing["endpoints"]] == ["open_orders"]


async def test_a_key_that_sees_no_endpoint_gets_the_404_of_a_missing_api(node):
    commit_only = node.key(endpoints=["send_note"])  # read ceiling: nothing it may call
    async with node.client(commit_only) as c:
        assert (await c.get(DOC)).status_code == 404
        assert (await c.get("/v1/sites")).json() == {"apis": []}


async def test_v1_sites_lists_what_the_key_sees_and_only_local_apis(node):
    key = node.key(surfaces=[SURFACE], ceiling="commit")
    async with node.client(key) as c:
        r = await c.get("/v1/sites")
    (api,) = r.json()["apis"]
    assert api["api"] == API and api["version"] == "1.0.0" and api["state"] == "active"
    assert api["surfaces"] == [SURFACE] and api["openapi"] == DOC
    assert {e["endpoint"]: e["methods"] for e in api["endpoints"]} == {"open_orders": ["GET", "POST"],
                                                                       "send_note": ["POST"]}


async def test_a_catalog_connector_is_not_a_site_api(tmp_path):
    from connector_samples import SITES, connector
    from siteapi_http_support import OWNER

    from agentos.runner.connectors.compile import compile_connector
    from agentos.runner.connectors.service import ConnectorService

    async with Node(tmp_path) as n:
        ConnectorService(n.flows, sites=SITES).install(compile_connector(connector(), sites=SITES), by=OWNER)
        key = n.key(surfaces=[SURFACE])
        async with n.client(key) as c:
            assert [a["api"] for a in (await c.get("/v1/sites")).json()["apis"]] == [API]
            assert (await c.get("/v1/sites/social/profile_summary")).status_code == 404
        assert "social" not in n.site_http.catalog()


async def test_round_trip_every_generated_path_answers_on_the_real_app(tmp_path):
    typed = search_endpoint(limit={"type": "integer", "minimum": 1, "default": 5}, exact={"type": "boolean"},
                            tags={"type": "array", "items": {"type": "string"}},
                            note={"type": "string", "description": "free text"})
    async with Node(tmp_path, doc=definition(search=typed)) as n:
        key = n.key(ceiling="commit", surfaces=[SURFACE])
        reader = n.key(surfaces=[SURFACE])  # what a read labels would make the commit after it ask a person
        async with n.client(key) as c, n.client(reader) as read:
            doc = valid((await c.get(DOC)).json())
            seen = {}
            for path, item in doc["paths"].items():
                if "{run}" in path:
                    continue
                for method, op in item.items():
                    kw = {}
                    if method == "get":
                        kw["params"] = {p["name"]: sample(p["schema"]) for p in op["parameters"]
                                        if "name" in p and p.get("in") == "query"}
                    else:
                        body = op["requestBody"]["content"]["application/json"]["schema"]
                        kw["json"] = {k: sample(body["properties"][k]) for k in body.get("required") or []}
                        if any(p.get("name") == "Idempotency-Key" for p in op["parameters"]):
                            kw["headers"] = {"Idempotency-Key": f"round-trip-{op['operationId']}"}
                    r = await (c if op["x-agentos-effect"] == "commit" else read).request(method, path, **kw)
                    assert r.status_code == 200, (method, path, r.text)
                    seen[op["operationId"]] = r.json()["source"]["run"]
            assert len(seen) == 5
            run = seen[f"{API}_send_note"]
            assert (await c.get(f"/v1/sites/{API}/runs/{run}")).status_code == 200


def sample(schema):
    if "enum" in schema:
        return schema["enum"][0]
    return {"integer": 1, "number": 1, "boolean": True, "array": ["a"], "object": {}, "string": "a"}[schema["type"]]


def fv_with(tmp_path, **props):
    flows = FlowService(RunnerStore(tmp_path / "runner.db"), FakeClock())
    fvs = install_for_test(flows, definition(search=search_endpoint(**props)))
    return next(f for f in fvs if f.id.endswith(".search"))


def test_on_is_already_reserved_at_save(tmp_path):
    from agentos.runner.connectors.compile import ConnectorInvalid

    with pytest.raises((ConnectorInvalid, ValueError)):
        fv_with(tmp_path, on={"type": "string"})


def test_coerce_query_takes_a_union_with_null_and_leaves_strings_alone():
    schema = {"properties": {"n": {"type": ["integer", "null"]}, "s": {"type": "string"}, "any": {}}}
    assert oas.coerce_query(schema, [("n", "4"), ("s", "4"), ("any", "4")]) == {"n": 4, "s": "4", "any": "4"}
