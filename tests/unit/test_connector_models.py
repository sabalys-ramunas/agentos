"""The connector format: model validation for documents, operations and bindings."""

import pytest
from connector_samples import commit_op, connector, read_op

from openajar.runner.connectors.compile import ConnectorInvalid, parse_connector
from openajar.runner.connectors.models import Binding, Connector


def _op(**kw):
    op = read_op()
    op.update(kw)
    return op


def _set(doc, path, value):
    cur = doc
    *parts, last = path.split(".")
    for p in parts:
        cur = cur[int(p)] if isinstance(cur, list) else cur[p]
    if value is _DEL:
        del cur[last]
    elif isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value
    return doc


_DEL = object()
OPS = "operations.profile_summary"


def test_the_samples_parse():
    c = parse_connector(connector({"profile_summary": read_op(), "post_photo": commit_op()}))
    assert isinstance(c, Connector)
    assert c.operations["post_photo"].acting_as.source == "cookie"
    assert c.operations["post_photo"].rehearsal_mode == "until_commit"
    assert c.operations["profile_summary"].rehearsal_mode == "full"


@pytest.mark.parametrize(("path", "value", "message"), [
    ("connector", "Social", "lowercase site key"),
    ("version", "1.0", "semver"),
    ("version", "01.0.0", "semver"),
    ("domain", "https://social.example", "registrable domain"),
    ("site", "other", "site must equal connector"),
    ("backends", ["api"], "browser backend only"),
    ("account_languages", ["english"], "two-letter"),
    ("account_languages", ["en", "en"], "twice"),
    ("operations", {}, "at least 1"),
    ("surprise", 1, "Extra inputs"),
    (f"{OPS}.effect", "pay", "out of the v1 catalog"),
    (f"{OPS}.effect", "irreversible", "out of the v1 catalog"),
    (f"{OPS}.input", {"type": "array"}, "type object"),
    (f"{OPS}.input", {"type": "object", "properties": {"acts_as_id": {"type": "string"}}}, "binding param"),
    (f"{OPS}.input", {"type": "object", "properties": {"on": {"type": "string"}}}, "not reserved"),
    (f"{OPS}.input", {"type": "object", "required": ["x"], "properties": {}}, "not declared"),
    (f"{OPS}.output", {"type": "object", "properties": {"x": {"type": "nope"}}}, "not a valid JSON schema"),
    (f"{OPS}.acting_as", {"kind": "account", "source": "url"}, "identity cookie"),
    (f"{OPS}.acting_as", {"kind": "fb_page"}, "source (url or href)"),
    (f"{OPS}.acting_as", {"kind": "fb_page", "source": "url", "pattern": "asset_id=\\d+"}, "capture group"),
    (f"{OPS}.acting_as", {"kind": "li_company", "source": "href", "pattern": "/company/(\\d+)/"}, "locator"),
    (f"{OPS}.acting_as", {"kind": "someone"}, "Input should be"),
    (f"{OPS}.signals", {"benign": ["captcha"], "hold": ["captcha"]}, "both benign and hold"),
    (f"{OPS}.signals", {"hold": ["Not A Name"]}, "lowercase_with_underscores"),
    (f"{OPS}.pace", {"per_day": 0}, "greater than or equal to 1"),
    (f"{OPS}.readback", {"live": {"outputs": {"x": "not a path!"}}}, "saved name and fields"),
    (f"{OPS}.steps", [], "at least 1"),
    (f"{OPS}.steps.0", {"type": "teleport"}, "does not match any of the expected tags"),
    (f"{OPS}.steps.0", {"type": "click", "locator": {"ref": "e12"}}, "snapshot refs"),
    (f"{OPS}.steps.0", {"type": "preflight", "checks": []}, "added by the compiler"),
    (f"{OPS}.steps.0", {"type": "click", "locator": {"role": "button", "attr": {"name": "href"}}},
     "exactly one of equals, prefix, regex"),
    (f"{OPS}.steps.0", {"type": "click", "locator": {"role": "button", "attr": {"name": "x", "regex": "("}}},
     "invalid attr regex"),
    (f"{OPS}.steps.0", {"type": "click", "locator": {"role": "button", "name": "A", "names": {"en": "A"}}},
     "either name or names"),
    (f"{OPS}.steps.0", {"type": "click", "locator": {"css": "button", "names": {"en": "A"}}}, "names need a role"),
    (f"{OPS}.steps.0", {"type": "click", "locator": {"role": "button", "names": {"english": "A"}}}, "two-letter"),
    (f"{OPS}.steps.0", {"type": "extract", "save_as": "items", "items": {"css": "x"}, "fields": {}},
     "Field required"),
    (f"{OPS}.steps.0", {"type": "extract", "save_as": "items", "items": {"css": "x"}, "max_items": 5,
                        "fields": {"id": {"attr": "id", "read": "text"}}}, "exactly one of attr"),
    (f"{OPS}.steps.0", {"type": "for_each", "over": "items", "as": "item", "steps": []}, "Field required"),
])
def test_model_validation_table(path, value, message):
    doc = _set(connector(), path, value)
    with pytest.raises(ConnectorInvalid) as e:
        parse_connector(doc)
    assert message in e.value.message


@pytest.mark.parametrize(("check", "message"), [
    ({"kind": "value_equals", "locator": {"role": "textbox"}}, "set param"),
    ({"kind": "count", "locator": {"role": "img"}}, "needs equals"),
    ({"kind": "count", "equals": 1}, "needs a locator"),
    ({"kind": "acting_as", "param": "caption"}, "takes nothing"),
])
def test_preflight_checks_are_typed(check, message):
    op = commit_op()
    op["commit"]["preflight"] = [check]
    with pytest.raises(ConnectorInvalid) as e:
        parse_connector(connector({"post_photo": op}))
    assert message in e.value.message


def test_commit_request_shape():
    op = commit_op()
    op["commit"]["request"]["url"] = "https://social.example/api/posts"
    with pytest.raises(ConnectorInvalid, match="anchored regex"):
        parse_connector(connector({"post_photo": op}))
    op = commit_op()
    op["commit"]["request"]["text"] = "caption"
    with pytest.raises(ConnectorInvalid, match="form\\|json"):
        parse_connector(connector({"post_photo": op}))


# ---------------------------------------------------------------- bindings


def test_binding_schema_and_params():
    b = Binding.model_validate({"surface": "biz-a/facebook-page", "connector": "facebook", "lane": "personal/facebook",
                                "acts_as": {"kind": "fb_page", "id": "100089000000001", "name": "biz-a"}})
    assert b.params() == {"acts_as_id": "100089000000001", "account_language": "en"}
    api = Binding.model_validate({"surface": "biz-c/shopify", "connector": "shopify", "backend": "api",
                                  "credential": "vault:biz-c/shopify-admin-token",
                                  "acts_as": {"kind": "account", "user_id": "42"},
                                  "fallback": {"backend": "browser", "lane": "biz-c/shopify"}})
    assert api.lane is None and api.fallback.lane == "biz-c/shopify"


@pytest.mark.parametrize(("data", "message"), [
    ({"acts_as": {"kind": "account", "user_id": "1"}}, "names the lane"),
    ({"lane": "x", "acts_as": {"kind": "account", "user_id": "1"}}, "lane id"),
    ({"lane": "a/b", "acts_as": {"kind": "account"}}, "pins the account's user_id"),
    ({"lane": "a/b", "acts_as": {"kind": "fb_page", "user_id": "1"}}, "names the asset id"),
    ({"backend": "api", "acts_as": {"kind": "account", "user_id": "1"}}, "vault credential"),
    ({"backend": "api", "credential": "shpat_123", "acts_as": {"kind": "account", "user_id": "1"}},
     "vault reference"),
    ({"surface": "nope", "lane": "a/b", "acts_as": {"kind": "account", "user_id": "1"}}, "surface must look"),
])
def test_binding_validation(data, message):
    base = {"surface": "biz-a/social", "connector": "social", **data}
    with pytest.raises(ValueError, match=message):
        Binding.model_validate(base)


def test_binding_must_match_the_operation():
    c = parse_connector(connector())
    ok = Binding.model_validate({"surface": "biz-a/social", "connector": "social", "lane": "biz-a/social",
                                 "acts_as": {"kind": "account", "user_id": "biz-a"}})
    ok.check_for(c, "profile_summary")
    page = ok.model_copy(update={"acts_as": ok.acts_as.model_copy(update={"kind": "fb_page", "user_id": None,
                                                                           "id": "1"})})
    with pytest.raises(ValueError, match="acts as account"):
        page.check_for(c, "profile_summary")
    with pytest.raises(ValueError, match="no operation"):
        ok.check_for(c, "delete_everything")
    with pytest.raises(ValueError, match="is in de"):
        ok.model_copy(update={"account_language": "de"}).check_for(c, "profile_summary")
